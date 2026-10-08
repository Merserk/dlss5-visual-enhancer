"""Strict byte accounting and a fixed lossless file helper for reference tests.

Production rolling-cache exports use FrameBlocks or RGBFrameBlocks. The older
file-based reference scheduler retains lossless RGB10 intermediates for its
timing and ownership oracles, independently of the user-facing cache modes.
"""
from __future__ import annotations

import itertools
import subprocess
import threading
from collections import deque
from contextlib import suppress
from fractions import Fraction
from pathlib import Path

import av

from . import app_log
from .cache_video import DEFAULT_CACHE_SIZE_GB, cache_ffmpeg_args, cache_size_bytes
from .ffmpeg.frames import _Reader, open_video_decoder, packed_frame
from .ffmpeg.nut import RawVideoPacketMuxer
from .ffmpeg.vulkan import prepare_command
from .frame_process import spawn_frame_process
from .jobs import BoundedLogBuffer, Cancelled, drain_bounded_text, use_job_controller
from .paths import FFMPEG

CACHE_BYTES = cache_size_bytes(DEFAULT_CACHE_SIZE_GB)
HISTORY_FRAMES = 32


class CacheBudget:
    """Account for actual bytes before writing, across all live cache files."""

    def __init__(self, limit=CACHE_BYTES):
        if limit < 1:
            raise ValueError("Rolling cache capacity must be positive.")
        self.limit = int(limit)
        self.used_bytes = self.peak_bytes = 0
        self._lock = threading.Lock()

    def reserve(self, size):
        with self._lock:
            if size < 0:
                raise ValueError("A cache reservation cannot be negative.")
            if size and self.used_bytes + size >= self.limit:
                raise RuntimeError("Rolling cache capacity exceeded; reduce the processing window.")
            self.used_bytes += size
            self.peak_bytes = max(self.peak_bytes, self.used_bytes)

    def release(self, size):
        with self._lock:
            if size < 0 or size > self.used_bytes:
                raise ValueError("Invalid cache release.")
            self.used_bytes -= size


class _CacheWriter:
    def __init__(self, path, budget):
        self.file = path.open("wb", buffering=0)
        self.budget, self.size = budget, 0

    def write(self, data):
        position = self.file.tell()
        growth = max(0, position + len(data) - self.size)
        self.budget.reserve(growth)
        try:
            written = self.file.write(data)
        except BaseException:
            self.budget.release(growth)
            raise
        end = max(self.size, position + written)
        self.budget.release(growth - (end - self.size))
        self.size = end
        return written

    def flush(self):
        self.file.flush()

    def seek(self, offset, whence=0):
        return self.file.seek(offset, whence)

    def tell(self):
        return self.file.tell()


def forget_traceback(exc):
    # Saved producer exceptions must not own suspended GPU/frame generators.
    import traceback
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        traceback.clear_frames(exc.__traceback__)
        exc.__traceback__ = None
        exc = exc.__cause__ or exc.__context__


