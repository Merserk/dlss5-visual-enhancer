"""Session-scoped custom-mask preparation and report-safe composition metadata."""
from __future__ import annotations

import hashlib
import itertools
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from PIL import Image, ImageOps

from .i18n import t

_REVISION_COUNTER = itertools.count(1)
_REVISION_LOCK = threading.Lock()


def _next_revision() -> int:
    with _REVISION_LOCK:
        return next(_REVISION_COUNTER)


@dataclass(frozen=True, slots=True)
class NRMaskSelection:
    """Validated upload identity. The path is temporary and must not be persisted."""

    path: str
    source_name: str
    sha256: str
    width: int
    height: int
    revision: int

    def state(self) -> dict[str, Any]:
        return asdict(self)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def inspect_nr_mask(path: str | Path | None) -> NRMaskSelection | None:
    """Validate an uploaded image without retaining decoded pixels."""
    if not path:
        return None
    source = Path(path)
    if not source.is_file():
        raise ValueError(t("composition.error.mask_missing"))
    try:
        with Image.open(source) as opened:
            opened.seek(0)
            oriented = ImageOps.exif_transpose(opened)
            width, height = oriented.size
            oriented.load()
    except Exception as exc:
        raise ValueError(t("composition.error.mask_unreadable", error=exc)) from exc
    if width <= 0 or height <= 0:
        raise ValueError(t("composition.error.mask_invalid_dimensions"))
    return NRMaskSelection(
        path=str(source.resolve()),
        source_name=source.name,
        sha256=_sha256(source),
        width=int(width),
        height=int(height),
        revision=_next_revision(),
    )


def mask_selection(value: object | None) -> NRMaskSelection | None:
    if value is None or value == "":
        return None
    if isinstance(value, NRMaskSelection):
        return value
    if isinstance(value, (str, Path)):
        return inspect_nr_mask(value)
    if isinstance(value, dict):
        required = {"path", "source_name", "sha256", "width", "height", "revision"}
        if not required.issubset(value):
            raise ValueError(t("composition.error.mask_state_incomplete"))
        return NRMaskSelection(
            path=str(value["path"]),
            source_name=str(value["source_name"]),
            sha256=str(value["sha256"]),
            width=int(value["width"]),
            height=int(value["height"]),
            revision=int(value["revision"]),
        )
    raise ValueError(t("composition.error.mask_state_invalid"))


def prepare_nr_mask(
    selection: object | None, width: int, height: int, feather: int,
) -> np.ndarray | None:
    """Decode, stretch, and feather one mask for a native render session."""
    selected = mask_selection(selection)
    if selected is None:
        return None
    if isinstance(feather, bool) or int(feather) != feather or not 0 <= int(feather) <= 128:
        raise ValueError(t("composition.error.mask_feather_range"))
    try:
        with Image.open(selected.path) as opened:
            opened.seek(0)
            rgba = np.asarray(
                ImageOps.exif_transpose(opened).convert("RGBA"), dtype=np.float32
            )
    except Exception as exc:
        raise ValueError(t("composition.error.mask_decode_failed", source_name=selected.source_name)) from exc
    rgb = rgba[..., :3] * (1.0 / 255.0)
    alpha = rgba[..., 3] * (1.0 / 255.0)
    mask = (
        rgb[..., 0] * 0.2126 + rgb[..., 1] * 0.7152 + rgb[..., 2] * 0.0722
    ) * alpha
    if mask.shape != (int(height), int(width)):
        interpolation = (
            cv2.INTER_AREA
            if mask.shape[1] > int(width) or mask.shape[0] > int(height)
            else cv2.INTER_LINEAR
        )
        mask = cv2.resize(mask, (int(width), int(height)), interpolation=interpolation)
    if feather:
        mask = cv2.GaussianBlur(
            mask, (0, 0), sigmaX=float(feather), sigmaY=float(feather),
            borderType=cv2.BORDER_REPLICATE,
        )
    return np.ascontiguousarray(np.clip(mask, 0.0, 1.0), dtype=np.float32)


def mask_report(selection: object | None, feather: int) -> dict[str, Any]:
    selected = mask_selection(selection)
    if selected is None:
        return {"active": False, "feather_px": int(feather), "feather_status": t("composition.status.inactive")}
    return {
        "active": True,
        "source_name": selected.source_name,
        "sha256": selected.sha256,
        "width": selected.width,
        "height": selected.height,
        "feather_px": int(feather),
    }


def report_options(options: object) -> dict[str, Any]:
    """Return dataclass options without leaking a Gradio temporary path."""
    values = asdict(options)
    selection = values.pop("nr_mask", None)
    values["custom_nr_mask"] = mask_report(selection, int(values.get("mask_feather", 0)))
    return values


def mask_status(selection: object | None) -> str:
    selected = mask_selection(selection)
    if selected is None:
        return ""
    return t(
        "composition.status.active",
        source_name=selected.source_name,
        width=selected.width,
        height=selected.height,
    )
