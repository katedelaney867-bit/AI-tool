@echo off
cd /d "%~dp0"
title AI Xiaogongju - Diagnose
rem Collect diagnostic info into a report the customer can send back.
rem Keep this file pure ASCII: cmd decodes .bat with the console code page,
rem so Chinese text here would be garbled on some machines.
if not exist "runtime\python.exe" (
  echo [ERROR] runtime\python.exe not found.
  echo Please unzip the WHOLE folder, do not move files out of it.
  pause
  exit /b 1
)
if not exist "doctor.py" (
  echo [ERROR] doctor.py not found next to this file.
  pause
  exit /b 1
)
"runtime\python.exe" "doctor.py"
echo.
echo The report file was created in this folder.
pause >nul
