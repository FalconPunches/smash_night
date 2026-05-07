@echo off
REM Launches Smash Night as administrator (required for RCM USB injection
REM when UAC is disabled / set to "Never notify").
REM Auto-runs setup if .venv is missing so the elevated launch can
REM rely on a working environment.

cd /d "%~dp0"

if not exist ".venv\Scripts\pythonw.exe" (
    call setup.bat
    if errorlevel 1 exit /b 1
)

powershell -NoProfile -Command "Start-Process -FilePath '%~dp0.venv\Scripts\pythonw.exe' -ArgumentList '%~dp0smash_night.py' -WorkingDirectory '%~dp0' -Verb RunAs"
