"""Ordered Neural Rendering workflow with Smart previews and rolling exports."""

from __future__ import annotations

import subprocess
import os
import math
import shutil
import tempfile
import threading
import time
from contextlib import nullcontext
from collections import OrderedDict
from dataclasses import asdict, dataclass, replace
from pathlib import Path

import numpy as np

from ..core import app_log, ffmpeg
from ..core.batch_progress import BatchProgress
from ..core.cache_cleanup import prune_preview_cache
from ..core.cache_video import cache_ffmpeg_args, cache_video_format
from ..core.disk_paths import OutputFile, prepare_output_dir
from ..core.dlss_modes import dlss_output_size
from ..core.ffmpeg.audio import plan_audio_streams
from ..core.ffmpeg.encoder import _codec_command
from ..core.ffmpeg.preview import (decode_preview_frame, extract_preview_subclip,
                                   tone_map_hdr_preview_frame)
from ..core.gpu_detection import detect_gpus
from ..core.jobs import Cancelled, JobController
from ..core.naming import output_filename, unique_output_path
from ..core.paths import (FFMPEG, JOBS, OUTPUTS, PREVIEW_CACHE,
                          DLSSSR_BRIDGE, DLSSSR_RUNTIME, DLSSNR_BRIDGE,
                          DLSSG_DIR, RUNTIME,
                          NEURAL_RUNTIME)
from ..core.nr_control_mask import mask_selection
from ..core.runtime import resolve_output_size
from ..frame_interpolation.models import FrameInterpolationOptions, resolve_target_rate
from ..frame_interpolation.processor import interpolate_video
from ..neural_rendering.image.batch import convert_images
from ..neural_rendering.image.decoder import _open_pillow_source, decode_image
from ..neural_rendering.image.encoder import _encode_image
from ..neural_rendering.image.models import IMAGE_EXTENSIONS, ImageConversionOptions
from ..neural_rendering.video.models import ConversionOptions
from ..neural_rendering.video.processor import convert_video
from ..settings.models import (UISettings, IMAGE_SCALING_FILTERS, VIDEO_SCALING_FILTERS,
                               IMAGE_16BIT_FORMATS,
                               validate_stage_layout)
from ..upscale.image.models import options_from_settings as image_upscale_options, output_size as image_upscale_size
from ..upscale.image.processor import upscale_image
from ..upscale.video.models import options_from_settings as video_upscale_options, output_size as video_upscale_size
from ..upscale.video.processor import upscale_video
from .coloring import (apply_cube_lut, compose_cube_lut, has_lut_adjustments,
                       identity_cube_lut, load_cube_lut, match_image_colors, save_cube_lut)
from .cas_sharpening import sharpen_image, sharpen_video
from .grain import grain_image, grain_video
from ..core.ffmpeg.grain import GRAIN_FIELDS, GrainOptions
from .preview_cache import PreviewStageCache, cache_key, file_identity, optional_file_identity

LOSSLESS_VIDEO = "FFV1 Lossless RGB 10-bit"
_FRAME_PREVIEW_CACHE_LIMIT = 128 * 1024 * 1024
_frame_preview_cache: OrderedDict[str, np.ndarray] = OrderedDict()
_frame_preview_cache_bytes = 0
_frame_preview_cache_lock = threading.RLock()

def _cached_frame_preview(key: str) -> np.ndarray | None:
    with _frame_preview_cache_lock:
        pixels = _frame_preview_cache.get(key)
        if pixels is None:
            return None
        _frame_preview_cache.move_to_end(key)
    return pixels.copy()

def _remember_frame_preview(key: str, pixels: np.ndarray) -> None:
    global _frame_preview_cache_bytes

    if pixels.nbytes > _FRAME_PREVIEW_CACHE_LIMIT:
        return
    retained = pixels.copy()
    with _frame_preview_cache_lock:
        previous = _frame_preview_cache.pop(key, None)
        if previous is not None:
            _frame_preview_cache_bytes -= previous.nbytes
        while (_frame_preview_cache and
               _frame_preview_cache_bytes + retained.nbytes > _FRAME_PREVIEW_CACHE_LIMIT):
            _, evicted = _frame_preview_cache.popitem(last=False)
            _frame_preview_cache_bytes -= evicted.nbytes
        _frame_preview_cache[key] = retained
        _frame_preview_cache_bytes += retained.nbytes

STAGE_LABELS = {
    "neural_model": "DLSS Neural Rendering",
    "scale_method": "Scaling",
    "dlss_super_resolution": "DLSS Super Resolution",
    "super_resolution": "RTX Super Resolution",
    "rtx_video_hdr": "RTX Video HDR",
    "frame_generation": "DLSS Frame Generation",
    "coloring": "Coloring",
    "cas_sharpening": "Sharpening",
    "grain": "Grain",
}

_NR_FIELDS = (
    "nr_style", "nr_intensity", "nr_passes", "local_tone_strength",
    "local_structure_strength", "skin_structure_strength",

     "automatic_mask",
)
_LUT_FIELDS = (
    "lut_resolution", "lut_mix", "lut_exposure", "lut_contrast",
    "lut_highlights", "lut_shadows", "lut_whites", "lut_blacks",
    "lut_midtones", "lut_temperature", "lut_tint", "lut_hue",
    "lut_vibrance", "lut_saturation",
)
_IMAGE_VSR_FIELDS = (
    "upscale_image_vsr_quality", "upscale_image_size_mode",
    "upscale_image_scale_factor", "upscale_image_width",
    "upscale_image_height", "upscale_image_aspect_lock",
)
_VIDEO_VSR_FIELDS = (
    "upscale_vsr_quality", "upscale_size_mode",
    "upscale_scale_factor", "upscale_width", "upscale_height",
    "upscale_aspect_lock",
)
_VIDEO_HDR_FIELDS = (
    "upscale_hdr_contrast",
    "upscale_hdr_saturation", "upscale_hdr_middle_gray",
    "upscale_hdr_peak_luminance", "upscale_hdr_precision",
)

def _preview_runtime_identity() -> list[dict | None]:
    # A replaced processing engine must never inherit intermediates produced
    # by the previous engine, including when the app is updated in place.
    source_root = Path(__file__).resolve().parents[1]
    return [optional_file_identity(path) for path in
            (Path(__file__), Path(__file__).with_name("rolling_workflow.py"),
             Path(__file__).with_name("rolling_pass_worker.py"),
             Path(__file__).with_name("rolling_passes.py"),
             Path(__file__).with_name("pass_delivery.py"),
             source_root / "core" / "pass_cache.py",
             source_root / "core" / "frame_blocks.py",
             Path(__file__).with_name("coloring.py"),
             Path(__file__).with_name("cas_sharpening.py"),
             Path(__file__).with_name("grain.py"),
             source_root / "core" / "ffmpeg" / "preview.py",
             source_root / "core" / "ffmpeg" / "frames.py",
             source_root / "core" / "ffmpeg" / "encoder.py",
             source_root / "core" / "ffmpeg" / "mux.py",
             source_root / "core" / "ffmpeg" / "vulkan.py",
             source_root / "core" / "ffmpeg" / "filters.py",
             source_root / "core" / "ffmpeg" / "sharpening.py",
             source_root / "core" / "ffmpeg" / "grain.py",
             source_root / "core" / "dlss_bridge.py",
             source_root / "core" / "neural_bridge.py",
             source_root / "core" / "runtime.py",
             source_root / "upscale" / "image" / "processor.py",
             source_root / "upscale" / "video" / "processor.py",
             source_root / "upscale" / "video" / "preview.py",
             source_root / "upscale" / "video" / "dlss_pipeline.py",
             source_root / "upscale" / "video" / "cuda_pipeline.py",
             source_root / "upscale" / "video" / "native.py",
             source_root / "upscale" / "video" / "media.py",
             source_root / "upscale" / "video" / "host_pipeline.py",
             source_root / "neural_rendering" / "image" / "batch.py",
             source_root / "neural_rendering" / "video" / "processor.py",
             source_root / "neural_rendering" / "video" / "cuda_pipeline.py",
             source_root / "neural_rendering" / "video" / "fast_preview.py",
             source_root / "frame_interpolation" / "processor.py",
             FFMPEG, DLSSSR_BRIDGE, DLSSSR_RUNTIME, DLSSNR_BRIDGE,
             NEURAL_RUNTIME,
             RUNTIME / "rtx_video" / "neuroframe_engine_upscaling.dll",
             RUNTIME / "rtx_video" / "nvngx_vsr.dll",
             RUNTIME / "rtx_video" / "nvngx_truehdr.dll",
             DLSSG_DIR / "nvngx_dlssg.dll")]

