from __future__ import annotations

import gc
import math
import os
import queue
import tempfile
import threading
import time
from contextlib import nullcontext, suppress
from dataclasses import dataclass, replace
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable

import av
from ..core.ffmpeg.frames import open_video_decoder, VideoOutput
import numpy as np
from av.codec.hwaccel import HWAccel

from ..core import app_log, ffmpeg
from ..core.disk_paths import OutputFile, prepare_output_dir
from ..core.gpu_selection import resolve_runtime_ai_gpu
from ..core.jobs import Cancelled, active_job
from ..core.naming import output_filename, unique_output_path, validate_rename
from ..core.paths import JOBS, OUTPUTS
from ..core.runtime import prepare_runtime
from ..upscale.video.cuda_transfer import CudaTransferPool
from .capabilities import probe_frame_interpolation_capabilities
from .models import FrameInterpolationOptions, FrameInterpolationResult, OPTICAL_FLOW_QUALITIES
from .native import DLSSGCudaSurface, DirectDLSSGSession
from .scheduler import choose_interpolation_plan, output_frame_count

_BATCH_CONTEXT = threading.local()
_END = object()
_NVENC = {
    "H.264 (NVIDIA NVENC)": "h264_nvenc",
    "H.265 (NVIDIA NVENC)": "hevc_nvenc",
    "AV1 (NVIDIA NVENC)": "av1_nvenc",
}


@dataclass(slots=True)
class TimedFrame:
    bridge_frame: Any
    encode_frame: Any
    timestamp: Fraction
    segment: int
    provenance: str
    source_index: int | None = None


@dataclass(slots=True)
class _Encoder:
    name: str
    display: str
    pixel_format: str
    cuda_input: bool
    options: dict[str, str]
    bitrate: int


def _is_nvenc(codec: str) -> bool:
    return ffmpeg._normalize_codec(codec) in _NVENC


def automatic_cuda_path(codec: str, rotation: int | float = 0) -> bool:
    """Select the fastest valid transport without a user-facing mode switch."""
    return _is_nvenc(codec) and not bool(rotation)


def _codec_available(name: str) -> bool:
    from ..core.ffmpeg.vulkan import ffmpeg_encoders
    return name in ffmpeg_encoders()


def _encoder(options: FrameInterpolationOptions, codec: str, width: int, height: int,
             rate: Fraction, hdr: bool, cuda_input: bool, ordinal: int) -> _Encoder:
    normalized = ffmpeg._normalize_codec(codec)
    quality = ffmpeg.resolve_encoding_quality(
        options.quality, codec, width, height, float(rate), hdr_mode=hdr)
    bitrate = int(quality.get("target_bitrate_kbps") or 0) * 1000
    if normalized in _NVENC:
        name = _NVENC[normalized]
        values = {"preset": "p6", "rc": "vbr", "gpu": str(ordinal),
                  "rc-lookahead": "0", "surfaces": "8", "delay": "3"}
        if name == "h264_nvenc":
            # Match the CLI route: CUDA-owned PyAV frames otherwise default
            # to Main and lose High profile's 8x8 transform on fine detail.
            values["profile"] = "high"
        if name != "av1_nvenc":
            values["tune"] = "hq"
        values.update(ffmpeg.nvenc_rate_control_options(quality))
        return _Encoder(name, name, "cuda" if cuda_input else ("p010le" if hdr else "nv12"),
                        cuda_input, values, bitrate)
    common = {"crf": "0"} if quality["mode"] == "constant-quality" else {}
    if normalized == "H.264":
        return _Encoder("libx264", "libx264", "yuv420p", False,
                        {"preset": "slow", **common}, bitrate)
    if normalized in {"H.265", "HEVC"}:
        return _Encoder("libx265", "libx265", "yuv420p10le" if hdr else "yuv420p",
                        False, {"preset": "slow", **common}, bitrate)
    if normalized == "AV1":
        name = "libsvtav1" if _codec_available("libsvtav1") else "libaom-av1"
        if not _codec_available(name):
            raise RuntimeError("No in-process CPU AV1 encoder is available.")
        values = ({"preset": "6"} if name == "libsvtav1" else {"cpu-used": "4"})
        if quality["mode"] == "constant-quality":
            values.update(ffmpeg.software_max_quality_options(name))
        return _Encoder(name, name, "yuv420p10le" if hdr else "yuv420p", False, values, bitrate)
    if normalized == "ProRes Proxy":
        values = {"profile": "0"}
        bits_per_mb = quality.get("bits_per_mb")
        if bits_per_mb is None and options.quality == "Auto (Default)":
            bits_per_mb = 300
        if bits_per_mb is not None:
            values["bits_per_mb"] = str(int(bits_per_mb))
        return _Encoder("prores_ks", "prores_ks (Proxy)", "yuv422p10le", False, values, 0)
    if normalized == "ProRes HQ":
        return _Encoder("prores_ks", "prores_ks (HQ)", "yuv422p10le", False,
                        {"profile": "3"}, 0)
    if normalized == "FFV1 Lossless RGB 10-bit":
        return _Encoder("ffv1", "ffv1 (Lossless RGB 10-bit)", "gbrp10le", False,
                        {"level": "3", "slicecrc": "1"}, 0)
    raise ValueError(f"Unsupported in-process codec: {codec}.")


