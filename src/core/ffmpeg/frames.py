"""Timestamped FFmpeg Vulkan codecs at host frame boundaries.

The bundled FFmpeg has Vulkan Video support that PyAV's libraries do not.
PyAV here only demuxes/muxes raw NUT packets; it never encodes compressed video.
"""
from __future__ import annotations

import math
import subprocess
import threading
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace

import av
import numpy as np

from ..jobs import BoundedLogBuffer, Cancelled, drain_bounded_text
from ..paths import FFMPEG
from .nut import RawVideoPacketMuxer
from .vulkan import prepare_command, stream_info


class _Reader:
    def __init__(self, pipe):
        self.pipe = pipe

    def read(self, size):
        return self.pipe.read(size)


class VideoDecoder:
    def __init__(self, source, controller, *, pixel_format: str | None = None,
                 selection=None, video_filter="", cwd=None, start_seconds: float = 0.0):
        self.controller, self.container = controller, None
        if not math.isfinite(start_seconds) or start_seconds < 0:
            raise ValueError("Decoder start time must be positive and finite.")
        aligned = max(0.0, start_seconds - .002)
        fast = max(0.0, aligned - 2.0)
        accurate = max(0.0, aligned - fast)
        info = stream_info(source)
        if pixel_format is None:
            source_format = info.get("pix_fmt", "yuv420p")
            # Keep the original layout when Vulkan can represent it. Conversion
            # to RGB/YUV required by a consumer is an explicit GPU operation.
            pixel_format = source_format if source_format in {"yuv420p", "yuv420p10le", "yuv422p10le", "gbrp10le", "rgba", "rgba64le", "nv12", "p010le"} else "rgba64le"
        # Retain the source matrix/transfer when a YUV host boundary is needed.
        # Otherwise the planner's generic BT.709 default changes tagged HDR
        # pixels while the AI consumer still interprets them as BT.2020.
        color_options = []
        for key, flag in (("color_space", "-colorspace"), ("color_primaries", "-color_primaries"),
                          ("color_transfer", "-color_trc"), ("color_range", "-color_range")):
            value = info.get(key)
            if key == "color_range" and av.VideoFormat(pixel_format).is_rgb:
                value = "pc"
            if value and value not in {"unknown", "unspecified"}:
                color_options.extend((flag, value))
        command = [str(FFMPEG), "-v", "warning", "-xerror", "-copyts", "-noautorotate",
                   *(["-ss", f"{fast:.6f}"] if start_seconds > 0 else []),
                   "-i", str(source),
                   *(["-ss", f"{accurate:.6f}"] if start_seconds > 0 else []),
                   "-map", "0:v:0", "-an", "-sn", "-dn", "-c:v", "rawvideo",
                   *(["-vf", video_filter] if video_filter else []),
                   "-pix_fmt", pixel_format, *color_options, "-fps_mode", "passthrough", "-enc_time_base", "demux",
                   "-f", "nut", "-write_index", "0", "pipe:1"]
        command = prepare_command(command, selection=selection or getattr(controller, "ffmpeg_device", None))
        self.logs = BoundedLogBuffer(max_tail=60)
        self.process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, cwd=cwd,
                                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        controller.register(self.process)
        self.thread = threading.Thread(target=drain_bounded_text, args=(self.process.stderr, self.logs), daemon=True)
        self.thread.start()
        try:
            self.container = av.open(_Reader(self.process.stdout), format="nut")
            # Raw NUT pixel-format tags differ between the CLI and PyAV's
            # FFmpeg builds (P010 can be identified as RGB555). The producer
            # explicitly writes this layout; decode its packets in that exact
            # format instead of interpreting 10-bit luma as packed RGB.
            stream = self.container.streams.video[0]
            if stream.codec_context.name != "rawvideo":
                raise ValueError("The frame decoder did not return raw video.")
            stream.codec_context.codec_tag = "\0\0\0\0"
            stream.codec_context.pix_fmt = pixel_format
        except BaseException:
            self.close()
            raise RuntimeError("FFmpeg decoder failed:\n" + "\n".join(self.logs.snapshot()))

    @property
    def streams(self):
        return self.container.streams

    def decode(self, *args, **kwargs):
        for frame in self.container.decode(*args, **kwargs):
            if self.controller.cancel.is_set():
                raise Cancelled("Render stopped by user.")
            yield frame
        self.process.wait(timeout=10)
        self.thread.join(timeout=2)
        if self.process.returncode:
            raise RuntimeError("FFmpeg decoder failed:\n" + "\n".join(self.logs.snapshot()))

    def close(self):
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait(timeout=5)
        if self.container:
            self.container.close()
            self.container = None
        self.thread.join(timeout=2)
        self.controller.unregister(self.process)
        self.process.stdout.close()
        self.process.stderr.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def open_video_decoder(source, controller, *, pixel_format=None, selection=None,
                       video_filter="", cwd=None, start_seconds=0.0):
    return VideoDecoder(source, controller, pixel_format=pixel_format,
                        selection=selection, video_filter=video_filter, cwd=cwd,
                        start_seconds=start_seconds)


