# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Smash Night is a Windows-only Tkinter GUI that browses GameBanana for Super Smash Bros. Ultimate mods and installs them onto a Switch SD card running Atmosphere CFW + ARCropolis. It also handles full Switch provisioning: downloading and laying down Atmosphere/Hekate/Skyline/ARCropolis to the SD, and injecting the fusée/hekate payload over USB via `TegraRcmSmash.exe`.

The entire app lives in one file: `smash_night.py` (~13k lines).

## Run / develop

- Launch normally: `.venv\Scripts\pythonw.exe smash_night.py`
- Launch as admin (required for RCM payload injection on systems with UAC fully disabled): double-click `Run_As_Admin.bat`
- There is no test suite, no linter config, no build step. `_test_search.py` is a one-off API probe (and currently imports a stale `gamebanana_browser` module name — broken, do not rely on it).
- The `.venv` checked in here points at `C:\Users\carobins\...` — it's stale on a fresh checkout. Recreate with `python -m venv .venv` and install `requests`, `Pillow`, optionally `py7zr`, `rarfile`, and (for 3D preview) `numpy`, `ssbh_data_py`, `trimesh`, `pyrender`.
- `fix_profiles.py` is a one-shot codemod that was run once to inject behavior into `smash_night.py`. It hardcodes a path under `c:\Users\gvopa\...` and is not meant to be re-run. Treat it as historical.

## Big picture

`smash_night.py` is structured as a long script of module-level helpers followed by a single giant `GameBananaBrowser(tk.Tk)` class that owns all UI. Section banners (`# ── SECTION ──`) are the navigation aid — use `grep -n '^# ──'` to jump.

### Module-level layers (top-to-bottom in the file)

1. **SD-card / path constants** (lines ~70–260). `SD_CARD` is auto-detected from removable + Switch-marker drives; `_apply_sd_drive()` mutates a set of derived globals (`ARCROPOLIS_MODS`, `ATMOSPHERE_CONTENTS`, `PLUGINS_DIR`, `EXEFS_DIR`, `ROMFS_DIR`, `PAYLOAD_SEARCH_PATHS`) when the active drive changes. Anything that touches the SD must read these *globals* (not cache them at import time).
2. **Provisioning profiles + GitHub release map** (lines ~220–290). `PROVISIONING_PROFILES` (Competitive / Skins Only / Custom) layers extra `.nro` plugins on top of `CORE_PLUGINS`. `GITHUB_REPOS` is the source of truth for what to download and how to recognize each asset by filename. Atmosphere has an "unofficial" path: when a new firmware ships, the official repo lags, so we fall back to `UNOFFICIAL_ATMOSPHERE_FORK` (`zandercodes/Atmosphere-unofficial`) for builds of the `22_support` branch, and then to a local zip in `switch_setup/downloads/`.
3. **GameBanana category tables** (~290–560). `FIGHTER_CATEGORIES` (skins, cat 3330), `STAGE_CATEGORIES` (cat 6089), `OTHER_CATEGORIES`, gameplay subcats. `MOD_TYPE_BY_CATEGORY` collapses a GameBanana subcategory id onto a coarse type (`skin` / `stage` / `moveset` / `modpack` / `mechanics` / `balance` / `ai` / `parameters` / `effect` / `music` / `ui` / `other`). Only `skin` mods drive the slot-picker UI (`SLOT_AWARE_MOD_TYPES`).
4. **File-conflict / slot classification** (~700–900, ~2360–2840). `_classify_mod_path(rel_path)` is the most important helper here: it returns `(fighter, slot)` for a relative file path inside a mod and is shared by:
   - `detect_file_conflicts()` — distinguishes *real* per-slot collisions (must remap) from shared-resource overlaps that ARCropolis layers last-write-wins (no prompt).
   - `compute_touched_slots()` — populates `gb_touched_cache.json` after install.
   - `simulate_resolved_layout()` / `diagnose_freeze_risks()` — pre-flight scan that mimics what SSBU's file resolver will see and flags freeze risks (orphan portrait/body, motion without model, multi-mod collisions).
