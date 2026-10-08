"""Identify crashed rolling owners before reclaiming their private job files."""
from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path

MARKER = "rolling-owner.json"


def process_identity(pid):
    """Creation FILETIME prevents confusing a reused PID with an active owner."""
    if int(pid) <= 0:
        return "unknown"
    if os.name != "nt":
        try:
            os.kill(int(pid), 0)
            return "alive"
        except ProcessLookupError:
            return None
        except PermissionError:
            return "unknown"
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes, kernel.OpenProcess.restype = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32], ctypes.c_void_p
    kernel.GetProcessTimes.argtypes = [ctypes.c_void_p, *([ctypes.POINTER(ctypes.c_uint64)] * 4)]
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel.OpenProcess(0x1000, False, int(pid))
    if not handle:
        return None if ctypes.get_last_error() == 87 else "unknown"
    try:
        values = [ctypes.c_uint64() for _ in range(4)]
        if not kernel.GetProcessTimes(handle, *(ctypes.byref(v) for v in values)):
            return "unknown"
        return str(values[0].value)
    finally:
        kernel.CloseHandle(handle)


def mark_owner(directory):
    pid = os.getpid()
    marker = Path(directory) / MARKER
    marker.write_text(json.dumps(dict(kind="rolling-passes-v3", pid=pid, identity=process_identity(pid))), encoding="utf-8")
    return marker


def _rolling_owners(root):
    root = Path(root).resolve()
    if not root.is_dir():
        return
    for entry in root.glob("visual-workflow-*"):
        if entry.is_symlink() or not entry.is_dir() or entry.resolve().parent != root:
            continue
        marker = entry / MARKER
        if marker.is_symlink() or not marker.is_file():
            continue
        try:
            owner = json.loads(marker.read_text(encoding="utf-8"))
            if owner.get("kind") != "rolling-passes-v3" or int(owner["pid"]) <= 0:
                continue
            current = process_identity(int(owner["pid"]))
            previous = owner.get("identity")
            stale = current is None or (current != "unknown" and
                isinstance(previous, str) and previous not in {"", "unknown"} and current != previous)
            yield entry, stale
        except (OSError, ValueError, KeyError, TypeError):
            continue


def stale_rolling_directories(root):
    """Only app-created workflow roots with a confirmed dead owner qualify."""
    for directory, stale in _rolling_owners(root):
        if stale:
            yield directory


def protected_rolling_names(root):
    """An old paused job remains live; an inaccessible owner is also protected."""
    return frozenset(directory.name for directory, _stale in _rolling_owners(root))
