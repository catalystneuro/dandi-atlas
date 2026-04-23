# Why Allen looks smooth and the macaque meshes don't

An investigation from first principles, grounded in actual measurements of the GLB files on disk. No earlier "what should we do" suggestions carried over — this is a fresh look.

## 1. The two meshes were produced by entirely different pipelines

**Allen (mouse)**

The pipeline is "download a finished mesh from someone else":

1. `scripts/build_data.py` calls the Allen Brain Atlas API and downloads one OBJ file per brain region (997 is the root).
2. `scripts/convert_meshes.py` opens each OBJ with `trimesh.load(..., process=False)` and writes it out as GLB.
3. Done.

Important: the Allen Institute produced those OBJs. Upstream, those meshes were hand-curated or at least heavily post-processed — smoothed, remeshed for uniform triangle quality, with vertex normals baked in. We never see the NIfTI. We just consume finished 3D models.

**Macaque (D99, NMT, MEBRAINS)**

The pipeline is "generate a mesh from a voxel grid":

1. `scripts/build_macaque_atlas.py` loads the parcellation NIfTI (an integer volume — each voxel has an atlas label).
2. For each label, it extracts the binary mask (`atlas_data == label_id`).
3. `skimage.measure.marching_cubes(mask, level=0.5)` walks the 0/1 boundary and emits triangles along the faces of the voxel grid.
4. Vertices are transformed from voxel space to world space with the NIfTI affine.
5. `trimesh.Trimesh(vertices=verts, faces=faces)` wraps it, then `simplify_quadric_decimation(face_count=TARGET)` reduces triangle count.
6. `mesh.export(path, file_type="glb")`.

Important: marching cubes walks a binary voxel grid. Its output is fundamentally **staircased** — the boundaries it finds are the boundaries of cubes. Adjacent triangles are nearly always either coplanar (inside a voxel face) or at a 45°/90° angle (at a voxel corner). Quadric decimation reduces triangle count but does not smooth the surface — it picks edges to collapse based on quadric error, which preserves the overall shape including the voxel-boundary kinks.

Nobody hand-smooths our macaque mesh. Nobody bakes normals. Whatever marching cubes produces goes straight to the user's browser.

## 2. Scale of each atlas

Before looking at per-mesh quality, it helps to have the totals in mind — they shape what "quality per mesh" is multiplied by when the scene is rendered.

| Atlas | Atlas areas (structure graph nodes) | Mesh files on disk | Regions with dandiset data | Total vertices | Total faces | Mean faces / mesh |
|---|---:|---:|---:|---:|---:|---:|
| **Allen CCF (mouse)** | 1,327 | 749 | **698** | 1,830,100 | 3,611,086 | 4,821 |
| **D99 (macaque)** | 397 | 382 | 3 | 1,194,193 | 2,397,970 | 6,277 |
| **NMT (macaque)** | 248 | 86 | 5 | 439,465 | 883,760 | 10,276 |
| **MEBRAINS (macaque)** | 198 | 196 | 3 | 342,755 | 689,125 | 3,516 |

A few things jump out:

- **Allen defines 1,327 brain areas** in its hierarchy but only 749 mesh files exist on disk — the viewer loads just the leaves and a few intermediate parents. D99 and MEBRAINS have a mesh for almost every area (382/397, 196/198) because those atlases are much flatter hierarchies. NMT has only 86 meshes for 248 areas because CHARM level-4 collapses many sub-areas.
- **Data coverage is the other big gap:** Allen has 698 regions with dandiset data (the entire hierarchy has at least one dandiset under it somewhere). All three macaque atlases have 3–5 regions with data, all under motor cortex.
- Allen's total vertex budget is 1.5–5× the macaque atlases, yet the scene rotates smoothly. That confirms the earlier point: face count is not the performance lever; overdraw and transparency are.

## 3. What's actually in each GLB file

Concrete measurements on the files currently on disk:

| Mesh | Verts | Faces | `NORMAL` attribute? | Mean dihedral | % edges sharp (>30°) |
|---|---:|---:|:---:|---:|---:|
| **Allen root (997.glb)** | 49,324 | 98,638 | **Yes** | **4.0°** | **0.5%** |
| Allen leaf (1000.glb) | 3,561 | 7,120 | Yes | 14.0° | 10.9% |
| **D99 root (9999.glb)** | 24,122 | 50,000 | **No** | **29.7°** | **33.5%** |
| NMT root (9999.glb) | 24,939 | 50,000 | No | 20.4° | 22.2% |
| D99 leaf F1_(4) (359.glb) | 5,001 | 10,000 | No | 29.8° | 47.9% |

