"""Small, human-readable render notes; never embed diagnostic or machine data."""
from __future__ import annotations

import re

from .jobs import Cancelled

APPLICATION = "Visual Enhancer"
LEGACY_APPLICATION = "DLSS 5 Visual Enhancer"
# Accept both headers when reading so outputs written before the rename
# still merge cleanly; new notes are always written with APPLICATION.
_APP_HEADER = r"(?:Visual Enhancer|DLSS 5 Visual Enhancer):"
PREFIX = "DLSS 5 Neural Rendering Settings - "
IMAGE_NOTE_FORMATS = frozenset({"PNG", "JPEG", "WebP", "AVIF", "TIFF"})
VIDEO_NOTE_FORMATS = frozenset({"MP4", "MKV"})
_LABELS = (
    "NR Style", "NR Intensity", "NR Passes", "Local Tone Strength",
    "Local Structure Strength", "Skin Structure Strength", "Automatic Mask",
    "NR Color Strength", "Tone Preservation", "Face/Skin Protection",
    "Grain Preservation", "Custom NR Mask", "Mask Feather",
    "GPU Memory Path", "Scale",
)
_NUMBER = r"-?\d+(?:\.\d+)?(?:e[+-]?\d+)?"
_SCALE_VALUE = r"(?:Source(?: \(Original\))?|200%|175%|150%|125%|75%|50%|25%)"
_VALUES = (
    r"(?:Default|Natural|Cinematic)",
    _NUMBER, r"[1-4]", _NUMBER, _NUMBER,
    _NUMBER + r"(?: \((?:Default|inactive - Automatic Mask Off)\))?",
    r"(?:On|Off)", _NUMBER, _NUMBER, _NUMBER, _NUMBER, r"(?:On|Off)", _NUMBER,
    r"(?:On|Off)", _SCALE_VALUE,
)
_V4_LABELS = (
    "NR Style", "NR Intensity", "Local Tone Strength",
    "Local Structure Strength", "Skin Structure Strength", "Automatic Mask",
    "NR Color Strength", "Tone Preservation", "Face/Skin Protection",
    "Grain Preservation", "Custom NR Mask", "Mask Feather",
    "GPU Memory Path", "Output Scale",
)
_V4_VALUES = (
    r"(?:Default|Natural|Cinematic)",
    _NUMBER, _NUMBER, _NUMBER,
    _NUMBER + r"(?: \((?:Default|inactive - Automatic Mask Off)\))?",
    r"(?:On|Off)", _NUMBER, _NUMBER, _NUMBER, _NUMBER, r"(?:On|Off)", _NUMBER,
    r"(?:On|Off)", _NUMBER + "x",
)
_V3_LABELS = (
    "NR Style", "NR Intensity", "Local Tone Strength",
    "Local Structure Strength", "Skin Structure Strength", "Automatic Mask",
    "NR Color Strength", "Tone Preservation", "Custom NR Mask", "Mask Feather",
    "GPU Memory Path", "Output Scale",
)
_V3_VALUES = (
    r"(?:Default|Natural|Cinematic)",
    _NUMBER, _NUMBER, _NUMBER,
    _NUMBER + r"(?: \((?:Default|inactive - Automatic Mask Off)\))?",
    r"(?:On|Off)", _NUMBER, _NUMBER, r"(?:On|Off)", _NUMBER,
    r"(?:On|Off)", _NUMBER + "x",
)
_V2_LABELS = (
    "NR Style", "NR Intensity", "Local Tone Strength",
    "Local Structure Strength", "Skin Structure Strength", "Automatic Mask",
    "GPU Memory Path", "Output Scale",
)
_V2_VALUES = (
    r"(?:Default|Natural|Cinematic)", _NUMBER, _NUMBER, _NUMBER,
    _NUMBER + r"(?: \((?:Default|inactive - Automatic Mask Off)\))?",
    r"(?:On|Off)", r"(?:On|Off)", _NUMBER + "x",
)
# Legacy notes written before NR Preset removal still carry a leading
# "NR Preset - ..." entry. Accept them for read/replace so old outputs merge
# cleanly; new notes are always written without the preset entry.
_LEGACY_VALUES = (
    r"(?:Default|Preset #[123])", r"(?:Default|Natural|Cinematic)",
    _NUMBER, _NUMBER, _NUMBER, _NUMBER + r"(?: \(Default\))?",
    r"(?:On|Off)", r"(?:On|Off)", _NUMBER + "x",
)
_LEGACY_LABELS = (
    "NR Preset", "NR Style", "NR Intensity", "Local Tone Strength",
    "Local Structure Strength", "Skin Structure Strength", "Automatic Mask",
    "GPU Memory Path", "Output Scale",
)
_OWN_NOTE = re.compile(
    r"(?m)^" + _APP_HEADER + r"\r?\n" + re.escape(PREFIX)
    + ", ".join(re.escape(label + " - ") + value for label, value in zip(_LABELS, _VALUES))
    + r"(?=\r?$)"
)
_V4_NOTE = re.compile(
    r"(?m)^" + _APP_HEADER + r"\r?\n" + re.escape(PREFIX)
    + ", ".join(re.escape(label + " - ") + value for label, value in zip(_V4_LABELS, _V4_VALUES))
    + r"(?=\r?$)"
)
_V3_NOTE = re.compile(
    r"(?m)^" + _APP_HEADER + r"\r?\n" + re.escape(PREFIX)
    + ", ".join(re.escape(label + " - ") + value for label, value in zip(_V3_LABELS, _V3_VALUES))
    + r"(?=\r?$)"
)
_V2_NOTE = re.compile(
    r"(?m)^" + _APP_HEADER + r"\r?\n" + re.escape(PREFIX)
    + ", ".join(re.escape(label + " - ") + value for label, value in zip(_V2_LABELS, _V2_VALUES))
    + r"(?=\r?$)"
)
_LEGACY_NOTE = re.compile(
    r"(?m)^" + _APP_HEADER + r"\r?\n" + re.escape(PREFIX)
    + ", ".join(re.escape(label + " - ") + value for label, value in zip(_LEGACY_LABELS, _LEGACY_VALUES))
    + r"(?=\r?$)"
)


