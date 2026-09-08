"""Allocation-conscious CPU motion resizing (not a GPU/VRAM allocator)."""
from __future__ import annotations

import cv2
import numpy as np


class MotionFieldResizer:
    """Own one reusable float32 scratch buffer; callers own their FP16 outputs.

    Scale vector magnitudes on the small guide grid before linear interpolation.
    This removes two strided, full-resolution multiplication passes. Linearity
    preserves the vector field apart from floating-point rounding. The returned
    float32 scratch is borrowed until the next call, so convert/copy before
    publishing it to another thread or a frame queue.
    """

    def __init__(self, width: int, height: int) -> None:
        if width <= 0 or height <= 0:
            raise ValueError("Motion dimensions must be positive.")
        self.width, self.height = int(width), int(height)
        self._scratch: np.ndarray | None = None

    def resize(self, flow: np.ndarray) -> np.ndarray:
        if flow.ndim != 3 or flow.shape[2] != 2 or min(flow.shape[:2]) <= 0:
            raise ValueError("Motion input must have shape (height, width, 2).")
        # Do not modify a DIS-owned buffer or a field retained by the caller.
        small = np.array(flow, dtype=np.float32, order="C", copy=True)
        small[..., 0] *= self.width / small.shape[1]
        small[..., 1] *= self.height / small.shape[0]
        if self._scratch is None:
            self._scratch = np.empty((self.height, self.width, 2), dtype=np.float32)
        cv2.resize(small, (self.width, self.height), dst=self._scratch,
                   interpolation=cv2.INTER_LINEAR)
        return self._scratch


def motion_to_fp16(flow: np.ndarray) -> np.ndarray:
    """Return an independent, contiguous FP16 buffer using OpenCV CPU dispatch.

    convertFp16 stores IEEE half *bits* in a CV_16S array. A NumPy view changes
    interpretation, not values, and keeps that owned allocation alive. Never
    pass its int16 storage directly as numeric int16 motion. Older/custom
    OpenCV builds without this entry point fall back to NumPy conversion.
    """
    source = np.ascontiguousarray(flow, dtype=np.float32)
    converter = getattr(cv2, "convertFp16", None)
    if converter is not None:
        try:
            return converter(source).view(np.float16)
        except cv2.error:
            pass
    return source.astype(np.float16)
