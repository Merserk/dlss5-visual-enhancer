from __future__ import annotations

"""Bounded NVDEC -> RTX Video CUDA -> same/cross-adapter NVENC pipeline."""

import gc
import math
import os
import queue
import shutil
import tempfile
import threading
import time
import uuid
from contextlib import suppress
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable

import av
from av.codec.hwaccel import HWAccel

from ...core import app_log, ffmpeg
from ...core.disk_paths import OutputFile, prepare_output_dir
from ...core.gpu_detection import detect_gpus
from ...core.gpu_selection import resolve_ai_gpu
from ...core.jobs import Cancelled
from ...core.naming import output_filename
from ...core.paths import JOBS
from .media import inspect_video, packed_bytes
from .models import UpscaleCapabilities, UpscaleOptions, UpscaleResult, output_size
from .native import RTXVideoSession, probe_capabilities
from .cuda_transfer import CudaTransferPool


_NVENC_CODEC = {
    "H.264 (NVIDIA NVENC)": "h264_nvenc",
    "H.265 (NVIDIA NVENC)": "hevc_nvenc",
    "AV1 (NVIDIA NVENC)": "av1_nvenc",
}
_END = object()


def is_nvenc(codec: str) -> bool:
    return ffmpeg._normalize_codec(codec) in _NVENC_CODEC


def _sdr_fallback(metadata: dict[str, Any]) -> str:
    height = int(metadata["stream"]["height"])
    return "bt470bg" if height == 576 else "smpte170m" if height <= 576 else "bt709"


def memory_route(codec: str, ai_gpu: dict[str, Any], video_gpu: dict[str, Any] | None) -> str:
    """Return the observable pixel-memory route for an explicit codec/GPU choice."""
    if not is_nvenc(codec):
        return "gpu_to_pinned_host"
    if video_gpu is None:
        raise RuntimeError("An NVIDIA encoder was selected but no NVENC adapter is available.")
    return ("cuda_zero_copy" if int(video_gpu["cuda_ordinal"]) == int(ai_gpu["cuda_ordinal"])
            else "cuda_cross_gpu_peer_or_pinned")


def normalize_frame_timing(
    *, frame_pts: int | None, frame_duration: int | None,
    frame_time_base: Fraction | None, stream_time_base: Fraction,
    origin_pts: int, delivered: int, default_duration: int,
    last_pts: int | None,
) -> tuple[int, int, bool]:
    """Normalize one CFR/VFR frame without dropping, duplicating, or re-timing it."""
    frame_tb = frame_time_base or stream_time_base
    fallback = frame_pts is None
    pts = (delivered * default_duration if fallback else
           round(Fraction(int(frame_pts)) * frame_tb / stream_time_base) - origin_pts)
    if last_pts is not None and pts <= last_pts:
        raise ValueError("Source timestamps are not strictly increasing; refusing to drop or duplicate frames.")
    duration = max(
        1, round(Fraction(int(frame_duration or default_duration)) * frame_tb / stream_time_base))
    return pts, duration, fallback


def _matrix_code(metadata: dict[str, Any]) -> int:
    value = str(metadata["stream"].get("color_space") or "").casefold()
    if value in {"", "unknown", "unspecified", "reserved"}:
        pixel_format = str(metadata["stream"].get("pix_fmt") or "yuv420p")
        value = "gbr" if av.VideoFormat(pixel_format).is_rgb else _sdr_fallback(metadata)
    if value in {"bt2020nc", "bt2020c"}:
        return 2
    if value in {"bt470bg", "smpte170m", "smpte240m", "fcc"}:
        return 0
    return 1


def _primaries_code(metadata: dict[str, Any]) -> int:
    value = str(metadata["stream"].get("color_primaries") or "").casefold()
    if value in {"", "unknown", "unspecified", "reserved"}:
        value = _sdr_fallback(metadata)
    if value == "bt2020":
        return 2
    if value in {"bt470bg", "bt470m"}:
        return 0
    if value in {"smpte170m", "smpte240m", "smpte431", "smpte432"}:
        return 3
    return 1


def _transfer_code(metadata: dict[str, Any]) -> int:
    value = str(metadata["stream"].get("color_transfer") or "").casefold()
    if value in {"iec61966-2-1", "srgb"}:
        return 1
    if value in {"gamma22", "bt470m"}:
        return 2
    if value in {"gamma28", "bt470bg"}:
        return 3
    if value == "linear":
        return 4
    return 0


