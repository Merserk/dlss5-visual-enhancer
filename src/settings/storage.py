from __future__ import annotations

import configparser
import json
import math
import os
import threading
from dataclasses import replace
from pathlib import Path

from ..core.ffmpeg import HDR_ALLOWED_CODECS
from ..core.paths import CONFIG_PATH
from ..core.naming import RENAME_MODES, validate_rename
from ..core.runtime import NR_STYLES, resolve_upscaling_mode
from ..core.dlss_modes import DLSS_METHODS, DLSS_MODES, DLSS_PRESETS
from ..frame_interpolation.models import ENGINE_CHOICES, FPS_CHOICES, PREVIEW_LENGTH_CHOICES
from .migration import _migrate_codec
from .models import (
    CODEC_CHOICES, CONFIG_SECTION, CONTAINER_CHOICES, DEFAULT_SETTINGS, IMAGE_FORMAT_CHOICES,
    IMAGE_16BIT_FORMATS, IMAGE_BIT_DEPTH_CHOICES,
    PREVIEW_ENCODING_CHOICES, QUALITY_CHOICES, UPSCALE_MODE_CHOICES,
    UPSCALE_PREVIEW_LENGTH_CHOICES, UISettings, _validate,
    IMAGE_STAGE_ORDER, VIDEO_STAGE_ORDER, IMAGE_SCALING_FILTERS, VIDEO_SCALING_FILTERS,
    LEGACY_IMAGE_STAGE_ORDER, LEGACY_VIDEO_STAGE_ORDER, COLORING_MODES, SHARPENING_METHODS,
    COLOR_MATCH_SOURCES, LUT_ADJUSTMENT_RANGES, LUT_RESOLUTIONS, migrate_stage_layout,
)
from ..upscale.video.models import SETTING_FIELDS, options_from_settings
from ..upscale.image.models import SETTING_FIELDS as IMAGE_UPSCALE_FIELDS, options_from_settings as image_upscale_options

