"""Opt-in Windows RTX 4090 profile. No administrator rights or registry changes.

Run with the packaged Python: --diagnose (read only), --apply, --launch, --restore.
Native DLSSNR/DLSSG adapter binding is NOT implemented by their current wrappers.
"""
from __future__ import annotations

import argparse
import configparser
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SECTION = "Settings"
PROFILE_NAME = "rtx4090-profile.json"
CODECS = {"H.264": "H.264 (NVIDIA NVENC)", "H.265": "H.265 (NVIDIA NVENC)",
          "HEVC": "H.265 (NVIDIA NVENC)", "AV1": "AV1 (NVIDIA NVENC)"}
CODEC_DEFAULTS = {"codec": "H.264", "frame_interpolation_codec": "H.264",
                  "upscale_codec": "H.265 (NVIDIA NVENC)"}


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_config(root: Path) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(interpolation=None)
    path = root / "config" / "config.ini"
    if path.exists():
        # Refuse malformed files rather than silently discarding user settings.
        with path.open(encoding="utf-8-sig") as stream:
            parser.read_file(stream)
    return parser


def write_config(root: Path, parser: configparser.ConfigParser) -> None:
    stream = io.StringIO()
    parser.write(stream)
    atomic_write(root / "config" / "config.ini", stream.getvalue())


