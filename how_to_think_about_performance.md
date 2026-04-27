# How to think about performance in the dandi-atlas viewer

A working mental model for predicting whether a rendering-related change will make the scene faster or slower — without benchmarking every idea. The goal is to make "feels slow" a number you can estimate in your head, and "Allen runs smoothly" a concrete budget you can measure against.

## 1. The currency is fragment-shader invocations, not triangles

Triangles feel like the obvious unit — "this mesh has 50k faces, that one has 200k" — but the GPU doesn't spend most of its time on triangles. It spends most of its time on **fragment-shader invocations**, also called "fragments" or "per-pixel shading."

For every triangle in the scene, the rasteriser works out which screen pixels it covers and then runs the fragment shader once for each of those pixels. The fragment shader is where Phong lighting, texture sampling, and alpha blending happen. It's the expensive step. Rasterisation (which triangle covers which pixel) is close to free on a modern GPU. Vertex shading (transforming vertices into screen space) scales with triangle count but usually isn't the bottleneck.

So when you want to predict whether a change will hurt performance, ask:

> How many fragment-shader invocations does this add or remove per frame?

Not "how many triangles." The same triangle can cost nothing (if it's culled by the z-buffer before the fragment shader runs) or an enormous amount (if it's a huge transparent triangle covering the whole screen that runs Phong for a million pixels).

## 2. The formula

```
fragment_calls_per_frame = screen_pixels × average_overdraw × side_factor
```

Three factors, each with a different source:

- `screen_pixels` comes from the user's window size and display density. Mostly not a lever for us.
- `average_overdraw` comes from how we structure the scene (opaque vs transparent meshes, how many layers overlap).
- `side_factor` comes from material settings (`FrontSide` vs `DoubleSide`).

Let's look at each.

## 3. `screen_pixels` — the one factor we don't control

This is the number of physical pixels the GPU actually renders to, not the logical window size. It's determined by two things multiplied:

**CSS canvas size.** How big the canvas element is on the page, in CSS pixels. In our app the canvas fills the available viewer area. A typical window is something like 1600 × 1000 = 1.6M CSS pixels.

**Device pixel ratio (DPR).** On a regular monitor, 1 CSS pixel corresponds to 1 physical pixel (DPR = 1). On a Retina-class display, 1 CSS pixel corresponds to 2×2 = 4 physical pixels (DPR = 2). On some modern phones and high-DPI laptops, DPR = 3 or more.

The app calls `renderer.setPixelRatio(window.devicePixelRatio)` without a cap (see `app.js` around line 350). That means on a Retina display the GPU is rendering at full device resolution. For a 1600 × 1000 CSS canvas:

| Display type | DPR | Physical pixels | Multiplier vs non-HiDPI |
|---|---:|---:|---:|
| Regular 1080p monitor | 1 | 1.6M | 1× |
| Retina MacBook / HiDPI laptop | 2 | 6.4M | **4×** |
| High-end phone, 4K Retina | 3 | 14.4M | **9×** |

This matters for *comparing performance across machines*, and it's why a scene that feels smooth on one person's laptop can be visibly choppy on another's. **A frame that cost 10 ms on DPR=1 can cost 40 ms on DPR=2** without any code change.

What we can do about it: call `renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2))` to cap at 2, giving up some crispness on very high-DPI displays in exchange for bounded frame time. We currently don't. Worth considering if we ever target high-DPI displays as a common case.

For budget reasoning, treat `screen_pixels` as fixed for the machine you're on. It normalises out when you compare atlases on the same screen.

## 4. `average_overdraw` — the lever that matters most

Overdraw is how many surfaces cover a single pixel, on average. A pixel at the centre of the brain probably has more than one mesh projecting onto it — the root, maybe a parent mesh, maybe a leaf mesh the user has clicked. Each of those surfaces runs the fragment shader once for that pixel, unless the GPU can skip it via early-z rejection.

Early-z only works on the **opaque render path**. Three.js sorts opaque meshes front-to-back and uses `depthWrite: true`. Once the closest opaque surface has written its depth at a pixel, the GPU rejects any further-back fragments at that pixel before the shader runs. Overdraw collapses to ~1 per pixel, no matter how many opaque surfaces stack up.

On the **transparent path**, meshes are sorted back-to-front with `depthWrite: false`. They don't block each other. Every surface that covers a pixel runs the full fragment shader and alpha-blends into the buffer. Overdraw equals the number of transparent surfaces at that pixel.

