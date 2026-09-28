"""Keep process-owned writable state beside the bundled application."""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEMP = ROOT / "temp"
UI_SETTINGS_PATH = TEMP / "desktop.ini"
_APP_ROOT_TOKEN = "${APP_ROOT}"
_APP_PATH_PREFIX = "${APP_ROOT}/"


def encode_app_path(value: str) -> str:
    """Keep references to files inside the bundle valid when it is moved."""
    if not value:
        return value
    path = Path(value)
    if not path.is_absolute():
        return value
    try:
        relative = path.resolve().relative_to(ROOT.resolve())
    except (OSError, ValueError):
        return value
    return _APP_PATH_PREFIX + relative.as_posix() if relative.parts else _APP_ROOT_TOKEN


def decode_app_path(value: str) -> str:
    if value == _APP_ROOT_TOKEN:
        return str(ROOT)
    if not value.startswith(_APP_PATH_PREFIX):
        return value
    relative = Path(value[len(_APP_PATH_PREFIX):])
    if not relative.parts or relative.is_absolute() or ".." in relative.parts:
        return value
    return str(ROOT / relative)


def configure_portable_environment(root: Path = ROOT) -> Path:
    """Set child-process and library cache paths before loading Qt or FFmpeg."""
    temp = Path(root).resolve() / "temp"
    scratch = temp / "scratch"
    profile = temp / "profile"
    paths = {
        "TEMP": scratch,
        "TMP": scratch,
        "TMPDIR": scratch,
        "APPDATA": profile / "Roaming",
        "LOCALAPPDATA": profile / "Local",
        "XDG_CONFIG_HOME": profile / "config",
        "XDG_DATA_HOME": profile / "data",
        "XDG_CACHE_HOME": profile / "cache",
        "QML_DISK_CACHE_PATH": temp / "qml-cache",
        "CUDA_CACHE_PATH": temp / "cuda-cache",
    }
    try:
        for path in (temp, temp / "app", *paths.values()):
            path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RuntimeError(f"The portable temp folder must be writable: {temp}") from exc
    os.environ.update({key: str(path) for key, path in paths.items()})
    # Qt Quick otherwise writes its automatic shader cache to the real
    # QStandardPaths CacheLocation under AppData on Windows.
    os.environ["QT_DISABLE_SHADER_DISK_CACHE"] = "1"
    os.environ["QSG_RHI_DISABLE_SHADER_DISK_CACHE"] = "1"
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    os.environ["PYTHONNOUSERSITE"] = "1"
    sys.dont_write_bytecode = True
    tempfile.tempdir = None  # Re-evaluate TEMP if a caller imported tempfile first.
    return temp
