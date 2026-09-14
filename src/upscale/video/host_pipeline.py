from __future__ import annotations

"""In-process NVDEC/software -> RTX Video -> pinned host -> CPU encoder pipeline."""

import ctypes
import gc
import math
import os
import queue
import tempfile
import threading
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable

import av
from av.codec.hwaccel import HWAccel

from ...core import app_log, ffmpeg
from ...core.disk_paths import OutputFile, prepare_output_dir
from ...core.jobs import Cancelled
from ...core.naming import output_filename
from ...core.paths import JOBS
from ...core.ffmpeg.codecs import _x265_hdr_params
from .cuda_pipeline import (
    _END,
    _SoftwareNormalizer,
    _chroma_location_code,
    _get_bounded,
    _matrix_code,
    _primaries_code,
    _put_bounded,
    _range_code,
    _set_color_properties,
    _transfer_code,
    normalize_frame_timing,
)
from .cuda_transfer import CU_MEMHOSTALLOC_PORTABLE, _CudaApi
from .media import inspect_video, packed_bytes
from .models import UpscaleCapabilities, UpscaleOptions, UpscaleResult, output_size
from .native import (
    FORMAT_NV12,
    FORMAT_P010,
    FORMAT_YUV422P10,
    RTXVideoSession,
    probe_capabilities,
)


@dataclass(slots=True)
class _EncoderSettings:
    codec: str
    display_name: str
    pixel_format: str
    native_format: int
    options: dict[str, str]
    bit_rate: int
    quality: dict[str, Any]


def _codec_available(name: str) -> bool:
    try:
        av.codec.Codec(name, "w")
        return True
    except Exception:
        return False


def _encoder_settings(options: UpscaleOptions, width: int, height: int,
                      rate: Fraction, ten_bit: bool) -> _EncoderSettings:
    codec = ffmpeg._normalize_codec(options.codec)
    quality = ffmpeg.resolve_encoding_quality(
        options.quality, options.codec, width, height, float(rate), hdr_mode=ten_bit)
    bit_rate = int(quality.get("target_bitrate_kbps") or 0) * 1000
    common: dict[str, str] = {}
    if quality["mode"] == "constant-quality":
        common["crf"] = "0"
    if codec == "H.264":
        return _EncoderSettings(
            "libx264", "libx264", "yuv420p", FORMAT_NV12,
            {"preset": "slow", **common}, bit_rate, quality)
    if codec in {"H.265", "HEVC"}:
        values = {"preset": "slow", **common}
        if options.hdr_enabled:
            colors = {"color_space": "bt2020nc", "color_transfer": "smpte2084",
                      "color_primaries": "bt2020", "hdr": True}
            hdr_parameters = _x265_hdr_params(colors)
            if hdr_parameters:
                values["x265-params"] = hdr_parameters
        return _EncoderSettings(
            "libx265", "libx265", "yuv420p10le" if ten_bit else "yuv420p",
            FORMAT_P010 if ten_bit else FORMAT_NV12, values, bit_rate, quality)
    if codec == "AV1":
        encoder = "libsvtav1" if _codec_available("libsvtav1") else "libaom-av1"
        if not _codec_available(encoder):
            raise RuntimeError(
                "AV1 CPU encoding is unavailable: neither libsvtav1 nor libaom-av1 can initialize.")
        values = ({"preset": "6"} if encoder == "libsvtav1" else {"cpu-used": "4"})
        values.update(common)
        if encoder == "libaom-av1" and quality["mode"] == "constant-quality":
            values["b"] = "0"
        return _EncoderSettings(
            encoder, encoder, "yuv420p10le" if ten_bit else "yuv420p",
            FORMAT_P010 if ten_bit else FORMAT_NV12, values, bit_rate, quality)
    if codec == "ProRes Proxy":
        values = {"profile": "0"}
        if quality.get("bits_per_mb") is not None:
            values["bits_per_mb"] = str(int(quality["bits_per_mb"]))
        return _EncoderSettings(
            "prores_ks", "prores_ks (Proxy)", "yuv422p10le",
            FORMAT_YUV422P10, values, 0, quality)
    raise ValueError(f"The in-process host pipeline does not support codec {options.codec!r}.")


