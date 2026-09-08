from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
PROFILE_PATH = ROOT / "tools" / "rtx4090_profile.py"
SPEC = importlib.util.spec_from_file_location("_rtx4090_profile_best_tests", PROFILE_PATH)
PROFILE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(PROFILE)

GPU = {
    "name": "NVIDIA GeForce RTX 4090",
    "uuid": "GPU-4090",
    "index": 0,
    "memory_mb": 24564,
    "pci_bus_id": "0000:01:00.0",
    "cuda_ordinal": 0,
    "cuda_identity_verified": True,
    "ai_compatible": True,
}


class BestProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_best_profile_sets_hardware_defaults_only(self):
        PROFILE.atomic_write(
            self.root / "config" / "config.ini",
            "[Settings]\n"
            "upscaling_factor=3\n"
            "frame_interpolation_target_fps=144\n"
            "hdr_mode=true\n"
            "nr_style=Cinematic\n"
            "quality=Best\n",
        )
        PROFILE.apply_profile(self.root, GPU, best_settings=True)
        settings = PROFILE.read_config(self.root)["Settings"]
        self.assertEqual(settings["ai_gpu_uuid"], GPU["uuid"])
        self.assertEqual(settings["video_gpu_uuid"], GPU["uuid"])
        self.assertEqual(settings["codec"], "H.265 (NVIDIA NVENC)")
        self.assertEqual(settings["frame_interpolation_codec"], "H.265 (NVIDIA NVENC)")
        self.assertEqual(settings["upscale_codec"], "H.265 (NVIDIA NVENC)")
        self.assertEqual(settings["upscale_vsr_quality"], "4")
        self.assertEqual(settings["upscale_image_vsr_quality"], "4")
        self.assertEqual(settings["quality"], "Auto (Default)")
        self.assertEqual(settings["upscaling_factor"], "3")
        self.assertEqual(settings["frame_interpolation_target_fps"], "144")
        self.assertEqual(settings["hdr_mode"], "true")
        self.assertEqual(settings["nr_style"], "Cinematic")

    def test_restore_returns_original_values(self):
        PROFILE.atomic_write(
            self.root / "config" / "config.ini",
            "[Settings]\ncodec=H.264\nquality=Best\nupscale_vsr_quality=2\n",
        )
        PROFILE.apply_profile(self.root, GPU, best_settings=True)
        skipped = PROFILE.restore_profile(self.root)
        settings = PROFILE.read_config(self.root)["Settings"]
        self.assertEqual(skipped, [])
        self.assertEqual(settings["codec"], "H.264")
        self.assertEqual(settings["quality"], "Best")
        self.assertEqual(settings["upscale_vsr_quality"], "2")
        self.assertNotIn("ai_gpu_uuid", settings)

    def test_v1_backup_upgrades_without_losing_original_codec(self):
        PROFILE.atomic_write(
            self.root / "config" / "config.ini",
            "[Settings]\nai_gpu_uuid=GPU-4090\nvideo_gpu_uuid=GPU-4090\ncodec=H.264 (NVIDIA NVENC)\n",
        )
        backup = {
            "version": 1,
            "gpu_uuid": GPU["uuid"],
            "previous": {
                "ai_gpu_uuid": None,
                "video_gpu_uuid": None,
                "codec": "H.264",
            },
            "applied": {
                "ai_gpu_uuid": GPU["uuid"],
                "video_gpu_uuid": GPU["uuid"],
                "codec": "H.264 (NVIDIA NVENC)",
            },
            "section_existed": True,
        }
        PROFILE.atomic_write(
            self.root / "config" / PROFILE.PROFILE_NAME,
            json.dumps(backup),
        )
        PROFILE.apply_profile(self.root, GPU, best_settings=True)
        upgraded = json.loads(
            (self.root / "config" / PROFILE.PROFILE_NAME).read_text(encoding="utf-8")
        )
        self.assertEqual(upgraded["version"], 2)
        self.assertEqual(upgraded["previous"]["codec"], "H.264")
        PROFILE.restore_profile(self.root)
        self.assertEqual(PROFILE.read_config(self.root)["Settings"]["codec"], "H.264")

    def test_best_profile_reapplies_controlled_settings_but_restore_stays_original(self):
        PROFILE.atomic_write(
            self.root / "config" / "config.ini",
            "[Settings]\ncodec=H.264\n",
        )
        PROFILE.apply_profile(self.root, GPU, best_settings=True)
        config = PROFILE.read_config(self.root)
        config["Settings"]["codec"] = "AV1 (NVIDIA NVENC)"
        PROFILE.write_config(self.root, config)
        PROFILE.apply_profile(self.root, GPU, best_settings=True)
        self.assertEqual(
            PROFILE.read_config(self.root)["Settings"]["codec"],
            "H.265 (NVIDIA NVENC)",
        )
        PROFILE.restore_profile(self.root)
        self.assertEqual(PROFILE.read_config(self.root)["Settings"]["codec"], "H.264")

    def test_best_launch_defaults_to_p5_without_touching_cuda_visibility(self):
        PROFILE.apply_profile(self.root, GPU, best_settings=True)
        with patch.dict(os.environ, {"CUDA_VISIBLE_DEVICES": "existing-value"}, clear=True), \
             patch.object(PROFILE.subprocess, "call", return_value=0) as call:
            self.assertEqual(PROFILE.launch(self.root, GPU, best_settings=True), 0)
            env = call.call_args.kwargs["env"]
            self.assertEqual(env["DLSS5_NVENC_PRESET"], "p5")
            self.assertEqual(env["CUDA_VISIBLE_DEVICES"], "existing-value")
            self.assertEqual(env["DLSS5_PREFERRED_GPU_UUID"], GPU["uuid"])


if __name__ == "__main__":
    unittest.main()
