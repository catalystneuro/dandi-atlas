#!/usr/bin/env python3
"""Shared library for the macaque-atlas build pipeline.

Holds atlas configs (`ATLAS_CONFIGS`, `DANDISET_ID`, `ROOT_ID`, etc.), label
parsers, structure-graph builders, mesh-generation helpers, and DANDI fetch
utilities used by:

  - scripts/build_macaque_atlas.py   (orchestrator that runs all stages)
  - scripts/build_root_mesh.py       (root template mesh only)
  - scripts/build_region_meshes.py   (per-region meshes from NIFTI parcellation)
  - scripts/build_mesh_manifest.py   (mesh_manifest.json from existing meshes)
  - scripts/update_macaque_data.py   (DANDI fetch + per-atlas JSON refresh)

This module exposes everything as importable functions; it has no CLI of its
own. Run one of the build_* / update_* scripts to invoke a particular pipeline
stage. See `dandi_atlas_improvement_list.md` for the rationale behind the
decomposition.
"""

import csv
import hashlib
import io
import json
import re
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np
from scipy.ndimage import gaussian_filter

from dandi_helpers import (
    get_nwb_assets_paged,
    get_download_url,
    get_ancestors,
    build_dandi_regions,
    extract_session,
    extract_subject,
)

# ---------------------------------------------------------------------------
# Paths and constants
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = Path(__file__).resolve().parent

TURNER_DATA = Path(
    "/home/heberto/data/turner"
)

D99_LABELS_FILE = TURNER_DATA / "d99_atlas/D99_v2.0_dist/D99_v2.0_labels_semicolon.txt"
NMT_LABELS_FILE = TURNER_DATA / "nmt_v2/NMT_v2.0_sym/tables_D99/D99_labeltable.txt"
CHARM_LABELS_FILE = TURNER_DATA / "nmt_v2/NMT_v2.0_sym/tables_CHARM/CHARM_key_all.txt"
CHARM_PALETTE_FILE = TURNER_DATA / "nmt_v2/NMT_v2.0_sym/tables_CHARM/hue_CHARM_cmap.pal"
MEBRAINS_LABELS_FILE = TURNER_DATA / "mebrains/MEBRAINS_labels.json"

# Siibra (Scalable Infrastructure for Integration of Brain Atlas Research
# Architectures, Forschungszentrum Jülich) provides the de-facto MEBRAINS
# colour palette across the EBRAINS ecosystem. We fetch the parcellation tree
# and the labelled-map indices from their configuration repo, join on
# region-name, and cache the resulting {label_id: "RRGGBB"} dict locally so
# subsequent builds don't re-hit the network.
SIIBRA_MEBRAINS_PARCELLATION_URL = (
    "https://raw.githubusercontent.com/FZJ-INM1-BDA/siibra-configurations/"
    "master/parcellations/mebrains_parcellation.json"
)
SIIBRA_MEBRAINS_LABELLED_MAP_URL = (
    "https://raw.githubusercontent.com/FZJ-INM1-BDA/siibra-configurations/"
    "master/maps/monkey-mebrains-labelled.json"
)
SIIBRA_MEBRAINS_PALETTE_CACHE = SCRIPTS_DIR / "siibra_mebrains_palette.json"

# D99 whole-brain cortical surfaces from HumanBrainED/RheMAP-Surf. AFNI's D99
# v2 distribution does NOT ship whole-brain surfaces (only per-ROI meshes
# under surfs_right/), so before this cache existed the D99 root was built by
# marching cubes on D99_template.nii.gz — watertight enough, but visually
# washed out compared to NMT's per-subject gray_surface.rsl.gii. RheMAP-Surf
# publishes CIVET-macaque-derived pial surfaces in scanner-RAS co-registered
# to the same D99_template.nii.gz we already use. 167k verts/hemisphere.
D99_PIAL_LH_URL = (
    "https://raw.githubusercontent.com/HumanBrainED/RheMAP-Surf/main/"
    "templates/D99/D99_L_AVG_T1_v2.L.PIAL.167625.surf.gii"
)
D99_PIAL_RH_URL = (
    "https://raw.githubusercontent.com/HumanBrainED/RheMAP-Surf/main/"
    "templates/D99/D99_L_AVG_T1_v2.R.PIAL.167625.surf.gii"
)
D99_PIAL_LH_CACHE = SCRIPTS_DIR / "d99_pial_lh.surf.gii"
D99_PIAL_RH_CACHE = SCRIPTS_DIR / "d99_pial_rh.surf.gii"

ATLAS_CONFIGS = {
    "d99": {
        "nifti": TURNER_DATA / "d99_atlas/D99_v2.0_dist/D99_atlas_v2.0.nii.gz",
        "template_nifti": TURNER_DATA / "d99_atlas/D99_v2.0_dist/D99_template.nii.gz",
        "labels_type": "d99",
        "hdf5_path": "general/localization/D99v2AtlasCoordinates",
        "output_dir": PROJECT_ROOT / "data/atlases/d99",
        "cache_file": SCRIPTS_DIR / "d99_electrode_cache.jsonl",
        "root_name": "D99 Atlas",
        # D99 ships only right-hemisphere per-region surfaces. We mirror each
        # surface along X to synthesise a bilateral mesh, matching the
        # bilateral parcellation volume.
        "gifti_surfaces": {
            "surfaces_dir": TURNER_DATA / "d99_atlas/D99_v2.0_dist/surfs_right",
            # Whole-brain root surfaces from RheMAP-Surf (fetched by
            # ensure_d99_pial_cache on first build). Cached paths only — the
            # ensure function populates them from GitHub before build runs.
            "whole_brain_lh": D99_PIAL_LH_CACHE,
            "whole_brain_rh": D99_PIAL_RH_CACHE,
            "filename_pattern": re.compile(r"\.k(\d+)\.gii$"),
            "mirror_hemisphere": True,
        },
    },
    "nmt": {
        "nifti": TURNER_DATA / "nmt_v2/NMT_v2.0_sym/NMT_v2.0_sym/supplemental_CHARM/CHARM_4_in_NMT_v2.0_sym.nii.gz",
        "labels_type": "charm",
        "hdf5_path": "general/localization/NMTv2AtlasCoordinates",
        "output_dir": PROJECT_ROOT / "data/atlases/nmt",
        "cache_file": SCRIPTS_DIR / "nmt_electrode_cache.jsonl",
        "root_name": "NMT v2.0 sym (CHARM)",
        # CHARM ships per-region surfaces per hierarchy level. Each per-region
        # surface is already bilateral (symmetric about X=0), so no mirroring
        # is needed. Whole-brain root uses lh+rh gray-surface union.
        "gifti_surfaces": {
            "charm_levels_dir": TURNER_DATA / "nmt_v2/NMT_v2.0_sym/NMT_v2.0_sym_surfaces/atlases/CHARM",
            "whole_brain_lh": TURNER_DATA / "nmt_v2/NMT_v2.0_sym/NMT_v2.0_sym_surfaces/lh.gray_surface.rsl.gii",
            "whole_brain_rh": TURNER_DATA / "nmt_v2/NMT_v2.0_sym/NMT_v2.0_sym_surfaces/rh.gray_surface.rsl.gii",
            "filename_pattern": re.compile(r"\.k(\d+)\.gii$"),
            "mirror_hemisphere": False,
        },
    },
    "mebrains": {
        "nifti": TURNER_DATA / "mebrains/MEBRAINS_parcellation.nii.gz",
        "template_nifti": TURNER_DATA / "mebrains/MEBRAINS_T1.nii.gz",
        "labels_type": "mebrains",
        "hdf5_path": "general/localization/MEBRAINSAtlasCoordinates",
        "output_dir": PROJECT_ROOT / "data/atlases/mebrains",
        "cache_file": SCRIPTS_DIR / "mebrains_electrode_cache.jsonl",
        "root_name": "MEBRAINS Atlas",
    },
}

UNIT_ALIASES = {
    "meters": "m", "m": "m",
    "millimeters": "mm", "mm": "mm",
    "micrometers": "um", "um": "um", "\u00b5m": "um",
}

UNIT_TO_MM = {"m": 1000.0, "mm": 1.0, "um": 0.001}

DANDISET_ID = "001636"
ROOT_ID = 9999
OUTSIDE_ID = 9998
CATEGORY_ID_START = 10001
SUBCATEGORY_ID_START = 10100
TARGET_FACES = 10_000
# Root (whole-brain outline) gets a higher face cap so surface detail shows.
# Region meshes stay at TARGET_FACES since they're smaller and overlap with each other.
# Root is rendered transparent + DoubleSide, so keep this modest — each face costs
# 2x to draw and enters the transparent-queue sort path.
ROOT_TARGET_FACES = 100_000
# GIFTI surfaces ship with their own smooth, well-balanced triangulation
# (median dihedral ≈ 0°, mean ≈ 10°). Aggressive quadric decimation destroys
# this quality — pushing the mean above 25°. We keep the GIFTI target much
# higher so most per-region meshes pass through unchanged; only truly huge
# surfaces (e.g. CHARM level 1 lobes with ~250k triangles) get decimated.
TARGET_FACES_GIFTI = 20_000
ROOT_TARGET_FACES_GIFTI = 60_000
MIN_VOXELS = 50

