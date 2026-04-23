"""Side-by-side snapshot of Allen CCF (mouse) vs macaque atlases for visual comparison."""
import asyncio, contextlib, json, socket, subprocess, time
from pathlib import Path
from playwright.async_api import async_playwright

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "debug_output" / "compare_atlases"
CASES = [
    ("allen_ccf_overview",   "http://localhost:8000/?atlas=allen_ccf&debug=1"),
    ("d99_overview",          "http://localhost:8000/?atlas=d99&debug=1"),
    ("nmt_overview",          "http://localhost:8000/?atlas=nmt&debug=1"),
    ("mebrains_overview",     "http://localhost:8000/?atlas=mebrains&debug=1"),
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
            print(f"\n=== {name} ===")
            await page.goto(url, wait_until="networkidle")
            await page.wait_for_function(
                "() => window.__debug && Object.keys(window.__debug.meshObjects).length > 10",
                timeout=60000,
            )
            await page.wait_for_timeout(2000)

            probe = await page.evaluate(
                """() => {
                    const d = window.__debug;
                    const THREE = d.THREE;
                    const stats = { face_counts: [], vertex_counts: [], mesh_count: 0, data_mesh_count: 0 };
                    for (const m of Object.values(d.meshObjects)) {
                        if (!m.isMesh) continue;
                        stats.mesh_count++;
                        if (m.userData.isData) stats.data_mesh_count++;
                        const geom = m.geometry;
                        const vc = geom.attributes.position.count;
                        const fc = geom.index ? geom.index.count / 3 : vc / 3;
                        stats.face_counts.push(fc);
                        stats.vertex_counts.push(vc);
                    }
                    stats.total_faces = stats.face_counts.reduce((a,b)=>a+b, 0);
                    stats.total_vertices = stats.vertex_counts.reduce((a,b)=>a+b, 0);
                    stats.mean_faces_per_mesh = stats.total_faces / Math.max(1, stats.mesh_count);
                    const root = d.meshObjects[d.meshManifest.root_id];
                    const rootMat = root && root.material;
                    stats.root = root ? {
                        face_count: root.geometry.index ? root.geometry.index.count/3 : root.geometry.attributes.position.count/3,
                        vertex_count: root.geometry.attributes.position.count,
                        opacity: rootMat ? rootMat.opacity : null,
                        material_type: rootMat ? rootMat.type : null,
                        wireframe: rootMat ? (rootMat.wireframe || false) : null,
                        transparent: rootMat ? rootMat.transparent : null,
                        side: rootMat ? rootMat.side : null,
                    } : null;
                    // Sample one non-root, non-active mesh
                    for (const m of Object.values(d.meshObjects)) {
                        if (m.userData && !m.userData.isRoot && m.material) {
                            stats.sample_non_root = {
                                id: m.userData.structureId,
                                opacity: m.material.opacity,
                                material_type: m.material.type,
                                wireframe: m.material.wireframe || false,
                                isData: !!m.userData.isData,
                            };
                            break;
                        }
                    }
                    stats.atlas = d.activeAtlas;
                    return stats;
                }"""
            )
            (OUT / f"{name}.json").write_text(json.dumps(probe, indent=2, default=str))
            print(f"  meshes: {probe['mesh_count']} total ({probe['data_mesh_count']} data), {probe['total_faces']:.0f} total faces, mean {probe['mean_faces_per_mesh']:.0f}/mesh")
            print(f"  root: {probe['root']}")
            print(f"  sample non-root: {probe.get('sample_non_root')}")

            for view in ("right", "dorsal"):
                await page.click(f'button[data-view="{view}"]')
                await page.wait_for_timeout(800)
                await page.screenshot(path=str(OUT / f"{name}_{view}.png"), full_page=False)
                print(f"  saved {name}_{view}.png")
        await b.close()

if __name__ == "__main__":
    with ensure_server(8000):
        asyncio.run(run())
