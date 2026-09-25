from __future__ import annotations

import subprocess
import threading
from functools import lru_cache
from pathlib import Path

from ..jobs import BoundedLogBuffer, JobController, drain_bounded_text, drain_text
from ..paths import FFMPEG
from .audio import AudioPlan, plan_audio_streams
from .codecs import (
    CODEC_CHOICES, _NVENC_ENCODERS, _base_codec, _hdr_color_args,
    _is_hdr_allowed_codec, _is_nvenc_codec, _normalize_codec, _x265_hdr_params,
    resolve_encoding_quality,
)

@lru_cache(maxsize=128)
def _encoder_probe(
    codec: str, width: int, height: int, gpu_ordinal: int | None = None
) -> bool:
    gpu_args = ["-gpu", str(gpu_ordinal)] if gpu_ordinal is not None else []
    command = [
        str(FFMPEG),
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        f"color=size={width}x{height}:rate=1",
        "-frames:v",
        "1",
        "-c:v",
        codec,
        *gpu_args,
        "-f",
        "null",
        "-",
    ]
    return (
        subprocess.run(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).returncode
        == 0
    )


@lru_cache(maxsize=16)
def _cpu_encoder_available(codec: str) -> bool:
    """Probe CPU encoder initialization once at a tiny resolution."""
    return _encoder_probe(codec, 64, 64, None)


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
    candidates = [gpu for gpu in gpus if gpu.get("cuda_ordinal") is not None]
    if gpu_uuid != "auto":
        candidates = [gpu for gpu in candidates if gpu.get("uuid") == gpu_uuid]
        if not candidates:
            raise RuntimeError("The selected Video Processing GPU is unavailable.")
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
    *,
    speed_profile: str = "default", output_depth: int | None = None,
) -> tuple[list[str], str, dict]:
    """Return FFmpeg codec args for an explicit user-facing codec choice.

    Plain names (H.264, H.265, AV1) are strictly CPU (libx264/libx265/libsvtav1).
    Suffixed names (H.264 (NVIDIA NVENC) etc.) are strictly NVENC and require a GPU.
    `require_nvenc` is kept for backward compat – NVENC codecs always require NVENC,
    CPU codecs always forbid fallback to NVENC.
    When ``hdr_mode`` is True, output is 10-bit (yuv420p10le/p010le) and input
    colorspace is copied via ``hdr_metadata`` for HDR_ALLOWED_CODECS.
    """
    if speed_profile not in {"default", "neural", "preview"}:
        raise ValueError(f"Unknown encoder speed profile: {speed_profile!r}.")
    software_preset = {"default": "slow", "neural": "medium", "preview": "veryfast"}[speed_profile]

    norm = _normalize_codec(codec)
    if hdr_mode and not _is_hdr_allowed_codec(norm):
        raise ValueError(
            f"HDR Mode is not available for {codec!r}; choose H.265, H.265 (NVIDIA NVENC), "
            "AV1, AV1 (NVIDIA NVENC), ProRes, or FFV1 Lossless RGB 10-bit."
        )
    high_depth = hdr_mode or (output_depth is not None and output_depth > 8)
    quality = resolve_encoding_quality(quality_name, codec, width, height, fps, hdr_mode=high_depth)
    # ProRes Proxy is always 10-bit; HDR just copies colorspace
    if norm == "ProRes Proxy":
        hdr_extra = _hdr_color_args(hdr_metadata) if hdr_metadata else []
        prores_quality = (
            ["-bits_per_mb", str(int(quality["bits_per_mb"]))]
            if quality.get("bits_per_mb") is not None
            else []
        )
        return (
            [
                "-c:v", "prores_ks", "-profile:v", "0",
                *prores_quality, "-pix_fmt", "yuv422p10le", *hdr_extra,
            ],
            "prores_ks (Proxy)",
            quality,
        )
    if norm == "ProRes HQ":
        return (
            ["-c:v", "prores_ks", "-profile:v", "3", "-pix_fmt", "yuv422p10le",
             *(_hdr_color_args(hdr_metadata) if hdr_metadata else [])],
            "prores_ks (HQ)", quality,
        )
    if norm == "FFV1 Lossless RGB 10-bit":
        return (
            ["-c:v", "ffv1", "-level", "3", "-slicecrc", "1", "-pix_fmt", "gbrp10le",
             "-color_range", "pc"],
            "ffv1 (Lossless RGB 10-bit)", quality,
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
    signal_metadata = hdr_metadata
    if high_depth and not hdr_mode:
        signal_metadata = dict(hdr_metadata or {})
        for field in ("color_space", "color_primaries", "color_transfer"):
            if signal_metadata.get(field) in (None, "", "unknown", "unspecified"):
                signal_metadata[field] = "bt709"
    hdr_color = _hdr_color_args(signal_metadata) if signal_metadata else []

    # CPU variants – never probe NVENC, always use software encoder
    if norm == "H.264":
        # H.264 is always 8-bit SDR – HDR Mode is not allowed (checked above), so plain yuv420p
        if require_nvenc:
            raise RuntimeError(
                f"H.264 (CPU) was requested but NVENC was required; choose H.264 (NVIDIA NVENC) for GPU encoding."
            )
        if hdr_mode:
            raise ValueError("HDR Mode is not available for H.264; choose H.265/AV1/ProRes.")
        x264_color = _x265_hdr_params(signal_metadata)
        return (
            ["-c:v", "libx264", "-preset", software_preset, *software_quality,
             "-pix_fmt", "yuv420p",
             *(["-x264-params", x264_color] if x264_color else []), *hdr_color],
            "libx264",
            quality,
        )
    if norm == "H.265" or norm == "HEVC":
        if require_nvenc:
            raise RuntimeError(
                f"H.265 (CPU) was requested but NVENC was required; choose H.265 (NVIDIA NVENC) for GPU encoding."
            )
        # Source precision or HDR selects 10-bit independently of SDR transfer.
        pix_fmt = "yuv420p10le" if high_depth else "yuv420p"
        if high_depth:
            x265_params = _x265_hdr_params(signal_metadata)
            x265_extra = ["-x265-params", x265_params] if x265_params else []
            range_extra = (
                ["-color_range", "pc"]
                if str((signal_metadata or {}).get("color_range") or "").lower()
                in {"pc", "jpeg", "full"} else []
            )
            # x265 owns VUI color fields; the generic range flag also tags the stream.
            return (
                ["-c:v", "libx265", "-preset", software_preset, *software_quality, "-pix_fmt", pix_fmt, *x265_extra, *range_extra],
                "libx265",
                quality,
            )
        return (
            ["-c:v", "libx265", "-preset", software_preset, *software_quality, "-pix_fmt", pix_fmt, *hdr_color],
            "libx265",
            quality,
        )
    if norm == "AV1":
        if require_nvenc:
            raise RuntimeError(
                "AV1 (CPU) was requested but NVENC was required; choose AV1 (NVIDIA NVENC) for GPU encoding."
            )
        pix_fmt = "yuv420p10le" if high_depth else "yuv420p"
        # Prefer libsvtav1 (fastest CPU AV1), fallback to libaom-av1
        if _cpu_encoder_available("libsvtav1"):
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
        if _cpu_encoder_available("libaom-av1"):
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
                "-c:v", "h264_nvenc", *gpu_args, "-preset", "p6", "-tune", "hq",
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
        pix_fmt = "p010le" if high_depth else "yuv420p"
        # HEVC HDR should use main10 implicitly via p010le
        return (
            [
                "-c:v", "hevc_nvenc", *gpu_args, "-preset", "p6", "-tune", "hq",
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
        pix_fmt = "p010le" if high_depth else "yuv420p"
        return (
            ["-c:v", "av1_nvenc", *gpu_args, "-preset", "p6", *nvenc_quality, "-pix_fmt", pix_fmt, *hdr_color],
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
    speed_profile: str = "default", source_audio: Path | None = None,
    direct_container: str | None = None, comment: str | None = None,
    audio_duration: float | None = None, include_source_metadata: bool = True,
    audio_plan: AudioPlan | None = None, output_depth: int | None = None,
):
    codec_args, selected, quality = _codec_command(
        codec, quality_name, width, height, fps, gpu_ordinal, require_nvenc, hdr_mode, hdr_metadata,
        speed_profile=speed_profile, output_depth=output_depth,
    )
    normalized_codec = _normalize_codec(codec)
    if (output_depth is not None and output_depth > 8
            and str((hdr_metadata or {}).get("color_range") or "").lower()
            in {"pc", "jpeg", "full"}
            and normalized_codec in {"H.265", "HEVC", "AV1"}):
        range_filter = "scale=in_range=pc:out_range=pc"
        video_filter = f"{video_filter},{range_filter}" if video_filter else range_filter
    if normalized_codec in {"ProRes Proxy", "ProRes HQ", "FFV1 Lossless RGB 10-bit"}:
        colors = hdr_metadata or {}
        primaries = str(colors.get("color_primaries") or "bt709")
        transfer = str(colors.get("color_transfer") or "bt709")
        if primaries in {"unknown", "unspecified"}:
            primaries = "bt2020" if colors.get("hdr") else "bt709"
        if transfer in {"unknown", "unspecified"}:
            transfer = "smpte2084" if colors.get("hdr") else "bt709"
        if normalized_codec == "FFV1 Lossless RGB 10-bit":
            tag_filter = ("format=gbrp10le,setparams="
                          f"color_primaries={primaries}:color_trc={transfer}:"
                          "colorspace=gbr:range=full")
        else:
            matrix = str(colors.get("color_space") or "bt709")
            if matrix in {"unknown", "unspecified"}:
                matrix = "bt2020nc" if colors.get("hdr") else "bt709"
            tag_filter = ("format=yuv422p10le,setparams="
                          f"color_primaries={primaries}:color_trc={transfer}:"
                          f"colorspace={matrix}:range=limited")
        video_filter = f"{video_filter},{tag_filter}" if video_filter else tag_filter
    direct_mux = source_audio is not None and direct_container in {"MP4", "MOV"}
    if direct_mux:
        selected_audio = audio_plan or plan_audio_streams(source_audio, direct_container, controller)
        command = [
            str(FFMPEG),
            "-hide_banner",
            "-loglevel",
            "warning",
            "-y",
            *( ["-copyts"] if keep_start_time else [] ),
            "-f",
            "nut",
            "-i",
            "pipe:0",
            *( ["-t", f"{float(audio_duration):.9f}"] if audio_duration is not None and audio_duration > 0 else [] ),
            "-i",
            str(source_audio),
            "-map",
            "0:v:0",
            "-map",
            "1:a?",
            *( ["-map_metadata", "1", "-map_chapters", "1"] if include_source_metadata else ["-map_metadata", "-1", "-map_chapters", "-1"] ),
            *( ["-vf", video_filter] if video_filter else [] ),
            *codec_args,
            *selected_audio.encoder_args(),
            "-fps_mode",
            "passthrough",
            *( ["-enc_time_base:v", "demux"] if preserve_timestamps else [] ),
            "-movflags",
            "+faststart",
            "-metadata:s:v:0",
            "rotate=0",
            *( ["-metadata", "comment=" + comment] if comment is not None else [] ),
            *( ["-avoid_negative_ts", "disabled"] if keep_start_time else [] ),
            str(temp_video),
        ]
    else:
        command = [
            str(FFMPEG),
            "-hide_banner",
            "-loglevel",
            "warning",
            "-y",
            *( ["-copyts"] if keep_start_time else [] ),
            "-f",
            "nut",
            "-i",
            "pipe:0",
            "-map",
            "0:v:0",
            "-an",
            *( ["-vf", video_filter] if video_filter else [] ),
            *codec_args,
            "-fps_mode",
            "passthrough",
            *( ["-enc_time_base:v", "demux"] if preserve_timestamps else [] ),
            *( ["-avoid_negative_ts", "disabled"] if keep_start_time else [] ),
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