def _stage_cache_settings(settings: UISettings, mode: str, stage: str, hdr: bool) -> dict:
    """Only settings consumed by this card belong to its cache key."""
    fields: tuple[str, ...]
    extras: dict = {}
    if stage == "cas_sharpening":
        fields = ("cas_sharpness", "sharpening_method")
    elif stage == "grain":
        fields = tuple(name for name in GRAIN_FIELDS if mode == "Video" or name != "grain_animated")
        extras["hdr_input"] = hdr
    elif stage == "neural_model":
        fields = _NR_FIELDS + (("video_gpu_uuid", "nr_optical_flow_quality") if mode == "Video" else ())
        fields += ("ai_gpu_uuid",)
        selected_mask = mask_selection(settings.nr_mask)
        extras["mask"] = (None if selected_mask is None else
                          {**selected_mask.state(), "file": file_identity(selected_mask.path)})
        extras["hdr_input"] = hdr
    elif stage == "scale_method":
        fields = ("upscaling_factor", "image_scaling_filter" if mode == "Image"
                  else "video_scaling_filter")
    elif stage == "dlss_super_resolution":
        fields = (("upscale_image_dlss_mode", "upscale_image_dlss_preset", "ai_gpu_uuid")
                  if mode == "Image" else
                  ("upscale_dlss_mode", "upscale_dlss_preset", "upscale_optical_flow_quality", "ai_gpu_uuid", "video_gpu_uuid"))
    elif stage == "super_resolution":
        fields = (("ai_gpu_uuid",) + _IMAGE_VSR_FIELDS if mode == "Image" else
                  ("ai_gpu_uuid", "video_gpu_uuid") + _VIDEO_VSR_FIELDS)
    elif stage == "rtx_video_hdr":
        fields = ("ai_gpu_uuid", "video_gpu_uuid") + _VIDEO_HDR_FIELDS
    elif stage == "frame_generation":
        fields = ("frame_interpolation_target_fps", "frame_interpolation_engine", "frame_interpolation_optical_flow_quality",
                  "ai_gpu_uuid", "video_gpu_uuid")
        extras["hdr_input"] = hdr
    elif stage == "coloring":
        fields = _LUT_FIELDS if mode == "Video" or settings.coloring_mode == "LUT" else ()
        if mode == "Image":
            fields += ("coloring_mode",)
            if settings.coloring_mode == "Color Match":
                fields += ("color_match_source",)
                if settings.color_match_source == "Selected Image":
                    extras["reference"] = file_identity(settings.color_match_reference)
            else:
                extras["lut"] = (file_identity(settings.lut_path) if settings.lut_path else None)
        else:
            extras["lut"] = (file_identity(settings.lut_path) if settings.lut_path else None)
    else:
        raise ValueError(f"Unknown processing card: {stage}")
    return {"ffmpeg_device": settings.ffmpeg_device, **{name: getattr(settings, name) for name in fields}, **extras}

def _video_scaling_filter(width: int, height: int, method: str) -> str:
    from ..core.ffmpeg.filters import scaling_filter
    return "ve_gpu," + scaling_filter(width, height, method)

@dataclass(frozen=True)
class PipelineEstimate:
    width: int
    height: int
    fps: float
    hdr: bool

@dataclass(frozen=True)
class PipelineSuccess:
    input_path: str
    output_path: str

@dataclass(frozen=True)
class PipelineFailure:
    input_path: str
    error: str
    cancelled: bool = False

@dataclass(frozen=True)
class PipelineBatchResult:
    successes: list[PipelineSuccess]
    failures: list[PipelineFailure]
    cancelled: bool

def enabled_stages(settings: UISettings, mode: str) -> tuple[str, ...]:
    order = settings.image_stage_order if mode == "Image" else settings.video_stage_order
    enabled = settings.image_enabled_stages if mode == "Image" else settings.video_enabled_stages
    validate_stage_layout(order, enabled, video=mode == "Video")
    return tuple(stage for stage in order if stage in enabled)

def _preferred_native_codec(settings: UISettings, estimate: PipelineEstimate,
                             stages: tuple[str, ...]) -> UISettings:
    """Keep a native NVIDIA pipeline on CUDA when the encoder is automatic."""
    from ..core.gpu_selection import prefer_cuda_video

    if (not prefer_cuda_video(settings.ffmpeg_device, settings.ai_gpu_uuid,
                              settings.video_gpu_uuid) or
            settings.codec not in {"H.264", "H.265", "AV1"} or
            not set(stages) & {"neural_model", "dlss_super_resolution",
                               "super_resolution", "rtx_video_hdr", "frame_generation"}):
        return settings
    try:
        from ..core.gpu_selection import detect_gpu

        gpu = detect_gpu(settings.ai_gpu_uuid)
        uuid = str(gpu["uuid"])
        if settings.video_gpu_uuid not in {"auto", uuid}:
            return settings
        codec = settings.codec + " (NVIDIA NVENC)"
        ffmpeg.resolve_video_gpu((gpu,), uuid, codec, estimate.width, estimate.height)
    except (OSError, RuntimeError, ValueError):
        return settings
    app_log.info("video-render", f"automatic NVIDIA NVENC selected for {settings.codec}")
    return replace(settings, codec=codec, video_gpu_uuid=uuid)

def preview_settings_key(settings: UISettings, mode: str, *, hdr: bool = False) -> str:
    """Identify the active pipeline, ignoring controls in disabled cards."""
    stages = []
    for stage in enabled_stages(settings, mode):
        stages.append((stage, _stage_cache_settings(settings, mode, stage, hdr)))
        if stage == "rtx_video_hdr":
            hdr = True
    return cache_key({"mode": mode, "stages": stages})

