from __future__ import annotations

import subprocess
import threading
from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

from .power import keep_system_awake


class Cancelled(RuntimeError):
    pass


class JobController:
    """Own cancellation state and subprocesses for one render."""

    def __init__(self) -> None:
        self.cancel = threading.Event()
        self._lock = threading.Lock()
        self._processes: list[subprocess.Popen] = []

    def register(self, process: subprocess.Popen) -> None:
        with self._lock:
            self._processes.append(process)
            cancelled = self.cancel.is_set()
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
        self.terminate_processes()

    def terminate_processes(self) -> None:
        with self._lock:
            processes = list(self._processes)
        for process in processes:
            if process.poll() is None:
                try:
                    process.terminate()
                except OSError:
                    pass


_RENDER_LOCK = threading.Lock()
_ACTIVE_LOCK = threading.Lock()
_ACTIVE: JobController | None = None
_JOB_CONTEXT = ContextVar("render_job_controller", default=None)


def current_job_controller():
    return _JOB_CONTEXT.get()


@contextmanager
def use_job_controller(controller: JobController):
    token = _JOB_CONTEXT.set(controller)
    try:
        yield
    finally:
        _JOB_CONTEXT.reset(token)


@contextmanager
def active_job(controller: JobController | None = None) -> Iterator[JobController]:
    """Claim the single GPU render slot and always release its resources."""
    global _ACTIVE
    if not _RENDER_LOCK.acquire(blocking=False):
        raise RuntimeError("Another GPU render is already running.")
    controller = controller or current_job_controller() or JobController()
    with _ACTIVE_LOCK:
        _ACTIVE = controller
    try:
        with keep_system_awake():
            yield controller
    finally:
        controller.terminate_processes()
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
