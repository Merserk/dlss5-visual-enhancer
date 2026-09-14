"""Compact real-time session log. Single writer, one line per event.

Every line is flushed immediately so the log is always current. Only short
compact entries are written: job start/done/cancelled/failed plus ffmpeg,
mpv, worker and startup errors with a bounded tail. Full details go to a
small ``.err`` file only on failure (see :func:`fail`).
"""
from __future__ import annotations

import atexit
import io
import re
import threading
import time
from pathlib import Path

from .paths import LOGS

_lock = threading.Lock()
_handle: io.TextIOWrapper | None = None
_session_path: Path | None = None

MAX_MSG = 300
MAX_TAIL = 500
RETENTION_DAYS = 14


def _close_handle() -> None:
    """Flush and close the process log without trying to write another event."""
    global _handle
    with _lock:
        handle, _handle = _handle, None
    if handle is not None:
        try:
            handle.close()
        except OSError:
            pass


atexit.register(_close_handle)


def _compact(text: object, limit: int) -> str:
    single = re.sub(r"\s+", " ", str(text or "")).strip()
    return single if len(single) <= limit else single[:limit] + "..."


def _redact(text: str) -> str:
    return re.sub(r"(https?://[^\s?]+)\?[^\s'\"]+", r"\1?<redacted>", text)


def sweep_old_sessions(days: int = RETENTION_DAYS) -> None:
    try:
        cutoff = time.time() - days * 86400
        for path in LOGS.glob("app-*.log"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink(missing_ok=True)
            except OSError:
                pass
        for path in LOGS.glob("*.err"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink(missing_ok=True)
            except OSError:
                pass
    except OSError:
        pass


def init_session() -> Path:
    """Create ``logs/app-<stamp>.log`` and make it the live session log."""
    global _handle, _session_path
    with _lock:
        if _session_path is not None:
            return _session_path
        LOGS.mkdir(parents=True, exist_ok=True)
        sweep_old_sessions()
        stamp = time.strftime("%Y%m%d-%H%M%S")
        _session_path = LOGS / f"app-{stamp}.log"
        _handle = open(_session_path, "a", encoding="utf-8", errors="replace")
        try:
            (_session_path.with_name("latest.txt")).write_text(
                _session_path.name, encoding="utf-8"
            )
        except OSError:
            pass
    info("app", "start")
    return _session_path


def session_path() -> str:
    with _lock:
        if _session_path is not None:
            return str(_session_path)
    return str(init_session())


def _write(level: str, tag: str, msg: str) -> None:
    global _handle
    line = f"[{time.strftime('%H:%M:%S')}] {level} {tag}: {msg}\n"
    with _lock:
        handle = _handle
    if handle is None:
        try:
            # init_session owns creation of the one process-wide handle. Opening
            # the returned path again here replaced that handle without closing
            # it whenever the first log call initialized the session.
            init_session()
        except OSError:
            return
        with _lock:
            handle = _handle
        if handle is None:
            return
    try:
        with _lock:
            handle.write(line)
            handle.flush()
    except (OSError, ValueError):
        pass


def info(tag: str, msg: object) -> None:
    _write("INFO", tag, _compact(_redact(str(msg)), MAX_MSG))


def error(tag: str, msg: object, tail: object = "") -> None:
    text = _compact(_redact(str(msg)), MAX_MSG)
    tail_text = _compact(_redact(str(tail)), MAX_TAIL)
    _write("ERROR", tag, f"{text} | tail={tail_text}" if tail_text else text)


def fail(tag: str, stem: str, err: object, tails: dict[str, object] | None = None) -> Path:
    """Write a small ``.err`` file and log one compact line. Returns the path."""
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(stem)).strip("-") or "job"
    path = LOGS / f"{safe}.err"
    try:
        LOGS.mkdir(parents=True, exist_ok=True)
        lines = [f"error: {err}"]
        for name, tail in (tails or {}).items():
            if isinstance(tail, (list, tuple)):
                tail = "\n".join(str(line) for line in tail[-40:])
            lines.append(f"--- {name} ---")
            lines.append(str(tail)[-4000:] or "(empty)")
        path.write_text("\n".join(lines), encoding="utf-8", errors="replace")
    except OSError:
        pass
    tail_text = ""
    if tails:
        first = next(iter(tails.values()), "")
        if isinstance(first, (list, tuple)):
            first = "\n".join(str(line) for line in first[-12:])
        tail_text = first
    error(tag, f"{err} | err={path.name}", tail_text)
    return path


class SessionStream:
    """Minimal stdout/stderr replacement writing raw lines to the session log."""

    def __init__(self) -> None:
        self.encoding = "utf-8"
        self.errors = "replace"

    def write(self, s: str) -> int:
        if not s or not s.strip():
            return len(s)
        with _lock:
            handle = _handle
        if handle is None:
            try:
                init_session()
            except OSError:
                return len(s)
            with _lock:
                handle = _handle
        try:
            with _lock:
                if handle is not None:
                    handle.write(s if s.endswith("\n") else s + "\n")
                    handle.flush()
        except (OSError, ValueError):
            pass
        return len(s)

    def writelines(self, lines) -> None:
        for line in lines:
            self.write(line)

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        return False

    def fileno(self):
        raise io.UnsupportedOperation("SessionStream has no fileno")

    def reconfigure(self, *args, **kwargs) -> None:
        if "encoding" in kwargs:
            self.encoding = kwargs["encoding"]
        if "errors" in kwargs:
            self.errors = kwargs["errors"]

    @property
    def buffer(self):
        raise io.UnsupportedOperation("SessionStream has no buffer")

    def readable(self) -> bool:
        return False

    def writable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return False

    @property
    def closed(self) -> bool:
        return False

    def close(self) -> None:
        pass
