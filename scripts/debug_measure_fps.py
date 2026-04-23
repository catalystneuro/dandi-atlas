"""Measure rotation FPS on each atlas via headless Playwright.

Records frame cadence while programmatically rotating the camera around the
scene, giving a concrete performance number to compare before/after rendering
policy changes.

Run via:
    uv run --with playwright python scripts/debug_measure_fps.py [label]

`label` (optional) is appended to the output JSON filename so we can compare
multiple runs (e.g. "pre_policy", "post_policy").
"""
import asyncio, contextlib, json, socket, subprocess, sys, time
from pathlib import Path
from playwright.async_api import async_playwright

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "debug_output" / "fps"

LABEL = sys.argv[1] if len(sys.argv) > 1 else "measurement"

SCENES = [
    ("allen_ccf_init",   "http://localhost:8000/?atlas=allen_ccf&debug=1"),
    ("d99_init",         "http://localhost:8000/?atlas=d99&debug=1"),
    ("nmt_init",         "http://localhost:8000/?atlas=nmt&debug=1"),
    ("mebrains_init",    "http://localhost:8000/?atlas=mebrains&debug=1"),
    ("d99_region",       "http://localhost:8000/?atlas=d99&debug=1#region=359"),
    ("nmt_region",       "http://localhost:8000/?atlas=nmt&debug=1#region=79"),
]

DURATION_MS = 3000  # measurement window per scene
VIEWPORT = {"width": 1600, "height": 1000}


def port_open(p):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.2); return s.connect_ex(("127.0.0.1", p)) == 0

