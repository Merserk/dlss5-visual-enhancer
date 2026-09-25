"""Ordered, file-backed Neural Rendering workflow shared by preview and export."""

from __future__ import annotations

import subprocess
import shutil
import tempfile
import time
from dataclasses import dataclass, replace
from pathlib import Path

import numpy as np

from ..core import app_log, ffmpeg
from ..core.batch_progress import BatchProgress
from ..core.cache_cleanup import prune_preview_cache
from ..core.disk_paths import OutputFile, prepare_output_dir
from ..core.dlss_modes import dlss_output_size
from ..core.ffmpeg.audio import plan_audio_streams
from ..core.ffmpeg.encoder import _codec_command
from ..core.gpu_detection import detect_gpus
from ..core.jobs import Cancelled, JobController
from ..core.naming import output_filename, unique_output_path
from ..core.paths import (FFMPEG, JOBS, OUTPUTS, PREVIEW_CACHE,
                          DLSSSR_BRIDGE, DLSSSR_RUNTIME, DLSSNR_BRIDGE,
                          DLSSNR_CALLER_SHIM, DLSSG_DIR, RUNTIME,
                          NEURAL_RUNTIME)
from ..core.nr_composition import mask_selection
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
from .denoising import denoise_image, denoise_video
from .preview_cache import PreviewStageCache, cache_key, file_identity, optional_file_identity


LOSSLESS_VIDEO = "FFV1 Lossless RGB 10-bit"
STAGE_LABELS = {
    "denoising": "Denoising",
    "neural_model": "DLSS Neural Rendering",
    "scale_method": "Scaling",
    "dlss_super_resolution": "DLSS Super Resolution",
    "super_resolution": "RTX Super Resolution",
    "frame_generation": "DLSS Frame Generation",
    "coloring": "Coloring",
    "cas_sharpening": "Sharpening",
}


_NR_FIELDS = (
    "nr_style", "nr_intensity", "nr_passes", "local_tone_strength",
    "local_structure_strength", "skin_structure_strength", "nr_color_strength",
    "tone_preservation", "face_skin_protection", "grain_preservation",
    "mask_feather", "automatic_mask",
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
    "upscale_vsr_enabled", "upscale_vsr_quality", "upscale_size_mode",
    "upscale_scale_factor", "upscale_width", "upscale_height",
    "upscale_aspect_lock", "upscale_hdr_enabled", "upscale_hdr_contrast",
    "upscale_hdr_saturation", "upscale_hdr_middle_gray",
    "upscale_hdr_peak_luminance", "upscale_hdr_precision",
)


def _preview_runtime_identity() -> list[dict | None]:
    # A replaced processing engine must never inherit intermediates produced
    # by the previous engine, including when the app is updated in place.
    source_root = Path(__file__).resolve().parents[1]
    return [optional_file_identity(path) for path in
            (Path(__file__), Path(__file__).with_name("coloring.py"),
             Path(__file__).with_name("denoising.py"),
             Path(__file__).with_name("cas_sharpening.py"),
             Path(__file__).with_name("nis_sharpening.py"),
             source_root / "core" / "dlss_bridge.py",
             source_root / "upscale" / "image" / "processor.py",
             source_root / "upscale" / "video" / "processor.py",
             source_root / "neural_rendering" / "image" / "batch.py",
             source_root / "neural_rendering" / "video" / "processor.py",
             source_root / "frame_interpolation" / "processor.py",
             FFMPEG, DLSSSR_BRIDGE, DLSSSR_RUNTIME, DLSSNR_BRIDGE,
             DLSSNR_CALLER_SHIM, NEURAL_RUNTIME,
             RUNTIME / "rtx_video" / "neuroframe_engine_upscaling.dll",
             RUNTIME / "rtx_video" / "nvngx_vsr.dll",
             RUNTIME / "rtx_video" / "nvngx_truehdr.dll",
             DLSSG_DIR / "nvngx_dlssg.dll")]


