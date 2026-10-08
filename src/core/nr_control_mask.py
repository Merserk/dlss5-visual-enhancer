"""Session-scoped custom-mask preparation and native DLSSNR.ControlMask metadata."""
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

from .ui_messages import UiMessage
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
        raise ValueError("The selected NR Control Mask is no longer available.")
    try:
        with Image.open(source) as opened:
            opened.seek(0)
            oriented = ImageOps.exif_transpose(opened)
            width, height = oriented.size
            oriented.load()
    except Exception as exc:
        raise ValueError(f"NR Control Mask is not a readable image: {exc}") from exc
    if width <= 0 or height <= 0:
        raise ValueError("NR Control Mask has invalid dimensions.")
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
            raise ValueError("NR Control Mask session state is incomplete.")
        return NRMaskSelection(
            path=str(value["path"]),
            source_name=str(value["source_name"]),
            sha256=str(value["sha256"]),
            width=int(value["width"]),
            height=int(value["height"]),
            revision=int(value["revision"]),
        )
    raise ValueError("NR Control Mask session state is invalid.")

def prepare_nr_mask(selection: object | None, width: int, height: int) -> np.ndarray | None:
    """Prepare native RGBA control values without compositing the rendered image.

    R is intensity, G local tone, B local structure. The runtime receives A
    unchanged as its fourth native mask channel. Grayscale masks broadcast
    their value to every channel; alpha never becomes a wrapper blend weight.
    """
    selected = mask_selection(selection)
    if selected is None:
        return None
    try:
        with Image.open(selected.path) as opened:
            opened.seek(0)
            oriented = ImageOps.exif_transpose(opened)
            if oriented.mode in {"1", "L", "I", "I;16", "I;16B", "I;16L", "F"}:
                gray = np.asarray(oriented, dtype=np.float32)
                scale = (65535.0 if oriented.mode in {"I", "I;16", "I;16B", "I;16L"}
                         else 1.0 if oriented.mode in {"1", "F"} else 255.0)
                mask = np.repeat((gray / scale)[..., None], 4, axis=2)
            else:
                mask = np.asarray(oriented.convert("RGBA"), dtype=np.float32) / 255.0
    except Exception as exc:
        raise ValueError(f"NR Control Mask {selected.source_name!r} could not be decoded.") from exc
    if mask.shape[:2] != (int(height), int(width)):
        mask = cv2.resize(mask, (int(width), int(height)), interpolation=cv2.INTER_NEAREST)
    if not np.isfinite(mask).all():
        raise ValueError("NR Control Mask contains non-finite values.")
    return np.ascontiguousarray(np.clip(mask, 0.0, 1.0), dtype=np.float32)

def mask_report(selection: object | None) -> dict[str, Any]:
    selected = mask_selection(selection)
    if selected is None:
        return {"active": False, "native_parameter": "DLSSNR.ControlMask"}
    return {"active": True, "native_parameter": "DLSSNR.ControlMask",
            "source_name": selected.source_name, "sha256": selected.sha256,
            "width": selected.width, "height": selected.height}

def report_options(options: object) -> dict[str, Any]:
    """Return dataclass options without leaking an app temporary path."""
    values = asdict(options)
    selection = values.pop("nr_mask", None)
    values["native_control_mask"] = mask_report(selection)
    return values

def mask_status(selection: object | None) -> str:
    selected = mask_selection(selection)
    if selected is None:
        return ""
    return UiMessage("Active: %1 — %2×%3.", selected.source_name, selected.width, selected.height)