def packed_frame(frame) -> np.ndarray:
    """Copy logical rows, omitting padding; no pixel conversion takes place."""
    rows = []
    for index, plane in enumerate(frame.planes):
        components = [c for c in frame.format.components if c.plane == index]
        row_bytes = sum(c.width * ((c.bits + 7) // 8) for c in components)
        if not components or row_bytes > plane.line_size:
            raise ValueError(f"Unsupported raw frame layout: {frame.format.name}.")
        rows.append(np.ascontiguousarray(np.frombuffer(plane, dtype=np.uint8)
                                         .reshape(plane.height, plane.line_size)[:, :row_bytes]).reshape(-1))
    return np.concatenate(rows)


class _CodecContext(SimpleNamespace):
    def open(self):
        pass  # The CLI encoder is initialized with the first frame's layout.


class _OutputStream:
    def __init__(self, codec, rate):
        self.codec, self.rate = codec, Fraction(rate)
        self.width = self.height = 0
        self.pix_fmt, self.time_base = "yuv420p", None
        self.codec_context = _CodecContext(options={}, bit_rate=0, time_base=None,
                                          colorspace=2, color_range=0, color_primaries=2, color_trc=2)

    def encode(self, frame=None):
        return () if frame is None else (frame,)


class VideoOutput:
    """Small host pipeline adapter that feeds timestamped frames to CLI codecs."""
    def __init__(self, destination, controller, *, selection=None):
        self.destination, self.controller, self.selection = destination, controller, selection or getattr(controller, "ffmpeg_device", None)
        self.stream = self.process = self.writer = self.thread = None
        self.logs = BoundedLogBuffer(max_tail=60)
        self.closed = False

    def add_stream(self, codec, rate, *, hwaccel=None):
        if hwaccel is not None:
            raise ValueError("FFmpeg Vulkan output accepts host frames; CUDA-owned frames require their native transport.")
        if self.stream:
            raise ValueError("VideoOutput accepts one video stream.")
        self.stream = _OutputStream(codec, rate)
        return self.stream

    def _start(self, frame):
        s, ctx = self.stream, self.stream.codec_context
        options = []
        for key, value in ctx.options.items():
            options += ["-" + key, str(value)]
        if ctx.bit_rate:
            options += ["-b:v", str(ctx.bit_rate)]
        colors = []
        for key, flag in (("colorspace", "-colorspace"), ("color_range", "-color_range"),
                          ("color_primaries", "-color_primaries"), ("color_trc", "-color_trc")):
            colors += [flag, str(getattr(ctx, key))]
        command = [str(FFMPEG), "-v", "warning", "-y", "-copyts", "-f", "nut", "-i", "pipe:0",
                   "-map", "0:v:0", "-an", "-c:v", s.codec, *options, "-pix_fmt", s.pix_fmt,
                   *colors, "-fps_mode", "passthrough", "-enc_time_base", "demux", str(self.destination)]
        input_matrix = 0 if frame.format.is_rgb else frame.colorspace if frame.colorspace != 2 else (9 if ctx.color_primaries == 9 else 1)
        tags = f"setparams=colorspace={input_matrix}:color_primaries={ctx.color_primaries}:color_trc={ctx.color_trc}:range={'full' if frame.format.is_rgb else 'limited'}"
        command[-1:-1] = ["-vf", tags]
        command = prepare_command(command, selection=self.selection, dimensions=(s.width, s.height), input_format=frame.format.name,
                                  rate=str(s.rate), time_base=str(ctx.time_base or s.time_base or frame.time_base))
        self.actual_encoder = command[command.index("-c:v") + 1]
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.PIPE, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.controller.register(self.process)
        self.thread = threading.Thread(target=drain_bounded_text, args=(self.process.stderr, self.logs), daemon=True)
        self.thread.start()
        self.writer = RawVideoPacketMuxer(self.process.stdin, width=s.width, height=s.height,
                                         rate=s.rate, time_base=ctx.time_base or s.time_base or frame.time_base,
                                         pix_fmt=frame.format.name)
        self.raw_format = frame.format.name

    def mux(self, frame):
        if self.closed:
            raise ValueError("Video encoder is closed.")
        if self.controller.cancel.is_set():
            raise Cancelled("Render stopped by user.")
        if not self.process:
            self._start(frame)
        if (frame.width, frame.height, frame.format.name) != (self.stream.width, self.stream.height, self.raw_format):
            raise ValueError("Host frame dimensions or format changed during encoding.")
        pts = round(Fraction(frame.pts) * frame.time_base / self.writer.time_base)
        duration = round(Fraction(frame.duration or 0) * frame.time_base / self.writer.time_base)
        try:
            self.writer.write(packed_frame(frame), pts, duration)
        except (OSError, av.error.FFmpegError) as exc:
            raise RuntimeError("FFmpeg encoder failed:\n" + "\n".join(self.logs.snapshot())) from exc

    def close(self):
        if self.closed:
            return
        self.closed = True
        if not self.process:
            return
        try:
            self.writer.close()
            self.process.stdin.close()
            while self.process.poll() is None:
                if self.controller.cancel.is_set():
                    raise Cancelled("Render stopped by user.")
                try:
                    self.process.wait(timeout=0.2)
                except subprocess.TimeoutExpired:
                    continue
            self.thread.join(timeout=2)
            if self.process.returncode:
                raise RuntimeError("FFmpeg encoder failed:\n" + "\n".join(self.logs.snapshot()))
        finally:
            if self.process.poll() is None:
                self.process.kill()
                self.process.wait(timeout=5)
            self.controller.unregister(self.process)
            self.thread.join(timeout=2)
            self.process.stderr.close()
