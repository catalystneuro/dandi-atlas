"""Capture region/dandiset/session views on all three macaque atlases with
the current palettes and policy, so we can diagnose why selection doesn't pop.
"""
import asyncio, contextlib, json, socket, subprocess, time
from pathlib import Path
from playwright.async_api import async_playwright

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "debug_output" / "prominence_current"

CASES = [
    ("d99_region",      "http://localhost:8000/?atlas=d99&debug=1#region=359"),
    ("d99_dandiset",    "http://localhost:8000/?atlas=d99&debug=1#dandiset=001636"),
    ("nmt_region",      "http://localhost:8000/?atlas=nmt&debug=1#region=79"),
    ("nmt_dandiset",    "http://localhost:8000/?atlas=nmt&debug=1#dandiset=001636"),
    ("mebrains_region", "http://localhost:8000/?atlas=mebrains&debug=1#region=303"),
    ("mebrains_dandiset","http://localhost:8000/?atlas=mebrains&debug=1#dandiset=001636"),
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
              const out = [];
              for (const [idStr, mesh] of Object.entries(d.meshObjects)) {
                if (!mesh.isMesh || !mesh.visible) continue;
                const mat = mesh.material;
                const hex = mat.color ? '#' + mat.color.getHexString().toUpperCase() : null;
                const id = parseInt(idStr);
                out.push({
                  id, isRoot: id === rootId,
                  color: hex,
                  opacity: mat.opacity,
                  transparent: mat.transparent,
                  depthTest: mat.depthTest,
                  depthWrite: mat.depthWrite,
                  renderOrder: mesh.renderOrder,
                  specular: mat.specular ? '#' + mat.specular.getHexString() : null,
                  emissive: mat.emissive ? '#' + mat.emissive.getHexString() : null,
                  shininess: mat.shininess,
                });
              }
              return out;
            }""")
            results[name] = state
            for item in state:
                tag = "ROOT" if item["isRoot"] else f"reg {item['id']}"
                print(f"  {tag}: color={item['color']} opacity={item['opacity']} "
                      f"emissive={item['emissive']} specular={item['specular']} "
                      f"shininess={item['shininess']} depthTest={item['depthTest']} "
                      f"depthWrite={item['depthWrite']} renderOrder={item['renderOrder']}")

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