def _stage_cache_settings(settings: UISettings, mode: str, stage: str, hdr: bool) -> dict:
    """Only settings consumed by this card belong to its cache key."""
    fields: tuple[str, ...]
    extras: dict = {}
    if stage == "denoising":
        fields = ("denoise_strength", "denoise_deblock")
        if mode == "Video":
            fields += ("denoise_temporal",)
    elif stage == "cas_sharpening":
        fields = ("cas_sharpness", "sharpening_method")
    elif stage == "neural_model":
        fields = _NR_FIELDS + (("shimmer_suppression", "video_gpu_uuid") if mode == "Video" else ())
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
                  ("upscale_dlss_mode", "upscale_dlss_preset", "ai_gpu_uuid", "video_gpu_uuid"))
    elif stage == "super_resolution":
        fields = (("ai_gpu_uuid",) + _IMAGE_VSR_FIELDS if mode == "Image" else
                  ("ai_gpu_uuid", "video_gpu_uuid") + _VIDEO_VSR_FIELDS)
    elif stage == "frame_generation":
        fields = ("frame_interpolation_target_fps", "frame_interpolation_engine",
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
    return {**{name: getattr(settings, name) for name in fields}, **extras}


def _video_scaling_filter(width: int, height: int, method: str) -> tuple[list[str], str]:
    """Return FFmpeg device arguments and the exact requested scaler."""
    if method == "Spline36":
        return [], f"format=gbrp10le,zscale=w={width}:h={height}:filter=spline36:matrixin=gbr:matrix=gbr"
    if method == "EWA Lanczos":
        return ["-init_hw_device", "vulkan=vk:0", "-filter_hw_device", "vk"], (
            "format=gbrp10le,hwupload,"
            f"libplacebo=w={width}:h={height}:upscaler=ewa_lanczos:downscaler=ewa_lanczos,"
            "hwdownload,format=gbrp10le"
        )
    flags = {"Lanczos": "lanczos", "Bicubic": "bicubic", "Area": "area", "Bilinear": "bilinear"}
    if method not in flags:
        raise ValueError(f"Unknown video scaling filter: {method!r}.")
    return [], f"scale={width}:{height}:flags={flags[method]}"


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


def estimate_pipeline(width: int, height: int, fps: float, hdr: bool,
                      settings: UISettings, mode: str) -> PipelineEstimate:
    """Preflight every enabled stage in its actual execution order."""
    if width < 1 or height < 1:
        raise ValueError("Input dimensions are unavailable.")
    if mode == "Video" and fps <= 0:
        raise ValueError("Input frame rate is unavailable; video export needs a known FPS.")
    for stage in enabled_stages(settings, mode):
        if stage == "denoising":
            pass  # Pixel size, frame rate and HDR flags are preserved.
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
                opts = replace(video_upscale_options(settings), engine="RTX Video Super Resolution",
                               codec=LOSSLESS_VIDEO,
                               container="MKV", quality="Auto (Default)")
                opts.validate(for_render=True)
                width, height, _ = video_upscale_size(width, height, opts)
                hdr = opts.hdr_enabled
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
        ffmpeg.validate_codec_container(settings.codec, ffmpeg.container_for_codec(settings.codec))
    return PipelineEstimate(width, height, fps, hdr)


def preflight_source(source: Path, settings: UISettings, mode: str) -> PipelineEstimate:
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
    metadata = ffmpeg.probe_video(source, count_mode="metadata",
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
                           stages_override: tuple[str, ...] | None = None) -> None:
    """Reject unavailable processing hardware before writing any stage output."""
    stages = stages_override if stages_override is not None else enabled_stages(settings, mode)
    if not stages:
        return
    needs_ai_gpu = bool(set(stages) & {"neural_model", "dlss_super_resolution", "super_resolution", "frame_generation"})
    if needs_ai_gpu:
        from ..core.gpu_selection import detect_gpu

        detect_gpu(settings.ai_gpu_uuid)
    if mode == "Video" and "scale_method" in stages and settings.video_scaling_filter == "EWA Lanczos":
        device_args, filter_graph = _video_scaling_filter(64, 64, "EWA Lanczos")
        try:
            _run_command([str(FFMPEG), "-hide_banner", "-loglevel", "error",
                          *device_args, "-f", "lavfi", "-i", "testsrc2=size=64x64:rate=1:duration=1",
                          "-vf", filter_graph, "-frames:v", "1", "-f", "null", "-"],
                         controller or JobController(), "EWA Lanczos capability check")
        except RuntimeError as exc:
            raise ValueError("EWA Lanczos requires FFmpeg libplacebo and an available Vulkan device.") from exc
    if "super_resolution" in stages:
        vsr = mode == "Image" or settings.upscale_vsr_enabled
        hdr = mode == "Video" and settings.upscale_hdr_enabled
        if vsr or hdr:
            from ..upscale.video.native import probe_capabilities

            caps = probe_capabilities(settings.ai_gpu_uuid, controller=controller)
            if vsr and not caps.vsr.get("available"):
                raise ValueError("RTX Super Resolution requires an available RTX Video Super Resolution runtime.")
            if hdr and not caps.hdr.get("available"):
                raise ValueError("RTX Video HDR requires an available RTX Video HDR runtime.")
    if "frame_generation" in stages:
        from ..frame_interpolation.capabilities import probe_frame_interpolation_capabilities

        caps = probe_frame_interpolation_capabilities(settings.ai_gpu_uuid)
        if not caps.available:
            raise ValueError("DLSS Frame Generation is unavailable. " + caps.detail)


def _run_command(command: list[str], controller: JobController, label: str,
                 *, cwd: Path | None = None) -> None:
    if controller.cancel.is_set():
        raise Cancelled("Render stopped by user.")
    process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                               cwd=cwd,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    controller.register(process)
    try:
        while True:
            if controller.cancel.is_set():
                raise Cancelled("Render stopped by user.")
            try:
                _, stderr = process.communicate(timeout=0.2)
                break
            except subprocess.TimeoutExpired:
                continue
        if process.returncode:
            raise RuntimeError(f"{label} failed: {stderr.decode('utf-8', 'replace')[-2500:]}")
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        controller.unregister(process)
        if process.stderr:
            process.stderr.close()


def _neural_image_options(settings: UISettings) -> ImageConversionOptions:
    return ImageConversionOptions(
        ai_gpu_uuid=settings.ai_gpu_uuid, nr_style=settings.nr_style,
        nr_intensity=settings.nr_intensity, nr_passes=settings.nr_passes,
        local_tone_strength=settings.local_tone_strength,
        local_structure_strength=settings.local_structure_strength,
        skin_structure_strength=settings.skin_structure_strength,
        nr_color_strength=settings.nr_color_strength,
        tone_preservation=settings.tone_preservation,
        face_skin_protection=settings.face_skin_protection,
        grain_preservation=settings.grain_preservation,
        mask_feather=settings.mask_feather, nr_mask=settings.nr_mask,
        automatic_mask=settings.automatic_mask, upscaling_factor=1.0,
        scale_method="Standard", output_format="TIFF", quality=100,
        preserve_metadata=False,
    )


def _neural_video_options(settings: UISettings, hdr: bool) -> ConversionOptions:
    return ConversionOptions(
        ai_gpu_uuid=settings.ai_gpu_uuid, video_gpu_uuid=settings.video_gpu_uuid,
        nr_style=settings.nr_style, nr_intensity=settings.nr_intensity,
        nr_passes=settings.nr_passes, local_tone_strength=settings.local_tone_strength,
        local_structure_strength=settings.local_structure_strength,
        skin_structure_strength=settings.skin_structure_strength,
        nr_color_strength=settings.nr_color_strength,
        tone_preservation=settings.tone_preservation,
        face_skin_protection=settings.face_skin_protection,
        grain_preservation=settings.grain_preservation,
        shimmer_suppression=settings.shimmer_suppression,
        mask_feather=settings.mask_feather, nr_mask=settings.nr_mask,
        automatic_mask=settings.automatic_mask, upscaling_factor=1.0,
        scale_method="Standard", codec=LOSSLESS_VIDEO, container="MKV",
        quality="Auto (Default)", preserve_hdr=hdr,
    )


def _image_stage(source: Path, stage: str, settings: UISettings,
                 directory: Path, controller: JobController, progress) -> Path:
    if stage == "denoising":
        return denoise_image(source, settings, directory, controller, progress, _run_command)
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
                                         interpolation=settings.image_scaling_filter),
                      ImageConversionOptions(output_format="TIFF", preserve_metadata=False),
                      {}, generate_preview=False, controller=controller,
                      has_transparency=decoded.alpha is not None)
        return output
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
        matched = apply_cube_lut(rendered.rgba, lut)
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
    color_args: list[str] = []
    for key, flag in (("color_primaries", "-color_primaries"),
                      ("color_transfer", "-color_trc")):
        value = metadata.get(key)
        if value and value != "unknown":
            color_args.extend((flag, str(value)))
    output = directory / "coloring.mkv"
    progress(0.1, "Applying LUT to video")
    _run_command([str(FFMPEG), "-hide_banner", "-loglevel", "error", "-y",
                  "-i", str(source), "-map", "0:v:0", "-map_metadata", "0", "-an",
                  "-vf", "format=gbrp10le,lut3d=file=reference.cube:interp=trilinear,format=gbrp10le",
                  "-c:v", "ffv1", "-level", "3", "-pix_fmt", "gbrp10le",
                  "-fps_mode", "passthrough", *color_args, "-color_range", "pc",
                  str(output)], controller, "Video LUT", cwd=directory)
    progress(1.0, "LUT complete")
    return output