Two huge differences jump out.

### 3a. Allen stores vertex normals. Macaque doesn't.

A glTF mesh may or may not carry a `NORMAL` attribute per vertex. Allen's do. Ours don't. When `NORMAL` is absent, Three.js's `GLTFLoader` falls back to `Geometry.computeVertexNormals()`, which *averages* face normals at each shared vertex. That would produce smooth shading *if the underlying geometry were smooth*. Which brings us to:

### 3b. The macaque surface is intrinsically jagged.

The "mean dihedral angle" column measures how aligned adjacent face normals are. On the Allen root, the average angle between neighboring faces is 4°; only 0.5% of edges are sharp (>30°). The surface is essentially continuous — every triangle nearly matches its neighbors' orientation.

On the D99 root, the average is 29.7° and a full third of edges are sharp. The surface is a collection of voxel-boundary facets joined at large angles. Even after 50,000-face smoothing, the underlying staircase geometry survives as a low-frequency crumpled-paper look.

**Both of these are fixable on our side.** Neither requires changing the Allen atlas or the browser-side rendering.

## 4. Why this turns into a bad-looking render

Phong shading (which we use — `MeshPhongMaterial`) computes at each pixel:

```
color = ambient + diffuse · (N · L) + specular · (R · V)^shininess
```

`N` is the surface normal interpolated from the three vertex normals of the triangle covering that pixel. The perceived quality of the shading depends almost entirely on how smoothly `N` varies across the surface.

Two ways the macaque render loses here:

1. **No `NORMAL` attribute → Three.js averages face normals per vertex.** Fine in isolation. But at edges where adjacent faces differ by 30°+, the averaged vertex normal is the mean of two orientations, producing a "folded-paper" look: each near-planar facet shades flat, and at their seams the shading kinks.
2. **The mesh itself is kinky.** Even if we wrote perfect normals, the underlying geometry is the shape of voxel boundaries. Smooth normals on a non-smooth surface produce the "wet plastic over a rock pile" aesthetic where you can see the discretization through the shading.

Allen shipped us a surface where the geometry is smooth *and* the normals are baked, so Phong just works.

## 5. Performance considerations (the middle third)

Before talking about fixes, it matters to know where the frame cost actually goes, because some of the plausible quality wins are expensive and some are free.

**Per-frame GPU cost is dominated by the fragment shader.** Each triangle rasterises N pixels; each pixel runs the fragment shader once per overlapping surface (overdraw). `MeshPhongMaterial` costs ~50 ALU ops per fragment invocation — normal interpolation, dot products, specular, alpha blend. Rasterisation itself is cheap; **the fragment shader is what the GPU budget is spent on**.

Right now the macaque render has three separate amplifiers working against us:

- **Overdraw from transparency.** Root, parent, and leaf meshes all overlap in world space. All are `transparent: true, depthWrite: false`. A pixel in the middle of the brain typically has 8–12 overlapping transparent surfaces, each running Phong for that pixel. Allen's opaque root (opacity 1.0) kills this — it goes through the opaque queue with early-z and an overdraw of ~1.
- **DoubleSide on transparent meshes.** Every triangle is rasterised twice (front and back). Doubles fragment cost.
- **Missing normals forces Three.js to compute normals at load.** One-time cost, not per-frame, so it doesn't affect rotation smoothness. Noting it for completeness.

Implications for quality choices:

- Anything that reduces *overdraw* gives performance headroom we can spend on quality elsewhere.
- Anything that changes *per-fragment cost* (e.g. material swap) multiplies by overdraw, so it's worth more than a face-count reduction of the same magnitude.
- Face count affects rasterisation (cheap) and vertex shader (cheap) more than fragment (expensive). **Face count is the wrong knob for perf tuning** on this app. It was the wrong knob even before the 150k→50k walkback.

So the frame budget is mostly a function of "how many Phong invocations happen per output pixel." Quality fixes that *remove* surfaces from overlap (hide context meshes, make root opaque) buy budget. Quality fixes that *improve* each mesh (smooth geometry, bake normals) cost nothing per-frame — they're build-time work.

## 6. How to make the macaque meshes actually look good

Quality interventions, ranked by quality win per unit of effort, with their performance implications called out.

### 6a. Smooth the surface at build time (biggest visual win, zero runtime cost)

Between marching cubes and simplify_quadric_decimation, run a surface smoothing pass. Standard options:

