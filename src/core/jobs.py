from __future__ import annotations

import subprocess
import threading
import time
from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator


class Cancelled(RuntimeError):
    pass


class Paused(RuntimeError):
    """Replay the uncommitted part after all GPU owners have exited."""


class JobController:
    """Own cancellation state and subprocesses for one render."""

    def __init__(self) -> None:
        self.cancel = threading.Event()
        self.pause_requested = threading.Event()
        self.paused = threading.Event()
        self.pause_epoch = 0
        self._resume = threading.Event()
        self._resume.set()
        self.phase = {}
        self.phase_callback = None
        self.ffmpeg_device: str | None = None
        self._lock = threading.Lock()
        self._processes: list[subprocess.Popen] = []

    def register(self, process: subprocess.Popen) -> None:
        with self._lock:
            self._processes.append(process)
            cancelled = self.cancel.is_set() or self.pause_requested.is_set()
        if cancelled and process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass

    def unregister(self, process: subprocess.Popen) -> None:
        with self._lock:
            if process in self._processes:
                self._processes.remove(process)

    def stop(self) -> None:
        self.cancel.set()
        self._resume.set()
        self.terminate_processes()

    def check(self) -> None:
        if self.cancel.is_set():
            raise Cancelled("Render stopped by user.")
        if self.pause_requested.is_set():
            raise Paused("Pausing export; releasing GPU workers.")

    def pause(self) -> None:
        if not self.cancel.is_set() and not self.pause_requested.is_set():
            self.pause_epoch += 1
            self._resume.clear()
            self.pause_requested.set()
            self.terminate_processes()

    def resume(self) -> None:
        self.pause_requested.clear()
        self._resume.set()

    def wait_for_resume(self) -> None:
        # Called only after the part's process tree and buffers are released.
        self.paused.set()
        self.report_phase(state="paused", message="Export paused; GPU memory released",
                          cache_bytes=0, worker_processes=0, fps=0.0)
        try:
            while not self._resume.wait(.2):
                if self.cancel.is_set():
                    raise Cancelled("Render stopped by user.")
            self.check()
        finally:
            self.paused.clear()

    def report_phase(self, **values) -> None:
        self.phase = {**self.phase, **values}
        if self.phase_callback:
            self.phase_callback(dict(self.phase))

    def terminate_processes(self) -> None:
        with self._lock:
            processes = list(self._processes)
        for process in processes:
            if process.poll() is None:
                try:
                    process.terminate()
                except OSError:
                    pass

    def release_processes(self) -> None:
        self.terminate_processes()
        with self._lock:
            processes = list(self._processes)
        for process in processes:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
            close_job = getattr(process, "close_job", None)
            if close_job:
                close_job()
            self.unregister(process)


_RENDER_LOCK = threading.Lock()
_ACTIVE_LOCK = threading.Lock()
_ACTIVE: JobController | None = None
_JOB_CONTEXT = ContextVar("render_job_controller", default=None)


def current_job_controller():
    return _JOB_CONTEXT.get()


def prepare_job(controller, function):
    """Retry disposable preparation after pause without advancing a render."""
    with use_job_controller(controller):
        while True:
            try:
                controller.check()
                return function()
            except Paused:
                controller.release_processes()
                controller.wait_for_resume()


@contextmanager
def use_job_controller(controller: JobController):
    token = _JOB_CONTEXT.set(controller)
    try:
        yield
    finally:
        _JOB_CONTEXT.reset(token)


def capture_process(command, *, timeout=20, controller=None):
    """Own even short codec/device probes so Stop releases their GPU contexts."""
    controller = controller or current_job_controller()
    if controller is not None and controller.cancel.is_set():
        raise Cancelled("Render stopped by user.")
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if controller is not None:
        controller.register(process)
    deadline = time.monotonic() + timeout
    try:
        while True:
            if controller is not None:
                controller.check()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command, timeout)
            try:
                stdout, stderr = process.communicate(timeout=min(.2, remaining))
                break
            except subprocess.TimeoutExpired:
                continue
        if controller is not None and controller.cancel.is_set():
            raise Cancelled("Render stopped by user.")
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
        if controller is not None:
            controller.unregister(process)
        process.stdout.close()
        process.stderr.close()


@contextmanager
def active_job(controller: JobController | None = None) -> Iterator[JobController]:
    """Claim the single GPU render slot and always release its resources."""
    global _ACTIVE
    if not _RENDER_LOCK.acquire(blocking=False):
        raise RuntimeError("Another GPU render is already running.")
    controller = controller or current_job_controller() or JobController()
    with _ACTIVE_LOCK:
        _ACTIVE = controller
    token = _JOB_CONTEXT.set(controller)
    try:
        yield controller
    finally:
        try:
            controller.release_processes()
        finally:
            _JOB_CONTEXT.reset(token)
            with _ACTIVE_LOCK:
                if _ACTIVE is controller:
                    _ACTIVE = None
            _RENDER_LOCK.release()


def cancel_active_job() -> str:
    with _ACTIVE_LOCK:
        controller = _ACTIVE
    if controller is None:
        return "No render is running."
    controller.stop()
    return "Stop requested; incomplete output will be removed and completed batch files retained."


def drain_text(stream, lines: list[str]) -> None:
    for raw in iter(stream.readline, b""):
        lines.append(raw.decode("utf-8", "replace").rstrip())


class BoundedLogBuffer:
    """Keep diagnostic evidence and a bounded tail instead of every frame log."""

    _IMPORTANT = (
        "profile applied",
        "bridge",
        "ngx",
        "stream source",
        "optimal settings",
        "complete:",
        "failed",
        "error",
        "exception",
    )

    def __init__(self, max_tail: int = 500, max_important: int = 100) -> None:
        self._tail: deque[str] = deque(maxlen=max_tail)
        self._important: list[str] = []
        self._max_important = max_important
        self._seen = 0
        self._lock = threading.Lock()

    def append(self, line: str) -> None:
        with self._lock:
            self._seen += 1
            self._tail.append(line)
            lowered = line.casefold()
            if (
                len(self._important) < self._max_important
                and any(marker.casefold() in lowered for marker in self._IMPORTANT)
            ):
                self._important.append(line)

    def snapshot(self) -> list[str]:
        with self._lock:
            important = list(self._important)
            tail = list(self._tail)
        seen: set[str] = set()
        return [line for line in [*important, *tail] if not (line in seen or seen.add(line))]

    @property
    def dropped_lines(self) -> int:
        with self._lock:
            return max(0, self._seen - len(self._tail))


def drain_bounded_text(stream, buffer: BoundedLogBuffer) -> None:
    for raw in iter(stream.readline, b""):
        buffer.append(raw.decode("utf-8", "replace").rstrip())