def _video_stage(source: Path, stage: str, settings: UISettings, hdr: bool,
                 directory: Path, controller: JobController, progress) -> Path:
    if stage == "denoising":
        return denoise_video(source, settings, directory, controller, progress, _run_command)
    if stage == "cas_sharpening":
        return sharpen_video(source, settings, directory, controller, progress)
    if stage == "neural_model":
        result = convert_video(source, _neural_video_options(settings, hdr), progress,
                               output_dir=directory, controller=controller)
    elif stage == "scale_method":
        metadata = ffmpeg.probe_video(source, count_mode="metadata", controller=controller)
        width, height = resolve_output_size(int(metadata["width"]), int(metadata["height"]),
                                            settings.upscaling_factor)
        output = directory / "scaled.mkv"
        device_args, filter_graph = _video_scaling_filter(width, height, settings.video_scaling_filter)
        _run_command([str(FFMPEG), "-hide_banner", "-loglevel", "error", "-y",
                      *device_args, "-i", str(source),
                      "-map", "0:v:0", "-map_metadata", "0", "-vf", filter_graph, "-c:v", "ffv1",
                      "-level", "3", "-pix_fmt", "gbrp10le", str(output)],
                     controller, f"{settings.video_scaling_filter} scaling")
        return output
    elif stage in {"dlss_super_resolution", "super_resolution"}:
        opts = video_upscale_options(settings)
        opts = replace(opts, engine="DLSS" if stage == "dlss_super_resolution"
                       else "RTX Video Super Resolution",
                       hdr_enabled=False if stage == "dlss_super_resolution" else opts.hdr_enabled)
        opts = replace(opts, codec=LOSSLESS_VIDEO, container="MKV", quality="Auto (Default)")
        result = upscale_video(source, opts, progress, output_dir=directory, controller=controller)
    elif stage == "frame_generation":
        opts = FrameInterpolationOptions(
            ai_gpu_uuid=settings.ai_gpu_uuid, video_gpu_uuid=settings.video_gpu_uuid,
            target_fps=settings.frame_interpolation_target_fps,
            engine=settings.frame_interpolation_engine,
            codec=LOSSLESS_VIDEO, container="MKV", quality="Auto (Default)",
            hdr_mode=hdr,
        )
        result = interpolate_video(source, opts, progress, output_dir=directory,
                                   controller=controller)
    else:
        raise ValueError(f"Unknown video processing card: {stage}")
    return Path(result.output_path)


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


