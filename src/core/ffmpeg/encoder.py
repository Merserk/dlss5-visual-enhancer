from __future__ import annotations

import subprocess
import threading
from pathlib import Path

from ..jobs import BoundedLogBuffer, JobController, drain_bounded_text, drain_text
from ..paths import FFMPEG
from ..gpu_selection import gpu_preference_key, preferred_gpu_uuid
from .performance import nvenc_preset, probe_encoder
from .codecs import (
    CODEC_CHOICES, _NVENC_ENCODERS, _base_codec, _hdr_color_args,
    _is_hdr_allowed_codec, _is_nvenc_codec, _normalize_codec, _x265_hdr_params,
    resolve_encoding_quality,
)

def _encoder_probe(
    codec: str, width: int, height: int, gpu_ordinal: int | None = None
) -> bool:
    return probe_encoder(FFMPEG, codec, width, height, gpu_ordinal)


# Legacy mapping kept for external callers; new display names use _NVENC_ENCODERS.
_NVENC_CODECS = {
    "H.264": "h264_nvenc",
    "HEVC": "hevc_nvenc",
    "AV1": "av1_nvenc",
    "H.265": "hevc_nvenc",
    "H.264 (NVIDIA NVENC)": "h264_nvenc",
    "H.265 (NVIDIA NVENC)": "hevc_nvenc",
    "AV1 (NVIDIA NVENC)": "av1_nvenc",
}


def probe_nvenc_codecs(gpu_ordinal: int) -> tuple[str, ...]:
    """Return user-facing codecs that can initialize NVENC on one CUDA device.

    Returns display names like "H.264 (NVIDIA NVENC)" so UI can filter.
    """
    # Probe the three actual NVENC encoders once, then map to display names.
    mapping = {
        "h264_nvenc": "H.264 (NVIDIA NVENC)",
        "hevc_nvenc": "H.265 (NVIDIA NVENC)",
        "av1_nvenc": "AV1 (NVIDIA NVENC)",
    }
    supported = []
    for encoder, display in mapping.items():
        if _encoder_probe(encoder, 256, 256, int(gpu_ordinal)):
            supported.append(display)
    return tuple(supported)


def resolve_video_gpu(
    gpus: tuple[dict, ...],
    gpu_uuid: str,
    codec: str,
    width: int,
    height: int,
) -> dict | None:
    """Resolve a stable UUID to a device that supports the requested NVENC codec.

    CPU codecs (plain H.264/H.265/AV1 without '(NVIDIA NVENC)') never require a GPU.
    NVENC codecs explicitly require a cuda device that can init the encoder.
    """
    norm = _normalize_codec(codec)
    if norm == "ProRes Proxy":
        return None
    # CPU variants do not need a GPU
    if not _is_nvenc_codec(norm):
        # Validate that plain codecs are known; if unknown, raise.
        if norm not in CODEC_CHOICES and norm not in ("HEVC", "H.265"):
            # Try base check
            try:
                _base_codec(norm)
            except ValueError as exc:
                raise ValueError(f"Unknown video codec: {codec!r}.") from exc
        # Plain variants: no GPU needed (CPU encoding)
        return None
    # NVENC path
    try:
        encoder = _NVENC_ENCODERS[norm]
    except KeyError as exc:
        raise ValueError(f"Unknown video codec: {codec!r}.") from exc
    gpu_uuid = preferred_gpu_uuid(gpu_uuid)
    candidates = sorted(
        (gpu for gpu in gpus if gpu.get("cuda_ordinal") is not None),
        key=gpu_preference_key, reverse=True,
    )
    if gpu_uuid != "auto":
        candidates = [gpu for gpu in candidates if gpu.get("uuid") == gpu_uuid]
        if not candidates:
            raise RuntimeError(
                "The selected Video Processing GPU is unavailable or its CUDA identity "
                "could not be verified. Check nvcuda.dll/driver and CUDA_VISIBLE_DEVICES; "
                "nvidia-smi indices are not interchangeable with CUDA ordinals."
            )
    for gpu in candidates:
        ordinal = int(gpu["cuda_ordinal"])
        if _encoder_probe(encoder, width, height, ordinal):
            selected = dict(gpu)
            selected["nvenc_codec"] = encoder
            return selected
    selection = "selected GPU" if gpu_uuid != "auto" else "available NVIDIA GPUs"
    raise RuntimeError(
        f"{norm} cannot encode the requested {width}×{height} output on the "
        f"{selection}. Choose another Video Processing GPU, codec, or output size."
    )


