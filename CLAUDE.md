# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Smash Night is a Windows-only Tkinter GUI that browses GameBanana for Super Smash Bros. Ultimate mods and installs them onto a Switch SD card running Atmosphere CFW + ARCropolis. It also handles full Switch provisioning: downloading and laying down Atmosphere/Hekate/Skyline/ARCropolis to the SD, and injecting the fusée/hekate payload over USB via `TegraRcmSmash.exe`.

The entire app lives in one file: `smash_night.py` (~13k lines).

## Run / develop

- Launch normally: double-click `Run.bat` (auto-runs `setup.bat` if `.venv` is missing or built on an unsupported Python), or `.venv\Scripts\pythonw.exe smash_night.py` directly.
- Launch as admin (required for RCM payload injection on systems with UAC fully disabled): double-click `Run_As_Admin.bat`
- There is no test suite, no linter config, no build step. `_test_search.py` is a one-off API probe (and currently imports a stale `gamebanana_browser` module name — broken, do not rely on it).
- **Python must be 3.10–3.12** — `ssbh_data_py` ships no wheels past 3.12, and without it the 3D stack dies and skin installs skip the slot picker (falling back to SSBH Editor). `setup.bat` enforces this: it finds (or winget-installs) Python 3.12, recreates `.venv` if it's on a too-new Python, installs `requirements.txt`, then installs `pyrender` with `--no-deps` (it hard-pins `PyOpenGL==3.1.0`, which modern pip refuses to resolve against the `>=3.1.7` override — never put pyrender back into requirements.txt), and winget-installs WinRAR if no UnRAR is present (`.rar` mods extract as empty husks without it). The in-app `_bootstrap_deps()` / `_bootstrap_unrar()` mirror most of this for people who launch the .py directly.
- `fix_profiles.py` is a one-shot codemod that was run once to inject behavior into `smash_night.py`. It hardcodes a path under `c:\Users\gvopa\...` and is not meant to be re-run. Treat it as historical.

## Big picture

`smash_night.py` is structured as a long script of module-level helpers followed by a single giant `GameBananaBrowser(tk.Tk)` class that owns all UI. Section banners (`# ── SECTION ──`) are the navigation aid — use `grep -n '^# ──'` to jump.

### Module-level layers (top-to-bottom in the file)