# Category color hues (HSL hue in degrees), covering both D99 and MEBRAINS
CATEGORY_HUES = {
    # D99 categories
    "Basal ganglia": 0,
    "Thalamus": 30,
    "Brainstem": 60,
    "Hypothalamus": 90,
    "Cerebellum": 120,
    "Fiber bundle": 180,
    "Cortex": 210,
    "Hippocampus": 240,
    "Amygdala": 270,
    "Basal forebrain": 300,
    "Bed nucleus of stria terminalis": 330,
    # MEBRAINS categories
    "Motor cortex": 0,
    "Prefrontal cortex": 30,
    "Parietal cortex": 60,
    "Visual cortex": 120,
    "Temporal cortex": 180,
    "Claustrum": 300,
    "White matter": 150,
    "Ventricle": 200,
    "Other": 150,
}


# D5a, per-category hue ranges for D99 (hue_centre, hue_width in degrees).
# Each region within a category gets a specific hue from hash(abbrev) within
# the (centre - width/2, centre + width/2) arc. Fixed saturation 0.6, lightness 0.45.
# Chosen to differ from Allen CCF's conventions so D99 reads as a standalone
# macaque atlas. See obsidian_docs/3d_visualization/macaque_color_policy.md.
D99_CATEGORY_RANGES = {
    "Cortex":                           (220, 60),   # blue family
    "Basal ganglia":                    (20,  40),   # warm red-orange
    "Thalamus":                         (320, 40),   # magenta-pink
    "Brainstem":                        (60,  40),   # olive-yellow
    "Cerebellum":                       (150, 40),   # teal-green
    "Hypothalamus":                     (95,  30),   # yellow-green
    "Hippocampus":                      (275, 30),   # violet
    "Amygdala":                         (0,   20),   # red
    "Basal forebrain":                  (180, 30),   # cyan
    "Fiber bundle":                     (45,  20),   # ochre
    "Bed nucleus of stria terminalis":  (340, 15),   # pink
    "Other":                            (100, 30),   # yellow-green fallback
}


# ---------------------------------------------------------------------------
# D99 label parsing (reused by NMT)
# ---------------------------------------------------------------------------


