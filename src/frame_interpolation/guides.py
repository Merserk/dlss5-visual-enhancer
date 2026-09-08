from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

from ..core.motion_resize import MotionFieldResizer, motion_to_fp16


@dataclass(slots=True)
class Guide:
    motion: np.ndarray
    reset: bool
    scene_score: float
    duplicate: bool
    confidence: float


class DLSSGGuideGenerator:
    """CUDA-free guide estimation; it never creates an image frame."""

    def __init__(self, width: int, height: int, flow_width: int = 640) -> None:
        self.resizer = MotionFieldResizer(width, height)
        self.width = width
        self.height = height
        scale = min(1.0, flow_width / max(1, width))
        self.flow_width = max(64, int(round(width * scale / 2) * 2))
        self.flow_height = max(64, int(round(height * scale / 2) * 2))
        self.previous: np.ndarray | None = None
        self.zero = np.zeros((height, width, 2), dtype=np.float16)
        self.flow = cv2.DISOpticalFlow_create(cv2.DISOPTICAL_FLOW_PRESET_MEDIUM)
        self.flow.setUseSpatialPropagation(True)
        self.flow.setFinestScale(1)

    def _gray(self, rgba: np.ndarray) -> np.ndarray:
        gray = cv2.cvtColor(rgba, cv2.COLOR_RGBA2GRAY)
        return cv2.resize(gray, (self.flow_width, self.flow_height), interpolation=cv2.INTER_AREA)

    def process(self, rgba: np.ndarray, *, force_reset: bool = False) -> Guide:
        current = self._gray(rgba)
        if self.previous is None:
            guide = Guide(self.zero, True, 1.0, False, 0.0)
        else:
            difference = cv2.absdiff(current, self.previous)
            score = cv2.mean(difference)[0] / 255.0
            duplicate = score < 0.0005
            reset = force_reset or score > 0.24
            if reset or duplicate:
                vectors = self.zero
                confidence = 1.0 if duplicate else 0.0
            else:
                calculated = self.flow.calc(current, self.previous, None)
                calculated = self.resizer.resize(calculated)
                # Reducing an H×W×2 boolean array along its tiny last axis is
                # expensive. These two channel checks produce the same mask.
                finite = np.isfinite(calculated[..., 0])
                np.logical_and(finite, np.isfinite(calculated[..., 1]), out=finite)
                confidence = float(np.mean(finite))
                if confidence < 1.0:
                    calculated[~finite] = 0
                reset = confidence < 0.98
                vectors = self.zero if reset else motion_to_fp16(calculated)
            guide = Guide(vectors, reset, score, duplicate, confidence)
        self.previous = current
        return guide
