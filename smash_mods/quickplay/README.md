# Quickplay-enabled SSBU Online Deluxe build

`libssbu_online_deluxe.nro` in this folder is **built by CI, not by hand** —
see `.github/workflows/build-online-deluxe.yml` and `BUILD_INFO.txt`
(written by the same run) for the exact upstream tag and commit.

## What it is

[saad-script/ssbu-online-deluxe](https://github.com/saad-script/ssbu-online-deluxe)
deliberately enables its latency slider and render-profile controls only in
Online Arena, Local Online and Nextendo-redirected Quickplay. In Nintendo's
own Quickplay/Elite the controls are inert and the native character-select
UI is hidden.

`vendor/ssbu-online-deluxe/` is upstream at a pinned tag with that gate
removed (two hunks in `src/net/mod.rs`, both marked `// Fork:`; see its
`VENDOR_INFO.md`). The workflow builds that tree in the same `cargo-skyline`
container the Smash modding projects' own CI uses and commits the result
here. Nothing is fetched from upstream at build time.

## How Smash Night uses it

`_download_plugin_from_github` prefers this file over upstream's release
**for the `.nro` only** (`ONLINE_DELUXE_QUICKPLAY_BUILD`). Everything else
Online Deluxe needs — the bundled Skyline (`main.npdm`, `subsdk9`),
`libssbusync.nro`, `libnx_over.nro` and the overclock sysmodule — is not in
the source repo and still comes from upstream's release. If this file is
absent, install falls back to upstream's `.nro` unchanged.

## Updating

When upstream tags a new version: follow the steps in
`vendor/ssbu-online-deluxe/VENDOR_INFO.md` (copy the new tree in, re-apply
the two marked hunks, update its table), push, and check the run. A build
failure leaves the previous binary here untouched.

## Caveat

Upstream keeps this off Nintendo's servers on purpose, and its README calls
the ban risk "non-zero". Using this build in Quickplay is a choice; the
upstream `.nro` behaves exactly as the author intends.