- **Taubin smoothing** (`trimesh.smoothing.filter_taubin`). Low-pass filter over vertex positions that preserves volume — unlike Laplacian smoothing, it doesn't shrink the mesh. A few iterations will take the mean dihedral from ~30° down toward single digits. This is what Allen's pipeline (or whoever made those OBJs) did at some stage.
- **Marching cubes with anti-aliasing.** Feed marching cubes a blurred version of the binary mask instead of hard 0/1 — pre-apply a small gaussian. Surface is extracted at an interpolated contour, producing smoother faces natively.
- **Remeshing** (e.g. pymeshlab's Poisson-disk sampling + screened Poisson reconstruction). Heavier, higher quality, changes vertex count. Probably overkill.

Runtime cost: **none**. The render sees a smaller dihedral distribution; Phong shading looks continuous instead of crumpled.

Trade-off: Slight loss of "which voxel does this vertex belong to" fidelity. Not a concern for a visualization atlas — we don't label at the voxel level.

### 6b. Bake smooth vertex normals into the GLB

After smoothing, explicitly compute `mesh.vertex_normals` (trimesh will do this on access) and export. The exporter includes the `NORMAL` attribute in glTF 2.0 if it's populated.

Implementation note: `trimesh.export(..., file_type="glb")` emits NORMAL if `vertex_normals` is explicitly cached on the mesh before export, but not necessarily otherwise. Easiest is to force it: `_ = mesh.vertex_normals` right before export, or pass `include_normals=True` to the GLB exporter if available.

Runtime cost: **none** (it's just more data in the file). A small file-size increase; Three.js skips `computeVertexNormals` on load.

Quality effect: Smooth shading actually works, and it's computed from the *true* smoothed geometry rather than reconstructed by averaging face normals at load time.

### 6c. Make the root opaque and use it like Allen does

Opaque rendering skips the transparent-queue sort, hits early-z rejection, and collapses overdraw for everything behind it to zero. On Allen, this is why a 98k-face root runs as smoothly as a solid object.

Trade-off: data regions *inside* the brain can't be seen through the surface. Today our only macaque data is cortical (on the surface), so nothing is hidden. If future dandisets include subcortical recordings, we'd want to switch back — or use a render-order trick (draw data regions last with `depthTest: false`).

Runtime win: single biggest possible. Cuts per-pixel Phong invocations to ~1 for the part of the brain silhouette covered by the root.

### 6d. Don't render context meshes by default

App.js line 510 loads every mesh file in the directory for macaque, "for full anatomical context." Every one of those is a 0.08-opacity surface overlapping many others. On NMT that's 80+ extra transparent meshes filling the scene.

Option: load their geometry but keep them `visible = false`. Unhide a mesh only when the user selects it or its ancestor. The user still sees the full hierarchy in the tree and can click into anything; the scene just isn't rendering every parcellation cell at once.

Runtime win: eliminates the worst overdraw amplifier for the non-selected case.

Quality effect: positive — non-selected brain looks cleaner, data region pops out against a clean backdrop instead of against a mat of faint context shapes.

### 6e. Cheaper material for context meshes (if we keep rendering them)

If we want to keep context meshes visible, switching them from `MeshPhongMaterial` to `MeshLambertMaterial` (per-vertex lighting, no specular) or `MeshBasicMaterial` (no lighting at all) cuts their fragment cost by an order of magnitude. Keep Phong for the root and data regions.

Trade-off: Basic material looks flat — just a color fill, no shading — which is arguably *better* for low-opacity "context" anyway: they shouldn't compete visually with the data layer.

## 7. A recommended build-time-first path

Three sequential experiments, each of which can be evaluated independently and each of which is a pure improvement:

1. **Smooth + bake normals** (6a + 6b) in `generate_meshes`. This is the Allen-equivalent upgrade on the build side and should get the macaque meshes from "crumpled paper" to "continuous surface" with zero runtime impact. One build, re-render, observe.

2. **Make the root opaque** (6c). The second-biggest runtime win available, trivial config change. Re-render, observe. If subcortical hiding isn't a problem (and it isn't, today), keep it. If the smoothing from step 1 is already producing Allen-quality results, this may not even be needed — the point of step 1 was to let Phong shading read the geometry correctly, and the point of step 3 is to reduce overdraw regardless.

3. **Hide context meshes by default** (6d). Last because it's a behavioral change that users may notice — not just a visual tweak. Ship it only if 1 + 2 don't get us where we want to be.

Changes 6e (material swap) and the DoubleSide→FrontSide knob stay in reserve.

This ordering also lines up with the first-principles observation that **the mesh data itself is the root cause**, not the renderer config. The macaque meshes are the voxel-boundary output of marching cubes. The Allen meshes were post-processed by someone else before we got them. Closing that gap on the build side is the intervention; everything else is workaround.
