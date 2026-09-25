from __future__ import annotations

from dataclasses import dataclass, replace
import math
from types import SimpleNamespace

from ..core.ffmpeg import CODEC_CHOICES as FFMPEG_CODEC_CHOICES, ENCODING_QUALITIES, HDR_ALLOWED_CODECS, hdr_mode_supported
from ..core.naming import validate_rename
from ..core.runtime import resolve_native_settings, resolve_upscaling_mode
from ..core.dlss_modes import DLSS_METHODS, validate_dlss
from ..frame_interpolation.models import ENGINE_CHOICES, FPS_CHOICES, PREVIEW_LENGTH_CHOICES
from .migration import _migrate_codec
from ..upscale.video.models import options_from_settings
from ..upscale.image.models import options_from_settings as image_upscale_options

QUALITY_CHOICES = ENCODING_QUALITIES
CODEC_CHOICES = FFMPEG_CODEC_CHOICES
CONTAINER_CHOICES = ("MP4", "MKV", "MOV")
IMAGE_FORMAT_CHOICES = ("PNG", "JPEG", "WebP", "AVIF", "TIFF")
IMAGE_16BIT_FORMATS = ("PNG", "TIFF")
IMAGE_BIT_DEPTH_CHOICES = (8, 16)
CONFIG_SECTION = "Settings"
PRESET_FORMAT = "dlss5-visual-enhancer-settings-preset"
PRESET_SCHEMA_VERSION = 18
MAX_PRESET_BYTES = 1024 * 1024

AUTOMATIC_MASK_CHOICES = ("Off", "On")

PREVIEW_ENCODING_CHOICES = ("Auto", "Always H.264", "Disabled")
UPSCALE_MODE_CHOICES = ("Image", "Video")
UPSCALE_PREVIEW_LENGTH_CHOICES = PREVIEW_LENGTH_CHOICES

IMAGE_SCALING_FILTERS = ("Lanczos4", "Area", "Bicubic", "Bilinear", "Nearest")
VIDEO_SCALING_FILTERS = ("Spline36", "Lanczos", "Bicubic", "Area", "Bilinear", "EWA Lanczos")

LEGACY_IMAGE_STAGE_ORDER = ("neural_model", "scale_method", "super_resolution")
LEGACY_VIDEO_STAGE_ORDER = (*LEGACY_IMAGE_STAGE_ORDER, "frame_generation")
PREVIOUS_IMAGE_STAGE_ORDER = ("neural_model", "scale_method", "dlss_super_resolution", "super_resolution")
BEFORE_DENOISING_IMAGE_STAGE_ORDER = (*PREVIOUS_IMAGE_STAGE_ORDER, "coloring")
BEFORE_CAS_IMAGE_STAGE_ORDER = ("denoising", *BEFORE_DENOISING_IMAGE_STAGE_ORDER)
IMAGE_STAGE_ORDER = (*BEFORE_CAS_IMAGE_STAGE_ORDER, "cas_sharpening")
PREVIOUS_VIDEO_STAGE_ORDER = (*PREVIOUS_IMAGE_STAGE_ORDER, "frame_generation")
BEFORE_DENOISING_VIDEO_STAGE_ORDER = (*PREVIOUS_VIDEO_STAGE_ORDER, "coloring")
BEFORE_CAS_VIDEO_STAGE_ORDER = ("denoising", *BEFORE_DENOISING_VIDEO_STAGE_ORDER)
VIDEO_STAGE_ORDER = (*BEFORE_CAS_VIDEO_STAGE_ORDER, "cas_sharpening")
COLORING_MODES = ("Color Match", "LUT")
SHARPENING_METHODS = ("NVIDIA NIS", "AMD CAS")
COLOR_MATCH_SOURCES = ("Input Image", "Selected Image")
LUT_RESOLUTIONS = (33, 65, 129)
LUT_ADJUSTMENT_RANGES = {
    "lut_mix": (0, 200),
    "lut_exposure": (-3.0, 3.0),
    "lut_contrast": (-100, 100),
    "lut_highlights": (-100, 100),
    "lut_shadows": (-100, 100),
    "lut_whites": (-100, 100),
    "lut_blacks": (-100, 100),
    "lut_midtones": (-100, 100),
    "lut_temperature": (-100, 100),
    "lut_tint": (-100, 100),
    "lut_hue": (-180, 180),
    "lut_vibrance": (-100, 100),
    "lut_saturation": (-100, 100),
}


