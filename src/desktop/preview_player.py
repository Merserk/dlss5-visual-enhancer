"""Decode cached video straight into Qt's display surface, without a video encode.

Qt's bundled decoder converts planar RGB10 to RGB8 and loses HDR signaling.
Use the application's decoder instead and upload 10-bit display frames. RGB10
cache files stay untouched; the YUV conversion exists only in display memory.
"""
from __future__ import annotations

from collections import deque
import math
import subprocess
import threading
import time

import av
import numpy as np
from PySide6.QtCore import QObject, Property, QRect, QSize, QTimer, QUrl, Signal, Slot
from PySide6.QtMultimedia import QVideoFrame, QVideoFrameFormat

from ..core.ffmpeg.probe import probe_video
from ..core.jobs import BoundedLogBuffer, JobController, drain_bounded_text
from ..core.paths import FFMPEG


class _PipeReader:
    def __init__(self, pipe):
        self.pipe = pipe

    def read(self, size):
        return self.pipe.read(size)


def _display_format(metadata: dict) -> QVideoFrameFormat:
    width, height = int(metadata["width"]), int(metadata["height"])
    result = QVideoFrameFormat(QSize((width + 1) // 2 * 2, (height + 1) // 2 * 2),
                              QVideoFrameFormat.PixelFormat.Format_P010)
    result.setViewport(QRect(0, 0, width, height))
    result.setColorRange(QVideoFrameFormat.ColorRange.ColorRange_Video)
    space = QVideoFrameFormat.ColorSpace
    result.setColorSpace(space.ColorSpace_BT2020 if metadata.get("color_primaries") == "bt2020"
                         else space.ColorSpace_BT709)
    transfer = QVideoFrameFormat.ColorTransfer
    result.setColorTransfer({
        "smpte2084": transfer.ColorTransfer_ST2084,
        "arib-std-b67": transfer.ColorTransfer_STD_B67,
        "linear": transfer.ColorTransfer_Linear,
        "gamma22": transfer.ColorTransfer_Gamma22,
        "gamma28": transfer.ColorTransfer_Gamma28,
    }.get(metadata.get("color_transfer"), transfer.ColorTransfer_BT709))
    return result


def _display_frame(pixels: bytes, display_format: QVideoFrameFormat,
                   position_us: int, duration_us: int) -> QVideoFrame:
    width, height = display_format.frameWidth(), display_format.frameHeight()
    if len(pixels) != width * height * 3:
        raise RuntimeError("Preview decoder returned an incomplete display frame.")
    result = QVideoFrame(display_format)
    if not result.map(QVideoFrame.MapMode.WriteOnly):
        raise RuntimeError("Cannot allocate the preview display frame.")
    try:
        offset = 0
        for index, rows in enumerate((height, height // 2)):
            # P010: a 16-bit luma plane followed by interleaved 16-bit U/V.
            row_bytes = width * 2
            size = rows * row_bytes
            source = np.frombuffer(pixels, dtype=np.uint8, count=size, offset=offset).reshape(rows, row_bytes)
            target = np.frombuffer(result.bits(index), dtype=np.uint8).reshape(
                rows, result.bytesPerLine(index))
            target[:, :row_bytes] = source[:, :row_bytes]
            offset += size
    finally:
        result.unmap()
    result.setStartTime(position_us)
    result.setEndTime(position_us + duration_us)
    return result


class PreviewPlayer(QObject):
    """Silent, bounded preview player; the original player masters audio/timeline.

    The small MediaPlayer-compatible surface keeps range playback, prewarming,
    and two-up comparison independent of the codec used for final exports.
    A single decoder thread owns each stream; seeks cancel old work immediately.
    """
    sourceChanged = Signal()
    videoOutputChanged = Signal()
    loopsChanged = Signal()
    positionChanged = Signal(int)
    durationChanged = Signal(int)
    videoFpsChanged = Signal()
    mediaStatusChanged = Signal(int)
    playbackStateChanged = Signal(int)
    errorOccurred = Signal(int, str)
    errorStringChanged = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._source, self._output, self._sink = QUrl(), None, None
        self._loops, self._position, self._duration = 1, 0, 0
        self._video_fps = 0.0
        self._status, self._state, self._error = 0, 0, ""
        self._condition = threading.Condition()
        self._queue = deque()
        self._generation = 0
        self._request = None
        self._controller = None
        self._closed = False
        self._primed = False
        self._finished = False
        self._last_frame = QVideoFrame()
        self._clock = (time.monotonic(), 0)
        self._timer = QTimer(self)
        self._timer.setInterval(5)
        self._timer.timeout.connect(self._tick)
        self._thread = threading.Thread(target=self._decode_loop, name="preview-playback", daemon=True)
        self._thread.start()
        # QML objects are released before the application, including tests.
        self.destroyed.connect(lambda *_: self.close())
        from PySide6.QtCore import QCoreApplication
        if QCoreApplication.instance():
            QCoreApplication.instance().aboutToQuit.connect(self.close)

    def _set_source(self, value):
        value = QUrl(value)
        if value == self._source:
            return
        self._source = value
        self._set_state(2)
        self._set_position_value(0)
        self._duration = 0
        self.durationChanged.emit(0)
        if self._video_fps:
            self._video_fps = 0.0
            self.videoFpsChanged.emit()
        self._error = ""
        self.errorStringChanged.emit()
        self._last_frame = QVideoFrame()
        if self._sink:
            self._sink.setVideoFrame(self._last_frame)
        self._set_status(1 if not value.isEmpty() else 0)
        self._restart()
        self.sourceChanged.emit()

    def _set_output(self, output):
        self._output = output
        self._sink = output.property("videoSink") if output is not None else None
        if self._sink and self._last_frame.isValid():
            self._sink.setVideoFrame(self._last_frame)
        self.videoOutputChanged.emit()

    def _set_loops(self, value):
        self._loops = int(value)
        self.loopsChanged.emit()

    source = Property(QUrl, lambda self: self._source, _set_source, notify=sourceChanged)
    videoOutput = Property(QObject, lambda self: self._output, _set_output, notify=videoOutputChanged)
    loops = Property(int, lambda self: self._loops, _set_loops, notify=loopsChanged)
    position = Property(int, lambda self: self._position, lambda self, v: self.seek(v), notify=positionChanged)
    duration = Property(int, lambda self: self._duration, notify=durationChanged)
    videoFps = Property(float, lambda self: self._video_fps, notify=videoFpsChanged)
    mediaStatus = Property(int, lambda self: self._status, notify=mediaStatusChanged)
    playbackState = Property(int, lambda self: self._state, notify=playbackStateChanged)
    errorString = Property(str, lambda self: self._error, notify=errorStringChanged)

    def _set_status(self, value):
        if self._status != value:
            self._status = value
            self.mediaStatusChanged.emit(value)

    def _set_state(self, value):
        if self._state != value:
            self._state = value
            self.playbackStateChanged.emit(value)

    def _set_position_value(self, value):
        if self._position != value:
            self._position = value
            self.positionChanged.emit(value)

    def _restart(self):
        with self._condition:
            if self._closed:
                return
            self._generation += 1
            if self._controller:
                self._controller.stop()
            self._queue.clear()
            self._primed = self._finished = False
            self._request = (self._generation, self._source.toLocalFile(), self._position)
            self._condition.notify_all()
        self._clock = (time.monotonic(), self._position)
        if not self._source.isEmpty():
            self._timer.start()
        else:
            self._timer.stop()

    @Slot(int)
    def seek(self, position):
        position = max(0, int(position))
        if self._duration:
            position = min(position, max(0, self._duration - 1))
        if position == self._position and self._status != 6:
            return
        self._set_position_value(position)
        self._restart()

    @Slot()
    def play(self):
        if self._source.isEmpty() or self._closed:
            return
        if self._status == 6:
            self.seek(0)
        if self._state != 1:
            self._clock = (time.monotonic(), self._position)
            self._set_state(1)
        self._timer.start()

    @Slot()
    def pause(self):
        self._set_state(2)
        if self._primed:
            self._timer.stop()

    @Slot()
    def stop(self):
        self.pause()
        self.seek(0)
        self._set_state(0)

    @Slot()
    def close(self):
        with self._condition:
            self._closed = True
            if self._controller:
                self._controller.stop()
            self._queue.clear()
            self._condition.notify_all()
        # Never wait for decoding on the GUI thread. Cancellation closes the
        # pipe, and the daemon releases its handles in the decoder's finally.

    def _enqueue(self, generation, kind, payload):
        with self._condition:
            while not self._closed and generation == self._generation and len(self._queue) >= 3:
                self._condition.wait()
            if self._closed or generation != self._generation:
                return False
            self._queue.append((kind, payload))
            return True

    def _decode_loop(self):
        while True:
            with self._condition:
                while not self._closed and self._request is None:
                    self._condition.wait()
                if self._closed:
                    return
                generation, path, position = self._request
                self._request = None
                controller = self._controller = JobController()
            if not path:
                continue
            try:
                metadata = probe_video(path, count_mode="metadata", controller=controller)
                if controller.cancel.is_set():
                    continue
                self._enqueue(generation, "duration", round(float(metadata.get("duration") or 0) * 1000))
                fps = float(metadata.get("fps") or 0)
                self._enqueue(generation, "fps", fps if math.isfinite(fps) and fps > 0 else 0.0)
                self._decode(path, metadata, position, generation, controller)
                if not controller.cancel.is_set():
                    self._enqueue(generation, "end", None)
            except Exception as exc:
                if not controller.cancel.is_set():
                    self._enqueue(generation, "error", str(exc))

    def _decode(self, path, metadata, position, generation, controller):
        fps = float(metadata.get("fps") or 30)
        start = max(0, math.floor(position * fps / 1000) / fps - .002)
        space = "bt2020" if metadata.get("color_primaries") == "bt2020" else "bt709"
        # Only convert for the display: no compressed encoder, file or proxy.
        command = [str(FFMPEG), "-hide_banner", "-v", "error", "-nostdin", "-copyts", "-start_at_zero", "-ss", f"{start:.6f}",
                   "-i", path, "-map", "0:v:0", "-an", "-sn", "-dn",
                   "-vf", f"scale=out_color_matrix={space}:out_range=tv,format=p010le,pad=ceil(iw/2)*2:ceil(ih/2)*2",
                   "-c:v", "rawvideo", "-pix_fmt", "p010le", "-fps_mode", "passthrough",
                   "-f", "nut", "-write_index", "0", "pipe:1"]
        logs = BoundedLogBuffer(max_tail=15)
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        controller.register(process)
        reader = threading.Thread(target=drain_bounded_text, args=(process.stderr, logs), daemon=True)
        reader.start()
        container = None
        try:
            container = av.open(_PipeReader(process.stdout), format="nut")
            display_format = _display_format(metadata)
            # Demux raw packets only. FFmpeg and PyAV can use different raw
            # pixel-format tags; our explicit P010 layout needs no decoder.
            for raw in container.demux(video=0):
                if not raw.size or raw.pts is None:
                    continue
                if controller.cancel.is_set():
                    return
                timestamp = float(raw.pts * raw.time_base)
                duration = float(raw.duration * raw.time_base) if raw.duration else 1 / fps
                frame = _display_frame(bytes(raw), display_format, round(timestamp * 1000000), round(duration * 1000000))
                if not self._enqueue(generation, "frame", frame):
                    return
            process.wait(timeout=5)
            reader.join(timeout=1)
            if process.returncode:
                raise RuntimeError("Preview decoding failed: " + "\n".join(logs.snapshot()))
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
            if container:
                container.close()
            reader.join(timeout=1)
            controller.unregister(process)
            process.stdout.close()
            process.stderr.close()

    def _tick(self):
        if self._closed:
            self._timer.stop()
            return
        playing = self._state == 1
        now = time.monotonic()
        target = self._clock[1] + (now - self._clock[0]) * 1000 if playing and self._primed else self._position
        with self._condition:
            while self._queue:
                kind, payload = self._queue[0]
                if kind == "frame" and self._primed and (not playing or payload.startTime() / 1000 > target + 2):
                    break
                self._queue.popleft()
                self._condition.notify_all()
                if kind == "duration":
                    self._duration = payload
                    self.durationChanged.emit(payload)
                elif kind == "fps":
                    if self._video_fps != payload:
                        self._video_fps = payload
                        self.videoFpsChanged.emit()
                elif kind == "frame":
                    self._last_frame = payload
                    if self._sink:
                        self._sink.setVideoFrame(payload)
                    if not self._primed:
                        self._primed = True
                        self._clock = (now, self._position)
                        self._set_status(2)
                elif kind == "end":
                    self._finished = True
                elif kind == "error":
                    self._error = payload
                    self.errorStringChanged.emit()
                    self._set_status(7)
                    self._set_state(0)
                    self.errorOccurred.emit(2, payload)
                    self._timer.stop()
                    return
        # A loaded/error notification can synchronously play, pause or seek
        # from QML. Honor that new state instead of the state at tick entry.
        playing = self._state == 1
        target = self._clock[1] + (time.monotonic() - self._clock[0]) * 1000
        if playing and self._primed:
            self._set_position_value(min(self._duration, round(target)))
            if self._finished and self._position >= self._duration:
                if self._loops < 0:
                    self.seek(0)
                else:
                    self._set_status(6)
                    self._set_state(0)
                    self._timer.stop()
        elif self._primed:
            self._timer.stop()
