from __future__ import annotations

import math
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path

from ...core import app_log, ffmpeg
from ...core.ffmpeg.preview import LOSSLESS_PREVIEW_CODEC
from ...core.gpu_detection import detect_gpus
from ...core.jobs import Cancelled, active_job
from .cuda_pipeline import convert_video_cuda_nvenc, is_nvenc
from .host_pipeline import convert_video_inprocess_host
from .media import inspect_video
from .models import UpscaleOptions, UpscaleResult, output_size
from .native import probe_capabilities


def upscale_video(input_path, options: UpscaleOptions | None = None, progress=None, *,
                  output_dir=None, controller=None, _owns_slot=False,
                  _capabilities=None, _preview_start_seconds: float = 0.0) -> UpscaleResult:
    options = replace(options) if options else UpscaleOptions()
    options.validate()
    source = Path(input_path).resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    if (not math.isfinite(_preview_start_seconds) or _preview_start_seconds < 0):
        raise ValueError("Invalid preview seek position.")
    if _preview_start_seconds > 0 and (
            (options.preview_frames is None and options.preview_seconds is None)
            or options.codec != LOSSLESS_PREVIEW_CODEC):
        raise ValueError("Direct seeking requires a lossless preview.")
    context = nullcontext(controller) if _owns_slot else active_job(controller)
    with context as controller:
        return _process(source, options, progress, output_dir, controller,
                        _capabilities, _preview_start_seconds)


def _process(source, options, progress, output_dir, controller, capabilities,
             preview_start_seconds):
    app_log.info("upscale", f"start src={source.name}")
    try:
        if progress:
            progress(0.01, "Checking SDR source and RTX Video capabilities")
        metadata = inspect_video(source, controller, reject_hdr=True)
        if preview_start_seconds > 0:
            # The established preview's FFV1 seek clip promotes SDR samples to
            # 10-bit RGB before the RTX/DLSS host boundary. Preserve that
            # processing precision while decoding the original video directly.
            metadata = {**metadata, "depth": max(10, int(metadata["depth"]))}
        output_width, output_height, _ = output_size(
            metadata["width"], metadata["height"], options)
        if options.prefer_nvenc and options.codec in {"H.264", "H.265", "AV1"}:
            from ...core.gpu_selection import detect_gpu

            try:
                ai_gpu = detect_gpu(options.ai_gpu_uuid)
                preferred_uuid = (str(ai_gpu["uuid"]) if options.video_gpu_uuid == "auto"
                                  else options.video_gpu_uuid)
                candidate = options.codec + " (NVIDIA NVENC)"
                ffmpeg.resolve_video_gpu(detect_gpus(), preferred_uuid, candidate,
                                         output_width, output_height)
            except (OSError, RuntimeError, ValueError):
                pass
            else:
                app_log.info("upscale", f"automatic NVIDIA NVENC selected for {options.codec}")
                options = replace(options, codec=candidate, video_gpu_uuid=preferred_uuid)
        capabilities = capabilities or (probe_capabilities(
            options.ai_gpu_uuid, controller=controller)
            if options.engine != "DLSS" or options.hdr_enabled else None)
        if options.engine == "DLSS":
            from ...core.gpu_selection import detect_gpu
            from .dlss_pipeline import convert_video_dlss
            from .cuda_pipeline import DLSSNeedsHostFallback
            ai_gpu = detect_gpu(options.ai_gpu_uuid)
            requested_video_gpu = (
                str(ai_gpu["uuid"]) if options.video_gpu_uuid == "auto"
                else options.video_gpu_uuid)
            video_gpu = ffmpeg.resolve_video_gpu(
                detect_gpus(), requested_video_gpu, options.codec,
                output_width, output_height)
            if is_nvenc(options.codec) and video_gpu is None:
                raise RuntimeError("The selected NVIDIA encoder cannot encode this DLSS output size.")
            if is_nvenc(options.codec):
                try:
                    return convert_video_cuda_nvenc(
                        source, options, controller=controller, progress=progress,
                        output_dir=output_dir, metadata=metadata,
                        capabilities=capabilities, video_gpu=video_gpu)
                except DLSSNeedsHostFallback:
                    app_log.info("upscale-dlss", "NVDEC unavailable; using the host frame boundary")
            return convert_video_dlss(
                source, options, controller=controller, progress=progress,
                output_dir=output_dir, metadata=metadata, video_gpu=video_gpu,
                preview_start_seconds=preview_start_seconds)
        requested_video_gpu = (
            str(capabilities.gpu["uuid"])
            if options.video_gpu_uuid == "auto"
            else options.video_gpu_uuid
        )
        video_gpu = ffmpeg.resolve_video_gpu(
            detect_gpus(), requested_video_gpu, options.codec,
            output_width, output_height)
        if is_nvenc(options.codec):
            if video_gpu is None:
                raise RuntimeError(
                    "An NVIDIA encoder was selected but the requested adapter cannot encode "
                    f"{output_width}×{output_height} with {options.codec}.")
            return convert_video_cuda_nvenc(
                source, options, controller=controller, progress=progress,
                output_dir=output_dir, metadata=metadata,
                capabilities=capabilities, video_gpu=video_gpu)
        return convert_video_inprocess_host(
            source, options, controller=controller, progress=progress,
            output_dir=output_dir, metadata=metadata,
            capabilities=capabilities, preview_start_seconds=preview_start_seconds)
    except BaseException as exc:
        cancelled = controller.cancel.is_set() or isinstance(exc, Cancelled)
        if cancelled:
            app_log.info("upscale", f"cancelled src={source.name}")
            if not isinstance(exc, Cancelled):
                raise Cancelled("Upscale stopped by user.") from exc
        else:
            app_log.fail("upscale", f"upscale-{source.stem}", exc)
        raise