def detect_devices(root: Path) -> tuple[dict, ...]:
    # Load the standard-library-only detector without importing the media stack.
    path = root / "src" / "core" / "gpu_detection.py"
    spec = importlib.util.spec_from_file_location("_dlss5_gpu_detector", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load GPU detector: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.detect_gpus()


def choose_4090(devices: tuple[dict, ...], uuid: str | None = None) -> dict:
    candidates = [gpu for gpu in devices if re.search(r"\bRTX\s+4090\b", gpu["name"], re.I)]
    if uuid:
        candidates = [gpu for gpu in candidates if gpu["uuid"] == uuid]
    if len(candidates) != 1:
        raise RuntimeError("Expected one RTX 4090. Use --gpu-uuid for multiple cards; "
                           "the profile never falls back to the display GPU.")
    gpu = dict(candidates[0])
    if not str(gpu.get("uuid", "")).startswith("GPU-"):
        raise RuntimeError("The 4090 has no stable NVIDIA GPU UUID.")
    if gpu.get("cuda_ordinal") is None or not gpu.get("cuda_identity_verified"):
        raise RuntimeError("Could not map the 4090 PCI identity to a CUDA ordinal. "
                           "Check nvcuda.dll, the driver and CUDA_VISIBLE_DEVICES; "
                           "a Task Manager/nvidia-smi index is not a CUDA ordinal.")
    return gpu


def profile_report(devices: tuple[dict, ...], gpu: dict) -> dict:
    return {
        "selected_gpu": gpu,
        "detected_gpus": list(devices),
        "binding": {
            "rtx_video": "Explicit DirectX LUID derived from matched CUDA device",
            "nvenc": "Explicit matched CUDA ordinal; actual encoder probes support",
            "dlss_neural_rendering": "Requested UUID only; native adapter NOT verified",
            "dlss_frame_generation": "Requested UUID only; native adapter NOT verified",
        },
        "native_adapter_verified": False,
        "vram": "No artificial cap added. Native runtimes allocate their own VRAM; "
                "this profile does not force 24 GB allocation or pool two GPUs.",
        "display": "No display, primary-adapter, browser, MPV or registry settings changed.",
        "windows": "Windows 10 native-runtime compatibility requires a local render test.",
    }


def apply_profile(root: Path, gpu: dict, *, convert_codecs: bool = True) -> Path:
    path = root / "config" / PROFILE_NAME
    if path.exists():
        raise RuntimeError("A 4090 profile is already applied. Restore it before applying "
                           "another; the original backup has not been overwritten.")
    parser = read_config(root)
    old_section = parser.has_section(SECTION)
    if not old_section:
        parser.add_section(SECTION)
    updates = {"ai_gpu_uuid": gpu["uuid"], "video_gpu_uuid": gpu["uuid"]}
    if convert_codecs:
        for key, default in CODEC_DEFAULTS.items():
            old = parser.get(SECTION, key, fallback=default)
            updates[key] = CODECS.get(old, old)  # ProRes and existing NVENC preserved.
    previous = {key: parser.get(SECTION, key, fallback=None) for key in updates}
    state = {"version": 1, "gpu_uuid": gpu["uuid"], "previous": previous,
             "applied": updates, "section_existed": old_section}
    # Persist rollback information BEFORE changing settings. An interrupted
    # operation can be recovered with --restore; original backup is never lost.
    atomic_write(path, json.dumps(state, indent=2))
    for key, value in updates.items():
        parser.set(SECTION, key, value)
    write_config(root, parser)
    return path


def restore_profile(root: Path) -> list[str]:
    path = root / "config" / PROFILE_NAME
    state = json.loads(path.read_text(encoding="utf-8"))
    if state.get("version") != 1:
        raise ValueError("Unsupported profile backup version.")
    parser = read_config(root)
    skipped = []
    for key, applied in state["applied"].items():
        old = state["previous"][key]
        current = parser.get(SECTION, key, fallback=None)
        if current == old:
            continue  # Also handles an interrupted apply before the config write.
        if current != applied:
            skipped.append(key)  # Preserve subsequent changes made in the app.
            continue
        if old is None:
            parser.remove_option(SECTION, key)
        else:
            parser.set(SECTION, key, old)
    if (not state["section_existed"] and parser.has_section(SECTION)
            and not parser.items(SECTION)):
        parser.remove_section(SECTION)
    write_config(root, parser)
    # Keep a history copy, but allow a fresh explicit --apply later.
    os.replace(path, path.with_name("rtx4090-profile.restored.json"))
    return skipped


def launch(root: Path, gpu: dict) -> int:
    state_path = root / "config" / PROFILE_NAME
    if not state_path.exists():
        apply_profile(root, gpu)
    state = json.loads(state_path.read_text(encoding="utf-8"))
    if state.get("gpu_uuid") != gpu["uuid"]:
        raise RuntimeError("The saved profile belongs to another GPU. Restore it first.")
    # Do not override later explicit UI selections or codec choices on each run.
    parser = read_config(root)
    for key in ("ai_gpu_uuid", "video_gpu_uuid"):
        if parser.get(SECTION, key, fallback="auto") not in {"auto", gpu["uuid"]}:
            raise RuntimeError(f"{key} was changed in Settings. Select the 4090/Automatic "
                               "or use the normal start.bat to keep that other GPU.")
    env = os.environ.copy()
    env["DLSS5_PREFERRED_GPU_UUID"] = gpu["uuid"]
    env.setdefault("DLSS5_NVENC_PRESET", "p4")
    if env["DLSS5_NVENC_PRESET"].lower() not in {f"p{i}" for i in range(1, 8)}:
        raise ValueError("DLSS5_NVENC_PRESET must be p1 through p7.")
    print(f"NVENC preset: {env['DLSS5_NVENC_PRESET']} (normal start.bat defaults to p6).", flush=True)
    print("WARNING: DLSSNR/Frame Generation native adapter binding remains unverified. "
          "See docs/RTX4090_WINDOWS10.md before relying on GPU isolation.", flush=True)
    return subprocess.call([sys.executable, str(root / "app.py")], cwd=root, env=env)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--diagnose", action="store_true", help="Read-only GPU/binding report (default)")
    actions.add_argument("--apply", action="store_true", help="Back up relevant settings; select 4090 and NVENC")
    actions.add_argument("--launch", action="store_true", help="Apply once, then launch with NVENC p4")
    actions.add_argument("--restore", action="store_true", help="Restore unchanged profile keys; preserve other edits")
    parser.add_argument("--gpu-uuid", help="Select an exact RTX 4090 UUID when more than one is installed")
    args = parser.parse_args(argv)
    try:
        if os.name != "nt":
            raise RuntimeError("This profile targets 64-bit Windows, not WSL/Linux.")
        if sys.maxsize <= 2**32:
            raise RuntimeError("Use the packaged 64-bit Python interpreter.")
        if args.restore:
            skipped = restore_profile(ROOT)
            print("Profile restored. Later user changes preserved: " + (", ".join(skipped) or "none"))
            return 0
        devices = detect_devices(ROOT)
        saved = ROOT / "config" / PROFILE_NAME
        saved_uuid = json.loads(saved.read_text(encoding="utf-8"))["gpu_uuid"] if saved.exists() else None
        gpu = choose_4090(devices, args.gpu_uuid or saved_uuid)
        print(json.dumps(profile_report(devices, gpu), indent=2), flush=True)
        if args.apply:
            print(f"Applied; rollback information: {apply_profile(ROOT, gpu)}")
        elif args.launch:
            return launch(ROOT, gpu)
        return 0
    except (OSError, ValueError, RuntimeError, configparser.Error) as exc:
        print(f"4090 profile: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
