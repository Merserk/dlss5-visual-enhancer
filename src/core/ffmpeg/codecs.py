from __future__ import annotations

import math


ENCODING_QUALITIES = ("Auto (Default)", "Max", "Best", "Good")

# User-facing codec choices: plain = CPU (libx264/libx265/libsvtav1), suffixed = NVIDIA NVENC.
CODEC_CHOICES = (
    "H.264",
    "H.264 (NVIDIA NVENC)",
    "H.265",
    "H.265 (NVIDIA NVENC)",
    "AV1",
    "AV1 (NVIDIA NVENC)",
    "ProRes Proxy",
)

_CODEC_ALIASES = {
    "HEVC": "H.265",
    "H265": "H.265",
    "H.265 (HEVC)": "H.265",
    "HEVC (NVIDIA NVENC)": "H.265 (NVIDIA NVENC)",
}

_BASE_CODEC_MAP = {
    "H.264": "H.264",
    "H.264 (NVIDIA NVENC)": "H.264",
    "H.265": "H.265",
    "H.265 (NVIDIA NVENC)": "H.265",
    "AV1": "AV1",
    "AV1 (NVIDIA NVENC)": "AV1",
    "ProRes Proxy": "ProRes Proxy",
}

_IS_NVENC_SET = {
    "H.264 (NVIDIA NVENC)",
    "H.265 (NVIDIA NVENC)",
    "AV1 (NVIDIA NVENC)",
}

_NVENC_ENCODERS = {
    "H.264 (NVIDIA NVENC)": "h264_nvenc",
    "H.265 (NVIDIA NVENC)": "hevc_nvenc",
    "AV1 (NVIDIA NVENC)": "av1_nvenc",
}

_AUTOMATIC_CONTAINERS = {
    "H.264": "MP4",
    "H.264 (NVIDIA NVENC)": "MP4",
    "H.265": "MKV",
    "H.265 (NVIDIA NVENC)": "MKV",
    "AV1": "MKV",
    "AV1 (NVIDIA NVENC)": "MKV",
    "ProRes Proxy": "MOV",
}

# Keep AUTO_BITRATE_DIVISORS keyed by base codec; "HEVC" alias preserved for compat.
AUTO_BITRATE_DIVISORS = {
    "H.264": 165_888,
    "H.265": 331_776,
    "HEVC": 331_776,
    "AV1": 414_720,
}

# HDR Mode is only meaningful for 10-bit capable codecs. H.264 (both CPU/NVENC) stays 8-bit SDR.
HDR_ALLOWED_CODECS = {
    "H.265",
    "H.265 (NVIDIA NVENC)",
    "AV1",
    "AV1 (NVIDIA NVENC)",
    "ProRes Proxy",
}

def _normalize_codec(codec: str) -> str:
    if not isinstance(codec, str):
        return codec
    c = codec.strip()
    return _CODEC_ALIASES.get(c, c)


def _base_codec(codec: str) -> str:
    norm = _normalize_codec(codec)
    base = _BASE_CODEC_MAP.get(norm)
    if base is None:
        raise ValueError(f"Unknown video codec: {codec!r}.")
    return base


def _is_nvenc_codec(codec: str) -> bool:
    return _normalize_codec(codec) in _IS_NVENC_SET


def _is_hdr_allowed_codec(codec: str) -> bool:
    return _normalize_codec(codec) in HDR_ALLOWED_CODECS


def hdr_mode_supported(codec: str) -> bool:
    """Public helper for UI: is HDR Mode toggle meaningful for this codec?"""
    return _is_hdr_allowed_codec(codec)


def container_for_codec(codec: str) -> str:
    """Return the single application container selected for a video codec."""
    normalized = _normalize_codec(codec)
    try:
        return _AUTOMATIC_CONTAINERS[normalized]
    except KeyError as exc:
        raise ValueError(f"Unknown video codec: {codec!r}.") from exc


def _hdr_color_args(metadata: dict | None) -> list[str]:
    """Return ffmpeg color flags copying input colorspace when possible.

    We copy color_space / color_primaries / color_transfer if they are known.
    For HDR mode we also ensure 10-bit; metadata may be from SDR as well – we still
    copy what we have, which preserves SDR in same space if input was SDR.
    """
    if not metadata:
        return []
    args: list[str] = []
    # ffprobe values map directly to ffmpeg -colorspace etc when not 'unknown'
    cs = metadata.get("color_space")
    if isinstance(cs, str) and cs not in ("unknown", "", None):
        # ffmpeg expects 'bt709', 'bt2020nc', 'bt2020c', etc. Probe already returns that.
        args.extend(["-colorspace", cs])
    cp = metadata.get("color_primaries")
    if isinstance(cp, str) and cp not in ("unknown", "", None):
        args.extend(["-color_primaries", cp])
    trc = metadata.get("color_transfer")
    if isinstance(trc, str) and trc not in ("unknown", "", None):
        args.extend(["-color_trc", trc])
    # color_range is not probed currently; default tv for HDR
    # If HDR, ensure range is tv
    if metadata.get("hdr"):
        # Ensure we signal full vs limited correctly – probe doesn't give range, use tv
        # Don't double-add if already present
        if "-color_range" not in args:
            args.extend(["-color_range", "tv"])
    return args


