"""Opt-in Windows RTX 4090 profile. No administrator rights or registry changes.

Run with packaged Python:
  --diagnose
  --apply [--best-settings]
  --launch [--best-settings]
  --restore

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
PROFILE_VERSION = 2
CODECS = {
    "H.264": "H.264 (NVIDIA NVENC)",
    "H.265": "H.265 (NVIDIA NVENC)",
    "HEVC": "H.265 (NVIDIA NVENC)",
    "AV1": "AV1 (NVIDIA NVENC)",
}
CODEC_DEFAULTS = {
    "codec": "H.264",
    "frame_interpolation_codec": "H.264",
    "upscale_codec": "H.265 (NVIDIA NVENC)",
}

# Hardware-oriented defaults only. Resolution, upscale factor, target FPS, HDR
# enablement, Neural Rendering effect controls, and rename/output choices are
# intentionally left alone because they depend on the media/job rather than GPU.
#
# H.265 NVENC is used instead of AV1 as the portable "best default": the 4090
# supports both, while HEVC keeps broader playback/editing compatibility and
# supports 10-bit HDR in this application. Auto bitrate avoids turning disk I/O
# into a bottleneck. VSR 4 is the highest quality exposed by this repo.
BEST_4090_SETTINGS = {
    "codec": "H.265 (NVIDIA NVENC)",
    "container": "MP4",
    "quality": "Auto (Default)",
    "frame_interpolation_engine": "Auto",
    "frame_interpolation_codec": "H.265 (NVIDIA NVENC)",
    "frame_interpolation_container": "MP4",
    "frame_interpolation_quality": "Auto (Default)",
    "upscale_vsr_enabled": "True",
    "upscale_vsr_quality": "4",
    "upscale_image_vsr_quality": "4",
    "upscale_codec": "H.265 (NVIDIA NVENC)",
    "upscale_container": "MP4",
    "upscale_quality": "Auto (Default)",
    "preview_encoding": "Auto",
    "dlss_model_preset": "Default",
}


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
        raise RuntimeError(
            "Expected one RTX 4090. Use --gpu-uuid for multiple cards; "
            "the profile never falls back to the display GPU."
        )
    gpu = dict(candidates[0])
    if not str(gpu.get("uuid", "")).startswith("GPU-"):
        raise RuntimeError("The 4090 has no stable NVIDIA GPU UUID.")
    if gpu.get("cuda_ordinal") is None or not gpu.get("cuda_identity_verified"):
        raise RuntimeError(
            "Could not map the 4090 PCI identity to a CUDA ordinal. "
            "Check nvcuda.dll, the driver and CUDA_VISIBLE_DEVICES; "
            "a Task Manager/nvidia-smi index is not a CUDA ordinal."
        )
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
        "best_4090_profile": {
            "nvenc_preset": "p5",
            "config": BEST_4090_SETTINGS,
            "not_forced": [
                "upscaling_factor",
                "frame_interpolation_target_fps",
                "HDR enablement",
                "Neural Rendering effect strength/style",
            ],
        },
        "native_adapter_verified": False,
        "vram": (
            "No artificial cap added. Native runtimes allocate their own VRAM; "
            "this profile does not force 24 GB allocation or pool two GPUs."
        ),
        "display": "No display, primary-adapter, browser, MPV or registry settings changed.",
        "windows": "Windows 10 native-runtime compatibility requires a local render test.",
    }


def _read_profile_state(path: Path) -> dict | None:
    if not path.exists():
        return None
    state = json.loads(path.read_text(encoding="utf-8"))
    version = int(state.get("version", 0))
    if version not in (1, PROFILE_VERSION):
        raise ValueError(f"Unsupported RTX 4090 profile backup version: {version}.")
    for key in ("gpu_uuid", "previous", "applied", "section_existed"):
        if key not in state:
            raise ValueError(f"RTX 4090 profile backup is missing {key!r}.")
    if not isinstance(state["previous"], dict) or not isinstance(state["applied"], dict):
        raise ValueError("RTX 4090 profile backup is malformed.")
    return state


def _profile_updates(
    parser: configparser.ConfigParser,
    gpu: dict,
    *,
    convert_codecs: bool,
    best_settings: bool,
) -> dict[str, str]:
    updates: dict[str, str] = {
        "ai_gpu_uuid": str(gpu["uuid"]),
        "video_gpu_uuid": str(gpu["uuid"]),
    }
    if best_settings:
        updates.update(BEST_4090_SETTINGS)
    elif convert_codecs:
        for key, default in CODEC_DEFAULTS.items():
            old = parser.get(SECTION, key, fallback=default)
            updates[key] = CODECS.get(old, old)  # ProRes and existing NVENC preserved.
    return updates


def apply_profile(
    root: Path,
    gpu: dict,
    *,
    convert_codecs: bool = True,
    best_settings: bool = False,
) -> Path:
    """Apply/upgrade the reversible profile.

    Existing rollback values are never replaced. If --best-settings adds a new
    controlled key, its value immediately before the upgrade becomes that key's
    rollback value. Calling --best-settings is explicit permission to reapply
    the hardware-oriented defaults even if those controlled settings changed.
    """
    path = root / "config" / PROFILE_NAME
    parser = read_config(root)
    old_section = parser.has_section(SECTION)
    if not old_section:
        parser.add_section(SECTION)

    state = _read_profile_state(path)
    if state is not None and state["gpu_uuid"] != gpu["uuid"]:
        raise RuntimeError("The saved RTX 4090 profile belongs to another GPU. Restore it first.")

    updates = _profile_updates(
        parser, gpu, convert_codecs=convert_codecs, best_settings=best_settings
    )
    previous = dict(state["previous"]) if state else {}
    applied = dict(state["applied"]) if state else {}
    for key in updates:
        if key not in previous:
            previous[key] = parser.get(SECTION, key, fallback=None)
    applied.update(updates)

    new_state = {
        "version": PROFILE_VERSION,
        "gpu_uuid": gpu["uuid"],
        "previous": previous,
        "applied": applied,
        "section_existed": state["section_existed"] if state else old_section,
        "mode": (
            "best-4090"
            if best_settings
            else state.get("mode", "gpu-and-nvenc") if state else "gpu-and-nvenc"
        ),
    }
    # Persist rollback information BEFORE changing settings. An interrupted
    # operation can be recovered with --restore; original values are never lost.
    atomic_write(path, json.dumps(new_state, indent=2))
    for key, value in updates.items():
        parser.set(SECTION, key, value)
    write_config(root, parser)
    return path


def restore_profile(root: Path) -> list[str]:
    path = root / "config" / PROFILE_NAME
    state = _read_profile_state(path)
    if state is None:
        raise RuntimeError("No applied RTX 4090 profile backup was found.")
    parser = read_config(root)
    skipped = []
    for key, applied in state["applied"].items():
        old = state["previous"].get(key)
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
    if (
        not state["section_existed"]
        and parser.has_section(SECTION)
        and not parser.items(SECTION)
    ):
        parser.remove_section(SECTION)
    write_config(root, parser)
    # Keep a history copy, but allow a fresh explicit --apply later.
    restored = path.with_name("rtx4090-profile.restored.json")
    if restored.exists():
        restored.unlink()
    os.replace(path, restored)
    return skipped


def launch(root: Path, gpu: dict, *, best_settings: bool = False) -> int:
    state_path = root / "config" / PROFILE_NAME
    if not state_path.exists():
        apply_profile(root, gpu, best_settings=best_settings)
    elif best_settings:
        # Upgrade the already-applied v1/v2 profile without losing original
        # rollback values. This makes the merged PR1 profile adopt the new
        # 4090 best defaults on the next normal start_4090.bat launch.
        apply_profile(root, gpu, best_settings=True)

    state = _read_profile_state(state_path)
    assert state is not None
    if state.get("gpu_uuid") != gpu["uuid"]:
        raise RuntimeError("The saved profile belongs to another GPU. Restore it first.")

    # Do not override a later explicit selection of a different GPU.
    parser = read_config(root)
    for key in ("ai_gpu_uuid", "video_gpu_uuid"):
        if parser.get(SECTION, key, fallback="auto") not in {"auto", gpu["uuid"]}:
            raise RuntimeError(
                f"{key} was changed in Settings. Select the 4090/Automatic "
                "or use the normal start.bat to keep that other GPU."
            )

    env = os.environ.copy()
    env["DLSS5_PREFERRED_GPU_UUID"] = gpu["uuid"]
    env.setdefault("DLSS5_NVENC_PRESET", "p5" if best_settings else "p4")
    preset = env["DLSS5_NVENC_PRESET"].lower()
    if preset not in {f"p{i}" for i in range(1, 8)}:
        raise ValueError("DLSS5_NVENC_PRESET must be p1 through p7.")
    env["DLSS5_NVENC_PRESET"] = preset

    label = "RTX 4090 balanced-performance" if best_settings else "RTX 4090"
    print(f"{label} profile active | NVENC preset: {preset}", flush=True)
    if best_settings:
        print(
            "Best-profile defaults: H.265 NVENC, Auto bitrate, RTX Video VSR Ultra, "
            "Auto Frame Generation engine. Job-specific scale/FPS/HDR choices are unchanged.",
            flush=True,
        )
    print(
        "WARNING: DLSSNR/Frame Generation native adapter binding remains unverified. "
        "See docs/RTX4090_WINDOWS10.md before relying on GPU isolation.",
        flush=True,
    )
    return subprocess.call([sys.executable, str(root / "app.py")], cwd=root, env=env)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--diagnose", action="store_true", help="Read-only GPU/binding report (default)")
    actions.add_argument("--apply", action="store_true", help="Back up relevant settings and apply the 4090 profile")
    actions.add_argument("--launch", action="store_true", help="Apply/update the profile, then launch the app")
    actions.add_argument("--restore", action="store_true", help="Restore unchanged profile keys; preserve other edits")
    parser.add_argument(
        "--best-settings",
        action="store_true",
        help="Use the 4090 balanced-performance defaults (H.265 NVENC, p5, VSR Ultra)",
    )
    parser.add_argument("--gpu-uuid", help="Select an exact RTX 4090 UUID when more than one is installed")
    args = parser.parse_args(argv)
    try:
        if os.name != "nt":
            raise RuntimeError("This profile targets 64-bit Windows, not WSL/Linux.")
        if sys.maxsize <= 2**32:
            raise RuntimeError("Use the packaged 64-bit Python interpreter.")
        if args.restore and args.best_settings:
            raise ValueError("--best-settings cannot be combined with --restore.")

        if args.restore:
            skipped = restore_profile(ROOT)
            print("Profile restored. Later user changes preserved: " + (", ".join(skipped) or "none"))
            return 0

        devices = detect_devices(ROOT)
        saved = ROOT / "config" / PROFILE_NAME
        saved_state = _read_profile_state(saved)
        saved_uuid = saved_state["gpu_uuid"] if saved_state else None
        gpu = choose_4090(devices, args.gpu_uuid or saved_uuid)
        print(json.dumps(profile_report(devices, gpu), indent=2), flush=True)

        if args.apply:
            print(
                "Applied; rollback information: "
                f"{apply_profile(ROOT, gpu, best_settings=args.best_settings)}"
            )
        elif args.launch:
            return launch(ROOT, gpu, best_settings=args.best_settings)
        elif args.best_settings:
            raise ValueError("--best-settings must be used with --apply or --launch.")
        return 0
    except (OSError, ValueError, RuntimeError, configparser.Error, json.JSONDecodeError) as exc:
        print(f"4090 profile: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