def load_settings(path: str | os.PathLike[str]) -> UISettings:
    config_path = Path(path)
    parser = configparser.ConfigParser()
    try:
        with config_path.open("r", encoding="utf-8") as stream:
            parser.read_file(stream)
    except (OSError, configparser.Error):
        return DEFAULT_SETTINGS

    section = parser[CONFIG_SECTION] if parser.has_section(CONFIG_SECTION) else {}

    def choice(key: str, choices: tuple[str, ...], default: str) -> str:
        value = section.get(key, default)
        # Migrate old HEVC naming before validation
        if key in ("codec", "frame_interpolation_codec"):
            migrated = _migrate_codec(value)
            if migrated in choices:
                return migrated
        return value if value in choices else default

    def codec_choice(key: str, default: str) -> str:
        raw = section.get(key, default)
        migrated = _migrate_codec(raw)
        if migrated in CODEC_CHOICES:
            return migrated
        return default

    def number(key: str, minimum: float, maximum: float, default: float) -> float:
        try:
            value = float(section.get(key, str(default)))
        except (TypeError, ValueError):
            return default
        return value if math.isfinite(value) and minimum <= value <= maximum else default

    def upscaling_factor() -> float:
        try:
            return resolve_upscaling_mode(float(section.get("upscaling_factor", "1.0")))[0]
        except (TypeError, ValueError):
            return DEFAULT_SETTINGS.upscaling_factor

    def image_quality() -> int:
        value = number("image_quality", 1, 100, DEFAULT_SETTINGS.image_quality)
        return int(value) if float(value).is_integer() else DEFAULT_SETTINGS.image_quality

    def integer(key: str, minimum: int, maximum: int, default: int) -> int:
        value = number(key, minimum, maximum, default)
        return int(value) if float(value).is_integer() else default

    def lut_resolution() -> int:
        value = integer("lut_resolution", min(LUT_RESOLUTIONS), max(LUT_RESOLUTIONS),
                        DEFAULT_SETTINGS.lut_resolution)
        return value if value in LUT_RESOLUTIONS else DEFAULT_SETTINGS.lut_resolution

    def boolean(key: str, default: bool) -> bool:
        raw_value = section.get(key)
        if raw_value is None:
            return default
        parsed = configparser.ConfigParser.BOOLEAN_STATES.get(str(raw_value).casefold())
        return parsed if parsed is not None else default

    def hdr_mode_value() -> bool:
        # Support both new 'hdr_mode' and legacy 'preserve_hdr' keys
        raw = section.get("hdr_mode")
        if raw is None:
            raw = section.get("preserve_hdr")
        if raw is None:
            return DEFAULT_SETTINGS.hdr_mode
        parsed = configparser.ConfigParser.BOOLEAN_STATES.get(str(raw).casefold())
        return parsed if parsed is not None else DEFAULT_SETTINGS.hdr_mode

    def fi_hdr_mode_value() -> bool:
        raw = section.get("frame_interpolation_hdr_mode")
        if raw is None:
            # Check legacy if ever used
            raw = section.get("frame_interpolation_preserve_hdr")
        if raw is None:
            return DEFAULT_SETTINGS.frame_interpolation_hdr_mode
        parsed = configparser.ConfigParser.BOOLEAN_STATES.get(str(raw).casefold())
        return parsed if parsed is not None else DEFAULT_SETTINGS.frame_interpolation_hdr_mode

    def frame_interpolation_engine() -> str:
        value = section.get(
            "frame_interpolation_engine",
            DEFAULT_SETTINGS.frame_interpolation_engine,
        )
        if value == "Experimental Cascade":
            return "Cascade"
        return value if value in ENGINE_CHOICES else DEFAULT_SETTINGS.frame_interpolation_engine

    image_format = choice("image_format", IMAGE_FORMAT_CHOICES, DEFAULT_SETTINGS.image_format)
    image_bit_depth = integer("image_bit_depth", 8, 16, DEFAULT_SETTINGS.image_bit_depth)
    if image_bit_depth not in IMAGE_BIT_DEPTH_CHOICES or (image_bit_depth == 16 and image_format not in IMAGE_16BIT_FORMATS):
        image_bit_depth = DEFAULT_SETTINGS.image_bit_depth

    image_rename_mode = choice(
        "image_rename_mode", RENAME_MODES, DEFAULT_SETTINGS.image_rename_mode
    )
    image_custom_suffix = section.get(
        "image_custom_suffix", DEFAULT_SETTINGS.image_custom_suffix
    )
    video_rename_mode = choice(
        "video_rename_mode", RENAME_MODES, DEFAULT_SETTINGS.video_rename_mode
    )
    video_custom_suffix = section.get(
        "video_custom_suffix", DEFAULT_SETTINGS.video_custom_suffix
    )
    frame_interpolation_rename_mode = choice(
        "frame_interpolation_rename_mode",
        RENAME_MODES,
        DEFAULT_SETTINGS.frame_interpolation_rename_mode,
    )
    frame_interpolation_custom_suffix = section.get(
        "frame_interpolation_custom_suffix",
        DEFAULT_SETTINGS.frame_interpolation_custom_suffix,
    )
    try:
        validate_rename(image_rename_mode, image_custom_suffix)
    except ValueError:
        image_rename_mode = DEFAULT_SETTINGS.image_rename_mode
        image_custom_suffix = DEFAULT_SETTINGS.image_custom_suffix
    try:
        validate_rename(video_rename_mode, video_custom_suffix)
    except ValueError:
        video_rename_mode = DEFAULT_SETTINGS.video_rename_mode
        video_custom_suffix = DEFAULT_SETTINGS.video_custom_suffix
    try:
        validate_rename(
            frame_interpolation_rename_mode,
            frame_interpolation_custom_suffix,
        )
    except ValueError:
        frame_interpolation_rename_mode = DEFAULT_SETTINGS.frame_interpolation_rename_mode
        frame_interpolation_custom_suffix = DEFAULT_SETTINGS.frame_interpolation_custom_suffix

    settings = UISettings(
        ai_gpu_uuid=section.get("ai_gpu_uuid", DEFAULT_SETTINGS.ai_gpu_uuid).strip()
        or DEFAULT_SETTINGS.ai_gpu_uuid,
        video_gpu_uuid=section.get("video_gpu_uuid", DEFAULT_SETTINGS.video_gpu_uuid).strip()
        or DEFAULT_SETTINGS.video_gpu_uuid,
        nr_style=choice("nr_style", tuple(NR_STYLES), DEFAULT_SETTINGS.nr_style),
        nr_intensity=number("nr_intensity", 0.0, 2.0, DEFAULT_SETTINGS.nr_intensity),
        nr_passes=integer("nr_passes", 1, 4, DEFAULT_SETTINGS.nr_passes),
        local_tone_strength=number(
            "local_tone_strength", 0.0, 2.0, DEFAULT_SETTINGS.local_tone_strength
        ),
        local_structure_strength=number(
            "local_structure_strength", 0.0, 2.0, DEFAULT_SETTINGS.local_structure_strength
        ),
        skin_structure_strength=number(
            "skin_structure_strength", -1.0, 2.0, DEFAULT_SETTINGS.skin_structure_strength
        ),
        nr_color_strength=number(
            "nr_color_strength", 0.0, 1.0, DEFAULT_SETTINGS.nr_color_strength
        ),
        tone_preservation=number(
            "tone_preservation", 0.0, 1.0, DEFAULT_SETTINGS.tone_preservation
        ),
        face_skin_protection=number(
            "face_skin_protection", 0.0, 1.0, DEFAULT_SETTINGS.face_skin_protection
        ),
        grain_preservation=number(
            "grain_preservation", 0.0, 1.0, DEFAULT_SETTINGS.grain_preservation
        ),
        shimmer_suppression=number(
            "shimmer_suppression", 0.0, 1.0, DEFAULT_SETTINGS.shimmer_suppression
        ),
        mask_feather=int(number(
            "mask_feather", 0, 128, DEFAULT_SETTINGS.mask_feather
        )),
        automatic_mask=boolean("automatic_mask", DEFAULT_SETTINGS.automatic_mask),
        upscaling_factor=upscaling_factor(),
        image_scaling_filter=choice("image_scaling_filter", IMAGE_SCALING_FILTERS,
                                    DEFAULT_SETTINGS.image_scaling_filter),
        video_scaling_filter=choice(
            "video_scaling_filter", VIDEO_SCALING_FILTERS,
            "Lanczos" if section.get("nr_scale_method") == "Standard"
            else DEFAULT_SETTINGS.video_scaling_filter,
        ),
        nr_scale_method=choice("nr_scale_method", DLSS_METHODS, DEFAULT_SETTINGS.nr_scale_method),
        nr_dlss_mode=choice("nr_dlss_mode", tuple(DLSS_MODES), DEFAULT_SETTINGS.nr_dlss_mode),
        nr_dlss_preset=choice("nr_dlss_preset", DLSS_PRESETS, DEFAULT_SETTINGS.nr_dlss_preset),
        nr_preview_length=choice(
            "nr_preview_length",
            PREVIEW_LENGTH_CHOICES,
            DEFAULT_SETTINGS.nr_preview_length,
        ),
        codec=codec_choice("codec", DEFAULT_SETTINGS.codec),
        container=choice("container", CONTAINER_CHOICES, DEFAULT_SETTINGS.container),
        quality=choice("quality", QUALITY_CHOICES, DEFAULT_SETTINGS.quality),
        hdr_mode=hdr_mode_value(),
        image_format=image_format,
        image_quality=image_quality(),
        image_bit_depth=image_bit_depth,
        denoise_strength=integer("denoise_strength", 0, 100, DEFAULT_SETTINGS.denoise_strength),
        denoise_deblock=boolean("denoise_deblock", DEFAULT_SETTINGS.denoise_deblock),
        denoise_temporal=integer("denoise_temporal", 0, 100, DEFAULT_SETTINGS.denoise_temporal),
        cas_sharpness=integer("cas_sharpness", 0, 100, DEFAULT_SETTINGS.cas_sharpness),
        sharpening_method=choice("sharpening_method", SHARPENING_METHODS,
                                 DEFAULT_SETTINGS.sharpening_method),
        coloring_mode=("LUT" if section.get("coloring_mode") == "Mode 2" else
                       choice("coloring_mode", COLORING_MODES, DEFAULT_SETTINGS.coloring_mode)),
        color_match_source=choice("color_match_source", COLOR_MATCH_SOURCES,
                                  DEFAULT_SETTINGS.color_match_source),
        color_match_reference=section.get("color_match_reference", "")[:4096],
        lut_path=section.get("lut_path", "")[:4096],
        lut_resolution=lut_resolution(),
        **{key: (number(key, *bounds, getattr(DEFAULT_SETTINGS, key)) if key == "lut_exposure"
                 else integer(key, *bounds, getattr(DEFAULT_SETTINGS, key)))
           for key, bounds in LUT_ADJUSTMENT_RANGES.items()},
        image_rename_mode=image_rename_mode,
        image_custom_suffix=image_custom_suffix,
        video_rename_mode=video_rename_mode,
        video_custom_suffix=video_custom_suffix,
        frame_interpolation_target_fps=choice(
            "frame_interpolation_target_fps",
            FPS_CHOICES,
            DEFAULT_SETTINGS.frame_interpolation_target_fps,
        ),
        frame_interpolation_engine=frame_interpolation_engine(),
        frame_interpolation_codec=codec_choice(
            "frame_interpolation_codec",
            DEFAULT_SETTINGS.frame_interpolation_codec,
        ),
        frame_interpolation_container=choice(
            "frame_interpolation_container",
            CONTAINER_CHOICES,
            DEFAULT_SETTINGS.frame_interpolation_container,
        ),
        frame_interpolation_quality=choice(
            "frame_interpolation_quality",
            QUALITY_CHOICES,
            DEFAULT_SETTINGS.frame_interpolation_quality,
        ),
        frame_interpolation_hdr_mode=fi_hdr_mode_value(),
        frame_interpolation_preview_length=choice(
            "frame_interpolation_preview_length",
            PREVIEW_LENGTH_CHOICES,
            DEFAULT_SETTINGS.frame_interpolation_preview_length,
        ),
        frame_interpolation_rename_mode=frame_interpolation_rename_mode,
        frame_interpolation_custom_suffix=frame_interpolation_custom_suffix,
        preview_encoding=choice(
            "preview_encoding",
            PREVIEW_ENCODING_CHOICES,
            DEFAULT_SETTINGS.preview_encoding,
        ),
        upscale_mode=choice(
            "upscale_mode",
            UPSCALE_MODE_CHOICES,
            DEFAULT_SETTINGS.upscale_mode,
        ),
        upscale_preview_length=choice(
            "upscale_preview_length",
            UPSCALE_PREVIEW_LENGTH_CHOICES,
            DEFAULT_SETTINGS.upscale_preview_length,
        ),
    )
    # First-run migration: Live mirrors default to the loaded shared NR
    # values so previous Live tunings survive the split. Factory defaults
    # apply only to fresh configs and to per-tab Reset.
    def live_upscaling_factor() -> float:
        try:
            return resolve_upscaling_mode(float(section.get(
                "live_upscaling_factor", str(settings.upscaling_factor))))[0]
        except (TypeError, ValueError):
            return settings.upscaling_factor
    settings = replace(
        settings,
        live_nr_style=choice("live_nr_style", tuple(NR_STYLES), settings.nr_style),
        live_nr_intensity=number("live_nr_intensity", 0.0, 2.0, settings.nr_intensity),
        live_nr_passes=integer("live_nr_passes", 1, 4, settings.nr_passes),
        live_local_tone_strength=number(
            "live_local_tone_strength", 0.0, 2.0, settings.local_tone_strength
        ),
        live_local_structure_strength=number(
            "live_local_structure_strength", 0.0, 2.0, settings.local_structure_strength
        ),
        live_skin_structure_strength=number(
            "live_skin_structure_strength", -1.0, 2.0, settings.skin_structure_strength
        ),
        live_nr_color_strength=number(
            "live_nr_color_strength", 0.0, 1.0, settings.nr_color_strength
        ),
        live_tone_preservation=number(
            "live_tone_preservation", 0.0, 1.0, settings.tone_preservation
        ),
        live_face_skin_protection=number(
            "live_face_skin_protection", 0.0, 1.0, settings.face_skin_protection
        ),
        live_grain_preservation=number(
            "live_grain_preservation", 0.0, 1.0, settings.grain_preservation
        ),
        live_shimmer_suppression=number(
            "live_shimmer_suppression", 0.0, 1.0, settings.shimmer_suppression
        ),
        live_mask_feather=int(number(
            "live_mask_feather", 0, 128, settings.mask_feather
        )),
        live_automatic_mask=boolean("live_automatic_mask", settings.automatic_mask),
        live_upscaling_factor=live_upscaling_factor(),
    )
    # Auto-disable HDR Mode if codec does not support it (e.g. H.264)
    try:
        if settings.hdr_mode and settings.codec not in HDR_ALLOWED_CODECS:
            settings = replace(settings, hdr_mode=False)
        if settings.frame_interpolation_hdr_mode and settings.frame_interpolation_codec not in HDR_ALLOWED_CODECS:
            settings = replace(settings, frame_interpolation_hdr_mode=False)
    except Exception:
        pass
    upscale_values = {}
    for prefix, names, factory in (("upscale_", SETTING_FIELDS, options_from_settings),
                                    ("upscale_image_", IMAGE_UPSCALE_FIELDS, image_upscale_options)):
        for name in names:
            key = prefix + name
            default = getattr(DEFAULT_SETTINGS, key)
            raw = section.get(key)
            if raw is None:
                continue
            try:
                if isinstance(default, bool):
                    value = boolean(key, default)
                elif isinstance(default, int):
                    value = int(raw)
                elif isinstance(default, float):
                    value = float(raw)
                else:
                    value = str(raw)
                if name == "vsr_quality" and value == 0:
                    value = default
                candidate = replace(settings, **upscale_values, **{key: value})
                factory(candidate).validate(for_render=False)
                upscale_values[key] = value
            except (ValueError, TypeError, OverflowError):
                continue
    settings = replace(settings, **upscale_values)

    def stage_values(order_key: str, enabled_key: str, default_order: tuple[str, ...], video: bool):
        try:
            order = tuple(json.loads(section.get(order_key, json.dumps(default_order))))
            enabled = tuple(json.loads(section.get(enabled_key, "[]")))
            legacy_order = LEGACY_VIDEO_STAGE_ORDER if video else LEGACY_IMAGE_STAGE_ORDER
            legacy = len(order) == len(legacy_order) and set(order) == set(legacy_order)
            engine = settings.upscale_engine if video else settings.upscale_image_engine
            migrated = migrate_stage_layout(order, enabled, video=video,
                                            scale_method=settings.nr_scale_method,
                                            upscale_engine=engine)
            return migrated[0], migrated[1], (order, enabled) if legacy else None
        except (TypeError, ValueError, json.JSONDecodeError):
            return default_order, (), None

    image_order, image_enabled, old_image = stage_values("image_stage_order", "image_enabled_stages", IMAGE_STAGE_ORDER, False)
    video_order, video_enabled, old_video = stage_values("video_stage_order", "video_enabled_stages", VIDEO_STAGE_ORDER, True)

    def scale_dlss_wins(old_layout, engine: str) -> bool:
        if not old_layout or settings.nr_scale_method != "DLSS":
            return False
        order, enabled = old_layout
        return ("scale_method" in enabled and
                ("super_resolution" not in enabled or engine != "DLSS" or
                 order.index("scale_method") < order.index("super_resolution")))

    image_scale_wins = scale_dlss_wins(old_image, settings.upscale_image_engine)
    video_scale_wins = scale_dlss_wins(old_video, settings.upscale_engine)
    return replace(
        settings,
        image_stage_order=image_order, image_enabled_stages=image_enabled,
        video_stage_order=video_order, video_enabled_stages=video_enabled,
        upscale_image_dlss_mode=settings.nr_dlss_mode if image_scale_wins else settings.upscale_image_dlss_mode,
        upscale_image_dlss_preset=settings.nr_dlss_preset if image_scale_wins else settings.upscale_image_dlss_preset,
        upscale_dlss_mode=settings.nr_dlss_mode if video_scale_wins else settings.upscale_dlss_mode,
        upscale_dlss_preset=settings.nr_dlss_preset if video_scale_wins else settings.upscale_dlss_preset,
        upscale_image_engine="RTX Video Super Resolution",
        upscale_engine="RTX Video Super Resolution",
        upscale_vsr_enabled=settings.upscale_vsr_enabled or not settings.upscale_hdr_enabled,
        nr_scale_method="Standard",
    )


