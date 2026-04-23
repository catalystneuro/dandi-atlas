"""Take screenshots of each view level with the visibility policy applied."""
import asyncio, contextlib, socket, subprocess, time
from pathlib import Path
from playwright.async_api import async_playwright

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "debug_output" / "policy_applied"

CASES = [
    ("allen_init",       "http://localhost:8000/?atlas=allen_ccf"),
    ("allen_region",     "http://localhost:8000/?atlas=allen_ccf#region=688"),
    ("d99_init",         "http://localhost:8000/?atlas=d99"),
    ("d99_region_F1",    "http://localhost:8000/?atlas=d99#region=359"),
    ("d99_dandiset",     "http://localhost:8000/?atlas=d99#dandiset=001636"),
    ("nmt_init",         "http://localhost:8000/?atlas=nmt"),
    ("nmt_region_M1",    "http://localhost:8000/?atlas=nmt#region=79"),
    ("nmt_dandiset",     "http://localhost:8000/?atlas=nmt#dandiset=001636"),
    ("mebrains_init",    "http://localhost:8000/?atlas=mebrains"),
    ("mebrains_dandiset","http://localhost:8000/?atlas=mebrains#dandiset=001636"),
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
        ctx = await b.new_context(viewport={"width": 1600, "height": 1000})
        page = await ctx.new_page()
        for name, url in CASES:
            await page.goto(url, wait_until="networkidle")
            await page.wait_for_function(
                "() => window.__debug !== undefined || (document.readyState === 'complete' && document.querySelector('canvas'))",
                timeout=60000,
            )
            await page.wait_for_timeout(2500)
            for view in ("right", "dorsal"):
                await page.evaluate(f'''document.querySelector('button[data-view="{view}"]').click()''')
                await page.wait_for_timeout(700)
                await page.screenshot(path=str(OUT / f"{name}_{view}.png"), full_page=False)
                print(f"  saved {name}_{view}.png")
        await b.close()

if __name__ == "__main__":
    with ensure_server(8000):
        asyncio.run(run())