def _range_code(metadata: dict[str, Any]) -> int:
    stream = metadata["stream"]
    tagged_full = str(stream.get("color_range") or "").casefold() in {"pc", "jpeg", "full"}
    pixel_format = str(stream.get("pix_fmt") or "yuv420p")
    return int(tagged_full or av.VideoFormat(pixel_format).is_rgb)


def _chroma_location_code(metadata: dict[str, Any]) -> int:
    return {"left": 0, "center": 1, "topleft": 2, "top": 3,
            "bottomleft": 4, "bottom": 5}.get(
        str(metadata["stream"].get("chroma_location") or "left").casefold(), 0)


def _set_color_properties(context: Any, hdr: bool) -> None:
    context.color_primaries = 9 if hdr else 1
    context.color_trc = 16 if hdr else 1
    context.colorspace = 9 if hdr else 1
    context.color_range = 1  # MPEG/limited range


class _SoftwareNormalizer:
    """In-process fallback matching the prior color/rotation/SAR boundary."""

    def __init__(self, frame: Any, metadata: dict[str, Any], width: int, height: int, ten_bit: bool) -> None:
        stream = metadata["stream"]
        sd = int(stream["height"]) <= 576
        fallback = "bt470bg" if int(stream["height"]) == 576 else "smpte170m" if sd else "bt709"
        matrix = str(stream.get("color_space") or fallback)
        primaries = str(stream.get("color_primaries") or fallback)
        transfer = str(stream.get("color_transfer") or "bt709")
        color_range = "pc" if _range_code(metadata) else "tv"
        precision = "yuv444p10le" if ten_bit else "yuv444p"
        final_format = "gbrp10le" if ten_bit else "rgba"
        graph = av.filter.Graph()
        source = graph.add_buffer(template=frame)
        colors = graph.add(
            "colorspace",
            f"ispace={matrix}:iprimaries={primaries}:itrc={transfer}:irange={color_range}:"
            f"all=bt709:trc=gamma22:range=pc:format={precision}",
        )
        source.link_to(colors)
        node = colors
        rotation = int(metadata["rotation"])
        if rotation == 90:
            rotated = graph.add("transpose", "cclock")
            node.link_to(rotated); node = rotated
        elif rotation == 270:
            rotated = graph.add("transpose", "clock")
            node.link_to(rotated); node = rotated
        elif rotation == 180:
            horizontal, vertical = graph.add("hflip"), graph.add("vflip")
            node.link_to(horizontal); horizontal.link_to(vertical); node = vertical
        scaled = graph.add("scale", f"{width}:{height}:flags=lanczos")
        square = graph.add("setsar", "1")
        formatted = graph.add("format", final_format)
        sink = graph.add("buffersink")
        node.link_to(scaled); scaled.link_to(square); square.link_to(formatted); formatted.link_to(sink)
        graph.configure()
        self.graph, self.source, self.sink = graph, source, sink

    def convert(self, frame: Any):
        self.source.push(frame)
        return packed_bytes(self.sink.pull())


def _put_bounded(target: queue.Queue, item: Any, stop: threading.Event,
                 controller: Any, failures: list[BaseException]) -> bool:
    while not stop.is_set():
        if controller.cancel.is_set():
            stop.set()
            return False
        if failures:
            stop.set()
            return False
        try:
            target.put(item, timeout=0.05)
            return True
        except queue.Full:
            continue
    return False


def _get_bounded(source: queue.Queue, stop: threading.Event, controller: Any,
                 failures: list[BaseException]) -> Any:
    while True:
        if controller.cancel.is_set():
            stop.set()
            raise Cancelled("Upscale stopped by user.")
        if failures:
            stop.set()
            raise failures[0]
        try:
            return source.get(timeout=0.05)
        except queue.Empty:
            if stop.is_set():
                if failures:
                    raise failures[0]
                return _END


