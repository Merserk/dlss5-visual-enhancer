@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul
title DLSS 5 Visual Enhancer - RTX 4090
set "PYTHONNOUSERSITE=1"
set "PYTHONDONTWRITEBYTECODE=1"
set "PYTHONIOENCODING=utf-8"
set "GRADIO_ANALYTICS_ENABLED=False"
set "PYTHON_EXE=%~dp0bin\python-3.13.15-embed-amd64\python.exe"
if not exist "%PYTHON_EXE%" (
    echo Missing portable Python. Apply this source update inside the matching v7.0 portable release.
    pause
    exit /b 1
)
if "%~1"=="" (
    "%PYTHON_EXE%" "%~dp0tools\rtx4090_profile.py" --launch
) else (
    "%PYTHON_EXE%" "%~dp0tools\rtx4090_profile.py" %*
)
if errorlevel 1 pause
