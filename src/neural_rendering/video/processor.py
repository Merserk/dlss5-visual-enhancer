from __future__ import annotations

import math
import os
import queue
import shutil
import subprocess
import threading
import time
from contextlib import nullcontext, suppress
from dataclasses import replace
from fractions import Fraction
from pathlib import Path
from typing import Callable

import av
import numpy as np
from av.codec.hwaccel import HWAccel

from ...core import app_log, ffmpeg
from ...core.gpu_selection import resolve_runtime_ai_gpu
from ...core.jobs import Cancelled, active_job
from ...core.naming import output_filename, validate_rename
from ...core.disk_paths import OutputFile, prepare_output_dir
from ...core.render_metadata import prepare_render_note
from ...core.paths import JOBS, OUTPUTS
from ...core.runtime import (
    DLSSFrameSession, prepare_runtime, resize_fit, rotate_frame,
)
from .guides import GuideFrame, TemporalGuideGenerator
from .models import ConversionOptions, ConversionResult
from .sizing import resolve_native_settings, resolve_output_size, resolve_upscaling_mode

validate_codec_container = ffmpeg.validate_codec_container
_BATCH_CONTEXT = threading.local()


def automatic_gpu_mode(codec: str) -> bool:
    """Use CUDA transport exactly when the selected encoder is NVENC."""
    return ffmpeg._is_nvenc_codec(codec)

def _validate_preview_options(
    options: ConversionOptions,
) -> tuple[float | None, int | None]:
    preview_seconds: float | None = None
    if options.preview_seconds is not None:
        try:
            preview_seconds = float(options.preview_seconds)
        except (TypeError, ValueError) as exc:
            raise ValueError("Preview duration must be a positive number of seconds.") from exc
        if not math.isfinite(preview_seconds) or preview_seconds <= 0:
            raise ValueError("Preview duration must be a positive number of seconds.")

    preview_frames: int | None = None
    if options.preview_frames is not None:
        if isinstance(options.preview_frames, bool):
            raise ValueError("Preview frame count must be a positive integer.")
        try:
            preview_frames = int(options.preview_frames)
        except (TypeError, ValueError) as exc:
            raise ValueError("Preview frame count must be a positive integer.") from exc
        if preview_frames <= 0 or preview_frames != options.preview_frames:
            raise ValueError("Preview frame count must be a positive integer.")
    if preview_seconds is not None and preview_frames is not None:
        raise ValueError("Choose either a timed preview or a frame preview, not both.")
    return preview_seconds, preview_frames