def _export_video(source: Path, current: Path, destination: Path,
                  settings: UISettings, controller: JobController,
                  *, preview: bool = False, pipeline_hdr: bool = False) -> None:
    metadata = ffmpeg.probe_video(current, count_mode="metadata", controller=controller)
    width, height = int(metadata["width"]), int(metadata["height"])
    codec = "H.264" if preview else settings.codec
    quality = "Best" if preview else settings.quality
    container = "MP4" if preview else ffmpeg.container_for_codec(codec)
    current_hdr = bool(metadata.get("hdr")) or pipeline_hdr
    hdr = current_hdr and not preview
    source_metadata = ffmpeg.probe_video(source, count_mode="metadata", controller=controller)
    encode_metadata = dict(metadata)
    # FFV1 RGB intermediates report matrix=gbr. Delivery codecs need the
    # original YUV matrix, or BT.2020 for HDR created by Super Resolution.
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
    elif preview and current_hdr:
        encode_metadata.update(color_space="bt709", color_primaries="bt709",
                               color_transfer="bt709", color_range="tv")
    tone_map = None
    if preview and current_hdr:
        tone_map = (
            "zscale=matrixin=gbr:primariesin=bt2020:transferin=smpte2084:rangein=full:"
            "transfer=linear:npl=100,format=gbrpf32le,zscale=primaries=bt709,"
            "tonemap=mobius:desat=2:peak=10,zscale=transfer=bt709:matrix=bt709:"
            "range=limited,format=yuv420p"
        )
    gpu = ffmpeg.resolve_video_gpu(detect_gpus(), settings.video_gpu_uuid,
                                   codec, width, height)
    codec_args, _, _ = _codec_command(codec, quality, width, height,
                                     float(metadata["fps"]),
                                     None if gpu is None else int(gpu["cuda_ordinal"]),
                                     hdr_mode=hdr, hdr_metadata=encode_metadata,
                                     speed_profile="preview" if preview else "default")
    audio = plan_audio_streams(source, container, controller)
    output = OutputFile(destination)
    try:
        command = [str(FFMPEG), "-hide_banner", "-loglevel", "error", "-y",
                   "-i", str(current), "-i", str(source),
                   "-map", "0:v:0", "-map", "1:a?", "-map_metadata", "1",
                   "-map_chapters", "1", *codec_args,
                   *(["-vf", tone_map, "-color_primaries", "bt709", "-color_trc", "bt709",
                      "-colorspace", "bt709", "-color_range", "tv"] if tone_map else []),
                   *audio.encoder_args(), "-fps_mode", "passthrough",
                   "-t", f"{max(float(metadata.get('duration') or 0), 1 / float(metadata['fps'])):.9f}",
                   str(output.temporary)]
        _run_command(command, controller, "Video export")
        if controller.cancel.is_set():
            raise Cancelled("Render stopped by user.")
        output.publish()
    finally:
        output.cleanup()


