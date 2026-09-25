from __future__ import annotations

import math
import queue
import threading
from collections import deque
from dataclasses import dataclass
from fractions import Fraction

import av
import numpy as np

from ..core.jobs import Cancelled

TIME_BASE = Fraction(1, 90000)


class PipeReader:
    """Expose only operations supported by a subprocess pipe to libavformat.

    BufferedReader has a seek method even on Windows pipes. Advertising it
    makes NUT probing attempt seeks and can silently consume opening frames.
    """

    def __init__(self, stream) -> None:
        self.stream = stream

    def read(self, size: int) -> bytes:
        return self.stream.read(size)


class PipeWriter:
    def __init__(self, stream) -> None:
        self.stream = stream

    def write(self, data) -> int:
        return self.stream.write(data)

    def flush(self) -> None:
        self.stream.flush()


def put(queue_: queue.Queue, item, cancel: threading.Event) -> None:
    while not cancel.is_set():
        try:
            queue_.put(item, timeout=0.1)
            return
        except queue.Full:
            pass
    raise Cancelled("Live stopped.")


def get(queue_: queue.Queue, cancel: threading.Event):
    while not cancel.is_set():
        try:
            return queue_.get(timeout=0.1)
        except queue.Empty:
            pass
    raise Cancelled("Live stopped.")


@dataclass(slots=True)
class VideoFrame:
    index: int
    pts: int
    duration: int
    rgba: np.ndarray
    reset: bool = False


class AdaptiveRate:
    """Select a regular source-frame cadence, never renumber media timestamps.

    Auto only steps down during a session, with headroom and hysteresis. It
    cannot oscillate between rates in response to network or player waits.
    """

    def __init__(self, source_rate: Fraction, choice: str) -> None:
        self.rate = min(source_rate, Fraction(60)) if choice == "Auto" else (
            source_rate if choice == "Source" else min(source_rate, Fraction(choice)))
        self.automatic = choice == "Auto"
        self.divisor = 1
        self.samples: deque[float] = deque(maxlen=60)
        self.observed = 0
        self._warmup_left = 6
        self.changes: list[dict] = []
        self._lock = threading.Lock()

    @property
    def fps(self) -> float:
        with self._lock:
            return float(self.rate) / self.divisor

    def accepts(self, index: int) -> bool:
        with self._lock:
            return index % self.divisor == 0

    def observe(self, dlss_seconds: float, guide_seconds: float, encode_seconds: float) -> None:
        if not self.automatic:
            return
        with self._lock:
            self.observed += 1
            # Exclude the cold feature evaluation and the first GPU ramp-up.
            if self._warmup_left:
                self._warmup_left -= 1
                return
            self.samples.append(max(dlss_seconds, guide_seconds, encode_seconds) + 0.002)
            if len(self.samples) < 24 or self.observed % 30:
                return
            sample = sorted(self.samples)[int((len(self.samples) - 1) * 0.90)]
            needed = max(1, math.ceil(float(self.rate) * sample / 0.80))
            # Short scheduling spikes should not keep ratcheting the rate down.
            if needed > self.divisor and sample * self.fps_unlocked() > 0.88:
                self.divisor = min(needed, max(1, math.ceil(float(self.rate))))
                self.changes.append({"after_frames": self.observed, "fps": self.fps_unlocked()})

    def reset_measurements(self) -> None:
        """Discard startup costs after a bridge-session change, keeping cadence."""
        with self._lock:
            self.samples.clear()
            self._warmup_left = 6

    def fps_unlocked(self) -> float:
        return float(self.rate) / self.divisor


class TimestampMuxer:
    """Carry enhanced RGBA and original audio timestamps over one NUT pipe."""

    def __init__(self, pipe, width: int, height: int, rate: Fraction, audio_template=None,
                 pix_fmt: str = "rgba") -> None:
        self.container = av.open(PipeWriter(pipe), mode="w", format="nut", options={"write_index": "0"})
        self.video = self.container.add_stream("rawvideo", rate=rate)
        self.video.width = width
        self.video.height = height
        self.video.pix_fmt = pix_fmt
        if pix_fmt == "rgba":
            self.video.codec_context.codec_tag = "RGBA"
        self.video.time_base = TIME_BASE
        self.audio = self.container.add_stream_from_template(audio_template) if audio_template else None
        self.container.start_encoding()

    def write(self, item: VideoFrame | av.Packet) -> None:
        if isinstance(item, VideoFrame):
            packet = av.Packet(memoryview(item.rgba).cast("B"))
            packet.stream = self.video
            packet.pts = packet.dts = item.pts
            packet.duration = item.duration
            packet.time_base = TIME_BASE
            packet.is_keyframe = True
        else:
            if self.audio is None:
                return
            packet = item
            packet.stream = self.audio
        self.container.mux(packet)

    def close(self) -> None:
        self.container.close()
