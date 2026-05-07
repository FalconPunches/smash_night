// ssbh_render — headless CLI that renders an SSBU model directory to a
// PNG using ScanMountGoat's ssbh_wgpu (the same shader pipeline that
// drives ssbh_editor). Mirrors the reference implementation in
// ssbh_wgpu/ssbh_wgpu_test/src/main.rs but trimmed to a single
// model + single output, with configurable dimensions and a
// proper aspect-ratio-aware camera.
//
// Invocation:  ssbh_render <model_dir> <output.png> [--width 800] [--height 600]

use std::path::{Path, PathBuf};

use clap::Parser;
use glam::{Mat4, Vec3, Vec4};
use ssbh_wgpu::{
    load_render_models, CameraTransforms, ModelFolder, ModelRenderOptions,
    SharedRenderData, SsbhRenderer, REQUIRED_FEATURES, REQUIRED_LIMITS,
};

#[derive(Parser, Debug)]
#[command(version, about = "Headless SSBU model renderer (ssbh_wgpu).", long_about = None)]
struct Args {
    /// Folder containing model.numshb / model.numdlb / model.numatb / nutexb files.
    model_dir: PathBuf,
    /// Output PNG path. Required unless --server is set.
    #[arg(required_unless_present = "server")]
    output: Option<PathBuf>,
    /// Render width in pixels.
    #[arg(long, default_value_t = 800)]
    width: u32,
    /// Render height in pixels.
    #[arg(long, default_value_t = 600)]
    height: u32,
    /// Camera Y rotation (yaw) in degrees. 0 = front-on, 35 = default 3/4.
    #[arg(long, default_value_t = 35.0)]
    yaw: f32,
    /// Camera X rotation (pitch) in degrees. Negative looks down.
    #[arg(long, default_value_t = 0.0)]
    pitch: f32,
    /// Horizontal pan offset (model-space units). Right-positive.
    #[arg(long, default_value_t = 0.0)]
    pan_x: f32,
    /// Vertical pan offset (model-space units). Up-positive.
    #[arg(long, default_value_t = 0.0)]
    pan_y: f32,
    /// Zoom factor — 1.0 fits the model, <1.0 zooms in, >1.0 zooms out.
    #[arg(long, default_value_t = 1.0)]
    zoom: f32,
    /// Persistent server mode: reads commands from stdin
    /// (one per line): "<yaw> <pitch> <pan_x> <pan_y> <zoom> <width> <height> <output_path>".
    /// Writes "OK" or "ERR <msg>" to stdout per command.
    /// Quits on EOF or "quit".
    #[arg(long, default_value_t = false)]
    server: bool,
}

fn main() -> anyhow::Result<()> {
    let _ = env_logger::try_init();
    let args = Args::parse();

    if !args.model_dir.is_dir() {
        anyhow::bail!("Model dir not found: {:?}", args.model_dir);
    }

    pollster::block_on(run(args))
}

/// Compute the AABB of the mesh in `<model_dir>/model.numshb`.
/// Returns ``(center, extent)`` where ``extent`` is the longest
/// axis half-length, used to size the camera distance.
fn compute_bounds(model_dir: &Path) -> Option<(Vec3, f32)> {
    let numshb = model_dir.join("model.numshb");
    if !numshb.is_file() {
        return None;
    }
    let mesh = ssbh_data::mesh_data::MeshData::from_file(&numshb).ok()?;
    let mut min = Vec3::splat(f32::INFINITY);
    let mut max = Vec3::splat(f32::NEG_INFINITY);
    let mut found = false;
    for obj in &mesh.objects {
        for attr in &obj.positions {
            // VectorData is an enum (Vec2/Vec3/Vec4); positions are
            // always Vec3 in SSBU.
            if let ssbh_data::mesh_data::VectorData::Vector3(rows) = &attr.data {
                for r in rows {
                    let v = Vec3::new(r[0], r[1], r[2]);
                    min = min.min(v);
                    max = max.max(v);
                    found = true;
                }
            }
        }
    }
    if !found {
        return None;
    }
    let center = (min + max) * 0.5;
    let size = max - min;
    let extent = size.x.max(size.y).max(size.z) * 0.5;
    Some((center, extent))
}