def _x265_hdr_params(metadata: dict | None) -> str | None:
    """Build x265-params for HDR/SDR copy when using libx265 10-bit."""
    if not metadata:
        return None
    # x265 expects colorprim, transfer, colormatrix as specific strings
    # Map ffprobe names to x265 names (they align for common cases)
    mapping = {
        "color_primaries": metadata.get("color_primaries", "unknown"),
        "color_transfer": metadata.get("color_transfer", "unknown"),
        "color_space": metadata.get("color_space", "unknown"),
    }
    # Filter unknowns – use bt709 fallback for SDR
    prim = mapping["color_primaries"] if mapping["color_primaries"] not in ("unknown", "", None) else "bt709"
    trc = mapping["color_transfer"] if mapping["color_transfer"] not in ("unknown", "", None) else "bt709"
    cmat = mapping["color_space"] if mapping["color_space"] not in ("unknown", "", None) else "bt709"
    # x265 colormatrix for bt2020nc is bt2020nc, for bt709 is bt709
    # Ensure x265-compatible values: bt2020nc is valid, bt709 is valid
    return f"colorprim={prim}:transfer={trc}:colormatrix={cmat}:range=limited"


def validate_codec_container(codec: str, container: str) -> None:
    norm = _normalize_codec(codec)
    if norm == "ProRes Proxy" and container == "MP4":
        raise ValueError("ProRes Proxy is not supported in MP4. Choose the MOV or MKV container.")
    if norm not in CODEC_CHOICES and norm not in _CODEC_ALIASES.values():
        # Allow alias but error on truly unknown for early feedback; _codec_command will also validate.
        if norm not in ("H.264", "H.264 (NVIDIA NVENC)", "H.265", "H.265 (NVIDIA NVENC)", "AV1", "AV1 (NVIDIA NVENC)", "ProRes Proxy", "HEVC"):
            raise ValueError(f"Unknown video codec: {codec!r}.")


def calculate_auto_bitrate_kbps(
    width: int,
    height: int,
    fps: float,
    codec: str,
    bit_depth: int = 8,
) -> int:
    norm = _normalize_codec(codec)
    base = _base_codec(norm) if norm in _BASE_CODEC_MAP else norm
    # ProRes has no auto bitrate
    if base == "ProRes Proxy":
        raise ValueError(f"Automatic bitrate is unavailable for codec {codec!r}.")
    try:
        divisor = AUTO_BITRATE_DIVISORS[base]
    except KeyError:
        # Fallback for HEVC alias
        try:
            divisor = AUTO_BITRATE_DIVISORS[_normalize_codec(codec)]
        except KeyError as exc:
            raise ValueError(f"Automatic bitrate is unavailable for codec {codec!r}.") from exc
    if width <= 0 or height <= 0 or not math.isfinite(fps) or fps <= 0 or bit_depth <= 0:
        raise ValueError("Automatic bitrate requires positive dimensions, frame rate, and bit depth.")
    value = width * height * fps * bit_depth * 2 / divisor
    return max(1, int(math.floor(value + 0.5)))


def resolve_encoding_quality(
    quality_name: str,
    codec: str,
    width: int,
    height: int,
    fps: float,
    hdr_mode: bool = False,
) -> dict:
    if quality_name not in ENCODING_QUALITIES:
        raise ValueError(f"Unknown encoding quality: {quality_name!r}.")
    norm = _normalize_codec(codec)
    if norm not in CODEC_CHOICES:
        # Accept HEVC alias
        if norm not in _BASE_CODEC_MAP and norm != "HEVC":
            raise ValueError(f"Unknown video codec: {codec!r}.")
    if _normalize_codec(norm) == "ProRes Proxy" or _base_codec(norm) == "ProRes Proxy":
        # prores_ks otherwise ignores the application's quality selection.
        # Keep profile 0 while allowing higher selections to preserve fine
        # temporally stabilized detail instead of reintroducing quantizer crawl.
        bits_per_mb = {
            "Auto (Default)": None,
            "Good": 300,
            "Best": 500,
            "Max": 800,
        }[quality_name]
        return {
            "selection": quality_name,
            "mode": "fixed-prores-proxy-profile",
            "target_bitrate_kbps": None,
            "cq": None,
            "bits_per_mb": bits_per_mb,
        }
    if quality_name == "Max":
        return {
            "selection": quality_name,
            "mode": "constant-quality",
            "target_bitrate_kbps": None,
            "cq": 0,
        }
    multiplier = {"Auto (Default)": 1, "Good": 2, "Best": 4}[quality_name]
    bit_depth = 10 if hdr_mode and _is_hdr_allowed_codec(codec) else 8
    auto = calculate_auto_bitrate_kbps(width, height, fps, codec, bit_depth=bit_depth)
    return {
        "selection": quality_name,
        "mode": "target-bitrate",
        "auto_bitrate_kbps": auto,
        "multiplier": multiplier,
        "target_bitrate_kbps": auto * multiplier,
        "cq": None,
    }
