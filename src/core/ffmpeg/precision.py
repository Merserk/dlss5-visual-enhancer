"""Keep sample depth independent from HDR transfer and signaling."""

from .codecs import _normalize_codec


def output_video_depth(source_depth: int, codec: str, hdr_mode: bool = False) -> int:
    name = _normalize_codec(codec)
    if name in {"H.264", "H.264 (NVIDIA NVENC)"}:
        return 8
    if name in {"ProRes Proxy", "ProRes HQ", "FFV1 Lossless RGB 10-bit"}:
        return 10
    return 10 if int(source_depth) > 8 or hdr_mode else 8


def decoded_rgba(frame, source_depth: int):
    """Decode to 16-bit RGBA before processing whenever source precision exceeds 8 bits."""
    return frame.to_ndarray(format="rgba64le" if int(source_depth) > 8 else "rgba")


def chroma_location_code(metadata: dict) -> int:
    """FFmpeg AVChromaLocation value; unspecified 4:2:0 uses MPEG left siting."""
    return {"left": 1, "center": 2, "topleft": 3, "top": 4,
            "bottomleft": 5, "bottom": 6}.get(
        str(metadata.get("chroma_location") or "").casefold(), 1
    )
