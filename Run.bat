@echo off
REM Launches Smash Night. Auto-runs setup on first launch so a fresh
REM checkout / portable share works double-click.

cd /d "%~dp0"

if not exist ".venv\Scripts\pythonw.exe" (
    call setup.bat
    if errorlevel 1 exit /b 1
)

start "" ".venv\Scripts\pythonw.exe" "smash_night.py"
