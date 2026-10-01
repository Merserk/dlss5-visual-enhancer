from __future__ import annotations

import subprocess
from .vulkan import prepare_command
from pathlib import Path

from .. import app_log
from ..paths import FFMPEG

LOSSLESS_PREVIEW_CODEC = "FFV1 Lossless RGB 10-bit"


def decode_timeline_frame(source: str | Path, metadata: dict, *,
                          start_seconds: float = 0.0, controller=None):
    """Seek to one timeline frame and return its RGBA samples in memory."""
    import numpy as np
    from ..jobs import Cancelled, current_job_controller

    controller = controller or current_job_controller()
    if controller is not None and controller.cancel.is_set():
        raise Cancelled("Preview cancelled.")
    width, height = int(metadata["width"]), int(metadata["height"])
    high_depth = int(metadata.get("depth") or 8) > 8
    pixel_format = "rgba64le" if high_depth else "rgba"
    start = max(0.0, float(start_seconds))
    aligned = max(0.0, start - 0.002)
    fast = max(0.0, aligned - 2.0)
    accurate = max(0.0, aligned - fast)
    command = [str(FFMPEG), "-hide_banner", "-loglevel", "error", "-xerror",
               "-noautorotate", "-ss", f"{fast:.6f}", "-i", str(source),
               "-ss", f"{accurate:.6f}", "-map", "0:v:0", "-frames:v", "1",
               "-an", "-sn", "-dn", "-fps_mode", "passthrough",
               "-f", "rawvideo", "-pix_fmt", pixel_format, "pipe:1"]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if controller is not None:
        controller.register(process)
    try:
        while True:
            if controller is not None and controller.cancel.is_set():
                raise Cancelled("Preview cancelled.")
            try:
                pixels, error = process.communicate(timeout=.2)
                break
            except subprocess.TimeoutExpired:
                continue
        if process.returncode:
            raise RuntimeError("Timeline frame decoding failed: " + error.decode("utf-8", "replace")[-2000:])
        dtype = "<u2" if high_depth else np.uint8
        if len(pixels) != width * height * 4 * (2 if high_depth else 1):
            raise RuntimeError("Timeline frame decoding returned unexpected dimensions.")
        return np.frombuffer(pixels, dtype=dtype).reshape(height, width, 4).copy()
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()
        if controller is not None:
            controller.unregister(process)
        process.stdout.close()
        process.stderr.close()


def tone_map_hdr_preview_frame(pixels, *, controller=None):
    """Show one processed PQ frame through the established HDR display mapping."""
    import tempfile

    import numpy as np

    from ..jobs import Cancelled, current_job_controller
    from ..paths import JOBS

    controller = controller or current_job_controller()
    if (pixels.ndim != 3 or pixels.shape[2] != 4 or pixels.dtype != np.uint16):
        raise ValueError("HDR preview requires a 16-bit RGBA frame.")
    if controller is not None and controller.cancel.is_set():
        raise Cancelled("Preview cancelled.")
    height, width = pixels.shape[:2]
    JOBS.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="visual-hdr-frame-", dir=JOBS) as temp:
        output = Path(temp) / "frame.mkv"
        command = [
            str(FFMPEG), "-hide_banner", "-loglevel", "error", "-y",
            "-f", "rawvideo", "-pixel_format", "rgba64le",
            "-video_size", f"{width}x{height}", "-framerate", "25", "-i", "pipe:0",
            "-frames:v", "1", "-vf",
            "format=gbrp10le,setparams=colorspace=gbr:range=full:"
            "color_primaries=bt2020:color_trc=smpte2084",
            "-c:v", "ffv1", "-level", "3", "-slicecrc", "1",
            "-pix_fmt", "gbrp10le", "-colorspace", "0", "-color_range", "pc",
            "-color_primaries", "bt2020", "-color_trc", "smpte2084",
            str(output),
        ]
        process = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if controller is not None:
            controller.register(process)
        try:
            payload = np.ascontiguousarray(pixels).tobytes()
            try:
                _, error = process.communicate(input=payload, timeout=.2)
            except subprocess.TimeoutExpired:
                while True:
                    if controller is not None and controller.cancel.is_set():
                        raise Cancelled("Preview cancelled.")
                    try:
                        _, error = process.communicate(timeout=.2)
                        break
                    except subprocess.TimeoutExpired:
                        continue
            if controller is not None and controller.cancel.is_set():
                raise Cancelled("Preview cancelled.")
            if process.returncode:
                raise RuntimeError("HDR preview encoding failed: " +
                                   error.decode("utf-8", "replace")[-2000:])
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()
            if controller is not None:
                controller.unregister(process)
            process.stdin.close()
            process.stderr.close()
        return decode_preview_frame(output, controller=controller)


