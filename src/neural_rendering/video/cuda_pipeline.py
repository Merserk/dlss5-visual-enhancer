from __future__ import annotations

"""GPU-resident offline Neural Rendering video pipeline."""

import gc
import math
import os
import shutil
import time
from contextlib import suppress
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable

import av
import numpy as np
from av.codec.hwaccel import HWAccel

from ...core import app_log, ffmpeg
from ...core.dlss_modes import dlss_output_size
from ...core.disk_paths import OutputFile, prepare_output_dir
from ...core.gpu_selection import resolve_runtime_ai_gpu
from ...core.jobs import Cancelled
from ...core.naming import output_filename
from ...core.paths import JOBS, OUTPUTS
from ...core.render_metadata import prepare_render_note
from ...core.runtime import (
    DLSSFrameSession,
    resize_fit,
    rotate_frame,
)
from .guides import TemporalGuideGenerator
from .models import ConversionOptions, ConversionResult
from .sizing import resolve_native_settings, resolve_output_size, resolve_upscaling_mode


_NVENC_CODEC = {
    "H.264 (NVIDIA NVENC)": "h264_nvenc",
    "H.265 (NVIDIA NVENC)": "hevc_nvenc",
    "AV1 (NVIDIA NVENC)": "av1_nvenc",
}


def _matrix_code(metadata: dict[str, Any]) -> int:
    value = str(metadata.get("color_space") or "").casefold()
    if "2020" in value:
        return 2
    if value in {"bt470bg", "smpte170m", "smpte240m", "fcc"}:
        return 0
    return 1


def _range_code(metadata: dict[str, Any]) -> int:
    return int(str(metadata.get("color_range") or "").casefold() in {"pc", "jpeg", "full"})


def _set_color_properties(codec_context: Any, metadata: dict[str, Any], hdr: bool) -> None:
    fallback = 2 if hdr else 1
    primaries = {"bt709": 1, "bt2020": 9}.get(str(metadata.get("color_primaries")), fallback)
    transfer = {
        "bt709": 1,
        "smpte2084": 16,
        "arib-std-b67": 18,
    }.get(str(metadata.get("color_transfer")), fallback)
    colorspace = {
        "bt709": 1,
        "bt470bg": 5,
        "smpte170m": 6,
        "bt2020nc": 9,
        "bt2020c": 10,
    }.get(str(metadata.get("color_space")), fallback)
    codec_context.color_primaries = primaries
    codec_context.color_trc = transfer
    codec_context.colorspace = colorspace
    codec_context.color_range = 2 if _range_code(metadata) else 1


def _hdr_bitstream_filter(codec_name: str, metadata: dict[str, Any], preserve_hdr: bool) -> str | None:
    if not preserve_hdr or not metadata.get("hdr"):
        return None
    primaries = {"bt2020": 9, "bt709": 1}.get(str(metadata.get("color_primaries")), 9)
    transfer = {"smpte2084": 16, "arib-std-b67": 18}.get(str(metadata.get("color_transfer")), 16)
    matrix = {"bt2020nc": 9, "bt2020c": 10, "bt709": 1}.get(str(metadata.get("color_space")), 9)
    full = _range_code(metadata)
    if codec_name == "hevc_nvenc":
        return (f"hevc_metadata=video_full_range_flag={full}:colour_primaries={primaries}:"
                f"transfer_characteristics={transfer}:matrix_coefficients={matrix}")
    if codec_name == "av1_nvenc":
        return (f"av1_metadata=color_range={full}:color_primaries={primaries}:"
                f"transfer_characteristics={transfer}:matrix_coefficients={matrix}")
    return None


def _check_cancel(controller: Any) -> None:
    if controller.cancel.is_set():
        raise Cancelled("Render stopped by user.")


def _progress(
    callback: Callable[[float, str], None] | None, value: float, message: str
) -> None:
    if callback is None:
        return
    try:
        callback(max(0.0, min(1.0, float(value))), message)
    except Exception:
        pass


