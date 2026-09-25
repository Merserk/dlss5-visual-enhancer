from __future__ import annotations

import time
import uuid
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path

import numpy as np
from PIL import Image

from ..video.native import RTXVideoSession, probe_capabilities
from ...core import app_log
from ...core.dlss_bridge import process_image as process_dlss_image
from ...core.disk_paths import OutputFile, prepare_output_dir
from ...core.jobs import Cancelled, active_job
from ...core.naming import output_filename, unique_output_path
from ...neural_rendering.image.decoder import decode_image
from ...neural_rendering.image.encoder import (
    _encode_image, save_full_size_image_preview,
)
from ...neural_rendering.image.models import ImageConversionOptions
from .models import IMAGE_EXTENSIONS, ImageUpscaleOptions, ImageUpscaleResult, output_size


def srgb_to_worker(rgba):
    """The video pipeline uses BT.709 primaries with BT.470M (gamma 2.2) RGB."""
    rgb = rgba[..., :3].astype(np.float32) / 255
    linear = np.where(rgb <= .04045, rgb / 12.92, ((rgb + .055) / 1.055) ** 2.4)
    packed = np.empty_like(rgba)
    packed[..., :3] = np.rint(np.clip(linear, 0, 1) ** (1 / 2.2) * 255).astype(np.uint8)
    packed[..., 3] = 255
    return np.ascontiguousarray(packed)


def worker_to_srgb(data, width, height, alpha):
    rgba = np.frombuffer(data, dtype=np.uint8).reshape(height, width, 4).copy()
    linear = (rgba[..., :3].astype(np.float32) / 255) ** 2.2
    rgb = np.where(linear <= .0031308, linear * 12.92, 1.055 * linear ** (1 / 2.4) - .055)
    rgba[..., :3] = np.rint(np.clip(rgb, 0, 1) * 255).astype(np.uint8)
    if alpha is None:
        rgba[..., 3] = 255
    else:
        with Image.fromarray(alpha) as mask:
            with mask.resize((width, height), Image.Resampling.LANCZOS) as scaled:
                rgba[..., 3] = np.asarray(scaled)
    return rgba


def preview_upscale_image(input_path, options=None, progress=None, *, controller=None):
    """Run the real RTX VSR still-image path without publishing output/report files."""
    options = replace(options) if options else ImageUpscaleOptions()
    options.validate()
    source = Path(input_path).resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    started = time.monotonic()

    with active_job(controller) as controller:
        def update(value, message):
            if controller.cancel.is_set():
                raise Cancelled("Image upscale preview stopped by user.")
            if progress:
                progress(value, message)

        update(.01, "Decoding image")
        decoded = decode_image(source)
        height, width = decoded.rgba.shape[:2]
        ow, oh = output_size(width, height, options)

        if options.engine == "DLSS":
            update(.12, "Processing DLSS image")
            processed, details = process_dlss_image(
                decoded.rgba, options.dlss_mode, options.dlss_preset,
                gpu_uuid=options.ai_gpu_uuid, controller=controller,
            )
            update(.88, "Preparing preview")
            preview = save_full_size_image_preview(
                processed, options.output_format, decoded.alpha is not None)
            status = (
                f"Preview complete: {source.name} | {width}×{height} → {ow}×{oh} | "
                f"DLSS {options.dlss_mode}, preset {options.dlss_preset}, "
                f"one evaluation | source-anchored DLSS | GPU: {details['gpu']} | "
                f"{time.monotonic() - started:.2f}s.\n"
                "Preview only — no production output image was saved."
            )
            update(1.0, "Preview ready")
            return preview, status

        update(.12, "Checking RTX Video capabilities")
        caps = probe_capabilities(options.ai_gpu_uuid, controller=controller)
        update(.24, "Processing with RTX VSR")
        with RTXVideoSession(
            width, height, ow, oh, options.native_options(), 1, caps, controller,
            image_srgb=True,
        ) as session:
            processed = np.frombuffer(
                session.process_frame(np.ascontiguousarray(decoded.rgba)), dtype=np.uint8
            ).reshape(oh, ow, 4).copy()
            if session.completed_frames != 1:
                raise RuntimeError("RTX VSR did not process exactly one image.")
            last_results = tuple(session.last_results)

        update(.88, "Preparing preview")
        preview = save_full_size_image_preview(
            processed, options.output_format, decoded.alpha is not None
        )
        elapsed = time.monotonic() - started
        gpu_name = str(caps.gpu.get("name") or caps.gpu.get("display_name") or "NVIDIA GPU")
        status = (
            f"Preview complete: {source.name} | {width}×{height} → {ow}×{oh} | "
            f"RTX VSR quality {int(options.vsr_quality)} | GPU: {gpu_name} | "
            f"SDK result VSR=0x{int(last_results[0]):08X} | {elapsed:.2f}s.\n"
            "Preview only — no production output image was saved."
        )
        update(1.0, "Preview ready")
        return preview, status