def parse_d99_labels():
    """Parse D99 semicolon-delimited labels file.

    Returns list of dicts with keys: index, abbreviation, name, category, subcategory.
    """
    entries = []
    with open(D99_LABELS_FILE, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            reader = csv.reader(io.StringIO(line), delimiter=";")
            fields = next(reader)
            fields = [field.strip() for field in fields]

            label_index = int(fields[0])
            abbreviation = fields[1].strip()
            name = fields[2].strip()

            category = fields[3].strip() if len(fields) > 3 else ""
            subcategory = fields[4].strip() if len(fields) > 4 else ""

            if not category:
                name_lower = name.lower()
                if any(
                    kw in name_lower
                    for kw in [
                        "cortex", "cortical", "area", "gyrus", "sulcus",
                        "opercul", "prefrontal", "parietal", "temporal",
                        "visual", "auditory", "somato", "insula", "cingulate",
                        "retrosplenial", "parahippocampal", "perirhinal",
                        "entorhinal", "precentral", "frontal area",
                        "belt region", "core region",
                    ]
                ):
                    category = "Cortex"
                elif any(kw in name_lower for kw in ["amygdal", "periamygdal"]):
                    category = "Amygdala"
                elif any(
                    kw in name_lower
                    for kw in ["hippocamp", "subicul", "fascia dentata", "ca1", "ca2", "ca3", "ca4"]
                ):
                    category = "Hippocampus"
                elif "claustrum" in name_lower:
                    category = "Basal forebrain"
                elif "olfactory" in name_lower:
                    category = "Cortex"
                else:
                    category = "Other"

            entries.append({
                "index": label_index,
                "abbreviation": abbreviation,
                "name": name,
                "category": category,
                "subcategory": subcategory,
            })
    return entries


# ---------------------------------------------------------------------------
# NMT label parsing (D99 labels in NMT numbering)
# ---------------------------------------------------------------------------


def parse_nmt_labels():
    """Parse NMT D99 label table and enrich with D99 metadata.

    The NMT volume uses its own label IDs (2-224) that differ from D99's (1-522).
    This function reads the NMT label table, then looks up each abbreviation in
    the D99 labels file for name, category, and subcategory metadata.

    Returns list of dicts with keys: index, abbreviation, name, category,
    subcategory, d99_abbreviation (the original D99 abbreviation for alias mapping).
    """
    # Build D99 abbreviation -> metadata lookup
    d99_entries = parse_d99_labels()
    d99_by_abbrev = {e["abbreviation"]: e for e in d99_entries}

    # Also build a normalized lookup for fuzzy matching
    d99_by_norm = {}
    for e in d99_entries:
        # Normalize: lowercase, strip "area_" prefix, remove special chars
        norm = e["abbreviation"].lower().replace("area_", "").replace("'", "").replace("?", "")
        d99_by_norm[norm] = e

    # Read NMT label table: "  NMT_ID  abbreviation"
    entries = []
    with open(NMT_LABELS_FILE) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            parts = line.split(None, 1)
            nmt_id = int(parts[0])
            abbrev = parts[1].strip()

            # Try exact match first
            d99_entry = d99_by_abbrev.get(abbrev)

            # Try normalized fuzzy match
            if d99_entry is None:
                norm = abbrev.lower().replace("area_", "").replace("'", "").replace("?", "")
                d99_entry = d99_by_norm.get(norm)

            if d99_entry is not None:
                # Store the D99 abbreviation so we can add it as an alias
                d99_abbrev = d99_entry["abbreviation"]
                entries.append({
                    "index": nmt_id,
                    "abbreviation": abbrev,
                    "name": d99_entry["name"],
                    "category": d99_entry["category"],
                    "subcategory": d99_entry["subcategory"],
                    "d99_abbreviation": d99_abbrev,
                })
            else:
                # No D99 match: use abbreviation as name, infer category
                name_lower = abbrev.lower()
                if "cerebellum" in name_lower:
                    category = "Cerebellum"
                else:
                    category = "Other"
                entries.append({
                    "index": nmt_id,
                    "abbreviation": abbrev,
                    "name": abbrev,
                    "category": category,
                    "subcategory": "",
                    "d99_abbreviation": None,
                })

    return entries


# ---------------------------------------------------------------------------
# CHARM label parsing (native NMT v2 parcellation)
# ---------------------------------------------------------------------------


def parse_charm_labels():
    """Parse CHARM_key_all.txt and return entries with hierarchy.

    CHARM provides a hierarchical cortical parcellation for NMT v2.0-sym at
    6 levels (level 1 = 4 lobes, level 6 = ~130 fine areas). The file is
    ordered parent-before-child, so we track ancestors at each level to
    determine parent-child relationships.

    Returns list of dicts with keys: index, abbreviation, name, level,
    parent_index (index of the parent entry, or None for level 1).
    """
    entries = []
    # Track the most recent entry at each level to determine parentage
    ancestors = {}

    with open(CHARM_LABELS_FILE) as f:
        next(f)  # skip header
        for line in f:
            line = line.strip()
            if not line:
                continue
            parts = line.split("\t")
            index = int(parts[0])
            abbreviation = parts[1]
            full_name = parts[2].replace("_", " ")
            first_level = int(parts[3])

            # Parent is the most recent ancestor at any level strictly less
            # than first_level. CHARM can skip levels (e.g. level 3 -> level 5),
            # so we must walk up from first_level - 1 until we find one.
            if first_level == 1:
                parent_index = None
            else:
                parent_index = None
                for ancestor_level in range(first_level - 1, 0, -1):
                    if ancestor_level in ancestors:
                        parent_index = ancestors[ancestor_level]
                        break

            ancestors[first_level] = index
            # Clear any stale deeper-level entries. A new entry at first_level
            # resets the context below it, so prior entries at deeper levels
            # (from a different branch) must not become false ancestors.
            for ancestor_level in list(ancestors):
                if ancestor_level > first_level:
                    del ancestors[ancestor_level]

            entries.append({
                "index": index,
                "abbreviation": abbreviation,
                "name": full_name,
                "level": first_level,
                "parent_index": parent_index,
            })

    return entries


def load_charm_palette():
    """Load the official CHARM colour palette shipped with NMT v2.0-sym.

    The file is a simple list of hex colours (one per line) with a text
    header on the first line. Line N+1 corresponds to CHARM label index N,
    so the palette maps 1-based label indices to hex colour strings.

    Returns dict {label_index: "rrggbb"} with no leading '#', matching the
    `color_hex_triplet` format used elsewhere in the script.
    """
    palette = {}
    with open(CHARM_PALETTE_FILE) as f:
        next(f)  # skip header line (e.g. "Hue_CHARM_v1.3")
        for label_index, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            hex_code = line.lstrip("#").upper()
            palette[label_index] = hex_code
    return palette


def _walk_siibra_regions(node, out):
    """Recursively collect (name, rgb) pairs from a siibra parcellation tree.

    The tree uses a mix of dicts and lists. A dict that carries both "name"
    and "rgb" is a recordable region; we also recurse into any list values
    and any dict values to keep walking deeper. Lists are traversed element
    by element.
    """
    if isinstance(node, dict):
        name = node.get("name")
        rgb = node.get("rgb")
        if isinstance(name, str) and isinstance(rgb, str):
            out.append((name, rgb))
        for value in node.values():
            if isinstance(value, (dict, list)):
                _walk_siibra_regions(value, out)
    elif isinstance(node, list):
        for item in node:
            _walk_siibra_regions(item, out)


def load_mebrains_palette_from_siibra():
    """Fetch (or load cached) MEBRAINS region colours from siibra-configurations.

    Joins the parcellation region tree with the labelled-map `indices` dict by
    region-name to produce {label_id: "RRGGBB"} (no '#' prefix, uppercased to
    match the `color_hex_triplet` format used elsewhere in this script).

    Caches the resulting dict at SIIBRA_MEBRAINS_PALETTE_CACHE so subsequent
    builds skip the network fetch. Returns an empty dict on network failure
    when no cache is available, so the caller can fall back to fabricated
    colours without crashing.
    """
    if SIIBRA_MEBRAINS_PALETTE_CACHE.exists():
        with open(SIIBRA_MEBRAINS_PALETTE_CACHE) as f:
            palette = json.load(f)
        # JSON object keys are strings; normalise to int label IDs.
        palette = {int(k): v for k, v in palette.items()}
        print(
            f"  Loaded MEBRAINS palette from cache "
            f"({len(palette)} entries, {SIIBRA_MEBRAINS_PALETTE_CACHE.name})"
        )
        return palette

    try:
        with urllib.request.urlopen(SIIBRA_MEBRAINS_PARCELLATION_URL) as response:
            parcellation = json.loads(response.read().decode("utf-8"))
        with urllib.request.urlopen(SIIBRA_MEBRAINS_LABELLED_MAP_URL) as response:
            labelled_map = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
        print(
            f"  WARNING: Could not fetch siibra MEBRAINS palette ({exc}); "
            f"falling back to fabricated colours."
        )
        return {}

    name_to_rgb = {}
    pairs = []
    _walk_siibra_regions(parcellation, pairs)
    for name, rgb in pairs:
        name_to_rgb[name] = rgb.lstrip("#").upper()

    indices = labelled_map.get("indices", {})
    palette = {}
    for region_name, entries in indices.items():
        rgb = name_to_rgb.get(region_name)
        if rgb is None:
            continue
        if not isinstance(entries, list):
            continue
        for entry in entries:
            label = entry.get("label")
            if label is None:
                continue
            palette[int(label)] = rgb

    # Persist cache (keys as strings per JSON convention).
    with open(SIIBRA_MEBRAINS_PALETTE_CACHE, "w") as f:
        json.dump({str(k): v for k, v in palette.items()}, f, indent=2, sort_keys=True)
    print(
        f"  Fetched MEBRAINS palette from siibra "
        f"({len(palette)} entries, cached to {SIIBRA_MEBRAINS_PALETTE_CACHE.name})"
    )
    return palette


def ensure_d99_pial_cache():
    """Download D99 RheMAP-Surf pial surfaces to the scripts cache if absent.

    The AFNI D99 v2 distribution ships no whole-brain surface, so we pull
    from the HumanBrainED/RheMAP-Surf community repository. Files are ~8 MB
    each; first build downloads them once, subsequent builds use the cache.
    """
    targets = [
        (D99_PIAL_LH_URL, D99_PIAL_LH_CACHE),
        (D99_PIAL_RH_URL, D99_PIAL_RH_CACHE),
    ]
    for url, dest in targets:
        if dest.exists() and dest.stat().st_size > 0:
            continue
        print(f"  Fetching {dest.name} from RheMAP-Surf...")
        try:
            with urllib.request.urlopen(url) as response:
                data = response.read()
            dest.write_bytes(data)
            print(f"    Cached {len(data):,} bytes to {dest}")
        except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
            print(f"    WARNING: could not fetch {url}: {exc}")
            return None, None
    return D99_PIAL_LH_CACHE, D99_PIAL_RH_CACHE


def build_charm_structure_graph(entries, root_name="NMT v2.0 sym (CHARM)"):
    """Build a structure graph from CHARM entries using their native hierarchy.

    Unlike build_structure_graph (which uses category/subcategory), this
    uses CHARM's own parent-child relationships from the level hierarchy.

    Returns (tree_root, id_to_structure, parent_map, abbrev_to_id).
    """
    id_to_structure = {}
    parent_map = {}
    abbrev_to_id = {}

    # Official CHARM colour palette, keyed by label index. Shipped with the
    # NMT v2.0-sym distribution as tables_CHARM/hue_CHARM_cmap.pal.
    charm_palette = load_charm_palette()

    root = {
        "id": ROOT_ID,
        "acronym": "root",
        "name": root_name,
        "color_hex_triplet": "FFFFFF",
        "parent_structure_id": None,
        "children": [],
    }
    id_to_structure[ROOT_ID] = root
    parent_map[ROOT_ID] = None

    outside_node = {
        "id": OUTSIDE_ID,
        "acronym": "outside",
        "name": "Outside atlas",
        "color_hex_triplet": "888888",
        "parent_structure_id": ROOT_ID,
        "children": [],
    }
    id_to_structure[OUTSIDE_ID] = outside_node
    parent_map[OUTSIDE_ID] = ROOT_ID

    for entry in entries:
        label_id = entry["index"]
        parent_index = entry["parent_index"]
        parent_id = parent_index if parent_index is not None else ROOT_ID

        # Use the official CHARM colour. Fall back to a neutral grey if the
        # palette is missing an entry for this label (shouldn't happen with
        # the shipped palette, which covers all 247 CHARM labels).
        color = charm_palette.get(label_id, "AAAAAA")

        node = {
            "id": label_id,
            "acronym": entry["abbreviation"],
            "name": entry["name"],
            "color_hex_triplet": color,
            "parent_structure_id": parent_id,
            "children": [],
        }
        id_to_structure[label_id] = node
        parent_map[label_id] = parent_id
        abbrev_to_id[entry["abbreviation"]] = label_id

    # Build tree by nesting children
    for node_id, node in id_to_structure.items():
        pid = node.get("parent_structure_id")
        if pid is not None and pid in id_to_structure:
            id_to_structure[pid]["children"].append(node)

    return [root], id_to_structure, parent_map, abbrev_to_id


# ---------------------------------------------------------------------------
# MEBRAINS label parsing
# ---------------------------------------------------------------------------


def _categorize_mebrains(base_name):
    """Infer category for a MEBRAINS region from its base name."""
    # Motor cortex (area 4 variants and frontal motor areas F2-F7)
    if base_name in ("4a", "4p", "4m"):
        return "Motor cortex"
    if base_name.startswith(("F2", "F3", "F4", "F5", "F6", "F7")):
        return "Motor cortex"
    # Prefrontal cortex (check specific names before digit-based catch-all)
    if base_name in ("44", "45A", "45B"):
        return "Prefrontal cortex"
    if base_name.startswith(("a46", "p46")):
        return "Prefrontal cortex"
    # Remaining digit-prefixed names: 8xx, 9x, 10x, 11x, 12x, 13x, 14x
    if base_name[:1].isdigit():
        return "Prefrontal cortex"
    # Parietal cortex (check before visual since VIP starts with V)
    if base_name.startswith(("PE", "PF", "PG")):
        return "Parietal cortex"
    if base_name in ("AIP", "DP", "LOP", "PIP"):
        return "Parietal cortex"
    if base_name.startswith(("VIP", "LIP", "MIP")):
        return "Parietal cortex"
    # Visual cortex
    if base_name.startswith("V") or base_name == "Opt":
        return "Visual cortex"
    # Temporal cortex
    if base_name == "TSA":
        return "Temporal cortex"
    # Subcortical
    if base_name in ("caudate nucleus", "putamen", "globus pallidus", "nucleus accumbens"):
        return "Basal ganglia"
    if base_name == "amygdala":
        return "Amygdala"
    if base_name == "claustrum":
        return "Claustrum"
    if base_name == "anterior commissure":
        return "White matter"
    if base_name == "lateral ventricle":
        return "Ventricle"
    return "Other"


def parse_mebrains_labels():
    """Parse MEBRAINS JSON labels file.

    Returns list of dicts with keys: index, abbreviation, name, category, subcategory.
    """
    with open(MEBRAINS_LABELS_FILE) as f:
        data = json.load(f)

    entries = []
    for label_id_str, name in data.items():
        label_id = int(label_id_str)

        if name.endswith(" left"):
            hemisphere = "L"
            base_name = name[:-5]
        elif name.endswith(" right"):
            hemisphere = "R"
            base_name = name[:-6]
        else:
            hemisphere = ""
            base_name = name

        category = _categorize_mebrains(base_name)

        # Abbreviation: base_name with hemisphere suffix for uniqueness
        if hemisphere:
            abbreviation = f"{base_name}_{hemisphere}"
        else:
            abbreviation = base_name

        entries.append({
            "index": label_id,
            "abbreviation": abbreviation,
            "name": name,
            "category": category,
            "subcategory": "",
        })
    return entries


# ---------------------------------------------------------------------------
# Structure graph building (shared by all atlases)
# ---------------------------------------------------------------------------


def _hsl_to_hex(h, s, l):
    """Convert HSL (h in degrees, s and l in 0-1) to hex color string."""
    c = (1 - abs(2 * l - 1)) * s
    x = c * (1 - abs((h / 60) % 2 - 1))
    m = l - c / 2

    if h < 60:
        r, g, b = c, x, 0
    elif h < 120:
        r, g, b = x, c, 0
    elif h < 180:
        r, g, b = 0, c, x
    elif h < 240:
        r, g, b = 0, x, c
    elif h < 300:
        r, g, b = x, 0, c
    else:
        r, g, b = c, 0, x

    r, g, b = int((r + m) * 255), int((g + m) * 255), int((b + m) * 255)
    return f"{r:02X}{g:02X}{b:02X}"


def _d99_color_for(abbreviation, category):
    """D5a: per-region hue hashed within the category's hue range."""
    centre, width = D99_CATEGORY_RANGES.get(category, D99_CATEGORY_RANGES["Other"])
    h = hashlib.md5(abbreviation.encode("utf-8")).hexdigest()[:8]
    frac = int(h, 16) / 0xFFFFFFFF
    hue = (centre - width / 2 + frac * width) % 360
    return _hsl_to_hex(hue, 0.6, 0.45)


def build_structure_graph(entries, root_name="D99 Atlas", color_overrides=None, labels_type=None):
    """Build a hierarchical structure graph from parsed label entries.

    If `color_overrides` is provided, it should be a dict {label_index:
    "RRGGBB"} supplying authoritative colours for specific regions (e.g. the
    siibra palette for MEBRAINS). Any entry whose index is present there uses
    that colour verbatim.

    When `labels_type == "d99"`, leaf region colours are generated via the D5a
    scheme (per-region hue hashed within each category's hue range in
    `D99_CATEGORY_RANGES`, fixed S=0.6 L=0.45). Otherwise, leaf colours fall
    back to the legacy per-index-within-category HSL formula keyed off
    CATEGORY_HUES.

    Returns (tree_root, id_to_structure, parent_map, abbrev_to_id).
    """
    # Assign colors by category with brightness variation
    category_counts = {}
    for entry in entries:
        cat = entry["category"]
        category_counts[cat] = category_counts.get(cat, 0) + 1

    entry_colors = {}
    category_index = {}
    for entry in entries:
        cat = entry["category"]
        current = category_index.get(cat, 0)
        category_index[cat] = current + 1
        if color_overrides is not None and entry["index"] in color_overrides:
            entry_colors[entry["index"]] = color_overrides[entry["index"]]
        elif labels_type == "d99":
            entry_colors[entry["index"]] = _d99_color_for(
                entry["abbreviation"], cat
            )
        else:
            hue = CATEGORY_HUES.get(cat, 150)
            total = category_counts[cat]
            lightness = 0.35 + 0.4 * (current / max(total - 1, 1))
            entry_colors[entry["index"]] = _hsl_to_hex(hue, 0.6, lightness)

    # Collect unique categories and subcategories
    categories = {}
    for entry in entries:
        cat = entry["category"]
        subcat = entry["subcategory"]
        if cat not in categories:
            categories[cat] = set()
        if subcat:
            primary_subcat = subcat.split("_")[0]
            categories[cat].add(primary_subcat)

    # Assign synthetic IDs
    category_ids = {}
    next_cat_id = CATEGORY_ID_START
    for cat in sorted(categories.keys()):
        category_ids[cat] = next_cat_id
        next_cat_id += 1

    subcategory_ids = {}
    next_subcat_id = SUBCATEGORY_ID_START
    for cat in sorted(categories.keys()):
        for subcat in sorted(categories[cat]):
            key = (cat, subcat)
            subcategory_ids[key] = next_subcat_id
            next_subcat_id += 1

    # Build flat structure list
    id_to_structure = {}
    parent_map = {}

    root = {
        "id": ROOT_ID,
        "acronym": "root",
        "name": root_name,
        "color_hex_triplet": "FFFFFF",
        "parent_structure_id": None,
        "children": [],
    }
    id_to_structure[ROOT_ID] = root
    parent_map[ROOT_ID] = None

    outside_node = {
        "id": OUTSIDE_ID,
        "acronym": "outside",
        "name": "Outside atlas",
        "color_hex_triplet": "888888",
        "parent_structure_id": ROOT_ID,
        "children": [],
    }
    id_to_structure[OUTSIDE_ID] = outside_node
    parent_map[OUTSIDE_ID] = ROOT_ID

    for cat, cat_id in category_ids.items():
        hue = CATEGORY_HUES.get(cat, 150)
        node = {
            "id": cat_id,
            "acronym": cat.replace(" ", "_"),
            "name": cat,
            "color_hex_triplet": _hsl_to_hex(hue, 0.5, 0.5),
            "parent_structure_id": ROOT_ID,
            "children": [],
        }
        id_to_structure[cat_id] = node
        parent_map[cat_id] = ROOT_ID

    for (cat, subcat), subcat_id in subcategory_ids.items():
        cat_id = category_ids[cat]
        hue = CATEGORY_HUES.get(cat, 150)
        node = {
            "id": subcat_id,
            "acronym": subcat.replace(" ", "_"),
            "name": subcat,
            "color_hex_triplet": _hsl_to_hex(hue, 0.5, 0.55),
            "parent_structure_id": cat_id,
            "children": [],
        }
        id_to_structure[subcat_id] = node
        parent_map[subcat_id] = cat_id

    abbrev_to_id = {}
    for entry in entries:
        label_id = entry["index"]
        cat = entry["category"]
        subcat = entry["subcategory"]

        if subcat:
            primary_subcat = subcat.split("_")[0]
            parent_id = subcategory_ids.get(
                (cat, primary_subcat), category_ids.get(cat, ROOT_ID)
            )
        else:
            parent_id = category_ids.get(cat, ROOT_ID)

        node = {
            "id": label_id,
            "acronym": entry["abbreviation"],
            "name": entry["name"],
            "color_hex_triplet": entry_colors.get(label_id, "AAAAAA"),
            "parent_structure_id": parent_id,
            "children": [],
        }
        id_to_structure[label_id] = node
        parent_map[label_id] = parent_id
        abbrev_to_id[entry["abbreviation"]] = label_id
        # For NMT entries, also add the D99 abbreviation as an alias so that
        # brain_region_id values from NWB files (which use D99 abbreviations)
        # resolve to the correct NMT structure IDs.
        d99_abbrev = entry.get("d99_abbreviation")
        if d99_abbrev and d99_abbrev != entry["abbreviation"]:
            abbrev_to_id[d99_abbrev] = label_id
        # Add string label ID as alias (e.g., "303" -> 303) so that
        # brain_region_id values stored as string IDs (MEBRAINS) resolve correctly.
        str_id = str(label_id)
        if str_id not in abbrev_to_id:
            abbrev_to_id[str_id] = label_id

    # Build tree by nesting children
    for node_id, node in id_to_structure.items():
        pid = node.get("parent_structure_id")
        if pid is not None and pid in id_to_structure:
            id_to_structure[pid]["children"].append(node)

    return [root], id_to_structure, parent_map, abbrev_to_id


# ---------------------------------------------------------------------------
# Mesh generation from NIfTI
# ---------------------------------------------------------------------------


def _export_template_root_glb(template_nifti, root_glb, target_faces, level=50.0):
    """Generate a root (whole-brain) mesh by marching cubes on a T1 template.

    Shared by both the pure-NIfTI path (MEBRAINS fallback) and the GIFTI path
    (D99, which has no upstream whole-brain surface). The template is treated
    as a continuous intensity field pre-smoothed with a gaussian, matching the
    per-region MC smoothing so the root blends visually with other outlines.
    After marching cubes we keep only the largest connected component: T1
    templates often contain small disjoint voxel islands (dura remnants,
    ventricle edges) that push the Euler number up by hundreds without
    contributing to the visible brain outline. Vertex normals are cached for
    GLB export; the marching-cubes output is kept in atlas-native RAS+ mm
    (no axis flip applied).

    Returns the exported trimesh.Trimesh (or None if extraction failed).
    """
    import nibabel as nib
    from skimage.measure import marching_cubes
    import trimesh

    t1_img = nib.load(str(template_nifti))
    t1_data = np.asarray(t1_img.dataobj, dtype=np.float32)
    t1_affine = t1_img.affine
    # Gaussian pre-smooth matches per-region MC blur (sigma=1.0) so the
    # isosurface is smoother than raw marching cubes on noisy MRI data.
    t1_smooth = gaussian_filter(t1_data, sigma=1.0)
    # Pad with zeros so the isosurface can never touch the grid boundary,
    # which would produce open edges in the MC output. The +1 voxel offset
    # in the affine compensates for the added padding so world coords stay
    # correct. Without this MEBRAINS produced ~66 boundary edges where the
    # brain touched the image edge.
    t1_smooth = np.pad(t1_smooth, 1, mode="constant", constant_values=0.0)
    pad_offset = t1_affine[:3, :3] @ np.array([-1.0, -1.0, -1.0])
    t1_affine = t1_affine.copy()
    t1_affine[:3, 3] += pad_offset
    verts, faces, _, _ = marching_cubes(t1_smooth, level=level)
    verts_homogeneous = np.column_stack([verts, np.ones(len(verts))])
    verts_world = (t1_affine @ verts_homogeneous.T).T[:, :3]
    # Vertices are now in atlas-native RAS+ mm world space (the NIFTI affine
    # normalizes any voxel-storage permutation). No additional axis flip — the
    # JSON / GLB outputs stay truthful to the source coordinate system.
    mesh = trimesh.Trimesh(vertices=verts_world, faces=faces)
    # Keep only the largest connected component to drop template noise.
    components = mesh.split(only_watertight=False)
    if len(components) > 1:
        components.sort(key=lambda c: len(c.faces), reverse=True)
        mesh = components[0]
    # Close any small holes introduced by MC at the grid boundary or in thin
    # tissue regions. This prevents the "see-through when rotating" effect
    # where the camera peeks through a hole in the near wall to the back
    # wall's front face.
    trimesh.repair.fill_holes(mesh)
    # Decimate to the target face count. Raw MC output is ~240k faces which
    # caused MEBRAINS to run at 8 FPS at init. We decimate *after* hole
    # filling and *before* winding normalization so the output has both a
    # manageable face count AND correctly outward-facing normals. Quadric
    # decimation can introduce a small number of non-manifold edges but
    # they no longer produce the rotate-to-see-through bug now that the
    # dominant problem (globally-inverted winding) is fixed downstream.
    if len(mesh.faces) > target_faces:
        mesh = mesh.simplify_quadric_decimation(face_count=target_faces)
    # Normalize face winding so normals point outward. Marching-cubes output
    # winding can occasionally be globally inward depending on the gradient
    # direction at the isosurface — under Three.js FrontSide culling that
    # means the surface disappears when viewed from outside, exposing the
    # opposite wall's inside-facing geometry ("see-through when rotating").
    # Defensive normalization handles the case.
    mesh.faces = _normalize_winding_outward(
        np.asarray(mesh.vertices), np.asarray(mesh.faces),
    )
    _ = mesh.vertex_normals  # bake normals for GLB export
    mesh.export(str(root_glb), file_type="glb")
    return mesh


def generate_meshes(
    nifti_file, meshes_dir, id_to_structure, template_nifti=None,
):
    """Generate GLB meshes from a NIfTI atlas volume.

    Root mesh: marching cubes on template_nifti if provided, else on the
    parcellation union as a fallback. See atlas_meshes_and_volumes.md for
    why the MEBRAINS pial-surfaces path was removed (open-midline shells).

    Returns list of label IDs that have no mesh.
    """
    import nibabel as nib
    from skimage.measure import marching_cubes
    import trimesh

    meshes_dir.mkdir(parents=True, exist_ok=True)

    img = nib.load(str(nifti_file))
    affine = img.affine
    atlas_data = np.asarray(img.dataobj, dtype=np.int16)

    unique_labels = set(np.unique(atlas_data)) - {0}
    print(f"Found {len(unique_labels)} non-zero labels in NIfTI volume")

    no_mesh = []
    generated = 0
    skipped_existing = 0
    skipped_small = 0

    # Root mesh (whole brain outline)
    root_glb = meshes_dir / f"{ROOT_ID}.glb"
    if not root_glb.exists():
        if template_nifti:
            # Use T1 template for a complete brain surface.
            print(f"Generating root mesh from template: {template_nifti.name}")
            mesh = _export_template_root_glb(
                template_nifti, root_glb, ROOT_TARGET_FACES,
            )
            print(f"  Root mesh: {len(mesh.faces)} faces")
            generated += 1
        else:
            # Use parcellation (union of all labeled voxels). Pre-blur the
            # binary mask so marching_cubes produces smoother surfaces.
            print("Generating root mesh (whole brain)...")
            root_mask = atlas_data > 0
            root_field = gaussian_filter(root_mask.astype(float), sigma=1.0)
            if root_field.max() <= 0.5:
                root_field = root_mask.astype(float)
            root_level = 0.5
            verts, faces, _, _ = marching_cubes(root_field, level=root_level)
            verts_homogeneous = np.column_stack([verts, np.ones(len(verts))])
            verts_world = (affine @ verts_homogeneous.T).T[:, :3]
            mesh = trimesh.Trimesh(vertices=verts_world, faces=faces)
            if len(mesh.faces) > ROOT_TARGET_FACES:
                mesh = mesh.simplify_quadric_decimation(face_count=ROOT_TARGET_FACES)
            _ = mesh.vertex_normals  # force trimesh to compute and cache normals so GLB export includes NORMAL attribute
            mesh.export(str(root_glb), file_type="glb")
            print(f"  Root mesh: {len(mesh.faces)} faces")
            generated += 1
    else:
        skipped_existing += 1

    # Per-region meshes
    for label_id in sorted(unique_labels):
        glb_path = meshes_dir / f"{label_id}.glb"
        if glb_path.exists():
            skipped_existing += 1
            continue

        mask = atlas_data == label_id
        voxel_count = np.sum(mask)
        if voxel_count < MIN_VOXELS:
            skipped_small += 1
            no_mesh.append(int(label_id))
            continue

        try:
            mask_smooth = gaussian_filter(mask.astype(float), sigma=1.0)
            # For very small or thin regions the blur can push the peak below
            # 0.5, which would leave level=0.5 outside the data range. Fall
            # back to the raw binary mask in that case so we still emit a mesh.
            if mask_smooth.max() <= 0.5:
                mask_smooth = mask.astype(float)
            verts, faces, _, _ = marching_cubes(mask_smooth, level=0.5)
            verts_homogeneous = np.column_stack([verts, np.ones(len(verts))])
            verts_world = (affine @ verts_homogeneous.T).T[:, :3]
            mesh = trimesh.Trimesh(vertices=verts_world, faces=faces)
            if len(mesh.faces) > TARGET_FACES:
                mesh = mesh.simplify_quadric_decimation(face_count=TARGET_FACES)
            _ = mesh.vertex_normals  # force trimesh to compute and cache normals so GLB export includes NORMAL attribute
            mesh.export(str(glb_path), file_type="glb")
            generated += 1
        except Exception as exc:
            print(f"  Failed mesh for label {label_id}: {exc}")
            no_mesh.append(int(label_id))

        if generated > 0 and generated % 50 == 0:
            print(f"  Generated {generated} meshes...")

    # Generate parent meshes by merging descendant voxel masks.
    # A node is a "leaf-in-volume" iff its ID appears in the NIfTI; everything
    # else (synthetic D99 categories and native CHARM/MEBRAINS parents alike)
    # needs a merged mesh from the union of its descendant leaves.
    children_map = {}
    for node_id, node in id_to_structure.items():
        pid = node.get("parent_structure_id")
        if pid is not None:
            children_map.setdefault(pid, []).append(node_id)

    parent_to_leaves = {}
    for node_id, node in id_to_structure.items():
        if node_id in (ROOT_ID, OUTSIDE_ID):
            continue
        if node_id in unique_labels:
            continue  # has own voxels, already handled by the per-region pass
        # Collect leaf-in-volume descendants
        stack = list(children_map.get(node_id, []))
        leaves = []
        while stack:
            cid = stack.pop()
            if cid in unique_labels:
                leaves.append(cid)
            stack.extend(children_map.get(cid, []))
        if leaves:
            parent_to_leaves[node_id] = leaves

    parent_generated = 0
    for parent_id, leaf_ids in sorted(parent_to_leaves.items()):
        glb_path = meshes_dir / f"{parent_id}.glb"
        if glb_path.exists():
            skipped_existing += 1
            continue

        # Merge all child voxel masks
        merged_mask = np.zeros_like(atlas_data, dtype=bool)
        for leaf_id in leaf_ids:
            merged_mask |= (atlas_data == leaf_id)

        voxel_count = np.sum(merged_mask)
        if voxel_count < MIN_VOXELS:
            skipped_small += 1
            no_mesh.append(parent_id)
            continue

        try:
            merged_smooth = gaussian_filter(merged_mask.astype(float), sigma=1.0)
            # Guard against very thin/sparse merged masks whose peak drops
            # below 0.5 after blurring (would fail marching_cubes).
            if merged_smooth.max() <= 0.5:
                merged_smooth = merged_mask.astype(float)
            verts, faces, _, _ = marching_cubes(merged_smooth, level=0.5)
            verts_homogeneous = np.column_stack([verts, np.ones(len(verts))])
            verts_world = (affine @ verts_homogeneous.T).T[:, :3]
            mesh = trimesh.Trimesh(vertices=verts_world, faces=faces)
            if len(mesh.faces) > TARGET_FACES:
                mesh = mesh.simplify_quadric_decimation(face_count=TARGET_FACES)
            _ = mesh.vertex_normals  # force trimesh to compute and cache normals so GLB export includes NORMAL attribute
            mesh.export(str(glb_path), file_type="glb")
            parent_generated += 1
        except Exception as exc:
            print(f"  Failed parent mesh for {parent_id}: {exc}")
            no_mesh.append(parent_id)

    print(f"  Parent meshes generated: {parent_generated}")

    # Only OUTSIDE_ID goes to no_mesh (category nodes now have meshes)
    no_mesh.append(OUTSIDE_ID)
    # Add any category nodes that still have no mesh file
    for node_id in id_to_structure:
        if node_id >= CATEGORY_ID_START:
            if not (meshes_dir / f"{node_id}.glb").exists():
                no_mesh.append(node_id)

    print(
        f"Meshes: {generated} leaf + {parent_generated} parent generated, "
        f"{skipped_existing} existing, {skipped_small} too small, "
        f"{len(no_mesh)} no mesh"
    )
    return sorted(set(no_mesh))


# ---------------------------------------------------------------------------
# Mesh generation from upstream GIFTI surfaces
# ---------------------------------------------------------------------------


def load_gifti_mesh(gii_path):
    """Load a GIFTI surface and return (vertices, faces) as numpy arrays.

    GIFTI surfaces have POINTSET (vertices) and TRIANGLE (faces) darrays.
    Returns vertices shape (N, 3) float32, faces shape (M, 3) int32.
    """
    import nibabel as nib
    g = nib.load(str(gii_path))
    verts = None
    faces = None
    for darray in g.darrays:
        intent = darray.intent
        if intent == "NIFTI_INTENT_POINTSET" or getattr(darray, "intent", None) == 1008:
            verts = darray.data
        elif intent == "NIFTI_INTENT_TRIANGLE" or getattr(darray, "intent", None) == 1009:
            faces = darray.data
    if verts is None or faces is None:
        raise ValueError(f"Could not find POINTSET+TRIANGLE in {gii_path}")
    return verts.astype("float32"), faces.astype("int32")


def _apply_world_flip(verts):
    """Identity pass-through (formerly applied an X-axis flip).

    Kept as a stub for back-compat with any caller that still imports it.
    Historically this mirrored vertex X coordinates so GIFTI surfaces matched
    the X-flipped marching-cubes output. Both flips have been removed — the
    pipeline now writes vertices in atlas-native RAS+ mm, faithful to the
    source NIFTI/GIFTI convention. New code should not call this.
    """
    return verts.copy()


def _normalize_winding_outward(verts, faces, sample_size=200):
    """Return `faces` with winding ensuring normals point outward on average.

    Some upstream GIFTI distributions (notably RheMAP-Surf for D99) ship LH
    and RH pial surfaces with opposite winding conventions — LH with normals
    pointing inward and RH outward (or vice versa). Rendering with FrontSide
    culling produces a visible hemisphere on one side and a chunky
    inside-out shell on the other. We check the sign of the average dot
    product between face normals and the outward direction (face centroid
    minus mesh centroid), and flip winding if negative.
    """
    import random
    if len(faces) == 0:
        return faces
    centroid = verts.mean(axis=0)
    n = min(sample_size, len(faces))
    rng = random.Random(0)
    idx = rng.sample(range(len(faces)), n)
    dots = []
    for i in idx:
        fi = faces[i]
        v0, v1, v2 = verts[fi[0]], verts[fi[1]], verts[fi[2]]
        normal = np.cross(v1 - v0, v2 - v0)
        nrm = np.linalg.norm(normal)
        if nrm < 1e-12:
            continue
        normal = normal / nrm
        fc = (v0 + v1 + v2) / 3
        outward = fc - centroid
        onrm = np.linalg.norm(outward)
        if onrm < 1e-12:
            continue
        dots.append(float(np.dot(normal, outward / onrm)))
    if not dots:
        return faces
    if sum(dots) / len(dots) < 0:
        return faces[:, ::-1].copy()
    return faces


def _mirror_mesh_x(verts, faces):
    """Mirror a mesh across X=0 and flip face winding to preserve front-facing
    normals.

    Returns (mirrored_verts, mirrored_faces) with the same shapes as input.
    """
    mirrored_verts = verts.copy()
    mirrored_verts[:, 0] *= -1
    mirrored_faces = faces[:, ::-1].copy()
    return mirrored_verts, mirrored_faces


def _build_trimesh_from_arrays(verts, faces, *, merge_duplicates=False):
    """Construct a trimesh.Trimesh from vertex/face arrays, force-cache
    vertex normals, and return the mesh.

    Vertices are passed through as-is (atlas-native RAS+ mm). Face winding is
    preserved from the source — _normalize_winding_outward downstream catches
    any inconsistent-winding GIFTI distributions.

    When merging several surfaces (e.g. D99 root from 730 pieces), set
    merge_duplicates=True to weld coincident vertices so adjacency information
    is continuous. That helps quadric decimation behave sensibly and yields
    smoother dihedral distributions. For single-region meshes this is a
    no-op since the upstream triangulation is already welded.
    """
    import trimesh
    mesh = trimesh.Trimesh(
        vertices=verts.copy(), faces=faces.copy(),
        process=merge_duplicates,
    )
    _ = mesh.vertex_normals  # force trimesh to compute and cache normals
    return mesh


def _find_charm_surface(charm_levels_dir, level, label_id, pattern):
    """Find the surface file for a CHARM label at a specific level.

    Returns the Path if found, None otherwise.
    """
    level_dir = Path(charm_levels_dir) / f"Level_{level}"
    if not level_dir.exists():
        return None
    for entry in level_dir.iterdir():
        if not entry.name.endswith(".gii"):
            continue
        m = pattern.search(entry.name)
        if m and int(m.group(1)) == label_id:
            return entry
    return None


def _find_d99_surface(surfaces_dir, label_id, pattern):
    """Find the D99 surface file for a label ID."""
    surfaces_dir = Path(surfaces_dir)
    if not surfaces_dir.exists():
        return None
    for entry in surfaces_dir.iterdir():
        if not entry.name.endswith(".gii"):
            continue
        m = pattern.search(entry.name)
        if m and int(m.group(1)) == label_id:
            return entry
    return None


def _export_gifti_glb(
    glb_path, verts, faces, *, mirror=False, target_faces=TARGET_FACES_GIFTI,
):
    """Load GIFTI arrays into a trimesh, optionally mirror across X=0,
    decimate to target_faces, bake normals, and export as GLB.

    Vertices stay in atlas-native RAS+ mm (no world-flip).

    Returns the trimesh.Trimesh for introspection (e.g. face count).
    """
    import trimesh

    if mirror:
        mverts, mfaces = _mirror_mesh_x(verts, faces)
        # Offset indices so we can concatenate into one vertex buffer.
        mfaces_shifted = mfaces + len(verts)
        combined_verts = np.vstack([verts, mverts])
        combined_faces = np.vstack([faces, mfaces_shifted])
    else:
        combined_verts = verts
        combined_faces = faces

    mesh = _build_trimesh_from_arrays(combined_verts, combined_faces)

    if len(mesh.faces) > target_faces:
        mesh = mesh.simplify_quadric_decimation(face_count=target_faces)
        _ = mesh.vertex_normals  # re-cache after decimation

    mesh.export(str(glb_path), file_type="glb")
    return mesh


def _export_merged_gifti_glb(
    glb_path, source_paths, *, mirror=False, target_faces=TARGET_FACES_GIFTI,
    merge_duplicates=False,
):
    """Load and merge multiple GIFTI surfaces into a single GLB.

    Each surface is optionally mirrored across X=0 (e.g. D99's right-only
    surfs) before concatenation. After merging we decimate to target_faces,
    bake normals, and export. Vertices stay in atlas-native RAS+ mm.
    """
    import trimesh

    pieces_verts = []
    pieces_faces = []
    offset = 0
    for path in source_paths:
        verts, faces = load_gifti_mesh(path)
        # Normalize per-piece winding so all pieces have outward normals.
        # RheMAP-Surf D99 pials ship LH inward / RH outward; without this
        # one hemisphere renders inside-out under FrontSide culling.
        faces = _normalize_winding_outward(verts, faces)
        if mirror:
            mverts, mfaces = _mirror_mesh_x(verts, faces)
            pieces_verts.append(verts)
            pieces_faces.append(faces + offset)
            offset += len(verts)
            pieces_verts.append(mverts)
            pieces_faces.append(mfaces + offset)
            offset += len(mverts)
        else:
            pieces_verts.append(verts)
            pieces_faces.append(faces + offset)
            offset += len(verts)

    if not pieces_verts:
        return None

    combined_verts = np.vstack(pieces_verts)
    combined_faces = np.vstack(pieces_faces)
    mesh = _build_trimesh_from_arrays(
        combined_verts, combined_faces, merge_duplicates=merge_duplicates,
    )

    if len(mesh.faces) > target_faces:
        mesh = mesh.simplify_quadric_decimation(face_count=target_faces)
        _ = mesh.vertex_normals

    mesh.export(str(glb_path), file_type="glb")
    return mesh


def generate_meshes_from_gifti(
    atlas, nifti_file, meshes_dir, id_to_structure, gifti_config, charm_entries=None,
    template_nifti=None,
):
    """Generate GLB meshes from upstream GIFTI surfaces.

    Parameters
    ----------
    atlas : str
        Atlas key ("d99" or "nmt"). Controls which surface-lookup strategy to
        use.
    nifti_file : Path
        Parcellation NIfTI. Only used to enumerate valid label IDs for D99 and
        to mark labels with no mesh source; the mesh geometry itself comes
        from the GIFTIs.
    meshes_dir : Path
        Output directory for GLB files.
    id_to_structure : dict
        Full structure graph; used to identify synthetic parents that need
        merged meshes (e.g. D99 category nodes).
    gifti_config : dict
        Per-atlas surface configuration from ATLAS_CONFIGS.
    charm_entries : list or None
        CHARM label entries (with `level` metadata) for NMT. Required for NMT
        to look up the right surface per label.
    template_nifti : Path or None
        Optional T1 template volume. If `gifti_config` does not provide a
        whole-brain surface (e.g. D99 has only per-region surfaces), the root
        mesh is generated by marching cubes on this template instead of
        unioning region surfaces. This yields a watertight outline whose
        Euler number is ~4 rather than thousands.

    Returns
    -------
    list[int]
        Label IDs that have no mesh file.
    """
    import nibabel as nib

    meshes_dir.mkdir(parents=True, exist_ok=True)

    img = nib.load(str(nifti_file))
    atlas_data = np.asarray(img.dataobj, dtype=np.int16)
    unique_labels = set(int(v) for v in np.unique(atlas_data)) - {0}

    pattern = gifti_config["filename_pattern"]
    mirror = gifti_config.get("mirror_hemisphere", False)

    no_mesh = []
    generated = 0
    skipped_existing = 0
    failed = 0

    # --- Root mesh (whole brain) --------------------------------------------
    root_glb = meshes_dir / f"{ROOT_ID}.glb"
    if not root_glb.exists():
        lh = gifti_config.get("whole_brain_lh")
        rh = gifti_config.get("whole_brain_rh")
        if lh is not None and rh is not None and Path(lh).exists() and Path(rh).exists():
            # Prefer an upstream whole-brain surface pair (NMT: gray_surface,
            # D99: RheMAP-Surf pial). Watertight, high-detail, no marching
            # cubes involved.
            print(f"Generating {atlas} root mesh from {Path(lh).name} + {Path(rh).name}")
            mesh = _export_merged_gifti_glb(
                root_glb, [lh, rh], mirror=False,
                target_faces=ROOT_TARGET_FACES_GIFTI,
            )
            if mesh is not None:
                print(f"  Root mesh: {len(mesh.faces)} faces")
                generated += 1
        elif atlas == "d99":
            # Fallback only if the pial cache is missing (offline build).
            if template_nifti is not None:
                print(
                    f"Generating D99 root mesh from template: {template_nifti.name}"
                )
                mesh = _export_template_root_glb(
                    template_nifti, root_glb, ROOT_TARGET_FACES_GIFTI,
                )
                if mesh is not None:
                    print(f"  Root mesh: {len(mesh.faces)} faces")
                    generated += 1
            else:
                print("Generating D99 root mesh from union of all region surfaces")
                sources = []
                for label_id in sorted(unique_labels):
                    p = _find_d99_surface(
                        gifti_config["surfaces_dir"], label_id, pattern,
                    )
                    if p is not None:
                        sources.append(p)
                if sources:
                    mesh = _export_merged_gifti_glb(
                        root_glb, sources, mirror=mirror,
                        target_faces=ROOT_TARGET_FACES_GIFTI,
                        merge_duplicates=True,
                    )
                    if mesh is not None:
                        print(f"  Root mesh: {len(mesh.faces)} faces from {len(sources)} sources")
                        generated += 1
                else:
                    print("  WARNING: no D99 surfaces found for root mesh")
                    no_mesh.append(ROOT_ID)
    else:
        skipped_existing += 1

    # --- Per-region leaf meshes ---------------------------------------------
    if atlas == "d99":
        # Every entry in `id_to_structure` whose ID is a D99 label should have
        # a corresponding surface file. Synthetic category / subcategory nodes
        # (IDs >= CATEGORY_ID_START) do not and are merged below.
        for label_id, node in sorted(id_to_structure.items()):
            if label_id in (ROOT_ID, OUTSIDE_ID):
                continue
            if label_id >= CATEGORY_ID_START:
                continue  # synthetic parent; handled via merge pass
            glb_path = meshes_dir / f"{label_id}.glb"
            if glb_path.exists():
                skipped_existing += 1
                continue
            source = _find_d99_surface(
                gifti_config["surfaces_dir"], label_id, pattern,
            )
            if source is None:
                no_mesh.append(label_id)
                continue
            try:
                verts, faces = load_gifti_mesh(source)
                _export_gifti_glb(
                    glb_path, verts, faces, mirror=mirror,
                    target_faces=TARGET_FACES_GIFTI,
                )
                generated += 1
                if generated % 50 == 0:
                    print(f"  Generated {generated} meshes...")
            except Exception as exc:
                print(f"  Failed mesh for label {label_id}: {exc}")
                failed += 1
                no_mesh.append(label_id)

    elif atlas == "nmt":
        if charm_entries is None:
            raise ValueError("charm_entries required for NMT GIFTI mesh generation")
        # Each CHARM entry carries its own native hierarchy level. Surfaces at
        # level N live under `atlases/CHARM/Level_N/`.
        charm_levels_dir = gifti_config["charm_levels_dir"]
        for entry in sorted(charm_entries, key=lambda e: e["index"]):
            label_id = entry["index"]
            level = entry["level"]
            glb_path = meshes_dir / f"{label_id}.glb"
            if glb_path.exists():
                skipped_existing += 1
                continue
            source = _find_charm_surface(
                charm_levels_dir, level, label_id, pattern,
            )
            if source is None:
                no_mesh.append(label_id)
                continue
            try:
                verts, faces = load_gifti_mesh(source)
                _export_gifti_glb(
                    glb_path, verts, faces, mirror=mirror,
                    target_faces=TARGET_FACES_GIFTI,
                )
                generated += 1
                if generated % 50 == 0:
                    print(f"  Generated {generated} meshes...")
            except Exception as exc:
                print(f"  Failed mesh for label {label_id}: {exc}")
                failed += 1
                no_mesh.append(label_id)

    # --- Synthetic parent meshes --------------------------------------------
    # Nodes that aren't backed by a surface file need their mesh merged from
    # descendant leaves. For D99 these are the synthetic category/subcategory
    # nodes (IDs >= CATEGORY_ID_START). For NMT/CHARM the hierarchy nodes with
    # surfaces are already handled; only the rare entry missing a surface (the
    # 8 hippocampal subdivisions) falls through here.
    children_map = {}
    for node_id, node in id_to_structure.items():
        pid = node.get("parent_structure_id")
        if pid is not None:
            children_map.setdefault(pid, []).append(node_id)

    def _descendant_leaves_with_meshes(parent_id):
        leaves = []
        stack = list(children_map.get(parent_id, []))
        while stack:
            cid = stack.pop()
            cid_glb = meshes_dir / f"{cid}.glb"
            if cid_glb.exists() and cid >= 0:
                # Any descendant with an on-disk mesh counts as a merge source.
                # We still recurse so that deeper subdivisions also contribute
                # when a parent node has its own mesh (rare but possible).
                leaves.append(cid_glb)
            stack.extend(children_map.get(cid, []))
        return leaves

    parent_generated = 0
    for node_id, node in sorted(id_to_structure.items()):
        if node_id in (ROOT_ID, OUTSIDE_ID):
            continue
        glb_path = meshes_dir / f"{node_id}.glb"
        if glb_path.exists():
            continue
        # Only merge-build synthetic parents that don't have direct surfaces.
        # (For CHARM, label IDs without a surface file would also land here;
        # they inherit from their descendants.)
        leaf_glbs = _descendant_leaves_with_meshes(node_id)
        if not leaf_glbs:
            no_mesh.append(node_id)
            continue
        try:
            import trimesh
            pieces = [trimesh.load(str(p), force="mesh") for p in leaf_glbs]
            merged = trimesh.util.concatenate(pieces)
            if len(merged.faces) > TARGET_FACES_GIFTI:
                merged = merged.simplify_quadric_decimation(
                    face_count=TARGET_FACES_GIFTI,
                )
            _ = merged.vertex_normals
            merged.export(str(glb_path), file_type="glb")
            parent_generated += 1
        except Exception as exc:
            print(f"  Failed parent mesh for {node_id}: {exc}")
            no_mesh.append(node_id)

    print(f"  Parent meshes generated: {parent_generated}")

    # OUTSIDE_ID never has a mesh; category nodes without a file also go here.
    no_mesh.append(OUTSIDE_ID)
    for node_id in id_to_structure:
        if node_id >= CATEGORY_ID_START:
            if not (meshes_dir / f"{node_id}.glb").exists():
                no_mesh.append(node_id)

    print(
        f"Meshes: {generated} leaf + {parent_generated} parent generated, "
        f"{skipped_existing} existing, {failed} failed, {len(set(no_mesh))} no mesh"
    )
    return sorted(set(no_mesh))


# ---------------------------------------------------------------------------
# Coordinate extraction from NWB files
# ---------------------------------------------------------------------------


def _read_hdf5_strings(group, key):
    """Read a string column from an HDF5 group, returning a list of strings."""
    if key not in group:
        return []
    raw = group[key][()]
    if hasattr(raw, "__iter__") and not isinstance(raw, (str, bytes)):
        return [v.decode("utf-8") if isinstance(v, bytes) else str(v) for v in raw]
    if isinstance(raw, bytes):
        return [raw.decode("utf-8")]
    return [str(raw)]


def extract_atlas_coords(url, asset_id, hdf5_path):
    """Extract atlas coordinates from the localization extension in an NWB file.

    Returns dict with keys: asset_id, coords, brain_region, brain_region_id,
    raw_coords (original coords before X negation), or None if not found.
    """
    import h5py
    import remfile

    rf = remfile.File(url)
    with h5py.File(rf, "r") as f:
        if hdf5_path not in f:
            return None

        t = f[hdf5_path]
        if not all(col in t for col in ("x", "y", "z")):
            return None

        x = t["x"][()]
        y = t["y"][()]
        z = t["z"][()]

        # Read unit attributes from HDF5 columns
        raw_units = []
        for col in ("x", "y", "z"):
            u = t[col].attrs.get("unit", None)
            if isinstance(u, bytes):
                u = u.decode("utf-8")
            raw_units.append(u)

        if raw_units[0] is not None and len(set(raw_units)) == 1:
            source_unit_raw = raw_units[0]
        else:
            source_unit_raw = None

        brain_region = _read_hdf5_strings(t, "brain_region")
        brain_region_id = _read_hdf5_strings(t, "brain_region_id")

    # Resolve unit alias and compute conversion factor
    source_unit = None
    scale = 1.0
    if source_unit_raw is not None:
        canonical = UNIT_ALIASES.get(source_unit_raw)
        if canonical is not None:
            source_unit = source_unit_raw
            scale = UNIT_TO_MM[canonical]
            if canonical != "mm":
                print(f"  [{asset_id}] Converting coordinates from {source_unit_raw} to mm (factor={scale})")
        else:
            print(f"  [{asset_id}] Unknown unit '{source_unit_raw}', assuming mm")

    coords = []
    raw_coords = []
    for xi, yi, zi in zip(x, y, z):
        xi, yi, zi = float(xi) * scale, float(yi) * scale, float(zi) * scale
        if xi != xi or yi != yi or zi != zi:
            continue
        # Coords are stored as-read from the NWB AnatomicalCoordinatesTable,
        # which the conversion side guarantees is in atlas-native RAS+ mm.
        # raw_coords is kept identical for back-compat with cache entries that
        # carried both fields.
        raw_coords.append([round(xi, 3), round(yi, 3), round(zi, 3)])
        coords.append([round(xi, 3), round(yi, 3), round(zi, 3)])

    if not coords:
        return None

    return {
        "asset_id": asset_id,
        "coords": coords,
        "raw_coords": raw_coords,
        "brain_region": brain_region,
        "brain_region_id": brain_region_id,
    }


# ---------------------------------------------------------------------------
# DANDI data extraction
# ---------------------------------------------------------------------------


def _load_cache(cache_file):
    """Load cached electrode data from JSONL file."""
    cache = {}
    if cache_file.exists():
        with open(cache_file, "r") as f:
            for line in f:
                entry = json.loads(line)
                cache[entry["asset_id"]] = entry
    return cache


def _append_cache(cache_file, entry):
    """Append a single entry to the cache file."""
    with open(cache_file, "a") as f:
        f.write(json.dumps(entry) + "\n")


def _map_regions_by_abbreviation(brain_region_ids, abbrev_to_id, id_to_structure):
    """Map brain_region_id values to structure graph regions.

    Accepts mixed input types (str, bytes, int) and a small set of sentinels
    (empty, None, "0", "outside" case-insensitive) which all map to OUTSIDE_ID.
    De-duplicates repeat labels within a single call.

    Returns a tuple (regions, unmatched), where unmatched is a list of the raw
    label values (strings) that didn't resolve to any structure in the atlas.
    Mirrors the per-asset `unmatched_locations` capture that the Allen pipeline
    does in process_asset_locations (scripts/update_data.py).
    """
    regions = []
    seen = set()
    unmatched = []
    unmatched_seen = set()
    for raw in brain_region_ids:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        key = None if raw is None else str(raw).strip()
        if not key or key == "0" or key.lower() == "outside":
            if OUTSIDE_ID in seen:
                continue
            seen.add(OUTSIDE_ID)
            regions.append({
                "id": OUTSIDE_ID,
                "acronym": "outside",
                "name": "Outside atlas",
            })
            continue
        label_id = abbrev_to_id.get(key)
        if label_id is None:
            try:
                label_id = abbrev_to_id.get(str(int(key)))
            except (TypeError, ValueError):
                pass
        if label_id is not None and label_id in id_to_structure:
            if label_id not in seen:
                seen.add(label_id)
                s = id_to_structure[label_id]
                regions.append({
                    "id": label_id,
                    "acronym": s["acronym"],
                    "name": s["name"],
                })
        else:
            if key not in unmatched_seen:
                unmatched_seen.add(key)
                unmatched.append(key)
    return regions, unmatched


def fetch_dandi_data(
    config, abbrev_to_id, id_to_structure, parent_map,
):
    """Fetch electrode data from DANDI 001636 and build all output files.

    Region mapping is abbreviation-primary: each electrode's brain_region_id
    is dict-looked-up in the structure graph. Rows with missing or unresolved
    brain_region_id contribute coordinates only — they don't add any region
    to the asset's region attribution. This matches the file-level aggregation
    model used by the Allen CCF pipeline (see scripts/update_data.py).
    """
    hdf5_path = config["hdf5_path"]
    cache_file = config["cache_file"]
    data_dir = config["output_dir"]
    electrodes_dir = data_dir / "electrodes"

    print(f"Fetching assets from dandiset {DANDISET_ID}...")
    assets = list(get_nwb_assets_paged(DANDISET_ID))
    print(f"  Found {len(assets)} NWB assets")

    cache = _load_cache(cache_file)
    print(f"  Cache has {len(cache)} entries")

    results = {}
    to_process = []
    for asset in assets:
        asset_id = asset["asset_id"]
        if asset_id in cache:
            results[asset_id] = cache[asset_id]
        else:
            to_process.append(asset)

    print(f"  Need to fetch {len(to_process)} new assets")

    def _process_one(asset):
        asset_id = asset["asset_id"]
        url = get_download_url(DANDISET_ID, asset_id)
        try:
            result = extract_atlas_coords(url, asset_id, hdf5_path)
            if result is None:
                result = {
                    "asset_id": asset_id,
                    "coords": None,
                    "raw_coords": None,
                    "brain_region": [],
                    "brain_region_id": [],
                }
            return result
        except Exception as exc:
            print(f"  Error processing {asset['path']}: {exc}")
            return {
                "asset_id": asset_id,
                "coords": None,
                "raw_coords": None,
                "brain_region": [],
                "brain_region_id": [],
                "error": str(exc),
            }

    if to_process:
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {pool.submit(_process_one, a): a for a in to_process}
            done = 0
            for future in as_completed(futures):
                result = future.result()
                asset_id = result["asset_id"]
                results[asset_id] = result
                _append_cache(cache_file, result)
                done += 1
                if done % 50 == 0:
                    print(f"  Processed {done}/{len(to_process)} assets")

    # Build output data
    dandiset_assets = {DANDISET_ID: []}
    electrodes_data = {}
    has_electrodes = False

    skipped_no_loc = 0
    for asset in assets:
        asset_id = asset["asset_id"]
        path = asset["path"]
        result = results.get(asset_id, {})

        if result.get("coords") is None and not result.get("brain_region_id"):
            skipped_no_loc += 1
            continue

        # Map electrodes to regions via abbreviation lookup against the
        # structure graph. Rows with missing or unresolved brain_region_id
        # contribute nothing to the asset's region set (file-level
        # aggregation: same forgiveness as Allen's location-string matching),
        # but the unresolved labels are captured per asset for visibility,
        # parallel to Allen's `unmatched_locations` field.
        brain_ids = result.get("brain_region_id") or []
        if brain_ids:
            regions, unmatched_brain_region_ids = _map_regions_by_abbreviation(
                brain_ids, abbrev_to_id, id_to_structure,
            )
        else:
            regions = []
            unmatched_brain_region_ids = []

        session = extract_session(path)

        dandiset_assets[DANDISET_ID].append({
            "path": path,
            "asset_id": asset_id,
            "regions": regions,
            "unmatched_brain_region_ids": unmatched_brain_region_ids,
            "session": session,
        })

        coords = result.get("coords")
        if coords:
            electrodes_data[asset_id] = coords
            has_electrodes = True

    if skipped_no_loc:
        print(f"  Skipped {skipped_no_loc} assets without localization")

    electrodes_dir.mkdir(parents=True, exist_ok=True)
    if electrodes_data:
        with open(electrodes_dir / f"{DANDISET_ID}.json", "w") as f:
            json.dump(electrodes_data, f)

    dandisets_with_electrodes = [DANDISET_ID] if has_electrodes else []
    dandi_regions = build_dandi_regions(dandiset_assets, id_to_structure, parent_map)

    return dandiset_assets, dandisets_with_electrodes, dandi_regions


# ---------------------------------------------------------------------------
# Convenience: build the in-memory atlas structure graph
# ---------------------------------------------------------------------------


def build_atlas_graph(atlas_key):
    """Parse this atlas's labels and return its in-memory structure graph.

    Returns
    -------
    (config, tree, id_to_structure, parent_map, abbrev_to_id, entries)
        `entries` is the parsed label list (D99 / CHARM / MEBRAINS shape) so
        callers that need it for mesh generation (e.g. NMT requires CHARM
        entries to look up per-level surfaces) get it without re-parsing.
    """
    config = ATLAS_CONFIGS[atlas_key]
    labels_type = config["labels_type"]

    if labels_type == "d99":
        entries = parse_d99_labels()
    elif labels_type == "charm":
        entries = parse_charm_labels()
    else:
        entries = parse_mebrains_labels()

    if labels_type == "charm":
        tree, id_to_structure, parent_map, abbrev_to_id = build_charm_structure_graph(
            entries, root_name=config["root_name"],
        )
    elif labels_type == "mebrains":
        mebrains_colors = load_mebrains_palette_from_siibra()
        tree, id_to_structure, parent_map, abbrev_to_id = build_structure_graph(
            entries,
            root_name=config["root_name"],
            color_overrides=mebrains_colors,
            labels_type="mebrains",
        )
    else:
        tree, id_to_structure, parent_map, abbrev_to_id = build_structure_graph(
            entries,
            root_name=config["root_name"],
            labels_type=labels_type,
        )

    return config, tree, id_to_structure, parent_map, abbrev_to_id, entries