def _matrix_code(metadata: dict[str, Any], *, hdr: bool = False) -> int:
    value = str(metadata.get("color_space") or "").casefold()
    if value in {"bt2020nc", "bt2020c"}:
        return 2
    if value in {"bt470bg", "smpte170m"}:
        return 0
    if value == "bt709":
        return 1
    if value == "fcc":
        return 3
    if value == "smpte240m":
        return 4
    # FFmpeg decodes untagged SDR YUV to RGB with its BT.601 default. Match
    # that interpretation when the RGB pixels return to an encoded YUV frame.
    return 2 if hdr else 0


def _range_code(metadata: dict[str, Any]) -> int:
    return int(str(metadata.get("color_range") or "").casefold() in {"pc", "jpeg", "full"})


def _primaries_code(metadata: dict[str, Any]) -> int:
    value = str(metadata.get("color_primaries") or "").casefold()
    return 2 if value == "bt2020" else 0 if value in {"bt470bg", "bt470m", "smpte170m"} else 1


def _transfer_code(metadata: dict[str, Any]) -> int:
    return {"iec61966-2-1": 1, "srgb": 1, "gamma22": 2, "bt470m": 2,
            "gamma28": 3, "bt470bg": 3, "linear": 4}.get(
        str(metadata.get("color_transfer") or "").casefold(), 0)


def _set_color_properties(context: Any, metadata: dict[str, Any], hdr: bool) -> None:
    primaries = str(metadata.get("color_primaries") or "").casefold()
    transfer = str(metadata.get("color_transfer") or "").casefold()
    matrix = str(metadata.get("color_space") or "").casefold()
    context.color_primaries = {"bt709": 1, "bt470m": 4, "bt470bg": 5,
                               "smpte170m": 6, "smpte240m": 7, "bt2020": 9,
                               "smpte432": 12}.get(primaries, 2)
    context.color_trc = {"bt709": 1, "bt470m": 4, "bt470bg": 5,
                         "smpte170m": 6, "smpte240m": 7, "linear": 8, "iec61966-2-1": 13,
                         "smpte2084": 16, "arib-std-b67": 18}.get(transfer, 2)
    context.colorspace = {"gbr": 0, "bt709": 1, "fcc": 4, "bt470bg": 5,
                          "smpte170m": 6, "smpte240m": 7, "bt2020nc": 9,
                          "bt2020c": 10}.get(matrix, 9 if hdr else 6)
    context.color_range = 2 if _range_code(metadata) else 1
    if hdr and primaries in {"", "unknown"}:
        context.color_primaries, context.color_trc, context.colorspace = 9, 16, 9


def _put(target: queue.Queue, item: Any, stop: threading.Event,
         controller: Any, failures: list[BaseException]) -> None:
    while not stop.is_set():
        if controller.cancel.is_set():
            stop.set()
            raise Cancelled("Frame interpolation stopped by user.")
        if failures:
            stop.set()
            raise failures[0]
        try:
            target.put(item, timeout=0.05)
            return
        except queue.Full:
            continue
    raise failures[0] if failures else Cancelled("Frame interpolation stopped by user.")


def _get(source: queue.Queue, stop: threading.Event,
         controller: Any, failures: list[BaseException]) -> Any:
    while True:
        if controller.cancel.is_set():
            stop.set()
            raise Cancelled("Frame interpolation stopped by user.")
        if failures:
            stop.set()
            raise failures[0]
        try:
            return source.get(timeout=0.05)
        except queue.Empty:
            if stop.is_set():
                raise failures[0] if failures else Cancelled("Frame interpolation stopped by user.")


def _validate_decoded_frame(frame: Any) -> Any:
    if bool(getattr(frame, "is_corrupt", False)):
        raise RuntimeError("The video decoder returned a corrupt frame.")
    return frame


def _open_frame_decoder(source: Path, controller, *, decode_device, hdr: bool,
                        rotation_filter: str):
    if decode_device is not None:
        decoded = None
        try:
            decoded = av.open(str(source), hwaccel=decode_device)
            stream = decoded.streams.video[0]
            stream.thread_type = "AUTO"
            frames = iter(decoded.decode(stream))
            first = _validate_decoded_frame(next(frames))
            sw_format = str(getattr(getattr(first, "sw_format", None), "name", ""))
            if first.format.name != "cuda" or sw_format not in {"nv12", "p010", "p010le"}:
                raise ValueError("NVDEC did not return a supported CUDA NV12/P010 surface.")
            return decoded, frames, first, "PyAV CUDA/NVDEC"
        except (av.error.FFmpegError, ValueError, RuntimeError, StopIteration) as exc:
            if decoded is not None:
                decoded.close()
            app_log.info("frame-interp", f"NVDEC unavailable; using the host frame boundary: {exc}")
    decoded = open_video_decoder(source, controller, pixel_format="p010le" if hdr else "rgba",
                                 video_filter=rotation_filter)
    stream = decoded.streams.video[0]
    stream.thread_type = "AUTO"
    frames = iter(decoded.decode(stream))
    try:
        first = _validate_decoded_frame(next(frames))
    except BaseException:
        decoded.close()
        raise
    return decoded, frames, first, "FFmpeg Vulkan/software fallback"