def save_settings(path: str | os.PathLike[str], settings: UISettings) -> None:
    settings = _validate(settings)
    config_path = Path(path)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    parser = configparser.ConfigParser()
    parser[CONFIG_SECTION] = {
        "image_stage_order": json.dumps(settings.image_stage_order),
        "video_stage_order": json.dumps(settings.video_stage_order),
        "image_enabled_stages": json.dumps(settings.image_enabled_stages),
        "video_enabled_stages": json.dumps(settings.video_enabled_stages),
        "denoise_strength": str(settings.denoise_strength),
        "denoise_deblock": str(settings.denoise_deblock).lower(),
        "denoise_temporal": str(settings.denoise_temporal),
        "cas_sharpness": str(settings.cas_sharpness),
        "sharpening_method": settings.sharpening_method,
        "coloring_mode": settings.coloring_mode,
        "color_match_source": settings.color_match_source,
        "color_match_reference": settings.color_match_reference.replace("%", "%%"),
        "lut_path": settings.lut_path.replace("%", "%%"),
        "lut_resolution": str(settings.lut_resolution),
        **{key: str(getattr(settings, key)) for key in LUT_ADJUSTMENT_RANGES},
        **{"upscale_image_" + name: str(getattr(settings, "upscale_image_" + name)) for name in IMAGE_UPSCALE_FIELDS},
        **{"upscale_" + name: str(getattr(settings, "upscale_" + name)) for name in SETTING_FIELDS},
        "upscale_mode": settings.upscale_mode,
        "upscale_preview_length": settings.upscale_preview_length,
        "ai_gpu_uuid": settings.ai_gpu_uuid,
        "video_gpu_uuid": settings.video_gpu_uuid,
        "nr_style": settings.nr_style,
        "nr_intensity": f"{settings.nr_intensity:.2f}",
        "nr_passes": str(settings.nr_passes),
        "local_tone_strength": f"{settings.local_tone_strength:.2f}",
        "local_structure_strength": f"{settings.local_structure_strength:.2f}",
        "skin_structure_strength": f"{settings.skin_structure_strength:.2f}",
        "nr_color_strength": f"{settings.nr_color_strength:.2f}",
        "tone_preservation": f"{settings.tone_preservation:.2f}",
        "face_skin_protection": f"{settings.face_skin_protection:.2f}",
        "grain_preservation": f"{settings.grain_preservation:.2f}",
        "shimmer_suppression": f"{settings.shimmer_suppression:.2f}",
        "mask_feather": str(settings.mask_feather),
        "automatic_mask": str(settings.automatic_mask).lower(),
        "upscaling_factor": f"{settings.upscaling_factor:g}",
        "image_scaling_filter": settings.image_scaling_filter,
        "video_scaling_filter": settings.video_scaling_filter,
        "nr_scale_method": settings.nr_scale_method,
        "nr_dlss_mode": settings.nr_dlss_mode,
        "nr_dlss_preset": settings.nr_dlss_preset,
        "nr_preview_length": settings.nr_preview_length,
        "live_nr_style": settings.live_nr_style,
        "live_nr_intensity": f"{settings.live_nr_intensity:.2f}",
        "live_nr_passes": str(settings.live_nr_passes),
        "live_local_tone_strength": f"{settings.live_local_tone_strength:.2f}",
        "live_local_structure_strength": f"{settings.live_local_structure_strength:.2f}",
        "live_skin_structure_strength": f"{settings.live_skin_structure_strength:.2f}",
        "live_nr_color_strength": f"{settings.live_nr_color_strength:.2f}",
        "live_tone_preservation": f"{settings.live_tone_preservation:.2f}",
        "live_face_skin_protection": f"{settings.live_face_skin_protection:.2f}",
        "live_grain_preservation": f"{settings.live_grain_preservation:.2f}",
        "live_shimmer_suppression": f"{settings.live_shimmer_suppression:.2f}",
        "live_mask_feather": str(settings.live_mask_feather),
        "live_automatic_mask": str(settings.live_automatic_mask).lower(),
        "live_upscaling_factor": f"{settings.live_upscaling_factor:g}",
        "codec": settings.codec,
        "container": settings.container,
        "quality": settings.quality,
        "hdr_mode": str(settings.hdr_mode).lower(),
        "image_format": settings.image_format,
        "image_quality": str(settings.image_quality),
        "image_bit_depth": str(settings.image_bit_depth),
        "image_rename_mode": settings.image_rename_mode,
        "image_custom_suffix": settings.image_custom_suffix,
        "video_rename_mode": settings.video_rename_mode,
        "video_custom_suffix": settings.video_custom_suffix,
        "frame_interpolation_target_fps": settings.frame_interpolation_target_fps,
        "frame_interpolation_engine": settings.frame_interpolation_engine,
        "frame_interpolation_codec": settings.frame_interpolation_codec,
        "frame_interpolation_container": settings.frame_interpolation_container,
        "frame_interpolation_quality": settings.frame_interpolation_quality,
        "frame_interpolation_hdr_mode": str(settings.frame_interpolation_hdr_mode).lower(),
        "frame_interpolation_preview_length": settings.frame_interpolation_preview_length,
        "frame_interpolation_rename_mode": settings.frame_interpolation_rename_mode,
        "frame_interpolation_custom_suffix": settings.frame_interpolation_custom_suffix,
        "preview_encoding": settings.preview_encoding,
    }

    temporary = config_path.with_name(f".{config_path.name}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="") as stream:
            parser.write(stream)
        os.replace(temporary, config_path)
    finally:
        if temporary.exists():
            temporary.unlink()


class _SettingsState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.current: UISettings | None = None


SETTINGS_STATE = _SettingsState()


def processing_gpu_settings() -> tuple[str, str]:
    with SETTINGS_STATE.lock:
        settings = SETTINGS_STATE.current or load_settings(CONFIG_PATH)
    return settings.ai_gpu_uuid, settings.video_gpu_uuid


def current_preview_encoding() -> str:
    from ..core.ffmpeg.preview import normalize_preview_encoding

    with SETTINGS_STATE.lock:
        settings = SETTINGS_STATE.current or load_settings(CONFIG_PATH)
    return normalize_preview_encoding(settings.preview_encoding)
