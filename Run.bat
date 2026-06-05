@echo off
REM Launches Smash Night. Auto-runs setup on first launch so a fresh
REM checkout / portable share works double-click.

cd /d "%~dp0"

REM Run setup when the venv is missing OR broken/unsupported (e.g. a stale
REM .venv copied from another PC, or one built on Python 3.13+ which has
REM no ssbh_data_py wheels - the 3D slot picker degrades without them).
set "NEED_SETUP="
if not exist ".venv\Scripts\pythonw.exe" set "NEED_SETUP=1"
if not defined NEED_SETUP (
    ".venv\Scripts\python.exe" -c "import sys; sys.exit(0 if (3,10) <= sys.version_info[:2] <= (3,12) else 1)" >nul 2>&1
    if errorlevel 1 set "NEED_SETUP=1"
)
if defined NEED_SETUP (
    call setup.bat
    if errorlevel 1 exit /b 1
)

start "" ".venv\Scripts\pythonw.exe" "smash_night.py"
