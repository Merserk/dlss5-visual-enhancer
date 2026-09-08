@echo off
setlocal EnableExtensions
cd /d "%~dp0"
chcp 65001 >nul
title DLSS 5 Visual Enhancer - RTX 4090 Best Profile
cls

rem RTX 4090 balanced-performance launcher.
rem - p5 is NVIDIA's balanced quality/performance NVENC preset.
rem - The Python profile pins AI/video selections to the verified RTX 4090 UUID,
rem   uses H.265 NVENC + Auto bitrate, RTX Video VSR Ultra, and Auto FG engine.
rem - 3-second compatibility previews use H.264 NVENC instead of CPU libx264.
rem - FFmpeg CUDA/NVDEC input decode is enabled in Auto mode with safe PyAV fallback.
rem - Job-specific scale, output FPS, HDR enablement and Neural Rendering effect
rem   controls are deliberately not forced.
rem - CUDA_VISIBLE_DEVICES is NOT set: DLSSNR/DLSSG are Direct3D/native workers,
rem   and hiding CUDA devices is not a reliable DirectX adapter-binding method.
set "PYTHONNOUSERSITE=1"
set "PYTHONDONTWRITEBYTECODE=1"
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"
set "GRADIO_ANALYTICS_ENABLED=False"

rem Allow advanced overrides before launch:
rem   set DLSS5_NVENC_PRESET=p6
rem   set DLSS5_FAST_PREVIEW_NVENC=0
rem   set DLSS5_CUDA_DECODE=off
rem   start_4090.bat
if not defined DLSS5_NVENC_PRESET set "DLSS5_NVENC_PRESET=p5"
if not defined DLSS5_FAST_PREVIEW_NVENC set "DLSS5_FAST_PREVIEW_NVENC=1"
if not defined DLSS5_CUDA_DECODE set "DLSS5_CUDA_DECODE=auto"

set "PYTHON_EXE=%~dp0bin\python-3.13.15-embed-amd64\python.exe"
if not exist "%PYTHON_EXE%" (
    echo ERROR: The portable runtime is missing.
    echo.
    echo For a clean install, run:
    echo   install_clean_4090.bat
    echo.
    echo The installer downloads the verified v7.0 runtime and overlays this source.
    pause
    exit /b 1
)

if "%~1"=="" (
    "%PYTHON_EXE%" "%~dp0tools\rtx4090_profile.py" --launch --best-settings
) else (
    "%PYTHON_EXE%" "%~dp0tools\rtx4090_profile.py" %*
)

set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" pause
exit /b %RC%