class DLSSGStage:
    def __init__(self, session: DirectDLSSGSession, generated_count: int, *,
                 detect_source_cuts: bool, cuda_output: bool, output_p010: bool,
                 colors: dict[str, int], preserve_source_rgb: bool = False) -> None:
        self.session, self.generated_count = session, int(generated_count)
        self.detect_source_cuts, self.cuda_output = detect_source_cuts, cuda_output
        self.output_p010, self.colors = output_p010, colors
        self.preserve_source_rgb = preserve_source_rgb
        self.previous: TimedFrame | None = None
        self.scene_cuts = self.duplicates = 0

    def push(self, frame: TimedFrame) -> list[TimedFrame]:
        previous = self.previous
        force_reset = previous is not None and frame.segment != previous.segment
        bridge_frame = frame.bridge_frame
        if (isinstance(bridge_frame, av.VideoFrame)
                and bridge_frame.format.name not in {"cuda", "p010", "p010le"}):
            bridge_frame = np.ascontiguousarray(bridge_frame.to_ndarray(format="rgba"))
        generated, detail = self.session.process_frame(
            bridge_frame, force_reset=force_reset,
            detect_scene_cut=self.detect_source_cuts, output_cuda=self.cuda_output,
            output_p010=self.output_p010, output_rgb=True, **self.colors)
        if self.preserve_source_rgb:
            source_rgb = self.session.copy_current_input_rgb()
            frame = replace(frame, bridge_frame=source_rgb, encode_frame=source_rgb)
        if previous is not None and detail["scene_cut"] and self.detect_source_cuts:
            frame = replace(frame, segment=previous.segment + 1)
            self.scene_cuts += 1
        self.duplicates += int(previous is not None and detail["duplicate"])
        output: list[TimedFrame] = []
        if previous is not None and not detail["reset"]:
            interval = frame.timestamp - previous.timestamp
            for index, pixels in enumerate(generated, start=1):
                output.append(TimedFrame(
                    pixels, pixels,
                    previous.timestamp + interval * Fraction(index, self.generated_count + 1),
                    frame.segment, "DLSSG"))
        self.previous = frame
        output.append(frame)
        return output


class NearestTimestampWriter:
    def __init__(self, target_rate: Fraction, output_count: int,
                 emit: Callable[[TimedFrame, int], None]) -> None:
        self.target_rate, self.output_count, self.emit = target_rate, int(output_count), emit
        self.next_index = 0
        self.previous: TimedFrame | None = None
        self.tie_late = False
        self.copied = self.generated = 0
        self.max_error = Fraction(0)
        self.selected_real_ids: set[int] = set()

    def _write(self, frame: TimedFrame, ideal: Fraction) -> None:
        self.emit(frame, self.next_index)
        self.max_error = max(self.max_error, abs(frame.timestamp - ideal))
        if frame.provenance == "DLSSG":
            self.generated += 1
        else:
            self.copied += 1
            if frame.source_index is not None:
                self.selected_real_ids.add(frame.source_index)
        self.next_index += 1

    def push(self, current: TimedFrame) -> None:
        for _ in self.iter_push(current):
            pass

    def iter_push(self, current: TimedFrame):
        """Emit lazily so long VFR holds cannot allocate an entire output burst."""
        if self.previous is None:
            self.previous = current
            return
        midpoint = (self.previous.timestamp + current.timestamp) / 2
        while self.next_index < self.output_count:
            ideal = Fraction(self.next_index, 1) / self.target_rate
            if ideal < midpoint:
                self._write(self.previous, ideal)
            elif ideal == midpoint:
                self._write(current if self.tie_late else self.previous, ideal)
                self.tie_late = not self.tie_late
            else:
                break
            yield None
        self.previous = current

    def finish(self) -> None:
        for _ in self.iter_finish():
            pass

    def iter_finish(self):
        if self.previous is None:
            raise ValueError("The input video contains no decodable frames.")
        while self.next_index < self.output_count:
            self._write(self.previous, Fraction(self.next_index, 1) / self.target_rate)
            yield None


def _duration_fraction(metadata: dict, source_rate: Fraction, frames: int, *, cfr: bool) -> Fraction:
    if cfr and frames > 0:
        return Fraction(frames, 1) / source_rate
    return Fraction(str(metadata.get("video_stream_duration") or metadata["duration"]))


def _validate(options: FrameInterpolationOptions) -> None:
    if not isinstance(options.optical_flow_quality, str) or options.optical_flow_quality not in OPTICAL_FLOW_QUALITIES:
        raise ValueError("Optical Flow Quality must be High, Medium, or Low.")
    _ = options.target_rate
    validate_rename(options.rename_mode, options.custom_suffix)
    ffmpeg.validate_codec_container(options.codec, options.container)
    if options.hdr_mode and not ffmpeg.hdr_mode_supported(options.codec):
        raise ValueError(f"HDR Mode is unavailable for {options.codec!r}.")
    if options.preview_seconds is not None and (
            not math.isfinite(float(options.preview_seconds)) or float(options.preview_seconds) <= 0):
        raise ValueError("Preview duration must be positive.")


