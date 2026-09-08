from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from types import ModuleType
from typing import Any, Iterator

from ...core.gpu_selection import detect_gpu
from ...core.paths import FFMPEG

CUDA_DECODE_ENV = "DLSS5_CUDA_DECODE"
CUDA_DECODE_CHOICES = {"auto", "on", "off"}

# Prefer a fully GPU-side YUV->RGBA conversion, then fall back to hardware
# decode + hardware download + CPU colorspace conversion for common formats.
_FILTER_CANDIDATES = (
    "scale_cuda=w=iw:h=ih:format=rgba:passthrough=0,hwdownload,format=rgba",
    "hwdownload,format=nv12,format=rgba",
    "hwdownload,format=p010le,format=rgba",
    "hwdownload,format=yuv444p,format=rgba",
    "hwdownload,format=yuv444p16le,format=rgba",
)

_LOCAL = threading.local()


def cuda_decode_mode() -> str:
    raw = os.environ.get(CUDA_DECODE_ENV, "auto").strip().lower()
    return raw if raw in CUDA_DECODE_CHOICES else "auto"


@dataclass
class DecodeContext:
    source: Path
    mode: str
    enabled: bool
    required: bool
    gpu: dict[str, Any] | None = None
    backend: str = "pyav-software"
    filter_graph: str | None = None
    fallback_reason: str | None = None
    frames: int = 0
    started: float = field(default_factory=time.perf_counter)
    decoder_seconds: float = 0.0

    def report(self) -> dict[str, Any]:
        gpu = self.gpu or {}
        return {
            "mode": self.mode,
            "enabled": self.enabled,
            "backend": self.backend,
            "filter_graph": self.filter_graph,
            "fallback_reason": self.fallback_reason,
            "frames": self.frames,
            "gpu": gpu.get("name"),
            "gpu_uuid": gpu.get("uuid"),
            "cuda_ordinal": gpu.get("cuda_ordinal"),
            "cuda_identity_verified": bool(gpu.get("cuda_identity_verified", False)),
            "decoder_lifetime_seconds": self.decoder_seconds,
        }


class _StreamProxy:
    def __init__(self, average_rate: Any, time_base: Any) -> None:
        self.average_rate = average_rate
        self.time_base = time_base
        self.thread_type = "AUTO"


class _StreamsProxy:
    def __init__(self, stream: _StreamProxy) -> None:
        self.video = [stream]


