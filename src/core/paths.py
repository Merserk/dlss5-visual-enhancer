from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
TEMP = ROOT / "temp"
APP_TEMP = TEMP / "app"
PREVIEW_CACHE = APP_TEMP / "cache"
RUNTIME = ROOT / "bin" / "runtime"
DLSSG_DIR = RUNTIME / "dlssg"
DLSSSR_DIR = RUNTIME / "dlsssr"
DLSSSR_BRIDGE = DLSSSR_DIR / "neuroframe_engine_super_resolution.dll"
DLSSSR_RUNTIME = DLSSSR_DIR / "nvngx_dlss.dll"
# In-process D3D12/NGX feature-18 runtime. The bridge and caller shim are
# self-contained and require no Python tensor framework or external add-on.
DLSSNR_DIR = RUNTIME / "dlssnr"
DLSSNR_BRIDGE = DLSSNR_DIR / "neuroframe_engine_neural_rendering.dll"
DLSSNR_CALLER_SHIM = DLSSNR_DIR / "neuroframe_caller.dll"
FFMPEG = ROOT / "bin" / "ffmpeg" / "bin" / "ffmpeg.exe"
FFPROBE = ROOT / "bin" / "ffmpeg" / "bin" / "ffprobe.exe"
NEURAL_RUNTIME = DLSSNR_DIR / "nvngx_dlssnr.dll"
# Live-tab externals (vendored, optional: only Live sessions require them).
MPV = ROOT / "bin" / "mpv" / "mpv.exe"
YTDLP = ROOT / "bin" / "yt-dlp" / "yt-dlp.exe"
# Ephemeral per-session HLS working dirs for Live (swept on stop/startup).
LIVE_DIR = ROOT / "live"
OUTPUTS = ROOT / "outputs"
LOGS = ROOT / "logs"
JOBS = ROOT / "jobs"
CONFIG_DIR = ROOT / "config"
CONFIG_PATH = CONFIG_DIR / "config.ini"