def extract_preview_subclip(
    source: str | Path,
    dest_dir: str | Path | None = None,
    *,
    start_seconds: float = 0.0,
    length_seconds: float | None = None,
    single_frame: bool = False,
    controller=None,
) -> str:
    """Extract a frame-quantized subclip starting at ``start_seconds``.

    Callers pass the *nominal frame start* ``(N-1)/fps`` matching the timeline
    counter ``floor(position*fps)+1``. A raw playhead anywhere inside frame N
    would otherwise select N+1, because ``-ss`` emits the first frame with
    ``pts >= START``. A 2 ms epsilon absorbs ``%.6f`` rounding on
    non-terminating rates (23.976/29.97) so the seek lands on N, not N+1.
    Accurate output seeking (``-i`` then ``-ss``) is used with a 2 s fast
    pre-seek so long 4K files stay fast while remaining frame-exact.
    Returns the temp clip path as string. Caller owns cleanup.
    """
    from ..disk_paths import OutputFile
    from ..jobs import current_job_controller

    controller = controller or current_job_controller()
    src = Path(source)
    if not src.is_file():
        raise FileNotFoundError(src)
    try:
        start = max(0.0, float(start_seconds or 0.0))
    except (TypeError, ValueError):
        start = 0.0
    if start <= 0 and not single_frame and not length_seconds:
        return str(src)
    from .probe import probe_video
    from ..jobs import Cancelled
    if controller is not None and controller.cancel.is_set():
        raise Cancelled("Preview cancelled.")
    metadata = probe_video(src, count_mode="metadata", controller=controller)
    # Epsilon: land just inside the intended frame's leading edge.
    aligned = max(0.0, start - 0.002)
    fast = max(0.0, aligned - 2.0)
    accurate = max(0.0, aligned - fast)
    from ..paths import OUTPUTS
    out_dir = Path(dest_dir) if dest_dir is not None else OUTPUTS
    out_dir.mkdir(parents=True, exist_ok=True)
    suffix = "_PLAYHEADFRAME.mkv" if single_frame else "_PLAYHEAD.mkv"
    dest = out_dir / f"{src.stem}{suffix}"
    counter = 1
    while dest.exists():
        counter += 1
        dest = out_dir / f"{src.stem}{suffix.replace('.mkv', f'_{counter}.mkv')}"
    output_file = OutputFile(dest)
    command = [
        str(FFMPEG), "-hide_banner", "-loglevel", "warning", "-y",
        "-ss", f"{fast:.6f}", "-i", str(src),
        "-ss", f"{accurate:.6f}",
    ]
    if single_frame:
        command += ["-frames:v", "1"]
    elif length_seconds is not None and float(length_seconds) > 0:
        command += ["-t", f"{float(length_seconds):.6f}"]
    color_args = []
    for key, flag in (("color_primaries", "-color_primaries"),
                      ("color_transfer", "-color_trc")):
        value = metadata.get(key)
        if value and value != "unknown":
            color_args.extend((flag, str(value)))
    command += [
        "-map", "0:v:0",
        "-an", "-map_metadata", "0",
        "-c:v", "ffv1", "-level", "3", "-slicecrc", "1",
        "-pix_fmt", "gbrp10le", "-colorspace", "0", "-color_range", "pc",
        *color_args,
        str(output_file.temporary),
    ]
    process = None
    try:
        command = prepare_command(
            command, selection=getattr(controller, "ffmpeg_device", None))
        process = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if controller is not None:
            controller.register(process)
        _stdout, stderr = process.communicate()
        if controller is not None and controller.cancel.is_set():
            raise Cancelled("Preview cancelled.")
        if process.returncode:
            app_log.error("ffmpeg-preview", "playhead subclip failed", (stderr or "")[-500:])
            raise RuntimeError("Playhead subclip failed:\n" + (stderr or "")[-2000:])
        if not Path(output_file.temporary).is_file():
            raise RuntimeError("Playhead subclip produced no file.")
        output_file.publish()
    finally:
        if process is not None:
            if process.poll() is None:
                process.terminate()
                process.communicate()
            if controller is not None:
                controller.unregister(process)
        output_file.cleanup()
    return str(dest)