class MetadataNoteError(ValueError):
    """The optional note could not be safely embedded or read back."""


def check_cancelled(controller) -> None:
    if controller is not None and controller.cancel.is_set():
        raise Cancelled("Render stopped by user.")


def record_embedding(diagnostics: dict | None, status: str, *, field=None, reason=None) -> None:
    if diagnostics is not None:
        diagnostics.update(status=status)
        if field is not None:
            diagnostics["field"] = field
        if reason is not None:
            diagnostics["reason"] = str(reason)


def embedding_warning(diagnostics: dict | None, exc: Exception) -> str:
    message = f"DLSS settings metadata was skipped; saved without the new note ({str(exc)[:500]})."
    record_embedding(diagnostics, "skipped", reason=str(exc)[:500])
    if diagnostics is not None:
        diagnostics["warning"] = message
    return message


def skin_structure_status(value: float, automatic_mask: bool) -> str:
    """Describe whether the private Skin control can affect this render."""
    if float(value) == -1.0:
        return "model default"
    if not automatic_mask:
        return "inactive — Automatic Mask Off"
    return "active"


def build_render_note(options) -> str:
    from .runtime import UPSCALING_MODES, resolve_native_settings, resolve_upscaling_mode

    native = resolve_native_settings(options)
    factor, _ = resolve_upscaling_mode(options.upscaling_factor)
    scale = UPSCALING_MODES[factor]["label"]
    number = lambda value: str(float(value)).removesuffix(".0")
    skin = number(native["skin_structure"])
    if native["skin_structure"] == -1:
        skin += " (Default)"
    elif not native["auto_mask"]:
        # Keep the embedded EXIF description ASCII; Pillow cannot round-trip
        # the human-readable em dash used by JSON reports in this field.
        skin += " (inactive - Automatic Mask Off)"
    values = (
        options.nr_style, number(native["intensity"]),
        str(int(native["nr_passes"])),
        number(native["local_tone"]), number(native["local_structure"]), skin,
        "On" if native["auto_mask"] else "Off",
        number(native["color_strength"]), number(native["tone_preservation"]),
        number(native["face_skin_protection"]), number(native["grain_preservation"]),
        "On" if getattr(options, "nr_mask", None) else "Off",
        number(native["mask_feather"]),
        "On", scale,
    )
    return APPLICATION + ":\n" + PREFIX + ", ".join(
        label + " - " + value for label, value in zip(_LABELS, values)
    )


def prepare_render_note(options, diagnostics: dict) -> str | None:
    try:
        return build_render_note(options)
    except (ValueError, TypeError, KeyError, AttributeError, StopIteration) as exc:
        embedding_warning(diagnostics, exc)
        return None


def merge_render_note(existing: str | None, note: str) -> str:
    if not isinstance(note, str) or not _OWN_NOTE.fullmatch(note):
        raise MetadataNoteError("Unrecognized render note")
    if existing is None or existing == "":
        return note
    if not isinstance(existing, str) or "\x00" in existing:
        raise MetadataNoteError("The existing description cannot be safely extended")
    matches = list(_OWN_NOTE.finditer(existing))
    v4 = list(_V4_NOTE.finditer(existing))
    v3 = list(_V3_NOTE.finditer(existing))
    v2 = list(_V2_NOTE.finditer(existing))
    legacy = list(_LEGACY_NOTE.finditer(existing))
    if matches:
        # Replace only a complete, recognized note. User descriptions, including
        # similarly named prose, remain untouched.
        return _OWN_NOTE.sub(lambda _match: note, existing)
    if v4:
        return _V4_NOTE.sub(lambda _match: note, existing)
    if v3:
        return _V3_NOTE.sub(lambda _match: note, existing)
    if v2:
        return _V2_NOTE.sub(lambda _match: note, existing)
    if legacy:
        # Old outputs still carry the removed NR Preset entry: replace the
        # whole legacy note with the new preset-free note.
        return _LEGACY_NOTE.sub(lambda _match: note, existing)
    return existing + "\n\n" + note
