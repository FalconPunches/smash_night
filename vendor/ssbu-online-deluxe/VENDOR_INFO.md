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

Four hunks across two files, each marked with a `// Fork:` comment
(`grep -rn "// Fork:" src` lists them all):

`src/net/mod.rs`
1. `online_melee_any_init` — always stores `MatchConnectionStatus::OnlineQuickPlay`
   instead of `Offline`-unless-Nextendo.
2. `is_valid_online_mode()` — `|| is_online_quickplay_mode()` unconditionally,
   instead of `|| (is_online_nextendo_redirect_active() && is_online_quickplay_mode())`.
3. `online_bg_matchmaking_init` — no longer resets the status to `Offline` when
   already in Quickplay. Quickplay searches for an opponent *after* the
   character select screen and this hook fires for that search; upstream's
   reset is why the match started with the *offline* render profile (Vanilla)
   regardless of the selection.

`src/render/profile.rs`
4. `match_init` — `in_real_online_match` is true in any valid online mode, not
   only when the pia session already reports connected. Quickplay brings the
   session up later than Arena does; upstream's `is_connected` gate sent it
   down the offline branch.

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
