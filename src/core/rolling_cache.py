"""Bounded FFV1 or ProRes Proxy packet spool with continuous codec state.

Packets become readable after their bytes are written, without waiting for a
whole segment. Segments are unlinked only when sealed and fully consumed.
Timestamps are carried separately, avoiding container timestamp rounding at
cache boundaries. This is disposable working storage, not a restart checkpoint.
"""
from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass
from pathlib import Path

import av
from av.video.reformatter import ColorRange, Colorspace, VideoReformatter

from .jobs import Cancelled
from .cache_video import CACHE_VIDEO_CODECS


CACHE_BYTES = 5_000_000_000
SEGMENT_BYTES = 64 * 1024 * 1024


@dataclass
class _Segment:
    path: Path
    size: int = 0
    readers: int = 0
    sealed: bool = False


@dataclass
class _Packet:
    segment: _Segment
    offset: int
    size: int
    pts: int
    duration: int
    time_base: object


class RollingFrameCache:
    def __init__(self, frames, directory: Path, controller, *,
                 limit: int = CACHE_BYTES, segment_bytes: int = SEGMENT_BYTES,
                 max_frames: int = 1800, threads: int = 4, codec: str = "FFV1"):
        if limit < 1 or max_frames < 1:
            raise ValueError("Rolling cache limits must be positive.")
        if codec not in CACHE_VIDEO_CODECS:
            raise ValueError(f"Unknown rolling cache codec: {codec!r}.")
        self.frames, self.directory, self.controller = iter(frames), Path(directory), controller
        self.codec = codec
        self.limit = int(limit)
        self.segment_bytes = min(int(segment_bytes), max(1, self.limit // 4))
        self.max_frames, self.threads = int(max_frames), int(threads)
        self.condition = threading.Condition()
        self.packets = deque()
        self.segments: list[_Segment] = []
        self.stop = threading.Event()
        self.finished = False
        self.failure = None
        self.codec_info = None
        self.cache_colorspace = None
        self.used_bytes = self.peak_bytes = self.written_frames = self.read_frames = 0
        self.waits = 0
        self.thread = None
        self.directory.mkdir(parents=True, exist_ok=True)

    def _check(self):
        if self.failure is not None:
            raise self.failure
        if self.stop.is_set() or self.controller.cancel.is_set():
            raise Cancelled("Rolling cache stopped.")

    def _reclaim(self, segment):
        # Called with the condition held; there are no open reader handles.
        if segment.sealed and segment.readers == 0:
            segment.path.unlink(missing_ok=True)
            self.used_bytes -= segment.size
            self.segments.remove(segment)
            self.condition.notify_all()

    def _produce(self):
        codec = file = segment = None
        reformatter = VideoReformatter()
        sequence = 0

        def seal():
            nonlocal file, segment
            if file is not None:
                file.close()
                file = None
                with self.condition:
                    segment.sealed = True
                    self._reclaim(segment)
                segment = None

        try:
            for frame in self.frames:
                self._check()
                if codec is None:
                    fmt = frame.format.name
                    if self.codec == "FFV1":
                        supported = {f.name for f in av.Codec("ffv1", "w").video_formats}
                        if fmt == "rgba":
                            fmt = "bgra"  # Exact channel permutation, no YUV conversion.
                        if fmt not in supported:
                            raise ValueError(f"FFV1 rolling cache cannot preserve {fmt} samples.")
                        codec = av.CodecContext.create("ffv1", "w")
                    else:
                        fmt = "yuv422p10le"
                        codec = av.CodecContext.create("prores_ks", "w")
                        matrix = int(frame.colorspace)
                        primaries = int(frame.color_primaries)
                        self.cache_colorspace = (
                            Colorspace.BT2020 if 9 in (matrix, primaries) else
                            Colorspace.ITU601 if matrix in {5, 6} or primaries in {5, 6} else
                            Colorspace.ITU709
                        )
                    codec.width, codec.height, codec.pix_fmt = frame.width, frame.height, fmt
                    codec.time_base = frame.time_base
                    codec.thread_count = self.threads
                    if self.codec == "FFV1":
                        codec.thread_type = "SLICE"
                        codec.options = {"level": "3", "slicecrc": "1", "coder": "1", "context": "0"}
                    else:
                        codec.options = {"profile": "0"}
                    codec.gop_size = 1
                    codec.open()
                    self.codec_info = (bytes(codec.extradata or b""), frame.width, frame.height,
                                       frame.colorspace, frame.color_range,
                                       frame.color_primaries, frame.color_trc)
                if (frame.width, frame.height) != (codec.width, codec.height):
                    raise ValueError("Frame dimensions changed within the rolling cache.")
                if self.codec == "FFV1":
                    pixels = frame if frame.format.name == codec.pix_fmt else frame.reformat(format=codec.pix_fmt)
                else:
                    pixels = reformatter.reformat(frame, format=codec.pix_fmt,
                                                  dst_colorspace=self.cache_colorspace,
                                                  src_color_range=ColorRange.JPEG if frame.format.is_rgb else ColorRange.MPEG,
                                                  dst_color_range=ColorRange.MPEG)
                packets = codec.encode(pixels)
                if len(packets) != 1:
                    raise RuntimeError(f"{self.codec} cache did not encode exactly one packet per frame.")
                data = bytes(packets[0])
                size = len(data)
                if size > self.limit:
                    raise RuntimeError("A single cached frame exceeds the rolling cache capacity.")
                if segment is not None and segment.size + size > self.segment_bytes:
                    seal()
                with self.condition:
                    full = self.used_bytes + size > self.limit or len(self.packets) >= self.max_frames
                if full:
                    # Sealing releases an already-consumed current segment and
                    # prevents deadlock even with a cache smaller than one chunk.
                    seal()
                with self.condition:
                    while self.used_bytes + size > self.limit or len(self.packets) >= self.max_frames:
                        self._check()
                        self.waits += 1
                        self.condition.wait(.1)
                    self._check()
                    if file is None:
                        suffix = ".ffv1" if self.codec == "FFV1" else ".prores"
                        segment = _Segment(self.directory / f"{sequence:08d}{suffix}")
                        sequence += 1
                        file = segment.path.open("wb", buffering=0)
                        self.segments.append(segment)
                    offset = segment.size
                    self.used_bytes += size
                    self.peak_bytes = max(self.peak_bytes, self.used_bytes)
                    segment.size += size
                    segment.readers += 1
                remaining = memoryview(data)
                while remaining:
                    self._check()
                    written = file.write(remaining)
                    if not written:
                        raise OSError("Rolling cache write did not complete.")
                    remaining = remaining[written:]
                del remaining
                with self.condition:
                    self.packets.append(_Packet(segment, offset, size, frame.pts,
                                                frame.duration, frame.time_base))
                    self.written_frames += 1
                    self.condition.notify_all()
                del frame, pixels, packets, data
            if codec is not None and codec.encode():
                raise RuntimeError(f"{self.codec} cache unexpectedly delayed video packets.")
        except BaseException as exc:
            with self.condition:
                self.failure = exc
                self.condition.notify_all()
        finally:
            # A final flush/close/unlink error must wake the consumer too. If
            # cleanup throws before finished is set, an empty consumer queue
            # would otherwise wait forever for a producer that has exited.
            for cleanup in (seal, getattr(self.frames, "close", None)):
                if cleanup:
                    try:
                        cleanup()
                    except BaseException as exc:
                        with self.condition:
                            if self.failure is None:
                                self.failure = exc
            with self.condition:
                self.finished = True
                self.condition.notify_all()

    def __iter__(self):
        if self.thread is not None:
            raise RuntimeError("A rolling cache can only be consumed once.")
        self.thread = threading.Thread(target=self._produce, name="rolling-frame-cache", daemon=True)
        self.thread.start()
        decoder = None
        reformatter = VideoReformatter()
        try:
            while True:
                with self.condition:
                    while not self.packets and not self.finished:
                        self._check()
                        self.condition.wait(.1)
                    self._check()
                    if not self.packets:
                        break
                    record = self.packets.popleft()
                    self.condition.notify_all()
                if decoder is None:
                    extra, width, height, *_ = self.codec_info
                    decoder = av.CodecContext.create("ffv1" if self.codec == "FFV1" else "prores", "r")
                    decoder.extradata = extra
                    decoder.width, decoder.height = width, height
                    decoder.thread_count = self.threads
                    decoder.thread_type = "SLICE"
                    decoder.open()
                with record.segment.path.open("rb", buffering=0) as source:
                    source.seek(record.offset)
                    data = source.read(record.size)
                if len(data) != record.size:
                    raise RuntimeError("Rolling cache packet is incomplete.")
                decoded = decoder.decode(av.Packet(data))
                if len(decoded) != 1 or decoded[0].is_corrupt:
                    raise RuntimeError("Rolling cache failed to decode a complete frame.")
                frame = decoded[0]
                if self.codec == "ProRes Proxy":
                    # Downstream rolling stages keep an RGB10 state. Convert
                    # the decoded 4:2:2 frame back to that working format.
                    frame = reformatter.reformat(frame, format="gbrp10le",
                                                 src_colorspace=self.cache_colorspace,
                                                 src_color_range=ColorRange.MPEG,
                                                 dst_color_range=ColorRange.JPEG)
                frame.pts, frame.duration, frame.time_base = record.pts, record.duration, record.time_base
                (_, _, _, frame.colorspace, frame.color_range,
                 frame.color_primaries, frame.color_trc) = self.codec_info
                with self.condition:
                    record.segment.readers -= 1
                    self._reclaim(record.segment)
                    self.read_frames += 1
                del data, record, decoded
                yield frame
            if self.read_frames != self.written_frames:
                raise RuntimeError("Rolling cache frame accounting does not match.")
        finally:
            self.close()

    def close(self):
        self.stop.set()
        with self.condition:
            self.condition.notify_all()
        if self.thread is not None and self.thread is not threading.current_thread():
            self.thread.join(timeout=30)
            if self.thread.is_alive():
                raise RuntimeError("Rolling cache producer did not stop.")
        with self.condition:
            for segment in list(self.segments):
                segment.path.unlink(missing_ok=True)
            self.segments.clear()
            self.packets.clear()
            self.used_bytes = 0


# Keep the previous import available to callers outside the desktop workflow.
LosslessFrameCache = RollingFrameCache
