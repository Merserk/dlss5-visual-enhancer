from __future__ import annotations

from dataclasses import dataclass

from ...core.runtime import NR_STYLES, UPSCALING_MODES


@dataclass(slots=True)
class ConversionOptions:
    ai_gpu_uuid: str = "auto"
    video_gpu_uuid: str = "auto"
    nr_style: str = "Default"
    nr_intensity: float = 1.0
    nr_passes: int = 1
    local_tone_strength: float = 1.0
    local_structure_strength: float = 1.0
    skin_structure_strength: float = -1.0
    nr_color_strength: float = 1.0
    tone_preservation: float = 0.0
    face_skin_protection: float = 0.0
    grain_preservation: float = 0.0
    shimmer_suppression: float = 0.70
    mask_feather: int = 0
    nr_mask: object | None = None
    upscaling_factor: float = 1.0
    scale_method: str = "Standard"
    dlss_mode: str = "Quality"
    dlss_preset: str = "Default"
    codec: str = "H.264 (NVIDIA NVENC)"
    container: str = "MP4"
    quality: str = "Auto (Default)"
    preserve_hdr: bool = False
    warmup_frames: int = 0
    preview_seconds: float | None = None
    preview_frames: int | None = None
    automatic_mask: bool = False
    rename_mode: str = "Auto"
    custom_suffix: str = "_Neural_Rendering"
    # True = truncated preview uses the forced H.264 SDR path (current behavior).
    # False = truncated preview uses the user's codec/container (HDR preserved).
    preview_compat: bool = True

@dataclass(slots=True)
class ConversionResult:
    output_path: str
    report_path: str
    frames: int
    nr_count_evidence: int
    elapsed_seconds: float
    gpu: str
    input_width: int
    input_height: int
    render_width: int
    render_height: int
    output_width: int
    output_height: int
    upscaling_factor: float
    neural_dimensions: dict[str, int] | None = None
    resize_method: str = "none"
    memory_path: str = "host_cuda_d3d12_shared"
    bridge_status: dict | None = None


@dataclass(slots=True)
class VideoConversionSuccess:
    index: int
    input_path: str
    result: ConversionResult


@dataclass(slots=True)
class VideoConversionFailure:
    index: int
    input_path: str
    error: str
    cancelled: bool = False


@dataclass(slots=True)
class VideoBatchResult:
    successes: list[VideoConversionSuccess]
    failures: list[VideoConversionFailure]
    cancelled: bool
    manifest_path: str
    archive_path: str | None = None
    archive_error: str = ""