def upscale_image(input_path, options=None, progress=None, *, output_dir=None, controller=None,
                   generate_previews=True, _owns_slot=False, _capabilities=None,
                   _session_cache=None):
    options = replace(options) if options else ImageUpscaleOptions()
    options.validate()
    source = Path(input_path).resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    with nullcontext(controller) if _owns_slot else active_job(controller) as controller:
        return _process(source, options, progress, output_dir, controller, generate_previews,
                        _capabilities, _session_cache)


def _process(source, options, progress, output_dir, controller, generate_previews, capabilities,
             session_cache):
    started = time.monotonic()
    stamp = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
    report_path = app_log.session_path()
    app_log.info("upscale-image", f"start src={source.name}")
    destination_file = None
    session = None

    def update(value, message):
        if controller.cancel.is_set():
            raise Cancelled("Image upscale stopped by user.")
        if progress:
            progress(value, message)

    try:
        update(.01, "Decoding image")
        decoded = decode_image(source)
        height, width = decoded.rgba.shape[:2]
        ow, oh = output_size(width, height, options)
        is_dlss = options.engine == "DLSS"
        caps = None if is_dlss else (capabilities or probe_capabilities(options.ai_gpu_uuid, controller=controller))
        output = unique_output_path(prepare_output_dir(output_dir) / output_filename(
            source, IMAGE_EXTENSIONS[options.output_format], options.rename_mode, options.custom_suffix,
            f"{source.stem}_{'DLSS' if is_dlss else 'RTXIMAGE'}_{stamp}"))
        destination_file = OutputFile(output)
        update(.15, "Processing with DLSS" if is_dlss else "Processing with RTX VSR")
        if is_dlss:
            processed, details = process_dlss_image(
                decoded.rgba, options.dlss_mode, options.dlss_preset,
                gpu_uuid=options.ai_gpu_uuid, controller=controller,
            )
            app_log.info("upscale-image", f"DLSS mode={options.dlss_mode} preset={options.dlss_preset} evaluations={details['evaluations']} guides=estimated render={details['render_width']}x{details['render_height']}")
        else:
            processed = None
        if not is_dlss:
            processed = _process_vsr_frame(decoded, width, height, ow, oh, options, caps, controller, session_cache)
        assert processed is not None
        update(.80, "Saving image")
        export = ImageConversionOptions(output_format=options.output_format, quality=int(options.quality),
                                        preserve_metadata=False)
        warnings = list(decoded.warnings)
        warnings.extend(_encode_image(destination_file.temporary, processed, export, {},
                                      generate_preview=generate_previews, preview_path=output, controller=controller,
                                      has_transparency=decoded.alpha is not None))
        with Image.open(destination_file.temporary) as saved:
            saved.load()
            if saved.size != (ow, oh):
                raise RuntimeError("Saved image dimensions do not match the upscale output.")
        update(.98, "Verifying output")
        elapsed = time.monotonic() - started
        app_log.info("upscale-image", f"done src={source.name} out={output.name} elapsed={elapsed:.1f}s")
        destination_file.publish()
        return ImageUpscaleResult(str(source), str(output), report_path, ow, oh,
                                  time.monotonic() - started, warnings)
    except BaseException as exc:
        cancelled = controller.cancel.is_set() or isinstance(exc, Cancelled)
        if not cancelled:
            tails = {"worker": list(session.logs)[-40:]} if session else None
            report_path = str(app_log.fail("upscale-image", f"upscale-image-{source.stem}", exc, tails))
        else:
            app_log.info("upscale-image", f"cancelled src={source.name}")
        if cancelled and not isinstance(exc, Cancelled):
            raise Cancelled("Image upscale stopped by user.") from exc
        raise
    finally:
        if destination_file:
            destination_file.cleanup()


def _process_vsr_frame(decoded, width, height, ow, oh, options, caps, controller, session_cache):
    key = (width, height, ow, oh, int(options.vsr_quality), str(caps.gpu.get("uuid", "")))
    if session_cache is None:
        session_context = RTXVideoSession(
            width, height, ow, oh, options.native_options(), 1, caps, controller,
            image_srgb=True)
    else:
        session = session_cache.get(key)
        if session is None or session.closed:
            session = RTXVideoSession(
                width, height, ow, oh, options.native_options(), 1, caps, controller,
                image_srgb=True)
            session_cache[key] = session
        session_context = nullcontext(session)
    with session_context as session:
        before = session.completed_frames
        processed = np.frombuffer(
            session.process_frame(np.ascontiguousarray(decoded.rgba)), dtype=np.uint8
        ).reshape(oh, ow, 4).copy()
        if session.completed_frames != before + 1:
            raise RuntimeError("RTX VSR did not process exactly one image.")
    return processed
