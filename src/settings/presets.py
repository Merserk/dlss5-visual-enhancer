from __future__ import annotations

import json
import math
import os
import re
import tempfile
from dataclasses import asdict, fields, replace
from pathlib import Path
from typing import Any

from ..core.paths import APP_TEMP
from .models import (
    DEFAULT_SETTINGS, MAX_PRESET_BYTES, PRESET_FORMAT, PRESET_SCHEMA_VERSION, UISettings, _validate,
    LUT_ADJUSTMENT_RANGES, migrate_stage_layout,
)

_WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}

def _preset_name(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("Preset name must be text.")
    name = value.strip()
    if not name:
        raise ValueError("Enter a preset name before exporting.")
    if len(name) > 120:
        raise ValueError("Preset name must be 120 characters or fewer.")
    if any(ord(character) < 32 for character in name):
        raise ValueError("Preset name cannot contain control characters.")
    return name


def preset_filename(name: str) -> str:
    """Return a portable JSON filename while preserving the display name in the file."""
    display_name = _preset_name(name)
    characters = [
        character if character.isalnum() or character in "-_" else "_"
        for character in display_name
    ]
    stem = re.sub(r"_+", "_", "".join(characters)).strip("-_")[:80].rstrip("-_")
    if not stem:
        raise ValueError("Preset name must contain at least one letter or number.")
    if stem.upper() in _WINDOWS_RESERVED_NAMES:
        stem += "_preset"
    return f"{stem}.json"


def preset_document(name: str, settings: UISettings) -> dict[str, Any]:
    """Build the versioned user-facing preset document."""
    display_name = _preset_name(name)
    _validate(settings)
    values = asdict(settings)
    values.pop("nr_mask", None)
    return {
        "format": PRESET_FORMAT,
        "schema_version": PRESET_SCHEMA_VERSION,
        "name": display_name,
        "settings": values,
    }


def export_settings_preset(name: str, settings: UISettings) -> Path:
    """Write a validated preset to an isolated temporary download directory."""
    document = preset_document(name, settings)
    filename = preset_filename(document["name"])
    APP_TEMP.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix="dlss5-settings-preset-", dir=APP_TEMP))
    path = directory / filename
    path.write_text(
        json.dumps(document, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return path.resolve()


def _coerce_preset_value(field_name: str, value: Any, current: UISettings) -> Any:
    expected = getattr(current, field_name)
    if isinstance(expected, tuple):
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise ValueError(f"Preset setting {field_name!r} must be a list of stage IDs.")
        return tuple(value)
    if isinstance(expected, bool):
        if not isinstance(value, bool):
            raise ValueError(f"Preset setting {field_name!r} must be a boolean.")
        return value
    if isinstance(expected, int):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"Preset setting {field_name!r} must be an integer.")
        return value
    if isinstance(expected, float):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"Preset setting {field_name!r} must be a number.")
        number = float(value)
        if not math.isfinite(number):
            raise ValueError(f"Preset setting {field_name!r} must be finite.")
        return number
    if isinstance(expected, str):
        if not isinstance(value, str):
            raise ValueError(f"Preset setting {field_name!r} must be text.")
        return value
    raise ValueError(f"Preset setting {field_name!r} has an unsupported type.")


