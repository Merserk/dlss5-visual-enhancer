"""Bounded binary pipes for native video frame processes."""
from __future__ import annotations

import os
import subprocess


FRAME_PIPE_BYTES = 512 * 1024


def _windows_frame_pipe():
    import _winapi
    import msvcrt

    handles = list(_winapi.CreatePipe(None, FRAME_PIPE_BYTES))
    streams = []
    try:
        for index, mode, flag in ((0, "rb", os.O_RDONLY), (1, "wb", os.O_WRONLY)):
            descriptor = msvcrt.open_osfhandle(handles[index], flag | os.O_BINARY)
            handles[index] = None  # The descriptor now owns the native handle.
            try:
                streams.append(os.fdopen(descriptor, mode))
            except BaseException:
                os.close(descriptor)
                raise
        return streams
    except BaseException:
        for stream in streams:
            stream.close()
        raise
    finally:
        for handle in handles:
            if handle is not None:
                _winapi.CloseHandle(handle)


def spawn_frame_process(command, *, stdin=None, stdout=None, **options):
    """Keep frame transfers out of Windows' small default anonymous pipe.

    Each direction requests a 512 KiB host buffer. Backpressure
    and normal Popen process ownership remain intact. Popen duplicates only its
    selected child handles; the temporary child ends close before returning.
    """
    if os.name != "nt":
        return subprocess.Popen(command, stdin=stdin, stdout=stdout, **options)
    if options.get("text") or options.get("universal_newlines"):
        raise ValueError("Video frame process pipes require binary transport.")
    children, parents = [], {}
    try:
        selected = {"stdin": stdin, "stdout": stdout}
        for name, value in selected.items():
            if value == subprocess.PIPE:
                reader, writer = _windows_frame_pipe()
                child, parent = (reader, writer) if name == "stdin" else (writer, reader)
                children.append(child)
                parents[name] = parent
                selected[name] = child
        process = subprocess.Popen(command, **selected, **options)
        for name, stream in parents.items():
            setattr(process, name, stream)
        parents.clear()
        return process
    finally:
        for stream in children + list(parents.values()):
            stream.close()
