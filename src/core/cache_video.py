"""Rolling-cache modes and fixed lossless intermediates for file-only previews."""
from __future__ import annotations


CACHE_MODES = ("Fast lossless", "Fast compressed")
CACHE_SIZE_CHOICES_GB = (5, 10, 15, 20, 25, 30)
DEFAULT_CACHE_SIZE_GB = 10


def validate_cache_mode(selection: str) -> str:
    if selection not in CACHE_MODES:
        raise ValueError("Cache Memory mode must be Fast lossless or Fast compressed.")
    return selection


def cache_size_bytes(size_gb: int) -> int:
    """Resolve an allowed per-part size in decimal GB, without allocating it."""
    if isinstance(size_gb, bool) or not isinstance(size_gb, int) or size_gb not in CACHE_SIZE_CHOICES_GB:
        raise ValueError("Cache size must be 5, 10, 15, 20, 25 or 30 GB.")
    return size_gb * 1_000_000_000


def cache_video_format(selection: str) -> tuple[str, str, str]:
    """Return the fixed preview format; export caches use frame blocks."""
    validate_cache_mode(selection)
    return "FFV1 Lossless RGB 10-bit", "MKV", ".mkv"


def cache_ffmpeg_args(selection: str, metadata: dict) -> list[str]:
    """Encode a lossless file-only preview, independently of the export mode."""
    validate_cache_mode(selection)
    args = ["-c:v", "ffv1", "-level", "3", "-pix_fmt", "gbrp10le"]
    for key, flag in (("color_primaries", "-color_primaries"),
                      ("color_transfer", "-color_trc")):
        value = metadata.get(key)
        if value and value not in {"unknown", "unspecified"}:
            args.extend((flag, "0" if value == "gbr" else str(value)))
    return [*args, "-color_range", "pc"]
