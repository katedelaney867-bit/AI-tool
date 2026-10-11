@echo off
rem ===========================================================
rem   AI Xiaogongju  --  one-click launcher
rem
rem   Must keep this file ASCII-only: cmd.exe parses .bat using
rem   the current console code page, so Chinese text inside would
rem   turn into garbage on some machines. All Chinese messages are
rem   printed by Python / PowerShell instead.
rem ===========================================================
cd /d "%~dp0"
title AI Xiaogongju

rem Disable the per-user site-packages directory.
rem The embedded Python enables "import site", which also pulls in
rem %APPDATA%\Python\Python3xx\site-packages. If the customer ever ran
rem "pip install --user", pip would consider those packages "already
rem satisfied" and SKIP installing ours -- leaving the app depending on
rem the customer's copy (and breaking the moment they remove it).
rem Measured on the build machine: paramiko/scipy/tqdm leaked in from
rem there, caused bogus "pip check" conflicts, and even made one pip
rem install fail so a whole mirror had to be retried.
rem With this set, the runtime only sees runtime\Lib\site-packages.
set PYTHONNOUSERSITE=1

if not exist "prepare.ps1" (
  echo [ERROR] prepare.ps1 not found.
  echo Please unzip the WHOLE folder, do not move files out of it.
  pause
  exit /b 1
)

rem --- Step 1: make sure the runtime + dependencies are ready -------------
rem Use the absolute path instead of relying on PATH: if the customer's PATH
rem is unusual or broken, "powershell" alone would fail to resolve.
set "PWSH=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
if not exist "%PWSH%" set "PWSH=powershell"

"%PWSH%" -NoProfile -ExecutionPolicy Bypass -File "%~dp0prepare.ps1"
if errorlevel 1 (
  echo.
  echo [ERROR] Environment setup failed. Read the messages above.
  echo.
  pause
  exit /b 1
)

rem --- Step 2: run ---------------------------------------------------------
if not exist "runtime\python.exe" (
  echo [ERROR] runtime\python.exe is missing after setup.
  pause
  exit /b 1
)

"runtime\python.exe" "app.py"

echo.
echo Program exited. Press any key to close this window.
pause >nul