def convert_video(
    input_path: str | os.PathLike[str],
    options: ConversionOptions | None = None,
    progress: Callable[[float, str], None] | None = None,
    *, output_dir: str | os.PathLike[str] | None = None, controller=None,
) -> ConversionResult:
    options = replace(options) if options is not None else ConversionOptions()
    options.container = ffmpeg.container_for_codec(options.codec)
    source = Path(input_path).resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    validate_codec_container(options.codec, options.container)
    validate_rename(options.rename_mode, options.custom_suffix)
    preview_seconds, preview_frames = _validate_preview_options(options)
    is_preview = preview_seconds is not None or preview_frames is not None
    # Compat previews use the forced H.264 SDR 8-bit path; user-encoded previews
    # (Preview Encoding Auto-playable / Disabled) preserve the HDR choice.
    compat_preview = is_preview and bool(getattr(options, "preview_compat", True))
    # Processing-engine selection is codec-driven. Keep the legacy option in
    # the data model for preset/API compatibility, but never let an old saved
    # value override the automatic NVENC/CPU routing decision.
    options.nr_gpu_mode = automatic_gpu_mode(options.codec)
    hdr_requested = bool(options.preserve_hdr)
    if compat_preview:
        hdr_requested = False
    if hdr_requested and not ffmpeg.hdr_mode_supported(options.codec):
        raise ValueError(
            f"HDR Mode is only available for H.265, H.265 (NVIDIA NVENC), AV1, AV1 (NVIDIA NVENC) and ProRes Proxy; "
            f"current codec is {options.codec!r}."
        )
    prepared_runtime = getattr(_BATCH_CONTEXT, "prepared_runtime", None)
    if prepared_runtime is None:
        prepared_runtime = prepare_runtime()
    batch_controller = getattr(_BATCH_CONTEXT, "controller", None)
    job_context = nullcontext(batch_controller) if batch_controller is not None else active_job(controller)

    with job_context as controller:
        assert controller is not None
        if options.nr_gpu_mode:
            from .cuda_pipeline import convert_video_cuda_nvenc

            return convert_video_cuda_nvenc(
                source,
                options,
                preview_seconds=preview_seconds,
                preview_frames=preview_frames,
                compat_preview=compat_preview,
                prepared_runtime=prepared_runtime,
                controller=controller,
                progress=progress,
                output_dir=output_dir,
            )
        started = time.perf_counter()

        def _report_progress(value: float, desc: str) -> None:
            if progress is None:
                return
            try:
                v = float(value)
                v = 0.0 if v < 0 else (1.0 if v > 1 else v)
                elapsed = time.perf_counter() - started
                if 0.01 < v < 0.99 and elapsed > 0.5:
                    eta = elapsed * (1 - v) / max(v, 1e-6)
                    desc = f"{desc} - Time Remaining: {eta:.1f}s"
                else:
                    v = float(value) if isinstance(value, (int, float)) else v
                # Use positional `desc` so it works for both gr.Progress(desc=) and report_item(message)
                progress(v, desc)
            except Exception:
                try:
                    progress(value, desc)
                except Exception:
                    pass

        timings: dict[str, float] = {}
        job_dir: Path | None = None
        output: Path | None = None
        output_file: OutputFile | None = None
        session: DLSSFrameSession | None = None
        gpu: dict | None = resolve_runtime_ai_gpu(
            prepared_runtime.gpus, prepared_runtime.runtime_bundle, options.ai_gpu_uuid
        )
        video_gpu: dict | None = None
        runtime_bundle: dict | None = prepared_runtime.runtime_bundle
        encoder = None
        encoder_setup_thread: threading.Thread | None = None
        producer_thread: threading.Thread | None = None
        writer_thread: threading.Thread | None = None
        pipeline_stop = threading.Event()
        pipeline_errors: queue.Queue[BaseException] = queue.Queue(maxsize=4)
        producer_stats: dict[str, float | int | str] = {}
        writer_stats: dict[str, float | int] = {}
        frame_accounting: dict[str, object] = {"source_verification": "not_required"}

        def record_pipeline_error(exc: BaseException) -> None:
            pipeline_stop.set()
            try:
                pipeline_errors.put_nowait(exc)
            except queue.Full:
                pass

        try:
            stage_started = time.perf_counter()
            metadata = ffmpeg.probe_video(source, count_mode="metadata", controller=controller)
            color_space = str(metadata.get("color_space") or "").casefold()
            color_matrix = 2 if "2020" in color_space else (
                0 if color_space in {"bt470bg", "smpte170m", "smpte240m", "fcc"} else 1
            )
            color_range = int(
                str(metadata.get("color_range") or "").casefold() in {"pc", "jpeg", "full"}
            )
            declared_frames = int(metadata["frames"])
            estimated_frames = declared_frames or max(
                1, int(math.ceil(float(metadata["duration"]) * float(metadata["fps"])))
            )
            if preview_frames is not None:
                estimated_frames = min(estimated_frames, preview_frames) if declared_frames else preview_frames
            elif preview_seconds is not None:
                estimated_frames = min(estimated_frames, max(
                    1, int(math.ceil(preview_seconds * float(metadata["fps"])))
                ))
            frame_accounting.update(
                declared_frames=declared_frames, estimated_frames=estimated_frames,
                metadata_corrected=False,
            )
            timings["probe_seconds"] = time.perf_counter() - stage_started
            input_width = int(metadata["width"])
            input_height = int(metadata["height"])
            factor, mode = resolve_upscaling_mode(options.upscaling_factor)
            output_width, output_height = resolve_output_size(
                input_width, input_height, factor
            )
            # HDR metadata to copy – 10-bit path when HDR Mode is on
            hdr_metadata = None
            effective_hdr = hdr_requested and (not is_preview or not compat_preview)
            if effective_hdr:
                hdr_metadata = {
                    "color_space": metadata.get("color_space", "unknown"),
                    "color_primaries": metadata.get("color_primaries", "unknown"),
                    "color_transfer": metadata.get("color_transfer", "unknown"),
                    "hdr": bool(metadata.get("hdr", False)),
                }
            destination = prepare_output_dir(output_dir, default=OUTPUTS)
            JOBS.mkdir(exist_ok=True)
            app_log.info("video-render", f"start src={source.name}")
            stamp = time.strftime("%Y%m%d-%H%M%S") + f"-{time.time_ns() % 1_000_000:06d}"
            job_dir = JOBS / f"{source.stem}-{stamp}-{os.getpid()}"
            job_dir.mkdir(parents=True, exist_ok=False)
            extension = {"MP4": ".mp4", "MKV": ".mkv", "MOV": ".mov"}.get(
                options.container
            )
            if extension is None:
                raise ValueError(f"Unknown output container: {options.container!r}.")
            output_kind = (
                "DLSS5"
                if not is_preview
                else (
                    "DLSS5_PREVIEW_FRAME"
                    if preview_frames is not None
                    else "DLSS5_PREVIEW"
                )
            )
            output = destination / output_filename(
                source,
                extension,
                options.rename_mode,
                options.custom_suffix,
                f"{source.stem}_{output_kind}_{stamp}",
            )
            output_file = OutputFile(output)
            temp_video = job_dir / (f"processed-video{extension}" if preview_frames is not None else "processed-video.mkv")
            native = resolve_native_settings(options)
            metadata_diagnostics: dict = {}
            render_note = prepare_render_note(options, metadata_diagnostics)
            video_duration = float(metadata.get("video_stream_duration") or 0.0)
            direct_mux = (
                options.container in {"MP4", "MOV"}
                and preview_frames is None
                and (preview_seconds is not None or video_duration > 0.0)
            )
            direct_comment = (
                ffmpeg.prepare_mux_comment(
                    source, options.container, render_note, metadata_diagnostics, controller
                )
                if direct_mux and not is_preview else None
            )
            if direct_mux and is_preview:
                metadata_diagnostics.update(status="skipped", reason="preview_fast_path")
            encoder_target = output_file.temporary if direct_mux else temp_video
            audio_duration = (
                float(preview_seconds)
                if preview_seconds is not None
                else (video_duration if video_duration > 0.0 else None)
            )
            _report_progress(0.01, f"Starting feature 18 on {gpu['display_name']}")

            encoder_setup: list[tuple] = []
            encoding_stage_started = time.perf_counter()

            def prepare_encoder() -> None:
                nonlocal video_gpu
                encoder_started = time.perf_counter()
                try:
                    # GPU ON co-locates NVENC with feature 18 on the AI GPU.
                    # GPU OFF retains the separate Video Processing selection.
                    encoder_gpu_uuid = (
                        str(gpu["uuid"]) if options.nr_gpu_mode else options.video_gpu_uuid
                    )
                    video_gpu = ffmpeg.resolve_video_gpu(
                        prepared_runtime.gpus,
                        encoder_gpu_uuid,
                        "H.264" if compat_preview else options.codec,
                        output_width,
                        output_height,
                    )
                    encoder_setup.append(
                        ffmpeg.start_encoder(
                            encoder_target,
                            options.codec,
                            options.quality,
                            controller,
                            output_width,
                            output_height,
                            float(metadata["fps"]),
                            None if video_gpu is None else int(video_gpu["cuda_ordinal"]),
                            video_gpu is not None,
                            hdr_mode=effective_hdr,
                            hdr_metadata=hdr_metadata,
                            preserve_timestamps=not metadata["cfr"],
                            speed_profile="preview" if compat_preview else "neural",
                            source_audio=source if direct_mux else None,
                            direct_container=options.container if direct_mux else None,
                            comment=direct_comment,
                            audio_duration=audio_duration if direct_mux else None,
                            include_source_metadata=not is_preview,
                        )
                    )
                except BaseException as exc:
                    record_pipeline_error(exc)
                finally:
                    timings["encoder_setup_seconds"] = (
                        time.perf_counter() - encoder_started
                    )

            encoder_setup_thread = threading.Thread(
                target=prepare_encoder, name="dlss5-encoder-setup", daemon=True
            )
            encoder_setup_thread.start()
            preopened_decoder = None
            # Final-residual stabilization needs the original RGBA frame after
            # composition.  Keep decode on the host for this non-NVENC path;
            # feature 18 and optical flow still execute on the selected GPU.
            if options.nr_gpu_mode and float(options.shimmer_suppression) <= 0.0:
                # FFmpeg's CUDA hwdevice must be created before NGX retains the
                # CUDA primary context on affected Windows driver versions.
                decode_device = HWAccel(
                    "cuda",
                    device=str(int(gpu.get("cuda_ordinal", gpu.get("index", 0)))),
                    allow_software_fallback=True,
                    options={"primary_ctx": "1"},
                    is_hw_owned=True,
                )
                preopened_decoder = av.open(str(source), hwaccel=decode_device)
            session_started = time.perf_counter()
            session = DLSSFrameSession(
                input_width=input_width,
                input_height=input_height,
                output_width=output_width,
                output_height=output_height,
                frame_count=None,
                warmup_frames=options.warmup_frames,
                factor=factor,
                mode=mode,
                native_settings=native,
                composition_mask=options.nr_mask,
                gpu=gpu,
                runtime_bundle=runtime_bundle,
                controller=controller,
            )
            timings["native_setup_seconds"] = time.perf_counter() - session_started
            encoder_setup_thread.join()
            encoder_setup_thread = None
            timings["setup_seconds"] = max(
                timings["native_setup_seconds"],
                timings.get("encoder_setup_seconds", 0.0),
            )
            if not pipeline_errors.empty():
                raise pipeline_errors.get_nowait()
            if not encoder_setup:
                raise RuntimeError("Video encoder did not finish preparing.")
            render_width = session.render_width
            render_height = session.render_height
            setup_result = session.setup_result
            minimum_width = session.minimum_width
            minimum_height = session.minimum_height
            maximum_width = session.maximum_width
            maximum_height = session.maximum_height
            _report_progress(
                0.03,
                f"DLSS {mode['name']}: {render_width}×{render_height} → "
                f"{output_width}×{output_height}",
            )

            (
                encoder,
                encoder_log_thread,
                encoder_logs,
                selected_encoder,
                encoding_quality,
            ) = encoder_setup[0]
            assert encoder.stdin is not None

            prepared_bytes = render_width * render_height * 4
            rendered_bytes = output_width * output_height * 4
            queue_slots = max(
                1,
                min(3, (384 * 1024 * 1024) // max(1, prepared_bytes + rendered_bytes)),
            )
            prepared_frames: queue.Queue[object] = queue.Queue(maxsize=queue_slots)
            rendered_frames: queue.Queue[object] = queue.Queue(maxsize=queue_slots)
            output_pool: queue.LifoQueue[np.ndarray] = queue.LifoQueue(maxsize=queue_slots + 1)
            for _ in range(queue_slots + 1):
                output_pool.put(np.empty((output_height, output_width, 4), dtype=np.uint8))
            stop_marker = object()

            def put_pipeline(target: queue.Queue[object], item: object) -> bool:
                while not pipeline_stop.is_set():
                    if controller.cancel.is_set():
                        return False
                    try:
                        target.put(item, timeout=0.1)
                        return True
                    except queue.Full:
                        continue
                return False

            def get_reusable(target: queue.LifoQueue[np.ndarray]) -> np.ndarray | None:
                while not pipeline_stop.is_set():
                    if controller.cancel.is_set():
                        return None
                    try:
                        return target.get(timeout=0.1)
                    except queue.Empty:
                        if not pipeline_errors.empty():
                            return None
                        continue
                return None

            def produce_frames() -> None:
                producer_started = time.perf_counter()
                decoded = 0
                container = None
                decode_seconds = 0.0
                prepare_seconds = 0.0
                guide_seconds = 0.0
                queue_wait_seconds = 0.0
                hardware_frames = 0
                software_frames = 0
                try:
                    if preopened_decoder is not None:
                        container = preopened_decoder
                    else:
                        container = av.open(str(source))
                    stream = container.streams.video[0]
                    stream.thread_type = "AUTO"
                    guides = TemporalGuideGenerator(render_width, render_height)
                    first_time: float | None = None
                    rate = float(stream.average_rate or 30)
                    default_duration = max(
                        1,
                        round(Fraction(1, 1) / metadata["rate"] / metadata["time_base"]),
                    )
                    decoder = iter(container.decode(stream))
                    while True:
                        if controller.cancel.is_set():
                            raise Cancelled("Render stopped by user.")
                        if pipeline_stop.is_set():
                            return
                        # Check a frame-count preview before asking the decoder for
                        # another frame.  The old for-loop decoded one unnecessary
                        # frame for every one-frame preview.
                        if preview_frames is not None and decoded >= preview_frames:
                            producer_stats["completion_reason"] = "preview_limit"
                            break
                        decode_started = time.perf_counter()
                        try:
                            frame = next(decoder)
                        except StopIteration:
                            decode_seconds += time.perf_counter() - decode_started
                            producer_stats["completion_reason"] = "eof"
                            break
                        decode_seconds += time.perf_counter() - decode_started
                        index = decoded
                        pts = int(
                            frame.pts if frame.pts is not None else decoded * default_duration
                        )
                        if preview_seconds is not None:
                            timestamp = float(Fraction(pts) * metadata["time_base"])
                            if first_time is None:
                                first_time = timestamp
                            if decoded and timestamp - first_time >= preview_seconds:
                                producer_stats["completion_reason"] = "preview_limit"
                                break
                        if frame.is_corrupt:
                            raise RuntimeError(
                                f"The video decoder marked source frame {index} as corrupt."
                            )
                        if frame.format.name == "cuda":
                            hardware_frames += 1
                            guide_started = time.perf_counter()
                            scene_score, reset = session.score_cuda_frame(
                                frame, color_matrix=color_matrix, color_range=color_range
                            )
                            guide = GuideFrame(reset=reset, scene_score=scene_score)
                            guide_seconds += time.perf_counter() - guide_started
                            prepared = frame
                        else:
                            software_frames += 1
                            prepare_started = time.perf_counter()
                            rgba = rotate_frame(
                                frame.to_ndarray(format="rgba"), metadata["rotation"]
                            )
                            if rgba.shape[1] != render_width or rgba.shape[0] != render_height:
                                rgba = resize_fit(rgba, render_width, render_height)
                            rgba = np.ascontiguousarray(rgba, dtype=np.uint8)
                            prepare_seconds += time.perf_counter() - prepare_started
                            guide_started = time.perf_counter()
                            guide = guides.process(rgba)
                            guide_seconds += time.perf_counter() - guide_started
                            prepared = rgba
                        duration = int(getattr(frame, "duration", None) or default_duration)
                        queue_started = time.perf_counter()
                        queued = put_pipeline(
                            prepared_frames,
                            (index, prepared, guide, pts, duration),
                        )
                        queue_wait_seconds += time.perf_counter() - queue_started
                        if not queued:
                            return
                        decoded += 1
                    put_pipeline(prepared_frames, stop_marker)
                    producer_stats["duplicate_frames"] = guides.duplicate_frames
                except BaseException as exc:
                    record_pipeline_error(exc)
                    put_pipeline(prepared_frames, stop_marker)
                finally:
                    producer_stats["decoded_frames"] = decoded
                    producer_stats["decode_seconds"] = decode_seconds
                    producer_stats["frame_prepare_seconds"] = prepare_seconds
                    producer_stats["guide_seconds"] = guide_seconds
                    producer_stats["queue_wait_seconds"] = queue_wait_seconds
                    producer_stats["hardware_frames"] = hardware_frames
                    producer_stats["software_frames"] = software_frames
                    if container is not None:
                        with suppress(Exception):
                            container.close()
                    producer_stats["seconds"] = time.perf_counter() - producer_started

            def write_frames() -> None:
                writer_started = time.perf_counter()
                written = 0
                queue_wait_seconds = 0.0
                mux_seconds = 0.0
                nut = None
                try:
                    nut = ffmpeg.RawVideoPacketMuxer(
                        encoder.stdin,
                        width=output_width,
                        height=output_height,
                        rate=metadata["rate"],
                        time_base=metadata["time_base"],
                    )
                    while not pipeline_stop.is_set():
                        if controller.cancel.is_set():
                            raise Cancelled("Render stopped by user.")
                        queue_started = time.perf_counter()
                        try:
                            item = rendered_frames.get(timeout=0.1)
                        except queue.Empty:
                            queue_wait_seconds += time.perf_counter() - queue_started
                            continue
                        queue_wait_seconds += time.perf_counter() - queue_started
                        if item is stop_marker:
                            break
                        processed, output_pts, duration = item
                        try:
                            mux_started = time.perf_counter()
                            nut.write(processed, output_pts, duration)
                            mux_seconds += time.perf_counter() - mux_started
                            written += 1
                        finally:
                            output_pool.put(processed)
                    if not pipeline_stop.is_set():
                        nut.close()
                        nut = None
                except BaseException as exc:
                    record_pipeline_error(exc)
                finally:
                    writer_stats["written_frames"] = written
                    writer_stats["queue_wait_seconds"] = queue_wait_seconds
                    writer_stats["raw_mux_seconds"] = mux_seconds
                    if nut is not None:
                        with suppress(Exception):
                            nut.close()
                    writer_stats["seconds"] = time.perf_counter() - writer_started

            producer_thread = threading.Thread(
                target=produce_frames, name="dlss5-video-producer", daemon=True
            )
            writer_thread = threading.Thread(
                target=write_frames, name="dlss5-video-writer", daemon=True
            )
            producer_thread.start()
            writer_thread.start()
            delivered = 0
            scene_resets = 0
            preview_pts_origin: int | None = None
            dlss_seconds = 0.0
            last_progress_update = 0.0
            while True:
                if controller.cancel.is_set():
                    raise Cancelled("Render stopped by user.")
                if not pipeline_errors.empty():
                    raise pipeline_errors.get_nowait()
                try:
                    item = prepared_frames.get(timeout=0.1)
                except queue.Empty:
                    continue
                if item is stop_marker:
                    break
                index, prepared, guide, pts, duration = item
                scene_resets += int(guide.reset and index != 0)
                output_buffer = get_reusable(output_pool)
                if output_buffer is None:
                    if not pipeline_errors.empty():
                        raise pipeline_errors.get_nowait()
                    raise Cancelled("Render stopped by user.")
                dlss_started = time.perf_counter()
                try:
                    if isinstance(prepared, av.VideoFrame) and prepared.format.name == "cuda":
                        processed, out_pts = session.process_cuda_frame_to_host(
                            index=index,
                            frame=prepared,
                            reset=guide.reset,
                            scene_score=guide.scene_score,
                            pts=pts,
                            color_matrix=color_matrix,
                            color_range=color_range,
                            rotation=int(metadata["rotation"]),
                            output_buffer=output_buffer,
                        )
                    else:
                        processed, out_pts = session.process(
                            index=index,
                            rgba=prepared,
                            reset=guide.reset,
                            pts=pts,
                            output_buffer=output_buffer,
                        )
                except BaseException:
                    output_pool.put(output_buffer)
                    raise
                dlss_seconds += time.perf_counter() - dlss_started
                if is_preview:
                    if preview_pts_origin is None:
                        preview_pts_origin = out_pts
                    out_pts -= preview_pts_origin
                if not put_pipeline(rendered_frames, (processed, out_pts, duration)):
                    output_pool.put(processed)
                    if not pipeline_errors.empty():
                        raise pipeline_errors.get_nowait()
                    raise Cancelled("Render stopped by user.")
                delivered += 1
                frame_accounting["processed_frames"] = delivered
                now = time.perf_counter()
                if now - last_progress_update >= 0.1:
                    _report_progress(
                        0.04 + 0.84 * min(1.0, delivered / estimated_frames),
                        f"DLSS 5 frame {delivered} (estimated {estimated_frames})",
                    )
                    last_progress_update = now

            if not delivered:
                raise RuntimeError("The input video contains no decodable frames.")
            if not put_pipeline(rendered_frames, stop_marker):
                if not pipeline_errors.empty():
                    raise pipeline_errors.get_nowait()
                raise Cancelled("Render stopped by user.")
            producer_thread.join()
            producer_thread = None
            writer_thread.join()
            writer_thread = None
            if not pipeline_errors.empty():
                raise pipeline_errors.get_nowait()
            frame_accounting.update(
                decoded_frames=producer_stats.get("decoded_frames"),
                written_frames=writer_stats.get("written_frames"),
                completion_reason=producer_stats.get("completion_reason"),
            )
            if (producer_stats.get("decoded_frames") != delivered
                    or writer_stats.get("written_frames") != delivered
                    or producer_stats.get("completion_reason") not in {"eof", "preview_limit"}):
                raise RuntimeError(f"Video pipeline did not deliver every decoded frame: {frame_accounting}")
            timings["producer_seconds"] = float(producer_stats.get("seconds", 0.0))
            timings["decode_and_guide_seconds"] = timings["producer_seconds"]
            timings["source_decode_seconds"] = float(producer_stats.get("decode_seconds", 0.0))
            timings["frame_prepare_seconds"] = float(producer_stats.get("frame_prepare_seconds", 0.0))
            timings["guide_seconds"] = float(producer_stats.get("guide_seconds", 0.0))
            timings["producer_queue_wait_seconds"] = float(producer_stats.get("queue_wait_seconds", 0.0))
            timings["dlss_seconds"] = dlss_seconds
            timings["encoder_feed_seconds"] = float(writer_stats.get("seconds", 0.0))
            timings["raw_nut_mux_seconds"] = float(writer_stats.get("raw_mux_seconds", 0.0))
            timings["writer_queue_wait_seconds"] = float(writer_stats.get("queue_wait_seconds", 0.0))
            if encoder.stdin and not encoder.stdin.closed:
                encoder.stdin.close()
            session.close()
            for name, value in session.process_timings.items():
                timings[f"native_{name}"] = float(value)
            frame_accounting["bridge_completed_frames"] = session.completed_frames
            frame_accounting["duplicate_frames"] = int(producer_stats.get("duplicate_frames", 0))
            if session.completed_frames != delivered:
                raise RuntimeError("Bridge completion does not match the processed frame count.")
            encoder_code = encoder.wait(timeout=120)
            encoder_log_thread.join(timeout=2)
            controller.unregister(encoder)
            timings["encoding_seconds"] = time.perf_counter() - encoding_stage_started
            if encoder_code:
                raise RuntimeError(
                    "Video encoder failed:\n" + "\n".join(encoder_logs[-40:])
                )

            # Reaching clean EOF in the decoder, successful one-for-one pipeline
            # accounting, the native END acknowledgement, and a successful encoder
            # exit are authoritative.  Do not decode the complete source a second
            # time just because its container omitted or misreported nb_frames.
            if producer_stats["completion_reason"] == "eof":
                frame_accounting["source_verification"] = "clean_decoder_eof"
                frame_accounting["metadata_corrected"] = bool(
                    declared_frames and declared_frames != delivered
                )
            else:
                frame_accounting["source_verification"] = "preview_limit"
            timings["source_verification_seconds"] = 0.0

            hardware_frames = int(producer_stats.get("hardware_frames", 0))
            software_frames = int(producer_stats.get("software_frames", 0))
            session.diagnostics.decode_backend = (
                "nvdec" if hardware_frames and not software_frames else
                "software" if software_frames and not hardware_frames else
                "nvdec+software"
            )
            session.diagnostics.encode_backend = selected_encoder
            nr_count = delivered
            resize_method = "none" if factor == 1.0 else "lanczos"
            memory_path = session.diagnostics.memory_path
            mux_started = time.perf_counter()
            if direct_mux:
                # MP4/MOV are encoded together with source audio/metadata in the
                # same FFmpeg pass, eliminating the complete intermediate-video
                # write/read/remux cycle.
                _report_progress(0.93, "Finalizing direct audio/video mux")
                ffmpeg.verify_mux_comment(
                    output_file.temporary, direct_comment, metadata_diagnostics, controller
                )
                timings["final_mux_seconds"] = 0.0
            elif preview_frames is not None:
                # A frame preview is a visual test, not a production master.  The
                # encoder already wrote the requested container; avoid reopening the
                # source for audio/chapters/metadata and remuxing a one-frame file.
                _report_progress(0.93, "Finalizing one-frame preview")
                shutil.copyfile(temp_video, output_file.temporary)
                metadata_diagnostics.update(status="skipped", reason="preview_fast_path")
                timings["final_mux_seconds"] = 0.0
            else:
                _report_progress(0.91, "Muxing original audio and metadata")
                ffmpeg.final_mux(
                    temp_video, source, output_file.temporary, options.container, controller,
                    render_note=render_note, metadata_diagnostics=metadata_diagnostics,
                )
                timings["final_mux_seconds"] = time.perf_counter() - mux_started
            timings["muxing_seconds"] = timings["final_mux_seconds"]

            # Header/stream verification is enough on the normal success path.  The
            # application already counted every decoded, DLSS-returned and written
            # frame; scanning or decoding the finished file again is pure O(file) I/O.
            verify_started = time.perf_counter()
            _report_progress(0.96, "Verifying saved output")
            verified = ffmpeg.probe_video(
                output_file.temporary, count_mode="metadata", controller=controller
            )
            declared_output_frames = int(verified.get("frames") or 0)
            frame_accounting["verified_output_frames"] = (
                declared_output_frames if declared_output_frames else delivered
            )
            frame_accounting["output_verification"] = (
                "container_frame_count" if declared_output_frames else "pipeline_accounting"
            )
            if declared_output_frames and declared_output_frames != delivered:
                raise RuntimeError(
                    f"Output container reports {declared_output_frames} frames instead of "
                    f"{delivered}."
                )
            if (verified["width"], verified["height"]) != (
                output_width,
                output_height,
            ):
                raise RuntimeError(
                    f"Output verification found {verified['width']}×{verified['height']} "
                    f"instead of {output_width}×{output_height}."
                )
            timings["verification_seconds"] = time.perf_counter() - verify_started

            elapsed = time.perf_counter() - started
            report_path = app_log.session_path()
            if controller.cancel.is_set():
                app_log.info("video-render", f"cancelled src={source.name} frames={delivered}")
                raise Cancelled("Render stopped by user.")
            app_log.info(
                "video-render",
                f"done src={source.name} out={output.name} frames={delivered} "
                f"elapsed={elapsed:.0f}s avg={delivered / max(elapsed, 1e-9):.1f}fps",
            )
            output_file.publish()
            _report_progress(1.0, "Complete — feature 18 confirmed")
            return ConversionResult(
                output_path=str(output), report_path=report_path, frames=delivered,
                nr_count_evidence=nr_count, elapsed_seconds=elapsed,
                gpu=gpu["display_name"], input_width=input_width,
                input_height=input_height, render_width=render_width,
                render_height=render_height, output_width=output_width,
                output_height=output_height, upscaling_factor=factor,
                neural_dimensions={"width": render_width, "height": render_height},
                resize_method=resize_method, memory_path=memory_path,
                bridge_status=session.structured_status(),
            )
        except Exception as exc:
            was_cancelled = controller.cancel.is_set()
            pipeline_stop.set()
            if controller.cancel.is_set():
                controller.stop()
            else:
                controller.terminate_processes()
            for target in (prepared_frames if "prepared_frames" in locals() else None,
                           rendered_frames if "rendered_frames" in locals() else None):
                if target is not None:
                    with suppress(queue.Full):
                        target.put_nowait(stop_marker)
            for thread in (encoder_setup_thread, producer_thread, writer_thread):
                if thread is not None:
                    thread.join(timeout=2)
            if session is not None and not session.closed:
                with suppress(Exception):
                    session.abort()
            if encoder is not None and encoder.stdin and not encoder.stdin.closed:
                with suppress(OSError):
                    encoder.stdin.close()
            if output_file is not None:
                output_file.cleanup(rollback=True)
            if was_cancelled and not isinstance(exc, Cancelled):
                raise Cancelled("Render stopped by user.") from exc
            if isinstance(exc, Cancelled):
                app_log.info("video-render", f"cancelled src={source.name}")
                raise
            tails: dict[str, object] = {}
            if session is not None:
                with suppress(Exception):
                    tails["bridge"] = session.bridge_logs[-20:]
            if "encoder_logs" in locals() and encoder_logs:
                tails["ffmpeg"] = list(encoder_logs)[-40:]
            report_path = app_log.fail("video-render", f"video-render-{source.stem}", exc, tails or None)
            raise RuntimeError(f"{exc}\nDetails: {report_path}") from exc
        finally:
            if output_file is not None:
                output_file.cleanup()
            pipeline_stop.set()
            # Also reap encoders from a failed parallel setup, before `encoder`
            # has been assigned. Pipes/log readers must not outlive the job.
            encoder_resources = encoder_setup[0] if "encoder_setup" in locals() and encoder_setup else None
            if encoder_resources is not None:
                encoder_process, log_thread = encoder_resources[:2]
                if encoder_process.poll() is None:
                    encoder_process.terminate()
                    try:
                        encoder_process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        encoder_process.kill()
                        encoder_process.wait(timeout=5)
                log_thread.join(timeout=2)
                controller.unregister(encoder_process)
                for stream in (encoder_process.stdin, encoder_process.stdout, encoder_process.stderr):
                    if stream is not None and not stream.closed:
                        with suppress(OSError):
                            stream.close()
            if job_dir and job_dir.exists():
                shutil.rmtree(job_dir, ignore_errors=True)
            if "preopened_decoder" in locals() and preopened_decoder is not None:
                with suppress(Exception):
                    preopened_decoder.close()