def _codec_command(
    codec: str,
    quality_name: str,
    width: int,
    height: int,
    fps: float,
    gpu_ordinal: int | None = None,
    require_nvenc: bool = False,
    hdr_mode: bool = False,
    hdr_metadata: dict | None = None,
) -> tuple[list[str], str, dict]:
    """Return FFmpeg codec args for an explicit user-facing codec choice.

    Plain names (H.264, H.265, AV1) are strictly CPU (libx264/libx265/libsvtav1).
    Suffixed names (H.264 (NVIDIA NVENC) etc.) are strictly NVENC and require a GPU.
    `require_nvenc` is kept for backward compat – NVENC codecs always require NVENC,
    CPU codecs always forbid fallback to NVENC.
    When ``hdr_mode`` is True, output is 10-bit (yuv420p10le/p010le) and input
    colorspace is copied via ``hdr_metadata`` for HDR_ALLOWED_CODECS.
    """
    norm = _normalize_codec(codec)
    if hdr_mode and not _is_hdr_allowed_codec(norm):
        raise ValueError(
            f"HDR Mode is not available for {codec!r}; choose H.265, H.265 (NVIDIA NVENC), "
            "AV1, AV1 (NVIDIA NVENC) or ProRes Proxy."
        )
    # HDR mode needs 10-bit divisor
    quality = resolve_encoding_quality(quality_name, codec, width, height, fps, hdr_mode=hdr_mode)
    if _is_nvenc_codec(norm):
        quality = {**quality, "nvenc_preset": nvenc_preset()}
    # ProRes Proxy is always 10-bit; HDR just copies colorspace
    if norm == "ProRes Proxy":
        hdr_extra = _hdr_color_args(hdr_metadata) if hdr_mode and hdr_metadata else []
        return (
            ["-c:v", "prores_ks", "-profile:v", "0", "-pix_fmt", "yuv422p10le", *hdr_extra],
            "prores_ks (Proxy)",
            quality,
        )
    if quality["mode"] == "constant-quality":
        nvenc_quality = ["-rc", "vbr", "-cq", "0", "-b:v", "0"]
        software_quality = ["-crf", "0"]
        bitrate = None
    else:
        bitrate = f"{quality['target_bitrate_kbps']}k"
        nvenc_quality = ["-rc", "vbr", "-b:v", bitrate]
        software_quality = ["-b:v", bitrate]
    gpu_args = ["-gpu", str(gpu_ordinal)] if gpu_ordinal is not None else []
    hdr_color = _hdr_color_args(hdr_metadata) if hdr_mode and hdr_metadata else []

    # CPU variants – never probe NVENC, always use software encoder
    if norm == "H.264":
        # H.264 is always 8-bit SDR – HDR Mode is not allowed (checked above), so plain yuv420p
        if require_nvenc:
            raise RuntimeError(
                f"H.264 (CPU) was requested but NVENC was required; choose H.264 (NVIDIA NVENC) for GPU encoding."
            )
        if hdr_mode:
            raise ValueError("HDR Mode is not available for H.264; choose H.265/AV1/ProRes.")
        return (
            ["-c:v", "libx264", "-preset", "slow", *software_quality, "-pix_fmt", "yuv420p"],
            "libx264",
            quality,
        )
    if norm == "H.265" or norm == "HEVC":
        if require_nvenc:
            raise RuntimeError(
                f"H.265 (CPU) was requested but NVENC was required; choose H.265 (NVIDIA NVENC) for GPU encoding."
            )
        # HDR → 10-bit yuv420p10le, SDR → yuv420p.
        pix_fmt = "yuv420p10le" if hdr_mode else "yuv420p"
        if hdr_mode:
            x265_params = _x265_hdr_params(hdr_metadata)
            x265_extra = ["-x265-params", x265_params] if x265_params else []
            # For x265, use x265-params exclusively for HDR (generic breaks VUI)
            return (
                ["-c:v", "libx265", "-preset", "slow", *software_quality, "-pix_fmt", pix_fmt, *x265_extra],
                "libx265",
                quality,
            )
        return (
            ["-c:v", "libx265", "-preset", "slow", *software_quality, "-pix_fmt", pix_fmt, *hdr_color],
            "libx265",
            quality,
        )
    if norm == "AV1":
        if require_nvenc:
            raise RuntimeError(
                "AV1 (CPU) was requested but NVENC was required; choose AV1 (NVIDIA NVENC) for GPU encoding."
            )
        pix_fmt = "yuv420p10le" if hdr_mode else "yuv420p"
        # Prefer libsvtav1 (fastest CPU AV1), fallback to libaom-av1
        if _encoder_probe("libsvtav1", width, height):
            if quality["mode"] == "constant-quality":
                return (
                    ["-c:v", "libsvtav1", "-preset", "6", "-crf", "0", "-pix_fmt", pix_fmt, *hdr_color],
                    "libsvtav1",
                    quality,
                )
            return (
                ["-c:v", "libsvtav1", "-preset", "6", "-b:v", bitrate, "-pix_fmt", pix_fmt, *hdr_color],
                "libsvtav1",
                quality,
            )
        if _encoder_probe("libaom-av1", width, height):
            if quality["mode"] == "constant-quality":
                return (
                    ["-c:v", "libaom-av1", "-cpu-used", "4", "-crf", "0", "-b:v", "0", "-pix_fmt", pix_fmt, *hdr_color],
                    "libaom-av1",
                    quality,
                )
            return (
                ["-c:v", "libaom-av1", "-cpu-used", "6", "-b:v", bitrate, "-pix_fmt", pix_fmt, *hdr_color],
                "libaom-av1",
                quality,
            )
        raise RuntimeError(
            "AV1 CPU encoding is unavailable: neither libsvtav1 nor libaom-av1 can initialize. "
            "Choose H.264/H.265 or AV1 (NVIDIA NVENC) if a GPU is available."
        )

    # NVENC variants – strictly require NVENC probe success
    if norm == "H.264 (NVIDIA NVENC)":
        if hdr_mode:
            raise ValueError("HDR Mode is not available for H.264 (NVIDIA NVENC); choose H.265/AV1.")
        if not _encoder_probe("h264_nvenc", width, height, gpu_ordinal):
            raise RuntimeError(
                f"H.264 (NVIDIA NVENC) cannot encode {width}×{height} on the selected Video Processing GPU. "
                "Choose H.264 (CPU) or another GPU."
            )
        return (
            [
                "-c:v", "h264_nvenc", *gpu_args, "-preset", nvenc_preset(), "-tune", "hq",
                *nvenc_quality, "-pix_fmt", "yuv420p",
            ],
            "h264_nvenc",
            quality,
        )
    if norm == "H.265 (NVIDIA NVENC)":
        if not _encoder_probe("hevc_nvenc", width, height, gpu_ordinal):
            raise RuntimeError(
                f"H.265 (NVIDIA NVENC) cannot encode {width}×{height} on the selected Video Processing GPU. "
                "Choose H.265 (CPU) or another GPU."
            )
        pix_fmt = "p010le" if hdr_mode else "yuv420p"
        # HEVC HDR should use main10 implicitly via p010le
        return (
            [
                "-c:v", "hevc_nvenc", *gpu_args, "-preset", nvenc_preset(), "-tune", "hq",
                *nvenc_quality, "-pix_fmt", pix_fmt, *hdr_color,
            ],
            "hevc_nvenc",
            quality,
        )
    if norm == "AV1 (NVIDIA NVENC)":
        if not _encoder_probe("av1_nvenc", width, height, gpu_ordinal):
            raise RuntimeError(
                f"AV1 (NVIDIA NVENC) cannot encode {width}×{height} on the selected GPU/driver. "
                "Choose AV1 (CPU) with libsvtav1, or H.264/H.265, or a lower upscaling factor."
            )
        pix_fmt = "p010le" if hdr_mode else "yuv420p"
        return (
            ["-c:v", "av1_nvenc", *gpu_args, "-preset", nvenc_preset(), *nvenc_quality, "-pix_fmt", pix_fmt, *hdr_color],
            "av1_nvenc",
            quality,
        )

    raise ValueError(f"Unknown video codec: {codec!r}.")


