"""Disposable native stage processes, with bounded timestamped frame transport.

A worker's Windows job owns its FFmpeg children too. Cancellation, an NGX
timeout, or a failed producer therefore cannot leave a GPU process behind.
The input handshake happens after job assignment, before any GPU work starts.
"""
from __future__ import annotations

import itertools
import json
import os
import subprocess
import sys
import threading
import time
from contextlib import suppress
from dataclasses import asdict
from fractions import Fraction
from pathlib import Path


class StageProcess:
    def __init__(self, request, directory):
        from ..core.frame_process import spawn_frame_process
        self._job = None
        self._kernel = None
        self.process = spawn_frame_process(
            [sys.executable, "-u", "-B", str(Path(__file__).resolve()), str(request)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=directory, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            start_new_session=os.name != "nt")
        self.stdin, self.stdout, self.stderr = self.process.stdin, self.process.stdout, self.process.stderr
        self.pid = self.process.pid
        try:
            if os.name == "nt":
                self._assign_job()
        except BaseException:
            self.process.kill()
            self.process.wait(timeout=10)
            for pipe in (self.stdin, self.stdout, self.stderr):
                pipe.close()
            self.close_job()
            raise

    def _assign_job(self):
        import ctypes as ct
        from ctypes import wintypes as wt

        class BasicLimits(ct.Structure):
            _fields_ = [("process_time", ct.c_int64), ("job_time", ct.c_int64),
                        ("flags", wt.DWORD), ("minimum", ct.c_size_t), ("maximum", ct.c_size_t),
                        ("active_processes", wt.DWORD), ("affinity", ct.c_size_t),
                        ("priority", wt.DWORD), ("scheduling", wt.DWORD)]

        class ExtendedLimits(ct.Structure):
            _fields_ = [("basic", BasicLimits), ("io", ct.c_uint64 * 6),
                        ("process_memory", ct.c_size_t), ("job_memory", ct.c_size_t),
                        ("peak_process", ct.c_size_t), ("peak_job", ct.c_size_t)]

        kernel = self._kernel = ct.WinDLL("kernel32", use_last_error=True)
        kernel.CreateJobObjectW.argtypes, kernel.CreateJobObjectW.restype = [ct.c_void_p, wt.LPCWSTR], wt.HANDLE
        kernel.SetInformationJobObject.argtypes = [wt.HANDLE, ct.c_int, ct.c_void_p, wt.DWORD]
        kernel.SetInformationJobObject.restype = wt.BOOL
        kernel.AssignProcessToJobObject.argtypes, kernel.AssignProcessToJobObject.restype = [wt.HANDLE, wt.HANDLE], wt.BOOL
        kernel.TerminateJobObject.argtypes, kernel.TerminateJobObject.restype = [wt.HANDLE, wt.UINT], wt.BOOL
        kernel.QueryInformationJobObject.argtypes = [wt.HANDLE, ct.c_int, ct.c_void_p, wt.DWORD, ct.c_void_p]
        kernel.QueryInformationJobObject.restype = wt.BOOL
        kernel.CloseHandle.argtypes, kernel.CloseHandle.restype = [wt.HANDLE], wt.BOOL
        self._job = kernel.CreateJobObjectW(None, None)
        if not self._job:
            raise ct.WinError(ct.get_last_error())
        limits = ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel.SetInformationJobObject(self._job, 9, ct.byref(limits), ct.sizeof(limits)):
            raise ct.WinError(ct.get_last_error())
        if not kernel.AssignProcessToJobObject(self._job, int(self.process._handle)):
            raise ct.WinError(ct.get_last_error())

    @property
    def returncode(self):
        return self.process.returncode

    def poll(self):
        return self.process.poll()

    def wait(self, timeout=None):
        started = time.monotonic()
        code = self.process.wait(timeout=timeout)
        # Root exit alone is insufficient: codecs/probes can have GPU children.
        while self.active_processes:
            self.kill()
            if timeout is not None and time.monotonic() - started >= timeout:
                raise subprocess.TimeoutExpired(self.process.args, timeout)
            time.sleep(.01)
        return code

    @property
    def active_processes(self):
        if not self._job:
            return int(self.poll() is None)
        import ctypes as ct
        # JOBOBJECT_BASIC_ACCOUNTING_INFORMATION: four LARGE_INTEGER + four DWORD.
        values = (ct.c_uint64 * 6)()
        if not self._kernel.QueryInformationJobObject(self._job, 1, values, ct.sizeof(values), None):
            raise ct.WinError(ct.get_last_error())
        return int(ct.cast(values, ct.POINTER(ct.c_uint32))[10])

    def kill(self):
        if self._job:
            self._kernel.TerminateJobObject(self._job, 1)
        elif os.name != "nt":
            import signal
            with suppress(ProcessLookupError):
                os.killpg(self.pid, signal.SIGKILL)
        elif self.poll() is None:
            self.process.kill()

    terminate = kill

    def close_job(self):
        if self._job:
            self.kill()
            self.wait(timeout=10)
            self._kernel.CloseHandle(self._job)
            self._job = None
        if os.name == "nt" and self.poll() is not None:
            self.process._handle.Close()


def stage_frames(stage, frames, before, after, settings, controller, directory, *,
                 window_end=None, last_window=True, phase_origin=0, window_descriptor=None,
                 window_start=Fraction(0), window_max_end=None):
    """Run one native stage; the process must exit before this generator ends."""
    import av
    from ..core import app_log
    from ..core.ffmpeg.frames import _Reader, packed_frame
    from ..core.ffmpeg.nut import RawVideoPacketMuxer
    from ..core.jobs import BoundedLogBuffer, Cancelled, drain_bounded_text
    from ..core.rolling_cache import forget_traceback
    from .rolling_workflow import _close_process

    frames = iter(frames)
    process = container = writer = logger = None
    failure = []
    logs = BoundedLogBuffer(max_tail=70)
    request = Path(directory) / "stage-request.json"
    complete = False
    try:
        controller.check()
        first = next(frames)
        payload = {"stage": stage, "before": asdict(before), "settings": asdict(settings),
                   "input_format": first.format.name, "window_end": window_end,
                   "last_window": last_window, "phase_origin": phase_origin,
                   "window_descriptor": window_descriptor, "window_start": window_start,
                   "window_max_end": window_max_end}
        request.write_text(json.dumps(payload, default=str), encoding="utf-8")
        process = StageProcess(request, directory)
        controller.register(process)
        app_log.info("rolling-cache", f"model load stage={stage} pid={process.pid}")
        logger = threading.Thread(target=drain_bounded_text, args=(process.stderr, logs), daemon=True)
        logger.start()

        def write():
            muxer = None
            try:
                controller.check()
                process.stdin.write(b"\x01")
                process.stdin.flush()
                muxer = RawVideoPacketMuxer(process.stdin, width=first.width, height=first.height,
                                           rate=before.rate, time_base=first.time_base, pix_fmt=first.format.name)
                for frame in itertools.chain((first,), frames):
                    controller.check()
                    pts = round(Fraction(frame.pts) * frame.time_base / muxer.time_base)
                    duration = round(Fraction(frame.duration or 0) * frame.time_base / muxer.time_base)
                    muxer.write(packed_frame(frame), pts, duration)
                muxer.close()
                muxer = None
                process.stdin.close()
            except BaseException as exc:
                forget_traceback(exc)
                failure.append(exc)
                process.kill()
            finally:
                if muxer:
                    with suppress(Exception):
                        muxer.close()
                close = getattr(frames, "close", None)
                if close:
                    try:
                        close()
                    except BaseException as exc:
                        forget_traceback(exc)
                        failure.append(exc)

        writer = threading.Thread(target=write, name="rolling-model-input", daemon=True)
        writer.start()
        container = av.open(_Reader(process.stdout), format="nut")
        for frame in container.decode(video=0):
            controller.check()
            if frame.is_corrupt or (frame.width, frame.height) != (after.width, after.height):
                raise RuntimeError("A model worker returned an invalid frame.")
            yield frame
        writer.join(timeout=30)
        if writer.is_alive():
            raise RuntimeError("Model worker input did not stop.")
        if failure and not isinstance(failure[0], BrokenPipeError):
            raise failure[0]
        process.wait(timeout=30)
        logger.join(timeout=2)
        controller.check()
        if process.returncode:
            raise RuntimeError(f"{stage} worker failed:\n" + "\n".join(logs.snapshot()))
        complete = True
    except (av.error.FFmpegError, OSError) as exc:
        if controller.cancel.is_set():
            raise Cancelled("Video processing stopped.") from exc
        if logger:
            logger.join(timeout=1)
        if failure and not isinstance(failure[0], BrokenPipeError):
            raise failure[0] from exc
        raise RuntimeError(f"{stage} worker failed:\n" + "\n".join(logs.snapshot())) from exc
    finally:
        if process:
            if not complete:
                process.kill()
            try:
                _close_process(process, controller, logger)
            finally:
                process.close_job()
            if writer:
                writer.join(timeout=30)
                if writer.is_alive():
                    raise RuntimeError("Model worker input did not stop during cleanup.")
            app_log.info("rolling-cache", f"model unloaded stage={stage} pid={process.pid}")
        else:
            close = getattr(frames, "close", None)
            if close:
                close()
        if container:
            container.close()
        request.unlink(missing_ok=True)
        failure.clear()


def _main():
    # A script entry point works with the portable interpreter's isolated path.
    root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(root))
    from src.portable import configure_portable_environment
    configure_portable_environment()
    import av
    from src.core.ffmpeg.frames import _Reader, packed_frame
    from src.core.ffmpeg.nut import RawVideoPacketMuxer
    from src.core.jobs import JobController
    from src.desktop.rolling_workflow import VideoState, _Control, native_stage
    from src.settings.models import UISettings

    output_pipe = sys.stdout.buffer
    sys.stdout = sys.stderr  # Python diagnostics cannot corrupt the binary pipe.
    if sys.stdin.buffer.read(1) != b"\x01":
        raise RuntimeError("Model worker did not receive its start handshake.")
    payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    if payload.get("protocol") == "descriptor-v2":
        os.environ["VE_STAGE_WORKER"] = "1"
        from src.desktop.rolling_pass_worker import process_request
        def emit(event):
            output_pipe.write((json.dumps(event, default=str) + "\n").encode("utf-8"))
            output_pipe.flush()
        process_request(payload, Path(sys.argv[1]).parent, emit)
        return
    values = payload["before"]
    values["rate"] = Fraction(values["rate"])
    before = VideoState(**values)
    settings = UISettings(**payload["settings"])
    owner = JobController()
    owner.ffmpeg_device = settings.ffmpeg_device
    controller = _Control(owner)
    with av.open(_Reader(sys.stdin.buffer), format="nut") as source:
        stream = source.streams.video[0]
        stream.codec_context.codec_tag = "\0\0\0\0"
        stream.codec_context.pix_fmt = payload["input_format"]
        frames, after = native_stage(payload["stage"], source.decode(video=0), before, settings,
                                     controller, Path(sys.argv[1]).parent, normalize_output=False,
                                     window_end=Fraction(payload["window_end"]) if payload["window_end"] else None,
                                     last_window=payload["last_window"], phase_origin=int(payload["phase_origin"]),
                                     window_descriptor=payload["window_descriptor"], window_start=Fraction(payload["window_start"]),
                                     window_max_end=Fraction(payload["window_max_end"]) if payload["window_max_end"] else None)
        muxer = None
        try:
            for frame in frames:
                if muxer is None:
                    muxer = RawVideoPacketMuxer(output_pipe, width=frame.width, height=frame.height,
                                               rate=after.rate, time_base=frame.time_base, pix_fmt=frame.format.name)
                muxer.write(packed_frame(frame), round(Fraction(frame.pts) * frame.time_base / muxer.time_base),
                            round(Fraction(frame.duration or 0) * frame.time_base / muxer.time_base))
            if muxer is None:
                raise RuntimeError("Model worker produced no frames.")
            muxer.close()
            muxer = None
            output_pipe.flush()
        finally:
            frames.close()
            if muxer:
                muxer.close()


if __name__ == "__main__":
    try:
        _main()
    except BaseException:
        import traceback
        traceback.print_exc(file=sys.stderr)
        sys.stderr.flush()
        os._exit(1)
    sys.stderr.flush()
    # OS teardown also releases process-lifetime NGX/device caches. Avoid
    # unloading proprietary DLLs through Python's atexit hooks (which can hang).
    os._exit(0)
