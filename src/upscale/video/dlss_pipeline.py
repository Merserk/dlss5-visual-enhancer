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
import cv2
import numpy as np
from av.codec.hwaccel import HWAccel

from ...core import app_log, ffmpeg
from ...core.disk_paths import OutputFile, prepare_output_dir
from ...core.dlss_bridge import DLSSSession
from ...core.gpu_selection import detect_gpu
from ...core.jobs import Cancelled
from ...core.naming import output_filename, unique_output_path
from ...core.paths import JOBS
from ...neural_rendering.video.guides import TemporalGuideGenerator
from .media import inspect_video, result_frame
from .models import UpscaleResult, output_size
from .native import FORMAT_RGBA8, FORMAT_R10, RTXVideoSession, probe_capabilities


def convert_video_dlss(source, options, *, controller, progress=None, output_dir=None,
                       metadata=None, video_gpu=None):
    started = time.perf_counter()
    metadata = metadata or inspect_video(source, controller, reject_hdr=True)
    width, height = int(metadata["width"]), int(metadata["height"])
    ow, oh, _ = output_size(width, height, options)
    gpu = detect_gpu(options.ai_gpu_uuid)
    ordinal = int(gpu["cuda_ordinal"])
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
            decode_device = HWAccel(
                "cuda", device=str(ordinal), allow_software_fallback=True,
                options={"primary_ctx": "1"}, is_hw_owned=True)
            input_container = av.open(str(source), hwaccel=decode_device)
            stream = input_container.streams.video[0]
            stream.thread_type = "AUTO"
            stream_tb = stream.time_base or Fraction(1, max(1, round(float(metadata["rate"]))))
            writer = ffmpeg.RawVideoPacketMuxer(
                encoder.stdin, width=ow, height=oh, rate=metadata["rate"],
                time_base=stream_tb, pix_fmt="rgba64le" if output_depth > 8 else "rgba")
            guides = TemporalGuideGenerator(width, height, cut_threshold=0.10)
            first_pts = None
            last_pts = None
            default_duration = max(1, round(Fraction(1) / metadata["rate"] / stream_tb))
            estimated = int(metadata["frames"] or max(1, math.ceil(metadata["duration"] * float(metadata["rate"]))))
            for frame in input_container.decode(stream):
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
                    rgba = cv2.resize(rgba, (width, height), interpolation=cv2.INTER_LANCZOS4)
                rgba = np.ascontiguousarray(rgba)
                timings["decode_seconds"] += time.perf_counter() - decode_tick
                guide = guides.process(rgba)
                cuts += int(delivered > 0 and guide.reset)
                dlss_tick = time.perf_counter()
                result = dlss.process(rgba, reset=guide.reset, phase=delivered)
                timings["dlss_seconds"] += time.perf_counter() - dlss_tick
                processed_float = np.clip(result.astype(np.float32), 0, 1)
                if hdr_session is not None:
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
                    processed = np.ascontiguousarray(hdr_frame.to_ndarray(format="rgba64le"))
                    timings["hdr_seconds"] += time.perf_counter() - hdr_tick
                else:
                    levels = 65535 if output_depth > 8 else 255
                    processed = np.rint(processed_float * levels).astype(
                        np.uint16 if output_depth > 8 else np.uint8)
                    processed[..., 3] = levels
                encode_tick = time.perf_counter()
                duration = round(Fraction(frame.duration or default_duration) *
                                 (frame.time_base or stream_tb) / stream_tb)
                writer.write(processed, pts - first_pts if preview else pts, max(1, duration))
                timings["encode_seconds"] += time.perf_counter() - encode_tick
                delivered += 1
                last_pts = pts
                if delivered % 4 == 0:
                    update(min(.86, .04 + .82 * delivered / max(1, estimated)), "DLSS video processing")
            if not delivered:
                raise ValueError("The source contains no decodable video frames.")
            writer.close(); writer = None
            encoder.stdin.close()
            if encoder.wait(timeout=600) != 0:
                encoder_thread.join(timeout=2)
                raise RuntimeError("Video encoder failed: " + "\n".join(list(encoder_logs)[-20:]))
            encoder_thread.join(timeout=2)
            controller.unregister(encoder)
            encoder = None
            input_container.close(); input_container = None
            if hdr_session:
                if hdr_session.completed_frames != delivered:
                    raise RuntimeError("RTX Video HDR frame count does not match DLSS output.")
                hdr_session.close(); hdr_session = None
            if dlss.frames != delivered:
                raise RuntimeError("DLSS frame count does not match the source.")
            if not preview and (not metadata["frames"] or delivered != metadata["frames"]):
                source_count = ffmpeg.probe_video(
                    source, count_mode="exact", strict_decode=True, controller=controller)
                if int(source_count["frames"]) != delivered:
                    raise RuntimeError(
                        f"Source has {source_count['frames']} frames but DLSS processed {delivered}.")
            render_w, render_h = dlss.last_result.render_width, dlss.last_result.render_height
            dlss.close(); dlss = None
            update(.90, "Muxing audio and metadata")
            audio_info = {}
            ffmpeg.final_mux(temp_video, source, output_file.temporary, options.container,
                             controller, preserve_supported_subtitles=True,
                             source_time_origin=metadata["origin"], audio_diagnostics=audio_info,
                             video_color_metadata={
                                 "color_space": "bt2020nc" if options.hdr_enabled else "bt709",
                                 "color_primaries": "bt2020" if options.hdr_enabled else "bt709",
                                 "color_transfer": "smpte2084" if options.hdr_enabled else "bt709",
                                 "color_range": "tv"})
            update(.96, "Verifying output")
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
            }
            output_file.publish()
            app_log.info("upscale-dlss", f"done src={source.name} out={output.name} frames={delivered} mode={options.dlss_mode} preset={options.dlss_preset} fps={delivered/max(elapsed, 1e-9):.2f}")
            update(1.0, "Complete — DLSS evaluated")
            return UpscaleResult(str(output), report_path, delivered, ow, oh,
                                 options.hdr_enabled, elapsed, bridge_version="DLSS ABI 1",
                                 memory_path="host_rgba_d3d12_host_encoder",
                                 decode_backend="NVDEC/software", encode_backend=selected,
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
