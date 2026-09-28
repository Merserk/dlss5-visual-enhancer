from __future__ import annotations

from ..core.ffmpeg import _normalize_codec

# Historical IDs used only to discard removed cards from saved layouts.
RETIRED_STAGE_IDS = frozenset({"denoising"})

_CODEC_ALIAS_TO_CANONICAL = {
    "HEVC": "H.265",
    "H265": "H.265",
    "HEVC (NVIDIA NVENC)": "H.265 (NVIDIA NVENC)",
}

def _migrate_codec(value: str) -> str:
    if not isinstance(value, str):
        return value
    v = value.strip()
    # Use ffmpeg normalizer which already handles alias
    try:
        return _normalize_codec(v)
    except Exception:
        return _CODEC_ALIAS_TO_CANONICAL.get(v, v)