### The one decision that dominates overdraw

Is the root mesh opaque or transparent?

- **Opaque root (Allen):** the root fills the silhouette of the brain, writes depth, and kills everything behind it. Overdraw ≈ 1 inside the silhouette, plus maybe 1 extra layer wherever a data region covers.
- **Transparent root (our macaque setup currently):** every surface contributes. The root, every parent mesh in the clicked region's ancestor chain, every context mesh from other lobes that happens to overlap in projection — all of them fire the fragment shader at every pixel they cover. You commonly see overdraw of 5–12 in the centre of the brain.

An opaque root on macaque would cut overdraw by ~8× in one flip of a switch.

### Other contributors

- **Parent meshes** (introduced for the CHARM hierarchy fix) overlap with their children by construction. Every one of them is a full extra layer.
- **Context meshes** (non-data regions rendered at low opacity for anatomical context) contribute overdraw wherever they overlap in projection. They're cheap per pixel individually but plentiful.
- **Camera angle** matters — looking straight down a narrow axis can stack more layers than a lateral view.

## 5. `side_factor` — front-face-only vs double-sided rendering

Every triangle has a front face (normal toward the camera) and a back face. The `side` property of a material tells the GPU which to render:

- `FrontSide` (default in Three.js): render only front-facing triangles. Back faces are culled before rasterisation. Factor = 1.
- `DoubleSide`: render both. Every triangle is rasterised twice, once per side. Factor = 2.

`DoubleSide` is needed when the mesh isn't watertight (there are holes showing back faces through the gaps) or when you want transparency to reveal the far side of a hollow object. Our macaque meshes are set to `DoubleSide` in `app.js` for both reasons: marching cubes produces non-watertight output at volume boundaries, and the low-opacity root relies on seeing through to the back wall.

Cost implication: on transparent meshes, `DoubleSide` adds a full 2× to fragment cost. On opaque meshes, early-z still culls most back-face pixels, so the effective factor is close to 1 regardless.

## 6. The Allen reference — why it's useful as a baseline

The Allen CCF scene runs smoothly on the user's machine. That's empirical. Whatever fragment-shader cost Allen generates per frame, the machine handles it at interactive frame rates. Treat that as our definition of "in budget."

We don't need to know absolute numbers (frames per second, milliseconds per frame) to use this. We just need to know how much more — or less — expensive a proposed change would make the scene compared to Allen.

**The budget unit is the frame cost index, with Allen = 1.0.** It's a ratio. Anything at or near 1.0 will render smoothly on any machine that runs Allen smoothly. Anything significantly above 1.0 has to earn it.

### Computing the index

For any scene configuration:

```
cost_index = (overdraw × effective_side_factor) / allen_baseline
```

where `allen_baseline` is Allen's (overdraw × effective_side_factor) ≈ 1.5 (opaque root path + a couple of data regions, with early-z rescuing back faces).

A practical estimate: figure out overdraw by counting the layers of meshes that typically overlap in the middle of the scene, multiply by the side factor, and divide by Allen's 1.5. Round to a convenient number.

## 7. Current atlas budgets (measured)

| Atlas | Root opacity | Typical overdraw | Effective side factor | Frame cost index |
|---|---:|---:|---:|---:|
| **Allen CCF (mouse)** | 1.0 (opaque) | ~1.5 | ~1.0 | **1.0** (baseline) |
| **D99 (macaque)** | 0.3 (transparent) | ~8 | 2.0 | **~11×** |
| **NMT (macaque)** | 0.3 (transparent) | ~7 | 2.0 | **~9×** |
| **MEBRAINS (macaque)** | 0.25 (transparent) | ~8 | 2.0 | **~11×** |

These numbers explain why the macaque scenes feel sluggish. They're not about triangle count — they're about the render path. All three macaque atlases are spending roughly 10× more fragment work per pixel than Allen, on the same hardware.

## 8. Using the framework — four worked examples

Each example starts with a proposed change, estimates how it moves each factor, and computes the new index.

### Example 1: make the NMT root opaque

- Root opacity: 0.3 → 1.0. Routes through opaque queue.
- Overdraw: 7 → ~2 (root becomes 1 due to early-z; one data region on top).
- Side factor on root: effective 1 instead of 2.
- Other transparent meshes still exist but are mostly behind the opaque root.
- New index: (2 × 1) / 1.5 ≈ **1.3×**.
- **Verdict:** essentially at Allen's budget. Likely renders smoothly on the same hardware. Cost: can't see subcortical structures through the brain; irrelevant to our motor-cortex-only data today.