def estimate_pipeline(width: int, height: int, fps: float, hdr: bool,
                      settings: UISettings, mode: str) -> PipelineEstimate:
    """Preflight every enabled stage in its actual execution order."""
    if width < 1 or height < 1:
        raise ValueError("Input dimensions are unavailable.")
    if mode == "Video" and fps <= 0:
        raise ValueError("Input frame rate is unavailable; video export needs a known FPS.")
    for stage in enabled_stages(settings, mode):
        if stage == "grain":
            GrainOptions.from_settings(settings, video=mode == "Video").validate()
        elif stage == "cas_sharpening":
            if mode == "Video" and hdr and settings.cas_sharpness:
                raise ValueError("Sharpening requires SDR video at this position. Move it before HDR conversion.")
        elif stage == "neural_model":
            if min(width, height) < 64:
                raise ValueError("DLSS Neural Rendering requires at least 64×64 input.")
            resolve_output_size(width, height, 1.0)
        elif stage == "scale_method":
            selected_filter = settings.image_scaling_filter if mode == "Image" else settings.video_scaling_filter
            choices = IMAGE_SCALING_FILTERS if mode == "Image" else VIDEO_SCALING_FILTERS
            if selected_filter not in choices:
                raise ValueError(f"Unknown {mode.lower()} scaling filter: {selected_filter!r}.")
            if mode == "Video" and hdr:
                raise ValueError("Scaling cannot process HDR video at this position. Move it before HDR conversion.")
            width, height = resolve_output_size(width, height, settings.upscaling_factor)
        elif stage == "dlss_super_resolution":
            if mode == "Video" and hdr:
                raise ValueError("DLSS Super Resolution cannot process HDR video at this position. Move it before HDR conversion.")
            dlss_mode = settings.upscale_image_dlss_mode if mode == "Image" else settings.upscale_dlss_mode
            width, height = dlss_output_size(width, height, dlss_mode, even=mode == "Video")
        elif stage == "super_resolution":
            if mode == "Image":
                options = replace(image_upscale_options(settings), engine="RTX Video Super Resolution")
                width, height = image_upscale_size(width, height, options)
            else:
                if hdr:
                    raise ValueError("RTX Super Resolution cannot process HDR video at this position. Move it before HDR conversion.")
                opts = _video_upscale_stage_options(settings, stage)
                opts.validate(for_render=True)
                width, height, _ = video_upscale_size(width, height, opts)
        elif stage == "rtx_video_hdr":
            if mode != "Video":
                raise ValueError("RTX Video HDR accepts video only.")
            if hdr:
                raise ValueError("RTX Video HDR requires SDR video at this position.")
            opts = _video_upscale_stage_options(settings, stage)
            opts.validate(for_render=True)
            width, height, _ = video_upscale_size(width, height, opts)
            hdr = True
        elif stage == "frame_generation":
            if mode != "Video":
                raise ValueError("DLSS Frame Generation accepts video only.")
            target_fps = float(resolve_target_rate(settings.frame_interpolation_target_fps))
            if fps <= 0:
                raise ValueError("Frame Generation requires a known input frame rate.")
            if target_fps <= fps:
                raise ValueError(
                    f"DLSS Frame Generation target {target_fps:g} FPS must exceed the "
                    f"{fps:g} FPS entering this card. Choose a higher target FPS."
                )
            fps = target_fps
        elif stage == "coloring":
            if mode == "Video" or settings.coloring_mode == "LUT":
                if settings.lut_path and not Path(settings.lut_path).is_file():
                    raise ValueError("The selected .cube LUT is unavailable.")
            elif settings.color_match_source == "Selected Image":
                if not settings.color_match_reference:
                    raise ValueError("Choose a reference image in Coloring before rendering.")
                if not Path(settings.color_match_reference).is_file():
                    raise ValueError("The selected Color Match reference image is unavailable.")
        else:
            raise ValueError(f"Unknown processing card: {stage}")
    if mode == "Video":
        if (width % 2 or height % 2) and settings.codec != LOSSLESS_VIDEO:
            raise ValueError(
                f"Video export requires even dimensions; this sequence ends at {width}×{height}. "
                "Choose an even output size in Scaling, DLSS Super Resolution, or RTX Super Resolution."
            )
        if hdr and not settings.hdr_mode:
            raise ValueError("HDR output requires 10-bit HDR Mode in Export Settings.")
        if hdr and not ffmpeg.hdr_mode_supported(settings.codec):
            raise ValueError("HDR output requires a 10-bit capable codec in Export Settings.")
        ffmpeg.resolve_container(settings.codec, settings.container)
    return PipelineEstimate(width, height, fps, hdr)

def preflight_source(source: Path, settings: UISettings, mode: str,
                     *, metadata: dict | None = None) -> PipelineEstimate:
    if not source.is_file():
        raise FileNotFoundError(source)
    if "coloring" in enabled_stages(settings, mode) and (mode == "Video" or settings.coloring_mode == "LUT"):
        if settings.lut_path:
            load_cube_lut(settings.lut_path)
    if mode == "Image":
        if ("coloring" in enabled_stages(settings, mode)
                and settings.coloring_mode == "Color Match"
                and settings.color_match_source == "Selected Image"):
            if not settings.color_match_reference:
                raise ValueError("Choose a reference image in Coloring before rendering.")
            reference = Path(settings.color_match_reference)
            if not reference.is_file():
                raise ValueError(f"Color Match reference image is unavailable: {reference}")
            try:
                reference_image, _ = _open_pillow_source(reference)
                reference_image.close()
            except Exception as exc:
                raise ValueError(f"Color Match reference image cannot be opened: {reference}") from exc
        image, _ = _open_pillow_source(source)
        try:
            width, height = image.size
            if image.getexif().get(274) in {5, 6, 7, 8}:
                width, height = height, width
        finally:
            image.close()
        return estimate_pipeline(width, height, 0.0, False, settings, mode)
    native_fg = ("frame_generation" in enabled_stages(settings, mode)
                 and settings.frame_interpolation_engine == "Native DLSSG")
    metadata = metadata or ffmpeg.probe_video(source, count_mode="metadata",
                                              inspect_timestamps=native_fg)
    result = estimate_pipeline(int(metadata["width"]), int(metadata["height"]),
                               float(metadata["fps"]), bool(metadata.get("hdr")), settings, mode)
    if "(NVIDIA NVENC)" in settings.codec:
        ffmpeg.resolve_video_gpu(detect_gpus(), settings.video_gpu_uuid,
                                 settings.codec, result.width, result.height)
    if native_fg:
        from ..frame_interpolation.capabilities import probe_frame_interpolation_capabilities
        from ..frame_interpolation.scheduler import choose_interpolation_plan

        caps = probe_frame_interpolation_capabilities(settings.ai_gpu_uuid)
        if not caps.available:
            raise ValueError("DLSS Frame Generation is unavailable. " + caps.detail)
        choose_interpolation_plan(metadata["rate"],
                                  resolve_target_rate(settings.frame_interpolation_target_fps),
                                  settings.frame_interpolation_engine,
                                  caps.native_multiplier, cfr=bool(metadata["cfr"]))
    return result

def preflight_capabilities(settings: UISettings, mode: str,
                           controller: JobController | None = None,
                           stages_override: tuple[str, ...] | None = None,
                           *, require_filter: bool = True) -> None:
    """Reject unavailable processing hardware before writing any stage output."""
    stages = stages_override if stages_override is not None else enabled_stages(settings, mode)
    if not stages:
        return
    if require_filter:
        from ..core.ffmpeg.vulkan import filter_device
        filter_device(settings.ffmpeg_device)
    needs_ai_gpu = bool(set(stages) & {"neural_model", "dlss_super_resolution", "super_resolution", "rtx_video_hdr", "frame_generation"})
    if needs_ai_gpu:
        from ..core.gpu_selection import detect_gpu

        detect_gpu(settings.ai_gpu_uuid)
    if set(stages) & {"super_resolution", "rtx_video_hdr"}:
        from ..upscale.video.native import probe_capabilities

        caps = probe_capabilities(settings.ai_gpu_uuid, controller=controller)
        if "super_resolution" in stages and not caps.vsr.get("available"):
            raise ValueError("RTX Super Resolution requires an available RTX Video Super Resolution runtime.")
        if "rtx_video_hdr" in stages and not caps.hdr.get("available"):
            raise ValueError("RTX Video HDR requires an available RTX Video HDR runtime.")
    if "frame_generation" in stages:
        from ..frame_interpolation.capabilities import probe_frame_interpolation_capabilities

        caps = probe_frame_interpolation_capabilities(settings.ai_gpu_uuid)
        if not caps.available:
            raise ValueError("DLSS Frame Generation is unavailable. " + caps.detail)

