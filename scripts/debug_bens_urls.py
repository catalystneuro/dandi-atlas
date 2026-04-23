"""Reproduce Ben's PR feedback URLs and snapshot current state."""
import asyncio, contextlib, json, socket, subprocess, time
from pathlib import Path
from playwright.async_api import async_playwright

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "debug_output" / "bens_urls"
CASES = [
    ("d99_region449", "http://localhost:8000/?atlas=d99&debug=1#region=449", 449),
    ("nmt_region77",  "http://localhost:8000/?atlas=nmt&debug=1#region=77",  77),
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
        for name, url, region_id in CASES:
            print(f"\n=== {name}: {url} ===")
            await page.goto(url, wait_until="networkidle")
            await page.wait_for_function(
                "() => window.__debug && Object.keys(window.__debug.meshObjects).length > 10",
                timeout=60000,
            )
            await page.wait_for_timeout(1500)
            probe = await page.evaluate(
                f"""(id) => {{
                    const d = window.__debug;
                    const mb = d.meshBounds(id);
                    const rm = d.meshBounds(d.meshManifest.root_id);
                    return {{
                        region: mb, root: rm,
                        camera: d.cameraState(),
                        visible_count: Object.values(d.meshObjects).filter(m=>m.visible).length,
                    }};
                }}""",
                region_id,
            )
            (OUT / f"{name}.json").write_text(json.dumps(probe, indent=2))
            for view in ("right", "dorsal", "left"):
                await page.click(f'button[data-view="{view}"]')
                await page.wait_for_timeout(600)
                await page.screenshot(path=str(OUT / f"{name}_{view}.png"), full_page=False)
                print(f"  saved {name}_{view}.png")
        await b.close()

if __name__ == "__main__":
    with ensure_server(8000):
        asyncio.run(run())
