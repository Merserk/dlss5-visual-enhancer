from __future__ import annotations

from dataclasses import dataclass, field, replace

from ..core.i18n import detect_system_language, SUPPORTED_LANGUAGES, normalize_language
from ..core.ffmpeg import CODEC_CHOICES as FFMPEG_CODEC_CHOICES, ENCODING_QUALITIES, HDR_ALLOWED_CODECS, hdr_mode_supported
from ..core.naming import validate_rename
from ..core.runtime import resolve_native_settings, resolve_upscaling_mode
from ..frame_interpolation.models import ENGINE_CHOICES, FPS_CHOICES
from .migration import _migrate_codec
from ..upscale.video.models import options_from_settings
from ..upscale.image.models import options_from_settings as image_upscale_options

QUALITY_CHOICES = ENCODING_QUALITIES
CODEC_CHOICES = FFMPEG_CODEC_CHOICES
CONTAINER_CHOICES = ("MP4", "MKV", "MOV")
IMAGE_FORMAT_CHOICES = ("PNG", "JPEG", "WebP", "AVIF", "TIFF")
CONFIG_SECTION = "Settings"
PRESET_FORMAT = "dlss5-visual-enhancer-settings-preset"
PRESET_SCHEMA_VERSION = 6
MAX_PRESET_BYTES = 1024 * 1024

AUTOMATIC_MASK_CHOICES = ("Off", "On")
LANGUAGE_CHOICES = SUPPORTED_LANGUAGES

PREVIEW_ENCODING_CHOICES = ("Auto", "Always H.264", "Disabled")
UPSCALE_MODE_CHOICES = ("Image", "Video")


def coerce_hdr_mode(codec: str, enabled: bool) -> bool:
    return bool(enabled) and hdr_mode_supported(codec)


def automatic_mask_choice(enabled: bool) -> str:
    return "On" if enabled else "Off"


def parse_automatic_mask(value: str) -> bool:
    if value not in AUTOMATIC_MASK_CHOICES:
        choices = ", ".join(AUTOMATIC_MASK_CHOICES)
        raise ValueError(f"Automatic Mask must be one of: {choices}.")
    return value == "On"

@dataclass(frozen=True, slots=True)
class UISettings:
    language: str = field(default_factory=detect_system_language)
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
    # Video/Live temporal residual stabilization. Image rendering always
    # remains reset-based and does not consume this value.
    shimmer_suppression: float = 0.70
    mask_feather: int = 0
    # Validated Gradio upload identity; intentionally omitted from config/presets.
    nr_mask: object | None = None
    upscaling_factor: float = 1.0
    # Factory default only. Existing saved codec values are loaded unchanged.
    codec: str = "H.264 (NVIDIA NVENC)"
    container: str = "MP4"
    quality: str = "Auto (Default)"
    hdr_mode: bool = False
    image_format: str = "PNG"
    image_quality: int = 95
    automatic_mask: bool = False
    image_rename_mode: str = "Auto"
    image_custom_suffix: str = "_Neural_Rendering"
    video_rename_mode: str = "Auto"
    video_custom_suffix: str = "_Neural_Rendering"
    nr_gpu_mode: bool = True
    frame_interpolation_target_fps: str = "60"
    frame_interpolation_engine: str = "Auto"
    frame_interpolation_codec: str = "H.264"
    frame_interpolation_container: str = "MP4"
    frame_interpolation_quality: str = "Auto (Default)"
    frame_interpolation_hdr_mode: bool = False
    frame_interpolation_rename_mode: str = "Auto"
    frame_interpolation_custom_suffix: str = "_Frame_Interpolation"
    preview_encoding: str = "Auto"
    full_size_image_previews: bool = False
    upscale_mode: str = "Image"
    upscale_image_vsr_quality: int = 4
    upscale_image_size_mode: str = "Scale factor"
    upscale_image_scale_factor: float = 2.0
    upscale_image_width: int = 3840
    upscale_image_height: int = 2160
    upscale_image_aspect_lock: bool = True
    upscale_image_output_format: str = "PNG"
    upscale_image_quality: int = 95
    upscale_image_preserve_metadata: bool = True
    upscale_image_rename_mode: str = "Auto"
    upscale_image_custom_suffix: str = "_Upscale"
    upscale_vsr_enabled: bool = True
    upscale_vsr_quality: int = 4
    upscale_size_mode: str = "Scale factor"
    upscale_scale_factor: float = 2.0
    upscale_width: int = 3840
    upscale_height: int = 2160
    upscale_aspect_lock: bool = True
    upscale_hdr_enabled: bool = False
    upscale_hdr_contrast: int = 100
    upscale_hdr_saturation: int = 100
    upscale_hdr_middle_gray: int = 50
    upscale_hdr_peak_luminance: int = 1000
    upscale_hdr_precision: str = "Packed 10-bit"
    upscale_codec: str = "H.265 (NVIDIA NVENC)"
    upscale_container: str = "MP4"
    upscale_quality: str = "Auto (Default)"
    upscale_rename_mode: str = "Auto"
    upscale_custom_suffix: str = "_Upscale"

    def component_values(
        self,
    ) -> tuple[str, float, int, float, float, float, float, float, float, float, float, int, float, bool, bool, str, str, str]:
        return (
            self.nr_style,
            self.nr_intensity,
            self.nr_passes,
            self.local_tone_strength,
            self.local_structure_strength,
            self.skin_structure_strength,
            self.nr_color_strength,
            self.tone_preservation,
            self.face_skin_protection,
            self.grain_preservation,
            self.shimmer_suppression,
            self.mask_feather,
            self.upscaling_factor,
            self.automatic_mask,
            self.nr_gpu_mode,
            self.codec,
            self.container,
            self.quality,
        )