def _run_command(command: list[str], controller: JobController, label: str,
                 *, cwd: Path | None = None, progress=None, metadata: dict | None = None) -> None:
    from ..core.ffmpeg.vulkan import run
    run(command, controller, label, cwd=cwd, progress=progress, metadata=metadata)

def _neural_image_options(settings: UISettings) -> ImageConversionOptions:
    return ImageConversionOptions(
        ai_gpu_uuid=settings.ai_gpu_uuid, nr_style=settings.nr_style,
        nr_intensity=settings.nr_intensity, nr_passes=settings.nr_passes,
        local_tone_strength=settings.local_tone_strength,
        local_structure_strength=settings.local_structure_strength,
        skin_structure_strength=settings.skin_structure_strength,

         nr_mask=settings.nr_mask,
        automatic_mask=settings.automatic_mask, upscaling_factor=1.0,
        scale_method="Standard", output_format="TIFF", quality=100,
        preserve_metadata=False,
    )

def _neural_video_options(settings: UISettings, hdr: bool) -> ConversionOptions:
    from ..core.gpu_selection import prefer_cuda_video

    cache_codec, cache_container, _ = cache_video_format(settings.cache_codec)
    return ConversionOptions(
        ai_gpu_uuid=settings.ai_gpu_uuid, video_gpu_uuid=settings.video_gpu_uuid,
        prefer_nvenc=prefer_cuda_video(settings.ffmpeg_device, settings.ai_gpu_uuid,
                                      settings.video_gpu_uuid),
        nr_style=settings.nr_style, nr_intensity=settings.nr_intensity,
        nr_passes=settings.nr_passes, local_tone_strength=settings.local_tone_strength,
        nr_optical_flow_quality=settings.nr_optical_flow_quality,
        local_structure_strength=settings.local_structure_strength,
        skin_structure_strength=settings.skin_structure_strength,

         nr_mask=settings.nr_mask,
        automatic_mask=settings.automatic_mask, upscaling_factor=1.0,
        scale_method="Standard", codec=cache_codec, container=cache_container,
        quality="Auto (Default)", preserve_hdr=hdr,
    )

def _video_upscale_stage_options(settings: UISettings, stage: str):
    # Each card owns its operation. Legacy combined-card switches must not
    # enable HDR inside VSR or cause the standalone HDR card to resize frames.
    cache_codec, cache_container, _ = cache_video_format(settings.cache_codec)
    return replace(video_upscale_options(settings),
                   engine="DLSS" if stage == "dlss_super_resolution" else "RTX Video Super Resolution",
                   vsr_enabled=stage != "rtx_video_hdr", hdr_enabled=stage == "rtx_video_hdr",
                   codec=cache_codec, container=cache_container, quality="Auto (Default)")

def _image_stage(source: Path, stage: str, settings: UISettings,
                 directory: Path, controller: JobController, progress) -> Path:
    if stage == "grain":
        return grain_image(source, settings, directory, controller, progress)
    if stage == "cas_sharpening":
        return sharpen_image(source, settings, directory, controller, progress)
    if stage == "neural_model":
        result = convert_images([source], _neural_image_options(settings), progress,
                                output_dir=directory, controller=controller,
                                generate_previews=False, create_zip=False)
        if result.failures:
            raise RuntimeError(result.failures[0].error)
        return Path(result.successes[0].output_path)
    if stage == "scale_method":
        from ..core.runtime import resize_fit
        decoded = decode_image(source)
        height, width = decoded.rgba.shape[:2]
        target_width, target_height = resolve_output_size(width, height, settings.upscaling_factor)
        output = directory / "scaled.tiff"
        _encode_image(output, resize_fit(decoded.rgba, target_width, target_height,
                                         interpolation=settings.image_scaling_filter, controller=controller),
                      ImageConversionOptions(output_format="TIFF", preserve_metadata=False),
                      {}, generate_preview=False, controller=controller,
                      has_transparency=decoded.alpha is not None)
        return output
    if stage not in {"dlss_super_resolution", "super_resolution"}:
        raise ValueError(f"Unknown image processing card: {stage}")
    opts = replace(image_upscale_options(settings),
                   engine="DLSS" if stage == "dlss_super_resolution" else "RTX Video Super Resolution",
                   output_format="TIFF", quality=100)
    result = upscale_image(source, opts, progress, output_dir=directory,
                           controller=controller, generate_previews=False)
    return Path(result.output_path)

def _coloring_image_stage(current: Path, input_image: Path, settings: UISettings,
                          directory: Path, controller: JobController, progress) -> Path:
    rendered = decode_image(current)
    if settings.coloring_mode == "LUT":
        lut = load_cube_lut(settings.lut_path) if settings.lut_path else identity_cube_lut()
        if has_lut_adjustments(settings):
            lut = compose_cube_lut(lut, settings, settings.lut_resolution)
        matched = apply_cube_lut(rendered.rgba, lut, controller=controller)
        message = "Applying LUT"
    else:
        reference = (input_image if settings.color_match_source == "Input Image"
                     else Path(settings.color_match_reference))
        if current.resolve() == reference.resolve():
            return current
        target = decode_image(reference)
        matched = match_image_colors(rendered.rgba, target.rgba)
        message = "Matching colors"
    if controller.cancel.is_set():
        raise Cancelled("Render stopped by user.")
    progress(0.8, message)
    output = directory / "color-matched.tiff"
    _encode_image(output, matched,
                  ImageConversionOptions(output_format="TIFF", preserve_metadata=False),
                  {}, generate_preview=False, controller=controller,
                  has_transparency=rendered.alpha is not None)
    progress(1.0, "Coloring complete")
    return output

def _coloring_video_stage(source: Path, settings: UISettings, directory: Path,
                          controller: JobController, progress) -> Path:
    # Work in the stage directory so FFmpeg can use a simple relative LUT
    # filename, including when the user's .cube path contains spaces or colons.
    staged_lut = directory / "reference.cube"
    if settings.lut_path and not has_lut_adjustments(settings):
        shutil.copyfile(settings.lut_path, staged_lut)
    else:
        save_cube_lut(staged_lut, settings.lut_path or None, settings, controller=controller)
    load_cube_lut(staged_lut)
    metadata = ffmpeg.probe_video(source, count_mode="metadata", controller=controller)
    output = directory / ("coloring" + cache_video_format(settings.cache_codec)[2])
    progress(0.0, "Applying LUT to video")
    _run_command([str(FFMPEG), "-hide_banner", "-loglevel", "error", "-y",
                  "-i", str(source), "-map", "0:v:0", "-map_metadata", "0", "-an",
                  "-vf", "ve_gpu,libplacebo=lut=reference.cube:lut_type=native:deband=0:dithering=-1:format=gbrp10le",
                  *cache_ffmpeg_args(settings.cache_codec, metadata),
                  "-fps_mode", "passthrough",
                  str(output)], controller, "Applying LUT to video", cwd=directory,
                 progress=progress, metadata=metadata)
    progress(1.0, "LUT complete")
    return output