/// Persistent render context — wgpu device + ssbh_wgpu renderer + the
/// loaded model. Created once per binary invocation; `render_one`
/// reuses everything for each frame.
struct RenderCtx {
    device: wgpu::Device,
    queue: wgpu::Queue,
    shared_data: SharedRenderData,
    renderer: SsbhRenderer,
    render_models: Vec<ssbh_wgpu::RenderModel>,
    bounds: Option<(Vec3, f32)>,
    surface_format: wgpu::TextureFormat,
    current_w: u32,
    current_h: u32,
}

async fn init_ctx(model_dir: &Path, w: u32, h: u32) -> anyhow::Result<RenderCtx> {
    let instance = wgpu::Instance::default();
    let adapter = instance
        .request_adapter(&wgpu::RequestAdapterOptions {
            power_preference: wgpu::PowerPreference::HighPerformance,
            compatible_surface: None,
            force_fallback_adapter: false,
        })
        .await?;

    let (device, queue) = adapter
        .request_device(&wgpu::DeviceDescriptor {
            label: Some("ssbh_render device"),
            required_features: REQUIRED_FEATURES,
            required_limits: REQUIRED_LIMITS,
            memory_hints: wgpu::MemoryHints::default(),
            trace: wgpu::Trace::Off,
            experimental_features: wgpu::ExperimentalFeatures::disabled(),
        })
        .await?;

    let shared_data = SharedRenderData::new(&device, &queue);
    let surface_format = wgpu::TextureFormat::Bgra8UnormSrgb;
    let renderer = SsbhRenderer::new(
        &device,
        &queue,
        w,
        h,
        1.0,
        [0.05, 0.05, 0.07, 1.0],
        surface_format,
    );

    let bounds = compute_bounds(model_dir);
    let folder = ModelFolder::load_folder(model_dir);
    let render_models = load_render_models(&device, &queue, &[folder], &shared_data);

    Ok(RenderCtx {
        device,
        queue,
        shared_data,
        renderer,
        render_models,
        bounds,
        surface_format,
        current_w: w,
        current_h: h,
    })
}

/// Render one frame at the given camera angle and resolution. Reuses
/// the loaded model + GPU state, so each call is fast (no init, no
/// model reload) — costs essentially just the render + readback.
fn render_one(
    ctx: &mut RenderCtx,
    width: u32,
    height: u32,
    yaw: f32,
    pitch: f32,
    pan_x: f32,
    pan_y: f32,
    zoom: f32,
    out_path: &Path,
) -> anyhow::Result<()> {
    // Only resize when the requested dimensions actually changed —
    // resize() rebuilds internal buffers and was visibly degrading
    // output when called every frame at the same size.
    if width != ctx.current_w || height != ctx.current_h {
        ctx.renderer.resize(&ctx.device, width, height, 1.0);
        ctx.current_w = width;
        ctx.current_h = height;
    }

    let camera = build_camera(width, height, ctx.bounds,
                              yaw, pitch, pan_x, pan_y, zoom);
    ctx.renderer.update_camera(&ctx.queue, camera);

    let render_texture = ctx.device.create_texture(&wgpu::TextureDescriptor {
        label: Some("ssbh_render output"),
        size: wgpu::Extent3d {
            width,
            height,
            depth_or_array_layers: 1,
        },
        mip_level_count: 1,
        sample_count: 1,
        dimension: wgpu::TextureDimension::D2,
        format: ctx.surface_format,
        usage: wgpu::TextureUsages::COPY_SRC | wgpu::TextureUsages::RENDER_ATTACHMENT,
        view_formats: &[],
    });
    let render_view = render_texture.create_view(&wgpu::TextureViewDescriptor::default());

    let unpadded_row_bytes = width * 4;
    let row_align = wgpu::COPY_BYTES_PER_ROW_ALIGNMENT;
    let padded_row_bytes = (unpadded_row_bytes + row_align - 1) / row_align * row_align;
    let buffer_size = (padded_row_bytes * height) as wgpu::BufferAddress;

    let readback = ctx.device.create_buffer(&wgpu::BufferDescriptor {
        label: Some("ssbh_render readback"),
        size: buffer_size,
        usage: wgpu::BufferUsages::COPY_DST | wgpu::BufferUsages::MAP_READ,
        mapped_at_creation: false,
    });

    let mut encoder = ctx.device.create_command_encoder(&wgpu::CommandEncoderDescriptor {
        label: Some("ssbh_render encoder"),
    });

    ctx.renderer.render_models(
        &mut encoder,
        &render_view,
        &ctx.render_models,
        ctx.shared_data.database(),
        &ModelRenderOptions::default(),
    );

    encoder.copy_texture_to_buffer(
        wgpu::TexelCopyTextureInfo {
            texture: &render_texture,
            mip_level: 0,
            origin: wgpu::Origin3d::ZERO,
            aspect: wgpu::TextureAspect::All,
        },
        wgpu::TexelCopyBufferInfo {
            buffer: &readback,
            layout: wgpu::TexelCopyBufferLayout {
                offset: 0,
                bytes_per_row: Some(padded_row_bytes),
                rows_per_image: Some(height),
            },
        },
        wgpu::Extent3d {
            width,
            height,
            depth_or_array_layers: 1,
        },
    );

    ctx.queue.submit(Some(encoder.finish()));

    let buffer_slice = readback.slice(..);
    let (tx, rx) = std::sync::mpsc::channel();
    buffer_slice.map_async(wgpu::MapMode::Read, move |r| {
        let _ = tx.send(r);
    });
    loop {
        let _ = ctx.device.poll(wgpu::PollType::Poll);
        match rx.try_recv() {
            Ok(result) => {
                result?;
                break;
            }
            Err(std::sync::mpsc::TryRecvError::Empty) => {
                std::thread::sleep(std::time::Duration::from_millis(1));
            }
            Err(e) => return Err(anyhow::anyhow!("recv: {e}")),
        }
    }

    let mapped = buffer_slice.get_mapped_range();
    let mut rgba = Vec::with_capacity((unpadded_row_bytes * height) as usize);
    for row in 0..height {
        let start = (row * padded_row_bytes) as usize;
        let end = start + unpadded_row_bytes as usize;
        let row_bgra = &mapped[start..end];
        for px in row_bgra.chunks_exact(4) {
            rgba.push(px[2]);
            rgba.push(px[1]);
            rgba.push(px[0]);
            rgba.push(px[3]);
        }
    }
    drop(mapped);
    readback.unmap();

    let img = image::RgbaImage::from_raw(width, height, rgba)
        .ok_or_else(|| anyhow::anyhow!("RgbaImage::from_raw size mismatch"))?;
    img.save(out_path)?;
    Ok(())
}