def convert_video_cuda_nvenc(
    source: Path,
    options: UpscaleOptions,
    *,
    controller: Any,
    progress: Callable[[float, str], None] | None,
    output_dir: str | os.PathLike[str] | None,
    metadata: dict[str, Any] | None = None,
    capabilities: UpscaleCapabilities | None = None,
    video_gpu: dict[str, Any] | None = None,
) -> UpscaleResult:
    """Run the production CUDA route, including explicit cross-GPU NVENC."""
    started = time.perf_counter()
    report_path = app_log.session_path()
    metadata = metadata or inspect_video(source, controller, reject_hdr=True)
    width, height = int(metadata["width"]), int(metadata["height"])
    output_width, output_height, _ = output_size(width, height, options)
    gpus = detect_gpus()
    ai_gpu = capabilities.gpu if capabilities is not None else resolve_ai_gpu(gpus, options.ai_gpu_uuid)
    ai_ordinal = int(ai_gpu["cuda_ordinal"])
    requested_video_uuid = str(ai_gpu["uuid"]) if options.video_gpu_uuid == "auto" else options.video_gpu_uuid
    video_gpu = video_gpu or ffmpeg.resolve_video_gpu(
        gpus, requested_video_uuid, options.codec, output_width, output_height)
    if video_gpu is None:
        raise RuntimeError("The CUDA RTX Video route requires an available NVENC adapter.")
    encode_ordinal = int(video_gpu["cuda_ordinal"])
    codec_name = _NVENC_CODEC[ffmpeg._normalize_codec(options.codec)]
    input_format = 2 if int(metadata["depth"]) > 8 else 1
    output_p010 = bool(options.hdr_enabled or (input_format == 2 and ffmpeg.hdr_mode_supported(options.codec)))
    quality = ffmpeg.resolve_encoding_quality(
        options.quality, options.codec, output_width, output_height,
        float(metadata["rate"]), hdr_mode=output_p010,
    )
    preview = options.preview_frames is not None or options.preview_seconds is not None
    stamp = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
    extension = {"MP4": ".mp4", "MKV": ".mkv", "MOV": ".mov"}[options.container]
    destination = prepare_output_dir(output_dir)
    kind = "RTXVIDEO_PREVIEW" if preview else "RTXVIDEO"
    output = destination / output_filename(
        source, extension, "Auto" if preview else options.rename_mode,
        options.custom_suffix, f"{source.stem}_{kind}_{stamp}",
    )
    destination_file = OutputFile(output)
    JOBS.mkdir(exist_ok=True)
    session: RTXVideoSession | None = None
    decoded_container: Any | None = None
    encoded_container: Any | None = None
    delivered = 0
    decode_backends: set[str] = set()
    timings = {
        "decode_seconds": 0.0, "software_prepare_seconds": 0.0,
        "bridge_input_seconds": 0.0, "ngx_seconds": 0.0,
        "bridge_output_seconds": 0.0, "encode_seconds": 0.0,
    }
    failures: list[BaseException] = []
    stop = threading.Event()
    decode_stop = threading.Event()
    decode_thread: threading.Thread | None = None
    encode_thread: threading.Thread | None = None
    transfer_pool: CudaTransferPool | None = None

    def update(value: float, message: str) -> None:
        if controller.cancel.is_set():
            raise Cancelled("Upscale stopped by user.")
        if progress:
            progress(max(0.0, min(1.0, value)), message)

    try:
        update(0.01, f"Preparing CUDA decode and encode on {ai_gpu.get('name', 'NVIDIA GPU')}")
        job = tempfile.TemporaryDirectory(prefix="rtx-video-cuda-", dir=JOBS)
        try:
            job_dir = Path(job.name)
            temp_video = job_dir / {"MP4": "encoded.mp4", "MKV": "encoded.mkv", "MOV": "encoded.mov"}[options.container]
            decode_device = HWAccel(
                "cuda", device=str(ai_ordinal), allow_software_fallback=True,
                options={"primary_ctx": "1"}, is_hw_owned=True,
            )
            encode_device = HWAccel(
                "cuda", device=str(encode_ordinal), options={"primary_ctx": "1"}, is_hw_owned=True,
            )
            decoded_container = av.open(str(source), hwaccel=decode_device)
            input_stream = decoded_container.streams.video[0]
            input_stream.thread_type = "AUTO"
            decoder = iter(decoded_container.decode(input_stream))
            decode_start = time.perf_counter()
            try:
                first_frame = next(decoder)
            except StopIteration as exc:
                raise ValueError("The input contains no decodable video frames.") from exc
            timings["decode_seconds"] += time.perf_counter() - decode_start

            encoded_container = av.open(str(temp_video), mode="w")
            output_stream = encoded_container.add_stream(codec_name, rate=metadata["rate"], hwaccel=encode_device)
            output_stream.width, output_stream.height = output_width, output_height
            output_stream.pix_fmt = "cuda"
            output_stream.codec_context.sw_format = "p010le" if output_p010 else "nv12"
            output_stream.time_base = input_stream.time_base
            output_stream.codec_context.time_base = input_stream.time_base
            encoder_options = {
                "preset": "p6", "rc": "vbr", "gpu": str(encode_ordinal),
                # Keep the production encoder's normal B-frame structure.  Bound
                # submission/output buffering to the bridge's eight externally
                # owned surfaces without changing p6/HQ/rate-control decisions.
                "rc-lookahead": "0", "surfaces": "8", "delay": "3",
            }
            if codec_name != "av1_nvenc":
                encoder_options["tune"] = "hq"
            if quality["mode"] == "constant-quality":
                encoder_options["cq"] = "0"
                output_stream.codec_context.bit_rate = 0
            else:
                output_stream.codec_context.bit_rate = int(quality["target_bitrate_kbps"]) * 1000
            output_stream.codec_context.options = encoder_options
            _set_color_properties(output_stream.codec_context, bool(options.hdr_enabled))
            # Force decoder and encoder CUDA contexts to exist before NGX.
            output_stream.codec_context.open()
            capabilities = capabilities or probe_capabilities(options.ai_gpu_uuid, controller=controller)
            session = RTXVideoSession(
                width, height, output_width, output_height, options, input_format,
                capabilities, controller,
            )
            if encode_ordinal != ai_ordinal:
                transfer_pool = CudaTransferPool(ai_ordinal, encode_ordinal, controller)

            decode_queue: queue.Queue = queue.Queue(maxsize=4)
            encode_queue: queue.Queue = queue.Queue(maxsize=4)

            def decode_worker() -> None:
                def put_decode(item: Any) -> bool:
                    while not stop.is_set() and not decode_stop.is_set():
                        if controller.cancel.is_set() or failures:
                            stop.set()
                            return False
                        try:
                            decode_queue.put(item, timeout=0.05)
                            return True
                        except queue.Full:
                            continue
                    return False

                try:
                    if not put_decode(first_frame):
                        return
                    while not stop.is_set() and not decode_stop.is_set():
                        tick = time.perf_counter()
                        try:
                            frame = next(decoder)
                        except StopIteration:
                            timings["decode_seconds"] += time.perf_counter() - tick
                            break
                        timings["decode_seconds"] += time.perf_counter() - tick
                        if frame.is_corrupt:
                            raise RuntimeError("The video decoder returned a corrupt frame.")
                        if not put_decode(frame):
                            return
                except BaseException as exc:
                    failures.append(exc); stop.set()
                finally:
                    if not decode_stop.is_set():
                        put_decode(_END)

            encoded_frames = [0]

            def encode_worker() -> None:
                try:
                    while not stop.is_set():
                        item = _get_bounded(encode_queue, stop, controller, failures)
                        if item is _END:
                            break
                        tick = time.perf_counter()
                        for packet in output_stream.encode(item):
                            encoded_container.mux(packet)
                        timings["encode_seconds"] += time.perf_counter() - tick
                        encoded_frames[0] += 1
                        del item
                    if not failures and not controller.cancel.is_set():
                        tick = time.perf_counter()
                        for packet in output_stream.encode():
                            encoded_container.mux(packet)
                        timings["encode_seconds"] += time.perf_counter() - tick
                except BaseException as exc:
                    failures.append(exc); stop.set()

            decode_thread = threading.Thread(target=decode_worker, name="rtx-video-decoder", daemon=True)
            encode_thread = threading.Thread(target=encode_worker, name="rtx-video-encoder", daemon=True)
            pipeline_started = time.perf_counter()
            decode_thread.start(); encode_thread.start()

            stream_tb = input_stream.time_base or Fraction(1, max(1, round(float(metadata["rate"]))))
            default_duration = max(1, round(Fraction(1, 1) / metadata["rate"] / stream_tb))
            origin_pts = round(Fraction(str(metadata["origin"])) / stream_tb)
            first_time: Fraction | None = None
            preview_pts_origin: int | None = None
            last_pts: int | None = None
            stopped_early = False
            timestamp_fallbacks = 0
            software_normalizer: _SoftwareNormalizer | None = None
            estimated = int(metadata["frames"] or max(1, math.ceil(metadata["duration"] * float(metadata["rate"]))))
            if options.preview_frames is not None:
                estimated = min(estimated, int(options.preview_frames))
            elif options.preview_seconds is not None:
                estimated = min(estimated, max(1, math.ceil(options.preview_seconds * float(metadata["rate"]))))
            last_update = 0.0
            while True:
                frame = _get_bounded(decode_queue, stop, controller, failures)
                if frame is _END:
                    break
                pts, duration, used_fallback = normalize_frame_timing(
                    frame_pts=frame.pts, frame_duration=frame.duration,
                    frame_time_base=frame.time_base, stream_time_base=stream_tb,
                    origin_pts=origin_pts, delivered=delivered,
                    default_duration=default_duration, last_pts=last_pts,
                )
                frame_tb = frame.time_base or stream_tb
                timestamp_fallbacks += int(used_fallback)
                timestamp = Fraction(pts) * stream_tb
                if first_time is None:
                    first_time = timestamp
                if ((options.preview_frames is not None and delivered >= int(options.preview_frames)) or
                        (options.preview_seconds is not None and delivered and
                         float(timestamp - first_time) >= float(options.preview_seconds))):
                    stopped_early = True
                    decode_stop.set()
                    del frame
                    break
                if frame.format.name == "cuda":
                    decode_backends.add("nvdec")
                    processed, detail = session.process_cuda_frame(
                        frame, color_matrix=_matrix_code(metadata), color_range=_range_code(metadata),
                        color_primaries=_primaries_code(metadata), color_transfer=_transfer_code(metadata),
                        chroma_location=_chroma_location_code(metadata),
                        rotation=int(metadata["rotation"]), output_p010=output_p010,
                    )
                else:
                    decode_backends.add("software")
                    tick = time.perf_counter()
                    if software_normalizer is None:
                        software_normalizer = _SoftwareNormalizer(frame, metadata, width, height, input_format == 2)
                    packed = software_normalizer.convert(frame)
                    timings["software_prepare_seconds"] += time.perf_counter() - tick
                    processed, detail = session.process_host_to_cuda_frame(
                        packed, output_p010=output_p010, pts=pts,
                        time_base=stream_tb, duration=duration,
                    )
                if transfer_pool is not None:
                    transferred, transfer_detail = transfer_pool.transfer(processed)
                    del processed
                    processed = transferred
                    timings["cross_gpu_seconds"] = (
                        timings.get("cross_gpu_seconds", 0.0) +
                        float(transfer_detail["copy_ms"]) / 1000.0
                    )
                timings["bridge_input_seconds"] += detail["input_ms"] / 1000.0
                timings["ngx_seconds"] += detail["ngx_ms"] / 1000.0
                timings["bridge_output_seconds"] += detail["output_ms"] / 1000.0
                if preview:
                    if preview_pts_origin is None:
                        preview_pts_origin = pts
                    processed.pts = pts - preview_pts_origin
                else:
                    processed.pts = pts
                processed.time_base = stream_tb
                processed.duration = duration
                if not _put_bounded(encode_queue, processed, stop, controller, failures):
                    raise failures[0] if failures else Cancelled("Upscale stopped by user.")
                delivered += 1
                last_pts = pts
                del frame, processed
                now = time.perf_counter()
                if now - last_update > 0.2:
                    update(
                        min(0.87, 0.05 + 0.82 * delivered / max(1, estimated)),
                        f"RTX Video CUDA: {width}×{height} → {output_width}×{output_height}; "
                        f"{delivered} frames; {delivered / max(0.01, now-started):.1f} fps",
                    )
                    last_update = now
            if not delivered:
                raise ValueError("The input contains no decodable video frames.")
            _put_bounded(encode_queue, _END, threading.Event(), controller, failures)
            decode_thread.join(timeout=30)
            encode_thread.join(timeout=180)
            if decode_thread.is_alive() or encode_thread.is_alive():
                raise RuntimeError("The bounded RTX Video pipeline did not drain cleanly.")
            timings["pipeline_seconds"] = time.perf_counter() - pipeline_started
            if failures:
                raise failures[0]
            if encoded_frames[0] != delivered:
                raise RuntimeError(f"NVENC accepted {encoded_frames[0]} frames instead of {delivered}.")
            encoded_container.close(); encoded_container = None
            decoded_container.close(); decoded_container = None
            gc.collect()
            transfer_diagnostics = transfer_pool.diagnostics() if transfer_pool is not None else {}
            if transfer_pool is not None:
                transfer_pool.close()
                transfer_pool = None
            session_status = session.structured_status(
                decode_backend="+".join(sorted(decode_backends)) or "unknown",
                encode_backend=codec_name,
            )
            session.close()
            if session.completed_frames != delivered:
                raise RuntimeError("RTX Video bridge completion does not match frame accounting.")
            gc.collect()

            if not preview and (not metadata["frames"] or delivered != metadata["frames"]):
                exact = ffmpeg.probe_video(source, count_mode="exact", strict_decode=True, controller=controller)
                if int(exact["frames"]) != delivered:
                    raise RuntimeError(f"Source has {exact['frames']} frames but only {delivered} were processed.")
            update(0.90, "Muxing original audio, subtitles, chapters, and metadata")
            mux_started = time.perf_counter()
            ffmpeg.final_mux(
                temp_video, source, destination_file.temporary, options.container, controller,
                preserve_supported_subtitles=True, source_time_origin=metadata["origin"],
            )
            timings["final_mux_seconds"] = time.perf_counter() - mux_started
            update(0.96, "Verifying output frames, resolution, and HDR signaling")
            verified = ffmpeg.probe_video(destination_file.temporary, count_mode="packets", controller=controller)
            if int(verified["frames"]) != delivered:
                verified = ffmpeg.probe_video(
                    destination_file.temporary, count_mode="exact", strict_decode=True, controller=controller)
            if int(verified["frames"]) != delivered or (int(verified["width"]), int(verified["height"])) != (output_width, output_height):
                raise RuntimeError("Saved output does not match processed frame count or dimensions.")
            saved = inspect_video(destination_file.temporary, controller)
            if options.hdr_enabled and (
                saved["depth"] < 10 or not saved["hdr"] or
                saved["stream"].get("color_primaries") != "bt2020" or
                saved["stream"].get("color_space") != "bt2020nc"
            ):
                raise RuntimeError("Saved file failed HDR color and bit-depth verification.")
            if not options.hdr_enabled and saved["hdr"]:
                raise RuntimeError("SDR output unexpectedly contains HDR signaling.")
            if controller.cancel.is_set():
                raise Cancelled("Upscale stopped by user.")
            elapsed = time.perf_counter() - started
            stage_per_frame = {
                "decode_ms": timings["decode_seconds"] * 1000 / delivered,
                "bridge_ms": (timings["bridge_input_seconds"] + timings["ngx_seconds"] + timings["bridge_output_seconds"]) * 1000 / delivered,
                "encode_ms": timings["encode_seconds"] * 1000 / delivered,
            }
            slowest_ms = max(stage_per_frame.values())
            ceiling_fps = 1000.0 / slowest_ms if slowest_ms > 0 else 0.0
            throughput = delivered / max(timings["pipeline_seconds"], 1e-9)
            timings.update(
                steady_state_fps=throughput, slowest_stage_ceiling_fps=ceiling_fps,
                ceiling_utilization=throughput / ceiling_fps if ceiling_fps else 0.0,
                timestamp_fallbacks=timestamp_fallbacks,
            )
            session_status["timings"] = dict(timings)
            session_status["adapters"] = {"ai": ai_gpu, "decode": ai_gpu, "encode": video_gpu}
            if transfer_diagnostics:
                session_status["cross_gpu_transfer"] = transfer_diagnostics
                session_status["memory_path"] = transfer_diagnostics["memory_path"]
                session_status["upload_bytes"] = (
                    int(session_status.get("upload_bytes", 0)) +
                    int(transfer_diagnostics["pinned_upload_bytes"])
                )
                session_status["download_bytes"] = (
                    int(session_status.get("download_bytes", 0)) +
                    int(transfer_diagnostics["pinned_download_bytes"])
                )
            destination_file.publish()
            app_log.info(
                "upscale-cuda", f"done src={source.name} out={output.name} frames={delivered} "
                f"elapsed={elapsed:.2f}s fps={delivered / max(elapsed, 1e-9):.1f} memory={session_status['memory_path']}",
            )
            update(1.0, "Complete — CUDA-resident RTX Video confirmed")
            return UpscaleResult(
                str(output), report_path, delivered, output_width, output_height,
                options.hdr_enabled, elapsed,
                bridge_version=str(capabilities.bridge_version),
                memory_path=str(session_status["memory_path"]),
                decode_backend=str(session_status["decode_backend"]),
                encode_backend=codec_name, timings=timings, bridge_status=session_status,
            )
        finally:
            job.cleanup()
    except BaseException:
        stop.set()
        decode_stop.set()
        for thread in (decode_thread, encode_thread):
            if thread is not None:
                thread.join(timeout=2)
        if session is not None and not session.closed:
            with suppress(Exception):
                session.close(abort=True)
        if transfer_pool is not None and not transfer_pool.closed:
            with suppress(Exception):
                transfer_pool.close(abort=True)
        if encoded_container is not None:
            with suppress(Exception):
                encoded_container.close()
        if decoded_container is not None:
            with suppress(Exception):
                decoded_container.close()
        destination_file.cleanup(rollback=True)
        raise
