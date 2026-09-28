"""Video intermediate formats selected by Cache Memory settings."""
from __future__ import annotations


CACHE_VIDEO_CODECS = ("FFV1", "ProRes Proxy")


def cache_video_format(selection: str) -> tuple[str, str, str]:
    """Return the encoder's public codec name, container, and file suffix."""
    if selection == "FFV1":
        return "FFV1 Lossless RGB 10-bit", "MKV", ".mkv"
    if selection == "ProRes Proxy":
        return "ProRes Proxy", "MOV", ".mov"
    raise ValueError(f"Unknown cache video codec: {selection!r}.")


def cache_ffmpeg_args(selection: str, metadata: dict) -> list[str]:
    """Encode a stage intermediate, preserving HDR signaling through ProRes."""
    if selection == "FFV1":
        args = ["-c:v", "ffv1", "-level", "3", "-pix_fmt", "gbrp10le"]
        for key, flag in (("color_primaries", "-color_primaries"),
                          ("color_transfer", "-color_trc")):
            value = metadata.get(key)
            if value and value not in {"unknown", "unspecified"}:
                args.extend((flag, "0" if value == "gbr" else str(value)))
        return [*args, "-color_range", "pc"]
    if selection != "ProRes Proxy":
        raise ValueError(f"Unknown cache video codec: {selection!r}.")
    hdr = bool(metadata.get("hdr"))
    primaries = metadata.get("color_primaries")
    transfer = metadata.get("color_transfer")
    matrix = metadata.get("color_space")
    if primaries in {None, "", "unknown", "unspecified", "gbr"}:
        primaries = "bt2020" if hdr else "bt709"
    if transfer in {None, "", "unknown", "unspecified", "gbr"}:
        transfer = "smpte2084" if hdr else "bt709"
    if matrix in {None, "", "unknown", "unspecified", "gbr"}:
        matrix = "bt2020nc" if hdr else "bt709"
    return ["-c:v", "prores_ks", "-profile:v", "0", "-pix_fmt", "yuv422p10le",
            "-color_primaries", str(primaries), "-color_trc", str(transfer),
            "-colorspace", str(matrix), "-color_range", "tv"]