async fn run(args: Args) -> anyhow::Result<()> {
    let mut ctx = init_ctx(&args.model_dir, args.width, args.height).await?;

    if args.server {
        // Server mode: read commands from stdin, render each, report
        // status on stdout. Format per line:
        //   <yaw> <pitch> <width> <height> <output_path>
        // EOF or "quit" exits.
        use std::io::{BufRead, Write};
        let stdin = std::io::stdin();
        let stdout = std::io::stdout();
        let mut out = stdout.lock();
        // Signal readiness so the parent can begin sending commands.
        writeln!(out, "READY")?;
        out.flush()?;

        for line in stdin.lock().lines() {
            let line = line?;
            let trimmed = line.trim();
            if trimmed.is_empty() {
                continue;
            }
            if trimmed == "quit" {
                break;
            }
            // Format: <yaw> <pitch> <pan_x> <pan_y> <zoom> <w> <h> <out>
            // Output path may contain spaces — split into 7 numeric
            // tokens then take the rest as the path.
            let mut parts = trimmed.splitn(8, ' ');
            let parse_f32 = |o: Option<&str>, name: &str| -> Result<f32, String> {
                o.ok_or_else(|| format!("missing {name}"))
                    .and_then(|s| s.parse().map_err(|e| format!("bad {name}: {e}")))
            };
            let parse_u32 = |o: Option<&str>, name: &str| -> Result<u32, String> {
                o.ok_or_else(|| format!("missing {name}"))
                    .and_then(|s| s.parse().map_err(|e| format!("bad {name}: {e}")))
            };
            let yaw = match parse_f32(parts.next(), "yaw") { Ok(v) => v, Err(e) => { writeln!(out, "ERR {e}")?; out.flush()?; continue; } };
            let pitch = match parse_f32(parts.next(), "pitch") { Ok(v) => v, Err(e) => { writeln!(out, "ERR {e}")?; out.flush()?; continue; } };
            let px = match parse_f32(parts.next(), "pan_x") { Ok(v) => v, Err(e) => { writeln!(out, "ERR {e}")?; out.flush()?; continue; } };
            let py = match parse_f32(parts.next(), "pan_y") { Ok(v) => v, Err(e) => { writeln!(out, "ERR {e}")?; out.flush()?; continue; } };
            let zm = match parse_f32(parts.next(), "zoom") { Ok(v) => v, Err(e) => { writeln!(out, "ERR {e}")?; out.flush()?; continue; } };
            let w = match parse_u32(parts.next(), "width") { Ok(v) => v, Err(e) => { writeln!(out, "ERR {e}")?; out.flush()?; continue; } };
            let h = match parse_u32(parts.next(), "height") { Ok(v) => v, Err(e) => { writeln!(out, "ERR {e}")?; out.flush()?; continue; } };
            let out_path = match parts.next() {
                Some(p) => PathBuf::from(p),
                None => { writeln!(out, "ERR missing output path")?; out.flush()?; continue; }
            };
            match render_one(&mut ctx, w, h, yaw, pitch, px, py, zm, &out_path) {
                Ok(_) => writeln!(out, "OK")?,
                Err(e) => writeln!(out, "ERR {e}")?,
            }
            out.flush()?;
        }
        Ok(())
    } else {
        let out = args
            .output
            .ok_or_else(|| anyhow::anyhow!("Output path is required in non-server mode"))?;
        render_one(
            &mut ctx,
            args.width,
            args.height,
            args.yaw,
            args.pitch,
            args.pan_x,
            args.pan_y,
            args.zoom,
            &out,
        )
    }
}

