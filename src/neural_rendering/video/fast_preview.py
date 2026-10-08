"""Lossless timed Neural Rendering previews from the original CUDA video frames."""

from __future__ import annotations

import math
import subprocess
import tempfile
import time
from contextlib import suppress
from fractions import Fraction
from pathlib import Path

import av
import numpy as np
from av.codec.hwaccel import HWAccel

from ...core import ffmpeg
from ...core.disk_paths import OutputFile, prepare_output_dir
from ...core.gpu_selection import resolve_runtime_ai_gpu
from ...core.jobs import Cancelled, JobController, active_job
from ...core.naming import unique_output_path
from ...core.paths import FFMPEG
from ...core.runtime import (DLSSFrameSession, prepare_runtime,
                             resolve_native_settings, resolve_upscaling_mode)
from .models import ConversionOptions

class FastClipUnavailable(RuntimeError):
    """The source cannot use the exact-frame CUDA preview route."""

def render_lossless_cuda_preview_clip(
    source: Path, options: ConversionOptions, metadata: dict, *,
    start_seconds: float, frame_count: int, output_dir: Path,
    controller: JobController, progress=None,
) -> Path:
    """Seek to a parked frame, render on CUDA, and encode its RGB samples to FFV1."""
    if (not metadata.get("cfr") or metadata.get("hdr")
            or int(metadata.get("rotation") or 0)
            or int(metadata.get("depth") or 8) > 8
            or any(metadata.get(field) != "bt709" for field in
                   ("color_space", "color_primaries", "color_transfer"))
            or options.scale_method != "Standard"
            or options.upscaling_factor != 1.0
            or options.preserve_hdr):
        raise FastClipUnavailable("Source needs the established lossless clip route.")
    if not source.is_file():
        raise FileNotFoundError(source)
    if frame_count < 1 or not math.isfinite(start_seconds) or start_seconds < 0:
        raise ValueError("Invalid preview frame range.")
    if controller.cancel.is_set():
        raise Cancelled("Preview cancelled.")

    prepared = prepare_runtime()
    gpu = resolve_runtime_ai_gpu(prepared.gpus, prepared.runtime_bundle,
                                 options.ai_gpu_uuid)
    ordinal = int(gpu["cuda_ordinal"])
    width, height = int(metadata["width"]), int(metadata["height"])
    factor, mode = resolve_upscaling_mode(1.0)
    destination = unique_output_path(
        prepare_output_dir(output_dir) / f"neural-preview-{time.time_ns()}.mkv")
    output = OutputFile(destination)
    process = session = None
    published = False
    try:
        with active_job(controller):
            decoder_device = HWAccel(
                "cuda", device=str(ordinal), allow_software_fallback=True,
                options={"primary_ctx": "1"}, is_hw_owned=True,
            )
            with av.open(str(source), hwaccel=decoder_device) as decoded:
                stream = decoded.streams.video[0]
                stream.thread_type = "AUTO"
                origin = float(Fraction(stream.start_time or 0) * stream.time_base)
                aligned = origin + max(0.0, start_seconds - .002)
                if start_seconds > 0:
                    decoded.seek(max(0, math.floor(aligned / float(stream.time_base))),
                                 stream=stream, backward=True, any_frame=False)
                frames = iter(decoded.decode(stream))
                first = None
                for frame in frames:
                    if controller.cancel.is_set():
                        raise Cancelled("Preview cancelled.")
                    if frame.pts is None:
                        raise FastClipUnavailable("Source frames have no timestamps.")
                    timestamp = float(Fraction(frame.pts) *
                                      (frame.time_base or stream.time_base))
                    if timestamp + 1e-9 >= aligned:
                        first = frame
                        break
                if first is None or first.format.name != "cuda":
                    raise FastClipUnavailable("Source has no CUDA frame at this position.")

                command = [
                    str(FFMPEG), "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "rawvideo", "-pixel_format", "rgba64le",
                    "-video_size", f"{width}x{height}",
                    "-framerate", str(metadata["rate"]), "-i", "pipe:0",
                    "-frames:v", str(frame_count), "-an", "-vf",
                    "format=gbrp10le,setparams=colorspace=gbr:range=full:"
                    "color_primaries=bt709:color_trc=bt709",
                    "-c:v", "ffv1", "-level", "3", "-slicecrc", "1",
                    "-pix_fmt", "gbrp10le", "-colorspace", "0", "-color_range", "pc",
                    "-color_primaries", "bt709", "-color_trc", "bt709",
                    str(output.temporary),
                ]
                with tempfile.TemporaryFile() as errors:
                    process = subprocess.Popen(
                        command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                        stderr=errors,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    )
                    controller.register(process)
                    session = DLSSFrameSession(
                        input_width=width, input_height=height,
                        output_width=width, output_height=height,
                        frame_count=None, warmup_frames=options.warmup_frames,
                        factor=factor, mode=mode,
                        native_settings=resolve_native_settings(options),
                        control_mask=options.nr_mask, gpu=gpu,
                        runtime_bundle=prepared.runtime_bundle,
                        controller=controller, cuda_video=True,
                    )
                    pixels = np.empty((height, width, 4), dtype=np.uint16)
                    color_space = str(metadata.get("color_space") or "").casefold()
                    matrix = 2 if "2020" in color_space else (
                        0 if color_space in {"bt470bg", "smpte170m", "smpte240m", "fcc"}
                        else 1)
                    full_range = int(str(metadata.get("color_range") or "").casefold()
                                     in {"pc", "jpeg", "full"})
                    chroma = ffmpeg.chroma_location_code(metadata)
                    delivered = 0
                    last_progress = 0.0
                    pending = first
                    while delivered < frame_count:
                        if controller.cancel.is_set():
                            raise Cancelled("Preview cancelled.")
                        if pending is None:
                            try:
                                pending = next(frames)
                            except StopIteration:
                                break
                        if pending.is_corrupt:
                            raise RuntimeError("Preview source contains a corrupt frame.")
                        if pending.format.name != "cuda" or pending.pts is None:
                            raise FastClipUnavailable("CUDA decode stopped during preview.")
                        score, reset = session.score_cuda_frame(
                            pending, color_matrix=matrix, color_range=full_range)
                        result, result_pts = session.process_cuda_frame_to_host(
                            index=delivered, frame=pending, reset=reset,
                            scene_score=score, pts=int(pending.pts),
                            color_matrix=matrix, color_range=full_range,
                            chroma_location=chroma, output_buffer=pixels)
                        if result_pts != int(pending.pts):
                            raise RuntimeError("Neural preview changed a frame timestamp.")
                        assert process.stdin is not None
                        try:
                            process.stdin.write(memoryview(result).cast("B"))
                        except (OSError, ValueError) as exc:
                            if controller.cancel.is_set():
                                raise Cancelled("Preview cancelled.") from exc
                            raise RuntimeError("Lossless preview encoder stopped early.") from exc
                        delivered += 1
                        pending = None
                        now = time.perf_counter()
                        if progress and (now - last_progress >= .1 or delivered == frame_count):
                            progress(min(.95, delivered / frame_count * .95),
                                     f"Rendering preview frames: {delivered:,} / {frame_count:,}")
                            last_progress = now
                    if not delivered:
                        raise FastClipUnavailable("No frames remain at the preview position.")
                    session.close()
                    session = None
                    assert process.stdin is not None
                    try:
                        process.stdin.close()
                    except OSError as exc:
                        if controller.cancel.is_set():
                            raise Cancelled("Preview cancelled.") from exc
                        raise RuntimeError("Lossless preview encoder stopped early.") from exc
                    while True:
                        if controller.cancel.is_set():
                            raise Cancelled("Preview cancelled.")
                        try:
                            code = process.wait(timeout=.2)
                            break
                        except subprocess.TimeoutExpired:
                            continue
                    if code:
                        errors.seek(0)
                        raise RuntimeError("Lossless preview encoding failed: " +
                                           errors.read().decode("utf-8", "replace")[-2000:])
                    controller.unregister(process)
                    process = None

            verified = ffmpeg.probe_video(output.temporary, count_mode="metadata",
                                          controller=controller)
            if ((int(verified["width"]), int(verified["height"])) != (width, height)
                    or int(verified.get("depth") or 0) < 10):
                raise RuntimeError("Lossless preview lost dimensions or precision.")
            if controller.cancel.is_set():
                raise Cancelled("Preview cancelled.")
            output.publish()
            published = True
            if progress:
                progress(1.0, "Preview complete")
            return destination
    finally:
        if session is not None:
            with suppress(Exception):
                session.abort()
        if process is not None:
            if process.poll() is None:
                process.kill()
            with suppress(Exception):
                process.wait(timeout=5)
            controller.unregister(process)
            if process.stdin is not None:
                with suppress(OSError):
                    process.stdin.close()
        output.cleanup(rollback=not published)