def import_settings_preset(
    path: str | os.PathLike[str], current: UISettings
) -> tuple[str, UISettings]:
    """Load and migrate all supported preset schemas, then validate atomically."""
    preset_path = Path(path)
    if preset_path.suffix.casefold() != ".json":
        raise ValueError("Choose a JSON preset file.")
    try:
        size = preset_path.stat().st_size
    except OSError as exc:
        raise ValueError("The selected preset file cannot be read.") from exc
    if size > MAX_PRESET_BYTES:
        raise ValueError("Preset file is too large; the maximum size is 1 MiB.")
    try:
        document = json.loads(preset_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("Preset file is not valid UTF-8 JSON.") from exc
    if not isinstance(document, dict):
        raise ValueError("Preset JSON must contain an object at its top level.")
    if document.get("format") != PRESET_FORMAT:
        raise ValueError("This JSON file is not a Visual Enhancer settings preset.")
    version = document.get("schema_version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise ValueError("Preset schema_version must be an integer.")
    if version not in range(1, PRESET_SCHEMA_VERSION + 1):
        direction = "newer" if version > PRESET_SCHEMA_VERSION else "unsupported"
        raise ValueError(
            f"Preset schema version {version} is {direction}; this build supports version "
            f"{PRESET_SCHEMA_VERSION}."
        )
    name = _preset_name(document.get("name"))
    imported = document.get("settings")
    if not isinstance(imported, dict):
        raise ValueError("Preset settings must be a JSON object.")

    known_names = {field.name for field in fields(UISettings)}
    changes = {
        key: _coerce_preset_value(key, value, current)
        for key, value in imported.items()
        if key in known_names and key != "nr_mask"
    }
    if version == 1:
        # v1's DLSS model-preset field was never applied by feature 18. Ignore
        # it. (The retired GPU staging switch from old presets is filtered by
        # known_names above; the runtime path is codec-driven now.)
        changes.pop("dlss_model_preset", None)
    if version < 3:
        changes["nr_color_strength"] = DEFAULT_SETTINGS.nr_color_strength
        changes["tone_preservation"] = DEFAULT_SETTINGS.tone_preservation
        changes["mask_feather"] = DEFAULT_SETTINGS.mask_feather
    if version < 4:
        changes["face_skin_protection"] = DEFAULT_SETTINGS.face_skin_protection
        changes["grain_preservation"] = DEFAULT_SETTINGS.grain_preservation
    if version < 5:
        changes["nr_passes"] = DEFAULT_SETTINGS.nr_passes
    if version < 6:
        changes["shimmer_suppression"] = DEFAULT_SETTINGS.shimmer_suppression
    if version < 8:
        for key in (
            "nr_scale_method", "nr_dlss_mode", "nr_dlss_preset",
            "upscale_image_engine", "upscale_image_dlss_mode", "upscale_image_dlss_preset",
            "upscale_engine", "upscale_dlss_mode", "upscale_dlss_preset",
        ):
            changes[key] = getattr(DEFAULT_SETTINGS, key)
    if version < 9:
        for key in ("image_stage_order", "video_stage_order", "image_enabled_stages", "video_enabled_stages"):
            changes[key] = getattr(DEFAULT_SETTINGS, key)
    if version < 10:
        old_scale = changes.get("nr_scale_method", DEFAULT_SETTINGS.nr_scale_method)
        changes["image_scaling_filter"] = "Lanczos4"
        changes["video_scaling_filter"] = (
            "Lanczos" if old_scale == "Standard" else DEFAULT_SETTINGS.video_scaling_filter
        )
        if version == 9:
            for mode, prefix in (("Image", "image"), ("Video", "video")):
                order_key = f"{prefix}_stage_order"
                enabled_key = f"{prefix}_enabled_stages"
                old_order = changes.get(order_key, getattr(DEFAULT_SETTINGS, order_key))
                old_enabled = changes.get(enabled_key, ())
                engine_key = "upscale_image_engine" if mode == "Image" else "upscale_engine"
                engine = changes.get(engine_key, getattr(DEFAULT_SETTINGS, engine_key))
                changes[order_key], changes[enabled_key] = migrate_stage_layout(
                    old_order, old_enabled, video=mode == "Video",
                    scale_method=old_scale, upscale_engine=engine,
                )
                if (old_scale == "DLSS" and "scale_method" in old_enabled and
                        ("super_resolution" not in old_enabled or engine != "DLSS" or
                         old_order.index("scale_method") < old_order.index("super_resolution"))):
                    mode_key = "upscale_image_dlss_mode" if mode == "Image" else "upscale_dlss_mode"
                    preset_key = "upscale_image_dlss_preset" if mode == "Image" else "upscale_dlss_preset"
                    changes[mode_key] = changes.get("nr_dlss_mode", DEFAULT_SETTINGS.nr_dlss_mode)
                    changes[preset_key] = changes.get("nr_dlss_preset", DEFAULT_SETTINGS.nr_dlss_preset)
        changes["nr_scale_method"] = "Standard"
        changes["upscale_image_engine"] = "RTX Video Super Resolution"
        changes["upscale_engine"] = "RTX Video Super Resolution"
        if not changes.get("upscale_vsr_enabled", DEFAULT_SETTINGS.upscale_vsr_enabled) and not changes.get(
            "upscale_hdr_enabled", DEFAULT_SETTINGS.upscale_hdr_enabled
        ):
            changes["upscale_vsr_enabled"] = True
    if version < 11:
        old_order = changes.get("image_stage_order", DEFAULT_SETTINGS.image_stage_order)
        old_enabled = changes.get("image_enabled_stages", ())
        changes["image_stage_order"], changes["image_enabled_stages"] = migrate_stage_layout(
            old_order, old_enabled, video=False,
            scale_method=changes.get("nr_scale_method", DEFAULT_SETTINGS.nr_scale_method),
            upscale_engine=changes.get("upscale_image_engine", DEFAULT_SETTINGS.upscale_image_engine),
        )
        for key in ("coloring_mode", "color_match_source", "color_match_reference"):
            changes[key] = getattr(DEFAULT_SETTINGS, key)
    if version < 12:
        old_order = changes.get("video_stage_order", DEFAULT_SETTINGS.video_stage_order)
        old_enabled = changes.get("video_enabled_stages", ())
        changes["video_stage_order"], changes["video_enabled_stages"] = migrate_stage_layout(
            old_order, old_enabled, video=True,
            scale_method=changes.get("nr_scale_method", DEFAULT_SETTINGS.nr_scale_method),
            upscale_engine=changes.get("upscale_engine", DEFAULT_SETTINGS.upscale_engine),
        )
        if changes.get("coloring_mode") == "Mode 2":
            changes["coloring_mode"] = "LUT"
        changes["lut_path"] = ""
    if version < 14:
        changes["image_bit_depth"] = DEFAULT_SETTINGS.image_bit_depth
    if version < 15:
        for key in ("lut_resolution", *LUT_ADJUSTMENT_RANGES):
            changes[key] = getattr(DEFAULT_SETTINGS, key)
    if version < 16:
        for mode, prefix in (("Image", "image"), ("Video", "video")):
            order_key = f"{prefix}_stage_order"
            enabled_key = f"{prefix}_enabled_stages"
            changes[order_key], changes[enabled_key] = migrate_stage_layout(
                changes.get(order_key, getattr(DEFAULT_SETTINGS, order_key)),
                changes.get(enabled_key, getattr(DEFAULT_SETTINGS, enabled_key)),
                video=mode == "Video",
                scale_method=changes.get("nr_scale_method", DEFAULT_SETTINGS.nr_scale_method),
                upscale_engine=changes.get(
                    "upscale_engine" if mode == "Video" else "upscale_image_engine",
                    DEFAULT_SETTINGS.upscale_engine if mode == "Video" else DEFAULT_SETTINGS.upscale_image_engine,
                ),
            )
        for key in ("denoise_strength", "denoise_deblock", "denoise_temporal"):
            changes[key] = getattr(DEFAULT_SETTINGS, key)
    if version < 17:
        for mode, prefix in (("Image", "image"), ("Video", "video")):
            order_key = f"{prefix}_stage_order"
            enabled_key = f"{prefix}_enabled_stages"
            changes[order_key], changes[enabled_key] = migrate_stage_layout(
                changes.get(order_key, getattr(DEFAULT_SETTINGS, order_key)),
                changes.get(enabled_key, getattr(DEFAULT_SETTINGS, enabled_key)),
                video=mode == "Video",
                scale_method=changes.get("nr_scale_method", DEFAULT_SETTINGS.nr_scale_method),
                upscale_engine=changes.get(
                    "upscale_engine" if mode == "Video" else "upscale_image_engine",
                    DEFAULT_SETTINGS.upscale_engine if mode == "Video" else DEFAULT_SETTINGS.upscale_image_engine,
                ),
            )
        changes["cas_sharpness"] = DEFAULT_SETTINGS.cas_sharpness
    if version < 18:
        changes["sharpening_method"] = DEFAULT_SETTINGS.sharpening_method
    # NR Preset was removed entirely (non-functional). Old preset files still
    # carry it; ignore so imports from previous builds keep working.
    # (Unknown keys are already filtered above; this covers any edge case where
    # the field still exists on older UISettings shapes.)
    changes.pop("nr_preset", None)
    # Presets created before the Upscale tab get its defaults independently of
    # whichever RTX settings happen to be selected when the preset is imported.
    for key in known_names:
        if key.startswith("upscale_") and key not in changes:
            changes[key] = getattr(DEFAULT_SETTINGS, key)
    if changes.get("upscale_vsr_quality") == 0:
        changes["upscale_vsr_quality"] = DEFAULT_SETTINGS.upscale_vsr_quality
    merged = replace(current, **changes)
    return name, _validate(merged)


def export_settings_preset_to(
    name: str,
    settings: UISettings,
    destination: str | os.PathLike[str],
) -> Path:
    """Atomically write a validated preset to a user-selected destination."""
    document = preset_document(name, settings)
    target = Path(destination).expanduser()
    if target.suffix.casefold() != ".json":
        target = target.with_suffix(".json")
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(document, indent=2, ensure_ascii=False) + "\n"
    fd, temp_name = tempfile.mkstemp(
        prefix=target.stem + ".", suffix=".tmp", dir=str(target.parent)
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, target)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise
    return target.resolve()
