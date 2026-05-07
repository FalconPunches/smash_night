# ssbh_render — headless SSBU model renderer

Rust CLI that renders an SSBU model directory to a PNG using
ScanMountGoat's `ssbh_wgpu` (the same shader graph that drives
`ssbh_editor`). When built, smash_night prefers this over its
pyrender approximation for thumbnails and the View Model viewer.

## Build

Requires the Rust toolchain. Install once:

```
winget install Rustlang.Rustup
```

(open a new terminal so PATH picks up `cargo`)

Then build:

```
cd ssbh_render
cargo build --release
```

The first build takes ~5–10 minutes (downloads + compiles wgpu, naga,
ssbh_wgpu, etc.). Subsequent builds are incremental.

The binary lands at `ssbh_render/target/release/ssbh_render.exe`.

## Run manually

```
ssbh_render <model_dir> <output.png> [--width 800] [--height 600]
```

Example:
```
ssbh_render ".mod_cache/175343/extracted/KHCloud_ExtraSlots/fighter/cloud/model/body/c08" cloud.png
```

## How smash_night uses it

`render_model_preview` checks for the binary at
`ssbh_render/target/release/ssbh_render.exe`. If present, it shells out;
otherwise it falls back to the pyrender PBR approximation.