class _CudaDecodeContainer:
    """PyAV-like read container backed by FFmpeg CUDA/NVDEC.

    FFmpeg decodes into CUDA frames. The first filter candidate also performs
    color conversion on CUDA before downloading RGBA. Fallback candidates keep
    decode on the GPU and download a native YUV format before converting to RGBA
    on the CPU. Frames are wrapped in NUT so PTS survive the pipe.
    """

    def __init__(
        self,
        source: Path,
        gpu: dict[str, Any],
        real_av: ModuleType,
        context: DecodeContext,
    ) -> None:
        ordinal = gpu.get("cuda_ordinal")
        if ordinal is None or not gpu.get("cuda_identity_verified"):
            raise RuntimeError("The selected GPU has no verified CUDA ordinal.")
        self._source = source
        self._real_av = real_av
        self._context = context
        self._process: subprocess.Popen[bytes] | None = None
        self._inner = None
        self._inner_stream = None
        self._iterator: Iterator[Any] | None = None
        self._first = None
        self._logs: deque[str] = deque(maxlen=80)
        self._log_thread: threading.Thread | None = None
        self._closed = False
        self._start_time = time.perf_counter()

        metadata_container = real_av.open(str(source))
        try:
            source_stream = metadata_container.streams.video[0]
            source_rate = source_stream.average_rate or Fraction(30, 1)
            source_time_base = source_stream.time_base or Fraction(1, 1000)
        finally:
            metadata_container.close()

        self._source_time_base = Fraction(source_time_base)
        self._stream = _StreamProxy(source_rate, self._source_time_base)
        self.streams = _StreamsProxy(self._stream)

        failures: list[str] = []
        for filter_graph in _FILTER_CANDIDATES:
            try:
                self._start_candidate(int(ordinal), filter_graph)
                context.backend = "ffmpeg-cuda-nvdec"
                context.filter_graph = filter_graph
                context.fallback_reason = None
                return
            except Exception as exc:
                failures.append(f"{filter_graph}: {exc}")
                self._teardown_candidate()
        raise RuntimeError(
            "FFmpeg CUDA decode could not initialize on the selected GPU. "
            + " | ".join(failures[-3:])
        )

    def _command(self, ordinal: int, filter_graph: str) -> list[str]:
        return [
            str(FFMPEG),
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-hwaccel",
            "cuda",
            "-hwaccel_device",
            str(ordinal),
            "-hwaccel_output_format",
            "cuda",
            "-extra_hw_frames",
            "8",
            "-noautorotate",
            "-copyts",
            "-i",
            str(self._source),
            "-map",
            "0:v:0",
            "-an",
            "-sn",
            "-dn",
            "-vf",
            filter_graph,
            "-fps_mode",
            "passthrough",
            "-c:v",
            "rawvideo",
            "-pix_fmt",
            "rgba",
            "-f",
            "nut",
            "pipe:1",
        ]

    def _read_logs(self) -> None:
        process = self._process
        if process is None or process.stderr is None:
            return
        for raw in iter(process.stderr.readline, b""):
            text = raw.decode("utf-8", "replace").rstrip()
            if text:
                self._logs.append(text)

    def _start_candidate(self, ordinal: int, filter_graph: str) -> None:
        self._process = subprocess.Popen(
            self._command(ordinal, filter_graph),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        assert self._process.stdout is not None
        self._log_thread = threading.Thread(
            target=self._read_logs, name="dlss5-cuda-decode-log", daemon=True
        )
        self._log_thread.start()
        self._inner = self._real_av.open(self._process.stdout, mode="r", format="nut")
        self._inner_stream = self._inner.streams.video[0]
        self._iterator = iter(self._inner.decode(self._inner_stream))
        try:
            first = next(self._iterator)
        except StopIteration as exc:
            code = self._process.wait(timeout=5)
            raise RuntimeError(
                f"decoder returned no frame (exit {code}): " + "\n".join(self._logs)[-2000:]
            ) from exc
        self._first = self._rescale_pts(first)

    def _rescale_pts(self, frame):
        if frame.pts is None:
            return frame
        frame_tb = frame.time_base or getattr(self._inner_stream, "time_base", None)
        if frame_tb is None:
            return frame
        target = Fraction(frame.pts) * Fraction(frame_tb) / self._source_time_base
        frame.pts = int(round(float(target)))
        return frame

    def decode(self, _stream=None):
        if self._closed:
            raise RuntimeError("CUDA decoder container is closed.")
        if self._first is not None:
            first, self._first = self._first, None
            self._context.frames += 1
            yield first
        assert self._iterator is not None
        for frame in self._iterator:
            self._context.frames += 1
            yield self._rescale_pts(frame)
        process = self._process
        if process is not None:
            code = process.wait(timeout=10)
            if code:
                if self._log_thread is not None:
                    self._log_thread.join(timeout=1)
                raise RuntimeError(
                    f"FFmpeg CUDA decoder exited with {code}: "
                    + "\n".join(self._logs)[-3000:]
                )

    def _teardown_candidate(self) -> None:
        inner, self._inner = self._inner, None
        if inner is not None:
            try:
                inner.close()
            except Exception:
                pass
        process, self._process = self._process, None
        if process is not None:
            if process.poll() is None:
                try:
                    process.terminate()
                    process.wait(timeout=3)
                except Exception:
                    try:
                        process.kill()
                        process.wait(timeout=3)
                    except Exception:
                        pass
            for stream in (process.stdout, process.stderr):
                if stream is not None and not stream.closed:
                    try:
                        stream.close()
                    except OSError:
                        pass
        if self._log_thread is not None:
            self._log_thread.join(timeout=1)
            self._log_thread = None
        self._inner_stream = None
        self._iterator = None
        self._first = None

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._teardown_candidate()
        self._context.decoder_seconds = time.perf_counter() - self._start_time


class _AVProxy:
    def __init__(self, real_av: ModuleType) -> None:
        self._real = real_av

    def __getattr__(self, name: str):
        return getattr(self._real, name)

    def open(self, file, *args, **kwargs):
        context: DecodeContext | None = getattr(_LOCAL, "context", None)
        mode = kwargs.get("mode")
        if mode is None and args and isinstance(args[0], str) and args[0] in {"r", "w"}:
            mode = args[0]
        reading = mode in (None, "r")
        if (
            context is not None
            and context.enabled
            and reading
            and isinstance(file, (str, os.PathLike))
        ):
            try:
                candidate = Path(file).resolve()
            except Exception:
                candidate = None
            if candidate == context.source:
                try:
                    return _CudaDecodeContainer(
                        context.source, context.gpu or {}, self._real, context
                    )
                except Exception as exc:
                    if context.required:
                        raise
                    context.backend = "pyav-software"
                    context.fallback_reason = str(exc)
        return self._real.open(file, *args, **kwargs)


def _make_context(input_path, options) -> DecodeContext:
    mode = cuda_decode_mode()
    source = Path(input_path).resolve()
    required = mode == "on"
    context = DecodeContext(source=source, mode=mode, enabled=False, required=required)
    if mode == "off":
        context.fallback_reason = "disabled by DLSS5_CUDA_DECODE=off"
        return context
    if os.name != "nt":
        context.fallback_reason = "CUDA decode is enabled only for the Windows portable runtime."
        if required:
            raise RuntimeError(context.fallback_reason)
        return context
    try:
        gpu = detect_gpu(
            getattr(options, "ai_gpu_uuid", "auto") if options is not None else "auto"
        )
    except Exception as exc:
        if required:
            raise
        context.fallback_reason = f"GPU selection failed: {exc}"
        return context
    context.gpu = gpu
    if gpu.get("cuda_ordinal") is None or not gpu.get("cuda_identity_verified"):
        context.fallback_reason = "The selected AI GPU has no verified PCI-to-CUDA mapping."
        if required:
            raise RuntimeError(context.fallback_reason)
        return context
    context.enabled = True
    return context


def _augment_report(result, context: DecodeContext) -> None:
    try:
        path = Path(result.report_path)
        report = json.loads(path.read_text(encoding="utf-8"))
        report["cuda_decode"] = context.report()

        frames = max(0, int(report.get("frames_processed") or 0))
        timings = report.get("timings") or {}
        dlss_seconds = float(timings.get("dlss_seconds") or 0.0)
        elapsed = float(report.get("elapsed_seconds") or 0.0)
        output = report.get("output_dimensions") or {}
        output_bytes = (
            int(output.get("width") or 0)
            * int(output.get("height") or 0)
            * 4
            * frames
        )
        report["performance_analysis"] = {
            "dlss_feature18_seconds": dlss_seconds,
            "dlss_feature18_ms_per_frame": (
                1000.0 * dlss_seconds / frames if frames else 0.0
            ),
            "dlss_feature18_share_of_elapsed": (
                dlss_seconds / elapsed if elapsed > 0 else 0.0
            ),
            "rgba_output_readback_gib": output_bytes / (1024 ** 3),
            "note": (
                "FFmpeg CUDA accelerates decode and compatible conversion stages. "
                "Feature-18 evaluation and RGBA readback remain a synchronous native "
                "worker stage and cannot be replaced by FFmpeg."
            ),
        }
        path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    except Exception:
        # Diagnostics must never turn a successful render into a failure.
        pass


def install_cuda_decode(processor_module) -> None:
    """Install an idempotent CUDA-decode wrapper around the video processor."""
    if getattr(processor_module, "_dlss5_cuda_decode_installed", False):
        return
    original = processor_module.convert_video
    real_av = processor_module.av
    processor_module.av = _AVProxy(real_av)

    def convert_video_with_cuda_decode(input_path, options=None, *args, **kwargs):
        context = _make_context(input_path, options)
        previous = getattr(_LOCAL, "context", None)
        _LOCAL.context = context
        result = None
        try:
            result = original(input_path, options, *args, **kwargs)
            return result
        finally:
            if result is not None:
                _augment_report(result, context)
            if previous is None:
                try:
                    delattr(_LOCAL, "context")
                except AttributeError:
                    pass
            else:
                _LOCAL.context = previous

    processor_module.convert_video = convert_video_with_cuda_decode
    processor_module._dlss5_cuda_decode_installed = True