def convert_video_cuda_nvenc(
    source: Path,
    options: ConversionOptions,
    *,
    preview_seconds: float | None,
    preview_frames: int | None,
    compat_preview: bool,
    prepared_runtime: Any,
    controller: Any,
    progress: Callable[[float, str], None] | None,
    output_dir: str | os.PathLike[str] | None,
) -> ConversionResult:
    """Decode with PyAV, evaluate feature 18, and encode directly from CUDA."""
    started = time.perf_counter()
    runtime_bundle = prepared_runtime.runtime_bundle
    gpu: dict[str, Any] | None = None
    video_gpu: dict[str, Any] | None = None
    session: DLSSFrameSession | None = None
    dlss_session = None
    output_file: OutputFile | None = None
    job_dir: Path | None = None
    delivered = 0
    dlss_scene_cuts = 0
    encoded_container: Any | None = None
    timings: dict[str, float] = {}
    frame_accounting: dict[str, Any] = {}
    decode_backends: set[str] = set()
    try:
        _check_cancel(controller)
        probe_started = time.perf_counter()
        metadata = ffmpeg.probe_video(source, count_mode="metadata", controller=controller)
        timings["probe_seconds"] = time.perf_counter() - probe_started
        declared_frames = int(metadata.get("frames") or 0)
        estimated_frames = declared_frames or max(
            1, int(math.ceil(float(metadata["duration"]) * float(metadata["fps"])))
        )
        if preview_frames is not None:
            estimated_frames = min(estimated_frames, preview_frames) if declared_frames else preview_frames
        elif preview_seconds is not None:
            estimated_frames = min(
                estimated_frames,
                max(1, int(math.ceil(preview_seconds * float(metadata["fps"])))),
            )
        factor, mode = resolve_upscaling_mode(
            1.0 if options.scale_method == "DLSS" else options.upscaling_factor)
        input_width, input_height = int(metadata["width"]), int(metadata["height"])
        output_width, output_height = (
            dlss_output_size(input_width, input_height, options.dlss_mode, even=True)
            if options.scale_method == "DLSS" else
            resolve_output_size(input_width, input_height, factor))
        effective_hdr = bool(options.preserve_hdr and not compat_preview)
        output_depth = ffmpeg.output_video_depth(metadata["depth"], options.codec, effective_hdr)
        output_p010 = output_depth > 8
        output_color_metadata = dict(metadata)
        if output_p010 and not effective_hdr:
            for field in ("color_space", "color_primaries", "color_transfer"):
                if output_color_metadata.get(field) in (None, "", "unknown", "unspecified"):
                    output_color_metadata[field] = "bt709"
        codec_name = _NVENC_CODEC[ffmpeg._normalize_codec(options.codec)]
        color_bitstream_filter = _hdr_bitstream_filter(codec_name, metadata, effective_hdr)
        gpu = resolve_runtime_ai_gpu(
            prepared_runtime.gpus, prepared_runtime.runtime_bundle, options.ai_gpu_uuid
        )
        video_gpu = ffmpeg.resolve_video_gpu(
            prepared_runtime.gpus,
            str(gpu["uuid"]),
            options.codec,
            output_width,
            output_height,
        )
        if video_gpu is None:
            raise RuntimeError("The selected AI GPU cannot initialize the requested NVENC codec.")
        ordinal = int(gpu.get("cuda_ordinal", gpu.get("index", 0)))
        if int(video_gpu["cuda_ordinal"]) != ordinal:
            raise RuntimeError("NVENC did not resolve to the Neural Rendering CUDA adapter.")

        destination = prepare_output_dir(output_dir, default=OUTPUTS)
        JOBS.mkdir(exist_ok=True)
        app_log.info("video-render-cuda", f"start src={source.name}")
        stamp = time.strftime("%Y%m%d-%H%M%S") + f"-{time.time_ns() % 1_000_000:06d}"
        extension = {"MP4": ".mp4", "MKV": ".mkv", "MOV": ".mov"}[options.container]
        is_preview = preview_seconds is not None or preview_frames is not None
        output_kind = "DLSS5" if not is_preview else (
            "DLSS5_PREVIEW_FRAME" if preview_frames is not None else "DLSS5_PREVIEW"
        )
        output = destination / output_filename(
            source,
            extension,
            options.rename_mode,
            options.custom_suffix,
            f"{source.stem}_{output_kind}_{stamp}",
        )
        output_file = OutputFile(output)
        job_dir = JOBS / f"{source.stem}-{stamp}-{os.getpid()}-cuda"
        job_dir.mkdir(parents=True, exist_ok=False)
        intermediate_extension = ".mkv" if options.container == "MKV" else ".mp4"
        temp_video = job_dir / f"processed-video{intermediate_extension}"
        metadata_diagnostics: dict[str, Any] = {}
        render_note = prepare_render_note(options, metadata_diagnostics)
        native = resolve_native_settings(options)
        matrix = _matrix_code(metadata)
        color_range = _range_code(metadata)
        chroma_location = ffmpeg.chroma_location_code(metadata)

        # FFmpeg and the bridge deliberately share CUDA's primary context.
        # Open the decoder first because some driver versions reject creating a
        # new FFmpeg CUDA hwdevice while another library has it current.
        decode_device = HWAccel(
            "cuda",
            device=str(ordinal),
            allow_software_fallback=True,
            options={"primary_ctx": "1"},
            is_hw_owned=True,
        )
        _progress(progress, 0.01, "Starting feature 18")
        with av.open(str(source), hwaccel=decode_device) as decoded:
            input_stream = decoded.streams.video[0]
            input_stream.thread_type = "AUTO"
            # Create both FFmpeg CUDA hardware contexts before NGX initializes.
            # This also covers codecs which intentionally fall back to software
            # decoding and therefore do not create a decoder CUDA context.
            encode_device = HWAccel(
                "cuda",
                device=str(ordinal),
                options={"primary_ctx": "1"},
                is_hw_owned=True,
            )
            quality = ffmpeg.resolve_encoding_quality(
                options.quality,
                options.codec,
                output_width,
                output_height,
                float(metadata["fps"]),
                hdr_mode=output_p010,
            )
            encoded_container = av.open(str(temp_video), mode="w")
            try:
                output_stream = encoded_container.add_stream(
                    codec_name, rate=metadata["rate"], hwaccel=encode_device
                )
                output_stream.width = output_width
                output_stream.height = output_height
                output_stream.pix_fmt = "cuda"
                output_stream.codec_context.sw_format = "p010le" if output_p010 else "nv12"
                output_stream.time_base = input_stream.time_base
                output_stream.codec_context.time_base = input_stream.time_base
                encoder_options = {"preset": "p6", "rc": "vbr", "gpu": str(ordinal)}
                if codec_name != "av1_nvenc":
                    encoder_options["tune"] = "hq"
                if quality["mode"] == "constant-quality":
                    encoder_options["cq"] = "0"
                else:
                    output_stream.codec_context.bit_rate = int(quality["target_bitrate_kbps"]) * 1000
                output_stream.codec_context.options = encoder_options
                _set_color_properties(output_stream.codec_context, metadata, effective_hdr)
            except BaseException:
                encoded_container.close()
                raise
            session_started = time.perf_counter()
            if options.scale_method == "DLSS":
                from ...core.dlss_bridge import DLSSSession
                dlss_session = DLSSSession(
                    input_width, input_height, options.dlss_mode, options.dlss_preset,
                    gpu_uuid=options.ai_gpu_uuid, even=True)
            session = DLSSFrameSession(
                input_width=output_width if dlss_session else input_width,
                input_height=output_height if dlss_session else input_height,
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
                cuda_video=True,
            )
            session.diagnostics.source_format = str(metadata["pixel_format"])
            session.diagnostics.output_format = "p010le" if output_p010 else "nv12"
            timings["native_setup_seconds"] = time.perf_counter() - session_started
            encode_started = time.perf_counter()
            with encoded_container as encoded:
                software_guides: TemporalGuideGenerator | None = None
                first_time: float | None = None
                preview_pts_origin: int | None = None
                default_duration = max(
                    1, round(Fraction(1, 1) / metadata["rate"] / input_stream.time_base)
                )
                decode_seconds = 0.0
                prepare_seconds = 0.0
                scene_seconds = 0.0
                evaluate_seconds = 0.0
                encode_seconds = 0.0
                completion_reason = "eof"
                decoder = iter(decoded.decode(input_stream))
                while True:
                    _check_cancel(controller)
                    if preview_frames is not None and delivered >= preview_frames:
                        completion_reason = "preview_limit"
                        break
                    decode_started = time.perf_counter()
                    try:
                        frame = next(decoder)
                    except StopIteration:
                        decode_seconds += time.perf_counter() - decode_started
                        break
                    decode_seconds += time.perf_counter() - decode_started
                    if frame.is_corrupt:
                        raise RuntimeError(f"The video decoder marked source frame {delivered} as corrupt.")
                    pts = int(frame.pts if frame.pts is not None else delivered * default_duration)
                    frame_time_base = frame.time_base or input_stream.time_base
                    timestamp = float(Fraction(pts) * frame_time_base)
                    if first_time is None:
                        first_time = timestamp
                    if preview_seconds is not None and delivered and timestamp - first_time >= preview_seconds:
                        completion_reason = "preview_limit"
                        break
                    duration = int(getattr(frame, "duration", None) or default_duration)

                    if frame.format.name == "cuda":
                        decode_backends.add("nvdec")
                        if dlss_session is not None:
                            frame, dlss_detail = dlss_session.process_cuda_frame(
                                frame, color_matrix=matrix, color_range=color_range,
                                rotation=int(metadata["rotation"]), phase=delivered,
                                output_p010=output_p010)
                            dlss_scene_cuts += int(dlss_detail["scene_cut"])
                            frame.pts, frame.time_base, frame.duration = pts, frame_time_base, duration
                        scene_started = time.perf_counter()
                        scene_score, reset = session.score_cuda_frame(
                            frame, color_matrix=matrix, color_range=color_range
                        )
                        scene_seconds += time.perf_counter() - scene_started
                        evaluate_started = time.perf_counter()
                        processed, output_pts = session.process_video_frame(
                            index=delivered,
                            frame=frame,
                            reset=reset,
                            scene_score=scene_score,
                            pts=pts,
                            duration=duration,
                            time_base=frame_time_base,
                            color_matrix=matrix,
                            color_range=color_range,
                            rotation=0 if dlss_session else int(metadata["rotation"]),
                            chroma_location=chroma_location,
                            output_p010=output_p010,
                        )
                    else:
                        decode_backends.add("software")
                        prepare_started = time.perf_counter()
                        rgba = rotate_frame(
                            ffmpeg.decoded_rgba(frame, metadata["depth"]), int(metadata["rotation"])
                        )
                        if dlss_session is not None:
                            if software_guides is None:
                                software_guides = TemporalGuideGenerator(rgba.shape[1], rgba.shape[0])
                            scene_started = time.perf_counter()
                            guide = software_guides.process(rgba)
                            scene_seconds += time.perf_counter() - scene_started
                            scene_score, reset = guide.scene_score, guide.reset
                            dlss_scene_cuts += int(reset and delivered > 0)
                            dlss_half = dlss_session.process(rgba, reset=reset, phase=delivered)
                            levels = 65535 if output_p010 else 255
                            rgba = np.rint(np.clip(dlss_half.astype(np.float32), 0, 1) * levels).astype(
                                np.uint16 if output_p010 else np.uint8)
                            rgba[..., 3] = levels
                        elif rgba.shape[:2] != (output_height, output_width):
                            rgba = resize_fit(rgba, output_width, output_height)
                        rgba = np.ascontiguousarray(rgba)
                        prepare_seconds += time.perf_counter() - prepare_started
                        if dlss_session is None:
                            if software_guides is None:
                                software_guides = TemporalGuideGenerator(output_width, output_height)
                            scene_started = time.perf_counter()
                            guide = software_guides.process(rgba)
                            scene_seconds += time.perf_counter() - scene_started
                            scene_score, reset = guide.scene_score, guide.reset
                        evaluate_started = time.perf_counter()
                        processed, output_pts = session.process_video_frame(
                            index=delivered,
                            rgba=rgba,
                            reset=reset,
                            scene_score=scene_score,
                            pts=pts,
                            duration=duration,
                            time_base=frame_time_base,
                            color_matrix=matrix,
                            color_range=color_range,
                            output_p010=output_p010,
                        )
                    evaluate_seconds += time.perf_counter() - evaluate_started
                    if output_pts != pts:
                        raise RuntimeError("The feature-18 frame ABI changed a video timestamp.")
                    if is_preview:
                        if preview_pts_origin is None:
                            preview_pts_origin = output_pts
                        processed.pts = output_pts - preview_pts_origin
                    encode_frame_started = time.perf_counter()
                    for packet in output_stream.encode(processed):
                        encoded.mux(packet)
                    encode_seconds += time.perf_counter() - encode_frame_started
                    del processed, frame
                    delivered += 1
                    _progress(
                        progress,
                        0.04 + 0.84 * min(1.0, delivered / estimated_frames),
                        "Rendering video frames",
                    )
                if not delivered:
                    raise RuntimeError("The input video contains no decodable frames.")
                flush_started = time.perf_counter()
                for packet in output_stream.encode():
                    encoded.mux(packet)
                encode_seconds += time.perf_counter() - flush_started
                timings.update(
                    source_decode_seconds=decode_seconds,
                    frame_prepare_seconds=prepare_seconds,
                    scene_score_seconds=scene_seconds,
                    dlss_seconds=evaluate_seconds,
                    nvenc_seconds=encode_seconds,
                )
                frame_accounting.update(
                    declared_frames=declared_frames,
                    decoded_frames=delivered,
                    processed_frames=delivered,
                    encoded_frames=delivered,
                    completion_reason=completion_reason,
                    source_verification="clean_decoder_eof" if completion_reason == "eof" else "preview_limit",
                )
            timings["encoding_stage_seconds"] = time.perf_counter() - encode_started
        assert session is not None
        session.close()
        if dlss_session is not None:
            if dlss_session.frames != delivered:
                raise RuntimeError("DLSS frame count does not match Neural Rendering input.")
        frame_accounting["bridge_completed_frames"] = session.completed_frames
        if session.completed_frames != delivered:
            raise RuntimeError("Bridge completion does not match the processed frame count.")
        gc.collect()

        mux_started = time.perf_counter()
        audio_diagnostics: dict = {}
        if preview_frames is not None:
            shutil.copyfile(temp_video, output_file.temporary)
            metadata_diagnostics.update(status="skipped", reason="preview_fast_path")
        else:
            _progress(progress, 0.91, "Muxing original audio, chapters, and metadata")
            ffmpeg.final_mux(
                temp_video,
                source,
                output_file.temporary,
                options.container,
                controller,
                render_note=render_note,
                metadata_diagnostics=metadata_diagnostics,
                audio_diagnostics=audio_diagnostics,
                video_bitstream_filter=color_bitstream_filter,
                video_color_metadata=output_color_metadata if output_p010 else None,
            )
        timings["final_mux_seconds"] = time.perf_counter() - mux_started
        _check_cancel(controller)
        verified = ffmpeg.probe_video(
            output_file.temporary, count_mode="metadata", controller=controller
        )
        if (int(verified["width"]), int(verified["height"])) != (output_width, output_height):
            raise RuntimeError("Saved output dimensions do not match the neural dimensions.")
        declared_output = int(verified.get("frames") or 0)
        if declared_output and declared_output != delivered:
            raise RuntimeError(
                f"Output container reports {declared_output} frames instead of {delivered}."
            )
        if effective_hdr and metadata.get("hdr") and (
            not verified.get("hdr") or verified.get("color_space") != str(metadata.get("color_space"))
        ):
            intermediate = ffmpeg.probe_video(temp_video, count_mode="metadata", controller=controller)
            raise RuntimeError(
                "Saved HDR output did not retain BT.2020 HDR signaling: "
                f"transfer={verified.get('color_transfer')} matrix={verified.get('color_space')} "
                f"primaries={verified.get('color_primaries')} range={verified.get('color_range')}; "
                f"intermediate transfer={intermediate.get('color_transfer')} "
                f"matrix={intermediate.get('color_space')} "
                f"primaries={intermediate.get('color_primaries')}."
            )

        status = session.structured_status()
        if dlss_session is not None:
            status["dlss_super_resolution"] = {
                "mode": options.dlss_mode, "preset": options.dlss_preset,
                "guides": "estimated", "frames": dlss_session.frames,
                "scene_cuts": dlss_scene_cuts,
                "render_width": dlss_session.last_result.render_width,
                "render_height": dlss_session.last_result.render_height,
                "gpu_pre_resize": (
                    dlss_session.last_result.render_width, dlss_session.last_result.render_height
                ) != (input_width, input_height),
            }
        temporal_timing = status.get("temporal_stabilization", {})
        for field in (
            "gpu_copy_in_seconds", "gpu_ngx_seconds", "gpu_copy_out_seconds",
            "cuda_input_wait_seconds", "d3d12_wait_seconds", "optical_flow_seconds",
        ):
            timings[field] = float(temporal_timing.get(field, 0.0))
        decode_backend = "+".join(sorted(decode_backends)) or "unknown"
        status["decode_backend"] = decode_backend
        status["encode_backend"] = codec_name
        status["audio_streams"] = audio_diagnostics.get("streams", [])
        status["timings"] = dict(timings)
        resize_method = "dlss" if dlss_session else ("none" if factor == 1.0 else "lanczos")
        elapsed = time.perf_counter() - started
        report_path = app_log.session_path()
        app_log.info(
            "video-render-cuda",
            f"done src={source.name} out={output.name} frames={delivered} "
            f"elapsed={elapsed:.0f}s avg={delivered / max(elapsed, 1e-9):.1f}fps",
        )
        output_file.publish()
        _progress(progress, 1.0, "Complete — feature 18 confirmed")
        return ConversionResult(
            output_path=str(output),
            report_path=str(report_path),
            frames=delivered,
            nr_count_evidence=delivered,
            elapsed_seconds=elapsed,
            gpu=str(gpu["display_name"]),
            input_width=input_width,
            input_height=input_height,
            render_width=output_width,
            render_height=output_height,
            output_width=output_width,
            output_height=output_height,
            upscaling_factor=(output_width / input_width if dlss_session else factor),
            neural_dimensions={"width": output_width, "height": output_height},
            resize_method=resize_method,
            memory_path="cuda_d3d12_shared",
            bridge_status=status,
        )
    except BaseException as exc:
        if session is not None and not session.closed:
            with suppress(Exception):
                session.abort()
        if output_file is not None:
            output_file.cleanup(rollback=True)
        if controller.cancel.is_set() and not isinstance(exc, Cancelled):
            raise Cancelled("Render stopped by user.") from exc
        if isinstance(exc, Cancelled):
            app_log.info("video-render-cuda", f"cancelled src={source.name}")
            raise
        tails: dict[str, object] = {}
        if session is not None:
            with suppress(Exception):
                tails["bridge"] = session.bridge_logs[-20:]
        tails["decode"] = "+".join(sorted(decode_backends)) or "unknown"
        failure = app_log.fail("video-render-cuda", f"video-render-cuda-{source.stem}", exc, tails)
        raise RuntimeError(f"{exc}\nDetails: {failure}") from exc
    finally:
        if dlss_session is not None:
            dlss_session.close()
        if encoded_container is not None:
            with suppress(Exception):
                encoded_container.close()
        if output_file is not None:
            output_file.cleanup()
        if job_dir is not None and job_dir.exists():
            shutil.rmtree(job_dir, ignore_errors=True)