def start_encoder(
    temp_video: Path,
    codec: str,
    quality_name: str,
    controller: JobController,
    width: int,
    height: int,
    fps: float,
    gpu_ordinal: int | None = None,
    require_nvenc: bool = False,
    hdr_mode: bool = False,
    hdr_metadata: dict | None = None,
    preserve_timestamps: bool = False,
    *, video_filter: str | None = None, keep_start_time: bool = False, bounded_logs: bool = False,
):
    codec_args, selected, quality = _codec_command(
        codec, quality_name, width, height, fps, gpu_ordinal, require_nvenc, hdr_mode, hdr_metadata
    )
    command = [
        str(FFMPEG),
        "-hide_banner",
        "-loglevel",
        "warning",
        "-y",
        *(["-copyts"] if keep_start_time else []),
        "-f",
        "nut",
        "-i",
        "pipe:0",
        "-map",
        "0:v:0",
        "-an",
        *(["-vf", video_filter] if video_filter else []),
        *codec_args,
        "-fps_mode",
        "passthrough",
        *(["-enc_time_base:v", "demux"] if preserve_timestamps else []),
        *(["-avoid_negative_ts", "disabled"] if keep_start_time else []),
        str(temp_video),
    ]
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    controller.register(process)
    logs = BoundedLogBuffer(max_tail=60) if bounded_logs else []
    assert process.stderr is not None
    thread = threading.Thread(target=drain_bounded_text if bounded_logs else drain_text, args=(process.stderr, logs), daemon=True)
    thread.start()
    return process, thread, logs, selected, quality
