"""Playwright-driven render diagnostic for the dandi-atlas viewer.

Launches headless Chromium against a locally-served build of the app,
inspects the Three.js scene via the `window.__debug` hook, and saves
screenshots at the six canonical orientation buttons.

Run with:
    uv run --with playwright python scripts/debug_render.py

Assumes `python3 -m http.server 8000` is running in the repo root, or
will start one transiently if not.
"""

import asyncio
import contextlib
import json
import socket
import subprocess
import time
from pathlib import Path

from playwright.async_api import async_playwright

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "debug_output"
URL = "http://localhost:8000/?atlas=nmt&debug=1#region=79"
VIEWPORT = {"width": 1600, "height": 1000}
VIEWS = ["dorsal", "ventral", "anterior", "posterior", "left", "right"]
PROBE_IDS = [9999, 1, 77, 78, 79, 80, 87, 92]


def port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.2)
        return s.connect_ex(("127.0.0.1", port)) == 0


@contextlib.contextmanager
def ensure_server(port: int = 8000):
    if port_open(port):
        yield None
        return
    proc = subprocess.Popen(
        ["python3", "-m", "http.server", str(port)],
        cwd=REPO,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(50):
        if port_open(port):
            break
        time.sleep(0.1)
    try:
        yield proc
    finally:
        proc.terminate()
        proc.wait(timeout=5)


async def run():
    OUT.mkdir(exist_ok=True)
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(viewport=VIEWPORT)
        page = await ctx.new_page()
        page.on("console", lambda msg: print(f"  [console.{msg.type}] {msg.text}"))
        page.on("pageerror", lambda err: print(f"  [pageerror] {err}"))

        print(f"Navigating to {URL}")
        await page.goto(URL, wait_until="networkidle")

        # Wait for the debug hook to exist and for meshes to load
        await page.wait_for_function(
            "() => window.__debug && Object.keys(window.__debug.meshObjects).length > 10",
            timeout=60000,
        )
        # Small settle delay for isolation/active styling to apply
        await page.wait_for_timeout(1500)

        # Query scene state for the probe meshes
        state = await page.evaluate(
            f"""(ids) => {{
                const d = window.__debug;
                const out = {{
                    camera: d.cameraState(),
                    root_id: d.meshManifest.root_id,
                    mesh_count: Object.keys(d.meshObjects).length,
                    meshes: {{}},
                }};
                for (const id of ids) out.meshes[id] = d.meshBounds(id);
                return out;
            }}""",
            PROBE_IDS,
        )
        (OUT / "scene_state.json").write_text(json.dumps(state, indent=2))
        print("Wrote scene_state.json")

        # Screenshot each orientation
        for view in VIEWS:
            sel = f'button[data-view="{view}"]'
            await page.click(sel)
            await page.wait_for_timeout(600)
            await page.screenshot(path=str(OUT / f"nmt_region79_{view}.png"), full_page=False)
            print(f"  saved nmt_region79_{view}.png")

        # Also grab an SI (92) snapshot at dorsal, for side-by-side comparison
        await page.goto("http://localhost:8000/?atlas=nmt&debug=1#region=92", wait_until="networkidle")
        await page.wait_for_function(
            "() => window.__debug && Object.keys(window.__debug.meshObjects).length > 10",
            timeout=60000,
        )
        await page.wait_for_timeout(1500)
        await page.click('button[data-view="dorsal"]')
        await page.wait_for_timeout(600)
        await page.screenshot(path=str(OUT / "nmt_region92_dorsal.png"), full_page=False)
        print("  saved nmt_region92_dorsal.png")

        await browser.close()


if __name__ == "__main__":
    with ensure_server(8000):
        asyncio.run(run())