def _preview_video_stage(source: Path, stage: str, settings: UISettings, hdr: bool,
                         directory: Path, controller: JobController, progress) -> Path:
    """Create a lossless intermediate for the Smart preview cache."""
    from .pass_process import run_request
    codec, container, suffix = cache_video_format(settings.cache_codec)
    output = directory / ("stage-preview" + suffix)
    preview_settings = replace(settings, codec=codec, container=container, hdr_mode=hdr)
    def event(values):
        if progress and values.get("event") == "preview-progress":
            progress(values["progress"], values["message"])
    result = run_request(dict(operation="preview-stage", source=str(source.resolve()), stage=stage,
                              settings=asdict(preview_settings), destination=str(output.resolve())),
                         directory / "stage-scope", controller, on_event=event)
    return Path(result["path"])

def _legacy_preview_video_stage(source: Path, stage: str, settings: UISettings, hdr: bool,
                                directory: Path, controller: JobController, progress) -> Path:
    """Compatibility adapter retained for external callers of old stage codecs."""
    if stage == "grain":
        return grain_video(source, settings, directory, controller, progress)
    if stage == "cas_sharpening":
        return sharpen_video(source, settings, directory, controller, progress)
    if stage == "neural_model":
        options = _neural_video_options(settings, hdr)
        result = convert_video(source, options, progress,
                               output_dir=directory, controller=controller)
    elif stage == "scale_method":
        metadata = ffmpeg.probe_video(source, count_mode="metadata", controller=controller)
        width, height = resolve_output_size(int(metadata["width"]), int(metadata["height"]),
                                            settings.upscaling_factor)
        output = directory / ("scaled" + cache_video_format(settings.cache_codec)[2])
        filter_graph = _video_scaling_filter(width, height, settings.video_scaling_filter)
        _run_command([str(FFMPEG), "-hide_banner", "-loglevel", "error", "-y",
                      "-i", str(source),
                      "-map", "0:v:0", "-map_metadata", "0", "-vf", filter_graph,
                      *cache_ffmpeg_args(settings.cache_codec, metadata), str(output)],
                     controller, f"{settings.video_scaling_filter} scaling",
                     progress=progress, metadata=metadata)
        return output
    elif stage in {"dlss_super_resolution", "super_resolution", "rtx_video_hdr"}:
        opts = _video_upscale_stage_options(settings, stage)
        result = upscale_video(source, opts, progress, output_dir=directory, controller=controller)
    elif stage == "frame_generation":
        cache_codec, cache_container, _ = cache_video_format(settings.cache_codec)
        opts = FrameInterpolationOptions(
            ai_gpu_uuid=settings.ai_gpu_uuid, video_gpu_uuid=settings.video_gpu_uuid,
            target_fps=settings.frame_interpolation_target_fps,
            engine=settings.frame_interpolation_engine,
            optical_flow_quality=settings.frame_interpolation_optical_flow_quality,
            codec=cache_codec, container=cache_container,
            quality="Auto (Default)",
            hdr_mode=hdr,
        )
        result = interpolate_video(source, opts, progress, output_dir=directory,
                                   controller=controller)
    else:
        raise ValueError(f"Unknown video processing card: {stage}")
    return Path(result.output_path)

def _render_single_video_stage(source: Path, stage: str, settings: UISettings,
                               hdr: bool, destination: Path, directory: Path,
                               controller: JobController, progress) -> str:
    """Deliver a native stage with the requested codec in one processing pass."""
    container = ffmpeg.resolve_container(settings.codec, settings.container)

    def report(value, message):
        if progress:
            progress(min(.98, max(0.0, float(value)) * .98),
                     f"Stage 1 of 1 · {STAGE_LABELS[stage]}: {message}")

    if stage == "neural_model":
        options = replace(_neural_video_options(settings, hdr),
                          codec=settings.codec, container=container,
                          quality=settings.quality)
        result = convert_video(source, options, report, output_dir=directory,
                               controller=controller)
    elif stage in {"dlss_super_resolution", "super_resolution", "rtx_video_hdr"}:
        options = replace(_video_upscale_stage_options(settings, stage),
                          codec=settings.codec, container=container,
                          quality=settings.quality)
        result = upscale_video(source, options, report, output_dir=directory,
                               controller=controller)
    elif stage == "frame_generation":
        options = FrameInterpolationOptions(
            ai_gpu_uuid=settings.ai_gpu_uuid, video_gpu_uuid=settings.video_gpu_uuid,
            target_fps=settings.frame_interpolation_target_fps,
            engine=settings.frame_interpolation_engine,
            optical_flow_quality=settings.frame_interpolation_optical_flow_quality,
            codec=settings.codec, container=container, quality=settings.quality,
            hdr_mode=hdr,
        )
        result = interpolate_video(source, options, report, output_dir=directory,
                                   controller=controller)
    else:
        raise ValueError(f"Unknown native video stage: {stage}.")

    _publish_video(Path(result.output_path), destination, controller)
    if progress:
        progress(1.0, "Export complete")
    return str(destination)