@dataclass(slots=True)
class _PinnedSlot:
    owner: "_PinnedFramePool"
    pointer: int
    plane_pointers: tuple[int, ...]
    strides: tuple[int, ...]
    rows: tuple[int, ...]
    row_bytes: tuple[int, ...]
    held: bool = False
    pts: int | None = None
    duration: int | None = None
    time_base: Fraction | None = None

    def release(self) -> None:
        with self.owner.condition:
            if self.held:
                self.held = False
                self.owner.condition.notify_all()

    def to_av_frame(self) -> tuple[av.VideoFrame, float]:
        tick = time.perf_counter()
        try:
            frame = av.VideoFrame(self.owner.width, self.owner.height, self.owner.pixel_format)
            for index, plane in enumerate(frame.planes):
                source = self.plane_pointers[index]
                source_stride = self.strides[index]
                count = self.row_bytes[index]
                rows = self.rows[index]
                destination = int(plane.buffer_ptr)
                destination_stride = int(plane.line_size)
                if count == source_stride == destination_stride:
                    ctypes.memmove(destination, source, count * rows)
                else:
                    for row in range(rows):
                        ctypes.memmove(destination + row * destination_stride,
                                       source + row * source_stride, count)
            frame.pts, frame.time_base = self.pts, self.time_base
            if self.duration is not None:
                frame.duration = self.duration
            return frame, time.perf_counter() - tick
        finally:
            self.release()