def decode_preview_frame(source: str | Path, *, controller=None):
    """Return a full-resolution 16-bit RGBA still without a video encode.

    HDR is mapped to the display's SDR image surface only. The cached clip
    retains its original samples and HDR signaling for video playback.
    """
    import numpy as np
    from ..jobs import Cancelled, current_job_controller
    from .probe import probe_video

    controller = controller or current_job_controller()
    if controller is not None and controller.cancel.is_set():
        raise Cancelled("Preview cancelled.")
    metadata = probe_video(source, count_mode="metadata", controller=controller)
    filters = []
    if metadata.get("hdr"):
        filters.append(
            "zscale=transfer=linear:npl=100,format=gbrpf32le,"
            "zscale=primaries=bt709,tonemap=mobius:desat=2,"
            "zscale=transfer=bt709:matrix=gbr:range=full"
        )
    command = [str(FFMPEG), "-hide_banner", "-loglevel", "error",
               "-i", str(source), "-map", "0:v:0", "-frames:v", "1", "-an",
               *(["-vf", ",".join(filters)] if filters else []),
               "-f", "rawvideo", "-pix_fmt", "rgba64le", "pipe:1"]
    command = prepare_command(command, selection=getattr(controller, "ffmpeg_device", None))
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if controller is not None:
        controller.register(process)
    try:
        pixels, error = process.communicate()
        if controller is not None and controller.cancel.is_set():
            raise Cancelled("Preview cancelled.")
        if process.returncode:
            raise RuntimeError("Preview frame decoding failed: " + error.decode("utf-8", "replace")[-2000:])
        width, height = int(metadata["width"]), int(metadata["height"])
        if len(pixels) != width * height * 8:
            raise RuntimeError("Preview frame has unexpected dimensions.")
        return np.frombuffer(pixels, dtype="<u2").reshape(height, width, 4)
    finally:
        if process.poll() is None:
            process.terminate()
            process.communicate()
        if controller is not None:
            controller.unregister(process)


def grab_video_poster_jpeg(
    source: str | Path,
    *,
    seconds: float = 0.0,
    max_width: int = 960,
    controller=None,
    timeout: float = 30.0,
) -> bytes:
    """Extract a bounded, cancellable software-decoded still for the UI."""
    import math
    import time
    from ..jobs import Cancelled, current_job_controller

    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("Poster timeout must be finite and positive.")
    controller = controller or current_job_controller()
    if controller is not None and controller.cancel.is_set():
        raise Cancelled("Media loading cancelled.")
    src = Path(source)
    if not src.is_file():
        raise FileNotFoundError(src)
    try:
        start = max(0.0, float(seconds or 0.0))
    except (TypeError, ValueError):
        start = 0.0
    aligned = max(0.0, start - 0.002)
    fast = max(0.0, aligned - 2.0)
    accurate = max(0.0, aligned - fast)
    command = [
        str(FFMPEG), "-hide_banner", "-loglevel", "error", "-nostdin",
        "-filter_threads", "1", "-threads", "2", "-hwaccel", "none",
        "-ss", f"{fast:.6f}", "-i", str(src),
        "-ss", f"{accurate:.6f}", "-map", "0:v:0", "-frames:v", "1",
        "-an", "-sn", "-dn", "-threads", "1",
        *([ "-vf", f"scale={int(max_width)}:-2"] if max_width and max_width > 0 else []),
        "-q:v", "3", "-f", "mjpeg", "pipe:1",
    ]
    # A list thumbnail must not initialize Vulkan codecs/filters or contend
    # with Qt's video decoder and AI rendering for GPU driver resources.
    deadline = time.monotonic() + timeout
    process = subprocess.Popen(
        command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if controller is not None:
        controller.register(process)
    try:
        while True:
            if controller is not None and controller.cancel.is_set():
                raise Cancelled("Media loading cancelled.")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"Poster extraction timed out after {timeout:g} seconds.")
            try:
                out, err = process.communicate(timeout=min(0.2, remaining))
                break
            except subprocess.TimeoutExpired:
                continue
        if controller is not None and controller.cancel.is_set():
            raise Cancelled("Media loading cancelled.")
        if process.returncode or not out:
            detail = err.decode("utf-8", "replace")[-500:]
            raise RuntimeError(f"Poster extraction failed: {detail}")
        return bytes(out)
    finally:
        try:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=5)
        finally:
            if controller is not None:
                controller.unregister(process)
            process.stdout.close()
            process.stderr.close()
