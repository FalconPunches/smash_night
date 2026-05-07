@echo off
REM ──────────────────────────────────────────────────────────────────
REM  Smash Night — first-time setup
REM  Creates a local .venv and installs all Python dependencies.
REM  Safe to re-run; pip install is idempotent.
REM ──────────────────────────────────────────────────────────────────

setlocal
cd /d "%~dp0"

echo.
echo === Smash Night setup ===
echo.

REM ── 1. Find a usable Python ──
set PYTHON=
for %%P in (py python python3) do (
    where %%P >nul 2>&1 && (set PYTHON=%%P & goto :py_found)
)

echo ERROR: No Python found on PATH.
echo.
echo Install Python 3.11+ from https://www.python.org/downloads/
echo  - Tick "Add python.exe to PATH" during install
echo  - Re-run this script after.
echo.
pause
exit /b 1

:py_found
echo Using Python: %PYTHON%
%PYTHON% --version

REM ── 2. Create venv if missing ──
if not exist ".venv\Scripts\python.exe" (
    echo.
    echo Creating .venv...
    %PYTHON% -m venv .venv
    if errorlevel 1 (
        echo ERROR: venv creation failed.
        pause
        exit /b 1
    )
)

REM ── 3. Install / upgrade dependencies ──
echo.
echo Installing dependencies...
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo ERROR: pip install failed. See messages above.
    echo Re-run setup.bat after fixing the issue.
    pause
    exit /b 1
)

REM ── 4. Quick sanity check on the Rust binary ──
if not exist "ssbh_render\target\release\ssbh_render.exe" (
    echo.
    echo NOTE: ssbh_render.exe not found.
    echo The app will fall back to its pyrender approximation
    echo  ^(colors will look duller^). To get ssbh-editor-quality
    echo  previews, build the Rust binary once:
    echo.
    echo    cd ssbh_render
    echo    cargo build --release
    echo.
    echo  Requires: ^>winget install Rustlang.Rustup^<  ^(open new
    echo  terminal after install so PATH picks up cargo^).
)

echo.
echo === Setup complete ===
echo.
echo  Run the app with:    Run.bat
echo  Run as admin:        Run_As_Admin.bat
echo.
endlocal
