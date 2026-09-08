from __future__ import annotations

import os
import unittest
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src.core.ffmpeg import preview
from src.neural_rendering.video import cuda_decode


class FastPreviewTests(unittest.TestCase):
    def test_default_compat_preview_remains_cpu_h264(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                preview.resolve_preview_codec("H.265 (NVIDIA NVENC)", "MP4", "Auto"),
                ("H.264", "MP4"),
            )
            self.assertTrue(
                preview.wants_compat_preview("H.265 (NVIDIA NVENC)", "MP4", "Auto")
            )

    def test_4090_fast_preview_uses_nvenc_and_gpu_resolution_path(self):
        with patch.dict(os.environ, {preview.FAST_PREVIEW_NVENC_ENV: "1"}, clear=True):
            self.assertEqual(
                preview.resolve_preview_codec("H.265 (NVIDIA NVENC)", "MP4", "Auto"),
                ("H.264 (NVIDIA NVENC)", "MP4"),
            )
            self.assertFalse(
                preview.wants_compat_preview("H.265 (NVIDIA NVENC)", "MP4", "Auto")
            )

    def test_existing_browser_playable_choice_is_not_rewritten(self):
        with patch.dict(os.environ, {preview.FAST_PREVIEW_NVENC_ENV: "1"}, clear=True):
            self.assertEqual(
                preview.resolve_preview_codec("H.264", "MP4", "Auto"),
                ("H.264", "MP4"),
            )


class CudaDecodeTests(unittest.TestCase):
    def test_decode_mode_values(self):
        with patch.dict(os.environ, {cuda_decode.CUDA_DECODE_ENV: "on"}, clear=True):
            self.assertEqual(cuda_decode.cuda_decode_mode(), "on")
        with patch.dict(os.environ, {cuda_decode.CUDA_DECODE_ENV: "garbage"}, clear=True):
            self.assertEqual(cuda_decode.cuda_decode_mode(), "auto")

    def test_cuda_command_pins_verified_ordinal_and_preserves_pts(self):
        instance = object.__new__(cuda_decode._CudaDecodeContainer)
        instance._source = Path("C:/input.mp4")
        command = instance._command(3, cuda_decode._FILTER_CANDIDATES[0])
        self.assertEqual(command[command.index("-hwaccel_device") + 1], "3")
        self.assertIn("-copyts", command)
        self.assertEqual(command[command.index("-pix_fmt") + 1], "rgba")
        self.assertEqual(command[-2:], ["nut", "pipe:1"])

    def test_pts_rescale_uses_original_timebase(self):
        instance = object.__new__(cuda_decode._CudaDecodeContainer)
        instance._source_time_base = Fraction(1, 90000)
        instance._inner_stream = SimpleNamespace(time_base=Fraction(1, 1000))
        frame = SimpleNamespace(pts=1000, time_base=Fraction(1, 1000))
        instance._rescale_pts(frame)
        self.assertEqual(frame.pts, 90000)


if __name__ == "__main__":
    unittest.main()