def render_item(source: Path, settings: UISettings, mode: str, destination: Path,
                controller: JobController, progress=None, *, preview: bool = False,
                capabilities_checked: bool = False,
                preview_cache: PreviewStageCache | None = None,
                source_cache_key: str | None = None) -> str:
    preflight_source(source, settings, mode)
    if not capabilities_checked and preview_cache is None:
        preflight_capabilities(settings, mode, controller)
    stages = enabled_stages(settings, mode)
    upstream_key = (source_cache_key or cache_key({"kind": "source", "mode": mode,
                                                   "source": file_identity(source)})) if preview_cache else ""
    JOBS.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="visual-workflow-", dir=JOBS) as temp:
        current = source
        hdr = False
        if mode == "Video":
            hdr = bool(ffmpeg.probe_video(source, count_mode="metadata", controller=controller).get("hdr"))
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
                                 f"{STAGE_LABELS[stage]}: using Smart cache")
                    upstream_key = stage_key
                    if stage == "super_resolution" and mode == "Video":
                        hdr = bool(settings.upscale_hdr_enabled)
                    continue
            directory = Path(temp) / f"{index:02d}-{stage}"
            directory.mkdir()
            stage_input = current
            if preview_cache and not capabilities_checked:
                preflight_capabilities(settings, mode, controller, (stage,))
            def report(value, message, i=index, name=stage):
                if progress:
                    progress((i + max(0.0, min(1.0, float(value)))) / (len(stages) + 1),
                             f"{STAGE_LABELS[name]}: {message}")
            if stage == "coloring":
                current = (_coloring_image_stage(current, source, settings, directory, controller, report)
                           if mode == "Image" else
                           _coloring_video_stage(current, settings, directory, controller, report))
            elif mode == "Image":
                current = _image_stage(current, stage, settings, directory, controller, report)
            else:
                current = _video_stage(current, stage, settings, hdr, directory, controller, report)
            if controller.cancel.is_set():
                raise Cancelled("Render stopped by user.")
            if preview_cache:
                current = preview_cache.publish(stage_key, directory, current,
                                                previous=stage_input)
                upstream_key = stage_key
            if stage == "super_resolution" and mode == "Video":
                hdr = bool(settings.upscale_hdr_enabled)
        if progress:
            progress(len(stages) / (len(stages) + 1), "Exporting result")
        destination.parent.mkdir(parents=True, exist_ok=True)
        if mode == "Image":
            _export_image(current, destination, settings, controller)
        else:
            _export_video(source, current, destination, settings, controller,
                          preview=preview, pipeline_hdr=hdr)
        if progress:
            progress(1.0, "Export complete")
    return str(destination)


