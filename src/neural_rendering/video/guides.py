from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np


SCENE_CUT_THRESHOLD = 0.24


@dataclass(slots=True)
class GuideFrame:
    reset: bool
    scene_score: float


class TemporalGuideGenerator:
    """Reduced-luma scene reset detector that retains history between cuts."""

    def __init__(self, width: int, height: int, flow_width: int = 640,
                 cut_threshold: float = SCENE_CUT_THRESHOLD) -> None:
        self.cut_threshold = cut_threshold
        scale = min(1.0, flow_width / max(1, width))
        self.luma_width = max(32, int(round(width * scale / 2) * 2))
        self.luma_height = max(32, int(round(height * scale / 2) * 2))
        self._small_rgba = np.empty((self.luma_height, self.luma_width, 4), dtype=np.uint8)
        self._gray_a = np.empty((self.luma_height, self.luma_width), dtype=np.uint8)
        self._gray_b = np.empty_like(self._gray_a)
        self._difference = np.empty_like(self._gray_a)
        self._current_gray = self._gray_a
        self.previous_gray: np.ndarray | None = None
        self.duplicate_frames = 0

    def _small_gray(self, rgba: np.ndarray) -> np.ndarray:
        if rgba.dtype == np.uint16:
            # Scene scoring may use 8-bit luma; the render frame stays 16-bit.
            small = cv2.resize(
                rgba, (self.luma_width, self.luma_height), interpolation=cv2.INTER_AREA
            )
            gray16 = cv2.cvtColor(small, cv2.COLOR_RGBA2GRAY)
            np.copyto(self._current_gray, (gray16 >> 8).astype(np.uint8))
            return self._current_gray
        cv2.resize(
            rgba,
            (self.luma_width, self.luma_height),
            dst=self._small_rgba,
            interpolation=cv2.INTER_AREA,
        )
        cv2.cvtColor(self._small_rgba, cv2.COLOR_RGBA2GRAY, dst=self._current_gray)
        return self._current_gray

    def process(
        self, rgba: np.ndarray, *, motion_buffer: np.ndarray | None = None
    ) -> GuideFrame:
        del motion_buffer
        current = self._small_gray(rgba)
        if self.previous_gray is None:
            reset = True
            scene_score = 1.0
        else:
            cv2.absdiff(current, self.previous_gray, dst=self._difference)
            if cv2.countNonZero(self._difference) == 0:
                self.duplicate_frames += 1
                scene_score = 0.0
            else:
                scene_score = float(np.mean(self._difference)) / 255.0
            reset = scene_score > self.cut_threshold

        self.previous_gray = current
        self._current_gray = self._gray_b if current is self._gray_a else self._gray_a
        return GuideFrame(reset=reset, scene_score=scene_score)
