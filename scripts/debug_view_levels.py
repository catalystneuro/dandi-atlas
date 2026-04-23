"""Capture the three view levels (init / region selected / dandiset selected)
for Allen CCF and D99 so we can compare what's actually visible and measure
overdraw in each state.
"""
import asyncio, contextlib, json, socket, subprocess, time
from pathlib import Path
from playwright.async_api import async_playwright

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "debug_output" / "view_levels"

CASES = [
    ("allen_init",     "http://localhost:8000/?atlas=allen_ccf&debug=1"),
    ("allen_region",   "http://localhost:8000/?atlas=allen_ccf&debug=1#region=688"),   # CTX Cerebral cortex
    ("allen_dandiset", "http://localhost:8000/?atlas=allen_ccf&debug=1#dandiset=000021"),
    ("d99_init",       "http://localhost:8000/?atlas=d99&debug=1"),
    ("d99_region",     "http://localhost:8000/?atlas=d99&debug=1#region=359"),          # F1_(4) / M1
    ("d99_dandiset",   "http://localhost:8000/?atlas=d99&debug=1#dandiset=001636"),
]

def port_open(p):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.2); return s.connect_ex(("127.0.0.1", p)) == 0

@contextlib.contextmanager
def ensure_server(port=8000):
    if port_open(port): yield None; return
    proc = subprocess.Popen(["python3","-m","http.server",str(port)], cwd=REPO, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(50):
        if port_open(port): break
        time.sleep(0.1)
    try: yield proc
    finally: proc.terminate(); proc.wait(timeout=5)

async def run():
    OUT.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as p:
        b = await p.chromium.launch(headless=True)
        ctx = await b.new_context(viewport={"width":1600,"height":1000})
        page = await ctx.new_page()
        for name, url in CASES:
            print(f"\n=== {name}: {url} ===")
            await page.goto(url, wait_until="networkidle")
            await page.wait_for_function(
                "() => window.__debug && Object.keys(window.__debug.meshObjects).length > 5",
                timeout=60000,
            )
            await page.wait_for_timeout(2000)

            probe = await page.evaluate("""() => {
              const d = window.__debug;
              const meshes = Object.values(d.meshObjects).filter(m => m.isMesh);
              const visible = meshes.filter(m => m.visible);
              // Breakdown by transparency and opacity bucket
              let opaque_visible = 0, translucent_visible = 0, barely_visible = 0;
              let total_tris_visible = 0;
              let total_tris_all = 0;
              for (const m of meshes) {
                const geom = m.geometry;
                const fc = geom.index ? geom.index.count/3 : geom.attributes.position.count/3;
                total_tris_all += fc;
                if (m.visible) {
                  total_tris_visible += fc;
                  const op = m.material.opacity;
                  const isTrans = m.material.transparent && op < 1;
                  if (!isTrans) opaque_visible++;
                  else if (op > 0.15) translucent_visible++;
                  else barely_visible++;
                }
              }
              const root = d.meshObjects[d.meshManifest.root_id];
              return {
                total_mesh_count: meshes.length,
                visible_count: visible.length,
                opaque_visible, translucent_visible, barely_visible,
                total_tris_visible, total_tris_all,
                root_opacity: root ? root.material.opacity : null,
                root_transparent: root ? root.material.transparent : null,
                root_visible: root ? root.visible : null,
                root_depthWrite: root ? root.material.depthWrite : null,
              };
            }""");
            (OUT / f"{name}.json").write_text(json.dumps(probe, indent=2))
            print(f"  total meshes: {probe['total_mesh_count']}  visible: {probe['visible_count']}")
            print(f"    opaque: {probe['opaque_visible']}  translucent: {probe['translucent_visible']}  barely-visible: {probe['barely_visible']}")
            print(f"  root: visible={probe['root_visible']} opacity={probe['root_opacity']} transparent={probe['root_transparent']} depthWrite={probe['root_depthWrite']}")
            print(f"  triangles: visible={probe['total_tris_visible']:,}  total loaded={probe['total_tris_all']:,}")

            # Take right-lateral and dorsal screenshots for each
            for view in ("right", "dorsal"):
                await page.evaluate(f'''document.querySelector('button[data-view="{view}"]').click()''')
                await page.wait_for_timeout(700)
                await page.screenshot(path=str(OUT / f"{name}_{view}.png"), full_page=False)
                print(f"  saved {name}_{view}.png")
        await b.close()

if __name__ == "__main__":
    with ensure_server(8000):
        asyncio.run(run())
