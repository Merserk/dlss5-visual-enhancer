from __future__ import annotations

import json
import math
import os
import re
import tempfile
from dataclasses import asdict, fields, replace
from pathlib import Path
from typing import Any

from ..core.paths import GRADIO_TEMP
from .models import (
    DEFAULT_SETTINGS, MAX_PRESET_BYTES, PRESET_FORMAT, PRESET_SCHEMA_VERSION, UISettings, _validate,
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
    GRADIO_TEMP.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix="dlss5-settings-preset-", dir=GRADIO_TEMP))
    path = directory / filename
    path.write_text(
        json.dumps(document, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return path.resolve()


def _coerce_preset_value(field_name: str, value: Any, current: UISettings) -> Any:
    expected = getattr(current, field_name)
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
        raise ValueError("This JSON file is not a DLSS 5 Visual Enhancer settings preset.")
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
        # it and default the newly introduced shared GPU staging switch to ON.
        changes.pop("dlss_model_preset", None)
        changes["nr_gpu_mode"] = DEFAULT_SETTINGS.nr_gpu_mode
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
    if version < 7:
        changes["frame_interpolation_gpu_mode"] = DEFAULT_SETTINGS.frame_interpolation_gpu_mode
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