5. **GameBanana API** (~930–1110). `api_search_mods` is the single search path for browse / stages / "Other". `api_get_mod_files` and `api_get_mod_images` round it out. All over `https://gamebanana.com/apiv11`. Network is `requests` with `verify=False` and urllib3 warnings suppressed.
6. **Install pipeline** (~1620–2240). `extract_archive` → `find_mod_content` (locates the romfs root inside the extracted tree) → `install_to_sd` (copies to `<SD>/ultimate/mods/<name>[_cXX]`, writes `.gb_meta.json`, applies optional `slot_map` or `target_slot` remap). `_remap_slots` / `_apply_slot_map` rename `cXX` directories *and* slot-bound filenames (UI bntx, sound nus3audio) consistently. `_repair_multislot_artifacts` cleans up GameBanana archives that ship malformed dirs like `c00, c03`.
7. **RCM injection** (~1640–1830). `inject_payload(smash_exe, payload_path)` has a careful three-branch strategy: (1) if already admin, run subprocess directly; (2) if `EnableLUA=0`, bail with a message pointing at `Run_As_Admin.bat` (Windows can't elevate at all in this state); (3) otherwise, elevate via `ShellExecuteEx` with `SEE_MASK_NOCLOSEPROCESS` and wait on the process handle. Do **not** replace this with PowerShell `Start-Process -Verb RunAs` — signed-script enforcement and ExecutionPolicy break it intermittently with a misleading "operation was canceled by the user" error.
8. **Profiles + favorites persistence** (~3260–3620). Two related concepts:
   - **User profiles** (`gb_profiles.json`) — curated lists of mods plus per-profile settings (`template`, `wifi_safe`, `unofficial_atmo`, custom plugin set). `profile_config()` applies template defaults, treating `plugins=None` as "inherit from template" and `[]` as "user explicitly disabled all plugins".
   - **Favorites** (`gb_favorites.json`) — flat per-mod stars surfaced in the Favorites view.
9. **`GameBananaBrowser`** (line 4037 onwards). One class, ~9000 lines, ~230 methods. The active view is tracked by `self._active_view` (`browse` / `stages` / `favorites` / `installed` / `setup` / etc.). UI rebuilds itself by clearing `self.results_inner` and repopulating from the relevant data source. Long-running work (downloads, installs, validation, audits) goes through `self._run_async` so the Tk main loop stays responsive; UI updates from worker threads must be wrapped in `self.root.after(...)`.

### State on disk

- `gb_profiles.json` — user mod profiles.
- `gb_favorites.json` — starred mods.
- `gb_audit_cache.json` — adult-only audit results, keyed by search params.
- `gb_touched_cache.json` — `{mod_id: {touched: [[fighter, slot], ...], ts: ...}}`. Re-populated on every successful install via `record_touched_for_mod`. Used by profile-validation to pre-flight-detect cross-character slot collisions (e.g. a mod tagged "Birdo" that secretly replaces `fighter/yoshi/c02/`) **without** re-extracting archives.
- `gb_sets.json` — legacy profile file. `load_profiles()` migrates it to `gb_profiles.json` once if the new file is missing.
- `.mod_cache/<mod_id>/` — extracted mod archives, kept around so we can re-render previews / re-install without re-downloading.
- `.render_cache/` — PNG thumbnails of 3D model previews produced by `render_model_preview` (uses `pyrender` + `trimesh` + `ssbh_data_py`; gracefully no-ops if those packages aren't installed).
- `payloads/hekate_latest.bin`, `payloads/fusee.bin` — bundled RCM payloads. Search order is in `PAYLOAD_SEARCH_PATHS`; hekate is preferred because it supports any FW Atmosphere supports.
- `rcm_tools/TegraRcmGUI_v2.6_portable/TegraRcmSmash.exe` — bundled RCM injector CLI; `find_rcm_smash()` searches a few likely locations.
- `ssbh_editor/ssbh_editor.exe` — auto-downloaded by `_ensure_ssbh_editor()` from the latest GitHub release; used for the "Open in SSBH Editor" action on installed skins.
- `switch_setup/mods/arcropolis/extracted/...`, `switch_setup/downloads/...` — pre-staged ARCropolis and (optionally) local Atmosphere overrides.

### A few non-obvious invariants

- `MAX_SLOT = 16` (c00..c15). SSBU vanilla only has 8 slots, but ARCropolis lets modded fighters exceed that — don't lower this without auditing the slot picker.
- `WIFI_UNSAFE_MOD_TYPES` is what gates the wifi-safe profile filter. Everything else (skin/stage/ui/music) is treated as wifi-safe.
- "Slot collision" and "shared overlap" are distinct concepts in this codebase — only the former should ever surface a remap prompt to the user. `_classify_mod_path` returning `(None, None)` means "shared resource, last-write-wins, do not warn".
- When the user picks a different SD drive from the multi-drive prompt at startup, you must call `_apply_sd_drive(drive)` rather than just reassigning `SD_CARD` — there are several derived globals.
- `numpy>=2.0` removed `np.infty`, but `pyrender.Viewer` still references it. There's a shim at the top of the file (`np.infty = np.inf`); leave it in if you touch the imports.
