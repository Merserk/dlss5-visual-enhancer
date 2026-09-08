from __future__ import annotations

import os
import subprocess
from pathlib import Path

from ..paths import FFMPEG
from .codecs import _base_codec

PREVIEW_ENCODING_CHOICES = ("Auto", "Always H.264", "Disabled")
DEFAULT_PREVIEW_ENCODING = "Auto"
FAST_PREVIEW_NVENC_ENV = "DLSS5_FAST_PREVIEW_NVENC"


def _fast_preview_nvenc_enabled() -> bool:
    value = os.environ.get(FAST_PREVIEW_NVENC_ENV, "").strip().lower()
    return value in {"1", "true", "yes", "on", "nvenc"}


def _compat_h264_codec() -> str:
    # The RTX 4090 launcher opts into this. Generic start.bat retains the
    # previous CPU H.264 fallback for maximum compatibility.
    return "H.264 (NVIDIA NVENC)" if _fast_preview_nvenc_enabled() else "H.264"


def normalize_preview_encoding(value: object) -> str:
    """Return a valid preview-encoding mode, defaulting to Auto."""
    if isinstance(value, str) and value.strip() in PREVIEW_ENCODING_CHOICES:
        return value.strip()
    return DEFAULT_PREVIEW_ENCODING


def is_user_playable_request(codec: str, container: str) -> bool:
    """Pre-check whether the requested encode settings are browser-playable.

    Strict definition: MP4 container + H.264 base codec (plain or NVENC).
    Used to pick the encode path without paying for a probe first.
    """
    try:
        if container != "MP4":
            return False
        return _base_codec(codec) == "H.264"
    except Exception:
        return False


def resolve_preview_codec(
    requested_codec: str, requested_container: str, mode: object
) -> tuple[str, str]:
    """Return the (codec, container) to encode a truncated preview with.

    The RTX 4090 launcher can opt the compatibility preview into H.264 NVENC.
    It remains the same browser-compatible H.264/MP4 format; only the encoder
    changes from CPU libx264 to the selected NVIDIA GPU.
    """
    normalized = normalize_preview_encoding(mode)
    if normalized == "Disabled":
        return requested_codec, requested_container
    if normalized == "Always H.264":
        return _compat_h264_codec(), "MP4"
    # Auto: reuse the user's settings when they are already browser-playable,
    # otherwise fall back to the compatible H.264/MP4 preview.
    if is_user_playable_request(requested_codec, requested_container):
        return requested_codec, requested_container
    return _compat_h264_codec(), "MP4"


def wants_compat_preview(
    requested_codec: str, requested_container: str, mode: object
) -> bool:
    """True only when the forced H.264 preview must use the CPU compatibility path.

    The processor historically treats compat_preview as a CPU H.264 path and
    intentionally suppresses GPU selection there. When the 4090 fast-preview
    switch is enabled we return False so H.264 NVENC is resolved against the
    explicitly selected Video Processing GPU instead of an implicit adapter.
    H.264 remains SDR regardless.
    """
    normalized = normalize_preview_encoding(mode)
    if normalized == "Disabled":
        return False
    if normalized == "Always H.264":
        return not _fast_preview_nvenc_enabled()
    if is_user_playable_request(requested_codec, requested_container):
        return False
    return not _fast_preview_nvenc_enabled()


def is_browser_playable(path: str | Path) -> bool:
    """Probe the actual output file: playable iff MP4 + H.264 video stream."""
    from .probe import probe_video

    candidate = Path(path)
    if candidate.suffix.lower() != ".mp4":
        return False
    try:
        metadata = probe_video(candidate, count_mode="metadata")
    except Exception:
        return False
    codec = str(metadata.get("codec") or "").lower()
    if codec not in ("h264", "avc"):
        return False
    container_format = str(metadata.get("format") or "").lower()
    # ffprobe reports e.g. "mov,mp4,m4a,3gp,3g2,mj2" for MP4 files.
    if "mp4" not in container_format:
        return False
    return True


def make_browser_preview(
    source: str | Path,
    dest_dir: str | Path | None = None,
    controller=None,
    *,
    sdr_filter: str | None = None,
) -> str:
    """Transcode an existing result file to a browser-playable H.264 MP4.

    Returns the new preview path as a string. Raises RuntimeError on failure.
    """
    from ..paths import OUTPUTS
    from ..jobs import current_job_controller
    from ..disk_paths import OutputFile

    controller = controller or current_job_controller()

    src = Path(source)
    if not src.is_file():
        raise FileNotFoundError(src)
    out_dir = Path(dest_dir) if dest_dir is not None else OUTPUTS
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / f"{src.stem}_BROWSERPREVIEW.mp4"
    counter = 1
    while dest.exists():
        counter += 1
        dest = out_dir / f"{src.stem}_BROWSERPREVIEW_{counter}.mp4"
    output_file = OutputFile(dest)
    command = [
        str(FFMPEG),
        "-hide_banner",
        "-loglevel",
        "warning",
        "-y",
        "-i",
        str(src),
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        *(
            [
                "-vf",
                sdr_filter,
                "-color_primaries",
                "bt709",
                "-color_trc",
                "bt709",
                "-colorspace",
                "bt709",
                "-color_range",
                "tv",
            ]
            if sdr_filter
            else []
        ),
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "18",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-movflags",
        "+faststart",
        str(output_file.temporary),
    ]
    process = None
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if controller is not None:
            controller.register(process)
        _stdout, stderr = process.communicate()
        if process.returncode:
            raise RuntimeError(
                "Browser preview transcode failed:\n" + (stderr or "")[-4000:]
            )
        if not is_browser_playable(output_file.temporary):
            raise RuntimeError("Browser preview transcode produced an unplayable file.")
        if controller is not None and controller.cancel.is_set():
            from ..jobs import Cancelled

            raise Cancelled("Preview cancelled.")
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


def resolve_final_preview(
    result_path: str | Path | None,
    mode: object,
    controller=None,
) -> tuple[str | None, bool]:
    """Decide which file the final-render in-app player should show.

    Returns (display_path, used_derivative). Applies the agreed policy:
    - Always H.264: current behavior (MP4 result only, else no preview).
    - Disabled: always show the actual file (even MKV/MOV).
    - Auto: probe the result; show directly when playable, else transcode
      one H.264 derivative and show that.
    """
    if not result_path:
        return None, False
    normalized = normalize_preview_encoding(mode)
    candidate = str(result_path)
    if normalized == "Disabled":
        return candidate, False
    if normalized == "Always H.264":
        if Path(candidate).suffix.lower() == ".mp4":
            return candidate, False
        return None, False
    # Auto
    try:
        if is_browser_playable(candidate):
            return candidate, False
    except Exception:
        pass
    try:
        derived = make_browser_preview(candidate, controller=controller)
    except Exception:
        # Never break the render status path: fall back to no in-app preview
        # (the real file is still in the download list).
        return None, False
    return derived, True
