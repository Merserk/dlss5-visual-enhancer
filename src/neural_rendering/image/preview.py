from __future__ import annotations

import time
from contextlib import suppress
from dataclasses import replace
from pathlib import Path
from typing import Callable

import cv2
import numpy as np
from PIL import Image

from ...core.gpu_selection import resolve_runtime_ai_gpu
from ...core.jobs import Cancelled, JobController, active_job
from ...core.runtime import (
    DLSSFrameSession,
    prepare_runtime,
    resize_fit,
    resolve_native_settings,
    resolve_output_size,
    resolve_upscaling_mode,
    verify_feature_18,
)
from .batch import _validate_options
from .decoder import decode_image
from .encoder import save_full_size_image_preview
from .models import ImageConversionOptions


def _update(controller: JobController, progress: Callable[[float, str], None] | None,
            value: float, message: str) -> None:
    if controller.cancel.is_set():
        raise Cancelled("Image preview stopped by user.")
    if progress is not None:
        progress(value, message)


def render_image_preview(
    input_path: str | Path,
    options: ImageConversionOptions | None = None,
    progress: Callable[[float, str], None] | None = None,
    *,
    controller: JobController | None = None,
) -> tuple[Image.Image | str, str]:
    """Run the real single-image feature-18 path without publishing any files."""
    options = _validate_options(replace(options) if options else ImageConversionOptions())
    source = Path(input_path).resolve()
    if not source.is_file():
        raise FileNotFoundError(source)

    started = time.monotonic()
    session: DLSSFrameSession | None = None
    processed = None
    render_rgba = None
    with active_job(controller) as controller:
        try:
            _update(controller, progress, .01, "Decoding image")
            decoded = decode_image(source)
            height, width = decoded.rgba.shape[:2]
            if width < 64 or height < 64:
                raise ValueError(
                    f"{source.name} is {width}×{height}; DLSS requires at least 64×64."
                )

            dlss_details = None
            if options.scale_method == "DLSS":
                from ...core.dlss_bridge import process_image as process_dlss_image
                _update(controller, progress, .08, "Processing DLSS phases")
                dlss_rgba, dlss_details = process_dlss_image(
                    decoded.rgba, options.dlss_mode, options.dlss_preset,
                    gpu_uuid=options.ai_gpu_uuid, controller=controller)
                output_height, output_width = dlss_rgba.shape[:2]
            else:
                dlss_rgba = None
                output_width, output_height = resolve_output_size(
                    width, height, options.upscaling_factor)
            prepared = prepare_runtime()
            gpu = resolve_runtime_ai_gpu(
                prepared.gpus, prepared.runtime_bundle, options.ai_gpu_uuid,
            )
            factor, mode = resolve_upscaling_mode(
                1.0 if dlss_rgba is not None else options.upscaling_factor)
            native = resolve_native_settings(options)

            _update(controller, progress, .12, "Starting D3D12/NGX bridge")
            session = DLSSFrameSession(
                input_width=output_width if dlss_rgba is not None else width,
                input_height=output_height if dlss_rgba is not None else height,
                output_width=output_width,
                output_height=output_height,
                frame_count=None,
                warmup_frames=options.warmup_frames,
                factor=factor,
                mode=mode,
                native_settings=native,
                composition_mask=options.nr_mask,
                gpu=gpu,
                runtime_bundle=prepared.runtime_bundle,
                controller=controller,
            )
            _update(controller, progress, .28, "Processing image")
            render_rgba = resize_fit(
                dlss_rgba if dlss_rgba is not None else decoded.rgba,
                session.render_width, session.render_height,
            )
            session.diagnostics.source_format = (
                f"{decoded.metadata['source_format']}/{decoded.metadata['source_bit_depth']}"
            )
            processed, _pts = session.process(
                index=0, rgba=render_rgba, reset=True, pts=0,
            )

            _update(controller, progress, .72, "Verifying feature 18")
            verify_feature_18(session.bridge_logs, session.structured_status())

            if decoded.alpha is None:
                processed[..., 3] = np.iinfo(processed.dtype).max
            elif decoded.alpha.shape == (output_height, output_width):
                processed[..., 3] = decoded.alpha
            else:
                processed[..., 3] = cv2.resize(
                    decoded.alpha,
                    (output_width, output_height),
                    interpolation=cv2.INTER_LANCZOS4,
                )

            render_width = session.render_width
            render_height = session.render_height
            resize_method = "dlss" if dlss_details else ("none" if factor == 1.0 else "lanczos")
            memory_path = session.diagnostics.memory_path
            gpu_name = str(gpu["display_name"])

            _update(controller, progress, .88, "Finalizing Neural Rendering preview")
            session.close()
            session = None

            _update(controller, progress, .94, "Preparing preview")
            preview = save_full_size_image_preview(
                processed, options.output_format, decoded.alpha is not None,
            )
            elapsed = time.monotonic() - started
            status = (
                f"Preview complete: {source.name} | {width}×{height} → "
                f"{output_width}×{output_height} | render {render_width}×{render_height} | "
                f"{resize_method} resize | {memory_path} | GPU: {gpu_name} | "
                f"{'DLSS ' + options.dlss_mode + ' preset ' + options.dlss_preset + ' → ' if dlss_details else ''}"
                f"{elapsed:.2f}s. Feature-18 execution verified.\n"
                "Preview only — no production output image was saved."
            )
            _update(controller, progress, 1.0, "Preview ready")
            return preview, status
        finally:
            if session is not None and not session.closed:
                with suppress(Exception):
                    session.abort()
            processed = None
            render_rgba = None