### Example 2: raise the root face count from 50k to 500k

- Triangle count goes up 10×.
- Rasterisation cost goes up (small — it's not the bottleneck anyway).
- Overdraw unchanged.
- Side factor unchanged.
- New index: ~11× × maybe 1.1 = **~12×** (marginal change).
- **Verdict:** roughly free for fragment-shader cost. Cheaper than intuition suggests, because all the new triangles are small and most are occluded or back-facing.

### Example 3: add 200 more transparent context meshes

- Overdraw: 7 → maybe 10 (a few of the new ones cover any given pixel).
- Side factor unchanged.
- New index: ~13× / Allen's 1.5 = **~13–14×**.
- **Verdict:** already over budget, getting worse. Don't add more transparent layers without a way to hide them when not selected.

### Example 4: Taubin smoothing + bake normals at build time

- Runtime overdraw, side factor, and pixel count: unchanged.
- Per-fragment shader cost: unchanged (still Phong).
- Geometry is smoother so the *visual* quality improves, but the *frame cost* doesn't move.
- New index: **unchanged (~11×)**.
- **Verdict:** pure free quality win. Do it unconditionally.

### What these examples teach

Performance-sensitive changes and visual-quality changes are largely orthogonal. Smoothing and normal-baking are visual upgrades with no cost. Opacity and render queue changes are cost upgrades with modest visual trade-offs. You can stack them.

The things to be suspicious of are changes that add transparent surfaces — those multiply overdraw and can quietly push the scene out of budget. Changes that reduce transparent surfaces or convert them to opaque are almost always wins.

## 9. The mental habit

Before every rendering-related change, ask this four-question sequence:

1. **Which factor does it touch?** `screen_pixels` (almost never), `overdraw` (often), or `side_factor` (occasionally)? Or is this a build-time change that runs once and doesn't touch the runtime at all?
2. **Does it change the render queue?** Opaque↔transparent is the single biggest lever. If you're flipping a mesh's `transparent` flag, you're making a roughly 10× decision.
3. **Does it add or remove overlapping surfaces?** Every parent mesh, context mesh, or transparent layer added is another Phong invocation per pixel.
4. **What does the resulting cost index look like compared to Allen's 1.0?**

If the answer to (4) stays near 1.0, ship it. If it balloons to 5× or 10×, either justify it with a concrete user-facing benefit or find a structural workaround (hide, opaque, cheaper material).

## 10. Checklist for future quality improvements

When you want to improve visual quality without hurting performance, prefer changes in this order:

1. **Build-time geometry improvements** — smoothing, normal baking, remeshing. Zero runtime cost.
2. **Opaque-queue changes** — making surfaces opaque where it doesn't lose information. Reduces overdraw.
3. **Face count and mesh detail on opaque meshes.** Cheap because back-facing triangles get culled.
4. **Higher-quality materials on the small number of "featured" surfaces** (the active region, the root). Expensive per pixel but the affected area is small.
5. **Additional transparent surfaces** — only after you've earned the headroom. Each one multiplies overdraw.

In the other direction, when you want to free up performance budget, work the list from bottom up: remove transparent surfaces first, then simplify overlapping meshes, then flip transparent to opaque where possible.

## Appendix: things not covered here that could matter later

- **Vertex-shader cost**: scales with vertex count, usually not the bottleneck. Could matter if we started using skinning or expensive per-vertex math.
- **Depth-sort overhead**: three.js sorts transparent meshes back-to-front every frame. Scales with mesh count, not triangle count. Currently negligible because we have <1000 meshes per atlas.
- **Texture memory and bandwidth**: we don't use textures right now. If region meshes gained per-region textures (say, parcellation colour overlays), memory bandwidth would join the cost list.
- **Shadow maps**: we don't use them. Adding shadows would roughly double frame cost because the scene is rasterised once per light from each light's viewpoint.
- **Multisample antialiasing (MSAA)**: Three.js uses it by default (`antialias: true` in `new WebGLRenderer`). MSAA runs fragment shaders at the edge of each primitive only, so its cost is usually bounded by the silhouette length, not total coverage. Not currently a concern.