def interpolate_video(
    input_path: str | os.PathLike[str], options: FrameInterpolationOptions | None = None,
    progress: Callable[[float, str], None] | None = None, *,
    output_dir: str | os.PathLike[str] | None = None, controller=None,
) -> FrameInterpolationResult:
    operation_started = time.perf_counter()
    options = replace(options) if options is not None else FrameInterpolationOptions()
    _validate(options)
    source = Path(input_path).resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    runtime_started = time.perf_counter()
    runtime = prepare_runtime()
    runtime_prepare_seconds = time.perf_counter() - runtime_started
    ai_gpu = resolve_runtime_ai_gpu(runtime.gpus, runtime.runtime_bundle, options.ai_gpu_uuid)
    capability_started = time.perf_counter()
    capabilities = probe_frame_interpolation_capabilities(options.ai_gpu_uuid)
    capability_probe_seconds = time.perf_counter() - capability_started
    if not capabilities.available:
        raise RuntimeError("Direct NVIDIA DLSS Frame Generation is unavailable. " + capabilities.detail)
    prepared_controller = getattr(_BATCH_CONTEXT, "controller", None)
    job_context = nullcontext(prepared_controller) if prepared_controller is not None else active_job(controller)
    with job_context as controller:
        assert controller is not None
        started = time.perf_counter()

        def update(value: float, message: str) -> None:
            if controller.cancel.is_set():
                raise Cancelled("Frame interpolation stopped by user.")
            if progress:
                value = max(0.0, min(1.0, float(value)))
                # Status bar shows only the main status; no ETA suffix.
                progress(value, message)

        output_file: OutputFile | None = None
        decoded_container: Any | None = None
        encoded_container: Any | None = None
        decode_thread: threading.Thread | None = None
        encode_thread: threading.Thread | None = None
        sessions: list[DirectDLSSGSession] = []
        transfer_pool: CudaTransferPool | None = None
        job: tempfile.TemporaryDirectory[str] | None = None
        stop, decode_stop = threading.Event(), threading.Event()
        failures: list[BaseException] = []
        timings: dict[str, float] = {
            "preflight_seconds": time.perf_counter() - operation_started,
            "runtime_prepare_seconds": runtime_prepare_seconds,
            "capability_probe_seconds": capability_probe_seconds,
            "session_initialization_seconds": 0.0,
            "probe_seconds": 0.0, "decode_seconds": 0.0, "software_prepare_seconds": 0.0,
            "bridge_input_seconds": 0.0, "optical_flow_seconds": 0.0,
            "dlssg_seconds": 0.0, "bridge_output_seconds": 0.0,
            "encode_seconds": 0.0, "cross_gpu_seconds": 0.0,
        }
        try:
            probe_start = time.perf_counter()
            metadata = ffmpeg.probe_video(
                source, count_mode="metadata", inspect_timestamps=True)
            source_rate = Fraction(metadata["rate"])
            cfr = bool(metadata.get("cfr", True))
            source_frames = int(metadata["frames"])
            if source_frames <= 0:
                source_frames = int(ffmpeg.probe_video(source, count_mode="exact")["frames"])
            duration = _duration_fraction(metadata, source_rate, source_frames, cfr=cfr)
            preview = options.preview_seconds is not None
            effective_hdr = bool(options.hdr_mode)
            if metadata["hdr"] and not effective_hdr:
                raise ValueError("HDR input requires Frame Interpolation HDR Mode with a 10-bit codec.")
            if preview:
                duration = min(duration, Fraction(str(options.preview_seconds)))
                source_frames = min(source_frames, ffmpeg.preview_frame_count(source, float(duration)))
                duration = min(duration, Fraction(source_frames, 1) / source_rate)
            plan = choose_interpolation_plan(
                source_rate, options.target_rate, options.engine,
                capabilities.native_multiplier, cfr=cfr)
            output_count = output_frame_count(duration, options.target_rate)
            timings["probe_seconds"] = time.perf_counter() - probe_start
            update(.01, "Preparing frame interpolation")

            selected_codec = options.codec
            video_gpu = ffmpeg.resolve_video_gpu(
                runtime.gpus, options.video_gpu_uuid, selected_codec,
                int(metadata["width"]), int(metadata["height"]))
            if _is_nvenc(selected_codec) and video_gpu is None:
                raise RuntimeError("The selected NVENC codec has no available encoding GPU.")
            ai_ordinal = int(ai_gpu["cuda_ordinal"])
            encode_ordinal = int(video_gpu["cuda_ordinal"]) if video_gpu is not None else ai_ordinal
            # The processing path is automatic, matching Upscale: an NVENC
            # output keeps decode/interpolation/encode on CUDA, while a CPU
            # codec uses the in-process host-staging ABI.
            want_cuda = automatic_cuda_path(selected_codec, metadata["rotation"])
            decode_device = HWAccel(
                "cuda", device=str(ai_ordinal), allow_software_fallback=False,
                options={"primary_ctx": "1"}, is_hw_owned=True) if want_cuda else None
            rotation_filter = {90: "transpose=clock", 180: "hflip,vflip", 270: "transpose=cclock"}.get(int(metadata["rotation"]), "")
            tick = time.perf_counter()
            try:
                decoded_container, decoder, first_frame, decode_backend = _open_frame_decoder(
                    source, controller, decode_device=decode_device, hdr=effective_hdr,
                    rotation_filter=rotation_filter)
            except StopIteration as exc:
                raise ValueError("The input contains no decodable video frames.") from exc
            timings["decode_seconds"] += time.perf_counter() - tick
            metadata["rotation"] = 0
            cuda_route = bool(want_cuda and first_frame.format.name == "cuda")
            output_p010 = bool(effective_hdr)
            encoder = _encoder(options, selected_codec, int(metadata["width"]),
                               int(metadata["height"]), options.target_rate,
                               effective_hdr, cuda_route, encode_ordinal)

            destination = prepare_output_dir(output_dir, default=OUTPUTS)
            stamp = time.strftime("%Y%m%d-%H%M%S") + f"-{time.time_ns()%1_000_000:06d}"
            extension = {"MP4": ".mp4", "MKV": ".mkv", "MOV": ".mov"}[options.container]
            output = unique_output_path(destination / output_filename(
                source, extension, options.rename_mode, options.custom_suffix,
                f"{source.stem}_{'DLSSFG_PREVIEW' if preview else 'DLSSFG'}_{stamp}"))
            output_file = OutputFile(output)
            JOBS.mkdir(exist_ok=True)
            job = tempfile.TemporaryDirectory(prefix="dlssg-bridge-", dir=JOBS)
            try:
                temp_video = Path(job.name) / ("encoded.mov" if options.container == "MOV" else
                                               "encoded.mkv" if options.container == "MKV" else "encoded.mp4")
                encoded_container = (av.open(str(temp_video), mode="w") if cuda_route
                                     else VideoOutput(temp_video, controller))
                encode_device = HWAccel(
                    "cuda", device=str(encode_ordinal), options={"primary_ctx": "1"},
                    is_hw_owned=True) if cuda_route else None
                output_stream = encoded_container.add_stream(
                    encoder.name, rate=options.target_rate, hwaccel=encode_device)
                output_stream.width, output_stream.height = int(metadata["width"]), int(metadata["height"])
                output_stream.pix_fmt = encoder.pixel_format
                if cuda_route:
                    output_stream.codec_context.sw_format = "p010le" if output_p010 else "nv12"
                output_tb = Fraction(1, 1) / options.target_rate
                output_stream.time_base = output_tb
                output_stream.codec_context.time_base = output_tb
                output_stream.codec_context.options = encoder.options
                output_stream.codec_context.bit_rate = encoder.bitrate
                if encoder.display == "prores_ks (Proxy)" and options.quality == "Auto (Default)":
                    # Proxy's default quantizer remains visibly softer even with
                    # the higher macroblock budget. Match its high-quality range.
                    output_stream.codec_context.qscale = True
                    output_stream.codec_context.global_quality = 3
                _set_color_properties(output_stream.codec_context, metadata, effective_hdr)
                if encoder.name == "prores_ks":
                    # Generated RGB is full range, but ProRes delivery must
                    # convert to limited YUV even when the source was full range.
                    output_stream.codec_context.color_range = 1
                if encoder.name == "ffv1":
                    output_stream.codec_context.colorspace = 0  # RGB, no YUV matrix.
                    output_stream.codec_context.color_range = 2  # Full range.
                output_stream.codec_context.open()

                generated_count = plan.generated_per_interval if plan.path == "Native DLSSG" else 1
                session_count = 1 if plan.path == "Native DLSSG" else plan.cascade_stages
                initialization_start = time.perf_counter()
                for _ in range(session_count):
                    sessions.append(DirectDLSSGSession(
                        int(metadata["width"]), int(metadata["height"]), generated_count,
                        controller, ai_ordinal, hdr=effective_hdr,
                        chroma_location=metadata.get("chroma_location", "left"),
                        optical_flow_quality=options.optical_flow_quality))
                timings["session_initialization_seconds"] = time.perf_counter() - initialization_start
                colors = {"color_matrix": _matrix_code(metadata, hdr=effective_hdr),
                          "color_range": _range_code(metadata),
                          "color_primaries": _primaries_code(metadata),
                          "color_transfer": _transfer_code(metadata),
                          "rotation": int(metadata["rotation"])}
                stages = [DLSSGStage(
                    session, generated_count, detect_source_cuts=index == 0,
                    cuda_output=cuda_route, output_p010=output_p010, colors=colors,
                    preserve_source_rgb=index == 0)
                    for index, session in enumerate(sessions)]
                if cuda_route and encode_ordinal != ai_ordinal:
                    transfer_pool = CudaTransferPool(ai_ordinal, encode_ordinal, controller)

                decode_queue: queue.Queue = queue.Queue(maxsize=4)
                encode_queue: queue.Queue = queue.Queue(maxsize=4)

                def decode_worker() -> None:
                    try:
                        _put(decode_queue, first_frame, stop, controller, failures)
                        while not stop.is_set() and not decode_stop.is_set():
                            frame_start = time.perf_counter()
                            try:
                                frame = next(decoder)
                            except StopIteration:
                                timings["decode_seconds"] += time.perf_counter() - frame_start
                                break
                            timings["decode_seconds"] += time.perf_counter() - frame_start
                            _validate_decoded_frame(frame)
                            _put(decode_queue, frame, stop, controller, failures)
                    except BaseException as exc:
                        failures.append(exc); stop.set()
                    finally:
                        if not decode_stop.is_set() and not stop.is_set():
                            with suppress(BaseException):
                                _put(decode_queue, _END, stop, controller, failures)

                encoded_frames = [0]

                def encode_worker() -> None:
                    try:
                        while not stop.is_set():
                            item = _get(encode_queue, stop, controller, failures)
                            if item is _END:
                                break
                            frame, pts = item
                            encode_start = time.perf_counter()
                            frame.pts, frame.time_base, frame.duration = pts, output_tb, 1
                            for packet in output_stream.encode(frame):
                                encoded_container.mux(packet)
                            timings["encode_seconds"] += time.perf_counter() - encode_start
                            encoded_frames[0] += 1
                            del frame, item
                        if not failures and not controller.cancel.is_set():
                            encode_start = time.perf_counter()
                            for packet in output_stream.encode():
                                encoded_container.mux(packet)
                            timings["encode_seconds"] += time.perf_counter() - encode_start
                    except BaseException as exc:
                        failures.append(exc); stop.set()

                decode_thread = threading.Thread(target=decode_worker, name="dlssg-decoder", daemon=True)
                encode_thread = threading.Thread(target=encode_worker, name="dlssg-encoder", daemon=True)
                decode_thread.start(); encode_thread.start()
                pipeline_start = time.perf_counter()
                decoded = discontinuities = 0
                last_timestamp: Fraction | None = None
                origin_timestamp: Fraction | None = None
                source_segment = 0
                nominal = Fraction(1, 1) / source_rate
                source_dimensions = (int(first_frame.width), int(first_frame.height))

                def emit(selected: TimedFrame, index: int) -> None:
                    frame = selected.encode_frame
                    if isinstance(frame, DLSSGCudaSurface):
                        convert_start = time.perf_counter()
                        if encoder.name == "prores_ks" or not cuda_route:
                            if effective_hdr:
                                # Pack transfer-encoded HDR RGB to physical G,B,R planes
                                # on CUDA, retaining exact round-to-even 10-bit precision.
                                frame = frame.to_host_gbrp10_frame()
                            else:
                                pixels = frame.to_host_rgb()
                                frame = av.VideoFrame.from_ndarray(pixels, format="rgba")
                        else:
                            yuv = frame.to_yuv_surface(
                                color_matrix=colors["color_matrix"],
                                color_range=colors["color_range"], p010=output_p010)
                            frame = yuv.to_av_frame()
                        timings["final_color_conversion_seconds"] = (
                            timings.get("final_color_conversion_seconds", 0.0) +
                            time.perf_counter() - convert_start)
                    if transfer_pool is not None:
                        transfer_start = time.perf_counter()
                        frame, _detail = transfer_pool.transfer(frame)
                        timings["cross_gpu_seconds"] += time.perf_counter() - transfer_start
                    _put(encode_queue, (frame, index), stop, controller, failures)

                writer = NearestTimestampWriter(options.target_rate, output_count, emit)
                last_update = 0.0
                while decoded < source_frames:
                    frame = _get(decode_queue, stop, controller, failures)
                    if frame is _END:
                        break
                    if (int(frame.width), int(frame.height)) != source_dimensions:
                        raise ValueError("Source dimensions changed during decoding.")
                    raw_timestamp = (Fraction(frame.pts) * Fraction(frame.time_base)
                                     if frame.pts is not None and frame.time_base is not None
                                     else Fraction(decoded, 1) / source_rate)
                    if origin_timestamp is None:
                        origin_timestamp = raw_timestamp
                    timestamp = raw_timestamp - origin_timestamp
                    if last_timestamp is not None and (
                            timestamp <= last_timestamp or timestamp - last_timestamp > nominal * 2):
                        source_segment += 1
                        discontinuities += 1
                    last_timestamp = timestamp
                    if cuda_route:
                        bridge_frame, encode_frame = frame, frame
                    else:
                        prepare_start = time.perf_counter()
                        if effective_hdr:
                            bridge_frame = encode_frame = frame
                        else:
                            rgba = np.ascontiguousarray(frame.to_ndarray(format="rgba"))
                            bridge_frame, encode_frame = rgba, frame
                        timings["software_prepare_seconds"] += time.perf_counter() - prepare_start
                    items = [TimedFrame(bridge_frame, encode_frame, timestamp, source_segment,
                                        "Source", decoded)]
                    for stage in stages:
                        next_items: list[TimedFrame] = []
                        for item in items:
                            native_start = time.perf_counter()
                            produced = stage.push(item)
                            native_elapsed = time.perf_counter() - native_start
                            next_items.extend(produced)
                            timings["bridge_total_seconds"] = timings.get("bridge_total_seconds", 0.0) + native_elapsed
                        items = next_items
                    for item in items:
                        writer.push(item)
                    decoded += 1
                    del frame, items
                    now = time.perf_counter()
                    if now - last_update > .2:
                        update(.04 + .82 * decoded / max(1, source_frames),
                               f"Interpolating frames: {decoded:,} / {source_frames:,} frames")
                        last_update = now
                if decoded != source_frames:
                    raise RuntimeError(f"Decoded {decoded} source frames; expected {source_frames}.")
                writer.finish()
                _put(encode_queue, _END, threading.Event(), controller, failures)
                decode_stop.set()
                # A truncated preview intentionally stops before decoder EOF.
                # Free one or more queue slots so a producer already blocked in
                # _put can publish its current frame, observe decode_stop, and exit.
                while True:
                    try:
                        decode_queue.get_nowait()
                    except queue.Empty:
                        break
                decode_thread.join(timeout=30); encode_thread.join(timeout=300)
                if decode_thread.is_alive() or encode_thread.is_alive():
                    raise RuntimeError("The bounded Frame Interpolation pipeline did not drain.")
                if failures:
                    raise failures[0]
                if encoded_frames[0] != output_count:
                    raise RuntimeError(f"Encoder accepted {encoded_frames[0]} frames; expected {output_count}.")
                timings["pipeline_seconds"] = time.perf_counter() - pipeline_start
                actual_encoder = (encoder.name if cuda_route else encoded_container.actual_encoder)
                encoded_container.close(); encoded_container = None
                decoded_container.close(); decoded_container = None
                gc.collect()

                session_diagnostics = [session.diagnostics() for session in sessions]
                for session in sessions:
                    timings["bridge_input_seconds"] += session.stage_timings["input_ms"] / 1000
                    timings["optical_flow_seconds"] += session.stage_timings["optical_flow_ms"] / 1000
                    timings["dlssg_seconds"] += session.stage_timings["ngx_ms"] / 1000
                    timings["bridge_output_seconds"] += session.stage_timings["output_ms"] / 1000
                    session.close()
                sessions.clear()
                transfer_diagnostics = transfer_pool.diagnostics() if transfer_pool else {}
                if transfer_pool:
                    transfer_pool.close(); transfer_pool = None
                update(.9, "Muxing original audio, subtitles, chapters, and metadata")
                mux_start = time.perf_counter()
                audio_diagnostics: dict = {}
                ffmpeg.final_mux(
                    temp_video, source, output_file.temporary, options.container, controller,
                    preserve_supported_subtitles=True,
                    source_time_origin=metadata.get("origin", 0),
                    audio_diagnostics=audio_diagnostics)
                timings["mux_seconds"] = time.perf_counter() - mux_start
                update(.96, "Verifying exact frame count, rational FPS, dimensions, and HDR")
                verify_start = time.perf_counter()
                verified = ffmpeg.probe_video(output_file.temporary, count_mode="packets", controller=controller)
                if int(verified["frames"]) != output_count:
                    verified = ffmpeg.probe_video(output_file.temporary, count_mode="exact", controller=controller)
                if int(verified["frames"]) != output_count or Fraction(verified["rate"]) != options.target_rate:
                    raise RuntimeError(
                        f"Output verification found {verified['frames']} frames at {verified['rate']} FPS; "
                        f"expected {output_count} at {options.target_rate}.")
                if (int(verified["width"]), int(verified["height"])) != (
                        int(metadata["width"]), int(metadata["height"])):
                    raise RuntimeError("Output dimensions changed during interpolation.")
                if effective_hdr and (not verified.get("hdr") or int(verified.get("depth", 0)) < 10):
                    raise RuntimeError("Saved interpolation failed HDR/10-bit verification.")
                expected = {
                    "ProRes HQ": ("prores", "yuv422p10le", 10),
                    "FFV1 Lossless RGB 10-bit": ("ffv1", "gbrp10le", 10),
                }.get(ffmpeg._normalize_codec(selected_codec))
                if expected and (verified.get("codec"), verified.get("pixel_format"),
                                 int(verified.get("depth") or 0)) != expected:
                    raise RuntimeError(f"Saved interpolation format does not match {selected_codec}.")
                timings["verification_seconds"] = time.perf_counter() - verify_start
                elapsed = time.perf_counter() - started
                timings["total_seconds"] = elapsed
                timings["steady_state_fps"] = output_count / max(timings["pipeline_seconds"], 1e-9)
                stage_rates = [
                    output_count / max(timings["decode_seconds"], 1e-9),
                    output_count / max(timings.get("bridge_total_seconds", 0.0), 1e-9),
                    output_count / max(timings["encode_seconds"], 1e-9),
                ]
                timings["slowest_stage_ceiling_fps"] = min(stage_rates)
                timings["ceiling_utilization"] = timings["steady_state_fps"] / max(min(stage_rates), 1e-9)
                upload_bytes = sum(int(item.get("upload_bytes", 0)) for item in session_diagnostics)
                download_bytes = sum(int(item.get("download_bytes", 0)) for item in session_diagnostics)
                if transfer_diagnostics:
                    upload_bytes += int(transfer_diagnostics.get("pinned_upload_bytes", 0))
                    download_bytes += int(transfer_diagnostics.get("pinned_download_bytes", 0))
                if cuda_route and encode_ordinal == ai_ordinal:
                    memory_path = "cuda_nvdec_shared_d3d12_dlssg_dlpack_nvenc"
                    if upload_bytes or download_bytes:
                        raise RuntimeError("Same-GPU CUDA route unexpectedly transferred full-frame host pixels.")
                elif cuda_route:
                    memory_path = str(transfer_diagnostics.get("memory_path") or "cuda_cross_gpu")
                else:
                    memory_path = "host_staging_inprocess_dlssg_encoder"
                pool_pressure = {
                    "capacity": max((int(item.get("surface_pool_capacity", 0))
                                     for item in session_diagnostics), default=0),
                    "allocated": max((int(item.get("surface_pool_allocated", 0))
                                      for item in session_diagnostics), default=0),
                    "waits": sum(int(item.get("surface_pool_waits", 0))
                                 for item in session_diagnostics),
                }
                scene_cuts = stages[0].scene_cuts if stages else 0
                duplicate_intervals = sum(stage.duplicates for stage in stages)
                # A target cadence such as 24 -> 60 does not necessarily land
                # on every real source timestamp; those frames are scheduling
                # alternatives, not decoder drops. Exact decoded/encoded counts
                # have already been asserted above, so the pipeline dropped none.
                unselected_source_frames = max(0, decoded - len(writer.selected_real_ids))
                dropped = 0
                diagnostics = {
                    "bridge": session_diagnostics, "cross_gpu_transfer": transfer_diagnostics,
                    "adapters": {"ai": ai_gpu, "decode": ai_gpu if cuda_route else "CPU",
                                 "encode": video_gpu if _is_nvenc(selected_codec) else "CPU"},
                    "discontinuities": discontinuities, "duplicate_intervals": duplicate_intervals,
                    "unselected_source_frames": unselected_source_frames,
                    "audio_streams": audio_diagnostics.get("streams", []),
                }
                output_file.publish()
                app_log.info("frame-interp", f"done src={source.name} out={output.name} "
                             f"frames={output_count} elapsed={elapsed:.2f}s memory={memory_path}")
                update(1.0, "Frame interpolation complete — in-process GPU pipeline confirmed")
                return FrameInterpolationResult(
                    output_path=str(output.resolve()), report_path=str(app_log.session_path()),
                    selected_path=plan.path, native_multiplier=plan.native_multiplier,
                    cascade_stages=plan.cascade_stages, copied_frames=writer.copied,
                    generated_frames=writer.generated, dropped_frames=dropped,
                    output_frames=output_count,
                    maximum_temporal_approximation_seconds=float(writer.max_error),
                    scene_cuts=scene_cuts, elapsed_seconds=elapsed, timings=timings,
                    bridge_version=capabilities.bridge_version,
                    bridge_abi_version=capabilities.bridge_abi_version,
                    nvof_available=capabilities.nvof_available, memory_path=memory_path,
                    decode_backend=decode_backend, encode_backend=actual_encoder,
                    upload_bytes=upload_bytes, download_bytes=download_bytes,
                    surface_pool_pressure=pool_pressure, diagnostics=diagnostics)
            finally:
                # On a failure the output container may still own the temporary
                # file. The outer handler closes it, then performs a second,
                # reliable cleanup attempt without masking the original error.
                with suppress(PermissionError):
                    job.cleanup()
        except BaseException as exc:
            stop.set(); decode_stop.set()
            for thread in (decode_thread, encode_thread):
                if thread is not None:
                    thread.join(timeout=2)
            for session in sessions:
                with suppress(Exception):
                    session.close(abort=True)
            if transfer_pool is not None:
                with suppress(Exception):
                    transfer_pool.close(abort=True)
            if encoded_container is not None:
                with suppress(Exception):
                    encoded_container.close()
            if decoded_container is not None:
                with suppress(Exception):
                    decoded_container.close()
            if job is not None:
                with suppress(Exception):
                    job.cleanup()
            if output_file is not None:
                output_file.cleanup(rollback=True)
            if controller.cancel.is_set() or isinstance(exc, Cancelled):
                raise Cancelled("Frame interpolation stopped by user.") from exc
            failure_path = app_log.fail("frame-interp", f"DLSSFG_{source.stem}", exc)
            raise RuntimeError(f"{exc}\nDetails: {failure_path.resolve()}") from exc
        finally:
            if output_file is not None:
                output_file.cleanup()
