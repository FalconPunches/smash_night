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

import io
import os
import re
import sys
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

# ─────────────────────────────────────────────────────────
#  CONSTANTS
# ─────────────────────────────────────────────────────────

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SKINS_DIR = os.path.join(SCRIPT_DIR, "skins")
SD_CARD = "D:\\"
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
# Payload .bin — SD card fusee (version-matched to Atmosphere) first,
# then local copies, then fallback reboot payload.
PAYLOAD_SEARCH_PATHS = [
    os.path.join(SD_CARD, "bootloader", "payloads", "fusee.bin"),
    os.path.join(SCRIPT_DIR, "payloads", "fusee.bin"),
    os.path.join(SCRIPT_DIR, "payloads", "hekate_latest.bin"),
    os.path.join(SD_CARD, "atmosphere", "reboot_payload.bin"),
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
            for slot in os.listdir(body_path):
                if os.path.isdir(os.path.join(body_path, slot)) and \
                   re.match(r'^c\d{2}$', slot):
                    occupied[slot] = {
                        "mod": mod_name,
                        "name": display_name,
                        "thumb_url": thumb_url,
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


def detect_file_conflicts(new_mod_path, exclude_mod_names=None):
    """Check if any game-file in *new_mod_path* would collide with files
    already present in any installed mod on the SD card.

    Parameters
    ----------
    new_mod_path : str
        Path to the extracted (or about-to-be-installed) mod content root
        that contains the ``fighter/`` / ``ui/`` tree.
    exclude_mod_names : set | None
        Mod folder names (basenames) to ignore — typically the same mod
        being re-installed.

    Returns
    -------
    dict   {relative_path: [mod_folder_name, ...]}
        Mapping of every conflicting relative file path to the list of
        *existing* mod folders that already contain it.
        Empty dict means no conflicts.
    """
    if not os.path.exists(ARCROPOLIS_MODS):
        return {}

    exclude = set(exclude_mod_names) if exclude_mod_names else set()
    new_files = _get_mod_file_set(new_mod_path)
    if not new_files:
        return {}

    conflicts = {}  # rel_path -> [mod_name, ...]
    for mod_name in os.listdir(ARCROPOLIS_MODS):
        if mod_name in exclude:
            continue
        mod_dir = os.path.join(ARCROPOLIS_MODS, mod_name)
        if not os.path.isdir(mod_dir):
            continue
        existing = _get_mod_file_set(mod_dir)
        overlap = new_files & existing
        for rel in overlap:
            conflicts.setdefault(rel, []).append(mod_name)
    return conflicts


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
    """Return a human-readable summary string from a conflicts dict."""
    # Group by existing mod
    by_mod = {}
    for rel, mods in conflicts.items():
        for m in mods:
            by_mod.setdefault(m, []).append(rel)
    lines = []
    for mod, files in sorted(by_mod.items()):
        sample = files[:3]
        extra = f" (+{len(files)-3} more)" if len(files) > 3 else ""
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
            resp = requests.get(url, params=params, timeout=30)
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

    resp = requests.get(url, params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    total = data.get("_aMetadata", {}).get("_nRecordCount", 0)
    records = data.get("_aRecords", [])
    return total, records


def api_get_mod_files(mod_id):
    """Get downloadable files for a specific mod."""
    url = f"{API_BASE}/Mod/{mod_id}"
    params = {"_csvProperties": "_aFiles,_sName"}
    resp = requests.get(url, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def api_get_mod_images(mod_id):
    """Fetch preview images for a mod from the GameBanana API.
    Returns list of dicts with 'large' and 'thumb' keys, same format
    as _extract_all_image_urls."""
    try:
        url = f"{API_BASE}/Mod/{mod_id}"
        params = {"_csvProperties": "_aPreviewMedia"}
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        return _extract_all_image_urls(data)
    except Exception:
        return []


class DownloadCancelled(Exception):
    """Raised when a download is cancelled by the user."""
    pass


def download_file_to(url, dest, progress_cb=None, cancel_check=None):
    """Download a URL to a local path with optional progress callback.
    cancel_check: callable returning True if download should be aborted."""
    resp = requests.get(url, stream=True, allow_redirects=True, timeout=120)
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
    return dest


def fetch_thumbnail(image_url):
    """Fetch a thumbnail image from URL, return PhotoImage or None."""
    if not HAS_PIL:
        return None
    try:
        resp = requests.get(image_url, timeout=10)
        resp.raise_for_status()
        img = Image.open(io.BytesIO(resp.content))
        # Resize to fit nicely
        img.thumbnail((220, 124), Image.LANCZOS)
        return ImageTk.PhotoImage(img)
    except Exception as e:
        print(f"    [thumb] Failed to load {image_url}: {e}")
        return None


def github_latest_asset(repo, asset_filter):
    """Get the latest release asset from GitHub matching asset_filter(name)->bool.
    Returns dict with version, url, filename, size, published — or None."""
    if not HAS_REQUESTS:
        return None
    try:
        url = f"https://api.github.com/repos/{repo}/releases/latest"
        resp = requests.get(url, timeout=30)
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
        resp = requests.get(url, timeout=30)
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
        resp = requests.get(url, timeout=30)
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
    """Extract an archive (.zip/.7z/.rar) to dest_dir."""
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
    """
    import subprocess
    if not os.path.isfile(smash_exe):
        return False, f"TegraRcmSmash.exe not found at:\n{smash_exe}", -100
    if not os.path.isfile(payload_path):
        return False, f"Payload file not found at:\n{payload_path}", -101

    try:
        result = subprocess.run(
            [smash_exe, payload_path],
            capture_output=True, text=True, timeout=30,
        )
        rc = result.returncode
        # On Windows, negative exit codes come back as unsigned 32-bit values.
        # e.g. -3 → 4294967293 (0xFFFFFFFD).  Convert back to signed.
        if rc > 0x7FFFFFFF:
            rc = rc - 0x100000000
        if rc >= 0:
            return True, "Payload injected successfully!", rc
        else:
            # Known TegraRcmSmash error codes
            errors = {
                -1: "Wrong USB driver version (need libusbK 3.0.7)",
                -2: "Failed to get USB driver version",
                -3: "Failed to open USB device handle",
                -4: "Wrong driver — install libusbK via Zadig or TegraRcmGUI",
                -5: "No device found in RCM mode\n\n"
                    "Make sure your Switch is:\n"
                    "  1. Powered off\n"
                    "  2. Jig inserted into right Joy-Con rail\n"
                    "  3. Hold Volume+ then press Power\n"
                    "  4. Screen stays black = RCM mode ✓",
                -6: "Win32 error listing USB devices",
                -50: "Failed to launch TegraRcmSmash.exe",
            }
            msg = errors.get(rc, f"Unknown error (RC={rc})")
            stderr_out = result.stderr.strip()
            if stderr_out:
                msg += f"\n\nDetails: {stderr_out}"
            return False, msg, rc
    except subprocess.TimeoutExpired:
        return False, "Timed out waiting for injection (30s)", -99
    except Exception as e:
        return False, f"Error running TegraRcmSmash: {e}", -98


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


def install_to_sd(archive_path, mod_name, metadata=None, target_slot=None,
                  slot_map=None):
    """Extract archive, find mod content, copy to SD card ARCropolis mods.
    If metadata dict is provided, writes .gb_meta.json for thumbnail mapping.
    If target_slot is specified (e.g. 'c03') and mod has 1 slot, remaps it.
    If slot_map is provided (dict: src_slot -> target_slot), applies that mapping.
    Returns (True, tmp_dir) to allow caller to manage tmp cleanup,
    or (True, None) after cleaning up internally."""
    print(f"  Extracting archive...")
    tmp_dir = tempfile.mkdtemp(prefix="gb_skin_")
    try:
        extract_archive(archive_path, tmp_dir)

        # Find mod content
        mod_path = find_mod_content(tmp_dir)
        if not mod_path:
            mod_path = tmp_dir
            print(f"  Note: No fighter/ui structure found, using archive root")

        # Detect source slots in archive
        src_slots = _get_archive_slots(mod_path)

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
        return True
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
    # UI chara dirs
    chara_dir = os.path.join(mod_path, "ui", "replace", "chara")
    if os.path.isdir(chara_dir):
        for d in os.listdir(chara_dir):
            if d.startswith("chara_"):
                try:
                    num = int(d.split("_", 1)[1])
                    slots.add(f"c{num:02d}")
                except (ValueError, IndexError):
                    pass
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

    Uses the same bottom-up walk approach as ``ultimate-reslotter``:
      - Directories named ``cXX`` where XX is a mapped source → rename
      - Files named with ``cXX`` → rename
      - UI bntx slot suffix → rename
      - config.json → rewrite contents
      - Entries whose slot isn't in slot_map are deleted.
    """
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


def _remap_slots(mod_path, target_slot):
    """Remap an extracted mod from its current slot(s) to *target_slot*.

    Mirrors the logic of ``ultimate-reslotter`` (Rust):
      1. Walk the tree bottom-up (deepest first) so renaming a parent dir
         doesn't invalidate child paths.
      2. **Directories** whose name matches ``c\\d\\d`` → rename to target.
      3. **Files** whose name matches ``c\\d\\d`` anywhere → rename.
      4. **UI bntx** files ``chara_<type>_<fighter>_<slot>.bntx`` → rename
         only the two-digit slot suffix (the ``chara_X`` directory is the
         portrait *type* and is NEVER renamed).
      5. **config.json** → replace all ``c\\d\\d`` and bntx slot refs inside
         the file contents.
    """
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


def add_mod_to_profile(profile_name, mod_entry):
    """Add a single mod entry to an existing profile (or create a new one).
    mod_entry is a dict with keys like mod_id, name, thumb_url, etc.
    Returns the new mod count."""
    profiles = load_profiles()
    profile = profiles.get(profile_name)
    if not profile:
        profile = {
            "created": datetime.now().isoformat(),
            "mod_count": 0,
            "mods": [],
        }
    # Avoid duplicates by mod_id
    mid = mod_entry.get("mod_id")
    if mid:
        profile["mods"] = [m for m in profile["mods"] if m.get("mod_id") != mid]
    profile["mods"].append(mod_entry)
    profile["mod_count"] = len(profile["mods"])
    profiles[profile_name] = profile
    save_profiles(profiles)
    return profile["mod_count"]


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


def _guess_character_from_meta(meta):
    """Guess character name from favorites metadata (tags or name)."""
    if not meta:
        return "Unknown"
    # Check tags first — fighter name is often a tag
    tags = meta.get("tags", [])
    # Build a lookup of known fighter names (lowercase) from our category dict
    known = {k.lower(): k for k in FIGHTER_CATEGORIES.keys()
             if k not in ("All Skins", "Assist Trophies/Pokemon", "Bosses",
                          "Items", "Other/Misc", "Packs", "Mii Hats")}
    for tag in tags:
        t = tag.lower().strip()
        if t in known:
            return known[t]
    # Check mod name for fighter references
    mod_name = meta.get("name", "").lower()
    for lk, display in known.items():
        if lk in mod_name:
            return display
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

            if is_stage:
                character = "Stages"
                mod_type = "stage"
            else:
                character = fighters[0].replace("_", " ").title() if fighters else "Other"
                mod_type = "skin"

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
        self._sd_poll_id = None        # after() id for SD card polling
        self._sd_present = os.path.exists(SD_CARD)  # current SD state
        self._rcm_poll_id = None       # after() id for RCM USB polling
        self._rcm_detected = False     # True when Switch is in RCM mode
        self._rcm_inject_btn = None    # reference to the inject button widget
        self._rcm_device_label = None  # reference to the RCM device status label
        self._active_profile = "Competitive"  # provisioning profile
        self._use_unofficial_atmo = True  # prefer unofficial/pre-release Atmosphere
        self._gallery_win = None       # reusable image gallery Toplevel
        self._fav_filter = "All"       # "All", "Skins Only", "Stages Only"

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

        # SD status
        self.sd_label = tk.Label(top, text="", font=(T.FONT, T.SZ_MD), bg="#000000")
        self.sd_label.pack(side="right")
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

        self.fighter_combo = ttk.Combobox(
            search_frame, textvariable=self.fighter_var,
            values=fighter_names, state="readonly", width=22,
            font=(T.FONT, T.SZ_MD))
        self.fighter_combo.pack(side="left", padx=(0, 12))

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

        # Sort
        tk.Label(search_frame, text="Sort:", bg=T.SURFACE, fg=T.FG,
                 font=(T.FONT, T.SZ_MD)).pack(side="left", padx=(0, 4))

        self.sort_var = tk.StringVar(value="Most Liked")
        sort_combo = ttk.Combobox(
            search_frame, textvariable=self.sort_var,
            values=list(SORT_OPTIONS.keys()), state="readonly", width=16,
            font=(T.FONT, T.SZ_MD))
        sort_combo.pack(side="left", padx=(0, 12))

        # Search button
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
        if os.path.exists(SD_CARD) and os.path.isdir(SD_CARD):
            self.sd_label.configure(text=f"SD ({SD_CARD}) Connected", fg=T.GREEN)
        else:
            self.sd_label.configure(text=f"SD ({SD_CARD}) Not found", fg=T.RED)

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
        """Attach multi-char type-ahead to a readonly ttk.Combobox.

        Inserts a custom bindtag at the front of the bind chain so our
        handler fires *before* the built-in single-char jump.
        """
        GameBananaBrowser._typeahead_counter += 1
        custom_tag = f"TypeAhead{GameBananaBrowser._typeahead_counter}"
        tags = combo.bindtags()
        combo.bindtags((custom_tag,) + tags)

        def _on_key(event):
            char = event.char
            if not char or not char.isprintable():
                return  # let non-printable keys (arrows, etc.) through
            # Reset timer
            if self._typeahead_timer is not None:
                self.root.after_cancel(self._typeahead_timer)
            self._typeahead_buf += char.lower()
            self._typeahead_timer = self.root.after(
                800, self._clear_typeahead)
            # Search current values for prefix match
            values = list(combo["values"])
            for i, v in enumerate(values):
                if v.lower().startswith(self._typeahead_buf):
                    combo.current(i)
                    break
            return "break"  # prevent built-in single-char handler

        self.root.bind_class(custom_tag, "<Key>", _on_key)

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

        # Swap category dropdown between Fighter / Stage / Other
        if view_name in ("browse", "stages", "other"):
            self._configure_category_dropdown(view_name)

        if view_name == "browse":
            self.results_label.configure(text="")
            self.page_label.configure(text="")
            self._current_page = 1
            if self._content_filter.get() == "Adult Only":
                self._run_async(self._do_adult_only_audit)
            else:
                self._run_async(self._do_search)
        elif view_name in ("stages", "other"):
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
        """Swap the category dropdown between fighter names, stage names, and other categories."""
        if mode == "other":
            self._category_label.configure(text="Category:")
            self.fighter_combo.configure(values=self._other_names)
            if self.fighter_var.get() not in OTHER_CATEGORIES:
                self.fighter_var.set("All Other")
        elif mode == "stages":
            self._category_label.configure(text="Stage:")
            self.fighter_combo.configure(values=self._stage_names)
            if self.fighter_var.get() not in STAGE_CATEGORIES:
                self.fighter_var.set("All Stages")
        else:
            self._category_label.configure(text="Fighter:")
            self.fighter_combo.configure(values=self._fighter_names)
            if self.fighter_var.get() not in FIGHTER_CATEGORIES:
                self.fighter_var.set("All Skins")

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
        elif self._active_view == "favorites":
            self._show_favorites()
        elif self._active_view == "installed":
            self._show_installed()
        elif self._active_view == "profiles":
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
        for w in self.results_inner.winfo_children():
            w.destroy()
        self._thumb_cache.clear()

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
                tk.Button(btn_row, text="Install Stage to SD", width=18,
                          bg=T.GREEN, fg=T.BG, font=(T.FONT, T.SZ_MD, "bold"),
                          relief="flat", cursor="hand2",
                          command=lambda mid=mod_id, mn=name, m=meta: self._run_async(
                              self._do_install_to_sd, mid, mn,
                              {**m, "mod_type": "stage"})
                          ).pack(side="left", padx=(0, 6))
            else:
                # Skin mod: try slot picker or plain install
                char = _guess_character_from_meta(meta)
                fighter_int = FIGHTER_INTERNAL.get(char)
                if fighter_int:
                    self._add_slot_picker(btn_row, mod_id, name, meta, fighter_int)
                else:
                    tk.Button(btn_row, text="Install to SD", width=14,
                              bg=T.GREEN, fg=T.BG, font=(T.FONT, T.SZ_MD, "bold"),
                              relief="flat", cursor="hand2",
                              command=lambda mid=mod_id, mn=name, m=meta: self._run_async(
                                  self._do_install_to_sd, mid, mn, m)
                              ).pack(side="left", padx=(0, 6))

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

        # "Add to Profile" button
        def _add_to_prof_fav(m=meta):
            entry = {
                "mod_id": m.get("mod_id"),
                "name": m.get("name", "?"),
                "character": _guess_character_from_meta(m) or "Other",
                "mod_type": m.get("mod_type", "skin"),
                "thumb_url": m.get("thumb_url"),
                "image_urls": m.get("image_urls", []),
                "url": m.get("url", ""),
                "submitter": m.get("submitter", ""),
            }
            self._pick_profile_and_add(entry)

        tk.Button(btn_row2, text="+ Profile", width=10,
                  bg=T.SURFACE1, fg=T.FG, font=(T.FONT, T.SZ_SM, "bold"),
                  relief="flat", cursor="hand2",
                  command=_add_to_prof_fav).pack(side="left", padx=(0, 6))

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
            values=fighter_choices, state="readonly", width=22,
            font=(T.FONT, T.SZ_MD))
        fighter_combo.pack(side="left", padx=(0, 6))

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
        for w in self.results_inner.winfo_children():
            w.destroy()
        self._thumb_cache.clear()

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

        # Favorite toggle (only if we have mod_id from metadata)
        mod_id = meta.get("mod_id") if meta else None
        if mod_id:
            is_fav = is_favorite(mod_id)
            fav_text = "♥ Favorited" if is_fav else "♡ Favorite"
            fav_bg = T.PEACH if is_fav else T.SURFACE1
            fav_fg = T.BG if is_fav else T.FG

            def _toggle_installed_fav(mid=mod_id, m=meta, btn=None,
                                     mtype=skin.get("mod_type", "skin")):
                if is_favorite(mid):
                    remove_favorite(mid)
                    print(f"  Removed '{m.get('name', '?')}' from favorites")
                    if btn:
                        btn.configure(text="♡ Favorite",
                                      bg=T.SURFACE1, fg=T.FG)
                else:
                    # Build a pseudo API record from .gb_meta.json
                    pseudo_rec = {
                        "_sName": m.get("name", "Unknown"),
                        "_aSubmitter": {"_sName": m.get("submitter", "?")},
                        "_nLikeCount": m.get("likes", 0),
                        "_nViewCount": m.get("views", 0),
                        "_bHasFiles": True,
                        "_sProfileUrl": m.get("url", ""),
                        "_aTags": m.get("tags", []),
                        "_sInitialVisibility": m.get(
                            "initial_visibility", "show"),
                        "_bHasContentRatings": m.get(
                            "has_content_ratings", False),
                    }
                    # Preserve thumbnail info
                    if m.get("thumb_url"):
                        pseudo_rec["_cached_thumb_url"] = m["thumb_url"]
                    if m.get("image_urls"):
                        pseudo_rec["_cached_image_urls"] = m["image_urls"]
                    add_favorite(mid, pseudo_rec, mod_type=mtype)
                    print(f"  Added '{m.get('name', '?')}' to favorites"
                          f" (type={mtype})")
                    if btn:
                        btn.configure(text="♥ Favorited",
                                      bg=T.PEACH, fg=T.BG)

            fav_btn = tk.Button(btn_row, text=fav_text, width=12,
                                bg=fav_bg, fg=fav_fg,
                                font=(T.FONT, T.SZ_SM, "bold"),
                                relief="flat", cursor="hand2")
            fav_btn.configure(
                command=lambda b=fav_btn, mid=mod_id, m=meta:
                    _toggle_installed_fav(mid, m, b))
            fav_btn.pack(side="left")

    def _create_profile_from_installed_ui(self):
        """Prompt for a name and snapshot current installed mods as a profile."""
        if not os.path.exists(ARCROPOLIS_MODS):
            messagebox.showwarning("No SD", "SD card mods folder not found.")
            return
        skins = list_installed_skins()
        if not skins:
            messagebox.showinfo("Empty", "No mods installed to save.")
            return

        name = simpledialog.askstring(
            "Save Profile", f"Name for this profile ({len(skins)} mods):",
            parent=self.root)
        if not name or not name.strip():
            return
        name = name.strip()

        # Check for overwrite
        existing = load_profiles()
        if name in existing:
            if not messagebox.askyesno(
                    "Overwrite Profile",
                    f"A profile named '{name}' already exists "
                    f"({existing[name]['mod_count']} mods).\n\n"
                    f"Overwrite it?"):
                return

        count = create_profile_from_installed(name)
        print(f"\n=== Saved profile '{name}' with {count} mod(s) ===\n")
        messagebox.showinfo("Profile Saved",
                            f"Saved '{name}' with {count} mod(s).\n\n"
                            "Go to the Profiles tab to manage or load it.")

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
        self.prev_btn.configure(state="disabled")
        self.next_btn.configure(state="disabled")
        self.page_label.configure(text="")

        for w in self.results_inner.winfo_children():
            w.destroy()

        profiles = load_profiles()
        if not isinstance(profiles, dict):
            print("  Warning: profile data is not a dict; resetting view.", file=sys.stderr)
            profiles = {}
        self.results_label.configure(text=f"{len(profiles)} profile(s)")

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
            btn_row, text="Rename", width=10,
            bg=T.YELLOW, fg=T.BG, font=(T.FONT, T.SZ_SM, "bold"),
            relief="flat", cursor="hand2",
            command=lambda n=profile_name: self._rename_profile(n),
        ).pack(side="left", padx=(0, 6))

        tk.Button(
            btn_row, text="Delete", width=10,
            bg=T.RED, fg=T.BG, font=(T.FONT, T.SZ_SM, "bold"),
            relief="flat", cursor="hand2",
            command=lambda n=profile_name, c=card: self._delete_profile(n, c),
        ).pack(side="left", padx=(0, 6))

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

    def _open_profile(self, profile_name):
        """Open a profile detail view showing each mod with thumbnails and remove buttons."""
        def _do_autoslot():
            try:
                slot_fix = autoslot_missing_profile_entries(profile_name)
                if slot_fix.get("assigned", 0):
                    print(f"  Auto-slotted {slot_fix['assigned']} missing entries.")
                if slot_fix.get("unslotted", 0):
                    print(f"  {slot_fix['unslotted']} skins unslotted (all slots used).")
            except Exception as e:
                import sys
                print(f"  Auto-slot error: {e}", file=sys.stderr)
        
        threading.Thread(target=_do_autoslot, daemon=True).start()

        profiles = load_profiles()
        profile = profiles.get(profile_name)
        if not profile:
            return

        self.prev_btn.configure(state="disabled")
        self.next_btn.configure(state="disabled")
        self.page_label.configure(text="")

        for w in self.results_inner.winfo_children():
            w.destroy()

        mods = profile.get("mods", [])
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
            hdr = tk.Frame(self.results_inner, bg=T.SURFACE1)
            hdr.pack(fill="x", padx=4, pady=(10, 2))
            tk.Label(hdr, text=f"  {char}  ({len(items)})",
                     bg=T.SURFACE1, fg=T.ACCENT,
                     font=(T.FONT, T.SZ_LG, "bold"), anchor="w"
                     ).pack(fill="x", padx=6, pady=3)
            for mod in items:
                self._add_profile_mod_card(profile_name, mod)

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

        card = tk.Frame(self.results_inner, bg=T.BG, padx=8, pady=6)
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
        info = tk.Frame(card, bg=T.BG)
        info.pack(side="left", fill="both", expand=True)

        title = mod_name
        tk.Label(info, text=title, bg=T.BG, fg=T.FG,
                 font=(T.FONT, T.SZ_XL, "bold"), anchor="w",
                 wraplength=420).pack(fill="x")

        # Slot badges
        if slot:
            slot_row = tk.Frame(info, bg=T.BG)
            slot_row.pack(fill="x", pady=(2, 0))
            for s in slot.replace(",", " ").split():
                s = s.strip()
                if s:
                    tk.Label(slot_row, text=s, bg=T.GREEN, fg=T.BG,
                             font=(T.MONO, T.SZ_XS, "bold"),
                             padx=4, pady=1).pack(side="left", padx=(0, 3))

        detail_parts = []
        if submitter:
            detail_parts.append(f"by {submitter}")
        if character and character != "Other":
            detail_parts.append(character)
        if detail_parts:
            tk.Label(info, text="  |  ".join(detail_parts), bg=T.BG, fg=T.SUBTEXT,
                     font=(T.FONT, T.SZ_SM), anchor="w").pack(fill="x", pady=(2, 0))

        # Buttons
        btn_row = tk.Frame(info, bg=T.BG)
        btn_row.pack(fill="x", pady=(4, 0))

        def _remove(mid=mod_id, fn=mod.get("folder_name"),
                   mn=mod_name, pn=profile_name):
            remove_mod_from_profile(pn, mod_id=mid, folder_name=fn)
            print(f"  Removed '{mn}' from profile '{pn}'")
            self._open_profile(pn)

        tk.Button(btn_row, text="Remove", width=10,
                  bg=T.RED, fg=T.BG, font=(T.FONT, T.SZ_SM, "bold"),
                  relief="flat", cursor="hand2",
                  command=_remove).pack(side="left", padx=(0, 6))

        if url:
            tk.Button(btn_row, text="Open Page", width=10,
                      bg=T.SURFACE1, fg=T.FG, font=(T.FONT, T.SZ_SM),
                      relief="flat", cursor="hand2",
                      command=lambda u=url: os.startfile(u)
                      ).pack(side="left", padx=(0, 6))

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
        downloadable = [m for m in mods if m.get("mod_id")]
        manual = [m for m in mods if not m.get("mod_id")]

        msg = (f"Load profile '{profile_name}' onto SD card?\n\n"
               f"  {len(downloadable)} mod(s) will be downloaded & installed\n")
        if manual:
            msg += f"  {len(manual)} mod(s) have no GameBanana ID (manual install needed)\n"
        msg += (f"\nThis will NOT remove existing mods.\n"
                "Mods already on the card will be re-downloaded.")

        if not messagebox.askyesno("Load Profile", msg):
            return

        self._run_async(self._do_load_profile, profile_name, downloadable, manual)

    def _do_load_profile(self, profile_name, downloadable, manual):
        """Background task: download and install each mod in the profile."""
        print(f"\n=== Loading profile '{profile_name}' "
              f"({len(downloadable)} mods) ===\n")
        success = 0
        failed = 0
        for i, mod in enumerate(downloadable, 1):
            mod_id = mod["mod_id"]
            mod_name = mod.get("name", f"Mod {mod_id}")
            slot = mod.get("slot")
            print(f"  [{i}/{len(downloadable)}] {mod_name}...")
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
                self._do_install_to_sd(mod_id, mod_name,
                                       metadata=meta, target_slot=slot)
                success += 1
            except Exception as e:
                print(f"    Error: {e}", file=sys.stderr)
                failed += 1

        if manual:
            print(f"\n  {len(manual)} mod(s) need manual install:")
            for m in manual:
                print(f"    - {m.get('name', m.get('folder_name', '?'))}")

        print(f"\n=== DONE — loaded profile '{profile_name}': "
              f"{success} installed, {failed} failed ===\n")

        self.root.after(0, self._check_sd)
        self.root.after(100, self._refresh_current_view)

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
        self._sd_present = os.path.exists(SD_CARD)
        self._poll_sd_card()

    def _stop_sd_poll(self):
        """Cancel any active SD card polling timer."""
        if self._sd_poll_id is not None:
            self.root.after_cancel(self._sd_poll_id)
            self._sd_poll_id = None

    def _poll_sd_card(self):
        """Check if SD card state changed; auto-refresh Setup tab if so."""
        now_present = os.path.exists(SD_CARD)
        if now_present != self._sd_present:
            self._sd_present = now_present
            state = "detected" if now_present else "removed"
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

        # Update inject button appearance
        if self._rcm_inject_btn and self._rcm_inject_btn.winfo_exists():
            # Only enable if paths are also resolved
            smash_ok = (find_rcm_smash() is not None
                        or (hasattr(self, "_custom_smash_path")
                            and os.path.isfile(getattr(self, "_custom_smash_path", ""))))
            pay_ok = (find_payload() is not None
                      or (hasattr(self, "_custom_payload_path")
                          and os.path.isfile(getattr(self, "_custom_payload_path", ""))))
            can = smash_ok and pay_ok and detected
            if can:
                self._rcm_inject_btn.configure(
                    bg=T.GREEN, fg=T.CRUST,
                    state="normal", cursor="hand2",
                    text="⚡ Inject Payload — READY!")
            else:
                self._rcm_inject_btn.configure(
                    bg=T.SURFACE1 if not (smash_ok and pay_ok) else T.YELLOW,
                    fg=T.OVERLAY if not (smash_ok and pay_ok) else T.CRUST,
                    state="normal" if (smash_ok and pay_ok) else "disabled",
                    cursor="hand2" if (smash_ok and pay_ok) else "arrow",
                    text="⚡ Inject Payload" + ("  (waiting for Switch…)" if (smash_ok and pay_ok) and not detected else ""))

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

        # Phase 1: build UI immediately with local-only checks (no GitHub)
        self._setup_latest = {}           # will be filled by background fetch
        self._setup_checks_done = False   # gates action buttons
        self._build_setup_ui(self._run_health_checks(latest={}), latest={})

        # Phase 2: fetch GitHub data in background, then refresh check rows
        self._run_async(self._do_setup_fetch_github)

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

    def _finish_setup_checks(self, checks, latest):
        """Main-thread: update check rows and enable action buttons."""
        if self._active_view != "setup":
            return  # user navigated away

        self._setup_checks_done = True

        # Update the summary line
        if hasattr(self, "_setup_summary_label"):
            total = len(checks)
            ok_count = sum(1 for c in checks if c["ok"])
            warn_count = sum(1 for c in checks if c.get("warn"))
            fail_count = total - ok_count
            parts = [f"{ok_count}/{total} passed"]
            if warn_count:
                parts.append(f"{warn_count} warning(s)")
            if fail_count:
                parts.append(f"{fail_count} issue(s)")
            color = T.GREEN if fail_count == 0 and warn_count == 0 else (
                T.YELLOW if fail_count == 0 else T.RED)
            self._setup_summary_label.configure(
                text="  •  ".join(parts), fg=color)

        # Update version hint
        if hasattr(self, "_setup_version_label"):
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

        # Replace check rows in the checks container
        if hasattr(self, "_setup_checks_frame"):
            for w in self._setup_checks_frame.winfo_children():
                w.destroy()
            section_labels = {
                "core": "Core Components",
                "profile": f"{self._active_profile} Plugins",
                "health": "SD Health",
            }
            shown_sections = set()
            for check in checks:
                sec = check.get("section", "core")
                if sec not in shown_sections:
                    shown_sections.add(sec)
                    lbl = section_labels.get(sec, sec)
                    sep = tk.Frame(self._setup_checks_frame, bg=T.SURFACE1, height=1)
                    sep.pack(fill="x", padx=12, pady=(10, 2))
                    tk.Label(self._setup_checks_frame, text=lbl,
                             bg=T.SURFACE, fg=T.ACCENT,
                             font=(T.FONT, T.SZ_LG, "bold")).pack(
                                 anchor="w", padx=12, pady=(2, 4))
                self._add_check_row(check, parent=self._setup_checks_frame)

        # Enable action buttons
        for btn in getattr(self, "_setup_action_btns", []):
            if btn.winfo_exists():
                btn.configure(state="normal", cursor="hand2")

    def _build_setup_ui(self, checks, latest):
        """Build the full Setup tab UI with check results."""
        for w in self.results_inner.winfo_children():
            w.destroy()
        self._thumb_cache.clear()

        profile = PROVISIONING_PROFILES.get(self._active_profile, {})
        profile_desc = profile.get("desc", "")
        is_pending = not self._setup_checks_done  # True on first render

        # ── Header + Profile selector ──
        hdr_frame = tk.Frame(self.results_inner, bg=T.SURFACE)
        hdr_frame.pack(fill="x", padx=12, pady=(12, 4))

        tk.Label(hdr_frame, text="SD Card Provisioning",
                 bg=T.SURFACE, fg=T.ACCENT,
                 font=(T.FONT, T.SZ_H2, "bold")).pack(side="left")

        # Profile dropdown (right-aligned)
        prof_frame = tk.Frame(hdr_frame, bg=T.SURFACE)
        prof_frame.pack(side="right")

        tk.Label(prof_frame, text="Profile:", bg=T.SURFACE, fg=T.FG,
                 font=(T.FONT, T.SZ_MD)).pack(side="left", padx=(0, 4))

        profile_var = tk.StringVar(value=self._active_profile)
        profile_combo = ttk.Combobox(
            prof_frame, textvariable=profile_var,
            values=list(PROVISIONING_PROFILES.keys()),
            state="readonly", width=18, font=(T.FONT, T.SZ_MD))
        profile_combo.pack(side="left")

        def _on_profile_change(event=None):
            new_prof = profile_var.get()
            if new_prof != self._active_profile:
                self._active_profile = new_prof
                print(f"  Switched profile → {new_prof}")
                self._show_setup()

        profile_combo.bind("<<ComboboxSelected>>", _on_profile_change)

        # ── Unofficial Atmosphere toggle ──
        unofficial_frame = tk.Frame(self.results_inner, bg=T.SURFACE)
        unofficial_frame.pack(fill="x", padx=12, pady=(2, 2))

        unofficial_var = tk.BooleanVar(value=self._use_unofficial_atmo)
        unofficial_cb = tk.Checkbutton(
            unofficial_frame,
            text=f"Use unofficial Atmosphere (branch: {ATMOSPHERE_SUPPORT_BRANCH})",
            variable=unofficial_var,
            bg=T.SURFACE, fg=T.PEACH, activebackground=T.SURFACE,
            activeforeground=T.PEACH, selectcolor=T.CRUST,
            font=(T.FONT, T.SZ_MD))
        unofficial_cb.pack(side="left")

        def _on_unofficial_toggle():
            self._use_unofficial_atmo = unofficial_var.get()
            label = "ON" if self._use_unofficial_atmo else "OFF"
            print(f"  Unofficial Atmosphere → {label}")
            # Re-fetch to pick up unofficial sources
            self._setup_checks_done = False
            self._show_setup()

        unofficial_cb.configure(command=_on_unofficial_toggle)

        if self._use_unofficial_atmo:
            tk.Label(unofficial_frame,
                     text="  ⚠ Pre-release — homebrew may not work",
                     bg=T.SURFACE, fg=T.YELLOW,
                     font=(T.FONT, T.SZ_SM)).pack(side="left")

        # Profile description
        tk.Label(self.results_inner, text=f"▸ {self._active_profile}: {profile_desc}",
                 bg=T.SURFACE, fg=T.OVERLAY,
                 font=(T.FONT, T.SZ_MD)).pack(anchor="w", padx=12, pady=(0, 6))

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
        shown_sections = set()
        for check in checks:
            sec = check.get("section", "core")
            if sec not in shown_sections:
                shown_sections.add(sec)
                lbl = section_labels.get(sec, sec)
                sep = tk.Frame(self._setup_checks_frame, bg=T.SURFACE1, height=1)
                sep.pack(fill="x", padx=12, pady=(10, 2))
                tk.Label(self._setup_checks_frame, text=lbl,
                         bg=T.SURFACE, fg=T.ACCENT,
                         font=(T.FONT, T.SZ_LG, "bold")).pack(
                             anchor="w", padx=12, pady=(2, 4))
            self._add_check_row(check, parent=self._setup_checks_frame)

        # ── Quick Actions — fixed bottom pane (not scrollable) ──
        af = self._setup_actions_frame
        for w in af.winfo_children():
            w.destroy()

        # Top separator
        tk.Frame(af, bg=T.SURFACE1, height=1).pack(fill="x", padx=8, pady=(0, 4))

        hdr_row = tk.Frame(af, bg=T.BG)
        hdr_row.pack(fill="x", padx=12, pady=(2, 4))
        tk.Label(hdr_row, text="Quick Actions",
                 bg=T.BG, fg=T.ACCENT,
                 font=(T.FONT, T.SZ_LG, "bold")).pack(side="left")
        if is_pending:
            tk.Label(hdr_row, text="  ⏳ loading…",
                     bg=T.BG, fg=T.YELLOW,
                     font=(T.FONT, T.SZ_SM)).pack(side="left")

        actions = [
            (f"Provision ({self._active_profile})", T.GREEN,
             "Install missing, remove extras, repair",
             lambda: self._run_async(self._provision)),
            ("Clean Provision", T.RED,
             "⚠ WIPE SD & fresh install from scratch",
             lambda: self._clean_provision_confirm()),
            ("Check for Updates", T.SURFACE1,
             "Compare installed vs latest GitHub",
             lambda: self._run_async(self._check_for_updates)),
            ("Update ALL", T.ACCENT,
             f"Download latest core + {self._active_profile}",
             lambda: self._run_async(self._update_all_from_github)),
            ("Clear Cache", T.YELLOW,
             "Delete ARCropolis cache",
             lambda: self._run_async(self._clear_cache)),
            ("Scan Conflicts", T.PEACH,
             "Check for romfs conflicts",
             lambda: self._run_async(self._scan_romfs_conflicts)),
            ("Refresh", T.SURFACE1,
             "Re-check all components",
             lambda: self._show_setup()),
        ]

        btns_row = tk.Frame(af, bg=T.BG)
        btns_row.pack(fill="x", padx=8, pady=(0, 6))

        self._setup_action_btns = []
        for label, color, desc, cmd in actions:
            row = tk.Frame(btns_row, bg=T.BG)
            row.pack(fill="x", pady=1)
            fg = T.BG if color not in (T.SURFACE1, T.YELLOW) else T.FG
            if color == T.YELLOW:
                fg = T.CRUST
            btn = tk.Button(row, text=label, width=26,
                      bg=color, fg=fg, font=(T.FONT, T.SZ_SM, "bold"),
                      relief="flat",
                      cursor="hand2" if not is_pending else "arrow",
                      state="normal" if not is_pending else "disabled",
                      command=cmd)
            btn.pack(side="left", padx=(0, 10))
            self._setup_action_btns.append(btn)
            tk.Label(row, text=desc, bg=T.BG, fg=T.OVERLAY,
                     font=(T.FONT, T.SZ_SM), anchor="w").pack(side="left", fill="x")

        # ── RCM Payload Injection ──
        tk.Frame(af, bg=T.SURFACE1, height=1).pack(fill="x", padx=8, pady=(6, 4))

        rcm_row = tk.Frame(af, bg=T.BG)
        rcm_row.pack(fill="x", padx=12, pady=(0, 2))
        tk.Label(rcm_row, text="🚀 RCM Payload Injection",
                 bg=T.BG, fg=T.ACCENT,
                 font=(T.FONT, T.SZ_LG, "bold")).pack(side="left")

        rcm_inner = tk.Frame(af, bg=T.BG)
        rcm_inner.pack(fill="x", padx=8, pady=(0, 6))

        # Auto-detect paths
        smash_path = find_rcm_smash()
        payload_path = find_payload()

        # TegraRcmSmash.exe status
        smash_row = tk.Frame(rcm_inner, bg=T.BG)
        smash_row.pack(fill="x", pady=1)
        smash_ok = smash_path is not None
        smash_icon = "✓" if smash_ok else "✕"
        smash_color = T.GREEN if smash_ok else T.RED
        smash_detail = os.path.basename(smash_path) if smash_ok else "Not found"
        tk.Label(smash_row, text=f"  {smash_icon} TegraRcmSmash:  {smash_detail}",
                 bg=T.BG, fg=smash_color,
                 font=(T.FONT, T.SZ_SM)).pack(side="left")

        if not smash_ok:
            def _browse_smash():
                from tkinter import filedialog
                p = filedialog.askopenfilename(
                    title="Locate TegraRcmSmash.exe",
                    filetypes=[("Executable", "*.exe")],
                    initialdir=SCRIPT_DIR)
                if p:
                    self._custom_smash_path = p
                    self._show_setup()
            tk.Button(smash_row, text="Browse…", width=8,
                      bg=T.SURFACE1, fg=T.FG, font=(T.FONT, T.SZ_XS),
                      relief="flat", cursor="hand2",
                      command=_browse_smash).pack(side="left", padx=(6, 0))
        # Check for user-overridden path
        if hasattr(self, "_custom_smash_path") and os.path.isfile(self._custom_smash_path):
            smash_path = self._custom_smash_path

        # Payload selector — scan for all .bin files
        pay_row = tk.Frame(rcm_inner, bg=T.BG)
        pay_row.pack(fill="x", pady=1)

        # Gather all .bin payloads from known directories
        payload_dirs = [
            os.path.join(SCRIPT_DIR, "payloads"),
            os.path.join(SD_CARD, "bootloader", "payloads"),
            os.path.join(SD_CARD, "atmosphere"),
        ]
        all_payloads = []  # list of (display_name, full_path)
        seen = set()
        for d in payload_dirs:
            if os.path.isdir(d):
                for f in sorted(os.listdir(d)):
                    if f.lower().endswith(".bin"):
                        full = os.path.join(d, f)
                        norm = os.path.normcase(os.path.abspath(full))
                        if norm not in seen:
                            seen.add(norm)
                            # Show relative context so user knows which folder
                            parent = os.path.basename(d)
                            all_payloads.append((f"{f}  ({parent}/)", full))
        # Also include custom user-browsed path
        if hasattr(self, "_custom_payload_path") and os.path.isfile(self._custom_payload_path):
            cp = self._custom_payload_path
            norm = os.path.normcase(os.path.abspath(cp))
            if norm not in seen:
                seen.add(norm)
                all_payloads.insert(0, (f"{os.path.basename(cp)}  (custom)", cp))

        if all_payloads:
            tk.Label(pay_row, text="  Payload:", bg=T.BG, fg=T.FG,
                     font=(T.FONT, T.SZ_SM)).pack(side="left", padx=(0, 4))

            # Determine which payload to pre-select
            payload_names = [name for name, _ in all_payloads]
            payload_map = {name: path for name, path in all_payloads}

            # Try to pick the previously selected or auto-detected one
            pre_select = payload_names[0]
            if hasattr(self, "_selected_payload_path"):
                for name, path in all_payloads:
                    if os.path.normcase(path) == os.path.normcase(self._selected_payload_path):
                        pre_select = name
                        break
            elif payload_path:
                for name, path in all_payloads:
                    if os.path.normcase(path) == os.path.normcase(payload_path):
                        pre_select = name
                        break

            pay_var = tk.StringVar(value=pre_select)
            pay_combo = ttk.Combobox(
                pay_row, textvariable=pay_var,
                values=payload_names, state="readonly", width=36,
                font=(T.FONT, T.SZ_SM))
            pay_combo.pack(side="left", padx=(0, 6))

            def _on_payload_select(event=None):
                chosen = pay_var.get()
                self._selected_payload_path = payload_map.get(chosen, "")

            pay_combo.bind("<<ComboboxSelected>>", _on_payload_select)

            # Set the effective payload_path from selection
            payload_path = payload_map.get(pre_select, payload_path)
            self._selected_payload_path = payload_path

            tk.Label(pay_row, text=f"({len(all_payloads)} found)",
                     bg=T.BG, fg=T.OVERLAY,
                     font=(T.FONT, T.SZ_XS)).pack(side="left", padx=(4, 0))
        else:
            # No payloads found at all
            tk.Label(pay_row, text="  ✕ No payload .bin files found",
                     bg=T.BG, fg=T.RED,
                     font=(T.FONT, T.SZ_SM)).pack(side="left")
            payload_path = None

        # Always show Browse button to add a custom payload
        def _browse_payload():
            from tkinter import filedialog
            p = filedialog.askopenfilename(
                title="Locate payload .bin (e.g. hekate_latest.bin)",
                filetypes=[("Payload", "*.bin"), ("All files", "*.*")],
                initialdir=os.path.join(SCRIPT_DIR, "payloads")
                if os.path.isdir(os.path.join(SCRIPT_DIR, "payloads"))
                else SCRIPT_DIR)
            if p:
                self._custom_payload_path = p
                self._selected_payload_path = p
                self._show_setup()
        tk.Button(pay_row, text="Browse…", width=8,
                  bg=T.SURFACE1, fg=T.FG, font=(T.FONT, T.SZ_XS),
                  relief="flat", cursor="hand2",
                  command=_browse_payload).pack(side="left", padx=(6, 0))

        # ── RCM device detection (live status) ──
        dev_row = tk.Frame(rcm_inner, bg=T.BG)
        dev_row.pack(fill="x", pady=(2, 0))
        self._rcm_device_label = tk.Label(
            dev_row,
            text=("  🎮 Switch detected in RCM mode — ready to inject!"
                  if self._rcm_detected
                  else "  🎮 No Switch in RCM mode"),
            bg=T.BG,
            fg=T.GREEN if self._rcm_detected else T.OVERLAY,
            font=(T.FONT, T.SZ_SM))
        self._rcm_device_label.pack(side="left")

        # Inject button + status label
        inject_row = tk.Frame(rcm_inner, bg=T.BG)
        inject_row.pack(fill="x", pady=(4, 0))

        can_inject = smash_path is not None and payload_path is not None
        self._rcm_status_label = tk.Label(inject_row, text="",
                                          bg=T.BG, fg=T.OVERLAY,
                                          font=(T.FONT, T.SZ_SM))

        def _do_inject():
            # Read the currently selected payload (may have changed via dropdown)
            effective_payload = getattr(self, "_selected_payload_path", payload_path)
            if not smash_path or not effective_payload:
                return
            self._rcm_status_label.configure(text="Injecting…", fg=T.YELLOW)
            self._rcm_status_label.update()
            self._run_async(self._inject_payload, smash_path, effective_payload)

        # Button appearance depends on paths + RCM detection
        ready = can_inject and self._rcm_detected
        if can_inject and not self._rcm_detected:
            btn_text = "⚡ Inject Payload  (waiting for Switch…)"
            btn_bg = T.YELLOW
            btn_fg = T.CRUST
        elif ready:
            btn_text = "⚡ Inject Payload — READY!"
            btn_bg = T.GREEN
            btn_fg = T.CRUST
        else:
            btn_text = "⚡ Inject Payload"
            btn_bg = T.SURFACE1
            btn_fg = T.OVERLAY

        inject_btn = tk.Button(
            inject_row, text=btn_text, width=32,
            bg=btn_bg, fg=btn_fg,
            font=(T.FONT, T.SZ_SM, "bold"),
            relief="flat",
            cursor="hand2" if can_inject else "arrow",
            state="normal" if can_inject else "disabled",
            command=_do_inject)
        inject_btn.pack(side="left", padx=(0, 10))
        self._rcm_inject_btn = inject_btn
        self._rcm_status_label.pack(side="left", fill="x")

        if not can_inject:
            missing = []
            if not smash_path:
                missing.append("TegraRcmSmash.exe")
            if not payload_path:
                missing.append("payload .bin")
            self._rcm_status_label.configure(
                text=f"Missing: {', '.join(missing)}", fg=T.RED)

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
            for nro in profile.get("plugins", []):
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
        profile_plugins = set(CORE_PLUGINS + profile.get("plugins", []))
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
        check_plugins = list(CORE_PLUGINS) + profile.get("plugins", [])
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
        profile_plugins = profile.get("plugins", [])
        profile_update_keys = profile.get("update_keys", [])

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
        """Internal: delete cache files without refreshing UI."""
        removed = 0
        cache_dir = os.path.join(SD_CARD, "ultimate", "cache")
        if os.path.exists(cache_dir):
            for f in os.listdir(cache_dir):
                fp = os.path.join(cache_dir, f)
                if os.path.isfile(fp):
                    os.remove(fp)
                    removed += 1
            print(f"    Cleared {removed} cache file(s)")
        meta_path = os.path.join(ATMOSPHERE_CONTENTS, "romfs_metadata.bin")
        if os.path.exists(meta_path):
            os.remove(meta_path)
            print(f"    Removed romfs_metadata.bin")
        if removed == 0 and not os.path.exists(meta_path):
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
        # Stay on current browsing tab (browse skins, stages, or other)
        if self._active_view not in ("browse", "stages", "other"):
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
        selection = self.fighter_var.get()
        is_stages = self._is_stage_mode()
        is_other = self._active_view == "other"
        if is_other:
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
        kind = "other" if is_other else ("stages" if is_stages else "skins")
        print(f"Searching {kind}: {label} (page {self._current_page})...")

        try:
            if is_other and not other_cat:
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
            self.root.after(0, lambda: self.results_label.configure(text="Error fetching results"))
            return

        self._total_results = total
        max_page = max(1, (total + RESULTS_PER_PAGE - 1) // RESULTS_PER_PAGE)

        # Update UI on main thread
        def _update():
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
        _meta = {
            "mod_id": mod_id, "name": name, "submitter": submitter,
            "likes": likes, "views": views, "url": url, "tags": tags,
            "thumb_url": _extract_thumb_url(rec),
            "image_urls": all_image_urls,
            "initial_visibility": rec.get("_sInitialVisibility", "show"),
            "has_content_ratings": rec.get("_bHasContentRatings", False),
        }

        if has_files:
            if self._is_stage_mode():
                # ── Stage mode: simple install (no slot picker) ──
                _meta["mod_type"] = "stage"
                tk.Button(btn_row, text="Install Stage to SD", width=18,
                          bg=T.GREEN, fg=T.BG, font=(T.FONT, T.SZ_MD, "bold"),
                          relief="flat", cursor="hand2",
                          command=lambda mid=mod_id, mn=name, m=_meta:
                              self._run_async(
                                  self._do_install_to_sd, mid, mn, m)
                          ).pack(side="left", padx=(0, 6))
            else:
                # ── Skin mode: slot picker ──
                # Determine fighter for slot scanning
                fighter_display = self.fighter_var.get()
                fighter_int = FIGHTER_INTERNAL.get(fighter_display)

                # If browsing All Skins, try to detect fighter from tags/name
                if not fighter_int:
                    guessed = _guess_character_from_meta({
                        "tags": tags, "name": name})
                    fighter_int = FIGHTER_INTERNAL.get(guessed)

                # Show slot picker row (with or without occupied info)
                self._add_slot_picker(btn_row, mod_id, name, _meta, fighter_int)

        # Second row: Favorite + Open Page
        btn_row2 = tk.Frame(info, bg=T.BG)
        btn_row2.pack(fill="x", pady=(2, 0))

        # Favorite toggle
        fav_text = "Unfavorite" if is_favorite(mod_id) else "Favorite"
        fav_color = T.PEACH if is_favorite(mod_id) else T.SURFACE1
        fav_fg = T.BG if is_favorite(mod_id) else T.FG

        def _toggle_fav(mid=mod_id, r=rec, btn=None,
                        mtype=_meta.get("mod_type", "skin")):
            if is_favorite(mid):
                remove_favorite(mid)
                print(f"  Removed '{r.get('_sName', '?')}' from favorites")
                if btn:
                    btn.configure(text="Favorite", bg=T.SURFACE1, fg=T.FG)
            else:
                add_favorite(mid, r, mod_type=mtype)
                print(f"  Added '{r.get('_sName', '?')}' to favorites")
                if btn:
                    btn.configure(text="Unfavorite", bg=T.PEACH, fg=T.BG)

        fav_btn = tk.Button(btn_row2, text=fav_text, width=10,
                            bg=fav_color, fg=fav_fg, font=(T.FONT, T.SZ_SM, "bold"),
                            relief="flat", cursor="hand2")
        fav_btn.configure(command=lambda b=fav_btn, mid=mod_id, r=rec: _toggle_fav(mid, r, b))
        fav_btn.pack(side="left", padx=(0, 6))

        if url:
            tk.Button(btn_row2, text="Open Page", width=10,
                      bg=T.SURFACE1, fg=T.FG, font=(T.FONT, T.SZ_SM),
                      relief="flat", cursor="hand2",
                      command=lambda u=url: os.startfile(u)
                      ).pack(side="left")

        # "Add to Profile" button
        def _add_to_profile(m=_meta):
            char = _guess_character_from_meta(m)
            entry = {
                "mod_id": m.get("mod_id"),
                "name": m.get("name", "?"),
                "character": char or "Other",
                "mod_type": m.get("mod_type", "skin"),
                "thumb_url": m.get("thumb_url"),
                "image_urls": m.get("image_urls", []),
                "url": m.get("url", ""),
                "submitter": m.get("submitter", ""),
            }
            self._pick_profile_and_add(entry)

        tk.Button(btn_row2, text="+ Profile", width=10,
                  bg=T.ACCENT, fg=T.BG, font=(T.FONT, T.SZ_SM, "bold"),
                  relief="flat", cursor="hand2",
                  command=_add_to_profile).pack(side="left", padx=(6, 0))

    def _add_slot_picker(self, parent, mod_id, mod_name, metadata, fighter_int):
        """Show 'Install to:' label + c00–c07 slot buttons.
        Filled slots show green, empty ones are greyed out.
        fighter_int may be None if the fighter couldn't be determined."""
        occupied = get_occupied_slots(fighter_int) if fighter_int else {}

        tk.Label(parent, text="Install to:", bg=T.BG, fg=T.OVERLAY,
                 font=(T.FONT, T.SZ_MD, "bold")).pack(side="left", padx=(0, 4))

        for i in range(8):
            slot = f"c{i:02d}"
            slot_info = occupied.get(slot)
            is_filled = slot_info is not None

            if is_filled:
                bg = T.GREEN
                fg = T.BG
            else:
                bg = T.SURFACE1
                fg = T.OVERLAY

            def _on_click(mid=mod_id, mn=mod_name, m=metadata, s=slot,
                          si=slot_info, filled=is_filled):
                if filled:
                    friendly = si["name"]
                    if not messagebox.askyesno(
                            "Slot Occupied",
                            f"Slot {s} already has:\n  {friendly}\n\n"
                            f"Replace with '{mn}'?"):
                        return
                self._run_async(self._do_install_to_sd, mid, mn, m, s)

            btn = tk.Button(
                parent, text=slot, width=3,
                bg=bg, fg=fg, font=(T.MONO, T.SZ_XS, "bold"),
                relief="flat", cursor="hand2",
                command=_on_click,
            )
            btn.pack(side="left", padx=1)

            # Hover effects with thumbnail tooltip
            if is_filled:
                friendly = slot_info["name"]
                thumb = slot_info.get("thumb_url")
                btn.bind("<Enter>", lambda e, b=btn, t=friendly, tu=thumb: (
                    b.configure(bg=T.YELLOW, fg=T.BG),
                    self._show_tooltip(b, t, thumb_url=tu)))
                btn.bind("<Leave>", lambda e, b=btn: (
                    b.configure(bg=T.GREEN, fg=T.BG),
                    self._hide_tooltip()))
            else:
                btn.bind("<Enter>", lambda e, b=btn: (
                    b.configure(bg=T.OVERLAY, fg=T.BG),
                    self._show_tooltip(b, "Empty — click to install")))
                btn.bind("<Leave>", lambda e, b=btn: (
                    b.configure(bg=T.SURFACE1, fg=T.OVERLAY),
                    self._hide_tooltip()))

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
                    resp = requests.get(url, timeout=15)
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
                    resp = requests.get(url, timeout=10)
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

    def _do_install_to_sd(self, mod_id, mod_name, metadata=None, target_slot=None):
        """Download the first file of a mod and install directly to SD card.
        Performs comprehensive file-level conflict detection BEFORE copying.
        If conflicts are found, auto-reslots to free slots or asks the user.

        Stage mods (metadata['mod_type'] == 'stage') skip slot logic entirely.
        """
        if not os.path.exists(SD_CARD):
            print(f"ERROR: SD card not found at {SD_CARD}")
            return

        is_stage = (metadata or {}).get("mod_type") == "stage"
        kind = "stage" if is_stage else "skin"

        slot_msg = f" to slot {target_slot}" if target_slot else ""
        print(f"\n--- Installing {kind} '{mod_name}'{slot_msg} to SD ---")

        try:
            # Download the archive
            archive_path = self._download_mod_archive(mod_id, mod_name)
            if not archive_path:
                return

            if is_stage:
                # ── Stage install: simple extract & copy, no slot logic ──
                os.makedirs(ARCROPOLIS_MODS, exist_ok=True)
                install_to_sd(archive_path, mod_name, metadata=metadata)
                try:
                    os.remove(archive_path)
                except Exception:
                    pass
                print(f"  DONE!\n")
                self.root.after(0, self._check_sd)
                self.root.after(100, self._refresh_current_view)
                return

            # Extract to peek at slot structure
            print(f"  Extracting archive to inspect...")
            tmp_dir = tempfile.mkdtemp(prefix="gb_peek_")
            try:
                extract_archive(archive_path, tmp_dir)
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

                slot_map = None  # will be set if multi-slot mapping is used

                if target_slot and len(all_src) > 1:
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

                if conflicts:
                    summary = _summarise_conflicts(conflicts)
                    print(f"  ⚠ Detected file conflicts:\n{summary}")

                    # Determine if we can auto-reslot
                    can_auto = False
                    if fighter_int and not slot_map:
                        # For single-slot mods (or "Install to SD" with no
                        # target), try to find a completely free slot
                        needed = max(len(all_src), 1)
                        free = find_free_body_slots(fighter_int, needed)
                        if len(free) >= needed:
                            can_auto = True

                    # Ask user on main thread
                    choice = [None]
                    choice_event = threading.Event()

                    def _ask_conflict():
                        msg = (f"Installing '{mod_name}' would conflict with "
                               f"existing mods:\n\n{summary}\n\n")
                        if can_auto:
                            msg += (f"Auto-reslot to free slot(s) "
                                    f"{', '.join(free[:needed])}?")
                            ans = messagebox.askyesnocancel(
                                "Slot Conflict Detected", msg,
                                icon="warning")
                            # Yes = auto-reslot, No = install anyway, Cancel = abort
                            if ans is True:
                                choice[0] = "auto"
                            elif ans is False:
                                choice[0] = "force"
                            else:
                                choice[0] = "cancel"
                        else:
                            msg += "Install anyway (may cause in-game issues)?"
                            if messagebox.askyesno(
                                    "Slot Conflict Detected", msg,
                                    icon="warning"):
                                choice[0] = "force"
                            else:
                                choice[0] = "cancel"
                        choice_event.set()

                    self.root.after(0, _ask_conflict)
                    choice_event.wait()

                    if choice[0] == "cancel":
                        print(f"  Cancelled by user.\n")
                        return
                    elif choice[0] == "auto":
                        if slot_map:
                            # Shouldn't reach here but be safe
                            pass
                        elif len(all_src) <= 1:
                            target_slot = free[0]
                            print(f"  Auto-reslotting to {target_slot}")
                        else:
                            # Build a new slot_map from src slots → free slots
                            slot_map = {}
                            for i, src in enumerate(all_src):
                                slot_map[src] = free[i]
                            print(f"  Auto-reslotting: "
                                  f"{', '.join(f'{s}->{t}' for s, t in slot_map.items())}")
                    # else: "force" — proceed as-is

                # ── Perform install ────────────────────────────
                os.makedirs(ARCROPOLIS_MODS, exist_ok=True)
                if slot_map:
                    install_to_sd(archive_path, mod_name, metadata=metadata,
                                  slot_map=slot_map)
                else:
                    install_to_sd(archive_path, mod_name, metadata=metadata,
                                  target_slot=target_slot)
            finally:
                shutil.rmtree(tmp_dir, ignore_errors=True)

            # Cleanup archive
            try:
                os.remove(archive_path)
            except Exception:
                pass

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

    def _download_mod_archive(self, mod_id, mod_name):
        """Download a mod's first file and return local path, or None."""
        print(f"  Fetching file info...")
        data = api_get_mod_files(mod_id)
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

        tmp_path = os.path.join(tempfile.gettempdir(), filename)
        self._show_progress(f"Downloading {filename}...")
        last_pct = [-1]

        def _progress(downloaded, total):
            pct = int(downloaded * 100 / total) if total else 0
            if pct != last_pct[0]:
                last_pct[0] = pct
                self._update_progress(downloaded, total)

        download_file_to(dl_url, tmp_path, _progress,
                         cancel_check=lambda: self._cancel_download)

        self._hide_progress()
        print(f"  Download complete.")
        return tmp_path

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

def main():
    if not HAS_REQUESTS:
        print("ERROR: 'requests' package required. Run: pip install requests")
        sys.exit(1)

    if not HAS_PIL:
        print("NOTE: Install Pillow for thumbnail previews: pip install Pillow")

    root = tk.Tk()
    app = GameBananaBrowser(root)
    root.mainloop()


if __name__ == "__main__":
    main()

