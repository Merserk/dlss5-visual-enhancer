from __future__ import annotations

from dataclasses import dataclass, field

IMAGE_FORMATS = ("PNG", "JPEG", "WebP", "AVIF", "TIFF")
IMAGE_EXTENSIONS = {"PNG": ".png", "JPEG": ".jpg", "WebP": ".webp", "AVIF": ".avif", "TIFF": ".tiff"}
RAW_EXTENSIONS = {
    ".3fr", ".arw", ".bay", ".cap", ".cr2", ".cr3", ".dcr", ".dcs", ".dng",
    ".drf", ".eip", ".erf", ".fff", ".gpr", ".iiq", ".k25", ".kdc", ".mdc",
    ".mef", ".mos", ".mrw", ".nef", ".nrw", ".obm", ".orf", ".pef", ".ptx",
    ".pxn", ".r3d", ".raf", ".raw", ".rw2", ".rwl", ".rwz", ".sr2", ".srf",
    ".srw", ".x3f",
}

@dataclass(slots=True)
class ImageConversionOptions:
    ai_gpu_uuid: str = "auto"
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
    mask_feather: int = 0
    nr_mask: object | None = None
    upscaling_factor: float = 1.0
    scale_method: str = "Standard"
    dlss_mode: str = "Quality"
    dlss_preset: str = "Default"
    output_format: str = "PNG"
    quality: int = 95
    preserve_metadata: bool = True
    warmup_frames: int = 0
    automatic_mask: bool = False
    rename_mode: str = "Auto"
    custom_suffix: str = "_Neural_Rendering"

    def neural_options(self) -> "ImageConversionOptions":
        # Image already carries every shared neural-rendering field needed by core.runtime.
        return self


@dataclass(slots=True)
class ImageConversionResult:
    input_path: str
    output_path: str
    report_path: str
    elapsed_seconds: float
    gpu: str
    input_width: int
    input_height: int
    render_width: int
    render_height: int
    output_width: int
    output_height: int
    upscaling_factor: float
    output_format: str
    neural_dimensions: dict[str, int] | None = None
    resize_method: str = "none"
    memory_path: str = "host_cuda_d3d12_shared"
    bridge_status: dict | None = None
    warnings: list[str] = field(default_factory=list)
    timings: dict[str, float] = field(default_factory=dict)


@dataclass(slots=True)
class ImageConversionFailure:
    input_path: str
    error: str


@dataclass(slots=True)
class ImageBatchResult:
    successes: list[ImageConversionResult]
    failures: list[ImageConversionFailure]
    cancelled: bool
    manifest_path: str
    zip_path: str | None
