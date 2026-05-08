"""
Smash Night — Smash Ultimate Mod Manager
==========================================

Browse, search, preview and 1-click install SSBU skins from GameBanana
directly to your SD card — no web browser needed.

Uses the GameBanana API v11:
  - Search / browse mods by fighter or keyword
  - Preview thumbnail images
  - Download, extract, and install to SD card in one click

Requires: requests, Pillow (for thumbnails)
"""

# ── Dependency bootstrap ──────────────────────────────────────────────────────
# Check for missing pip packages and auto-install them from requirements.txt
# before anything else runs. On success the script re-execs itself so all
# imports see the freshly-installed packages.
def _bootstrap_deps():
    import importlib.util, subprocess, os, sys
    _REQUIRED = {          # import-name : pip package name
        "requests":     "requests",
        "PIL":          "Pillow",
        "py7zr":        "py7zr",
        "rarfile":      "rarfile",
        "numpy":        "numpy",
        "ssbh_data_py": "ssbh_data_py",
        "trimesh":      "trimesh",
        "pyrender":     "pyrender",
    }
    missing = [pip for imp, pip in _REQUIRED.items()
               if importlib.util.find_spec(imp) is None]
    # PyOpenGL 3.1.0 (pinned by pyrender) has a ctypes bug on Python 3.11+
    # that breaks OffscreenRenderer. Upgrade silently if needed.
    # Deliberately avoids importing `packaging` (may not be present).
    try:
        import OpenGL
        parts = tuple(int(x) for x in OpenGL.__version__.split(".")[:3])
        if parts < (3, 1, 7):
            missing.append("PyOpenGL>=3.1.7")
    except Exception:
        missing.append("PyOpenGL>=3.1.7")
    if missing:
        req = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "requirements.txt")
        if os.path.isfile(req):
            print(f"[Smash Night] Installing missing packages: {', '.join(missing)}")
            subprocess.check_call([sys.executable, "-m", "pip", "install",
                                    "-r", req, "--quiet"])
        else:
            print(f"[Smash Night] Installing missing packages: {', '.join(missing)}")
            subprocess.check_call([sys.executable, "-m", "pip", "install",
                                    "--quiet"] + missing)
        # Re-exec so the new packages are importable in this process
        os.execv(sys.executable, [sys.executable] + sys.argv)


def _bootstrap_ssbh_render():
    """Build ssbh_render.exe in the background if it's missing and cargo exists.

    - If already built: do nothing (fast path).
    - If cargo is on PATH: kick off `cargo build --release` on a daemon thread
      so the UI still opens immediately. First build takes ~5-10 min.
    - If cargo is missing: print a one-time hint pointing to winget.
      Does NOT block startup or show a dialog — the app falls back to pyrender.
    """
    import os, sys, shutil, subprocess, threading

    exe = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "ssbh_render", "target", "release", "ssbh_render.exe")
    if os.path.isfile(exe):
        return  # already built, nothing to do

    src = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ssbh_render")
    if not os.path.isdir(src):
        return  # source not present — skip silently

    cargo = shutil.which("cargo")
    if not cargo:
        # Check common Rust install location in case PATH isn't refreshed yet
        default = os.path.expanduser(r"~\.cargo\bin\cargo.exe")
        if os.path.isfile(default):
            cargo = default

    if not cargo:
        print(
            "[Smash Night] ssbh_render not built — 3D previews will use pyrender "
            "(colors may be off).\n"
            "  For accurate renders, install Rust once:\n"
            "    winget install Rustlang.Rustup\n"
            "  Then reopen this app and it will build automatically.",
            file=sys.stderr)
        return

    def _build():
        print("[Smash Night] Building ssbh_render in background "
              "(first run ~5-10 min)…")
        try:
            result = subprocess.run(
                [cargo, "build", "--release"],
                cwd=src,
                capture_output=True, text=True)
            if result.returncode == 0:
                print("[Smash Night] ssbh_render built successfully — "
                      "accurate 3D previews now enabled.")
            else:
                print(f"[Smash Night] ssbh_render build failed:\n"
                      f"{result.stderr[-800:]}", file=sys.stderr)
        except Exception as e:
            print(f"[Smash Night] ssbh_render build error: {e}", file=sys.stderr)

    threading.Thread(target=_build, daemon=True).start()


_bootstrap_deps()
_bootstrap_ssbh_render()
# ─────────────────────────────────────────────────────────────────────────────

import io
import os
import re
import sys
import copy
import json
import time
import shutil
import zipfile
import tempfile
import threading
import traceback
from datetime import datetime
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox, simpledialog
from pathlib import Path
from urllib.parse import quote

try:
    import requests
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

try:
    from PIL import Image, ImageTk
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

try:
    import py7zr
    HAS_PY7ZR = True
except ImportError:
    HAS_PY7ZR = False

try:
    import rarfile
    HAS_RARFILE = True
except ImportError:
    HAS_RARFILE = False

try:
    import numpy as np
    import ssbh_data_py
    import trimesh
    import pyrender
    HAS_3D_RENDER = True
    # numpy 2.0 removed np.infty; pyrender.Viewer still references it
    if not hasattr(np, "infty"):
        np.infty = np.inf
except ImportError:
    HAS_3D_RENDER = False

# ─────────────────────────────────────────────────────────
#  CONSTANTS
# ─────────────────────────────────────────────────────────

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SKINS_DIR = os.path.join(SCRIPT_DIR, "skins")

# ── SSBH Editor (3D model viewer) ──
SSBH_EDITOR_DIR = os.path.join(SCRIPT_DIR, "ssbh_editor")
SSBH_EDITOR_EXE = os.path.join(SSBH_EDITOR_DIR, "ssbh_editor.exe")
SSBH_EDITOR_RELEASE_URL = (
    "https://github.com/ScanMountGoat/ssbh_editor/releases/latest")
# Mod archive / extracted-tree cache. Kept under SCRIPT_DIR (in
# .gitignore) rather than %LOCALAPPDATA% because the Microsoft Store
# Python redirects AppData\Local through per-app virtualization —
# our cached files would be visible to Python but invisible to the
# Win32 ``ssbh_render.exe`` binary, breaking the Rust renderer and
# making every preview fall back to the pyrender approximation.
# Migrates any virtualized cache back to the in-repo location on
# first run so the user doesn't lose previously-downloaded mods.
def _resolve_mod_cache_dir():
    target = os.path.join(SCRIPT_DIR, ".mod_cache")
    os.makedirs(target, exist_ok=True)
    # One-time migration from the previous %LOCALAPPDATA% location
    # AND from MS-Store Python's virtualised path. Both are searched
    # because users who never had the virtualised redirect should
    # still pick up any cache they accumulated during the AppData
    # experiment.
    candidates = []
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    if base:
        candidates.append(os.path.join(base, "smash_night", "mod_cache"))
    pkg_root = os.path.expanduser(r"~\AppData\Local\Packages")
    if os.path.isdir(pkg_root):
        try:
            for d in os.listdir(pkg_root):
                cand = os.path.join(pkg_root, d, "LocalCache",
                                     "Local", "smash_night",
                                     "mod_cache")
                if os.path.isdir(cand):
                    candidates.append(cand)
        except OSError:
            pass
    for src in candidates:
        if (os.path.isdir(src) and src != target
                and not os.listdir(target)):
            try:
                # Move sub-entries one at a time so we can co-exist
                # with whatever's already in target.
                for entry in os.listdir(src):
                    s = os.path.join(src, entry)
                    t = os.path.join(target, entry)
                    if not os.path.exists(t):
                        try:
                            os.rename(s, t)
                        except OSError:
                            import shutil
                            shutil.move(s, t)
                print(f"  [cache] Migrated mod cache: {src} -> {target}")
            except Exception as e:
                print(f"  [cache] Migration from {src} failed: {e}",
                      file=sys.stderr)
    return target

MOD_CACHE_DIR = _resolve_mod_cache_dir()
RENDER_CACHE_DIR = os.path.join(SCRIPT_DIR, ".render_cache")

# ── ultimate_tex_cli (Switch nutexb → PNG decoder) ──
# Used by the in-app 3D preview to render mods with their actual textures.
ULTIMATE_TEX_DIR = os.path.join(SCRIPT_DIR, "ultimate_tex_cli")
ULTIMATE_TEX_EXE = os.path.join(ULTIMATE_TEX_DIR, "ultimate_tex_cli.exe")
NUTEXB_PNG_CACHE_DIR = os.path.join(RENDER_CACHE_DIR, "nutexb_png")

# ── ssbh_render (Rust CLI using ssbh_wgpu — same shader as ssbh_editor) ──
# When present, this is preferred over our pyrender pipeline because it
# produces ssbh_editor-quality renders. Built from ./ssbh_render via cargo.
SSBH_RENDER_DIR = os.path.join(SCRIPT_DIR, "ssbh_render")
SSBH_RENDER_EXE = os.path.join(SSBH_RENDER_DIR, "target", "release",
                                "ssbh_render.exe")

# ── SD card drive detection ──
# Auto-detects all removable / non-system drives. The Switch SD card mounts
# wherever Windows assigns it — order here is just the preferred fallback.
SD_CANDIDATES = ["E:\\", "F:\\", "G:\\", "H:\\", "D:\\", "I:\\", "J:\\"]

def _is_removable_drive(drive: str) -> bool:
    """True if *drive* is a removable/USB drive (not a fixed HDD/SSD)."""
    try:
        import ctypes
        # GetDriveType: 2=removable, 3=fixed, 4=network, 5=cdrom
        dtype = ctypes.windll.kernel32.GetDriveTypeW(ctypes.c_wchar_p(drive))
        return dtype == 2
    except Exception:
        return False

def _looks_like_switch_sd(drive: str) -> bool:
    """Heuristic: drive contains at least one Switch CFW marker dir/file.

    Built-in SD card readers on some laptops report their card as
    GetDriveType==3 (fixed) instead of 2 (removable). When that happens
    we still want to find the card, so we treat any non-system drive
    that has Switch hallmarks as a candidate.
    """
    if not drive or drive.upper().startswith("C:"):
        return False
    try:
        if not os.path.isdir(drive):
            return False
    except Exception:
        return False
    markers = ("atmosphere", "bootloader", "switch", "Nintendo",
               "hbmenu.nro", "boot.dat", "payload.bin")
    for m in markers:
        try:
            if os.path.exists(os.path.join(drive, m)):
                return True
        except Exception:
            pass
    return False

def _present_sd_drives():
    """Return every drive that could plausibly be the Switch SD card.

    Includes:
      • Removable drives (USB sticks, USB SD readers)
      • Any non-C drive that already has Atmosphere/bootloader/switch
        on it (covers internal SD readers that report as 'fixed').
    """
    drives = []
    for letter in "DEFGHIJKLMNOPQRSTUVWXYZ":
        p = f"{letter}:\\"
        if not os.path.exists(p):
            continue
        if _is_removable_drive(p) or _looks_like_switch_sd(p):
            drives.append(p)
    return drives

def _detect_sd_drive():
    """Return the first removable drive, preferring the order in SD_CANDIDATES."""
    present = _present_sd_drives()
    for c in SD_CANDIDATES:
        if c in present:
            return c
    return present[0] if present else SD_CANDIDATES[0]

# Initialise with the detected drive (or first candidate as fallback).
SD_CARD = _detect_sd_drive()
ARCROPOLIS_MODS = os.path.join(SD_CARD, "ultimate", "mods")

SMASH_TITLE_ID = "01006A800016E000"
ATMOSPHERE_CONTENTS = os.path.join(SD_CARD, "atmosphere", "contents", SMASH_TITLE_ID)
PLUGINS_DIR = os.path.join(ATMOSPHERE_CONTENTS, "romfs", "skyline", "plugins")
EXEFS_DIR = os.path.join(ATMOSPHERE_CONTENTS, "exefs")
ROMFS_DIR = os.path.join(ATMOSPHERE_CONTENTS, "romfs")

# ── RCM Payload Injection ──
# TegraRcmSmash.exe — the CLI tool that does the actual fusée gelée exploit.
# We search several likely locations; user can also browse manually.
RCM_SMASH_SEARCH_PATHS = [
    os.path.join(SCRIPT_DIR, "rcm_tools", "TegraRcmGUI_v2.6_portable", "TegraRcmSmash.exe"),
    os.path.join(SCRIPT_DIR, "rcm_tools", "TegraRcmSmash.exe"),
    os.path.join(SCRIPT_DIR, "TegraRcmSmash.exe"),
]
# Payload .bin — prefer the bundled hekate_latest.bin (always supports
# whatever firmware Atmosphere supports). fusee.bin is kept as a
# fallback for environments that pre-stage the SD with a fusee build,
# and the SD's reboot_payload.bin is the last resort.
PAYLOAD_SEARCH_PATHS = [
    os.path.join(SCRIPT_DIR, "payloads", "hekate_latest.bin"),
    os.path.join(SD_CARD, "bootloader", "payloads", "hekate_latest.bin"),
    os.path.join(SD_CARD, "bootloader", "payloads", "fusee.bin"),
    os.path.join(SCRIPT_DIR, "payloads", "fusee.bin"),
    os.path.join(SD_CARD, "atmosphere", "reboot_payload.bin"),
]

def _apply_sd_drive(drive):
    """Update all SD-card-derived module globals when the active drive changes."""
    global SD_CARD, ARCROPOLIS_MODS, ATMOSPHERE_CONTENTS, PLUGINS_DIR, EXEFS_DIR, ROMFS_DIR
    SD_CARD = drive
    ARCROPOLIS_MODS = os.path.join(drive, "ultimate", "mods")
    ATMOSPHERE_CONTENTS = os.path.join(drive, "atmosphere", "contents", SMASH_TITLE_ID)
    PLUGINS_DIR = os.path.join(ATMOSPHERE_CONTENTS, "romfs", "skyline", "plugins")
    EXEFS_DIR = os.path.join(ATMOSPHERE_CONTENTS, "exefs")
    ROMFS_DIR = os.path.join(ATMOSPHERE_CONTENTS, "romfs")
    PAYLOAD_SEARCH_PATHS[:] = [
        os.path.join(SCRIPT_DIR, "payloads", "hekate_latest.bin"),
        os.path.join(drive, "bootloader", "payloads", "hekate_latest.bin"),
        os.path.join(drive, "bootloader", "payloads", "fusee.bin"),
        os.path.join(SCRIPT_DIR, "payloads", "fusee.bin"),
        os.path.join(drive, "atmosphere", "reboot_payload.bin"),
    ]

# Local copies of plugin .nro files (bundled with this repo)
LOCAL_PLUGINS = {
    "libarcropolis.nro": os.path.join(SCRIPT_DIR, "switch_setup", "mods", "arcropolis",
                                       "extracted", "atmosphere", "contents",
                                       SMASH_TITLE_ID, "romfs", "skyline", "plugins",
                                       "libarcropolis.nro"),
    "liblatency_slider_de.nro": os.path.join(SCRIPT_DIR, "smash_mods",
                                              "liblatency_slider_de.nro"),
    "libless_delay.nro": os.path.join(SCRIPT_DIR, "smash_mods", "libless_delay.nro"),
}

# Known plugins metadata
KNOWN_PLUGINS = {
    "libarcropolis.nro": {"name": "ARCropolis", "wifi_safe": True,
        "desc": "File replacement engine — loads skin mods"},
    "liblatency_slider_de.nro": {"name": "Latency Slider DE", "wifi_safe": True,
        "desc": "Adjusts local input buffer for less lag"},
    "libless_delay.nro": {"name": "Less Delay", "wifi_safe": True,
        "desc": "Client-side vsync mod, reduces display latency"},
}

# ── Provisioning Profiles ──
# Core components (shared by ALL profiles): SD card, Atmosphere, Hekate,
# sys-patch, Skyline, ARCropolis, mods folder, cache.
# Each profile adds its own plugin set on top of core.
PROVISIONING_PROFILES = {
    "Competitive": {
        "desc": "Tournament-ready: low-latency plugins, wifi safe, no gameplay changes",
        "plugins": ["liblatency_slider_de.nro", "libless_delay.nro"],
        "update_keys": ["latency_slider", "less_delay"],
    },
    "Skins Only": {
        "desc": "Just ARCropolis for loading skin mods — no extra plugins",
        "plugins": [],
        "update_keys": [],
    },
    "Custom": {
        "desc": "Anything goes — gameplay mods, parameter edits, modpacks",
        "plugins": [],
        "update_keys": [],
    },
    # Future profiles:
    # "Gameplay Mods": {
    #     "desc": "Gameplay-changing mods — training packs, moveset edits, etc.",
    #     "plugins": ["libarcropolis.nro", ...],
    #     "update_keys": [...],
    # },
}

CORE_PLUGINS = ["libarcropolis.nro"]  # always installed regardless of profile
CORE_UPDATE_KEYS = ["atmosphere", "fusee", "hekate", "sys_patch", "skyline", "arcropolis"]

# ── Unofficial / Pre-release Atmosphere Support ──
# When official Atmosphere hasn't released support for a new firmware,
# the community builds from the in-progress support branch.
# This setting makes provisioning prefer pre-releases and local overrides
# over the latest stable release.
ATMOSPHERE_SUPPORT_BRANCH = "22_support"  # active support branch for new FW
LOCAL_ATMOSPHERE_DIR = os.path.join(SCRIPT_DIR, "switch_setup", "downloads")

# Fork repo that publishes unofficial builds.
# zandercodes builds the 22_support branch and publishes releases with
# atmosphere zip + fusee.bin.  Change this if a different fork becomes active.
UNOFFICIAL_ATMOSPHERE_FORK = "zandercodes/Atmosphere-unofficial"

# GitHub repos for downloading latest releases
GITHUB_REPOS = {
    "arcropolis": ("Raytwo/ARCropolis", lambda n: n == "release.zip"),
    "skyline": ("skyline-dev/skyline", lambda n: n == "skyline.zip"),
    "latency_slider": ("Naxdy/latency-slider-de",
                        lambda n: n == "liblatency_slider_de.nro"),
    "less_delay": ("Naxdy/less-delay", lambda n: n == "libless_delay.nro"),
    "atmosphere": ("Atmosphere-NX/Atmosphere",
                    lambda n: n.startswith("atmosphere-") and n.endswith(".zip")
                              and "WITHOUT" not in n.upper()),
    "fusee": ("Atmosphere-NX/Atmosphere", lambda n: n == "fusee.bin"),
    "hekate": ("CTCaer/hekate",
               lambda n: n.startswith("hekate_ctcaer_") and n.endswith(".zip")),
    "sys_patch": ("impeeza/sys-patch",
                  lambda n: n.startswith("sys-patch") and n.endswith(".zip")),
}

# Map nro filename -> GITHUB_REPOS key for quick lookup
_NRO_TO_REPO = {
    "libarcropolis.nro": "arcropolis",
    "liblatency_slider_de.nro": "latency_slider",
    "libless_delay.nro": "less_delay",
}

SSBU_GAME_ID = 6498
SKINS_ROOT_CAT = 3330  # "Skins" root category for SSBU
STAGES_ROOT_CAT = 6089  # "Stages" root category for SSBU
OTHER_ROOT_CAT = None    # No single root — queries game-wide, post-filters

API_BASE = "https://gamebanana.com/apiv11"

# GameBanana subcategory IDs for each fighter under Skins (cat 3330)
# Verified against GameBanana API v11 ModCategory endpoint
FIGHTER_CATEGORIES = {
    "All Skins": SKINS_ROOT_CAT,
    "Assist Trophies/Pokemon": 7601,
    "Banjo & Kazooie": 7607,
    "Bayonetta": 7586,
    "Bosses": 7602,
    "Bowser": 7527,
    "Bowser Jr.": 7530,
    "Byleth": 7609,
    "Captain Falcon": 7558,
    "Charizard": 7555,
    "Chrom": 7595,
    "Cloud": 7585,
    "Corrin": 7567,
    "Daisy": 7590,
    "Dark Pit": 7571,
    "Dark Samus": 7593,
    "Diddy Kong": 7533,
    "Donkey Kong": 7532,
    "Dr. Mario": 7528,
    "Duck Hunt": 7579,
    "Falco": 7546,
    "Fox": 7545,
    "Ganondorf": 7537,
    "Greninja": 7557,
    "Hero": 7606,
    "Ice Climbers": 7561,
    "Ike": 7564,
    "Incineroar": 7594,
    "Inkling": 7597,
    "Isabelle": 7596,
    "Items": 7603,
    "Ivysaur": 7554,
    "Jigglypuff": 7549,
    "Joker": 7605,
    "Ken": 7598,
    "King Dedede": 7544,
    "King K. Rool": 7591,
    "Kirby": 7542,
    "Link": 7534,
    "Little Mac": 7576,
    "Lucario": 7556,
    "Lucas": 7560,
    "Lucina": 7566,
    "Luigi": 7525,
    "Mario": 7524,
    "Marth": 7562,
    "Mega Man": 7582,
    "Meta Knight": 7543,
    "Mewtwo": 7550,
    "Mii Brawler": 7587,
    "Mii Gunner": 7589,
    "Mii Hats": 7613,
    "Mii Swordfighter": 7588,
    "Min Min": 7610,
    "Mr. Game & Watch": 7568,
    "Ness": 7559,
    "Olimar": 7573,
    "Other/Misc": 7523,
    "Pac-Man": 7583,
    "Packs": 7611,
    "Palutena": 7570,
    "Peach": 7526,
    "Pichu": 7551,
    "Pikachu": 7548,
    "Piranha Plant": 7604,
    "Pit": 7569,
    "Pokemon Trainer": 7552,
    "Pyra/Mythra": 7615,
    "R.O.B.": 7574,
    "Richter": 7600,
    "Ridley": 7592,
    "Robin": 7565,
    "Rosalina & Luma": 7529,
    "Roy": 7563,
    "Ryu": 7584,
    "Samus": 7540,
    "Sephiroth": 7614,
    "Sheik": 7536,
    "Shulk": 7578,
    "Simon": 7599,
    "Snake": 7580,
    "Sonic": 7581,
    "Squirtle": 7553,
    "Steve": 7612,
    "Terry": 7608,
    "Toon Link": 7539,
    "Villager": 7575,
    "Wario": 7572,
    "Wii Fit Trainer": 7577,
    "Wolf": 7547,
    "Yoshi": 7531,
    "Young Link": 7538,
    "Zelda": 7535,
    "Zero Suit Samus": 7541,
}

# GameBanana subcategory IDs for stages (cat 6089)
# Verified via GameBanana old API (Core/Item/Data name field)
STAGE_CATEGORIES = {
    "All Stages": STAGES_ROOT_CAT,
    "3D Land": 15485,
    "75 m": 6139,
    "Arena Ferox": 13957,
    "Backgrounds Only": 15955,
    "Balloon Fight": 6141,
    "Battlefield": 6096,
    "Big Blue": 15954,
    "Boxing Ring": 6112,
    "Bridge of Eldin": 6121,
    "Brinstar": 16270,
    "Brinstar Depths": 16271,
    "Castle Siege": 6092,
    "Cloud Sea of Alrest": 16272,
    "Corneria": 16273,
    "Distant Planet": 16274,
    "Dracula's Castle": 6124,
    "Dream Land": 6104,
    "Duck Hunt": 16275,
    "Final Destination": 6095,
    "Find Mii": 16276,
    "Flat Zone X": 16277,
    "Fountain of Dreams": 6138,
    "Fourside": 6113,
    "Garden of Hope": 16278,
    "Garreg Mach Monastery": 16279,
    "Gerudo Valley": 16280,
    "Green Hill Zone": 6115,
    "Halberd": 6130,
    "Hanenbow": 6126,
    "Kalos Pokémon League": 6106,
    "King of Fighters Stadium": 6143,
    "Kongo Jungle": 6129,
    "Living Room": 16282,
    "Lylat Cruise": 6110,
    "Magicant": 6094,
    "Mario Bros.": 6144,
    "Mario Galaxy": 16283,
    "Mementos": 6101,
    "Midgar": 6128,
    "Mishima Dojo": 15486,
    "Mushroom Kingdom": 6102,
    "Mushroom Kingdom II": 6100,
    "Mushroom Kingdom U": 16284,
    "Mute City SNES": 6103,
    "New Pork City": 16285,
    "Norfair": 6109,
    "Online Practice Stage": 6132,
    "Other/Misc": 6090,
    "Pac-Land": 6093,
    "Palutena's Temple": 6142,
    "PictoChat 2": 6120,
    "Pilotwings": 15487,
    "Pirate Ship": 6114,
    "Pokémon Stadium": 6133,
    "Pokémon Stadium 2": 6098,
    "Port Town Aero Dive": 6122,
    "Princess Peach's Castle": 6131,
    "Prism Tower": 6135,
    "Results Screen": 6099,
    "Saffron City": 6123,
    "Shadow Moses Island": 16286,
    "Skyloft": 16287,
    "Smashville": 6107,
    "Spear Pillar": 6137,
    "Spirit Train": 6127,
    "Spring Stadium": 6134,
    "Summit": 16288,
    "Super Happy Tree": 16290,
    "Super Mario Maker": 16289,
    "Suzaku Castle": 6105,
    "Temple": 16291,
    "The Great Cave Offensive": 6125,
    "Tomodachi Life": 16292,
    "Tortimer Island": 16293,
    "Town and City": 6117,
    "Training": 6097,
    "Unova Pokémon League": 6136,
    "WarioWare, Inc.": 6091,
    "Wii Fit Studio": 16294,
    "Wily Castle": 6108,
    "Windy Hill Zone": 6116,
    "Wuhu Island": 16295,
    "Yggdrasil's Altar": 16296,
    "Yoshi's Island": 6111,
    "Yoshi's Island (Melee)": 6118,
    "Yoshi's Story": 6119,
}

# "Other" root categories — everything that isn't Skins or Stages
OTHER_CATEGORIES = {
    "All Other": 0,          # sentinel: merged query across all below
    "Effects": 1177,
    "Gameplay": 26521,
    "Music Packs": 15929,
    "UI": 1760,
}

# The actual root category IDs that make up "Other"
_OTHER_CAT_IDS = [cid for cid in OTHER_CATEGORIES.values() if cid]

# ── Gameplay-pack root + sub-categories ──
# These are full-game / mechanics overhauls (HDR, Hewdraw Remix, Turbo Mode,
# AI changes, balance patches, parameter tweaks).  They live under the
# "Gameplay" root (26521) on GameBanana.  We pull them out into their own
# top-level browsing tab because they're fundamentally different from
# per-character skins / movesets.
GAMEPLAY_ROOT_CAT = 26521
PACK_CATEGORIES = {
    "All Packs": 0,          # sentinel: merged query across all below
    "Modpacks": 31562,       # HDR, Hewdraw Remix, etc.
    "Mechanics": 3326,       # Turbo Mode, etc.
    "Balance": 31561,
    "AI": 31564,
    "Parameters": 31563,
}
_PACK_CAT_IDS = [cid for cid in PACK_CATEGORIES.values() if cid]

# ── Mod-type classification by GameBanana category ──
# Maps a GameBanana subcategory id (or root cat id) to a coarse mod_type.
# Used so that, e.g., a music pack from the "Other" tab isn't installed as
# a skin (which causes incorrect slot-picker UI and bad Installed grouping).
#
# Granular types we care about:
#   skin       — per-fighter visual swap (the only type that needs slot UI)
#   stage      — stage replacement
#   moveset    — per-fighter moveset (full or partial)
#   modpack    — game-wide pack (HDR, Hewdraw, Turbo, …)
#   mechanics  — gameplay mechanics tweak
#   balance    — balance patch
#   ai         — CPU AI behaviour
#   parameters — fighter param tweaks
#   effect     — VFX
#   music      — BGM / soundtrack pack
#   ui         — menu / HUD changes
#   other      — anything else
_MOVESET_ROOT_CAT  = 3325
_MOVESET_FULL_CAT  = 31566
_MOVESET_PART_CAT  = 31565

MOD_TYPE_BY_CATEGORY = {
    SKINS_ROOT_CAT:      "skin",
    STAGES_ROOT_CAT:     "stage",
    _MOVESET_ROOT_CAT:   "moveset",
    _MOVESET_FULL_CAT:   "moveset",
    _MOVESET_PART_CAT:   "moveset",
    GAMEPLAY_ROOT_CAT:   "modpack",
    31562:               "modpack",
    3326:                "mechanics",
    31561:               "balance",
    31564:               "ai",
    31563:               "parameters",
    1177:                "effect",
    15929:               "music",
    1760:                "ui",
}
# All fighter sub-categories under Skins are skins.
for _cid in FIGHTER_CATEGORIES.values():
    MOD_TYPE_BY_CATEGORY.setdefault(_cid, "skin")
# All stage sub-categories under Stages are stages.
for _cid in STAGE_CATEGORIES.values():
    MOD_TYPE_BY_CATEGORY.setdefault(_cid, "stage")

# Mod types that REQUIRE slot picker UI (per-fighter slot remapping).
SLOT_AWARE_MOD_TYPES = frozenset(("skin",))


def _classify_mod_type_from_meta(meta, default="other"):
    """Return the coarse mod_type ("skin"/"stage"/"modpack"/…) for a mod.

    Strategy:
      1. Trust ``meta["mod_type"]`` if it's a known non-default value.
      2. Use ``meta["category_id"]`` against ``MOD_TYPE_BY_CATEGORY``.
      3. Use ``meta["root_category_id"]`` if we have it.
      4. Fall back to *default* (caller decides — usually "other").
    """
    if not meta:
        return default

    existing = meta.get("mod_type")
    if existing and existing not in (None, "", "skin"):
        # Trust an explicit non-default value (e.g. "stage", "modpack").
        return existing

    for key in ("category_id", "root_category_id"):
        cid = meta.get(key)
        try:
            cid = int(cid) if cid is not None else None
        except (TypeError, ValueError):
            cid = None
        if cid is None:
            continue
        t = MOD_TYPE_BY_CATEGORY.get(cid)
        if t:
            return t

    # Honour the existing "skin" if explicitly set — caller can override.
    if existing == "skin":
        return "skin"
    return default

SORT_OPTIONS = {
    "Most Liked": "Generic_MostLiked",
    "Most Downloaded": "Generic_MostDownloaded",
    "Most Viewed": "Generic_MostViewed",
    "Newest": "Generic_LatestDateModified",
}

RESULTS_PER_PAGE = 15
MAX_SLOT = 16  # c00 through c15 — modded fighters can exceed vanilla 8

# Map display fighter names → SSBU internal folder names (under fighter/)
FIGHTER_INTERNAL = {
    "Banjo & Kazooie": "buddy", "Bayonetta": "bayonetta", "Bowser": "koopa",
    "Bowser Jr.": "koopajr", "Byleth": "master", "Captain Falcon": "captain",
    "Charizard": "plizardon", "Chrom": "chrom", "Cloud": "cloud",
    "Corrin": "kamui", "Daisy": "daisy", "Dark Pit": "pitb",
    "Dark Samus": "samusd", "Diddy Kong": "diddy", "Donkey Kong": "donkey",
    "Dr. Mario": "mariod", "Duck Hunt": "duckhunt", "Falco": "falco",
    "Fox": "fox", "Ganondorf": "ganon", "Greninja": "gekkouga",
    "Hero": "brave", "Ice Climbers": "ice_climber", "Ike": "ike",
    "Incineroar": "gaogaen", "Inkling": "inkling", "Isabelle": "shizue",
    "Ivysaur": "pfushigisou", "Jigglypuff": "purin", "Joker": "jack",
    "Ken": "ken", "King Dedede": "dedede", "King K. Rool": "krool",
    "Kirby": "kirby", "Link": "link", "Little Mac": "littlemac",
    "Lucario": "lucario", "Lucas": "lucas", "Lucina": "lucina",
    "Luigi": "luigi", "Mario": "mario", "Marth": "marth",
    "Mega Man": "rockman", "Meta Knight": "metaknight", "Mewtwo": "mewtwo",
    "Mii Brawler": "miifighter", "Mii Gunner": "miigunner",
    "Mii Swordfighter": "miiswordsman", "Min Min": "tantan",
    "Mr. Game & Watch": "gamewatch", "Ness": "ness", "Olimar": "pikmin",
    "Pac-Man": "pacman", "Palutena": "palutena", "Peach": "peach",
    "Pichu": "pichu", "Pikachu": "pikachu", "Piranha Plant": "packun",
    "Pit": "pit", "Pokemon Trainer": "ptrainer",
    "Pyra/Mythra": "element", "R.O.B.": "robot", "Richter": "richter",
    "Ridley": "ridley", "Robin": "reflet", "Rosalina & Luma": "rosetta",
    "Roy": "roy", "Ryu": "ryu", "Samus": "samus",
    "Sephiroth": "edge", "Sheik": "sheik", "Shulk": "shulk",
    "Simon": "simon", "Snake": "snake", "Sonic": "sonic",
    "Squirtle": "pzenigame", "Steve": "pickel", "Terry": "dolly",
    "Toon Link": "toonlink", "Villager": "murabito", "Wario": "wario",
    "Wii Fit Trainer": "wiifit", "Wolf": "wolf", "Yoshi": "yoshi",
    "Young Link": "younglink", "Zelda": "zelda", "Zero Suit Samus": "szerosuit",
}

# Reverse lookup: internal name → display name
INTERNAL_TO_DISPLAY = {v: k for k, v in FIGHTER_INTERNAL.items()}


def get_occupied_slots(fighter_internal):
    """Scan all installed mods on SD to find which slots (c00-c07) are occupied
    for a given fighter internal name.
    Returns dict mapping slot string -> {'mod': folder_name, 'thumb_url': url|None}."""
    occupied = {}
    if not os.path.exists(ARCROPOLIS_MODS):
        return occupied
    for mod_name in os.listdir(ARCROPOLIS_MODS):
        mod_path = os.path.join(ARCROPOLIS_MODS, mod_name)
        body_path = os.path.join(mod_path, "fighter",
                                 fighter_internal, "model", "body")
        if os.path.isdir(body_path):
            # Try to read thumbnail URL from metadata
            thumb_url = None
            display_name = mod_name
            meta_path = os.path.join(mod_path, ".gb_meta.json")
            if os.path.exists(meta_path):
                try:
                    with open(meta_path, "r", encoding="utf-8") as f:
                        meta = json.load(f)
                    thumb_url = meta.get("thumb_url")
                    display_name = meta.get("name", mod_name)
                except Exception:
                    pass
            mod_id_val = None
            if os.path.exists(meta_path):
                try:
                    with open(meta_path, "r", encoding="utf-8") as _f:
                        _m = json.load(_f)
                    mod_id_val = _m.get("mod_id")
                except Exception:
                    pass
            for slot in os.listdir(body_path):
                if os.path.isdir(os.path.join(body_path, slot)) and \
                   re.match(r'^c\d{2}$', slot):
                    occupied[slot] = {
                        "mod": mod_name,
                        "name": display_name,
                        "thumb_url": thumb_url,
                        "mod_id": mod_id_val,
                    }
    return occupied


def _detect_fighter_internal_from_archive(extracted_path):
    """Detect fighter internal name from extracted mod content."""
    mod_root = find_mod_content(extracted_path)
    if not mod_root:
        return None
    fighter_dir = os.path.join(mod_root, "fighter")
    if os.path.isdir(fighter_dir):
        fighters = [f for f in os.listdir(fighter_dir)
                     if os.path.isdir(os.path.join(fighter_dir, f))]
        if fighters:
            return fighters[0]  # primary fighter
    return None


# ─────────────────────────────────────────────────────────
#  FILE-LEVEL CONFLICT DETECTION
# ─────────────────────────────────────────────────────────

# Metadata / non-game files that should be ignored during conflict checks.
_META_FILES = frozenset((".gb_meta.json", "config.json", "README.txt",
                         "README.md", "info.toml"))


def _get_mod_file_set(mod_root_path):
    """Return set of relative file paths (forward-slash, lowered) for a mod
    folder on the SD card.  Metadata files are excluded."""
    result = set()
    for root, _dirs, files in os.walk(mod_root_path):
        for f in files:
            if f in _META_FILES:
                continue
            rel = os.path.relpath(os.path.join(root, f),
                                  mod_root_path).replace("\\", "/")
            result.add(rel.lower())
    return result


# ─── Path classification ─────────────────────────────────
# A relative mod path is classified as (fighter, slot).  Both may be None.
#   (fighter, slot)  → fully slot-specific.  Same key in two mods = REAL
#                      slot conflict that must be resolved by remapping.
#   (fighter, None)  → fighter-wide but slot-agnostic (rare).
#   (None,    None)  → shared resource (msg files, ui_chara_db, params,
#                      character-select layouts…).  Overlaps here are
#                      "last-write wins" by ARCropolis and are NOT a slot
#                      collision; they should not trigger the slot-conflict
#                      modal that asks the user to remap.
_SLOT_DIR_RE   = re.compile(r'^c(\d{2})$')
_SLOT_TOKEN_RE = re.compile(r'(?:^|[_/])c(\d{2})(?:[_/.]|$)')
_UI_BNTX_RE    = re.compile(r'^chara_\d+_([a-z][a-z0-9_]*?)_(\d{2})\.bntx$',
                            re.I)
_SOUND_FILE_RE = re.compile(
    r'^(?:vc_|se_|bgm_|narration_)?([a-z][a-z0-9_]*?)_c(\d{2})'
    r'\.(?:nus3audio|nus3bank)$', re.I)


def _classify_mod_path(rel_path):
    """Return ``(fighter, slot)`` tuple for a relative mod file path.

    Used to distinguish *real* per-slot collisions (same fighter+slot in two
    mods) from shared-resource overlaps that ARCropolis can layer safely.
    """
    parts = rel_path.replace("\\", "/").lower().split("/")
    if len(parts) < 2:
        return None, None

    fighter = None
    rest = []  # parts to scan for a slot token

    if parts[0] == "fighter":
        fighter = parts[1]
        rest = parts[2:]
    elif (parts[0] == "effect" and len(parts) >= 3
          and parts[1] == "fighter"):
        fighter = parts[2]
        rest = parts[3:]
    elif (parts[0] == "sound" and len(parts) >= 4
          and parts[1] == "bank"
          and parts[2] in ("fighter", "fighter_voice", "narration")):
        m = _SOUND_FILE_RE.match(parts[-1])
        if m:
            return m.group(1), f"c{m.group(2)}"
        return None, None
    elif (parts[0] == "ui" and len(parts) >= 5
          and parts[1] == "replace" and parts[2] == "chara"):
        # NOTE: parts[3] is "chara_<TYPE>" (portrait type, NOT a slot).
        m = _UI_BNTX_RE.match(parts[-1])
        if m:
            return m.group(1), f"c{m.group(2)}"
        return None, None
    elif parts[0] == "item":
        # Item slots aren't keyed by fighter; treat as shared.
        return None, None
    else:
        # Truly shared (param/, ui/message/, ui/param/database/, ui/menu/, …)
        return None, None

    # Look for a slot token within the remaining path.  Prefer a bare
    # ``cXX`` directory component, then any ``_cXX_`` token in a filename.
    slot = None
    for p in rest:
        m = _SLOT_DIR_RE.match(p)
        if m:
            slot = f"c{m.group(1)}"
            break
    if slot is None:
        for p in rest:
            m = _SLOT_TOKEN_RE.search(p)
            if m:
                slot = f"c{m.group(1)}"
                break
    return fighter, slot


def detect_file_conflicts(new_mod_path, exclude_mod_names=None):
    """Compare *new_mod_path* against every installed SD-card mod and
    classify the overlaps.

    Returns
    -------
    dict with two keys:
        ``slot``   {(fighter, slot): {mod_name: [rel_path, ...]}}
            *Real* per-slot collisions — the same fighter+slot is touched
            by another mod.  These are the ones the user must resolve
            (remap to a free slot, or knowingly overwrite).
        ``shared`` {mod_name: [rel_path, ...]}
            Overlaps on shared resources (msg files, ui_chara_db, params,
            etc.).  ARCropolis layers these last-write-wins; they are
            NOT slot conflicts and should not trigger a remap prompt.
    """
    out = {"slot": {}, "shared": {}}
    if not os.path.exists(ARCROPOLIS_MODS):
        return out

    exclude = set(exclude_mod_names) if exclude_mod_names else set()
    new_files = _get_mod_file_set(new_mod_path)
    if not new_files:
        return out

    new_class = {rel: _classify_mod_path(rel) for rel in new_files}

    for mod_name in os.listdir(ARCROPOLIS_MODS):
        if mod_name in exclude:
            continue
        mod_dir = os.path.join(ARCROPOLIS_MODS, mod_name)
        if not os.path.isdir(mod_dir):
            continue
        existing = _get_mod_file_set(mod_dir)
        overlap = new_files & existing
        for rel in overlap:
            fighter, slot = new_class[rel]
            if fighter and slot:
                key = (fighter, slot)
                out["slot"].setdefault(key, {}) \
                           .setdefault(mod_name, []).append(rel)
            else:
                out["shared"].setdefault(mod_name, []).append(rel)
    return out


def find_free_body_slots(fighter_internal, count=1):
    """Return a list of up to *count* body-slot strings (e.g. ``['c10','c11']``)
    that are NOT occupied by any installed mod for *fighter_internal*.
    Searches c00 → c15."""
    occupied = get_occupied_slots(fighter_internal)
    free = []
    for i in range(MAX_SLOT):
        slot = f"c{i:02d}"
        if slot not in occupied:
            free.append(slot)
            if len(free) >= count:
                break
    return free


def _summarise_conflicts(conflicts):
    """Return a human-readable summary string from a *new-style* conflicts
    dict (``{"slot": {...}, "shared": {...}}``).  Slot collisions are
    reported per (fighter, slot); shared overlaps are listed separately
    and clearly labelled as non-blocking."""
    lines = []

    slot_part = conflicts.get("slot") or {}
    if slot_part:
        lines.append("Slot collisions (same fighter + same slot):")
        for (fighter, slot), mods in sorted(slot_part.items()):
            display = INTERNAL_TO_DISPLAY.get(fighter, fighter)
            for mod, files in sorted(mods.items()):
                sample = files[:2]
                extra = f" (+{len(files)-2} more)" if len(files) > 2 else ""
                lines.append(
                    f"  • {display} {slot}  ↔  {mod}: "
                    f"{len(files)} file(s){extra}")
                for s in sample:
                    lines.append(f"      {s}")

    shared_part = conflicts.get("shared") or {}
    if shared_part:
        if lines:
            lines.append("")
        lines.append("Shared-resource overlaps "
                     "(layered by ARCropolis, not a slot collision):")
        for mod, files in sorted(shared_part.items()):
            sample = files[:2]
            extra = f" (+{len(files)-2} more)" if len(files) > 2 else ""
            lines.append(f"  • {mod}: {len(files)} file(s){extra}")
            for s in sample:
                lines.append(f"      {s}")

    return "\n".join(lines)


# ─────────────────────────────────────────────────────────
#  CATPPUCCIN MOCHA THEME
# ─────────────────────────────────────────────────────────

class T:
    BG       = "#1e1e2e"
    FG       = "#cdd6f4"
    ACCENT   = "#89b4fa"
    GREEN    = "#a6e3a1"
    RED      = "#f38ba8"
    YELLOW   = "#f9e2af"
    PEACH    = "#fab387"
    SURFACE  = "#313244"
    SURFACE1 = "#45475a"
    CRUST    = "#11111b"
    OVERLAY  = "#7f849c"
    SUBTEXT  = "#a6adc8"
    FONT     = "Segoe UI"
    MONO     = "Consolas"
    # Font sizes (change these to scale the entire UI)
    SZ_XS    = 9       # was 7  — tags, tiny labels
    SZ_SM    = 10      # was 8  — secondary info, buttons
    SZ_MD    = 11      # was 9  — body text, controls
    SZ_LG    = 12      # was 10 — section headers
    SZ_XL    = 13      # was 11 — card titles
    SZ_XXL   = 14      # was 12 — status icons
    SZ_H2    = 15      # was 13 — page headers
    SZ_H1    = 16      # was 14 — app title

# ─────────────────────────────────────────────────────────
#  API HELPERS
# ─────────────────────────────────────────────────────────

def api_search_mods(query="", category_id=None, sort="Generic_MostLiked",
                    page=1, per_page=RESULTS_PER_PAGE,
                    root_cat=None):
    """Search GameBanana for SSBU mods. Returns (total, [records]).

    *root_cat* defaults to SKINS_ROOT_CAT. Pass STAGES_ROOT_CAT to browse
    stages instead.  Pass None for game-wide (no category filter).

    When a text query is provided, uses the /Util/Search/Results endpoint.
    NOTE: The Search endpoint ignores _aFilters[Generic_Category], so we
    must post-filter results client-side using _aRootCategory._idRow.
    We over-fetch and filter, then paginate locally.

    Without a text query, uses /Mod/Index which correctly respects the
    category filter.
    """
    if root_cat is None and category_id is not None:
        root_cat = SKINS_ROOT_CAT

    if not HAS_REQUESTS:
        raise RuntimeError("requests package not installed")

    # Whether the caller has narrowed to a true *sub*-category (e.g.
    # "Byleth" within Skins, or "Mechanics" within Packs).  Selecting
    # "All Skins" sets ``category_id == root_cat`` and is treated as a
    # root-level browse, not a sub-category.
    has_subcat = (category_id is not None
                  and root_cat is not None
                  and category_id != root_cat)

    if query.strip() and has_subcat:
        # ── Text search inside a specific sub-category ──
        # The Search endpoint ignores category filters, so a "Byleth" +
        # "Enlightened" query would otherwise return every skin in the
        # game whose name happens to contain "enlightened" (and may not
        # surface the actual Byleth mod at all because of search-engine
        # ranking).  Browsing the sub-category and filtering names
        # client-side is both correct and predictable.
        url = f"{API_BASE}/Mod/Index"
        needle = query.strip().lower()
        # Pull the same sort order the user picked, so name matches keep
        # their relative ordering even after we trim.
        page_size = 50
        max_pages = 20  # cap at ~1000 records per category
        matched = []
        for api_page in range(1, max_pages + 1):
            params = {
                "_nPerpage": page_size,
                "_nPage": api_page,
                "_sSort": sort,
                "_aFilters[Generic_Game]": SSBU_GAME_ID,
                "_aFilters[Generic_Category]": category_id,
            }
            resp = requests.get(url, params=params, verify=False, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            batch = data.get("_aRecords", [])
            if not batch:
                break
            for rec in batch:
                name = (rec.get("_sName") or "").lower()
                if needle in name:
                    matched.append(rec)
            if len(batch) < page_size:
                break  # exhausted the category
        start = (page - 1) * per_page
        return len(matched), matched[start:start + per_page]

    if query.strip():
        # ── Text search — post-filter by root category ──
        # The API ignores category filters on the Search endpoint, so we
        # fetch in batches and keep only records matching our root_cat.
        url = f"{API_BASE}/Util/Search/Results"
        base_params = {
            "_sSearchString": query.strip(),
            "_sOrder": "best_match",
            "_idGameRow": SSBU_GAME_ID,
            "_sModelName": "Mod",
        }
        # Collect enough filtered results to fill the requested page.
        # We need (page * per_page) matching records to serve page N.
        need = page * per_page
        matched = []
        api_page = 1
        # Fetch larger batches to minimize round-trips
        batch_size = max(per_page * 3, 50)
        api_total = None
        max_api_pages = 20  # safety limit
        while len(matched) < need and api_page <= max_api_pages:
            params = dict(base_params)
            params["_nPerpage"] = batch_size
            params["_nPage"] = api_page
            resp = requests.get(url, params=params, verify=False, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            if api_total is None:
                api_total = data.get("_aMetadata", {}).get("_nRecordCount", 0)
            batch = data.get("_aRecords", [])
            if not batch:
                break
            for rec in batch:
                rc = rec.get("_aRootCategory", {})
                # Search endpoint doesn't include _idRow in _aRootCategory,
                # but _sProfileUrl ends with /cats/<id>
                rc_url = rc.get("_sProfileUrl", "")
                try:
                    rc_id = int(rc_url.rstrip("/").rsplit("/", 1)[-1])
                except (ValueError, IndexError):
                    rc_id = None
                if root_cat is None or rc_id == root_cat:
                    matched.append(rec)
            api_page += 1
        # Slice for the requested page
        start = (page - 1) * per_page
        page_records = matched[start:start + per_page]
        # Estimate total: if we exhausted the API without hitting our limit,
        # len(matched) is the true total; otherwise it's a lower bound.
        est_total = len(matched)
        return est_total, page_records
    else:
        # ── Browse by category / sort ──
        url = f"{API_BASE}/Mod/Index"
        params = {
            "_nPerpage": per_page,
            "_nPage": page,
            "_sSort": sort,
        }

        if root_cat is None:
            # Game-wide query (no category filter)
            params["_aFilters[Generic_Game]"] = SSBU_GAME_ID
        elif category_id and category_id != root_cat:
            params["_aFilters[Generic_Category]"] = category_id
        else:
            params["_aFilters[Generic_Game]"] = SSBU_GAME_ID
            params["_aFilters[Generic_Category]"] = root_cat

    resp = requests.get(url, params=params, verify=False, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    total = data.get("_aMetadata", {}).get("_nRecordCount", 0)
    records = data.get("_aRecords", [])
    return total, records


def api_get_mod_files(mod_id):
    """Get downloadable files for a specific mod.

    GameBanana occasionally returns an empty body or an HTML error page
    in place of the JSON envelope (rate limit, transient WAF block,
    Cloudflare interstitial). A bare ``resp.json()`` on those surfaces
    a baffling ``Expecting value: line 1 column 1 (char 0)`` to the
    user the moment they click Install. Retry once with a short delay,
    and on persistent failure surface a meaningful error.
    """
    url = f"{API_BASE}/Mod/{mod_id}"
    params = {"_csvProperties": "_aFiles,_sName"}
    last_err = None
    for attempt in range(2):
        try:
            resp = requests.get(url, params=params,
                                 verify=False, timeout=30)
            resp.raise_for_status()
            text = resp.text or ""
            if not text.strip():
                raise ValueError("empty response body")
            try:
                return resp.json()
            except json.JSONDecodeError as je:
                # Likely an HTML error page; capture a snippet for the
                # error message instead of raw "char 0".
                snippet = text.strip()[:120]
                raise ValueError(
                    f"non-JSON response (got: {snippet!r})") from je
        except Exception as e:
            last_err = e
            if attempt == 0:
                time.sleep(0.6)
                continue
            break
    raise RuntimeError(
        f"GameBanana API failed for mod {mod_id}: {last_err}") from last_err


def api_get_mod_images(mod_id):
    """Fetch preview images for a mod from the GameBanana API.
    Returns list of dicts with 'large' and 'thumb' keys, same format
    as _extract_all_image_urls."""
    try:
        url = f"{API_BASE}/Mod/{mod_id}"
        params = {"_csvProperties": "_aPreviewMedia"}
        resp = requests.get(url, params=params, verify=False, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        return _extract_all_image_urls(data)
    except Exception:
        return []


class DownloadCancelled(Exception):
    """Raised when a download is cancelled by the user."""
    pass


_ARCHIVE_MAGIC = {
    ".zip": (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"),
    ".7z":  (b"7z\xbc\xaf\x27\x1c",),
    ".rar": (b"Rar!\x1a\x07\x00", b"Rar!\x1a\x07\x01\x00"),
}


def _validate_archive_magic(path):
    """Inspect the first few bytes of a downloaded archive and confirm
    it actually IS the format the extension claims.

    GameBanana sometimes returns an HTML error page (rate limit / WAF
    block / login required) with the wrong content-type, or a 200 OK
    with an empty body. Either way we end up with a "file" that's
    not really an archive. Catching this here means callers fail
    fast with a clear message instead of getting "File is not a zip
    file" cryptically several layers up.

    Returns ``(ok: bool, message: str)``. ``ok=True`` means the magic
    bytes match the extension.
    """
    if not os.path.isfile(path):
        return False, f"file does not exist: {path}"
    size = os.path.getsize(path)
    if size == 0:
        return False, "0-byte download (server returned empty body)"
    if size < 64:
        return False, f"suspiciously tiny ({size} bytes — likely an error page)"
    ext = os.path.splitext(path)[1].lower()
    expected = _ARCHIVE_MAGIC.get(ext)
    if not expected:
        return True, "unknown extension; skipping magic check"
    try:
        with open(path, "rb") as f:
            head = f.read(16)
    except OSError as e:
        return False, f"read failed: {e}"
    if any(head.startswith(m) for m in expected):
        return True, "ok"
    # Common case: server returned an HTML error page.
    if head[:5] in (b"<!DOC", b"<html", b"<HTML", b"<!doc"):
        return False, ("got an HTML error page instead of an "
                       f"{ext} archive — likely rate-limited or "
                       "the download URL expired. Wait a minute "
                       "and retry.")
    return False, (f"file does not look like {ext} (first bytes: "
                   f"{head[:8]!r}) — possibly corrupted, encrypted, "
                   "or wrong format.")


def download_file_to(url, dest, progress_cb=None, cancel_check=None):
    """Download a URL to a local path with optional progress callback.
    cancel_check: callable returning True if download should be aborted."""
    resp = requests.get(url, stream=True, allow_redirects=True, verify=False, timeout=120)
    resp.raise_for_status()
    total = int(resp.headers.get("content-length", 0))
    downloaded = 0
    with open(dest, "wb") as f:
        for chunk in resp.iter_content(chunk_size=8192):
            if cancel_check and cancel_check():
                raise DownloadCancelled("Download cancelled by user")
            if chunk:
                f.write(chunk)
                downloaded += len(chunk)
                if progress_cb and total:
                    progress_cb(downloaded, total)
    # Server returned an empty body. Don't pretend the download
    # succeeded — caller will end up with a 0-byte file masquerading
    # as an archive.
    if downloaded == 0:
        try:
            os.remove(dest)
        except OSError:
            pass
        raise RuntimeError(
            "Server returned an empty body (0 bytes) — likely a "
            "rate limit or transient block. Wait a minute and retry.")
    return dest


def fetch_thumbnail(image_url):
    """Fetch a thumbnail image from URL, return PhotoImage or None."""
    if not HAS_PIL:
        return None
    try:
        resp = requests.get(image_url, verify=False, timeout=10)
        resp.raise_for_status()
        img = Image.open(io.BytesIO(resp.content))
        # Resize to fit nicely
        img.thumbnail((220, 124), Image.LANCZOS)
        return ImageTk.PhotoImage(img)
    except Exception as e:
        print(f"    [thumb] Failed to load {image_url}: {e}")
        return None


# ── SSBH Editor integration ──

def _ensure_ssbh_editor():
    """Return the path to ssbh_editor.exe, downloading it from GitHub
    if it isn't already present. Returns ``None`` on failure."""
    if os.path.isfile(SSBH_EDITOR_EXE):
        return SSBH_EDITOR_EXE
    if not HAS_REQUESTS:
        print("  ! Cannot download ssbh_editor — requests not available.")
        return None
    print("  Downloading ssbh_editor from GitHub…")
    asset = github_latest_asset(
        "ScanMountGoat/ssbh_editor",
        lambda n: "win" in n.lower() and n.endswith(".zip"))
    if not asset:
        print("  ! Could not find ssbh_editor Windows release.")
        return None
    zip_path = os.path.join(tempfile.gettempdir(), asset["filename"])
    try:
        download_file_to(asset["url"], zip_path)
        os.makedirs(SSBH_EDITOR_DIR, exist_ok=True)
        import zipfile
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(SSBH_EDITOR_DIR)
        os.remove(zip_path)
    except Exception as e:
        print(f"  ! ssbh_editor download failed: {e}")
        return None
    # The zip may nest the exe in a subfolder — search for it.
    for root, _dirs, files in os.walk(SSBH_EDITOR_DIR):
        for f in files:
            if f.lower() == "ssbh_editor.exe":
                found = os.path.join(root, f)
                print(f"  ssbh_editor ready at {found}")
                return found
    print("  ! ssbh_editor.exe not found in download.")
    return None


def _find_model_folder(mod_root):
    """Locate the first ``model/body/cXX`` (or ``model/<any>/cXX``)
    directory inside an extracted mod. Returns the path to the model
    tree root (e.g. ``…/fighter/packun/model/body/c00``) or ``None``.
    """
    content = find_mod_content(mod_root)
    if not content:
        return None
    fighter_dir = os.path.join(content, "fighter")
    if not os.path.isdir(fighter_dir):
        return None
    for fighter in os.listdir(fighter_dir):
        model_dir = os.path.join(fighter_dir, fighter, "model")
        if not os.path.isdir(model_dir):
            continue
        # Prefer "body" tree, fall back to first tree found.
        for tree in sorted(os.listdir(model_dir),
                           key=lambda t: (0 if t == "body" else 1, t)):
            tree_dir = os.path.join(model_dir, tree)
            if not os.path.isdir(tree_dir):
                continue
            for slot in sorted(os.listdir(tree_dir)):
                slot_dir = os.path.join(tree_dir, slot)
                if os.path.isdir(slot_dir) and \
                        re.fullmatch(r"c\d{2}", slot, re.I):
                    return slot_dir
    return None


# ── 3-D model preview renderer ──────────────────────────

# Per-process cache: nutexb path → decoded PIL.Image (or False if it failed).
# Saves the cost of re-running the CLI / re-loading PNGs across slot renders.
_NUTEXB_IMAGE_CACHE = {}
# Per-process flag: only print the "downloading ultimate_tex_cli…" message
# once even if many slots all need decoding on first run.
_ULTIMATE_TEX_WARNED_MISSING = False


def _ensure_ultimate_tex_cli():
    """Return the path to ultimate_tex_cli.exe, downloading it from
    GitHub if it isn't already present. Returns ``None`` on failure.

    Used by the textured 3D preview to decode SSBU's swizzled BC-compressed
    .nutexb textures into PNGs that pyrender can sample.
    """
    global _ULTIMATE_TEX_WARNED_MISSING
    if os.path.isfile(ULTIMATE_TEX_EXE):
        return ULTIMATE_TEX_EXE
    if not HAS_REQUESTS:
        if not _ULTIMATE_TEX_WARNED_MISSING:
            print("  ! Cannot download ultimate_tex_cli — requests not available.")
            _ULTIMATE_TEX_WARNED_MISSING = True
        return None
    print("  Downloading ultimate_tex_cli from GitHub…")
    asset = github_latest_asset(
        "ScanMountGoat/ultimate_tex",
        lambda n: ("cli" in n.lower() and "win" in n.lower()
                   and n.lower().endswith(".zip")))
    if not asset:
        # Some releases ship the CLI inside the GUI bundle — try a looser filter.
        asset = github_latest_asset(
            "ScanMountGoat/ultimate_tex",
            lambda n: "win" in n.lower() and n.lower().endswith(".zip"))
    if not asset:
        print("  ! Could not find ultimate_tex_cli Windows release.")
        return None
    zip_path = os.path.join(tempfile.gettempdir(), asset["filename"])
    try:
        download_file_to(asset["url"], zip_path)
        os.makedirs(ULTIMATE_TEX_DIR, exist_ok=True)
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(ULTIMATE_TEX_DIR)
        os.remove(zip_path)
    except Exception as e:
        print(f"  ! ultimate_tex_cli download failed: {e}")
        return None
    for root, _dirs, files in os.walk(ULTIMATE_TEX_DIR):
        for f in files:
            if f.lower() == "ultimate_tex_cli.exe":
                found = os.path.join(root, f)
                print(f"  ultimate_tex_cli ready at {found}")
                return found
    print("  ! ultimate_tex_cli.exe not found in download.")
    return None


def _decode_nutexb_to_image(nutexb_path):
    """Decode a Switch ``.nutexb`` to a :class:`PIL.Image.Image` via
    ``ultimate_tex_cli``. Result is cached on disk under
    :data:`NUTEXB_PNG_CACHE_DIR` keyed by absolute path + size + mtime,
    so repeat decodes for the same file are instant.

    Returns ``None`` if decoding fails (missing CLI, bad file, etc.) so
    callers can fall back to flat-color rendering.
    """
    if not HAS_PIL or not os.path.isfile(nutexb_path):
        return None

    cached = _NUTEXB_IMAGE_CACHE.get(nutexb_path)
    if cached is False:
        return None
    if cached is not None:
        return cached

    try:
        st = os.stat(nutexb_path)
        cache_key = f"{abs(hash(nutexb_path))}_{st.st_size}_{int(st.st_mtime)}"
    except OSError:
        _NUTEXB_IMAGE_CACHE[nutexb_path] = False
        return None
    os.makedirs(NUTEXB_PNG_CACHE_DIR, exist_ok=True)
    png_path = os.path.join(NUTEXB_PNG_CACHE_DIR, f"{cache_key}.png")

    if not os.path.isfile(png_path):
        cli = _ensure_ultimate_tex_cli()
        if not cli:
            _NUTEXB_IMAGE_CACHE[nutexb_path] = False
            return None
        try:
            import subprocess
            subprocess.run(
                [cli, nutexb_path, png_path],
                check=True, capture_output=True, timeout=30,
                creationflags=0x08000000)  # CREATE_NO_WINDOW
        except subprocess.CalledProcessError:
            # ultimate_tex_cli returns non-zero for many benign cases
            # (slot dir doesn't exist, exotic format, malformed file).
            # Cache the negative result and move on without spamming.
            _NUTEXB_IMAGE_CACHE[nutexb_path] = False
            return None
        except Exception as e:
            print(f"  nutexb decode failed for {os.path.basename(nutexb_path)}: {e}",
                  file=sys.stderr)
            _NUTEXB_IMAGE_CACHE[nutexb_path] = False
            return None
        if not os.path.isfile(png_path):
            _NUTEXB_IMAGE_CACHE[nutexb_path] = False
            return None

    try:
        # Keep alpha — SSBU eye/hair/clothing textures rely on it. We
        # convert to RGBA explicitly so the channel layout is stable
        # regardless of what mode ultimate_tex_cli wrote.
        img = Image.open(png_path).convert("RGBA")
        _NUTEXB_IMAGE_CACHE[nutexb_path] = img
        return img
    except Exception:
        _NUTEXB_IMAGE_CACHE[nutexb_path] = False
        return None


def _resolve_nutexb_for_texture(slot_dir, tex0_name):
    """Find the .nutexb file matching a Texture0 reference.

    SSBU material entries reference textures by stem (no extension),
    e.g. ``"body_001_col"`` → ``body_001_col.nutexb``. The file usually
    lives in the same slot directory as the model files.
    """
    if not tex0_name:
        return None
    base = os.path.basename(tex0_name).lower()
    if not base.endswith(".nutexb"):
        base += ".nutexb"
    candidate = os.path.join(slot_dir, base)
    if os.path.isfile(candidate):
        return candidate
    # Case-insensitive fallback (Windows is normally fine, but mods are
    # sometimes packed on Linux with stricter casing).
    for f in os.listdir(slot_dir):
        if f.lower() == base:
            return os.path.join(slot_dir, f)
    return None


def _find_all_model_slots(mod_root):
    """Return a list of ``(slot_label, slot_dir)`` for every ``cXX``
    folder found inside the fighter's ``model/body/`` subtree.
    Sorted by slot name (c00, c01, …).
    """
    content = find_mod_content(mod_root)
    if not content:
        return []
    fighter_dir = os.path.join(content, "fighter")
    if not os.path.isdir(fighter_dir):
        return []
    slots = []
    for fighter in os.listdir(fighter_dir):
        model_dir = os.path.join(fighter_dir, fighter, "model")
        if not os.path.isdir(model_dir):
            continue
        for tree in sorted(os.listdir(model_dir),
                           key=lambda t: (0 if t == "body" else 1, t)):
            tree_dir = os.path.join(model_dir, tree)
            if not os.path.isdir(tree_dir):
                continue
            for slot in sorted(os.listdir(tree_dir)):
                slot_dir = os.path.join(tree_dir, slot)
                if os.path.isdir(slot_dir) and \
                        re.fullmatch(r"c\d{2}", slot, re.I):
                    # Only include slots that have a mesh file
                    if any(f.lower().endswith(".numshb")
                           for f in os.listdir(slot_dir)):
                        slots.append((slot.lower(), slot_dir))
            if slots:
                return slots
    return slots


def _build_colored_scene(model_dir):
    """Read mesh + material data from *model_dir* and build a
    :class:`pyrender.Scene` whose materials are flat-shaded with the
    UV-mapped textures via pyrender's generic PBR, with type-specific
    handling for the four shapes SSBU mods come in (see ``_classify_mod``).

    SSBU rendering principles (learned from ScanMountGoat's open-source
    ssbh_wgpu — model.wgsl + nutexb_wgpu/src/lib.rs):

    1. **SSBU is NOT cel-shaded.** It's stylized GGX PBR. The "cel look"
       comes from SSS skin softening (CustomVector11/30) + rim lighting
       (CustomVector14/StageCustomVector8) + emissive added flatly on
       top of diffuse. We don't have stage data, so we approximate:
       ambient + 2 directional lights gives roughly the right diffuse,
       and rim/SSS effects are missing.

    2. **Final color formula** (model.wgsl ~lines 1473–1500):
           outColor = diffuse*(1-metal)/π + specular*AO + emission*0.5
       Emission (Texture5) is **added**, not multiplied or substituted.
       Our current "swap T0→T5 when T0 is near-black" is a coarse
       approximation that works because the dim diffuse contributes
       little and the rendered scene reads as the emission color.

    3. **Texture role map** (per material, by glTF/SSBU slot):
       - Texture0  = base color (col)
       - Texture1  = overlay color, blended via CustomBoolean11 (additive)
                     or alpha (line 230 ``Blend``)
       - Texture4  = normal map (BC5: only R,G are real;
                     Z = sqrt(max(1 − x² − y², 0)); A is a CAVITY map)
       - Texture5  = emission
       - Texture6  = PRM (R=metallic, G=roughness, B=AO, A·0.2=F0)
       - Texture7  = #replace_cubemap (env reflection)
       - Texture14 = layered eye color

    4. **Mesh→material binding is authoritative via the .numdlb.** Match
       on (mesh_object_name, mesh_object_subindex) → material_label.
       ssbh_wgpu does NOT pattern-match mesh names. We mirror that for
       full mods; partial mods (no matl) need our heuristic ladder.

    5. **No special "eye" code path** — eye highlights are baked into
       the col texture or driven by emission. The "iris follows camera"
       effect is a UV transform via CustomVector6, not a shader trick.

    What we don't reproduce (without writing a custom shader):
    - Rim lighting (depends on stage data we don't have)
    - SSS for skin
    - PBR specular with proper PRM channels (we hardcode rough/metal)
    - BC5 normal map Z-reconstruction + tangent-space lighting
    - Multi-layer texture blending (Texture0+Texture1 with boolean)

    Returns ``(scene, combined_trimesh)`` or ``(None, None)`` on
    failure.
    """
    if not HAS_3D_RENDER:
        return None, None

    import colorsys

    # Pick the canonical mesh + matching material/layout files. SSBU
    # mods often ship alternate matls alongside the real one
    # (e.g. ``dark_model.numatb`` next to ``model.numatb``); the
    # alternates aren't bound to the mesh and only exist as toggles
    # in tools. Always prefer the file that shares its base name with
    # the .numshb so material → mesh lookups land on the right entries.
    numshb_files = [f for f in os.listdir(model_dir)
                    if f.lower().endswith(".numshb")]
    if not numshb_files:
        return None, None
    numshb_files.sort(key=lambda f: (0 if f.lower() == "model.numshb" else 1, f))
    numshb = os.path.join(model_dir, numshb_files[0])
    base = os.path.splitext(numshb_files[0])[0].lower()

    def _pick_paired(ext):
        candidates = [f for f in os.listdir(model_dir)
                      if f.lower().endswith(ext)]
        if not candidates:
            return None
        # Exact base match wins; failing that, prefer "model.<ext>",
        # then fall back to first alphabetical.
        for f in candidates:
            if os.path.splitext(f)[0].lower() == base:
                return os.path.join(model_dir, f)
        for f in candidates:
            if f.lower() == "model" + ext:
                return os.path.join(model_dir, f)
        return os.path.join(model_dir, sorted(candidates)[0])

    numatb = _pick_paired(".numatb")
    numdlb = _pick_paired(".numdlb")
    nusktb = _pick_paired(".nusktb")

    try:
        mesh_data = ssbh_data_py.mesh_data.read_mesh(numshb)
    except Exception:
        return None, None

    # Per-slot hue rotation — only used as a last-ditch fallback when
    # the texture for a material can't be decoded.
    slot_offset = 0
    m_slot = re.match(r'c(\d{2})$', os.path.basename(model_dir).lower())
    if m_slot:
        slot_offset = (int(m_slot.group(1)) * 23) % 360

    def _srgb_to_linear(c):
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    def _hls_to_linear(h_deg, l, s):
        r, g, b = colorsys.hls_to_rgb((h_deg % 360) / 360.0, l, s)
        return (_srgb_to_linear(r), _srgb_to_linear(g), _srgb_to_linear(b))

    # Build bone-name → bind-pose world transform map. Parent-boned
    # meshes (helmets, hair, shells, eye overlays) store their
    # vertices in their parent bone's *local* space; we need to apply
    # the bone's world transform to bring them back into model space,
    # otherwise they pile up at the model's origin (visible as
    # "shell at the feet" / "hair at the ankles").
    #
    # ssbh_data_py returns 4×4 matrices in row-major form, suitable
    # for ``p_world = p_local @ M`` with row vectors.
    bone_world = {}
    if nusktb:
        try:
            skel = ssbh_data_py.skel_data.read_skel(nusktb)
            for bone in skel.bones:
                try:
                    m = np.asarray(skel.calculate_world_transform(bone),
                                   dtype=np.float32)
                    bone_world[bone.name] = m
                except Exception:
                    pass
        except Exception:
            pass

    # Build mesh→material mapping
    mesh_to_mat = {}
    if numdlb:
        try:
            modl = ssbh_data_py.modl_data.read_modl(numdlb)
            for e in modl.entries:
                mesh_to_mat[(e.mesh_object_name,
                             e.mesh_object_subindex)] = e.material_label
        except Exception:
            pass

    # ──────────────────────────────────────────────────────────────────
    # MOD TYPE CLASSIFIER
    # ──────────────────────────────────────────────────────────────────
    # SSBU mods come in distinct shapes that need different handling.
    # We classify the mod up-front so per-mesh decisions can dispatch
    # on type instead of re-deriving heuristics every iteration.
    #
    # Types:
    #   "full_textured"  — Has matl + skel + complete texture set.
    #                      Trust the matl: textures map to meshes
    #                      exactly as the mod author specified. Examples:
    #                      KHCloud_ExtraSlots, DryBowserFullReplacement.
    #
    #   "partial_costume" — Costume swap that inherits face/skin/hair
    #                       from vanilla. Ships only the changed pieces
    #                       (clothes, mask, accessories). No matl. Uses
    #                       generic short labels (body/skin/hair/heir).
    #                       Face-region meshes can't be textured — they
    #                       expect vanilla textures we don't have. Force
    #                       skin/hair to vanilla-expectation defaults.
    #                       Example: Mr. L (Luigi).
    #
    #   "partial_body"   — Character/body swap (Funky Kong style). No
    #                      matl, but ships its own dominant body atlas.
    #                      Map body label → that atlas via semantic
    #                      prefix family. Example: DonkeyKong_FunkyKong.
    #
    #   "art_style"      — Style mod where textures are intentionally
    #                      tiny/single-color (16×16 swatches) and visual
    #                      detail comes from SSBU's normal-mapped PBR.
    #                      We can't replicate that shader; rendering is
    #                      necessarily flat. Example: Art & Style Kirby.
    #
    #   "unknown"        — Doesn't match any pattern; fall through to
    #                      best-effort heuristic ladder.
    GENERIC_LABELS = frozenset((
        "body", "skin", "hair", "heir", "alp", "def",
        "head", "tongue", "mouth", "",
    ))

    def _classify_mod():
        has_matl = numatb is not None
        labels_set = {label for (_, _), label in mesh_to_mat.items()}

        if not has_matl:
            # No matl ⇒ partial mod. Differentiate costume vs body by
            # whether the labels are *exclusively* the generic family.
            if labels_set and labels_set.issubset(GENERIC_LABELS):
                return "partial_costume"
            return "partial_body"

        # Has matl. Probe for art_style: when the body/skin atlas is
        # tiny (≲ 4 KB ≈ 16×16 single-color swatch), the mod relies on
        # normal-mapped PBR shading for visual detail — we can render
        # flat color but the actual SSBU shader detail is unreachable.
        try:
            for f in os.listdir(model_dir):
                fl = f.lower()
                if fl.startswith("skin_") and fl.endswith("_col.nutexb"):
                    try:
                        if os.path.getsize(os.path.join(model_dir, f)) < 4096:
                            return "art_style"
                    except OSError:
                        pass
        except OSError:
            pass

        return "full_textured"

    mod_type = _classify_mod()

    # Build material data. Two render paths:
    #   • "Textured" — UV-mapped pyrender material. Used for the body,
    #     clothing, hair, accessories — anything where Texture0 is a
    #     real diffuse map intended to be sampled with the mesh's UVs.
    #   • "Flat color" — single averaged color from the texture, no
    #     UV mapping. Used for eye/mouth/special-shader materials
    #     where the texture's UV layout is designed for SSBU's custom
    #     compositing shader (eye highlight overlays etc). Mapping
    #     those textures naively stretches them across whole faces.
    def _pick_color_tex(tex_map):
        # SSBU cel-shading pattern: ``Texture0`` is bound to a diffuse
        # that's intentionally near-black and the actual visible color
        # lives in ``Texture5`` (emissive). Some cel-shaded materials
        # even bind PRM/NOR textures to Texture0 by mistake. We only
        # accept a texture name in slots 0/1/14/5 if it looks like a
        # color map (``_col`` stem or no suffix). Texture5 is checked
        # last as the cel-shading rescue.
        def _looks_like_color(name):
            if not name or name.startswith("/common/"):
                return False
            stem = name.lower().rsplit("_", 1)
            if len(stem) == 2 and stem[1] in ("nor", "prm"):
                return False
            return True
        for slot in ("Texture0", "Texture1", "Texture14"):
            name = tex_map.get(slot, "")
            if _looks_like_color(name):
                return name
        # Cel-shading rescue: Texture5 (emissive) when nothing better.
        emi = tex_map.get("Texture5", "")
        if _looks_like_color(emi):
            return emi
        return None

    def _is_special_shader_material(label, tex_map):
        # Eye materials: SSBU shader composites multiple eye textures
        # into a moving iris with highlight overlay; we can't replicate
        # that, so render flat. BUT only flag as eye-special when the
        # matl actually references eye textures — some stylized mods
        # (e.g. Art & Style Kirby) reuse ``Eye*`` labels for ordinary
        # body materials with no textures bound, and flat-coloring
        # those gives random palette hues instead of body color.
        if label and label.lower().startswith("eye"):
            return any(name and not name.startswith("/common/")
                       for name in tex_map.values())
        # Texture0 bound to a `/common/shader/...` default means this
        # material uses a non-standard shader path and Texture0 isn't
        # the actual diffuse.
        t0 = tex_map.get("Texture0", "")
        if t0.startswith("/common/"):
            return True
        return False

    def _average_linear_color(img):
        """Downsample to 16×16 ignoring near-transparent pixels and
        return the *characteristic* linear-RGB color of the texture.

        Plain mean produces muddy tones because SSBU diffuse maps bake
        in heavy AO/shadows — the dark pixels drag the average toward
        brown for everything. Instead we take the mean of the brightest
        50% of pixels, which preserves the perceptually-dominant tone
        (white for bone textures, red for shells, etc.) while still
        averaging out per-pixel noise.
        """
        small = img.resize((16, 16), Image.BILINEAR)
        arr = np.asarray(small, dtype=np.float32)
        if arr.ndim != 3:
            return None
        if arr.shape[2] >= 4:
            mask = arr[..., 3] > 16.0
            pix = arr[mask][:, :3] if mask.any() else arr[..., :3].reshape(-1, 3)
        else:
            pix = arr[..., :3].reshape(-1, 3)
        if len(pix) == 0:
            return None
        # Drop the darkest half (shadows, AO, sub-surface darks). The
        # remaining pixels' mean is closer to the texture's "label" color.
        lum = pix.mean(axis=1)
        threshold = float(np.median(lum))
        bright = pix[lum >= threshold]
        if len(bright) == 0:
            bright = pix
        srgb = (bright.mean(axis=0) / 255.0).clip(0.0, 1.0)
        return tuple(_srgb_to_linear(float(c)) for c in srgb)

    mat_textures = {}      # label -> PIL.Image (RGBA), for textured path
    mat_normals = {}       # label -> PIL.Image (RGB normal map, glTF-style)
    mat_occlusions = {}    # label -> PIL.Image (RGB AO, R channel = occlusion)
    mat_emissions = {}     # label -> PIL.Image (RGB emissive, added via emi*0.5)
    mat_colors = {}        # label -> linear RGB triple, for flat path
    mat_prm_params = {}    # label -> (metallicFactor, roughnessFactor) from PRM

    def _occlusion_from_prm(prm_img):
        """Extract AO from SSBU PRM (B channel) into a glTF-style
        occlusionTexture (R channel). Returns ``None`` if the channel
        is uniform (no per-pixel detail to add)."""
        try:
            arr = np.asarray(prm_img)
            if arr.ndim != 3 or arr.shape[2] < 3:
                return None
            ao = arr[..., 2]
            if int(ao.max()) - int(ao.min()) < 10:
                return None  # flat AO, no value
            ao_rgb = np.stack([ao, ao, ao], axis=-1).astype(np.uint8)
            return Image.fromarray(ao_rgb, mode="RGB")
        except Exception:
            return None

    def _normal_map_for_pyrender(nor_img):
        """Convert an SSBU nor texture to a glTF-conformant normal map.

        SSBU encodes normals in BC5 (only RG components decoded by
        ultimate_tex_cli; B is unused/cavity, A is cavity per
        ssbh_wgpu). Convert to standard glTF normals where
        ``RGB = (Nx*0.5+0.5, Ny*0.5+0.5, Nz*0.5+0.5)`` with
        ``Nz = sqrt(max(1 - Nx² - Ny², 0))``.

        Returns an RGB PIL.Image. Returns ``None`` if the source
        looks flat (no surface variation in RG channels) — providing
        an identity normal map adds nothing and risks breaking
        unrelated rendering paths.
        """
        try:
            arr = np.asarray(nor_img, dtype=np.float32)
            if arr.ndim != 3 or arr.shape[2] < 2:
                return None
            r = arr[..., 0]
            g = arr[..., 1]
            # Flat-normal detection: if R or G barely vary, the texture
            # is a constant up-vector (no surface detail) — skip.
            if r.max() - r.min() < 8 and g.max() - g.min() < 8:
                return None
            nx = (r / 127.5) - 1.0
            ny = (g / 127.5) - 1.0
            nz = np.sqrt(np.clip(1.0 - nx * nx - ny * ny, 0.0, 1.0))
            b = ((nz + 1.0) * 127.5).clip(0, 255)
            out = np.stack([r, g, b], axis=-1).astype(np.uint8)
            return Image.fromarray(out, mode="RGB")
        except Exception:
            return None
    # Set later if synthesis needs it; closure refs in _ingest_material
    # require the name to exist before the matl-pass calls run.
    fallback_body_color = None
    art_style_default_color = None

    # Hardcoded vanilla-expectation defaults for skin/hair/etc. labels.
    # Used by _ingest_material when a label has no matching texture in
    # its family — partial mods commonly inherit these regions from
    # vanilla, which we can't access. sRGB; gamma-converted on use.
    _SEMANTIC_DEFAULT_SRGB = {
        "skin": (0.93, 0.78, 0.62),  # warm peach
        "head": (0.93, 0.78, 0.62),
        "hair": (0.30, 0.20, 0.12),  # brown
        "heir": (0.30, 0.20, 0.12),
        "tongue": (0.75, 0.40, 0.40),
        "mouth":  (0.75, 0.40, 0.40),
    }

    def _ingest_material(label, tex_map):
        special = _is_special_shader_material(label, tex_map)
        tex_name = _pick_color_tex(tex_map)
        # Resolve to an actual file. If the matl-referenced texture
        # isn't shipped (or the matl entry has no Texture0 at all),
        # walk progressively looser fallbacks rooted in SSBU naming
        # convention so we don't drop a whole material to flat color
        # because of one missing file.
        nutexb = (_resolve_nutexb_for_texture(model_dir, tex_name)
                  if tex_name else None)
        if nutexb is None and label:
            # 1. ``<label>_col`` — runtime convention many shader
            #    variants use even when the matl entry is empty.
            #    Rescues e.g. Kirby's ``skin_kirby_001`` whose matl
            #    references only the normal map.
            convention = label + "_col"
            nutexb = _resolve_nutexb_for_texture(model_dir, convention)
            if nutexb:
                tex_name = convention
        if nutexb is None and label:
            # 2. Sister-texture fallback. Matl referenced
            #    ``skin_cloud_001_col`` but only ``skin_cloud_002_col``
            #    is shipped → use the sister; same character, same
            #    body-part family, so the tone is right.
            tokens = (label or "").split("_")
            for prefix_len in range(len(tokens), 1, -1):
                prefix = "_".join(tokens[:prefix_len]) + "_"
                try:
                    sisters = sorted(
                        f for f in os.listdir(model_dir)
                        if f.lower().startswith(prefix.lower())
                        and f.lower().endswith("_col.nutexb"))
                except OSError:
                    sisters = []
                if sisters:
                    nutexb = os.path.join(model_dir, sisters[0])
                    tex_name = os.path.splitext(sisters[0])[0]
                    break
        img = None
        if nutexb:
            img = _decode_nutexb_to_image(nutexb)
        # Cel-shading content rescue: SSBU cel-shaded materials bind
        # an *intentionally near-black* diffuse to Texture0; the real
        # color lives in Texture5 (emissive). Texture0's filename
        # ends in ``_col`` so the priority pick can't tell — but the
        # decoded content can. If the chosen image is overwhelmingly
        # dark, prefer Texture5's content instead.
        if img is not None:
            try:
                small = img.resize((4, 4), Image.BILINEAR)
                arr = np.asarray(small, dtype=np.float32)
                if arr.ndim == 3:
                    luminance = arr[..., :3].mean()
                    if luminance < 18.0:  # essentially black
                        emi_name = tex_map.get("Texture5", "")
                        if (emi_name and not emi_name.startswith("/common/")
                                and emi_name != tex_name):
                            emi_path = _resolve_nutexb_for_texture(
                                model_dir, emi_name)
                            if emi_path:
                                emi_img = _decode_nutexb_to_image(emi_path)
                                if emi_img is not None:
                                    img = emi_img
                                    tex_name = emi_name
            except Exception:
                pass
        # NOTE: We do NOT use PRM (Texture6) values to drive metallic/
        # roughness here. Per ssbh_wgpu, PRM is R=metallic, G=roughness,
        # B=AO, A·0.2=F0 — but those values assume SSBU's full IBL +
        # rim + SSS pipeline. In pyrender's vanilla PBR with no
        # environment lighting, applying SSBU's metallic faithfully
        # produces color-shifted output (Kirby's pink → olive). We
        # keep the conservative defaults.

        # Resolve normal map (Texture4 = `_nor`) and reconstruct Z
        # from BC5-style XY-only encoding so pyrender's normal mapping
        # gets a glTF-conformant input. Skipped silently when the
        # source is a flat normal (no surface variation).
        if img is not None and not special:
            nor_name = tex_map.get("Texture4", "")
            if (not nor_name or nor_name.startswith("/common/")
                    or not nor_name.lower().endswith("_nor")):
                if tex_name and tex_name.lower().endswith("_col"):
                    nor_name = tex_name[:-len("_col")] + "_nor"
                else:
                    nor_name = ""
            if nor_name:
                nor_path = _resolve_nutexb_for_texture(model_dir, nor_name)
                if nor_path:
                    raw_nor = _decode_nutexb_to_image(nor_path)
                    if raw_nor is not None:
                        gltf_nor = _normal_map_for_pyrender(raw_nor)
                        if gltf_nor is not None:
                            mat_normals[label] = gltf_nor

            # Emissive (Texture5) — per ssbh_wgpu's final color formula
            # ``outColor = diffuse + specular*AO + emission*0.5``,
            # emission is *additive*, not a substitute for the diffuse.
            # We pass it as emissiveTexture so pyrender adds it on top
            # of the lit color. Visible on cel-shaded armor edges,
            # glowing details, neon accents.
            emi_name = tex_map.get("Texture5", "")
            if emi_name and not emi_name.startswith("/common/"):
                # Skip when Texture5 is identical to Texture0 (some
                # SFXPBS shaders bind it as a same-channel duplicate;
                # adding it doubles the diffuse contribution).
                if emi_name != tex_name:
                    emi_path = _resolve_nutexb_for_texture(model_dir, emi_name)
                    if emi_path:
                        emi_img = _decode_nutexb_to_image(emi_path)
                        if emi_img is not None:
                            mat_emissions[label] = emi_img

            # Resolve PRM and extract AO (B channel) → occlusionTexture.
            # SSBU bakes ambient occlusion into PRM's blue channel; per
            # glTF spec, occlusionTexture's red channel controls lighting
            # attenuation, so we shuffle B→R. Visible improvement is
            # darkening in mesh creases / under-armor regions.
            prm_name = tex_map.get("Texture6", "")
            if (not prm_name or prm_name.startswith("/common/")
                    or not prm_name.lower().endswith("_prm")):
                if tex_name and tex_name.lower().endswith("_col"):
                    prm_name = tex_name[:-len("_col")] + "_prm"
                else:
                    prm_name = ""
            if prm_name:
                prm_path = _resolve_nutexb_for_texture(model_dir, prm_name)
                if prm_path:
                    prm_img = _decode_nutexb_to_image(prm_path)
                    if prm_img is not None:
                        ao_img = _occlusion_from_prm(prm_img)
                        if ao_img is not None:
                            mat_occlusions[label] = ao_img

        if img is not None and not special:
            mat_textures[label] = img
        else:
            color = (_average_linear_color(img)
                     if img is not None else None)
            if color is None:
                # art_style mods: stylized labels (Eye* used for body)
                # should all share the body atlas's color, not random
                # palette hues.
                if art_style_default_color is not None:
                    color = art_style_default_color
                else:
                    # Skin/hair/etc. labels with no matching texture
                    # fall to a vanilla-expectation default tone
                    # rather than ``fallback_body_color`` which may
                    # be a mask/accessory in partial mods.
                    semantic_default = _SEMANTIC_DEFAULT_SRGB.get(
                        (label or "").lower())
                    if semantic_default is not None:
                        color = tuple(_srgb_to_linear(c) for c in semantic_default)
                    elif fallback_body_color is not None:
                        color = fallback_body_color
                    else:
                        color = _hls_to_linear(
                            hash(tex_name or label) + slot_offset,
                            0.45, 0.40)
            mat_colors[label] = color

    if numatb:
        try:
            matl = ssbh_data_py.matl_data.read_matl(numatb)
            for entry in matl.entries:
                tex_map = {t.param_id.name: t.data
                           for t in entry.textures}
                _ingest_material(entry.material_label, tex_map)
        except Exception:
            pass

    # Some mods (partial skin packs) ship only .numshb + .numdlb +
    # a handful of `_col.nutexb` textures, with no .numatb at all —
    # ARCropolis fills the materials from the vanilla game at runtime,
    # but we don't have access to those. Synthesize materials from
    # the textures present in the slot dir, matched to the labels the
    # .numdlb references.
    #
    # Naming-convention guess (material ``skin_donkey_001`` →
    # ``skin_donkey_001_col.nutexb``) works for some mods, but other
    # partial mods reference shader-style labels like ``body`` /
    # ``ShaderfxShader7`` that don't match any texture name. Fall back
    # to the *largest* ``_col.nutexb`` in the slot dir as a default
    # body atlas — partial mods typically ship one big skin atlas plus
    # small accessory textures, so size disambiguates them reliably.
    def _default_body_texture():
        candidates = []
        try:
            for f in os.listdir(model_dir):
                if f.lower().endswith("_col.nutexb"):
                    p = os.path.join(model_dir, f)
                    try:
                        candidates.append((os.path.getsize(p), f))
                    except OSError:
                        pass
        except OSError:
            return None
        if not candidates:
            return None
        candidates.sort(reverse=True)  # largest first
        # Return the texture stem (without .nutexb) so it flows
        # through _resolve_nutexb_for_texture cleanly.
        return os.path.splitext(candidates[0][1])[0]

    # Semantic-prefix mapping for partial mods that ship .numdlb with
    # short generic labels (``body``, ``skin``, ``heir``) and no matl.
    # SSBU's vanilla naming convention pairs material regions with
    # texture filename prefixes; partial mods that override clothing
    # but inherit vanilla skin/hair don't ship matching textures, so
    # we walk the prefix family before falling through to "any largest
    # _col." This prevents a face texture (``def_luigi_001_col``) from
    # being stretched over the body mesh ("tie dye") when the actual
    # body texture (``alp_luigi_001_col``) is right there.
    _SEMANTIC_PREFIXES = {
        # ``body`` falls through to ``skin_*`` because some mods
        # (e.g. Funky Kong) store the whole body atlas under
        # ``skin_<char>_001_col`` and reference it as ``body``.
        "body": ("body_", "alp_", "skin_"),
        "alp":  ("alp_", "body_"),
        "skin": ("skin_",),
        "hair": ("hair_",),
        "heir": ("hair_",),       # common misspelling
        "def":  ("def_", "skin_"),
        "head": ("def_", "head_"),
        "tongue": ("tongue_", "mouth_"),
        "mouth":  ("mouth_", "tongue_"),
    }

    def _semantic_match(label):
        prefixes = _SEMANTIC_PREFIXES.get((label or "").lower())
        if not prefixes:
            return None
        try:
            files = os.listdir(model_dir)
        except OSError:
            return None
        for prefix in prefixes:
            matches = sorted(
                f for f in files
                if f.lower().startswith(prefix)
                and f.lower().endswith("_col.nutexb"))
            if matches:
                return os.path.splitext(matches[0])[0]
        return None

    # The face-mesh override (force face-region meshes to peach skin
    # default) only makes sense for ``partial_costume`` mods like
    # Mr. L, where the .numdlb reuses ``body`` for both clothing AND
    # face/mask meshes and the mod's body texture would tie-dye the
    # face. For ``partial_body`` mods like Funky Kong the body atlas
    # IS the gorilla face texture and forcing peach would be wrong.
    apply_face_override = (mod_type == "partial_costume")


    # For ``art_style`` mods (Kirby with 16×16 single-pink swatches),
    # the visual identity is a single body color — every untextured
    # material should pick up that color, not a synthetic FALLBACK_HUES
    # rotation that produces blue/cyan/etc on body parts.
    if mod_type == "art_style":
        try:
            for f in sorted(os.listdir(model_dir)):
                fl = f.lower()
                if fl.startswith("skin_") and fl.endswith("_col.nutexb"):
                    p = os.path.join(model_dir, f)
                    img_tmp = _decode_nutexb_to_image(p)
                    if img_tmp is not None:
                        # Plain mean (not bright-half) since these are
                        # already single-tone swatches — the median
                        # filter would just discard the texture.
                        small = img_tmp.resize((4, 4), Image.BILINEAR)
                        arr = np.asarray(small, dtype=np.float32)
                        if arr.ndim == 3:
                            srgb = (arr[..., :3].reshape(-1, 3).mean(axis=0)
                                    / 255.0).clip(0.0, 1.0)
                            art_style_default_color = tuple(
                                _srgb_to_linear(float(c)) for c in srgb)
                            break
        except OSError:
            pass

    referenced_labels = {label for (_, _), label in mesh_to_mat.items()}
    fallback_body_tex = None
    needs_fallback = any(
        label and label not in mat_textures and label not in mat_colors
        for label in referenced_labels)
    if needs_fallback:
        fallback_body_tex = _default_body_texture()
        # Compute average color of the largest body atlas — used as a
        # plausible flat-color stand-in for labels whose family has no
        # matching texture (e.g. Mr. L's ``skin``/``heir`` when the
        # mod doesn't ship vanilla skin textures). Picks up the mod's
        # character-palette tone instead of a synthetic FALLBACK_HUES
        # purple/cyan that doesn't match the rest of the model.
        if fallback_body_tex:
            nutexb = _resolve_nutexb_for_texture(model_dir, fallback_body_tex)
            if nutexb:
                img = _decode_nutexb_to_image(nutexb)
                if img is not None:
                    fallback_body_color = _average_linear_color(img)

    for label in referenced_labels:
        if label in mat_textures or label in mat_colors:
            continue
        candidate = (label or "").strip()
        if not candidate:
            continue
        # 1. Try ``<label>_col`` and ``<label>`` directly.
        tex_name = None
        if _resolve_nutexb_for_texture(model_dir, candidate + "_col"):
            tex_name = candidate + "_col"
        elif _resolve_nutexb_for_texture(model_dir, candidate):
            tex_name = candidate
        # 2. Semantic-prefix map for short generic labels.
        if tex_name is None:
            tex_name = _semantic_match(candidate)
        # 3. Fall back to the slot's largest body atlas — but ONLY
        #    for labels that aren't in the semantic map. A label like
        #    ``skin`` whose family ``skin_*`` had no match means the
        #    mod doesn't ship a skin atlas; falling to a non-skin
        #    texture (e.g. ``def_luigi_001_col`` face atlas) just
        #    smears it across the body geometry with wrong UVs
        #    ("tie dye"). For genuinely unknown labels (e.g.
        #    ``ShaderfxShader7``) the fallback is still the best guess.
        if (tex_name is None and fallback_body_tex
                and candidate.lower() not in _SEMANTIC_PREFIXES):
            tex_name = fallback_body_tex
        synth_tex_map = {"Texture0": tex_name} if tex_name else {}
        _ingest_material(label, synth_tex_map)

    FALLBACK_HUES = (20, 210, 95, 320, 50, 175)

    scene = pyrender.Scene(
        bg_color=np.array([0.11, 0.11, 0.18, 1.0]),
        ambient_light=np.array([0.40, 0.40, 0.42, 1.0]))

    # Cache pyrender Texture objects per source image — many materials
    # reuse the same diffuse map (skin, metal sheets, etc.) and we
    # don't want to upload the same image to GPU multiple times.
    pyrender_textures = {}
    has_alpha_cache = {}

    def _image_has_alpha(img):
        cached = has_alpha_cache.get(id(img))
        if cached is not None:
            return cached
        if img.mode != "RGBA":
            has_alpha_cache[id(img)] = False
            return False
        try:
            lo, _hi = img.getchannel("A").getextrema()
            result = lo < 250
        except Exception:
            result = True
        has_alpha_cache[id(img)] = result
        return result

    def _make_textured_material(img, prm_params=None,
                                normal_img=None, ao_img=None,
                                emi_img=None):
        tex = pyrender_textures.get(id(img))
        if tex is None:
            tex = pyrender.Texture(source=img, source_channels="RGBA")
            pyrender_textures[id(img)] = tex
        metallic, roughness = (prm_params if prm_params else (0.05, 0.75))
        kwargs = dict(
            baseColorTexture=tex,
            metallicFactor=metallic, roughnessFactor=roughness,
            doubleSided=True)
        if _image_has_alpha(img):
            kwargs["alphaMode"] = "MASK"
            kwargs["alphaCutoff"] = 0.5
        if normal_img is not None:
            ntex = pyrender_textures.get(id(normal_img))
            if ntex is None:
                ntex = pyrender.Texture(
                    source=normal_img, source_channels="RGB")
                pyrender_textures[id(normal_img)] = ntex
            kwargs["normalTexture"] = ntex
        if ao_img is not None:
            otex = pyrender_textures.get(id(ao_img))
            if otex is None:
                otex = pyrender.Texture(
                    source=ao_img, source_channels="RGB")
                pyrender_textures[id(ao_img)] = otex
            kwargs["occlusionTexture"] = otex
        if emi_img is not None:
            etex = pyrender_textures.get(id(emi_img))
            if etex is None:
                etex = pyrender.Texture(
                    source=emi_img,
                    source_channels="RGBA" if emi_img.mode == "RGBA" else "RGB")
                pyrender_textures[id(emi_img)] = etex
            kwargs["emissiveTexture"] = etex
            # ssbh_wgpu adds emission*0.5 — match.
            kwargs["emissiveFactor"] = [0.5, 0.5, 0.5]
        return pyrender.MetallicRoughnessMaterial(**kwargs)

    def _make_flat_material(color, prm_params=None):
        metallic, roughness = (prm_params if prm_params else (0.05, 0.75))
        return pyrender.MetallicRoughnessMaterial(
            baseColorFactor=[color[0], color[1], color[2], 1.0],
            metallicFactor=metallic, roughnessFactor=roughness,
            doubleSided=True)

    # Pre-pass: filter SSBU's runtime-toggled visibility meshes.
    # Names ending ``_VIS_O_OBJShape`` are expression/animation variants
    # of the same body region — SSBU shows ONE of them at a time based
    # on facial-expression state. Without that gating logic we'd render
    # 18 overlapping ``Cloud_Mouth_<Expression>`` meshes and the result
    # is an opaque black blob over the face. Keep only one neutral
    # variant per group.
    _ACTIVE_STATE_TOKENS = (
        "_Talk", "_Attack", "_HeavyAttack", "_Ouch", "_HeavyOuch",
        "_Down", "_Ottotto", "_Furafura", "_Pattern", "_Voice",
        "_Hot", "_Bound", "_Eflame", "_Smash", "_Final",
        "_Harfblink", "_Halfblink", "_Blink",
        "_StepPose", "_Catch", "_Fall",
    )
    # Note: UP-suffix variants (FaceNUP, BoundUP, ...) are deliberately
    # NOT stripped — UP meshes are the *upper* face/region paired with
    # a separate non-UP lower mesh, not duplicates. Stripping merged
    # them and we'd drop one half of the face (e.g. lose the mustache).
    _STATE_WORDS = (
        "Result", "FaceN", "Talk", "Talk2", "Attack", "HeavyAttack",
        "Ouch", "HeavyOuch", "Down", "Ottotto", "Furafura",
        "PatternA", "PatternB", "PatternC", "PatternD",
        "VoiceA", "VoiceB", "VoiceC", "Hot", "Bound",
        "Eflame", "Smash", "Final",
        "Harfblink1", "Harfblink2", "Harfblink3", "Halfblink", "Blink",
    )

    def _vis_group_key(name):
        # ``Cloud_Mouth_Result_VIS_O_OBJShape`` → ``Cloud_Mouth``
        # Case-insensitive: different mods use ``FaceN`` vs ``faceN``.
        if "_VIS_O_" not in name:
            return None
        head = name.split("_VIS_O_")[0]
        head_low = head.lower()
        for word in _STATE_WORDS:
            suffix = "_" + word.lower()
            if head_low.endswith(suffix):
                head = head[: -len(suffix)]
                break
        return head.lower()

    def _matches_active_token(name):
        n_low = name.lower()
        return any(tok.lower() in n_low for tok in _ACTIVE_STATE_TOKENS)

    selected_in_group = {}  # group key → preferred mesh name
    for obj in mesh_data.objects:
        n = obj.name
        if "_VIS_O_" not in n:
            continue
        if _matches_active_token(n):
            continue
        key = _vis_group_key(n)
        if key is None:
            continue
        # Prefer ``_FaceN_`` (face-neutral), then ``_Result_``, then
        # whatever appears first. Case-insensitive comparison.
        n_low = n.lower()
        prev = selected_in_group.get(key)
        prev_low = (prev or "").lower()
        rank = (0 if "_facen_" in n_low
                else 1 if "_result_" in n_low else 2)
        prev_rank = (0 if "_facen_" in prev_low
                     else 1 if "_result_" in prev_low else 2)
        if prev is None or rank < prev_rank:
            selected_in_group[key] = n

    all_meshes = []
    fallback_idx = 0

    for obj in mesh_data.objects:
        if not obj.positions or len(obj.vertex_indices) < 3:
            continue
        # Apply the visibility filter to expression/animation variants.
        if "_VIS_O_" in obj.name:
            if _matches_active_token(obj.name):
                continue
            key = _vis_group_key(obj.name)
            if key in selected_in_group and selected_in_group[key] != obj.name:
                continue
        verts = np.asarray(obj.positions[0].data, dtype=np.float32)
        if verts.ndim != 2 or len(verts) == 0:
            continue
        if verts.shape[1] > 3:
            verts = verts[:, :3]
        indices = np.asarray(obj.vertex_indices, dtype=np.uint32)
        if len(indices) < 3:
            continue
        faces = indices.reshape(-1, 3)

        normals = None
        if obj.normals:
            ndata = np.asarray(obj.normals[0].data, dtype=np.float32)
            if ndata.ndim == 2 and len(ndata) == len(verts):
                normals = ndata[:, :3] if ndata.shape[1] > 3 else ndata

        # Apply parent-bone bind-pose transform for static-rigged
        # meshes. Skinned meshes (no parent_bone_name, populated
        # bone_influences) are already in model space.
        parent_bone = getattr(obj, "parent_bone_name", "") or ""
        if parent_bone and parent_bone in bone_world:
            M = bone_world[parent_bone]
            verts_h = np.column_stack(
                [verts, np.ones(len(verts), dtype=np.float32)])
            verts = (verts_h @ M)[:, :3].astype(np.float32, copy=False)
            if normals is not None:
                # Normals transform by the rotation block only (no
                # translation). For row-major p @ M we use M[:3, :3].
                normals = (normals @ M[:3, :3]).astype(np.float32, copy=False)

        uvs = None
        if obj.texture_coordinates:
            udata = np.asarray(obj.texture_coordinates[0].data,
                               dtype=np.float32)
            if udata.ndim == 2 and len(udata) == len(verts):
                uvs = udata[:, :2].copy()

        tangents = None
        if obj.tangents:
            tdata = np.asarray(obj.tangents[0].data, dtype=np.float32)
            if tdata.ndim == 2 and len(tdata) == len(verts):
                # SSBU tangents are (X, Y, Z, handedness) in glTF format.
                # pyrender accepts them as float32 (N, 4).
                tangents = tdata[:, :4].copy()

        # Trimesh kept around purely for bounds → camera framing.
        tm = trimesh.Trimesh(vertices=verts, faces=faces,
                             vertex_normals=normals, process=False)
        all_meshes.append(tm)

        key = (obj.name, obj.subindex)
        mat_label = mesh_to_mat.get(key, "")

        img = mat_textures.get(mat_label)
        # Face-region override — type-specific. Only fires for
        # ``partial_costume`` mods where the body atlas is clothing,
        # not skin. For ``partial_body`` mods (Funky Kong) the body
        # atlas IS the character's skin and we want it on the face.
        if apply_face_override and img is not None:
            n = obj.name.lower()
            is_face_mesh = (
                "face" in n or "_eye" in n or "head_" in n
                or "_talk" in n or "_ouch" in n or "_attack" in n
                or "_mouth" in n or "_voice" in n or "_blink" in n
                or "_pose" in n or "_hot" in n)
            if is_face_mesh:
                img = None  # fall through to skin-default flat color
                mat_label = "skin"  # so the semantic default fires

        if img is not None and uvs is not None:
            normal_img = mat_normals.get(mat_label)
            # Need tangents for normal mapping; pyrender derives TBN
            # from tangents + normals. Without tangents the normal
            # map is interpreted in an undefined frame and produces
            # garbage colors.
            if normal_img is not None and tangents is None:
                normal_img = None
            ao_img = mat_occlusions.get(mat_label)
            emi_img = mat_emissions.get(mat_label)
            material = _make_textured_material(
                img, mat_prm_params.get(mat_label),
                normal_img=normal_img, ao_img=ao_img,
                emi_img=emi_img)
            prim_kwargs = {
                "positions": verts,
                "indices": faces,
                "texcoord_0": uvs,
                "material": material,
            }
            if normals is not None:
                prim_kwargs["normals"] = normals
            if tangents is not None and normal_img is not None:
                prim_kwargs["tangents"] = tangents
            prim = pyrender.Primitive(**prim_kwargs)
            scene.add(pyrender.Mesh(primitives=[prim]))
        else:
            color = mat_colors.get(mat_label)
            if color is None:
                # art_style mods get their body color, not a hue rotation.
                if art_style_default_color is not None:
                    color = art_style_default_color
                else:
                    # Prefer per-label semantic defaults (peach for skin,
                    # brown for hair, etc.) — partial mods inherit these
                    # from vanilla and we can't access vanilla.
                    semantic_default = _SEMANTIC_DEFAULT_SRGB.get(
                        (mat_label or "").lower())
                    if semantic_default is not None:
                        color = tuple(_srgb_to_linear(c) for c in semantic_default)
                    elif fallback_body_color is not None:
                        color = fallback_body_color
                    else:
                        base_h = FALLBACK_HUES[fallback_idx % len(FALLBACK_HUES)]
                        color = _hls_to_linear(base_h + slot_offset, 0.45, 0.40)
                        fallback_idx += 1
            scene.add(pyrender.Mesh.from_trimesh(
                tm, material=_make_flat_material(
                    color, mat_prm_params.get(mat_label))))

    if not all_meshes:
        return None, None

    combined = trimesh.util.concatenate(all_meshes)
    return scene, combined


def _add_camera_and_lights(scene, combined):
    """Add auto-framing camera and lights to a pyrender scene."""
    bounds = combined.bounds
    center = (bounds[0] + bounds[1]) / 2.0
    extent = float(np.linalg.norm(bounds[1] - bounds[0]))
    dist = extent * 1.15
    cam_pos = center + np.array([0.0, extent * 0.05, dist])

    fwd = center - cam_pos
    fwd /= np.linalg.norm(fwd)
    world_up = np.array([0.0, 1.0, 0.0])
    right = np.cross(fwd, world_up)
    right /= (np.linalg.norm(right) + 1e-8)
    up = np.cross(right, fwd)

    cam_pose = np.eye(4)
    cam_pose[:3, 0] = right
    cam_pose[:3, 1] = up
    cam_pose[:3, 2] = -fwd
    cam_pose[:3, 3] = cam_pos
    scene.add(pyrender.PerspectiveCamera(yfov=np.pi / 4.0), pose=cam_pose)

    scene.add(pyrender.DirectionalLight(color=[1.0, 1.0, 1.0],
                                        intensity=3.0), pose=cam_pose)
    fill_pose = cam_pose.copy()
    fill_pose[:3, 3] = center - np.array([dist * 0.5,
                                           -extent * 0.2,
                                           dist * 0.3])
    scene.add(pyrender.DirectionalLight(color=[0.4, 0.45, 0.6],
                                        intensity=1.5), pose=fill_pose)


def _try_ssbh_render(model_dir, width, height, cache_path):
    """Render via the Rust ssbh_wgpu CLI if it's been built. Returns a
    PIL.Image on success, None on failure (caller falls back to pyrender).

    The Rust binary uses the same shader graph as ssbh_editor, so its
    output is much closer to "ground truth" than our pyrender approx.

    Quiet-fails when ``model_dir`` doesn't exist (frequent benign case
    where the caller probed a slot the mod doesn't ship). All other
    failures DO print so we can spot regressions where the binary is
    erroring out and we're silently falling back to the inferior
    pyrender path.
    """
    if not os.path.isfile(SSBH_RENDER_EXE):
        return None
    if not HAS_PIL:
        return None
    if not model_dir or not os.path.isdir(model_dir):
        return None
    try:
        import subprocess
        result = subprocess.run(
            [SSBH_RENDER_EXE, model_dir, cache_path,
             "--width", str(width), "--height", str(height)],
            capture_output=True, text=True, timeout=60,
            creationflags=0x08000000)  # CREATE_NO_WINDOW
        if result.returncode != 0:
            err = result.stderr.strip()[:200]
            if "Model dir not found" not in err:
                print(f"  ssbh_render failed (rc={result.returncode}) "
                      f"for {model_dir}: {err}", file=sys.stderr)
            return None
        if not os.path.isfile(cache_path):
            print(f"  ssbh_render produced no output for {model_dir}",
                  file=sys.stderr)
            return None
        return Image.open(cache_path).copy()
    except Exception as exc:
        print(f"  ssbh_render error: {exc}", file=sys.stderr)
        return None


def render_model_preview(model_dir, width=320, height=240):
    """Read SSBH model files from *model_dir*, render a static
    front-facing preview and return a :class:`PIL.Image.Image`.
    Returns ``None`` if rendering fails or deps are missing.

    Prefers the Rust ssbh_render binary (ssbh_wgpu-based, ssbh-editor
    quality) when built; falls back to our pyrender approximation.

    Results are cached as PNG under :data:`RENDER_CACHE_DIR` keyed
    by the *model_dir* path so repeat calls are instant.
    """
    if not HAS_PIL:
        return None

    import hashlib
    # Cache key includes the path AND a fingerprint of the model's
    # actual contents (numshb + numatb + numdlb mtime/size). Without
    # the fingerprint, deleting and re-extracting the mod cache
    # produces identical paths but possibly different contents, and
    # we'd serve stale PNGs from buggy intermediate renders. Include
    # a renderer-version tag too so improvements to the shader/scene
    # invalidate cleanly without manual cache-clear.
    sig_parts = [model_dir]
    try:
        for fname in sorted(os.listdir(model_dir)):
            if fname.lower().endswith((".numshb", ".numatb",
                                         ".numdlb", ".nusktb")):
                p = os.path.join(model_dir, fname)
                try:
                    st = os.stat(p)
                    sig_parts.append(
                        f"{fname}:{st.st_size}:{int(st.st_mtime)}")
                except OSError:
                    pass
    except OSError:
        pass
    sig_parts.append("rv6")  # bump on renderer-behavior changes
    cache_key = hashlib.sha256(
        "\n".join(sig_parts).encode()).hexdigest()[:16]
    os.makedirs(RENDER_CACHE_DIR, exist_ok=True)

    # ── Preferred path: ssbh_wgpu via Rust CLI (when built) ──
    ssbh_cache_path = os.path.join(
        RENDER_CACHE_DIR, f"sw_{cache_key}_{width}x{height}.png")
    if os.path.isfile(ssbh_cache_path):
        try:
            return Image.open(ssbh_cache_path).copy()
        except Exception:
            pass
    img = _try_ssbh_render(model_dir, width, height, ssbh_cache_path)
    if img is not None:
        return img

    # ── Fallback: pyrender PBR approximation ──
    if not HAS_3D_RENDER:
        return None
    cache_path = os.path.join(RENDER_CACHE_DIR,
                              f"v37_{cache_key}_{width}x{height}.png")
    if os.path.isfile(cache_path):
        try:
            return Image.open(cache_path).copy()
        except Exception:
            pass

    scene, combined = _build_colored_scene(model_dir)
    if scene is None:
        return None

    _add_camera_and_lights(scene, combined)

    try:
        renderer = pyrender.OffscreenRenderer(width, height)
        color, _depth = renderer.render(scene)
        renderer.delete()
    except Exception as exc:
        print(f"  OffscreenRenderer failed for {model_dir}: {exc}",
              file=sys.stderr)
        return None

    img = Image.fromarray(color)
    try:
        img.save(cache_path)
    except Exception:
        pass
    return img


def github_latest_asset(repo, asset_filter):
    """Get the latest release asset from GitHub matching asset_filter(name)->bool.
    Returns dict with version, url, filename, size, published — or None."""
    if not HAS_REQUESTS:
        return None
    try:
        url = f"https://api.github.com/repos/{repo}/releases/latest"
        resp = requests.get(url, verify=False, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        body = data.get("body", "") or ""
        for asset in data.get("assets", []):
            if asset_filter(asset["name"]):
                return {
                    "version": data["tag_name"],
                    "url": asset["browser_download_url"],
                    "filename": asset["name"],
                    "size": asset.get("size", 0),
                    "published": data.get("published_at", ""),
                    "body": body,
                }
    except Exception:
        pass
    return None


def _fetch_all_latest_versions():
    """Fetch latest version info for every component from GitHub.
    Returns dict keyed by GITHUB_REPOS key -> github_latest_asset result (or None)."""
    results = {}
    for key, (repo, filt) in GITHUB_REPOS.items():
        results[key] = github_latest_asset(repo, filt)
    return results


def github_prerelease_asset(repo, asset_filter):
    """Get the newest pre-release asset from GitHub matching asset_filter(name)->bool.
    Checks all releases (not just 'latest') and returns the most recent pre-release.
    Returns dict with version, url, filename, size, published, prerelease — or None."""
    if not HAS_REQUESTS:
        return None
    try:
        url = f"https://api.github.com/repos/{repo}/releases?per_page=15"
        resp = requests.get(url, verify=False, timeout=30)
        resp.raise_for_status()
        for release in resp.json():
            if not release.get("prerelease") and not release.get("draft"):
                continue  # skip stable releases
            body = release.get("body", "") or ""
            for asset in release.get("assets", []):
                if asset_filter(asset["name"]):
                    return {
                        "version": release["tag_name"],
                        "url": asset["browser_download_url"],
                        "filename": asset["name"],
                        "size": asset.get("size", 0),
                        "published": release.get("published_at", ""),
                        "body": body,
                        "prerelease": True,
                    }
    except Exception:
        pass
    return None


def github_branch_asset(repo, branch, asset_filter):
    """Get the latest GitHub Actions artifact from a branch.
    Falls back to checking for branch-tagged pre-releases.
    Returns dict with version, url, filename — or None."""
    if not HAS_REQUESTS:
        return None
    # Strategy 1: check for pre-releases whose tag or target matches the branch
    try:
        url = f"https://api.github.com/repos/{repo}/releases?per_page=30"
        resp = requests.get(url, verify=False, timeout=30)
        resp.raise_for_status()
        for release in resp.json():
            commitish = release.get("target_commitish", "")
            tag = release.get("tag_name", "")
            if branch not in commitish and branch not in tag:
                continue
            body = release.get("body", "") or ""
            for asset in release.get("assets", []):
                if asset_filter(asset["name"]):
                    return {
                        "version": f"{tag} ({branch})",
                        "url": asset["browser_download_url"],
                        "filename": asset["name"],
                        "size": asset.get("size", 0),
                        "published": release.get("published_at", ""),
                        "body": body,
                        "prerelease": True,
                        "branch": branch,
                    }
    except Exception:
        pass
    return None


def _resolve_unofficial_atmosphere(asset_filter):
    """Try all unofficial sources in priority order.
    Returns (info_dict, source_label) or (None, None).
    Priority: local override > fork pre-release > official pre-release > official branch."""
    # 1. Local override zip (not from master branch)
    local_ov = find_local_atmosphere_override()
    if local_ov:
        return local_ov, "local override"

    # 2. User's fork (if configured) — pre-releases published by CI workflow
    if UNOFFICIAL_ATMOSPHERE_FORK:
        info = github_prerelease_asset(UNOFFICIAL_ATMOSPHERE_FORK, asset_filter)
        if info:
            return info, f"fork ({UNOFFICIAL_ATMOSPHERE_FORK})"
        info = github_branch_asset(
            UNOFFICIAL_ATMOSPHERE_FORK, ATMOSPHERE_SUPPORT_BRANCH, asset_filter)
        if info:
            return info, f"fork branch ({UNOFFICIAL_ATMOSPHERE_FORK})"

    # 3. Official repo pre-release
    info = github_prerelease_asset("Atmosphere-NX/Atmosphere", asset_filter)
    if info:
        return info, "official pre-release"

    # 4. Official repo branch-tagged release
    info = github_branch_asset(
        "Atmosphere-NX/Atmosphere", ATMOSPHERE_SUPPORT_BRANCH, asset_filter)
    if info:
        return info, f"official branch ({ATMOSPHERE_SUPPORT_BRANCH})"

    return None, None


def find_local_atmosphere_override():
    """Check LOCAL_ATMOSPHERE_DIR for a manually-placed unofficial Atmosphere zip.
    Skips zips that match an official release (contain '-master-' in the name).
    Returns dict with version, path, filename — or None."""
    if not os.path.isdir(LOCAL_ATMOSPHERE_DIR):
        return None
    best = None
    for f in os.listdir(LOCAL_ATMOSPHERE_DIR):
        if f.startswith("atmosphere-") and f.endswith(".zip") and "WITHOUT" not in f.upper():
            # Skip official release zips: they contain '-master-' in the filename
            # Unofficial/branch builds contain the branch name instead, e.g.
            # atmosphere-1.10.2-22_support-b108318996+...
            if "-master-" in f:
                continue
            path = os.path.join(LOCAL_ATMOSPHERE_DIR, f)
            mtime = os.path.getmtime(path)
            # Parse version from filename: atmosphere-X.Y.Z-branch-commit+...
            m = re.match(r'atmosphere-([\d.]+(?:-[^+]+)?)', f)
            version = m.group(1) if m else "unknown"
            entry = {
                "version": version,
                "path": path,
                "filename": f,
                "size": os.path.getsize(path),
                "mtime": mtime,
                "local": True,
            }
            if best is None or mtime > best["mtime"]:
                best = entry
    return best


def find_local_fusee_override():
    """Check LOCAL_ATMOSPHERE_DIR for a manually-placed fusee.bin.
    Returns path or None."""
    for name in ("fusee.bin",):
        path = os.path.join(LOCAL_ATMOSPHERE_DIR, name)
        if os.path.isfile(path):
            return path
    return None


# ─────────────────────────────────────────────────────────
#  EXTRACT + INSTALL LOGIC
# ─────────────────────────────────────────────────────────

def extract_archive(filepath, dest_dir):
    """Extract an archive (.zip/.7z/.rar) to dest_dir.

    Raises RuntimeError if extraction completes but writes zero
    files — that's a silent failure case (encrypted archive,
    corrupt download, or archive containing only empty dirs) that
    callers definitely want to know about before they trust the
    cache state.
    """
    pre_count = sum(len(fs) for _, _, fs in os.walk(dest_dir)) \
        if os.path.isdir(dest_dir) else 0
    ext = os.path.splitext(filepath)[1].lower()
    if ext == ".zip":
        with zipfile.ZipFile(filepath, "r") as zf:
            zf.extractall(dest_dir)
    elif ext == ".7z":
        if HAS_PY7ZR:
            import py7zr as _p
            with _p.SevenZipFile(filepath, mode="r") as z:
                z.extractall(dest_dir)
        else:
            raise RuntimeError("py7zr not installed (pip install py7zr)")
    elif ext == ".rar":
        if HAS_RARFILE:
            unrar_paths = [r"C:\Program Files\WinRAR\UnRAR.exe",
                           r"C:\Program Files (x86)\WinRAR\UnRAR.exe"]
            for p in unrar_paths:
                if os.path.exists(p):
                    rarfile.UNRAR_TOOL = p
                    break
            with rarfile.RarFile(filepath, "r") as rf:
                rf.extractall(dest_dir)
        else:
            raise RuntimeError("rarfile not installed (pip install rarfile)")
    else:
        raise RuntimeError(f"Unknown archive format: {ext}")
    # Catch silent failures — extraction "succeeded" but produced
    # nothing useful.
    post_count = sum(len(fs) for _, _, fs in os.walk(dest_dir))
    if post_count <= pre_count:
        raise RuntimeError(
            f"Archive '{os.path.basename(filepath)}' extracted but "
            "produced no new files (corrupt / encrypted / empty?)")


# ─────────────────────────────────────────────────────────
#  RCM PAYLOAD INJECTION HELPERS
# ─────────────────────────────────────────────────────────

def find_rcm_smash():
    """Locate TegraRcmSmash.exe. Returns path or None."""
    for p in RCM_SMASH_SEARCH_PATHS:
        if os.path.isfile(p):
            return p
    return None


def find_payload():
    """Locate the best fusee/payload .bin file. Returns path or None."""
    for p in PAYLOAD_SEARCH_PATHS:
        if os.path.isfile(p):
            return p
    return None


def inject_payload(smash_exe, payload_path):
    """Run TegraRcmSmash.exe to inject a payload.

    Returns (success: bool, message: str, return_code: int).

    Strategy:
      1. If we're already admin, run TegraRcmSmash directly. No elevation
         dance, no prompts, no chance of "canceled by the user".
      2. If UAC is fully disabled (EnableLUA=0) we can't elevate at all
         from a non-elevated process — bail out and tell the user to
         relaunch via Run_As_Admin.bat.
      3. Otherwise, elevate via ShellExecuteEx (SEE_MASK_NOCLOSEPROCESS)
         and wait on the process handle directly. This is the documented
         Win32 path; it doesn't go through PowerShell, so it's not
         affected by ExecutionPolicy / signed-script enforcement that
         can make `Start-Process -Verb RunAs` fail with the bogus
         "operation was canceled by the user" error.
    """
    import subprocess
    import ctypes
    from ctypes import wintypes

    if not os.path.isfile(smash_exe):
        return False, f"TegraRcmSmash.exe not found at:\n{smash_exe}", -100
    if not os.path.isfile(payload_path):
        return False, f"Payload file not found at:\n{payload_path}", -101

    # ── 1. Already elevated? Run directly. ──
    already_admin = False
    try:
        already_admin = bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        already_admin = False

    print(f"  [inject] already_admin={already_admin}")

    if already_admin:
        try:
            result = subprocess.run(
                [smash_exe, payload_path],
                capture_output=True, text=True, timeout=60,
                creationflags=0x08000000,  # CREATE_NO_WINDOW
            )
            rc = result.returncode
            if rc > 0x7FFFFFFF:
                rc = rc - 0x100000000
            return _interpret_rcm_rc(rc)
        except subprocess.TimeoutExpired:
            return False, "Timed out waiting for injection (60s)", -99
        except Exception as e:
            return False, f"Error running TegraRcmSmash: {e}", -98

    # ── 2. UAC fully off? Then we can't elevate at all. ──
    enable_lua = 1
    try:
        import winreg
        with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System") as k:
            enable_lua = winreg.QueryValueEx(k, "EnableLUA")[0]
    except Exception:
        pass
    print(f"  [inject] EnableLUA={enable_lua}")

    if enable_lua == 0:
        return (False,
                "UAC is fully disabled (EnableLUA=0), so Windows cannot "
                "grant elevation to this process.\n\n"
                "Relaunch Smash Night via Run_As_Admin.bat — that .bat "
                "uses RunAs at launch time, which works even with UAC "
                "disabled.",
                -50)

    # ── 3. Elevate via ShellExecuteEx (Win32 native, no PowerShell). ──
    SEE_MASK_NOCLOSEPROCESS = 0x00000040
    SEE_MASK_NO_CONSOLE = 0x00008000
    SW_HIDE = 0
    INFINITE = 0xFFFFFFFF

    class SHELLEXECUTEINFOW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("fMask", ctypes.c_ulong),
            ("hwnd", wintypes.HWND),
            ("lpVerb", wintypes.LPCWSTR),
            ("lpFile", wintypes.LPCWSTR),
            ("lpParameters", wintypes.LPCWSTR),
            ("lpDirectory", wintypes.LPCWSTR),
            ("nShow", ctypes.c_int),
            ("hInstApp", wintypes.HINSTANCE),
            ("lpIDList", ctypes.c_void_p),
            ("lpClass", wintypes.LPCWSTR),
            ("hkeyClass", wintypes.HKEY),
            ("dwHotKey", wintypes.DWORD),
            ("hIconOrMonitor", wintypes.HANDLE),
            ("hProcess", wintypes.HANDLE),
        ]

    sei = SHELLEXECUTEINFOW()
    sei.cbSize = ctypes.sizeof(sei)
    sei.fMask = SEE_MASK_NOCLOSEPROCESS | SEE_MASK_NO_CONSOLE
    sei.hwnd = None
    sei.lpVerb = "runas"
    sei.lpFile = smash_exe
    sei.lpParameters = f'"{payload_path}"'
    sei.lpDirectory = os.path.dirname(smash_exe)
    sei.nShow = SW_HIDE

    shell32 = ctypes.windll.shell32
    shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(SHELLEXECUTEINFOW)]
    shell32.ShellExecuteExW.restype = wintypes.BOOL

    if not shell32.ShellExecuteExW(ctypes.byref(sei)):
        err = ctypes.GetLastError()
        # ERROR_CANCELLED = 1223 — user clicked No on the UAC prompt.
        if err == 1223:
            return (False,
                    "UAC elevation was canceled at the prompt.\n\n"
                    "Click 'Yes' on the UAC dialog to allow injection, "
                    "or relaunch via Run_As_Admin.bat to skip the "
                    "prompt entirely.",
                    -50)
        return (False,
                f"ShellExecuteEx failed (Win32 error {err}). "
                f"Try relaunching via Run_As_Admin.bat.",
                -50)

    # Wait for the elevated process and grab its exit code.
    kernel32 = ctypes.windll.kernel32
    kernel32.WaitForSingleObject(sei.hProcess, INFINITE)
    rc = wintypes.DWORD()
    kernel32.GetExitCodeProcess(sei.hProcess, ctypes.byref(rc))
    kernel32.CloseHandle(sei.hProcess)

    rc_signed = rc.value
    if rc_signed > 0x7FFFFFFF:
        rc_signed = rc_signed - 0x100000000
    return _interpret_rcm_rc(rc_signed)


def _interpret_rcm_rc(rc):
    """Map a TegraRcmSmash.exe exit code to (success, message, rc)."""
    if rc >= 0:
        return True, "Payload injected successfully!", rc
    errors = {
        -1: "Wrong USB driver version (need libusbK 3.0.7)",
        -2: "Failed to get USB driver version",
        -3: "Failed to open USB device handle — reinstall libusbK via Zadig",
        -4: "Wrong driver — install libusbK via Zadig or TegraRcmGUI",
        -5: "No device found in RCM mode\n\n"
            "Make sure your Switch is:\n"
            "  1. Powered off\n"
            "  2. Jig inserted into right Joy-Con rail\n"
            "  3. Hold Volume+ then press Power\n"
            "  4. Screen stays black = RCM mode ✓",
        -6: "Win32 error listing USB devices\n\n"
            "The libusbK driver may not be installed correctly.\n"
            "Open Zadig → select APX device → install libusbK.",
        -50: "Failed to launch TegraRcmSmash.exe",
    }
    return False, errors.get(rc, f"Unknown error (RC={rc})"), rc


def detect_rcm_device():
    """Check if a Nintendo Switch in RCM mode is connected via USB.

    Looks for USB device with VID 0955 (NVIDIA) and PID 7321 (Tegra RCM).
    Uses WMI via a quick PowerShell command — zero extra pip dependencies.
    Returns True if the device is detected, False otherwise.
    """
    import subprocess
    try:
        # Query Win32_PnPEntity for the Tegra RCM USB device.
        # The DeviceID contains VID_0955&PID_7321 when the Switch is in RCM.
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_PnPEntity "
             "| Where-Object { $_.DeviceID -like '*VID_0955&PID_7321*' } "
             "| Select-Object -First 1 -ExpandProperty DeviceID"],
            capture_output=True, text=True, timeout=5,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        return bool(result.stdout.strip())
    except Exception:
        return False


def find_mod_content(folder_path):
    """Recursively find the folder containing romfs content directories.
    Recognises fighter/, ui/, effect/, sound/, stage/, param/, and stream/."""
    _ROMFS_DIRS = ("fighter", "ui", "effect", "sound", "stage", "param", "stream")
    for d in _ROMFS_DIRS:
        if os.path.exists(os.path.join(folder_path, d)):
            return folder_path
    for item in os.listdir(folder_path):
        ip = os.path.join(folder_path, item)
        if os.path.isdir(ip):
            result = find_mod_content(ip)
            if result:
                return result
    return None


def _strip_broken_skel_pattern(mod_path):
    """Force vanilla mesh + skel + motion at slots that ship a
    custom skeleton without ``model.nuhlpb``.

    Empirical pattern (Doctor Doomario, Dr. Fate Mario both ship
    custom ``model.nusktb`` without ``model.nuhlpb`` and both
    freeze SSBU on match load; Mini Ganondorf ships both and
    works). The author shipped a custom skeleton but didn't ship
    the helper-bones bridge that lets vanilla animations and
    physics drive the new bone hierarchy. Vanilla SSBU motion
    files at the slot path reference bones by name; if the custom
    skel renames or removes any of them, the animation system
    panics the moment a match begins.

    Strategy: at any slot where this pattern is detected, drop:
      • The custom model bundle: ``model.numatb / numdlb / numshb /
        numshexb / nusktb``. ARCropolis falls back to the vanilla
        mesh + matl + skel for that slot.
      • The matching motion subtree (``fighter/<f>/motion/<tree>/cXX/``).
        Vanilla motion + swing files load instead, all aligned with
        the now-vanilla skeleton.

    Custom textures (``.nutexb``), UI bntx, and sound files are
    left untouched — they overlay vanilla via path replacement
    and don't depend on bone hierarchies. The slot ends up as
    "vanilla mesh wearing the mod's textures." Visually less
    faithful than the author intended, but **it doesn't freeze**.
    Mods that ship a proper ``nuhlpb`` (Mini Ganondorf) skip this
    path entirely — their custom skel is left intact.

    Returns ``[(slot_path, [removed_rel_paths]), ...]`` for
    logging.
    """
    BODY_TREE_NAMES = {"body", "fighter", "head", "weapon"}
    MODEL_BUNDLE = ("model.numatb", "model.numdlb", "model.numshb",
                    "model.numshexb", "model.nusktb")
    stripped = []
    fighter_root = os.path.join(mod_path, "fighter")
    if not os.path.isdir(fighter_root):
        return stripped
    for fighter in os.listdir(fighter_root):
        f_dir = os.path.join(fighter_root, fighter)
        model_dir = os.path.join(f_dir, "model")
        motion_dir = os.path.join(f_dir, "motion")
        if not os.path.isdir(model_dir):
            continue
        for tree in os.listdir(model_dir):
            if tree.lower() not in BODY_TREE_NAMES:
                continue
            model_tree = os.path.join(model_dir, tree)
            if not os.path.isdir(model_tree):
                continue
            for slot in os.listdir(model_tree):
                model_slot = os.path.join(model_tree, slot)
                if (not os.path.isdir(model_slot)
                        or not re.fullmatch(
                            r"c\d{2}", slot, re.I)):
                    continue
                has_numshb = os.path.isfile(os.path.join(
                    model_slot, "model.numshb"))
                has_nusktb = os.path.isfile(os.path.join(
                    model_slot, "model.nusktb"))
                has_nuhlpb = os.path.isfile(os.path.join(
                    model_slot, "model.nuhlpb"))
                if not (has_numshb and has_nusktb and not has_nuhlpb):
                    continue

                removed_model = []
                for fn in MODEL_BUNDLE:
                    full = os.path.join(model_slot, fn)
                    if os.path.isfile(full):
                        try:
                            os.remove(full)
                            removed_model.append(fn)
                        except OSError as e:
                            print(f"    Warning: could not strip "
                                  f"{full}: {e}")

                # Motion subtree for this slot, if present.
                motion_slot = os.path.join(motion_dir, tree, slot)
                removed_motion = []
                if os.path.isdir(motion_slot):
                    for r, _ds, fs in os.walk(motion_slot):
                        for fn in fs:
                            removed_motion.append(
                                os.path.relpath(
                                    os.path.join(r, fn),
                                    motion_slot
                                ).replace("\\", "/"))
                    try:
                        shutil.rmtree(motion_slot)
                    except OSError as e:
                        print(f"    Warning: could not strip "
                              f"{motion_slot}: {e}")

                if removed_model or removed_motion:
                    stripped.append(
                        (model_slot, removed_model + removed_motion))
                    print(
                        f"    [auto-strip] {fighter}/{tree}/{slot}: "
                        f"removed {len(removed_model)} model file(s) "
                        f"+ {len(removed_motion)} motion file(s) — "
                        "custom skeleton without matching "
                        "model.nuhlpb (would freeze match load via "
                        "bone-reference mismatch in vanilla "
                        "animations / swing.prc). Vanilla "
                        f"{fighter} mesh + skel + motion loads "
                        "instead; custom textures still apply.")
    return stripped


def _find_overlay_root(folder):
    """Return the directory whose direct children include ``atmosphere/``
    and/or ``ultimate/mods/``, or None. Walks one level deep so a single
    wrapper folder inside the archive (e.g. the ``TR4SH Rebuffed 1.44
    Essentials/`` folder a ZIP unpacks to) is transparent. Overlay
    archives never nest deeper than a single wrapper."""
    def has_overlay(d):
        return (os.path.isdir(os.path.join(d, "atmosphere"))
                or os.path.isdir(os.path.join(d, "ultimate", "mods")))
    if has_overlay(folder):
        return folder
    try:
        entries = os.listdir(folder)
    except OSError:
        return None
    for entry in entries:
        sub = os.path.join(folder, entry)
        if os.path.isdir(sub) and has_overlay(sub):
            return sub
    return None


def _install_sd_overlay(overlay_root, mod_name, metadata=None,
                        target_slot=None, slot_map=None):
    """Install an SD-root-overlay archive (TR4SH, HDR, Hewdraw, …).

    These archives ship a top-level ``atmosphere/`` and/or
    ``ultimate/mods/`` tree meant to be merged onto the SD root.  Their
    ``ultimate/mods/`` can hold multiple sibling mod folders, each with
    its own ``plugin.nro`` (the canonical ARCropolis per-mod plugin
    filename — never a stray).  The single-mod ``find_mod_content`` +
    copy path picks one sub-folder and drops everything else, which
    is what produced the empty TR4SH husk (only the UI sub-mod landed
    on SD, the actual gameplay ``plugin.nro`` was lost).

    target_slot / slot_map are ignored: overlay archives don't carry a
    single-slot semantic to remap against.
    """
    if target_slot or slot_map:
        print("    [overlay] target_slot/slot_map ignored — overlay "
              "archives ship multiple mods at fixed paths.")

    overlay_atmo = os.path.join(overlay_root, "atmosphere")
    overlay_ult_mods = os.path.join(overlay_root, "ultimate", "mods")
    deployed = []

    # 1. atmosphere/ → <SD>/atmosphere/.  Merge: leave existing
    #    plugins (libarcropolis.nro, latency_slider, …) alone, only
    #    add or update files this archive ships.
    if os.path.isdir(overlay_atmo):
        atmo_dest = os.path.join(SD_CARD, "atmosphere")
        print(f"  Merging atmosphere/ overlay into {atmo_dest}")
        shutil.copytree(overlay_atmo, atmo_dest, dirs_exist_ok=True)

    # 2. Each ultimate/mods/<sub>/ becomes its own mod folder under
    #    <SD>/ultimate/mods/.  Apply install-time hygiene per sub-mod
    #    but never strip ``plugin.nro``.
    mt = (metadata or {}).get("mod_type", "skin")
    plugin_bearing = mt in {"modpack", "mechanics", "balance", "ai",
                            "parameters", "moveset", "gameplay"}
    if os.path.isdir(overlay_ult_mods):
        for sub in sorted(os.listdir(overlay_ult_mods)):
            sub_path = os.path.join(overlay_ult_mods, sub)
            if not os.path.isdir(sub_path):
                continue

            _strip_freeze_risks_in_mod(sub_path)
            _strip_stray_dev_files_in_mod(sub_path)
            _strip_invalid_nutexb_in_mod(sub_path)

            # Stray-NRO strip — but always preserve plugin.nro (the
            # canonical ARCropolis per-mod plugin filename), and skip
            # the strip entirely for plugin-bearing parent mod types.
            stray = _detect_stray_nro_in_mod(sub_path)
            for rel in stray:
                if os.path.basename(rel).lower() == "plugin.nro":
                    print(f"    [keep-nro] {sub}/{rel}: canonical "
                          "ARCropolis per-mod plugin, preserved.")
                    continue
                if plugin_bearing:
                    print(f"    [keep-nro] {sub}/{rel}: parent mod "
                          f"type '{mt}' carries plugins as deliverables.")
                    continue
                full = os.path.join(sub_path, rel)
                try:
                    os.remove(full)
                    print(f"    [auto-strip] {sub}/{rel}: stray .nro "
                          "removed (ARCropolis #173).")
                except OSError as e:
                    print(f"    Warning: could not strip {full}: {e}")

            dest = os.path.join(ARCROPOLIS_MODS, sub)
            if os.path.isdir(dest):
                print(f"  Replacing existing '{sub}'…")
                shutil.rmtree(dest)
            print(f"  Deploying sub-mod: {sub}")
            shutil.copytree(sub_path, dest)

            if metadata:
                meta = dict(metadata)
                meta["sub_mod"] = sub
                meta["overlay_parent"] = mod_name
                try:
                    with open(os.path.join(dest, ".gb_meta.json"), "w",
                              encoding="utf-8") as f:
                        json.dump(meta, f, indent=2, ensure_ascii=False)
                except Exception:
                    pass

            deployed.append(dest)

    if any(os.path.isfile(os.path.join(d, "plugin.nro")) for d in deployed):
        print("  Note: this archive ships per-mod plugin.nro files. "
              "If the mod stays silent in-game, it likely needs "
              "Smashline 2 (libsmashline_plugin.nro) and/or NRO Hook "
              "(libnro_hook.nro) at atmosphere/contents/"
              "01006A800016E000/romfs/skyline/plugins/. Those are "
              "external prerequisites not shipped on GameBanana.")

    print(f"  SD-overlay install done: {len(deployed)} sub-mod(s) deployed.")
    return deployed[0] if deployed else None


def install_to_sd(archive_path, mod_name, metadata=None, target_slot=None,
                  slot_map=None, extracted_dir=None):
    """Extract archive, find mod content, copy to SD card ARCropolis mods.

    If ``extracted_dir`` is given, the archive is NOT re-extracted —
    we copy that pre-extracted tree to a working tmp dir and proceed
    from there. This is what 'Load to SD' uses when a profile has
    already populated ``MOD_CACHE_DIR/<mod_id>/extracted/``, skipping
    both the network download AND the redundant extraction.

    If metadata dict is provided, writes .gb_meta.json for thumbnail mapping.
    If target_slot is specified (e.g. 'c03') and mod has 1 slot, remaps it.
    If slot_map is provided (dict: src_slot -> target_slot), applies that mapping.
    """
    tmp_dir = tempfile.mkdtemp(prefix="gb_skin_")
    try:
        if extracted_dir is not None and os.path.isdir(extracted_dir):
            print(f"  Using cached extracted tree (no extract).")
            shutil.copytree(extracted_dir, tmp_dir, dirs_exist_ok=True)
        else:
            print(f"  Extracting archive...")
            extract_archive(archive_path, tmp_dir)

        # SD-root overlay archives (TR4SH, HDR, Hewdraw, …) ship a
        # top-level atmosphere/ and/or ultimate/mods/ tree meant to be
        # merged onto the SD root. find_mod_content's DFS would
        # otherwise descend into a single ultimate/mods/<sub>/ folder
        # and copy only that, dropping the per-mod plugin.nro files
        # AND the entire atmosphere/ subtree (helper plugins, exefs
        # patches, manual_html replacements). Detect the shape early
        # and route through the merge path instead.
        overlay_root = _find_overlay_root(tmp_dir)
        if overlay_root:
            print("  Detected SD-root overlay archive — merging onto SD.")
            return _install_sd_overlay(
                overlay_root, mod_name, metadata=metadata,
                target_slot=target_slot, slot_map=slot_map)

        # Find mod content
        mod_path = find_mod_content(tmp_dir)
        if not mod_path:
            mod_path = tmp_dir
            print(f"  Note: No fighter/ui structure found, using archive root")

        # Detect source slots in archive
        src_slots = _get_archive_slots(mod_path)

        # Auto-strip every freeze-risk pattern detected by
        # deep_diagnose_mod_slot — partial bundles, custom skel
        # without nuhlpb, custom skel without matching motion
        # subtree. Done BEFORE slot remap so subsequent config
        # regeneration sees the stripped files as missing and drops
        # stale registrations.
        _strip_freeze_risks_in_mod(mod_path)

        # Strip uncompiled-source / authoring residue: model.nuanmb
        # at model/ paths, model.nusrcmdlb, model.xmb, temp/ dirs,
        # .wav leftovers. These get registered by config-regen
        # otherwise and crash CSS preview rendering when scrolled.
        _strip_stray_dev_files_in_mod(mod_path)

        # Strip nutexb files that aren't actually NUTEXB-format.
        # Authors sometimes export TEX/PNG/DDS and rename to .nutexb
        # — ARCropolis loads them as NUTEXB and crashes match load.
        _strip_invalid_nutexb_in_mod(mod_path)

        # Stray .nro plugins shipped inside a *skin* mod are a
        # documented freeze cause (ARCropolis #173). For plugin-style
        # mods (TR4SH, Better AI, Training Modpack, …) the .nro IS the
        # entire deliverable — stripping it leaves an empty husk that
        # appears to install successfully but does nothing in-game.
        # Only strip when the mod type is one where an in-mod .nro is
        # genuinely stray (skin/stage/ui/music/effect).
        _mod_type = (metadata or {}).get("mod_type", "skin")
        _PLUGIN_BEARING = {"modpack", "mechanics", "balance", "ai",
                           "parameters", "moveset", "gameplay"}
        if _mod_type in _PLUGIN_BEARING:
            _kept_nro = _detect_stray_nro_in_mod(mod_path)
            for _rel in _kept_nro:
                print(f"    [keep-nro] {_rel}: preserved — mod type "
                      f"'{_mod_type}' carries plugins as deliverables.")
        else:
            _stray_nro = _detect_stray_nro_in_mod(mod_path)
            for _rel in _stray_nro:
                # plugin.nro is the canonical ARCropolis per-mod plugin
                # filename — it's never stray, regardless of mod type.
                if os.path.basename(_rel).lower() == "plugin.nro":
                    print(f"    [keep-nro] {_rel}: canonical "
                          "ARCropolis per-mod plugin, preserved.")
                    continue
                _full = os.path.join(mod_path, _rel)
                try:
                    os.remove(_full)
                    print(f"    [auto-strip] {_rel}: stray .nro plugin "
                          "removed (would freeze on first match per "
                          "ARCropolis #173).")
                except OSError as e:
                    print(f"    Warning: could not strip {_full}: {e}")

        # Apply slot mapping if provided
        if slot_map:
            _apply_slot_map(mod_path, slot_map)
            slot_label = ",".join(f"{s}->{t}" for s, t in slot_map.items())
        elif target_slot and src_slots:
            # Single target: if 1 source slot, just remap
            if len(src_slots) == 1:
                _remap_slots(mod_path, target_slot)
            else:
                # Multiple source slots but single target — remap first to target
                _remap_slots(mod_path, target_slot)
            slot_label = target_slot
        else:
            slot_label = None

        # Re-run the freeze-risk strip AFTER slot remap. The remap
        # renames model/<tree>/cXX directories but motion subtrees
        # at fighter/<f>/motion/<tree>/cXX/ may not get moved in
        # lockstep — that orphans the motion at the old slot path
        # and produces "custom skel without matching motion" at the
        # new slot, which freezes match-load. The pre-remap strip
        # above only catches issues visible at source slots; this
        # second pass catches inconsistencies introduced by the
        # remap itself.
        _strip_freeze_risks_in_mod(mod_path)

        # Create destination
        safe_name = re.sub(r'[^\w\s\-]', '', mod_name).strip().replace(" ", "_")
        if not safe_name:
            safe_name = "gb_skin"

        # Build slot suffix — but only if an actual remap occurred
        actual_suffix = None
        if slot_map:
            # Check if any mapping actually changed a slot
            actually_remapped = any(s != t for s, t in slot_map.items())
            if actually_remapped or len(slot_map) > 1:
                targets = sorted(set(slot_map.values()))
                actual_suffix = "_".join(targets)
        elif target_slot and src_slots:
            # Check if the archive's only slot already matches the target
            all_slots = []
            for _fi, sl in src_slots.items():
                all_slots.extend(sl)
            if len(all_slots) == 1 and all_slots[0] == target_slot:
                actual_suffix = None  # no remap needed, no suffix
            else:
                actual_suffix = target_slot

        name_with_suffix = f"{safe_name}_{actual_suffix}" if actual_suffix else safe_name
        dest = os.path.join(ARCROPOLIS_MODS, name_with_suffix)

        # Remove any existing install of the same mod (with or without slot suffix)
        # to prevent duplicates like Foo and Foo_c00 coexisting
        for existing in os.listdir(ARCROPOLIS_MODS):
            existing_path = os.path.join(ARCROPOLIS_MODS, existing)
            if not os.path.isdir(existing_path):
                continue
            # Match if existing is the base name or base name + any slot suffix
            if existing == safe_name or existing.startswith(safe_name + "_c"):
                if existing_path != dest:
                    print(f"  Removing previous install '{existing}'...")
                    shutil.rmtree(existing_path)

        if os.path.exists(dest):
            print(f"  Removing existing '{name_with_suffix}'...")
            shutil.rmtree(dest)

        print(f"  Copying to SD card...")
        shutil.copytree(mod_path, dest)

        # Write metadata
        if metadata:
            meta_data = dict(metadata)
            if slot_label:
                meta_data["slot"] = slot_label
            meta_path = os.path.join(dest, ".gb_meta.json")
            try:
                with open(meta_path, "w", encoding="utf-8") as f:
                    json.dump(meta_data, f, indent=2, ensure_ascii=False)
            except Exception:
                pass

        fc = sum(len(f) for _, _, f in os.walk(dest))
        slot_msg = f" (slots {slot_label})" if slot_label else ""
        print(f"  Installed: {name_with_suffix} ({fc} files){slot_msg}")
        return dest
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _get_archive_slots(mod_path):
    """Peek inside extracted mod to find what slot directories exist.
    Returns dict: fighter_internal -> sorted list of slots."""
    result = {}
    fighter_dir = os.path.join(mod_path, "fighter")
    if not os.path.isdir(fighter_dir):
        return result
    for fname in os.listdir(fighter_dir):
        body = os.path.join(fighter_dir, fname, "model", "body")
        if os.path.isdir(body):
            slots = sorted(d for d in os.listdir(body)
                           if os.path.isdir(os.path.join(body, d))
                           and re.match(r'^c\d{2}$', d))
            if slots:
                result[fname] = slots
    return result


def _get_all_slot_numbers(mod_path):
    """Scan the extracted mod for ALL cXX slot numbers used anywhere:
    fighter model parts, UI chara dirs, sound files, item dirs.
    Returns a sorted set of slot strings like {'c00', 'c01', 'c04'}."""
    slots = set()
    # Fighter model parts
    fighter_dir = os.path.join(mod_path, "fighter")
    if os.path.isdir(fighter_dir):
        for fname in os.listdir(fighter_dir):
            model_dir = os.path.join(fighter_dir, fname, "model")
            if not os.path.isdir(model_dir):
                continue
            for part in os.listdir(model_dir):
                part_dir = os.path.join(model_dir, part)
                if os.path.isdir(part_dir):
                    for d in os.listdir(part_dir):
                        if os.path.isdir(os.path.join(part_dir, d)) and \
                           re.match(r'^c\d{2}$', d):
                            slots.add(d)
    # UI bntx files — the slot is the 2-digit suffix in the filename, NOT
    # the ``chara_<TYPE>`` parent directory (those are portrait types).
    chara_dir = os.path.join(mod_path, "ui", "replace", "chara")
    if os.path.isdir(chara_dir):
        for type_dir in os.listdir(chara_dir):
            type_path = os.path.join(chara_dir, type_dir)
            if not (os.path.isdir(type_path)
                    and type_dir.lower().startswith("chara_")):
                continue
            for fname in os.listdir(type_path):
                m = _UI_BNTX_RE.match(fname)
                if m:
                    slots.add(f"c{int(m.group(2)):02d}")
    # Sound files
    for sub in ("fighter", "fighter_voice"):
        snd_dir = os.path.join(mod_path, "sound", "bank", sub)
        if os.path.isdir(snd_dir):
            for f in os.listdir(snd_dir):
                m = re.match(r'^.+_c(\d{2})\.nus3audio$', f, re.I)
                if m:
                    slots.add(f"c{int(m.group(1)):02d}")
    # Item dirs
    item_dir = os.path.join(mod_path, "item")
    if os.path.isdir(item_dir):
        for iname in os.listdir(item_dir):
            body = os.path.join(item_dir, iname, "model", "body")
            if os.path.isdir(body):
                for d in os.listdir(body):
                    if os.path.isdir(os.path.join(body, d)) and \
                       re.match(r'^c\d{2}$', d):
                        slots.add(d)
    return sorted(slots)


def _apply_slot_map(mod_path, slot_map):
    """Apply a multi-slot mapping (src_slot -> target_slot) to the mod.

    Reslot rules now match the canonical CSharpM7/jozz024/blu-dev
    ``reslotter.py`` algorithm — see :func:`_canonical_reslot_pair`
    for the per-pair specifics. Each source slot is reslotted
    independently. Source slots NOT in the map are dropped (we're
    installing only the variants the user picked).
    """
    # 1. Drop fighter-tree slots NOT in the map (the user didn't
    #    pick those variants). Sound + effect + UI files for
    #    unmapped slots also go.
    fighter_root = os.path.join(mod_path, "fighter")
    if os.path.isdir(fighter_root):
        for fighter in os.listdir(fighter_root):
            f_dir = os.path.join(fighter_root, fighter)
            model_dir = os.path.join(f_dir, "model")
            if not os.path.isdir(model_dir):
                continue
            for tree in os.listdir(model_dir):
                tree_dir = os.path.join(model_dir, tree)
                if not os.path.isdir(tree_dir):
                    continue
                for d in list(os.listdir(tree_dir)):
                    if (re.fullmatch(r"c\d{2}", d, re.I)
                            and d.lower() not in slot_map
                            and d.lower() not in slot_map.values()):
                        shutil.rmtree(
                            os.path.join(tree_dir, d), ignore_errors=True)

    # 2. For each (src, tgt) pair, run the canonical reslot.
    for fighter in (os.listdir(fighter_root)
                    if os.path.isdir(fighter_root) else []):
        for src, tgt in list(slot_map.items()):
            if src == tgt:
                # No remap needed; canonical reslotter would no-op.
                continue
            renames = _canonical_reslot_pair(
                mod_path, fighter.lower(), src.lower(), tgt.lower())
            for old_rel, new_rel in renames:
                # Compact log line — matches the old "Reslotted" output
                # so existing parsers / log readers still work.
                if "/" in old_rel and "/" in new_rel:
                    print(f"    Reslotted: {os.path.basename(old_rel)} "
                          f"-> {os.path.basename(new_rel)}")

    # 3. config.json — register the reslotted files. This is the
    # critical piece: ARCropolis reads ``new-dir-files`` to know
    # which custom files to overlay onto each slot. Without an
    # entry for our reslotted file, the game falls back to vanilla
    # at that slot — so the mod files exist on disk but never get
    # used. (This is the root cause of "Doc Mario skin reslotted
    # to a new slot loads blank / freezes": the rename worked but
    # the registration didn't.)
    _regenerate_config_json(mod_path, slot_map)
    return  # legacy body intentionally elided


def _regenerate_config_json(mod_path, slot_map):
    """Rebuild ``config.json`` so every custom file currently on disk
    is registered under the right ``new-dir-files`` key.

    Mirrors the canonical CSharpM7/jozz024 reslotter's
    ``add_missing_files`` step but works file-system-first: re-derive
    the entries from what's actually on disk after rename, instead
    of from the pre-rename file list.

    We always preserve every top-level key the author shipped
    (``unshare-blacklist``, ``share-to-vanilla``, ``share-to-added``,
    ``new-dir-infos``, etc.) — only ``new-dir-files`` and slot
    tokens inside string values get rewritten. Authors often
    under-register on purpose or by oversight (e.g. Doctor Doomario
    only registers textures, not its custom mesh files). Per the
    ARCropolis source, files at vanilla slot paths replace vanilla
    only when they hash-match a vanilla file — every other custom
    file MUST be in ``new-dir-files`` to ever load. Disk-walking
    and registering everything is the canonical fix.
    """
    cfg_path = os.path.join(mod_path, "config.json")

    # Load the author's full config (every key) so we don't lose
    # ``unshare-blacklist`` or other directives. Fall back to a
    # minimal template only when the mod ships no config at all.
    config = {
        "new-dir-infos": [],
        "new-dir-infos-base": {},
        "share-to-vanilla": {},
        "new-dir-files": {},
        "share-to-added": {},
    }
    if os.path.isfile(cfg_path):
        try:
            with open(cfg_path, "r", encoding="utf-8") as fh:
                existing = json.load(fh) or {}
            if isinstance(existing, dict):
                config = existing
            # Make sure new-dir-files exists as a dict so the walk
            # below can populate it.
            if not isinstance(config.get("new-dir-files"), dict):
                config["new-dir-files"] = {}
        except Exception as e:
            print(f"    Warning: could not read existing "
                  f"{cfg_path}: {e}")

    # First, fix up references inside any pre-existing string keys
    # that pointed at the source slot — they need to point at the
    # target slot.
    def _remap_str(s):
        for src, tgt in slot_map.items():
            s = re.sub(
                rf"(?<![a-z0-9]){re.escape(src)}(?![a-z0-9])",
                tgt, s, flags=re.IGNORECASE)
        return s

    def _remap_value(v):
        if isinstance(v, str):
            return _remap_str(v)
        if isinstance(v, list):
            return [_remap_value(x) for x in v]
        if isinstance(v, dict):
            return {_remap_str(k): _remap_value(val)
                    for k, val in v.items()}
        return v

    config = _remap_value(config)

    # Strip orphan paths from the loaded ``new-dir-files`` — entries
    # whose target doesn't exist on disk. Mods often ship configs
    # generated for their original multi-slot layout (e.g. Mini
    # Ganondorf's config registers 145 paths across c00/c01/c02
    # even though only c02 is actually shipped). Carrying those
    # forward makes ARCropolis fail to resolve them and can crash
    # adjacent slots / the whole character.
    cleaned_ndf = {}
    dropped = 0
    for key, entries in (config.get("new-dir-files") or {}).items():
        if not isinstance(entries, list):
            cleaned_ndf[key] = entries
            continue
        kept = []
        for entry in entries:
            if not isinstance(entry, str):
                continue
            full = os.path.join(mod_path,
                                 entry.replace("/", os.sep))
            if os.path.isfile(full):
                kept.append(entry)
            else:
                dropped += 1
        if kept:
            cleaned_ndf[key] = kept
    config["new-dir-files"] = cleaned_ndf
    if dropped:
        print(f"    Stripped {dropped} orphan config.json path(s) "
              "(referenced files not on disk)")

    # Strip orphan share-to-vanilla / share-to-added entries the
    # same way — paths whose source doesn't exist on disk get
    # dropped from those redirect tables.
    for tbl_name in ("share-to-vanilla", "share-to-added"):
        tbl = config.get(tbl_name) or {}
        if not isinstance(tbl, dict):
            continue
        cleaned = {}
        for src, dests in tbl.items():
            if not isinstance(dests, list):
                continue
            kept = [d for d in dests
                    if isinstance(d, str)
                    and os.path.isfile(os.path.join(
                        mod_path, d.replace("/", os.sep)))]
            if kept:
                cleaned[src] = kept
        config[tbl_name] = cleaned

    # Disk-walk: register every custom file under the appropriate
    # ``fighter/<f>/<slot>`` key. This matches CSharpM7's
    # ``add_missing_files`` behavior and is necessary because per
    # the ARCropolis source, files at vanilla slot paths only
    # auto-replace when their hash matches a vanilla file — every
    # other custom file must be registered here to ever load. We
    # also handle: motion subtrees (swing.prc), sound bank files,
    # effect files, and UI bntx.
    # The walk runs over the FINAL slots (target side of the map);
    # for identity remaps the target == source, so iterating
    # ``set(slot_map.values())`` covers all relevant slots.

    # Identity slot maps: every src == tgt (no rename happened).
    # We still want to register custom files the author may have
    # under-registered, so let the walk run.
    target_slots = set(slot_map.values()) if slot_map else set()
    for tgt in target_slots:
        # Single canonical key per slot per fighter.
        # Need to know the fighter name(s) — derive from any
        # ``fighter/<name>/`` subdir present.
        fighter_root = os.path.join(mod_path, "fighter")
        fighters = []
        if os.path.isdir(fighter_root):
            fighters = [d for d in os.listdir(fighter_root)
                         if os.path.isdir(
                             os.path.join(fighter_root, d))]

        # Bare 2-digit slot form for the effect / UI / sound checks
        # that don't use ``cXX``.
        tgt_n = tgt.lstrip("c")

        for fighter in fighters:
            key = f"fighter/{fighter}/{tgt}"
            entries = config["new-dir-files"].setdefault(key, [])
            seen = set(entries)

            def _add(rel):
                rel = rel.replace("\\", "/")
                if rel not in seen:
                    entries.append(rel)
                    seen.add(rel)

            # 1. fighter/<f>/<anything>/<tgt>/...
            #    Catches model trees AND motion subtrees AND any
            #    other slot-pathed content.
            f_dir = os.path.join(fighter_root, fighter)
            for r, _ds, fs in os.walk(f_dir):
                # Match any path containing /<tgt>/ as a directory.
                norm = r.replace("\\", "/") + "/"
                if f"/{tgt}/" not in norm:
                    continue
                for fn in fs:
                    full = os.path.join(r, fn)
                    rel = os.path.relpath(full, mod_path)
                    _add(rel)

            # 2. sound/bank/fighter/se_<f>_<tgt>.* and
            #    sound/bank/fighter_voice/vc_<f>_<tgt>.*
            for sub in ("fighter", "fighter_voice"):
                snd_dir = os.path.join(mod_path, "sound", "bank", sub)
                if not os.path.isdir(snd_dir):
                    continue
                prefixes = (f"se_{fighter}_{tgt}",
                             f"vc_{fighter}_{tgt}")
                for fn in os.listdir(snd_dir):
                    if any(fn.startswith(p) for p in prefixes):
                        rel = os.path.relpath(
                            os.path.join(snd_dir, fn), mod_path)
                        _add(rel)

            # 3. effect/fighter/<f>/...<tgt_n>...  (bare digits!)
            ef_dir = os.path.join(mod_path, "effect", "fighter",
                                    fighter)
            if os.path.isdir(ef_dir):
                for r, _ds, fs in os.walk(ef_dir):
                    for fn in fs:
                        full = os.path.join(r, fn)
                        rel = os.path.relpath(full, mod_path)
                        if tgt_n in rel.replace("\\", "/").rsplit(
                                "/", 1)[-1]:
                            _add(rel)
                # Effect transplant subtree gets routed to the
                # special ``fighter/<f>/cmn`` key (canonical
                # reslotter does this — let it through unchanged).
                # We don't add a separate key here — the user's mod
                # archive ships transplant content under
                # ``effect/.../transplant/`` and it'll have already
                # been picked up by the walk above via path matching.

            # 4. ui/replace[_patch]/chara/.../chara_*_<f>_<tgt_n>.bntx
            for ui_root in ("ui/replace/chara", "ui/replace_patch/chara"):
                ui_dir = os.path.join(mod_path,
                                       *ui_root.split("/"))
                if not os.path.isdir(ui_dir):
                    continue
                ui_re = re.compile(
                    rf"^chara_\d+_{re.escape(fighter)}_"
                    rf"{re.escape(tgt_n)}\.bntx$",
                    re.IGNORECASE)
                for r, _ds, fs in os.walk(ui_dir):
                    for fn in fs:
                        if ui_re.match(fn):
                            rel = os.path.relpath(
                                os.path.join(r, fn), mod_path)
                            _add(rel)

    try:
        with open(cfg_path, "w", encoding="utf-8") as fh:
            json.dump(config, fh, indent=4, ensure_ascii=False)
        total = sum(len(v) for v in config["new-dir-files"].values())
        print(f"    Wrote config.json with {total} new-dir-files "
              f"entries across {len(config['new-dir-files'])} key(s)")
    except Exception as e:
        print(f"    Warning: could not write {cfg_path}: {e}")


def _legacy_apply_slot_map_unused(mod_path, slot_map):
    """Pre-canonical implementation kept for reference. Deleted from
    the call graph — see the new :func:`_apply_slot_map` above."""
    c_pat   = re.compile(r'c(\d{2})')
    ui_pat  = re.compile(r'(chara_\d+_[a-zA-Z]+_)(\d{2})(\.bntx)')
    cfg_pat = re.compile(r'(?i)^config\.json$')

    # Collect entries sorted by descending depth (bottom-up)
    entries = []
    for root, dirs, files in os.walk(mod_path):
        depth = root.replace("\\", "/").count("/")
        for f in files:
            entries.append((depth + 1, os.path.join(root, f), False))
        for d in dirs:
            entries.append((depth + 1, os.path.join(root, d), True))
    entries.sort(key=lambda x: -x[0])

    # Helper: given a cXX match, return the mapped target or None (drop)
    def _map_slot(m):
        src = f"c{m.group(1) if m.lastindex and m.lastindex >= 1 else m.group(0)[1:]}"
        # Normalise to full match
        src_full = m.group(0)  # e.g. "c04"
        if src_full in slot_map:
            return slot_map[src_full]
        return None  # not in map → should be dropped

    for _depth, path, is_dir in entries:
        if not os.path.exists(path):
            continue
        name = os.path.basename(path)

        # config.json — rewrite contents
        if not is_dir and cfg_pat.match(name):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    contents = fh.read()
                def _repl_c(m):
                    tgt = slot_map.get(m.group(0))
                    return tgt if tgt else m.group(0)
                contents = c_pat.sub(_repl_c, contents)
                def _repl_ui(m):
                    old_slot = f"c{int(m.group(2)):02d}"
                    tgt = slot_map.get(old_slot)
                    if tgt:
                        return f"{m.group(1)}{tgt[1:]}{m.group(3)}"
                    return m.group(0)
                contents = ui_pat.sub(_repl_ui, contents)
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(contents)
                print(f"    Updated config.json")
            except Exception as e:
                print(f"    Warning: could not update {path}: {e}")
            continue

        # UI bntx files — rename only the slot suffix
        if not is_dir and ui_pat.search(name):
            um = ui_pat.search(name)
            old_slot = f"c{int(um.group(2)):02d}"
            if old_slot in slot_map:
                tgt = slot_map[old_slot]
                new_name = ui_pat.sub(
                    lambda m: f"{m.group(1)}{tgt[1:]}{m.group(3)}", name)
                if new_name != name:
                    new_path = os.path.join(os.path.dirname(path), new_name)
                    if os.path.exists(new_path):
                        os.remove(new_path)
                    os.rename(path, new_path)
                    print(f"    Reslotted UI: {name} -> {new_name}")
            elif old_slot not in slot_map:
                os.remove(path)
            continue

        # Dirs / files with cXX in the name
        cm = c_pat.search(name)
        if cm:
            matched_slot = cm.group(0)  # e.g. "c04"
            if matched_slot in slot_map:
                tgt = slot_map[matched_slot]
                new_name = c_pat.sub(tgt, name)
                if new_name != name:
                    new_path = os.path.join(os.path.dirname(path), new_name)
                    if is_dir and os.path.exists(new_path):
                        shutil.rmtree(new_path)
                    if not is_dir and os.path.exists(new_path):
                        os.remove(new_path)
                    os.rename(path, new_path)
                    print(f"    Reslotted: {name} -> {new_name}")
            elif matched_slot not in slot_map:
                # This slot is being dropped
                if is_dir:
                    shutil.rmtree(path)
                else:
                    os.remove(path)


def _canonical_reslot_pair(mod_path, fighter_name, source_slot,
                            target_slot):
    """Apply a single ``source_slot -> target_slot`` reslot using the
    same rename rules as the canonical CSharpM7/jozz024/blu-dev
    ``reslotter.py`` ("ssbu-skin-reslotter"):

      • ``fighter/<f>/.../cXX/...`` → substring replace ``/cXX/`` →
        ``/cYY/`` (PATH-segment based, NOT bare cXX in filenames).
      • ``sound/bank/fighter/se_<f>_cXX.*`` and
        ``sound/bank/fighter_voice/vc_<f>_cXX.*`` → ``_cXX`` suffix
        substring replace.
      • ``effect/fighter/<f>/...XX...`` → BARE 2-digit replace
        (``03`` → ``05``, *not* ``c03`` → ``c05``).
      • UI ``ui/replace[_patch]/chara/.../chara_*_<f>_NN.bntx`` →
        2-digit suffix replace, with Ice-Climbers / Aegis special
        casing for the fighter key.
      • Anything else (other fighters, cmn, common, params shared
        across slots) is left untouched.

    Operates IN-PLACE on ``mod_path``. Returns a list of
    ``(old_rel_path, new_rel_path)`` for every file that was renamed
    so callers can write the corresponding ``config.json`` entries.

    See ``ssbu-skin-reslotter``: BluJay (https://github.com/blu-dev),
    Jozz (https://github.com/jozz024/ssbu-skin-reslotter), and
    Coolsonickirby — credit goes to them for the canonical algorithm.
    """
    src = source_slot.lower()
    tgt = target_slot.lower()
    if src == tgt:
        return []
    # Bare 2-digit forms used by effect dir.
    src_n = src.lstrip("c")
    tgt_n = tgt.lstrip("c")

    # UI fighter-key remapping (Ice Climbers + Aegis quirks).
    fighter_keys = [fighter_name]
    if fighter_name in ("popo", "nana"):
        fighter_keys = ["ice_climber"]
    elif fighter_name == "eflame":
        fighter_keys = ["eflame_first", "eflame_only"]
    elif fighter_name == "elight":
        fighter_keys = ["elight_first", "elight_only"]

    ui_re = re.compile(
        r"^(chara_\d+_(?:" + "|".join(re.escape(k) for k in fighter_keys)
        + r")_)(\d{2})(\.bntx)$", re.IGNORECASE)

    fighter_path_seg = f"/{src}/"
    fighter_path_target = f"/{tgt}/"
    sound_se_prefix = f"se_{fighter_name}_{src}"
    sound_vc_prefix = f"vc_{fighter_name}_{src}"
    effect_prefix = f"effect/fighter/{fighter_name}/"

    # Walk bottom-up so renames don't break parent paths mid-walk.
    entries = []
    for root, dirs, files in os.walk(mod_path):
        depth = root.replace("\\", "/").count("/")
        for f in files:
            entries.append((depth + 1, os.path.join(root, f), False))
        for d in dirs:
            entries.append((depth + 1, os.path.join(root, d), True))
    entries.sort(key=lambda e: -e[0])

    renames = []
    for _depth, path, is_dir in entries:
        if is_dir:
            # We don't rename dirs explicitly; the path-substring
            # replacement on each file's full path implicitly walks
            # them out as files are moved. Empty src dirs are pruned
            # at the end.
            continue
        rel = os.path.relpath(path, mod_path).replace("\\", "/")
        new_rel = None

        # 1. Fighter tree (path-segment based)
        if rel.startswith(f"fighter/{fighter_name}/"):
            if fighter_path_seg not in f"/{rel}/":
                continue
            new_rel = rel.replace(fighter_path_seg, fighter_path_target)

        # 2. Sound files (suffix on bare filename)
        elif (rel.startswith(f"sound/bank/fighter/{sound_se_prefix}")
              or rel.startswith(
                  f"sound/bank/fighter_voice/{sound_vc_prefix}")):
            new_rel = rel.replace(f"_{src}", f"_{tgt}")

        # 3. Effect files (BARE digits)
        elif rel.startswith(effect_prefix):
            # The canonical tool replaces `current_alt.strip('c')` →
            # `target_alt.strip('c')`, which is the 2-digit form.
            # First-occurrence is enough since effect filenames
            # encode the slot once.
            if src_n in rel[len(effect_prefix):]:
                head, tail = rel[:len(effect_prefix)], rel[len(effect_prefix):]
                new_rel = head + tail.replace(src_n, tgt_n, 1)

        # 4. UI bntx (2-digit suffix on the bntx filename)
        elif (rel.startswith("ui/replace/chara/")
              or rel.startswith("ui/replace_patch/chara/")):
            base = os.path.basename(rel)
            m = ui_re.match(base)
            if m and m.group(2) == src_n:
                new_base = f"{m.group(1)}{tgt_n}{m.group(3)}"
                new_rel = os.path.join(
                    os.path.dirname(rel), new_base
                ).replace("\\", "/")

        if not new_rel or new_rel == rel:
            continue

        new_path = os.path.join(mod_path, new_rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(new_path), exist_ok=True)
        if os.path.exists(new_path):
            try:
                if os.path.isdir(new_path):
                    shutil.rmtree(new_path)
                else:
                    os.remove(new_path)
            except OSError:
                pass
        os.rename(path, new_path)
        renames.append((rel, new_rel))

    # Prune now-empty source slot dirs so leftover ``cXX`` shells
    # don't confuse the SD-side conflict detector.
    for root, dirs, files in os.walk(mod_path, topdown=False):
        if not dirs and not files:
            try:
                os.rmdir(root)
            except OSError:
                pass

    return renames


def _remap_slots(mod_path, target_slot):
    """Single-target reslot: rebuild the slot_map by detecting all
    cXX folders the mod ships with and remapping each to ``target_slot``.

    For multi-slot archives we keep the FIRST detected source slot and
    drop the rest (mirrors the canonical reslotter's per-source-slot
    workflow — the user expressed intent for one specific target slot,
    so the other variants in the archive aren't relevant).
    """
    src_slots = _get_archive_slots(mod_path)
    # Pick first available source slot from any fighter tree.
    chosen_src = None
    for slots in src_slots.values():
        if slots:
            chosen_src = slots[0]
            break
    if chosen_src is None:
        # Nothing to rename — texture-only mod, etc. Just rewrite
        # config.json refs.
        cfg_path = os.path.join(mod_path, "config.json")
        if os.path.isfile(cfg_path):
            try:
                with open(cfg_path, "r", encoding="utf-8") as fh:
                    contents = fh.read()
                # Best-effort: replace all cXX with target.
                contents = re.sub(r"c\d{2}", target_slot, contents)
                with open(cfg_path, "w", encoding="utf-8") as fh:
                    fh.write(contents)
            except Exception:
                pass
        return
    # Use the canonical map-based pathway.
    _apply_slot_map(mod_path, {chosen_src: target_slot})
    return  # done; legacy body below is dead code, kept for diff clarity


def _legacy_remap_slots_unused(mod_path, target_slot):
    """Pre-canonical implementation. Replaced by the body above."""
    c_pat   = re.compile(r'c\d{2}')
    ui_pat  = re.compile(r'(chara_\d+_[a-zA-Z]+_)(\d{2})(\.bntx)')
    cfg_pat = re.compile(r'(?i)^config\.json$')

    # Collect entries sorted by descending depth (bottom-up)
    entries = []
    for root, dirs, files in os.walk(mod_path):
        depth = root.replace("\\", "/").count("/")
        for f in files:
            entries.append((depth + 1, os.path.join(root, f), False))
        for d in dirs:
            entries.append((depth + 1, os.path.join(root, d), True))
    entries.sort(key=lambda x: -x[0])

    tgt = target_slot  # e.g. "c05"
    tgt_num = tgt[1:]  # e.g. "05"

    for _depth, path, is_dir in entries:
        name = os.path.basename(path)

        # config.json — rewrite contents
        if not is_dir and cfg_pat.match(name):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    contents = fh.read()
                contents = c_pat.sub(tgt, contents)
                contents = ui_pat.sub(
                    lambda m: f"{m.group(1)}{tgt_num}{m.group(3)}", contents)
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(contents)
                print(f"    Updated config.json")
            except Exception as e:
                print(f"    Warning: could not update {path}: {e}")
            continue

        # UI bntx files — rename only the slot suffix in filename
        if not is_dir and ui_pat.search(name):
            new_name = ui_pat.sub(
                lambda m: f"{m.group(1)}{tgt_num}{m.group(3)}", name)
            if new_name != name:
                new_path = os.path.join(os.path.dirname(path), new_name)
                if os.path.exists(new_path):
                    os.remove(new_path)
                os.rename(path, new_path)
                print(f"    Reslotted UI: {name} -> {new_name}")
            continue

        # Dirs / files whose name contains cXX
        if c_pat.search(name):
            new_name = c_pat.sub(tgt, name)
            if new_name != name:
                new_path = os.path.join(os.path.dirname(path), new_name)
                if is_dir and os.path.exists(new_path):
                    shutil.rmtree(new_path)
                if not is_dir and os.path.exists(new_path):
                    os.remove(new_path)
                os.rename(path, new_path)
                print(f"    Reslotted: {name} -> {new_name}")


def _remap_ui_slots(mod_path, target_slot):
    """Remap bntx filenames inside ui/replace/chara/ directories.
    Only the two-digit slot suffix is changed; chara_X dir names are kept."""
    slot_num = int(target_slot[1:])
    chara_dir = os.path.join(mod_path, "ui", "replace", "chara")
    if not os.path.isdir(chara_dir):
        return
    for sub in os.listdir(chara_dir):
        sub_path = os.path.join(chara_dir, sub)
        if not os.path.isdir(sub_path) or not sub.startswith("chara_"):
            continue
        for bntx in list(os.listdir(sub_path)):
            m = re.match(r'^(chara_\d+_\w+_)(\d{2})(\.bntx)$', bntx)
            if m:
                old_idx = int(m.group(2))
                if old_idx != slot_num:
                    new_name = f"{m.group(1)}{slot_num:02d}{m.group(3)}"
                    os.rename(os.path.join(sub_path, bntx),
                              os.path.join(sub_path, new_name))
                    print(f"    Remapped UI file: {bntx} -> {new_name}")


# ─────────────────────────────────────────────────────────
#  FAVORITES PERSISTENCE
# ─────────────────────────────────────────────────────────

FAVORITES_FILE = os.path.join(SCRIPT_DIR, "gb_favorites.json")
PROFILES_FILE = os.path.join(SCRIPT_DIR, "gb_profiles.json")

# ─────────────────────────────────────────────────────────
#  ADULT-ONLY AUDIT CACHE
# ─────────────────────────────────────────────────────────

AUDIT_CACHE_FILE = os.path.join(SCRIPT_DIR, "gb_audit_cache.json")
# Per-mod_id cache of the *real* (fighter, slot) tuples a mod actually
# touches on disk. Populated after every successful install so we can
# pre-flight-check a profile for collisions BEFORE re-running a long
# bulk install — including the cross-character case (a mod tagged
# "Birdo" that secretly replaces fighter/yoshi/c02/...).
TOUCHED_CACHE_FILE = os.path.join(SCRIPT_DIR, "gb_touched_cache.json")


def load_audit_cache():
    """Load the adult-only audit cache.  Returns dict with structure:
    { "<cache_key>": { "flagged": [...], "pages_scanned": int,
                       "total_scanned": int, "total_api": int,
                       "timestamp": str, "sort": str } }
    """
    if os.path.exists(AUDIT_CACHE_FILE):
        try:
            with open(AUDIT_CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_audit_cache(cache):
    """Persist the audit cache to disk."""
    with open(AUDIT_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2, ensure_ascii=False)


# ── Touched-slot cache (per mod_id) ──

def load_touched_cache():
    """Load the per-mod_id touched-slots cache.

    Structure: ``{ "<mod_id>": {"touched": [["yoshi","c02"], ...],
                                "ts": float} }``
    """
    if os.path.exists(TOUCHED_CACHE_FILE):
        try:
            with open(TOUCHED_CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_touched_cache(cache):
    with open(TOUCHED_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2, ensure_ascii=False)


def compute_touched_slots(installed_dir):
    """Walk ``installed_dir`` and return a sorted list of unique
    ``[fighter, slot]`` pairs the mod actually replaces.

    Uses :func:`_classify_mod_path` so the same logic that drives
    :func:`detect_file_conflicts` decides what counts as a slot-bound
    file.  Files that classify as shared (msg, ui_chara_db, params, …)
    are ignored — they don't cause slot collisions.
    """
    touched = set()
    if not os.path.isdir(installed_dir):
        return []
    for root, _, files in os.walk(installed_dir):
        for fn in files:
            if fn == ".gb_meta.json":
                continue
            full = os.path.join(root, fn)
            try:
                rel = os.path.relpath(full, installed_dir).replace("\\", "/")
            except ValueError:
                continue
            f, s = _classify_mod_path(rel)
            if f and s:
                touched.add((f, s))
    return sorted([list(t) for t in touched])


def record_touched_for_mod(mod_id, installed_dir):
    """Persist the touched-slot list for *mod_id* after a successful install.

    Silent on failure — this is best-effort metadata, not critical.
    """
    if not mod_id:
        return []
    try:
        touched = compute_touched_slots(installed_dir)
        if not touched:
            return []
        cache = load_touched_cache()
        cache[str(mod_id)] = {
            "touched": touched,
            "ts": time.time(),
        }
        save_touched_cache(cache)
        return touched
    except Exception as e:
        print(f"  ! Could not record touched slots for {mod_id}: {e}")
        return []


def get_cached_touched(mod_id):
    """Return cached ``[[fighter,slot], ...]`` for *mod_id* or ``[]``."""
    if not mod_id:
        return []
    entry = load_touched_cache().get(str(mod_id))
    if not entry:
        return []
    out = entry.get("touched") or []
    # Be defensive: only keep well-formed pairs.
    return [list(t) for t in out
            if isinstance(t, (list, tuple)) and len(t) == 2]


def _split_multislot_dir(dir_path):
    """Repair a malformed multi-slot directory like ``c00, c03`` by
    renaming it to the **first** slot token only (``c00``).

    Mod authors sometimes pack a single folder named ``c00, c03`` to
    say "this content applies to both slots". The game's filesystem
    layer can't parse the comma so the slot never resolves — but
    cloning the content into *both* slots overrides whatever the
    user actually assigned to the second slot in their profile, which
    just trades one bug for another. The right behavior is to honor
    the first slot only and let the user pick a different one in the
    slot picker if they want the mod somewhere else.

    Returns the slot token used, or ``""`` if the directory wasn't
    malformed / couldn't be repaired.
    """
    base = os.path.basename(dir_path)
    parent = os.path.dirname(dir_path)
    tokens = re.findall(r'c\d{2}', base, flags=re.I)
    if len(tokens) < 2:
        return ""
    if re.fullmatch(r'c\d{2}', base, flags=re.I):
        return ""
    primary = tokens[0].lower()
    target = os.path.join(parent, primary)
    if os.path.exists(target):
        # First slot already owns a real folder — merge missing files
        # in (don't overwrite) and drop the malformed dir.
        for root_, _ds, fs in os.walk(dir_path):
            rel = os.path.relpath(root_, dir_path)
            dest_root = (target if rel == "."
                         else os.path.join(target, rel))
            os.makedirs(dest_root, exist_ok=True)
            for f in fs:
                dst = os.path.join(dest_root, f)
                if not os.path.exists(dst):
                    try:
                        shutil.copy2(os.path.join(root_, f), dst)
                    except Exception:
                        pass
        try:
            shutil.rmtree(dir_path, ignore_errors=True)
        except Exception:
            return ""
    else:
        try:
            os.rename(dir_path, target)
        except Exception:
            return ""
    return primary


def _split_multislot_file(file_path, slot_re=re.compile(
        r'(?P<prefix>.+?_)(?P<head>\d{2})(?P<sep>[,;\s]+)c?(?P<tail>\d{2})'
        r'(?P<rest>(?:\s*,\s*c?\d{2})*)(?P<ext>\.[A-Za-z0-9]+)$')):
    """Repair a malformed multi-slot filename like
    ``chara_0_packun_00, c03.bntx`` by renaming it to the **first**
    slot only (``chara_0_packun_00.bntx``). See :func:`_split_multislot_dir`
    for why we don't clone into every listed slot.

    Returns the new path, or ``""`` if the filename wasn't malformed.
    """
    base = os.path.basename(file_path)
    parent = os.path.dirname(file_path)
    m = slot_re.match(base)
    if not m:
        return ""
    head = m.group("head")
    prefix = m.group("prefix")
    ext = m.group("ext")
    new_name = f"{prefix}{head}{ext}"
    dst = os.path.join(parent, new_name)
    if os.path.exists(dst) and dst != file_path:
        # Authoritative copy already there — just remove the malformed one.
        try:
            os.remove(file_path)
        except Exception:
            return ""
        return dst
    try:
        os.rename(file_path, dst)
    except Exception:
        return ""
    return dst


def _repair_multislot_artifacts(installed_dir):
    """Walk the entire installed mod tree and split any folders or
    files whose name encodes multiple slots in a single token (e.g.
    ``c00, c03`` or ``chara_0_packun_00, c03.bntx``) into proper
    per-slot copies.

    Mutating the tree while we walk would skip entries, so we pass
    twice with full snapshots.

    Returns ``(split_dirs, split_files)`` — counts for the report.
    """
    if not os.path.isdir(installed_dir):
        return 0, 0

    split_dirs = 0
    split_files = 0

    # ── Pass 1: directories ──
    # Collect all suspect dir paths first, then split. Walk top-down
    # because splitting a parent before recursing into it makes the
    # original path go away.
    suspects = []
    for root_, dirs, _files in os.walk(installed_dir):
        for d in dirs:
            tokens = re.findall(r'c\d{2}', d, flags=re.I)
            if len(tokens) >= 2 and not re.fullmatch(r'c\d{2}', d, re.I):
                suspects.append(os.path.join(root_, d))
    # Sort deepest-first so a malformed parent doesn't disappear before
    # we get to its (also malformed) children.
    suspects.sort(key=lambda p: p.count(os.sep), reverse=True)
    for s in suspects:
        if _split_multislot_dir(s):
            split_dirs += 1

    # ── Pass 2: files ──
    for root_, _ds, files in os.walk(installed_dir):
        for f in files:
            # Cheap pre-filter: any cXX or N,N token in a filename.
            if "," not in f and ";" not in f:
                continue
            full = os.path.join(root_, f)
            if _split_multislot_file(full):
                split_files += 1

    return split_dirs, split_files


def _slot_dir_signature(slot_dir):
    """Return a hashable signature describing the contents of a slot
    directory (relative path → file size).  Two slot dirs are treated
    as clones when their signatures match, regardless of slot number.
    """
    sig = []
    for root_, _ds, files in os.walk(slot_dir):
        rel = os.path.relpath(root_, slot_dir)
        for f in files:
            try:
                size = os.path.getsize(os.path.join(root_, f))
            except OSError:
                size = -1
            entry = f if rel == "." else f"{rel.replace(os.sep, '/')}/{f}"
            sig.append((entry, size))
    return tuple(sorted(sig))


def _dedupe_cloned_slots(installed_dir):
    """Find body-slot directories under ``fighter/<f>/model/body/``
    (and the parallel ``effect/`` / ``motion/`` / ``model/<part>/``
    trees) whose contents are byte-equivalent to a sibling slot, and
    remove all but the lowest-numbered slot.

    This undoes the damage from the earlier "split into both slots"
    repair: a single mod that ends up with identical ``c00/`` and
    ``c03/`` content was probably an over-eager clone, and keeping
    the duplicate causes the mod to override every slot it appears
    in (shadowing other mods the user actually assigned to those
    slots).

    Conservative: only deletes when the signature matches *exactly*
    (same relative paths + same file sizes). Anything that's been
    edited differs and is left alone.

    Returns the list of deleted paths (relative to ``installed_dir``).
    """
    if not os.path.isdir(installed_dir):
        return []

    deleted = []
    # Any directory whose direct children are slot dirs (cXX). We can't
    # know up front which subtrees have slot dirs, so just walk and
    # process every directory whose children look like slots.
    for root_, dirs, _files in os.walk(installed_dir):
        slot_dirs = [d for d in dirs if re.fullmatch(r'c\d{2}', d, re.I)]
        if len(slot_dirs) < 2:
            continue
        # Group identical-signature slot dirs together.
        by_sig = {}
        for d in sorted(slot_dirs):  # deterministic: lowest cXX wins
            full = os.path.join(root_, d)
            sig = _slot_dir_signature(full)
            if not sig:
                continue
            by_sig.setdefault(sig, []).append(full)
        for sig, paths in by_sig.items():
            if len(paths) < 2:
                continue
            # First path (lowest cXX) is authoritative — drop the rest.
            for dup in paths[1:]:
                try:
                    shutil.rmtree(dup)
                    deleted.append(os.path.relpath(dup, installed_dir))
                except Exception:
                    pass
    return deleted


def _dedupe_cloned_ui_bntx(installed_dir):
    """Drop UI portrait bntx files whose ``_NN`` slot suffix has no
    surviving body dir for that fighter, *and* whose content matches
    another slot's portrait byte-for-byte.

    Pairs with :func:`_dedupe_cloned_slots`: when we removed a clone
    body slot, the matching portrait it pointed at becomes orphan
    UI — but only if the portrait was a clone too. (Genuine multi-slot
    portrait packs leave each portrait with distinct content even if
    the body is a clone, so we don't touch those.)

    Returns the list of deleted paths (relative).
    """
    if not os.path.isdir(installed_dir):
        return []

    bntx_re = re.compile(r'^(chara_\d+_)([a-z][a-z0-9_]*?)_(\d{2})\.bntx$',
                         re.I)
    deleted = []
    for ui_kind in ("replace", "replace_patch"):
        chara_root = os.path.join(installed_dir, "ui", ui_kind, "chara")
        if not os.path.isdir(chara_root):
            continue
        for sub in os.listdir(chara_root):
            sub_dir = os.path.join(chara_root, sub)
            if not os.path.isdir(sub_dir):
                continue
            # Group bntx files by (prefix, fighter) → {slot: (path, size)}
            groups = {}
            for fn in os.listdir(sub_dir):
                m = bntx_re.match(fn)
                if not m:
                    continue
                prefix, fighter, slot = m.group(1), m.group(2).lower(), m.group(3)
                full = os.path.join(sub_dir, fn)
                try:
                    size = os.path.getsize(full)
                except OSError:
                    continue
                groups.setdefault((prefix, fighter), {})[slot] = (full, size)
            for key, by_slot in groups.items():
                if len(by_slot) < 2:
                    continue
                # Group identical-size files; keep lowest slot, drop rest.
                by_size = {}
                for slot, (path, size) in by_slot.items():
                    by_size.setdefault(size, []).append((slot, path))
                for size, items in by_size.items():
                    if len(items) < 2:
                        continue
                    items.sort()  # lowest slot first
                    for _slot, p in items[1:]:
                        try:
                            os.remove(p)
                            deleted.append(os.path.relpath(p, installed_dir))
                        except Exception:
                            pass
    return deleted


def _detect_single_fighter(mod_dir):
    """If a mod's ``fighter/`` tree contains exactly one fighter, return
    its internal name; otherwise return ``None``. Multi-fighter mods
    (movesets, modpacks) shouldn't be auto-reslotted."""
    fighter_root = os.path.join(mod_dir, "fighter")
    if not os.path.isdir(fighter_root):
        return None
    fighters = [d for d in os.listdir(fighter_root)
                if os.path.isdir(os.path.join(fighter_root, d))]
    return fighters[0] if len(fighters) == 1 else None


def _internal_body_slots(mod_dir, fighter):
    """Return the set of cXX body-slot dirs the mod actually contains
    for ``fighter``."""
    body = os.path.join(mod_dir, "fighter", fighter, "model", "body")
    if not os.path.isdir(body):
        return set()
    return {d.lower() for d in os.listdir(body)
            if os.path.isdir(os.path.join(body, d))
            and re.fullmatch(r'c\d{2}', d, re.I)}


def _parse_slot_tokens(name):
    """Pull every ``cXX`` token from a folder/file name in order."""
    return [t.lower() for t in re.findall(r'c\d{2}', name, re.I)]


def _rename_slot_in_mod(mod_dir, fighter, old_slot, new_slot):
    """Rename every per-slot artifact inside a mod from ``old_slot`` to
    ``new_slot`` for the given fighter:

      • Every directory whose name is exactly ``old_slot`` (anywhere
        in the mod tree — body, motion, effect, model/<part>/, …).
        Bottom-up so deep paths don't break mid-rename.
      • Every ``chara_*_<fighter>_<NN>.bntx`` UI portrait whose slot
        suffix matches ``old_slot``.

    If the destination already exists (e.g. mod also had ``new_slot``
    content for some reason), we *merge* missing files in and drop the
    old. Idempotent."""
    if old_slot == new_slot:
        return
    # ── Per-slot directories ──
    candidates = []
    for root_, dirs, _files in os.walk(mod_dir):
        for d in dirs:
            if d.lower() == old_slot.lower():
                candidates.append(os.path.join(root_, d))
    candidates.sort(key=lambda p: p.count(os.sep), reverse=True)
    for c in candidates:
        new = os.path.join(os.path.dirname(c), new_slot)
        if os.path.exists(new):
            for r2, _ds, fs in os.walk(c):
                rel = os.path.relpath(r2, c)
                dest = new if rel == "." else os.path.join(new, rel)
                os.makedirs(dest, exist_ok=True)
                for f in fs:
                    dst_f = os.path.join(dest, f)
                    if not os.path.exists(dst_f):
                        try:
                            shutil.move(os.path.join(r2, f), dst_f)
                        except Exception:
                            pass
            try:
                shutil.rmtree(c, ignore_errors=True)
            except Exception:
                pass
        else:
            try:
                os.rename(c, new)
            except Exception:
                pass

    # ── UI bntx files ──
    bntx_re = re.compile(
        r'^(chara_\d+_)([a-z][a-z0-9_]*?)_(\d{2})\.bntx$', re.I)
    new_num = new_slot[1:]
    old_num = old_slot[1:]
    for ui_kind in ("replace", "replace_patch"):
        chara_root = os.path.join(mod_dir, "ui", ui_kind, "chara")
        if not os.path.isdir(chara_root):
            continue
        for sub in os.listdir(chara_root):
            sub_dir = os.path.join(chara_root, sub)
            if not os.path.isdir(sub_dir):
                continue
            for fn in list(os.listdir(sub_dir)):
                m = bntx_re.match(fn)
                if not m:
                    continue
                if m.group(2).lower() != fighter.lower():
                    continue
                if m.group(3) != old_num:
                    continue
                new_name = f"{m.group(1)}{m.group(2)}_{new_num}.bntx"
                old_path = os.path.join(sub_dir, fn)
                new_path = os.path.join(sub_dir, new_name)
                if os.path.exists(new_path) and old_path != new_path:
                    try:
                        os.remove(old_path)
                    except Exception:
                        pass
                else:
                    try:
                        os.rename(old_path, new_path)
                    except Exception:
                        pass


def _rename_top_level_to_slot(mod_dir, new_slot):
    """Rename a top-level mod folder so its name reflects the
    single resolved slot (e.g. ``Ridley_Plant_c00, c03`` →
    ``Ridley_Plant_c03``). Returns the new path on success or the
    original on no-op / collision."""
    parent = os.path.dirname(mod_dir)
    base = os.path.basename(mod_dir)
    tokens = _parse_slot_tokens(base)
    if len(tokens) < 2:
        return mod_dir
    # Strip the trailing "_cXX(, cYY)+" run and append "_<new_slot>".
    stripped = re.sub(r'_?c\d{2}(?:[,;\s]+c?\d{2})*$', '', base, flags=re.I)
    stripped = stripped.rstrip("_- ")
    if not stripped:
        stripped = base
    new_base = f"{stripped}_{new_slot}"
    new_path = os.path.join(parent, new_base)
    if os.path.exists(new_path) and new_path != mod_dir:
        # Conflict on rename target — leave the folder alone, it'll
        # still work, just with the messy name.
        return mod_dir
    try:
        os.rename(mod_dir, new_path)
        return new_path
    except Exception:
        return mod_dir


def resolve_cross_mod_slot_conflicts(mods_root):
    """Find multi-slot-named mod folders whose chosen slot collides
    with a single-slot-named mod for the same fighter, and reslot the
    multi-slot mod to the first free token from its folder name.

    Returns ``[(old_folder, new_folder, fighter, old_slot, new_slot),
              ...]`` for the report.
    """
    moved = []
    if not os.path.isdir(mods_root):
        return moved

    # Pass 1: build occupancy from "fixed" (single-slot-named) mods.
    occupancy = {}     # (fighter, slot) -> set of mod folder names
    multi = []         # list of (folder_name, full_path, fighter, current_slots, tokens)
    for entry in os.listdir(mods_root):
        full = os.path.join(mods_root, entry)
        if not os.path.isdir(full):
            continue
        if not os.path.exists(os.path.join(full, ".gb_meta.json")):
            continue
        fighter = _detect_single_fighter(full)
        if not fighter:
            continue
        slots = _internal_body_slots(full, fighter)
        tokens = _parse_slot_tokens(entry)
        if len(tokens) >= 2:
            multi.append((entry, full, fighter, slots, tokens))
        else:
            for s in slots:
                occupancy.setdefault((fighter, s), set()).add(entry)

    # Pass 2: try to move each multi-slot-named mod off any colliding
    # slot to a free token from its name.
    for entry, full, fighter, slots, tokens in multi:
        # The mod may currently have one or more body slots; find each
        # that collides with a fixed mod for this fighter.
        for current in sorted(slots):
            others = occupancy.get((fighter, current), set()) - {entry}
            if not others:
                # Not colliding here; whatever it has is fine. Add to
                # occupancy so subsequent multi-slot mods see it.
                occupancy.setdefault((fighter, current), set()).add(entry)
                continue
            # Pick first token that's free.
            free = None
            for t in tokens:
                holders = occupancy.get((fighter, t), set()) - {entry}
                if not holders:
                    free = t
                    break
            if free is None:
                print(f"    ! {entry}: every slot in {tokens} is taken; "
                      f"manual fix needed (collides with {sorted(others)})")
                continue
            try:
                _rename_slot_in_mod(full, fighter, current, free)
            except Exception as e:
                print(f"    ! {entry}: reslot {current}→{free} failed: {e}")
                continue
            occupancy.setdefault((fighter, free), set()).add(entry)
            new_full = _rename_top_level_to_slot(full, free)
            moved.append((entry, os.path.basename(new_full),
                          fighter, current, free))
            # Update locals so subsequent iterations see the new path.
            full = new_full
            slots = _internal_body_slots(full, fighter)

    return moved


# ── Fighter model-tree requirements ────────────────────────────────
# Reserved for fighters where missing files in the source archive
# genuinely freeze the game. After testing, *no entries are needed* —
# ARCropolis falls back to vanilla files for any tree the mod doesn't
# ship, so a Plant skin that only retextures ``model/body`` works
# fine even though Plant also uses ``model/bosspackun``,
# ``model/mario``, and ``model/spikeball`` at runtime.
#
# Real Plant freezes come from:
#   • Cross-mod slot collisions (two mods writing the same cXX with
#     mismatched contents) — handled by the SD-level scan.
#   • Malformed multi-slot folders like ``c00, c03`` — handled by
#     :func:`_repair_multislot_artifacts`.
#   • Portrait-without-body or body-without-portrait — handled by
#     :func:`diagnose_freeze_risks`.
#
# Keep the dict as the integration point in case a future fighter
# really does need partial-coverage detection.
MULTI_MODEL_FIGHTERS: dict[str, list[str]] = {}


def simulate_resolved_layout(mods_root):
    """Walk every mod folder under ``mods_root`` and produce the
    per-fighter, per-slot view the SSBU file resolver will see at
    runtime — *without* booting the Switch.

    Returns ``{fighter: {slot: {"model_dirs": {tree: [mod, ...]},
                                "ui_portraits": [(chara_dir, mod), ...],
                                "owners": {mod, ...},
                                "has_motion": bool,
                                "has_effect": bool}}}``.
    """
    layout = {}
    if not os.path.isdir(mods_root):
        return layout

    bntx_re = re.compile(
        r'^chara_\d+_([a-z][a-z0-9_]*?)_(\d{2})\.bntx$', re.I)

    for entry in os.listdir(mods_root):
        full = os.path.join(mods_root, entry)
        if not os.path.isdir(full):
            continue

        fighter_root = os.path.join(full, "fighter")
        if os.path.isdir(fighter_root):
            for fighter in os.listdir(fighter_root):
                model_root = os.path.join(fighter_root, fighter, "model")
                motion_root = os.path.join(fighter_root, fighter, "motion")
                effect_root = os.path.join(full, "effect", "fighter", fighter)
                if not os.path.isdir(model_root):
                    continue
                for tree in os.listdir(model_root):
                    tree_dir = os.path.join(model_root, tree)
                    if not os.path.isdir(tree_dir):
                        continue
                    for slot in os.listdir(tree_dir):
                        if not re.fullmatch(r'c\d{2}', slot, re.I):
                            continue
                        slot_dir = os.path.join(tree_dir, slot)
                        if not os.path.isdir(slot_dir):
                            continue
                        has_files = False
                        for _r, _ds, fs in os.walk(slot_dir):
                            if fs:
                                has_files = True
                                break
                        if not has_files:
                            continue
                        f_layout = layout.setdefault(fighter.lower(), {})
                        s_layout = f_layout.setdefault(slot.lower(), {
                            "model_dirs": {},
                            "ui_portraits": [],
                            "owners": set(),
                            "has_motion": False,
                            "has_effect": False,
                        })
                        s_layout["model_dirs"].setdefault(
                            tree.lower(), []).append(entry)
                        s_layout["owners"].add(entry)
                        motion_slot = os.path.join(motion_root, "body", slot)
                        if (os.path.isdir(motion_slot)
                                and any(os.scandir(motion_slot))):
                            s_layout["has_motion"] = True
                        if os.path.isdir(effect_root):
                            for f in os.listdir(effect_root):
                                if slot.lower() in f.lower():
                                    s_layout["has_effect"] = True
                                    break

        for ui_kind in ("replace", "replace_patch"):
            chara_root = os.path.join(full, "ui", ui_kind, "chara")
            if not os.path.isdir(chara_root):
                continue
            for sub in os.listdir(chara_root):
                sub_dir = os.path.join(chara_root, sub)
                if not os.path.isdir(sub_dir):
                    continue
                for fn in os.listdir(sub_dir):
                    m = bntx_re.match(fn)
                    if not m:
                        continue
                    fighter = m.group(1).lower()
                    slot = f"c{m.group(2)}"
                    f_layout = layout.setdefault(fighter, {})
                    s_layout = f_layout.setdefault(slot, {
                        "model_dirs": {},
                        "ui_portraits": [],
                        "owners": set(),
                        "has_motion": False,
                        "has_effect": False,
                    })
                    s_layout["ui_portraits"].append((sub, entry))
                    s_layout["owners"].add(entry)

    return layout


def diagnose_freeze_risks(mods_root):
    """Run :func:`simulate_resolved_layout` and return a list of
    high-confidence freeze causes.

    Each diagnostic is a dict with ``severity`` ('freeze' | 'warning'),
    ``fighter``, ``slot``, ``issue``, ``detail``, ``mods``.
    """
    diagnostics = []
    layout = simulate_resolved_layout(mods_root)

    for fighter, slots in sorted(layout.items()):
        required = MULTI_MODEL_FIGHTERS.get(fighter)
        for slot, info in sorted(slots.items()):
            owners = sorted(info["owners"])
            present = set(info["model_dirs"].keys())

            # 1. Missing companion model trees (Plant, Trainer, …).
            if required and "body" in present:
                missing = [t for t in required if t not in present]
                if missing:
                    diagnostics.append({
                        "severity": "freeze",
                        "fighter": fighter,
                        "slot": slot,
                        "issue": ("missing model tree(s): "
                                  + ", ".join(missing)),
                        "detail": (
                            f"{fighter} {slot} has body content but is "
                            f"missing model/{', model/'.join(missing)} "
                            f"— SSBU freezes on character pick when a "
                            f"slot has body without all required model "
                            f"directories."),
                        "mods": owners,
                    })

            # 2. Portrait without body — game tries to load missing model.
            if info["ui_portraits"] and "body" not in present:
                diagnostics.append({
                    "severity": "freeze",
                    "fighter": fighter,
                    "slot": slot,
                    "issue": "portrait without body",
                    "detail": (
                        f"{fighter} {slot} has a UI portrait but no "
                        f"body model — freezes when the portrait "
                        f"references a missing body."),
                    "mods": owners,
                })

            # 3. Body without portrait — cosmetic.
            if not info["ui_portraits"] and "body" in present:
                diagnostics.append({
                    "severity": "warning",
                    "fighter": fighter,
                    "slot": slot,
                    "issue": "body without portrait",
                    "detail": (
                        f"{fighter} {slot} has body model but no "
                        f"chara portrait. Vanilla portrait will be "
                        f"reused; not a freeze, just visual mismatch."),
                    "mods": owners,
                })

            # 4. Multiple mods contributing the same model tree.
            for tree, contribs in info["model_dirs"].items():
                if len(contribs) > 1:
                    diagnostics.append({
                        "severity": "warning",
                        "fighter": fighter,
                        "slot": slot,
                        "issue": ("multiple mods writing "
                                  f"model/{tree}"),
                        "detail": (
                            f"{fighter} {slot} model/{tree} written by "
                            f"{len(contribs)} mod(s): "
                            + ", ".join(contribs)
                            + " — last load wins."),
                        "mods": contribs,
                    })

    diagnostics.sort(key=lambda d: (
        0 if d["severity"] == "freeze" else 1,
        d["fighter"], d["slot"]))
    return diagnostics



def _archive_has_fatal_author_bug(extracted_dir):
    """Inspect a mod's already-extracted archive cache for fatal
    author bugs that will crash SSBU.

    Returns ``(is_broken: bool, reason: str)``. Detects:

      • Body tree ships custom ``model.nusktb`` (skeleton) but no
        matching ``model.nuhlpb`` (helper bones). The vanilla
        nuhlpb references vanilla bone IDs that don't exist in the
        custom skel — game crashes on **match load** when the
        engine first evaluates helper bones (CSS still works
        because it shows the static rest pose). This is the
        smoking-gun pattern for the user-confirmed
        "Doctor Doomario / Dr. Fate Mario crash on match load"
        case while Mini Ganondorf (which DOES ship nuhlpb) works.

    The earlier "numshb without nusktb" check was demoted because
    most Kirby/WFT skin retex mods ship that way intentionally and
    work fine via vanilla-bone fallback.

    Run before installing to the SD so we can refuse / warn instead
    of silently shipping a guaranteed crash.
    """
    if not extracted_dir or not os.path.isdir(extracted_dir):
        return False, ""
    fighter_root = None
    for r, ds, _fs in os.walk(extracted_dir):
        if "fighter" in ds:
            fighter_root = os.path.join(r, "fighter")
            break
    if not fighter_root:
        return False, ""
    for fighter in os.listdir(fighter_root):
        body = os.path.join(fighter_root, fighter, "model", "body")
        if not os.path.isdir(body):
            continue
        for slot in os.listdir(body):
            if not re.fullmatch(r"c\d{2}", slot, re.I):
                continue
            slot_dir = os.path.join(body, slot)
            if not os.path.isdir(slot_dir):
                continue
            files = set(os.listdir(slot_dir))
            has_mesh = "model.numshb" in files
            has_skel = "model.nusktb" in files
            has_helper = "model.nuhlpb" in files
            if has_mesh and has_skel and not has_helper:
                return True, (
                    f"body/{slot.lower()} ships custom model.nusktb "
                    "(skeleton) but no model.nuhlpb (helper bones) — "
                    "will crash on match load when helper bones "
                    "evaluate against the custom skel")
    return False, ""


def _eject_volume_win32(drive_letter):
    """Safely dismount a Windows volume so the user can pull the
    cable / SD card. Returns ``(ok: bool, message: str)``.

    Implements the canonical lock → dismount → eject sequence that
    Windows' own "Safely Remove Hardware" tray icon performs. Works
    for both true removable volumes and "fixed" USB mass storage
    devices (the Switch in USB-storage mode usually reports as
    fixed, so the simpler shell ``Eject`` verb fails on it — this
    routine handles both).
    """
    import ctypes
    from ctypes import wintypes
    drive_letter = drive_letter.strip(":\\/")
    if not drive_letter:
        return False, "no drive letter"

    GENERIC_READ = 0x80000000
    GENERIC_WRITE = 0x40000000
    FILE_SHARE_READ = 0x00000001
    FILE_SHARE_WRITE = 0x00000002
    OPEN_EXISTING = 3
    FSCTL_LOCK_VOLUME = 0x00090018
    FSCTL_DISMOUNT_VOLUME = 0x00090020
    IOCTL_STORAGE_MEDIA_REMOVAL = 0x002D4804
    IOCTL_STORAGE_EJECT_MEDIA = 0x002D4808
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    kernel32 = ctypes.windll.kernel32
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
        wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
        wintypes.HANDLE]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.DeviceIoControl.argtypes = [
        wintypes.HANDLE, wintypes.DWORD,
        wintypes.LPVOID, wintypes.DWORD,
        wintypes.LPVOID, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
    kernel32.DeviceIoControl.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    path = f"\\\\.\\{drive_letter.upper()}:"
    handle = kernel32.CreateFileW(
        path, GENERIC_READ | GENERIC_WRITE,
        FILE_SHARE_READ | FILE_SHARE_WRITE,
        None, OPEN_EXISTING, 0, None)
    if not handle or handle == INVALID_HANDLE_VALUE:
        err = ctypes.get_last_error() or "unknown"
        return False, (f"Couldn't open volume {path} (error {err}). "
                       "Try running as administrator.")

    bytes_returned = wintypes.DWORD(0)
    try:
        # 1. LOCK — try a few times; opens may release shortly.
        locked = False
        for attempt in range(8):
            if kernel32.DeviceIoControl(
                    handle, FSCTL_LOCK_VOLUME, None, 0, None, 0,
                    ctypes.byref(bytes_returned), None):
                locked = True
                break
            time.sleep(0.25)
        if not locked:
            err = ctypes.get_last_error() or "unknown"
            return False, (
                "Couldn't lock the volume — something else has "
                f"a handle open on it (error {err}). Close any "
                "Explorer windows / 3D viewer popups pointed at "
                "the SD and try again.")

        # 2. DISMOUNT — invalidate the file system so reads/writes
        # immediately fail. This alone is enough to make "Safe to
        # Remove" succeed for most fixed-disk USB volumes.
        if not kernel32.DeviceIoControl(
                handle, FSCTL_DISMOUNT_VOLUME, None, 0, None, 0,
                ctypes.byref(bytes_returned), None):
            err = ctypes.get_last_error() or "unknown"
            return False, f"Couldn't dismount the volume (error {err})."

        # 3. PreventRemoval=FALSE — let the device know media can
        # safely come out. (Some drivers refuse the eject IOCTL
        # without this.)
        prevent = ctypes.c_byte(0)  # FALSE
        kernel32.DeviceIoControl(
            handle, IOCTL_STORAGE_MEDIA_REMOVAL,
            ctypes.byref(prevent), 1, None, 0,
            ctypes.byref(bytes_returned), None)

        # 4. EJECT — request the device spin down / disconnect.
        # Returns success on USB mass storage even when the device
        # itself can't power-eject (e.g. dock-attached drives).
        kernel32.DeviceIoControl(
            handle, IOCTL_STORAGE_EJECT_MEDIA, None, 0, None, 0,
            ctypes.byref(bytes_returned), None)

        return True, "ok"
    finally:
        try:
            kernel32.CloseHandle(handle)
        except Exception:
            pass


def deep_diagnose_mod_slot(slot_dir, slot_rel_prefix=None,
                           registered_rel_paths=None):
    """Parse the SSBH binaries inside a single ``cXX`` slot folder and
    return a list of issue dicts.

    Catches the failure modes that actually crash SSBU:

      • Mesh present but **skeleton (nusktb) missing** — the mod
        ships ``model.numshb`` (and matl, etc.) but no skel. SSBU
        loads the mesh, can't find bones to drive animations,
        crashes on character-select.
      • Empty mesh (``numshb`` parses but has 0 mesh objects).
      • Empty / unparseable skeleton.
      • Modl → mesh references out of sync with the actual numshb.
      • Unparseable SSBH binaries (corrupted file).

    ``slot_rel_prefix`` is the slot dir's path relative to mod root,
    e.g. ``"fighter/mariod/model/body/c07"`` (lower-case, forward
    slashes). When combined with ``registered_rel_paths`` (a set of
    lower-cased forward-slash paths from ``config.json``'s
    ``new-dir-files``), freeze-severity checks against files that
    are NOT in that set are skipped — ARCropolis won't overlay an
    unregistered file, so a broken file the author left on disk but
    didn't register can't crash the game. When either is ``None``,
    every file on disk is treated as if it could be loaded
    (legacy / no-config-shipped behaviour).

    Returns ``[{"severity": "freeze"|"warning", "issue": ..., "detail": ...}, ...]``.
    Empty list = the slot looks healthy as far as we can tell.
    """
    issues = []
    if not os.path.isdir(slot_dir):
        return issues

    def _is_registered(filename):
        if registered_rel_paths is None or slot_rel_prefix is None:
            return True
        return f"{slot_rel_prefix}/{filename.lower()}" in registered_rel_paths
    has_numdlb = os.path.isfile(os.path.join(slot_dir, "model.numdlb"))
    has_numshb = os.path.isfile(os.path.join(slot_dir, "model.numshb"))
    has_numatb = os.path.isfile(os.path.join(slot_dir, "model.numatb"))
    has_nusktb = os.path.isfile(os.path.join(slot_dir, "model.nusktb"))
    if not has_numdlb and not has_numshb:
        # Texture-only slot (legitimate — many retex mods ship just
        # nutexb files alongside an unmodded model).
        return issues

    # Mesh-without-skeleton: a yellow flag, not a red one. Whether
    # this actually crashes in-game depends on whether the mod's
    # mesh uses vanilla-compatible bones (works fine — many Kirby
    # texture/topology swaps ship this way intentionally) or
    # custom bones (will crash without a matching ``model.nusktb``).
    # Without parsing the mesh's bone references we can't tell, so
    # surface as a warning so the user can investigate without
    # being told the mod is definitely broken.
    BODY_TREE_NAMES = {"body", "fighter", "head", "weapon"}
    tree_name = os.path.basename(os.path.dirname(slot_dir)).lower()
    if has_numshb and not has_nusktb and tree_name in BODY_TREE_NAMES:
        issues.append({
            "severity": "warning",
            "issue": "no model.nusktb (skeleton) shipped",
            "detail": (
                "Mod ships a body mesh but no skeleton. Harmless "
                "if the mesh uses vanilla bones (common for "
                "texture/topology re-skins); freezes the game if "
                "the mesh uses custom bones. Test in-game — if "
                "the slot crashes, the mod author needs to ship a "
                "matching nusktb."),
        })

    # Custom skeleton without a matching helper-bone file. SSBU's
    # vanilla ``model.nuhlpb`` references vanilla bone IDs; if the
    # mod ships a custom ``model.nusktb`` (different hierarchy) but
    # not a matching ``model.nuhlpb``, the engine evaluates helper
    # bones against the custom skel and panics when it can't find
    # the vanilla bone names. CSS still loads (static rest pose)
    # but **match load crashes** the moment animations start driving
    # the helper rig. This is the smoking gun for the user's
    # "Doctor Doomario / Dr. Fate Mario crash on match load" case
    # while Mini Ganondorf (which DOES ship nuhlpb) works.
    has_nuhlpb = os.path.isfile(os.path.join(slot_dir, "model.nuhlpb"))
    # Only flag if ARC will actually load the broken nusktb. Many
    # mods ship a leftover nusktb in the slot dir but don't register
    # it in config.json — ARC ignores those files entirely so they
    # can't crash the game.
    if (has_nusktb and has_numshb and not has_nuhlpb
            and tree_name in BODY_TREE_NAMES
            and _is_registered("model.nusktb")):
        issues.append({
            "severity": "freeze",
            "issue": (
                "custom skeleton without matching model.nuhlpb"),
            "detail": (
                "Mod ships a custom model.nusktb (skeleton) but no "
                "model.nuhlpb (helper bones). SSBU loads the static "
                "mesh fine on character-select, but crashes on "
                "match load when the engine evaluates helper bones "
                "against the custom skeleton and can't find the "
                "vanilla bone IDs they reference. Author needs to "
                "export and ship a matching model.nuhlpb."),
        })

    # Custom skel on body tree without matching motion subtree.
    # When the mod overrides the bone hierarchy but ships no per-slot
    # motion files at fighter/<f>/motion/<tree>/cXX/, ARCropolis loads
    # vanilla motion against the custom skel — vanilla motion targets
    # bones by name, and any rename/removal in the custom skel
    # crashes the animation system on match start. Empirically the
    # other half of the "freeze on match load" pattern: complete-
    # bundle mods that ship nusktb+nuhlpb but no motion (Football
    # Mario, Robo Mario, Dr. Fate Mario) freeze where Mini Ganondorf
    # (which ships motion/body/cXX) does not.
    if (has_nusktb and tree_name in BODY_TREE_NAMES
            and _is_registered("model.nusktb")):
        motion_slot = slot_dir.replace(
            os.sep + "model" + os.sep,
            os.sep + "motion" + os.sep, 1)
        motion_has_files = False
        if os.path.isdir(motion_slot):
            for _r, _ds, fs in os.walk(motion_slot):
                if fs:
                    motion_has_files = True
                    break
        if not motion_has_files:
            issues.append({
                "severity": "freeze",
                "issue": (
                    "custom skeleton without matching motion subtree"),
                "detail": (
                    "Mod ships a custom model.nusktb on the body "
                    "tree but no motion files at the matching "
                    "fighter/<f>/motion/<tree>/cXX/ path. SSBU "
                    "loads vanilla motion against the custom skel; "
                    "any renamed/removed bone crashes animation on "
                    "match start. Either ship matching motion or "
                    "drop the custom skel."),
            })

    # Partial model bundle: at least one of the three core assets
    # (numshb / numdlb / numatb) is present and registered but at
    # least one other is missing. SSBU resolves the missing pieces
    # from vanilla and the present pieces from the mod, producing
    # cross-references that don't line up (vanilla numdlb's mesh
    # names don't match the custom numshb, custom numatb materials
    # aren't referenced by vanilla numdlb, etc). Match-start freeze.
    has_numshb_loaded = has_numshb and _is_registered("model.numshb")
    has_numdlb_loaded = has_numdlb and _is_registered("model.numdlb")
    has_numatb_loaded = has_numatb and _is_registered("model.numatb")
    core_loaded = sum(
        (has_numshb_loaded, has_numdlb_loaded, has_numatb_loaded))
    if (core_loaded > 0 and core_loaded < 3
            and tree_name in BODY_TREE_NAMES):
        missing = []
        if not has_numshb_loaded: missing.append("numshb")
        if not has_numdlb_loaded: missing.append("numdlb")
        if not has_numatb_loaded: missing.append("numatb")
        present = []
        if has_numshb_loaded: present.append("numshb")
        if has_numdlb_loaded: present.append("numdlb")
        if has_numatb_loaded: present.append("numatb")
        issues.append({
            "severity": "freeze",
            "issue": "partial model bundle on body tree",
            "detail": (
                f"Mod ships {', '.join(present)} but not "
                f"{', '.join(missing)}. SSBU mixes the custom "
                "files with vanilla for the missing pieces, "
                "producing cross-references that don't resolve "
                "(vanilla modl mesh names vs custom shb, custom "
                "matl labels not in vanilla modl, etc). Either "
                "ship the full bundle or drop everything but "
                "textures."),
        })

    try:
        import ssbh_data_py
    except ImportError:
        return issues  # diagnostic requires the parser

    # ── Material → texture references ──
    matl = None
    if has_numatb:
        try:
            matl = ssbh_data_py.matl_data.read_matl(
                os.path.join(slot_dir, "model.numatb"))
        except Exception as e:
            if _is_registered("model.numatb"):
                issues.append({
                    "severity": "freeze",
                    "issue": "model.numatb unparseable",
                    "detail": f"{e}",
                })

    # NOTE: a previous version of this check walked the matl's
    # texture references and flagged anything not present as a
    # ``.nutexb`` in the slot dir. That fired on ~every Doc Mario /
    # Wii Fit / Kirby skin because the mod's matl references vanilla
    # textures (``alp_mariod_001_col``, ``def_wiifit_001_nor``,
    # ``KirbyGamewatchEye*``, …) that ARCropolis resolves to the
    # base game — not actually missing. Filtering them out properly
    # needs the canonical reslotter's ``dir_info_with_files_trimmed.json``
    # vanilla index, which we don't ship. Dropped until we can
    # filter accurately; freeze-severity structural checks below
    # are far more reliable.

    # ── Mesh count ──
    if has_numshb:
        numshb_loaded = _is_registered("model.numshb")
        try:
            mesh = ssbh_data_py.mesh_data.read_mesh(
                os.path.join(slot_dir, "model.numshb"))
            mc = len(getattr(mesh, "objects", []) or [])
            if mc == 0 and numshb_loaded:
                issues.append({
                    "severity": "freeze",
                    "issue": "model.numshb has 0 meshes",
                    "detail": ("Empty meshlist crashes the game when "
                               "the slot is selected."),
                })
        except Exception as e:
            if numshb_loaded:
                issues.append({
                    "severity": "freeze",
                    "issue": "model.numshb unparseable",
                    "detail": f"{e}",
                })

    # ── Skeleton sanity ──
    if has_nusktb:
        nusktb_loaded = _is_registered("model.nusktb")
        try:
            skel = ssbh_data_py.skel_data.read_skel(
                os.path.join(slot_dir, "model.nusktb"))
            bones = getattr(skel, "bones", []) or []
            if len(bones) == 0 and nusktb_loaded:
                issues.append({
                    "severity": "freeze",
                    "issue": "model.nusktb has 0 bones",
                    "detail": ("Empty skeleton — the game expects at "
                               "least a root bone."),
                })
        except Exception as e:
            if nusktb_loaded:
                issues.append({
                    "severity": "freeze",
                    "issue": "model.nusktb unparseable",
                    "detail": f"{e}",
                })

    # ── Modl ↔ Mesh consistency ──
    if has_numdlb and has_numshb:
        modl_loaded = (_is_registered("model.numdlb")
                       and _is_registered("model.numshb"))
        try:
            modl = ssbh_data_py.modl_data.read_modl(
                os.path.join(slot_dir, "model.numdlb"))
            mesh = ssbh_data_py.mesh_data.read_mesh(
                os.path.join(slot_dir, "model.numshb"))
            mesh_names = {(o.name, o.subindex)
                          for o in (mesh.objects or [])}
            orphans = []
            for entry in (modl.entries or []):
                key = (entry.mesh_object_name, entry.mesh_object_subindex)
                if key not in mesh_names:
                    orphans.append(entry.mesh_object_name)
            if orphans and modl_loaded:
                preview = ", ".join(orphans[:5])
                extra = (f" (+{len(orphans)-5} more)"
                         if len(orphans) > 5 else "")
                issues.append({
                    "severity": "warning",
                    "issue": (f"modl references {len(orphans)} mesh "
                              "object(s) absent from numshb"),
                    "detail": f"{preview}{extra}",
                })
        except Exception:
            # Already reported above if either file is unparseable.
            pass

    return issues


def deep_diagnose_mods_root(mods_root):
    """Run :func:`deep_diagnose_mod_slot` against every fighter slot
    on the SD card AND apply mod-level checks modeled on what the
    canonical reslotter expects on disk for ARCropolis to load a
    mod correctly:

      • ``config.json`` exists and has ``new-dir-files`` entries
        registering every custom file the mod ships.
      • The expected base file set is present per slot
        (``numshb`` + ``nusktb`` + ``numatb`` + ``numdlb``).
      • Multi-model fighters (Plant body+mario+spikeball,
        Trainer fighter+pfushigisou+plizardon+pzenigame, mariod
        body+capsule trees) have every required tree present.
      • Slot has files on disk but zero registrations in
        ``config.json``'s ``new-dir-files`` — the mod's config is
        missing entries for this tree, so ARC won't load anything.
        (Files on disk that AREN'T registered alongside files that
        ARE registered are deliberate author leftovers, not flagged.)
    """
    out = []
    if not os.path.isdir(mods_root):
        return out

    for mod_folder in os.listdir(mods_root):
        mod_dir = os.path.join(mods_root, mod_folder)
        if not os.path.isdir(mod_dir):
            continue
        fighter_root = os.path.join(mod_dir, "fighter")
        if not os.path.isdir(fighter_root):
            continue

        # Load config.json (or note its absence).
        cfg_path = os.path.join(mod_dir, "config.json")
        cfg = None
        if os.path.isfile(cfg_path):
            try:
                with open(cfg_path, "r", encoding="utf-8") as fh:
                    cfg = json.load(fh) or {}
            except Exception as e:
                out.append({
                    "mod": mod_folder, "fighter": "?",
                    "tree": "config", "slot": "—",
                    "severity": "freeze",
                    "issue": "config.json unparseable",
                    "detail": str(e),
                })
                cfg = None
        # Build set of files that ARE registered, keyed by their
        # relative path. Lower-cased + forward-slash for case-
        # insensitive comparison on Windows.
        registered = set()
        if cfg:
            ndf = cfg.get("new-dir-files") or {}
            for entries in ndf.values():
                if isinstance(entries, list):
                    for e in entries:
                        if isinstance(e, str):
                            registered.add(e.lower().replace("\\", "/"))

        for fighter in os.listdir(fighter_root):
            f_dir = os.path.join(fighter_root, fighter)
            model_dir = os.path.join(f_dir, "model")
            if not os.path.isdir(model_dir):
                continue
            trees_present = set(os.listdir(model_dir))
            slots_per_tree = {}
            for tree in trees_present:
                tree_dir = os.path.join(model_dir, tree)
                if not os.path.isdir(tree_dir):
                    continue
                slots_per_tree[tree] = sorted(
                    s for s in os.listdir(tree_dir)
                    if re.fullmatch(r"c\d{2}", s, re.I)
                    and os.path.isdir(os.path.join(tree_dir, s)))

            # CHECK A: multi-tree fighters need every required tree.
            required_trees = MULTI_MODEL_FIGHTERS.get(
                fighter.lower(), [])
            customised_slots = set()
            for slots in slots_per_tree.values():
                for s in slots:
                    customised_slots.add(s.lower())
            for slot in customised_slots:
                missing_trees = [
                    t for t in required_trees
                    if slot not in slots_per_tree.get(t, [])]
                if missing_trees:
                    out.append({
                        "mod": mod_folder,
                        "fighter": fighter.lower(),
                        "tree": "model",
                        "slot": slot,
                        "severity": "freeze",
                        "issue": (f"missing companion model tree(s): "
                                  + ", ".join(missing_trees)),
                        "detail": (
                            f"{INTERNAL_TO_DISPLAY.get(fighter.lower(), fighter)} "
                            f"is a multi-model fighter — picking the "
                            "slot will freeze if any companion tree "
                            "is missing for that slot."),
                    })

            # CHECK B + C: per-slot deep diagnose + orphan-file check.
            for tree, slots in slots_per_tree.items():
                tree_dir = os.path.join(model_dir, tree)
                for slot in slots:
                    slot_dir = os.path.join(tree_dir, slot)
                    slot_rel_prefix = (
                        f"fighter/{fighter}/model/{tree}/"
                        f"{slot}").lower()
                    # B: deep diagnose (parse SSBH binaries). Pass
                    # the registered-paths set so checks against
                    # files ARC won't load are suppressed — many
                    # mods ship leftover broken files in the slot
                    # dir but only register the custom-named ones,
                    # so on-disk anomalies don't always indicate a
                    # crash risk.
                    for issue in deep_diagnose_mod_slot(
                            slot_dir,
                            slot_rel_prefix=slot_rel_prefix,
                            registered_rel_paths=(
                                registered if cfg else None)):
                        out.append({
                            "mod": mod_folder,
                            "fighter": fighter.lower(),
                            "tree": tree,
                            "slot": slot.lower(),
                            **issue,
                        })
                    # C: orphan-file warning. Only fires when the
                    # slot has files on disk but ZERO of them are
                    # registered — that signals a real config gap
                    # (likely from an outright missing or broken
                    # config). When the slot has SOME registered
                    # files, the others are intentional author
                    # leftovers ARC ignores; flagging them is just
                    # noise.
                    if cfg and registered:
                        files_in_slot = []
                        registered_in_slot = 0
                        for fn in os.listdir(slot_dir):
                            full = os.path.join(slot_dir, fn)
                            if os.path.isfile(full):
                                rel = f"{slot_rel_prefix}/{fn.lower()}"
                                if rel in registered:
                                    registered_in_slot += 1
                                else:
                                    files_in_slot.append(fn)
                        if files_in_slot and registered_in_slot == 0:
                            preview = ", ".join(files_in_slot[:5])
                            extra = (f" (+{len(files_in_slot)-5} more)"
                                     if len(files_in_slot) > 5 else "")
                            out.append({
                                "mod": mod_folder,
                                "fighter": fighter.lower(),
                                "tree": tree,
                                "slot": slot.lower(),
                                "severity": "warning",
                                "issue": (
                                    f"{len(files_in_slot)} file(s) "
                                    "on disk but slot has no "
                                    "config.json registrations"),
                                "detail": (
                                    "ARCropolis won't load any of "
                                    "these — likely the mod's "
                                    "config is missing entries for "
                                    f"this tree. Files: "
                                    f"{preview}{extra}"),
                            })

        # CHECK D: mod ships custom assets but no config.json at all.
        any_custom_files = False
        for r, _ds, fs in os.walk(fighter_root):
            if any(f.endswith((".numshb", ".numatb", ".numdlb",
                                ".nutexb", ".nusktb"))
                   for f in fs):
                any_custom_files = True
                break
        if any_custom_files and cfg is None:
            out.append({
                "mod": mod_folder, "fighter": "?",
                "tree": "config", "slot": "—",
                "severity": "warning",
                "issue": "config.json missing",
                "detail": (
                    "Mod ships custom fighter assets but doesn't "
                    "have a config.json registering them with "
                    "ARCropolis. Files may not be loaded; reinstall "
                    "the mod through the app to regenerate one."),
            })

        # CHECK E: config.json registers paths that don't exist on
        # disk — over-registered "new-dir-files" entries (common when
        # a multi-slot mod ships configs for slots whose actual files
        # we trimmed during install). ARCropolis tries to resolve
        # these and can crash when the file is referenced for an
        # asset that's structurally critical.
        if cfg:
            ndf = cfg.get("new-dir-files") or {}
            orphaned = []
            for key, entries in ndf.items():
                if not isinstance(entries, list):
                    continue
                for entry in entries:
                    if not isinstance(entry, str):
                        continue
                    rel = entry.replace("/", os.sep)
                    full = os.path.join(mod_dir, rel)
                    if not os.path.isfile(full):
                        orphaned.append(entry)
            if orphaned:
                preview = ", ".join(orphaned[:4])
                extra = (f" (+{len(orphaned)-4} more)"
                         if len(orphaned) > 4 else "")
                out.append({
                    "mod": mod_folder, "fighter": "—",
                    "tree": "config", "slot": "—",
                    "severity": "warning",
                    "issue": (
                        f"config.json references {len(orphaned)} "
                        "path(s) that don't exist on disk"),
                    "detail": (
                        "ARCropolis will fail to find these and may "
                        "crash adjacent slots: "
                        f"{preview}{extra}"),
                })

    return out


def _strip_freeze_risks_in_mod(mod_dir):
    """No-op. An earlier version of this function aggressively
    stripped model bundles for any slot the diagnostic flagged as
    "freeze risk" (custom skel without nuhlpb / without motion /
    partial bundle). That stripping turned out to be wrong — the
    same mods (Doctor Doomario, Robo Mario, etc) are used by
    other players without freezes, which proves the patterns
    aren't actually fatal at the mod level. Most likely the real
    freeze cause was elsewhere in the install pipeline (e.g. our
    own config-regen registering a previously-stripped path). The
    diagnostic still surfaces these patterns as informational
    warnings via deep_diagnose_mod_slot, but no longer triggers
    auto-strip. Returns ``[]`` always so callers see "nothing
    stripped" without the helper code path having to be removed.
    """
    return []


def _legacy_strip_freeze_risks_in_mod_unused(mod_dir):
    """Pre-revert implementation kept for diff legibility. Do not
    call. See ``_strip_freeze_risks_in_mod`` above for rationale.
    """
    MODEL_FILES = ("model.numatb", "model.numdlb", "model.numshb",
                   "model.numshexb", "model.nusktb", "model.nuhlpb",
                   "model.adjb")
    if not os.path.isdir(mod_dir):
        return []

    cfg_path = os.path.join(mod_dir, "config.json")
    cfg = None
    registered = set()
    if os.path.isfile(cfg_path):
        try:
            with open(cfg_path, "r", encoding="utf-8") as fh:
                cfg = json.load(fh) or {}
            for entries in (cfg.get("new-dir-files") or {}).values():
                if isinstance(entries, list):
                    for e in entries:
                        if isinstance(e, str):
                            registered.add(
                                e.lower().replace("\\", "/"))
        except Exception:
            cfg = None
            registered = set()

    fighter_root = os.path.join(mod_dir, "fighter")
    if not os.path.isdir(fighter_root):
        return []

    stripped = []

    for fighter in os.listdir(fighter_root):
        f_dir = os.path.join(fighter_root, fighter)
        model_dir = os.path.join(f_dir, "model")
        motion_dir = os.path.join(f_dir, "motion")
        if not os.path.isdir(model_dir):
            continue
        for tree in os.listdir(model_dir):
            tree_dir = os.path.join(model_dir, tree)
            if not os.path.isdir(tree_dir):
                continue
            for slot in os.listdir(tree_dir):
                slot_dir = os.path.join(tree_dir, slot)
                if (not os.path.isdir(slot_dir)
                        or not re.fullmatch(r"c\d{2}", slot, re.I)):
                    continue
                slot_rel_prefix = (
                    f"fighter/{fighter}/model/{tree}/"
                    f"{slot}").lower()

                # Pass registered_rel_paths=None so freeze rules
                # fire regardless of the author's current
                # config.json registrations. Rationale:
                # _regenerate_config_json runs LATER in the install
                # pipeline and registers every file on disk under
                # new-dir-files, so any broken model file we leave
                # behind here will be loaded by ARCropolis at runtime.
                # Gating on the author's (often incomplete) original
                # registration set lets these freeze patterns through
                # — confirmed by Doctor Doomario, whose author config
                # only registered nutexb files but ships a broken
                # custom nusktb that would otherwise survive the
                # strip and crash on match load.
                issues = deep_diagnose_mod_slot(
                    slot_dir,
                    slot_rel_prefix=slot_rel_prefix,
                    registered_rel_paths=None)
                freeze_issues = [
                    i for i in issues
                    if i.get("severity") == "freeze"]
                if not freeze_issues:
                    continue

                removed = []
                for fn in MODEL_FILES:
                    full = os.path.join(slot_dir, fn)
                    if os.path.isfile(full):
                        try:
                            os.remove(full)
                            removed.append(fn)
                        except OSError as e:
                            print(f"    Warning: could not strip "
                                  f"{full}: {e}")

                motion_slot = os.path.join(motion_dir, tree, slot)
                if os.path.isdir(motion_slot):
                    try:
                        shutil.rmtree(motion_slot)
                        removed.append(f"motion/{tree}/{slot}/*")
                    except OSError as e:
                        print(f"    Warning: could not strip "
                              f"{motion_slot}: {e}")

                if cfg:
                    ndf = cfg.get("new-dir-files") or {}
                    motion_prefix = (
                        f"fighter/{fighter}/motion/{tree}/"
                        f"{slot}/").lower()
                    model_files_lower = {f.lower()
                                          for f in MODEL_FILES}
                    pruned_any = False
                    for key in list(ndf.keys()):
                        entries = ndf[key]
                        if not isinstance(entries, list):
                            continue
                        kept = []
                        for entry in entries:
                            if not isinstance(entry, str):
                                kept.append(entry)
                                continue
                            entry_l = entry.lower().replace("\\", "/")
                            if entry_l.startswith(
                                    slot_rel_prefix + "/"):
                                tail = entry_l[
                                    len(slot_rel_prefix) + 1:]
                                if tail in model_files_lower:
                                    pruned_any = True
                                    continue
                            if entry_l.startswith(motion_prefix):
                                pruned_any = True
                                continue
                            kept.append(entry)
                        ndf[key] = kept
                    if pruned_any:
                        try:
                            with open(cfg_path, "w",
                                      encoding="utf-8") as fh:
                                json.dump(cfg, fh, indent=2)
                        except OSError as e:
                            print(f"    Warning: could not update "
                                  f"config.json: {e}")

                if removed:
                    stripped.append({
                        "slot_dir": slot_dir,
                        "issues": freeze_issues,
                        "removed": removed,
                    })
                    issue_summary = "; ".join(
                        i["issue"] for i in freeze_issues)
                    print(
                        f"    [auto-strip] {fighter}/{tree}/{slot}: "
                        f"{len(removed)} file(s) removed — freeze "
                        f"risk: {issue_summary}. Slot falls back "
                        "to vanilla; textures kept.")
    return stripped


def _strip_stray_dev_files_in_mod(mod_dir):
    """No-op while we test the bare-minimum install pipeline.

    Originally stripped uncompiled-source / authoring residue
    (model.nuanmb / model.nusrcmdlb / model.xmb / temp/ dirs /
    .wav files / default_params.nutexb). The user reports that
    skin mods used successfully by other players show as vanilla
    after our install pipeline runs — pointing at over-aggressive
    stripping somewhere in the pipeline. This function is no-op'd
    to verify whether stripping these "obvious junk" files is in
    fact the cause. If the un-stripped install works, we'll re-
    enable the rules selectively (one at a time, with empirical
    confirmation each is safe).
    """
    return []


def _legacy_strip_stray_dev_files_in_mod_unused(mod_dir):
    """Pre-revert implementation. Do not call. Kept for diff."""
    if not os.path.isdir(mod_dir):
        return []

    removed = []
    fighter_root = os.path.join(mod_dir, "fighter")
    if os.path.isdir(fighter_root):
        for fighter in os.listdir(fighter_root):
            f_dir = os.path.join(fighter_root, fighter)
            model_dir = os.path.join(f_dir, "model")
            if not os.path.isdir(model_dir):
                continue
            for tree in os.listdir(model_dir):
                tree_dir = os.path.join(model_dir, tree)
                if not os.path.isdir(tree_dir):
                    continue
                for slot in os.listdir(tree_dir):
                    slot_dir = os.path.join(tree_dir, slot)
                    if (not os.path.isdir(slot_dir)
                            or not re.fullmatch(
                                r"c\d{2}", slot, re.I)):
                        continue
                    for fn in (
                            "model.nuanmb",
                            "model.nusrcmdlb",
                            "model.xmb",
                            "default_params.nutexb"):
                        full = os.path.join(slot_dir, fn)
                        if os.path.isfile(full):
                            try:
                                os.remove(full)
                                rel = os.path.relpath(
                                    full, mod_dir
                                ).replace("\\", "/")
                                removed.append(rel)
                                print(f"    [auto-strip] {rel}: "
                                      "stray authoring file (would "
                                      "be registered by config and "
                                      "may freeze CSS preview).")
                            except OSError as e:
                                print(f"    Warning: could not "
                                      f"strip {full}: {e}")

    for r, ds, fs in os.walk(mod_dir, topdown=False):
        for d in list(ds):
            d_lower = d.lower()
            if (d_lower == "temp" or d_lower.endswith("_tmp")
                    or d_lower == "tmp"):
                full = os.path.join(r, d)
                try:
                    shutil.rmtree(full)
                    rel = os.path.relpath(
                        full, mod_dir).replace("\\", "/")
                    removed.append(f"{rel}/*")
                    print(f"    [auto-strip] {rel}/*: authoring "
                          "temp dir (voice-clone / texture-tool "
                          "dump leftovers).")
                except OSError as e:
                    print(f"    Warning: could not strip {full}: {e}")
        for fn in fs:
            if fn.lower().endswith(".wav"):
                full = os.path.join(r, fn)
                try:
                    os.remove(full)
                    rel = os.path.relpath(
                        full, mod_dir).replace("\\", "/")
                    removed.append(rel)
                    print(f"    [auto-strip] {rel}: stray .wav "
                          "(authoring residue).")
                except OSError as e:
                    print(f"    Warning: could not strip {full}: {e}")

    return removed


def _strip_invalid_nutexb_in_mod(mod_dir):
    """No-op. An earlier version of this function thought ``b" XNT"``
    was the NUTEXB tail magic and stripped any file lacking it —
    but the actual valid tail is ``b" XET"`` (verified against
    Mini Ganondorf, which works in-game). The check stripped 1240+
    legitimate textures across the cache + SD before the bug was
    caught. Kept as a no-op so the strip pipeline (install_to_sd,
    preflight cache check) can keep calling it without touching
    each call site. Returns ``[]`` always.
    """
    return []


def _detect_stray_nro_in_mod(mod_dir):
    """Return list of relative ``.nro`` paths inside a mod folder.

    ``.nro`` files are Skyline plugins; ARCropolis loads any it finds
    on the SD at boot. A stray ``.nro`` shipped inside a skin mod
    folder (commonly when a "Training Modpack" archive is extracted
    over a skin mod) gets loaded as a plugin, can be incompatible
    with the running build, and freezes the game on first match.
    Reference: github.com/Raytwo/ARCropolis/issues/173 (root cause:
    "it was the .nro files").
    """
    found = []
    if not os.path.isdir(mod_dir):
        return found
    for r, _ds, fs in os.walk(mod_dir):
        for fn in fs:
            if fn.lower().endswith(".nro"):
                found.append(
                    os.path.relpath(
                        os.path.join(r, fn), mod_dir
                    ).replace("\\", "/"))
    return found


def audit_mixed_added_base_slots(mods_root):
    """Detect the ARCropolis ≥3.7.0 freeze pattern: when a fighter
    has mods on BOTH base slots (c00..c07) AND added slots (c08+),
    the game crashes at the VS screen.

    Per ARCropolis discussion #451:
        "Issue resolves if mods are placed exclusively on added or
         non-added slots, but not both."

    We can't auto-fix this — the user has to choose which set to
    keep. Returns a dict
    ``{fighter: {"base": [(slot, mod_folder), ...],
                 "added": [(slot, mod_folder), ...]}}``
    for fighters with the conflict (both lists non-empty).
    """
    if not os.path.isdir(mods_root):
        return {}
    per_fighter = {}
    for mod_folder in os.listdir(mods_root):
        mod_dir = os.path.join(mods_root, mod_folder)
        fighter_root = os.path.join(mod_dir, "fighter")
        if not os.path.isdir(fighter_root):
            continue
        for fighter in os.listdir(fighter_root):
            f_dir = os.path.join(fighter_root, fighter)
            if not os.path.isdir(f_dir):
                continue
            slots_seen = set()
            for tree_root_name in ("model", "motion", "sound"):
                tree_root = os.path.join(f_dir, tree_root_name)
                if not os.path.isdir(tree_root):
                    continue
                for tree in os.listdir(tree_root):
                    tree_path = os.path.join(tree_root, tree)
                    if not os.path.isdir(tree_path):
                        continue
                    for slot in os.listdir(tree_path):
                        if (re.fullmatch(r"c\d{2}", slot, re.I)
                                and os.path.isdir(
                                    os.path.join(tree_path, slot))):
                            slots_seen.add(slot.lower())
            if not slots_seen:
                continue
            entry = per_fighter.setdefault(
                fighter.lower(), {"base": [], "added": []})
            for slot in slots_seen:
                if re.fullmatch(r"c0[0-7]", slot, re.I):
                    entry["base"].append((slot, mod_folder))
                else:
                    entry["added"].append((slot, mod_folder))

    return {f: data for f, data in per_fighter.items()
            if data["base"] and data["added"]}


def sanity_check_install(installed_dir):
    """Walk an installed mod folder and quarantine inconsistencies that
    are known to freeze SSBU on character-select / load.

    Concretely we:
      • Rename malformed multi-slot folders / files like ``c00, c03``
        or ``chara_0_packun_00, c03.bntx`` to their **first** slot
        only.  Mod authors sometimes ship these as a hint that "the
        content applies to both slots", but the game can't parse the
        comma so the slot never resolves.  We don't clone into every
        listed slot because that would override whatever the user
        actually assigned to those slots in their profile — let the
        slot picker decide where the mod ends up.
      • Drop cloned-slot duplicates: if a mod has two body-slot dirs
        with byte-identical signatures (same paths + sizes), keep the
        lowest slot and remove the rest.  Pairs with the rename: it
        cleans up SD cards that were already touched by the previous
        "split into both slots" release.
      • Delete a body-slot directory (``fighter/<x>/<...>/cXX``) whose
        model folder is empty.  Empty body slots crash the game when
        it tries to load the slot.
      • Delete an orphan UI bntx file in
        ``ui/replace[_patch]/chara/<chara_N>/chara_*_<x>_<YY>.bntx``
        when no matching body slot ``cYY`` exists for that fighter
        (game freezes when the portrait points at a missing body).
      • Warn on body slots that have no portrait in any chara_N dir
        (base portrait will be reused — not fatal but worth noting).

    Returns a dict ``{"deleted_files": [...], "deleted_dirs": [...],
                       "split_dirs": int, "split_files": int,
                       "deduped_dirs": int, "deduped_files": int,
                       "warnings": [...]}`` for logging.
    """
    report = {"deleted_files": [], "deleted_dirs": [],
              "split_dirs": 0, "split_files": 0,
              "deduped_dirs": 0, "deduped_files": 0,
              "warnings": []}
    if not os.path.isdir(installed_dir):
        return report

    # ── 0. Split malformed multi-slot artifacts FIRST ──
    # Has to run before everything else so the body / UI scans see the
    # repaired (single-slot) layout.
    sd, sf = _repair_multislot_artifacts(installed_dir)
    report["split_dirs"] = sd
    report["split_files"] = sf

    # ── 0b. Drop cloned-slot duplicates ──
    # Undoes the damage from an earlier release that copied a single
    # malformed ``c00, c03`` folder into BOTH ``c00/`` and ``c03/``.
    # Identical signatures → keep the lowest slot, drop the rest.
    dup_dirs = _dedupe_cloned_slots(installed_dir)
    for p in dup_dirs:
        report["deleted_dirs"].append(p)
    report["deduped_dirs"] = len(dup_dirs)
    dup_files = _dedupe_cloned_ui_bntx(installed_dir)
    for p in dup_files:
        report["deleted_files"].append(p)
    report["deduped_files"] = len(dup_files)

    # ── 1. Discover body slots per fighter ──
    body_slots = {}  # fighter -> set of slots that have a non-empty body dir
    fighter_root = os.path.join(installed_dir, "fighter")
    if os.path.isdir(fighter_root):
        for fname in os.listdir(fighter_root):
            body_dir = os.path.join(fighter_root, fname, "model", "body")
            if not os.path.isdir(body_dir):
                continue
            for sub in list(os.listdir(body_dir)):
                full = os.path.join(body_dir, sub)
                if not os.path.isdir(full):
                    continue
                if not re.match(r'^c\d{2}$', sub):
                    continue
                # Empty body folders (just the cXX dir, no contents) are
                # a known crash trigger; drop them outright.
                has_content = False
                for _r, _d, fs in os.walk(full):
                    if fs:
                        has_content = True
                        break
                if not has_content:
                    try:
                        shutil.rmtree(full, ignore_errors=True)
                        report["deleted_dirs"].append(
                            os.path.relpath(full, installed_dir))
                    except Exception:
                        pass
                    continue
                body_slots.setdefault(fname, set()).add(sub)

    # ── 2. Discover UI bntx slots per fighter ──
    # ARCropolis honors both ``ui/replace`` and ``ui/replace_patch`` —
    # check both roots so we don't miss patch-style mods.
    ui_slots = {}  # fighter -> {slot: [path, ...]}
    bntx_re = re.compile(r'^chara_\d+_([a-z][a-z0-9_]*?)_(\d{2})\.bntx$',
                         re.I)
    for ui_kind in ("replace", "replace_patch"):
        chara_root = os.path.join(installed_dir, "ui", ui_kind, "chara")
        if not os.path.isdir(chara_root):
            continue
        for sub in list(os.listdir(chara_root)):
            sub_dir = os.path.join(chara_root, sub)
            if not os.path.isdir(sub_dir):
                continue
            for fn in list(os.listdir(sub_dir)):
                m = bntx_re.match(fn)
                if not m:
                    continue
                fighter = m.group(1).lower()
                slot = f"c{m.group(2)}"
                ui_slots.setdefault(fighter, {}) \
                        .setdefault(slot, []) \
                        .append(os.path.join(sub_dir, fn))

    # ── 3. Drop orphan UI bntx (no body for that slot) ──
    for fighter, by_slot in ui_slots.items():
        body_for_fighter = body_slots.get(fighter, set())
        # If the mod has NO body directory for this fighter at all, the
        # UI files are just decorations — leave them alone.
        if not body_for_fighter:
            continue
        for slot, paths in by_slot.items():
            if slot in body_for_fighter:
                continue
            for p in paths:
                try:
                    os.remove(p)
                    report["deleted_files"].append(
                        os.path.relpath(p, installed_dir))
                except Exception:
                    pass

    # ── 4. Warn on orphan body slots (body but no UI portrait) ──
    # Don't auto-delete: many mods legitimately ship body-only edits
    # with the assumption that the base portrait is reused.  Just
    # surface a warning so the operator sees what got installed.
    for fighter, slots in body_slots.items():
        ui_for_fighter = set(ui_slots.get(fighter, {}).keys())
        for slot in slots:
            if slot not in ui_for_fighter:
                report["warnings"].append(
                    f"{fighter}/{slot}: body present without portrait "
                    f"(base portrait will be reused)")

    return report


def validate_profile_collisions(profile_name):
    """Detect slot conflicts inside a saved profile.

    For every skin in the profile we union two sources of truth:
      1. **Metadata** — ``character`` (mapped to its internal name) +
         every ``cXX`` token in ``slot``. Catches the "two mods both
         claim Yoshi c02" case at zero cost.
      2. **Cached touched slots** — ``get_cached_touched(mod_id)``,
         populated after every successful install. Catches the cross-
         character case (a mod tagged "Birdo" that secretly replaces
         ``fighter/yoshi/c02/...``) on the *second* and later
         provisions of any profile that contains it.

    Returns a list of dicts:
        ``[{"fighter": "yoshi", "slot": "c02",
            "mods": [{"name":..., "mod_id":...}, ...]}, ...]``
    Only groups with two or more *distinct* mods are returned.
    """
    profiles = load_profiles()
    profile = profiles.get(profile_name, {})
    mods = profile.get("mods", [])

    # (fighter_internal, slot) -> list of {name, mod_id}
    occupancy = {}

    def _key_pairs_for_mod(mod):
        seen = set()
        # Metadata-tagged. Only trust this if the character maps to a
        # known fighter — otherwise everything ends up in a fake "other"
        # bucket and produces spurious collisions across unrelated mods.
        char = mod.get("character") or ""
        f_internal = FIGHTER_INTERNAL.get(char)
        slot_value = str(mod.get("slot", ""))
        explicit_slots = set()
        if f_internal:
            for part in slot_value.replace(",", " ").split():
                s = part.strip().lower()
                if re.match(r"^c\d{2}$", s):
                    explicit_slots.add(s)
                    seen.add((f_internal, s))
        # Cached touches (gb_touched_cache.json) come from PRIOR
        # installs of this mod and are PRE-REMAP — i.e. the slots the
        # archive's files lived under at install time, not the slots
        # the entry will end up in after the loader applies its
        # slot_map. We use them ONLY for two purposes:
        #
        #   1. Cross-character detection — a mod tagged "Birdo" that
        #      secretly replaces fighter/yoshi/c02. We add cached
        #      touches whose fighter differs from the entry's metadata
        #      fighter so the collision check still catches that.
        #   2. Legacy entries with no explicit slot at all — fall back
        #      to whatever the cache last saw.
        #
        # Critically, we do NOT add same-fighter cached touches for
        # entries with an explicit slot, because the profile's slot is
        # the authoritative post-install destination. Including stale
        # pre-remap slots produced false-positive collisions whenever
        # the user re-slotted a mod via drag-drop.
        for f_real, s_real in get_cached_touched(mod.get("mod_id")):
            if not (isinstance(f_real, str) and isinstance(s_real, str)):
                continue
            f_real_l = f_real.lower()
            s_real_l = s_real.lower()
            if not explicit_slots:
                seen.add((f_real_l, s_real_l))
            elif f_internal and f_real_l != f_internal:
                seen.add((f_real_l, s_real_l))
        return seen

    for mod in mods:
        if mod.get("mod_type", "skin") != "skin":
            continue
        ident = {
            "name": mod.get("name") or mod.get("folder_name") or "?",
            "mod_id": mod.get("mod_id"),
        }
        for key in _key_pairs_for_mod(mod):
            occupancy.setdefault(key, []).append(ident)

    out = []
    for (fighter, slot), entries in occupancy.items():
        # Dedupe by mod_id|name so a mod that lists the same slot twice
        # (or appears via both metadata + touched) only counts once.
        seen = set()
        unique = []
        for e in entries:
            k = (e["mod_id"], e["name"])
            if k in seen:
                continue
            seen.add(k)
            unique.append(e)
        if len(unique) >= 2:
            out.append({
                "fighter": fighter,
                "slot": slot,
                "mods": unique,
            })

    out.sort(key=lambda c: (c["fighter"], c["slot"]))
    return out


def load_favorites():
    """Load favorites from JSON file. Returns dict keyed by mod_id (str)."""
    if os.path.exists(FAVORITES_FILE):
        try:
            with open(FAVORITES_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_favorites(favs):
    """Save favorites dict to JSON file."""
    with open(FAVORITES_FILE, "w", encoding="utf-8") as f:
        json.dump(favs, f, indent=2, ensure_ascii=False)


def add_favorite(mod_id, rec, mod_type="skin"):
    """Add a mod to favorites, storing essential metadata.

    *mod_type* should be "skin" or "stage" so the Favorites tab can
    separate them properly.
    """
    favs = load_favorites()
    cid, cname, rid = _extract_category_info(rec)
    # Re-derive mod_type from the GameBanana category if the caller passed
    # the default "skin" — this avoids storing music packs / modpacks etc.
    # as skins.
    if mod_type == "skin":
        mod_type = _classify_mod_type_from_meta(
            {"category_id": cid, "root_category_id": rid},
            default="skin")
    favs[str(mod_id)] = {
        "mod_id": mod_id,
        "name": rec.get("_sName", "Unknown"),
        "submitter": rec.get("_aSubmitter", {}).get("_sName", "?"),
        "likes": rec.get("_nLikeCount", 0),
        "views": rec.get("_nViewCount", 0),
        "has_files": rec.get("_bHasFiles", False),
        "url": rec.get("_sProfileUrl", ""),
        "tags": rec.get("_aTags", []),
        "thumb_url": _extract_thumb_url(rec),
        "image_urls": _extract_all_image_urls(rec),
        "initial_visibility": rec.get("_sInitialVisibility", "show"),
        "has_content_ratings": rec.get("_bHasContentRatings", False),
        "category_id": cid,
        "category_name": cname,
        "root_category_id": rid,
        "mod_type": mod_type,
    }
    save_favorites(favs)
    return True


def remove_favorite(mod_id):
    """Remove a mod from favorites."""
    favs = load_favorites()
    favs.pop(str(mod_id), None)
    save_favorites(favs)


def is_favorite(mod_id):
    return str(mod_id) in load_favorites()


# ─────────────────────────────────────────────────────────
#  MOD PROFILES (curated collections of mods)
# ─────────────────────────────────────────────────────────

def load_profiles():
    """Load all saved mod profiles. Returns dict: profile_name -> profile_data."""
    # Migrate legacy gb_sets.json if it exists and gb_profiles.json does not
    legacy = os.path.join(SCRIPT_DIR, "gb_sets.json")
    if not os.path.exists(PROFILES_FILE) and os.path.exists(legacy):
        try:
            shutil.copy2(legacy, PROFILES_FILE)
        except Exception:
            pass
    if os.path.exists(PROFILES_FILE):
        try:
            with open(PROFILES_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_profiles(profiles):
    """Persist all mod profiles to disk."""
    with open(PROFILES_FILE, "w", encoding="utf-8") as f:
        json.dump(profiles, f, indent=2, ensure_ascii=False)


# Mod types that *change gameplay* and therefore desync online play.
# Skins / stages / UI / music are visual only and are considered wifi-safe.
WIFI_UNSAFE_MOD_TYPES = frozenset((
    "moveset", "modpack", "mechanics", "balance", "ai",
    "parameters", "gameplay", "effect",
))


def profile_config(profile):
    """Return the settings dict for a mod profile, applying defaults.

    Stored on each gb_profiles.json entry as a flat set of keys so the
    file format stays backward compatible — older profiles without these
    keys just get the defaults.
    """
    template = profile.get("template", "Skins Only")
    # ``plugins`` is None when the profile has never been customized — we
    # interpret that as "use whatever the template ships with" so existing
    # profiles keep working.  An empty list means the user explicitly
    # turned every plugin off.
    raw_plugins = profile.get("plugins")
    if raw_plugins is None:
        effective_plugins = list(
            PROVISIONING_PROFILES.get(template, {}).get("plugins", []))
    else:
        effective_plugins = list(raw_plugins)
    return {
        "template": template,
        "wifi_safe": bool(profile.get("wifi_safe", False)),
        # Default new profiles to the unofficial Atmosphere branch — every
        # current FW needs it until the official build catches up.
        "unofficial_atmo": bool(profile.get("unofficial_atmo", True)),
        "plugins": effective_plugins,
    }


def create_profile_from_installed(profile_name):
    """Snapshot current installed mods into a named profile.
    Returns the number of mods captured."""
    skins = list_installed_skins()
    mods = []
    for skin in skins:
        meta = skin.get("meta") or {}
        entry = {
            "folder_name": skin["name"],
            "mod_id": meta.get("mod_id"),
            "name": meta.get("name", skin["name"]),
            "slot": meta.get("slot"),
            "character": skin.get("character", "Other"),
            "mod_type": skin.get("mod_type", "skin"),
            "thumb_url": meta.get("thumb_url"),
            "image_urls": meta.get("image_urls", []),
            "url": meta.get("url", ""),
            "submitter": meta.get("submitter", ""),
        }
        mods.append(entry)

    profiles = load_profiles()
    profiles[profile_name] = {
        "created": datetime.now().isoformat(),
        "mod_count": len(mods),
        "mods": mods,
    }
    save_profiles(profiles)
    return len(mods)


def _remove_matching_profile_entries(profile_name, mod_id, source_slot,
                                       exclude_slot=None):
    """Remove every entry from ``profile_name`` whose ``mod_id`` AND
    ``source_slot`` match the given pair, OPTIONALLY skipping the
    entry already pinned at ``exclude_slot`` (so the caller can
    pre-stage a "move" without nuking what they just installed).

    Used by drag-drop to enforce "one slot per (mod_id, source_slot)"
    and stop the user accumulating duplicate rows by re-dragging the
    same variant to different targets.
    """
    if mod_id is None:
        return 0
    profiles = load_profiles()
    profile = profiles.get(profile_name)
    if not profile:
        return 0
    src = (source_slot or "").strip().lower()
    excl = (exclude_slot or "").strip().lower()
    keep = []
    removed = 0
    for m in profile.get("mods", []):
        if (m.get("mod_id") == mod_id
                and str(m.get("source_slot", "")).strip().lower() == src):
            entry_slot = str(m.get("slot", "")).strip().lower()
            if excl and entry_slot == excl:
                keep.append(m)
                continue
            removed += 1
            continue
        keep.append(m)
    if removed:
        profile["mods"] = keep
        profile["mod_count"] = len(keep)
        profiles[profile_name] = profile
        save_profiles(profiles)
    return removed


def add_mod_to_profile(profile_name, mod_entry, merge=True):
    """Add a single mod entry to a profile.

    When ``merge`` is True (default — for legacy bulk-install flows),
    re-adding the same ``mod_id`` merges the new slot into the
    existing entry's ``slot`` string. That preserves the old behaviour
    where a multi-slot mod ends up as one entry with ``slot = "c00,
    c02"``.

    When ``merge`` is False (drag-and-drop installs), the new entry is
    appended **only if** there isn't already an entry with the same
    ``(mod_id, slot, source_slot)`` triple. Without this guard, a user
    who re-drops the same source variant onto the same target slot a
    few times accumulates identical duplicate rows in their profile.
    """
    profiles = load_profiles()
    profile = profiles.get(profile_name)
    if not profile:
        profile = {
            "created": datetime.now().isoformat(),
            "mod_count": 0,
            "mods": [],
        }
    mid = mod_entry.get("mod_id")
    new_slot = str(mod_entry.get("slot", "")).strip().lower()
    new_src = str(mod_entry.get("source_slot", "")).strip().lower()
    if merge and mid:
        existing = next((m for m in profile["mods"] if m.get("mod_id") == mid), None)
        if existing:
            if new_slot:
                cur_slots = set(
                    s.strip() for s in
                    str(existing.get("slot", "")).replace(",", " ").split()
                    if re.match(r"^c\d{2}$", s.strip())
                )
                cur_slots.add(new_slot)
                existing["slot"] = ", ".join(sorted(cur_slots))
            profile["mod_count"] = len(profile["mods"])
            profiles[profile_name] = profile
            save_profiles(profiles)
            return profile["mod_count"]
    # Distinct-entry path: skip if an identical entry already exists
    # (same mod_id, slot AND source_slot). Drag-drop users who re-drop
    # the same variant onto the same target would otherwise stack up
    # 4-5 copies of the same row in their profile.
    if not merge and mid is not None:
        for m in profile["mods"]:
            if m.get("mod_id") != mid:
                continue
            ex_slot = str(m.get("slot", "")).strip().lower()
            ex_src = str(m.get("source_slot", "")).strip().lower()
            if ex_slot == new_slot and ex_src == new_src:
                profile["mod_count"] = len(profile["mods"])
                profiles[profile_name] = profile
                save_profiles(profiles)
                return profile["mod_count"]
    profile["mods"].append(mod_entry)
    profile["mod_count"] = len(profile["mods"])
    profiles[profile_name] = profile
    save_profiles(profiles)
    return profile["mod_count"]


def remove_profile_slot(profile_name, fighter_int, slot):
    """Remove ``slot`` for ``fighter_int`` from a profile.

    If the matching entry covers ONLY that slot, the entire entry is
    dropped. If it's a multi-slot entry (``slot = "c00 c02"``), only
    the requested slot is stripped from the slot string and the entry
    is kept — otherwise removing one variant of a multi-slot mod
    would silently remove ALL of its variants.

    Returns a dict describing what changed:
        {"action": "removed"|"slot_stripped", "entry": ...}
    or ``None`` if nothing matched.
    """
    profiles = load_profiles()
    profile = profiles.get(profile_name)
    if not profile or not fighter_int or not slot:
        return None
    slot = slot.lower()
    display = INTERNAL_TO_DISPLAY.get(fighter_int)
    keep = []
    result = None
    for m in profile.get("mods", []):
        if m.get("mod_type", "skin") != "skin":
            keep.append(m)
            continue
        char = str(m.get("character", ""))
        char_int = FIGHTER_INTERNAL.get(char) or char
        if char != display and char_int != fighter_int:
            keep.append(m)
            continue
        slot_value = str(m.get("slot", "")).lower()
        slots_in_entry = [s.strip()
                          for s in slot_value.replace(",", " ").split()
                          if s.strip()]
        if slot not in slots_in_entry:
            keep.append(m)
            continue
        if len(slots_in_entry) <= 1:
            # Single-slot entry — drop it entirely.
            result = {"action": "removed", "entry": dict(m)}
            continue
        # Multi-slot entry — strip just this slot, keep the entry.
        remaining = [s for s in slots_in_entry if s != slot]
        new_m = dict(m)
        new_m["slot"] = " ".join(remaining)
        keep.append(new_m)
        result = {"action": "slot_stripped",
                  "entry": dict(m), "remaining": remaining}
    if result is None:
        return None
    profile["mods"] = keep
    profile["mod_count"] = len(keep)
    profiles[profile_name] = profile
    save_profiles(profiles)
    return result


def remove_mod_from_profile(profile_name, mod_id=None, folder_name=None):
    """Remove a mod from a profile by mod_id or folder_name.
    Returns remaining mod count or None."""
    profiles = load_profiles()
    profile = profiles.get(profile_name)
    if not profile:
        return None
    if mod_id:
        profile["mods"] = [m for m in profile["mods"] if m.get("mod_id") != mod_id]
    elif folder_name:
        profile["mods"] = [m for m in profile["mods"]
                           if m.get("folder_name") != folder_name]
    else:
        return profile.get("mod_count")
    profile["mod_count"] = len(profile["mods"])
    profiles[profile_name] = profile
    save_profiles(profiles)
    return profile["mod_count"]


def set_mod_enabled(profile_name, enabled, mod_id=None, folder_name=None):
    """Toggle a mod's ``enabled`` flag inside a profile.

    Disabled mods stay in the profile (so the user can re-enable them
    later without losing slot/character metadata) but are skipped by
    profile-load installs and treated as "should not be on the SD",
    so loading the profile will *uninstall* them if they're already
    present. Returns the new bool, or ``None`` if the mod wasn't found.
    """
    profiles = load_profiles()
    profile = profiles.get(profile_name)
    if not profile:
        return None
    target = None
    for m in profile.get("mods", []):
        if mod_id is not None and m.get("mod_id") == mod_id:
            target = m
            break
        if folder_name and m.get("folder_name") == folder_name:
            target = m
            break
    if target is None:
        return None
    target["enabled"] = bool(enabled)
    save_profiles(profiles)
    return target["enabled"]


def autoslot_missing_profile_entries(profile_name):
    """Fill missing slot values for skin entries in a saved profile.

    Uses the fighter's currently occupied SD slots plus already-assigned
    slots within this profile, then picks the first free cXX slot.

    Returns a summary dict:
      {'assigned': int, 'unslotted': int}
    """
    profiles = load_profiles()
    profile = profiles.get(profile_name)
    if not profile:
        return {"assigned": 0, "unslotted": 0}

    mods = profile.get("mods", [])
    if not mods:
        return {"assigned": 0, "unslotted": 0}

    used_by_fighter = {}
    occupied_cache = {}

    def _parse_slots(slot_value):
        if not slot_value:
            return []
        out = []
        for part in str(slot_value).replace(",", " ").split():
            s = part.strip().lower()
            if re.match(r"^c\d{2}$", s):
                out.append(s)
        return out

    # Reserve slots already explicitly set in the profile.
    for mod in mods:
        if mod.get("mod_type", "skin") != "skin":
            continue
        character = mod.get("character")
        fighter_internal = FIGHTER_INTERNAL.get(character)
        if not fighter_internal:
            continue
        used = used_by_fighter.setdefault(fighter_internal, set())
        used.update(_parse_slots(mod.get("slot")))

    assigned = 0
    unslotted = 0
    changed = False

    for mod in mods:
        if mod.get("mod_type", "skin") != "skin":
            continue
        if _parse_slots(mod.get("slot")):
            continue

        character = mod.get("character")
        fighter_internal = FIGHTER_INTERNAL.get(character)
        if not fighter_internal:
            unslotted += 1
            continue

        if fighter_internal not in occupied_cache:
            occupied_cache[fighter_internal] = set(
                get_occupied_slots(fighter_internal).keys()
            )

        used = used_by_fighter.setdefault(fighter_internal, set())
        taken = occupied_cache[fighter_internal] | used

        picked = None
        for i in range(MAX_SLOT):
            candidate = f"c{i:02d}"
            if candidate not in taken:
                picked = candidate
                break

        if not picked:
            unslotted += 1
            continue

        mod["slot"] = picked
        used.add(picked)
        assigned += 1
        changed = True

    if changed:
        profile["mods"] = mods
        profile["mod_count"] = len(mods)
        profiles[profile_name] = profile
        save_profiles(profiles)

    return {"assigned": assigned, "unslotted": unslotted}


def rename_profile(old_name, new_name):
    """Rename a profile. Returns True on success."""
    profiles = load_profiles()
    if old_name not in profiles or new_name in profiles:
        return False
    profiles[new_name] = profiles.pop(old_name)
    save_profiles(profiles)
    return True


def delete_profile(profile_name):
    """Delete a saved mod profile."""
    profiles = load_profiles()
    profiles.pop(profile_name, None)
    save_profiles(profiles)


def _unique_profile_name(profiles, base):
    """Return a profile name based on ``base`` that doesn't collide with
    any existing profile. Tries ``base`` first, then ``base 2``, ``base 3``…"""
    if base not in profiles:
        return base
    i = 2
    while f"{base} {i}" in profiles:
        i += 1
    return f"{base} {i}"


def duplicate_profile(old_name, new_name=None):
    """Deep-copy a saved profile under a new name. If ``new_name`` is
    None or already taken, a unique ``"<old> (Copy)"`` variant is
    generated. The duplicate keeps the full mod list and settings but
    gets a fresh ``created`` timestamp. Returns the new name on success
    or None if the source profile doesn't exist."""
    profiles = load_profiles()
    src = profiles.get(old_name)
    if src is None:
        return None
    if not new_name or new_name in profiles or new_name == old_name:
        new_name = _unique_profile_name(profiles, f"{old_name} (Copy)")
    dup = copy.deepcopy(src)
    dup["created"] = datetime.now().isoformat()
    profiles[new_name] = dup
    save_profiles(profiles)
    return new_name


def _extract_thumb_url(rec):
    """Extract the best thumbnail URL from an API record."""
    # Check for pre-resolved cached URL first (from audit cache)
    cached = rec.get("_cached_thumb_url")
    if cached:
        return cached
    media = rec.get("_aPreviewMedia", {})
    images = media.get("_aImages", [])
    if images:
        img0 = images[0]
        base = img0.get("_sBaseUrl", "")
        fname = (img0.get("_sFile220") or img0.get("_sFile530") or
                 img0.get("_sFile100") or img0.get("_sFile"))
        if base and fname:
            return f"{base}/{fname}"
    return None


def _extract_all_image_urls(rec):
    """Extract all image URLs from an API record.
    Returns list of dicts with 'large' (full-res) and 'thumb' (530px) keys."""
    # Check for pre-resolved cached URLs first (from installed .gb_meta.json)
    cached = rec.get("_cached_image_urls")
    if cached:
        return cached
    result = []
    media = rec.get("_aPreviewMedia", {})
    for img in media.get("_aImages", []):
        base = img.get("_sBaseUrl", "")
        large = img.get("_sFile") or img.get("_sFile530") or img.get("_sFile220")
        thumb = img.get("_sFile530") or img.get("_sFile220") or large
        if base and large:
            result.append({
                "large": f"{base}/{large}",
                "thumb": f"{base}/{thumb}" if thumb else f"{base}/{large}",
            })
    return result


# ─────────────────────────────────────────────────────────
#  CONTENT MATURITY CLASSIFICATION
# ─────────────────────────────────────────────────────────


def is_mod_mature(rec_or_meta):
    """Classify a mod as mature/suggestive.  Returns True if flagged."""
    return is_mod_mature_detailed(rec_or_meta)[0]


def is_mod_mature_detailed(rec_or_meta):
    """Classify a mod as mature based on GameBanana API fields or stored metadata.
    Works with both raw API records (_sInitialVisibility) and saved metadata
    (initial_visibility).

    Returns (is_mature: bool, reason: str).

    Detection layers:
      1. _bHasContentRatings / has_content_ratings — GB's own content flag
      2. _sInitialVisibility != "show" — "warn" and "hide" both indicate mature
    """
    # ── Layer 1: GameBanana content-rating flag (most reliable) ──
    if rec_or_meta.get("_bHasContentRatings"):
        return True, "API: hasContentRatings"
    if rec_or_meta.get("has_content_ratings"):      # stored metadata
        return True, "meta: hasContentRatings"

    # ── Layer 2: visibility flag — anything other than "show" is restricted ──
    api_vis = rec_or_meta.get("_sInitialVisibility")
    if api_vis and api_vis != "show":               # catches "warn" AND "hide"
        return True, f"API: visibility={api_vis}"
    stored_vis = rec_or_meta.get("initial_visibility")
    if stored_vis and stored_vis != "show":
        return True, f"meta: visibility={stored_vis}"

    return False, ""


# ─────────────────────────────────────────────────────────
#  INSTALLED SKINS HELPERS
# ─────────────────────────────────────────────────────────

def get_mod_slots(mod_path):
    """Return dict mapping fighter_internal -> sorted list of slot strings
    for a specific installed mod.  e.g. {'koopa': ['c00','c01',...]})"""
    result = {}
    fighter_dir = os.path.join(mod_path, "fighter")
    if not os.path.isdir(fighter_dir):
        return result
    for fighter_name in os.listdir(fighter_dir):
        body_dir = os.path.join(fighter_dir, fighter_name, "model", "body")
        if not os.path.isdir(body_dir):
            continue
        slots = sorted(d for d in os.listdir(body_dir)
                       if os.path.isdir(os.path.join(body_dir, d))
                       and re.match(r'^c\d{2}$', d))
        if slots:
            result[fighter_name] = slots
    return result


def remove_single_slot(mod_path, fighter_internal, slot):
    """Remove a single slot directory from a mod.
    Cleans up fighter body dir and ui chara dir for that slot.
    Returns True if removed, False if not found."""
    removed_something = False

    # Remove body slot
    body_dir = os.path.join(mod_path, "fighter", fighter_internal,
                            "model", "body", slot)
    if os.path.isdir(body_dir):
        shutil.rmtree(body_dir)
        removed_something = True

    # Remove matching UI chara slot if present
    slot_num = int(slot[1:])
    chara_target = f"chara_{slot_num}"
    chara_dir = os.path.join(mod_path, "ui", "replace", "chara", chara_target)
    if os.path.isdir(chara_dir):
        shutil.rmtree(chara_dir)

    # Check if the mod is now completely empty of fighter content
    remaining = get_mod_slots(mod_path)
    if not remaining:
        # No slots left — remove the entire mod folder
        shutil.rmtree(mod_path, ignore_errors=True)
        return True  # signal full removal

    return removed_something


def swap_mod_slot(mod_path, fighter_internal, from_slot, to_slot):
    """Swap a mod's slot from one position to another (e.g. c02 → c05).
    Renames body and ui chara directories.  Returns True on success."""
    body_base = os.path.join(mod_path, "fighter", fighter_internal,
                             "model", "body")
    from_body = os.path.join(body_base, from_slot)
    to_body = os.path.join(body_base, to_slot)

    if not os.path.isdir(from_body):
        return False
    if os.path.exists(to_body):
        # Target slot already exists in this mod — use a temp to swap
        tmp = to_body + "_tmp_swap"
        os.rename(to_body, tmp)
        os.rename(from_body, to_body)
        os.rename(tmp, from_body)
    else:
        os.rename(from_body, to_body)

    # Rename matching UI chara dirs if present
    from_num = int(from_slot[1:])
    to_num = int(to_slot[1:])
    chara_base = os.path.join(mod_path, "ui", "replace", "chara")
    from_chara = os.path.join(chara_base, f"chara_{from_num}")
    to_chara = os.path.join(chara_base, f"chara_{to_num}")
    if os.path.isdir(from_chara):
        if os.path.exists(to_chara):
            tmp = to_chara + "_tmp_swap"
            os.rename(to_chara, tmp)
            os.rename(from_chara, to_chara)
            os.rename(tmp, from_chara)
        else:
            os.rename(from_chara, to_chara)

    return True


def _detect_fighter_from_mod(mod_path):
    """Detect fighter name(s) from the fighter/ subfolder inside a mod."""
    fighter_dir = os.path.join(mod_path, "fighter")
    if os.path.isdir(fighter_dir):
        fighters = [f for f in os.listdir(fighter_dir)
                     if os.path.isdir(os.path.join(fighter_dir, f))]
        if fighters:
            return sorted(fighters)
    return []


def _is_stage_mod(mod_path, meta=None):
    """Detect whether an installed mod is a stage mod.
    Checks metadata tag, or falls back to directory structure heuristics:
    stage mods typically have stage/ or ui/replace/stage/ directories
    and NO fighter/ directory."""
    if meta and meta.get("mod_type") == "stage":
        return True
    # Heuristic: has stage/ dir but no fighter/ dir
    has_stage = os.path.isdir(os.path.join(mod_path, "stage"))
    has_fighter = os.path.isdir(os.path.join(mod_path, "fighter"))
    if has_stage and not has_fighter:
        return True
    # Heuristic: has ui/replace/stage/ but no fighter/
    ui_stage = os.path.isdir(os.path.join(mod_path, "ui", "replace", "stage"))
    if ui_stage and not has_fighter:
        return True
    return False


# Reverse: GameBanana subcategory id → display fighter name.
# Skips "All Skins" so the root category isn't treated as a specific fighter.
FIGHTER_CATEGORY_TO_DISPLAY = {
    cid: name for name, cid in FIGHTER_CATEGORIES.items()
    if name != "All Skins"
}

# Non-fighter buckets that should never count as a real fighter match.
_NON_FIGHTER_NAMES = frozenset((
    "All Skins", "Assist Trophies/Pokemon", "Bosses",
    "Items", "Other/Misc", "Packs", "Mii Hats",
))


def _extract_category_info(rec):
    """Extract ``(category_id, category_name, root_category_id)`` from a
    GameBanana mod record.

    GameBanana ``/Mod/Index`` returns ``_aCategory`` with ``_idRow`` and
    ``_sName``, plus an ``_aRootCategory`` (or ``_aSuperCategory``) with
    the parent / root section id.  Older or thinned responses may put
    the id under ``_aSuperCategory`` or in the ``_sProfileUrl`` of the
    category. Returns ``(None, None, None)`` if no info is present.
    """
    if not rec:
        return None, None, None
    cat = rec.get("_aCategory") or {}
    cid = cat.get("_idRow")
    cname = cat.get("_sName")
    if cid is None:
        url = cat.get("_sProfileUrl", "") or ""
        try:
            cid = int(url.rstrip("/").rsplit("/", 1)[-1])
        except (ValueError, IndexError):
            cid = None
    try:
        cid = int(cid) if cid is not None else None
    except (TypeError, ValueError):
        cid = None

    root = rec.get("_aRootCategory") or rec.get("_aSuperCategory") or {}
    rid = root.get("_idRow")
    if rid is None:
        url = root.get("_sProfileUrl", "") or ""
        try:
            rid = int(url.rstrip("/").rsplit("/", 1)[-1])
        except (ValueError, IndexError):
            rid = None
    try:
        rid = int(rid) if rid is not None else None
    except (TypeError, ValueError):
        rid = None
    return cid, cname, rid


def _guess_character_from_meta(meta):
    """Guess the *target* fighter for a mod from metadata.

    Priority (most reliable first):
        1. ``meta["category_id"]`` — the GameBanana subcategory the mod was
           filed under.  For SSBU skins, that subcategory IS the fighter, so
           this is by far the most reliable signal and handles cases like
           "Kirby skin OVER Donkey Kong" correctly (the mod is under the
           Donkey Kong category).
        2. "OVER X" / "FOR X" / "ON X" / "(X)" patterns in the mod name —
           common modder convention naming the *target* fighter last.
        3. Word-boundary tag match — prefers the *longest* (most specific)
           fighter name (so "Donkey Kong" beats "Kong" or "Donkey").
        4. Word-boundary name match — same longest-wins rule, then prefer
           the LAST occurrence (modders write theme-then-target).

    Returns the display fighter name, or ``"Other"`` if nothing matched.
    """
    if not meta:
        return "Unknown"

    # 1) GameBanana category — authoritative.
    cid = meta.get("category_id")
    try:
        cid = int(cid) if cid is not None else None
    except (TypeError, ValueError):
        cid = None
    if cid is not None:
        disp = FIGHTER_CATEGORY_TO_DISPLAY.get(cid)
        if disp and disp not in _NON_FIGHTER_NAMES:
            return disp

    # Lookup table of (lowercase fighter name → display name), longest
    # names first so "Donkey Kong" wins over a stray "Kong".
    known_pairs = sorted(
        ((k.lower(), k) for k in FIGHTER_CATEGORIES
         if k not in _NON_FIGHTER_NAMES),
        key=lambda p: -len(p[0]),
    )

    mod_name = (meta.get("name") or "").lower()

    # 2) Explicit "over X" / "for X" / "on X" / "(X)" markers in name.
    if mod_name:
        for marker in (" over ", " for ", " on ", "(", "["):
            idx = mod_name.rfind(marker)
            if idx == -1:
                continue
            tail = mod_name[idx + len(marker):]
            for lk, display in known_pairs:
                # word-boundary match at start of tail
                if re.match(rf'\s*{re.escape(lk)}\b', tail):
                    return display

    # 3) Tag match — pick the longest (most specific) fighter named in tags.
    tags = [t.lower().strip() for t in (meta.get("tags") or [])
            if isinstance(t, str)]
    for lk, display in known_pairs:
        if lk in tags:
            return display

    # 4) Word-boundary name match, longest first; if equal length, last
    #    occurrence wins (theme-then-target convention).
    if mod_name:
        best = None
        for lk, display in known_pairs:
            for m in re.finditer(rf'\b{re.escape(lk)}\b', mod_name):
                if best is None or len(lk) > best[0]:
                    best = (len(lk), m.start(), display)
                elif len(lk) == best[0] and m.start() > best[1]:
                    best = (len(lk), m.start(), display)
        if best:
            return best[2]

    return "Other"


def list_installed_skins():
    """Return list of dicts for each installed skin on SD card."""
    if not os.path.exists(ARCROPOLIS_MODS):
        return []
    result = []
    for name in sorted(os.listdir(ARCROPOLIS_MODS)):
        p = os.path.join(ARCROPOLIS_MODS, name)
        if os.path.isdir(p):
            fc = sum(len(f) for _, _, f in os.walk(p))
            size = sum(os.path.getsize(os.path.join(r, f))
                       for r, _, files in os.walk(p) for f in files)
            # Read metadata if available
            meta = None
            meta_path = os.path.join(p, ".gb_meta.json")
            if os.path.exists(meta_path):
                try:
                    with open(meta_path, "r", encoding="utf-8") as f:
                        meta = json.load(f)
                except Exception:
                    pass
            # Detect fighter(s) from directory structure
            fighters = _detect_fighter_from_mod(p)
            is_stage = _is_stage_mod(p, meta)
            # Also try metadata tags
            if not fighters and meta and not is_stage:
                char = _guess_character_from_meta(meta)
                if char and char != "Other":
                    fighters = [char.lower()]

            # Resolve mod_type: prefer the type stored in metadata at install
            # time (which knows the GameBanana category, so e.g. music packs
            # and modpacks aren't mis-labelled as skins).  Fall back to
            # heuristics on disk.
            if is_stage:
                mod_type = "stage"
            else:
                mod_type = _classify_mod_type_from_meta(
                    meta, default="skin" if fighters else "other")

            if mod_type == "stage":
                character = "Stages"
            elif mod_type == "skin":
                character = (fighters[0].replace("_", " ").title()
                             if fighters else "Other")
            else:
                # Non-character bucket — group by mod_type label.
                character = mod_type.title()

            result.append({"name": name, "path": p,
                           "file_count": fc, "size": size,
                           "meta": meta, "character": character,
                           "mod_type": mod_type})
    return result


def uninstall_skin(name):
    """Remove an installed skin from SD card."""
    p = os.path.join(ARCROPOLIS_MODS, name)
    if os.path.exists(p):
        shutil.rmtree(p)
        return True
    return False


# ─────────────────────────────────────────────────────────
#  TEXT REDIRECTOR
# ─────────────────────────────────────────────────────────

class TextRedirector:
    def __init__(self, widget, tag=""):
        self.widget = widget
        self.tag = tag
    def write(self, text):
        self.widget.configure(state="normal")
        self.widget.insert(tk.END, text, (self.tag,))
        self.widget.see(tk.END)
        self.widget.configure(state="disabled")
    def flush(self):
        pass

# ═══════════════════════════════════════════════════════════
#  MAIN GUI
# ═══════════════════════════════════════════════════════════

class GameBananaBrowser:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Smash Night — Smash Ultimate Mod Manager")
        self.root.configure(bg=T.BG)
        self.root.geometry("1280x900")
        self.root.minsize(1000, 720)

        self._busy = False
        self._cancel_download = False
        self._current_page = 1
        self._total_results = 0
        self._thumb_cache = {}  # keep references to PhotoImages
        self._active_view = "browse"  # "browse", "stages", "favorites", "installed", "setup"
        self._open_profile_name = None  # name of profile detail page if open
        self._sd_poll_id = None        # after() id for SD card polling
        self._sd_present = os.path.exists(SD_CARD)  # current SD state
        self._rcm_poll_id = None       # after() id for RCM USB polling
        self._rcm_detected = False     # True when Switch is in RCM mode
        self._rcm_inject_btn = None    # reference to the inject button widget
        self._rcm_device_label = None  # reference to the RCM device status label
        self._active_profile = "Competitive"  # provisioning profile
        self._active_user_profile = None  # user profile (gb_profiles.json key)
        self._use_unofficial_atmo = True  # prefer unofficial/pre-release Atmosphere
        self._gallery_win = None       # reusable image gallery Toplevel
        self._fav_filter = "All"       # "All", "Skins Only", "Stages Only"
        self._profile_mode = False     # True to add mods to profile instead of SD
        self._profile_mode_target = None  # which profile to add to in profile mode
        self._slot_picker_registry = []   # [(btn, fighter_int, slot, card_mod_id), ...]
        self._slot_counter_registry = []  # [(label_widget, card_mod_id), ...]
        self._install_btn_registry = []   # [(btn, card_mod_id), ...] — non-slot Install buttons

        self._build_ui()
        self._redirect_output()

        # Start SD card polling globally (keeps top-right label current)
        self._start_sd_poll()

        # Initial load
        self.root.after(300, lambda: self._run_async(self._do_search))

    # ── Build UI ─────────────────────────────────────────

    def _build_ui(self):
        # ── Banner image (cycles in order each launch) ──
        banner_dir = os.path.join(SCRIPT_DIR, "assets", "banners")
        self._banner_photo = None  # keep reference to prevent GC
        if os.path.isdir(banner_dir):
            imgs = sorted(f for f in os.listdir(banner_dir)
                          if f.lower().endswith((".png", ".jpg", ".jpeg", ".gif")))
            if imgs:
                idx_file = os.path.join(banner_dir, ".banner_index")
                try:
                    last_idx = int(open(idx_file).read().strip())
                except Exception:
                    last_idx = -1
                cur_idx = (last_idx + 1) % len(imgs)
                try:
                    with open(idx_file, "w") as fh:
                        fh.write(str(cur_idx))
                except Exception:
                    pass
                chosen = os.path.join(banner_dir, imgs[cur_idx])
                try:
                    from PIL import Image, ImageTk
                    img = Image.open(chosen)
                    # Scale to fit width ~1260px, keep aspect ratio
                    target_w = 1260
                    ratio = target_w / img.width
                    target_h = int(img.height * ratio)
                    if target_h > 140:
                        target_h = 140
                        ratio = target_h / img.height
                        target_w = int(img.width * ratio)
                    img = img.resize((target_w, target_h), Image.LANCZOS)
                    self._banner_photo = ImageTk.PhotoImage(img)
                except Exception as e:
                    print(f"  [Banner] Could not load {chosen}: {e}")

        # ── Top bar ──
        top = tk.Frame(self.root, bg="#000000")
        top.pack(fill="x", padx=12, pady=(10, 0))

        if self._banner_photo:
            banner_label = tk.Label(top, image=self._banner_photo, bg="#000000")
            banner_label.pack(side="left")
        else:
            tk.Label(top, text="SMASH NIGHT",
                     font=(T.FONT, T.SZ_H1, "bold"), bg="#000000", fg=T.ACCENT).pack(side="left")

        # SD status block (right side): label on top, Eject button
        # underneath. Using a sub-frame keeps both pieces aligned to
        # the right edge of the banner and stacked vertically.
        sd_block = tk.Frame(top, bg="#000000")
        sd_block.pack(side="right")
        self.sd_label = tk.Label(sd_block, text="",
                                  font=(T.FONT, T.SZ_MD), bg="#000000")
        self.sd_label.pack(anchor="e")
        # Action row under the SD label: Diagnose + Eject.
        sd_btn_row = tk.Frame(sd_block, bg="#000000")
        sd_btn_row.pack(anchor="e", pady=(2, 0))
        # Diagnose: parse every cXX slot folder on the SD with
        # ssbh_data_py and report missing textures, broken meshes,
        # skeleton issues — the kinds of failures that cause "no
        # texture" / "weird geometry" / "freeze" symptoms.
        self.diagnose_btn = tk.Button(
            sd_btn_row, text="🩺 Diagnose", width=10,
            bg=T.SURFACE1, fg=T.FG,
            font=(T.FONT, T.SZ_SM, "bold"),
            relief="flat", cursor="hand2",
            command=self._run_deep_diagnose_sd)
        self.diagnose_btn.pack(side="left", padx=(0, 4))
        # Eject button — safely dismounts the SD volume so the user
        # can unplug the Switch without "Drive in use" errors. Hidden
        # while no SD is detected; ``_check_sd`` toggles its state.
        self.eject_btn = tk.Button(
            sd_btn_row, text="⏏ Eject", width=10,
            bg=T.SURFACE1, fg=T.FG,
            font=(T.FONT, T.SZ_SM, "bold"),
            relief="flat", cursor="hand2",
            command=self._eject_sd)
        self.eject_btn.pack(side="left")
        self._check_sd()

        # ── Search bar ──
        search_frame = tk.Frame(self.root, bg=T.SURFACE, pady=8, padx=10)
        search_frame.pack(fill="x", padx=12, pady=(8, 0))

        # Fighter / Stage dropdown (label + combo swap when switching tabs)
        self._category_label = tk.Label(search_frame, text="Fighter:",
                                        bg=T.SURFACE, fg=T.FG,
                                        font=(T.FONT, T.SZ_MD))
        self._category_label.pack(side="left", padx=(0, 4))

        self.fighter_var = tk.StringVar(value="All Skins")
        fighter_names = sorted(FIGHTER_CATEGORIES.keys())
        # Put "All Skins" first
        fighter_names.remove("All Skins")
        fighter_names.insert(0, "All Skins")
        self._fighter_names = fighter_names

        # Pre-build stage names list too
        stage_names = sorted(STAGE_CATEGORIES.keys())
        stage_names.remove("All Stages")
        stage_names.insert(0, "All Stages")
        self._stage_names = stage_names

        # Pre-build other category names list
        other_names = sorted(OTHER_CATEGORIES.keys())
        other_names.remove("All Other")
        other_names.insert(0, "All Other")
        self._other_names = other_names

        # Pre-build pack (gameplay overhaul) category names list
        pack_names = sorted(PACK_CATEGORIES.keys())
        pack_names.remove("All Packs")
        pack_names.insert(0, "All Packs")
        self._pack_names = pack_names

        self.fighter_combo = ttk.Combobox(
            search_frame, textvariable=self.fighter_var,
            values=fighter_names, state="normal", width=22,
            font=(T.FONT, T.SZ_MD))
        self.fighter_combo.pack(side="left", padx=(0, 12))
        # Auto-search when the category/fighter/stage/pack dropdown changes.
        self.fighter_combo.bind("<<ComboboxSelected>>",
                                lambda e: self._on_search())

        # Type-ahead: accumulate keystrokes and jump to best match
        self._typeahead_buf = ""
        self._typeahead_timer = None
        self._setup_combo_typeahead(self.fighter_combo)

        # Search text
        tk.Label(search_frame, text="Search:", bg=T.SURFACE, fg=T.FG,
                 font=(T.FONT, T.SZ_MD)).pack(side="left", padx=(0, 4))

        self.search_var = tk.StringVar()
        self.search_entry = tk.Entry(
            search_frame, textvariable=self.search_var, width=30,
            bg=T.CRUST, fg=T.FG, insertbackground=T.FG,
            font=(T.FONT, T.SZ_LG), relief="flat")
        self.search_entry.pack(side="left", padx=(0, 12))
        self.search_entry.bind("<Return>", lambda e: self._on_search())

        # Sort lives in the Profile/Sort bar below; create the StringVar
        # here so all the existing readers find ``self.sort_var``.
        self.sort_var = tk.StringVar(value="Most Liked")

        tk.Button(search_frame, text="Search", width=10,
                  bg=T.ACCENT, fg=T.BG, font=(T.FONT, T.SZ_MD, "bold"),
                  relief="flat", cursor="hand2",
                  command=self._on_search).pack(side="left", padx=(0, 12))

        # Content filter (global — affects Browse, Installed, Favorites)
        _CONTENT_FILTER_PW = "espresso"
        CONTENT_FILTERS = {"All Content": "all",
                           "Kid Friendly": "kid",
                           "Adult Only": "adult"}
        tk.Label(search_frame, text="🔒", bg=T.SURFACE, fg=T.FG,
                 font=(T.FONT, T.SZ_MD)).pack(side="left", padx=(0, 3))
        self._content_filter = tk.StringVar(value="Kid Friendly")
        cf_combo = ttk.Combobox(
            search_frame, textvariable=self._content_filter,
            values=list(CONTENT_FILTERS.keys()), state="readonly", width=14,
            font=(T.FONT, T.SZ_MD))
        cf_combo.pack(side="left", padx=(0, 4))

        def _on_content_filter_change(event=None):
            chosen = self._content_filter.get()
            if chosen != "Kid Friendly":
                # Require password to unlock
                pw = simpledialog.askstring(
                    "Password Required",
                    f"Enter password to switch to '{chosen}':",
                    show="*", parent=self.root)
                if pw != _CONTENT_FILTER_PW:
                    self._content_filter.set("Kid Friendly")
                    if pw is not None:  # None = cancelled
                        messagebox.showwarning("Wrong Password",
                                               "Incorrect password. "
                                               "Staying on Kid Friendly.")
                    return
            self._refresh_current_view()

        cf_combo.bind("<<ComboboxSelected>>", _on_content_filter_change)

        # ── View tab bar ──
        tab_bar = tk.Frame(self.root, bg=T.SURFACE, pady=4, padx=10)
        tab_bar.pack(fill="x", padx=12, pady=(6, 0))

        self._tab_buttons = {}
        for tab_name, tab_label in [("browse", "Browse Skins"),
                                     ("stages", "Browse Stages"),
                                     ("packs", "Browse Packs"),
                                     ("other", "Browse Other"),
                                     ("favorites", "Favorites"),
                                     ("installed", "Installed"),
                                     ("profiles", "Profiles"),
                                     ("setup", "Setup")]:
            btn = tk.Button(tab_bar, text=tab_label, width=14,
                            font=(T.FONT, T.SZ_MD, "bold"), relief="flat",
                            cursor="hand2",
                            command=lambda t=tab_name: self._switch_view(t))
            btn.pack(side="left", padx=(0, 4))
            self._tab_buttons[tab_name] = btn

        self._highlight_active_tab()

        # ── Profile + Sort control bar ──
        # Profile mode is ALWAYS on now: every install routes through a
        # profile.  Direct-to-SD installs from Browse are no longer allowed.
        # The space that used to hold the "Add to Profile" checkbox now
        # holds the GameBanana sort selector for quick switching.
        self._profile_mode = True
        profile_mode_bar = tk.Frame(self.root, bg=T.SURFACE, pady=4, padx=10)
        profile_mode_bar.pack(fill="x", padx=12, pady=(0, 0))

        tk.Label(profile_mode_bar, text="Active Profile:",
                 bg=T.SURFACE, fg=T.FG,
                 font=(T.FONT, T.SZ_MD, "bold")).pack(side="left", padx=(0, 6))

        self._profile_mode_var = tk.BooleanVar(value=True)  # legacy compat
        self._profile_list_var = tk.StringVar()
        self._profile_combo = ttk.Combobox(
            profile_mode_bar, textvariable=self._profile_list_var,
            values=[], state="readonly", width=22,
            font=(T.FONT, T.SZ_MD))
        self._profile_combo.pack(side="left", padx=(0, 4))
        self._profile_combo.bind("<<ComboboxSelected>>",
                                 self._on_profile_mode_profile_change)

        # Refresh profile list button
        tk.Button(profile_mode_bar, text="Refresh", width=8,
                  bg=T.SURFACE1, fg=T.FG, font=(T.FONT, T.SZ_SM),
                  relief="flat", cursor="hand2",
                  command=self._refresh_profile_list).pack(side="left",
                                                            padx=(0, 16))

        # GameBanana sort selector — moved here from the search bar so it's
        # easier to spot and doesn't crowd the search input.
        tk.Label(profile_mode_bar, text="Sort:", bg=T.SURFACE, fg=T.FG,
                 font=(T.FONT, T.SZ_MD)).pack(side="left", padx=(0, 4))
        sort_combo2 = ttk.Combobox(
            profile_mode_bar, textvariable=self.sort_var,
            values=list(SORT_OPTIONS.keys()), state="readonly", width=18,
            font=(T.FONT, T.SZ_MD))
        sort_combo2.pack(side="left", padx=(0, 12))
        sort_combo2.bind("<<ComboboxSelected>>", lambda e: self._on_search())

        # Auto-populate the profile list at startup so the user doesn't have
        # to click Refresh first.
        self.root.after(50, self._refresh_profile_list_silent)

        # ── Main body: results (left) + log (right) ──
        body = tk.PanedWindow(self.root, orient="horizontal", bg=T.BG,
                              sashwidth=6, sashrelief="flat")
        body.pack(fill="both", expand=True, padx=12, pady=(6, 10))

        # Left: scrollable results list
        left = tk.Frame(body, bg=T.SURFACE)
        body.add(left, width=800, minsize=520)

        # Results info + pagination (inside left panel)
        nav_frame = tk.Frame(left, bg=T.BG)
        nav_frame.pack(fill="x", padx=4, pady=(4, 2))

        self.results_label = tk.Label(nav_frame, text="",
                                      font=(T.FONT, T.SZ_MD), bg=T.BG, fg=T.OVERLAY)
        self.results_label.pack(side="left")

        self.next_btn = tk.Button(nav_frame, text="Next >>", width=8,
                                  bg=T.SURFACE1, fg=T.FG, font=(T.FONT, T.SZ_SM),
                                  relief="flat", cursor="hand2",
                                  command=self._next_page)
        self.next_btn.pack(side="right", padx=2)

        self.page_label = tk.Label(nav_frame, text="Page 1",
                                   font=(T.FONT, T.SZ_MD), bg=T.BG, fg=T.FG)
        self.page_label.pack(side="right", padx=6)

        self.prev_btn = tk.Button(nav_frame, text="<< Prev", width=8,
                                  bg=T.SURFACE1, fg=T.FG, font=(T.FONT, T.SZ_SM),
                                  relief="flat", cursor="hand2",
                                  command=self._prev_page)
        self.prev_btn.pack(side="right", padx=2)

        # Fixed bottom pane for Setup Quick Actions (hidden by default)
        self._setup_actions_frame = tk.Frame(left, bg=T.BG)
        # (will be shown/hidden via pack/pack_forget in _build_setup_ui)

        # Scrollable results area (canvas + scrollbar in a container)
        self._canvas_wrap = tk.Frame(left, bg=T.SURFACE)
        self._canvas_wrap.pack(fill="both", expand=True)

        self.results_canvas = tk.Canvas(self._canvas_wrap, bg=T.SURFACE, highlightthickness=0)
        sb = tk.Scrollbar(self._canvas_wrap, orient="vertical", command=self.results_canvas.yview)
        self.results_inner = tk.Frame(self.results_canvas, bg=T.SURFACE)
        self.results_inner.bind("<Configure>",
            lambda e: self._update_scroll_region())
        self._canvas_window = self.results_canvas.create_window(
            (0, 0), window=self.results_inner, anchor="nw")
        self.results_canvas.configure(yscrollcommand=sb.set)
        # Keep inner frame width matched to canvas width
        self.results_canvas.bind("<Configure>", self._on_canvas_configure)
        sb.pack(side="right", fill="y")
        self.results_canvas.pack(side="left", fill="both", expand=True)

        def _mwheel(event):
            # Only scroll the browser canvas if the event originates from the
            # main window (not a gallery Toplevel) and content overflows.
            try:
                w = event.widget
                # Walk up to check we're in the root window, not a Toplevel
                top = w.winfo_toplevel()
                if top is not self.root:
                    return  # let gallery handle its own scroll
            except Exception:
                return
            if self.results_inner.winfo_reqheight() > self.results_canvas.winfo_height():
                self.results_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        self.results_canvas.bind_all("<MouseWheel>", _mwheel)

        # Right: log panel
        right = tk.Frame(body, bg=T.SURFACE)
        body.add(right, minsize=250)

        hdr = tk.Frame(right, bg=T.SURFACE)
        hdr.pack(fill="x")
        tk.Label(hdr, text="Log", font=(T.FONT, T.SZ_LG, "bold"),
                 bg=T.SURFACE, fg=T.ACCENT).pack(side="left", padx=10, pady=(4, 2))
        tk.Button(hdr, text="Clear", width=6,
                  bg=T.SURFACE1, fg=T.FG, font=(T.FONT, T.SZ_SM),
                  relief="flat", cursor="hand2",
                  command=self._clear_log).pack(side="right", padx=10, pady=(4, 2))

        # Download progress bar (hidden by default)
        self.progress_frame = tk.Frame(right, bg=T.SURFACE)

        prog_top = tk.Frame(self.progress_frame, bg=T.SURFACE)
        prog_top.pack(fill="x", padx=10, pady=(4, 0))

        self.progress_label = tk.Label(prog_top, text="Downloading...",
                                       bg=T.SURFACE, fg=T.FG,
                                       font=(T.FONT, T.SZ_SM), anchor="w")
        self.progress_label.pack(side="left", fill="x", expand=True)

        self.stop_btn = tk.Button(prog_top, text="Stop", width=6,
                                  bg=T.RED, fg=T.BG, font=(T.FONT, T.SZ_SM, "bold"),
                                  relief="flat", cursor="hand2",
                                  command=self._cancel_current_download)
        self.stop_btn.pack(side="right")

        style = ttk.Style()
        style.theme_use("default")
        style.configure("GB.Horizontal.TProgressbar",
                         troughcolor=T.CRUST, background=T.GREEN,
                         thickness=14)
        self.progress_bar = ttk.Progressbar(
            self.progress_frame, orient="horizontal", length=200,
            mode="determinate", style="GB.Horizontal.TProgressbar")
        self.progress_bar.pack(fill="x", padx=10, pady=(2, 6))
        # Don't pack progress_frame yet — shown only during downloads

        self.log = scrolledtext.ScrolledText(
            right, wrap="word", state="disabled", height=20,
            bg=T.CRUST, fg=T.FG, insertbackground=T.FG,
            font=(T.MONO, T.SZ_MD), relief="flat")
        self.log.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.log.tag_configure("stderr", foreground=T.RED)

    def _clear_log(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", tk.END)
        self.log.configure(state="disabled")

    def _on_canvas_configure(self, event):
        """Keep inner frame width matched to canvas, and update scroll region."""
        self.results_canvas.itemconfigure(self._canvas_window, width=event.width)
        self._update_scroll_region()

    def _update_scroll_region(self):
        """Update scroll region; reset to top if content fits in view."""
        self.results_canvas.configure(scrollregion=self.results_canvas.bbox("all"))
        # If content fits, snap to top so it can't float around
        if self.results_inner.winfo_reqheight() <= self.results_canvas.winfo_height():
            self.results_canvas.yview_moveto(0)

    def _redirect_output(self):
        sys.stdout = TextRedirector(self.log, "stdout")
        sys.stderr = TextRedirector(self.log, "stderr")

    def _check_sd(self):
        connected = os.path.exists(SD_CARD) and os.path.isdir(SD_CARD)
        if connected:
            self.sd_label.configure(text=f"SD ({SD_CARD}) Connected", fg=T.GREEN)
        else:
            self.sd_label.configure(text=f"SD ({SD_CARD}) Not found", fg=T.RED)
        # Toggle the Eject + Diagnose buttons so they only fire when
        # there's actually a connected volume to inspect / dismount.
        try:
            self.eject_btn.configure(
                state=("normal" if connected else "disabled"))
        except (AttributeError, tk.TclError):
            pass
        try:
            self.diagnose_btn.configure(
                state=("normal" if connected else "disabled"))
        except (AttributeError, tk.TclError):
            pass

    def _run_deep_diagnose_sd(self):
        """Walk every mod folder on the SD card, parse its SSBH
        binaries (numatb / numdlb / numshb / nusktb) with ssbh_data_py,
        and report the kind of file-level inconsistencies that cause
        "no texture" / "weird geometry" / "freeze on slot select"
        symptoms in-game.

        Surfaces results in a popup grouped by mod folder. Worker
        thread keeps the UI responsive — large SD cards have ~50+
        mods × multiple slots to parse.
        """
        if not (os.path.isdir(ARCROPOLIS_MODS)):
            messagebox.showinfo("Diagnose",
                                 f"No SD mods folder at "
                                 f"{ARCROPOLIS_MODS}.")
            return
        try:
            import ssbh_data_py  # noqa: F401
        except ImportError:
            messagebox.showerror(
                "Diagnose",
                "ssbh_data_py is not installed. Run setup.bat to "
                "pull it in (it's in requirements.txt).")
            return

        def _worker():
            try:
                print(f"\n=== SD Diagnostic ({ARCROPOLIS_MODS}) ===")
                issues = deep_diagnose_mods_root(ARCROPOLIS_MODS)
                self._print_diagnose_report(issues)
            except Exception as e:
                print(f"  ! Diagnose failed: {e}", file=sys.stderr)

        threading.Thread(target=_worker, daemon=True).start()

    def _print_diagnose_report(self, issues):
        """Dump the deep-diagnostic results to stdout (which the log
        panel mirrors), grouped by mod folder. No popup — keeping it
        inline with the rest of the install / load output.
        """
        if not issues:
            print("  ✓ No file-level issues detected. Every SSBH "
                  "binary parsed cleanly, every material's textures "
                  "resolved, every modl→mesh reference matched.\n")
            return
        freezes = sum(1 for i in issues if i["severity"] == "freeze")
        warns = len(issues) - freezes
        print(f"  🩺 {len(issues)} issue(s) — "
              f"{freezes} freeze, {warns} warning")
        print()
        # Group by mod folder, then by severity within each.
        by_mod = {}
        for it in issues:
            by_mod.setdefault(it["mod"], []).append(it)
        # Print mods with freeze issues first, then warning-only.
        def _sort_key(item):
            mod, mod_issues = item
            f = sum(1 for i in mod_issues if i["severity"] == "freeze")
            return (-f, -len(mod_issues), mod.lower())
        for mod_folder, mod_issues in sorted(
                by_mod.items(), key=_sort_key):
            mod_freezes = sum(1 for i in mod_issues
                              if i["severity"] == "freeze")
            tag = ("✗ FREEZE" if mod_freezes
                   else "• warn  ")
            print(f"  {tag}  {mod_folder}  "
                  f"({len(mod_issues)} issue{'s' if len(mod_issues)!=1 else ''})")
            for it in mod_issues:
                sev = it["severity"]
                sym = "      ✗" if sev == "freeze" else "      •"
                disp = INTERNAL_TO_DISPLAY.get(
                    it["fighter"], it["fighter"])
                print(f"{sym} {disp} {it['slot']} ({it['tree']}): "
                      f"{it['issue']}")
                if it.get("detail"):
                    detail = it["detail"]
                    if len(detail) > 220:
                        detail = detail[:220] + "…"
                    print(f"          {detail}")
            print()
        print(f"=== End SD Diagnostic ===\n")

    def _eject_sd(self):
        """Safely dismount the connected SD volume so the user can
        unplug the Switch / pull the card without Windows complaining
        the drive is in use.

        Uses the canonical Win32 sequence:
          1. ``CreateFileW`` on ``\\\\.\\<drive>:`` with FILE_SHARE_*
             flags to get a volume handle.
          2. ``FSCTL_LOCK_VOLUME`` — fail fast if anything else has
             the volume open.
          3. ``FSCTL_DISMOUNT_VOLUME`` — drop the file system.
          4. ``IOCTL_STORAGE_MEDIA_REMOVAL`` (PreventMediaRemoval=
             FALSE) and ``IOCTL_STORAGE_EJECT_MEDIA`` — physically
             eject for true removable volumes; on fixed-disk USB
             mass storage devices (Switch USB-storage mode) the
             dismount alone is what makes "Safe to Remove" work.

        The previous PowerShell ``Shell.Application.InvokeVerb('Eject')``
        approach was silently skipped for volumes Windows reports as
        "Fixed" — which the Switch's USB-storage often is.
        """
        if not (os.path.exists(SD_CARD) and os.path.isdir(SD_CARD)):
            messagebox.showinfo("Eject", "No SD card connected.")
            return
        drive_letter = SD_CARD.rstrip("\\/").rstrip(":")
        if not drive_letter:
            messagebox.showerror(
                "Eject",
                f"Couldn't parse SD drive letter from {SD_CARD!r}.")
            return
        # No confirmation prompt — clicking the button is the
        # confirmation. (Errors still surface a dialog.)

        def _do():
            ok, msg = _eject_volume_win32(drive_letter)
            if ok:
                print(f"\n=== Ejected SD ({drive_letter}:) — "
                      f"safe to disconnect ===\n")
                self.root.after(0, lambda: messagebox.showinfo(
                    "Eject",
                    f"Safely ejected {drive_letter}:\n\n"
                    "You can now unplug the Switch / remove the "
                    "SD card."))
                self.root.after(0, self._check_sd)
            else:
                print(f"  ! Eject failed: {msg}", file=sys.stderr)
                self.root.after(0, lambda m=msg:
                    messagebox.showerror(
                        "Eject failed",
                        f"Couldn't eject {drive_letter}:\n\n{m}\n\n"
                        "Common causes:\n"
                        "  • Another program has a file open on the "
                        "SD (close File Explorer windows).\n"
                        "  • An install / load is still running.\n"
                        "  • The mod cache has a render-cache image "
                        "or a .gb_meta.json open from a recent "
                        "preview — close any 3D viewer windows."))

        threading.Thread(target=_do, daemon=True).start()

    # ── Download progress bar ────────────────────────────

    def _show_progress(self, label="Downloading..."):
        """Show the progress bar in the log panel."""
        self._cancel_download = False
        def _do():
            self.progress_label.configure(text=label)
            self.progress_bar.configure(value=0)
            self.stop_btn.configure(state="normal")
            self.progress_frame.pack(fill="x", before=self.log)
        self.root.after(0, _do)

    def _hide_progress(self):
        """Hide the progress bar."""
        def _do():
            self.progress_frame.pack_forget()
        self.root.after(0, _do)

    def _cancel_current_download(self):
        """Signal the download thread to stop."""
        self._cancel_download = True
        self.stop_btn.configure(state="disabled")
        self.progress_label.configure(text="Cancelling...")
        print("  Download cancel requested...")

    def _update_progress(self, downloaded, total):
        """Update progress bar value (called from download thread, throttled)."""
        pct = int(downloaded * 100 / total) if total else 0
        mb_done = downloaded / 1024 / 1024
        mb_total = total / 1024 / 1024
        def _do():
            self.progress_bar.configure(value=pct, maximum=100)
            self.progress_label.configure(
                text=f"Downloading... {pct}%  ({mb_done:.1f} / {mb_total:.1f} MB)")
        self.root.after(0, _do)

    # ── View switching ───────────────────────────────────

    def _highlight_active_tab(self):
        """Update tab button styles to reflect active view."""
        for name, btn in self._tab_buttons.items():
            if name == self._active_view:
                btn.configure(bg=T.ACCENT, fg=T.BG)
            else:
                btn.configure(bg=T.SURFACE1, fg=T.FG)

    _typeahead_counter = 0  # class-level counter for unique tag names

    def _setup_combo_typeahead(self, combo):
        """Editable-combobox filtering. The combobox is in ``state="normal"``
        (an editable Entry that owns a dropdown), so the user can:

          • Type freely — what they type appears in the box.
          • Backspace works as expected (it's just a regular Entry).
          • As they type, the dropdown auto-filters to entries that
            CONTAIN their query (case-insensitive); the first match
            is highlighted.
          • Enter accepts the first filtered match (or the typed text
            verbatim if it exactly equals one of the values), restores
            the full unfiltered values list for next time, and fires
            ``<<ComboboxSelected>>`` so any wired search runs.
          • Escape clears the typed text back to the previous value
            and closes the dropdown.

        The original ``values`` list is captured up-front and stored
        on the widget so reload (when the view-switcher swaps fighter
        / stage / other / pack value sets) can be detected and the
        cached "all values" updated accordingly.
        """
        all_values = list(combo["values"])
        combo._smash_all_values = all_values
        last_committed = {"v": combo.get()}

        def _refresh_all_values_if_changed():
            # The view-switcher reconfigures `values=` when switching
            # between Fighter/Stage/Other/Pack. Detect that so our
            # cached "all values" stays accurate.
            current = list(combo["values"])
            cached = combo._smash_all_values
            if (set(current) != set(cached)
                    and len(current) > 0
                    and current != all_values):
                # Only treat it as a real change when the size is
                # "full" (>1) — filtered subsets are smaller.
                if len(current) >= len(cached) - 1:
                    combo._smash_all_values = current

        def _restore_all_values():
            try:
                combo.configure(values=combo._smash_all_values)
            except tk.TclError:
                pass

        def _filter(query):
            q = query.lower().strip()
            if not q:
                filtered = combo._smash_all_values
            else:
                # Prefix match on the full value first so "Pir" picks
                # **Piranha Plant** (not "Captain Falcon" because it
                # contains a P). If no prefix matches, fall back to
                # any value containing the query, so the dropdown
                # doesn't go empty for typos / partial matches.
                prefix = [v for v in combo._smash_all_values
                          if str(v).lower().startswith(q)]
                if prefix:
                    filtered = prefix
                else:
                    filtered = [v for v in combo._smash_all_values
                                if q in str(v).lower()]
            try:
                combo.configure(values=filtered)
            except tk.TclError:
                pass
            return filtered

        def _commit(value=None):
            if value is None:
                txt = combo.get().strip()
                exact = next((v for v in combo._smash_all_values
                              if str(v).lower() == txt.lower()), None)
                if exact is not None:
                    value = exact
                else:
                    # Prefer the row currently highlighted in our
                    # custom dropdown (Down-arrow nav), else first
                    # filtered, else the typed text.
                    chosen = None
                    lb = custom_dd.get("listbox")
                    if lb is not None and custom_dd["win"] is not None:
                        sel = lb.curselection()
                        if sel:
                            try:
                                chosen = lb.get(int(sel[0]))
                            except tk.TclError:
                                chosen = None
                    if chosen is None:
                        filtered = list(combo["values"])
                        if filtered:
                            chosen = filtered[0]
                    value = chosen if chosen is not None else txt
            try:
                combo.set(value)
            except tk.TclError:
                pass
            last_committed["v"] = value
            _restore_all_values()
            _close_custom_dropdown()
            try:
                combo.tk.call("ttk::combobox::Unpost", combo)
            except tk.TclError:
                pass
            try:
                combo.event_generate("<<ComboboxSelected>>")
            except tk.TclError:
                pass

        # Custom autocomplete dropdown. ttk::combobox's built-in
        # popdown installs a global grab that fights any attempt to
        # keep focus on the entry while the dropdown is visible. We
        # bypass that entirely with a plain Toplevel containing a
        # Listbox, anchored below the combo. No grab, no focus
        # transfer — typing always reaches the entry.
        custom_dd = {"win": None, "listbox": None}

        def _close_custom_dropdown():
            w = custom_dd.get("win")
            if w is None:
                return
            try:
                w.destroy()
            except tk.TclError:
                pass
            custom_dd["win"] = None
            custom_dd["listbox"] = None

        # Register the closer on `self` so other dialogs (Install,
        # etc.) can dismiss any lingering autocomplete dropdown when
        # they open. Without this, the topmost dropdown sits on top
        # of newly-opened popups and silently eats their clicks.
        if not hasattr(self, "_combo_dropdown_closers"):
            self._combo_dropdown_closers = []
        self._combo_dropdown_closers.append(_close_custom_dropdown)

        def _show_custom_dropdown(values):
            if not values:
                _close_custom_dropdown()
                return
            w = custom_dd.get("win")
            if w is None or not w.winfo_exists():
                w = tk.Toplevel(combo)
                w.overrideredirect(True)
                w.attributes("-topmost", True)
                lb = tk.Listbox(
                    w, bg=T.CRUST, fg=T.FG,
                    selectbackground=T.ACCENT, selectforeground=T.BG,
                    highlightthickness=0, bd=1, relief="solid",
                    activestyle="none",
                    font=(T.FONT, T.SZ_MD), exportselection=False)
                lb.pack(fill="both", expand=True)

                def _on_lb_click(_e=None):
                    sel = lb.curselection()
                    if sel:
                        try:
                            value = lb.get(int(sel[0]))
                        except tk.TclError:
                            return
                        _commit(value)
                lb.bind("<Button-1>",
                         lambda e: w.after(10, _on_lb_click), add="+")
                custom_dd["win"] = w
                custom_dd["listbox"] = lb
            lb = custom_dd["listbox"]
            lb.delete(0, "end")
            for v in values[:200]:
                lb.insert("end", v)
            lb.selection_clear(0, "end")
            lb.selection_set(0)
            lb.activate(0)
            # Cap the displayed row count so the dropdown doesn't run
            # off the screen for huge lists. Use the listbox's own
            # height attribute so we get accurate sizing from Tk.
            shown = min(len(values), 12)
            try:
                lb.configure(height=shown)
            except tk.TclError:
                pass

            # Position right below the combo, same width. Use the
            # listbox's natural reqheight + a few px so the bottom row
            # isn't visually clipped by the toplevel's border.
            try:
                combo.update_idletasks()
                x = combo.winfo_rootx()
                y = combo.winfo_rooty() + combo.winfo_height()
                width = max(combo.winfo_width(), 200)
                lb.update_idletasks()
                # reqheight already accounts for font metrics; add a
                # small bottom margin so the last row's descenders
                # ('g', 'p', 'y') aren't chopped.
                height = lb.winfo_reqheight() + 6
                w.geometry(f"{width}x{height}+{x}+{y}")
                w.deiconify()
                w.lift()
            except tk.TclError:
                pass

        def _on_key_release(event):
            if event.keysym in ("Return", "KP_Enter", "Escape",
                                 "Tab", "ISO_Left_Tab",
                                 "Up", "Down"):
                return None  # arrow keys handled by their own bindings
            _refresh_all_values_if_changed()
            query = combo.get()
            filtered = _filter(query)
            _show_custom_dropdown(filtered)
            return None

        def _nav_custom(direction):
            """Move highlight in the custom dropdown by ±1.
            Returns True if we actually moved (so the caller can
            'break' Tk's default arrow handling, which would
            otherwise cycle through ``values`` and re-render the
            combobox with its default (white) theme state).
            """
            lb = custom_dd.get("listbox")
            if lb is None or custom_dd.get("win") is None:
                return False
            try:
                size = lb.size()
            except tk.TclError:
                return False
            if size == 0:
                return False
            cur = lb.curselection()
            if cur:
                idx = int(cur[0]) + direction
            else:
                idx = 0 if direction > 0 else size - 1
            idx = max(0, min(idx, size - 1))
            lb.selection_clear(0, "end")
            lb.selection_set(idx)
            lb.activate(idx)
            lb.see(idx)
            return True

        def _on_down(_e):
            if _nav_custom(+1):
                return "break"
            # No custom dropdown open — show one and highlight first.
            _refresh_all_values_if_changed()
            query = combo.get()
            values = _filter(query) if query.strip() else combo._smash_all_values
            _show_custom_dropdown(values)
            return "break"

        def _on_up(_e):
            if _nav_custom(-1):
                return "break"
            return "break"

        def _on_return(_e):
            _commit()
            return "break"

        def _on_escape(_e):
            _restore_all_values()
            try:
                combo.set(last_committed["v"])
            except tk.TclError:
                pass
            _close_custom_dropdown()
            try:
                combo.tk.call("ttk::combobox::Unpost", combo)
            except tk.TclError:
                pass
            return "break"

        def _on_focus_in(_e):
            # Select the entire current text so the user's first
            # keystroke replaces it (matches typical address-bar UX).
            try:
                combo.select_range(0, "end")
                combo.icursor("end")
            except tk.TclError:
                pass

        def _on_combobox_selected(_e):
            # User clicked a row in the *built-in* dropdown (we still
            # leave that path functional via the dropdown arrow).
            last_committed["v"] = combo.get()
            _restore_all_values()
            _close_custom_dropdown()

        def _on_focus_out(_e):
            # Close the custom dropdown when focus leaves the combo
            # (clicked elsewhere). Delay slightly so a click ON the
            # custom dropdown's listbox can still register.
            self.root.after(150, _close_custom_dropdown)

        def _on_arrow_click(event):
            """Intercept clicks on the combobox dropdown arrow so our
            custom autocomplete dropdown shows instead of ttk's
            built-in popdown (which steals focus and looks different).
            Clicks anywhere else (the entry portion) fall through to
            normal Tk behavior so text editing still works.
            """
            try:
                elem = combo.identify(event.x, event.y)
            except tk.TclError:
                return None
            if not elem or "downarrow" not in elem.lower():
                return None  # entry click — let Tk handle
            # Toggle: if our custom dropdown is already up, close it.
            if custom_dd.get("win") is not None:
                _close_custom_dropdown()
                return "break"
            _refresh_all_values_if_changed()
            query = combo.get()
            # If the entry has typed text, show filtered matches;
            # otherwise show the full list.
            if query.strip():
                values = _filter(query)
            else:
                values = combo._smash_all_values
            _show_custom_dropdown(values)
            try:
                combo.focus_set()
                combo.icursor("end")
            except tk.TclError:
                pass
            return "break"

        combo.bind("<Button-1>", _on_arrow_click, add="+")
        combo.bind("<KeyRelease>", _on_key_release, add="+")
        combo.bind("<Down>", _on_down, add="+")
        combo.bind("<Up>", _on_up, add="+")
        combo.bind("<Return>", _on_return, add="+")
        combo.bind("<KP_Enter>", _on_return, add="+")
        combo.bind("<Escape>", _on_escape, add="+")
        combo.bind("<FocusIn>", _on_focus_in, add="+")
        combo.bind("<FocusOut>", _on_focus_out, add="+")
        combo.bind("<<ComboboxSelected>>", _on_combobox_selected, add="+")

    def _clear_typeahead(self):
        """Reset the type-ahead buffer for the fighter/stage combobox."""
        self._typeahead_buf = ""
        self._typeahead_timer = None

    def _switch_view(self, view_name):
        """Switch between Browse / Stages / Favorites / Installed / Setup."""
        self._active_view = view_name
        self._highlight_active_tab()

        # Hide setup actions pane when not on setup
        if view_name != "setup":
            self._setup_actions_frame.pack_forget()
            self._stop_rcm_poll()  # stop RCM polling when leaving Setup

        # Swap category dropdown between Fighter / Stage / Other / Packs
        if view_name in ("browse", "stages", "other", "packs"):
            self._configure_category_dropdown(view_name)

        if view_name == "browse":
            self.results_label.configure(text="")
            self.page_label.configure(text="")
            self._current_page = 1
            if self._content_filter.get() == "Adult Only":
                self._run_async(self._do_adult_only_audit)
            else:
                self._run_async(self._do_search)
        elif view_name in ("stages", "other", "packs"):
            self.results_label.configure(text="")
            self.page_label.configure(text="")
            self._current_page = 1
            self._run_async(self._do_search)
        elif view_name == "favorites":
            self._show_favorites()
        elif view_name == "installed":
            self._show_installed()
        elif view_name == "profiles":
            self._show_profiles()
        elif view_name == "setup":
            self._show_setup()

    def _configure_category_dropdown(self, mode):
        """Swap the category dropdown between fighter names, stage names,
        other categories, and gameplay-pack categories."""
        # Helper: update the typeahead's cached full values list so
        # filtering uses the right base set after a view switch.
        def _set_full_values(values):
            self.fighter_combo.configure(values=values)
            self.fighter_combo._smash_all_values = list(values)
        if mode == "other":
            self._category_label.configure(text="Category:")
            _set_full_values(self._other_names)
            if self.fighter_var.get() not in OTHER_CATEGORIES:
                self.fighter_var.set("All Other")
        elif mode == "packs":
            self._category_label.configure(text="Pack Type:")
            _set_full_values(self._pack_names)
            if self.fighter_var.get() not in PACK_CATEGORIES:
                self.fighter_var.set("All Packs")
        elif mode == "stages":
            self._category_label.configure(text="Stage:")
            _set_full_values(self._stage_names)
            if self.fighter_var.get() not in STAGE_CATEGORIES:
                self.fighter_var.set("All Stages")
        else:
            self._category_label.configure(text="Fighter:")
            _set_full_values(self._fighter_names)
            if self.fighter_var.get() not in FIGHTER_CATEGORIES:
                self.fighter_var.set("All Skins")

    def _on_profile_mode_toggle(self):
        """Legacy no-op: profile mode is now always on. Kept so that any
        stale callers don't crash."""
        self._profile_mode = True

    def _refresh_profile_list_silent(self):
        """Same as :meth:`_refresh_profile_list` but never pops a dialog if
        no profiles exist (used at startup)."""
        profiles = load_profiles()
        profile_names = sorted(profiles.keys())
        self._profile_combo.configure(values=profile_names)
        if profile_names:
            current = self._profile_list_var.get()
            if current not in profile_names:
                self._profile_list_var.set(profile_names[0])
            self._profile_mode_target = self._profile_list_var.get()
            self._recolor_all_slot_pickers()
        else:
            self._profile_list_var.set("")
            self._profile_mode_target = None

    def _refresh_profile_list(self):
        """Load available profiles into the profile combo."""
        profiles = load_profiles()
        profile_names = sorted(profiles.keys())
        self._profile_combo.configure(values=profile_names)
        if profile_names:
            # Auto-select first profile if not already selected
            current = self._profile_list_var.get()
            if current not in profile_names:
                self._profile_list_var.set(profile_names[0])
            self._profile_mode_target = self._profile_list_var.get()
            self._recolor_all_slot_pickers()
        else:
            self._profile_list_var.set("")
            self._profile_mode_target = None
            messagebox.showwarning(
                "No Profiles",
                "No profiles found. Create one from the Profiles tab "
                "before installing mods.")

    def _on_profile_mode_profile_change(self, _event=None):
        """Update active profile mode target and recolor visible slot pickers."""
        self._profile_mode_target = self._profile_list_var.get() or None
        self._recolor_all_slot_pickers()

    def _get_profile_occupied_slots(self, profile_name, fighter_int):
        """Return occupied cXX slots for a fighter from a saved profile."""
        occupied = {}
        if not profile_name or not fighter_int:
            return occupied

        profiles = load_profiles()
        profile = profiles.get(profile_name, {})
        mods = profile.get("mods", [])
        favs = load_favorites()
        display_name = INTERNAL_TO_DISPLAY.get(fighter_int)

        for mod in mods:
            if mod.get("mod_type", "skin") != "skin":
                continue
            mod_char = str(mod.get("character", ""))
            if mod_char != display_name and FIGHTER_INTERNAL.get(mod_char) != fighter_int:
                continue

            slot_value = str(mod.get("slot", ""))
            for part in slot_value.replace(",", " ").split():
                slot = part.strip().lower()
                if re.match(r"^c\d{2}$", slot):
                    thumb_url = mod.get("thumb_url")
                    if not thumb_url and mod.get("mod_id"):
                        fav_meta = favs.get(str(mod.get("mod_id")), {})
                        thumb_url = (fav_meta.get("thumb_url")
                                     or fav_meta.get("_cached_thumb_url"))
                    occupied[slot] = {
                        "mod": mod.get("folder_name") or mod.get("name", "?"),
                        "name": mod.get("name", "?"),
                        "thumb_url": thumb_url,
                        "mod_id": mod.get("mod_id"),
                        # Carries the source slot the user dragged
                        # from in the Install dialog. When set, the
                        # destination renderer prefers this exact
                        # cXX dir from the mod cache instead of
                        # falling back to "first cXX in the walk".
                        "source_slot": mod.get("source_slot") or "",
                    }

        return occupied

    def _recolor_all_slot_pickers(self):
        """Recolor every registered slot-picker button to reflect current
        profile / SD occupancy without rebuilding the view."""
        if not self._slot_picker_registry:
            return
        # Cache occupancy per fighter so we don't hit disk repeatedly
        occ_cache = {}
        for entry in self._slot_picker_registry:
            btn, fighter_int, slot, card_mod_id = entry
            try:
                if not btn.winfo_exists():
                    continue
            except Exception:
                continue

            key = (self._profile_mode, self._profile_mode_target, fighter_int)
            if key not in occ_cache:
                if fighter_int and self._profile_mode and self._profile_mode_target:
                    occ_cache[key] = self._get_profile_occupied_slots(
                        self._profile_mode_target, fighter_int)
                else:
                    occ_cache[key] = get_occupied_slots(fighter_int) if fighter_int else {}
            occupied = occ_cache[key]

            slot_info = occupied.get(slot)
            is_filled = slot_info is not None
            is_self = (is_filled
                       and card_mod_id is not None
                       and str(slot_info.get("mod_id") or "") == str(card_mod_id))

            if is_self:
                new_bg, new_fg = T.ACCENT, T.BG
            elif is_filled:
                new_bg, new_fg = T.GREEN, T.BG
            else:
                new_bg, new_fg = T.SURFACE1, T.OVERLAY

            try:
                # Don't revert an optimistic click: if we'd go gray but the
                # button is already blue (ACCENT), a pending write hasn't
                # landed yet — trust the in-place update and skip.
                try:
                    current_bg = btn.cget("bg")
                except Exception:
                    current_bg = ""
                if new_bg == T.SURFACE1 and current_bg == T.ACCENT:
                    continue

                btn.configure(bg=new_bg, fg=new_fg)
                # Rebind hover so tooltip / leave-color stay in sync
                friendly = slot_info["name"] if is_filled else None
                thumb = slot_info.get("thumb_url") if is_filled else None
                if is_filled:
                    btn.bind("<Enter>", lambda e, b=btn, t=friendly, tu=thumb: (
                        b.configure(bg=T.YELLOW, fg=T.BG),
                        self._show_tooltip(b, t, thumb_url=tu)))
                    btn.bind("<Leave>", lambda e, b=btn, c=new_bg: (
                        b.configure(bg=c, fg=T.BG),
                        self._hide_tooltip()))
                else:
                    btn.bind("<Enter>", lambda e, b=btn: (
                        b.configure(bg=T.OVERLAY, fg=T.BG),
                        self._show_tooltip(b, "Empty — click to install")))
                    btn.bind("<Leave>", lambda e, b=btn: (
                        b.configure(bg=T.SURFACE1, fg=T.OVERLAY),
                        self._hide_tooltip()))
            except Exception:
                pass

        # Update slot counter labels
        for lbl, card_mod_id in self._slot_counter_registry:
            self._update_slot_counter(lbl, card_mod_id)

        # Update non-slot Install buttons (stage / modpack / unmapped-fighter
        # cards) to reflect whether the mod is already in the active profile.
        self._recolor_install_buttons()

    def _register_install_button(self, btn, mod_id, mod_name, metadata):
        """Track a plain (non-slot-picker) Install button so its label
        can be flipped to "In Profile" / back when the mod is added or
        removed from the active profile.

        Also wires the click: clicking when already in-profile removes
        the mod, so the same button serves as a toggle.
        """
        self._install_btn_registry.append(
            (btn, mod_id, mod_name, metadata))
        # Reflect current state immediately.
        self._refresh_install_button(btn, mod_id, mod_name, metadata)

    def _is_mod_in_active_profile(self, mod_id):
        """Return True if ``mod_id`` is in the active user profile."""
        target = self._profile_mode_target
        if not target or mod_id is None:
            return False
        try:
            profiles = load_profiles()
            entries = profiles.get(target, {}).get("mods", [])
            mid = str(mod_id)
            return any(str(m.get("mod_id")) == mid for m in entries)
        except Exception:
            return False

    def _refresh_install_button(self, btn, mod_id, mod_name, metadata):
        """Repaint a single Install button based on profile membership."""
        try:
            if not btn.winfo_exists():
                return
        except Exception:
            return
        in_profile = self._is_mod_in_active_profile(mod_id)
        if in_profile:
            btn.configure(
                text="✓ In Profile  (remove)",
                bg=T.SURFACE1, fg=T.OVERLAY,
                command=lambda mid=mod_id, mn=mod_name: self._run_async(
                    self._remove_mod_from_active_profile, mid, mn))
        else:
            mtype = (metadata or {}).get("mod_type", "skin")
            # Mirror the original label conventions used at creation time.
            if mtype == "stage":
                label = "Install"
            elif mtype not in ("skin",):
                label = f"Install ({mtype.title()})"
            else:
                label = "Install"
            btn.configure(
                text=label,
                bg=T.GREEN, fg=T.BG,
                command=lambda mid=mod_id, mn=mod_name, m=metadata:
                    self._run_async(self._install_mod, mid, mn, m))

    def _recolor_install_buttons(self):
        """Sync every registered plain Install button with current
        profile membership, pruning any whose widgets have been
        destroyed (view change, etc.)."""
        if not self._install_btn_registry:
            return
        live = []
        for entry in self._install_btn_registry:
            btn, mod_id, mod_name, metadata = entry
            try:
                if not btn.winfo_exists():
                    continue
            except Exception:
                continue
            self._refresh_install_button(btn, mod_id, mod_name, metadata)
            live.append(entry)
        self._install_btn_registry = live

    def _remove_mod_from_active_profile(self, mod_id, mod_name):
        """Remove a mod from the active profile (used by the in-profile
        toggle on a plain Install button)."""
        target = self._profile_mode_target
        if not target:
            return
        remove_mod_from_profile(target, mod_id=mod_id)
        print(f"  Removed '{mod_name}' from profile '{target}'")
        self.root.after(0, self._recolor_all_slot_pickers)

    def _refresh_current_view(self):
        """Re-render the current view so slot pickers pick up SD changes."""
        if self._active_view == "browse":
            if self._content_filter.get() == "Adult Only":
                self._run_async(self._do_adult_only_audit)
            else:
                self._run_async(self._do_search)
        elif self._active_view == "stages":
            self._run_async(self._do_search)
        elif self._active_view == "other":
            self._run_async(self._do_search)
        elif self._active_view == "packs":
            self._run_async(self._do_search)
        elif self._active_view == "favorites":
            self._show_favorites()
        elif self._active_view == "installed":
            self._show_installed()
        elif self._active_view == "profiles":
            # If a specific profile detail page is open, re-render that
            # one instead of bouncing the user back to the profile list.
            opened = getattr(self, "_open_profile_name", None)
            if opened:
                self._open_profile(opened)
            else:
                self._show_profiles()
        elif self._active_view == "setup":
            self._show_setup()

    def _passes_content_filter(self, rec_or_meta):
        """Check if a mod passes the current content filter.
        rec_or_meta can be a raw API record or stored metadata dict."""
        filt = self._content_filter.get()
        if filt == "All Content":
            return True
        mature = is_mod_mature(rec_or_meta)
        if filt == "Kid Friendly":
            return not mature
        if filt == "Adult Only":
            return mature
        return True

    def _show_favorites(self):
        """Populate results pane with favorited mods, grouped by character."""
        favs = load_favorites()
        # Hide pagination
        self.prev_btn.configure(state="disabled")
        self.next_btn.configure(state="disabled")
        self.page_label.configure(text="")

        # Apply content filter (with audit logging)
        filt = self._content_filter.get()
        filtered = {}
        hidden_items = []
        for k, v in favs.items():
            mature, reason = is_mod_mature_detailed(v)
            passes = (filt == "All Content"
                      or (filt == "Kid Friendly" and not mature)
                      or (filt == "Adult Only" and mature))
            if passes:
                filtered[k] = v
            else:
                hidden_items.append((v.get("name", "?"), reason))
        hidden = len(favs) - len(filtered)

        # Audit log
        if hidden_items:
            print(f"  [Filter: {filt}] Favorites: {len(filtered)} shown, "
                  f"{hidden} hidden")
            for hname, hreason in hidden_items:
                print(f"    ✕ {hname}  ({hreason})")

        # ── Apply type filter (Skins Only / Stages Only / All) ──
        type_filt = self._fav_filter
        if type_filt == "Skins Only":
            filtered = {k: v for k, v in filtered.items()
                        if v.get("mod_type", "skin") != "stage"}
        elif type_filt == "Stages Only":
            filtered = {k: v for k, v in filtered.items()
                        if v.get("mod_type") == "stage"}

        # ── Apply local search text ──
        search_text = self.search_var.get().strip().lower()
        if search_text:
            filtered = {
                k: v for k, v in filtered.items()
                if (search_text in v.get("name", "").lower()
                    or search_text in v.get("submitter", "").lower()
                    or any(search_text in t.lower()
                           for t in v.get("tags", [])))
            }

        lbl = f"{len(filtered)} favorite(s)"
        if hidden:
            lbl += f"  ({hidden} hidden by {filt} filter)"
        if search_text:
            lbl += f'  — search: "{search_text}"'
        if type_filt != "All":
            lbl += f"  [{type_filt}]"
        self.results_label.configure(text=lbl)

        # Clear results
        self._slot_picker_registry.clear()
        self._slot_counter_registry.clear()
        for w in self.results_inner.winfo_children():
            w.destroy()
        self._thumb_cache.clear()
        # Reset scroll position and region when clearing, to avoid stale state
        self.results_canvas.yview_moveto(0)
        self.results_canvas.configure(scrollregion="0 0 0 0")

        # ── Filter bar (type dropdown) ──
        filter_bar = tk.Frame(self.results_inner, bg=T.SURFACE1)
        filter_bar.pack(fill="x", padx=4, pady=(6, 4))

        tk.Label(filter_bar, text="Show:", bg=T.SURFACE1, fg=T.FG,
                 font=(T.FONT, T.SZ_MD)).pack(side="left", padx=(8, 4))

        fav_filt_var = tk.StringVar(value=self._fav_filter)
        fav_filt_combo = ttk.Combobox(
            filter_bar, textvariable=fav_filt_var,
            values=["All", "Skins Only", "Stages Only"],
            state="readonly", width=14,
            font=(T.FONT, T.SZ_MD))
        fav_filt_combo.pack(side="left", padx=(0, 12))

        def _on_fav_filter_change(event=None):
            self._fav_filter = fav_filt_var.get()
            self._show_favorites()

        fav_filt_combo.bind("<<ComboboxSelected>>", _on_fav_filter_change)

        if search_text:
            def _clear_search():
                self.search_var.set("")
                self._show_favorites()
            tk.Button(filter_bar, text=f"✕ Clear search", width=14,
                      bg=T.PEACH, fg=T.BG, font=(T.FONT, T.SZ_SM, "bold"),
                      relief="flat", cursor="hand2",
                      command=_clear_search).pack(side="left", padx=(0, 6))

        if not filtered:
            filt = self._content_filter.get()
            if not favs:
                msg = ("No favorites yet.\n"
                       "Click 'Favorite' on any mod to save it here.")
            elif search_text:
                msg = f'No favorites match "{search_text}".'
            else:
                msg = f"No favorites match the current filters."
            tk.Label(self.results_inner, text=msg,
                     bg=T.SURFACE, fg=T.OVERLAY,
                     font=(T.FONT, T.SZ_XL), justify="center").pack(pady=40)
            return

        # Separate stages from skins
        stage_favs = {}
        skin_favs = {}
        for mod_id_str, meta in filtered.items():
            if meta.get("mod_type") == "stage":
                stage_favs[mod_id_str] = meta
            else:
                skin_favs[mod_id_str] = meta

        # ── Stages section ──
        if stage_favs:
            hdr = tk.Frame(self.results_inner, bg=T.ACCENT)
            hdr.pack(fill="x", padx=4, pady=(10, 2))
            tk.Label(hdr, text=f"  🗺  Stages  ({len(stage_favs)})",
                     bg=T.ACCENT, fg=T.BG,
                     font=(T.FONT, T.SZ_LG, "bold"), anchor="w").pack(fill="x", padx=6, pady=3)
            for meta in stage_favs.values():
                self._add_favorite_card(meta)

        # ── Skins section — group by character ──
        if skin_favs:
            groups = {}
            for mod_id_str, meta in skin_favs.items():
                char = _guess_character_from_meta(meta)
                groups.setdefault(char, []).append(meta)

            # Sort: named characters first (alphabetically), "Other" last
            sorted_chars = sorted(groups.keys(), key=lambda c: (c == "Other", c == "Unknown", c))

            if stage_favs:
                # Add a separator between stages and skins
                sep_hdr = tk.Frame(self.results_inner, bg=T.ACCENT)
                sep_hdr.pack(fill="x", padx=4, pady=(14, 2))
                tk.Label(sep_hdr, text=f"  🎨  Skins  ({len(skin_favs)})",
                         bg=T.ACCENT, fg=T.BG,
                         font=(T.FONT, T.SZ_LG, "bold"), anchor="w").pack(fill="x", padx=6, pady=3)

            for char in sorted_chars:
                items = groups[char]
                # Section header
                hdr = tk.Frame(self.results_inner, bg=T.SURFACE1)
                hdr.pack(fill="x", padx=4, pady=(10, 2))
                tk.Label(hdr, text=f"  {char}  ({len(items)})",
                         bg=T.SURFACE1, fg=T.ACCENT,
                         font=(T.FONT, T.SZ_LG, "bold"), anchor="w").pack(fill="x", padx=6, pady=3)
                for meta in items:
                    self._add_favorite_card(meta)

    def _add_favorite_card(self, meta):
        """Add a card for a favorited mod."""
        mod_id = meta.get("mod_id")
        name = meta.get("name", "Unknown")
        submitter = meta.get("submitter", "?")
        likes = meta.get("likes", 0)
        views = meta.get("views", 0)
        has_files = meta.get("has_files", False)
        url = meta.get("url", "")
        tags = meta.get("tags", [])
        thumb_url = meta.get("thumb_url")
        image_urls = meta.get("image_urls", [])

        card = tk.Frame(self.results_inner, bg=T.BG, padx=8, pady=6)
        card.pack(fill="x", padx=8, pady=4)

        # Thumbnail
        thumb_frame = tk.Frame(card, bg=T.SURFACE1, width=250, height=140)
        thumb_frame.pack(side="left", padx=(0, 10))
        thumb_frame.pack_propagate(False)

        thumb_label = tk.Label(thumb_frame, text="Loading...",
                               bg=T.SURFACE1, fg=T.OVERLAY, font=(T.FONT, T.SZ_SM))
        thumb_label.pack(expand=True)

        if thumb_url:
            threading.Thread(target=self._load_thumb,
                           args=(thumb_label, thumb_url, mod_id),
                           daemon=True).start()

        # Click thumbnail to open image gallery (fetches from API if needed)
        if thumb_url or image_urls:
            def _open_gallery(e=None, n=name, urls=image_urls,
                              mid=mod_id):
                self._open_gallery_with_fetch(n, urls, mod_id=mid,
                                              fav_id=mid)
            thumb_label.configure(cursor="hand2")
            thumb_label.bind("<Button-1>", _open_gallery)
            if len(image_urls) > 1:
                badge = tk.Label(thumb_frame, text=f"📷 {len(image_urls)}",
                                 bg=T.SURFACE1, fg=T.ACCENT,
                                 font=(T.FONT, T.SZ_XS))
                badge.place(relx=1.0, rely=1.0, anchor="se", x=-4, y=-2)

        # Info
        info = tk.Frame(card, bg=T.BG)
        info.pack(side="left", fill="both", expand=True)

        is_stage = meta.get("mod_type") == "stage"

        title_row = tk.Frame(info, bg=T.BG)
        title_row.pack(fill="x")
        tk.Label(title_row, text=name, bg=T.BG, fg=T.FG,
                 font=(T.FONT, T.SZ_XL, "bold"), anchor="w",
                 wraplength=400).pack(side="left", fill="x", expand=True)
        if is_stage:
            tk.Label(title_row, text="STAGE", bg=T.SURFACE1, fg=T.ACCENT,
                     font=(T.FONT, T.SZ_XS, "bold"), padx=4).pack(
                         side="right", padx=(4, 0))

        stats = f"by {submitter}  |  {likes} likes  |  {views:,} views"
        tk.Label(info, text=stats, bg=T.BG, fg=T.SUBTEXT,
                 font=(T.FONT, T.SZ_SM), anchor="w").pack(fill="x", pady=(2, 4))

        if tags:
            tag_text = ", ".join(tags[:3])
            if len(tags) > 3:
                tag_text += f" (+{len(tags)-3})"
            tk.Label(info, text=tag_text, bg=T.BG, fg=T.SUBTEXT,
                     font=(T.FONT, T.SZ_XS), anchor="w", wraplength=420).pack(fill="x")

        # Buttons
        btn_row = tk.Frame(info, bg=T.BG)
        btn_row.pack(fill="x", pady=(4, 0))

        if has_files:
            if is_stage:
                # Stage mod: simple install (no slot picker)
                stage_meta = {**meta, "mod_type": "stage"}
                _ib = tk.Button(btn_row, width=18,
                          font=(T.FONT, T.SZ_MD, "bold"),
                          relief="flat", cursor="hand2")
                _ib.pack(side="left", padx=(0, 6))
                self._register_install_button(_ib, mod_id, name, stage_meta)
            else:
                # Skin mod: try slot picker or plain install
                char = _guess_character_from_meta(meta)
                fighter_int = FIGHTER_INTERNAL.get(char)
                if fighter_int:
                    self._add_slot_picker(btn_row, mod_id, name, meta, fighter_int)
                else:
                    _ib = tk.Button(btn_row, width=14,
                              font=(T.FONT, T.SZ_MD, "bold"),
                              relief="flat", cursor="hand2")
                    _ib.pack(side="left", padx=(0, 6))
                    self._register_install_button(_ib, mod_id, name, meta)

        # Second button row for unfav/open
        btn_row2 = tk.Frame(info, bg=T.BG)
        btn_row2.pack(fill="x", pady=(2, 0))

        def _unfav(mid=mod_id, mn=name, c=card):
            remove_favorite(mid)
            c.destroy()
            print(f"  Removed '{mn}' from favorites")
            # Update count
            favs = load_favorites()
            self.results_label.configure(text=f"{len(favs)} favorite(s)")

        tk.Button(btn_row2, text="Unfavorite", width=10,
                  bg=T.PEACH, fg=T.BG, font=(T.FONT, T.SZ_SM, "bold"),
                  relief="flat", cursor="hand2",
                  command=_unfav).pack(side="left", padx=(0, 6))

        if url:
            tk.Button(btn_row2, text="Open Page", width=10,
                      bg=T.SURFACE1, fg=T.FG, font=(T.FONT, T.SZ_SM),
                      relief="flat", cursor="hand2",
                      command=lambda u=url: os.startfile(u)
                      ).pack(side="left", padx=(0, 6))

        # "Assign Category" button — always available so user can reassign
        def _reassign_category(mid=mod_id, mn=name):
            self._reassign_favorite_category(mid, mn)

        tk.Button(btn_row2, text="🏷 Assign", width=10,
                  bg=T.ACCENT,
                  fg=T.BG, font=(T.FONT, T.SZ_SM, "bold"),
                  relief="flat", cursor="hand2",
                  command=_reassign_category).pack(side="left", padx=(0, 6))

    def _reassign_favorite_category(self, mod_id, mod_name):
        """Show a dialog to reassign a favorite's mod_type (skin/stage)
        and optionally tag it with a fighter name for grouping."""
        favs = load_favorites()
        entry = favs.get(str(mod_id))
        if not entry:
            return

        dlg = tk.Toplevel(self.root)
        dlg.title(f"Assign Category — {mod_name}")
        dlg.configure(bg=T.BG)
        dlg.resizable(False, False)
        dlg.grab_set()

        tk.Label(dlg, text=f"Assign category for:", bg=T.BG, fg=T.SUBTEXT,
                 font=(T.FONT, T.SZ_MD)).pack(padx=20, pady=(14, 2))
        tk.Label(dlg, text=mod_name, bg=T.BG, fg=T.FG,
                 font=(T.FONT, T.SZ_LG, "bold"),
                 wraplength=360).pack(padx=20, pady=(0, 10))

        cur_type = entry.get("mod_type", "skin")
        tk.Label(dlg, text=f"Current type: {cur_type}", bg=T.BG, fg=T.OVERLAY,
                 font=(T.FONT, T.SZ_SM)).pack(padx=20, pady=(0, 8))

        # ── Type selector ──
        type_frame = tk.Frame(dlg, bg=T.BG)
        type_frame.pack(fill="x", padx=20, pady=(0, 8))
        tk.Label(type_frame, text="Type:", bg=T.BG, fg=T.FG,
                 font=(T.FONT, T.SZ_MD)).pack(side="left", padx=(0, 6))
        type_var = tk.StringVar(value=cur_type)
        for val, label_text in [("skin", "🎨 Skin"), ("stage", "🗺 Stage")]:
            tk.Radiobutton(type_frame, text=label_text, variable=type_var,
                           value=val, bg=T.BG, fg=T.FG,
                           activebackground=T.BG, activeforeground=T.ACCENT,
                           selectcolor=T.SURFACE1,
                           font=(T.FONT, T.SZ_MD)).pack(side="left", padx=(0, 10))

        # ── Fighter tag (for skins) ──
        fighter_frame = tk.Frame(dlg, bg=T.BG)
        fighter_frame.pack(fill="x", padx=20, pady=(0, 8))
        tk.Label(fighter_frame, text="Fighter tag:", bg=T.BG, fg=T.FG,
                 font=(T.FONT, T.SZ_MD)).pack(side="left", padx=(0, 6))

        # Build fighter list (same as category dropdown but without meta entries)
        fighter_choices = ["(none)"] + sorted(
            k for k in FIGHTER_CATEGORIES.keys()
            if k not in ("All Skins", "Assist Trophies/Pokemon", "Bosses",
                         "Items", "Other/Misc", "Packs", "Mii Hats"))
        cur_tags = entry.get("tags", [])
        # Try to detect current fighter tag from tags
        cur_fighter = "(none)"
        known_lower = {k.lower(): k for k in fighter_choices if k != "(none)"}
        for t in cur_tags:
            if t.lower() in known_lower:
                cur_fighter = known_lower[t.lower()]
                break
        # Fallback: use the character guess (checks tags + mod name)
        if cur_fighter == "(none)":
            guessed = _guess_character_from_meta(entry)
            if guessed in fighter_choices:
                cur_fighter = guessed
            elif guessed.lower() in known_lower:
                cur_fighter = known_lower[guessed.lower()]

        fighter_var = tk.StringVar(value=cur_fighter)
        fighter_combo = ttk.Combobox(
            fighter_frame, textvariable=fighter_var,
            values=fighter_choices, state="normal", width=22,
            font=(T.FONT, T.SZ_MD))
        fighter_combo.pack(side="left", padx=(0, 6))
        self._setup_combo_typeahead(fighter_combo)

        tk.Label(dlg, text="(Fighter tag helps group skins under\n"
                 "the correct character heading)",
                 bg=T.BG, fg=T.OVERLAY, font=(T.FONT, T.SZ_XS),
                 justify="center").pack(padx=20, pady=(0, 10))

        # ── Buttons ──
        btn_frame = tk.Frame(dlg, bg=T.BG)
        btn_frame.pack(fill="x", padx=20, pady=(0, 14))

        def _save():
            new_type = type_var.get()
            new_fighter = fighter_var.get()
            # Update mod_type
            entry["mod_type"] = new_type
            # Update tags with fighter name if chosen
            if new_fighter != "(none)":
                tags = list(entry.get("tags", []))
                # Remove old fighter tags
                tags = [t for t in tags
                        if t.lower() not in known_lower]
                tags.insert(0, new_fighter)
                entry["tags"] = tags
            favs[str(mod_id)] = entry
            save_favorites(favs)
            print(f"  Reassigned '{mod_name}': type={new_type}"
                  + (f", fighter={new_fighter}" if new_fighter != "(none)" else ""))
            dlg.destroy()
            # Refresh favorites view
            self._show_favorites()

        tk.Button(btn_frame, text="Save", width=10,
                  bg=T.GREEN, fg=T.BG, font=(T.FONT, T.SZ_MD, "bold"),
                  relief="flat", cursor="hand2",
                  command=_save).pack(side="left", padx=(0, 8))
        tk.Button(btn_frame, text="Cancel", width=10,
                  bg=T.SURFACE1, fg=T.FG, font=(T.FONT, T.SZ_MD),
                  relief="flat", cursor="hand2",
                  command=dlg.destroy).pack(side="left")

        # Center dialog over main window
        dlg.update_idletasks()
        w = dlg.winfo_width()
        h = dlg.winfo_height()
        x = self.root.winfo_rootx() + (self.root.winfo_width() - w) // 2
        y = self.root.winfo_rooty() + (self.root.winfo_height() - h) // 2
        dlg.geometry(f"+{x}+{y}")

    def _show_installed(self):
        """Populate results pane with installed skins from SD card, grouped by character."""
        self.prev_btn.configure(state="disabled")
        self.next_btn.configure(state="disabled")
        self.page_label.configure(text="")

        # Clear results
        self._slot_picker_registry.clear()
        self._slot_counter_registry.clear()
        for w in self.results_inner.winfo_children():
            w.destroy()
        self._thumb_cache.clear()
        # Reset scroll position and region when clearing, to avoid stale state
        self.results_canvas.yview_moveto(0)
        self.results_canvas.configure(scrollregion="0 0 0 0")

        if not os.path.exists(ARCROPOLIS_MODS):
            self.results_label.configure(text="SD card mods folder not found")
            tk.Label(self.results_inner, text="SD card or mods folder not detected.\n"
                     f"Expected: {ARCROPOLIS_MODS}",
                     bg=T.SURFACE, fg=T.RED,
                     font=(T.FONT, T.SZ_XL), justify="center").pack(pady=40)
            return

        skins = list_installed_skins()

        # Apply content filter (with audit logging)
        filt = self._content_filter.get()
        filtered = []
        hidden_items = []
        for skin in skins:
            meta = skin.get("meta") or {}
            # Also check the mod folder name for keyword matching
            check = dict(meta)
            if not check.get("name"):
                check["name"] = skin.get("name", "")
            mature, reason = is_mod_mature_detailed(check)
            passes = (filt == "All Content"
                      or (filt == "Kid Friendly" and not mature)
                      or (filt == "Adult Only" and mature))
            if passes:
                filtered.append(skin)
            else:
                hidden_items.append((check.get("name", "?"), reason))

        hidden = len(skins) - len(filtered)
        lbl = f"{len(filtered)} installed skin(s) on SD"
        if hidden:
            lbl += f"  ({hidden} hidden by {filt} filter)"
        self.results_label.configure(text=lbl)

        # Audit log
        if hidden_items:
            print(f"  [Filter: {filt}] Installed: {len(filtered)} shown, "
                  f"{hidden} hidden")
            for hname, hreason in hidden_items:
                print(f"    ✕ {hname}  ({hreason})")

        if not filtered:
            filt = self._content_filter.get()
            msg = ("No skins installed yet.\n"
                   "Use Browse to find and install skins!"
                   if not skins else
                   f"No installed skins match the '{filt}' filter.")
            tk.Label(self.results_inner, text=msg,
                     bg=T.SURFACE, fg=T.OVERLAY,
                     font=(T.FONT, T.SZ_XL), justify="center").pack(pady=40)
            return

        # ── "Uninstall All Skins" action bar ──
        action_bar = tk.Frame(self.results_inner, bg=T.SURFACE)
        action_bar.pack(fill="x", padx=4, pady=(6, 2))

        all_skin_names = [s["name"] for s in skins]  # unfiltered list

        tk.Button(
            action_bar, text="🗑  Uninstall All Skins", width=22,
            bg=T.RED, fg="#1e1e2e", font=(T.FONT, T.SZ_MD, "bold"),
            relief="flat", cursor="hand2",
            command=lambda names=all_skin_names:
                self._uninstall_all_skins(names),
        ).pack(side="left", padx=8, pady=6)

        tk.Label(action_bar,
                 text=f"{len(all_skin_names)} skin(s) installed",
                 bg=T.SURFACE, fg=T.OVERLAY,
                 font=(T.FONT, T.SZ_SM)).pack(side="left", padx=4)

        tk.Button(
            action_bar, text="💾  Save as Profile", width=16,
            bg=T.ACCENT, fg=T.BG, font=(T.FONT, T.SZ_MD, "bold"),
            relief="flat", cursor="hand2",
            command=self._create_profile_from_installed_ui,
        ).pack(side="right", padx=8, pady=6)

        # Group by character
        groups = {}
        for skin in filtered:
            char = skin.get("character", "Other")
            groups.setdefault(char, []).append(skin)

        sorted_chars = sorted(groups.keys(), key=lambda c: (c == "Other", c))

        for char in sorted_chars:
            items = groups[char]
            # Section header
            hdr = tk.Frame(self.results_inner, bg=T.SURFACE1)
            hdr.pack(fill="x", padx=4, pady=(10, 2))
            tk.Label(hdr, text=f"  {char}  ({len(items)})",
                     bg=T.SURFACE1, fg=T.ACCENT,
                     font=(T.FONT, T.SZ_LG, "bold"), anchor="w").pack(fill="x", padx=6, pady=3)
            for skin in items:
                self._add_installed_card(skin)

    def _add_installed_card(self, skin):
        """Add a card for an installed skin on SD — with thumbnail if metadata exists."""
        name = skin["name"]
        path = skin["path"]
        fc = skin["file_count"]
        size_mb = skin["size"] / 1024 / 1024
        meta = skin.get("meta")  # from .gb_meta.json

        card = tk.Frame(self.results_inner, bg=T.BG, padx=8, pady=6)
        card.pack(fill="x", padx=8, pady=4)

        # Thumbnail (real image if metadata available, fallback icon otherwise)
        thumb_frame = tk.Frame(card, bg=T.SURFACE1, width=250, height=140)
        thumb_frame.pack(side="left", padx=(0, 10))
        thumb_frame.pack_propagate(False)

        thumb_label = tk.Label(thumb_frame, text="[MOD]",
                               bg=T.SURFACE1, fg=T.ACCENT,
                               font=(T.MONO, T.SZ_LG, "bold"))
        thumb_label.pack(expand=True)

        thumb_url = meta.get("thumb_url") if meta else None
        image_urls = meta.get("image_urls", []) if meta else []
        mod_id = meta.get("mod_id") if meta else None
        if thumb_url:
            thumb_label.configure(text="Loading...", fg=T.OVERLAY,
                                  font=(T.FONT, T.SZ_SM))
            cache_key = f"installed_{name}"
            threading.Thread(target=self._load_thumb,
                           args=(thumb_label, thumb_url, cache_key),
                           daemon=True).start()

        # Click thumbnail to open image gallery (fetches from API if needed)
        if thumb_url or image_urls:
            display_name = meta.get("name", name) if meta else name
            meta_file = os.path.join(path, ".gb_meta.json")

            def _open_gallery(e=None, n=display_name, urls=image_urls,
                              mid=mod_id, mp=meta_file):
                self._open_gallery_with_fetch(n, urls, mod_id=mid,
                                              meta_path=mp)
            thumb_label.configure(cursor="hand2")
            thumb_label.bind("<Button-1>", _open_gallery)
            if len(image_urls) > 1:
                badge = tk.Label(thumb_frame, text=f"📷 {len(image_urls)}",
                                 bg=T.SURFACE1, fg=T.ACCENT,
                                 font=(T.FONT, T.SZ_XS))
                badge.place(relx=1.0, rely=1.0, anchor="se", x=-4, y=-2)

        # Info
        info = tk.Frame(card, bg=T.BG)
        info.pack(side="left", fill="both", expand=True)

        # Use original name from metadata if available, folder name as fallback
        display_name = meta.get("name", name) if meta else name
        title_row = tk.Frame(info, bg=T.BG)
        title_row.pack(fill="x")
        tk.Label(title_row, text=display_name, bg=T.BG, fg=T.FG,
                 font=(T.FONT, T.SZ_XL, "bold"), anchor="w",
                 wraplength=400).pack(side="left", fill="x", expand=True)
        if skin.get("mod_type") == "stage":
            tk.Label(title_row, text="STAGE", bg=T.SURFACE1, fg=T.ACCENT,
                     font=(T.FONT, T.SZ_XS, "bold"), padx=4).pack(
                         side="right", padx=(4, 0))

        # Show submitter/stats from metadata if available
        if meta and meta.get("submitter"):
            submitter = meta.get("submitter", "?")
            likes = meta.get("likes", 0)
            views = meta.get("views", 0)
            stats_text = f"by {submitter}  |  {likes} likes  |  {views:,} views"
            tk.Label(info, text=stats_text, bg=T.BG, fg=T.SUBTEXT,
                     font=(T.FONT, T.SZ_SM), anchor="w").pack(fill="x", pady=(2, 0))

        details = f"{fc} files  |  {size_mb:.1f} MB"
        if name != display_name:
            details += f"  |  Folder: {name}"
        tk.Label(info, text=details, bg=T.BG, fg=T.SUBTEXT,
                 font=(T.FONT, T.SZ_SM), anchor="w").pack(fill="x", pady=(2, 4))

        # Show tags from metadata
        if meta and meta.get("tags"):
            tags = meta["tags"]
            tag_text = ", ".join(tags[:3])
            if len(tags) > 3:
                tag_text += f" (+{len(tags)-3})"
            tk.Label(info, text=tag_text, bg=T.BG, fg=T.SUBTEXT,
                     font=(T.FONT, T.SZ_XS), anchor="w", wraplength=420).pack(fill="x")

        # ── Per-slot display & uninstall (skins only) ──
        is_stage = skin.get("mod_type") == "stage"
        if not is_stage:
            mod_slots = get_mod_slots(path)
        else:
            mod_slots = {}
        if mod_slots:
            for fighter_int, slots in mod_slots.items():
                slot_row = tk.Frame(info, bg=T.BG)
                slot_row.pack(fill="x", pady=(3, 0))
                fighter_disp = INTERNAL_TO_DISPLAY.get(fighter_int, fighter_int)
                tk.Label(slot_row, text=f"{fighter_disp}:",
                         bg=T.BG, fg=T.SUBTEXT,
                         font=(T.FONT, T.SZ_SM)).pack(side="left", padx=(0, 4))

                for slot in slots:
                    def _do_remove_slot(n=name, p=path, fi=fighter_int,
                                        s=slot, c=card, dn=display_name):
                        fi_disp = INTERNAL_TO_DISPLAY.get(fi, fi)
                        if not messagebox.askyesno(
                                "Unload Slot",
                                f"Unload slot {s} ({fi_disp}) from\n"
                                f"'{dn}'?\n\n"
                                "If this is the last slot, the entire mod "
                                "will be deleted."):
                            return
                        gone = remove_single_slot(p, fi, s)
                        if gone and not os.path.exists(p):
                            c.destroy()
                            print(f"  Unloaded {s} ({fi_disp}) — mod '{n}' "
                                  f"is now empty, deleted.")
                        else:
                            print(f"  Unloaded {s} ({fi_disp}) from '{n}'")
                            self._show_installed()
                        skins = list_installed_skins()
                        self.results_label.configure(
                            text=f"{len(skins)} installed skin(s) on SD")

                    def _do_swap_slot(n=name, p=path, fi=fighter_int,
                                      s=slot, dn=display_name, target=""):
                        fi_disp = INTERNAL_TO_DISPLAY.get(fi, fi)
                        if target == s:
                            return
                        # Check if target slot is occupied by ANOTHER mod
                        occupied = get_occupied_slots(fi)
                        other = occupied.get(target)
                        if other and other["mod"] != os.path.basename(p):
                            msg = (f"Slot {target} is occupied by "
                                   f"'{other['name']}'.\n\n"
                                   f"Swap {s} ↔ {target} anyway?\n"
                                   f"(Both mods will swap positions.)")
                            if not messagebox.askyesno("Swap Slots", msg):
                                return
                            # Swap in the OTHER mod too (other direction)
                            other_path = os.path.join(ARCROPOLIS_MODS,
                                                      other["mod"])
                            swap_mod_slot(other_path, fi, target, s)
                            print(f"  Swapped '{other['name']}' "
                                  f"{target} → {s}")

                        ok = swap_mod_slot(p, fi, s, target)
                        if ok:
                            print(f"  Swapped '{dn}' {s} → {target} "
                                  f"({fi_disp})")
                            self._show_installed()
                        else:
                            print(f"  ✗ Swap failed for '{dn}' {s} → "
                                  f"{target}")

                    def _show_slot_menu(event, n=name, p=path,
                                        fi=fighter_int, s=slot,
                                        c=card, dn=display_name):
                        # Build a custom popup window with a grid of slot
                        # buttons (c00-c15) + thumbnail tooltips + unload.
                        popup = tk.Toplevel(self.root)
                        popup.overrideredirect(True)
                        popup.configure(bg=T.CRUST, bd=1, relief="solid")
                        popup.attributes("-topmost", True)

                        def _dismiss(e=None):
                            try:
                                popup.destroy()
                            except tk.TclError:
                                pass

                        popup.bind("<Escape>", _dismiss)
                        # Dismiss when clicking outside
                        popup.bind("<FocusOut>", _dismiss)

                        # Header
                        tk.Label(popup, text=f"Move {s} to…",
                                 bg=T.CRUST, fg=T.ACCENT,
                                 font=(T.FONT, T.SZ_SM, "bold")
                                 ).pack(padx=8, pady=(6, 2))

                        # Slot grid — 2 rows of 8
                        occupied = get_occupied_slots(fi)
                        grid = tk.Frame(popup, bg=T.CRUST)
                        grid.pack(padx=6, pady=2)

                        for i in range(MAX_SLOT):
                            target = f"c{i:02d}"
                            row_idx = i // 8
                            col_idx = i % 8
                            occ = occupied.get(target)
                            is_self = (target == s)

                            if is_self:
                                bg, fg = T.PEACH, T.BG
                            elif occ:
                                bg, fg = T.SURFACE1, T.OVERLAY
                            else:
                                bg, fg = T.GREEN, T.BG

                            sbtn = tk.Label(
                                grid, text=target, width=4,
                                bg=bg, fg=fg,
                                font=(T.MONO, T.SZ_XS, "bold"),
                                cursor="hand2" if not is_self else "",
                                relief="flat", padx=2, pady=2)
                            sbtn.grid(row=row_idx, column=col_idx,
                                      padx=1, pady=1)

                            if is_self:
                                continue  # current slot — no action

                            # Click → swap
                            sbtn.bind(
                                "<Button-1>",
                                lambda e, t=target: (
                                    _dismiss(),
                                    _do_swap_slot(n, p, fi, s, dn, t)))

                            # Hover effects + thumbnail tooltip
                            if occ:
                                friendly = occ["name"]
                                thumb = occ.get("thumb_url")
                                sbtn.bind("<Enter>", lambda e, b=sbtn,
                                          t=friendly, tu=thumb: (
                                    b.configure(bg=T.YELLOW, fg=T.BG),
                                    self._show_tooltip(b, t,
                                                       thumb_url=tu)))
                                sbtn.bind("<Leave>", lambda e, b=sbtn: (
                                    b.configure(bg=T.SURFACE1,
                                                fg=T.OVERLAY),
                                    self._hide_tooltip()))
                            else:
                                sbtn.bind("<Enter>", lambda e, b=sbtn: (
                                    b.configure(bg="#82ddb0"),
                                    self._show_tooltip(b, "Empty")))
                                sbtn.bind("<Leave>", lambda e, b=sbtn: (
                                    b.configure(bg=T.GREEN, fg=T.BG),
                                    self._hide_tooltip()))

                        # Separator + Unload button
                        sep = tk.Frame(popup, bg=T.SURFACE1, height=1)
                        sep.pack(fill="x", padx=6, pady=(4, 2))
                        unload_btn = tk.Label(
                            popup, text=f"🗑  Unload {s}",
                            bg=T.CRUST, fg=T.RED,
                            font=(T.FONT, T.SZ_SM), cursor="hand2",
                            padx=8, pady=4)
                        unload_btn.pack(fill="x", padx=6, pady=(0, 6))
                        unload_btn.bind("<Button-1>", lambda e: (
                            _dismiss(),
                            _do_remove_slot(n, p, fi, s, c, dn)))
                        unload_btn.bind("<Enter>", lambda e, b=unload_btn:
                                        b.configure(bg=T.RED, fg=T.BG))
                        unload_btn.bind("<Leave>", lambda e, b=unload_btn:
                                        b.configure(bg=T.CRUST, fg=T.RED))

                        # Position near the click
                        popup.update_idletasks()
                        pw = popup.winfo_reqwidth()
                        ph = popup.winfo_reqheight()
                        sx = self.root.winfo_screenwidth()
                        sy = self.root.winfo_screenheight()
                        px = min(event.x_root, sx - pw - 4)
                        py = min(event.y_root, sy - ph - 4)
                        popup.geometry(f"+{px}+{py}")
                        popup.focus_set()

                    btn = tk.Button(
                        slot_row, text=slot, width=3,
                        bg=T.PEACH, fg=T.BG, font=(T.MONO, T.SZ_XS, "bold"),
                        relief="flat", cursor="hand2")
                    btn.pack(side="left", padx=1)
                    btn.bind("<Button-3>", _show_slot_menu)
                    btn.bind("<Enter>", lambda e, b=btn: b.configure(
                        bg=T.ACCENT, fg=T.BG))
                    btn.bind("<Leave>", lambda e, b=btn: b.configure(
                        bg=T.PEACH, fg=T.BG))

        # Buttons row
        btn_row = tk.Frame(info, bg=T.BG)
        btn_row.pack(fill="x", pady=(4, 0))

        def _do_uninstall(n=name, c=card, dn=display_name):
            if messagebox.askyesno("Uninstall Skin",
                                   f"Remove entire '{dn}' from SD card?\n"
                                   "This removes ALL slots and cannot be undone."):
                ok = uninstall_skin(n)
                if ok:
                    c.destroy()
                    print(f"  Uninstalled: {n}")
                    skins = list_installed_skins()
                    self.results_label.configure(
                        text=f"{len(skins)} installed skin(s) on SD")
                else:
                    print(f"  Failed to uninstall: {n}", file=sys.stderr)

        tk.Button(btn_row, text="Uninstall All", width=12,
                  bg=T.RED, fg=T.BG, font=(T.FONT, T.SZ_MD, "bold"),
                  relief="flat", cursor="hand2",
                  command=_do_uninstall).pack(side="left", padx=(0, 6))

        tk.Button(btn_row, text="Open Folder", width=12,
                  bg=T.SURFACE1, fg=T.FG, font=(T.FONT, T.SZ_SM),
                  relief="flat", cursor="hand2",
                  command=lambda p=path: os.startfile(p)
                  ).pack(side="left", padx=(0, 6))

        # Open GameBanana page if we have the URL
        gb_url = meta.get("url") if meta else None
        if gb_url:
            tk.Button(btn_row, text="Open Page", width=10,
                      bg=T.SURFACE1, fg=T.FG, font=(T.FONT, T.SZ_SM),
                      relief="flat", cursor="hand2",
                      command=lambda u=gb_url: os.startfile(u)
                      ).pack(side="left", padx=(0, 6))

        # Install — open drag-and-drop install dialog with 3D previews
        tk.Button(btn_row, text="Install", width=11,
                  bg=T.ACCENT, fg=T.BG,
                  font=(T.FONT, T.SZ_SM, "bold"),
                  relief="flat", cursor="hand2",
                  command=lambda p=path, dn=display_name:
                      self._view_model(mod_name=dn, installed_path=p)
                  ).pack(side="left", padx=(0, 6))

    def _create_profile_from_installed_ui(self):
        """Open a rich Create-Profile dialog: name + template + wifi/atmo +
        plugins, optionally snapshotting currently-installed SD mods.

        These same fields are exposed by the Manage Profiles dialog, so
        anything chosen here can be edited again later.
        """
        can_snapshot = os.path.exists(ARCROPOLIS_MODS)
        skins = list_installed_skins() if can_snapshot else []
        snap_count = len(skins)

        win = tk.Toplevel(self.root)
        win.title("Create Profile")
        win.configure(bg=T.SURFACE)
        win.transient(self.root)
        win.grab_set()
        win.geometry("520x540")

        body = tk.Frame(win, bg=T.SURFACE)
        body.pack(fill="both", expand=True, padx=14, pady=12)

        tk.Label(body, text="Create Profile", bg=T.SURFACE, fg=T.ACCENT,
                 font=(T.FONT, T.SZ_LG, "bold")).grid(
                     row=0, column=0, columnspan=2, sticky="w",
                     pady=(0, 8))

        # Default name — suggest a unique one
        existing = load_profiles()
        base = "New Profile"
        default_name = base
        i = 2
        while default_name in existing:
            default_name = f"{base} {i}"
            i += 1

        tk.Label(body, text="Name:", bg=T.SURFACE, fg=T.FG,
                 font=(T.FONT, T.SZ_SM, "bold")).grid(
                     row=1, column=0, sticky="w", pady=(2, 2))
        name_var = tk.StringVar(value=default_name)
        tk.Entry(body, textvariable=name_var,
                 bg=T.CRUST, fg=T.FG, insertbackground=T.FG,
                 font=(T.FONT, T.SZ_MD)).grid(
                     row=1, column=1, sticky="we", pady=(2, 2))

        wifi_var = tk.BooleanVar(value=True)
        tk.Checkbutton(
            body, text="Wifi-Safe (block gameplay-affecting mods)",
            variable=wifi_var, bg=T.SURFACE, fg=T.GREEN,
            selectcolor=T.CRUST, activebackground=T.SURFACE,
            activeforeground=T.GREEN, font=(T.FONT, T.SZ_SM)).grid(
                row=3, column=0, columnspan=2, sticky="w", pady=(6, 2))

        atmo_var = tk.BooleanVar(value=True)
        tk.Checkbutton(
            body,
            text=f"Use unofficial Atmosphere ({ATMOSPHERE_SUPPORT_BRANCH})",
            variable=atmo_var, bg=T.SURFACE, fg=T.PEACH,
            selectcolor=T.CRUST, activebackground=T.SURFACE,
            activeforeground=T.PEACH, font=(T.FONT, T.SZ_SM)).grid(
                row=4, column=0, columnspan=2, sticky="w", pady=(2, 4))

        # Plugin checkboxes — one per known optional plugin, all enabled
        # by default for the typical "tournament Switch" baseline.
        tk.Label(body, text="Plugins:", bg=T.SURFACE, fg=T.FG,
                 font=(T.FONT, T.SZ_SM, "bold")).grid(
                     row=5, column=0, sticky="nw", pady=(8, 2))
        plugins_frame = tk.Frame(body, bg=T.SURFACE)
        plugins_frame.grid(row=5, column=1, sticky="we", pady=(8, 2))
        plugin_vars = {}
        for nro, meta in KNOWN_PLUGINS.items():
            if nro in CORE_PLUGINS:
                continue
            v = tk.BooleanVar(value=True)
            plugin_vars[nro] = v
            row = tk.Frame(plugins_frame, bg=T.SURFACE)
            row.pack(fill="x", anchor="w")
            tk.Checkbutton(
                row, text=meta.get("name", nro), variable=v,
                bg=T.SURFACE, fg=T.FG, selectcolor=T.CRUST,
                activebackground=T.SURFACE, activeforeground=T.FG,
                font=(T.FONT, T.SZ_SM)).pack(side="left")
            desc = meta.get("desc", "")
            if desc:
                tk.Label(row, text=f"— {desc}", bg=T.SURFACE,
                         fg=T.OVERLAY,
                         font=(T.FONT, T.SZ_XS)).pack(side="left",
                                                       padx=(6, 0))

        # Snapshot toggle: default OFF so a freshly-created profile starts
        # empty. The previous default copied whatever was on the SD, which
        # surprised users who expected "new profile" to mean "blank slate".
        snapshot_var = tk.BooleanVar(value=False)
        if snap_count:
            tk.Checkbutton(
                body,
                text=f"Snapshot {snap_count} mod(s) currently installed on SD",
                variable=snapshot_var, bg=T.SURFACE, fg=T.ACCENT,
                selectcolor=T.CRUST, activebackground=T.SURFACE,
                activeforeground=T.ACCENT,
                font=(T.FONT, T.SZ_SM)).grid(
                    row=6, column=0, columnspan=2, sticky="w",
                    pady=(10, 2))
        else:
            tk.Label(body,
                     text="(no SD mods to snapshot — profile starts empty)",
                     bg=T.SURFACE, fg=T.OVERLAY,
                     font=(T.FONT, T.SZ_XS)).grid(
                         row=6, column=0, columnspan=2, sticky="w",
                         pady=(10, 2))

        body.columnconfigure(1, weight=1)

        btn_row = tk.Frame(win, bg=T.SURFACE)
        btn_row.pack(fill="x", padx=14, pady=(0, 12))

        def _do_create():
            name = name_var.get().strip()
            if not name:
                messagebox.showwarning("Name Required",
                                       "Profile name cannot be empty.",
                                       parent=win)
                return
            profiles = load_profiles()
            if name in profiles:
                if not messagebox.askyesno(
                        "Overwrite Profile",
                        f"A profile named '{name}' already exists "
                        f"({len(profiles[name].get('mods', []))} mods).\n\n"
                        "Overwrite it?", parent=win):
                    return

            mods = []
            if snap_count and snapshot_var.get():
                # Snapshot logic — same as create_profile_from_installed
                # but inlined so we can attach our config keys atomically.
                for skin in skins:
                    meta = skin.get("meta") or {}
                    mods.append({
                        "folder_name": skin["name"],
                        "mod_id": meta.get("mod_id"),
                        "name": meta.get("name", skin["name"]),
                        "slot": meta.get("slot"),
                        "character": skin.get("character", "Other"),
                        "mod_type": skin.get("mod_type", "skin"),
                        "thumb_url": meta.get("thumb_url"),
                        "image_urls": meta.get("image_urls", []),
                        "url": meta.get("url", ""),
                        "submitter": meta.get("submitter", ""),
                    })

            template = "Custom"
            profiles[name] = {
                "created": datetime.now().isoformat(),
                "mod_count": len(mods),
                "mods": mods,
                "template": template,
                "wifi_safe": bool(wifi_var.get()),
                "unofficial_atmo": bool(atmo_var.get()),
                "plugins": [nro for nro, v in plugin_vars.items()
                            if v.get()],
            }
            save_profiles(profiles)
            print(f"\n=== Created profile '{name}' "
                  f"({len(mods)} mods) ===\n")
            # Make the just-created profile the active install target
            # so subsequent drag-drops land here instead of the
            # previously-loaded profile.
            self._active_user_profile = name
            self._profile_mode_target = name
            try:
                self._refresh_profile_list_silent()
            except Exception:
                pass
            print(f"  Active install target: '{name}'")
            win.destroy()
            if self._active_view == "profiles":
                self._show_profiles()

        tk.Button(btn_row, text="Create", width=12,
                  bg=T.ACCENT, fg=T.BG,
                  font=(T.FONT, T.SZ_MD, "bold"), relief="flat",
                  cursor="hand2",
                  command=_do_create).pack(side="right", padx=(6, 0))
        tk.Button(btn_row, text="Cancel", width=10,
                  bg=T.SURFACE1, fg=T.FG,
                  font=(T.FONT, T.SZ_MD), relief="flat", cursor="hand2",
                  command=win.destroy).pack(side="right")

    # ── "Add to Profile" picker dialog ──

    def _pick_profile_and_add(self, mod_entry):
        """Show a dialog letting the user pick (or create) a profile to add a mod to."""
        profiles = load_profiles()
        profile_names = sorted(profiles.keys())

        dlg = tk.Toplevel(self.root)
        dlg.title("Add to Profile")
        dlg.configure(bg=T.BG)
        dlg.transient(self.root)
        dlg.grab_set()

        tk.Label(dlg, text=f"Add '{mod_entry.get('name', '?')}' to:", bg=T.BG, fg=T.FG,
                 font=(T.FONT, T.SZ_LG, "bold"), wraplength=400).pack(padx=12, pady=(12, 6))

        # ── Slot picker (for skins only, multi-select) ──
        is_skin = mod_entry.get("mod_type", "skin") == "skin"
        selected_slots = set()
        if is_skin:
            # Pre-populate from existing slot value
            existing = mod_entry.get("slot", "")
            if existing:
                for part in existing.replace(",", " ").split():
                    selected_slots.add(part.strip())

            slot_frame = tk.Frame(dlg, bg=T.BG)
            slot_frame.pack(fill="x", padx=12, pady=(0, 8))
            tk.Label(slot_frame, text="Slots:", bg=T.BG, fg=T.OVERLAY,
                     font=(T.FONT, T.SZ_SM)).pack(side="left", padx=(0, 6))

            slot_btns = {}
            count_lbl = tk.Label(slot_frame, text="8 remaining", bg=T.BG,
                                 fg=T.ACCENT, font=(T.FONT, T.SZ_SM, "bold"))
            count_lbl.pack(side="right", padx=(6, 0))

            def _update_count():
                remaining = 8 - len(selected_slots)
                count_lbl.configure(
                    text=f"{remaining} remaining",
                    fg=T.GREEN if remaining == 0 else T.ACCENT)

            for i in range(8):
                s = f"c{i:02d}"
                def _toggle_slot(slot=s):
                    if slot in selected_slots:
                        selected_slots.discard(slot)
                        slot_btns[slot].configure(bg=T.SURFACE1, fg=T.OVERLAY)
                    else:
                        selected_slots.add(slot)
                        slot_btns[slot].configure(bg=T.GREEN, fg=T.BG)
                    _update_count()
                bg = T.GREEN if s in selected_slots else T.SURFACE1
                fg = T.BG if s in selected_slots else T.OVERLAY
                btn = tk.Button(slot_frame, text=s, width=4,
                                bg=bg, fg=fg,
                                font=(T.MONO, T.SZ_SM, "bold"),
                                relief="flat", cursor="hand2",
                                command=_toggle_slot)
                btn.pack(side="left", padx=1)
                slot_btns[s] = btn

            _update_count()

        def _build_entry():
            """Return mod_entry with chosen slot(s) applied."""
            e = dict(mod_entry)
            if selected_slots:
                e["slot"] = ", ".join(sorted(selected_slots))
            return e

        if profile_names:
            tk.Label(dlg, text="Existing profiles:", bg=T.BG, fg=T.OVERLAY,
                     font=(T.FONT, T.SZ_SM)).pack(anchor="w", padx=12)
            listbox_frame = tk.Frame(dlg, bg=T.SURFACE)
            listbox_frame.pack(fill="both", expand=True, padx=12, pady=(2, 6))
            lb = tk.Listbox(listbox_frame, bg=T.SURFACE, fg=T.FG,
                            font=(T.FONT, T.SZ_MD), selectbackground=T.ACCENT,
                            selectforeground=T.BG, relief="flat",
                            highlightthickness=0)
            lb.pack(fill="both", expand=True)
            for pn in profile_names:
                cnt = profiles[pn].get("mod_count", 0)
                lb.insert("end", f"{pn}  ({cnt} mods)")

            def _add_existing():
                sel = lb.curselection()
                if not sel:
                    return
                chosen = profile_names[sel[0]]
                final = _build_entry()
                count = add_mod_to_profile(chosen, final)
                print(f"  Added '{final.get('name', '?')}' to profile "
                      f"'{chosen}' ({count} mods)")
                dlg.destroy()
                if self._active_view == "profiles":
                    self._show_profiles()

            tk.Button(dlg, text="Add to Selected", width=16,
                      bg=T.GREEN, fg=T.BG, font=(T.FONT, T.SZ_MD, "bold"),
                      relief="flat", cursor="hand2",
                      command=_add_existing).pack(pady=(0, 4))

        # "New Profile" option
        new_frame = tk.Frame(dlg, bg=T.BG)
        new_frame.pack(fill="x", padx=12, pady=(4, 12))
        tk.Label(new_frame, text="Or new profile:", bg=T.BG, fg=T.OVERLAY,
                 font=(T.FONT, T.SZ_SM)).pack(anchor="w")
        name_var = tk.StringVar()
        entry = tk.Entry(new_frame, textvariable=name_var, bg=T.SURFACE, fg=T.FG,
                         font=(T.FONT, T.SZ_MD), insertbackground=T.FG, relief="flat")
        entry.pack(fill="x", pady=(2, 4))

        def _add_new():
            n = name_var.get().strip()
            if not n:
                return
            final = _build_entry()
            count = add_mod_to_profile(n, final)
            print(f"  Added '{final.get('name', '?')}' to new profile "
                  f"'{n}' ({count} mods)")
            dlg.destroy()
            if self._active_view == "profiles":
                self._show_profiles()

        tk.Button(new_frame, text="Create & Add", width=14,
                  bg=T.ACCENT, fg=T.BG, font=(T.FONT, T.SZ_MD, "bold"),
                  relief="flat", cursor="hand2",
                  command=_add_new).pack()

        # Let geometry manager figure out natural size, then enforce minimum
        dlg.update_idletasks()
        w = max(dlg.winfo_reqwidth() + 40, 420)
        h = max(dlg.winfo_reqheight() + 20, 420)
        dlg.geometry(f"{w}x{h}")
        dlg.minsize(420, 420)

    def _show_profiles(self):
        """Populate results pane with saved mod profiles."""
        self._open_profile_name = None
        self.prev_btn.configure(state="disabled")
        self.next_btn.configure(state="disabled")
        self.page_label.configure(text="")

        self._slot_picker_registry.clear()
        self._slot_counter_registry.clear()
        for w in self.results_inner.winfo_children():
            w.destroy()

        # Reset scroll position and region when clearing, to avoid stale state
        self.results_canvas.yview_moveto(0)
        self.results_canvas.configure(scrollregion="0 0 0 0")

        profiles = load_profiles()
        if not isinstance(profiles, dict):
            print("  Warning: profile data is not a dict; resetting view.", file=sys.stderr)
            profiles = {}
        self.results_label.configure(text=f"{len(profiles)} profile(s)")

        action_bar = tk.Frame(self.results_inner, bg=T.SURFACE)
        action_bar.pack(fill="x", padx=4, pady=(6, 4))

        tk.Button(
            action_bar, text="Create Profile", width=14,
            bg=T.ACCENT, fg=T.BG, font=(T.FONT, T.SZ_MD, "bold"),
            relief="flat", cursor="hand2",
            command=self._create_profile_from_installed_ui,
        ).pack(side="left", padx=8, pady=6)

        tk.Button(
            action_bar, text="⚙ Manage Profiles…", width=20,
            bg=T.SURFACE1, fg=T.FG, font=(T.FONT, T.SZ_MD, "bold"),
            relief="flat", cursor="hand2",
            command=self._open_profile_setup_dialog,
        ).pack(side="left", padx=4, pady=6)

        if not profiles:
            tk.Label(self.results_inner,
                     text="No profiles yet.\n\n"
                          "Browse or favorite mods, then click\n"
                          "'Add to Profile' to build a collection.\n\n"
                          "Or go to Installed and click 'Save as Profile'.",
                     bg=T.SURFACE, fg=T.OVERLAY,
                     font=(T.FONT, T.SZ_XL), justify="center").pack(pady=40)
            return

        for pname, pdata in sorted(profiles.items()):
            try:
                self._add_profile_card(pname, pdata)
            except Exception as e:
                print(f"  Error rendering profile '{pname}': {e}", file=sys.stderr)

    def _add_profile_card(self, profile_name, profile_data):
        """Add a summary card for a saved profile."""
        mods = profile_data.get("mods", [])
        created = profile_data.get("created", "")
        mod_count = profile_data.get("mod_count", len(mods))

        card = tk.Frame(self.results_inner, bg=T.BG, padx=10, pady=8)
        card.pack(fill="x", padx=8, pady=4)

        # Profile name
        tk.Label(card, text=profile_name, bg=T.BG, fg=T.FG,
                 font=(T.FONT, T.SZ_H2, "bold"), anchor="w"
                 ).pack(fill="x")

        # Stats — group mods by character for summary
        chars = {}
        for m in mods:
            c = m.get("character", "Other")
            chars[c] = chars.get(c, 0) + 1
        char_summary = ", ".join(f"{c} ({n})" for c, n in
                                 sorted(chars.items(), key=lambda x: -x[1])[:5])
        if len(chars) > 5:
            char_summary += f", +{len(chars) - 5} more"

        date_str = ""
        if created:
            try:
                dt = datetime.fromisoformat(created)
                date_str = dt.strftime("%b %d, %Y %I:%M %p")
            except Exception:
                date_str = created

        info_text = f"{mod_count} mod(s)  |  Created: {date_str}"
        tk.Label(card, text=info_text, bg=T.BG, fg=T.SUBTEXT,
                 font=(T.FONT, T.SZ_SM), anchor="w").pack(fill="x", pady=(2, 0))

        if char_summary:
            tk.Label(card, text=char_summary, bg=T.BG, fg=T.OVERLAY,
                     font=(T.FONT, T.SZ_SM), anchor="w",
                     wraplength=600).pack(fill="x", pady=(2, 0))

        # Thumbnail strip — show first few mod thumbnails
        if mods:
            thumb_strip = tk.Frame(card, bg=T.BG)
            thumb_strip.pack(fill="x", pady=(6, 4))
            shown = 0
            for m in mods:
                if shown >= 8:
                    break
                thumb_url = m.get("thumb_url")
                if not thumb_url:
                    continue
                tf = tk.Frame(thumb_strip, bg=T.SURFACE1, width=90, height=60)
                tf.pack(side="left", padx=(0, 4))
                tf.pack_propagate(False)
                tlbl = tk.Label(tf, bg=T.SURFACE1, text="...",
                                fg=T.OVERLAY, font=(T.FONT, T.SZ_XS))
                tlbl.pack(expand=True)
                threading.Thread(
                    target=self._load_thumb,
                    args=(tlbl, thumb_url, f"prof_{profile_name}_{m.get('mod_id')}"),
                    daemon=True).start()
                shown += 1
            if len(mods) > 8:
                tk.Label(thumb_strip, text=f"+{len(mods) - 8}",
                         bg=T.BG, fg=T.OVERLAY,
                         font=(T.FONT, T.SZ_SM)).pack(side="left", padx=4)

        # Buttons
        btn_row = tk.Frame(card, bg=T.BG)
        btn_row.pack(fill="x", pady=(4, 0))

        tk.Button(
            btn_row, text="Open", width=10,
            bg=T.ACCENT, fg=T.BG, font=(T.FONT, T.SZ_MD, "bold"),
            relief="flat", cursor="hand2",
            command=lambda n=profile_name: self._open_profile(n),
        ).pack(side="left", padx=(0, 6))

        tk.Button(
            btn_row, text="Load to SD", width=14,
            bg=T.GREEN, fg=T.BG, font=(T.FONT, T.SZ_MD, "bold"),
            relief="flat", cursor="hand2",
            command=lambda n=profile_name: self._load_profile(n),
        ).pack(side="left", padx=(0, 6))

        tk.Button(
            btn_row, text="↻ Redownload", width=14,
            bg=T.PEACH, fg=T.BG, font=(T.FONT, T.SZ_SM, "bold"),
            relief="flat", cursor="hand2",
            command=lambda n=profile_name: self._redownload_profile(n),
        ).pack(side="left", padx=(0, 6))

        tk.Button(
            btn_row, text="Duplicate", width=10,
            bg=T.SURFACE1, fg=T.FG, font=(T.FONT, T.SZ_SM, "bold"),
            relief="flat", cursor="hand2",
            command=lambda n=profile_name: self._duplicate_profile(n),
        ).pack(side="left", padx=(0, 6))

        tk.Button(
            btn_row, text="Delete", width=10,
            bg=T.RED, fg=T.BG, font=(T.FONT, T.SZ_SM, "bold"),
            relief="flat", cursor="hand2",
            command=lambda n=profile_name, c=card: self._delete_profile(n, c),
        ).pack(side="left", padx=(0, 6))

    def _redownload_profile(self, profile_name):
        """Wipe the cache for every mod in the named profile and
        download fresh archives. Useful when you suspect a mod's
        cached download is corrupted or the author has updated their
        archive on GameBanana since you first cached it.

        Doesn't touch the SD card. After this, the next Load to SD
        will reinstall every mod from the freshly-downloaded
        archives.
        """
        profiles = load_profiles()
        profile = profiles.get(profile_name)
        if not profile:
            messagebox.showerror("Redownload",
                                  f"Profile '{profile_name}' not found.")
            return
        mods = [m for m in profile.get("mods", [])
                if m.get("mod_id") and m.get("enabled", True)]
        if not mods:
            messagebox.showinfo("Redownload",
                                 f"Profile '{profile_name}' has no "
                                 "downloadable mods.")
            return

        prog = self._show_install_prepare_dialog(
            f"Redownloading '{profile_name}'…")

        def _worker():
            print(f"\n=== Redownloading profile '{profile_name}' "
                  f"({len(mods)} mods) ===\n")
            ok = failed = 0
            for i, m in enumerate(mods, 1):
                mid = m["mod_id"]
                name = m.get("name") or f"Mod {mid}"
                prog.set_phase(f"[{i}/{len(mods)}] {name}…")
                cache_dir = os.path.join(MOD_CACHE_DIR, str(mid))
                # Wipe the entire cache for this mod_id so the
                # download path doesn't return the old cached archive.
                if os.path.isdir(cache_dir):
                    try:
                        shutil.rmtree(cache_dir, ignore_errors=True)
                    except Exception as e:
                        print(f"  ! cache wipe failed for {name}: "
                              f"{e}", file=sys.stderr)
                # Download fresh + extract.
                try:
                    archive = self._download_mod_archive(mid, name)
                    if not archive:
                        print(f"  ✗ {name}: no downloadable file",
                              file=sys.stderr)
                        failed += 1
                        continue
                    extracted = os.path.join(cache_dir, "extracted")
                    os.makedirs(extracted, exist_ok=True)
                    extract_archive(archive, extracted)
                    print(f"  ✓ {name}")
                    ok += 1
                except Exception as e:
                    print(f"  ✗ {name}: {e}", file=sys.stderr)
                    failed += 1
            self.root.after(0, prog.close)
            self.root.after(0, lambda: messagebox.showinfo(
                "Redownload",
                f"Profile '{profile_name}' redownload complete.\n\n"
                f"  ✓ {ok} fresh\n"
                f"  ✗ {failed} failed\n\n"
                "Next Load to SD will reinstall everything from the "
                "fresh cache."))
            print(f"\n=== Redownload done: {ok} ok, "
                  f"{failed} failed ===\n")

        threading.Thread(target=_worker, daemon=True).start()

    def _cache_active_profile(self):
        """Cache the profile currently selected in the top bar dropdown.
        Convenience wrapper around :meth:`_cache_profile` for the
        '⬇ Cache Now' button."""
        target = (self._profile_mode_target
                  or self._active_user_profile
                  or self._profile_list_var.get())
        if not target:
            messagebox.showinfo(
                "Cache Now",
                "No active profile selected. Pick one from the Active "
                "Profile dropdown first, or use Profiles → Cache "
                "Profile on a specific profile card.")
            return
        self._cache_profile(target)

    def _cache_profile(self, profile_name):
        """Pre-download and extract every mod in a profile so a later
        ``Load to SD`` runs offline (no network round-trips during
        install). Useful when the user wants to set up the app at
        home, then drive to a tournament where the LAN may be flaky.

        Already-cached mods are skipped — this is incremental and
        safe to re-run any time. Cache lives under ``MOD_CACHE_DIR``
        (``%LOCALAPPDATA%/smash_night/mod_cache`` by default).
        """
        profiles = load_profiles()
        profile = profiles.get(profile_name)
        if not profile:
            messagebox.showerror("Cache Profile",
                                  f"Profile '{profile_name}' not found.")
            return
        mods = [m for m in profile.get("mods", [])
                if m.get("mod_id") and m.get("enabled", True)]
        if not mods:
            messagebox.showinfo("Cache Profile",
                                 f"Profile '{profile_name}' has no "
                                 "downloadable mods to cache.")
            return

        # Quick budget check — figure out how many mods are already
        # cached vs. how many we'd need to download.
        cached = 0
        for m in mods:
            extracted = os.path.join(MOD_CACHE_DIR,
                                      str(m["mod_id"]), "extracted")
            if os.path.isdir(extracted) and os.listdir(extracted):
                cached += 1
        missing = len(mods) - cached
        if missing == 0:
            messagebox.showinfo(
                "Cache Profile",
                f"All {len(mods)} mod(s) in '{profile_name}' are "
                "already cached.\n\n"
                f"Cache location:\n{MOD_CACHE_DIR}")
            return
        if not messagebox.askyesno(
                "Cache Profile",
                f"Pre-download {missing} mod(s) for profile "
                f"'{profile_name}' so 'Load to SD' runs offline?\n\n"
                f"  • Already cached: {cached}\n"
                f"  • Will download:  {missing}\n\n"
                f"Cache location:\n{MOD_CACHE_DIR}"):
            return

        prog = self._show_install_prepare_dialog(
            f"Caching '{profile_name}'…")

        def _worker():
            ok = 0
            failed = 0
            try:
                for i, m in enumerate(mods, 1):
                    if not prog or getattr(prog, "_smash_aborted", False):
                        # If user closed the dialog, stop gracefully.
                        break
                    mid = m["mod_id"]
                    name = m.get("name") or f"Mod {mid}"
                    extracted = os.path.join(
                        MOD_CACHE_DIR, str(mid), "extracted")
                    if os.path.isdir(extracted) and os.listdir(extracted):
                        prog.set_phase(
                            f"[{i}/{len(mods)}] {name} (cached)")
                        ok += 1
                        continue
                    prog.set_phase(
                        f"[{i}/{len(mods)}] {name} — downloading…")
                    try:
                        archive = self._download_mod_archive(mid, name)
                        if not archive:
                            failed += 1
                            print(f"  [cache] no downloadable file "
                                  f"for {name}", file=sys.stderr)
                            continue
                        prog.set_phase(
                            f"[{i}/{len(mods)}] {name} — extracting…")
                        os.makedirs(extracted, exist_ok=True)
                        extract_archive(archive, extracted)
                        self._cleanup_archive(archive)
                        ok += 1
                    except Exception as e:
                        failed += 1
                        print(f"  [cache] {name} failed: {e}",
                              file=sys.stderr)
            finally:
                self.root.after(0, prog.close)
                self.root.after(0, lambda: messagebox.showinfo(
                    "Cache Profile",
                    f"Profile '{profile_name}' cached.\n\n"
                    f"  ✓ Ready: {ok}\n"
                    f"  ✗ Failed: {failed}\n\n"
                    f"Location:\n{MOD_CACHE_DIR}"))

        threading.Thread(target=_worker, daemon=True).start()

    def _rename_profile(self, old_name):
        """Prompt for a new name and rename the profile."""
        new_name = simpledialog.askstring(
            "Rename Profile", f"New name for '{old_name}':",
            parent=self.root)
        if not new_name or not new_name.strip() or new_name.strip() == old_name:
            return
        new_name = new_name.strip()
        profiles = load_profiles()
        if new_name in profiles:
            messagebox.showwarning("Name Taken",
                                   f"A profile named '{new_name}' already exists.")
            return
        if rename_profile(old_name, new_name):
            print(f"  Renamed profile '{old_name}' -> '{new_name}'")
            self._show_profiles()
        else:
            messagebox.showerror("Error", "Could not rename profile.")

    def _duplicate_profile(self, old_name):
        """Prompt for a name and deep-copy the profile under that name.
        Empty input falls through to an auto-generated ``<old> (Copy)``
        variant so the user can just hit Enter to accept the default."""
        profiles = load_profiles()
        if old_name not in profiles:
            messagebox.showerror("Duplicate",
                                 f"Profile '{old_name}' not found.")
            return
        suggested = _unique_profile_name(profiles, f"{old_name} (Copy)")
        new_name = simpledialog.askstring(
            "Duplicate Profile",
            f"Name for the copy of '{old_name}':",
            initialvalue=suggested, parent=self.root)
        if new_name is None:
            return  # user hit Cancel
        new_name = (new_name or "").strip()
        if new_name and new_name in profiles:
            messagebox.showwarning(
                "Name Taken",
                f"A profile named '{new_name}' already exists.")
            return
        created = duplicate_profile(old_name, new_name or None)
        if not created:
            messagebox.showerror("Error", "Could not duplicate profile.")
            return
        print(f"  Duplicated profile '{old_name}' -> '{created}'")
        self._show_profiles()

    def _open_profile(self, profile_name):
        """Open a profile detail view showing each mod with thumbnails and remove buttons."""
        # Remember which profile is open so background refreshes (e.g.
        # _refresh_current_view fired by an install) re-render this
        # profile's detail view instead of bouncing the user back to
        # the profile list.
        self._open_profile_name = profile_name
        # Auto-slotting OFF. Slots are user-authoritative — entries
        # without a slot stay slot-less until the user assigns one
        # via drag-drop. Don't silently move things.

        profiles = load_profiles()
        profile = profiles.get(profile_name)
        if not profile:
            return

        self.prev_btn.configure(state="disabled")
        self.next_btn.configure(state="disabled")
        self.page_label.configure(text="")

        self._slot_picker_registry.clear()
        self._slot_counter_registry.clear()
        for w in self.results_inner.winfo_children():
            w.destroy()

        mods = profile.get("mods", [])
        enabled_count = sum(1 for m in mods if m.get("enabled", True))
        disabled_count = len(mods) - enabled_count
        if disabled_count:
            self.results_label.configure(
                text=f"Profile: {profile_name}  —  "
                     f"{enabled_count} enabled, "
                     f"{disabled_count} disabled "
                     f"({len(mods)} total)")
        else:
            self.results_label.configure(
                text=f"Profile: {profile_name}  —  {len(mods)} mod(s)")

        # Back button + action bar
        action_bar = tk.Frame(self.results_inner, bg=T.SURFACE)
        action_bar.pack(fill="x", padx=4, pady=(6, 2))

        tk.Button(
            action_bar, text="← Back to Profiles", width=18,
            bg=T.SURFACE1, fg=T.FG, font=(T.FONT, T.SZ_MD, "bold"),
            relief="flat", cursor="hand2",
            command=self._show_profiles,
        ).pack(side="left", padx=8, pady=6)

        tk.Button(
            action_bar, text="Load to SD", width=14,
            bg=T.GREEN, fg=T.BG, font=(T.FONT, T.SZ_MD, "bold"),
            relief="flat", cursor="hand2",
            command=lambda n=profile_name: self._load_profile(n),
        ).pack(side="left", padx=(0, 6), pady=6)

        tk.Button(
            action_bar, text="Validate", width=10,
            bg=T.SURFACE1, fg=T.FG, font=(T.FONT, T.SZ_MD, "bold"),
            relief="flat", cursor="hand2",
            command=lambda n=profile_name: self._validate_profile(n),
        ).pack(side="left", padx=(0, 6), pady=6)

        if not mods:
            tk.Label(self.results_inner,
                     text="This profile is empty.\n\n"
                          "Browse mods and click 'Add to Profile'\n"
                          "to add them here.",
                     bg=T.SURFACE, fg=T.OVERLAY,
                     font=(T.FONT, T.SZ_XL), justify="center").pack(pady=40)
            return

        # Group by character
        groups = {}
        for m in mods:
            char = m.get("character", "Other")
            groups.setdefault(char, []).append(m)

        sorted_chars = sorted(groups.keys(), key=lambda c: (c == "Other", c))

        for char in sorted_chars:
            items = groups[char]
            enabled_in_group = sum(1 for m in items if m.get("enabled", True))
            disabled_in_group = len(items) - enabled_in_group
            hdr = tk.Frame(self.results_inner, bg=T.SURFACE1)
            hdr.pack(fill="x", padx=4, pady=(10, 2))
            label_txt = f"  {char}  ({len(items)})"
            if disabled_in_group:
                label_txt += f"  — {disabled_in_group} disabled"
            tk.Label(hdr, text=label_txt,
                     bg=T.SURFACE1, fg=T.ACCENT,
                     font=(T.FONT, T.SZ_LG, "bold"), anchor="w"
                     ).pack(side="left", fill="x", expand=True, padx=6, pady=3)

            # Per-character bulk toggle. The most common bisecting
            # workflow on the Switch is "disable every skin for fighter
            # X, see if game stops freezing, then re-enable one at a
            # time" — so put the button right next to the header.
            if enabled_in_group:
                bulk_label = "Disable all"
                bulk_target = False
                bulk_bg = T.SURFACE
            else:
                bulk_label = "Enable all"
                bulk_target = True
                bulk_bg = T.GREEN
            tk.Button(
                hdr, text=bulk_label, width=12,
                bg=bulk_bg, fg=T.FG if bulk_target is False else T.BG,
                font=(T.FONT, T.SZ_SM, "bold"),
                relief="flat", cursor="hand2",
                command=lambda pn=profile_name, c=char,
                        target=bulk_target:
                    self._bulk_set_character_enabled(pn, c, target)
            ).pack(side="right", padx=6, pady=3)

            for mod in items:
                self._add_profile_mod_card(profile_name, mod)

    def _autofix_profile_slot_collisions(self, profile_name):
        """Reassign duplicate same-character slot assignments to free
        slots within the profile. Keeps the alphabetically-first mod
        on its current slot, bumps the rest to the next unused
        ``cNN`` for that character (searching c00 → c15).

        Also normalises malformed multi-slot strings like
        ``"c00, c03"`` → keeps the first token, bumps or drops extra
        tokens that collide.

        Returns ``(changed_mods, untouched_collisions)`` where
        ``untouched_collisions`` lists groups we couldn't fix because
        every slot c00..c15 was taken by enabled mods of that fighter.
        """
        profiles = load_profiles()
        profile = profiles.get(profile_name)
        if not profile:
            return [], []

        mods = profile.get("mods", [])

        changed = []

        # ── Phase 1: normalise multi-slot strings ──
        # "c00, c03" → "c00" (first token only). The mod was always
        # installed to the first slot by _repair_multislot_artifacts;
        # the profile entry just carried the original metadata string.
        for m in mods:
            raw = (m.get("slot") or "").strip()
            tokens = re.findall(r'c\d{2}', raw, re.I)
            if len(tokens) > 1:
                old = m.get("slot")
                m["slot"] = tokens[0].lower()
                changed.append({
                    "character": m.get("character", "?"),
                    "name": m.get("name", "?"),
                    "from": old,
                    "to": tokens[0].lower(),
                })

        # ── Phase 2: fix same-character same-slot collisions ──
        groups = {}
        for m in mods:
            if not m.get("enabled", True):
                continue
            char = m.get("character") or "Other"
            if char == "Other":
                continue
            slot = (m.get("slot") or "").lower().strip()
            tokens = re.findall(r'c\d{2}', slot, re.I)
            if not tokens:
                continue
            slot = tokens[0].lower()
            groups.setdefault((char, slot), []).append(m)

        # Per-character occupancy snapshot from the profile itself.
        occupied_by_char = {}
        for m in mods:
            if not m.get("enabled", True):
                continue
            char = m.get("character") or "Other"
            slot = (m.get("slot") or "").lower().strip()
            tokens = re.findall(r'c\d{2}', slot, re.I)
            if tokens:
                occupied_by_char.setdefault(char, set()).add(
                    tokens[0].lower())

        changed = []
        unfixable = []
        for (char, slot), group in sorted(groups.items()):
            if len(group) <= 1:
                continue
            # Keep the first (alphabetical by name) on the current
            # slot, reassign the rest.
            group_sorted = sorted(group, key=lambda x: x.get("name", ""))
            for dup in group_sorted[1:]:
                taken = occupied_by_char.setdefault(char, set())
                new_slot = None
                for i in range(MAX_SLOT):
                    cand = f"c{i:02d}"
                    if cand not in taken:
                        new_slot = cand
                        break
                if not new_slot:
                    unfixable.append((char, slot,
                                      dup.get("name", "?")))
                    continue
                old_slot = dup.get("slot")
                dup["slot"] = new_slot
                taken.add(new_slot)
                changed.append({
                    "character": char,
                    "name": dup.get("name", "?"),
                    "from": old_slot,
                    "to": new_slot,
                })

        # ── Phase 3: fix cross-character collisions ──
        # Use the cache's fighter identity with the profile's slot
        # to detect mods mis-categorised under a different character
        # but targeting the same fighter on disk.
        real_groups = {}
        for m in mods:
            if not m.get("enabled", True):
                continue
            char = m.get("character") or "Other"
            if char == "Other":
                continue
            slot = (m.get("slot") or "").lower().strip()
            tokens = re.findall(r'c\d{2}', slot, re.I)
            if not tokens:
                continue
            mid = m.get("mod_id")
            cached = get_cached_touched(mid) if mid else []
            if cached:
                fighters = sorted({f.lower() for f, _s in cached})
            else:
                fighters = [FIGHTER_INTERNAL.get(char, char.lower())]
            for fighter in fighters:
                for s in tokens:
                    real_groups.setdefault(
                        (fighter, s.lower()), []).append(m)

        for (fighter, slot), group in sorted(real_groups.items()):
            if len(group) <= 1:
                continue
            labels = {m.get("character", "?") for m in group}
            if len(labels) <= 1:
                continue  # same-label already handled in phase 2
            # Keep mods whose character maps to this fighter (they're
            # correctly categorised). Reslot the mis-categorised ones.
            disp = INTERNAL_TO_DISPLAY.get(fighter, fighter)
            correct = [m for m in group
                       if m.get("character") == disp]
            wrong = [m for m in group
                     if m.get("character") != disp]
            if not correct:
                wrong = sorted(group, key=lambda x: x.get("name", ""))
                correct = [wrong.pop(0)]
            for dup in wrong:
                char = dup.get("character") or "Other"
                taken = occupied_by_char.setdefault(char, set())
                new_slot = None
                for i in range(MAX_SLOT):
                    cand = f"c{i:02d}"
                    if cand not in taken:
                        new_slot = cand
                        break
                if not new_slot:
                    unfixable.append((char, slot,
                                      dup.get("name", "?")))
                    continue
                old_slot = dup.get("slot")
                dup["slot"] = new_slot
                taken.add(new_slot)
                changed.append({
                    "character": char,
                    "name": dup.get("name", "?"),
                    "from": old_slot,
                    "to": new_slot,
                })

        if changed:
            save_profiles(profiles)
        return changed, unfixable

    def _deep_verify_multimodel_mods(self, profile_name, fighter_internal):
        """Download (if needed), extract, and inspect each enabled mod
        whose character maps to ``fighter_internal`` (a multi-model
        fighter like ``packun``). For every slot the mod claims, we
        check whether the archive ships every required model tree
        from :data:`MULTI_MODEL_FIGHTERS`.

        Returns a list of dicts:
        ``{"mod_id", "name", "slot", "missing_trees", "present_trees"}``
        — one per (mod, slot) that has body content but is missing
        one or more companion trees. Those are the freeze culprits.

        Archives are cached under ``SCRIPT_DIR/.mod_cache/<mod_id>/``
        so re-running is cheap.
        """
        required = MULTI_MODEL_FIGHTERS.get(fighter_internal, [])
        if not required:
            return []

        profiles = load_profiles()
        profile = profiles.get(profile_name) or {}
        disp = INTERNAL_TO_DISPLAY.get(fighter_internal, fighter_internal)
        candidates = [
            m for m in profile.get("mods", [])
            if m.get("enabled", True)
            and (m.get("character") or "").lower() == disp.lower()
            and m.get("mod_id")
        ]
        if not candidates:
            return []

        cache_root = MOD_CACHE_DIR
        os.makedirs(cache_root, exist_ok=True)

        culprits = []
        for m in candidates:
            mid = m.get("mod_id")
            name = m.get("name", f"#{mid}")
            mod_cache = os.path.join(cache_root, str(mid))
            extracted = os.path.join(mod_cache, "extracted")

            print(f"  Verifying {name} (id={mid})…")
            try:
                if not os.path.isdir(extracted) or not os.listdir(extracted):
                    os.makedirs(extracted, exist_ok=True)
                    archive = self._download_mod_archive(mid, name)
                    if not archive:
                        print(f"    skip (no downloadable file)")
                        continue
                    extract_archive(archive, extracted)
                    self._cleanup_archive(archive)

                content = find_mod_content(extracted) or extracted
                model_root = os.path.join(
                    content, "fighter", fighter_internal, "model")
                # Build {slot: set(trees_present)}
                slot_trees = {}
                if os.path.isdir(model_root):
                    for tree in os.listdir(model_root):
                        tree_dir = os.path.join(model_root, tree)
                        if not os.path.isdir(tree_dir):
                            continue
                        for slot in os.listdir(tree_dir):
                            if not re.fullmatch(r"c\d{2}", slot, re.I):
                                continue
                            sd_path = os.path.join(tree_dir, slot)
                            # Only count slots with actual files.
                            has_files = any(
                                fs for _r, _ds, fs in os.walk(sd_path) if fs)
                            if has_files:
                                slot_trees.setdefault(
                                    slot.lower(), set()).add(tree.lower())

                for slot, present in slot_trees.items():
                    if "body" not in present:
                        continue  # nothing to crash on without body
                    missing = [t for t in required if t not in present]
                    if missing:
                        culprits.append({
                            "mod_id": mid,
                            "name": name,
                            "slot": slot,
                            "missing_trees": missing,
                            "present_trees": sorted(present),
                        })
                        print(f"    ✗ {slot}: missing {', '.join(missing)}")
                    else:
                        print(f"    ✓ {slot}: complete")
            except Exception as e:
                print(f"    ! verify failed: {e}")

        return culprits

    def _diagnose_profile_freeze_risks(self, profile_name):
        """Static analysis of a saved profile's mod list — *no SD card
        access*. Returns a list of risk dicts in the same shape as
        :func:`diagnose_freeze_risks` so the popup can render both
        sources uniformly.

        We can only see what the profile knows: ``character``,
        ``slot``, ``mod_type``, ``enabled``. Concrete patterns:

          • Two or more enabled skin/UI mods on the same fighter+slot.
            ARCropolis is last-load-wins for these — usually playable
            but the result is a coin-flip, and partial overlaps
            (one mod with body, one with portrait) freeze on pick.
          • A multi-model fighter (Plant, Pokémon Trainer) has
            enabled skins. We flag this as a *warning* with the mod
            list so the user can eyeball whether each entry is a
            full-coverage release. Only the SD-level scan can prove
            completeness, but flagging is still useful.
        """
        profiles = load_profiles()
        profile = profiles.get(profile_name) or {}
        mods = [m for m in profile.get("mods", []) if m.get("enabled", True)]

        risks = []

        # ── Within-profile slot collisions ──
        # Group enabled mods by (character, slot) and flag any group
        # bigger than one when both contributors look like they touch
        # the same fighter slot.
        #
        # Slot strings can be malformed multi-slot like "c00, c03"
        # (comes from mod metadata or GameBanana descriptions). We
        # parse those into individual cXX tokens so they're checked
        # against every slot they claim.
        slot_groups = {}
        malformed_slots = []   # mods with comma-separated slot strings
        for m in mods:
            char = m.get("character") or "Other"
            raw_slot = (m.get("slot") or "").strip()
            mtype = (m.get("mod_type") or "").lower()
            # Only fighter-slot–scoped types collide by slot.
            if mtype and mtype not in (
                    "skin", "ui", "alt costume", "alt", "model",
                    "effect", "moveset", "voice"):
                continue
            if char == "Other":
                continue
            # Parse individual cXX tokens from the slot string.
            tokens = re.findall(r'c\d{2}', raw_slot, re.I)
            if not tokens:
                continue
            # Flag malformed multi-slot strings for auto-repair.
            if len(tokens) > 1:
                malformed_slots.append(m)
            for slot in tokens:
                slot = slot.lower()
                slot_groups.setdefault((char, slot), []).append(m)

        # ── Malformed multi-slot string warnings ──
        # These are profile entries like slot="c00, c03" which cause
        # collisions with every slot they list. Flag as freeze risk.
        for m in malformed_slots:
            char = m.get("character") or "Other"
            internal = FIGHTER_INTERNAL.get(char, char.lower())
            name = m.get("name", f"#{m.get('mod_id', '?')}")
            raw = m.get("slot", "?")
            tokens = re.findall(r'c\d{2}', raw, re.I)
            risks.append({
                "severity": "freeze",
                "fighter": internal,
                "slot": raw,
                "issue": (f"multi-slot string '{raw}' — "
                          f"claims {len(tokens)} slots"),
                "detail": (
                    f"{char}: '{name}' has slot '{raw}' which "
                    f"means it's assigned to {', '.join(tokens)} "
                    f"simultaneously. This collides with any other "
                    f"mod on those slots and is the #1 cause of "
                    f"character-pick freezes."),
                "mods": [name],
            })

        for (char, slot), group in sorted(slot_groups.items()):
            if len(group) <= 1:
                continue
            internal = FIGHTER_INTERNAL.get(char, char.lower())
            names = [m.get("name", f"#{m.get('mod_id', '?')}")
                     for m in group]
            risks.append({
                "severity": "warning",
                "fighter": internal,
                "slot": slot,
                "issue": f"{len(group)} mods assigned to same slot",
                "detail": (
                    f"{char} {slot} has {len(group)} enabled mods. "
                    f"ARCropolis loads them in alphabetical order so "
                    f"only the last one wins. If their contents "
                    f"overlap partially (one ships body, another "
                    f"only portrait) it can freeze on character pick."),
                "mods": names,
            })

        # ── Cross-character collision detection ──
        # A mod categorised as "Ridley" but actually targeting
        # fighter/packun/ on disk will collide with Piranha Plant
        # mods on the same cXX slot. The touched-slots cache
        # (populated at install time) tells us the *real*
        # fighter_internal. We pair the cache's fighter identity with
        # the PROFILE's slot assignment (not the cache's slot, which
        # reflects post-reslot state from the last install).
        real_groups = {}
        for m in mods:
            char = m.get("character") or "Other"
            raw_slot = (m.get("slot") or "").strip()
            mtype = (m.get("mod_type") or "").lower()
            if mtype and mtype not in (
                    "skin", "ui", "alt costume", "alt", "model",
                    "effect", "moveset", "voice"):
                continue
            if char == "Other":
                continue
            tokens = re.findall(r'c\d{2}', raw_slot, re.I)
            if not tokens:
                continue
            # Determine the fighter identity this mod targets on disk.
            mid = m.get("mod_id")
            cached = get_cached_touched(mid) if mid else []
            if cached:
                # Use the cache's fighter(s). Pair with the profile's
                # slot tokens (the install pipeline will use the
                # profile slot, not the cache's old reslotted slot).
                fighters = sorted({f.lower() for f, _s in cached})
            else:
                fighters = [FIGHTER_INTERNAL.get(char, char.lower())]
            for fighter in fighters:
                for slot in tokens:
                    real_groups.setdefault(
                        (fighter, slot.lower()), []).append(m)

        for (fighter, slot), group in sorted(real_groups.items()):
            if len(group) <= 1:
                continue
            # Only flag if the group contains mods with different
            # character labels — same-label collisions are already
            # caught above in same-character checks.
            labels = {m.get("character", "?") for m in group}
            if len(labels) <= 1:
                continue
            disp = INTERNAL_TO_DISPLAY.get(fighter, fighter)
            names = [f"{m.get('name','?')} [{m.get('character','?')}]"
                     for m in group]
            risks.append({
                "severity": "freeze",
                "fighter": fighter,
                "slot": slot,
                "issue": (f"cross-character collision on "
                          f"{disp} {slot}"),
                "detail": (
                    f"These mods are categorised under different "
                    f"characters ({', '.join(sorted(labels))}) but "
                    f"all target {disp} {slot} on disk. They "
                    f"will overwrite each other and likely freeze "
                    f"on character pick."),
                "mods": names,
            })

        # ── Multi-model fighter advisory ──
        # We can't see file contents from the profile, so we can't
        # *prove* a Plant skin is incomplete. But we can list every
        # enabled Plant/Trainer mod so the user can sanity-check
        # them, and bisect via the per-character toggle.
        for internal, required in MULTI_MODEL_FIGHTERS.items():
            disp = INTERNAL_TO_DISPLAY.get(internal, internal)
            relevant = [m for m in mods
                        if (m.get("character") or "").lower() == disp.lower()]
            if not relevant:
                continue
            slots = sorted({(m.get("slot") or "").lower()
                            for m in relevant
                            if m.get("slot")})
            names = [m.get("name", f"#{m.get('mod_id', '?')}")
                     for m in relevant]
            risks.append({
                "severity": "warning",
                "fighter": internal,
                "slot": ", ".join(slots) or "?",
                "issue": (f"{disp} is a multi-model fighter "
                          f"({len(relevant)} mod(s) enabled)"),
                "detail": (
                    f"{disp} requires every modded slot to ship "
                    f"all of: model/{', model/'.join(required)}. "
                    f"Profiles can't verify file contents — load "
                    f"to SD and re-Validate, or bisect with the "
                    f"per-character Disable-all button if {disp} "
                    f"crashes the game."),
                "mods": names,
            })

        risks.sort(key=lambda d: (
            0 if d["severity"] == "freeze" else 1,
            d["fighter"], d["slot"]))
        return risks

    def _validate_profile(self, profile_name, pre_load=False):
        """Validate a profile end-to-end:

          1. **Profile-only checks** — slot collisions, multi-model
             fighter advisories. No filesystem access required.
          2. **SD-card checks** — if the SD is plugged in *and* its
             mods folder is non-empty, additionally run the file
             simulator against the live install for full coverage.

        If profile-level slot collisions are present we offer to
        auto-reslot the duplicates to free slots before exiting.
        Combined report goes to a popup + the console.

        When ``pre_load=True`` (called from ``_load_profile``):
          • Auto-fixes slot collisions silently (no prompt).
          • Still prompts for the deep-verify download since that
            requires user consent + a network round-trip.
          • Skips the final "Validation passed" popup when clean.
          • Returns ``True`` if it's safe to proceed with the load,
            or ``False`` if the user backed out at a freeze prompt.
        """
        profile_risks = self._diagnose_profile_freeze_risks(profile_name)

        # If we found slot collisions (same-character OR cross-character),
        # offer to fix them in-place before showing the report.
        collisions = [r for r in profile_risks
                      if "same slot" in r.get("issue", "")
                      or "cross-character" in r.get("issue", "")
                      or "multi-slot" in r.get("issue", "")]
        if collisions:
            freeze_count = sum(1 for r in collisions
                               if r["severity"] == "freeze")
            preview = "\n".join(
                f"  {'✗' if r['severity']=='freeze' else '•'} "
                f"{INTERNAL_TO_DISPLAY.get(r['fighter'], r['fighter'])}"
                f" {r['slot']}: {', '.join(r['mods'])}"
                for r in collisions[:8])
            extra = (f"\n  …and {len(collisions) - 8} more"
                     if len(collisions) > 8 else "")
            # Auto-fix is OFF. The user's slot assignments are
            # explicit and authoritative — we don't move them. Just
            # report the collisions so they show up in the log.
            print(f"\n  ⚠ {len(collisions)} slot collision(s) "
                  f"detected in '{profile_name}' — leaving as-is "
                  "(slots are user-authoritative).")
            for r in collisions[:8]:
                disp = INTERNAL_TO_DISPLAY.get(
                    r["fighter"], r["fighter"])
                print(f"    • {disp} {r['slot']}: "
                      f"{', '.join(r['mods'])}")
            if False:  # disabled auto-fix branch kept for diff clarity
                changed, unfixable = \
                    self._autofix_profile_slot_collisions(profile_name)
                # Re-run the diagnosis post-fix so the report reflects
                # the updated profile state, and refresh the UI if the
                # user is currently looking at this profile.
                profile_risks = self._diagnose_profile_freeze_risks(
                    profile_name)
                if hasattr(self, "_active_profile_view") \
                        and self._active_profile_view == profile_name:
                    self._open_profile(profile_name)

        # Multi-model fighter advisories: offer to deep-verify by
        # downloading & inspecting each Plant/Trainer mod's archive.
        # This is the *only* way to actually prove a Plant skin is
        # missing companion model trees without plugging in the SD.
        mm_warnings = [r for r in profile_risks
                       if r.get("fighter") in MULTI_MODEL_FIGHTERS
                       and "multi-model" in r.get("issue", "")]
        for r in mm_warnings:
            fighter_internal = r["fighter"]
            disp = INTERNAL_TO_DISPLAY.get(fighter_internal,
                                           fighter_internal)
            mod_count = len(r.get("mods", []))
            if not messagebox.askyesno(
                f"Deep-verify {disp}?",
                f"{disp} has {mod_count} enabled mod(s) and is a "
                "multi-model fighter — picking the character freezes "
                "if any slot is missing companion model trees "
                f"({', '.join(MULTI_MODEL_FIGHTERS[fighter_internal])}).\n\n"
                f"Download and inspect each {disp} mod's archive to "
                "find which one is broken?\n\n"
                "(Archives cache under .mod_cache so this is one-time.)"):
                continue
            print(f"\n=== Deep verify {disp} for '{profile_name}' ===")
            self._show_progress(f"Verifying {disp} mods…")
            try:
                culprits = self._deep_verify_multimodel_mods(
                    profile_name, fighter_internal)
            finally:
                self._hide_progress()
            print("=== End deep verify ===\n")

            if not culprits:
                messagebox.showinfo(
                    f"{disp} verified",
                    f"All enabled {disp} mods ship every required "
                    "model tree on every slot they cover. "
                    f"If {disp} still freezes, the cause is "
                    "elsewhere (motion files, sound, mod overlap on "
                    "the SD).")
                continue

            # Cull duplicates by mod_id for the disable prompt.
            culprit_ids = {}
            for c in culprits:
                culprit_ids.setdefault(c["mod_id"], c)

            preview = "\n".join(
                f"  ✗ {c['name']} {c['slot']} — missing: "
                f"{', '.join(c['missing_trees'])}"
                for c in culprits[:8])
            extra = (f"\n  …and {len(culprits) - 8} more issue(s)"
                     if len(culprits) > 8 else "")
            if messagebox.askyesno(
                f"⚠ Broken {disp} mods found",
                f"{len(culprit_ids)} mod(s) ship body without all "
                f"required {disp} companion trees:\n\n"
                f"{preview}{extra}\n\n"
                "Disable these in the profile? "
                "(They'll stay in the profile but skip on Load to SD.)"):
                profiles_now = load_profiles()
                p = profiles_now.get(profile_name) or {}
                disabled_count = 0
                for mod in p.get("mods", []):
                    if mod.get("mod_id") in culprit_ids \
                            and mod.get("enabled", True):
                        mod["enabled"] = False
                        disabled_count += 1
                if disabled_count:
                    save_profiles(profiles_now)
                    print(f"  Disabled {disabled_count} broken "
                          f"{disp} mod(s).")
                    if hasattr(self, "_active_profile_view") \
                            and self._active_profile_view == profile_name:
                        self._open_profile(profile_name)
                # Re-run profile diagnosis so the final report
                # reflects the disabled mods.
                profile_risks = self._diagnose_profile_freeze_risks(
                    profile_name)

        sd_risks = []
        sd_status = None
        if not os.path.isdir(ARCROPOLIS_MODS):
            sd_status = (f"SD not detected at {ARCROPOLIS_MODS} — "
                         "skipped file-level scan.")
        else:
            try:
                installed = [d for d in os.listdir(ARCROPOLIS_MODS)
                             if os.path.isdir(os.path.join(
                                 ARCROPOLIS_MODS, d))]
            except OSError as e:
                installed = []
                sd_status = f"SD scan failed: {e}"
            if not sd_status:
                if not installed:
                    sd_status = (f"{ARCROPOLIS_MODS} is empty — "
                                 "load this profile to the SD first "
                                 "for a full file-level scan.")
                else:
                    try:
                        sd_risks = diagnose_freeze_risks(ARCROPOLIS_MODS)
                        sd_status = (f"Scanned {len(installed)} "
                                     f"installed mod folder(s).")
                    except Exception as e:
                        sd_status = f"SD scan failed: {e}"

        # Pre-load mode: filter out SD-side risks whose contributing
        # folders are ALL about to be removed by the imminent sync.
        # Without this, the user gets blocked by warnings about leftover
        # mods from a previous profile that the very-next-step would
        # have wiped — exactly the "Skeleton_Luigi_c07 freezes!" prompt
        # for a folder that the diff is going to delete in 2 seconds.
        if pre_load and sd_risks:
            try:
                profiles_now = load_profiles()
                profile_mods = profiles_now.get(profile_name, {}).get(
                    "mods", [])
                profile_folders = set()
                profile_mod_ids = set()
                for m in profile_mods:
                    if not m.get("enabled", True):
                        continue
                    if m.get("mod_id") is not None:
                        profile_mod_ids.add(str(m["mod_id"]))
                    fn = m.get("folder_name")
                    if fn:
                        profile_folders.add(fn)
                # Build the set of SD folders that the diff will keep
                # (mod_id in profile OR no mod_id and folder_name in
                # profile OR untracked manual without metadata).
                installed_map, untracked_manual = (
                    self._scan_installed_mod_ids())
                kept_folders = set(untracked_manual)
                for mid, folder in installed_map.items():
                    if str(mid) in profile_mod_ids:
                        kept_folders.add(folder)
                    elif folder in profile_folders:
                        kept_folders.add(folder)
                # Drop risks whose every contributing folder will be
                # removed.
                filtered = []
                for r in sd_risks:
                    contribs = set(r.get("mods", []))
                    if contribs and contribs.isdisjoint(kept_folders):
                        # All contributors are going to be removed
                        # — risk is irrelevant after sync.
                        continue
                    filtered.append(r)
                if len(filtered) != len(sd_risks):
                    print(f"  Pre-load: ignoring "
                          f"{len(sd_risks) - len(filtered)} SD risk(s) "
                          "from folder(s) the sync will remove.")
                sd_risks = filtered
            except Exception as e:
                print(f"  ! Pre-load risk-filter failed: {e}",
                      file=sys.stderr)

        all_risks = profile_risks + sd_risks
        freezes = [r for r in all_risks if r["severity"] == "freeze"]
        warnings = [r for r in all_risks if r["severity"] == "warning"]

        # ── Console dump (always full detail) ──
        print(f"\n=== Validation report for profile '{profile_name}' ===")
        print(f"  SD: {sd_status}")
        print(f"  Profile checks: {len(profile_risks)} issue(s)")
        print(f"  SD checks: {len(sd_risks)} issue(s)")
        for r in all_risks:
            disp = INTERNAL_TO_DISPLAY.get(r["fighter"], r["fighter"])
            print(f"  [{r['severity'].upper():7}] {disp} {r['slot']} "
                  f"— {r['issue']}")
            print(f"    {r['detail']}")
            print(f"    Contributing: {', '.join(r['mods'])}")
        print(f"=== End validation ===\n")

        # ── Popup body ──
        lines = [f"Profile: {profile_name}",
                 sd_status or "",
                 ""]
        if freezes:
            lines.append(f"FREEZE RISKS ({len(freezes)}):")
            for r in freezes:
                disp = INTERNAL_TO_DISPLAY.get(r["fighter"], r["fighter"])
                lines.append(f"  ✗ {disp} {r['slot']} — {r['issue']}")
                for m in r["mods"][:3]:
                    lines.append(f"      from: {m}")
                if len(r["mods"]) > 3:
                    lines.append(f"      …and {len(r['mods']) - 3} more")
            lines.append("")
        if warnings:
            lines.append(f"Warnings ({len(warnings)}):")
            for r in warnings[:15]:
                disp = INTERNAL_TO_DISPLAY.get(r["fighter"], r["fighter"])
                lines.append(f"  • {disp} {r['slot']}: {r['issue']}")
            if len(warnings) > 15:
                lines.append(f"  …and {len(warnings) - 15} more")
            lines.append("")

        if not all_risks:
            lines.append("No issues detected.")
            if not sd_risks and "skipped" in (sd_status or ""):
                lines.append("(Profile-only check — plug the SD in "
                             "and re-Validate after loading for a "
                             "full file-level scan.)")

        body = "\n".join(l for l in lines if l is not None)
        if freezes:
            if pre_load:
                # Pre-load: ask the user whether to proceed despite
                # the freeze risk. Default = abort.
                proceed = messagebox.askyesno(
                    "⚠ Freeze Risk — Load anyway?",
                    body + "\n\nLoad to SD anyway? "
                    "(Picking the listed character(s) will likely "
                    "crash the game.)")
                return bool(proceed)
            messagebox.showwarning("⚠ Freeze Risk Detected", body)
            return False
        elif warnings:
            if pre_load:
                # Warnings only — proceed silently, just log.
                print(f"  Pre-load validate: {len(warnings)} "
                      "warning(s), no freeze risk. Proceeding.")
                return True
            messagebox.showinfo("Validation: warnings only", body)
            return True
        else:
            if pre_load:
                print("  Pre-load validate: clean.")
                return True
            messagebox.showinfo("Validation passed", body)
            return True

    def _validate_installed_mods(self):
        """Run the freeze-pattern static analyzer against the current
        SD card mods folder and surface the results without doing any
        installs / deletions. Cheap — pure read-only walk plus the
        :func:`diagnose_freeze_risks` simulation.
        """
        # If the SD isn't visible we can't validate anything — the path
        # would walk to an empty/missing folder and falsely report
        # "all clean". Make the caller plug the card in first.
        if not os.path.isdir(ARCROPOLIS_MODS):
            messagebox.showwarning(
                "SD card not detected",
                f"Can't validate — {ARCROPOLIS_MODS} doesn't exist.\n\n"
                "Plug the SD card in (or use the SD picker in the top "
                "bar) and click Validate again.")
            return

        # Count how many tracked mod folders are actually there. An empty
        # mods/ folder is "clean" but useless to validate, so we say so
        # explicitly instead of green-lighting it silently.
        try:
            mod_dirs = [d for d in os.listdir(ARCROPOLIS_MODS)
                        if os.path.isdir(os.path.join(ARCROPOLIS_MODS, d))]
        except OSError as e:
            messagebox.showerror("Validate failed", str(e))
            return
        if not mod_dirs:
            messagebox.showwarning(
                "Nothing to validate",
                f"{ARCROPOLIS_MODS} is empty — no mods installed.\n\n"
                "Load a profile to the SD first, then Validate.")
            return

        try:
            risks = diagnose_freeze_risks(ARCROPOLIS_MODS)
        except Exception as e:
            messagebox.showerror("Validate failed", str(e))
            return

        freezes = [r for r in risks if r["severity"] == "freeze"]
        warnings = [r for r in risks if r["severity"] == "warning"]

        if not risks:
            messagebox.showinfo(
                "Validation passed",
                f"Scanned {len(mod_dirs)} mod folder(s) under "
                f"{ARCROPOLIS_MODS}.\n\n"
                "Every modded slot has the model trees and portraits "
                "the game expects. Safe to load on the Switch.")
            return

        lines = []
        if freezes:
            lines.append(
                f"FREEZE RISKS ({len(freezes)}) — picking these "
                f"characters will likely crash:")
            for r in freezes:
                disp = INTERNAL_TO_DISPLAY.get(r["fighter"], r["fighter"])
                lines.append(f"  ✗ {disp} {r['slot']} — {r['issue']}")
                for m in r["mods"][:2]:
                    lines.append(f"      from: {m}")
                if len(r["mods"]) > 2:
                    lines.append(
                        f"      …and {len(r['mods']) - 2} more")
            lines.append("")
        if warnings:
            lines.append(
                f"Warnings ({len(warnings)}) — playable, just visual "
                f"or load-order issues:")
            for r in warnings[:12]:
                disp = INTERNAL_TO_DISPLAY.get(r["fighter"], r["fighter"])
                lines.append(f"  • {disp} {r['slot']}: {r['issue']}")
            if len(warnings) > 12:
                lines.append(f"  …and {len(warnings) - 12} more")

        # Also dump full report to console so the user can scroll.
        print(f"\n=== Validation report for {ARCROPOLIS_MODS} ===")
        for r in risks:
            disp = INTERNAL_TO_DISPLAY.get(r["fighter"], r["fighter"])
            print(f"  [{r['severity'].upper():7}] {disp} {r['slot']} — "
                  f"{r['issue']}")
            print(f"    {r['detail']}")
            print(f"    Contributing mod(s): {', '.join(r['mods'])}")
        print(f"=== End validation ===\n")

        body = "\n".join(lines) + (
            "\n\nFull report printed to the console. Disable / remove "
            "the listed mod(s) and run Validate again before loading "
            "the SD." if freezes else "")
        if freezes:
            messagebox.showwarning("⚠ Freeze Risk Detected", body)
        else:
            messagebox.showinfo("Validation: warnings only", body)

    def _bulk_set_character_enabled(self, profile_name, character, enabled):
        """Enable or disable every mod in ``profile_name`` whose
        character matches ``character``. Used to bisect a fighter-
        specific freeze (e.g. "disable all Plant skins, see if game
        stops crashing on character-pick"). Re-renders the profile
        view when done.
        """
        profiles = load_profiles()
        profile = profiles.get(profile_name)
        if not profile:
            return
        changed = 0
        for m in profile.get("mods", []):
            if m.get("character", "Other") != character:
                continue
            current = m.get("enabled", True)
            if current != enabled:
                m["enabled"] = bool(enabled)
                changed += 1
        if changed:
            save_profiles(profiles)
            verb = "Enabled" if enabled else "Disabled"
            print(f"  {verb} {changed} {character} mod(s) in profile "
                  f"'{profile_name}'"
                  + ("" if enabled else
                     " — they'll be uninstalled from SD on next load."))
        self._open_profile(profile_name)

    def _rename_profile_mod(self, profile_name, mod_id, folder_name,
                             current_name):
        """Rename a single skin entry inside a profile.

        We change the user-visible ``name`` field on the profile entry
        only — ``mod_id`` (GameBanana identity) and ``folder_name``
        (on-SD location) stay put. The new label shows up in the
        profile detail view, in the Install dialog, and on the SD's
        ``.gb_meta.json`` *next* time the mod is reinstalled.
        """
        new_name = simpledialog.askstring(
            "Rename Skin",
            f"New display name for '{current_name}':",
            initialvalue=current_name,
            parent=self.root)
        if new_name is None:
            return  # cancelled
        new_name = new_name.strip()
        if not new_name or new_name == current_name:
            return

        profiles = load_profiles()
        profile = profiles.get(profile_name)
        if not profile:
            messagebox.showerror("Rename Skin",
                                 f"Profile '{profile_name}' not found.")
            return

        # Match by mod_id when present (the most stable key); fall
        # back to folder_name for mods without a GameBanana id.
        target = None
        for m in profile.get("mods", []):
            if mod_id is not None and m.get("mod_id") == mod_id:
                target = m
                break
            if (mod_id is None and folder_name
                    and m.get("folder_name") == folder_name):
                target = m
                break
        if target is None:
            messagebox.showerror(
                "Rename Skin",
                f"Couldn't find '{current_name}' in profile "
                f"'{profile_name}'.")
            return

        target["name"] = new_name
        save_profiles(profiles)
        print(f"  Renamed '{current_name}' -> '{new_name}' "
              f"in profile '{profile_name}'")
        self._open_profile(profile_name)

    def _add_profile_mod_card(self, profile_name, mod):
        """Add a card for a mod inside a profile — with thumbnail and remove button."""
        mod_name = mod.get("name", mod.get("folder_name", "?"))
        mod_id = mod.get("mod_id")
        slot = mod.get("slot", "")
        character = mod.get("character", "")
        submitter = mod.get("submitter", "")
        thumb_url = mod.get("thumb_url")
        image_urls = mod.get("image_urls", [])
        url = mod.get("url", "")
        enabled = mod.get("enabled", True)

        # Disabled mods get a dimmed surface so the row reads as
        # "skipped on load" at a glance.
        card_bg = T.BG if enabled else T.SURFACE1
        card = tk.Frame(self.results_inner, bg=card_bg, padx=8, pady=6)
        card.pack(fill="x", padx=8, pady=4)

        # Left: thumbnail
        thumb_frame = tk.Frame(card, bg=T.SURFACE1, width=160, height=100)
        thumb_frame.pack(side="left", padx=(0, 10))
        thumb_frame.pack_propagate(False)

        thumb_label = tk.Label(thumb_frame, text="No image",
                               bg=T.SURFACE1, fg=T.OVERLAY,
                               font=(T.FONT, T.SZ_SM))
        thumb_label.pack(expand=True)

        if thumb_url:
            thumb_label.configure(text="Loading...")
            threading.Thread(
                target=self._load_thumb,
                args=(thumb_label, thumb_url, f"pmod_{mod_id}"),
                daemon=True).start()

        # Click for gallery
        if image_urls:
            def _open_gallery(e=None, n=mod_name, urls=image_urls):
                self._show_image_gallery(n, urls)
            thumb_label.configure(cursor="hand2")
            thumb_label.bind("<Button-1>", _open_gallery)

        # Right: info
        info = tk.Frame(card, bg=card_bg)
        info.pack(side="left", fill="both", expand=True)

        title = mod_name
        title_fg = T.FG if enabled else T.OVERLAY
        title_text = title if enabled else f"{title}  (disabled)"
        tk.Label(info, text=title_text, bg=card_bg, fg=title_fg,
                 font=(T.FONT, T.SZ_XL, "bold"), anchor="w",
                 wraplength=420).pack(fill="x")

        # Slot badges
        if slot:
            slot_row = tk.Frame(info, bg=card_bg)
            slot_row.pack(fill="x", pady=(2, 0))
            badge_bg = T.GREEN if enabled else T.SUBTEXT
            for s in slot.replace(",", " ").split():
                s = s.strip()
                if s:
                    tk.Label(slot_row, text=s, bg=badge_bg, fg=T.BG,
                             font=(T.MONO, T.SZ_XS, "bold"),
                             padx=4, pady=1).pack(side="left", padx=(0, 3))
        elif mod.get("mod_type", "skin") == "skin":
            tk.Label(info, text="No slot assigned", bg=T.SURFACE1, fg=T.OVERLAY,
                     font=(T.MONO, T.SZ_XS, "bold"), padx=6, pady=2,
                     anchor="w").pack(fill="x", pady=(2, 0))

        detail_parts = []
        if submitter:
            detail_parts.append(f"by {submitter}")
        if character and character != "Other":
            detail_parts.append(character)
        if detail_parts:
            tk.Label(info, text="  |  ".join(detail_parts),
                     bg=card_bg, fg=T.SUBTEXT,
                     font=(T.FONT, T.SZ_SM), anchor="w").pack(fill="x", pady=(2, 0))

        # Buttons
        btn_row = tk.Frame(info, bg=card_bg)
        btn_row.pack(fill="x", pady=(4, 0))

        def _toggle(mid=mod_id, fn=mod.get("folder_name"),
                    mn=mod_name, pn=profile_name, was=enabled):
            new_val = set_mod_enabled(pn, not was, mod_id=mid, folder_name=fn)
            if new_val is None:
                return
            verb = "Enabled" if new_val else "Disabled"
            print(f"  {verb} '{mn}' in profile '{pn}'"
                  + ("" if new_val else
                     " — will be uninstalled from SD on next load."))
            self._open_profile(pn)

        toggle_label = "Disable" if enabled else "Enable"
        toggle_bg = T.SURFACE1 if enabled else T.GREEN
        toggle_fg = T.FG if enabled else T.BG
        tk.Button(btn_row, text=toggle_label, width=10,
                  bg=toggle_bg, fg=toggle_fg,
                  font=(T.FONT, T.SZ_SM, "bold"),
                  relief="flat", cursor="hand2",
                  command=_toggle).pack(side="left", padx=(0, 6))

        def _remove(mid=mod_id, fn=mod.get("folder_name"),
                   mn=mod_name, pn=profile_name):
            remove_mod_from_profile(pn, mod_id=mid, folder_name=fn)
            print(f"  Removed '{mn}' from profile '{pn}'")
            self._open_profile(pn)

        tk.Button(btn_row, text="Remove", width=10,
                  bg=T.RED, fg=T.BG, font=(T.FONT, T.SZ_SM, "bold"),
                  relief="flat", cursor="hand2",
                  command=_remove).pack(side="left", padx=(0, 6))

        def _rename(mid=mod_id, fn=mod.get("folder_name"),
                    mn=mod_name, pn=profile_name):
            self._rename_profile_mod(pn, mid, fn, mn)

        tk.Button(btn_row, text="Rename", width=10,
                  bg=T.YELLOW, fg=T.BG,
                  font=(T.FONT, T.SZ_SM, "bold"),
                  relief="flat", cursor="hand2",
                  command=_rename).pack(side="left", padx=(0, 6))

        if url:
            tk.Button(btn_row, text="Open Page", width=10,
                      bg=T.SURFACE1, fg=T.FG, font=(T.FONT, T.SZ_SM),
                      relief="flat", cursor="hand2",
                      command=lambda u=url: os.startfile(u)
                      ).pack(side="left", padx=(0, 6))

        if mod_id:
            # Capture metadata so the install dialog can do drag-to-slot
            # without needing a separate metadata fetch.
            mod_meta = {
                "mod_id": mod_id,
                "name": mod_name,
                "thumb_url": thumb_url,
                "image_urls": image_urls,
                "url": url,
            }
            tk.Button(btn_row, text="Install", width=11,
                      bg=T.ACCENT, fg=T.BG,
                      font=(T.FONT, T.SZ_SM, "bold"),
                      relief="flat", cursor="hand2",
                      command=lambda mid=mod_id, mn=mod_name, m=mod_meta:
                          self._view_model(mod_id=mid, mod_name=mn, metadata=m)
                      ).pack(side="left", padx=(0, 6))

    def _view_model(self, mod_id=None, mod_name="", installed_path=None,
                    metadata=None):
        """Show a multi-slot 3D preview of the mod's model in a popup.

        Each costume slot (c00, c01, …) gets a rendered thumbnail
        with a button to launch an interactive 3D viewer.

        For uncached mods this method downloads + extracts the archive,
        which can take several seconds. To keep the UI responsive (and
        the Install button from looking stuck/depressed) we run the
        slow path on a worker thread behind a small modal progress
        dialog, then resume on the main thread to open the popup.
        """
        # Cache hit + plain installed-path: synchronous fast path is fine.
        if installed_path and os.path.isdir(installed_path):
            self._open_install_for_extracted(installed_path, mod_name,
                                              mod_id=mod_id,
                                              metadata=metadata,
                                              installed_path=installed_path)
            return

        if not mod_id:
            messagebox.showerror("View Model",
                                 "No mod ID or installed path available.")
            return

        cache_dir = os.path.join(MOD_CACHE_DIR, str(mod_id))
        extracted = os.path.join(cache_dir, "extracted")

        if os.path.isdir(extracted) and os.listdir(extracted):
            # Cached — open immediately.
            self._open_install_for_extracted(extracted, mod_name,
                                              mod_id=mod_id,
                                              metadata=metadata,
                                              installed_path=None)
            return

        # ── Slow path: download + extract on a background thread ──
        # Show a modal "Preparing…" dialog so the user gets immediate
        # feedback (the Install button click reaches the mainloop and
        # paints the dialog before kicking off the worker).
        prog_win = self._show_install_prepare_dialog(mod_name)

        def _phase(text):
            self.root.after(0, lambda t=text: prog_win.set_phase(t))

        def _worker():
            archive_path = None
            try:
                _phase("Fetching file info from GameBanana…")
                self._show_progress(f"Downloading {mod_name}…")
                _phase("Downloading archive…")
                archive_path = self._download_mod_archive(mod_id, mod_name)
                if not archive_path:
                    self.root.after(0, lambda: messagebox.showerror(
                        "Download failed",
                        f"No downloadable file for {mod_name}."))
                    return
                _phase("Extracting archive…")
                os.makedirs(extracted, exist_ok=True)
                extract_archive(archive_path, extracted)
                _phase("Opening Install dialog…")
                self.root.after(0, lambda: self._open_install_for_extracted(
                    extracted, mod_name, mod_id=mod_id,
                    metadata=metadata, installed_path=None))
            except Exception as e:
                err = str(e)
                self.root.after(0, lambda: messagebox.showerror(
                    "Preview failed", err))
            finally:
                self._hide_progress()
                self._cleanup_archive(archive_path)
                self.root.after(0, prog_win.close)

        threading.Thread(target=_worker, daemon=True).start()

    def _open_install_for_extracted(self, extracted, mod_name,
                                     mod_id=None, metadata=None,
                                     installed_path=None):
        """Once the mod's files are on disk (cached or freshly
        extracted), enumerate slots and open the Install dialog. Must
        be called on the Tk main thread.
        """
        slots = _find_all_model_slots(extracted)
        if not slots:
            model_dir = _find_model_folder(extracted)
            if model_dir:
                slots = [(os.path.basename(model_dir).lower(), model_dir)]
            else:
                content = find_mod_content(extracted) or extracted
                has_numshb = any(
                    f.endswith(".numshb")
                    for _r, _ds, fs in os.walk(content) for f in fs)
                if not has_numshb:
                    messagebox.showinfo(
                        "No model files",
                        f"'{mod_name}' doesn't contain renderable model "
                        f"files — it may be a UI-only or texture-swap mod.\n\n"
                        f"Opening the extracted folder instead.")
                    os.startfile(extracted)
                    return
                slots = [("model", content)]

        if not HAS_3D_RENDER:
            self._open_ssbh_editor(slots[0][1], mod_name)
            return

        self._show_model_slots_popup(mod_name, slots, mod_id=mod_id,
                                      metadata=metadata,
                                      installed_path=installed_path)

    def _show_install_prepare_dialog(self, mod_name):
        """Tiny modal-ish dialog shown while we download + extract a
        mod for the Install preview. Has a determinate-looking
        animated progressbar (mode='indeterminate') and a phase label
        the worker thread updates via :meth:`set_phase`.

        Returns an object with ``set_phase(text)`` and ``close()``
        methods, callable from any thread (they marshal to the Tk
        main thread internally).
        """
        win = tk.Toplevel(self.root)
        win.title("Preparing Install…")
        win.configure(bg=T.SURFACE)
        win.transient(self.root)
        win.resizable(False, False)
        try:
            win.grab_set()
        except tk.TclError:
            pass

        body = tk.Frame(win, bg=T.SURFACE, padx=22, pady=18)
        body.pack(fill="both", expand=True)

        tk.Label(body, text=mod_name, bg=T.SURFACE, fg=T.FG,
                 font=(T.FONT, T.SZ_LG, "bold"),
                 wraplength=360).pack(anchor="w")
        phase_var = tk.StringVar(value="Starting…")
        tk.Label(body, textvariable=phase_var, bg=T.SURFACE,
                 fg=T.OVERLAY, font=(T.FONT, T.SZ_SM),
                 wraplength=360,
                 justify="left").pack(anchor="w", pady=(4, 8))

        bar = ttk.Progressbar(body, mode="indeterminate", length=360)
        bar.pack(fill="x")
        bar.start(80)

        # Center on the main window.
        win.update_idletasks()
        w = win.winfo_reqwidth()
        h = win.winfo_reqheight()
        x = self.root.winfo_rootx() + max(
            0, (self.root.winfo_width() - w) // 2)
        y = self.root.winfo_rooty() + max(
            0, (self.root.winfo_height() - h) // 2)
        win.geometry(f"{w}x{h}+{x}+{y}")

        closed = {"v": False}

        class Handle:
            @staticmethod
            def set_phase(text):
                if closed["v"]:
                    return
                try:
                    phase_var.set(text)
                except tk.TclError:
                    pass

            @staticmethod
            def close():
                if closed["v"]:
                    return
                closed["v"] = True
                try:
                    bar.stop()
                except tk.TclError:
                    pass
                try:
                    win.grab_release()
                except tk.TclError:
                    pass
                try:
                    win.destroy()
                except tk.TclError:
                    pass

        # If the user closes the dialog manually we still let the
        # worker finish, but it'll just see a no-op handle.
        win.protocol("WM_DELETE_WINDOW", Handle.close)
        return Handle

    def _load_install_thumb(self, label, url, key, target_w, target_h):
        """Fetch a GameBanana thumbnail and resize it to fill the
        Install dialog's pixel box (rather than ``fetch_thumbnail``'s
        global 220×124 max). Keeps source/destination thumbnails the
        same visible size."""
        if not HAS_PIL or not HAS_REQUESTS:
            return
        try:
            resp = requests.get(url, verify=False, timeout=10)
            resp.raise_for_status()
            img = Image.open(io.BytesIO(resp.content))
            img.thumbnail((target_w, target_h), Image.LANCZOS)
            photo = ImageTk.PhotoImage(img)
        except Exception as e:
            self.root.after(0, lambda: self._set_thumb_text(
                label, "No preview"))
            print(f"    [install thumb] {url}: {e}")
            return
        self._thumb_cache[key] = photo  # prevent GC
        self.root.after(0, lambda: self._set_thumb(label, photo))

    def _find_installed_model_dir(self, occ, fighter_int, slot_key):
        """Locate a renderable model directory for an installed mod
        slot. Returns the slot dir path or ``None``.

        Search order:
          1. ``ARCROPOLIS_MODS/<mod>/fighter/<f>/model/body/<slot>``
             (SD card — only valid when the card is plugged in)
          2. Any ``model/body/<slot>`` under
             ``.mod_cache/<mod_id>/extracted/`` — works without SD
             since the cache is on the PC's disk
        """
        if not fighter_int:
            return None

        # 1. SD card.
        mod_folder = occ.get("mod") if occ else None
        if mod_folder:
            candidate = os.path.join(
                ARCROPOLIS_MODS, mod_folder, "fighter", fighter_int,
                "model", "body", slot_key)
            try:
                if os.path.isdir(candidate) and any(
                        f.endswith(".numshb")
                        for f in os.listdir(candidate)):
                    return candidate
            except OSError:
                pass

        # 2. .mod_cache by mod_id.
        mod_id = occ.get("mod_id") if occ else None
        if mod_id:
            extracted = os.path.join(MOD_CACHE_DIR, str(mod_id), "extracted")
            if os.path.isdir(extracted):
                source_slot = (occ.get("source_slot") or "").lower()
                src_match = None       # path matching source_slot
                slot_key_match = None  # path matching slot_key
                fallback = None        # any cXX dir for this fighter
                for root, _dirs, files in os.walk(extracted):
                    if not any(f.endswith(".numshb") for f in files):
                        continue
                    parts = os.path.normpath(root).replace(
                        "\\", "/").split("/")
                    # Need at least .../fighter/<f>/model/body/cXX.
                    if (len(parts) < 5
                            or parts[-2] != "body"
                            or parts[-3] != "model"
                            or parts[-5] != "fighter"
                            or parts[-4] != fighter_int
                            or not re.fullmatch(r"c\d{2}", parts[-1])):
                        continue
                    cur_slot = parts[-1].lower()
                    if source_slot and cur_slot == source_slot:
                        src_match = root
                        break  # exact source_slot wins outright
                    if cur_slot == slot_key:
                        if slot_key_match is None:
                            slot_key_match = root
                    if fallback is None:
                        fallback = root
                # Priority: source_slot > slot_key > any cXX.
                return src_match or slot_key_match or fallback
        return None

    def _wire_hover_thumb_swap(self, label, url, cache_key,
                                target_w, target_h):
        """Bind ``<Enter>`` / ``<Leave>`` on a thumbnail label so it
        shows the GameBanana web preview while the cursor is over it
        and reverts to whatever was there (typically the 3D render)
        on leave. The web image is fetched once on first hover and
        cached on the label for future toggles."""
        # State stored on the label:
        #   _smash_drag_image    — currently displayed image (also the
        #                           ghost source for drag-drop)
        #   _smash_normal_image  — the image to restore on <Leave>
        #   _smash_hover_image   — the fetched GameBanana preview
        def _show_hover(_e=None):
            normal = label.cget("image")
            if normal and not getattr(label, "_smash_hover_active", False):
                label._smash_normal_image_name = normal
                label._smash_normal_image = getattr(
                    label, "_smash_drag_image", None)
            cached = getattr(label, "_smash_hover_image", None)
            if cached is not None:
                label._smash_hover_active = True
                try:
                    label.configure(image=cached, text="")
                except tk.TclError:
                    pass
                return

            def _fetch():
                if not HAS_PIL or not HAS_REQUESTS:
                    return
                try:
                    resp = requests.get(url, verify=False, timeout=10)
                    resp.raise_for_status()
                    img = Image.open(io.BytesIO(resp.content))
                    img.thumbnail((target_w, target_h), Image.LANCZOS)
                    photo = ImageTk.PhotoImage(img)
                except Exception as e:
                    print(f"    [hover thumb] {url}: {e}")
                    return
                self._thumb_cache[cache_key] = photo  # prevent GC

                def _apply():
                    label._smash_hover_image = photo
                    # Only swap in if the cursor is still over the
                    # widget by the time the fetch finished.
                    if getattr(label, "_smash_hover_pending", False):
                        try:
                            label.configure(image=photo, text="")
                            label._smash_hover_active = True
                        except tk.TclError:
                            pass
                self.root.after(0, _apply)

            label._smash_hover_pending = True
            threading.Thread(target=_fetch, daemon=True).start()

        def _hide_hover(_e=None):
            label._smash_hover_pending = False
            if not getattr(label, "_smash_hover_active", False):
                return
            label._smash_hover_active = False
            normal = getattr(label, "_smash_normal_image", None)
            try:
                if normal is not None:
                    label.configure(image=normal, text="")
                else:
                    name = getattr(label, "_smash_normal_image_name", "")
                    if name:
                        label.configure(image=name, text="")
            except tk.TclError:
                pass

        label.bind("<Enter>", _show_hover)
        label.bind("<Leave>", _hide_hover)

    def _build_install_cell(self, cell, slot_label, name_text,
                             model_dir=None, thumb_url=None,
                             thumb_url_key=None,
                             thumb_w=240, thumb_h=160,
                             drag_source=False,
                             drag_dest=False,
                             on_remove=None,
                             on_download=None,
                             hover_thumb_url=None,
                             hover_thumb_key=None):
        """Build the contents of one Install-dialog cell with the
        layout shared between source and destination columns:

            [ slot title ]
            [ 240×160 thumb box ]
            [ name / variant label  (fixed 2-line height) ]
            [ Open 3D button  (disabled if no model_dir) ]

        Identical structure on both sides → cells stay vertically
        aligned regardless of which side a thumb has loaded.

        Stores the inner thumb ``tk.Label`` on ``cell._smash_thumb_label``
        so callers can queue it for sequential 3D rendering.
        """
        for w in cell.winfo_children():
            w.destroy()

        # Title.
        title_label = tk.Label(cell, text=slot_label.upper(),
                                bg=T.SURFACE, fg=T.ACCENT,
                                font=(T.MONO, T.SZ_MD, "bold"))
        title_label.pack(pady=(4, 0))

        # Fixed-pixel thumb box.
        thumb_box = tk.Frame(cell, bg=T.SURFACE1,
                             width=thumb_w, height=thumb_h)
        thumb_box.pack(padx=4, pady=4)
        thumb_box.pack_propagate(False)
        thumb = tk.Label(
            thumb_box,
            text="(empty)" if not (model_dir or thumb_url) else "Loading…",
            bg=T.SURFACE1, fg=T.OVERLAY,
            font=(T.FONT, T.SZ_SM),
            cursor="fleur" if drag_source else "")
        thumb.pack(expand=True, fill="both")
        cell._smash_thumb_label = thumb

        # Hover-swap: peek at the GameBanana preview image when the
        # cursor is over the thumbnail. Useful for cells where the
        # 3D render came out wrong (texture-only mods, broken
        # geometry, etc.) — the web thumb is what the mod author
        # actually sees in-game and is the definitive visual.
        if hover_thumb_url:
            self._wire_hover_thumb_swap(thumb, hover_thumb_url,
                                        hover_thumb_key or hover_thumb_url,
                                        thumb_w, thumb_h)

        # If we don't have a model_dir but do have a web thumbnail
        # (rare with current logic, but kept for fallback), kick that
        # off here.
        if not model_dir and thumb_url:
            threading.Thread(
                target=self._load_install_thumb,
                args=(thumb, thumb_url, thumb_url_key or thumb_url,
                      thumb_w, thumb_h),
                daemon=True).start()

        # One-line name label, hard-truncated with ellipsis. No
        # ``wraplength`` so the label cannot grow vertically and shove
        # the Open-3D button or thumb into the next cell.
        full = name_text or "—"
        max_chars = max(8, (thumb_w // 7))  # ~7 px per char at SZ_XS
        if len(full) > max_chars:
            full = full[:max_chars - 1] + "…"
        name_label = tk.Label(cell, text=full,
                               bg=T.SURFACE, fg=T.FG,
                               font=(T.FONT, T.SZ_XS))
        name_label.pack(pady=(0, 0))

        # Bottom-row buttons: Open 3D + (optional) Download / Remove.
        btn_row = tk.Frame(cell, bg=T.SURFACE)
        btn_row.pack(pady=(0, 4))
        btn_state = "normal" if model_dir else "disabled"
        tk.Button(btn_row, text="Open 3D",
                  bg=T.SURFACE1, fg=T.FG,
                  font=(T.FONT, T.SZ_XS), relief="flat",
                  cursor="hand2",
                  state=btn_state,
                  command=(lambda d=model_dir:
                           self._launch_interactive_viewer(d))
                  if model_dir else None
                  ).pack(side="left", padx=(0, 4))
        if on_download is not None:
            # Refresh icon — fetches the archive (or re-tries finding
            # a model if it's already cached) and renders or falls
            # back to the web thumbnail.
            tk.Button(btn_row, text="↻",
                      bg=T.GREEN, fg=T.BG,
                      font=(T.FONT, T.SZ_MD, "bold"), relief="flat",
                      cursor="hand2", width=2,
                      command=on_download
                      ).pack(side="left", padx=(0, 4))
        if on_remove is not None:
            tk.Button(btn_row, text="✕ Remove",
                      bg=T.SURFACE1, fg=T.RED,
                      font=(T.FONT, T.SZ_XS, "bold"), relief="flat",
                      cursor="hand2",
                      command=on_remove
                      ).pack(side="left")

        # Drag wiring. Bind on the cell, thumb, thumb_box, title, and
        # name labels — every visible part of the card EXCEPT the
        # button row (whose buttons must remain clickable). Without
        # this, clicking the slot title or mod name caption looked
        # like a "dead zone" for drag.
        drag_targets = [cell, thumb, thumb_box, title_label, name_label]

        if drag_source and model_dir:
            self._bind_drag_source(drag_targets, slot_label, model_dir,
                                    kind="source", thumb_label=thumb)
            for w in drag_targets:
                w.bind("<Double-Button-1>",
                       lambda _e, d=model_dir:
                       self._launch_interactive_viewer(d))
        elif drag_dest:
            # Destination cells can be dragged onto another destination
            # to swap (target occupied) or move (target empty) within
            # the active profile. The dest-drag handler doesn't need
            # the model dir — it operates on slot labels — so we wire
            # this even when there's no rendered 3D model (texture-
            # only mods, mods that haven't downloaded yet, etc.).
            self._bind_drag_source(drag_targets, slot_label,
                                    model_dir or "",
                                    kind="dest", thumb_label=thumb)
            for w in drag_targets:
                try:
                    w.configure(cursor="fleur")
                except tk.TclError:
                    pass
            if model_dir:
                for w in drag_targets:
                    w.bind("<Double-Button-1>",
                           lambda _e, d=model_dir:
                           self._launch_interactive_viewer(d))

    def _build_dest_slot_contents(self, box, slot_key, occ,
                                  fighter_int, thumb_w, thumb_h,
                                  ctx=None):
        """Render one destination cXX cell using the same layout as
        source cells so columns align. Returns ``(model_dir, label)``
        for the sequential render queue when this slot has an
        installed mod with a renderable model on disk; ``None``
        otherwise.

        When the mod has no local cache (never downloaded), kicks
        off a background download so the cell can render the actual
        model the next time we re-look-up. Shows "Downloading…" in
        the cell while the worker runs.
        """
        installed_slot_dir = (
            self._find_installed_model_dir(occ, fighter_int, slot_key)
            if occ else None)

        # If we have a mod_id but no cache, we're about to fetch the
        # archive in the background and render the actual 3D model —
        # don't simultaneously load a static GameBanana web thumb,
        # because the thumb's load callback would race against (and
        # overwrite) the "Downloading preview…" status text we set
        # in ``_auto_fetch_preview``.
        will_auto_fetch = (occ and occ.get("mod_id")
                           and not installed_slot_dir
                           and ctx is not None)

        name_text = (occ.get("name") or occ.get("mod") or "—") if occ else "—"
        thumb_url = (occ.get("thumb_url")
                     if (occ and not installed_slot_dir
                         and not will_auto_fetch)
                     else None)
        thumb_key = (f"slot_{fighter_int}_{slot_key}"
                     if thumb_url else None)

        on_remove = None
        if occ and ctx is not None:
            on_remove = lambda c=ctx, s=slot_key: self._handle_remove_slot(c, s)

        # Hover-swap to the GameBanana web thumbnail when there's a
        # 3D render but the user wants to see the mod author's
        # canonical preview image instead.
        hover_url = (occ.get("thumb_url") if occ
                     and installed_slot_dir else None)
        hover_key = (f"hover_dest_{fighter_int}_{slot_key}"
                     if hover_url else None)

        self._build_install_cell(
            box, slot_key, name_text,
            model_dir=installed_slot_dir,
            thumb_url=thumb_url,
            thumb_url_key=thumb_key,
            thumb_w=thumb_w, thumb_h=thumb_h,
            drag_source=False,
            # Drag-to-move is allowed for any occupied slot, not just
            # those with a 3D render — moves operate on slot labels
            # so the user can re-slot texture-only mods or skins
            # whose model hasn't been cached yet.
            drag_dest=occ is not None,
            on_remove=on_remove,
            hover_thumb_url=hover_url,
            hover_thumb_key=hover_key)

        if installed_slot_dir:
            return (installed_slot_dir, box._smash_thumb_label)

        # No local model yet — if we have a mod_id, fetch in the
        # background so this cell shows the actual rendered slot
        # next time the dialog refreshes.
        if occ and occ.get("mod_id") and ctx is not None:
            self._auto_fetch_preview(box, occ, fighter_int, slot_key, ctx,
                                      thumb_w=thumb_w, thumb_h=thumb_h)
        return None

    def _auto_fetch_preview(self, box, occ, fighter_int, slot_key, ctx,
                             thumb_w=280, thumb_h=160, force=True):
        """Background download → extract → re-render for an install
        destination cell whose mod has no local cache yet.

        Dedupes in-flight fetches per mod_id so a multi-slot mod
        won't get downloaded twice. ``force=True`` (manual ↻ click)
        bypasses the dedupe so the user always gets a visible retry.
        After download, if the archive contained a renderable mesh
        we queue a 3D render; if it's a texture-only mod, we fall
        back to the GameBanana web thumbnail.
        """
        if not hasattr(self, "_install_preview_fetches"):
            self._install_preview_fetches = set()
        mod_id = occ.get("mod_id")
        mod_name = occ.get("name") or occ.get("mod") or f"Mod {mod_id}"
        thumb_url = occ.get("thumb_url") or ""
        if mod_id in self._install_preview_fetches and not force:
            return

        thumb = getattr(box, "_smash_thumb_label", None)
        if thumb is not None:
            try:
                thumb.configure(text="Downloading\npreview…",
                                fg=T.OVERLAY, image="")
            except tk.TclError:
                pass

        self._install_preview_fetches.add(mod_id)

        def _fall_back_to_web_thumb():
            if thumb is None:
                return
            if thumb_url:
                threading.Thread(
                    target=self._load_install_thumb,
                    args=(thumb, thumb_url,
                          f"slot_{fighter_int}_{slot_key}",
                          thumb_w, thumb_h),
                    daemon=True).start()
            else:
                try:
                    thumb.configure(text="No preview\navailable",
                                    fg=T.OVERLAY)
                except tk.TclError:
                    pass

        def _work():
            try:
                cache_dir = os.path.join(MOD_CACHE_DIR, str(mod_id))
                extracted = os.path.join(cache_dir, "extracted")
                if not os.path.isdir(extracted) or not os.listdir(extracted):
                    print(f"  [auto-preview] downloading '{mod_name}'…")
                    archive = self._download_mod_archive(mod_id, mod_name)
                    if not archive:
                        print(f"  [auto-preview] no downloadable file "
                              f"for '{mod_name}'")
                        self.root.after(0, _fall_back_to_web_thumb)
                        return
                    os.makedirs(extracted, exist_ok=True)
                    extract_archive(archive, extracted)
                    self._cleanup_archive(archive)
                slot_dir = self._find_installed_model_dir(
                    occ, fighter_int, slot_key)
                if slot_dir and thumb is not None:
                    self.root.after(0, lambda: self._render_slots_sequential(
                        [(slot_dir, thumb)]))
                else:
                    # Texture-only or non-model mod — fall back to the
                    # mod's GameBanana thumbnail.
                    self.root.after(0, _fall_back_to_web_thumb)
            except Exception as e:
                print(f"  [auto-preview] error fetching {mod_name}: {e}",
                      file=sys.stderr)
                self.root.after(0, _fall_back_to_web_thumb)
            finally:
                self._install_preview_fetches.discard(mod_id)

        threading.Thread(target=_work, daemon=True).start()

    def _handle_remove_slot(self, ctx, slot_key):
        """Remove the mod currently occupying ``slot_key`` from the
        active profile (or note that SD-direct removal isn't wired
        from the install dialog). Refreshes the destination strip
        when done.
        """
        fighter_int = ctx.get("fighter_int")
        target_profile = (self._profile_mode_target
                          or self._active_user_profile)
        occ = self._install_target_occupancy(fighter_int).get(slot_key)
        if not occ:
            return
        if not target_profile:
            messagebox.showinfo(
                "Remove",
                "Slot removal from the install dialog is currently "
                "wired only when a profile is the active install "
                "target. Use the Installed Skins view to uninstall "
                "from the SD card directly.",
                parent=ctx["win"])
            return
        if not messagebox.askyesno(
                "Remove",
                f"Remove '{occ.get('name', '?')}' from "
                f"{slot_key.upper()} in profile '{target_profile}'?",
                parent=ctx["win"]):
            return
        result = remove_profile_slot(target_profile, fighter_int, slot_key)
        if result:
            entry = result.get("entry") or {}
            if result.get("action") == "slot_stripped":
                print(f"  Stripped slot {slot_key} from "
                      f"'{entry.get('name', '?')}' in profile "
                      f"'{target_profile}' (kept "
                      f"{', '.join(result.get('remaining', []))})")
            else:
                print(f"  Removed '{entry.get('name', '?')}' "
                      f"from profile '{target_profile}' (slot {slot_key})")
        self._refresh_install_destinations(ctx)

    def _install_target_occupancy(self, fighter_int):
        """Return the slot→mod-info dict the Install dialog should
        display in its destination strip.

        The Install workflow is profile-centric: the right column shows
        what's in the *active profile*, never the raw SD card. When no
        profile is active, the strip is empty — drag-drop will then
        prompt the user to create a profile on the fly. Showing SD
        contents in this case was misleading because the user could
        accidentally "remove" something that wasn't actually in any
        profile they own.
        """
        if not fighter_int:
            return {}
        target = (self._profile_mode_target if self._profile_mode
                  else None) or self._active_user_profile
        if not target:
            return {}
        return self._get_profile_occupied_slots(target, fighter_int)

    def _show_model_slots_popup(self, mod_name, slots, mod_id=None,
                                metadata=None, installed_path=None):
        """Open the Install dialog: source slots on the left (3D rendered,
        draggable), destination c00–c07 strip on the right (showing
        currently-installed mod thumbnails). Drag a source slot onto a
        destination to install with a slot remap.

        If an Install dialog is already open, the left ("Available")
        column is hot-swapped to show the newly-clicked mod's source
        slots instead of opening a second window. The right column is
        also re-derived so destinations reflect the now-relevant
        fighter. This avoids a stack of overlapping dialogs as the user
        clicks Install on different mods.
        """
        # Dismiss any lingering combobox autocomplete dropdowns —
        # those are topmost Toplevels that would otherwise sit over
        # the install dialog and silently eat drag-press events.
        for closer in getattr(self, "_combo_dropdown_closers", []):
            try:
                closer()
            except Exception:
                pass

        # If an Install dialog is already open, just swap the contents.
        existing = getattr(self, "_install_window", None)
        if existing is not None:
            try:
                if existing.winfo_exists():
                    self._reload_install_dialog(
                        existing, mod_name, slots, mod_id=mod_id,
                        metadata=metadata, installed_path=installed_path)
                    return
            except tk.TclError:
                # Window was destroyed but reference lingered; fall
                # through to building a fresh one.
                pass

        win = tk.Toplevel(self.root)
        win.title(f"Install — {mod_name}")
        win.configure(bg=T.BG)
        win.transient(self.root)

        self._install_window = win
        win.protocol("WM_DELETE_WINDOW",
                     lambda w=win: self._on_install_window_close(w))

        # Title labels (kept as attributes so reload can update them).
        win._smash_title_label = tk.Label(
            win, text=mod_name, bg=T.BG, fg=T.FG,
            font=(T.FONT, T.SZ_LG, "bold"), wraplength=700)
        win._smash_title_label.pack(padx=10, pady=(10, 2))
        win._smash_subtitle_label = tk.Label(
            win, text="", bg=T.BG, fg=T.OVERLAY,
            font=(T.MONO, T.SZ_SM))
        win._smash_subtitle_label.pack(padx=10, pady=(0, 6))

        # Two-column body: source previews on left, destinations on right.
        body = tk.Frame(win, bg=T.BG)
        body.pack(fill="both", expand=True, padx=10, pady=4)

        # ── LEFT: source slot previews (3D rendered, draggable) ──
        left = tk.LabelFrame(body, text="Available", bg=T.BG, fg=T.FG,
                              font=(T.FONT, T.SZ_MD, "bold"))
        left.pack(side="left", fill="both", expand=True, padx=(0, 4))
        win._smash_left_frame = left

        # ── MIDDLE: drag-direction indicator ──
        divider = tk.Frame(body, bg=T.BG, width=44)
        divider.pack(side="left", fill="y", padx=2)
        divider.pack_propagate(False)
        tk.Label(divider, text="drag", bg=T.BG, fg=T.OVERLAY,
                 font=(T.FONT, T.SZ_XS)).pack(pady=(40, 0))
        tk.Label(divider, text="→", bg=T.BG, fg=T.ACCENT,
                 font=(T.FONT, 28, "bold")).pack(pady=(2, 0))
        tk.Label(divider, text="to\ninstall", bg=T.BG, fg=T.OVERLAY,
                 font=(T.FONT, T.SZ_XS), justify="center").pack(pady=(2, 0))

        # ── RIGHT: destination c00–c07 strip ──
        right = tk.LabelFrame(body, text="Installed slots", bg=T.BG, fg=T.FG,
                               font=(T.FONT, T.SZ_MD, "bold"))
        right.pack(side="left", fill="both", expand=True, padx=(4, 0))
        win._smash_right_frame = right

        win._smash_install_ctx = {
            "dest_widgets": {},
            "fighter_int": None,
            "mod_id": None,
            "mod_name": "",
            "metadata": {},
            "installed_path": None,
            "win": win,
            # Bottom-row "Open in ssbh_editor" target updates with each
            # reload so it always points at the currently-displayed mod.
            "first_slot_dir": None,
        }

        # Bottom button row
        bottom = tk.Frame(win, bg=T.BG)
        bottom.pack(fill="x", padx=10, pady=(4, 10))

        ctx = win._smash_install_ctx
        win._smash_open_editor_btn = tk.Button(
            bottom, text="Open in ssbh_editor", width=18,
            bg=T.SURFACE1, fg=T.FG, font=(T.FONT, T.SZ_SM),
            relief="flat", cursor="hand2",
            command=lambda: self._open_ssbh_editor(
                ctx.get("first_slot_dir") or "",
                ctx.get("mod_name") or ""))
        win._smash_open_editor_btn.pack(side="left", padx=(0, 6))
        tk.Button(bottom, text="Validate", width=12,
                  bg=T.SURFACE1, fg=T.FG,
                  font=(T.FONT, T.SZ_SM, "bold"),
                  relief="flat", cursor="hand2",
                  command=lambda: self._show_verify_slots_report(ctx)
                  ).pack(side="left", padx=(0, 6))
        # Save: re-asserts the destination strip as authoritative
        # (drag-drop already writes through, so this is mostly a
        # confirmation), shows the validate report, and closes.
        tk.Button(bottom, text="💾 Save", width=10,
                  bg=T.GREEN, fg=T.BG,
                  font=(T.FONT, T.SZ_SM, "bold"),
                  relief="flat", cursor="hand2",
                  command=lambda: self._save_and_close_install(
                      ctx, win)
                  ).pack(side="right")
        tk.Button(bottom, text="Close", width=8,
                  bg=T.SURFACE1, fg=T.FG,
                  font=(T.FONT, T.SZ_SM),
                  relief="flat", cursor="hand2",
                  command=win.destroy
                  ).pack(side="right", padx=(0, 6))

        # Populate left + right for the first time, then size the window.
        self._reload_install_dialog(
            win, mod_name, slots, mod_id=mod_id,
            metadata=metadata, installed_path=installed_path,
            initial=True)

    def _save_and_close_install(self, ctx, win):
        """User clicked Save in the install dialog. Treat the
        destination strip as the authoritative truth for the active
        profile and write it through. (Drag-drop already writes the
        profile as you drop, so this is mostly an idempotent
        confirmation — but it also strips any phantom entries that
        may have crept in.)

        Shows the slot-map report so the user sees exactly what got
        locked in, then closes the install window.
        """
        target_profile = (self._profile_mode_target
                          or self._active_user_profile)
        fighter_int = ctx.get("fighter_int")
        if not target_profile:
            messagebox.showinfo(
                "Save",
                "No active profile to save into.")
            return
        # Read current destination strip occupancy from the profile.
        # Anything that's NOT in the strip but is in the profile for
        # this fighter is removed (the user has cleared it).
        occ = (self._install_target_occupancy(fighter_int)
               if fighter_int else {})
        # Build the canonical mod_id set from the strip.
        strip_ids = {str(v.get("mod_id"))
                     for v in occ.values() if v.get("mod_id")}
        if fighter_int:
            try:
                profiles = load_profiles()
                profile = profiles.get(target_profile) or {}
                kept = []
                removed = 0
                disp = INTERNAL_TO_DISPLAY.get(fighter_int)
                for m in profile.get("mods", []):
                    if (m.get("mod_type") or "skin") != "skin":
                        kept.append(m); continue
                    char = str(m.get("character", ""))
                    char_int = (FIGHTER_INTERNAL.get(char) or char)
                    if char != disp and char_int != fighter_int:
                        kept.append(m); continue
                    # Keep only entries whose mod_id is in the strip.
                    if str(m.get("mod_id")) in strip_ids:
                        kept.append(m)
                    else:
                        removed += 1
                if removed:
                    profile["mods"] = kept
                    profile["mod_count"] = len(kept)
                    profiles[target_profile] = profile
                    save_profiles(profiles)
                    print(f"  💾 Saved: removed {removed} stale "
                          f"{INTERNAL_TO_DISPLAY.get(fighter_int, fighter_int)} "
                          f"entry/entries from profile "
                          f"'{target_profile}'.")
                else:
                    print(f"  💾 Saved: profile '{target_profile}' "
                          f"already matches destination strip.")
            except Exception as e:
                print(f"  ! Save failed: {e}", file=sys.stderr)
        # Show the slot-map report so the user sees what's locked in.
        self._show_verify_slots_report(ctx)
        # Close after a tick so the report popup gets focus.
        try:
            win.after(50, win.destroy)
        except tk.TclError:
            pass

    def _show_verify_slots_report(self, ctx):
        """Pop up a report of every entry in the active profile,
        grouped by character, showing src → dst slot exactly as
        stored. Lets the user verify their drag-drop assignments
        landed where they wanted.
        """
        target_profile = (self._profile_mode_target
                          or self._active_user_profile)
        if not target_profile:
            messagebox.showinfo("Validate",
                                 "No active profile to validate.")
            return
        profiles = load_profiles()
        profile = profiles.get(target_profile) or {}
        mods = profile.get("mods", [])
        if not mods:
            messagebox.showinfo(
                "Validate",
                f"Profile '{target_profile}' is empty.")
            return

        win = tk.Toplevel(ctx.get("win") or self.root)
        win.title(f"Validate — {target_profile}")
        win.configure(bg=T.SURFACE)
        win.geometry("680x520")

        head = tk.Frame(win, bg=T.SURFACE, padx=12, pady=10)
        head.pack(fill="x")
        tk.Label(head, text=f"Slot map: {target_profile}",
                  bg=T.SURFACE, fg=T.FG,
                  font=(T.FONT, T.SZ_LG, "bold")).pack(anchor="w")
        tk.Label(head,
                  text="Source → Destination per skin, grouped by "
                       "fighter. If anything's wrong, drag it to "
                       "the right slot in the install dialog and "
                       "re-validate.",
                  bg=T.SURFACE, fg=T.OVERLAY,
                  font=(T.FONT, T.SZ_SM),
                  wraplength=640, justify="left").pack(
                      anchor="w", pady=(2, 0))

        # Scrollable body.
        body = tk.Frame(win, bg=T.SURFACE)
        body.pack(fill="both", expand=True, padx=12, pady=(0, 8))
        canvas = tk.Canvas(body, bg=T.SURFACE, highlightthickness=0)
        scroll = tk.Scrollbar(body, orient="vertical",
                               command=canvas.yview)
        inner = tk.Frame(canvas, bg=T.SURFACE)
        inner.bind("<Configure>",
                    lambda _e: canvas.configure(
                        scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=scroll.set)
        canvas.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        # Group by character.
        by_char = {}
        for m in mods:
            if not m.get("enabled", True):
                continue
            if (m.get("mod_type") or "skin") != "skin":
                continue
            ch = m.get("character") or "Other"
            by_char.setdefault(ch, []).append(m)

        if not by_char:
            tk.Label(inner, text="No skin entries.",
                      bg=T.SURFACE, fg=T.OVERLAY,
                      font=(T.FONT, T.SZ_MD)).pack(anchor="w", padx=12,
                                                    pady=20)

        def _slot_key(m):
            s = str(m.get("slot", "")).strip().lower()
            mt = re.match(r"c(\d{2})", s)
            return int(mt.group(1)) if mt else 99

        for ch in sorted(by_char.keys(), key=str.lower):
            entries = sorted(by_char[ch], key=_slot_key)

            # Detect intra-character slot collisions (same dst slot).
            slot_counts = {}
            for e in entries:
                s = str(e.get("slot", "")).strip().lower()
                slot_counts[s] = slot_counts.get(s, 0) + 1

            block = tk.Frame(inner, bg=T.BG, padx=10, pady=6)
            block.pack(fill="x", padx=4, pady=(0, 6))
            tk.Label(block, text=ch, bg=T.BG, fg=T.ACCENT,
                      font=(T.FONT, T.SZ_MD, "bold"),
                      anchor="w").pack(fill="x")
            for e in entries:
                name = (e.get("name") or "?")
                src = e.get("source_slot") or ""
                dst = str(e.get("slot") or "").strip().lower()
                if src and src != dst:
                    arrow = f"{src} → {dst}"
                else:
                    arrow = f"{dst}"
                clash = slot_counts.get(dst, 0) > 1
                clash_tag = "  ⚠ CLASH" if clash else ""
                row_color = T.RED if clash else T.FG
                tk.Label(block,
                          text=f"  {arrow:14}  {name}{clash_tag}",
                          bg=T.BG, fg=row_color,
                          font=(T.MONO, T.SZ_SM),
                          anchor="w", justify="left").pack(
                              fill="x", padx=(8, 0))

        tk.Button(win, text="Close", width=10,
                   bg=T.SURFACE1, fg=T.FG,
                   font=(T.FONT, T.SZ_SM),
                   relief="flat", cursor="hand2",
                   command=win.destroy).pack(pady=(0, 10))

    def _on_install_window_close(self, win):
        """Drop our singleton reference when the user closes the dialog,
        so the next Install click opens a fresh window instead of trying
        to reuse a destroyed one.
        """
        if getattr(self, "_install_window", None) is win:
            self._install_window = None
        win.destroy()

    def _reload_install_dialog(self, win, mod_name, slots, mod_id=None,
                                metadata=None, installed_path=None,
                                initial=False):
        """Rebuild the left (Available) column, refresh the right
        (destination) column for the new fighter, and update titles.

        ``initial`` is True only on the first call from
        :meth:`_show_model_slots_popup`; after that we're just swapping
        contents inside the same Toplevel.
        """
        # Update titles
        win.title(f"Install — {mod_name}")
        win._smash_title_label.configure(text=mod_name)
        win._smash_subtitle_label.configure(
            text=f"{len(slots)} source slot(s) — drag to a destination")

        # Detect target fighter. Try several sources in order of
        # reliability — the source slot's path is best (`.../fighter/<f>/
        # model/body/cXX`), but fails for texture-only mods whose
        # extracted layout falls back to the root content folder.
        # Without a fighter the destination strip can't render and the
        # drop-time character stamp can't fire, so we cast a wider net.
        fighter_int = None
        # 1. Source slot path
        if slots:
            try:
                parts = slots[0][1].replace("\\", "/").split("/")
                if "fighter" in parts:
                    fi = parts.index("fighter")
                    if fi + 1 < len(parts):
                        cand = parts[fi + 1]
                        if cand in INTERNAL_TO_DISPLAY:
                            fighter_int = cand
            except Exception:
                pass
        # 2. Walk all source slot dirs for any `fighter/<f>/` parent
        if not fighter_int and slots:
            for _, sdir in slots:
                try:
                    norm = sdir.replace("\\", "/").split("/")
                    if "fighter" in norm:
                        fi = norm.index("fighter")
                        if fi + 1 < len(norm):
                            cand = norm[fi + 1]
                            if cand in INTERNAL_TO_DISPLAY:
                                fighter_int = cand
                                break
                except Exception:
                    continue
        # 3. Walk the extracted/installed root for a fighter/<f>/ dir
        if not fighter_int:
            walk_root = installed_path
            if not walk_root and mod_id:
                cand_root = os.path.join(MOD_CACHE_DIR, str(mod_id),
                                          "extracted")
                if os.path.isdir(cand_root):
                    walk_root = cand_root
            if walk_root and os.path.isdir(walk_root):
                try:
                    for r, dirs, _files in os.walk(walk_root):
                        if "fighter" in dirs:
                            fdir = os.path.join(r, "fighter")
                            try:
                                for child in os.listdir(fdir):
                                    if (os.path.isdir(
                                            os.path.join(fdir, child))
                                            and child in INTERNAL_TO_DISPLAY):
                                        fighter_int = child
                                        break
                            except OSError:
                                pass
                            if fighter_int:
                                break
                except OSError:
                    pass
        # 4. Metadata's category_id (GameBanana subcategory == fighter)
        if not fighter_int and metadata:
            guessed = _guess_character_from_meta(metadata)
            if guessed and guessed in FIGHTER_INTERNAL:
                fighter_int = FIGHTER_INTERNAL[guessed]

        THUMB_W, THUMB_H = 280, 160
        DEST_COLS = 2
        SRC_COLS = 2

        # ── Wipe and rebuild LEFT column ──
        left = win._smash_left_frame
        for child in left.winfo_children():
            child.destroy()

        thumb_labels = []
        src_hover_url = (metadata or {}).get("thumb_url") or ""
        for idx, (slot_label, slot_dir) in enumerate(slots):
            row, col = divmod(idx, SRC_COLS)
            cell = tk.Frame(left, bg=T.SURFACE, relief="ridge", bd=1,
                            cursor="fleur")
            cell.grid(row=row, column=col, padx=4, pady=4, sticky="n")
            self._build_install_cell(
                cell, slot_label, mod_name, model_dir=slot_dir,
                thumb_w=THUMB_W, thumb_h=THUMB_H,
                drag_source=True,
                hover_thumb_url=src_hover_url,
                hover_thumb_key=f"hover_src_{mod_id}_{slot_label}")
            thumb_labels.append((slot_dir, cell._smash_thumb_label))

        # Update ctx for new mod (must happen BEFORE rebuilding right
        # so destination cells capture the right mod_id for hover state).
        ctx = win._smash_install_ctx
        ctx["fighter_int"] = fighter_int
        ctx["mod_id"] = mod_id
        ctx["mod_name"] = mod_name
        ctx["metadata"] = metadata or {}
        ctx["installed_path"] = installed_path
        ctx["first_slot_dir"] = slots[0][1] if slots else None

        # ── Wipe and rebuild RIGHT column ──
        right = win._smash_right_frame
        for child in right.winfo_children():
            child.destroy()
        dest_widgets = {}
        ctx["dest_widgets"] = dest_widgets

        occupied = self._install_target_occupancy(fighter_int)
        dest_render_queue = []
        for i in range(8):
            slot_key = f"c{i:02d}"
            row, col = divmod(i, DEST_COLS)
            box = tk.Frame(right, bg=T.SURFACE, relief="ridge", bd=2)
            box.grid(row=row, column=col, padx=4, pady=4, sticky="n")
            render_label = self._build_dest_slot_contents(
                box, slot_key, occupied.get(slot_key),
                fighter_int, THUMB_W, THUMB_H, ctx=ctx)
            if render_label is not None:
                dest_render_queue.append(render_label)
            dest_widgets[slot_key] = box

        if initial:
            # First-time sizing: snap window to natural size of contents.
            win.update_idletasks()
            natural_w = win.winfo_reqwidth() + 16
            natural_h = win.winfo_reqheight() + 16
            screen_w = self.root.winfo_screenwidth()
            screen_h = self.root.winfo_screenheight()
            max_w = min(natural_w, screen_w - 40)
            max_h = min(natural_h, screen_h - 80)
            win.geometry(f"{max_w}x{max_h}")
            x = (self.root.winfo_rootx()
                 + (self.root.winfo_width() - max_w) // 2)
            y = (self.root.winfo_rooty()
                 + (self.root.winfo_height() - max_h) // 2)
            win.geometry(f"+{max(0,x)}+{max(0,y)}")
        else:
            # Reload: bring the (already-sized) window back to the front
            # so the user sees the swap happened.
            try:
                win.lift()
                win.focus_set()
            except tk.TclError:
                pass

        # Kick off sequential 3D rendering — first the source slots
        # (left), then the destination installed mods (right).
        self._render_slots_sequential(thumb_labels + dest_render_queue)

    def _bind_drag_source(self, widgets, slot_label, slot_dir,
                           kind="source", thumb_label=None):
        """Wire mouse-press/motion/release on ``widgets`` to act as a
        drag source.

        ``kind`` selects the drop semantics:
          • ``"source"`` — drag from the mod's source slot into a
            destination cell to install (existing behavior).
          • ``"dest"``   — drag a destination's slot onto another
            destination to swap (or move to an empty slot) within the
            active profile.

        The drag ghost is built from the source label's currently
        applied image when one is available so the user sees the
        actual model thumbnail follow the cursor; otherwise we fall
        back to a tiny accent-coloured chip with the slot text.
        """

        def _cleanup_drag(top):
            """Release any pointer grab and clear the in-flight state.
            Safe to call repeatedly — covers all the corner cases
            where a drag aborts mid-flight (window destroyed, error
            in handler, alt-tab, etc.)."""
            drag = getattr(top, "_smash_drag", None)
            if drag:
                ghost = drag.get("ghost")
                if ghost is not None:
                    try:
                        ghost.destroy()
                    except tk.TclError:
                        pass
                grabber = drag.get("grabber")
                if grabber is not None:
                    try:
                        grabber.grab_release()
                    except tk.TclError:
                        pass
                top._smash_drag = None
            ctx = getattr(top, "_smash_install_ctx", None)
            if ctx:
                for box in ctx["dest_widgets"].values():
                    try:
                        box.configure(bg=T.SURFACE)
                    except tk.TclError:
                        pass

        def on_press(event):
            top = event.widget.winfo_toplevel()
            # If a previous drag left state behind (e.g. release fired
            # over the ghost toplevel and we never saw it), clear it
            # before starting a new one.
            if getattr(top, "_smash_drag", None) is not None:
                _cleanup_drag(top)

            ghost = tk.Toplevel(top)
            ghost.overrideredirect(True)
            ghost.attributes("-topmost", True)
            ghost.attributes("-alpha", 0.85)
            # Tk on Windows won't make a window mouse-transparent, but
            # we can avoid most "release-on-ghost" misfires by routing
            # all subsequent pointer events through a grab on the
            # source widget (set below).
            ghost.configure(bg=T.ACCENT)

            ghost_img = (getattr(thumb_label, "_smash_drag_image", None)
                         if thumb_label is not None else None)
            if ghost_img is not None:
                tk.Label(ghost, image=ghost_img, bg=T.SURFACE1,
                         bd=2, relief="solid",
                         highlightthickness=2,
                         highlightbackground=T.ACCENT).pack()
            else:
                tk.Label(ghost, text=f"→ {slot_label.upper()}",
                         bg=T.ACCENT, fg=T.BG,
                         font=(T.FONT, T.SZ_MD, "bold"),
                         padx=10, pady=4).pack()
            ghost.geometry(f"+{event.x_root + 12}+{event.y_root + 8}")

            # Grab pointer events to the source widget so motion +
            # release always come back to us, even when the ghost
            # window is technically under the cursor.
            grabber = event.widget
            try:
                grabber.grab_set()
            except tk.TclError:
                grabber = None

            top._smash_drag = {
                "kind": kind,
                "src_slot": slot_label,
                "slot_dir": slot_dir,
                "ghost": ghost,
                "grabber": grabber,
            }

        def on_motion(event):
            top = event.widget.winfo_toplevel()
            drag = getattr(top, "_smash_drag", None)
            if not drag:
                return
            ghost = drag.get("ghost")
            if ghost is not None:
                try:
                    ghost.geometry(
                        f"+{event.x_root + 12}+{event.y_root + 8}")
                except tk.TclError:
                    pass
            ctx = getattr(top, "_smash_install_ctx", None)
            if ctx:
                target = self._dest_under_cursor(top, ctx,
                                                  event.x_root, event.y_root)
                for slot, box in ctx["dest_widgets"].items():
                    try:
                        box.configure(
                            bg=T.ACCENT if slot == target else T.SURFACE)
                    except tk.TclError:
                        pass

        def on_release(event):
            top = event.widget.winfo_toplevel()
            drag = getattr(top, "_smash_drag", None)
            if not drag:
                return
            x_root, y_root = event.x_root, event.y_root
            # Tear down ghost + grab + state BEFORE running drop
            # handlers so a handler exception (or refresh that
            # destroys the cells) doesn't leave us mid-drag.
            _cleanup_drag(top)
            ctx = getattr(top, "_smash_install_ctx", None)
            if not ctx:
                return
            target = self._dest_under_cursor(top, ctx, x_root, y_root)
            if target is None:
                return
            if kind == "source":
                self._handle_install_drop(ctx, slot_label, target)
            else:
                self._handle_dest_swap(ctx, slot_label, target)

        for w in widgets:
            w.bind("<ButtonPress-1>", on_press, add="+")
            w.bind("<B1-Motion>", on_motion, add="+")
            w.bind("<ButtonRelease-1>", on_release, add="+")

    def _handle_dest_swap(self, ctx, src_slot, target_slot):
        """Drag-drop within the destination strip. Move (target empty)
        or swap (target occupied) within the active profile.
        """
        if src_slot == target_slot:
            return
        target_profile = (self._profile_mode_target
                          or self._active_user_profile)
        fighter_int = ctx.get("fighter_int")
        if not target_profile or not fighter_int:
            return

        profiles = load_profiles()
        profile = profiles.get(target_profile)
        if not profile:
            return
        display = INTERNAL_TO_DISPLAY.get(fighter_int)
        src_lc = src_slot.lower()
        tgt_lc = target_slot.lower()
        # Iterate and adjust slot strings on entries whose character
        # matches the fighter under view. Multi-slot entries are
        # split: only the dragged variant is reslotted.
        new_mods = []
        moved = False
        # Identify the entry currently at target_slot first so we can
        # SWAP it back to src_slot in a single pass.
        for m in profile.get("mods", []):
            if m.get("mod_type", "skin") != "skin":
                new_mods.append(m); continue
            char = str(m.get("character", ""))
            char_int = FIGHTER_INTERNAL.get(char) or char
            if char != display and char_int != fighter_int:
                new_mods.append(m); continue
            slots_in_entry = [s.strip().lower() for s in
                              str(m.get("slot", ""))
                              .replace(",", " ").split()
                              if s.strip()]

            def _replace(slots, a, b):
                return [b if s == a else s for s in slots]

            had_src = src_lc in slots_in_entry
            had_tgt = tgt_lc in slots_in_entry
            if had_src and had_tgt:
                # Same entry covers both — swap is a no-op semantically.
                new_mods.append(m); continue
            if had_src:
                # Move the dragged slot to target. (Swap step below
                # handles any other entry sitting at target.)
                if len(slots_in_entry) == 1:
                    nm = dict(m)
                    nm["slot"] = tgt_lc
                    if nm.get("source_slot"):
                        nm["source_slot"] = nm["source_slot"]  # unchanged
                    new_mods.append(nm)
                else:
                    # Multi-slot entry — split: keep the original
                    # entry without src_lc, add a fresh entry at
                    # tgt_lc that retains origin info.
                    keep = dict(m)
                    keep["slot"] = " ".join(s for s in slots_in_entry
                                              if s != src_lc)
                    new_mods.append(keep)
                    moved_entry = dict(m)
                    moved_entry["slot"] = tgt_lc
                    new_mods.append(moved_entry)
                moved = True
            elif had_tgt:
                # Will be redirected to src_lc as the swap counterpart.
                if len(slots_in_entry) == 1:
                    nm = dict(m)
                    nm["slot"] = src_lc
                    new_mods.append(nm)
                else:
                    keep = dict(m)
                    keep["slot"] = " ".join(s for s in slots_in_entry
                                              if s != tgt_lc)
                    new_mods.append(keep)
                    swapped = dict(m)
                    swapped["slot"] = src_lc
                    new_mods.append(swapped)
            else:
                new_mods.append(m)

        if not moved:
            return
        profile["mods"] = new_mods
        profile["mod_count"] = len(new_mods)
        profiles[target_profile] = profile
        save_profiles(profiles)
        print(f"  {src_slot} ↔ {target_slot} in profile "
              f"'{target_profile}' (fighter {fighter_int})")
        self._refresh_install_destinations(ctx)

    def _dest_under_cursor(self, top, ctx, x_root, y_root):
        """Return the destination slot string (``c01``...) under the
        absolute screen coordinates, or ``None`` if not over a slot.
        Walks the widget tree because the cursor may be on a child
        (label) of the box we care about.
        """
        target_widget = top.winfo_containing(x_root, y_root)
        if target_widget is None:
            return None
        # Find which dest box (if any) contains the target widget.
        boxes = ctx["dest_widgets"]
        cur = target_widget
        # Walk up parents until we hit one in dest_widgets (or root).
        while cur is not None:
            for slot, box in boxes.items():
                if cur is box:
                    return slot
            try:
                cur = cur.master
            except Exception:
                break
        return None

    def _handle_install_drop(self, ctx, src_slot, target_slot):
        """User dropped a source slot onto a destination. Route to the
        active profile (the standard install path in this app) when
        one is selected; fall through to direct SD install otherwise.
        """
        mod_id = ctx.get("mod_id")
        mod_name = ctx.get("mod_name", "")
        metadata = dict(ctx.get("metadata") or {})

        if mod_id is None and ctx.get("installed_path"):
            messagebox.showinfo(
                "Install",
                "Re-slotting an already-installed mod is not yet wired "
                "up — drop directly from the Available column on the "
                "left into a destination slot for fresh installs.")
            return

        if not mod_id:
            messagebox.showerror("Install", "Missing mod identifier.")
            return

        # Stamp the source slot into metadata so when this profile
        # entry is later loaded to SD, only the dragged variant gets
        # remapped (not all source slots auto-resolved).
        metadata.setdefault("source_slot", src_slot)

        target_profile = self._profile_mode_target or self._active_user_profile
        fighter_int = ctx.get("fighter_int")

        # Always stamp the character from the install dialog's
        # detected fighter (extracted from the source slot path:
        # .../fighter/<f>/model/body/cXX). This is more reliable than
        # ``_guess_character_from_meta``, which fails for mods whose
        # GameBanana category was stripped (e.g. when navigating from
        # the Adult Only audit view) AND whose name doesn't include
        # the fighter — many adult mods like "Bunny Suit Pyra and
        # Mythra" would otherwise end up with character="Other" and
        # silently disappear from the destination strip after install.
        if fighter_int:
            metadata["character"] = INTERNAL_TO_DISPLAY.get(
                fighter_int, metadata.get("character") or "Other")

        # No active profile? Prompt to create one inline before installing,
        # so drag-drop installs always end up in *some* profile rather than
        # silently going straight to the SD card.
        if not target_profile:
            target_profile = self._prompt_create_profile_inline(
                parent=ctx.get("win"))
            if not target_profile:
                return  # user cancelled

        # Detect overwrite: is the destination slot already occupied?
        # We don't ask — drag-drop just commits. Removal still
        # confirms because it can't be undone via another drag.
        existing_occ = self._install_target_occupancy(fighter_int).get(
            target_slot) if fighter_int else None

        def _do():
            try:
                # Overwrite: remove the existing occupant of the target
                # slot first so the new entry takes its slot cleanly.
                if existing_occ:
                    remove_profile_slot(target_profile, fighter_int,
                                        target_slot)
                # Move semantics: if this exact (mod_id, source_slot)
                # is already in the profile at SOME OTHER slot, remove
                # those entries too. Without this, dragging the same
                # source variant to a new target leaves the old entry
                # in place and you end up with the same skin at both
                # slots — which is rarely what users want and is what
                # was producing "duplicates" in the profile view.
                _remove_matching_profile_entries(
                    target_profile, mod_id,
                    src_slot.lower(), exclude_slot=target_slot.lower())
                self._do_install_to_profile(
                    mod_id, mod_name,
                    metadata=metadata,
                    target_slot=target_slot,
                    profile_name=target_profile)
            except Exception as e:
                print(f"  install drop error: {e}", file=sys.stderr)
                self.root.after(0, lambda:
                    messagebox.showerror("Install failed", str(e),
                                         parent=ctx["win"]))
                return
            self.root.after(0, lambda: self._refresh_install_destinations(ctx))

        threading.Thread(target=_do, daemon=True).start()

    def _prompt_create_profile_inline(self, parent=None):
        """Modal mini-dialog: ask the user to name a new profile, create
        it empty, set it as the active install target, and return its
        name. Returns None if the user cancels.

        This is the lightweight path the Install dialog uses when there's
        no active profile to drop into — full template/plugin choices
        can still be edited later in Manage Profiles.
        """
        existing = load_profiles()
        base = "New Profile"
        default_name = base
        i = 2
        while default_name in existing:
            default_name = f"{base} {i}"
            i += 1

        win = tk.Toplevel(parent or self.root)
        win.title("Create Profile to Install Into")
        win.configure(bg=T.SURFACE)
        win.transient(parent or self.root)
        win.grab_set()
        win.geometry("420x180")

        result = {"name": None}

        body = tk.Frame(win, bg=T.SURFACE)
        body.pack(fill="both", expand=True, padx=14, pady=12)

        tk.Label(body,
                 text="No active profile — name a new one to install into:",
                 bg=T.SURFACE, fg=T.FG,
                 font=(T.FONT, T.SZ_MD), wraplength=380).pack(
                     anchor="w", pady=(0, 8))

        name_var = tk.StringVar(value=default_name)
        entry = tk.Entry(body, textvariable=name_var,
                         bg=T.CRUST, fg=T.FG, insertbackground=T.FG,
                         font=(T.FONT, T.SZ_MD))
        entry.pack(fill="x", pady=(0, 6))
        entry.focus_set()
        entry.select_range(0, "end")

        tk.Label(body,
                 text="Starts empty. You can edit template/plugins/wifi-safe "
                      "later in Manage Profiles.",
                 bg=T.SURFACE, fg=T.OVERLAY,
                 font=(T.FONT, T.SZ_XS), wraplength=380,
                 justify="left").pack(anchor="w")

        def _ok(_e=None):
            name = name_var.get().strip()
            if not name:
                messagebox.showwarning("Name Required",
                                       "Profile name cannot be empty.",
                                       parent=win)
                return
            profiles = load_profiles()
            if name in profiles:
                if not messagebox.askyesno(
                        "Profile Exists",
                        f"Profile '{name}' already exists. Use it as the "
                        "active install target (without overwriting)?",
                        parent=win):
                    return
            else:
                profiles[name] = {
                    "created": datetime.now().isoformat(),
                    "mod_count": 0,
                    "mods": [],
                    "template": "Custom",
                    "wifi_safe": True,
                    "unofficial_atmo": True,
                    "plugins": [nro for nro in KNOWN_PLUGINS
                                if nro not in CORE_PLUGINS],
                }
                save_profiles(profiles)
                print(f"\n=== Created profile '{name}' (empty) ===\n")
            self._active_user_profile = name
            self._profile_mode_target = name
            try:
                self._refresh_profile_list_silent()
                self._profile_list_var.set(name)
            except Exception:
                pass
            print(f"  Active install target: '{name}'")
            result["name"] = name
            win.destroy()

        def _cancel(_e=None):
            win.destroy()

        btn_row = tk.Frame(win, bg=T.SURFACE)
        btn_row.pack(fill="x", padx=14, pady=(0, 12))
        tk.Button(btn_row, text="Create & Install", width=16,
                  bg=T.ACCENT, fg=T.BG,
                  font=(T.FONT, T.SZ_MD, "bold"), relief="flat",
                  cursor="hand2",
                  command=_ok).pack(side="right", padx=(6, 0))
        tk.Button(btn_row, text="Cancel", width=10,
                  bg=T.SURFACE1, fg=T.FG,
                  font=(T.FONT, T.SZ_MD), relief="flat", cursor="hand2",
                  command=_cancel).pack(side="right")

        entry.bind("<Return>", _ok)
        entry.bind("<Escape>", _cancel)

        # Modal wait — don't return until the user closes the dialog.
        self.root.wait_window(win)
        return result["name"]

    def _refresh_install_destinations(self, ctx):
        """After an install completes, rebuild the destination strip
        in place so the user sees what's now installed.
        """
        fighter_int = ctx.get("fighter_int")
        if not fighter_int:
            return
        occupied = self._install_target_occupancy(fighter_int)
        queue = []
        for slot_key, box in ctx["dest_widgets"].items():
            entry = self._build_dest_slot_contents(
                box, slot_key, occupied.get(slot_key),
                fighter_int, 280, 160, ctx=ctx)
            if entry is not None:
                queue.append(entry)
        if queue:
            self._render_slots_sequential(queue)

    def _render_slots_sequential(self, slot_list):
        """Render all slots one-by-one in a single background thread.
        *slot_list* is a list of ``(model_dir, tk.Label)`` pairs."""
        def _safe_configure(label, **kw):
            # The owning dialog may have been closed between the time
            # we scheduled this callback and the time it ran. Bail out
            # silently in that case so a stale render doesn't raise a
            # TclError into the Tk callback handler.
            try:
                if not label.winfo_exists():
                    return
                label.configure(**kw)
            except tk.TclError:
                pass

        def _work():
            for model_dir, label in slot_list:
                self.root.after(
                    0, lambda l=label: _safe_configure(l, text="Rendering…"))
                try:
                    img = render_model_preview(model_dir, 280, 160)
                except Exception as exc:
                    print(f"  Render error for {model_dir}: {exc}",
                          file=sys.stderr)
                    img = None
                if img is None:
                    self.root.after(
                        0, lambda l=label: _safe_configure(
                            l, text="No preview", fg=T.RED))
                else:
                    self.root.after(
                        0, lambda l=label, i=img: self._apply_thumb(l, i))
        threading.Thread(target=_work, daemon=True).start()

    def _apply_thumb(self, label, pil_img):
        """Put a rendered PIL image into a tk.Label.

        No-ops if the label's containing window has already been
        destroyed (common when an install dialog closes before the
        background thumbnail render finishes)."""
        try:
            if not label.winfo_exists():
                return
        except tk.TclError:
            return
        try:
            photo = ImageTk.PhotoImage(pil_img)
            label.configure(image=photo, text="",
                            width=pil_img.width, height=pil_img.height)
            label._photo = photo
            label._smash_drag_image = photo  # ghost source for drag-drop
        except tk.TclError:
            # Widget was destroyed mid-configure, or PhotoImage couldn't
            # bind to a dead interpreter. Either way nothing to recover.
            return
        except Exception:
            try:
                if label.winfo_exists():
                    label.configure(text="Render error", fg=T.RED)
            except tk.TclError:
                pass

    def _launch_interactive_viewer(self, model_dir):
        """Open the interactive 3D viewer in a separate process.

        pyrender.Viewer uses pyglet, which (a) pins its event loop to
        the thread that imports pyglet.app and (b) needs a fresh OpenGL
        context. Running it in-process alongside Tkinter+OffscreenRenderer
        deadlocks or fails with `wglChoosePixelFormatARB is not exported`,
        so we spawn ``python smash_night.py --view-model <path>`` instead.
        That subprocess has its own clean GL state and main thread.
        """
        if not os.path.isdir(model_dir):
            messagebox.showerror("Viewer", f"Model folder not found:\n{model_dir}")
            return
        print(f"  Launching interactive viewer for: {model_dir}")
        import subprocess as _sp
        # Prefer pythonw so no extra console window appears alongside the viewer.
        py_exe = sys.executable
        pyw = os.path.join(os.path.dirname(py_exe), "pythonw.exe")
        if os.path.isfile(pyw):
            py_exe = pyw
        try:
            _sp.Popen([py_exe, os.path.abspath(__file__),
                       "--view-model", model_dir],
                      cwd=SCRIPT_DIR)
        except Exception as e:
            messagebox.showerror("Viewer",
                                 f"Could not launch viewer subprocess:\n{e}")

    def _open_ssbh_editor(self, model_dir, mod_name):
        """Launch ssbh_editor externally with Explorer pointing at the folder."""
        exe = _ensure_ssbh_editor()
        if not exe:
            messagebox.showerror(
                "ssbh_editor not found",
                "Could not find or download ssbh_editor.\n\n"
                "Download it manually from:\n"
                "github.com/ScanMountGoat/ssbh_editor/releases\n\n"
                f"and place the folder at:\n{SSBH_EDITOR_DIR}")
            return
        print(f"  Launching ssbh_editor for: {model_dir}")
        import subprocess as _sp
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(model_dir)
            _sp.Popen([exe], cwd=os.path.dirname(exe))
            _sp.Popen(["explorer", "/select,", model_dir])
            messagebox.showinfo(
                "View Model",
                f"ssbh_editor is opening.\n\n"
                f"Drag the highlighted folder from Explorer "
                f"into ssbh_editor, or use File → Open Folder "
                f"and paste the path (already copied to clipboard).\n\n"
                f"{os.path.basename(model_dir)}")
        except Exception as e:
            messagebox.showerror("Launch failed",
                                 f"Could not start ssbh_editor:\n{e}")

    def _delete_profile(self, profile_name, card_widget):
        """Delete a saved profile after confirmation."""
        if not messagebox.askyesno("Delete Profile",
                                   f"Delete profile '{profile_name}'?\n"
                                   "This only removes the saved profile, not "
                                   "any installed mods."):
            return
        delete_profile(profile_name)
        card_widget.destroy()
        profiles = load_profiles()
        if not isinstance(profiles, dict):
            print("  Warning: profile data is not a dict; resetting view.", file=sys.stderr)
            profiles = {}
        self.results_label.configure(text=f"{len(profiles)} profile(s)")
        # If the deleted profile was the active install target, clear it
        # so the next drag-drop install doesn't silently auto-recreate it
        # (add_mod_to_profile creates missing profiles by design).
        was_active = getattr(self, "_active_user_profile", None) == profile_name
        if was_active:
            self._active_user_profile = None
            print(f"  Cleared active install target (was '{profile_name}')")
        if getattr(self, "_profile_mode_target", None) == profile_name:
            self._profile_mode_target = None
        # Force the combo's selected value off the deleted name, then
        # refresh the values list. _refresh_profile_list_silent would
        # otherwise auto-select the first remaining profile when current
        # is missing — we want a deletion to leave the dropdown blank
        # (user must consciously pick a new active profile).
        try:
            if self._profile_list_var.get() == profile_name or was_active:
                self._profile_list_var.set("")
                self._profile_mode_target = None
            remaining = sorted(profiles.keys())
            self._profile_combo.configure(values=remaining)
            self._recolor_all_slot_pickers()
        except Exception:
            pass
        print(f"  Deleted profile '{profile_name}'")

    def _update_profile(self, profile_name):
        """Re-snapshot current installed mods into an existing profile."""
        if not os.path.exists(ARCROPOLIS_MODS):
            messagebox.showwarning("No SD", "SD card mods folder not found.")
            return
        skins = list_installed_skins()
        if not skins:
            messagebox.showinfo("Empty", "No mods installed on SD.")
            return

        profiles = load_profiles()
        old = profiles.get(profile_name, {})
        old_count = old.get("mod_count", 0)

        if not messagebox.askyesno(
                "Update Profile",
                f"Replace profile '{profile_name}' ({old_count} mods) with "
                f"current SD contents ({len(skins)} mods)?"):
            return

        count = create_profile_from_installed(profile_name)
        print(f"\n=== Updated profile '{profile_name}': "
              f"{old_count} -> {count} mod(s) ===\n")
        messagebox.showinfo("Profile Updated",
                            f"'{profile_name}' updated to {count} mod(s).")
        self._show_profiles()

    def _load_profile(self, profile_name):
        """Load a saved profile onto the SD card — downloads and installs each mod."""
        profiles = load_profiles()
        profile_data = profiles.get(profile_name)
        if not profile_data:
            messagebox.showerror("Error", f"Profile '{profile_name}' not found.")
            return

        mods = profile_data.get("mods", [])
        # Disabled mods stay in the profile but are excluded from the
        # install set. They'll be uninstalled from the SD by the delta
        # sync (since their mod_id won't appear in profile_ids).
        active = [m for m in mods if m.get("enabled", True)]
        disabled = [m for m in mods if not m.get("enabled", True)]
        downloadable = [m for m in active if m.get("mod_id")]
        manual = [m for m in active if not m.get("mod_id")]

        cfg = profile_config(profile_data)

        # Wifi-safe profiles refuse to load any gameplay-affecting mods so
        # the player can't accidentally desync online matches.
        if cfg["wifi_safe"]:
            unsafe = [m for m in active
                      if (m.get("mod_type") or "").lower()
                      in WIFI_UNSAFE_MOD_TYPES]
            if unsafe:
                names = "\n".join(
                    f"  • {m.get('name', '?')}  "
                    f"({m.get('mod_type', '?')})"
                    for m in unsafe[:8])
                more = ("\n  …and "
                        f"{len(unsafe) - 8} more" if len(unsafe) > 8 else "")
                messagebox.showerror(
                    "Wifi-Safe Profile Blocked",
                    f"Profile '{profile_name}' is marked WIFI-SAFE but "
                    f"contains {len(unsafe)} gameplay-affecting mod(s):\n\n"
                    f"{names}{more}\n\n"
                    "Remove these mods from the profile, or uncheck "
                    "Wifi-Safe in the profile's Setup → Manage Profiles "
                    "entry, then try again.")
                return

        # Pre-load validation: auto-fix slot collisions and offer to
        # deep-verify multi-model fighters before we touch the SD.
        # Returns False if the user backed out at a freeze prompt.
        print(f"\n=== Pre-load validate: '{profile_name}' ===")
        if not self._validate_profile(profile_name, pre_load=True):
            print("  Aborted by user at freeze-risk prompt.")
            return
        # Profile may have been mutated (slot reassignments, disabled
        # broken Plant skins) — reload from disk before computing the
        # install set.
        profiles = load_profiles()
        profile_data = profiles.get(profile_name) or profile_data
        mods = profile_data.get("mods", [])
        active = [m for m in mods if m.get("enabled", True)]
        disabled = [m for m in mods if not m.get("enabled", True)]
        downloadable = [m for m in active if m.get("mod_id")]
        manual = [m for m in active if not m.get("mod_id")]

        # Pre-flight cache check: count mods that aren't in the local
        # cache. These will need to be downloaded during the load.
        # Cached mods install offline (extracted tree → SD copy).
        cached = 0
        uncached_mods = []
        for m in downloadable:
            mid = m.get("mod_id")
            if not mid:
                continue
            ext = os.path.join(MOD_CACHE_DIR, str(mid), "extracted")
            if os.path.isdir(ext) and os.listdir(ext):
                cached += 1
            else:
                uncached_mods.append(m)

        msg = (f"Sync profile '{profile_name}' to SD card?\n\n"
               f"  • Mods already installed will be kept as-is.\n"
               f"  • SD mods that aren't in this profile will be removed.\n"
               f"  • Manual / hand-copied mod folders are NOT touched.\n\n"
               f"  {len(downloadable)} mod(s) tracked in this profile\n"
               f"  ✓ {cached} cached locally — will install offline\n")
        if uncached_mods:
            msg += (f"  ⬇ {len(uncached_mods)} need to download "
                    f"(network required)\n")
            preview = "\n".join(f"      • {m.get('name', '?')}"
                                 for m in uncached_mods[:5])
            msg += preview + "\n"
            if len(uncached_mods) > 5:
                msg += f"      …and {len(uncached_mods) - 5} more\n"
        if manual:
            msg += f"  {len(manual)} mod(s) have no GameBanana ID (manual install needed)\n"
        if disabled:
            msg += f"  {len(disabled)} mod(s) disabled — will be uninstalled from SD if present\n"
        msg += "\nProceed?"

        if not messagebox.askyesno("Load Profile", msg):
            return

        self._run_async(self._do_load_profile, profile_name, downloadable, manual)

    def _wipe_arcropolis_mods(self):
        """Delete every mod folder under ``ARCROPOLIS_MODS`` and clear the
        ARCropolis cache so the next boot picks up the new layout cleanly.

        CFW base files live OUTSIDE this directory (``atmosphere/``,
        ``bootloader/``, ``switch/``, ``Nintendo/``…) and are never touched.
        """
        import shutil as _sh
        removed_mods = 0
        if os.path.isdir(ARCROPOLIS_MODS):
            for entry in os.listdir(ARCROPOLIS_MODS):
                full = os.path.join(ARCROPOLIS_MODS, entry)
                try:
                    if os.path.isdir(full):
                        _sh.rmtree(full, ignore_errors=True)
                        removed_mods += 1
                    elif os.path.isfile(full):
                        os.remove(full)
                except Exception as e:
                    print(f"    ! Could not remove {entry}: {e}")
            print(f"    Removed {removed_mods} mod folder(s) from "
                  f"{ARCROPOLIS_MODS}")
        else:
            os.makedirs(ARCROPOLIS_MODS, exist_ok=True)
            print(f"    Created empty {ARCROPOLIS_MODS}")

        # Cache + romfs metadata get stale when mods change — clear them too.
        try:
            self._do_clear_cache()
        except Exception as e:
            print(f"    ! Cache clear failed: {e}")

        return removed_mods

    def _post_install_sanity(self, dest, mod_name):
        """Run :func:`sanity_check_install` and log what got cleaned up.

        Best-effort — sanity issues are logged but never abort the
        install, since *something* on the SD is still better than
        nothing for the player.
        """
        try:
            report = sanity_check_install(dest)
        except Exception as e:
            print(f"  ! Sanity check failed for {mod_name}: {e}")
            return
        if report["deleted_dirs"]:
            print(f"  ⚠ Removed {len(report['deleted_dirs'])} empty body "
                  f"slot(s) (would crash on load):")
            for d in report["deleted_dirs"][:6]:
                print(f"      {d}")
            if len(report['deleted_dirs']) > 6:
                print(f"      …and {len(report['deleted_dirs']) - 6} more")
        if report["deleted_files"]:
            print(f"  ⚠ Removed {len(report['deleted_files'])} orphan UI "
                  f"portrait(s) (no matching body slot):")
            for f in report["deleted_files"][:6]:
                print(f"      {f}")
            if len(report['deleted_files']) > 6:
                print(f"      …and {len(report['deleted_files']) - 6} more")
        # Warnings are intentionally quiet — body-only edits are common
        # and not actually broken. Only show under verbose if needed.

    def _scan_mod_slot_set(self, folder_name):
        """Return the set of ``cXX`` slots a mod folder currently
        occupies on the SD card.

        We scan two places: any ``cXX`` directory under
        ``fighter/<f>/<category>/`` and any slot-bound filename pattern
        like ``chara_*_<f>_NN.bntx`` or ``..._cNN.nus3audio``. This
        gives us a reliable answer even for mods whose folder name
        doesn't embed slot info (or embeds it incorrectly).
        """
        slots = set()
        if not folder_name:
            return slots
        root = os.path.join(ARCROPOLIS_MODS, folder_name)
        if not os.path.isdir(root):
            return slots
        slot_re = re.compile(r"^c(\d{2})$", re.IGNORECASE)
        # Slot-bound filename patterns we care about (UI bntx + sound)
        fname_re = re.compile(
            r"_(c?\d{2})\.(bntx|nus3audio)$", re.IGNORECASE)
        for dirpath, dirnames, filenames in os.walk(root):
            for d in dirnames:
                m = slot_re.match(d)
                if m:
                    slots.add(f"c{int(m.group(1)):02d}")
            for f in filenames:
                m = fname_re.search(f)
                if m:
                    tok = m.group(1).lstrip("c")
                    if tok.isdigit():
                        slots.add(f"c{int(tok):02d}")
        return slots

    def _scan_installed_mod_ids(self):
        """Walk ``ARCROPOLIS_MODS`` and return ``{mod_id: folder_name}``
        for every installed mod folder that has a ``.gb_meta.json`` with
        a ``mod_id``. Folders without metadata (manual installs, hand-
        copied mods) are returned under the special key ``None`` as a
        list so callers can decide whether to leave them alone.
        """
        out = {}
        manual = []
        if not os.path.isdir(ARCROPOLIS_MODS):
            return out, manual
        for entry in os.listdir(ARCROPOLIS_MODS):
            full = os.path.join(ARCROPOLIS_MODS, entry)
            if not os.path.isdir(full):
                continue
            meta_path = os.path.join(full, ".gb_meta.json")
            if not os.path.exists(meta_path):
                manual.append(entry)
                continue
            try:
                with open(meta_path, "r", encoding="utf-8") as f:
                    mid = (json.load(f) or {}).get("mod_id")
            except Exception:
                manual.append(entry)
                continue
            if mid is None:
                manual.append(entry)
                continue
            # Multiple folders for the same mod_id (re-install glitch) →
            # keep the most-recently-modified one.
            prev = out.get(mid)
            if prev is None or (os.path.getmtime(full)
                                > os.path.getmtime(
                                    os.path.join(ARCROPOLIS_MODS, prev))):
                out[mid] = entry
        return out, manual

    def _preflight_recheck_cached_archives(self, downloadable):
        """Before the install loop runs, scan each profile mod for:

          1. **Broken cache** — extracted archive contains freeze-
             risk files (custom skel without nuhlpb, partial bundle,
             stray authoring residue, etc). We **strip** the cache
             in place rather than wiping it; redownloading would
             pull the same broken file from GameBanana, so wipe-
             then-redownload would loop forever. The strip leaves
             the cache as "vanilla mesh + custom textures" — same
             outcome as install_to_sd would produce on a fresh
             install — and avoids re-downloading the entire archive
             on every profile reload.
          2. **Stale on-SD install** — the SD folder's
             ``config.json`` has ``new-dir-files`` entries pointing
             at files that don't exist on disk (orphan paths from
             multi-slot configs the original author shipped). These
             carry forward across loads because the diff sees the
             mod_id on the SD and skips reinstall, but the orphans
             can crash adjacent slots in-game.

        Returns the set of ``mod_id`` strings that need a forced
        reinstall — caller adds them to both ``to_remove`` and
        ``to_install`` so the install loop runs again with fresh
        downloads + clean configs.
        """
        force_reinstall = set()
        if not downloadable:
            return force_reinstall

        # 1a. Strip caches in place. Idempotent — clean caches
        # produce no output and don't trigger a force reinstall.
        # Only when the strip actually removes files does the SD
        # need to be re-synced (the SD's copy predates the strip).
        stripped_caches = []
        for m in downloadable:
            mid = m.get("mod_id")
            if not mid:
                continue
            cache_dir = os.path.join(MOD_CACHE_DIR, str(mid))
            extracted = os.path.join(cache_dir, "extracted")
            if not (os.path.isdir(extracted) and os.listdir(extracted)):
                continue
            try:
                stripped_any = False
                for top in os.listdir(extracted):
                    sub = os.path.join(extracted, top)
                    if not os.path.isdir(sub):
                        continue
                    if _strip_freeze_risks_in_mod(sub):
                        stripped_any = True
                    if _strip_stray_dev_files_in_mod(sub):
                        stripped_any = True
                    if _strip_invalid_nutexb_in_mod(sub):
                        stripped_any = True
                if stripped_any:
                    name = m.get("name") or f"mod {mid}"
                    stripped_caches.append(name)
                    force_reinstall.add(str(mid))
            except Exception as e:
                print(f"    ! cache strip failed for {mid}: {e}",
                      file=sys.stderr)
        if stripped_caches:
            print(f"  ↻ Stripped {len(stripped_caches)} cached "
                  "archive(s) in place; SD will be re-synced from "
                  "the now-clean cache.")

        # 1b. SD-side check: even when the cache is gone (e.g.
        # wiped on a previous load that didn't get to reinstall),
        # the SD install itself can be inspected for the same
        # fatal author bug. If found, force reinstall (the install
        # path will redownload from GameBanana since cache is
        # empty, and the new archive may be fixed).
        installed_map_for_check, _ = self._scan_installed_mod_ids()
        # ``_scan_installed_mod_ids`` keys by whatever type the JSON
        # held — usually int but sometimes str. Normalize to strings
        # for the lookup below.
        sd_by_str = {str(k): v
                     for k, v in installed_map_for_check.items()}
        sd_broken = []
        for m in downloadable:
            mid = m.get("mod_id")
            if not mid:
                continue
            mid_str = str(mid)
            if mid_str in force_reinstall:
                continue
            sd_folder = sd_by_str.get(mid_str)
            if not sd_folder:
                continue
            sd_path = os.path.join(ARCROPOLIS_MODS, sd_folder)
            broken, reason = _archive_has_fatal_author_bug(sd_path)
            if broken:
                name = m.get("name") or f"mod {mid}"
                print(f"  ↻ On-SD install of '{name}' looks broken "
                      f"({reason}) — forcing redownload + reinstall.")
                # Wipe any leftover cache too so we get a fresh
                # download attempt.
                try:
                    cache_dir = os.path.join(MOD_CACHE_DIR, mid_str)
                    if os.path.isdir(cache_dir):
                        shutil.rmtree(cache_dir, ignore_errors=True)
                except Exception:
                    pass
                sd_broken.append(name)
                force_reinstall.add(mid_str)
        if sd_broken:
            print(f"  ↻ Queued {len(sd_broken)} SD-broken mod(s) "
                  "for forced redownload.")

        # NOTE: An earlier version auto-disabled mods matching the
        # "custom skel + no nuhlpb" pattern. Reverted — that pattern
        # CORRELATES with crashes but isn't necessarily the cause.
        # The actual cause appears to be that motion/sound/effect
        # files aren't being registered in config.json, so vanilla
        # physics gets used and references bones the custom skel
        # doesn't have. _regenerate_config_json now walks all
        # subtrees (motion/, sound/, effect/, ui/) instead of just
        # model/, so those mods may actually work after a fresh
        # install pass. Don't disable preemptively.

        # 2. Detect SD installs whose config.json has orphan paths
        # and queue them for forced reinstall (the install path
        # will regenerate config.json with no orphans).
        installed_map, _ = self._scan_installed_mod_ids()
        bloated = []
        for mid_str, folder in installed_map.items():
            sd_mod = os.path.join(ARCROPOLIS_MODS, folder)
            cfg_path = os.path.join(sd_mod, "config.json")
            if not os.path.isfile(cfg_path):
                continue
            try:
                with open(cfg_path, "r", encoding="utf-8") as fh:
                    cfg = json.load(fh) or {}
            except Exception:
                continue
            ndf = cfg.get("new-dir-files") or {}
            orphan_count = 0
            for entries in ndf.values():
                if not isinstance(entries, list):
                    continue
                for e in entries:
                    if not isinstance(e, str):
                        continue
                    full = os.path.join(
                        sd_mod, e.replace("/", os.sep))
                    if not os.path.isfile(full):
                        orphan_count += 1
            # Threshold: ignore tiny orphan counts (a single stale
            # ref isn't worth the redownload cost). 10+ orphans is
            # a reliable signal of a bloated multi-slot config.
            if orphan_count >= 10:
                # Find the matching profile entry to get the name.
                name = next(
                    (m.get("name") for m in downloadable
                     if str(m.get("mod_id")) == str(mid_str)), folder)
                print(f"  ↻ '{name}' has {orphan_count} orphan "
                      "config.json paths on SD — forcing reinstall "
                      "to regenerate a clean config.")
                bloated.append(name)
                force_reinstall.add(str(mid_str))
        if bloated:
            print(f"  ↻ Queued {len(bloated)} mod(s) for config "
                  "regen.")
        return force_reinstall

    def _do_load_profile(self, profile_name, downloadable, manual):
        """Background task: bring the SD card into sync with the saved
        profile, installing only the *delta* relative to what's already
        there.

        For every mod folder on the SD card that has a ``.gb_meta.json``
        we know its ``mod_id``. Any installed ``mod_id`` not present in
        the profile is removed; any profile ``mod_id`` not yet on the SD
        is downloaded and installed; matching IDs are left untouched.

        Folders without ``.gb_meta.json`` are treated as manual / hand-
        copied content and are NEVER deleted by this routine.
        """
        print(f"\n=== Loading profile '{profile_name}' "
              f"({len(downloadable)} mods) ===\n")

        # ── Pre-sync hygiene sweep ──
        # Repair any pre-existing breakage on the SD *before* we look
        # at the delta. This catches the "reload the exact same
        # profile to a broken card" case: the diff would be empty so
        # no installs run, but the cards's bad state still needs
        # fixing. Results are merged into the final summary popup.
        self._last_preload_repairs = []
        self._last_preload_reslots = []
        try:
            pre_report = self._sweep_mods_hygiene(label="Pre-load")
            self._last_preload_repairs = pre_report.get("mods", [])
            self._last_preload_reslots = pre_report.get("reslotted", [])
        except Exception as e:
            print(f"  ! Pre-load hygiene sweep failed: {e}")

        # Profile-level slot collision summary (informational only).
        # Conflicts are auto-resolved per-mod during install — we never
        # block the bulk operation on a dialog.
        try:
            collisions = validate_profile_collisions(profile_name)
        except Exception as e:
            print(f"  ! Pre-flight collision check failed: {e}")
            collisions = []

        if collisions:
            print(f"  ⚠ Pre-flight: {len(collisions)} potential slot "
                  f"collision(s) — will auto-resolve during install.")
            for c in collisions[:8]:
                disp = INTERNAL_TO_DISPLAY.get(c["fighter"], c["fighter"])
                names = " ↔ ".join(m["name"] for m in c["mods"])
                print(f"    • {disp} {c['slot']}: {names}")
            if len(collisions) > 8:
                print(f"    …and {len(collisions) - 8} more")

        # ── Pre-flight: force redownload of broken cached archives ──
        # Wipe broken caches AND detect stale-config installs on the
        # SD. Returned set is mod_ids that MUST be reinstalled even
        # if the diff would otherwise call them "unchanged".
        force_reinstall_ids = set()
        try:
            force_reinstall_ids = (
                self._preflight_recheck_cached_archives(downloadable)
                or set())
        except Exception as e:
            print(f"  ! Pre-flight cache check failed: {e}",
                  file=sys.stderr)

        # ── Compute delta ──
        os.makedirs(ARCROPOLIS_MODS, exist_ok=True)
        installed_map, untracked = self._scan_installed_mod_ids()
        installed_ids = {str(k) for k in installed_map.keys()}
        profile_ids = {str(m["mod_id"]) for m in downloadable
                       if m.get("mod_id") is not None}

        to_remove = installed_ids - profile_ids
        to_install_ids = profile_ids - installed_ids

        # Same mod_id present on SD AND in profile: not necessarily
        # "unchanged". If the slots differ (e.g. SD has the mod at
        # c00, c03 from a previous multi-slot install but the profile
        # now says c06), the old SD folder is stale. Force-remove it
        # and reinstall to the profile's slot so we don't end up with
        # files written to slots the profile no longer wants.
        already_ok = set()
        for mid in (profile_ids & installed_ids):
            sd_folder = installed_map.get(mid) or installed_map.get(
                int(mid) if mid.isdigit() else mid)
            sd_slots = self._scan_mod_slot_set(sd_folder) if sd_folder else set()
            profile_slots = set()
            for m in downloadable:
                if str(m.get("mod_id")) != mid:
                    continue
                for tok in str(m.get("slot", "")).replace(",", " ").split():
                    tok = tok.strip().lower()
                    if re.match(r"^c\d{2}$", tok):
                        profile_slots.add(tok)
            # Force-reinstall trumps "unchanged" — the pre-flight
            # found a broken cache or a bloated config that needs
            # regeneration. We can't fix those without running the
            # install loop again, so drop the mod into both lists.
            if mid in force_reinstall_ids:
                to_remove.add(mid)
                to_install_ids.add(mid)
            elif sd_slots and profile_slots and sd_slots == profile_slots:
                already_ok.add(mid)
            else:
                # Stale or unknown — reinstall to the right slots.
                to_remove.add(mid)
                to_install_ids.add(mid)

        print(f"  Diff vs SD: "
              f"{len(already_ok)} unchanged, "
              f"{len(to_install_ids)} to install, "
              f"{len(to_remove)} to remove"
              f"{f', {len(untracked)} untracked (kept)' if untracked else ''}.")

        # 1. Remove mods that are on the SD but no longer in the profile.
        if to_remove:
            import shutil as _sh
            for mid in to_remove:
                folder = installed_map.get(mid) or installed_map.get(int(mid)
                                                                     if mid.isdigit()
                                                                     else mid)
                if not folder:
                    continue
                full = os.path.join(ARCROPOLIS_MODS, folder)
                try:
                    _sh.rmtree(full, ignore_errors=True)
                    print(f"    - Removed: {folder}")
                except Exception as e:
                    print(f"    ! Could not remove {folder}: {e}")
            # Stale ARCropolis cache must be cleared whenever the layout
            # changes, otherwise the Switch will boot with a mismatched
            # romfs index.
            try:
                self._do_clear_cache()
            except Exception as e:
                print(f"    ! Cache clear failed: {e}")

        # 2. Install only the new mods.
        success = 0
        failed = 0
        # Build install list in the original profile order so deterministic
        # auto-reslotting picks the same free slot every run.
        install_queue = [m for m in downloadable
                         if m.get("mod_id") is not None
                         and str(m["mod_id"]) in to_install_ids]

        if not install_queue and not to_remove:
            print(f"  ✓ SD already matches profile — nothing to do.\n")
        for i, mod in enumerate(install_queue, 1):
            mod_id = mod["mod_id"]
            mod_name = mod.get("name", f"Mod {mod_id}")
            slot = mod.get("slot")
            # If this entry was created by the drag-and-drop Install
            # dialog, ``source_slot`` records *which* of the mod's
            # variants the user picked. Build an explicit slot_map so
            # the install pipeline doesn't fall back to "first source
            # slot" heuristics (which would silently substitute a
            # different variant of a multi-slot mod).
            source_slot = (mod.get("source_slot") or "").strip().lower()
            slot_map = None
            if source_slot and slot:
                slot_map = {source_slot: slot.strip().lower()}
            print(f"  [{i}/{len(install_queue)}] {mod_name}...")
            try:
                meta = {
                    "mod_id": mod_id,
                    "name": mod_name,
                    "thumb_url": mod.get("thumb_url"),
                    "image_urls": mod.get("image_urls", []),
                    "url": mod.get("url", ""),
                    "submitter": mod.get("submitter", ""),
                    "mod_type": mod.get("mod_type", "skin"),
                }
                if mod.get("mod_type") == "stage":
                    meta["mod_type"] = "stage"
                self._do_install_to_sd(
                    mod_id, mod_name, metadata=meta,
                    target_slot=None if slot_map else slot,
                    slot_map=slot_map)
                success += 1
            except Exception as e:
                print(f"    Error: {e}", file=sys.stderr)
                failed += 1

        if manual:
            print(f"\n  {len(manual)} mod(s) need manual install:")
            for m in manual:
                print(f"    - {m.get('name', m.get('folder_name', '?'))}")

        # ── Post-sync hygiene sweep ──
        # Walk every installed mod folder and strip orphan UI bntx /
        # empty body slot dirs that would freeze SSBU on character-
        # select. This catches:
        #   • Mods that were already on the SD before we added the
        #     install-time sanity scrub.
        #   • Cross-mod issues that only become visible once everything
        #     in the profile is in place.
        # Cheap on a clean SD, only does work when there's something to
        # fix. Always safe to re-run.
        try:
            post_report = self._sweep_mods_hygiene(label="Post-sync")
        except Exception as e:
            print(f"  ! Post-sync hygiene sweep failed: {e}")
            post_report = {"mods": [], "files": 0, "dirs": 0}

        # ── Freeze-risk static analysis ──
        # Simulate the runtime resolved layout and surface any slots
        # that match known crash patterns (Plant missing companion
        # models, portrait without body, etc.) so the user gets an
        # alert BEFORE plugging the SD back in.
        try:
            risks = diagnose_freeze_risks(ARCROPOLIS_MODS)
        except Exception as e:
            print(f"  ! Freeze-risk analysis failed: {e}")
            risks = []
        freeze_risks = [r for r in risks if r["severity"] == "freeze"]
        post_warnings = [r for r in risks if r["severity"] == "warning"]
        if freeze_risks:
            print(f"\n  ⚠ {len(freeze_risks)} likely freeze cause(s) "
                  f"detected:")
            for r in freeze_risks[:8]:
                disp = INTERNAL_TO_DISPLAY.get(r["fighter"], r["fighter"])
                print(f"    • {disp} {r['slot']}: {r['issue']}")
                for m in r["mods"][:3]:
                    print(f"        from {m}")
            if len(freeze_risks) > 8:
                print(f"    …and {len(freeze_risks) - 8} more")
        else:
            print(f"\n  ✓ No freeze-pattern risks detected.")

        # Post-sync collision report. The pre-load validation runs
        # against the SD's PRE-sync state (which may be stale from a
        # previous profile), so its warnings are easy to misread as
        # "things still wrong". This second pass reflects what's
        # actually on the SD now — the user's source of truth.
        if post_warnings:
            print(f"\n  ⚠ Post-sync: {len(post_warnings)} warning(s) "
                  f"remain after install:")
            for r in post_warnings[:8]:
                disp = INTERNAL_TO_DISPLAY.get(r["fighter"], r["fighter"])
                print(f"    • {disp} {r['slot']}: {r['issue']}")
            if len(post_warnings) > 8:
                print(f"    …and {len(post_warnings) - 8} more")
        else:
            print(f"  ✓ Post-sync: SD layout is clean (no slot "
                  f"collisions, no overlapping writers).")

        # ── Deep file-level diagnostic (auto-run on every load) ──
        # Parses each mod folder's SSBH binaries and surfaces the
        # author-broken cases that crash the game on slot select:
        # missing body skeleton, empty mesh, multi-tree fighters
        # missing companion trees, files unregistered in
        # config.json. Same logic the manual 🩺 Diagnose button
        # runs — wired in here so users see the report
        # automatically right after Load to SD.
        try:
            deep_issues = deep_diagnose_mods_root(ARCROPOLIS_MODS)
            self._print_diagnose_report(deep_issues)
        except Exception as e:
            print(f"  ! Post-sync deep diagnostic failed: {e}",
                  file=sys.stderr)

        # ── Slot map report (src → dst per mod) ──
        # Show the user exactly where each mod ended up so they can
        # verify their explicit slot assignments were respected.
        # Re-read the profile so we reflect the post-load state.
        try:
            profiles_now = load_profiles()
            mods_now = (profiles_now.get(profile_name) or {}
                        ).get("mods", [])
            print(f"\n=== Slot map ({profile_name}) ===")
            # Group by character so the per-fighter slot picture
            # is easy to scan.
            by_char = {}
            for m in mods_now:
                if not m.get("enabled", True):
                    continue
                if (m.get("mod_type") or "skin") != "skin":
                    continue
                ch = m.get("character") or "Other"
                by_char.setdefault(ch, []).append(m)
            for ch in sorted(by_char.keys(), key=str.lower):
                entries = by_char[ch]
                # Sort by destination slot for readability
                def _slot_key(e):
                    s = str(e.get("slot", "")).strip().lower()
                    m = re.match(r"c(\d{2})", s)
                    return int(m.group(1)) if m else 99
                entries.sort(key=_slot_key)
                print(f"  {ch}:")
                for e in entries:
                    name = (e.get("name") or "?")[:50]
                    src = e.get("source_slot") or "—"
                    dst = e.get("slot") or "—"
                    arrow = (f"{src} → {dst}"
                             if src and src != dst
                             else f"{dst}        (no remap)")
                    print(f"    {arrow:24}  {name}")
            print(f"=== End slot map ===")
        except Exception as e:
            print(f"  ! Slot map failed: {e}", file=sys.stderr)

        print(f"\n=== DONE — synced profile '{profile_name}': "
              f"{success} added, {len(to_remove)} removed, "
              f"{len(already_ok)} kept, {failed} failed ===\n")

        # ── Final user-visible summary ──
        # Combine pre-load and post-sync repairs into one alert so the
        # user sees exactly what was wrong with the SD and confirms it
        # has been repaired.
        all_repaired = list(getattr(self, "_last_preload_repairs", []))
        all_repaired.extend(post_report.get("mods", []))
        all_reslots = list(getattr(self, "_last_preload_reslots", []))
        all_reslots.extend(post_report.get("reslotted", []))
        if all_repaired or all_reslots or freeze_risks:
            self.root.after(0, self._show_repair_summary,
                            profile_name, all_repaired, all_reslots,
                            freeze_risks)
        # Reset for next load.
        self._last_preload_repairs = []
        self._last_preload_reslots = []

        self.root.after(0, self._check_sd)
        self.root.after(100, self._refresh_current_view)

    def _sweep_mods_hygiene(self, label="Hygiene"):
        """Run :func:`sanity_check_install` against every mod folder
        currently on the SD card. Logs and removes any orphan UI bntx
        / empty body slot dirs across the whole profile install.

        Skips folders without a ``.gb_meta.json`` (manual / hand-copied
        mods are owned by the user; we don't touch them).

        Returns a structured report::

            {"mods": [{"name": str, "files": int, "dirs": int,
                       "warnings": [str, ...]}, ...],
             "files": total_files, "dirs": total_dirs}
        """
        result = {"mods": [], "files": 0, "dirs": 0,
                  "split_dirs": 0, "split_files": 0,
                  "reslotted": []}
        if not os.path.isdir(ARCROPOLIS_MODS):
            return result

        # ── Pass 0: cross-mod slot conflict resolver ──
        # Multi-slot-named folders ("Ridley_Plant_c00, c03") whose
        # current internal slot collides with a single-slot mod get
        # reslotted to the first free token from their name. Also
        # renames the top-level folder to drop the comma so future
        # runs don't re-process them.
        try:
            moved = resolve_cross_mod_slot_conflicts(ARCROPOLIS_MODS)
        except Exception as e:
            print(f"    ! Cross-mod resolver failed: {e}")
            moved = []
        if moved:
            for old_name, new_name, fighter, old_slot, new_slot in moved:
                print(f"    Reslotted {old_name} → {new_name} "
                      f"(packun {old_slot}→{new_slot})"
                      .replace("packun ", f"{fighter} "))
            result["reslotted"] = moved
            # Wipe any stale conflicts.json so the next boot's view is fresh.
            cf = os.path.join(SD_CARD, "ultimate", "arcropolis",
                              "conflicts.json")
            try:
                if os.path.isfile(cf):
                    os.remove(cf)
            except Exception:
                pass

        for entry in os.listdir(ARCROPOLIS_MODS):
            full = os.path.join(ARCROPOLIS_MODS, entry)
            if not os.path.isdir(full):
                continue
            meta = os.path.join(full, ".gb_meta.json")
            if not os.path.exists(meta):
                continue
            try:
                report = sanity_check_install(full)
            except Exception as e:
                print(f"    ! Hygiene scan failed for {entry}: {e}")
                continue
            d_files = len(report.get("deleted_files", []))
            d_dirs = len(report.get("deleted_dirs", []))
            s_dirs = report.get("split_dirs", 0)
            s_files = report.get("split_files", 0)
            if d_files or d_dirs or s_dirs or s_files:
                result["mods"].append({
                    "name": entry,
                    "files": d_files,
                    "dirs": d_dirs,
                    "split_dirs": s_dirs,
                    "split_files": s_files,
                    "warnings": report.get("warnings", []),
                })
                result["files"] += d_files
                result["dirs"] += d_dirs
                result.setdefault("split_dirs", 0)
                result.setdefault("split_files", 0)
                result["split_dirs"] += s_dirs
                result["split_files"] += s_files
                bits = []
                if s_dirs:
                    bits.append(f"{s_dirs} multi-slot folder(s) renamed")
                if s_files:
                    bits.append(f"{s_files} multi-slot file(s) renamed")
                if d_dirs:
                    bits.append(f"{d_dirs} empty body slot(s)")
                if d_files:
                    bits.append(f"{d_files} orphan UI portrait(s)")
                print(f"    Cleaned {entry}: {', '.join(bits)}")
        if result["mods"] or result["reslotted"]:
            split_total = (result.get("split_dirs", 0)
                           + result.get("split_files", 0))
            reslot_total = len(result.get("reslotted", []))
            print(f"  {label} sweep: {len(result['mods'])} mod(s) cleaned, "
                  f"{result['dirs']} body slot(s) + {result['files']} "
                  f"UI file(s) removed"
                  + (f", {split_total} multi-slot artifact(s) renamed"
                     if split_total else "")
                  + (f", {reslot_total} cross-mod conflict(s) resolved"
                     if reslot_total else "")
                  + ". Clearing cache to avoid stale index.")
            try:
                self._do_clear_cache()
            except Exception as e:
                print(f"    ! Cache clear after sweep failed: {e}")
        else:
            print(f"  {label} sweep: clean — no orphan files found.")
        return result

    def _show_repair_summary(self, profile_name, repaired,
                             reslots=None, freeze_risks=None):
        """UI-thread popup listing every mod that needed repair while
        loading ``profile_name``. Merges duplicate entries (same mod
        repaired in both passes) so the user sees one row per mod.
        """
        reslots = reslots or []
        freeze_risks = freeze_risks or []
        if not repaired and not reslots and not freeze_risks:
            return
        merged = {}
        for r in repaired:
            slot = merged.setdefault(r["name"],
                                     {"files": 0, "dirs": 0,
                                      "split_dirs": 0, "split_files": 0,
                                      "warnings": []})
            slot["files"] += r.get("files", 0)
            slot["dirs"] += r.get("dirs", 0)
            slot["split_dirs"] += r.get("split_dirs", 0)
            slot["split_files"] += r.get("split_files", 0)
            slot["warnings"].extend(r.get("warnings", []))

        lines = []
        for name, info in sorted(merged.items()):
            bits = []
            if info["split_dirs"]:
                bits.append(f"{info['split_dirs']} multi-slot folder(s) renamed")
            if info["split_files"]:
                bits.append(f"{info['split_files']} multi-slot file(s) renamed")
            if info["dirs"]:
                bits.append(f"{info['dirs']} empty body slot(s)")
            if info["files"]:
                bits.append(f"{info['files']} orphan portrait(s)")
            lines.append(f"  • {name} — {', '.join(bits)}")

        # Cross-mod reslotting (the actual fix for ARCropolis conflicts).
        reslot_lines = []
        for old_name, new_name, fighter, old_slot, new_slot in reslots:
            display_fighter = INTERNAL_TO_DISPLAY.get(fighter, fighter)
            reslot_lines.append(
                f"  • {new_name} — moved from {display_fighter} "
                f"{old_slot} → {new_slot} to avoid collision")

        total_dirs = sum(i["dirs"] for i in merged.values())
        total_files = sum(i["files"] for i in merged.values())
        total_sd = sum(i["split_dirs"] for i in merged.values())
        total_sf = sum(i["split_files"] for i in merged.values())
        repair_lines = []
        if reslot_lines:
            repair_lines.append(
                f"Resolved {len(reslot_lines)} cross-mod slot collision(s) "
                f"by moving multi-slot mods off conflicting slots. The "
                f"ARCropolis 'conflicts' warning at boot should be gone "
                f"now.")
        if total_sd or total_sf:
            repair_lines.append(
                f"Renamed {total_sd} multi-slot folder(s) and "
                f"{total_sf} multi-slot file(s) to their first slot "
                f"only.")
        if total_dirs or total_files:
            repair_lines.append(
                f"Removed {total_dirs} empty body slot(s) and "
                f"{total_files} orphan UI portrait(s).")

        sections = []
        if freeze_risks:
            risk_lines = []
            for r in freeze_risks:
                disp = INTERNAL_TO_DISPLAY.get(r["fighter"], r["fighter"])
                risk_lines.append(
                    f"  ✗ {disp} {r['slot']} — {r['issue']}")
                for m in r["mods"][:2]:
                    risk_lines.append(f"      from: {m}")
                if len(r["mods"]) > 2:
                    risk_lines.append(
                        f"      …and {len(r['mods']) - 2} more")
            sections.append(
                "FREEZE RISKS DETECTED — picking these characters "
                "will likely crash the game:\n"
                + "\n".join(risk_lines))
        if lines:
            sections.append("Files cleaned up:\n" + "\n".join(lines))
        if reslot_lines:
            sections.append("Conflicts resolved:\n" + "\n".join(reslot_lines))

        if freeze_risks:
            tail = ("\n\n→ Open the profile and disable the listed "
                    "mod(s) (or remove them) BEFORE plugging the SD "
                    "back in. Use 'Validate Profile' on the profile "
                    "view to recheck.")
        else:
            tail = ("\n\nARCropolis cache was cleared so these mods "
                    "will load cleanly on the next boot. This profile "
                    "is now safe to load.")

        body = (f"Profile load report for '{profile_name}':\n\n"
                + "\n\n".join(sections)
                + ("\n\n" + "\n".join(repair_lines) if repair_lines else "")
                + tail)
        try:
            title = ("⚠ Freeze Risk Detected"
                     if freeze_risks else "SD Card Repaired")
            if freeze_risks:
                messagebox.showwarning(title, body)
            else:
                messagebox.showinfo(title, body)
        except Exception:
            pass

    def _uninstall_all_skins(self, skin_names):
        """Prompt the user and then remove ALL skins from the SD card."""
        count = len(skin_names)
        if count == 0:
            messagebox.showinfo("Nothing to Remove", "No skins are installed.")
            return

        if not messagebox.askyesno(
                "Uninstall ALL Skins",
                f"This will permanently remove all {count} skin(s) "
                f"from the SD card.\n\n"
                f"Are you sure?",
                icon="warning"):
            return

        # Second confirmation for safety
        if not messagebox.askyesno(
                "Really Uninstall Everything?",
                f"Last chance — delete {count} mod folder(s) from\n"
                f"{ARCROPOLIS_MODS}?",
                icon="warning"):
            return

        removed = 0
        failed = 0
        for name in skin_names:
            try:
                if uninstall_skin(name):
                    removed += 1
                    print(f"  Removed: {name}")
                else:
                    failed += 1
                    print(f"  Not found: {name}")
            except Exception as e:
                failed += 1
                print(f"  Error removing {name}: {e}", file=sys.stderr)

        summary = f"Removed {removed} skin(s)."
        if failed:
            summary += f"\n{failed} could not be removed."
        print(f"\n  {summary}\n")
        messagebox.showinfo("Uninstall Complete", summary)

        self._check_sd()
        self._show_installed()

    # ── SD card auto-detection ─────────────────────────────

    def _start_sd_poll(self):
        """Begin polling for SD card insertion/removal every 2 seconds."""
        self._stop_sd_poll()  # cancel any existing timer first
        _apply_sd_drive(_detect_sd_drive())
        self._sd_present = os.path.exists(SD_CARD)
        self._poll_sd_card()

    def _stop_sd_poll(self):
        """Cancel any active SD card polling timer."""
        if self._sd_poll_id is not None:
            self.root.after_cancel(self._sd_poll_id)
            self._sd_poll_id = None

    def _poll_sd_card(self):
        """Check if SD card state changed (or drive letter swapped); auto-refresh Setup tab if so."""
        detected = _detect_sd_drive()
        now_present = os.path.exists(detected)
        drive_changed = now_present and (detected != SD_CARD)
        if now_present != self._sd_present or drive_changed:
            self._sd_present = now_present
            if now_present:
                _apply_sd_drive(detected)
                state = f"detected at {SD_CARD}"
            else:
                state = "removed"
            print(f"  [SD poll] SD card {state} — refreshing…")
            # Always update the top-right SD status label
            self._check_sd()
            # Auto-refresh if we're still on the Setup tab
            if self._active_view == "setup":
                self._show_setup()
                return  # _show_setup will restart polling
        # Schedule next poll (2 seconds)
        self._sd_poll_id = self.root.after(2000, self._poll_sd_card)

    # ── RCM device auto-detection ──────────────────────────

    def _start_rcm_poll(self):
        """Begin polling for Switch in RCM mode every 2 seconds."""
        self._stop_rcm_poll()
        # Do first check in a background thread (WMI query can take ~200ms)
        self._rcm_poll_tick()

    def _stop_rcm_poll(self):
        """Cancel any active RCM polling timer."""
        if self._rcm_poll_id is not None:
            self.root.after_cancel(self._rcm_poll_id)
            self._rcm_poll_id = None

    def _rcm_poll_tick(self):
        """Background-check for RCM device, then schedule next tick."""
        def _check():
            detected = detect_rcm_device()
            self.root.after(0, lambda: self._rcm_poll_update(detected))
        threading.Thread(target=_check, daemon=True).start()

    def _rcm_poll_update(self, detected):
        """UI-thread callback: update RCM status if state changed."""
        changed = (detected != self._rcm_detected)
        self._rcm_detected = detected

        # Update the device status label
        if self._rcm_device_label and self._rcm_device_label.winfo_exists():
            if detected:
                self._rcm_device_label.configure(
                    text="  🎮 Switch detected in RCM mode — ready to inject!",
                    fg=T.GREEN)
            else:
                self._rcm_device_label.configure(
                    text="  🎮 No Switch in RCM mode",
                    fg=T.OVERLAY)

        # Update inject button appearance.  Single-state rule: green and
        # clickable iff a Switch is in RCM mode, otherwise muted/disabled.
        # The device-state label next to it explains *why* it's disabled.
        if self._rcm_inject_btn and self._rcm_inject_btn.winfo_exists():
            if detected:
                self._rcm_inject_btn.configure(
                    bg=T.GREEN, fg=T.CRUST,
                    state="normal", cursor="hand2",
                    text="⚡ Inject Payload")
            else:
                self._rcm_inject_btn.configure(
                    bg=T.SURFACE1, fg=T.OVERLAY,
                    state="disabled", cursor="arrow",
                    text="⚡ Inject Payload")

        if changed and detected:
            print("  [RCM] 🎮 Switch detected in RCM mode!")
        elif changed and not detected:
            print("  [RCM] Switch disconnected from RCM mode.")

        # Schedule next poll (2 seconds)
        self._rcm_poll_id = self.root.after(2000, self._rcm_poll_tick)

    # ── Setup / Verify tab ───────────────────────────────

    def _show_setup(self):
        """Show the Setup & Verify tab — renders instantly, fetches GitHub async."""
        # Start SD card auto-detection polling
        self._start_sd_poll()
        # Start RCM device polling (detects Switch in RCM mode)
        self._start_rcm_poll()

        # Hide pagination
        self.prev_btn.configure(state="disabled")
        self.next_btn.configure(state="disabled")
        self.page_label.configure(text="")
        self.results_label.configure(text="SD Card Setup & Verify")

        # Phase 1: render the UI shell IMMEDIATELY with empty checks so
        # the tab swap feels instant.  Local SD scans in
        # ``_run_health_checks`` can take a noticeable amount of time on
        # a real card; running them on the main thread before any UI is
        # drawn made tab switching feel laggy.
        self._setup_latest = {}
        self._setup_checks_done = False
        self._build_setup_ui([], latest={})

        # Phase 2: run local SD checks off the main thread, then refresh
        # the rows.  Phase 3 (GitHub) chains off the end of this so we
        # don't fire two fetches.
        self._run_async(self._do_setup_initial_checks)

    def _do_setup_initial_checks(self):
        """Background: run the local-only health checks (no GitHub) and
        push them into the UI, then chain into the GitHub fetch."""
        checks = self._run_health_checks(latest={})
        # Reuse the same finisher used after the GitHub fetch — it knows
        # how to repaint check rows from the main thread.
        self.root.after(0, lambda: self._refresh_setup_check_rows(checks))
        # Now do the slower GitHub fetch on the same background slot.
        self._do_setup_fetch_github()

    def _refresh_setup_check_rows(self, checks):
        """Main-thread: replace placeholder rows with the supplied checks
        and re-enable the action buttons.

        Originally we waited for the GitHub fetch before re-enabling, but
        that left "Quick Actions" / "Provision" looking active (their
        bg colors aren't muted) while still being ``state="disabled"``,
        so clicks silently did nothing.  The buttons themselves don't
        need GitHub data — only the version-comparison rows do — so
        flipping ``mark_done=True`` here is safe.
        """
        if self._active_view != "setup":
            return
        if not hasattr(self, "_setup_checks_frame"):
            return
        self._render_check_rows(checks, mark_done=True)

    def _do_setup_fetch_github(self):
        """Background: fetch latest GitHub info, then update Setup UI."""
        print("\n=== Fetching latest versions from GitHub… ===\n")
        latest = {}
        for key, (repo, filt) in GITHUB_REPOS.items():
            info = github_latest_asset(repo, filt)
            latest[key] = info
            tag = info["version"] if info else "?"
            print(f"    {key}: {tag}")

        # When unofficial mode is enabled, also check for unofficial Atmosphere
        if getattr(self, '_use_unofficial_atmo', False):
            print(f"\n    [Unofficial mode] Checking alternative Atmosphere sources…")
            atmo_filter = GITHUB_REPOS["atmosphere"][1]
            result, source = _resolve_unofficial_atmosphere(atmo_filter)
            if result:
                if result.get("local"):
                    unofficial = {
                        "version": f"{result['version']} (local)",
                        "filename": result["filename"],
                        "size": result["size"],
                        "local": True,
                    }
                else:
                    unofficial = result
                latest["atmosphere_unofficial"] = unofficial
                print(f"    atmosphere (unofficial): {unofficial.get('version', '?')} via {source}")
            else:
                print(f"    atmosphere (unofficial): not found")
                print(f"      Place an unofficial atmosphere-*.zip in switch_setup/downloads/")
                print(f"      or set UNOFFICIAL_ATMOSPHERE_FORK and build with the included workflow")

        self._setup_latest = latest

        # Re-run checks with GitHub data and update UI on main thread
        checks = self._run_health_checks(latest)
        print(f"\n=== DONE — version check complete ===\n")
        self.root.after(0, lambda: self._finish_setup_checks(checks, latest))

    def _render_check_rows(self, checks, mark_done=False):
        """Repaint the SD-check rows in the Setup tab.

        Shared by the post-local-scan and post-GitHub renderers.  When
        ``mark_done`` is True the action buttons are enabled and the
        header summary turns green/yellow/red based on the results.
        """
        # Async callback may fire after the user navigated away or the
        # Setup view was rebuilt — the cached widgets would then be dead.
        if (not hasattr(self, "_setup_checks_frame")
                or not self._setup_checks_frame.winfo_exists()):
            return

        # Header summary
        if (hasattr(self, "_setup_summary_label")
                and self._setup_summary_label.winfo_exists()):
            total = len(checks)
            if total == 0:
                self._setup_summary_label.configure(
                    text="⏳ Scanning SD card…", fg=T.YELLOW)
            else:
                ok_count = sum(1 for c in checks if c["ok"])
                warn_count = sum(1 for c in checks if c.get("warn"))
                fail_count = total - ok_count
                parts = [f"{ok_count}/{total} passed"]
                if warn_count:
                    parts.append(f"{warn_count} warning(s)")
                if fail_count:
                    parts.append(f"{fail_count} issue(s)")
                color = (T.GREEN if fail_count == 0 and warn_count == 0
                         else (T.YELLOW if fail_count == 0 else T.RED))
                self._setup_summary_label.configure(
                    text="  •  ".join(parts), fg=color)

        # Replace check rows
        for w in self._setup_checks_frame.winfo_children():
            w.destroy()
        section_labels = {
            "core": "Core Components",
            "profile": f"{self._active_profile} Plugins",
            "health": "SD Health",
        }
        section_stats = {}
        for c in checks:
            sec = c.get("section", "core")
            s = section_stats.setdefault(
                sec, {"total": 0, "ok": 0, "warn": 0})
            s["total"] += 1
            if c["ok"]:
                s["ok"] += 1
            elif c.get("warn"):
                s["warn"] += 1
        shown_sections = set()
        for check in checks:
            sec = check.get("section", "core")
            if sec not in shown_sections:
                shown_sections.add(sec)
                lbl = section_labels.get(sec, sec)
                sep = tk.Frame(self._setup_checks_frame,
                               bg=T.SURFACE1, height=1)
                sep.pack(fill="x", padx=12, pady=(10, 2))
                hdr_row = tk.Frame(self._setup_checks_frame, bg=T.SURFACE)
                hdr_row.pack(fill="x", padx=12, pady=(2, 4))
                tk.Label(hdr_row, text=lbl,
                         bg=T.SURFACE, fg=T.ACCENT,
                         font=(T.FONT, T.SZ_LG, "bold")).pack(side="left")
                st = section_stats.get(sec,
                                       {"total": 0, "ok": 0, "warn": 0})
                if st["total"]:
                    fail = st["total"] - st["ok"] - st["warn"]
                    parts = [f"{st['ok']}/{st['total']} passed"]
                    if st["warn"]:
                        parts.append(f"{st['warn']} warning(s)")
                    if fail:
                        parts.append(f"{fail} issue(s)")
                    summary = "  ·  ".join(parts)
                    color = (T.GREEN if fail == 0 and st["warn"] == 0
                             else (T.YELLOW if fail == 0 else T.RED))
                    tk.Label(hdr_row, text=summary,
                             bg=T.SURFACE, fg=color,
                             font=(T.FONT, T.SZ_SM, "bold")).pack(
                                 side="right")
            self._add_check_row(check, parent=self._setup_checks_frame)

        if mark_done:
            self._setup_checks_done = True
            for btn in getattr(self, "_setup_action_btns", []):
                if btn.winfo_exists():
                    btn.configure(state="normal", cursor="hand2")

    def _finish_setup_checks(self, checks, latest):
        """Main-thread: update check rows and enable action buttons."""
        if self._active_view != "setup":
            return  # user navigated away

        # Update version hint — widget may have been torn down by a
        # concurrent re-render of the Setup tab, so guard winfo_exists().
        if (hasattr(self, "_setup_version_label")
                and self._setup_version_label.winfo_exists()):
            ls_info = latest.get("latency_slider")
            if ls_info:
                body = ls_info.get("body", "")
                import re as _re
                m = _re.search(r'compatible with version\s+([\d.]+)\s+of SSBU',
                               body, _re.IGNORECASE)
                if m:
                    self._setup_version_label.configure(
                        text=f"Plugins target SSBU {m.group(1)} — keep your game updated.",
                        fg=T.OVERLAY)
                else:
                    self._setup_version_label.configure(text="")
            else:
                self._setup_version_label.configure(text="")

        self._render_check_rows(checks, mark_done=True)

    def _build_setup_ui(self, checks, latest):
        """Build the full Setup tab UI with check results."""
        self._slot_picker_registry.clear()
        self._slot_counter_registry.clear()
        for w in self.results_inner.winfo_children():
            w.destroy()
        self._thumb_cache.clear()
        # Reset scroll position and region when clearing, to avoid stale state
        self.results_canvas.yview_moveto(0)
        self.results_canvas.configure(scrollregion="0 0 0 0")

        profile = PROVISIONING_PROFILES.get(self._active_profile, {})
        profile_desc = profile.get("desc", "")
        is_pending = not self._setup_checks_done  # True on first render

        # ── Header + Manage Profiles button ──
        # The profile dropdown / unofficial-Atmosphere checkbox / description
        # used to live inline here and ate four rows of vertical space. They
        # now live behind a single "⚙ Manage Profiles…" dialog so the SD
        # check rows have room to breathe.
        hdr_frame = tk.Frame(self.results_inner, bg=T.SURFACE)
        hdr_frame.pack(fill="x", padx=12, pady=(12, 4))

        tk.Label(hdr_frame, text="SD Card Provisioning",
                 bg=T.SURFACE, fg=T.ACCENT,
                 font=(T.FONT, T.SZ_H2, "bold")).pack(side="left")

        tk.Button(hdr_frame, text="⚙ Manage Profiles…",
                  bg=T.SURFACE1, fg=T.FG, font=(T.FONT, T.SZ_SM, "bold"),
                  relief="flat", cursor="hand2",
                  command=self._open_profile_setup_dialog).pack(side="right")

        # Compact one-liner: active user profile + flags. Falls back to
        # the provisioning template name if no user profile is selected
        # yet (first run, or before any profile has been created).
        all_profs = load_profiles()
        active_user = self._active_user_profile
        if active_user not in all_profs:
            active_user = next(iter(all_profs), None)
            self._active_user_profile = active_user
        active_data = all_profs.get(active_user, {}) if active_user else {}
        active_cfg = profile_config(active_data)
        flags = []
        if active_cfg["wifi_safe"]:
            flags.append("Wifi-Safe")
        if active_cfg["unofficial_atmo"]:
            flags.append(f"Unofficial Atmo ({ATMOSPHERE_SUPPORT_BRANCH})")
        flag_str = ("  ·  " + "  ·  ".join(flags)) if flags else ""
        display_name = active_user or "(none — create one in Manage Profiles)"
        tk.Label(self.results_inner,
                 text=f"▸ Active: {display_name}  "
                      f"[{active_cfg['template']}]{flag_str}",
                 bg=T.SURFACE, fg=T.OVERLAY,
                 font=(T.FONT, T.SZ_MD)).pack(anchor="w", padx=12,
                                              pady=(0, 6))

        # Summary line (updatable)
        if is_pending:
            summary_text = "⏳ Checking for updates…"
            summary_color = T.YELLOW
        else:
            total = len(checks)
            ok_count = sum(1 for c in checks if c["ok"])
            warn_count = sum(1 for c in checks if c.get("warn"))
            fail_count = total - ok_count
            parts = [f"{ok_count}/{total} passed"]
            if warn_count:
                parts.append(f"{warn_count} warning(s)")
            if fail_count:
                parts.append(f"{fail_count} issue(s)")
            summary_text = "  •  ".join(parts)
            summary_color = T.GREEN if fail_count == 0 and warn_count == 0 else (
                T.YELLOW if fail_count == 0 else T.RED)

        self._setup_summary_label = tk.Label(
            self.results_inner, text=summary_text,
            bg=T.SURFACE, fg=summary_color,
            font=(T.FONT, T.SZ_LG, "bold"))
        self._setup_summary_label.pack(anchor="w", padx=12, pady=(0, 4))

        # Version hint placeholder (updated async)
        self._setup_version_label = tk.Label(
            self.results_inner, text="",
            bg=T.SURFACE, fg=T.OVERLAY,
            font=(T.FONT, T.SZ_SM))
        self._setup_version_label.pack(anchor="w", padx=12, pady=(0, 6))

        # ── Component checks container (replaceable) ──
        self._setup_checks_frame = tk.Frame(self.results_inner, bg=T.SURFACE)
        self._setup_checks_frame.pack(fill="x")

        section_labels = {
            "core": "Core Components",
            "profile": f"{self._active_profile} Plugins",
            "health": "SD Health",
        }
        # Pre-compute per-section pass/issue counts so we can render them
        # next to each section header (e.g. "Core Components — 2/3 passed · 1 issue").
        section_stats = {}
        for c in checks:
            sec = c.get("section", "core")
            s = section_stats.setdefault(sec, {"total": 0, "ok": 0, "warn": 0})
            s["total"] += 1
            if c["ok"]:
                s["ok"] += 1
            elif c.get("warn"):
                s["warn"] += 1

        shown_sections = set()
        for check in checks:
            sec = check.get("section", "core")
            if sec not in shown_sections:
                shown_sections.add(sec)
                lbl = section_labels.get(sec, sec)
                sep = tk.Frame(self._setup_checks_frame, bg=T.SURFACE1, height=1)
                sep.pack(fill="x", padx=12, pady=(10, 2))
                hdr_row = tk.Frame(self._setup_checks_frame, bg=T.SURFACE)
                hdr_row.pack(fill="x", padx=12, pady=(2, 4))
                tk.Label(hdr_row, text=lbl,
                         bg=T.SURFACE, fg=T.ACCENT,
                         font=(T.FONT, T.SZ_LG, "bold")).pack(side="left")
                st = section_stats.get(sec, {"total": 0, "ok": 0, "warn": 0})
                if not is_pending and st["total"]:
                    fail = st["total"] - st["ok"] - st["warn"]
                    parts = [f"{st['ok']}/{st['total']} passed"]
                    if st["warn"]:
                        parts.append(f"{st['warn']} warning(s)")
                    if fail:
                        parts.append(f"{fail} issue(s)")
                    summary = "  ·  ".join(parts)
                    color = (T.GREEN if fail == 0 and st["warn"] == 0
                             else (T.YELLOW if fail == 0 else T.RED))
                    tk.Label(hdr_row, text=summary,
                             bg=T.SURFACE, fg=color,
                             font=(T.FONT, T.SZ_SM, "bold")).pack(
                                 side="right")
            self._add_check_row(check, parent=self._setup_checks_frame)

        # ── Compact action bar — fixed bottom pane (not scrollable) ──
        # The full Quick Actions list lives behind a popup menu and the RCM
        # payload-injection workflow runs in its own dialog. This frees up
        # vertical room for the SD provisioning checks above, which is the
        # primary thing this tab is supposed to show.
        af = self._setup_actions_frame
        for w in af.winfo_children():
            w.destroy()

        tk.Frame(af, bg=T.SURFACE1, height=1).pack(fill="x", padx=8, pady=(0, 4))

        bar = tk.Frame(af, bg=T.BG)
        bar.pack(fill="x", padx=10, pady=(2, 8))

        # All bar buttons share these knobs so they end up the same height.
        _btn_kw = dict(font=(T.FONT, T.SZ_MD, "bold"), relief="flat",
                       padx=12, pady=6, bd=0, highlightthickness=0)

        # Menus get their own (larger) styling so they don't render at the
        # tiny system default.  Children of `af` so they outlive any single
        # button widget rebuild.
        _menu_kw = dict(tearoff=0, bg=T.SURFACE, fg=T.FG,
                        activebackground=T.ACCENT, activeforeground=T.BG,
                        font=(T.FONT, T.SZ_MD), bd=0,
                        activeborderwidth=0, borderwidth=0)

        def _popup_below(menu, widget):
            """Drop a menu just under its trigger button. Using tk_popup on
            ButtonRelease (instead of letting Menubutton handle press+drag)
            avoids the Windows quirk where releasing the click while still
            over the button auto-selects whatever menu item happens to be
            under the cursor."""
            try:
                widget.update_idletasks()
                x = widget.winfo_rootx()
                y = widget.winfo_rooty() + widget.winfo_height()
                menu.tk_popup(x, y)
            finally:
                menu.grab_release()

        # ── Primary action — Provision active profile ──
        active_pname = (self._active_user_profile
                        or self._active_profile)

        provision_btn = tk.Button(
            bar, text="Provision",
            bg=T.GREEN, fg=T.BG,
            activebackground=T.GREEN, activeforeground=T.BG,
            cursor="hand2" if not is_pending else "arrow",
            state="normal" if not is_pending else "disabled",
            command=lambda p=active_pname: self._provision_profile(p),
            **_btn_kw)
        provision_btn.pack(side="left", padx=(0, 6))

        self._setup_action_btns = [provision_btn]

        # ── Quick Actions popup ──
        actions_btn = tk.Button(
            bar, text="Quick Actions  ▾",
            bg=T.ACCENT, fg=T.BG,
            activebackground=T.ACCENT, activeforeground=T.BG,
            cursor="hand2" if not is_pending else "arrow",
            state="normal" if not is_pending else "disabled",
            **_btn_kw)
        actions_menu = tk.Menu(actions_btn, **_menu_kw)
        for label, cmd in [
            ("Check for Updates",
             lambda: self._run_async(self._check_for_updates)),
            (f"Update ALL ({self._active_profile})",
             lambda: self._run_async(self._update_all_from_github)),
            ("Clear ARCropolis Cache",
             lambda: self._run_async(self._clear_cache)),
            ("Scan romfs Conflicts",
             lambda: self._run_async(self._scan_romfs_conflicts)),
            ("Refresh Setup Checks",
             lambda: self._show_setup()),
        ]:
            actions_menu.add_command(label=label, command=cmd)
        actions_menu.add_separator()
        actions_menu.add_command(label="Clean Provision (WIPE SD)…",
                                 foreground=T.RED,
                                 command=self._clean_provision_confirm)
        actions_btn.configure(
            command=lambda: _popup_below(actions_menu, actions_btn))
        actions_btn.pack(side="left", padx=(0, 6))
        self._setup_action_btns.append(actions_btn)

        # Clear All Mods — promoted to its own button so it's always visible.
        tk.Button(bar, text="Clear All Mods",
                  bg=T.PEACH, fg=T.BG,
                  activebackground=T.PEACH, activeforeground=T.BG,
                  cursor="hand2",
                  command=self._clear_all_mods_confirm,
                  **_btn_kw).pack(side="left", padx=(0, 6))

        # Inject Payload — opens a dedicated dialog window.  Always reads
        # "Inject Payload"; it goes green and clickable only when a Switch
        # in RCM mode is detected (the device-state label to the right
        # already explains *why* it's disabled, so the button label
        # doesn't need to repeat that).
        rcm_ready = self._rcm_detected
        self._rcm_inject_btn = tk.Button(
            bar, text="⚡ Inject Payload",
            bg=T.GREEN if rcm_ready else T.SURFACE1,
            fg=T.CRUST if rcm_ready else T.OVERLAY,
            activebackground=T.GREEN if rcm_ready else T.SURFACE1,
            activeforeground=T.CRUST if rcm_ready else T.OVERLAY,
            cursor="hand2" if rcm_ready else "arrow",
            state="normal" if rcm_ready else "disabled",
            command=self._open_payload_dialog, **_btn_kw)
        self._rcm_inject_btn.pack(side="left", padx=(0, 6))

        # Live RCM detection status to the right.
        self._rcm_device_label = tk.Label(
            bar,
            text=("🎮 Switch in RCM mode" if self._rcm_detected
                  else "🎮 No Switch in RCM mode"),
            bg=T.BG,
            fg=T.GREEN if self._rcm_detected else T.OVERLAY,
            font=(T.FONT, T.SZ_SM))
        self._rcm_device_label.pack(side="right", padx=(8, 0))

        # Legacy attribute referenced elsewhere — keep it alive but hidden.
        self._rcm_status_label = tk.Label(af, text="", bg=T.BG)

        # Show the fixed pane — repack so actions is at bottom, canvas above
        self._canvas_wrap.pack_forget()
        af.pack(side="bottom", fill="x")
        self._canvas_wrap.pack(fill="both", expand=True)

    def _run_health_checks(self, latest=None):
        """Run all component checks, return list of dicts.
        `latest` is a dict of GITHUB_REPOS key -> github_latest_asset result."""
        if latest is None:
            latest = {}
        checks = []

        # 1. SD Card
        sd_ok = os.path.exists(SD_CARD) and os.path.isdir(SD_CARD)
        sd_detail = "Connected" if sd_ok else "NOT FOUND — insert SD card"

        # Check filesystem type — exFAT is known to corrupt Atmosphere files
        sd_fs_warn = False
        if sd_ok:
            try:
                import subprocess as _sp
                _vol = _sp.run(
                    ["powershell", "-NoProfile", "-Command",
                     f"(Get-Volume -DriveLetter '{SD_CARD[0]}').FileSystemType"],
                    capture_output=True, text=True, timeout=5,
                    creationflags=_sp.CREATE_NO_WINDOW)
                fs_type = _vol.stdout.strip()
                if fs_type:
                    sd_detail += f"  ({fs_type})"
                    if fs_type.lower() == "exfat":
                        sd_detail += ("  ⚠ exFAT — REFORMAT TO FAT32!\n"
                                      "  exFAT causes 'Unable to identify package' errors")
                        sd_fs_warn = True
            except Exception:
                pass

        checks.append({
            "name": "SD Card",
            "desc": f"Drive {SD_CARD}",
            "ok": sd_ok and not sd_fs_warn,
            "warn": sd_fs_warn,
            "detail": sd_detail,
            "fixable": False,
            "section": "core",
        })

        if not sd_ok:
            # Core stubs
            for name in ["Atmosphere CFW", "Hekate / Fusée", "sys-patch",
                         "Skyline (exefs)", "ARCropolis"]:
                checks.append({"name": name, "desc": "", "ok": False,
                                "detail": "SD card required", "fixable": False,
                                "section": "core"})
            # Profile plugin stubs
            profile = PROVISIONING_PROFILES.get(self._active_profile, {})
            for nro in self._active_plugin_list():
                pname = KNOWN_PLUGINS.get(nro, {}).get("name", nro)
                checks.append({"name": pname, "desc": "", "ok": False,
                                "detail": "SD card required", "fixable": False,
                                "section": "profile"})
            # Health stubs
            for name in ["Mods Folder", "Cache Status"]:
                checks.append({"name": name, "desc": "", "ok": False,
                                "detail": "SD card required", "fixable": False,
                                "section": "health"})
            return checks

        # 2. Atmosphere CFW
        atmo_dir = os.path.join(SD_CARD, "atmosphere")
        atmo_ok = os.path.exists(atmo_dir)
        atmo_info = latest.get("atmosphere")
        atmo_unofficial = latest.get("atmosphere_unofficial")
        atmo_detail = "NOT FOUND"
        if atmo_ok:
            atmo_detail = "Installed"
            if atmo_info:
                atmo_detail += f"  (stable: {atmo_info['version']})"
            if atmo_unofficial:
                atmo_detail += f"\n  ⚡ Unofficial available: {atmo_unofficial['version']}"
        checks.append({
            "name": "Atmosphere CFW",
            "desc": "Custom firmware",
            "ok": atmo_ok,
            "detail": atmo_detail,
            "fixable": True, "fix_key": "atmosphere",
            "section": "core",
        })

        # 2b. Hekate bootloader / Fusée payload
        bootloader_dir = os.path.join(SD_CARD, "bootloader")
        hekate_ok = os.path.exists(bootloader_dir)
        fusee_path = os.path.join(bootloader_dir, "payloads", "fusee.bin") \
            if hekate_ok else ""
        fusee_alt = os.path.join(SD_CARD, "bootloader", "update.bin")
        hekate_info = latest.get("hekate")
        fusee_info = latest.get("fusee")
        hekate_detail = "NOT FOUND"
        if hekate_ok:
            hekate_detail = "Installed"
            if hekate_info:
                hekate_detail += f"  (latest: {hekate_info['version']})"
            # Check fusee
            fusee_exists = os.path.exists(fusee_path) if fusee_path else False
            if not fusee_exists:
                hekate_detail += "  ⚠ fusee.bin missing"
        checks.append({
            "name": "Hekate / Fusée",
            "desc": "Bootloader + payload",
            "ok": hekate_ok,
            "detail": hekate_detail,
            "fixable": True, "fix_key": "hekate",
            "section": "core",
        })

        # 2c. sys-patch sysmodule
        sys_patch_dir = os.path.join(SD_CARD, "atmosphere", "contents", "420000000000000B")
        sys_patch_nsp = os.path.join(sys_patch_dir, "exefs.nsp")
        sys_patch_flag = os.path.join(sys_patch_dir, "flags", "boot2.flag")
        sys_patch_ok = os.path.exists(sys_patch_nsp)
        sys_patch_info = latest.get("sys_patch")
        sys_patch_detail = "Missing"
        if sys_patch_ok:
            sys_patch_detail = "Installed"
            if not os.path.exists(sys_patch_flag):
                sys_patch_detail += "  ⚠ boot2.flag missing"
            if sys_patch_info:
                sys_patch_detail += f"  (latest: {sys_patch_info['version']})"
        elif sys_patch_info:
            sys_patch_detail = f"Missing (latest: {sys_patch_info['version']})"
        checks.append({
            "name": "sys-patch",
            "desc": "Signature patches sysmodule",
            "ok": sys_patch_ok,
            "detail": sys_patch_detail,
            "fixable": True, "fix_key": "sys_patch",
            "section": "core",
        })

        # 3. Skyline (exefs)
        subsdk = os.path.join(EXEFS_DIR, "subsdk9")
        npdm = os.path.join(EXEFS_DIR, "main.npdm")
        skyline_ok = os.path.exists(subsdk) and os.path.exists(npdm)
        skyline_detail = "Missing"
        sky_info = latest.get("skyline")
        if skyline_ok:
            skyline_detail = "Installed"
            if sky_info:
                skyline_detail += f"  (latest: {sky_info['version']})"
        checks.append({
            "name": "Skyline (exefs)",
            "desc": "Plugin framework",
            "ok": skyline_ok,
            "detail": skyline_detail,
            "fixable": True, "fix_key": "skyline",
            "section": "core",
        })

        # 4-6. Plugins — only check core + active profile plugins
        plugin_repo_map = {
            "libarcropolis.nro": "arcropolis",
            "liblatency_slider_de.nro": "latency_slider",
            "libless_delay.nro": "less_delay",
        }
        profile = PROVISIONING_PROFILES.get(self._active_profile, {})
        profile_plugins = set(CORE_PLUGINS + self._active_plugin_list())
        for nro_name, info in KNOWN_PLUGINS.items():
            if nro_name not in profile_plugins:
                continue
            nro_path = os.path.join(PLUGINS_DIR, nro_name)
            present = os.path.exists(nro_path)
            repo_key = plugin_repo_map.get(nro_name)
            gh = latest.get(repo_key) if repo_key else None

            detail = "Missing"
            warn = False
            if present:
                sz = os.path.getsize(nro_path)
                detail = "Installed"
                if gh:
                    gh_sz = gh["size"]
                    # For direct .nro downloads, size comparison is meaningful
                    if repo_key != "arcropolis" and gh_sz > 0 and abs(sz - gh_sz) > 512:
                        detail += f"  ⚠ update available ({gh['version']})"
                        warn = True
                    else:
                        detail += f"  ✓ {gh['version']}"
            else:
                if gh:
                    detail = f"Missing (latest: {gh['version']})"

            checks.append({
                "name": info["name"],
                "desc": info["desc"],
                "ok": present and not warn,
                "warn": warn,
                "detail": detail,
                "fixable": True, "fix_key": nro_name,
                "section": "core" if nro_name in CORE_PLUGINS else "profile",
            })

        # 4b. Detect installed plugins NOT in this profile (candidates for removal)
        for nro_name, info in KNOWN_PLUGINS.items():
            if nro_name in profile_plugins:
                continue  # already checked above
            nro_path = os.path.join(PLUGINS_DIR, nro_name)
            if os.path.exists(nro_path):
                sz = os.path.getsize(nro_path)
                checks.append({
                    "name": info["name"],
                    "desc": info["desc"],
                    "ok": True,
                    "warn": True,
                    "detail": f"Not in {self._active_profile} — removed on Provision",
                    "fixable": True, "fix_key": f"remove:{nro_name}",
                    "section": "profile",
                })

        # 7. Mods folder
        mods_ok = os.path.exists(ARCROPOLIS_MODS)
        mod_count = 0
        if mods_ok:
            mod_count = len([d for d in os.listdir(ARCROPOLIS_MODS)
                            if os.path.isdir(os.path.join(ARCROPOLIS_MODS, d))])
        checks.append({
            "name": "Mods Folder",
            "desc": "Installed skins directory",
            "ok": mods_ok,
            "detail": f"{mod_count} skin(s) installed" if mods_ok else "NOT FOUND",
            "fixable": True, "fix_key": "mods_folder",
            "section": "health",
        })

        # 8. Disabled plugins folder — leftover disabled plugins can cause issues
        disabled_dir = os.path.join(PLUGINS_DIR, "disabled")
        disabled_nros = []
        if os.path.exists(disabled_dir):
            disabled_nros = [f for f in os.listdir(disabled_dir) if f.endswith(".nro")]
        if disabled_nros:
            checks.append({
                "name": "Disabled Plugins",
                "desc": "Leftover disabled plugins",
                "ok": True,  # not critical
                "warn": True,
                "detail": f"{len(disabled_nros)} disabled plugin(s): {', '.join(disabled_nros)}",
                "fixable": False,
                "section": "health",
            })

        # 9. Loose romfs files (from old manual installs, can conflict with ARCropolis)
        loose_romfs = os.path.join(ROMFS_DIR, "ark")
        has_loose = os.path.exists(loose_romfs) and any(
            os.scandir(loose_romfs)) if os.path.exists(loose_romfs) else False
        # Also check for fighter/ dir directly in romfs
        loose_fighter = os.path.join(ROMFS_DIR, "fighter")
        has_loose_fighter = os.path.exists(loose_fighter)
        if has_loose or has_loose_fighter:
            checks.append({
                "name": "Loose romfs Files",
                "desc": "Legacy romfs files — may conflict",
                "ok": False,
                "detail": "Found loose files — should be removed",
                "fixable": True, "fix_key": "loose_romfs",
                "section": "health",
            })

        # 10. Slot conflicts — multiple mods claiming the same fighter+slot
        conflicts = self._find_slot_conflicts()
        if conflicts:
            n = len(conflicts)
            # Build a short summary of the worst offenders
            examples = []
            for (fighter, slot), mods in sorted(conflicts.items())[:3]:
                f_disp = INTERNAL_TO_DISPLAY.get(fighter, fighter)
                examples.append(f"{f_disp}/{slot} ({len(mods)} mods)")
            detail = f"{n} conflict(s): {', '.join(examples)}"
            if n > 3:
                detail += f" + {n - 3} more"
            checks.append({
                "name": "Slot Conflicts",
                "desc": "Multiple mods claim the same fighter+slot — only one will load",
                "ok": False,
                "detail": detail,
                "fixable": True, "fix_key": "conflicts",
                "section": "health",
            })

        # 11. Cache / romfs_metadata
        meta_path = os.path.join(ATMOSPHERE_CONTENTS, "romfs_metadata.bin")
        meta_exists = os.path.exists(meta_path)
        cache_dir = os.path.join(SD_CARD, "ultimate", "cache")
        cache_exists = os.path.exists(cache_dir) and any(
            True for f in os.listdir(cache_dir) if os.path.isfile(os.path.join(cache_dir, f))
        ) if os.path.exists(cache_dir) else False
        cache_detail = "Clean"
        cache_ok = True
        if meta_exists:
            meta_sz = os.path.getsize(meta_path)
            cache_detail = f"romfs_metadata.bin exists ({meta_sz / 1024:.0f} KB) — may be stale"
            cache_ok = False
        if cache_exists:
            n_cache = len([f for f in os.listdir(cache_dir)
                          if os.path.isfile(os.path.join(cache_dir, f))])
            cache_detail += f"  |  {n_cache} cache file(s)"
            cache_ok = False
        checks.append({
            "name": "Cache Status",
            "desc": "Stale cache can cause crashes after mod/plugin changes",
            "ok": cache_ok,
            "detail": cache_detail,
            "fixable": meta_exists or cache_exists,
            "fix_key": "cache",
            "section": "health",
        })

        return checks

    def _add_check_row(self, check, parent=None):
        """Add a single health-check row to the results panel."""
        container = parent or self.results_inner
        row = tk.Frame(container, bg=T.BG, padx=10, pady=5)
        row.pack(fill="x", padx=8, pady=2)

        # Status icon
        if check.get("warn") and check["ok"]:
            icon_text = "⚠"
            icon_fg = T.YELLOW
        elif check["ok"]:
            icon_text = "✓"
            icon_fg = T.GREEN
        else:
            icon_text = "✗"
            icon_fg = T.RED

        tk.Label(row, text=icon_text, width=2,
                 bg=T.BG, fg=icon_fg,
                 font=(T.FONT, T.SZ_XXL, "bold")).pack(side="left", padx=(0, 6))

        # Info
        info_frame = tk.Frame(row, bg=T.BG)
        info_frame.pack(side="left", fill="x", expand=True)

        tk.Label(info_frame, text=check["name"],
                 bg=T.BG, fg=T.FG,
                 font=(T.FONT, T.SZ_LG, "bold"), anchor="w",
                 justify="left").pack(fill="x")

        # Color: green=ok, yellow=warn, red=fail
        if check.get("warn"):
            detail_fg = T.YELLOW
        elif check["ok"]:
            detail_fg = T.SUBTEXT
        else:
            detail_fg = T.RED

        detail_text = check["detail"]
        if check.get("desc"):
            detail_text = f"{check['desc']}  —  {detail_text}"
        tk.Label(info_frame, text=detail_text,
                 bg=T.BG, fg=detail_fg,
                 font=(T.FONT, T.SZ_SM), anchor="w", justify="left",
                 wraplength=580).pack(fill="x")

        # Fix / Update / Remove / Resolve button
        if check.get("fixable") and (not check["ok"] or check.get("warn")):
            fix_key = check.get("fix_key", "")
            if fix_key.startswith("remove:"):
                btn_text = "Remove"
                btn_color = T.RED
            elif fix_key == "conflicts":
                btn_text = "Resolve"
                btn_color = T.PEACH
            elif check.get("warn") and check["ok"]:
                btn_text = "Update"
                btn_color = T.PEACH
            else:
                btn_text = "Fix"
                btn_color = T.PEACH
            tk.Button(row, text=btn_text, width=7,
                      bg=btn_color, fg=T.BG,
                      font=(T.FONT, T.SZ_SM, "bold"),
                      relief="flat", cursor="hand2",
                      command=lambda k=fix_key: self._run_async(
                          self._fix_component, k)
                      ).pack(side="right", padx=(6, 0))

    def _fix_component(self, key):
        """Fix a single missing component, or remove an extra plugin."""
        if key.startswith("remove:"):
            nro_name = key[len("remove:"):]
            self._remove_plugin(nro_name)
        elif key == "skyline":
            self._install_skyline()
        elif key in LOCAL_PLUGINS:
            self._install_plugin(key)
        elif key == "atmosphere":
            self._update_atmosphere()
        elif key == "hekate":
            self._update_hekate()
        elif key == "sys_patch":
            self._update_sys_patch()
        elif key == "mods_folder":
            os.makedirs(ARCROPOLIS_MODS, exist_ok=True)
            print(f"  Created mods folder: {ARCROPOLIS_MODS}")
        elif key == "cache":
            self._clear_cache()
            return  # _clear_cache already refreshes
        elif key == "loose_romfs":
            self._clean_loose_romfs()
        elif key == "conflicts":
            conflicts = self._find_slot_conflicts()
            if conflicts:
                self.root.after(0, lambda c=conflicts: self._show_conflict_resolver(c))
            else:
                print("  No conflicts found.")
            return  # resolver handles its own refresh
        # Refresh the setup view
        self.root.after(500, self._show_setup)

    def _open_profile_setup_dialog(self):
        """Manage the user-defined profiles: pick the active one, edit
        flags (Wifi-Safe, Unofficial Atmosphere) and the plugin set, see
        whether the inserted SD card already matches the profile, and
        provision the card from here.

        Each profile is the full per-Switch configuration — the same
        fields shown here are also offered in the Create Profile dialog
        so nothing is hidden behind a separate "template" concept.
        """
        win = tk.Toplevel(self.root)
        win.title("Manage Profiles")
        win.configure(bg=T.SURFACE)
        win.geometry("640x560")
        win.transient(self.root)

        tk.Label(win, text="Manage Profiles",
                 bg=T.SURFACE, fg=T.ACCENT,
                 font=(T.FONT, T.SZ_LG, "bold")).pack(anchor="w",
                                                      padx=14, pady=(12, 4))
        tk.Label(win,
                 text="Each profile is a self-contained target — pick its "
                 "plugins, flag whether it must stay\nWifi-Safe, and "
                 "choose stable vs. unofficial Atmosphere.  "
                 "Provision Card writes the selection to SD.",
                 bg=T.SURFACE, fg=T.OVERLAY, justify="left",
                 font=(T.FONT, T.SZ_SM)).pack(anchor="w", padx=14,
                                              pady=(0, 8))

        body = tk.Frame(win, bg=T.SURFACE)
        body.pack(fill="both", expand=True, padx=14, pady=(0, 4))

        list_frame = tk.Frame(body, bg=T.SURFACE)
        list_frame.pack(side="left", fill="y")
        tk.Label(list_frame, text="Profiles", bg=T.SURFACE, fg=T.FG,
                 font=(T.FONT, T.SZ_SM, "bold")).pack(anchor="w")
        listbox = tk.Listbox(list_frame, width=22, height=14,
                             bg=T.CRUST, fg=T.FG,
                             selectbackground=T.ACCENT,
                             selectforeground=T.BG,
                             highlightthickness=0,
                             font=(T.FONT, T.SZ_SM))
        listbox.pack(fill="y", expand=True)

        edit_frame = tk.Frame(body, bg=T.SURFACE)
        edit_frame.pack(side="left", fill="both", expand=True, padx=(14, 0))

        tk.Label(edit_frame, text="Name:", bg=T.SURFACE, fg=T.FG,
                 font=(T.FONT, T.SZ_SM)).grid(row=0, column=0, sticky="w",
                                              pady=(4, 2))
        name_var = tk.StringVar()
        name_entry = tk.Entry(edit_frame, textvariable=name_var, width=28,
                              bg=T.CRUST, fg=T.FG, insertbackground=T.FG,
                              relief="flat", font=(T.FONT, T.SZ_SM))
        name_entry.grid(row=0, column=1, sticky="we", pady=(4, 2))

        wifi_var = tk.BooleanVar(value=True)
        tk.Checkbutton(
            edit_frame, text="Wifi-Safe (block gameplay-affecting mods)",
            variable=wifi_var,
            bg=T.SURFACE, fg=T.FG, selectcolor=T.CRUST,
            activebackground=T.SURFACE, activeforeground=T.FG,
            font=(T.FONT, T.SZ_SM)).grid(row=1, column=0, columnspan=2,
                                          sticky="w", pady=(8, 0))

        atmo_var = tk.BooleanVar(value=True)
        tk.Checkbutton(
            edit_frame,
            text=f"Use unofficial Atmosphere ({ATMOSPHERE_SUPPORT_BRANCH})",
            variable=atmo_var,
            bg=T.SURFACE, fg=T.PEACH, selectcolor=T.CRUST,
            activebackground=T.SURFACE, activeforeground=T.PEACH,
            font=(T.FONT, T.SZ_SM)).grid(row=2, column=0, columnspan=2,
                                          sticky="w", pady=(2, 4))

        # Plugin checkboxes — one for every known optional plugin.  We no
        # longer hide any behind a "template" concept; the user just
        # picks what they want and the card is provisioned to match.
        tk.Label(edit_frame, text="Plugins:", bg=T.SURFACE, fg=T.FG,
                 font=(T.FONT, T.SZ_SM, "bold")).grid(row=3, column=0,
                                                       sticky="nw",
                                                       pady=(8, 2))
        plugins_frame = tk.Frame(edit_frame, bg=T.SURFACE)
        plugins_frame.grid(row=3, column=1, sticky="we", pady=(8, 2))
        plugin_vars = {}
        for nro, meta in KNOWN_PLUGINS.items():
            if nro in CORE_PLUGINS:
                continue  # ARCropolis is mandatory, not user-toggleable
            v = tk.BooleanVar(value=False)
            plugin_vars[nro] = v
            row = tk.Frame(plugins_frame, bg=T.SURFACE)
            row.pack(fill="x", anchor="w")
            tk.Checkbutton(
                row, text=meta.get("name", nro), variable=v,
                bg=T.SURFACE, fg=T.FG, selectcolor=T.CRUST,
                activebackground=T.SURFACE, activeforeground=T.FG,
                font=(T.FONT, T.SZ_SM)).pack(side="left")
            desc = meta.get("desc", "")
            if desc:
                tk.Label(row, text=f"— {desc}", bg=T.SURFACE,
                         fg=T.OVERLAY,
                         font=(T.FONT, T.SZ_XS)).pack(side="left",
                                                       padx=(6, 0))

        # Live "card matches profile?" banner — recomputed whenever the
        # selection or any toggle changes so the user can see at a
        # glance whether Provision Card would actually do anything.
        status_label = tk.Label(edit_frame, text="", bg=T.SURFACE,
                                fg=T.OVERLAY, justify="left",
                                wraplength=360,
                                font=(T.FONT, T.SZ_SM, "bold"))
        status_label.grid(row=4, column=0, columnspan=2, sticky="w",
                          pady=(10, 2))

        info_label = tk.Label(edit_frame, text="", bg=T.SURFACE,
                              fg=T.OVERLAY, justify="left", wraplength=360,
                              font=(T.FONT, T.SZ_XS))
        info_label.grid(row=5, column=0, columnspan=2, sticky="w",
                        pady=(2, 0))
        edit_frame.grid_columnconfigure(1, weight=1)

        state = {"original_name": None}
        loading = {"flag": False}  # re-entrancy guard for _autosave

        def _current_cfg_from_ui():
            return {
                "template": "Custom",
                "wifi_safe": bool(wifi_var.get()),
                "unofficial_atmo": bool(atmo_var.get()),
                "plugins": [nro for nro, v in plugin_vars.items()
                            if v.get()],
            }

        def _refresh_status(*_args):
            cfg = _current_cfg_from_ui()
            _, msg, color = self._profile_provision_status(cfg)
            status_label.configure(text=msg, fg=color)

        wifi_var.trace_add("write", _refresh_status)
        atmo_var.trace_add("write", _refresh_status)
        for v in plugin_vars.values():
            v.trace_add("write", _refresh_status)

        def _load_selected(_event=None):
            sel = listbox.curselection()
            if not sel:
                return
            raw = listbox.get(sel[0])
            name = raw[2:]
            data = load_profiles().get(name, {})
            cfg = profile_config(data)
            loading["flag"] = True
            try:
                state["original_name"] = name
                name_var.set(name)
                wifi_var.set(cfg["wifi_safe"])
                atmo_var.set(cfg["unofficial_atmo"])
                sel_plugins = set(cfg["plugins"])
                for nro, v in plugin_vars.items():
                    v.set(nro in sel_plugins)
            finally:
                loading["flag"] = False
            mod_count = len(data.get("mods", []))
            info_label.configure(text=f"{mod_count} mod(s) saved.")
            _refresh_status()

        def _refresh_listbox(select=None):
            listbox.delete(0, "end")
            names = sorted(load_profiles().keys())
            for n in names:
                marker = "● " if n == self._active_user_profile else "  "
                listbox.insert("end", f"{marker}{n}")
            if select and select in names:
                idx = names.index(select)
                listbox.selection_clear(0, "end")
                listbox.selection_set(idx)
                listbox.activate(idx)
                _load_selected()
            elif names:
                listbox.selection_set(0)
                _load_selected()

        listbox.bind("<<ListboxSelect>>", _load_selected)

        btn_row = tk.Frame(win, bg=T.SURFACE)
        btn_row.pack(fill="x", padx=14, pady=(4, 12))

        def _save():
            new_name = name_var.get().strip()
            old = state["original_name"]
            if not new_name:
                messagebox.showwarning("Name Required",
                                       "Profile name cannot be empty.",
                                       parent=win)
                return
            profiles = load_profiles()
            if old and old != new_name and new_name in profiles:
                messagebox.showerror(
                    "Name Conflict",
                    f"A profile named '{new_name}' already exists.",
                    parent=win)
                return
            old_data = profiles.pop(old, {}) if old else {}
            data = old_data
            data.setdefault("created", datetime.now().isoformat())
            data.setdefault("mods", [])
            # Templates were retired — every profile is "Custom" so the
            # legacy code paths that still inspect ``template`` keep
            # behaving sensibly.
            data["template"] = "Custom"
            data["wifi_safe"] = bool(wifi_var.get())
            data["unofficial_atmo"] = bool(atmo_var.get())
            data["plugins"] = [nro for nro, v in plugin_vars.items()
                               if v.get()]
            profiles[new_name] = data
            save_profiles(profiles)
            if self._active_user_profile == old:
                self._active_user_profile = new_name
            _refresh_listbox(select=new_name)
            self._refresh_profile_list_silent()
            # Refresh the "card matches profile?" banner — the user
            # stays in the dialog so they can click Provision Card next
            # if the card needs to be updated.  No surprise navigation.
            _refresh_status()

        def _new():
            profiles = load_profiles()
            base = "New Profile"
            name = base
            i = 2
            while name in profiles:
                name = f"{base} {i}"
                i += 1
            # Default new profiles to all known plugins enabled, Wifi-Safe
            # on, unofficial Atmosphere on — the common "tournament
            # Switch" baseline.
            all_plugins = [nro for nro in KNOWN_PLUGINS
                           if nro not in CORE_PLUGINS]
            profiles[name] = {
                "created": datetime.now().isoformat(),
                "mods": [],
                "template": "Custom",
                "wifi_safe": True,
                "unofficial_atmo": True,
                "plugins": all_plugins,
            }
            save_profiles(profiles)
            # Newly-created profile becomes the active one if there isn't
            # one yet, so the Setup tab's "Provision: <name>" button
            # picks it up without the user having to click Refresh.
            if not self._active_user_profile:
                self._active_user_profile = name
                cfg = profile_config(profiles[name])
                if cfg["template"] in PROVISIONING_PROFILES:
                    self._active_profile = cfg["template"]
                self._use_unofficial_atmo = cfg["unofficial_atmo"]
            # Invalidate the cached Setup checks so the next visit (or
            # the immediate re-render below) reflects the new profile
            # without requiring a manual Refresh click.
            self._setup_checks_done = False
            _refresh_listbox(select=name)
            self._refresh_profile_list_silent()
            # If the Setup view is currently rendered behind this dialog,
            # rebuild it now so the Provision button label updates.
            if self._active_view == "setup":
                self._show_setup()

        def _delete():
            old = state["original_name"]
            if not old:
                return
            if not messagebox.askyesno(
                    "Delete Profile",
                    f"Permanently delete profile '{old}'?\n"
                    "Saved mods in this profile will be lost (the SD card "
                    "itself is not touched).",
                    icon="warning", parent=win):
                return
            profiles = load_profiles()
            profiles.pop(old, None)
            save_profiles(profiles)
            if self._active_user_profile == old:
                remaining = sorted(profiles.keys())
                self._active_user_profile = (remaining[0] if remaining
                                             else None)
            if self._profile_mode_target == old:
                self._profile_mode_target = self._active_user_profile
            _refresh_listbox()
            self._refresh_profile_list_silent()
            self._show_setup()

        def _set_active():
            old = state["original_name"]
            if not old:
                return
            cfg = profile_config(load_profiles().get(old, {}))
            self._active_user_profile = old
            if cfg["template"] in PROVISIONING_PROFILES:
                self._active_profile = cfg["template"]
            self._use_unofficial_atmo = cfg["unofficial_atmo"]
            self._setup_checks_done = False
            _refresh_listbox(select=old)
            self._show_setup()

        def _provision_card_clicked():
            old = state["original_name"]
            if not old:
                return
            # Persist any pending edits in the form first so the card
            # actually gets what the user sees.  Re-fetch listbox name
            # because _save may rename the entry.
            _save()
            target = self._active_user_profile or old
            win.destroy()
            self._switch_view("setup")
            self._provision_profile(target)

        def _duplicate():
            old = state["original_name"]
            if not old:
                return
            # Persist any pending edits first so the copy reflects what
            # the user sees in the form, not whatever was last saved.
            _save()
            # _save may have renamed; pick up whatever the active name is.
            src_name = self._active_user_profile if (
                self._active_user_profile == name_var.get().strip()
            ) else name_var.get().strip() or old
            new_name = duplicate_profile(src_name)
            if not new_name:
                messagebox.showerror("Duplicate",
                                     "Could not duplicate profile.",
                                     parent=win)
                return
            _refresh_listbox(select=new_name)
            self._refresh_profile_list_silent()

        tk.Button(btn_row, text="New", width=8, bg=T.SURFACE1, fg=T.FG,
                  font=(T.FONT, T.SZ_SM, "bold"), relief="flat",
                  cursor="hand2", command=_new).pack(side="left",
                                                     padx=(0, 4))
        tk.Button(btn_row, text="Duplicate", width=10, bg=T.SURFACE1,
                  fg=T.FG, font=(T.FONT, T.SZ_SM, "bold"), relief="flat",
                  cursor="hand2", command=_duplicate).pack(side="left",
                                                           padx=(0, 4))
        tk.Button(btn_row, text="Delete", width=8, bg=T.RED, fg=T.CRUST,
                  font=(T.FONT, T.SZ_SM, "bold"), relief="flat",
                  cursor="hand2", command=_delete).pack(side="left",
                                                        padx=(0, 4))
        provision_btn = tk.Button(
            btn_row, text="Provision Card", width=14, bg=T.PEACH,
            fg=T.CRUST, font=(T.FONT, T.SZ_SM, "bold"),
            relief="flat", cursor="hand2",
            command=_provision_card_clicked)
        provision_btn.pack(side="left", padx=(0, 4))
        tk.Button(btn_row, text="Close", width=8, bg=T.SURFACE1, fg=T.FG,
                  font=(T.FONT, T.SZ_SM), relief="flat", cursor="hand2",
                  command=win.destroy).pack(side="right", padx=(0, 6))

        # Wire status banner → also drives the Provision Card button.
        # When the card already matches the in-memory selection the
        # button is greyed; in any other state (drift, unprovisioned,
        # no SD) it lights back up so the user can act on it.
        def _refresh_provision_button(*_args):
            cfg = _current_cfg_from_ui()
            st, _msg, _color = self._profile_provision_status(cfg)
            if st == "match":
                provision_btn.configure(state="disabled", cursor="arrow",
                                        bg=T.SURFACE1, fg=T.OVERLAY,
                                        text="Card Matches Profile")
            elif st == "no_sd":
                provision_btn.configure(state="disabled", cursor="arrow",
                                        bg=T.SURFACE1, fg=T.OVERLAY,
                                        text="Insert SD to Provision")
            else:
                provision_btn.configure(state="normal", cursor="hand2",
                                        bg=T.PEACH, fg=T.CRUST,
                                        text="Provision Card")

        wifi_var.trace_add("write", _refresh_provision_button)
        atmo_var.trace_add("write", _refresh_provision_button)
        for v in plugin_vars.values():
            v.trace_add("write", _refresh_provision_button)

        # Auto-persist toggle changes — Save button was confusing UX so
        # the dialog now writes through every flag flip immediately.
        # Name edits are committed on focus-out of the entry (below) so
        # mid-typing doesn't spam half-typed names into the JSON.
        def _autosave(*_args):
            if loading["flag"]:
                return
            if state["original_name"]:
                _save()
        wifi_var.trace_add("write", _autosave)
        atmo_var.trace_add("write", _autosave)
        for v in plugin_vars.values():
            v.trace_add("write", _autosave)
        name_entry.bind("<FocusOut>", lambda _e: _autosave())
        name_entry.bind("<Return>", lambda _e: _autosave())

        # Initial paint after listbox seeds the form.
        win.after(0, _refresh_provision_button)

        _refresh_listbox(select=self._active_user_profile)

    def _active_plugin_list(self):
        """Return the effective plugin list for the active user profile.

        Falls back to the active provisioning template's plugin list when
        no user profile is selected, so first-run / no-profile flows
        still provision a sensible default.
        """
        prof = load_profiles().get(self._active_user_profile or "")
        if prof is None:
            return list(PROVISIONING_PROFILES.get(
                self._active_profile, {}).get("plugins", []))
        return profile_config(prof)["plugins"]

    def _profile_provision_status(self, cfg):
        """Return ``(state, msg, color)`` describing whether the SD card
        currently matches the supplied profile config.

        ``state`` is one of ``"match"``, ``"drift"``, ``"unprovisioned"``,
        ``"no_sd"`` so callers can branch.  Used by the Manage Profiles
        dialog to show whether the inserted card is already configured
        for the selected profile.
        """
        if not os.path.exists(SD_CARD):
            return "no_sd", "No SD card detected", T.OVERLAY
        if not os.path.isdir(PLUGINS_DIR):
            return "unprovisioned", "Card not provisioned (no plugins dir)", T.RED
        non_core = [nro for nro in KNOWN_PLUGINS if nro not in CORE_PLUGINS]
        expected = set(cfg["plugins"]) | set(CORE_PLUGINS)
        present = {nro for nro in KNOWN_PLUGINS
                   if os.path.exists(os.path.join(PLUGINS_DIR, nro))}
        missing = expected - present
        extra = (present & set(non_core)) - expected
        if not missing and not extra:
            return "match", "✅ Card is provisioned for this profile", T.GREEN
        parts = []
        if missing:
            names = ", ".join(KNOWN_PLUGINS.get(n, {}).get("name", n)
                              for n in sorted(missing))
            parts.append(f"missing: {names}")
        if extra:
            names = ", ".join(KNOWN_PLUGINS.get(n, {}).get("name", n)
                              for n in sorted(extra))
            parts.append(f"unexpected: {names}")
        return "drift", "⚠ Card differs — " + "; ".join(parts), T.YELLOW

    def _provision_profile(self, profile_name):
        """Switch the active user profile (and its unofficial-Atmosphere
        flag) before kicking off provisioning. Bound to every entry in
        the Provision dropdown so the user picks "what to provision" in
        one click."""
        all_profiles = load_profiles()
        prof = all_profiles.get(profile_name, {})
        cfg = profile_config(prof)
        self._active_user_profile = profile_name
        # The provisioning template (Competitive plugins, Skins Only, …)
        # determines which plugins land on the SD card.  Fall back to
        # "Skins Only" if the saved template no longer exists.
        if cfg["template"] in PROVISIONING_PROFILES:
            self._active_profile = cfg["template"]
        elif profile_name in PROVISIONING_PROFILES:
            self._active_profile = profile_name
        else:
            self._active_profile = "Skins Only"
        # Each user profile owns its own Atmosphere preference so a
        # tournament Switch can stay on stable while a casual one runs
        # the support branch.
        self._use_unofficial_atmo = cfg["unofficial_atmo"]
        print(f"  Provisioning '{profile_name}'  →  "
              f"template={self._active_profile}, "
              f"wifi_safe={cfg['wifi_safe']}, "
              f"unofficial_atmo={cfg['unofficial_atmo']}")
        self._setup_checks_done = False
        self._show_setup()
        self._run_async(self._provision)

    def _clear_all_mods_confirm(self):
        """Wipe every mod from ``ARCROPOLIS_MODS`` (CFW base files are
        untouched) after a two-step confirmation."""
        if not os.path.exists(SD_CARD):
            messagebox.showerror("No SD Card",
                                 f"SD card not found at {SD_CARD}")
            return

        try:
            existing = [e for e in os.listdir(ARCROPOLIS_MODS)
                        if os.path.isdir(os.path.join(ARCROPOLIS_MODS, e))] \
                if os.path.isdir(ARCROPOLIS_MODS) else []
        except Exception:
            existing = []

        if not existing:
            messagebox.showinfo("Nothing to Clear",
                                "No ARCropolis mods are installed on the "
                                "SD card.")
            return

        if not messagebox.askyesno(
                "⚠ Clear All Mods — Step 1/2",
                f"This will permanently DELETE all {len(existing)} mod "
                f"folder(s) from:\n\n  {ARCROPOLIS_MODS}\n\n"
                "CFW base files (atmosphere, bootloader, switch, …) and "
                "ARCropolis itself are NOT touched.\n\n"
                "Continue?",
                icon="warning"):
            return

        if not messagebox.askyesno(
                "⚠ Clear All Mods — ARE YOU SURE?",
                "LAST CHANCE.\n\n"
                f"Every ARCropolis mod folder will be removed and the "
                f"ARCropolis cache cleared.\n\n"
                "This cannot be undone.\n\nProceed?",
                icon="warning"):
            return

        self._run_async(self._do_clear_all_mods)

    def _do_clear_all_mods(self):
        """Background worker for :meth:`_clear_all_mods_confirm`."""
        print("\n=== Clearing all ARCropolis mods ===\n")
        n = self._wipe_arcropolis_mods()
        print(f"\n=== DONE — removed {n} mod folder(s) ===\n")
        self.root.after(0, self._check_sd)
        self.root.after(100, self._refresh_current_view)
        self.root.after(200, self._show_setup)

    def _open_payload_dialog(self):
        """Open a stand-alone window that handles RCM payload selection
        and injection. Replaces the inline RCM block that used to occupy
        a large slice of the Setup tab."""
        if getattr(self, "_payload_dialog", None) is not None:
            try:
                self._payload_dialog.deiconify()
                self._payload_dialog.lift()
                self._payload_dialog.focus_set()
                return
            except Exception:
                self._payload_dialog = None

        win = tk.Toplevel(self.root)
        self._payload_dialog = win
        win.title("RCM Payload Injection")
        win.configure(bg=T.SURFACE)
        win.geometry("620x320")
        win.transient(self.root)

        def _on_close():
            self._payload_dialog = None
            win.destroy()
        win.protocol("WM_DELETE_WINDOW", _on_close)

        tk.Label(win, text="🚀 RCM Payload Injection",
                 bg=T.SURFACE, fg=T.ACCENT,
                 font=(T.FONT, T.SZ_LG, "bold")).pack(anchor="w",
                                                      padx=14, pady=(12, 6))

        # Resolve TegraRcmSmash.exe (prefer user override).
        smash_path = (getattr(self, "_custom_smash_path", None)
                      if hasattr(self, "_custom_smash_path")
                      and os.path.isfile(self._custom_smash_path)
                      else find_rcm_smash())

        smash_row = tk.Frame(win, bg=T.SURFACE)
        smash_row.pack(fill="x", padx=14, pady=(0, 4))
        smash_ok = smash_path is not None
        tk.Label(smash_row,
                 text=("✓ TegraRcmSmash:  "
                       f"{os.path.basename(smash_path)}" if smash_ok
                       else "✕ TegraRcmSmash:  Not found"),
                 bg=T.SURFACE, fg=T.GREEN if smash_ok else T.RED,
                 font=(T.FONT, T.SZ_SM)).pack(side="left")

        def _browse_smash():
            from tkinter import filedialog
            p = filedialog.askopenfilename(
                title="Locate TegraRcmSmash.exe",
                filetypes=[("Executable", "*.exe")],
                initialdir=SCRIPT_DIR, parent=win)
            if p:
                self._custom_smash_path = p
                _on_close()
                self._open_payload_dialog()
        tk.Button(smash_row, text="Browse…", width=10,
                  bg=T.SURFACE1, fg=T.FG, font=(T.FONT, T.SZ_XS),
                  relief="flat", cursor="hand2",
                  command=_browse_smash).pack(side="left", padx=(8, 0))

        # Gather payloads.
        payload_dirs = [
            os.path.join(SCRIPT_DIR, "payloads"),
            os.path.join(SD_CARD, "bootloader", "payloads"),
            os.path.join(SD_CARD, "atmosphere"),
        ]
        all_payloads = []
        seen = set()
        for d in payload_dirs:
            if os.path.isdir(d):
                for f in sorted(os.listdir(d)):
                    if f.lower().endswith(".bin"):
                        full = os.path.join(d, f)
                        norm = os.path.normcase(os.path.abspath(full))
                        if norm not in seen:
                            seen.add(norm)
                            all_payloads.append(
                                (f"{f}  ({os.path.basename(d)}/)", full))
        if hasattr(self, "_custom_payload_path") \
                and os.path.isfile(self._custom_payload_path):
            cp = self._custom_payload_path
            norm = os.path.normcase(os.path.abspath(cp))
            if norm not in seen:
                seen.add(norm)
                all_payloads.insert(
                    0, (f"{os.path.basename(cp)}  (custom)", cp))

        pay_row = tk.Frame(win, bg=T.SURFACE)
        pay_row.pack(fill="x", padx=14, pady=(8, 4))
        tk.Label(pay_row, text="Payload:", bg=T.SURFACE, fg=T.FG,
                 font=(T.FONT, T.SZ_MD)).pack(side="left", padx=(0, 6))

        payload_map = {name: path for name, path in all_payloads}
        payload_names = list(payload_map.keys())

        pre_select = ""
        if payload_names:
            pre_select = payload_names[0]
            prev = getattr(self, "_selected_payload_path", None) \
                or find_payload()
            if prev:
                for name, path in all_payloads:
                    if os.path.normcase(path) == os.path.normcase(prev):
                        pre_select = name
                        break

        pay_var = tk.StringVar(value=pre_select)
        pay_combo = ttk.Combobox(
            pay_row, textvariable=pay_var,
            values=payload_names, state="readonly", width=42,
            font=(T.FONT, T.SZ_SM))
        pay_combo.pack(side="left", padx=(0, 6))

        def _on_payload_select(_event=None):
            self._selected_payload_path = payload_map.get(pay_var.get(), "")
        pay_combo.bind("<<ComboboxSelected>>", _on_payload_select)
        if pre_select:
            self._selected_payload_path = payload_map.get(pre_select, "")

        def _browse_payload():
            from tkinter import filedialog
            init = os.path.join(SCRIPT_DIR, "payloads")
            if not os.path.isdir(init):
                init = SCRIPT_DIR
            p = filedialog.askopenfilename(
                title="Locate payload .bin (e.g. hekate_latest.bin)",
                filetypes=[("Payload", "*.bin"), ("All files", "*.*")],
                initialdir=init, parent=win)
            if p:
                self._custom_payload_path = p
                self._selected_payload_path = p
                _on_close()
                self._open_payload_dialog()
        tk.Button(pay_row, text="Browse…", width=10,
                  bg=T.SURFACE1, fg=T.FG, font=(T.FONT, T.SZ_XS),
                  relief="flat", cursor="hand2",
                  command=_browse_payload).pack(side="left", padx=(0, 0))

        # Device status.
        dev_label = tk.Label(
            win,
            text=("🎮 Switch detected in RCM mode — ready to inject!"
                  if self._rcm_detected
                  else "🎮 No Switch in RCM mode — connect & enter RCM"),
            bg=T.SURFACE,
            fg=T.GREEN if self._rcm_detected else T.OVERLAY,
            font=(T.FONT, T.SZ_MD))
        dev_label.pack(anchor="w", padx=14, pady=(10, 4))

        # Status / Inject row.
        status_label = tk.Label(win, text="", bg=T.SURFACE, fg=T.OVERLAY,
                                font=(T.FONT, T.SZ_SM))
        status_label.pack(anchor="w", padx=14, pady=(2, 4))

        btn_row = tk.Frame(win, bg=T.SURFACE)
        btn_row.pack(fill="x", padx=14, pady=(8, 12))

        def _inject():
            effective_payload = getattr(self, "_selected_payload_path", "")
            if not smash_path or not effective_payload:
                missing = []
                if not smash_path:
                    missing.append("TegraRcmSmash.exe")
                if not effective_payload:
                    missing.append("payload .bin")
                status_label.configure(
                    text=f"Missing: {', '.join(missing)}", fg=T.RED)
                return
            status_label.configure(text="Injecting…", fg=T.YELLOW)
            status_label.update()
            self._run_async(self._inject_payload, smash_path,
                            effective_payload)

        # Same rule as the bar button: green + clickable iff Switch is in
        # RCM mode AND we have both a smash binary and a payload selected.
        can_inject = bool(smash_path and pre_select and self._rcm_detected)
        tk.Button(
            btn_row,
            text="⚡ Inject Payload",
            bg=T.GREEN if can_inject else T.SURFACE1,
            fg=T.CRUST if can_inject else T.OVERLAY,
            font=(T.FONT, T.SZ_MD, "bold"),
            relief="flat",
            cursor="hand2" if can_inject else "arrow",
            state="normal" if can_inject else "disabled",
            command=_inject).pack(side="left")

        tk.Button(btn_row, text="Close", width=10,
                  bg=T.SURFACE1, fg=T.FG, font=(T.FONT, T.SZ_MD),
                  relief="flat", cursor="hand2",
                  command=_on_close).pack(side="right")

        # Re-route the legacy status label so _inject_payload's UI update
        # writes into our dialog while it's open.
        self._rcm_status_label = status_label

    def _inject_payload(self, smash_exe, payload_path):
        """Background: call TegraRcmSmash.exe to inject the payload."""
        print(f"\n=== RCM Payload Injection ===")
        print(f"  Executable: {smash_exe}")
        print(f"  Payload:    {payload_path}")

        success, message, rc = inject_payload(smash_exe, payload_path)

        def _update_ui():
            if success:
                self._rcm_status_label.configure(
                    text=f"✓ {message}", fg=T.GREEN)
                print(f"  ✓ {message} (RC={rc})")
            else:
                self._rcm_status_label.configure(
                    text=f"✕ RC={rc}", fg=T.RED)
                print(f"  ✕ {message} (RC={rc})")
                # Show detailed error in a messagebox
                from tkinter import messagebox
                messagebox.showerror("Inject Failed", message)

        self.root.after(0, _update_ui)

    # ── Clean Provision (wipe + fresh install) ──────────

    # Folders to DELETE during clean provision (everything CFW-related).
    # Nintendo/ is intentionally preserved — it holds game saves & updates.
    _CLEAN_WIPE_DIRS = ["atmosphere", "bootloader", "config", "switch", "ultimate"]
    _CLEAN_WIPE_FILES = ["hbmenu.nro", "exosphere.ini", "BCT.ini"]

    def _get_sd_filesystem(self):
        """Return the filesystem type of the SD card (e.g. 'FAT32', 'exFAT') or None."""
        import subprocess as _sp
        try:
            r = _sp.run(
                ["powershell", "-NoProfile", "-Command",
                 f"(Get-Volume -DriveLetter '{SD_CARD[0]}').FileSystemType"],
                capture_output=True, text=True, timeout=5,
                creationflags=_sp.CREATE_NO_WINDOW)
            return r.stdout.strip() or None
        except Exception:
            return None

    def _clean_provision_confirm(self):
        """Multi-step confirmation before wiping the SD card."""
        if not os.path.exists(SD_CARD):
            messagebox.showerror("No SD Card", f"SD card not found at {SD_CARD}")
            return

        fs_type = self._get_sd_filesystem() or "unknown"

        # First warning
        ok1 = messagebox.askyesno(
            "⚠ Clean Provision — Step 1/2",
            f"This will:\n\n"
            f"  1. FORMAT {SD_CARD} as FAT32  (currently {fs_type})\n"
            f"  2. Install fresh {self._active_profile} provision\n\n"
            f"EVERYTHING on the SD card will be erased.\n\n"
            f"Continue?",
            icon="warning")
        if not ok1:
            return

        # Second warning
        ok2 = messagebox.askyesno(
            "⚠ Clean Provision — Step 2/2 — ARE YOU SURE?",
            f"LAST CHANCE!\n\n"
            f"The SD card ({SD_CARD}) will be FORMATTED as FAT32.\n"
            f"ALL data on the card will be permanently erased.\n\n"
            f"This cannot be undone.\n\n"
            f"Proceed?",
            icon="warning")
        if not ok2:
            return

        self._run_async(self._clean_provision)

    def _find_fat32format(self):
        """Locate fat32format.exe. Returns path or None."""
        search = [
            os.path.join(SCRIPT_DIR, "rcm_tools", "fat32format.exe"),
            os.path.join(SCRIPT_DIR, "fat32format.exe"),
        ]
        for p in search:
            if os.path.isfile(p):
                return p
        return None

    def _format_sd_fat32(self):
        """Format SD card as FAT32, handling >32 GB cards.

        Uses our Python-native FAT32 formatter (fat32_format.py) which
        bypasses Windows' 32 GB limitation via raw disk access.

        Returns True if successful, False otherwise.
        """
        import subprocess as _sp
        drive_letter = SD_CARD[0]

        # Primary method: Python-native FAT32 formatter
        try:
            from fat32_format import format_fat32
            print(f"    Using Python FAT32 formatter…")
            format_fat32(drive_letter, cluster_size_kb=64, label="SWITCH",
                         progress_fn=lambda msg: print(f"    {msg}"))
            return True
        except Exception as e:
            print(f"    ✕ Python formatter failed: {e}")

        # Fallback: fat32format.exe if present
        fat32fmt = self._find_fat32format()
        if fat32fmt:
            print(f"    Trying {os.path.basename(fat32fmt)}…")
            try:
                result = _sp.run(
                    [fat32fmt, "-c64", f"{drive_letter}:"],
                    capture_output=True, text=True, timeout=120,
                    input="y\n")
                if result.returncode == 0:
                    print(f"    ✓ Formatted as FAT32")
                    return True
            except Exception:
                pass

        # Last fallback: built-in format (only works ≤32 GB)
        print(f"    Trying built-in format…")
        try:
            result = _sp.run(
                ["cmd", "/c", f"format {drive_letter}: /FS:FAT32 /Q /V:SWITCH /Y"],
                capture_output=True, text=True, timeout=120)
            if result.returncode == 0:
                print(f"    ✓ Formatted as FAT32")
                return True
        except Exception:
            pass

        print(f"    ✕ All format methods failed.")
        return False

    def _clean_provision(self):
        """Background: format SD as FAT32, then provision."""
        print(f"\n{'='*50}")
        print(f"  CLEAN PROVISION — format + provision ({SD_CARD})")
        print(f"{'='*50}\n")

        print(f"  Formatting {SD_CARD} as FAT32…")
        if not self._format_sd_fat32():
            def _show_err():
                messagebox.showerror(
                    "Format Failed",
                    f"Could not format SD card as FAT32.\n\n"
                    f"For >32 GB cards, place fat32format.exe in:\n"
                    f"  rcm_tools/fat32format.exe\n\n"
                    f"Then try Clean Provision again.")
            self.root.after(0, _show_err)
            return

        # Verify
        fs = self._get_sd_filesystem()
        print(f"  Filesystem after format: {fs or 'unknown'}")

        print(f"\n  SD card formatted. Starting fresh provision…\n")

        # Run normal provision (installs everything from scratch)
        self._provision()

    def _provision(self):
        """Provision the SD card: install missing, remove extras, repair issues."""
        profile = self._active_profile
        print(f"\n=== Provisioning ({profile}) ===")
        checks = self._run_health_checks()
        installed = 0
        removed = 0
        has_conflicts = False
        for check in checks:
            key = check.get("fix_key", "")
            if not ((not check["ok"] or check.get("warn")) and check.get("fixable")):
                continue
            if key == "conflicts":
                has_conflicts = True
                continue  # handle at end via dialog
            if key.startswith("remove:"):
                nro = key[len("remove:"):]
                print(f"\n  Removing: {check['name']}…")
                self._remove_plugin(nro)
                removed += 1
                continue
            print(f"\n  Installing: {check['name']}…")
            ok = True
            if key == "skyline":
                self._install_skyline()
            elif key in LOCAL_PLUGINS:
                ok = self._install_plugin(key)
            elif key == "atmosphere":
                self._update_atmosphere()
            elif key == "hekate":
                self._update_hekate()
            elif key == "sys_patch":
                self._update_sys_patch()
            elif key == "mods_folder":
                os.makedirs(ARCROPOLIS_MODS, exist_ok=True)
                print(f"    Created mods folder")
            elif key == "cache":
                self._do_clear_cache()
            elif key == "loose_romfs":
                self._clean_loose_romfs()
            if ok:
                installed += 1

        total = installed + removed
        if total == 0 and not has_conflicts:
            print(f"\n  ✓ SD card already matches {profile} profile — nothing to do!")
        else:
            parts = []
            if installed:
                parts.append(f"{installed} installed/repaired")
            if removed:
                parts.append(f"{removed} removed")
            print(f"\n  Done — {', '.join(parts)}.")

        if has_conflicts:
            conflicts = self._find_slot_conflicts()
            if conflicts:
                print(f"\n  {len(conflicts)} slot conflict(s) remain — opening resolver…")
                self.root.after(300, lambda c=conflicts: self._show_conflict_resolver(c))
                return  # resolver handles its own refresh

        print(f"\n=== DONE — provisioning complete ===\n")
        self.root.after(500, self._show_setup)

    def _remove_plugin(self, nro_name):
        """Remove a plugin .nro from the SD card plugins folder."""
        nro_path = os.path.join(PLUGINS_DIR, nro_name)
        plugin_name = KNOWN_PLUGINS.get(nro_name, {}).get("name", nro_name)
        if os.path.exists(nro_path):
            os.remove(nro_path)
            print(f"    ✓ Removed {plugin_name} ({nro_name})")
        else:
            print(f"    {plugin_name} not found on SD — already removed")

    def _download_plugin_from_github(self, nro_name):
        """Download a plugin .nro from GitHub (always fetches latest)."""
        os.makedirs(PLUGINS_DIR, exist_ok=True)
        dest = os.path.join(PLUGINS_DIR, nro_name)

        repo_key = _NRO_TO_REPO.get(nro_name)
        if not repo_key or repo_key not in GITHUB_REPOS:
            print(f"    No GitHub source for {nro_name}")
            return False

        repo, filt = GITHUB_REPOS[repo_key]
        print(f"    Downloading latest from GitHub ({repo})…")
        info = github_latest_asset(repo, filt)
        if not info:
            print(f"    ✗ Could not find release on GitHub")
            return False

        if repo_key == "arcropolis":
            # ARCropolis comes as a zip
            tmp = os.path.join(tempfile.gettempdir(), "arc_release.zip")
            download_file_to(info["url"], tmp)
            with zipfile.ZipFile(tmp, "r") as zf:
                for f in zf.namelist():
                    if f.endswith("libarcropolis.nro"):
                        with zf.open(f) as src:
                            with open(dest, "wb") as dst:
                                dst.write(src.read())
                        break
            os.remove(tmp)
        else:
            download_file_to(info["url"], dest)

        sz = os.path.getsize(dest) if os.path.exists(dest) else 0
        name = KNOWN_PLUGINS.get(nro_name, {}).get("name", nro_name)
        print(f"    ✓ {name} {info['version']} installed ({sz / 1024:.0f} KB)")

        # Also update local cache copy so we stay current
        local = LOCAL_PLUGINS.get(nro_name)
        if local:
            os.makedirs(os.path.dirname(local), exist_ok=True)
            shutil.copy2(dest, local)
            print(f"    Updated local cache: {local}")

        return True

    def _install_plugin(self, nro_name):
        """Install a plugin .nro to SD — try GitHub first, fall back to local copy."""
        os.makedirs(PLUGINS_DIR, exist_ok=True)
        dest = os.path.join(PLUGINS_DIR, nro_name)

        # Try GitHub first (always get latest)
        if self._download_plugin_from_github(nro_name):
            return True

        # Fall back to local copy if GitHub fails
        local = LOCAL_PLUGINS.get(nro_name)
        if local and os.path.exists(local):
            shutil.copy2(local, dest)
            sz = os.path.getsize(dest)
            name = KNOWN_PLUGINS.get(nro_name, {}).get("name", nro_name)
            print(f"    ⚠ Installed {name} from LOCAL copy ({sz / 1024:.0f} KB)")
            print(f"      WARNING: This may be outdated! Check internet and try Update.")
            return True

        print(f"    ✗ No source available for {nro_name}")
        return False

    def _install_skyline(self):
        """Install Skyline exefs files from GitHub."""
        os.makedirs(EXEFS_DIR, exist_ok=True)
        repo, filt = GITHUB_REPOS["skyline"]
        print(f"    Downloading Skyline from GitHub…")
        info = github_latest_asset(repo, filt)
        if info:
            tmp = os.path.join(tempfile.gettempdir(), "skyline_tmp.zip")
            download_file_to(info["url"], tmp)
            with zipfile.ZipFile(tmp, "r") as zf:
                for f in zf.namelist():
                    fn = Path(f).name
                    if fn in ("subsdk9", "main.npdm"):
                        with zf.open(f) as src:
                            with open(os.path.join(EXEFS_DIR, fn), "wb") as dst:
                                dst.write(src.read())
                        print(f"    ✓ Installed: {fn}")
            os.remove(tmp)
            print(f"    Skyline {info['version']} installed.")
        else:
            print(f"    ✗ Could not find Skyline release on GitHub")

    def _update_atmosphere(self):
        """Download and deploy latest Atmosphere to SD card.
        When unofficial mode is enabled, prefers: local override > fork > pre-release > branch > stable."""
        use_unofficial = getattr(self, '_use_unofficial_atmo', False)
        atmo_filter = GITHUB_REPOS["atmosphere"][1]
        fusee_filter = lambda n: n == "fusee.bin"

        info = None
        from_local = False
        local_override = None
        fusee_local = None

        if use_unofficial:
            print(f"    Unofficial mode ON — checking alternative sources…")
            result, source = _resolve_unofficial_atmosphere(atmo_filter)
            if result and result.get("local"):
                local_override = result
                from_local = True
                print(f"    Found {source}: {local_override['filename']} "
                      f"({local_override['version']})")
            elif result:
                info = result
                print(f"    Found {source}: {info['version']} ({info['filename']})")
            else:
                print(f"    No unofficial build found — falling back to latest stable")

            fusee_local = find_local_fusee_override()

        if not info and not from_local:
            if not use_unofficial:
                print(f"    Downloading latest Atmosphere from GitHub…")
            repo, filt = GITHUB_REPOS["atmosphere"]
            info = github_latest_asset(repo, filt)

        if not info and not from_local:
            print(f"    ✗ Could not find any Atmosphere build")
            if use_unofficial:
                print(f"    ╭─────────────────────────────────────────────────────╮")
                print(f"    │ No unofficial build available.  To get one:         │")
                print(f"    │                                                     │")
                print(f"    │ Option A — Fork & build with GitHub Actions:        │")
                print(f"    │   1. Fork Atmosphere-NX/Atmosphere on GitHub        │")
                print(f"    │   2. Copy .github/workflows/build-atmosphere.yml    │")
                print(f"    │      from this repo into your fork                  │")
                print(f"    │   3. Run the workflow on the {ATMOSPHERE_SUPPORT_BRANCH} branch     │")
                print(f"    │   4. Set UNOFFICIAL_ATMOSPHERE_FORK in the code     │")
                print(f"    │                                                     │")
                print(f"    │ Option B — Manual override:                         │")
                print(f"    │   Place an unofficial atmosphere-*.zip into:        │")
                print(f"    │   switch_setup/downloads/                           │")
                print(f"    │   (filename must NOT contain '-master-')            │")
                print(f"    ╰─────────────────────────────────────────────────────╯")
            return False

        # Deploy atmosphere zip
        if from_local and local_override:
            print(f"    Installing from local: {local_override['filename']}")
            with zipfile.ZipFile(local_override["path"], "r") as zf:
                zf.extractall(SD_CARD)
            print(f"    ✓ Atmosphere {local_override['version']} (local) deployed to SD card.")
        else:
            tag = info['version']
            pre_tag = " ⚠ PRE-RELEASE" if info.get("prerelease") else ""
            print(f"    Downloading {tag}{pre_tag}…")
            tmp_zip = os.path.join(tempfile.gettempdir(), "atmosphere_latest.zip")
            download_file_to(info["url"], tmp_zip)
            print(f"    Extracting to {SD_CARD}…")
            with zipfile.ZipFile(tmp_zip, "r") as zf:
                zf.extractall(SD_CARD)
            os.remove(tmp_zip)
            print(f"    ✓ Atmosphere {tag}{pre_tag} deployed to SD card.")

        # Deploy fusee.bin
        if fusee_local:
            payloads_dir = os.path.join(SD_CARD, "bootloader", "payloads")
            os.makedirs(payloads_dir, exist_ok=True)
            fusee_dest = os.path.join(payloads_dir, "fusee.bin")
            shutil.copy2(fusee_local, fusee_dest)
            sz = os.path.getsize(fusee_dest)
            print(f"    ✓ fusee.bin (local override) installed ({sz / 1024:.0f} KB)")
            local_payloads = os.path.join(SCRIPT_DIR, "payloads")
            os.makedirs(local_payloads, exist_ok=True)
            try:
                shutil.copy2(fusee_local, os.path.join(local_payloads, "fusee.bin"))
            except Exception:
                pass
        else:
            fusee_info = None
            if use_unofficial:
                result, _ = _resolve_unofficial_atmosphere(fusee_filter)
                if result and not result.get("local"):
                    fusee_info = result
            if not fusee_info:
                repo_f, filt_f = GITHUB_REPOS["fusee"]
                fusee_info = github_latest_asset(repo_f, filt_f)

            if fusee_info:
                payloads_dir = os.path.join(SD_CARD, "bootloader", "payloads")
                os.makedirs(payloads_dir, exist_ok=True)
                fusee_dest = os.path.join(payloads_dir, "fusee.bin")
                download_file_to(fusee_info["url"], fusee_dest)
                sz = os.path.getsize(fusee_dest)
                pre_tag = " (pre-release)" if fusee_info.get("prerelease") else ""
                print(f"    ✓ fusee.bin {fusee_info['version']}{pre_tag} installed ({sz / 1024:.0f} KB)")

                local_payloads = os.path.join(SCRIPT_DIR, "payloads")
                os.makedirs(local_payloads, exist_ok=True)
                local_fusee = os.path.join(local_payloads, "fusee.bin")
                try:
                    shutil.copy2(fusee_dest, local_fusee)
                    print(f"    ✓ Local copy updated: payloads/fusee.bin")
                except Exception as e:
                    print(f"    ⚠ Could not update local fusee.bin: {e}")
            else:
                print(f"    ⚠ Could not download fusee.bin — you may need to get it manually")

        return True

    def _update_hekate(self):
        """Download and deploy latest Hekate to SD card."""
        print(f"    Downloading latest Hekate from GitHub…")

        repo, filt = GITHUB_REPOS["hekate"]
        info = github_latest_asset(repo, filt)
        if not info:
            print(f"    ✗ Could not find Hekate release on GitHub")
            return False
        print(f"    Found {info['version']} ({info['filename']})")

        tmp_zip = os.path.join(tempfile.gettempdir(), "hekate_latest.zip")
        download_file_to(info["url"], tmp_zip)

        # Hekate zip contains a bootloader/ folder — extract to SD root
        print(f"    Extracting to {SD_CARD}…")
        with zipfile.ZipFile(tmp_zip, "r") as zf:
            zf.extractall(SD_CARD)

            # Also update local payloads/hekate_latest.bin with the
            # hekate_ctcaer_X.X.X.bin from the zip so inject stays current.
            local_payloads = os.path.join(SCRIPT_DIR, "payloads")
            os.makedirs(local_payloads, exist_ok=True)
            hekate_bins = [n for n in zf.namelist()
                           if n.endswith(".bin") and "hekate" in n.lower()]
            if hekate_bins:
                src_name = hekate_bins[0]
                local_dest = os.path.join(local_payloads, "hekate_latest.bin")
                with zf.open(src_name) as src:
                    with open(local_dest, "wb") as dst:
                        dst.write(src.read())
                print(f"    ✓ Local copy updated: payloads/hekate_latest.bin")

        os.remove(tmp_zip)
        print(f"    ✓ Hekate {info['version']} deployed to SD card.")

        return True

    def _update_sys_patch(self):
        """Download and deploy latest sys-patch sysmodule to SD card."""
        print(f"    Downloading latest sys-patch from GitHub…")

        repo, filt = GITHUB_REPOS["sys_patch"]
        info = github_latest_asset(repo, filt)
        if not info:
            print(f"    ✗ Could not find sys-patch release on GitHub")
            return False
        print(f"    Found {info['version']} ({info['filename']})")

        tmp_zip = os.path.join(tempfile.gettempdir(), "sys_patch_latest.zip")
        download_file_to(info["url"], tmp_zip)

        # sys-patch zip contains atmosphere/ folder — extract to SD root
        print(f"    Extracting to {SD_CARD}…")
        with zipfile.ZipFile(tmp_zip, "r") as zf:
            zf.extractall(SD_CARD)
        os.remove(tmp_zip)

        # Ensure boot2.flag exists so it auto-starts on boot
        flag_dir = os.path.join(SD_CARD, "atmosphere", "contents", "420000000000000B", "flags")
        os.makedirs(flag_dir, exist_ok=True)
        flag_path = os.path.join(flag_dir, "boot2.flag")
        if not os.path.exists(flag_path):
            with open(flag_path, "w") as f:
                pass  # empty file
            print(f"    ✓ Created boot2.flag (auto-start)")

        print(f"    ✓ sys-patch {info['version']} deployed to SD card.")
        return True

    def _check_for_updates(self):
        """Compare installed component versions/sizes against latest GitHub releases."""
        profile = PROVISIONING_PROFILES.get(self._active_profile, {})
        profile_name = self._active_profile

        print(f"\n=== Checking for Updates ({profile_name}) ===\n")
        print("  Fetching latest releases from GitHub…\n")

        latest = {}
        for key, (repo, filt) in GITHUB_REPOS.items():
            latest[key] = github_latest_asset(repo, filt)

        # Check unofficial sources when enabled
        if getattr(self, '_use_unofficial_atmo', False):
            print("  [Unofficial mode] Checking alternative Atmosphere sources…\n")
            atmo_filter = GITHUB_REPOS["atmosphere"][1]
            result, source = _resolve_unofficial_atmosphere(atmo_filter)
            if result:
                if result.get("local"):
                    latest["atmosphere_unofficial"] = {
                        "version": f"{result['version']} (local)", "local": True}
                else:
                    latest["atmosphere_unofficial"] = result
                print(f"    Found via {source}: {latest['atmosphere_unofficial'].get('version', '?')}\n")
            else:
                print(f"    No unofficial build found\n")

        updates = []
        up_to_date = []
        missing = []

        # ── Core components ──

        # Atmosphere
        atmo_dir = os.path.join(SD_CARD, "atmosphere")
        atmo_info = latest.get("atmosphere")
        atmo_unofficial = latest.get("atmosphere_unofficial")
        if os.path.isdir(atmo_dir) and atmo_info:
            detail = f"installed  |  stable: {atmo_info['version']}"
            if atmo_unofficial:
                detail += f"  |  unofficial: {atmo_unofficial['version']}"
            up_to_date.append(("Atmosphere", detail))
        elif atmo_info:
            detail = f"NOT installed  |  stable: {atmo_info['version']}"
            if atmo_unofficial:
                detail += f"  |  unofficial: {atmo_unofficial['version']}"
            missing.append(("Atmosphere", detail))

        # Hekate
        bl_dir = os.path.join(SD_CARD, "bootloader")
        hekate_info = latest.get("hekate")
        if os.path.isdir(bl_dir) and hekate_info:
            up_to_date.append(("Hekate", f"installed  |  latest: {hekate_info['version']}"))
        elif hekate_info:
            missing.append(("Hekate", f"NOT installed  |  latest: {hekate_info['version']}"))

        # sys-patch
        sp_nsp = os.path.join(SD_CARD, "atmosphere", "contents",
                              "420000000000000B", "exefs.nsp")
        sp_info = latest.get("sys_patch")
        if os.path.exists(sp_nsp) and sp_info:
            up_to_date.append(("sys-patch", f"installed  |  latest: {sp_info['version']}"))
        elif sp_info:
            missing.append(("sys-patch", f"NOT installed  |  latest: {sp_info['version']}"))

        # Skyline
        subsdk = os.path.join(EXEFS_DIR, "subsdk9")
        sky_info = latest.get("skyline")
        if os.path.exists(subsdk) and sky_info:
            up_to_date.append(("Skyline", f"installed  |  latest: {sky_info['version']}"))
        elif sky_info:
            missing.append(("Skyline", f"NOT installed  |  latest: {sky_info['version']}"))

        # ── Plugins (core + profile) ──
        plugin_repo_map = {
            "libarcropolis.nro": "arcropolis",
            "liblatency_slider_de.nro": "latency_slider",
            "libless_delay.nro": "less_delay",
        }
        check_plugins = list(CORE_PLUGINS) + self._active_plugin_list()
        for nro_name in check_plugins:
            repo_key = plugin_repo_map.get(nro_name)
            gh = latest.get(repo_key) if repo_key else None
            name = KNOWN_PLUGINS.get(nro_name, {}).get("name", nro_name)
            nro_path = os.path.join(PLUGINS_DIR, nro_name)

            if not os.path.exists(nro_path):
                ver = f"  |  latest: {gh['version']}" if gh else ""
                missing.append((name, f"NOT installed{ver}"))
                continue

            sz = os.path.getsize(nro_path)
            if not gh:
                up_to_date.append((name, f"{sz / 1024:.0f} KB  |  no GitHub release to compare"))
                continue

            gh_sz = gh["size"]
            ver = gh["version"]
            # For zip-based assets (arcropolis), skip size comparison
            if repo_key == "arcropolis":
                up_to_date.append((name, f"{sz / 1024:.0f} KB  |  latest: {ver}"))
            elif gh_sz > 0 and abs(sz - gh_sz) > 512:
                updates.append((name,
                    f"{sz / 1024:.0f} KB → {gh_sz / 1024:.0f} KB  |  "
                    f"latest: {ver}  ⚠ SIZE MISMATCH"))
            else:
                up_to_date.append((name, f"{sz / 1024:.0f} KB  |  latest: {ver}  ✓ up to date"))

        # ── Print summary ──
        if updates:
            print(f"  ⬆  UPDATES AVAILABLE ({len(updates)}):")
            for name, detail in updates:
                print(f"     • {name}  —  {detail}")
            print()

        if missing:
            print(f"  ✗  MISSING ({len(missing)}):")
            for name, detail in missing:
                print(f"     • {name}  —  {detail}")
            print()

        if up_to_date:
            print(f"  ✓  UP TO DATE ({len(up_to_date)}):")
            for name, detail in up_to_date:
                print(f"     • {name}  —  {detail}")
            print()

        if updates:
            print(f"  → Use 'Update ALL from GitHub' to download {len(updates)} update(s).")
        elif missing:
            print(f"  → Use 'Provision' to install {len(missing)} missing component(s).")
        else:
            print(f"  ✓ Everything is up to date for {profile_name} profile!")

        print(f"\n=== DONE — update check complete ===\n")

    def _update_all_from_github(self):
        """Download latest versions of core + profile components from GitHub."""
        profile = PROVISIONING_PROFILES.get(self._active_profile, {})
        profile_plugins = self._active_plugin_list()
        # Map effective plugins back to their GitHub update keys via the
        # static reverse table so we only fetch what this profile uses.
        profile_update_keys = [_NRO_TO_REPO[p] for p in profile_plugins
                               if p in _NRO_TO_REPO]

        # Build ordered steps: core first, then profile plugins
        steps = [
            ("Atmosphere CFW", lambda: self._update_atmosphere()),
            ("Hekate bootloader", lambda: self._update_hekate()),
            ("sys-patch", lambda: self._update_sys_patch()),
            ("Skyline", lambda: (os.makedirs(EXEFS_DIR, exist_ok=True),
                                  self._install_skyline())),
            ("ARCropolis", lambda: (os.makedirs(PLUGINS_DIR, exist_ok=True),
                                    self._download_plugin_from_github("libarcropolis.nro"))),
        ]
        # Add profile-specific plugins
        for nro_name in profile_plugins:
            plugin_name = KNOWN_PLUGINS.get(nro_name, {}).get("name", nro_name)
            steps.append((plugin_name, lambda n=nro_name:
                          self._download_plugin_from_github(n)))

        total = len(steps) + 1  # +1 for cache clear
        print(f"\n=== Updating All ({self._active_profile} profile) — {total} steps ===")

        for i, (label, action) in enumerate(steps, 1):
            print(f"\n  [{i}/{total}] {label}…")
            action()

        print(f"\n  [{total}/{total}] Clearing cache…")
        self._do_clear_cache()

        print(f"\n  ✓ All components updated ({self._active_profile})! "
              f"Safe to eject SD and boot Switch.")
        print(f"\n=== DONE — update complete ===\n")
        self.root.after(500, self._show_setup)

    def _clear_cache(self):
        """Delete ARCropolis cache and romfs_metadata.bin, then refresh Setup."""
        self._do_clear_cache()
        self.root.after(200, self._show_setup)

    def _do_clear_cache(self):
        """Internal: delete every known ARCropolis / Atmosphere cache
        artifact that can hold a stale view of the mods folder.

        Stale caches are the #1 cause of "I deleted the mod but the
        game still freezes" — ARCropolis indexes the SD on first boot
        and reuses that index until the cache is wiped.
        """
        import shutil as _sh
        removed_files = 0
        removed_dirs = 0

        # Files to delete (any subset may exist)
        cache_files = [
            os.path.join(SD_CARD, "ultimate", "cache_filesystem.bin"),
            os.path.join(SD_CARD, "ultimate", "_filesystem.bin"),
            os.path.join(ATMOSPHERE_CONTENTS, "romfs_metadata.bin"),
        ]

        # Whole directories to delete (recursively, any subset may exist)
        cache_dirs = [
            os.path.join(SD_CARD, "ultimate", "cache"),
            os.path.join(SD_CARD, "ultimate", "arcropolis", "cache"),
            os.path.join(SD_CARD, "ultimate", "arcropolis", "logs"),
        ]

        for fp in cache_files:
            if os.path.isfile(fp):
                try:
                    os.remove(fp)
                    removed_files += 1
                    print(f"    Removed {os.path.relpath(fp, SD_CARD)}")
                except Exception as e:
                    print(f"    ! Could not remove {fp}: {e}")

        for dp in cache_dirs:
            if os.path.isdir(dp):
                # Count contents before nuking so the log is useful.
                inner = 0
                for _r, _d, fs in os.walk(dp):
                    inner += len(fs)
                try:
                    _sh.rmtree(dp, ignore_errors=True)
                    removed_dirs += 1
                    print(f"    Cleared {os.path.relpath(dp, SD_CARD)} "
                          f"({inner} file(s))")
                except Exception as e:
                    print(f"    ! Could not clear {dp}: {e}")

        if removed_files == 0 and removed_dirs == 0:
            print(f"    Cache already clean")
        print(f"\n=== DONE — cache cleared ===\n")

    def _scan_romfs_conflicts(self):
        """Scan for common romfs problems that cause ARCropolis crashes."""
        print("\n=== Scanning for romfs Conflicts ===\n")
        issues = 0

        # Check for loose romfs files
        for subdir in ("fighter", "ui", "sound", "effect"):
            path = os.path.join(ROMFS_DIR, subdir)
            if os.path.exists(path):
                print(f"  ✗ Loose romfs directory: {path}")
                print(f"    This is a legacy file-replacement approach that conflicts with ARCropolis.")
                issues += 1

        # Check for ark:/
        ark_path = os.path.join(ROMFS_DIR, "ark")
        if os.path.exists(ark_path) and any(os.scandir(ark_path)):
            print(f"  ✗ Loose ark directory: {ark_path}")
            issues += 1

        # Check romfs_metadata.bin
        meta_path = os.path.join(ATMOSPHERE_CONTENTS, "romfs_metadata.bin")
        if os.path.exists(meta_path):
            sz = os.path.getsize(meta_path)
            print(f"  ⚠ romfs_metadata.bin present ({sz / 1024:.0f} KB)")
            print(f"    Should be deleted after any mod changes to force rebuild.")
            issues += 1

        # Check for slot conflicts
        conflicts = self._find_slot_conflicts()
        for (fighter, slot), mods in conflicts.items():
            print(f"  ⚠ CONFLICT: {fighter}/{slot} claimed by: {', '.join(mods)}")
            issues += 1

        if issues == 0:
            print("  ✓ No conflicts found! Everything looks clean.")
        else:
            print(f"\n  Found {issues} issue(s).")
            if conflicts:
                print(f"  {len(conflicts)} slot conflict(s) — use the Resolve button in health checks.")
                # Open the resolver on the main thread
                self.root.after(300, lambda c=conflicts: self._show_conflict_resolver(c))
        print(f"\n=== DONE — conflict scan complete ===\n")

    def _find_slot_conflicts(self):
        """Scan mods folder for slot conflicts.
        Returns dict of (fighter, slot) -> [mod_names] where len > 1."""
        conflicts = {}
        if not os.path.exists(ARCROPOLIS_MODS):
            return conflicts
        slot_owners = {}  # (fighter, slot) -> [mod_names]
        for mod_dir in os.listdir(ARCROPOLIS_MODS):
            mod_path = os.path.join(ARCROPOLIS_MODS, mod_dir)
            if not os.path.isdir(mod_path):
                continue
            fighter_dir = os.path.join(mod_path, "fighter")
            if not os.path.exists(fighter_dir):
                continue
            for fighter in os.listdir(fighter_dir):
                body = os.path.join(fighter_dir, fighter, "model", "body")
                if os.path.exists(body):
                    for slot in os.listdir(body):
                        if os.path.isdir(os.path.join(body, slot)):
                            key = (fighter, slot)
                            slot_owners.setdefault(key, []).append(mod_dir)
        for key, mods in slot_owners.items():
            if len(mods) > 1:
                conflicts[key] = mods
        return conflicts

    def _show_conflict_resolver(self, conflicts):
        """Show a dialog where user picks which mod keeps each conflicted slot."""
        if not conflicts:
            return

        dlg = tk.Toplevel(self.root)
        dlg.title("Resolve Slot Conflicts")
        dlg.configure(bg=T.BG)
        dlg.transient(self.root)
        dlg.grab_set()
        dlg.resizable(True, True)
        dlg.minsize(520, 300)

        # Header
        tk.Label(dlg, text="Slot Conflicts Detected",
                 bg=T.BG, fg=T.RED, font=(T.FONT, T.SZ_H2, "bold")
                 ).pack(anchor="w", padx=16, pady=(12, 2))
        tk.Label(dlg,
                 text="Multiple mods claim the same fighter+slot. Pick which to KEEP for each.\n"
                      "The other mods will have that slot removed (the mod stays, just the slot goes).",
                 bg=T.BG, fg=T.OVERLAY, font=(T.FONT, T.SZ_MD), justify="left"
                 ).pack(anchor="w", padx=16, pady=(0, 10))

        # Scrollable frame
        container = tk.Frame(dlg, bg=T.BG)
        container.pack(fill="both", expand=True, padx=8, pady=4)
        canvas = tk.Canvas(container, bg=T.BG, highlightthickness=0)
        scrollbar = tk.Scrollbar(container, orient="vertical", command=canvas.yview)
        inner = tk.Frame(canvas, bg=T.BG)
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        # One row per conflict — radio buttons to pick the keeper
        selections = {}  # (fighter, slot) -> StringVar

        sorted_conflicts = sorted(conflicts.items(), key=lambda x: (x[0][0], x[0][1]))

        for (fighter, slot), mods in sorted_conflicts:
            fighter_disp = INTERNAL_TO_DISPLAY.get(fighter, fighter)
            row_frame = tk.Frame(inner, bg=T.SURFACE, padx=8, pady=6)
            row_frame.pack(fill="x", padx=6, pady=3)

            tk.Label(row_frame,
                     text=f"{fighter_disp}  /  {slot}",
                     bg=T.SURFACE, fg=T.ACCENT,
                     font=(T.FONT, T.SZ_LG, "bold")).pack(anchor="w")

            var = tk.StringVar(value=mods[0])  # default to first mod
            selections[(fighter, slot)] = var

            for mod_name in sorted(mods):
                rb = tk.Radiobutton(row_frame, text=mod_name,
                                    variable=var, value=mod_name,
                                    bg=T.SURFACE, fg=T.FG,
                                    selectcolor=T.SURFACE1,
                                    activebackground=T.SURFACE,
                                    activeforeground=T.ACCENT,
                                    font=(T.FONT, T.SZ_MD),
                                    anchor="w")
                rb.pack(anchor="w", padx=(16, 0))

        # Buttons
        btn_frame = tk.Frame(dlg, bg=T.BG)
        btn_frame.pack(fill="x", padx=16, pady=(8, 14))

        result = [False]

        def _apply():
            result[0] = True
            dlg.destroy()

        def _cancel():
            dlg.destroy()

        tk.Button(btn_frame, text="Apply — Remove Losers", width=22,
                  bg=T.GREEN, fg=T.BG, font=(T.FONT, T.SZ_LG, "bold"),
                  relief="flat", cursor="hand2",
                  command=_apply).pack(side="left", padx=(0, 8))

        tk.Button(btn_frame, text="Cancel", width=10,
                  bg=T.SURFACE1, fg=T.FG, font=(T.FONT, T.SZ_MD),
                  relief="flat", cursor="hand2",
                  command=_cancel).pack(side="left")

        # Center dialog
        dlg.update_idletasks()
        w = dlg.winfo_width()
        h = dlg.winfo_height()
        x = self.root.winfo_rootx() + (self.root.winfo_width() - w) // 2
        y = self.root.winfo_rooty() + (self.root.winfo_height() - h) // 2
        dlg.geometry(f"+{x}+{y}")

        self.root.wait_window(dlg)

        if not result[0]:
            print("  Conflict resolution cancelled.")
            return

        # Apply: for each conflict, remove the slot from non-selected mods
        print("\n=== Resolving Conflicts ===\n")
        removed_count = 0
        for (fighter, slot), var in selections.items():
            keeper = var.get()
            mods = conflicts[(fighter, slot)]
            fighter_disp = INTERNAL_TO_DISPLAY.get(fighter, fighter)
            for mod_name in mods:
                if mod_name == keeper:
                    continue
                mod_path = os.path.join(ARCROPOLIS_MODS, mod_name)
                if os.path.isdir(mod_path):
                    removed = remove_single_slot(mod_path, fighter, slot)
                    if removed:
                        print(f"  ✓ {fighter_disp}/{slot}: removed from {mod_name} (kept {keeper})")
                        removed_count += 1

        print(f"\n  Resolved {removed_count} conflict(s).")

        # Clear cache since we changed mods
        self._do_clear_cache()
        print("  Cache cleared.")

        # Refresh
        self.root.after(300, self._show_setup)

    def _clean_loose_romfs(self):
        """Remove loose romfs directories that conflict with ARCropolis."""
        import shutil as _shutil
        for subdir in ("fighter", "ui", "sound", "effect"):
            path = os.path.join(ROMFS_DIR, subdir)
            if os.path.exists(path):
                _shutil.rmtree(path)
                print(f"    Removed loose romfs: {subdir}/")
        ark_path = os.path.join(ROMFS_DIR, "ark")
        if os.path.exists(ark_path):
            _shutil.rmtree(ark_path)
            print(f"    Removed loose romfs: ark/")

    # ── Search / navigation ──────────────────────────────

    def _on_search(self):
        # If on Favorites tab, filter favorites locally instead of API search
        if self._active_view == "favorites":
            self._show_favorites()
            return
        # Stay on current browsing tab (browse skins, stages, packs, or other)
        if self._active_view not in ("browse", "stages", "other", "packs"):
            self._active_view = "browse"
            self._configure_category_dropdown("browse")
        self._highlight_active_tab()
        self._current_page = 1
        if self._active_view == "browse" and self._content_filter.get() == "Adult Only":
            self._run_async(self._do_adult_only_audit)
        else:
            self._run_async(self._do_search)

    def _next_page(self):
        max_page = max(1, (self._total_results + RESULTS_PER_PAGE - 1) // RESULTS_PER_PAGE)
        if self._current_page < max_page:
            self._current_page += 1
            self._run_async(self._do_search)

    def _prev_page(self):
        if self._current_page > 1:
            self._current_page -= 1
            self._run_async(self._do_search)

    def _is_stage_mode(self):
        """True when the active view is Browse Stages."""
        return self._active_view == "stages"

    def _do_search(self):
        # Generation counter: rapid character/category switching kicks
        # off concurrent search threads. The slowest one would otherwise
        # finish last and overwrite the user's most recent selection
        # with stale data — sometimes blanking the results pane when
        # the late-arriving response was an empty error. We bump on
        # entry, capture our own generation, and skip the UI update if
        # a newer search has started.
        if not hasattr(self, "_search_gen"):
            self._search_gen = 0
        self._search_gen += 1
        my_gen = self._search_gen

        selection = self.fighter_var.get()
        is_stages = self._is_stage_mode()
        is_other = self._active_view == "other"
        is_packs = self._active_view == "packs"
        if is_packs:
            pack_cat = PACK_CATEGORIES.get(selection, 0)
            if pack_cat:
                cat_id = pack_cat
                root_cat = pack_cat
            else:
                # "All Packs" — merged across all pack sub-categories.
                cat_id = None
                root_cat = None
        elif is_other:
            other_cat = OTHER_CATEGORIES.get(selection, 0)
            if other_cat:
                # Specific category like Effects, Gameplay, etc.
                cat_id = other_cat
                root_cat = other_cat
            else:
                # "All Other" — merge results from all Other sub-categories
                cat_id = None
                root_cat = None
        elif is_stages:
            cat_id = STAGE_CATEGORIES.get(selection, STAGES_ROOT_CAT)
            root_cat = STAGES_ROOT_CAT
        else:
            cat_id = FIGHTER_CATEGORIES.get(selection, SKINS_ROOT_CAT)
            root_cat = SKINS_ROOT_CAT
        query = self.search_var.get().strip()
        sort_key = SORT_OPTIONS.get(self.sort_var.get(), "Generic_MostLiked")

        label = selection
        if query:
            label += f' / "{query}"'
        kind = ("packs" if is_packs
                else "other" if is_other
                else "stages" if is_stages
                else "skins")
        print(f"Searching {kind}: {label} (page {self._current_page})...")

        try:
            if is_packs and not pack_cat:
                # "All Packs": query each pack sub-category, merge & sort
                all_recs = []
                all_total = 0
                for cid in _PACK_CAT_IDS:
                    t, recs = api_search_mods(
                        query=query, category_id=cid, sort=sort_key,
                        page=1, per_page=RESULTS_PER_PAGE,
                        root_cat=cid,
                    )
                    all_total += t
                    all_recs.extend(recs)
                sort_field = {
                    "Generic_MostLiked": "_nLikeCount",
                    "Generic_MostDownloaded": "_nDownloadCount",
                    "Generic_MostViewed": "_nViewCount",
                    "Generic_LatestDateModified": "_tsDateUpdated",
                }.get(sort_key, "_nLikeCount")
                all_recs.sort(key=lambda r: r.get(sort_field, 0), reverse=True)
                total = all_total
                records = all_recs[:RESULTS_PER_PAGE]
            elif is_other and not other_cat:
                # "All Other": query each sub-category, merge & sort
                all_recs = []
                all_total = 0
                for cid in _OTHER_CAT_IDS:
                    t, recs = api_search_mods(
                        query=query, category_id=cid, sort=sort_key,
                        page=1, per_page=RESULTS_PER_PAGE,
                        root_cat=cid,
                    )
                    all_total += t
                    all_recs.extend(recs)
                # Sort merged results by the chosen sort key
                sort_field = {
                    "Generic_MostLiked": "_nLikeCount",
                    "Generic_MostDownloaded": "_nDownloadCount",
                    "Generic_MostViewed": "_nViewCount",
                    "Generic_LatestDateModified": "_tsDateUpdated",
                }.get(sort_key, "_nLikeCount")
                all_recs.sort(key=lambda r: r.get(sort_field, 0), reverse=True)
                total = all_total
                records = all_recs[:RESULTS_PER_PAGE]
            else:
                total, records = api_search_mods(
                    query=query,
                    category_id=cat_id,
                    sort=sort_key,
                    page=self._current_page,
                    per_page=RESULTS_PER_PAGE,
                    root_cat=root_cat,
                )
        except Exception as e:
            print(f"API Error: {e}", file=sys.stderr)
            # Only show the error label if we're still the latest
            # search; otherwise the user has already moved on and a
            # fresher result is in flight or already shown.
            if my_gen == self._search_gen:
                self.root.after(0, lambda: self.results_label.configure(
                    text="Error fetching results"))
            return

        if my_gen != self._search_gen:
            print(f"  (stale search dropped — user switched selections)\n")
            return

        self._total_results = total
        max_page = max(1, (total + RESULTS_PER_PAGE - 1) // RESULTS_PER_PAGE)

        # Update UI on main thread
        def _update():
            # Re-check on the main thread too: another search may have
            # finished between when our worker thread queued this and
            # when Tk picked it up.
            if my_gen != self._search_gen:
                return
            self.results_label.configure(
                text=f"{total:,} {kind} for {label}")
            self.page_label.configure(text=f"Page {self._current_page}/{max_page}")
            self.prev_btn.configure(state="normal" if self._current_page > 1 else "disabled")
            self.next_btn.configure(state="normal" if self._current_page < max_page else "disabled")
            self._populate_results(records)

        self.root.after(0, _update)
        print(f"  Found {total:,} results, showing {len(records)}")
        print(f"  DONE\n")

    # ── Adult Only audit (cached, progressive) ──────────

    def _audit_cache_key(self):
        """Build a cache key from current fighter/search/sort selection."""
        fighter = self.fighter_var.get()
        query = self.search_var.get().strip()
        sort_label = self.sort_var.get()
        return f"{fighter}|{query}|{sort_label}"

    def _do_adult_only_audit(self, resume=False):
        """Scan pages to find flagged mods. Uses a persistent JSON cache so
        results survive across app restarts.

        If resume=False (default): show cached results instantly, then scan a
        batch of new pages starting where we left off.
        If resume=True: continue scanning from where cache left off.
        """
        fighter = self.fighter_var.get()
        cat_id = FIGHTER_CATEGORIES.get(fighter, SKINS_ROOT_CAT)
        query = self.search_var.get().strip()
        sort_key = SORT_OPTIONS.get(self.sort_var.get(), "Generic_MostLiked")
        cache_key = self._audit_cache_key()

        label = fighter
        if query:
            label += f' / "{query}"'

        # Load existing cache
        cache = load_audit_cache()
        entry = cache.get(cache_key, {})
        cached_flagged = entry.get("flagged", [])
        cached_pages = entry.get("pages_scanned", 0)
        cached_scanned = entry.get("total_scanned", 0)
        cached_total_api = entry.get("total_api", 0)
        cached_complete = entry.get("complete", False)

        # If we have cached data and not resuming, show it immediately
        if cached_flagged and not resume:
            self.root.after(0, lambda: self._render_audit_results(
                cached_flagged, cached_scanned, cached_total_api,
                cached_pages, label, cached_complete))
            # Also kick off a background scan for new pages if not complete
            if not cached_complete:
                print(f"  [Audit] Showing {len(cached_flagged)} cached results. "
                      f"Scanned {cached_scanned}/{cached_total_api}. "
                      f"Use 'Scan More' or 'Scan ALL' to continue.")
            return

        # Show scanning indicator
        start_page = cached_pages + 1
        def _show_scanning():
            self.results_label.configure(
                text=f"🔍 Scanning from page {start_page}...")
            self.page_label.configure(text="")
            self.prev_btn.configure(state="disabled")
            self.next_btn.configure(state="disabled")
            if not cached_flagged:
                self._slot_picker_registry.clear()
                self._slot_counter_registry.clear()
                for w in self.results_inner.winfo_children():
                    w.destroy()
                tk.Label(self.results_inner,
                         text="Scanning pages for flagged content...\n"
                              "Results will be cached for next time.",
                         bg=T.SURFACE, fg=T.OVERLAY,
                         font=(T.FONT, T.SZ_XL), justify="center").pack(pady=40)
        self.root.after(0, _show_scanning)

        # Scan a batch of pages
        BATCH_SIZE = 20
        flagged_new = []
        total_scanned = cached_scanned
        total_api = cached_total_api or 0
        pages_scanned = cached_pages
        complete = False

        end_page = start_page + BATCH_SIZE - 1
        print(f"\n=== Audit scan: pages {start_page}-{end_page} of {label} ===")

        for page in range(start_page, end_page + 1):
            try:
                total_api, records = api_search_mods(
                    query=query, category_id=cat_id,
                    sort=sort_key, page=page, per_page=RESULTS_PER_PAGE)
            except Exception as e:
                print(f"  Page {page} error: {e}", file=sys.stderr)
                break

            if not records:
                complete = True
                break

            total_scanned += len(records)
            pages_scanned = page
            page_hits = 0
            for rec in records:
                mature, reason = is_mod_mature_detailed(rec)
                if mature:
                    # Store serialisable subset for cache
                    _cid, _cname, _rid = _extract_category_info(rec)
                    flagged_new.append({
                        "mod_id": rec.get("_idRow"),
                        "name": rec.get("_sName", "?"),
                        "reason": reason,
                        "vis": rec.get("_sInitialVisibility", "?"),
                        "cr": rec.get("_bHasContentRatings", False),
                        "likes": rec.get("_nLikeCount", 0),
                        "views": rec.get("_nViewCount", 0),
                        "submitter": rec.get("_aSubmitter", {}).get("_sName", "?"),
                        "url": rec.get("_sProfileUrl", ""),
                        "tags": rec.get("_aTags", []),
                        "thumb_url": _extract_thumb_url(rec),
                        "has_files": rec.get("_bHasFiles", False),
                        "image_urls": _extract_all_image_urls(rec),
                        "category_id": _cid,
                        "category_name": _cname,
                        "root_category_id": _rid,
                        "_rec": rec,  # full record for card rendering (not saved)
                    })
                    page_hits += 1

            print(f"  Page {page}: {page_hits} flagged / {len(records)} "
                  f"(new: {len(flagged_new)}, "
                  f"total: {len(cached_flagged) + len(flagged_new)})")

            if total_scanned >= total_api:
                complete = True
                break

        # Merge with cache (deduplicate by mod_id)
        seen_ids = {f.get("mod_id") for f in cached_flagged}
        for item in flagged_new:
            if item.get("mod_id") not in seen_ids:
                # Remove non-serialisable _rec before saving
                save_item = {k: v for k, v in item.items() if k != "_rec"}
                cached_flagged.append(save_item)
                seen_ids.add(item.get("mod_id"))

        # Save to cache
        cache[cache_key] = {
            "flagged": cached_flagged,
            "pages_scanned": pages_scanned,
            "total_scanned": total_scanned,
            "total_api": total_api,
            "complete": complete,
            "sort": self.sort_var.get(),
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        save_audit_cache(cache)

        print(f"  Scan batch done: {len(cached_flagged)} total flagged, "
              f"{total_scanned}/{total_api} scanned"
              f"{' (COMPLETE)' if complete else ''}")

        # Render results
        self.root.after(0, lambda: self._render_audit_results(
            cached_flagged, total_scanned, total_api,
            pages_scanned, label, complete))

    def _do_adult_only_scan_all(self):
        """Scan ALL remaining pages until complete.  Runs in background."""
        cache_key = self._audit_cache_key()
        cache = load_audit_cache()
        entry = cache.get(cache_key, {})

        if entry.get("complete"):
            print("  [Audit] Already fully scanned.")
            self.root.after(0, lambda: self._render_audit_results(
                entry["flagged"], entry["total_scanned"], entry["total_api"],
                entry["pages_scanned"],
                self.fighter_var.get(), True))
            return

        fighter = self.fighter_var.get()
        cat_id = FIGHTER_CATEGORIES.get(fighter, SKINS_ROOT_CAT)
        query = self.search_var.get().strip()
        sort_key = SORT_OPTIONS.get(self.sort_var.get(), "Generic_MostLiked")
        label = fighter + (f' / "{query}"' if query else "")

        cached_flagged = entry.get("flagged", [])
        start_page = entry.get("pages_scanned", 0) + 1
        total_scanned = entry.get("total_scanned", 0)
        total_api = entry.get("total_api", 0)
        pages_scanned = entry.get("pages_scanned", 0)

        # Update UI to show we're scanning
        def _show():
            self.results_label.configure(
                text=f"🔍 SCAN ALL running from page {start_page}...")
        self.root.after(0, _show)

        seen_ids = {f.get("mod_id") for f in cached_flagged}
        max_page = max(1, (total_api + RESULTS_PER_PAGE - 1) // RESULTS_PER_PAGE) \
            if total_api else 9999

        print(f"\n=== SCAN ALL: pages {start_page}–{max_page} of {label} ===")

        for page in range(start_page, max_page + 1):
            try:
                total_api, records = api_search_mods(
                    query=query, category_id=cat_id,
                    sort=sort_key, page=page, per_page=RESULTS_PER_PAGE)
            except Exception as e:
                print(f"  Page {page} error: {e}", file=sys.stderr)
                break

            if not records:
                break

            total_scanned += len(records)
            pages_scanned = page
            page_hits = 0
            for rec in records:
                mature, reason = is_mod_mature_detailed(rec)
                if mature and rec.get("_idRow") not in seen_ids:
                    _cid, _cname, _rid = _extract_category_info(rec)
                    cached_flagged.append({
                        "mod_id": rec.get("_idRow"),
                        "name": rec.get("_sName", "?"),
                        "reason": reason,
                        "vis": rec.get("_sInitialVisibility", "?"),
                        "cr": rec.get("_bHasContentRatings", False),
                        "likes": rec.get("_nLikeCount", 0),
                        "views": rec.get("_nViewCount", 0),
                        "submitter": rec.get("_aSubmitter", {}).get("_sName", "?"),
                        "url": rec.get("_sProfileUrl", ""),
                        "tags": rec.get("_aTags", []),
                        "thumb_url": _extract_thumb_url(rec),
                        "has_files": rec.get("_bHasFiles", False),
                        "image_urls": _extract_all_image_urls(rec),
                        "category_id": _cid,
                        "category_name": _cname,
                        "root_category_id": _rid,
                    })
                    seen_ids.add(rec.get("_idRow"))
                    page_hits += 1

            if page % 10 == 0:
                print(f"  Page {page}: total flagged {len(cached_flagged)}, "
                      f"scanned {total_scanned}/{total_api}")
                # Update label periodically
                n = len(cached_flagged)
                self.root.after(0, lambda n=n, p=page: self.results_label.configure(
                    text=f"🔍 SCAN ALL — page {p}, {n} flagged so far..."))

            if total_scanned >= total_api:
                break

        # Save final cache
        cache = load_audit_cache()
        cache[cache_key] = {
            "flagged": cached_flagged,
            "pages_scanned": pages_scanned,
            "total_scanned": total_scanned,
            "total_api": total_api,
            "complete": True,
            "sort": self.sort_var.get(),
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        save_audit_cache(cache)

        print(f"  SCAN ALL complete: {len(cached_flagged)} flagged out of "
              f"{total_scanned} scanned ({total_api} total)")

        self.root.after(0, lambda: self._render_audit_results(
            cached_flagged, total_scanned, total_api,
            pages_scanned, label, True))

    def _render_audit_results(self, flagged, total_scanned, total_api,
                              pages_scanned, label, complete):
        """Build the Adult Only audit results UI from cached data."""
        self._slot_picker_registry.clear()
        self._slot_counter_registry.clear()
        for w in self.results_inner.winfo_children():
            w.destroy()
        self._thumb_cache.clear()

        pct = int(total_scanned * 100 / total_api) if total_api else 0
        status = "COMPLETE ✓" if complete else f"{pct}% scanned"
        self.results_label.configure(
            text=f"🔞 {len(flagged)} flagged  |  "
                 f"{total_scanned:,}/{total_api:,} scanned  |  {status}")
        self.page_label.configure(text="")
        self.prev_btn.configure(state="disabled")
        self.next_btn.configure(state="disabled")

        # ── Banner ──
        banner = tk.Frame(self.results_inner, bg=T.SURFACE1, padx=10, pady=8)
        banner.pack(fill="x", padx=4, pady=(4, 4))

        tk.Label(banner,
                 text=f"🔞  Content Audit — {len(flagged)} flagged mod(s) "
                      f"found in {total_scanned:,} scanned",
                 bg=T.SURFACE1, fg=T.YELLOW,
                 font=(T.FONT, T.SZ_MD, "bold"), anchor="w").pack(fill="x")

        tk.Label(banner,
                 text=f"These are the mods Kid Friendly hides.  "
                      f"Scanned {total_scanned:,} of {total_api:,} "
                      f"({pct}%) in '{label}'.  "
                      f"{'Scan complete.' if complete else 'Use buttons below to scan more.'}",
                 bg=T.SURFACE1, fg=T.OVERLAY,
                 font=(T.FONT, T.SZ_SM), anchor="w",
                 wraplength=700).pack(fill="x", pady=(2, 0))

        # Detection breakdown
        reason_cats = {}
        for item in flagged:
            reason = item.get("reason", "") if isinstance(item, dict) else item[1]
            cat = reason.split(":")[0].strip() if reason else "unknown"
            reason_cats[cat] = reason_cats.get(cat, 0) + 1
        if reason_cats:
            parts = [f"{cat}: {cnt}" for cat, cnt
                     in sorted(reason_cats.items(), key=lambda x: -x[1])]
            tk.Label(banner,
                     text="Detection: " + "  |  ".join(parts),
                     bg=T.SURFACE1, fg=T.OVERLAY,
                     font=(T.FONT, T.SZ_SM), anchor="w").pack(fill="x", pady=(2, 0))

        # ── Action buttons ──
        btn_row = tk.Frame(self.results_inner, bg=T.SURFACE)
        btn_row.pack(fill="x", padx=8, pady=(4, 8))

        if not complete:
            tk.Button(btn_row, text="Scan More (+20 pages)", width=22,
                      bg=T.ACCENT, fg=T.BG,
                      font=(T.FONT, T.SZ_MD, "bold"), relief="flat",
                      cursor="hand2",
                      command=lambda: self._run_async(
                          self._do_adult_only_audit, resume=True)
                      ).pack(side="left", padx=(0, 6))

            remaining = total_api - total_scanned
            tk.Button(btn_row,
                      text=f"Scan ALL ({remaining:,} remaining)", width=26,
                      bg=T.PEACH, fg=T.BG,
                      font=(T.FONT, T.SZ_MD, "bold"), relief="flat",
                      cursor="hand2",
                      command=lambda: self._run_async(
                          self._do_adult_only_scan_all)
                      ).pack(side="left", padx=(0, 6))

        tk.Button(btn_row, text="Clear Cache", width=12,
                  bg=T.SURFACE1, fg=T.FG,
                  font=(T.FONT, T.SZ_SM), relief="flat",
                  cursor="hand2",
                  command=lambda: self._clear_audit_cache()
                  ).pack(side="right")

        if not flagged:
            tk.Label(self.results_inner,
                     text="No flagged content found in scanned pages.\n"
                          "Try 'Scan More' to check additional pages.",
                     bg=T.SURFACE, fg=T.OVERLAY,
                     font=(T.FONT, T.SZ_XL), justify="center").pack(pady=30)
            return

        # ── Render cards ──
        for item in flagged:
            reason = item.get("reason", "")
            # If we have a full API record (_rec), use it; otherwise build
            # a minimal record from cache data for _add_result_card
            rec = item.get("_rec")
            if not rec:
                rec = {
                    "_idRow": item.get("mod_id"),
                    "_sName": item.get("name", "?"),
                    "_nLikeCount": item.get("likes", 0),
                    "_nViewCount": item.get("views", 0),
                    "_aSubmitter": {"_sName": item.get("submitter", "?")},
                    "_bHasFiles": item.get("has_files", False),
                    "_aTags": item.get("tags", []),
                    "_sProfileUrl": item.get("url", ""),
                    "_aPreviewMedia": {},
                }
                # Inject pre-resolved thumb URL so _extract_thumb_url works
                thumb = item.get("thumb_url")
                if thumb:
                    rec["_cached_thumb_url"] = thumb
                img_urls = item.get("image_urls", [])
                if img_urls:
                    rec["_cached_image_urls"] = img_urls
                # Re-inflate the category info we cached. Without this,
                # _extract_category_info(rec) returns (None, None, None)
                # and _guess_character_from_meta falls through to fragile
                # name-matching — adult mods like "Bunny Suit Pyra" then
                # end up tagged character="Other" and disappear from the
                # install dialog's destination strip.
                cid = item.get("category_id")
                cname = item.get("category_name")
                if cid is not None:
                    rec["_aCategory"] = {
                        "_idRow": cid,
                        "_sName": cname or "",
                    }
                rid = item.get("root_category_id")
                if rid is not None:
                    rec["_aRootCategory"] = {"_idRow": rid}
            self._add_result_card(rec, flag_reason=reason)

    def _clear_audit_cache(self):
        """Clear the audit cache for the current selection and re-scan."""
        cache_key = self._audit_cache_key()
        cache = load_audit_cache()
        cache.pop(cache_key, None)
        save_audit_cache(cache)
        print(f"  [Audit] Cache cleared for '{cache_key}'")
        self._run_async(self._do_adult_only_audit)

    def _populate_results(self, records):
        """Build result cards in the results pane."""
        self._slot_picker_registry.clear()
        self._slot_counter_registry.clear()
        for w in self.results_inner.winfo_children():
            w.destroy()
        self._thumb_cache.clear()

        if not records:
            tk.Label(self.results_inner, text="No results found",
                     bg=T.SURFACE, fg=T.OVERLAY,
                     font=(T.FONT, T.SZ_XL)).pack(pady=30)
            return

        filt = self._content_filter.get()
        show_recs = []
        hidden_names = []  # for audit log
        for rec in records:
            mature, reason = is_mod_mature_detailed(rec)
            passes = (filt == "All Content"
                      or (filt == "Kid Friendly" and not mature)
                      or (filt == "Adult Only" and mature))
            if not passes:
                name = rec.get("_sName", "?")
                hidden_names.append((name, reason))
            else:
                show_recs.append(rec)

        shown = len(show_recs)
        hidden = len(hidden_names)

        # ── Filter banner at TOP of results — visible in the UI ──
        if filt != "All Content" and hidden > 0:
            self._add_filter_banner(filt, shown, hidden, hidden_names)

        # ── Render cards ──
        for rec in show_recs:
            self._add_result_card(rec)

        # Audit log — always print filter activity so user can verify
        if filt != "All Content":
            print(f"  [Filter: {filt}] Showing {shown}, "
                  f"hidden {hidden} of {len(records)} on this page")
            if hidden_names and filt == "Kid Friendly":
                for hname, hreason in hidden_names:
                    print(f"    ✕ {hname}  ({hreason})")

        # Update results label with filter info
        if hidden > 0:
            cur = self.results_label.cget("text")
            self.results_label.configure(
                text=f"{cur}  ({hidden} hidden by {filt} filter)")

        if shown == 0:
            tk.Label(self.results_inner,
                     text=f"No results match the '{filt}' filter on this page.\n"
                          f"({hidden} mod(s) filtered out)",
                     bg=T.SURFACE, fg=T.OVERLAY,
                     font=(T.FONT, T.SZ_XL), justify="center").pack(pady=30)

    def _add_filter_banner(self, filt, shown, hidden, hidden_names):
        """Add a visible banner at the TOP of results showing filter activity.
        This builds trust by making filtering transparent."""
        # Collect reason categories
        reason_cats = {}
        for _name, reason in hidden_names:
            cat = reason.split(":")[0].strip()
            reason_cats[cat] = reason_cats.get(cat, 0) + 1

        if filt == "Kid Friendly":
            icon = "🛡️"
            summary = (f"{icon}  Kid Friendly active — "
                       f"{hidden} flagged mod(s) hidden on this page")
        else:  # Adult Only
            icon = "🔞"
            summary = (f"{icon}  Adult Only — showing {shown} flagged, "
                       f"{hidden} clean mod(s) hidden")

        banner = tk.Frame(self.results_inner, bg=T.SURFACE1, padx=10, pady=6)
        banner.pack(fill="x", padx=4, pady=(4, 8))

        tk.Label(banner, text=summary, bg=T.SURFACE1, fg=T.YELLOW,
                 font=(T.FONT, T.SZ_MD, "bold"), anchor="w",
                 wraplength=700).pack(fill="x")

        # Show detection method breakdown
        if reason_cats and filt == "Kid Friendly":
            parts = []
            for cat, count in sorted(reason_cats.items(), key=lambda x: -x[1]):
                parts.append(f"{cat}: {count}")
            detail = "Detection: " + "  |  ".join(parts)
            tk.Label(banner, text=detail, bg=T.SURFACE1, fg=T.OVERLAY,
                     font=(T.FONT, T.SZ_SM), anchor="w").pack(fill="x", pady=(2, 0))

        # List hidden mod names
        if hidden_names:
            names_text = ", ".join(n for n, _ in hidden_names)
            if len(names_text) > 120:
                names_text = names_text[:117] + "..."
            tk.Label(banner, text=f"Hidden: {names_text}", bg=T.SURFACE1,
                     fg=T.OVERLAY, font=(T.FONT, T.SZ_XS), anchor="w",
                     wraplength=700).pack(fill="x", pady=(2, 0))

    def _add_result_card(self, rec, flag_reason=None):
        """Add a single mod result card.
        If flag_reason is provided, shows a red badge with the detection reason."""
        mod_id = rec.get("_idRow")
        name = rec.get("_sName", "Unknown")
        likes = rec.get("_nLikeCount", 0)
        views = rec.get("_nViewCount", 0)
        submitter = rec.get("_aSubmitter", {}).get("_sName", "?")
        has_files = rec.get("_bHasFiles", False)
        tags = rec.get("_aTags", [])
        url = rec.get("_sProfileUrl", "")

        # Check wifi safe tag
        is_wifi_safe = any("wifi safe" in t.lower() for t in tags)

        # Get thumbnail URL
        thumb_url = _extract_thumb_url(rec)

        # All images for gallery
        all_image_urls = rec.get("_cached_image_urls") or _extract_all_image_urls(rec)

        # Card frame
        card = tk.Frame(self.results_inner, bg=T.BG, padx=8, pady=6)
        card.pack(fill="x", padx=8, pady=4)

        # Left: thumbnail placeholder
        thumb_frame = tk.Frame(card, bg=T.SURFACE1, width=250, height=140)
        thumb_frame.pack(side="left", padx=(0, 10))
        thumb_frame.pack_propagate(False)

        thumb_label = tk.Label(thumb_frame, text="Loading...",
                               bg=T.SURFACE1, fg=T.OVERLAY,
                               font=(T.FONT, T.SZ_SM))
        thumb_label.pack(expand=True)

        # Load thumbnail async
        if thumb_url:
            threading.Thread(target=self._load_thumb,
                           args=(thumb_label, thumb_url, mod_id),
                           daemon=True).start()

        # Click thumbnail to open image gallery
        if all_image_urls:
            def _open_gallery(e=None, n=name, urls=all_image_urls):
                self._show_image_gallery(n, urls)
            thumb_label.configure(cursor="hand2")
            thumb_label.bind("<Button-1>", _open_gallery)
            # Show image count badge if multiple
            if len(all_image_urls) > 1:
                badge = tk.Label(thumb_frame, text=f"📷 {len(all_image_urls)}",
                                 bg=T.SURFACE1, fg=T.ACCENT,
                                 font=(T.FONT, T.SZ_XS))
                badge.place(relx=1.0, rely=1.0, anchor="se", x=-4, y=-2)

        # Right: info + buttons
        info = tk.Frame(card, bg=T.BG)
        info.pack(side="left", fill="both", expand=True)

        # Title row
        title_row = tk.Frame(info, bg=T.BG)
        title_row.pack(fill="x")

        tk.Label(title_row, text=name, bg=T.BG, fg=T.FG,
                 font=(T.FONT, T.SZ_XL, "bold"), anchor="w",
                 wraplength=420).pack(side="left", fill="x", expand=True)

        if is_wifi_safe:
            tk.Label(title_row, text="WIFI SAFE", bg=T.SURFACE1, fg=T.GREEN,
                     font=(T.FONT, T.SZ_XS, "bold"), padx=4).pack(side="right", padx=(4, 0))

        # Show flag reason badge in Adult Only audit mode
        if flag_reason:
            tk.Label(title_row, text=f"⚠ {flag_reason}", bg=T.RED, fg=T.BG,
                     font=(T.FONT, T.SZ_XS, "bold"), padx=4, pady=1).pack(
                         side="right", padx=(4, 0))

        # Stats row
        stats = f"by {submitter}  |  {likes} likes  |  {views:,} views"
        tk.Label(info, text=stats, bg=T.BG, fg=T.SUBTEXT,
                 font=(T.FONT, T.SZ_SM), anchor="w").pack(fill="x", pady=(2, 4))

        # Tags (just first few)
        if tags:
            tag_text = ", ".join(tags[:3])
            if len(tags) > 3:
                tag_text += f" (+{len(tags)-3})"
            tk.Label(info, text=tag_text, bg=T.BG, fg=T.SUBTEXT,
                     font=(T.FONT, T.SZ_XS), anchor="w", wraplength=420).pack(fill="x")

        # Buttons row
        btn_row = tk.Frame(info, bg=T.BG)
        btn_row.pack(fill="x", pady=(4, 0))

        # Build metadata for Installed view thumbnail mapping
        _cid, _cname, _rid = _extract_category_info(rec)
        _meta = {
            "mod_id": mod_id, "name": name, "submitter": submitter,
            "likes": likes, "views": views, "url": url, "tags": tags,
            "thumb_url": _extract_thumb_url(rec),
            "image_urls": all_image_urls,
            "initial_visibility": rec.get("_sInitialVisibility", "show"),
            "category_id": _cid,
            "category_name": _cname,
            "root_category_id": _rid,
            "has_content_ratings": rec.get("_bHasContentRatings", False),
        }

        if has_files:
            # Pick a sensible *default* for the classifier based on
            # the current view. Without this, audit-cache stubs that
            # lost their category_id (Adult Only audit, or older
            # caches built before we started saving category info)
            # would classify as "other" and route the Install button
            # through the direct-install path — bypassing the drag-
            # and-drop dialog the user expects.
            view = getattr(self, "_active_view", "browse")
            default_type = "skin"
            if view == "stages":
                default_type = "stage"
            elif view == "other":
                default_type = "other"
            elif view == "packs":
                default_type = "modpack"
            inferred_type = _classify_mod_type_from_meta(
                _meta, default=default_type)

            if self._is_stage_mode() or inferred_type == "stage":
                # ── Stage / non-skin install: simple install (no slot picker) ──
                _meta["mod_type"] = "stage" if self._is_stage_mode() else inferred_type
                _ib = tk.Button(btn_row, width=18,
                          font=(T.FONT, T.SZ_MD, "bold"),
                          relief="flat", cursor="hand2")
                _ib.pack(side="left", padx=(0, 6))
                self._register_install_button(_ib, mod_id, name, _meta)
            elif inferred_type not in SLOT_AWARE_MOD_TYPES:
                # Modpacks, mechanics, music, ui, effects, movesets, etc.
                # — install as-is, no slot remapping.
                _meta["mod_type"] = inferred_type
                _ib = tk.Button(btn_row, width=22,
                          font=(T.FONT, T.SZ_MD, "bold"),
                          relief="flat", cursor="hand2")
                _ib.pack(side="left", padx=(0, 6))
                self._register_install_button(_ib, mod_id, name, _meta)
            else:
                # ── Skin mode: slot picker ──
                # Determine fighter for slot scanning
                fighter_display = self.fighter_var.get()
                fighter_int = FIGHTER_INTERNAL.get(fighter_display)

                # If browsing All Skins, try to detect fighter from
                # category / tags / name (in that priority order).
                if not fighter_int:
                    guessed = _guess_character_from_meta({
                        "category_id": _meta.get("category_id"),
                        "tags": tags, "name": name})
                    fighter_int = FIGHTER_INTERNAL.get(guessed)

                # Backfill character + mod_type so profile entries match correctly
                _meta["mod_type"] = "skin"
                if fighter_int:
                    _meta["character"] = INTERNAL_TO_DISPLAY.get(
                        fighter_int, fighter_display)
                elif not _meta.get("character"):
                    _meta["character"] = _guess_character_from_meta(
                        {"category_id": _meta.get("category_id"),
                         "tags": tags, "name": name}) or "Other"
                # Old c00..c07 slot-picker row removed — installs now
                # go through the drag-and-drop Install dialog so the
                # user sees source previews + destination occupancy
                # before committing.

        # Button row: Favorite + Open Page
        btn_row2 = tk.Frame(info, bg=T.BG)
        btn_row2.pack(fill="x", pady=(2, 0))

        if mod_id:
            is_fav = is_favorite(mod_id)
            fav_text = "♥ Favorited" if is_fav else "♡ Favorite"
            fav_bg   = T.PEACH if is_fav else T.SURFACE1
            fav_fg   = T.BG    if is_fav else T.FG

            def _toggle_fav(mid=mod_id, r=rec, btn=None,
                            mtype=_meta.get("mod_type", "skin")):
                if is_favorite(mid):
                    remove_favorite(mid)
                    print(f"  Removed '{r.get('_sName', '?')}' from favorites")
                    if btn:
                        btn.configure(text="♡ Favorite", bg=T.SURFACE1, fg=T.FG)
                else:
                    add_favorite(mid, r, mod_type=mtype)
                    print(f"  Added '{r.get('_sName', '?')}' to favorites")
                    if btn:
                        btn.configure(text="♥ Favorited", bg=T.PEACH, fg=T.BG)

            fav_btn = tk.Button(btn_row2, text=fav_text, width=12,
                                bg=fav_bg, fg=fav_fg,
                                font=(T.FONT, T.SZ_SM, "bold"),
                                relief="flat", cursor="hand2")
            fav_btn.configure(command=lambda b=fav_btn: _toggle_fav(btn=b))
            fav_btn.pack(side="left", padx=(0, 6))

        if url:
            tk.Button(btn_row2, text="Open Page", width=10,
                      bg=T.SURFACE1, fg=T.FG, font=(T.FONT, T.SZ_SM),
                      relief="flat", cursor="hand2",
                      command=lambda u=url: os.startfile(u)
                      ).pack(side="left", padx=(0, 6))

        if mod_id:
            tk.Button(btn_row2, text="Install", width=11,
                      bg=T.ACCENT, fg=T.BG,
                      font=(T.FONT, T.SZ_SM, "bold"),
                      relief="flat", cursor="hand2",
                      command=lambda mid=mod_id, mn=name, m=_meta:
                          self._view_model(mod_id=mid, mod_name=mn, metadata=m)
                      ).pack(side="left", padx=(0, 6))

    def _add_slot_picker(self, parent, mod_id, mod_name, metadata, fighter_int):
        """Show 'Install to:' label + c00–c07 slot buttons.
        Filled slots show green, empty ones are greyed out.
        In Profile Mode, occupancy comes from the selected profile.
        fighter_int may be None if the fighter couldn't be determined."""
        if fighter_int and self._profile_mode and self._profile_mode_target:
            occupied = self._get_profile_occupied_slots(
                self._profile_mode_target, fighter_int)
        else:
            occupied = get_occupied_slots(fighter_int) if fighter_int else {}

        tk.Label(parent, text="Install to:", bg=T.BG, fg=T.OVERLAY,
                 font=(T.FONT, T.SZ_MD, "bold")).pack(side="left", padx=(0, 4))

        def _bind_filled(b, name, thumb, base_color=None):
            bc = base_color or T.GREEN
            b.bind("<Enter>", lambda e, b=b, t=name, tu=thumb: (
                b.configure(bg=T.YELLOW, fg=T.BG),
                self._show_tooltip(b, t, thumb_url=tu)))
            b.bind("<Leave>", lambda e, b=b, c=bc: (
                b.configure(bg=c, fg=T.BG),
                self._hide_tooltip()))

        def _bind_empty(b):
            b.bind("<Enter>", lambda e, b=b: (
                b.configure(bg=T.OVERLAY, fg=T.BG),
                self._show_tooltip(b, "Empty — click to install")))
            b.bind("<Leave>", lambda e, b=b: (
                b.configure(bg=T.SURFACE1, fg=T.OVERLAY),
                self._hide_tooltip()))

        for i in range(8):
            slot = f"c{i:02d}"
            slot_info = occupied.get(slot)
            is_filled = slot_info is not None
            # Blue when this exact mod is already in this slot
            is_self = (is_filled
                       and mod_id is not None
                       and str(slot_info.get("mod_id") or "") == str(mod_id))

            if is_self:
                bg = T.ACCENT
                fg = T.BG
            elif is_filled:
                bg = T.GREEN
                fg = T.BG
            else:
                bg = T.SURFACE1
                fg = T.OVERLAY

            btn = tk.Button(
                parent, text=slot, width=3,
                bg=bg, fg=fg, font=(T.MONO, T.SZ_XS, "bold"),
                relief="flat", cursor="hand2",
            )
            btn.pack(side="left", padx=1)

            def _on_click(mid=mod_id, mn=mod_name, m=metadata, s=slot,
                          si=slot_info, filled=is_filled, b=btn):
                if filled:
                    friendly = si["name"]
                    if not messagebox.askyesno(
                            "Slot Occupied",
                            f"Slot {s} already has:\n  {friendly}\n\n"
                            f"Replace with '{mn}'?"):
                        return
                # Optimistic in-place update when adding to profile
                if self._profile_mode and self._profile_mode_target:
                    b.configure(bg=T.ACCENT, fg=T.BG)
                    thumb = m.get("thumb_url") if m else None
                    _bind_filled(b, mn, thumb, base_color=T.ACCENT)
                    # Optimistically increment counter label for this card
                    for _lbl, _mid in self._slot_counter_registry:
                        if str(_mid) == str(mid):
                            try:
                                cur = self._count_mod_slots_in_profile(mid)
                                # +1 because the profile write happens async
                                n = cur + 1
                                _lbl.configure(text=f"+{n} slots" if n >= 2 else "")
                            except Exception:
                                pass
                self._run_async(self._install_mod, mid, mn, m, s)

            btn.configure(command=_on_click)

            # Hover effects with thumbnail tooltip
            if is_filled:
                friendly = slot_info["name"]
                thumb = slot_info.get("thumb_url")
                _bind_filled(btn, friendly, thumb, base_color=bg)
            else:
                _bind_empty(btn)

            # Register so recolor can update all pickers without full rebuild
            self._slot_picker_registry.append((btn, fighter_int, slot, mod_id))

        # Slot counter label — shows how many slots this mod occupies in the profile
        count_lbl = tk.Label(parent, text="", bg=T.BG, fg=T.ACCENT,
                             font=(T.MONO, T.SZ_XS, "bold"))
        count_lbl.pack(side="left", padx=(6, 0))
        self._slot_counter_registry.append((count_lbl, mod_id))
        # Initialise the counter right away
        self._update_slot_counter(count_lbl, mod_id)

    def _count_mod_slots_in_profile(self, mod_id):
        """Return number of slots the given mod occupies in the active profile."""
        if not self._profile_mode or not self._profile_mode_target or not mod_id:
            return 0
        profiles = load_profiles()
        profile = profiles.get(self._profile_mode_target, {})
        for m in profile.get("mods", []):
            if str(m.get("mod_id", "")) == str(mod_id):
                slots = [s.strip() for s in
                         str(m.get("slot", "")).replace(",", " ").split()
                         if re.match(r"^c\d{2}$", s.strip())]
                return len(slots)
        return 0

    def _update_slot_counter(self, lbl, mod_id):
        """Set the text of a slot-counter label for a given mod."""
        try:
            if not lbl.winfo_exists():
                return
        except Exception:
            return
        n = self._count_mod_slots_in_profile(mod_id)
        if n >= 2:
            lbl.configure(text=f"+{n} slots")
        elif n == 1:
            lbl.configure(text="")
        else:
            lbl.configure(text="")

    def _show_tooltip(self, widget, text, thumb_url=None):
        """Show a floating tooltip near a widget, optionally with a thumbnail."""
        self._hide_tooltip()
        x = widget.winfo_rootx() + widget.winfo_width() // 2
        y = widget.winfo_rooty() + widget.winfo_height() + 4
        self._tooltip = tw = tk.Toplevel(self.root)
        tw.wm_overrideredirect(True)
        tw.attributes("-topmost", True)
        tw.configure(bg=T.CRUST)

        # Thumbnail (if URL provided)
        if thumb_url:
            img_lbl = tk.Label(tw, bg=T.CRUST, text="", width=28, height=8)
            img_lbl.pack(padx=4, pady=(4, 0))

            cache_key = f"tip_{thumb_url}"
            cached = self._thumb_cache.get(cache_key)
            if cached:
                img_lbl.configure(image=cached, width=0, height=0)
            else:
                img_lbl.configure(text="Loading...", fg=T.OVERLAY,
                                  font=(T.FONT, T.SZ_XS))
                def _load(url=thumb_url, lbl=img_lbl, key=cache_key, win=tw):
                    photo = fetch_thumbnail(url)
                    if photo:
                        self._thumb_cache[key] = photo
                        def _apply():
                            try:
                                if win.winfo_exists():
                                    lbl.configure(image=photo, text="",
                                                  width=0, height=0)
                            except tk.TclError:
                                pass
                        self.root.after(0, _apply)
                threading.Thread(target=_load, daemon=True).start()

        # Text label
        lbl = tk.Label(tw, text=text, bg=T.CRUST, fg=T.FG,
                       font=(T.FONT, T.SZ_SM), padx=6, pady=2,
                       wraplength=260, justify="center")
        lbl.pack()

        # Position after packing so we know the size
        tw.update_idletasks()
        tw_w = tw.winfo_width()
        # Center horizontally on the button, position below
        tx = x - tw_w // 2
        tw.wm_geometry(f"+{tx}+{y}")

    def _hide_tooltip(self):
        tw = getattr(self, "_tooltip", None)
        if tw:
            tw.destroy()
            self._tooltip = None

    def _load_thumb(self, label, url, mod_id):
        """Load a thumbnail image in a background thread."""
        photo = fetch_thumbnail(url)
        if photo:
            self._thumb_cache[mod_id] = photo  # prevent GC
            self.root.after(0, lambda: self._set_thumb(label, photo))
        else:
            self.root.after(0, lambda: self._set_thumb_text(label, "No preview"))

    def _set_thumb(self, label, photo):
        try:
            label.configure(image=photo, text="")
            label._smash_drag_image = photo  # ghost source for drag-drop
        except tk.TclError:
            pass  # widget was destroyed

    def _set_thumb_text(self, label, text):
        try:
            label.configure(text=text)
        except tk.TclError:
            pass  # widget was destroyed

    # ── Image gallery ──────────────────────────────────

    def _open_gallery_with_fetch(self, title, image_urls, mod_id=None,
                                  meta_path=None, fav_id=None):
        """Open the image gallery, fetching images from the API if needed.
        If image_urls is empty and mod_id is available, fetches from
        GameBanana and backfills .gb_meta.json or favorites.json."""
        if image_urls:
            self._show_image_gallery(title, image_urls)
            return

        if not mod_id:
            return

        # Fetch in background, then open gallery on main thread
        def _fetch():
            urls = api_get_mod_images(mod_id)
            if not urls:
                return

            # Backfill metadata on disk
            if meta_path and os.path.exists(meta_path):
                try:
                    with open(meta_path, "r", encoding="utf-8") as f:
                        meta = json.load(f)
                    meta["image_urls"] = urls
                    with open(meta_path, "w", encoding="utf-8") as f:
                        json.dump(meta, f, indent=2, ensure_ascii=False)
                except Exception:
                    pass

            # Backfill favorites
            if fav_id:
                try:
                    favs = load_favorites()
                    key = str(fav_id)
                    if key in favs:
                        favs[key]["image_urls"] = urls
                        save_favorites(favs)
                except Exception:
                    pass

            self.root.after(0, lambda: self._show_image_gallery(title, urls))

        threading.Thread(target=_fetch, daemon=True).start()

    def _show_image_gallery(self, title, image_data):
        """Open a centered gallery with clickable thumbnail strip.
        Reuses existing gallery window if one is already open.
        image_data: list of dicts {'large': url, 'thumb': url}
                    or list of plain URL strings (legacy/fallback)."""
        if not image_data or not HAS_PIL:
            return

        # Normalise: accept both list-of-dicts and list-of-strings
        images = []
        for item in image_data:
            if isinstance(item, dict):
                images.append(item)
            else:
                images.append({"large": item, "thumb": item})

        # If a gallery window already exists and is alive, destroy & replace
        if self._gallery_win is not None:
            try:
                self._gallery_win.destroy()
            except tk.TclError:
                pass
            self._gallery_win = None

        win = tk.Toplevel(self.root)
        self._gallery_win = win
        win.title(title)
        win.configure(bg=T.BG)
        win.overrideredirect(False)

        # Size & center on screen
        gw, gh = 1200, 900
        sx = win.winfo_screenwidth()
        sy = win.winfo_screenheight()
        x = (sx - gw) // 2
        y = (sy - gh) // 2
        win.geometry(f"{gw}x{gh}+{x}+{y}")
        win.minsize(800, 600)
        win.transient(self.root)

        def _on_close():
            closed[0] = True
            self._gallery_win = None
            try:
                win.destroy()
            except tk.TclError:
                pass

        win.protocol("WM_DELETE_WINDOW", _on_close)

        # State
        closed = [False]  # flag to stop bg threads updating dead widgets
        idx = [0]
        main_cache = {}      # index -> PhotoImage (large)
        thumb_photos = {}    # index -> PhotoImage (strip)
        thumb_labels = []    # tk.Label widgets (actually frames) in the strip
        STRIP_W, STRIP_H = 250, 140  # match browser card thumbnails

        # ── Header ──
        header = tk.Frame(win, bg=T.SURFACE)
        header.pack(fill="x")
        title_lbl = tk.Label(header, text=title, bg=T.SURFACE, fg=T.FG,
                             font=(T.FONT, T.SZ_H2, "bold"))
        title_lbl.pack(side="left", padx=10, pady=6)
        counter_lbl = tk.Label(header, text="", bg=T.SURFACE, fg=T.SUBTEXT,
                               font=(T.FONT, T.SZ_MD))
        counter_lbl.pack(side="right", padx=10, pady=6)

        # ── Main image area ──
        img_frame = tk.Frame(win, bg=T.BG)
        img_frame.pack(fill="both", expand=True, padx=10, pady=(10, 4))

        img_label = tk.Label(img_frame, text="Loading…", bg=T.BG, fg=T.OVERLAY,
                             font=(T.FONT, T.SZ_LG))
        img_label.pack(expand=True)

        # ── Thumbnail strip (horizontally scrollable) ──
        strip_height = STRIP_H + 16
        strip_outer = tk.Frame(win, bg=T.SURFACE, height=strip_height)
        strip_outer.pack(fill="x", padx=10, pady=(0, 4))
        strip_outer.pack_propagate(False)

        strip_canvas = tk.Canvas(strip_outer, bg=T.SURFACE, height=strip_height,
                                 highlightthickness=0, bd=0)
        strip_canvas.pack(fill="both", expand=True)

        strip_inner = tk.Frame(strip_canvas, bg=T.SURFACE)
        strip_canvas.create_window((0, 0), window=strip_inner, anchor="nw")

        def _on_strip_configure(e=None):
            strip_canvas.configure(scrollregion=strip_canvas.bbox("all"))

        strip_inner.bind("<Configure>", _on_strip_configure)

        # Mouse-wheel horizontal scroll — ONLY on strip, consume event
        def _strip_scroll(event):
            strip_canvas.xview_scroll(-1 * (event.delta // 120), "units")
            return "break"  # prevent event from bubbling to parent

        strip_canvas.bind("<MouseWheel>", _strip_scroll)
        strip_outer.bind("<MouseWheel>", _strip_scroll)

        # ── Nav bar ──
        nav = tk.Frame(win, bg=T.BG)
        nav.pack(fill="x", padx=10, pady=(0, 8))

        prev_btn = tk.Button(nav, text="◀  Prev", width=10,
                             bg=T.SURFACE1, fg=T.FG, font=(T.FONT, T.SZ_MD, "bold"),
                             relief="flat", cursor="hand2")
        prev_btn.pack(side="left", padx=(0, 6))

        next_btn = tk.Button(nav, text="Next  ▶", width=10,
                             bg=T.SURFACE1, fg=T.FG, font=(T.FONT, T.SZ_MD, "bold"),
                             relief="flat", cursor="hand2")
        next_btn.pack(side="right", padx=(6, 0))

        close_btn = tk.Button(nav, text="Close", width=8,
                              bg=T.PEACH, fg=T.BG, font=(T.FONT, T.SZ_MD, "bold"),
                              relief="flat", cursor="hand2",
                              command=_on_close)
        close_btn.pack()

        # ── Highlight active thumbnail ──
        def _highlight(index):
            for i, tf in enumerate(thumb_labels):
                if i == index:
                    tf.configure(bg=T.ACCENT, highlightbackground=T.ACCENT,
                                 highlightthickness=2)
                else:
                    tf.configure(bg=T.SURFACE, highlightbackground=T.SURFACE,
                                 highlightthickness=0)

        # ── Update nav state ──
        def _update_nav():
            n = len(images)
            counter_lbl.configure(text=f"{idx[0] + 1} / {n}")
            prev_btn.configure(state="normal" if idx[0] > 0 else "disabled")
            next_btn.configure(state="normal" if idx[0] < n - 1 else "disabled")

        # ── Show a main image by index ──
        def _show_image(index):
            idx[0] = index
            _update_nav()
            _highlight(index)
            img_label.configure(image="", text="Loading…")

            if index in main_cache:
                _apply_main(main_cache[index])
                return

            def _fetch():
                try:
                    url = images[index]["large"]
                    resp = requests.get(url, verify=False, timeout=15)
                    resp.raise_for_status()
                    if closed[0]:
                        return
                    pil_img = Image.open(io.BytesIO(resp.content))
                    max_w = max(win.winfo_width() - 30, 600)
                    max_h = max(win.winfo_height() - strip_height - 100, 400)
                    pil_img.thumbnail((max_w, max_h), Image.LANCZOS)
                    photo = ImageTk.PhotoImage(pil_img)
                    main_cache[index] = photo
                    if not closed[0]:
                        win.after(0, lambda p=photo: _apply_main(p))
                except Exception:
                    if not closed[0]:
                        try:
                            win.after(0, lambda: img_label.configure(
                                text="Failed to load"))
                        except tk.TclError:
                            pass

            threading.Thread(target=_fetch, daemon=True).start()

        def _apply_main(photo):
            try:
                img_label.configure(image=photo, text="")
                img_label.image = photo
            except tk.TclError:
                pass

        # ── Build thumbnail strip ──
        for i, img_info in enumerate(images):
            # Fixed-size frame per thumbnail (like browser cards)
            tf = tk.Frame(strip_inner, bg=T.SURFACE, width=STRIP_W, height=STRIP_H)
            tf.pack(side="left", padx=2, pady=2)
            tf.pack_propagate(False)

            lbl = tk.Label(tf, bg=T.SURFACE, cursor="hand2")
            lbl.pack(expand=True)
            lbl.bind("<Button-1>", lambda e, ix=i: _show_image(ix))
            lbl.bind("<MouseWheel>", _strip_scroll)
            tf.bind("<MouseWheel>", _strip_scroll)
            thumb_labels.append(tf)

            # Load strip thumbnail async
            def _load_strip_thumb(lbl_ref=lbl, url=img_info["thumb"], ix=i):
                try:
                    resp = requests.get(url, verify=False, timeout=10)
                    resp.raise_for_status()
                    if closed[0]:
                        return
                    pil_img = Image.open(io.BytesIO(resp.content))
                    pil_img.thumbnail((STRIP_W, STRIP_H), Image.LANCZOS)
                    photo = ImageTk.PhotoImage(pil_img)
                    thumb_photos[ix] = photo
                    if not closed[0]:
                        win.after(0, lambda p=photo, l=lbl_ref:
                                  _apply_strip(l, p))
                except Exception:
                    pass

            threading.Thread(target=_load_strip_thumb, daemon=True).start()

        def _apply_strip(lbl, photo):
            try:
                lbl.configure(image=photo)
                lbl.image = photo
            except tk.TclError:
                pass

        # ── Prev / Next ──
        def _prev():
            if idx[0] > 0:
                _show_image(idx[0] - 1)

        def _next():
            if idx[0] < len(images) - 1:
                _show_image(idx[0] + 1)

        prev_btn.configure(command=_prev)
        next_btn.configure(command=_next)

        # Keyboard nav
        win.bind("<Left>", lambda e: _prev())
        win.bind("<Right>", lambda e: _next())
        win.bind("<Escape>", lambda e: _on_close())

        # Start
        _show_image(0)

    # ── Install actions ────────────────────────────────

    def _install_mod(self, mod_id, mod_name, metadata=None, target_slot=None):
        """Route every install through the active profile.

        Direct-to-SD installs from Browse are no longer allowed: SD
        contents are written exclusively when a profile is *loaded*. If no
        profile is selected we surface a clear error instead of silently
        modifying the SD card.
        """
        if not self._profile_mode_target:
            self.root.after(0, lambda: messagebox.showwarning(
                "Select a Profile",
                "All installs go through profiles now.\n\n"
                "Choose a profile in the bar at the top of the window "
                "(or create one from the Profiles tab), then try again."))
            return
        self._do_install_to_profile(mod_id, mod_name, metadata, target_slot,
                                    profile_name=self._profile_mode_target)

    def _do_install_to_profile(self, mod_id, mod_name, metadata=None,
                               target_slot=None, profile_name=None):
        """Add a mod to the selected profile (profile mode).
        Downloads and caches metadata, then adds entry to profile."""
        target = profile_name or self._profile_mode_target
        if not target:
            messagebox.showerror("Profile Error", "No profile selected.")
            return

        print(f"\n=== Installing to profile '{target}': {mod_name} ===\n")

        character = "Other"
        if metadata:
            character = (metadata.get("character")
                         or _guess_character_from_meta(metadata)
                         or "Other")

        thumb_url = None
        if metadata:
            thumb_url = (metadata.get("thumb_url")
                         or metadata.get("_cached_thumb_url"))

        image_urls = []
        if metadata:
            image_urls = (metadata.get("image_urls")
                          or metadata.get("_cached_image_urls")
                          or [])

        # Capture the explicit source slot the user dragged from in
        # the Install dialog so the loader can later remap that exact
        # variant to ``target_slot`` instead of falling back to "first
        # source slot" heuristics.
        source_slot = (metadata or {}).get("source_slot") or ""

        # Build mod entry from metadata
        mod_entry = {
            "mod_id": mod_id,
            "name": mod_name,
            "character": character,
            "mod_type": metadata.get("mod_type", "skin") if metadata else "skin",
            "slot": target_slot or "",
            "source_slot": source_slot,
            "thumb_url": thumb_url,
            "image_urls": image_urls,
            "url": metadata.get("url", "") if metadata else "",
            "submitter": metadata.get("submitter", "") if metadata else "",
        }

        # Add to profile. Drag-and-drop installs (which always carry a
        # ``source_slot``) MUST stay distinct — merging would collapse
        # them to one entry with one source_slot, breaking the
        # destination renderer for every additional drag of the same
        # mod into a different slot.
        count = add_mod_to_profile(target, mod_entry,
                                    merge=not bool(source_slot))
        print(f"  Added '{mod_name}' to profile '{target}' ({count} mods total)")
        # Recolor all visible slot pickers in-place (no scroll reset)
        self.root.after(0, self._recolor_all_slot_pickers)

    def _do_install_to_sd(self, mod_id, mod_name, metadata=None, target_slot=None,
                          slot_map=None):
        """Download the first file of a mod and install directly to SD card.
        Performs comprehensive file-level conflict detection BEFORE copying.
        If conflicts are found, auto-reslots to free slots or asks the user.

        ``slot_map`` (when supplied by a caller — e.g. the drag-and-drop
        Install dialog) takes precedence over ``target_slot`` and the
        auto-reslot heuristics: it explicitly maps source slots to
        targets and unmapped source slots get dropped on disk by
        ``_apply_slot_map``.

        Stage mods (metadata['mod_type'] == 'stage') skip slot logic entirely.
        """
        if not os.path.exists(SD_CARD):
            print(f"ERROR: SD card not found at {SD_CARD}")
            return

        kind = (metadata or {}).get("mod_type") or "skin"
        is_stage = (kind == "stage")

        slot_msg = f" to slot {target_slot}" if target_slot else ""
        print(f"\n--- Installing {kind} '{mod_name}'{slot_msg} to SD ---")

        try:
            # Cache-first: if MOD_CACHE_DIR has the extracted tree, we
            # skip the download AND the redundant inspect-extract.
            # Otherwise hit the network (and that download path also
            # populates the cache).
            cached_extracted = os.path.join(
                MOD_CACHE_DIR, str(mod_id), "extracted")
            have_extracted = (os.path.isdir(cached_extracted)
                              and os.listdir(cached_extracted))
            archive_path = None
            if not have_extracted:
                archive_path = self._download_mod_archive(mod_id, mod_name)
                if not archive_path:
                    return

            if is_stage:
                # ── Stage install: simple extract & copy, no slot logic ──
                os.makedirs(ARCROPOLIS_MODS, exist_ok=True)
                dest = install_to_sd(
                    archive_path, mod_name, metadata=metadata,
                    extracted_dir=(cached_extracted if have_extracted
                                    else None))
                if dest:
                    self._post_install_sanity(dest, mod_name)
                    record_touched_for_mod(mod_id, dest)
                self._cleanup_archive(archive_path)
                print(f"  DONE!\n")
                self.root.after(0, self._check_sd)
                self.root.after(100, self._refresh_current_view)
                return

            # Extract to peek at slot structure. Always extract into
            # the cache first (so the cache is fully populated for
            # future operations) and copy from cache to the working
            # tmp_dir for the in-place slot manipulation.
            tmp_dir = tempfile.mkdtemp(prefix="gb_peek_")
            try:
                if not have_extracted:
                    print(f"  Extracting archive into cache...")
                    os.makedirs(cached_extracted, exist_ok=True)
                    try:
                        extract_archive(archive_path, cached_extracted)
                        have_extracted = True
                    except Exception as e:
                        # Wipe the partial cache so a retry doesn't
                        # think we have a half-extracted archive.
                        shutil.rmtree(cached_extracted, ignore_errors=True)
                        os.makedirs(cached_extracted, exist_ok=True)
                        raise RuntimeError(
                            f"Extract failed for '{mod_name}': {e}"
                        ) from e
                print(f"  Using cached extracted tree.")
                shutil.copytree(cached_extracted, tmp_dir,
                                 dirs_exist_ok=True)
                mod_path = find_mod_content(tmp_dir)
                if not mod_path:
                    mod_path = tmp_dir

                src_slots = _get_archive_slots(mod_path)
                all_src = []
                fighter_int = None
                for fi, slots in src_slots.items():
                    fighter_int = fi
                    all_src = slots
                    break

                # Build safe_name to figure out which existing mod folder to
                # exclude from conflict checks (re-install of same mod)
                safe_name = re.sub(r'[^\w\s\-]', '', mod_name).strip().replace(" ", "_")
                if not safe_name:
                    safe_name = "gb_skin"
                exclude_mods = set()
                if os.path.exists(ARCROPOLIS_MODS):
                    for existing in os.listdir(ARCROPOLIS_MODS):
                        if existing == safe_name or \
                           existing.startswith(safe_name + "_c"):
                            exclude_mods.add(existing)

                # If caller supplied an explicit slot_map (drag-drop
                # Install dialog), don't let the heuristics below
                # overwrite it.
                caller_provided_map = slot_map is not None

                if not caller_provided_map and target_slot and len(all_src) > 1:
                    # Multi-slot mod clicked on a specific slot button —
                    # just map the first variant to the target, drop the rest.
                    # No dialog needed; the user explicitly picked a slot.
                    slot_map = {all_src[0]: target_slot}
                    # If the target slot is already one of the variants,
                    # keep it as-is and drop the others.
                    if target_slot in all_src:
                        slot_map = {target_slot: target_slot}
                    print(f"  Mod contains {len(all_src)} variants: "
                          f"{', '.join(all_src)}")
                    print(f"  Slot mapping: "
                          f"{', '.join(f'{s}->{t}' for s, t in slot_map.items())}")

                # ── Conflict detection ──────────────────────────
                # Apply the planned remapping to a working copy so we can
                # check what files the final mod would actually contain.
                check_dir = tempfile.mkdtemp(prefix="gb_check_")
                try:
                    shutil.copytree(mod_path, os.path.join(check_dir, "_mod"),
                                    dirs_exist_ok=True)
                    check_mod = os.path.join(check_dir, "_mod")

                    if slot_map:
                        _apply_slot_map(check_mod, slot_map)
                    elif target_slot and all_src:
                        _remap_slots(check_mod, target_slot)

                    conflicts = detect_file_conflicts(check_mod, exclude_mods)
                finally:
                    shutil.rmtree(check_dir, ignore_errors=True)

                slot_conflicts   = conflicts.get("slot")   or {}
                shared_conflicts = conflicts.get("shared") or {}

                # Shared overlaps are layered by ARCropolis (last-write-wins
                # for non-slot files like ui_chara_db, msg_*.xmsbt, params).
                # Log them but do NOT prompt — they aren't slot collisions.
                if shared_conflicts:
                    s_mods = ", ".join(sorted(shared_conflicts))
                    s_count = sum(len(v) for v in shared_conflicts.values())
                    print(f"  Note: {s_count} shared-resource file(s) "
                          f"overlap with: {s_mods} (ARCropolis will layer "
                          f"these; no slot collision).")

                if slot_conflicts:
                    summary = _summarise_conflicts(
                        {"slot": slot_conflicts, "shared": {}})
                    print(f"  ⚠ Detected slot collisions:\n{summary}")

                    # Auto-resolve: if there's a free slot, remap to it;
                    # otherwise just install on top (last-write-wins).
                    # Bulk profile installs should never block on a prompt.
                    # Caller-provided slot_map is respected and never
                    # overwritten by the auto-reslotter.
                    if fighter_int and not slot_map and not caller_provided_map:
                        needed = max(len(all_src), 1)
                        free = find_free_body_slots(fighter_int, needed)
                        if len(free) >= needed:
                            if len(all_src) <= 1:
                                target_slot = free[0]
                                print(f"  Auto-reslotting to {target_slot}")
                            else:
                                slot_map = {src: free[i]
                                            for i, src in enumerate(all_src)}
                                print(f"  Auto-reslotting: "
                                      f"{', '.join(f'{s}->{t}' for s, t in slot_map.items())}")
                        else:
                            print(f"  No free slot available — "
                                  f"installing on top of existing mod "
                                  f"(last-write-wins).")
                    else:
                        print(f"  Slot already chosen — "
                              f"installing on top (last-write-wins).")

                # ── Perform install ────────────────────────────
                # Pass extracted_dir when we already have a cached
                # extract — install_to_sd skips the redundant
                # extraction in that case.
                ext_dir = cached_extracted if have_extracted else None
                os.makedirs(ARCROPOLIS_MODS, exist_ok=True)
                if slot_map:
                    dest = install_to_sd(archive_path, mod_name,
                                         metadata=metadata,
                                         slot_map=slot_map,
                                         extracted_dir=ext_dir)
                else:
                    dest = install_to_sd(archive_path, mod_name,
                                         metadata=metadata,
                                         target_slot=target_slot,
                                         extracted_dir=ext_dir)
                if dest:
                    # Strip orphan UI bntx / empty body slots that
                    # would freeze SSBU on character-select.
                    self._post_install_sanity(dest, mod_name)
                    # Persist the real (fighter,slot) tuples this mod
                    # actually replaced so subsequent bulk-installs
                    # can pre-flight-check this profile for collisions.
                    record_touched_for_mod(mod_id, dest)
            finally:
                shutil.rmtree(tmp_dir, ignore_errors=True)

            # Keep the archive around if it's in MOD_CACHE_DIR (so a
            # subsequent install for the same mod runs offline);
            # delete it only if it ended up somewhere temporary.
            self._cleanup_archive(archive_path)

            print(f"  DONE!\n")
            self.root.after(0, self._check_sd)
            self.root.after(100, self._refresh_current_view)

        except DownloadCancelled:
            self._hide_progress()
            print(f"  Download cancelled.\n")

        except Exception as e:
            self._hide_progress()
            print(f"  Error: {e}", file=sys.stderr)
            traceback.print_exc()

    def _cleanup_archive(self, archive_path):
        """Conditionally delete an archive after extraction.

        Skip deletion if the archive lives inside ``MOD_CACHE_DIR``;
        those are the persistent cache copies kept for offline /
        re-extract scenarios. Old code paths that downloaded to
        ``tempfile.gettempdir()`` still get cleaned up.
        """
        if not archive_path:
            return
        try:
            ap = os.path.normcase(os.path.abspath(archive_path))
            cache = os.path.normcase(os.path.abspath(MOD_CACHE_DIR))
            if ap.startswith(cache + os.sep) or ap.startswith(cache):
                return  # cached — keep it
            if os.path.isfile(archive_path):
                os.remove(archive_path)
        except OSError:
            pass

    def _download_mod_archive(self, mod_id, mod_name):
        """Download a mod's first file and return its local path, or None.

        Cache-first: checks ``MOD_CACHE_DIR/<mod_id>/archive/`` for any
        previously-downloaded archive before hitting the network. The
        download itself goes straight into the cache directory (not
        the temp dir), so subsequent installs / re-extracts don't
        re-fetch from GameBanana. The archive is preserved indefinitely
        — installers no longer delete it after extraction.
        """
        cache_dir = os.path.join(MOD_CACHE_DIR, str(mod_id), "archive")

        # Cache hit: return any existing archive that passes the
        # magic-bytes check. Validate before reusing — if a previous
        # download saved an HTML error page or 0-byte file as
        # "<filename>.zip", we want to redownload, not feed garbage
        # to the extractor.
        if os.path.isdir(cache_dir):
            try:
                for f in sorted(os.listdir(cache_dir)):
                    cand = os.path.join(cache_dir, f)
                    if not os.path.isfile(cand):
                        continue
                    ok, msg = _validate_archive_magic(cand)
                    if ok:
                        print(f"  Using cached archive: {f}")
                        return cand
                    print(f"  ! Cached archive looks invalid "
                          f"({msg}) — wiping and redownloading.")
                    try:
                        os.remove(cand)
                    except OSError:
                        pass
            except OSError:
                pass

        print(f"  Fetching file info...")
        try:
            data = api_get_mod_files(mod_id)
        except Exception as e:
            print(f"  ! GameBanana API request failed: {e}",
                  file=sys.stderr)
            self.root.after(0, lambda err=str(e): messagebox.showerror(
                "GameBanana API Error",
                f"Couldn't fetch file info for '{mod_name}'.\n\n{err}\n\n"
                "Usually a transient rate limit or network blip — "
                "try clicking Install again in a few seconds."))
            return None
        files = data.get("_aFiles", [])

        if not files:
            print(f"  No downloadable files found!")
            return None

        chosen = files[0]
        if len(files) > 1:
            print(f"  {len(files)} files available, using first: {chosen['_sFile']}")

        dl_url = chosen.get("_sDownloadUrl")
        filename = chosen.get("_sFile", "mod.zip")
        filesize = chosen.get("_nFilesize", 0)

        if not dl_url:
            print(f"  No download URL!")
            return None

        print(f"  Downloading: {filename} ({filesize / 1024 / 1024:.1f} MB)")

        os.makedirs(cache_dir, exist_ok=True)
        # Download into a `.part` file first so a cancelled / failed
        # download doesn't leave a corrupt cache entry that subsequent
        # cache-hit checks would happily return.
        final_path = os.path.join(cache_dir, filename)
        part_path = final_path + ".part"
        self._show_progress(f"Downloading {filename}...")
        last_pct = [-1]

        def _progress(downloaded, total):
            pct = int(downloaded * 100 / total) if total else 0
            if pct != last_pct[0]:
                last_pct[0] = pct
                self._update_progress(downloaded, total)

        try:
            download_file_to(dl_url, part_path, _progress,
                              cancel_check=lambda: self._cancel_download)
        except Exception:
            # Clean up the partial on any failure so retries don't see
            # a stale half-download.
            try:
                if os.path.exists(part_path):
                    os.remove(part_path)
            except OSError:
                pass
            self._hide_progress()
            raise

        try:
            os.replace(part_path, final_path)
        except OSError:
            # Fall back to leaving the .part file if rename fails — at
            # least the bytes are on disk for debugging.
            self._hide_progress()
            print(f"  ! Failed to rename {part_path} -> {final_path}")
            return part_path

        self._hide_progress()

        # Validate magic bytes before declaring success. If the server
        # returned an HTML error page or empty body, we want to fail
        # loudly NOW rather than letting the extractor blow up later
        # with a cryptic "File is not a zip file" three layers up.
        ok, msg = _validate_archive_magic(final_path)
        if not ok:
            print(f"  ! Downloaded file is not a valid archive: {msg}",
                  file=sys.stderr)
            try:
                os.remove(final_path)
            except OSError:
                pass
            raise RuntimeError(
                f"Download for '{mod_name}' is not a valid archive: "
                f"{msg}")
        print(f"  Download complete (cached).")
        return final_path

    def _show_slot_map_dialog(self, mod_name, src_slots, default_target,
                               occupied, fighter_int):
        """Visual slot-mapping dialog.  Users click source variants on the
        left, then click a target slot on the grid to assign it.  Green =
        empty, yellow = occupied, peach = already assigned by this dialog.
        Returns dict {src: tgt} or None."""
        dlg = tk.Toplevel(self.root)
        dlg.title("Choose Slots")
        dlg.configure(bg=T.BG)
        dlg.transient(self.root)
        dlg.grab_set()
        dlg.resizable(False, False)

        fighter_disp = INTERNAL_TO_DISPLAY.get(fighter_int, fighter_int) \
            if fighter_int else "?"

        # ── State ───────────────────────────────────────
        assignments = {}          # src_slot -> tgt_slot (or absent = skip)
        active_src = [None]       # which source variant is selected for mapping
        src_labels = {}           # src_slot -> (label_widget, arrow_label)
        grid_btns = {}            # tgt_slot -> button widget

        # ── Header ──────────────────────────────────────
        tk.Label(dlg, text=f"Install: {mod_name}",
                 bg=T.BG, fg=T.FG, font=(T.FONT, T.SZ_XL, "bold"),
                 wraplength=480).pack(padx=20, pady=(14, 2))
        tk.Label(dlg, text=f"{fighter_disp}  ·  {len(src_slots)} variant(s) in this mod",
                 bg=T.BG, fg=T.OVERLAY, font=(T.FONT, T.SZ_MD)).pack(padx=20, pady=(0, 4))
        tk.Label(dlg, text="Click a variant, then click a slot to assign it.",
                 bg=T.BG, fg=T.SUBTEXT, font=(T.FONT, T.SZ_SM)).pack(padx=20, pady=(0, 10))

        # ── Source variants (left side) ─────────────────
        body = tk.Frame(dlg, bg=T.BG)
        body.pack(fill="both", padx=20, pady=(0, 6))

        src_frame = tk.Frame(body, bg=T.SURFACE, padx=8, pady=8)
        src_frame.pack(side="left", fill="y", padx=(0, 12))

        tk.Label(src_frame, text="Mod Variants", bg=T.SURFACE,
                 fg=T.ACCENT, font=(T.FONT, T.SZ_SM, "bold")).pack(pady=(0, 6))

        def _refresh_ui():
            """Update all visual states after any assignment change."""
            for s, (lbl, arrow_lbl) in src_labels.items():
                tgt = assignments.get(s)
                is_active = (active_src[0] == s)
                if tgt:
                    lbl.configure(bg=T.PEACH, fg=T.BG)
                    arrow_lbl.configure(text=f"→ {tgt}", fg=T.GREEN)
                elif is_active:
                    lbl.configure(bg=T.ACCENT, fg=T.BG)
                    arrow_lbl.configure(text="← click a slot", fg=T.ACCENT)
                else:
                    lbl.configure(bg=T.SURFACE1, fg=T.FG)
                    arrow_lbl.configure(text="(not assigned)", fg=T.OVERLAY)

            # Refresh grid
            used_tgts = set(assignments.values())
            for tgt_slot, btn in grid_btns.items():
                is_occ = tgt_slot in occupied
                is_assigned = tgt_slot in used_tgts
                if is_assigned:
                    # Find which src maps here
                    src_for = [s for s, t in assignments.items() if t == tgt_slot]
                    btn.configure(bg=T.PEACH, fg=T.BG,
                                  text=f"{tgt_slot}\n←{src_for[0]}")
                elif is_occ:
                    btn.configure(bg=T.SURFACE1, fg=T.OVERLAY,
                                  text=tgt_slot)
                else:
                    btn.configure(bg=T.GREEN, fg=T.BG,
                                  text=tgt_slot)

            # Update summary
            n_assigned = len(assignments)
            n_total = len(src_slots)
            summary_lbl.configure(
                text=f"{n_assigned} of {n_total} assigned"
                     + ("  ·  ready to install!" if n_assigned > 0 else ""),
                fg=T.GREEN if n_assigned > 0 else T.OVERLAY)
            install_btn.configure(
                state="normal" if n_assigned > 0 else "disabled")

        def _select_src(src):
            if active_src[0] == src:
                active_src[0] = None   # deselect
            else:
                active_src[0] = src
            _refresh_ui()

        for src in src_slots:
            row = tk.Frame(src_frame, bg=T.SURFACE)
            row.pack(fill="x", pady=2)

            lbl = tk.Label(row, text=f"  {src}  ", bg=T.SURFACE1, fg=T.FG,
                           font=(T.MONO, T.SZ_MD, "bold"),
                           cursor="hand2", padx=6, pady=4)
            lbl.pack(side="left", padx=(0, 6))
            lbl.bind("<Button-1>", lambda e, s=src: _select_src(s))

            arrow_lbl = tk.Label(row, text="(not assigned)", bg=T.SURFACE,
                                 fg=T.OVERLAY, font=(T.FONT, T.SZ_SM),
                                 anchor="w", width=14)
            arrow_lbl.pack(side="left", fill="x")

            src_labels[src] = (lbl, arrow_lbl)

        # ── Target slot grid (right side) ──────────────
        grid_outer = tk.Frame(body, bg=T.SURFACE, padx=8, pady=8)
        grid_outer.pack(side="left", fill="both", expand=True)

        tk.Label(grid_outer, text="Target Slots on SD", bg=T.SURFACE,
                 fg=T.ACCENT, font=(T.FONT, T.SZ_SM, "bold")).pack(pady=(0, 6))

        # Legend
        legend = tk.Frame(grid_outer, bg=T.SURFACE)
        legend.pack(fill="x", pady=(0, 6))
        for color, label in [(T.GREEN, "Empty"), (T.SURFACE1, "Occupied"),
                              (T.PEACH, "Assigned")]:
            tk.Label(legend, text=" ● ", bg=T.SURFACE, fg=color,
                     font=(T.FONT, T.SZ_SM)).pack(side="left")
            tk.Label(legend, text=label, bg=T.SURFACE, fg=T.FG,
                     font=(T.FONT, T.SZ_XS)).pack(side="left", padx=(0, 8))

        grid = tk.Frame(grid_outer, bg=T.SURFACE)
        grid.pack(padx=4, pady=2)

        def _click_target(tgt_slot):
            src = active_src[0]
            # If clicking an already-assigned target, un-assign it
            existing_src = [s for s, t in assignments.items() if t == tgt_slot]
            if existing_src:
                del assignments[existing_src[0]]
                _refresh_ui()
                return
            if src is None:
                return  # nothing selected
            # Remove previous assignment for this src if any
            if src in assignments:
                del assignments[src]
            assignments[src] = tgt_slot
            # Auto-advance to next unassigned src
            active_src[0] = None
            for s in src_slots:
                if s not in assignments:
                    active_src[0] = s
                    break
            _refresh_ui()

        for i in range(MAX_SLOT):
            tgt_slot = f"c{i:02d}"
            row_idx = i // 8
            col_idx = i % 8
            is_occ = tgt_slot in occupied

            bg = T.SURFACE1 if is_occ else T.GREEN
            fg = T.OVERLAY if is_occ else T.BG

            btn = tk.Label(grid, text=tgt_slot, width=5, height=2,
                           bg=bg, fg=fg,
                           font=(T.MONO, T.SZ_XS, "bold"),
                           cursor="hand2", relief="flat",
                           padx=2, pady=2)
            btn.grid(row=row_idx, column=col_idx, padx=2, pady=2)
            btn.bind("<Button-1>", lambda e, t=tgt_slot: _click_target(t))
            grid_btns[tgt_slot] = btn

            # Tooltip for occupied slots
            if is_occ:
                friendly = occupied[tgt_slot]["name"]
                thumb = occupied[tgt_slot].get("thumb_url")
                btn.bind("<Enter>", lambda e, b=btn, t=friendly, tu=thumb: (
                    self._show_tooltip(b, t, thumb_url=tu)))
                btn.bind("<Leave>", lambda e, b=btn: self._hide_tooltip())

        # ── Summary & Buttons ──────────────────────────
        summary_lbl = tk.Label(dlg, text="0 of 0 assigned", bg=T.BG,
                               fg=T.OVERLAY, font=(T.FONT, T.SZ_SM))
        summary_lbl.pack(padx=20, pady=(6, 2))

        btn_frame = tk.Frame(dlg, bg=T.BG)
        btn_frame.pack(fill="x", padx=20, pady=(4, 14))

        result = [None]

        def _auto_fill():
            """Assign all unassigned variants to the first available empty slots."""
            used = set(assignments.values())
            empties = [f"c{i:02d}" for i in range(MAX_SLOT)
                       if f"c{i:02d}" not in occupied and f"c{i:02d}" not in used]
            for src in src_slots:
                if src not in assignments and empties:
                    assignments[src] = empties.pop(0)
            active_src[0] = None
            for s in src_slots:
                if s not in assignments:
                    active_src[0] = s
                    break
            _refresh_ui()

        def _clear_all():
            assignments.clear()
            active_src[0] = src_slots[0] if src_slots else None
            _refresh_ui()

        def _ok():
            result[0] = dict(assignments)
            dlg.destroy()

        def _cancel():
            result[0] = None
            dlg.destroy()

        tk.Button(btn_frame, text="⚡ Auto-fill Empty", width=16,
                  bg=T.ACCENT, fg=T.BG, font=(T.FONT, T.SZ_MD, "bold"),
                  relief="flat", cursor="hand2",
                  command=_auto_fill).pack(side="left", padx=(0, 6))

        tk.Button(btn_frame, text="Clear All", width=10,
                  bg=T.SURFACE1, fg=T.FG, font=(T.FONT, T.SZ_SM),
                  relief="flat", cursor="hand2",
                  command=_clear_all).pack(side="left", padx=(0, 12))

        install_btn = tk.Button(
            btn_frame, text="Install Selected", width=16,
            bg=T.GREEN, fg=T.BG, font=(T.FONT, T.SZ_LG, "bold"),
            relief="flat", cursor="hand2", state="disabled",
            command=_ok)
        install_btn.pack(side="right", padx=(8, 0))

        tk.Button(btn_frame, text="Cancel", width=8,
                  bg=T.SURFACE1, fg=T.FG, font=(T.FONT, T.SZ_MD),
                  relief="flat", cursor="hand2",
                  command=_cancel).pack(side="right")

        # ── Initial state: auto-select first variant ───
        if src_slots:
            active_src[0] = src_slots[0]
        _refresh_ui()

        # Center dialog
        dlg.update_idletasks()
        w = dlg.winfo_width()
        h = dlg.winfo_height()
        x = self.root.winfo_rootx() + (self.root.winfo_width() - w) // 2
        y = self.root.winfo_rooty() + (self.root.winfo_height() - h) // 2
        dlg.geometry(f"+{x}+{y}")

        self.root.wait_window(dlg)
        return result[0]

    # ── Async runner ─────────────────────────────────────

    def _run_async(self, fn, *args, **kwargs):
        """Run a function asynchronously.  Download/install tasks honour
        the _busy flag so two heavy jobs can't overlap.  Lightweight work
        (search, tab switching) is always allowed, even during a download."""
        # Determine if this is a heavy (download/install) operation
        heavy_fns = (self._do_install_to_sd, self._do_setup_fetch_github,
                     self._provision, self._fix_component,
                     self._check_for_updates,
                     self._update_all_from_github, self._scan_romfs_conflicts)
        heavy = fn in heavy_fns
        if heavy and self._busy:
            print("Busy -- please wait.\n")
            return
        if heavy:
            self._busy = True
        def _worker():
            try:
                fn(*args, **kwargs)
            except Exception as e:
                print(f"Error: {e}", file=sys.stderr)
                traceback.print_exc()
            finally:
                if heavy:
                    self.root.after(0, self._done)
        threading.Thread(target=_worker, daemon=True).start()

    def _done(self):
        self._busy = False

# ═══════════════════════════════════════════════════════════
#  ENTRY POINT
# ═══════════════════════════════════════════════════════════

def _drive_label(drive: str) -> str:
    """Return a short description like 'D:\\ — Data (Fixed, 953GB)'."""
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        # GetVolumeInformation: drive label
        vol_buf = ctypes.create_unicode_buffer(256)
        kernel32.GetVolumeInformationW(
            ctypes.c_wchar_p(drive), vol_buf, 256,
            None, None, None, None, 0
        )
        label = vol_buf.value or "(no label)"
        # GetDriveType: 2=removable, 3=fixed, 4=network, 5=cdrom
        dtype = kernel32.GetDriveTypeW(ctypes.c_wchar_p(drive))
        kind = {2: "Removable", 3: "Fixed", 4: "Network", 5: "CD-ROM"}.get(dtype, "?")
        # Disk size
        free = ctypes.c_ulonglong(0)
        total = ctypes.c_ulonglong(0)
        kernel32.GetDiskFreeSpaceExW(
            ctypes.c_wchar_p(drive), None,
            ctypes.byref(total), ctypes.byref(free)
        )
        gb = total.value / (1024**3) if total.value else 0
        return f"{drive}  —  {label}  ({kind}, {gb:.0f} GB)"
    except Exception:
        return drive


def _ask_sd_drive(drives: list) -> str:
    """Show a small dialog letting the user pick which drive to use."""
    result = {"drive": drives[0]}

    root = tk._default_root
    dlg = tk.Toplevel(root)
    dlg.title("Select SD Card Drive")
    dlg.resizable(False, False)
    dlg.grab_set()

    tk.Label(dlg, text="Multiple drives detected.\nWhich one is your Switch SD card?",
             padx=20, pady=12, justify="left").pack()

    choice = tk.StringVar(value=drives[0])
    for d in drives:
        tk.Radiobutton(dlg, text=_drive_label(d), variable=choice,
                       value=d, padx=20, anchor="w",
                       justify="left").pack(anchor="w", fill="x")

    def _ok():
        result["drive"] = choice.get()
        dlg.destroy()

    tk.Button(dlg, text="OK", command=_ok, width=10, pady=4).pack(pady=10)
    dlg.protocol("WM_DELETE_WINDOW", _ok)  # treat close as OK
    dlg.wait_window()
    return result["drive"]


def _run_standalone_viewer(model_dir):
    """Entry point for the `--view-model <path>` subprocess invocation.

    Prefers the Rust ssbh_render binary (ssbh-editor-quality output)
    rendered to PNG and shown in a Tk window. Falls back to pyrender's
    interactive viewer if the binary isn't built.
    """
    if not os.path.isdir(model_dir):
        print(f"ERROR: model folder not found: {model_dir}", file=sys.stderr)
        sys.exit(2)

    # ── Preferred: ssbh_render (matches ssbh_editor's shader graph) ──
    if os.path.isfile(SSBH_RENDER_EXE) and HAS_PIL:
        try:
            _show_rotatable_window(model_dir)
            return
        except Exception as e:
            print(f"  rotatable viewer error: {e}", file=sys.stderr)

    # ── Fallback: pyrender interactive viewer ──
    if not HAS_3D_RENDER:
        print("ERROR: 3D dependencies missing (numpy, ssbh_data_py, "
              "trimesh, pyrender)", file=sys.stderr)
        sys.exit(2)
    scene, combined = _build_colored_scene(model_dir)
    if scene is None:
        print("ERROR: could not build 3D scene from model data",
              file=sys.stderr)
        sys.exit(2)
    _add_camera_and_lights(scene, combined)
    try:
        pyrender.Viewer(scene, use_raymond_lighting=True,
                        viewport_size=(800, 600),
                        run_in_thread=False)
    except Exception as e:
        print(f"ERROR: viewer failed: {e}", file=sys.stderr)
        sys.exit(2)


def _show_rotatable_window(model_dir):
    """Open a Tk window that re-renders via ssbh_render on mouse drag.

    Uses ssbh_render's --server mode so we keep one persistent
    subprocess across all rotations — no wgpu init / model reload
    per frame. Renders are ~10× faster than the one-shot path.
    """
    import tempfile, subprocess, threading

    title_root = os.path.basename(os.path.dirname(os.path.dirname(model_dir)))
    if not title_root:
        title_root = os.path.basename(model_dir)

    width, height = 1100, 900
    cache_dir = tempfile.mkdtemp(prefix="smash_night_view_")

    # Spawn the persistent server. Wait for "READY" handshake.
    proc = subprocess.Popen(
        [SSBH_RENDER_EXE, model_dir, "--server",
         "--width", str(width), "--height", str(height)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, bufsize=1,
        creationflags=0x08000000)
    ready_line = proc.stdout.readline().strip()
    if ready_line != "READY":
        # Server failed to start — fall back to one-shot mode by
        # raising; caller will run pyrender path.
        proc.kill()
        raise RuntimeError(
            f"ssbh_render server didn't ready (got {ready_line!r})")

    proc_lock = threading.Lock()

    def server_render(yaw, pitch, pan_x, pan_y, zoom):
        """Send one render command, return output path on success."""
        # Cache by quantized state — same view = same cached PNG.
        key = (f"y{int(round(yaw))}_p{int(round(pitch))}"
               f"_x{int(round(pan_x*10))}_y{int(round(pan_y*10))}"
               f"_z{int(round(zoom*100))}")
        out_path = os.path.join(cache_dir, f"{key}.png")
        if os.path.isfile(out_path):
            return out_path
        with proc_lock:
            if proc.poll() is not None:
                return None
            try:
                proc.stdin.write(
                    f"{yaw:.2f} {pitch:.2f} "
                    f"{pan_x:.3f} {pan_y:.3f} {zoom:.3f} "
                    f"{width} {height} {out_path}\n")
                proc.stdin.flush()
                response = proc.stdout.readline().strip()
            except (OSError, ValueError):
                return None
        if response.startswith("OK"):
            return out_path if os.path.isfile(out_path) else None
        if response:
            print(f"  ssbh_render server: {response}", file=sys.stderr)
        return None

    state = {
        "yaw": 35.0,
        "pitch": 0.0,
        "pan_x": 0.0,
        "pan_y": 0.0,
        "zoom": 1.0,
        "drag_start": None,        # rotate
        "pan_start": None,         # middle-click pan
        "rendering": False,
        "pending_render": False,
    }

    root = tk.Tk()
    root.title(f"3D View — {title_root}  (drag to rotate, Esc to close)")
    root.configure(bg="#1a1a24")

    label = tk.Label(root, bg="#1a1a24",
                     text="Loading…", fg="#888",
                     width=width // 8, height=height // 16)
    label.pack(padx=8, pady=8)
    status = tk.Label(root, text="", bg="#1a1a24", fg="#999",
                      font=("Segoe UI", 9))
    status.pack(pady=(0, 6))

    def update_image(path):
        try:
            img = Image.open(path)
            photo = ImageTk.PhotoImage(img)
            label.configure(image=photo, text="", width=width, height=height)
            label.image = photo
        except Exception:
            pass

    def render_async():
        yaw, pitch = state["yaw"], state["pitch"]
        pan_x, pan_y = state["pan_x"], state["pan_y"]
        zoom = state["zoom"]
        if state["rendering"]:
            state["pending_render"] = True
            return
        state["rendering"] = True

        def _work():
            path = server_render(yaw, pitch, pan_x, pan_y, zoom)

            def _apply():
                state["rendering"] = False
                if path:
                    update_image(path)
                    status.configure(
                        text=f"yaw={yaw:.0f}° pitch={pitch:.0f}° "
                             f"pan=({pan_x:+.1f},{pan_y:+.1f}) "
                             f"zoom={zoom:.2f}×")
                else:
                    status.configure(text="Render failed")
                if state["pending_render"]:
                    state["pending_render"] = False
                    render_async()

            root.after(0, _apply)

        threading.Thread(target=_work, daemon=True).start()

    # --- Left-click drag = rotate ---
    def on_press_l(event):
        state["drag_start"] = (event.x, event.y,
                               state["yaw"], state["pitch"])

    def on_drag_l(event):
        if state["drag_start"] is None:
            return
        sx, sy, syaw, spitch = state["drag_start"]
        state["yaw"] = (syaw + (event.x - sx) * 0.4) % 360.0
        state["pitch"] = max(-80.0, min(80.0,
                                        spitch + (event.y - sy) * 0.4))
        render_async()

    def on_release_l(event):
        if state["drag_start"] is None:
            return
        state["drag_start"] = None
        render_async()

    # --- Middle-click drag = pan ---
    def on_press_m(event):
        state["pan_start"] = (event.x, event.y,
                              state["pan_x"], state["pan_y"])

    def on_drag_m(event):
        if state["pan_start"] is None:
            return
        sx, sy, spx, spy = state["pan_start"]
        # Pan in model-space units. ~30 px = 1 unit at default zoom
        # gives intuitive feel for typical SSBU character sizes.
        scale = 0.04 * state["zoom"]
        state["pan_x"] = spx + (event.x - sx) * scale
        # Invert vertical: drag mouse down → model moves down on screen
        # (matches "drag the scene with the mouse" convention).
        state["pan_y"] = spy - (event.y - sy) * scale
        render_async()

    def on_release_m(event):
        if state["pan_start"] is None:
            return
        state["pan_start"] = None
        render_async()

    # --- Mouse wheel = zoom ---
    def on_wheel(event):
        # event.delta on Windows: ±120 per notch.
        notches = event.delta / 120.0
        # Each notch scales zoom by 1.15× (out) or 1/1.15× (in).
        factor = (1.0 / 1.15) ** notches
        state["zoom"] = max(0.15, min(8.0, state["zoom"] * factor))
        render_async()

    label.bind("<ButtonPress-1>", on_press_l)
    label.bind("<B1-Motion>", on_drag_l)
    label.bind("<ButtonRelease-1>", on_release_l)
    label.bind("<ButtonPress-2>", on_press_m)
    label.bind("<B2-Motion>", on_drag_m)
    label.bind("<ButtonRelease-2>", on_release_m)
    label.bind("<MouseWheel>", on_wheel)
    root.bind("<Escape>", lambda e: root.destroy())

    render_async()

    root.update_idletasks()
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    root.geometry(f"+{(sw - width) // 2}+{max(20, (sh - height) // 2)}")
    root.mainloop()

    # Tear down server + temp cache.
    try:
        with proc_lock:
            if proc.poll() is None:
                try:
                    proc.stdin.write("quit\n")
                    proc.stdin.flush()
                except (OSError, ValueError):
                    pass
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
    except Exception:
        pass
    try:
        for f in os.listdir(cache_dir):
            os.remove(os.path.join(cache_dir, f))
        os.rmdir(cache_dir)
    except OSError:
        pass


def main():
    if len(sys.argv) >= 3 and sys.argv[1] == "--view-model":
        _run_standalone_viewer(sys.argv[2])
        return

    if not HAS_REQUESTS:
        print("ERROR: 'requests' package required. Run: pip install requests")
        sys.exit(1)

    if not HAS_PIL:
        print("NOTE: Install Pillow for thumbnail previews: pip install Pillow")

    root = tk.Tk()

    drives = _present_sd_drives()
    if len(drives) > 1:
        picked = _ask_sd_drive(drives)
        _apply_sd_drive(picked)

    app = GameBananaBrowser(root)
    root.mainloop()


if __name__ == "__main__":
    main()


