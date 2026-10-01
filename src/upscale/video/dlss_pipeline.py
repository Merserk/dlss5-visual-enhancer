"""DLSS video route with separate optional RTX Video HDR evaluation."""

from __future__ import annotations

import math
import tempfile
import time
import uuid
from contextlib import suppress
from fractions import Fraction
from pathlib import Path

import av
from ...core.ffmpeg.frames import open_video_decoder
import cv2
import numpy as np

from ...core import app_log, ffmpeg
from ...core.disk_paths import OutputFile, prepare_output_dir
from ...core.dlss_bridge import DLSSSession
from ...core.jobs import Cancelled
from ...core.naming import output_filename, unique_output_path
from ...core.paths import JOBS
from ...neural_rendering.video.guides import TemporalGuideGenerator
from .media import inspect_video, result_frame
from .models import UpscaleResult, output_size
from .native import FORMAT_RGBA8, FORMAT_R10, RTXVideoSession, probe_capabilities


def convert_video_dlss(source, options, *, controller, progress=None, output_dir=None,
                       metadata=None, video_gpu=None, preview_start_seconds=0.0):
    started = time.perf_counter()
    metadata = metadata or inspect_video(source, controller, reject_hdr=True)
    width, height = int(metadata["width"]), int(metadata["height"])
    ow, oh, _ = output_size(width, height, options)
    source_high_depth = int(metadata["depth"]) > 8
    output_depth = ffmpeg.output_video_depth(metadata["depth"], options.codec, options.hdr_enabled)
    preview = options.preview_frames is not None or options.preview_seconds is not None
    extension = {"MP4": ".mp4", "MKV": ".mkv", "MOV": ".mov"}[options.container]
    stamp = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
    destination = prepare_output_dir(output_dir)
    output = unique_output_path(destination / output_filename(
        source, extension, "Auto" if preview else options.rename_mode,
        options.custom_suffix, f"{source.stem}_DLSS_{stamp}"))
    output_file = OutputFile(output)
    report_path = app_log.session_path()
    input_container = None
    encoder = None
    writer = None
    hdr_session = None
    dlss = None
    delivered = 0
    cuts = 0
    timings = {"decode_seconds": 0.0, "dlss_seconds": 0.0,
               "hdr_seconds": 0.0, "encode_seconds": 0.0}

    def update(value, message):
        if controller.cancel.is_set():
            raise Cancelled("DLSS upscale stopped by user.")
        if progress:
            progress(value, message)

    JOBS.mkdir(exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix="dlss-video-", dir=JOBS) as work:
            temp_video = Path(work) / "encoded.mkv"
            update(.01, "Starting DLSS Super Resolution")
            dlss = DLSSSession(width, height, options.dlss_mode, options.dlss_preset,
                               gpu_uuid=options.ai_gpu_uuid, even=True)
            if (dlss.output_width, dlss.output_height) != (ow, oh):
                raise RuntimeError("DLSS output sizing differs from the video encoder size.")
            if options.hdr_enabled:
                from dataclasses import replace
                caps = probe_capabilities(options.ai_gpu_uuid, controller=controller)
                hdr_session = RTXVideoSession(
                    ow, oh, ow, oh, replace(options, vsr_enabled=False),
                    FORMAT_R10 if source_high_depth else FORMAT_RGBA8, caps, controller)
            encoder, encoder_thread, encoder_logs, selected, quality = ffmpeg.start_encoder(
                temp_video, options.codec, options.quality, controller, ow, oh,
                float(metadata["rate"]),
                None if video_gpu is None else int(video_gpu["cuda_ordinal"]),
                video_gpu is not None, hdr_mode=options.hdr_enabled,
                hdr_metadata={
                    "color_space": "bt2020nc" if options.hdr_enabled else "bt709",
                    "color_primaries": "bt2020" if options.hdr_enabled else "bt709",
                    "color_transfer": "smpte2084" if options.hdr_enabled else "bt709",
                    "color_range": "tv", "hdr": options.hdr_enabled,
                }, preserve_timestamps=True, bounded_logs=True,
                output_depth=output_depth)
            input_container = open_video_decoder(
                source, controller,
                pixel_format="rgba64le" if source_high_depth else "rgba",
                start_seconds=preview_start_seconds)
            stream = input_container.streams.video[0]
            stream.thread_type = "AUTO"
            stream_tb = stream.time_base or Fraction(1, max(1, round(float(metadata["rate"]))))
            writer = ffmpeg.RawVideoPacketMuxer(
                encoder.stdin, width=ow, height=oh, rate=metadata["rate"],
                time_base=stream_tb, pix_fmt="gbrp10le" if hdr_session else "rgba64le" if output_depth > 8 else "rgba")
            guides = TemporalGuideGenerator(width, height, cut_threshold=0.10)
            processed_float = None
            first_pts = None
            last_pts = None
            default_duration = max(1, round(Fraction(1) / metadata["rate"] / stream_tb))
            estimated = int(metadata["frames"] or max(1, math.ceil(metadata["duration"] * float(metadata["rate"]))))
            timings["setup_seconds"] = time.perf_counter() - started
            pipeline_tick = time.perf_counter()
            decoded = iter(input_container.decode(stream))
            while True:
                decode_tick = time.perf_counter()
                try:
                    frame = next(decoded)
                except StopIteration:
                    break
                timings["decode_seconds"] += time.perf_counter() - decode_tick
                if frame.is_corrupt:
                    raise RuntimeError("The source decoder returned a corrupt frame.")
                decode_tick = time.perf_counter()
                pts = round(Fraction(frame.pts) * (frame.time_base or stream_tb) / stream_tb) if frame.pts is not None else (
                    0 if last_pts is None else last_pts + default_duration)
                if last_pts is not None and pts <= last_pts:
                    pts = last_pts + default_duration
                if first_pts is None:
                    first_pts = pts
                if ((options.preview_frames is not None and delivered >= int(options.preview_frames)) or
                    (options.preview_seconds is not None and delivered and
                     float(Fraction(pts - first_pts) * stream_tb) >= options.preview_seconds)):
                    break
                rgba = frame.to_ndarray(format="rgba64le" if source_high_depth else "rgba")
                rotation = int(metadata["rotation"])
                if rotation:
                    rgba = cv2.rotate(rgba, {90: cv2.ROTATE_90_CLOCKWISE,
                                             180: cv2.ROTATE_180,
                                             270: cv2.ROTATE_90_COUNTERCLOCKWISE}[rotation])
                if rgba.shape[:2] != (height, width):
                    from ...core.ffmpeg.filters import resize_fit
                    rgba = resize_fit(rgba, width, height, controller=controller)
                rgba = np.ascontiguousarray(rgba)
                timings["decode_seconds"] += time.perf_counter() - decode_tick
                guide = guides.process(rgba)
                cuts += int(delivered > 0 and guide.reset)
                dlss_tick = time.perf_counter()
                result = dlss.process(rgba, reset=guide.reset, phase=delivered,
                                      output_dtype=np.float16 if hdr_session else np.uint16 if output_depth > 8 else np.uint8)
                timings["dlss_seconds"] += time.perf_counter() - dlss_tick
                postprocess_tick = time.perf_counter()
                if hdr_session is not None:
                    if processed_float is None:
                        processed_float = np.empty(result.shape, dtype=np.float32)
                    # Avoid NumPy's software half-float clipping loop.
                    np.clip(result, 0, 1, out=processed_float, dtype=np.float32)
                    hdr_tick = time.perf_counter()
                    if source_high_depth:
                        rgb10 = np.rint(processed_float[..., :3] * 1023).astype(np.uint32)
                        hdr_input = np.ascontiguousarray(
                            rgb10[..., 0] | (rgb10[..., 1] << 10) |
                            (rgb10[..., 2] << 20) | np.uint32(3 << 30))
                    else:
                        hdr_input = np.rint(processed_float * 255).astype(np.uint8)
                        hdr_input[..., 3] = 255
                    hdr_data = hdr_session.process_frame(hdr_input)
                    hdr_frame = result_frame(hdr_data, ow, oh, 2)
                    from ...core.ffmpeg.frames import packed_frame
                    processed = packed_frame(hdr_frame)
                    timings["hdr_seconds"] += time.perf_counter() - hdr_tick
                else:
                    processed = result
                timings["postprocess_seconds"] = timings.get("postprocess_seconds", 0.) + time.perf_counter() - postprocess_tick
                encode_tick = time.perf_counter()
                duration = round(Fraction(frame.duration or default_duration) *
                                 (frame.time_base or stream_tb) / stream_tb)
                writer.write(processed, pts - first_pts if preview else pts, max(1, duration))
                timings["encode_seconds"] += time.perf_counter() - encode_tick
                delivered += 1
                last_pts = pts
                if delivered % 4 == 0:
                    update(min(.86, .04 + .82 * delivered / max(1, estimated)),
                           f"DLSS video processing: {delivered:,} / {estimated:,} frames")
            if not delivered:
                raise ValueError("The source contains no decodable video frames.")
            timings["pipeline_seconds"] = time.perf_counter() - pipeline_tick
            writer.close(); writer = None
            encoder.stdin.close()
            flush_tick = time.perf_counter()
            if encoder.wait(timeout=600) != 0:
                encoder_thread.join(timeout=2)
                raise RuntimeError("Video encoder failed: " + "\n".join(list(encoder_logs)[-20:]))
            encoder_thread.join(timeout=2)
            timings["encoder_flush_seconds"] = time.perf_counter() - flush_tick
            controller.unregister(encoder)
            encoder = None
            input_container.close(); input_container = None
            if hdr_session:
                if hdr_session.completed_frames != delivered:
                    raise RuntimeError("RTX Video HDR frame count does not match DLSS output.")
                hdr_session.close(); hdr_session = None
            if dlss.frames != delivered:
                raise RuntimeError("DLSS frame count does not match the source.")
            # Strict source EOF and one evaluation/packet per frame already
            # establish the count when the container omits nb_frames.
            if not preview and metadata["frames"] and delivered != metadata["frames"]:
                source_count = ffmpeg.probe_video(
                    source, count_mode="exact", strict_decode=True, controller=controller)
                if int(source_count["frames"]) != delivered:
                    raise RuntimeError(
                        f"Source has {source_count['frames']} frames but DLSS processed {delivered}.")
            render_w, render_h = dlss.last_result.render_width, dlss.last_result.render_height
            native_diagnostics = dlss.diagnostics()
            bridge_version = dlss.bridge_version
            dlss.close(); dlss = None
            update(.90, "Muxing audio and metadata")
            mux_tick = time.perf_counter()
            audio_info = {}
            ffmpeg.final_mux(temp_video, source, output_file.temporary, options.container,
                             controller, preserve_supported_subtitles=True,
                             source_time_origin=metadata["origin"], audio_diagnostics=audio_info,
                             video_only=preview_start_seconds > 0,
                             video_color_metadata={
                                 "color_space": "bt2020nc" if options.hdr_enabled else "bt709",
                                 "color_primaries": "bt2020" if options.hdr_enabled else "bt709",
                                 "color_transfer": "smpte2084" if options.hdr_enabled else "bt709",
                                 "color_range": "tv"})
            timings["final_mux_seconds"] = time.perf_counter() - mux_tick
            update(.96, "Verifying output")
            verification_tick = time.perf_counter()
            saved = ffmpeg.probe_video(output_file.temporary, count_mode="packets", controller=controller)
            if int(saved["frames"]) != delivered:
                saved = ffmpeg.probe_video(output_file.temporary, count_mode="exact",
                                           strict_decode=True, controller=controller)
            if (int(saved["frames"]) != delivered or
                (int(saved["width"]), int(saved["height"])) != (ow, oh)):
                raise RuntimeError("DLSS output has an incorrect frame count or size.")
            if options.hdr_enabled:
                verified = inspect_video(output_file.temporary, controller)
                if (not verified["hdr"] or verified["depth"] < 10 or
                    verified["stream"].get("color_primaries") != "bt2020" or
                    verified["stream"].get("color_space") != "bt2020nc"):
                    raise RuntimeError("DLSS → RTX Video HDR output lost HDR signaling.")
            timings["verification_seconds"] = time.perf_counter() - verification_tick
            elapsed = time.perf_counter() - started
            status = {
                "engine": "DLSS Super Resolution", "mode": options.dlss_mode,
                "preset": options.dlss_preset, "guide_estimated": True,
                "media_pipeline": "source-anchored DLSS", "synthetic_jitter": False,
                "render_width": render_w, "render_height": render_h,
                "gpu_pre_resize": (render_w, render_h) != (width, height),
                "scene_cuts": cuts, "frames": delivered,
                "rtx_video_hdr": bool(options.hdr_enabled),
                "audio_streams": audio_info.get("streams", []),
                "timings": timings,
                "motion_backend": "gpu_lucas_kanade", **native_diagnostics,
                "bridge_version": bridge_version,
            }
            output_file.publish()
            app_log.info("upscale-dlss", f"done src={source.name} out={output.name} frames={delivered} mode={options.dlss_mode} preset={options.dlss_preset} fps={delivered/max(elapsed, 1e-9):.2f}")
            update(1.0, "Complete — DLSS evaluated")
            return UpscaleResult(str(output), report_path, delivered, ow, oh,
                                 options.hdr_enabled, elapsed, bridge_version=bridge_version,
                                 memory_path="host_rgba_d3d12_host_encoder",
                                 decode_backend="FFmpeg Vulkan/software", encode_backend=selected,
                                 timings=timings, bridge_status=status)
    finally:
        if writer:
            with suppress(Exception): writer.close()
        if input_container:
            with suppress(Exception): input_container.close()
        if hdr_session:
            with suppress(Exception): hdr_session.close(abort=True)
        if dlss:
            dlss.close()
        if encoder:
            with suppress(Exception):
                if encoder.poll() is None: encoder.kill()
                encoder.wait(timeout=5)
            controller.unregister(encoder)
        output_file.cleanup()
