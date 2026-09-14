from __future__ import annotations

from pathlib import Path

from .i18n import t


RENAME_MODES = ("Auto", "Copy", "Custom")
_INVALID_FILENAME_CHARACTERS = set('<>:"/\\|?*')


def validate_rename(mode: str, custom_suffix: str) -> str:
    if mode not in RENAME_MODES:
        choices = ", ".join(RENAME_MODES)
        raise ValueError(t("naming.error.rename_choices", choices=choices))
    suffix = str(custom_suffix or "")
    if mode != "Custom":
        return suffix
    if not suffix:
        raise ValueError(t("naming.error.custom_suffix_required"))
    if any(character in _INVALID_FILENAME_CHARACTERS or ord(character) < 32 for character in suffix):
        raise ValueError(t("naming.error.custom_suffix_invalid_chars"))
    if suffix.endswith((" ", ".")):
        raise ValueError(t("naming.error.custom_suffix_trailing"))
    return suffix


def output_filename(
    source: Path,
    extension: str,
    mode: str,
    custom_suffix: str,
    auto_stem: str,
) -> str:
    suffix = validate_rename(mode, custom_suffix)
    if not extension.startswith("."):
        raise ValueError(t("naming.error.output_extension"))
    if mode == "Auto":
        stem = auto_stem
    else:
        stem = source.stem.strip().rstrip(".") or "output"
        if mode == "Custom":
            stem += suffix
    return f"{stem}{extension}"


def require_available_output(path: Path) -> None:
    if path.exists():
        raise FileExistsError(t("naming.error.output_exists", name=path.name))
