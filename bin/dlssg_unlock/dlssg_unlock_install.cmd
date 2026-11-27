@echo off
rem ===========================================================================
rem  Visual Enhancer - DLSS-G unlocker installer (RTX 20/30)
rem
rem  Installs the DLSS-G unlocker into the Visual Enhancer folder:
rem    dxgi.dll            proxy that forwards to the real system DLL and
rem                        intercepts the NVIDIA DLSS-G runtime
rem    dlssg_sm86.ini      its configuration (DLSS engine only)
rem    dlssg_sm86\         backend, frame-generation kernels, runtime
rem
rem  Nothing is installed when the file or folder already exists there - an
rem  existing installation is left untouched. Verification is a byte
rem  comparison (fc /b), not a hash.
rem
rem  Usage:
rem    dlssg_unlock_install.cmd                 apply
rem    dlssg_unlock_install.cmd -Check          report only
rem    dlssg_unlock_install.cmd -WhatIf         show what would happen
rem    dlssg_unlock_install.cmd -AppRoot <dir>  override the installation folder
rem
rem  The installation folder is found as: -AppRoot, then the folder two levels
rem  above this script (bin\dlssg_unlock\..\..), then %APPPATH%\Visual Enhancer.
rem
rem  Exit codes
rem    0  ok / nothing to do        1  check failed (something to do)
rem    2  no installation found     4  copy or verification failed
rem
rem  Environment: NOPAUSE=1 skips the final pause.
rem ===========================================================================
setlocal EnableExtensions EnableDelayedExpansion
set "RC=0"
set "MODE=apply"
set "APPROOT="
set "SCRIPTDIR=%~dp0"
for %%I in ("%SCRIPTDIR%.") do set "SCRIPTDIR=%%~fI"

:parse
if "%~1"=="" goto :parsed
if /i "%~1"=="-Check"   ( set "MODE=check"  & shift & goto :parse )
if /i "%~1"=="-WhatIf"  ( set "MODE=whatif" & shift & goto :parse )
if /i "%~1"=="-AppRoot" ( set "APPROOT=%~2" & shift & shift & goto :parse )
echo [ERROR] Unknown argument: %~1
set "RC=2"
goto :done
:parsed

rem --- 1) locate the installation ------------------------------------------
set "APP="
if defined APPROOT if exist "%APPROOT%\Visual Enhancer.exe" set "APP=%APPROOT%"
if defined APPROOT if not defined APP goto :bad-approot
if not defined APP if exist "%SCRIPTDIR%\..\..\Visual Enhancer.exe" set "APP=%SCRIPTDIR%\..\.."
if not defined APP if defined APPPATH if exist "%APPPATH%\Visual Enhancer\Visual Enhancer.exe" set "APP=%APPPATH%\Visual Enhancer"
if not defined APP goto :no-app
for %%I in ("%APP%.") do set "APP=%%~fI"
if not exist "%APP%\Visual Enhancer.exe" goto :no-app

echo ============================================================
echo   Visual Enhancer - DLSS-G unlocker (RTX 20/30)
echo ============================================================
echo.
echo Installation: %APP%
echo Source:       %SCRIPTDIR%
echo.

set "P_FILE1=dlssg_sm86.ini"
set "P_FILE2=dxgi.dll"
set "P_DIR1=dlssg_sm86"

call :check-file "%P_FILE1%" S1
call :check-file "%P_FILE2%" S2
call :check-dir  "%P_DIR1%"  S3

call :say-file "%P_FILE1%" !S1!
call :say-file "%P_FILE2%" !S2!
call :say-dir  "%P_DIR1%"  !S3!

set "OLD=0"
if exist "%APP%\%P_FILE2%" for %%A in ("%APP%\%P_FILE2%") do for %%B in ("%SCRIPTDIR%\%P_FILE2%") do if not "%%~zA"=="%%~zB" set "OLD=1"
if "%OLD%"=="1" (
    echo.
    echo NOTE:    a different %P_FILE2% is already installed ^(other size^).
    echo          It was left untouched - remove it first to switch versions.
)

set "TODO=0"
if not "!S1!"=="present" if not "!S1!"=="skip"   set /a TODO+=1
if not "!S2!"=="present" if not "!S2!"=="skip"   set /a TODO+=1
if not "!S3!"=="present" if not "!S3!"=="skip"   set /a TODO+=1

rem --- 3) report only ------------------------------------------------------
if /i "%MODE%"=="check" (
    if "!TODO!"=="0" (
        echo.
        echo Result: OK - the unlocker is installed.
        set "RC=0"
    ) else (
        echo.
        echo Result: TO DO - !TODO! item^(s^) to copy.
        set "RC=1"
    )
    goto :done
)

if "!TODO!"=="0" (
    echo.
    echo Result: nothing to do - the unlocker is already installed.
    goto :done
)