def _image_at_bit_depth(rgba: np.ndarray, settings: UISettings) -> np.ndarray:
    """Convert the processed pixels to the requested delivery depth."""
    depth = settings.image_bit_depth
    if depth == 16:
        if settings.image_format not in IMAGE_16BIT_FORMATS:
            raise ValueError("16-bit image output is available for PNG and TIFF only.")
        if rgba.dtype == np.uint8:
            return rgba.astype(np.uint16) * 257
        return rgba
    if depth == 8:
        if rgba.dtype == np.uint16:
            return ((rgba.astype(np.uint32) + 128) // 257).astype(np.uint8)
        return rgba
    raise ValueError("Image bit depth must be 8 or 16 bits.")

def _export_image(current: Path, destination: Path,
                  settings: UISettings, controller: JobController) -> None:
    rendered = decode_image(current)
    pixels = _image_at_bit_depth(rendered.rgba, settings)
    opts = ImageConversionOptions(output_format=settings.image_format,
                                  quality=settings.image_quality,
                                  preserve_metadata=False)
    output = OutputFile(destination)
    try:
        _encode_image(output.temporary, pixels, opts, {},
                      generate_preview=False, controller=controller,
                      has_transparency=rendered.alpha is not None)
        if controller.cancel.is_set():
            raise Cancelled("Render stopped by user.")
        output.publish()
    finally:
        output.cleanup()

def _publish_video(current: Path, destination: Path, controller: JobController,
                   *, preview: bool = False) -> Path:
    """Publish a completed video without another encode.

    A hard link is enough on the same volume. Cross-volume or optional-cache
    fallback copies remain cancellable and are published atomically.
    """
    if preview:
        destination = destination.with_suffix(current.suffix)
    elif destination.suffix.casefold() != current.suffix.casefold():
        raise ValueError("The processed video container does not match the export destination.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    output = OutputFile(destination)
    try:
        if controller.cancel.is_set():
            raise Cancelled("Video processing cancelled.")
        output.temporary.unlink()
        try:
            os.link(current, output.temporary)
        except OSError:
            with current.open("rb") as source, output.temporary.open("wb") as target:
                while data := source.read(1024 * 1024):
                    if controller.cancel.is_set():
                        raise Cancelled("Video processing cancelled.")
                    target.write(data)
        if controller.cancel.is_set():
            raise Cancelled("Video processing cancelled.")
        output.publish()
    finally:
        output.cleanup()
    return destination

def _export_video(source: Path, current: Path, destination: Path,
                  settings: UISettings, controller: JobController,
                  *, pipeline_hdr: bool = False, progress=None,
                  video_filter: str | None = None, output_metadata: dict | None = None,
                  working_directory: Path | None = None, verify_output: bool = False) -> None:
    metadata = output_metadata or ffmpeg.probe_video(current, count_mode="metadata", controller=controller)
    width, height = int(metadata["width"]), int(metadata["height"])
    codec = settings.codec
    quality = settings.quality
    container = ffmpeg.resolve_container(codec, settings.container)
    current_hdr = bool(metadata.get("hdr")) or pipeline_hdr
    hdr = current_hdr
    source_metadata = ffmpeg.probe_video(source, count_mode="metadata", controller=controller)
    encode_metadata = dict(metadata)
    # FFV1 RGB intermediates report matrix=gbr. Delivery codecs need the
    # original YUV matrix, or BT.2020 for HDR created by RTX Video HDR.
    if metadata.get("color_space") == "gbr" and codec != LOSSLESS_VIDEO:
        encode_metadata["color_space"] = "bt2020nc" if hdr else (
            source_metadata.get("color_space")
            if source_metadata.get("color_space") not in {None, "", "unknown", "gbr"}
            else "bt709"
        )
        encode_metadata["color_range"] = "tv"
    if hdr:
        encode_metadata["color_primaries"] = "bt2020"
        encode_metadata["color_transfer"] = "smpte2084"
        encode_metadata["color_space"] = "bt2020nc"
    gpu = ffmpeg.resolve_video_gpu(detect_gpus(), settings.video_gpu_uuid,
                                   codec, width, height)
    codec_args, _, _ = _codec_command(codec, quality, width, height,
                                     float(metadata["fps"]),
                                     None if gpu is None else int(gpu["cuda_ordinal"]),
                                     hdr_mode=hdr, hdr_metadata=encode_metadata,
                                     speed_profile="default")
    audio = plan_audio_streams(source, container, controller)
    output = OutputFile(destination)
    try:
        command = [str(FFMPEG), "-hide_banner", "-loglevel", "error", "-y",
                   *(["-xerror"] if verify_output else []),
                   "-i", str(current), "-i", str(source),
                   "-map", "0:v:0", "-map", "1:a?", "-map_metadata", "1",
                   "-map_chapters", "1", *(["-vf", video_filter] if video_filter else []), *codec_args,
                   *audio.encoder_args(), "-fps_mode", "passthrough", "-enc_time_base:v", "demux",
                   "-t", f"{max(float(metadata.get('duration') or 0), 1 / float(metadata['fps'])):.9f}",
                   str(output.temporary)]
        _run_command(command, controller, "Video export", progress=progress, metadata=metadata,
                     cwd=working_directory)
        if controller.cancel.is_set():
            raise Cancelled("Render stopped by user.")
        if verify_output:
            saved = ffmpeg.probe_video(output.temporary, count_mode="packets", controller=controller)
            if ((int(saved["width"]), int(saved["height"])) != (width, height)
                    or int(saved["frames"]) < 1
                    or (int(metadata.get("frames") or 0) > 0
                        and int(saved["frames"]) != int(metadata["frames"]))):
                raise RuntimeError("Video export frame count or dimensions do not match the processing pipeline.")
            if hdr and (not saved.get("hdr") or int(saved.get("depth") or 0) < 10):
                raise RuntimeError("Video export lost HDR signaling or precision.")
            if controller.cancel.is_set():
                raise Cancelled("Render stopped by user.")
        output.publish()
    finally:
        output.cleanup()

def render_item(source: Path, settings: UISettings, mode: str, destination: Path,
                controller: JobController, progress=None, *, preview: bool = False,
                capabilities_checked: bool = False,
                preview_cache: PreviewStageCache | None = None,
                source_cache_key: str | None = None,
                preflight_estimate: PipelineEstimate | None = None) -> str:
    controller.ffmpeg_device = settings.ffmpeg_device
    estimate = preflight_estimate or preflight_source(source, settings, mode)
    stages = enabled_stages(settings, mode)
    direct_native = (mode == "Video" and not preview and len(stages) == 1
                     and stages[0] in {"neural_model", "dlss_super_resolution",
                                       "super_resolution", "rtx_video_hdr", "frame_generation"})
    if not capabilities_checked and preview_cache is None:
        preflight_capabilities(settings, mode, controller,
                               require_filter=not direct_native and not (
                                   mode == "Image" and stages == ("neural_model",)))
    if mode == "Video" and not preview:
        settings = _preferred_native_codec(settings, estimate, stages)
    upstream_key = (source_cache_key or cache_key({"kind": "source", "mode": mode,
                                                   "source": file_identity(source)})) if preview_cache else ""
    JOBS.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="visual-workflow-", dir=JOBS) as temp:
        if mode == "Video" and not preview:
            from .rolling_workflow import render_rolling_video
            destination.parent.mkdir(parents=True, exist_ok=True)
            return render_rolling_video(source, settings, destination, controller,
                                        Path(temp), stages, progress)
        current = source
        # Smart previews remain lossless and share their existing FFV1 cache keys.
        stage_settings = replace(settings, cache_codec="Fast lossless") if preview else settings
        hdr = estimate.hdr if mode == "Video" else False
        for index, stage in enumerate(stages):
            if controller.cancel.is_set():
                raise Cancelled("Render stopped by user.")
            stage_key = (cache_key({"kind": "stage", "mode": mode, "upstream": upstream_key,
                                    "stage": stage,
                                    "settings": _stage_cache_settings(settings, mode, stage, hdr)})
                         if preview_cache else "")
            if preview_cache:
                cached = preview_cache.lookup(stage_key, current)
                if cached is not None:
                    current = cached
                    if progress:
                        progress((index + 1) / (len(stages) + 1),
                                 f"Stage {index + 1} of {len(stages) + 1} · {STAGE_LABELS[stage]}: using Smart cache")
                    upstream_key = stage_key
                    if stage == "rtx_video_hdr":
                        hdr = True
                    continue
            directory = Path(temp) / f"{index:02d}-{stage}"
            directory.mkdir()
            stage_input = current
            if preview_cache and not capabilities_checked:
                preflight_capabilities(settings, mode, controller, (stage,),
                                       require_filter=not (mode == "Image" and stage == "neural_model"))
            def report(value, message, i=index, name=stage):
                if progress:
                    progress((i + max(0.0, min(1.0, float(value)))) / (len(stages) + 1),
                             f"Stage {i + 1} of {len(stages) + 1} · {STAGE_LABELS[name]}: {message}")
            report(0.0, "Preparing")
            if stage == "coloring":
                current = (_coloring_image_stage(current, source, settings, directory, controller, report)
                           if mode == "Image" else
                           _coloring_video_stage(current, stage_settings, directory, controller, report))
            elif mode == "Image":
                current = _image_stage(current, stage, settings, directory, controller, report)
            else:
                current = _preview_video_stage(current, stage, stage_settings, hdr,
                                               directory, controller, report)
            if controller.cancel.is_set():
                raise Cancelled("Render stopped by user.")
            if preview_cache:
                current = preview_cache.publish(stage_key, directory, current,
                                                previous=stage_input)
                upstream_key = stage_key
            if stage == "rtx_video_hdr":
                hdr = True
        if progress:
            progress(len(stages) / (len(stages) + 1),
                     "Preparing cached preview" if preview and mode == "Video" else
                     f"Stage {len(stages) + 1} of {len(stages) + 1} · Exporting result")
        destination.parent.mkdir(parents=True, exist_ok=True)
        if mode == "Image":
            _export_image(current, destination, settings, controller)
        else:
            destination = _publish_video(current, destination, controller, preview=True)
        if progress:
            progress(1.0, "Preview ready" if preview else "Export complete")
    return str(destination)

def render_pipeline_batch(input_paths, settings: UISettings, mode: str,
                          progress=None, *, output_dir=None, controller=None,
                          on_item_update=None, same_as_input=False) -> PipelineBatchResult:
    paths = [Path(path).resolve() for path in input_paths]
    if not paths:
        raise ValueError("Add a file before rendering.")
    controller = controller or JobController()
    from ..core.jobs import prepare_job
    estimates = [prepare_job(controller, lambda path=path: preflight_source(path, settings, mode)) for path in paths]
    stages = enabled_stages(settings, mode)
    direct_native = (mode == "Video" and len(stages) == 1
                     and stages[0] in {"neural_model", "dlss_super_resolution",
                                       "super_resolution", "rtx_video_hdr", "frame_generation"})
    prepare_job(controller, lambda: preflight_capabilities(settings, mode, controller,
                                                           require_filter=not direct_native and not (
                                                               mode == "Image" and stages == ("neural_model",))))
    reporter = BatchProgress(paths, on_item_update, progress)
    successes: list[PipelineSuccess] = []
    failures: list[PipelineFailure] = []
    reserved: set[Path] = set()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    from ..core.neural_bridge import BRIDGE_MANAGER
    # Reuse for NR-only queues. Mixed pipelines keep their per-stage release
    # boundary so another GPU stage does not overlap retained NR resources.
    native_batch = (BRIDGE_MANAGER.image_batch() if mode == "Image" and stages == ("neural_model",)
                    else nullcontext())
    try:
        with native_batch:
            for index, path in enumerate(paths):
                if controller.cancel.is_set():
                    break
                reporter.advance(index, 0.0, "Preparing pipeline")
                folder = prepare_output_dir(path.parent if same_as_input else output_dir, default=OUTPUTS)
                extension = (IMAGE_EXTENSIONS[settings.image_format] if mode == "Image" else
                             {"MP4": ".mp4", "MKV": ".mkv", "MOV": ".mov"}[
                                 ffmpeg.resolve_container(settings.codec, settings.container)])
                rename_mode = settings.image_rename_mode if mode == "Image" else settings.video_rename_mode
                suffix = settings.image_custom_suffix if mode == "Image" else settings.video_custom_suffix
                output = unique_output_path(folder / output_filename(
                    path, extension, rename_mode, suffix, f"{path.stem}_Pipeline_{stamp}"), reserved)
                reserved.add(output)
                try:
                    rendered = render_item(path, settings, mode, output, controller,
                                           lambda value, message, i=index: reporter.advance(i, value, message),
                                           capabilities_checked=True,
                                           preflight_estimate=estimates[index])
                except Exception as exc:
                    cancelled = isinstance(exc, Cancelled) or controller.cancel.is_set()
                    failures.append(PipelineFailure(str(path), str(exc), cancelled))
                    reporter.fail(index, exc, cancelled=cancelled)
                    if cancelled:
                        controller.stop()
                        break
                else:
                    successes.append(PipelineSuccess(str(path), rendered))
                    reporter.complete(index, rendered)
        if controller.cancel.is_set():
            reporter.skip_from(0)
        reporter.finish(cancelled=controller.cancel.is_set(), manifest_path=app_log.session_path())
        return PipelineBatchResult(successes, failures, controller.cancel.is_set())
    except BaseException as exc:
        reporter.finish(cancelled=controller.cancel.is_set(), error=str(exc))
        raise

def render_pipeline_preview(source: str, settings: UISettings, mode: str, *,
                            output_dir: Path, controller=None, progress=None,
                            start_seconds: float = 0.0,
                            clip_seconds: float | None = None,
                            cache_dir: Path = PREVIEW_CACHE) -> tuple[str | np.ndarray, str]:
    controller = controller or JobController()
    if not os.environ.get("VE_STAGE_WORKER"):
        from .pass_process import run_request
        JOBS.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="visual-preview-scope-", dir=JOBS) as temporary:
            def event(values):
                if progress and values.get("event") == "preview-progress":
                    progress(values["progress"], values["message"])
            result = run_request(dict(operation="pipeline-preview", source=source, settings=asdict(settings),
                mode=mode, output_dir=str(Path(output_dir).resolve()), cache_dir=str(Path(cache_dir).resolve()),
                start_seconds=start_seconds, clip_seconds=clip_seconds), temporary, controller, on_event=event)
            media = np.load(result["array"], allow_pickle=False) if result.get("array") else result["path"]
            return media, result["message"]
    controller.ffmpeg_device = settings.ffmpeg_device
    source_path = Path(source).resolve()
    if controller.cancel.is_set():
        raise Cancelled("Preview cancelled.")
    if not source_path.is_file():
        raise FileNotFoundError(source_path)
    stages = enabled_stages(settings, mode)
    if not stages:
        # The native player already seeks/plays the unprocessed video. Do not
        # probe, convert to FFV1, or decode another copy just to show it again.
        media = decode_image(source_path).rgba if mode == "Image" else str(source_path)
        return media, "Source preview (all processing cards are off)."
    if mode == "Video" and clip_seconds is None:
        frame_key = None
        if "frame_generation" not in stages:
            frame_key = cache_key({
                "kind": "parked-frame", "source": file_identity(source_path),
                "settings": preview_settings_key(settings, mode, hdr=False),
                "start_seconds": round(float(start_seconds), 6),
                "runtime": _preview_runtime_identity(),
            })
            cached = _cached_frame_preview(frame_key)
            if cached is not None:
                if controller.cancel.is_set():
                    raise Cancelled("Preview cancelled.")
                return cached, "Pipeline frame preview complete."
        metadata = ffmpeg.probe_video(source_path, count_mode="metadata", controller=controller)
        hdr_preview = bool(metadata.get("hdr") or "rtx_video_hdr" in stages)
        pq_input = (not metadata.get("hdr") or
                    metadata.get("color_transfer") == "smpte2084")
        fast_hdr = hdr_preview and pq_input and stages in {
            ("neural_model",), ("rtx_video_hdr",),
        }
        if (len(stages) == 1 and not int(metadata.get("rotation") or 0) and frame_key is not None
                and (not hdr_preview or fast_hdr)):
            preview_settings = (replace(settings, codec=LOSSLESS_VIDEO, container="MKV",
                                        quality="Auto (Default)", hdr_mode=True)
                                if hdr_preview else settings)
            preflight_source(source_path, preview_settings, mode, metadata=metadata)
            preflight_capabilities(
                settings, mode, controller,
                require_filter=hdr_preview or not set(stages).issubset({
                    "neural_model", "dlss_super_resolution", "super_resolution",
                    "rtx_video_hdr",
                }),
            )
            from .rolling_workflow import render_preview_frame
            pixels = render_preview_frame(source_path, replace(settings, cache_codec="Fast lossless"),
                                          controller, start_seconds, stages, metadata)
            if hdr_preview:
                pixels = tone_map_hdr_preview_frame(pixels, controller=controller)
            if controller.cancel.is_set():
                raise Cancelled("Preview cancelled.")
            _remember_frame_preview(frame_key, pixels)
            return pixels, "Pipeline frame preview complete."
    if (mode == "Video" and clip_seconds is not None and clip_seconds > 0
            and stages == ("neural_model",)):
        from ..neural_rendering.video.fast_preview import (
            FastClipUnavailable, render_lossless_cuda_preview_clip)

        options = _neural_video_options(replace(settings, cache_codec="Fast lossless"), False)
        if options.prefer_nvenc:
            metadata = ffmpeg.probe_video(source_path, count_mode="metadata",
                                          controller=controller)
            color_fields = ("color_space", "color_primaries", "color_transfer")
            compatible_color = all(metadata.get(field) == "bt709"
                                   for field in color_fields)
            if (metadata.get("cfr") and not metadata.get("hdr")
                    and not int(metadata.get("rotation") or 0)
                    and int(metadata.get("depth") or 8) <= 8
                    and compatible_color):
                preview_settings = replace(settings, codec=LOSSLESS_VIDEO,
                                           container="MKV", quality="Auto (Default)",
                                           hdr_mode=True)
                preflight_source(source_path, preview_settings, mode, metadata=metadata)
                frame_count = max(1, math.ceil(float(clip_seconds) *
                                               float(metadata["fps"]) - 1e-6))
                stage_cache = PreviewStageCache(cache_dir)
                clip_key = cache_key({
                    "kind": "neural-cuda-lossless-clip",
                    "source": file_identity(source_path),
                    "settings": preview_settings_key(settings, mode, hdr=False),
                    "start_seconds": round(float(start_seconds), 6),
                    "clip_seconds": round(float(clip_seconds), 6),
                    "frames": frame_count,
                    "runtime": _preview_runtime_identity(),
                })
                cached = stage_cache.lookup(clip_key)
                if cached is not None:
                    if controller.cancel.is_set():
                        raise Cancelled("Preview cancelled.")
                    return str(cached.resolve()), "Pipeline video preview complete."
                preflight_capabilities(settings, mode, controller,
                                       require_filter=False)
                JOBS.mkdir(parents=True, exist_ok=True)
                with tempfile.TemporaryDirectory(
                        prefix="visual-cuda-preview-", dir=JOBS) as temp:
                    directory = Path(temp) / "clip"
                    directory.mkdir()
                    try:
                        result = render_lossless_cuda_preview_clip(
                            source_path, options, metadata,
                            start_seconds=start_seconds, frame_count=frame_count,
                            output_dir=directory, controller=controller,
                            progress=progress)
                    except FastClipUnavailable:
                        pass
                    else:
                        published = stage_cache.publish(clip_key, directory, result)
                        if published.resolve() == result.resolve():
                            output_dir = prepare_output_dir(output_dir)
                            durable = unique_output_path(
                                output_dir / f"pipeline-preview-{time.time_ns()}.mkv")
                            shutil.move(str(result), str(durable))
                            published = durable
                        prune_preview_cache(root=stage_cache.root,
                                            protected_keys=stage_cache.used_keys,
                                            min_idle_seconds=0)
                        return str(published.resolve()), "Pipeline video preview complete."
    if (mode == "Video" and clip_seconds is not None and clip_seconds > 0
            and stages in {("dlss_super_resolution",), ("super_resolution",)}):
        metadata = ffmpeg.probe_video(source_path, count_mode="metadata",
                                      controller=controller)
        if (metadata.get("cfr") and not metadata.get("hdr")
                and not int(metadata.get("rotation") or 0)
                and int(metadata.get("depth") or 8) <= 8
                and all(metadata.get(field) == "bt709" for field in
                        ("color_space", "color_primaries", "color_transfer"))):
            from ..upscale.video.preview import preview_upscale_native

            preview_settings = replace(settings, codec=LOSSLESS_VIDEO,
                                       container="MKV", quality="Auto (Default)",
                                       hdr_mode=True)
            preflight_source(source_path, preview_settings, mode, metadata=metadata)
            stage_cache = PreviewStageCache(cache_dir)
            clip_key = cache_key({
                "kind": "upscale-direct-seek-lossless-clip",
                "stage": stages[0],
                "source": file_identity(source_path),
                "settings": preview_settings_key(settings, mode, hdr=False),
                "start_seconds": round(float(start_seconds), 6),
                "clip_seconds": round(float(clip_seconds), 6),
                "runtime": _preview_runtime_identity(),
            })
            cached = stage_cache.lookup(clip_key)
            if cached is not None:
                if controller.cancel.is_set():
                    raise Cancelled("Preview cancelled.")
                return str(cached.resolve()), "Pipeline video preview complete."
            preflight_capabilities(settings, mode, controller, require_filter=False)
            JOBS.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(
                    prefix="visual-upscale-preview-", dir=JOBS) as temp:
                directory = Path(temp) / "clip"
                directory.mkdir()
                result, _ = preview_upscale_native(
                    str(source_path), _video_upscale_stage_options(settings, stages[0]),
                    one_frame=False, progress=progress, controller=controller,
                    output_dir=directory, start_seconds=start_seconds,
                    preview_seconds=clip_seconds)
                published = stage_cache.publish(clip_key, directory, Path(result))
                if published.resolve() == Path(result).resolve():
                    output_dir = prepare_output_dir(output_dir)
                    durable = unique_output_path(
                        output_dir / f"pipeline-preview-{time.time_ns()}.mkv")
                    shutil.move(str(result), str(durable))
                    published = durable
                prune_preview_cache(root=stage_cache.root,
                                    protected_keys=stage_cache.used_keys,
                                    min_idle_seconds=0)
                return str(published.resolve()), "Pipeline video preview complete."
    if mode == "Video":
        # Preview storage and playback are independent of delivery settings.
        settings = replace(settings, codec=LOSSLESS_VIDEO, container="MKV",
                           quality="Auto (Default)", hdr_mode=True)
    preflight_source(source_path, settings, mode)
    stage_cache = PreviewStageCache(cache_dir)
    root_key = cache_key({"kind": "source", "mode": mode,
                          "source": file_identity(source_path),
                          "runtime": _preview_runtime_identity()})
    output_dir = prepare_output_dir(output_dir)
    if mode == "Image":
        output = unique_output_path(output_dir / f"pipeline-preview-{time.time_ns()}.png")
        preview_settings = replace(settings, image_format="PNG", image_quality=100)
        path = render_item(source_path, preview_settings, mode, output, controller, progress,
                           preview=True,
                           preview_cache=stage_cache, source_cache_key=root_key)
        prune_preview_cache(root=stage_cache.root, protected_keys=stage_cache.used_keys,
                            min_idle_seconds=0)
        return path, "Pipeline image preview complete."
    JOBS.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="visual-preview-source-", dir=JOBS) as temp:
        clip_key = cache_key({"kind": "source-clip", "source": root_key,
                              "start_seconds": float(start_seconds),
                              "clip_seconds": None if clip_seconds is None else float(clip_seconds)})
        clip = stage_cache.lookup(clip_key)
        if clip is None:
            clip_dir = Path(temp) / "source-clip"
            clip_dir.mkdir()
            clip = _extract_lossless_preview_clip(source_path, clip_dir, controller,
                                                  start_seconds, clip_seconds)
            if controller.cancel.is_set():
                raise Cancelled("Render stopped by user.")
            clip = stage_cache.publish(clip_key, clip_dir, clip)
        elif progress:
            progress(0.0, "Using cached source clip")
        output = unique_output_path(output_dir / f"pipeline-preview-{time.time_ns()}.mkv")
        output = render_item(clip, settings, mode, output, controller, progress, preview=True,
                             preview_cache=stage_cache,
                             source_cache_key=clip_key)
    prune_preview_cache(root=stage_cache.root, protected_keys=stage_cache.used_keys,
                        min_idle_seconds=0)
    if clip_seconds is None:
        return decode_preview_frame(output, controller=controller), "Pipeline frame preview complete."
    return str(output), "Pipeline video preview complete (lossless cached clip)."

def _extract_lossless_preview_clip(source: Path, directory: Path,
                                   controller: JobController, start_seconds: float,
                                   clip_seconds: float | None) -> Path:
    """Frame-accurate clip cut without changing pixels before the first card."""
    return Path(extract_preview_subclip(
        source, dest_dir=directory, controller=controller,
        start_seconds=start_seconds, length_seconds=clip_seconds,
        single_frame=clip_seconds is None))
