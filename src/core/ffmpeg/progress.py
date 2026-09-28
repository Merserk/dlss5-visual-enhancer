"""Live FFmpeg work counters, independent of its human-readable stderr logs."""
from __future__ import annotations

import math
import queue
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Callable

from ..jobs import Cancelled, JobController


def _number(value) -> float:
    try:
        number = float(value)
        return max(0.0, number) if math.isfinite(number) else 0.0
    except (TypeError, ValueError, OverflowError):
        return 0.0


class VideoProgress:
    """Parse complete -progress records using frames, or timestamps for VFR/MKV.

    Metadata probing never decodes the source to count frames. When a container
    has no declared count, duration supplies the fraction and FPS supplies only
    an estimated total for the queue's frame/rate readout.
    """

    def __init__(self, metadata: dict | None, label: str):
        metadata = metadata or {}
        self.label = label
        self.frames = int(_number(metadata.get("frames")))
        self.duration = _number(metadata.get("video_duration")) or _number(metadata.get("duration"))
        self.estimated_frames = self.frames or round(self.duration * _number(metadata.get("fps")))
        self.record: dict[str, str] = {}
        self.fraction = 0.0
        self.processed = 0
        self.seconds = 0.0
        self.last_update: tuple[float, str] | None = None

    def feed(self, raw: bytes) -> tuple[float, str] | None:
        key, separator, value = raw.decode("utf-8", "replace").strip().partition("=")
        if not separator:
            return None
        self.record[key] = value
        if key != "progress":
            return None
        record, self.record = self.record, {}
        self.processed = max(self.processed, int(_number(record.get("frame"))))
        # Both out_time_us and the older out_time_ms are microseconds in FFmpeg.
        seconds = _number(record.get("out_time_us") or record.get("out_time_ms")) / 1_000_000
        if not seconds:
            parts = record.get("out_time", "").split(":")
            if len(parts) == 3 and not parts[0].startswith("-"):
                seconds = _number(parts[0]) * 3600 + _number(parts[1]) * 60 + _number(parts[2])
        self.seconds = max(self.seconds, seconds)
        if self.frames:
            fraction = self.processed / self.frames
        elif self.duration:
            fraction = self.seconds / self.duration
        else:
            fraction = 0.0
        # Even progress=end is sent before FFmpeg has returned successfully.
        self.fraction = max(self.fraction, min(.99, fraction))
        detail = f"{self.label}: {self.processed:,}"
        if self.estimated_frames:
            detail += f" / {self.estimated_frames:,}"
        detail += " frames"
        if not self.frames and self.estimated_frames:
            detail += " (estimated total)"
        update = (self.fraction, detail)
        if update == self.last_update:
            return None
        self.last_update = update
        return update


def run_with_progress(command: list[str], controller: JobController, label: str,
                      progress: Callable[[float, str], None], *, metadata: dict | None = None,
                      cwd: Path | None = None) -> None:
    """Drain machine progress live while retaining stderr and prompt cancellation.

    The reader only queues records. The render thread invokes callbacks, so a
    finished stage cannot leave late events overwriting the following stage.
    """
    if controller.cancel.is_set():
        raise Cancelled("Render stopped by user.")
    command = [command[0], "-nostdin", "-nostats", "-stats_period", "0.2",
               "-progress", "pipe:1", *command[1:]]
    tracker = VideoProgress(metadata, label)
    updates: queue.SimpleQueue[tuple[float, str]] = queue.SimpleQueue()
    reader_done = threading.Event()
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=errors,
                                   stdin=subprocess.DEVNULL, cwd=cwd,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        controller.register(process)

        def read_progress():
            try:
                for raw in iter(process.stdout.readline, b""):
                    update = tracker.feed(raw)
                    if update is not None:
                        updates.put(update)
            finally:
                reader_done.set()

        reader = threading.Thread(target=read_progress, name="ffmpeg-progress", daemon=True)
        reader.start()
        try:
            progress(0.0, label)
            while not (reader_done.is_set() and process.poll() is not None):
                if controller.cancel.is_set():
                    raise Cancelled("Render stopped by user.")
                try:
                    progress(*updates.get(timeout=.05))
                except queue.Empty:
                    pass
            while not updates.empty():
                if controller.cancel.is_set():
                    raise Cancelled("Render stopped by user.")
                progress(*updates.get_nowait())
            if controller.cancel.is_set():
                raise Cancelled("Render stopped by user.")
            if process.wait():
                errors.seek(0, 2)
                errors.seek(max(0, errors.tell() - 2500))
                raise RuntimeError(f"{label} failed: {errors.read().decode('utf-8', 'replace')}")
            progress(1.0, f"{label} complete")
        finally:
            try:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
            finally:
                controller.unregister(process)
                reader.join(timeout=2)
                process.stdout.close()