@contextlib.contextmanager
def ensure_server(port=8000):
    if port_open(port):
        yield None
        return
    proc = subprocess.Popen(
        ["python3","-m","http.server",str(port)], cwd=REPO,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    for _ in range(50):
        if port_open(port): break
        time.sleep(0.1)
    try: yield proc
    finally: proc.terminate(); proc.wait(timeout=5)


async def measure_one(page, name, url):
    print(f"\n=== {name}: {url} ===", flush=True)
    nav_start = time.perf_counter()
    await page.goto(url, wait_until="networkidle")
    scene_ready_start = time.perf_counter()
    await page.wait_for_function(
        "() => window.__debug && Object.keys(window.__debug.meshObjects).length > 3",
        timeout=60000,
    )
    scene_ready_at = time.perf_counter()
    load_total_ms = (scene_ready_at - nav_start) * 1000
    load_after_nav_ms = (scene_ready_at - scene_ready_start) * 1000
    print(f"  load: {load_total_ms:.0f}ms total (networkidle→scene-ready: {load_after_nav_ms:.0f}ms)")
    await page.wait_for_timeout(1500)

    result = await page.evaluate(
        f"""async () => {{
            const d = window.__debug;
            const camera = d.camera;
            const controls = d.controls;
            const target = controls.target.clone();
            const dist = camera.position.distanceTo(target);
            const up = camera.up.clone().normalize();

            // Build an orbital axis perpendicular to camera.up
            const THREE = d.THREE;
            const startOffset = camera.position.clone().sub(target);

            const frames = [];
            let lastTime = performance.now();
            const startTime = lastTime;

            return new Promise(resolve => {{
                function tick() {{
                    const now = performance.now();
                    frames.push(now - lastTime);
                    lastTime = now;

                    const elapsed = now - startTime;
                    // Rotate the camera around the target's up axis
                    const angle = (elapsed / 1000) * 1.0;  // 1 rad/sec
                    const cos = Math.cos(angle), sin = Math.sin(angle);

                    // Rotation in the plane perpendicular to up
                    // Works for both Z-up (macaque) and Y-up-ish (allen)
                    let newOffset;
                    if (Math.abs(up.z) > 0.5) {{
                        // Z-up: rotate in xy plane
                        newOffset = new THREE.Vector3(
                            startOffset.x * cos - startOffset.y * sin,
                            startOffset.x * sin + startOffset.y * cos,
                            startOffset.z
                        );
                    }} else {{
                        // Y-up (or -Y up): rotate in xz plane
                        newOffset = new THREE.Vector3(
                            startOffset.x * cos - startOffset.z * sin,
                            startOffset.y,
                            startOffset.x * sin + startOffset.z * cos
                        );
                    }}
                    camera.position.copy(target).add(newOffset);
                    camera.lookAt(target);
                    controls.update();

                    if (elapsed < {DURATION_MS}) {{
                        requestAnimationFrame(tick);
                    }} else {{
                        // Strip the first frame time (includes navigation jitter)
                        const clean = frames.slice(1);
                        clean.sort((a, b) => a - b);
                        const mean = clean.reduce((a,b)=>a+b, 0) / clean.length;
                        const median = clean[Math.floor(clean.length/2)];
                        const p95 = clean[Math.floor(clean.length*0.95)];
                        const p5  = clean[Math.floor(clean.length*0.05)];
                        const total = elapsed;
                        resolve({{
                            frame_count: clean.length,
                            duration_ms: total,
                            fps: clean.length / (total / 1000),
                            frame_time_ms: {{
                                mean: mean,
                                median: median,
                                p5: p5,
                                p95: p95,
                                min: clean[0],
                                max: clean[clean.length-1],
                            }},
                        }});
                    }}
                }}
                requestAnimationFrame(tick);
            }});
        }}"""
    )

    # Also record scene breakdown for context
    ctx = await page.evaluate(
        """() => {
            const d = window.__debug;
            const meshes = Object.values(d.meshObjects).filter(m => m.isMesh);
            const visible = meshes.filter(m => m.visible);
            let total_tri = 0, visible_tri = 0;
            for (const m of meshes) {
                const geom = m.geometry;
                const fc = geom.index ? geom.index.count/3 : geom.attributes.position.count/3;
                total_tri += fc;
                if (m.visible) visible_tri += fc;
            }
            return {
                total_meshes: meshes.length,
                visible_meshes: visible.length,
                total_triangles: total_tri,
                visible_triangles: visible_tri,
            };
        }"""
    )

    result["scene"] = ctx
    result["load_time_ms"] = load_total_ms
    print(f"  meshes: {ctx['total_meshes']} total, {ctx['visible_meshes']} visible")
    print(f"  triangles: {ctx['visible_triangles']:,} visible / {ctx['total_triangles']:,} total")
    print(f"  FPS: {result['fps']:.1f}   frame time p50={result['frame_time_ms']['median']:.1f}ms  p95={result['frame_time_ms']['p95']:.1f}ms")
    return result


async def run():
    OUT.mkdir(parents=True, exist_ok=True)
    all_results = {}
    async with async_playwright() as p:
        b = await p.chromium.launch(headless=True)
        ctx = await b.new_context(viewport=VIEWPORT)
        page = await ctx.new_page()
        for name, url in SCENES:
            all_results[name] = await measure_one(page, name, url)
        await b.close()

    outfile = OUT / f"fps_{LABEL}.json"
    outfile.write_text(json.dumps(all_results, indent=2))
    print(f"\nWrote {outfile}")

    # Print a summary table
    print("\n=== Summary ({label}) ===".format(label=LABEL))
    print(f"{'Scene':<18}  {'load ms':>8}  {'FPS':>6}  {'p50':>6}  {'p95':>6}  {'meshes':>7}  {'tris':>11}")
    for name, r in all_results.items():
        print(f"{name:<18}  {r.get('load_time_ms', 0):>8.0f}  {r['fps']:>6.1f}  "
              f"{r['frame_time_ms']['median']:>6.1f}  {r['frame_time_ms']['p95']:>6.1f}  "
              f"{r['scene']['total_meshes']:>7}  {r['scene']['total_triangles']:>11,}")


if __name__ == "__main__":
    with ensure_server(8000):
        asyncio.run(run())
