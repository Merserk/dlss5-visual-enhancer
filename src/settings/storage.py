from __future__ import annotations

import configparser
import math
import os
import threading
from dataclasses import replace
from pathlib import Path

from ..core.i18n import normalize_language
from ..core.ffmpeg import HDR_ALLOWED_CODECS
from ..core.paths import CONFIG_PATH
from ..core.naming import RENAME_MODES, validate_rename
from ..core.runtime import NR_STYLES, resolve_upscaling_mode
from ..frame_interpolation.models import ENGINE_CHOICES, FPS_CHOICES
from .migration import _migrate_codec
from .models import (
    CODEC_CHOICES, CONFIG_SECTION, CONTAINER_CHOICES, DEFAULT_SETTINGS, IMAGE_FORMAT_CHOICES,
    PREVIEW_ENCODING_CHOICES, QUALITY_CHOICES, UPSCALE_MODE_CHOICES, UISettings, _validate,
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
        language=normalize_language(section.get("language")),
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
        nr_gpu_mode=boolean("nr_gpu_mode", DEFAULT_SETTINGS.nr_gpu_mode),
        upscaling_factor=upscaling_factor(),
        codec=codec_choice("codec", DEFAULT_SETTINGS.codec),
        container=choice("container", CONTAINER_CHOICES, DEFAULT_SETTINGS.container),
        quality=choice("quality", QUALITY_CHOICES, DEFAULT_SETTINGS.quality),
        hdr_mode=hdr_mode_value(),
        image_format=choice(
            "image_format", IMAGE_FORMAT_CHOICES, DEFAULT_SETTINGS.image_format
        ),
        image_quality=image_quality(),
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
        frame_interpolation_gpu_mode=boolean(
            "frame_interpolation_gpu_mode", DEFAULT_SETTINGS.frame_interpolation_gpu_mode),
        frame_interpolation_rename_mode=frame_interpolation_rename_mode,
        frame_interpolation_custom_suffix=frame_interpolation_custom_suffix,
        preview_encoding=choice(
            "preview_encoding",
            PREVIEW_ENCODING_CHOICES,
            DEFAULT_SETTINGS.preview_encoding,
        ),
        full_size_image_previews=boolean(
            "full_size_image_previews", DEFAULT_SETTINGS.full_size_image_previews
        ),
        upscale_mode=choice(
            "upscale_mode",
            UPSCALE_MODE_CHOICES,
            DEFAULT_SETTINGS.upscale_mode,
        ),
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
    return replace(settings, **upscale_values)


def save_settings(path: str | os.PathLike[str], settings: UISettings) -> None:
    settings = _validate(settings)
    config_path = Path(path)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    parser = configparser.ConfigParser()
    parser[CONFIG_SECTION] = {
        **{"upscale_image_" + name: str(getattr(settings, "upscale_image_" + name)) for name in IMAGE_UPSCALE_FIELDS},
        **{"upscale_" + name: str(getattr(settings, "upscale_" + name)) for name in SETTING_FIELDS},
        "upscale_mode": settings.upscale_mode,
        "language": settings.language,
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
        "nr_gpu_mode": str(settings.nr_gpu_mode).lower(),
        "upscaling_factor": f"{settings.upscaling_factor:g}",
        "codec": settings.codec,
        "container": settings.container,
        "quality": settings.quality,
        "hdr_mode": str(settings.hdr_mode).lower(),
        "image_format": settings.image_format,
        "image_quality": str(settings.image_quality),
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
        "frame_interpolation_gpu_mode": str(settings.frame_interpolation_gpu_mode).lower(),
        "frame_interpolation_rename_mode": settings.frame_interpolation_rename_mode,
        "frame_interpolation_custom_suffix": settings.frame_interpolation_custom_suffix,
        "preview_encoding": settings.preview_encoding,
        "full_size_image_previews": str(settings.full_size_image_previews).lower(),
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


def current_settings() -> UISettings:
    with SETTINGS_STATE.lock:
        return SETTINGS_STATE.current or load_settings(CONFIG_PATH)


def processing_gpu_settings() -> tuple[str, str]:
    with SETTINGS_STATE.lock:
        settings = SETTINGS_STATE.current or load_settings(CONFIG_PATH)
    return settings.ai_gpu_uuid, settings.video_gpu_uuid


def current_preview_encoding() -> str:
    from ..core.ffmpeg.preview import normalize_preview_encoding

    with SETTINGS_STATE.lock:
        settings = SETTINGS_STATE.current or load_settings(CONFIG_PATH)
    return normalize_preview_encoding(settings.preview_encoding)


def full_size_image_previews_enabled() -> bool:
    """Return the saved image-preview quality mode for one operation snapshot."""
    with SETTINGS_STATE.lock:
        settings = SETTINGS_STATE.current or load_settings(CONFIG_PATH)
    return bool(settings.full_size_image_previews)