DEFAULT_SETTINGS = UISettings()


def _validate(settings: UISettings) -> UISettings:
    settings = replace(settings, language=normalize_language(settings.language))
    options_from_settings(settings).validate(for_render=False)
    image_upscale_options(settings).validate(for_render=False)
    for label, value in (
        ("AI Processing GPU", settings.ai_gpu_uuid),
        ("Video Processing GPU", settings.video_gpu_uuid),
    ):
        if not isinstance(value, str) or not value.strip() or len(value) > 160:
            raise ValueError(f"{label} selection must be Automatic or a valid GPU UUID.")
    resolve_native_settings(settings)
    if isinstance(settings.nr_passes, bool) or not isinstance(settings.nr_passes, int):
        raise ValueError("NR Passes must be an integer from 1 to 4.")
    if not 1 <= settings.nr_passes <= 4:
        raise ValueError("NR Passes must be between 1 and 4.")
    if isinstance(settings.mask_feather, bool) or not isinstance(settings.mask_feather, int):
        raise ValueError("Mask Feather must be an integer from 0 to 128.")
    if not 0 <= settings.mask_feather <= 128:
        raise ValueError("Mask Feather must be between 0 and 128 pixels.")
    resolve_upscaling_mode(settings.upscaling_factor)
    if not isinstance(settings.automatic_mask, bool):
        raise ValueError("Automatic Mask must be a boolean value.")
    if not isinstance(settings.nr_gpu_mode, bool):
        raise ValueError("Neural Rendering GPU mode must be a boolean value.")
    if not isinstance(settings.hdr_mode, bool):
        raise ValueError("HDR Mode must be a boolean value.")
    if not isinstance(settings.frame_interpolation_hdr_mode, bool):
        raise ValueError("Frame Interpolation HDR Mode must be a boolean value.")
    if not isinstance(settings.full_size_image_previews, bool):
        raise ValueError("Full size quality preview must be a boolean value.")
    if settings.language not in LANGUAGE_CHOICES:
        raise ValueError(f"Unknown language: {settings.language!r}.")
    # Migrate old codec names before validation
    migrated_codec = _migrate_codec(settings.codec)
    migrated_fi_codec = _migrate_codec(settings.frame_interpolation_codec)
    if migrated_codec != settings.codec or migrated_fi_codec != settings.frame_interpolation_codec:
        settings = replace(settings, codec=migrated_codec, frame_interpolation_codec=migrated_fi_codec)
    # HDR Mode is only allowed for 10-bit capable codecs; if enabled with H.264, auto-disable for old configs
    # For strict validation (presets), raise if mismatched – caller can decide; here we raise for explicit mismatch
    _HDR_ALLOWED = HDR_ALLOWED_CODECS

    if settings.hdr_mode and settings.codec not in _HDR_ALLOWED:
        raise ValueError(
            f"HDR Mode is only available for {', '.join(sorted(_HDR_ALLOWED))}; current codec is {settings.codec!r}."
        )
    if settings.frame_interpolation_hdr_mode and settings.frame_interpolation_codec not in _HDR_ALLOWED:
        raise ValueError(
            f"Frame Interpolation HDR Mode is only available for {', '.join(sorted(_HDR_ALLOWED))}; current codec is {settings.frame_interpolation_codec!r}."
        )
    allowed = {
        "Video codec": (settings.codec, CODEC_CHOICES),
        "Container": (settings.container, CONTAINER_CHOICES),
        "Encoding quality": (settings.quality, QUALITY_CHOICES),
        "Image format": (settings.image_format, IMAGE_FORMAT_CHOICES),
        "Frame Interpolation FPS": (
            settings.frame_interpolation_target_fps,
            FPS_CHOICES,
        ),
        "Frame Interpolation engine": (
            settings.frame_interpolation_engine,
            ENGINE_CHOICES,
        ),
        "Frame Interpolation codec": (
            settings.frame_interpolation_codec,
            CODEC_CHOICES,
        ),
        "Frame Interpolation container": (
            settings.frame_interpolation_container,
            CONTAINER_CHOICES,
        ),
        "Frame Interpolation quality": (
            settings.frame_interpolation_quality,
            QUALITY_CHOICES,
        ),
        "Preview encoding": (
            settings.preview_encoding,
            PREVIEW_ENCODING_CHOICES,
        ),
        "Upscale mode": (
            settings.upscale_mode,
            UPSCALE_MODE_CHOICES,
        ),
    }
    for label, (value, choices) in allowed.items():
        if value not in choices:
            raise ValueError(f"Unknown {label}: {value!r}.")
    if isinstance(settings.image_quality, bool) or not 1 <= int(settings.image_quality) <= 100:
        raise ValueError("Image quality must be an integer from 1 to 100.")
    if int(settings.image_quality) != settings.image_quality:
        raise ValueError("Image quality must be an integer from 1 to 100.")
    validate_rename(settings.image_rename_mode, settings.image_custom_suffix)
    validate_rename(settings.video_rename_mode, settings.video_custom_suffix)
    validate_rename(
        settings.frame_interpolation_rename_mode,
        settings.frame_interpolation_custom_suffix,
    )
    return settings
