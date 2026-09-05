# Vendored: ssbu-online-deluxe (Quickplay-enabled)

| | |
|---|---|
| Upstream | https://github.com/saad-script/ssbu-online-deluxe |
| Tag / commit | `v1.4.1` = `c54c116` (2026-09-04, "Merge pull request #52 from frame-0/v1.4.1-latency-patch") |
| License | AGPL-3.0 — see `LICENSE` (unchanged). This tree is a modified copy; the modification is described below. |
| Built by | `.github/workflows/build-online-deluxe.yml` → `smash_mods/quickplay/libssbu_online_deluxe.nro` |

## Why it's vendored

Upstream deliberately enables its latency slider and render-profile
controls only in Online Arena, Local Online and Nextendo-redirected
Quickplay. In Nintendo's own Quickplay/Elite the controls are inert and the
native character-select UI is hidden. We want them there, and we want the
whole thing editable and buildable from this repo alone — no fork, no
external repo at build time.

## What changed vs. upstream

Exactly two hunks, both in `src/net/mod.rs`, both marked with a `// Fork:`
comment:

1. `online_melee_any_init` — always stores `MatchConnectionStatus::OnlineQuickPlay`
   instead of `Offline`-unless-Nextendo.
2. `is_valid_online_mode()` — `|| is_online_quickplay_mode()` unconditionally,
   instead of `|| (is_online_nextendo_redirect_active() && is_online_quickplay_mode())`.

The `is_online_nextendo_redirect_active()` helper is left in place, unused.

## What was dropped from the upstream tree

`.git/`, `.github/` (upstream's issue template), `scripts/` (upstream's
PowerShell SD-folder installer — Smash Night does that job) and
`emu_build.sh` (a personal emulator dev loop). Everything the build needs
is here: `src/`, `lib/libimgui_smash.a` (the link blob `build.rs` points
at — required), `Cargo.toml`, `Cargo.lock` (pins the 13.0.5 dependency
commits), `build.rs`.

## Updating to a new upstream version

1. Clone upstream at the new tag.
2. Copy its tree over this directory (keeping this file), then re-apply the
   two hunks above — they are small and the surrounding code rarely moves.
3. Update the table at the top, push. The workflow builds on any change
   under `vendor/ssbu-online-deluxe/`.

Only the `.nro` is built here. The bundled Skyline (`main.npdm`,
`subsdk9`), `libssbusync.nro`, `libnx_over.nro` and the overclock sysmodule
are not in the source repo and still come from upstream's release.