1. **SD-card / path constants** (lines ~70–260). `SD_CARD` is auto-detected from removable + Switch-marker drives; `_apply_sd_drive()` mutates a set of derived globals (`ARCROPOLIS_MODS`, `ATMOSPHERE_CONTENTS`, `PLUGINS_DIR`, `EXEFS_DIR`, `ROMFS_DIR`, `PAYLOAD_SEARCH_PATHS`) when the active drive changes. Anything that touches the SD must read these *globals* (not cache them at import time).
2. **Provisioning profiles + GitHub release map** (lines ~220–290). `PROVISIONING_PROFILES` (Competitive / Skins Only / Custom) layers extra `.nro` plugins on top of `CORE_PLUGINS`. `GITHUB_REPOS` is the source of truth for what to download and how to recognize each asset by filename. Atmosphere has an "unofficial" path: when a new firmware ships, the official repo lags, so we fall back to `UNOFFICIAL_ATMOSPHERE_FORK` (`zandercodes/Atmosphere-unofficial`) for builds of the `22_support` branch, and then to a local zip in `switch_setup/downloads/`.

   ARCropolis has the same lag problem in a sharper form: it hard-refuses any Smash version but the one it was built for, and a new Smash update can leave the fix on `main` unreleased for days (13.0.5 shipped; v4.0.9 was still a 13.0.4 build). `find_local_arcropolis_override()` looks in `switch_setup/downloads/` for a hand-placed `libarcropolis.nro` (or a zip containing one, e.g. a CI artifact) and `_download_plugin_from_github` prefers it over GitHub — which also keeps *Update All* from clobbering it. `arcropolis_required_smash_version()` reads the required Smash version out of any `libarcropolis.nro` (the refusal dialog is embedded as a plain string), and the Setup tab shows it as "built for Smash X" so a mismatch is visible before boot.

   The automatic answer to that lag is the **nightly**: ARCropolis CI builds on every push to its default branch (`ARCROPOLIS_CI_BRANCH = "master"` — not `main`) and uploads an artifact named `arcropolis`. `github_latest_ci_artifact()` picks the newest non-expired one from that branch and `_install_arcropolis_nightly()` extracts `libarcropolis.nro` from it. It is a per-profile setting, `nightly_arcropolis`, threaded exactly like `unofficial_atmo` (default on, checkbox in both profile dialogs, `self._use_nightly_arcropolis`). Precedence in `_download_plugin_from_github` is local override → nightly → release. GitHub does not serve artifact downloads anonymously, so the nightly needs `github_token.txt`; without one it falls back to the release and says so in the install log and the Setup row.

   `KNOWN_PLUGINS` is the plugin registry — dispatch on it, not on `LOCAL_PLUGINS` (that's only a fallback *cache* of `.nro` files bundled in this repo, and most plugins have no bundled copy). Two flags matter:
   - `dependency: True` — installed and removed alongside the mod that needs it, never toggled on its own. `expand_plugin_deps()` adds them; `selectable_plugins()` hides them from the profile editor.
   - `deprecated: True` — never installed, but still listed so a card provisioned by an older build is recognised and cleaned up. `RETIRED_PLUGINS` maps each to its replacement, and `migrate_retired_plugins()` translates at *read* time inside `profile_config()` — `gb_profiles.json` is never rewritten, so an older build of the app can still read it.

   **SSBU Online Deluxe** (`saad-script/ssbu-online-deluxe`) replaced Latency Slider DE + Less Delay when SSBU 13.0.5 broke both. It is not a standalone `.nro`: `ONLINE_DELUXE_DEPS` lists six plugins it needs, it pins its *own* Skyline build (`_install_skyline()` takes `main.npdm`/`subsdk9` from the mod's release, because upstream warns the current skyline-dev release crashes with it), and it wants a boot2 overclock sysmodule under `OC_SYSMODULE_TITLE_ID` (`oc_sysmodule_dir()`, `_install_oc_sysmodule()`). A partial install is a guaranteed crash on boot — never let one of these pieces be installed without the others.

   `extract_release_file(repo, filename, dest)` is the single install path for every plugin: it prefers a release asset named exactly `filename`, else searches inside every `.zip` asset for a member with that basename. Match on the *payload* filename, never the asset name — upstream renames release archives between versions far more often than the files inside them. Release archives are cached per run in `_RELEASE_ZIP_CACHE` (the Online Deluxe zip supplies seven files); call `_clear_release_cache()` when a provision/update run finishes.

   **Quickplay fork.** Upstream deliberately enables its latency / render-profile controls only in Arena, Local Online and Nextendo-Quickplay (`is_valid_online_mode()` in `src/net/mod.rs`, plus the status store in `online_melee_any_init`). `FalconPunches/ssbu-online-deluxe` is a fork with both gates removed; its CI publishes `libssbu_online_deluxe.nro` to a rolling `quickplay-latest` release. `deluxe_nro_sources()` makes `_download_plugin_from_github` try the fork (`ONLINE_DELUXE_FORK_REPO_KEY`) before upstream for the `.nro` *only* — Skyline, ssbusync, nx-over and the sysmodule are not in the source repo and keep coming from upstream's release via `ONLINE_DELUXE_REPO_KEY`. `_NRO_TO_REPO[ONLINE_DELUXE_NRO]` still names upstream so the Setup row's version hint tracks the real project. If the fork has no release, install falls back to upstream and the log says so.
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

- `MAX_SLOT = 16` (c00..c15) is how far to *scan* when reading a card — a c08+ mod someone installed by hand must still be seen and conflict-checked. `VANILLA_SLOTS = 8` is how far this app may *assign*: every site that picks a free slot goes through `assignable_slots()` (c00..c07), and both slot pickers render c08+ greyed and unclickable. Slots c08+ don't exist in `data.arc`; they only work with `new-dir-infos` / `new-dir-infos-base` / `share-to-added` declarations that `_regenerate_config_json` does not generate, so handing one out produces a mod that freezes at match load. Don't reintroduce `range(MAX_SLOT)` in an assigning path.
- `_regenerate_config_json` must never filter `share-to-vanilla` / `share-to-added` by disk existence. Per ARCropolis `fs.rs`, each key is a source *hash* resolved against the arc and each destination is fed to `add_shared_file` precisely when it is *not* already a file — a destination not being on disk is the designed case. Filtering on it deletes every entry and is what made reslotted skins freeze while native-slot installs (which never rewrite `config.json`) kept working. Only `new-dir-files` entries are real mod files and safe to orphan-strip.
- `WIFI_UNSAFE_MOD_TYPES` is what gates the wifi-safe profile filter. Everything else (skin/stage/ui/music) is treated as wifi-safe.
- "Slot collision" and "shared overlap" are distinct concepts in this codebase — only the former should ever surface a remap prompt to the user. `_classify_mod_path` returning `(None, None)` means "shared resource, last-write-wins, do not warn".
- When the user picks a different SD drive from the multi-drive prompt at startup, you must call `_apply_sd_drive(drive)` rather than just reassigning `SD_CARD` — there are several derived globals.
- `numpy>=2.0` removed `np.infty`, but `pyrender.Viewer` still references it. There's a shim at the top of the file (`np.infty = np.inf`); leave it in if you touch the imports.