def render_pipeline_batch(input_paths, settings: UISettings, mode: str,
                          progress=None, *, output_dir=None, controller=None,
                          on_item_update=None, same_as_input=False) -> PipelineBatchResult:
    paths = [Path(path).resolve() for path in input_paths]
    if not paths:
        raise ValueError("Add a file before rendering.")
    controller = controller or JobController()
    for path in paths:
        preflight_source(path, settings, mode)
    preflight_capabilities(settings, mode, controller)
    reporter = BatchProgress(paths, on_item_update, progress)
    successes: list[PipelineSuccess] = []
    failures: list[PipelineFailure] = []
    reserved: set[Path] = set()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    try:
        for index, path in enumerate(paths):
            if controller.cancel.is_set():
                break
            reporter.advance(index, 0.0, "Preparing pipeline")
            folder = prepare_output_dir(path.parent if same_as_input else output_dir, default=OUTPUTS)
            extension = (IMAGE_EXTENSIONS[settings.image_format] if mode == "Image" else
                         {"MP4": ".mp4", "MKV": ".mkv", "MOV": ".mov"}[ffmpeg.container_for_codec(settings.codec)])
            rename_mode = settings.image_rename_mode if mode == "Image" else settings.video_rename_mode
            suffix = settings.image_custom_suffix if mode == "Image" else settings.video_custom_suffix
            output = unique_output_path(folder / output_filename(
                path, extension, rename_mode, suffix, f"{path.stem}_Pipeline_{stamp}"), reserved)
            reserved.add(output)
            try:
                rendered = render_item(path, settings, mode, output, controller,
                                       lambda value, message, i=index: reporter.advance(i, value, message),
                                       capabilities_checked=True)
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
                            cache_dir: Path = PREVIEW_CACHE) -> tuple[str, str]:
    controller = controller or JobController()
    source_path = Path(source).resolve()
    preflight_source(source_path, settings, mode)
    if mode == "Image" and not enabled_stages(settings, mode):
        decoded = decode_image(source_path)
        return decoded.rgba, "Source preview (all processing cards are off)."
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
        output = unique_output_path(output_dir / f"pipeline-preview-{time.time_ns()}.mp4")
        render_item(clip, settings, mode, output, controller, progress, preview=True,
                    preview_cache=stage_cache,
                    source_cache_key=clip_key)
    prune_preview_cache(root=stage_cache.root, protected_keys=stage_cache.used_keys,
                        min_idle_seconds=0)
    return str(output), "Pipeline video preview complete."


def _extract_lossless_preview_clip(source: Path, directory: Path,
                                   controller: JobController, start_seconds: float,
                                   clip_seconds: float | None) -> Path:
    """Frame-accurate clip cut without changing pixels before the first card."""
    metadata = ffmpeg.probe_video(source, count_mode="metadata", controller=controller)
    start = max(0.0, float(start_seconds or 0.0) - 0.002)
    fast_seek = max(0.0, start - 2.0)
    accurate_seek = max(0.0, start - fast_seek)
    output = directory / "source-clip.mkv"
    color_args: list[str] = []
    for key, flag in (("color_primaries", "-color_primaries"),
                      ("color_transfer", "-color_trc"),
                      ("color_space", "-colorspace")):
        value = metadata.get(key)
        if value and value != "unknown":
            color_args.extend((flag, str(value)))
    command = [str(FFMPEG), "-hide_banner", "-loglevel", "error", "-y",
               "-ss", f"{fast_seek:.6f}", "-i", str(source),
               "-ss", f"{accurate_seek:.6f}",
               *(["-frames:v", "1"] if clip_seconds is None else
                 ["-t", f"{float(clip_seconds):.6f}"]),
               "-map", "0:v:0", "-map_metadata", "0", "-an",
               "-c:v", "ffv1", "-level", "3", "-pix_fmt", "gbrp10le",
               *color_args, str(output)]
    _run_command(command, controller, "Preview clip extraction")
    return output