if /i "%MODE%"=="whatif" (
    if "!S1!"=="copy" echo [whatif] would copy %P_FILE1%
    if "!S2!"=="copy" echo [whatif] would copy %P_FILE2%
    if "!S3!"=="copy" echo [whatif] would copy folder %P_DIR1%
    echo.
    echo Result: nothing changed.
    goto :done
)

if "!S1!"=="copy" (
    copy /y "%SCRIPTDIR%\%P_FILE1%" "%APP%\%P_FILE1%" >nul
    if errorlevel 1 ( echo [ERROR] copy of %P_FILE1% failed. & set "RC=4" & goto :done )
    fc /b "%SCRIPTDIR%\%P_FILE1%" "%APP%\%P_FILE1%" >nul
    if errorlevel 1 ( echo [ERROR] %P_FILE1% does not match the source. & set "RC=4" & goto :done )
    echo Copied:  %P_FILE1%
)
if "!S2!"=="copy" (
    copy /y "%SCRIPTDIR%\%P_FILE2%" "%APP%\%P_FILE2%" >nul
    if errorlevel 1 ( echo [ERROR] copy of %P_FILE2% failed. & set "RC=4" & goto :done )
    fc /b "%SCRIPTDIR%\%P_FILE2%" "%APP%\%P_FILE2%" >nul
    if errorlevel 1 ( echo [ERROR] %P_FILE2% does not match the source. & set "RC=4" & goto :done )
    echo Copied:  %P_FILE2%
)
if "!S3!"=="copy" (
    xcopy /e /i /y /q "%SCRIPTDIR%\%P_DIR1%" "%APP%\%P_DIR1%" >nul
    if errorlevel 1 ( echo [ERROR] copy of %P_DIR1% failed. & set "RC=4" & goto :done )
    call :verify-dir "%SCRIPTDIR%\%P_DIR1%" "%APP%\%P_DIR1%"
    if errorlevel 1 ( echo [ERROR] %P_DIR1% does not match the source. & set "RC=4" & goto :done )
    echo Copied:  %P_DIR1%\
)

echo.
echo Result: done - the unlocker is installed.
goto :done

:check-file
set "OUTVAR=%~2"
if not exist "%SCRIPTDIR%\%~1" ( set "%OUTVAR%=skip" & goto :eof )
if exist "%APP%\%~1"          ( set "%OUTVAR%=present" & goto :eof )
set "%OUTVAR%=copy"
goto :eof

:check-dir
set "OUTVAR=%~2"
if not exist "%SCRIPTDIR%\%~1\" ( set "%OUTVAR%=skip" & goto :eof )
if exist "%APP%\%~1\"          ( set "%OUTVAR%=present" & goto :eof )
set "%OUTVAR%=copy"
goto :eof

:say-file
if "%~2"=="present" echo File:    %~1 - already installed, left untouched.
if "%~2"=="copy"    echo File:    %~1 - will be copied.
if "%~2"=="skip"    echo File:    %~1 - missing next to this script, skipped.
goto :eof

:say-dir
if "%~2"=="present" echo Folder:  %~1 - already installed, left untouched.
if "%~2"=="copy"    echo Folder:  %~1 - will be copied.
if "%~2"=="skip"    echo Folder:  %~1 - missing next to this script, skipped.
goto :eof

:verify-dir
rem Counts and byte sums must match, then every file is compared byte by byte.
set "VS=%~1"
set "VD=%~2"
set /a SC=0
set /a SB=0
set /a DC=0
set /a DB=0
for /f "delims=" %%F in ('dir /b /s /a-d "%VS%" 2^>nul') do (
    set /a SC+=1
    set /a SB+=%%~zF
)
for /f "delims=" %%F in ('dir /b /s /a-d "%VD%" 2^>nul') do (
    set /a DC+=1
    set /a DB+=%%~zF
)
if not "%SC%"=="%DC%" exit /b 1
if not "%SB%"=="%DB%" exit /b 1
for /f "delims=" %%F in ('dir /b /s /a-d "%VS%" 2^>nul') do (
    set "REL=%%F"
    set "REL=!REL:%VS%=!"
    fc /b "%%F" "%VD%!REL!" >nul
    if errorlevel 1 exit /b 1
)
exit /b 0

:bad-approot
echo [ERROR] -AppRoot does not point at an installation: %APPROOT%
echo         Visual Enhancer.exe was not found there.
set "RC=2"
goto :done

:no-app
echo [ERROR] No Visual Enhancer installation found.
echo         Checked: -AppRoot, ..\.. above this script, %%APPPATH%%\Visual Enhancer.
set "RC=2"

:done
if not "%RC%"=="0" if not "%RC%"=="1" (
    echo.
    echo [ERROR] Exit code %RC% - see the messages above.
)
if /i not "%NOPAUSE%"=="1" pause
endlocal & exit /b %RC%
