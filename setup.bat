@echo off
REM ------------------------------------------------------------------
REM  Smash Night - first-time setup
REM  Fully automatic on a fresh PC:
REM   - finds (or winget-installs) Python 3.12
REM   - creates the local .venv (recreates it if it uses a Python that
REM     ssbh_data_py has no wheels for, i.e. 3.13+)
REM   - installs all Python dependencies (pyrender needs a special
REM     --no-deps install - see requirements.txt for why)
REM   - installs WinRAR if no RAR extractor is present (.rar mods)
REM  Safe to re-run; every step is idempotent.
REM ------------------------------------------------------------------

setlocal
cd /d "%~dp0"

echo.
echo === Smash Night setup ===
echo.

REM -- 1. Find a Python that ssbh_data_py ships wheels for (3.10-3.12) --
set "PYTHON="
for %%V in (3.12 3.11 3.10) do (
    if not defined PYTHON (
        py -%%V -c "pass" >nul 2>&1 && set "PYTHON=py -%%V"
    )
)

if not defined PYTHON (
    echo No Python 3.10-3.12 found. Installing Python 3.12 via winget...
    winget install --id Python.Python.3.12 --silent --accept-package-agreements --accept-source-agreements
    REM The py launcher picks the new install up immediately; PATH for
    REM python.exe itself only refreshes in NEW consoles, so prefer py.
    py -3.12 -c "pass" >nul 2>&1 && set "PYTHON=py -3.12"
)
if not defined PYTHON (
    REM Last resort: the default per-user install location (covers the
    REM case where the py launcher itself was not installed/refreshed).
    if exist "%LocalAppData%\Programs\Python\Python312\python.exe" (
        set "PYTHON="%LocalAppData%\Programs\Python\Python312\python.exe""
    )
)
if not defined PYTHON (
    echo ERROR: Could not find or install Python 3.12.
    echo.
    echo Install it manually from https://www.python.org/downloads/
    echo  ^(any 3.10 - 3.12; tick "Add python.exe to PATH"^)
    echo then re-run this script.
    echo.
    pause
    exit /b 1
)

echo Using Python: %PYTHON%
%PYTHON% --version

REM -- 2. Recreate .venv if it runs an unsupported Python (e.g. 3.13+) --
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -c "import sys; sys.exit(0 if (3,10) <= sys.version_info[:2] <= (3,12) else 1)" >nul 2>&1
    if errorlevel 1 (
        echo Existing .venv uses a Python without ssbh_data_py wheels - recreating...
        rmdir /s /q .venv
    )
)

REM -- 3. Create venv if missing --
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

REM -- 4. Install / upgrade dependencies --
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
REM pyrender hard-pins PyOpenGL==3.1.0, which conflicts with the >=3.1.7
REM override in requirements.txt (modern pip refuses to resolve it). Install
REM it without deps - its real dependencies come from requirements.txt.
".venv\Scripts\python.exe" -m pip install --no-deps "pyrender>=0.1.45"
if errorlevel 1 (
    echo.
    echo ERROR: pyrender install failed. See messages above.
    pause
    exit /b 1
)

REM -- 5. RAR extractor (GameBanana mods are often .rar) --
set "UNRAR="
if exist "%ProgramFiles%\WinRAR\UnRAR.exe" set "UNRAR=1"
if exist "%ProgramFiles(x86)%\WinRAR\UnRAR.exe" set "UNRAR=1"
where unrar >nul 2>&1 && set "UNRAR=1"
if not defined UNRAR (
    echo.
    echo Installing WinRAR for .rar mod support...
    winget install --id RARLab.WinRAR --silent --accept-package-agreements --accept-source-agreements
    if errorlevel 1 (
        echo WARNING: WinRAR auto-install failed. .rar mods will not extract
        echo until you install it: winget install RARLab.WinRAR
    )
)

REM -- 6. Quick sanity check on the Rust binary --
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