class LosslessFrameCache:
    def __init__(self, frames, directory: Path, controller, *, limit=CACHE_BYTES,
                 budget=None, max_frames=None, threads=4, metadata=None,
                 rate=None, video_filter="", dimensions=None, decode_format="gbrp10le", on_packet=None):
        if max_frames is not None and max_frames < 1:
            raise ValueError("Rolling cache frame limit must be positive.")
        self.frames, self.directory, self.controller = iter(frames), Path(directory), controller
        self.metadata, self.rate = dict(metadata or {}), rate
        self.video_filter, self.dimensions = video_filter, dimensions
        self.decode_format = decode_format
        self.budget = budget if budget is not None else CacheBudget(limit)
        self.limit = self.budget.limit
        self.max_frames = None if max_frames is None else int(max_frames)
        self.threads, self.on_packet = int(threads), on_packet
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "frames.mkv"
        self.timings = []
        self.written_frames = self.read_frames = self.used_bytes = 0
        self.encoder = None
        self.decode_backend = None
        self.color_info = None
        self.process = self.writer_thread = self.decoder = None
        self.stop = threading.Event()
        self.built = self.closed = self.consumed = False

    @property
    def peak_bytes(self):
        return self.budget.peak_bytes

    def _check(self):
        if self.stop.is_set() or self.controller.cancel.is_set():
            raise Cancelled("Rolling cache stopped.")

    def build(self):
        """Finish a bounded window before the next model can load."""
        if self.built:
            return self
        first = None
        output = encoded = spool = logger = None
        pending = deque()
        condition = threading.Condition()
        failure = []
        writer_done = threading.Event()
        logs = BoundedLogBuffer(max_tail=60)
        try:
            self._check()
            first = next(self.frames)
            self.color_info = (first.color_primaries, first.color_trc)
            rate = Fraction(self.rate or Fraction(1, 1) / first.time_base / max(1, first.duration))
            meta = dict(self.metadata)
            for key, fallback in (("color_primaries", "bt2020" if meta.get("hdr") else "bt709"),
                                  ("color_transfer", "smpte2084" if meta.get("hdr") else "bt709")):
                if meta.get(key) in {None, "", "unknown", "unspecified"}:
                    meta[key] = fallback
            matrix = "gbr" if first.format.is_rgb else meta.get("color_space") or "bt709"
            if matrix in {"unknown", "unspecified"}:
                matrix = "bt2020nc" if meta.get("hdr") else "bt709"
            tags = (f"setparams=colorspace={matrix}:range={'full' if first.format.is_rgb or meta.get('color_range') == 'pc' else 'limited'}:"
                    f"color_primaries={meta['color_primaries']}:color_trc={meta['color_transfer']}")
            graph = self.video_filter(first) if callable(self.video_filter) else self.video_filter
            command = [str(FFMPEG), "-v", "warning", "-xerror", "-copyts", "-probesize", "32", "-analyzeduration", "0",
                       "-f", "nut", "-i", "pipe:0", "-map", "0:v:0", "-an", "-vf", tags + ("," + graph if graph else ""),
                       *cache_ffmpeg_args("Fast lossless", meta), "-threads", str(self.threads),
                       "-fps_mode", "passthrough", "-enc_time_base", "demux",
                       "-f", "nut", "-write_index", "0", "-flush_packets", "1", "pipe:1"]
            with use_job_controller(self.controller):
                command = prepare_command(command, selection=self.controller.ffmpeg_device,
                                          dimensions=self.dimensions or (first.width, first.height), input_format=first.format.name,
                                          rate=str(rate), time_base=str(first.time_base), preserve_samples=True)
            self.encoder = command[command.index("-c:v") + 1]
            self.process = spawn_frame_process(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self.controller.register(self.process)
            logger = threading.Thread(target=drain_bounded_text, args=(self.process.stderr, logs), daemon=True)
            logger.start()

            def write():
                muxer = None
                try:
                    muxer = RawVideoPacketMuxer(self.process.stdin, width=first.width, height=first.height,
                                               rate=rate, time_base=first.time_base, pix_fmt=first.format.name)
                    for frame in itertools.chain((first,), self.frames):
                        self._check()
                        if (frame.width, frame.height, frame.format.name) != (first.width, first.height, first.format.name):
                            raise ValueError("Cache input dimensions or sample format changed.")
                        with condition:
                            pending.append((frame.pts, frame.duration, frame.time_base))
                        pts = round(Fraction(frame.pts) * frame.time_base / muxer.time_base)
                        duration = round(Fraction(frame.duration or 0) * frame.time_base / muxer.time_base)
                        muxer.write(packed_frame(frame), pts, duration)
                    muxer.close()
                    muxer = None
                    self.process.stdin.close()
                except BaseException as exc:
                    forget_traceback(exc)
                    failure.append(exc)
                    if self.process.poll() is None:
                        self.process.kill()
                finally:
                    if muxer:
                        with suppress(Exception):
                            muxer.close()
                    try:
                        close = getattr(self.frames, "close", None)
                        if close:
                            close()
                    except BaseException as exc:
                        forget_traceback(exc)
                        failure.append(exc)
                    writer_done.set()

            self.writer_thread = threading.Thread(target=write, name="rolling-cache-input", daemon=True)
            self.writer_thread.start()
            encoded = av.open(_Reader(self.process.stdout), format="nut")
            spool = _CacheWriter(self.path, self.budget)
            output = av.open(spool, mode="w", format="matroska")
            stream = output.add_stream_from_template(encoded.streams.video[0])
            context = stream.codec_context
            primaries = {"bt709": 1, "bt470m": 4, "bt470bg": 5, "smpte170m": 6, "smpte240m": 7,
                         "film": 8, "bt2020": 9, "smpte428": 10, "smpte431": 11, "smpte432": 12}
            transfers = {"bt709": 1, "bt470m": 4, "bt470bg": 5, "smpte170m": 6, "smpte240m": 7,
                         "linear": 8, "iec61966-2-1": 13, "bt2020-10": 14, "bt2020-12": 15,
                         "smpte2084": 16, "smpte428": 17, "arib-std-b67": 18}
            context.color_primaries = primaries.get(meta["color_primaries"], first.color_primaries)
            context.color_trc = transfers.get(meta["color_transfer"], first.color_trc)
            context.color_range = 2
            matrix_name = command[command.index("-colorspace") + 1]
            context.colorspace = int(matrix_name) if matrix_name.isdigit() else {
                "gbr": 0, "bt709": 1, "fcc": 4, "bt470bg": 5, "smpte170m": 6,
                "smpte240m": 7, "bt2020nc": 9, "bt2020c": 10}.get(matrix_name, 2)
            self.color_info = (context.color_primaries, context.color_trc)
            for packet in encoded.demux(video=0):
                self._check()
                if not packet.size:
                    continue
                if self.max_frames is not None and self.written_frames >= self.max_frames:
                    raise RuntimeError("A rolling cache window exceeded its frame limit.")
                with condition:
                    if not pending:
                        raise RuntimeError("Cache encoder emitted an unexpected packet.")
                    timing = pending.popleft()
                packet.stream = stream
                output.mux(packet)
                self.timings.append(timing)
                self.written_frames += 1
                if self.on_packet:
                    self.on_packet(timing)
            self.writer_thread.join(timeout=30)
            if not writer_done.is_set():
                raise RuntimeError("Rolling cache input did not stop.")
            if failure:
                raise failure[0]
            self.process.wait(timeout=10)
            logger.join(timeout=2)
            self._check()
            if self.process.returncode or pending or not self.written_frames:
                raise RuntimeError("Rolling cache encoder failed:\n" + "\n".join(logs.snapshot()))
            output.close()
            output = None
            self.built = True
            app_log.info("rolling-cache", f"lossless file encoder={self.encoder} frames={self.written_frames} bytes={spool.size}")
        except BaseException as exc:
            self.stop.set()
            if self.controller.cancel.is_set():
                raise Cancelled("Rolling cache stopped.") from exc
            if failure and not isinstance(failure[0], BrokenPipeError):
                raise failure[0] from exc
            if isinstance(exc, (av.error.FFmpegError, OSError)):
                if logger:
                    logger.join(timeout=1)
                raise RuntimeError("Rolling cache encoder failed:\n" + "\n".join(logs.snapshot())) from exc
            raise
        finally:
            if self.process:
                if self.process.poll() is None:
                    self.process.kill()
                self.process.wait(timeout=10)
                self.controller.unregister(self.process)
                if self.writer_thread:
                    self.writer_thread.join(timeout=30)
                if logger:
                    logger.join(timeout=2)
                for pipe in (self.process.stdin, self.process.stdout, self.process.stderr):
                    if pipe:
                        with suppress(OSError):
                            pipe.close()
            else:
                close = getattr(self.frames, "close", None)
                if close:
                    close()
            for container in (output, encoded):
                if container:
                    with suppress(Exception):
                        container.close()
            if spool:
                spool.file.close()
                self.used_bytes = spool.size
            pending.clear()
            failure.clear()
            self.frames = iter(())
            if not self.built:
                self.close()
        return self

    def __iter__(self):
        if self.consumed:
            raise RuntimeError("A rolling cache can only be consumed once.")
        self.consumed = True
        try:
            self.build()
            self._check()
            self.decoder = open_video_decoder(self.path, self.controller, pixel_format=self.decode_format,
                                              preserve_samples=True, threads=self.threads, gpu_threads=1)
            self.decode_backend = self.decoder.decode_backend
            app_log.info("rolling-cache", f"lossless file decode={self.decode_backend} format={self.decode_format}")
            for frame in self.decoder.decode(video=0):
                self._check()
                if frame.is_corrupt or self.read_frames >= len(self.timings):
                    raise RuntimeError("Rolling cache returned an invalid frame.")
                frame.pts, frame.duration, frame.time_base = self.timings[self.read_frames]
                frame.colorspace, frame.color_range = 0, 2
                frame.color_primaries, frame.color_trc = self.color_info
                self.read_frames += 1
                yield frame
            if self.read_frames != self.written_frames:
                raise RuntimeError("Rolling cache frame accounting does not match.")
        finally:
            self.close()

    def close(self):
        if self.closed:
            return
        self.stop.set()
        if self.process and self.process.poll() is None:
            self.process.kill()
            self.process.wait(timeout=10)
        if self.writer_thread and self.writer_thread is not threading.current_thread():
            self.writer_thread.join(timeout=30)
            if self.writer_thread.is_alive():
                raise RuntimeError("Rolling cache producer did not stop.")
        if self.writer_thread is None:
            close = getattr(self.frames, "close", None)
            if close:
                close()
        try:
            if self.decoder:
                self.decoder.close()
                self.decoder = None
        finally:
            self.path.unlink(missing_ok=True)
            self.budget.release(self.used_bytes)
            self.used_bytes = 0
            self.timings.clear()
            self.closed = True