def validate_stage_layout(order: tuple[str, ...], enabled: tuple[str, ...], *, video: bool) -> None:
    allowed = VIDEO_STAGE_ORDER if video else IMAGE_STAGE_ORDER
    if not isinstance(order, tuple) or len(order) != len(allowed) or set(order) != set(allowed):
        raise ValueError("Pipeline order must contain each available processing card exactly once.")
    if not isinstance(enabled, tuple) or len(enabled) != len(set(enabled)) or not set(enabled) <= set(allowed):
        raise ValueError("Pipeline enabled stages contain an unavailable or duplicate card.")


def migrate_stage_layout(order: tuple[str, ...], enabled: tuple[str, ...], *,
                         video: bool, scale_method: str, upscale_engine: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Upgrade old card layouts while retaining their order and enabled behavior."""
    allowed = VIDEO_STAGE_ORDER if video else IMAGE_STAGE_ORDER
    if set(order) == set(allowed) and len(order) == len(allowed):
        validate_stage_layout(order, enabled, video=video)
        return order, enabled
    before_cas = BEFORE_CAS_VIDEO_STAGE_ORDER if video else BEFORE_CAS_IMAGE_STAGE_ORDER
    if len(order) == len(before_cas) and set(order) == set(before_cas):
        if len(enabled) != len(set(enabled)) or not set(enabled) <= set(order):
            raise ValueError("Pipeline enabled stages contain an unavailable or duplicate card.")
        result = (*order, "cas_sharpening"), enabled
        validate_stage_layout(*result, video=video)
        return result
    before_denoising = (BEFORE_DENOISING_VIDEO_STAGE_ORDER if video
                        else BEFORE_DENOISING_IMAGE_STAGE_ORDER)
    if len(order) == len(before_denoising) and set(order) == set(before_denoising):
        if len(enabled) != len(set(enabled)) or not set(enabled) <= set(order):
            raise ValueError("Pipeline enabled stages contain an unavailable or duplicate card.")
        result = ("denoising", *order, "cas_sharpening"), enabled
        validate_stage_layout(*result, video=video)
        return result
    previous = PREVIOUS_VIDEO_STAGE_ORDER if video else PREVIOUS_IMAGE_STAGE_ORDER
    if len(order) == len(previous) and set(order) == set(previous):
        if len(enabled) != len(set(enabled)) or not set(enabled) <= set(order):
            raise ValueError("Pipeline enabled stages contain an unavailable or duplicate card.")
        result = ("denoising", *order, "coloring", "cas_sharpening"), enabled
        validate_stage_layout(*result, video=video)
        return result
    old = LEGACY_VIDEO_STAGE_ORDER if video else LEGACY_IMAGE_STAGE_ORDER
    if len(order) != len(old) or set(order) != set(old) or len(enabled) != len(set(enabled)) or not set(enabled) <= set(old):
        raise ValueError("Pipeline order contains unknown or duplicate processing cards.")
    scale_dlss_active = "scale_method" in enabled and scale_method == "DLSS"
    upscale_dlss_active = "super_resolution" in enabled and upscale_engine == "DLSS"
    if scale_dlss_active and upscale_dlss_active:
        dlss_origin = next(stage for stage in order if stage in {"scale_method", "super_resolution"})
    elif scale_dlss_active:
        dlss_origin = "scale_method"
    else:
        dlss_origin = "super_resolution"
    migrated_order: list[str] = []
    for stage in order:
        if stage == "scale_method":
            if dlss_origin == "scale_method":
                migrated_order.append("dlss_super_resolution")
            migrated_order.append(stage)
        elif stage == "super_resolution":
            if dlss_origin == "super_resolution":
                migrated_order.append("dlss_super_resolution")
            migrated_order.append(stage)
        else:
            migrated_order.append(stage)
    migrated_enabled = set(enabled) - {"scale_method", "super_resolution"}
    if "scale_method" in enabled:
        migrated_enabled.add("dlss_super_resolution" if scale_method == "DLSS" else "scale_method")
    if "super_resolution" in enabled:
        migrated_enabled.add("dlss_super_resolution" if upscale_engine == "DLSS" else "super_resolution")
    result = tuple(migrated_order), tuple(stage for stage in migrated_order if stage in migrated_enabled)
    result = ("denoising", *result[0], "coloring", "cas_sharpening"), result[1]
    validate_stage_layout(*result, video=video)
    return result


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
    image_stage_order: tuple[str, ...] = IMAGE_STAGE_ORDER
    video_stage_order: tuple[str, ...] = VIDEO_STAGE_ORDER
    image_enabled_stages: tuple[str, ...] = ()
    video_enabled_stages: tuple[str, ...] = ()
    denoise_strength: int = 50
    denoise_deblock: bool = True
    denoise_temporal: int = 30
    sharpening_method: str = "AMD CAS"
    # Legacy key retained so existing CAS settings and presets keep their value.
    cas_sharpness: int = 50
    coloring_mode: str = "Color Match"
    color_match_source: str = "Input Image"
    color_match_reference: str = ""
    lut_path: str = ""
    lut_resolution: int = 33
    lut_mix: int = 100
    lut_exposure: float = 0.0
    lut_contrast: int = 0
    lut_highlights: int = 0
    lut_shadows: int = 0
    lut_whites: int = 0
    lut_blacks: int = 0
    lut_midtones: int = 0
    lut_temperature: int = 0
    lut_tint: int = 0
    lut_hue: int = 0
    lut_vibrance: int = 0
    lut_saturation: int = 0
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
    # Validated native file-picker identity; intentionally omitted from config/presets.
    nr_mask: object | None = None
    upscaling_factor: float = 1.0
    image_scaling_filter: str = "Lanczos4"
    video_scaling_filter: str = "Spline36"
    nr_scale_method: str = "Standard"
    nr_dlss_mode: str = "Quality"
    nr_dlss_preset: str = "Default"
    nr_preview_length: str = "3"
    # Independent Live-tab mirrors of the NR controls above. The Live tab
    # reads/writes only these, so tuning or resetting Live never touches
    # the Neural Rendering tab (and vice versa). nr_mask stays shared:
    # only the Neural tab manages the custom mask.
    live_nr_style: str = "Default"
    live_nr_intensity: float = 1.0
    live_nr_passes: int = 1
    live_local_tone_strength: float = 1.0
    live_local_structure_strength: float = 1.0
    live_skin_structure_strength: float = -1.0
    live_nr_color_strength: float = 1.0
    live_tone_preservation: float = 0.0
    live_face_skin_protection: float = 0.0
    live_grain_preservation: float = 0.0
    live_shimmer_suppression: float = 0.70
    live_mask_feather: int = 0
    live_automatic_mask: bool = False
    live_upscaling_factor: float = 1.0
    # Factory default only. Existing saved codec values are loaded unchanged.
    codec: str = "H.264 (NVIDIA NVENC)"
    container: str = "MP4"
    quality: str = "Auto (Default)"
    hdr_mode: bool = False
    image_format: str = "PNG"
    image_quality: int = 95
    image_bit_depth: int = 8
    automatic_mask: bool = False
    image_rename_mode: str = "Auto"
    image_custom_suffix: str = "_Neural_Rendering"
    video_rename_mode: str = "Auto"
    video_custom_suffix: str = "_Neural_Rendering"
    frame_interpolation_target_fps: str = "60"
    frame_interpolation_engine: str = "Auto"
    # Factory default only. Existing saved selections are loaded unchanged.
    frame_interpolation_codec: str = "H.264 (NVIDIA NVENC)"
    frame_interpolation_container: str = "MP4"
    frame_interpolation_quality: str = "Auto (Default)"
    frame_interpolation_hdr_mode: bool = False
    frame_interpolation_rename_mode: str = "Auto"
    frame_interpolation_custom_suffix: str = "_Frame_Interpolation"
    frame_interpolation_preview_length: str = "3"
    preview_encoding: str = "Auto"
    upscale_mode: str = "Image"
    upscale_image_vsr_quality: int = 4
    upscale_image_engine: str = "RTX Video Super Resolution"
    upscale_image_dlss_mode: str = "Quality"
    upscale_image_dlss_preset: str = "Default"
    upscale_image_size_mode: str = "Scale factor"
    upscale_image_scale_factor: float = 2.0
    upscale_image_width: int = 3840
    upscale_image_height: int = 2160
    upscale_image_aspect_lock: bool = True
    upscale_image_output_format: str = "PNG"
    upscale_image_quality: int = 95
    upscale_image_rename_mode: str = "Auto"
    upscale_image_custom_suffix: str = "_Upscale"
    upscale_vsr_enabled: bool = True
    upscale_engine: str = "RTX Video Super Resolution"
    upscale_dlss_mode: str = "Quality"
    upscale_dlss_preset: str = "Default"
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
    upscale_container: str = "MKV"
    upscale_quality: str = "Auto (Default)"
    upscale_preview_length: str = "3"
    upscale_rename_mode: str = "Auto"
    upscale_custom_suffix: str = "_Upscale"

    def component_values(
        self,
    ) -> tuple[str, float, int, float, float, float, float, float, float, float, float, int, float, bool, str, str, str]:
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
            self.codec,
            self.container,
            self.quality,
        )


def live_effect_options(settings: UISettings) -> SimpleNamespace:
    """Namespace exposing the Live tab's independent NR values under the
    shared nr_* names consumed by the live pipeline (EffectSettings).

    The custom mask stays shared: only the Neural tab manages it.
    """
    return SimpleNamespace(
        nr_style=settings.live_nr_style,
        nr_intensity=settings.live_nr_intensity,
        nr_passes=settings.live_nr_passes,
        local_tone_strength=settings.live_local_tone_strength,
        local_structure_strength=settings.live_local_structure_strength,
        skin_structure_strength=settings.live_skin_structure_strength,
        nr_color_strength=settings.live_nr_color_strength,
        tone_preservation=settings.live_tone_preservation,
        face_skin_protection=settings.live_face_skin_protection,
        grain_preservation=settings.live_grain_preservation,
        shimmer_suppression=settings.live_shimmer_suppression,
        mask_feather=settings.live_mask_feather,
        nr_mask=settings.nr_mask,
        automatic_mask=settings.live_automatic_mask,
    )


DEFAULT_SETTINGS = UISettings()


def _validate(settings: UISettings) -> UISettings:
    validate_stage_layout(settings.image_stage_order, settings.image_enabled_stages, video=False)
    validate_stage_layout(settings.video_stage_order, settings.video_enabled_stages, video=True)
    for name in ("denoise_strength", "denoise_temporal", "cas_sharpness"):
        value = getattr(settings, name)
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 100:
            raise ValueError(f"{name} must be an integer from 0 to 100.")
    if not isinstance(settings.denoise_deblock, bool):
        raise ValueError("Compression artifact cleanup must be a boolean value.")
    if settings.sharpening_method not in SHARPENING_METHODS:
        raise ValueError(f"Unknown sharpening method: {settings.sharpening_method!r}.")
    if settings.coloring_mode not in COLORING_MODES:
        raise ValueError(f"Unknown Coloring mode: {settings.coloring_mode!r}.")
    if settings.color_match_source not in COLOR_MATCH_SOURCES:
        raise ValueError(f"Unknown Color Match source: {settings.color_match_source!r}.")
    if not isinstance(settings.color_match_reference, str) or len(settings.color_match_reference) > 4096:
        raise ValueError("Color Match reference must be a valid image path.")
    if not isinstance(settings.lut_path, str) or len(settings.lut_path) > 4096:
        raise ValueError("LUT must be a valid .cube file path.")
    if isinstance(settings.lut_resolution, bool) or settings.lut_resolution not in LUT_RESOLUTIONS:
        raise ValueError("LUT resolution must be 33, 65, or 129.")
    for name, (minimum, maximum) in LUT_ADJUSTMENT_RANGES.items():
        value = getattr(settings, name)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"{name} must be a finite number.")
        if name != "lut_exposure" and (not isinstance(value, int) or isinstance(value, bool)):
            raise ValueError(f"{name} must be an integer.")
        if not minimum <= value <= maximum:
            raise ValueError(f"{name} must be between {minimum} and {maximum}.")
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
    if isinstance(settings.live_nr_passes, bool) or not isinstance(settings.live_nr_passes, int):
        raise ValueError("Live NR Passes must be an integer from 1 to 4.")
    if not 1 <= settings.live_nr_passes <= 4:
        raise ValueError("Live NR Passes must be between 1 and 4.")
    if isinstance(settings.mask_feather, bool) or not isinstance(settings.mask_feather, int):
        raise ValueError("Mask Feather must be an integer from 0 to 128.")
    if not 0 <= settings.mask_feather <= 128:
        raise ValueError("Mask Feather must be between 0 and 128 pixels.")
    if isinstance(settings.live_mask_feather, bool) or not isinstance(settings.live_mask_feather, int):
        raise ValueError("Live Mask Feather must be an integer from 0 to 128.")
    if not 0 <= settings.live_mask_feather <= 128:
        raise ValueError("Live Mask Feather must be between 0 and 128 pixels.")
    resolve_upscaling_mode(settings.upscaling_factor)
    if settings.image_scaling_filter not in IMAGE_SCALING_FILTERS:
        raise ValueError(f"Unknown image scaling filter: {settings.image_scaling_filter!r}.")
    if settings.video_scaling_filter not in VIDEO_SCALING_FILTERS:
        raise ValueError(f"Unknown video scaling filter: {settings.video_scaling_filter!r}.")
    if settings.nr_scale_method not in DLSS_METHODS:
        raise ValueError(f"Unknown Neural Rendering scale method: {settings.nr_scale_method!r}.")
    validate_dlss(settings.nr_dlss_mode, settings.nr_dlss_preset)
    resolve_upscaling_mode(settings.live_upscaling_factor)
    if not isinstance(settings.automatic_mask, bool):
        raise ValueError("Automatic Mask must be a boolean value.")
    if not isinstance(settings.live_automatic_mask, bool):
        raise ValueError("Live Automatic Mask must be a boolean value.")
    if not isinstance(settings.hdr_mode, bool):
        raise ValueError("HDR Mode must be a boolean value.")
    if not isinstance(settings.frame_interpolation_hdr_mode, bool):
        raise ValueError("Frame Interpolation HDR Mode must be a boolean value.")
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
        "Frame Interpolation preview length": (
            settings.frame_interpolation_preview_length,
            PREVIEW_LENGTH_CHOICES,
        ),
        "Upscale preview length": (
            settings.upscale_preview_length,
            UPSCALE_PREVIEW_LENGTH_CHOICES,
        ),
        "Neural Rendering preview length": (
            settings.nr_preview_length,
            PREVIEW_LENGTH_CHOICES,
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
    if isinstance(settings.image_bit_depth, bool) or settings.image_bit_depth not in IMAGE_BIT_DEPTH_CHOICES:
        raise ValueError("Image bit depth must be 8 or 16 bits.")
    if settings.image_bit_depth == 16 and settings.image_format not in IMAGE_16BIT_FORMATS:
        raise ValueError("16-bit image output is available for PNG and TIFF only.")
    validate_rename(settings.image_rename_mode, settings.image_custom_suffix)
    validate_rename(settings.video_rename_mode, settings.video_custom_suffix)
    validate_rename(
        settings.frame_interpolation_rename_mode,
        settings.frame_interpolation_custom_suffix,
    )
    options_from_settings(settings).validate(for_render=False)
    image_upscale_options(settings).validate(for_render=False)
    return settings