/// Build a 3/4 fighter-style view that frames the model. Matches the
/// camera setup in ssbh_wgpu_test (fov 0.5 rad ≈ 28.6°, far 400_000)
/// but with auto-fit distance derived from the model's bounding box
/// so tall characters (Bowser, Ganondorf) aren't cropped and small
/// characters (Kirby, Pichu) aren't lost in the void.
fn build_camera(
    width: u32,
    height: u32,
    bounds: Option<(Vec3, f32)>,
    yaw_deg: f32,
    pitch_deg: f32,
    pan_x: f32,
    pan_y: f32,
    zoom: f32,
) -> CameraTransforms {
    let aspect = width as f32 / height as f32;
    let fov_y = 0.5f32; // radians ≈ 28.6°

    // Default fallback (typical SSBU fighter dimensions) if mesh
    // bounds couldn't be computed.
    let (center, extent) = bounds.unwrap_or((Vec3::new(0.0, 8.0, 0.0), 12.0));

    // Distance to fit the longest axis in view, with a 30% margin.
    // Use the smaller of fov_y / fov_x to handle wide aspect ratios.
    let fov_x = 2.0 * (((fov_y * 0.5).tan()) * aspect).atan();
    let limiting_fov = fov_y.min(fov_x);
    let fit_distance = (extent * 1.3) / (limiting_fov * 0.5).tan();
    // Zoom multiplies the camera distance: zoom < 1 → closer, zoom > 1 → farther.
    let zoom_clamped = zoom.max(0.1).min(20.0);
    let distance = fit_distance * zoom_clamped;

    // Caller-controllable yaw/pitch around the model centroid so
    // the smash_night Tk viewer can re-render at arbitrary angles.
    let rotation = Mat4::from_rotation_x(pitch_deg.to_radians())
        * Mat4::from_rotation_y(yaw_deg.to_radians());
    // Pan shifts the camera target on screen. We translate in view
    // space (after rotation) so panning feels natural regardless of
    // how the model is rotated.
    let view_offset = Vec3::new(pan_x, -center.y + pan_y, -distance);
    let model_view_matrix = Mat4::from_translation(view_offset) * rotation;

    let near = (distance - extent * 4.0).max(0.1);
    let far = distance + extent * 8.0;
    let projection_matrix = Mat4::perspective_rh(fov_y, aspect, near, far);

    let mvp = projection_matrix * model_view_matrix;
    let camera_pos = model_view_matrix.inverse() * Vec4::new(0.0, 0.0, 0.0, 1.0);

    CameraTransforms {
        model_view_matrix,
        projection_matrix,
        mvp_matrix: mvp,
        mvp_inv_matrix: mvp.inverse(),
        camera_pos,
        screen_dimensions: Vec4::new(width as f32, height as f32, 1.0, 0.0),
    }
}