class _PinnedFramePool:
    """Four preallocated page-locked frames shared by RTX and encoder stages."""

    def __init__(self, ordinal: int, width: int, height: int, native_format: int,
                 pixel_format: str, controller: Any, *, capacity: int = 4,
                 timeout: float = 180.0) -> None:
        self.api = _CudaApi()
        self.ordinal, self.width, self.height = int(ordinal), int(width), int(height)
        self.native_format, self.pixel_format = int(native_format), str(pixel_format)
        self.controller, self.capacity, self.timeout = controller, int(capacity), float(timeout)
        self.condition = threading.Condition()
        self.pool_waits = 0
        self.closed = False
        self.device = self.api.device(self.ordinal)
        self.context = self.api.primary_context(self.device)
        if native_format == FORMAT_NV12:
            strides = (width, width)
            rows = (height, (height + 1) // 2)
            row_bytes = strides
        elif native_format == FORMAT_P010:
            strides = (width * 2, width * 2)
            rows = (height, (height + 1) // 2)
            row_bytes = strides
        elif native_format == FORMAT_YUV422P10:
            strides = (width * 2, ((width + 1) // 2) * 2, ((width + 1) // 2) * 2)
            rows = (height, height, height)
            row_bytes = strides
        else:
            self.api.check(self.api.primary_release(self.device), "cuDevicePrimaryCtxRelease")
            raise ValueError("Unsupported pinned-frame format.")
        self.frame_bytes = sum(stride * count for stride, count in zip(strides, rows))
        self.slots: list[_PinnedSlot] = []
        try:
            with self.api.current(self.context):
                for _ in range(self.capacity):
                    allocation = ctypes.c_void_p()
                    self.api.check(self.api.cuMemHostAlloc(
                        ctypes.byref(allocation), self.frame_bytes, CU_MEMHOSTALLOC_PORTABLE),
                        "cuMemHostAlloc(encoder frame)")
                    base = int(allocation.value or 0)
                    pointers, offset = [], 0
                    for stride, count in zip(strides, rows):
                        pointers.append(base + offset)
                        offset += stride * count
                    self.slots.append(_PinnedSlot(
                        self, base, tuple(pointers), tuple(strides), tuple(rows),
                        tuple(row_bytes)))
        except BaseException:
            self.close(abort=True)
            raise

    def acquire(self) -> _PinnedSlot:
        deadline = time.monotonic() + self.timeout
        with self.condition:
            while True:
                if self.controller.cancel.is_set():
                    raise Cancelled("Upscale stopped by user.")
                for slot in self.slots:
                    if not slot.held:
                        slot.held = True
                        return slot
                self.pool_waits += 1
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError("Pinned encoder-frame pool remained exhausted.")
                self.condition.wait(timeout=min(0.05, remaining))

    def diagnostics(self) -> dict[str, Any]:
        return {
            "surface_pool_capacity": self.capacity,
            "surface_pool_allocated": len(self.slots),
            "surface_pool_waits": self.pool_waits,
            "bytes_per_surface": self.frame_bytes,
            "allocation": "cuda_page_locked_host",
        }

    def close(self, *, abort: bool = False) -> None:
        if self.closed:
            return
        deadline = time.monotonic() + (2.0 if abort else self.timeout)
        with self.condition:
            while any(slot.held for slot in self.slots):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    if abort:
                        self.closed = True
                        return
                    raise RuntimeError("Encoder retained pinned frames past shutdown.")
                self.condition.wait(timeout=min(0.05, remaining))
        try:
            with self.api.current(self.context):
                for slot in self.slots:
                    if slot.pointer:
                        self.api.check(self.api.cuMemFreeHost(ctypes.c_void_p(slot.pointer)),
                                       "cuMemFreeHost(encoder frame)")
                        slot.pointer = 0
        finally:
            self.api.check(self.api.primary_release(self.device), "cuDevicePrimaryCtxRelease")
            self.closed = True


def convert_video_inprocess_host(
    source: Path,
    options: UpscaleOptions,
    *,
    controller: Any,
    progress: Callable[[float, str], None] | None,
    output_dir: str | os.PathLike[str] | None,
    metadata: dict[str, Any] | None = None,
    capabilities: UpscaleCapabilities | None = None,
) -> UpscaleResult:
    """Run CPU/ProRes encoding without raw frame or NUT subprocess pipes."""
    started = time.perf_counter()
    report_path = app_log.session_path()
    metadata = metadata or inspect_video(source, controller, reject_hdr=True)
    width, height = int(metadata["width"]), int(metadata["height"])
    output_width, output_height, _ = output_size(width, height, options)
    capabilities = capabilities or probe_capabilities(options.ai_gpu_uuid, controller=controller)
    ai_gpu = capabilities.gpu
    ordinal = int(ai_gpu["cuda_ordinal"])
    input_format = 2 if int(metadata["depth"]) > 8 else 1
    ten_bit = bool(
        options.hdr_enabled or
        (input_format == 2 and ffmpeg.hdr_mode_supported(options.codec)) or
        ffmpeg._normalize_codec(options.codec) == "ProRes Proxy")
    encoder = _encoder_settings(options, output_width, output_height, metadata["rate"], ten_bit)
    preview = options.preview_frames is not None or options.preview_seconds is not None
    stamp = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
    extension = {"MP4": ".mp4", "MKV": ".mkv", "MOV": ".mov"}[options.container]
    destination = prepare_output_dir(output_dir)
    kind = "RTXVIDEO_PREVIEW" if preview else "RTXVIDEO"
    output = destination / output_filename(
        source, extension, "Auto" if preview else options.rename_mode,
        options.custom_suffix, f"{source.stem}_{kind}_{stamp}")
    destination_file = OutputFile(output)
    decoded_container: Any | None = None
    encoded_container: Any | None = None
    session: RTXVideoSession | None = None
    pinned_pool: _PinnedFramePool | None = None
    decode_thread: threading.Thread | None = None
    encode_thread: threading.Thread | None = None
    failures: list[BaseException] = []
    stop, decode_stop = threading.Event(), threading.Event()
    delivered = 0
    decode_backends: set[str] = set()
    timings: dict[str, float] = {
        "decode_seconds": 0.0, "software_prepare_seconds": 0.0,
        "bridge_input_seconds": 0.0, "ngx_seconds": 0.0,
        "bridge_output_seconds": 0.0, "host_copy_seconds": 0.0,
        "encode_seconds": 0.0,
    }

    def update(value: float, message: str) -> None:
        if controller.cancel.is_set():
            raise Cancelled("Upscale stopped by user.")
        if progress:
            progress(max(0.0, min(1.0, value)), message)

    def drain_slots(target: queue.Queue | None) -> None:
        if target is None:
            return
        while True:
            try:
                item = target.get_nowait()
            except queue.Empty:
                return
            if isinstance(item, _PinnedSlot):
                item.release()

    encode_queue: queue.Queue | None = None
    try:
        update(0.01, f"Preparing in-process CUDA and {encoder.display_name}")
        JOBS.mkdir(exist_ok=True)
        job = tempfile.TemporaryDirectory(prefix="rtx-video-host-", dir=JOBS)
        try:
            job_dir = Path(job.name)
            temp_video = job_dir / ("encoded.mov" if encoder.codec == "prores_ks" else "encoded.mp4")
            decode_device = HWAccel(
                "cuda", device=str(ordinal), allow_software_fallback=True,
                options={"primary_ctx": "1"}, is_hw_owned=True)
            decoded_container = av.open(str(source), hwaccel=decode_device)
            input_stream = decoded_container.streams.video[0]
            input_stream.thread_type = "AUTO"
            decoder_iterator = iter(decoded_container.decode(input_stream))
            tick = time.perf_counter()
            try:
                first_frame = next(decoder_iterator)
            except StopIteration as exc:
                raise ValueError("The input contains no decodable video frames.") from exc
            timings["decode_seconds"] += time.perf_counter() - tick

            encoded_container = av.open(str(temp_video), mode="w")
            output_stream = encoded_container.add_stream(encoder.codec, rate=metadata["rate"])
            output_stream.width, output_stream.height = output_width, output_height
            output_stream.pix_fmt = encoder.pixel_format
            stream_tb = input_stream.time_base or Fraction(1, max(1, round(float(metadata["rate"]))))
            output_stream.time_base = stream_tb
            output_stream.codec_context.time_base = stream_tb
            output_stream.codec_context.options = encoder.options
            output_stream.codec_context.bit_rate = encoder.bit_rate
            _set_color_properties(output_stream.codec_context, bool(options.hdr_enabled))
            output_stream.codec_context.open()

            session = RTXVideoSession(
                width, height, output_width, output_height, options, input_format,
                capabilities, controller)
            pinned_pool = _PinnedFramePool(
                ordinal, output_width, output_height, encoder.native_format,
                {FORMAT_NV12: "nv12", FORMAT_P010: "p010le",
                 FORMAT_YUV422P10: "yuv422p10le"}[encoder.native_format], controller)
            decode_queue: queue.Queue = queue.Queue(maxsize=4)
            encode_queue = queue.Queue(maxsize=4)

            def decode_worker() -> None:
                def put(item: Any) -> bool:
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
                    if not put(first_frame):
                        return
                    while not stop.is_set() and not decode_stop.is_set():
                        frame_tick = time.perf_counter()
                        try:
                            frame = next(decoder_iterator)
                        except StopIteration:
                            timings["decode_seconds"] += time.perf_counter() - frame_tick
                            break
                        timings["decode_seconds"] += time.perf_counter() - frame_tick
                        if frame.is_corrupt:
                            raise RuntimeError("The video decoder returned a corrupt frame.")
                        if not put(frame):
                            return
                except BaseException as exc:
                    failures.append(exc)
                    stop.set()
                finally:
                    if not decode_stop.is_set():
                        put(_END)

            encoded_frames = [0]

            def encode_worker() -> None:
                try:
                    while not stop.is_set():
                        item = _get_bounded(encode_queue, stop, controller, failures)
                        if item is _END:
                            break
                        encode_tick = time.perf_counter()
                        frame, copy_seconds = item.to_av_frame()
                        timings["host_copy_seconds"] += copy_seconds
                        if frame.format.name != encoder.pixel_format:
                            copy_tick = time.perf_counter()
                            frame = frame.reformat(format=encoder.pixel_format)
                            timings["host_copy_seconds"] += time.perf_counter() - copy_tick
                        for packet in output_stream.encode(frame):
                            encoded_container.mux(packet)
                        timings["encode_seconds"] += time.perf_counter() - encode_tick
                        encoded_frames[0] += 1
                        del item, frame
                    if not failures and not controller.cancel.is_set():
                        encode_tick = time.perf_counter()
                        for packet in output_stream.encode():
                            encoded_container.mux(packet)
                        timings["encode_seconds"] += time.perf_counter() - encode_tick
                except BaseException as exc:
                    failures.append(exc)
                    stop.set()

            decode_thread = threading.Thread(target=decode_worker, name="rtx-host-decoder", daemon=True)
            encode_thread = threading.Thread(target=encode_worker, name="rtx-host-encoder", daemon=True)
            pipeline_started = time.perf_counter()
            decode_thread.start()
            encode_thread.start()

            default_duration = max(1, round(Fraction(1, 1) / metadata["rate"] / stream_tb))
            origin_pts = round(Fraction(str(metadata["origin"])) / stream_tb)
            first_time: Fraction | None = None
            preview_origin: int | None = None
            last_pts: int | None = None
            timestamp_fallbacks = 0
            stopped_early = False
            software_normalizer: _SoftwareNormalizer | None = None
            source_dimensions = (int(first_frame.width), int(first_frame.height))
            estimated = int(metadata["frames"] or max(
                1, math.ceil(metadata["duration"] * float(metadata["rate"]))))
            if options.preview_frames is not None:
                estimated = min(estimated, int(options.preview_frames))
            elif options.preview_seconds is not None:
                estimated = min(estimated, max(
                    1, math.ceil(options.preview_seconds * float(metadata["rate"]))))
            last_update = 0.0
            while True:
                frame = _get_bounded(decode_queue, stop, controller, failures)
                if frame is _END:
                    break
                if (int(frame.width), int(frame.height)) != source_dimensions:
                    raise ValueError("Source dimensions changed during decoding.")
                pts, duration, used_fallback = normalize_frame_timing(
                    frame_pts=frame.pts, frame_duration=frame.duration,
                    frame_time_base=frame.time_base, stream_time_base=stream_tb,
                    origin_pts=origin_pts, delivered=delivered,
                    default_duration=default_duration, last_pts=last_pts)
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
                slot = pinned_pool.acquire()
                try:
                    if frame.format.name == "cuda":
                        decode_backends.add("nvdec")
                        detail = session.process_cuda_to_host_planar(
                            frame, output_format=encoder.native_format,
                            plane_pointers=slot.plane_pointers, strides=slot.strides,
                            color_matrix=_matrix_code(metadata), color_range=_range_code(metadata),
                            color_primaries=_primaries_code(metadata),
                            color_transfer=_transfer_code(metadata),
                            chroma_location=_chroma_location_code(metadata),
                            rotation=int(metadata["rotation"]))
                    else:
                        decode_backends.add("software")
                        prepare_tick = time.perf_counter()
                        if software_normalizer is None:
                            software_normalizer = _SoftwareNormalizer(
                                frame, metadata, width, height, input_format == 2)
                        packed = software_normalizer.convert(frame)
                        timings["software_prepare_seconds"] += time.perf_counter() - prepare_tick
                        detail = session.process_host_to_host_planar(
                            packed, output_format=encoder.native_format,
                            plane_pointers=slot.plane_pointers, strides=slot.strides)
                    timings["bridge_input_seconds"] += detail["input_ms"] / 1000.0
                    timings["ngx_seconds"] += detail["ngx_ms"] / 1000.0
                    timings["bridge_output_seconds"] += detail["output_ms"] / 1000.0
                    if preview:
                        if preview_origin is None:
                            preview_origin = pts
                        slot.pts = pts - preview_origin
                    else:
                        slot.pts = pts
                    slot.time_base, slot.duration = stream_tb, duration
                    if not _put_bounded(encode_queue, slot, stop, controller, failures):
                        raise failures[0] if failures else Cancelled("Upscale stopped by user.")
                except BaseException:
                    slot.release()
                    raise
                delivered += 1
                last_pts = pts
                del frame, slot
                now = time.perf_counter()
                if now - last_update > 0.2:
                    update(
                        min(0.87, 0.05 + 0.82 * delivered / max(1, estimated)),
                        f"RTX Video pinned pipeline: {width}×{height} → {output_width}×{output_height}; "
                        f"{delivered} frames; {delivered / max(0.01, now-started):.1f} fps")
                    last_update = now
            if not delivered:
                raise ValueError("The input contains no decodable video frames.")
            _put_bounded(encode_queue, _END, threading.Event(), controller, failures)
            decode_thread.join(timeout=30)
            encode_thread.join(timeout=600)
            if decode_thread.is_alive() or encode_thread.is_alive():
                raise RuntimeError("The bounded host-encoding pipeline did not drain cleanly.")
            timings["pipeline_seconds"] = time.perf_counter() - pipeline_started
            if failures:
                raise failures[0]
            if encoded_frames[0] != delivered:
                raise RuntimeError(f"The encoder accepted {encoded_frames[0]} frames instead of {delivered}.")
            encoded_container.close()
            encoded_container = None
            decoded_container.close()
            decoded_container = None
            gc.collect()
            pool_status = pinned_pool.diagnostics()
            pinned_pool.close()
            pinned_pool = None
            session_status = session.structured_status(
                decode_backend="+".join(sorted(decode_backends)) or "unknown",
                encode_backend=encoder.display_name)
            session.close()
            if session.completed_frames != delivered:
                raise RuntimeError("RTX Video bridge completion does not match frame accounting.")
            if not preview and (not metadata["frames"] or delivered != metadata["frames"]):
                exact = ffmpeg.probe_video(source, count_mode="exact", strict_decode=True,
                                           controller=controller)
                if int(exact["frames"]) != delivered:
                    raise RuntimeError(
                        f"Source has {exact['frames']} frames but only {delivered} were processed.")
            update(0.90, "Muxing original audio, subtitles, chapters, and metadata")
            mux_tick = time.perf_counter()
            ffmpeg.final_mux(
                temp_video, source, destination_file.temporary, options.container, controller,
                preserve_supported_subtitles=True, source_time_origin=metadata["origin"])
            timings["final_mux_seconds"] = time.perf_counter() - mux_tick
            update(0.96, "Verifying output frames, resolution, chroma, and HDR signaling")
            verified = ffmpeg.probe_video(destination_file.temporary, count_mode="packets",
                                          controller=controller)
            if int(verified["frames"]) != delivered:
                verified = ffmpeg.probe_video(
                    destination_file.temporary, count_mode="exact", strict_decode=True,
                    controller=controller)
            if (int(verified["frames"]) != delivered or
                    (int(verified["width"]), int(verified["height"])) !=
                    (output_width, output_height)):
                raise RuntimeError("Saved output does not match processed frame count or dimensions.")
            saved = inspect_video(destination_file.temporary, controller)
            if encoder.codec == "prores_ks" and saved["stream"].get("pix_fmt") != "yuv422p10le":
                raise RuntimeError("ProRes output did not preserve direct 10-bit 4:2:2 chroma.")
            if options.hdr_enabled and (
                    saved["depth"] < 10 or not saved["hdr"] or
                    saved["stream"].get("color_primaries") != "bt2020" or
                    saved["stream"].get("color_space") != "bt2020nc"):
                raise RuntimeError("Saved file failed HDR color and bit-depth verification.")
            if not options.hdr_enabled and saved["hdr"]:
                raise RuntimeError("SDR output unexpectedly contains HDR signaling.")
            if controller.cancel.is_set():
                raise Cancelled("Upscale stopped by user.")
            elapsed = time.perf_counter() - started
            throughput = delivered / max(timings["pipeline_seconds"], 1e-9)
            bridge_seconds = (timings["bridge_input_seconds"] + timings["ngx_seconds"] +
                              timings["bridge_output_seconds"])
            stage_ms = {
                "decode_ms": timings["decode_seconds"] * 1000 / delivered,
                "bridge_ms": bridge_seconds * 1000 / delivered,
                "encode_ms": timings["encode_seconds"] * 1000 / delivered,
            }
            slowest_ms = max(stage_ms.values())
            ceiling_fps = 1000.0 / slowest_ms if slowest_ms else 0.0
            timings.update(
                steady_state_fps=throughput,
                slowest_stage_ceiling_fps=ceiling_fps,
                ceiling_utilization=throughput / ceiling_fps if ceiling_fps else 0.0,
                timestamp_fallbacks=timestamp_fallbacks)
            memory_path = (
                "cuda_to_pinned_host"
                if decode_backends == {"nvdec"}
                else "host_upload_cuda_to_pinned_host"
            )
            session_status["timings"] = dict(timings)
            session_status["memory_path"] = memory_path
            session_status["pinned_pool"] = pool_status
            session_status["adapters"] = {"ai": ai_gpu, "decode": ai_gpu, "encode": "CPU"}
            destination_file.publish()
            app_log.info(
                "upscale-host", f"done src={source.name} out={output.name} frames={delivered} "
                f"elapsed={elapsed:.2f}s fps={delivered / max(elapsed, 1e-9):.1f} "
                f"encoder={encoder.display_name}")
            update(1.0, "Complete — in-process pinned RTX Video path confirmed")
            return UpscaleResult(
                str(output), report_path, delivered, output_width, output_height,
                options.hdr_enabled, elapsed,
                bridge_version=str(capabilities.bridge_version),
                memory_path=memory_path,
                decode_backend="+".join(sorted(decode_backends)) or "unknown",
                encode_backend=encoder.display_name, timings=timings,
                bridge_status=session_status)
        finally:
            job.cleanup()
    except BaseException:
        stop.set()
        decode_stop.set()
        for thread in (decode_thread, encode_thread):
            if thread is not None:
                thread.join(timeout=2)
        drain_slots(encode_queue)
        if encoded_container is not None:
            with suppress(Exception):
                encoded_container.close()
        if decoded_container is not None:
            with suppress(Exception):
                decoded_container.close()
        if session is not None and not session.closed:
            with suppress(Exception):
                session.close(abort=True)
        if pinned_pool is not None and not pinned_pool.closed:
            with suppress(Exception):
                pinned_pool.close(abort=True)
        destination_file.cleanup(rollback=True)
        raise
