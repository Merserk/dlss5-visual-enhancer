from __future__ import annotations

from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path

from ...core import app_log, ffmpeg
from ...core.gpu_detection import detect_gpus
from ...core.jobs import Cancelled, active_job
from .cuda_pipeline import convert_video_cuda_nvenc, is_nvenc
from .host_pipeline import convert_video_inprocess_host
from .media import inspect_video
from .models import UpscaleOptions, UpscaleResult, output_size
from .native import probe_capabilities


def upscale_video(input_path, options: UpscaleOptions | None = None, progress=None, *,
                  output_dir=None, controller=None, _owns_slot=False,
                  _capabilities=None) -> UpscaleResult:
    options = replace(options) if options else UpscaleOptions()
    options.container = ffmpeg.container_for_codec(options.codec)
    options.validate()
    source = Path(input_path).resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    context = nullcontext(controller) if _owns_slot else active_job(controller)
    with context as controller:
        return _process(source, options, progress, output_dir, controller, _capabilities)


def _process(source, options, progress, output_dir, controller, capabilities):
    app_log.info("upscale", f"start src={source.name}")
    try:
        if progress:
            progress(0.01, "Checking SDR source and RTX Video capabilities")
        metadata = inspect_video(source, controller, reject_hdr=True)
        output_width, output_height, _ = output_size(
            metadata["width"], metadata["height"], options)
        capabilities = capabilities or probe_capabilities(
            options.ai_gpu_uuid, controller=controller)
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
            capabilities=capabilities)
    except BaseException as exc:
        cancelled = controller.cancel.is_set() or isinstance(exc, Cancelled)
        if cancelled:
            app_log.info("upscale", f"cancelled src={source.name}")
            if not isinstance(exc, Cancelled):
                raise Cancelled("Upscale stopped by user.") from exc
        else:
            app_log.fail("upscale", f"upscale-{source.stem}", exc)
        raise
