"""CPU unit tests; native Windows workers and hardware encoding are NOT exercised.

Run: python -m unittest discover -s tests -v
Requires numpy and opencv-python (or opencv-python-headless).
Modules load in a private namespace to avoid importing the optional UI stack.
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
PREFIX = "_dlss5_performance_tests"


def load(name, path):
    full = PREFIX + "." + name
    spec = importlib.util.spec_from_file_location(full, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[full] = module
    spec.loader.exec_module(module)
    return module


for suffix in ("", ".core", ".core.ffmpeg", ".neural_rendering", ".neural_rendering.video",
               ".frame_interpolation"):
    package = types.ModuleType(PREFIX + suffix)
    package.__path__ = []
    sys.modules[package.__name__] = package

detection = load("core.gpu_detection", "src/core/gpu_detection.py")
selection = load("core.gpu_selection", "src/core/gpu_selection.py")
resizing = load("core.motion_resize", "src/core/motion_resize.py")
probes = load("core.ffmpeg.performance", "src/core/ffmpeg/performance.py")
codecs = load("core.ffmpeg.codecs", "src/core/ffmpeg/codecs.py")
# Only unused process-controller dependencies are substituted. Codec logic is real.
jobs = types.ModuleType(PREFIX + ".core.jobs")
for key in ("BoundedLogBuffer", "JobController", "drain_bounded_text", "drain_text"):
    setattr(jobs, key, Mock())
sys.modules[jobs.__name__] = jobs
paths = types.ModuleType(PREFIX + ".core.paths")
paths.FFMPEG = ROOT / "bin" / "ffmpeg" / "bin" / "ffmpeg.exe"
sys.modules[paths.__name__] = paths
encoder = load("core.ffmpeg.encoder", "src/core/ffmpeg/encoder.py")
nr = load("neural_rendering.video.guides", "src/neural_rendering/video/guides.py")
fg = load("frame_interpolation.guides", "src/frame_interpolation/guides.py")
profile = load("profile", "tools/rtx4090_profile.py")

GPU0 = dict(name="NVIDIA GeForce RTX 4090", uuid="GPU-4090", index=0, memory_mb=24564,
            pci_bus_id="0000:01:00.0", cuda_ordinal=1, cuda_identity_verified=True,
            ai_compatible=True)
GPU1 = dict(name="NVIDIA GeForce RTX 2080 Ti", uuid="GPU-2080", index=1, memory_mb=11264,
            pci_bus_id="0000:02:00.0", cuda_ordinal=0, cuda_identity_verified=True,
            ai_compatible=True)


class GPUSelectionTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {}, clear=True)
        self.env.start()
        selection.clear_gpu_selection_cache()
        self.addCleanup(self.env.stop)

    def test_4090_wins_reversed_order(self):
        self.assertEqual(selection.resolve_ai_gpu((GPU1, GPU0))["uuid"], GPU0["uuid"])

    def test_explicit_selection_wins(self):
        os.environ["DLSS5_PREFERRED_GPU_UUID"] = GPU0["uuid"]
        self.assertEqual(selection.resolve_ai_gpu((GPU0, GPU1), GPU1["uuid"])["uuid"], GPU1["uuid"])

    def test_environment_pins_automatic(self):
        os.environ["DLSS5_PREFERRED_GPU_UUID"] = GPU1["uuid"]
        self.assertEqual(selection.resolve_ai_gpu((GPU0, GPU1))["uuid"], GPU1["uuid"])

    def test_missing_pinned_device_fails(self):
        os.environ["DLSS5_PREFERRED_GPU_UUID"] = "GPU-missing"
        with self.assertRaises(RuntimeError):
            selection.resolve_ai_gpu((GPU0, GPU1))

    def test_no_rtx_fails(self):
        with self.assertRaises(RuntimeError):
            selection.resolve_ai_gpu((dict(GPU0, ai_compatible=False),))

    def test_detect_returns_independent_dictionary(self):
        with patch.object(selection, "detect_gpus", return_value=(GPU0,)):
            selection.detect_gpu()["name"] = "changed"
            self.assertEqual(selection.detect_gpu()["name"], GPU0["name"])

    def test_pci_normalization(self):
        self.assertEqual(detection._normalize_pci_bus_id("00000000:af:01.0"), "0000:AF:01.0")

    def test_cuda_is_pci_mapped_not_smi_index(self):
        row = "0, GPU-4090, 00000000:01:00.0, NVIDIA GeForce RTX 4090, 595.79, 24564\n"
        with patch.object(detection.subprocess, "run", return_value=Mock(returncode=0, stdout=row)), \
             patch.object(detection, "_cuda_device_identities", return_value={"0000:01:00.0": {"cuda_ordinal": 1}}):
            self.assertEqual(detection.detect_gpus()[0]["cuda_ordinal"], 1)

    def test_cuda_unknown_never_guesses_ordinal(self):
        row = "0, GPU-4090, 00000000:01:00.0, NVIDIA GeForce RTX 4090, 595.79, 24564\n"
        with patch.object(detection.subprocess, "run", return_value=Mock(returncode=0, stdout=row)), \
             patch.object(detection, "_cuda_device_identities", return_value={}):
            gpu = detection.detect_gpus()[0]
            self.assertIsNone(gpu["cuda_ordinal"])
            self.assertFalse(gpu["cuda_identity_verified"])


class MotionTests(unittest.TestCase):
    def test_half_conversion_matches_numpy_bits(self):
        flow = np.random.default_rng(5).normal(0, 50, (73, 131, 2)).astype(np.float32)
        actual = resizing.motion_to_fp16(flow)
        expected = flow.astype(np.float16)
        np.testing.assert_array_equal(actual.view(np.uint16), expected.view(np.uint16))
        self.assertTrue(actual.flags.c_contiguous)
        self.assertFalse(np.shares_memory(flow, actual))

    def test_half_special_values_and_subnormals(self):
        flow = np.array([0., -0., np.inf, -np.inf, np.nan, 65504., 2**-24, -2**-24], np.float32).reshape(2, 2, 2)
        expected = flow.astype(np.float16)
        actual = resizing.motion_to_fp16(flow)
        np.testing.assert_array_equal(actual, expected)
        np.testing.assert_array_equal(np.signbit(actual), np.signbit(expected))

    def test_half_fallback_without_opencv_entrypoint(self):
        flow = np.ones((64, 64, 2), np.float32)
        with patch.object(resizing.cv2, "convertFp16", None):
            np.testing.assert_array_equal(resizing.motion_to_fp16(flow), flow.astype(np.float16))

    def test_half_outputs_independent_between_frames(self):
        source = np.ones((64, 64, 2), np.float32)
        first = resizing.motion_to_fp16(source)
        source.fill(2)
        second = resizing.motion_to_fp16(source)
        self.assertTrue(np.all(first == 1))
        self.assertFalse(np.shares_memory(first, second))

    def test_linear_equivalence_and_input_ownership(self):
        flow = np.random.default_rng(7).normal(size=(72, 128, 2)).astype(np.float32)
        original = flow.copy()
        expected = cv2.resize(flow, (511, 289), interpolation=cv2.INTER_LINEAR)
        expected[..., 0] *= 511 / 128
        expected[..., 1] *= 289 / 72
        actual = resizing.MotionFieldResizer(511, 289).resize(flow)
        np.testing.assert_allclose(actual, expected, rtol=2e-5, atol=3e-6)
        np.testing.assert_array_equal(flow, original)

    def test_scratch_reused_but_half_output_owned(self):
        resizer = resizing.MotionFieldResizer(128, 128)
        first = resizer.resize(np.ones((64, 64, 2), np.float32))
        owned = first.astype(np.float16)
        second = resizer.resize(np.zeros((64, 64, 2), np.float32))
        self.assertIs(first, second)
        self.assertTrue(np.all(owned == 2))
        self.assertFalse(np.shares_memory(owned, second))

    def test_invalid_motion_shape_rejected(self):
        with self.assertRaises(ValueError):
            resizing.MotionFieldResizer(128, 128).resize(np.zeros((8, 8), np.float32))

    def test_invalid_dimensions_rejected(self):
        with self.assertRaises(ValueError):
            resizing.MotionFieldResizer(0, 128)

    def test_nr_duplicate_skips_flow_without_history_reset(self):
        guide = nr.TemporalGuideGenerator(128, 128)
        image = np.full((128, 128, 4), 32, np.uint8)
        self.assertTrue(guide.process(image).reset)
        guide.dis = Mock()
        result = guide.process(image)
        guide.dis.calc.assert_not_called()
        self.assertFalse(result.reset)
        self.assertEqual(result.scene_score, 0)
        self.assertTrue(np.all(result.motion == 0))

    def test_nr_scene_cut_resets(self):
        guide = nr.TemporalGuideGenerator(128, 128)
        guide.process(np.zeros((128, 128, 4), np.uint8))
        self.assertTrue(guide.process(np.full((128, 128, 4), 255, np.uint8)).reset)

    def test_nr_nonfinite_flow_resets(self):
        guide = nr.TemporalGuideGenerator(128, 128)
        guide.process(np.zeros((128, 128, 4), np.uint8))
        guide.dis = Mock()
        guide.dis.calc.return_value = np.full((128, 128, 2), np.nan, np.float32)
        result = guide.process(np.full((128, 128, 4), 10, np.uint8))
        self.assertTrue(result.reset)
        self.assertTrue(np.isfinite(result.motion).all())

    def test_nr_queued_arrays_do_not_alias(self):
        guide = nr.TemporalGuideGenerator(128, 128)
        guide.process(np.zeros((128, 128, 4), np.uint8))
        guide.dis = Mock()
        guide.dis.calc.return_value = np.ones((128, 128, 2), np.float32)
        first = guide.process(np.full((128, 128, 4), 10, np.uint8)).motion
        guide.dis.calc.return_value = np.full((128, 128, 2), 2, np.float32)
        second = guide.process(np.full((128, 128, 4), 20, np.uint8)).motion
        self.assertFalse(np.shares_memory(first, second))
        self.assertTrue(np.all(first == 1))

    def test_fg_force_reset_and_duplicates_preserved(self):
        guide = fg.DLSSGGuideGenerator(128, 128)
        image = np.full((128, 128, 4), 40, np.uint8)
        guide.process(image)
        result = guide.process(image, force_reset=True)
        self.assertTrue(result.reset)
        self.assertTrue(result.duplicate)
        self.assertEqual(result.confidence, 1)

    def test_fg_nonfinite_confidence_resets(self):
        guide = fg.DLSSGGuideGenerator(128, 128)
        guide.process(np.zeros((128, 128, 4), np.uint8))
        guide.flow = Mock()
        guide.flow.calc.return_value = np.full((128, 128, 2), np.nan, np.float32)
        result = guide.process(np.full((128, 128, 4), 10, np.uint8))
        self.assertTrue(result.reset)
        self.assertEqual(result.confidence, 0)
        self.assertTrue(np.isfinite(result.motion).all())


class EncoderTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.exe = Path(self.temp.name) / "ffmpeg.exe"
        self.exe.write_bytes(b"fake executable for mocked probes")
        probes.clear_encoder_probe_cache()

    def test_default_preset_preserves_p6(self):
        self.assertEqual(probes.nvenc_preset(), "p6")

    def test_speed_preset_is_explicit(self):
        os.environ["DLSS5_NVENC_PRESET"] = "P4"
        self.assertEqual(probes.nvenc_preset(), "p4")

    def test_invalid_preset_rejected(self):
        os.environ["DLSS5_NVENC_PRESET"] = "fastest"
        with self.assertRaises(ValueError):
            probes.nvenc_preset()

    def test_success_probe_reused_and_correct_ordinal(self):
        with patch.object(probes.subprocess, "run", return_value=Mock(returncode=0)) as run:
            for _ in range(2):
                self.assertTrue(probes.probe_encoder(self.exe, "h264_nvenc", 1920, 1080, 1))
            self.assertEqual(run.call_count, 1)
            command = run.call_args.args[0]
            self.assertEqual(command[command.index("-gpu") + 1], "1")
            self.assertEqual(run.call_args.kwargs["timeout"], 20)

    def test_probe_failures_not_cached(self):
        with patch.object(probes.subprocess, "run", side_effect=[Mock(returncode=1), Mock(returncode=0)]) as run:
            self.assertFalse(probes.probe_encoder(self.exe, "hevc_nvenc", 1920, 1080, 1))
            self.assertTrue(probes.probe_encoder(self.exe, "hevc_nvenc", 1920, 1080, 1))
            self.assertEqual(run.call_count, 2)

    def test_probe_timeout_retried(self):
        with patch.object(probes.subprocess, "run", side_effect=[subprocess.TimeoutExpired("ffmpeg", 20), Mock(returncode=0)]):
            self.assertFalse(probes.probe_encoder(self.exe, "hevc_nvenc", 1920, 1080, 1))
            self.assertTrue(probes.probe_encoder(self.exe, "hevc_nvenc", 1920, 1080, 1))

    def test_probe_key_tracks_resolution_visibility_and_binary(self):
        with patch.object(probes.subprocess, "run", return_value=Mock(returncode=0)) as run:
            probes.probe_encoder(self.exe, "h264_nvenc", 1920, 1080, 1)
            probes.probe_encoder(self.exe, "h264_nvenc", 3840, 2160, 1)
            os.environ["CUDA_VISIBLE_DEVICES"] = "GPU-4090"
            probes.probe_encoder(self.exe, "h264_nvenc", 3840, 2160, 1)
            self.exe.write_bytes(b"different binary size")
            probes.probe_encoder(self.exe, "h264_nvenc", 3840, 2160, 1)
            self.assertEqual(run.call_count, 4)

    def test_probe_expiration(self):
        with patch.object(probes.subprocess, "run", return_value=Mock(returncode=0)) as run, \
             patch.object(probes.time, "monotonic", return_value=100):
            probes.probe_encoder(self.exe, "h264_nvenc", 1920, 1080, 1)
            with patch.object(probes.time, "monotonic", return_value=161):
                probes.probe_encoder(self.exe, "h264_nvenc", 1920, 1080, 1)
            self.assertEqual(run.call_count, 2)

    def test_probe_cache_bounded(self):
        with patch.object(probes.subprocess, "run", return_value=Mock(returncode=0)):
            for width in range(130):
                probes.probe_encoder(self.exe, "h264_nvenc", 256 + 2 * width, 256, 1)
            self.assertLessEqual(len(probes._CACHE), 128)

    def test_cpu_codec_never_resolves_gpu(self):
        with patch.object(encoder, "_encoder_probe") as probe:
            for codec in ("H.264", "H.265", "AV1", "ProRes Proxy"):
                self.assertIsNone(encoder.resolve_video_gpu((), "GPU-missing", codec, 1920, 1080))
            probe.assert_not_called()

    def test_nvenc_selection_uses_mapped_cuda_ordinal(self):
        with patch.object(encoder, "_encoder_probe", return_value=True) as probe:
            result = encoder.resolve_video_gpu((GPU1, GPU0), "auto", "AV1 (NVIDIA NVENC)", 3840, 2160)
            self.assertEqual(result["uuid"], GPU0["uuid"])
            probe.assert_called_once_with("av1_nvenc", 3840, 2160, 1)

    def test_unknown_cuda_identity_does_not_fall_back(self):
        with self.assertRaises(RuntimeError):
            encoder.resolve_video_gpu((dict(GPU0, cuda_ordinal=None), GPU1), GPU0["uuid"],
                                      "H.264 (NVIDIA NVENC)", 1920, 1080)

    def test_nvenc_command_preset_gpu_and_quality_metadata(self):
        os.environ["DLSS5_NVENC_PRESET"] = "p4"
        with patch.object(encoder, "_encoder_probe", return_value=True):
            args, name, quality = encoder._codec_command("H.265 (NVIDIA NVENC)", "Good", 3840, 2160, 60, 1, hdr_mode=True)
            self.assertEqual(args[args.index("-gpu") + 1], "1")
            self.assertEqual(args[args.index("-preset") + 1], "p4")
            self.assertEqual(args[args.index("-pix_fmt") + 1], "p010le")
            self.assertEqual(quality["nvenc_preset"], "p4")
            self.assertEqual(quality["multiplier"], 2)
            self.assertEqual(name, "hevc_nvenc")


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_choose_by_name_not_smi_index(self):
        self.assertEqual(profile.choose_4090((GPU1, dict(GPU0, index=7)))["uuid"], GPU0["uuid"])

    def test_ambiguous_4090_fails(self):
        with self.assertRaises(RuntimeError):
            profile.choose_4090((GPU0, dict(GPU0, uuid="GPU-other")))

    def test_missing_cuda_mapping_fails(self):
        with self.assertRaises(RuntimeError):
            profile.choose_4090((dict(GPU0, cuda_identity_verified=False),))

    def test_apply_restore_preserves_other_settings(self):
        profile.atomic_write(self.root / "config/config.ini", "[Settings]\ncodec=H.265\nquality=Best\ncustom_suffix=100%done\n[Other]\nfoo=bar\n")
        backup = profile.apply_profile(self.root, GPU0)
        self.assertTrue(backup.exists())
        config = profile.read_config(self.root)
        self.assertEqual(config["Settings"]["codec"], "H.265 (NVIDIA NVENC)")
        self.assertEqual(config["Settings"]["quality"], "Best")
        profile.restore_profile(self.root)
        config = profile.read_config(self.root)
        self.assertEqual(config["Settings"]["codec"], "H.265")
        self.assertEqual(config["Settings"]["custom_suffix"], "100%done")
        self.assertEqual(config["Other"]["foo"], "bar")
        self.assertNotIn("ai_gpu_uuid", config["Settings"])

    def test_backup_not_overwritten(self):
        backup = profile.apply_profile(self.root, GPU0)
        text = backup.read_text()
        with self.assertRaises(RuntimeError):
            profile.apply_profile(self.root, GPU0)
        self.assertEqual(backup.read_text(), text)

    def test_restore_preserves_later_user_changes(self):
        profile.apply_profile(self.root, GPU0)
        config = profile.read_config(self.root)
        config["Settings"]["codec"] = "ProRes Proxy"
        profile.write_config(self.root, config)
        self.assertIn("codec", profile.restore_profile(self.root))
        self.assertEqual(profile.read_config(self.root)["Settings"]["codec"], "ProRes Proxy")

    def test_empty_config_round_trip(self):
        profile.apply_profile(self.root, GPU0)
        profile.restore_profile(self.root)
        self.assertFalse(profile.read_config(self.root).has_section("Settings"))

    def test_prores_not_converted(self):
        profile.atomic_write(self.root / "config/config.ini", "[Settings]\ncodec=ProRes Proxy\n")
        profile.apply_profile(self.root, GPU0)
        self.assertEqual(profile.read_config(self.root)["Settings"]["codec"], "ProRes Proxy")

    def test_reports_do_not_claim_native_affinity(self):
        self.assertFalse(profile.profile_report((GPU0,), GPU0)["native_adapter_verified"])

    def test_launch_keeps_display_environment_and_quality_override(self):
        with patch.dict(os.environ, {"DLSS5_NVENC_PRESET": "p6", "CUDA_VISIBLE_DEVICES": "GPU-4090"}), \
             patch.object(profile.subprocess, "call", return_value=0) as run:
            self.assertEqual(profile.launch(self.root, GPU0), 0)
            env = run.call_args.kwargs["env"]
            self.assertEqual(env["DLSS5_NVENC_PRESET"], "p6")
            self.assertEqual(env["CUDA_VISIBLE_DEVICES"], "GPU-4090")
            self.assertEqual(env["DLSS5_PREFERRED_GPU_UUID"], GPU0["uuid"])

    def test_launch_refuses_silent_override_of_later_ui_gpu(self):
        profile.apply_profile(self.root, GPU0)
        config = profile.read_config(self.root)
        config["Settings"]["ai_gpu_uuid"] = GPU1["uuid"]
        profile.write_config(self.root, config)
        with self.assertRaises(RuntimeError):
            profile.launch(self.root, GPU0)

    def test_lazy_package_does_not_load_media_stack(self):
        result = subprocess.run([sys.executable, "-c", "import src, sys; assert not any(x in sys.modules for x in ('av','gradio','cv2')); assert 'convert_video' in dir(src)"], cwd=ROOT, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
