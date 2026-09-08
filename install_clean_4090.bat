@echo off
setlocal EnableExtensions
cd /d "%~dp0"
chcp 65001 >nul
title DLSS 5 Visual Enhancer - Clean RTX 4090 Installer
cls

echo DLSS 5 Visual Enhancer - Clean RTX 4090 Installer
echo.
echo This downloads the verified v7.0 portable runtime, overlays this repository,
echo verifies required files, applies the RTX 4090 best profile, and preserves an
echo existing install as a timestamped backup instead of deleting it.
echo.

where powershell.exe >nul 2>nul
if errorlevel 1 (
    echo ERROR: Windows PowerShell is required.
    pause
    exit /b 1
)

set "INSTALLER=%~dp0tools\install_clean_4090.ps1"
if not exist "%INSTALLER%" (
    echo ERROR: Missing installer helper: "%INSTALLER%"
    pause
    exit /b 1
)

rem Double-click = clean install to a sibling DLSS5_4090_PORTABLE folder and launch.
rem Advanced usage examples:
rem   install_clean_4090.bat -InstallDir "E:\Apps\DLSS5_4090"
rem   install_clean_4090.bat -InstallDir "E:\Apps\DLSS5_4090" -Launch
rem   install_clean_4090.bat -KeepDownload
if "%~1"=="" (
    powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%INSTALLER%" -SourceRoot "%~dp0" -Launch
) else (
    powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%INSTALLER%" -SourceRoot "%~dp0" %*
)

set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
    echo.
    echo Installation failed with exit code %RC%.
    pause
)
exit /b %RC%
