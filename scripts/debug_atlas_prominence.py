"""Compare region prominence across D99, NMT, MEBRAINS at each view level.

Captures screenshots and extracts the color + geometric size of the active
mesh on each atlas so we can diagnose why D99/NMT might look less prominent
than MEBRAINS.
"""
import asyncio, contextlib, json, socket, subprocess, time
from pathlib import Path
from playwright.async_api import async_playwright

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "debug_output" / "prominence"

# For each atlas: region-level, dandiset-level, session-level URLs
# Pick the first session from 001636 by asset_id
CASES = [
    ("d99_region",     "http://localhost:8000/?atlas=d99&debug=1#region=359"),
    ("d99_dandiset",   "http://localhost:8000/?atlas=d99&debug=1#dandiset=001636"),
    ("nmt_region",     "http://localhost:8000/?atlas=nmt&debug=1#region=79"),
    ("nmt_dandiset",   "http://localhost:8000/?atlas=nmt&debug=1#dandiset=001636"),
    ("mebrains_region", "http://localhost:8000/?atlas=mebrains&debug=1#region=303"),
    ("mebrains_dandiset", "http://localhost:8000/?atlas=mebrains&debug=1#dandiset=001636"),
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
    results = {}
    async with async_playwright() as p:
        b = await p.chromium.launch(headless=True)
        ctx = await b.new_context(viewport={"width": 1600, "height": 1000})
        page = await ctx.new_page()
        for name, url in CASES:
            print(f"\n=== {name} ===")
            await page.goto(url, wait_until="networkidle")
            await page.wait_for_function(
                "() => window.__debug && Object.keys(window.__debug.meshObjects).length > 3",
                timeout=60000,
            )
            await page.wait_for_timeout(2500)

            state = await page.evaluate("""() => {
              const d = window.__debug;
              const rootId = d.meshManifest.root_id;
              const visible = [];
              for (const [idStr, mesh] of Object.entries(d.meshObjects)) {
                if (!mesh.isMesh || !mesh.visible) continue;
                const mat = mesh.material;
                const hex = mat.color ? '#' + mat.color.getHexString() : null;
                const id = parseInt(idStr);
                const isRoot = id === rootId;
                const box = new d.THREE.Box3().setFromObject(mesh);
                const size = box.getSize(new d.THREE.Vector3()).toArray();
                visible.push({
                  id: id,
                  isRoot: isRoot,
                  color: hex,
                  opacity: mat.opacity,
                  transparent: mat.transparent,
                  size: size,
                  volume_bbox: size[0]*size[1]*size[2],
                });
              }
              return visible;
            }""");
            results[name] = state
            for item in state:
                tag = "ROOT" if item["isRoot"] else f"region {item['id']}"
                print(f"  {tag}: color={item['color']} opacity={item['opacity']}  size={[round(s,1) for s in item['size']]}  bbox_vol={item['volume_bbox']:,.0f}")

            for view in ("right", "dorsal"):
                await page.evaluate(f'''document.querySelector('button[data-view="{view}"]').click()''')
                await page.wait_for_timeout(700)
                await page.screenshot(path=str(OUT / f"{name}_{view}.png"), full_page=False)
        await b.close()
    (OUT / "state.json").write_text(json.dumps(results, indent=2))
    print(f"\nWrote state + screenshots to {OUT}")

if __name__ == "__main__":
    with ensure_server(8000):
        asyncio.run(run())
