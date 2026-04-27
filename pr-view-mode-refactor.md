# Consolidate view mode logic into declarative config

The 3D viewer has four implicit view modes (atlas, region, dandiset, session) with visibility logic duplicated across roughly ten functions. Each function independently sets mesh opacity, visibility, material properties, and control row display. This duplication caused issue #12: `applyActive` used plain `regionAlpha` for all meshes, but the region-alpha slider handler correctly used `orig.opacity * regionAlpha` for non-root meshes, so dragging the slider would snap to a different opacity formula.

This PR introduces a `VIEW_MODES` config object that declares, per mode, what each mesh category (root, active, non-active) should look like, and a single `applyViewMode` function that reads the config and applies it. All call sites now go through `applyViewMode` instead of reimplementing the same loops with slightly different parameters.

The opacity bug is fixed as a prerequisite: a new `computeActiveOpacity(mesh)` function centralizes the formula, and `applyActive` uses it. The region-alpha slider handler now just updates the value and calls `applyViewMode`, so there is exactly one code path for opacity regardless of how the update is triggered.

I have left the electrode-control-row management with `showElectrodePoints`/`clearElectrodePoints` rather than pulling it into `applyViewMode`, since electrode visibility depends on whether a specific session has coordinate data, not just on the view mode.

Deleted functions: `restoreOriginal`, `showAllRegions`, `showOutlineSlider`, `applyIsolation`. All had zero remaining callers after the refactor.
