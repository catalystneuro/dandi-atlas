#!/usr/bin/env python3
"""Update WHS-SD rat DANDI data files.

Refreshes the DANDI-derived JSON files for the rat (WHS-SD) atlas without
needing the local WHS-SD MBAT source files (parcellation NIfTI, ILF hierarchy,
T2* template) that build_rat_atlas.py requires. Works in CI runners that only
have the repo checked out.

The structure tree is reconstructed from the already-committed
data/atlases/whs_sd/structure_graph.json. Location strings are streamed from
DANDI via HTTP (same path as build_rat_atlas.py's fetch_rat_dandi_data and
fetch_rat_dandi_sweep). Embargoed dandisets (EMBARGOED_DANDISETS, e.g. 001699)
are not reachable from CI, so their previously-committed records are preserved
verbatim from the existing dandiset_assets.json.

Files written:
  - dandiset_assets.json
  - dandisets_with_electrodes.json
  - dandi_regions.json
  - mesh_manifest.json       (only the data_structures and ancestor_structures
                              fields; all_meshes, no_mesh, root_id preserved)

Files NOT written:
  - structure_graph.json     (read-only here; comes from local WHS-SD source via
                              build_rat_atlas.py)
  - meshes/*.glb             (atlas-source-derived; built by build_rat_atlas.py)
  - uberon_mapping.json      (read-only here; generated separately)

Usage:
    python scripts/update_rat_data.py
    python scripts/update_rat_data.py --mode full
    python scripts/update_rat_data.py --skip-sweep
"""

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from dandi_helpers import build_dandi_regions, build_parent_map, get_ancestors
from macaque_atlas_lib import OUTSIDE_ID, ROOT_ID, _normalize_region_name
from rat_atlas_lib import (
    ATLAS_CONFIGS,
    DANDISET_IDS,
    EMBARGOED_DANDISETS,
    fetch_rat_dandi_data,
    fetch_rat_dandi_sweep,
)


PROJECT_ROOT = Path(__file__).resolve().parent.parent
LAST_UPDATED_FILE = PROJECT_ROOT / "data" / "last_updated.json"
ATLAS_KEY = "whs_sd"


def reconstruct_lookups_from_graph(structure_graph):
    """Walk the saved structure_graph.json and rebuild the lookups that
    fetch_rat_dandi_data needs (id_to_structure, abbrev_to_id, name_to_id).

    Mirrors update_macaque_data.reconstruct_lookups_from_graph but tracks the
    rat-specific shape (acronym and full name both populate name_to_id when
    distinct, since the WHS-SD ILF often uses the same string for both).
    """
    id_to_structure = {}
    abbrev_to_id = {}
    name_to_id = {}

    def walk(node):
        node_id = node["id"]
        record = {k: v for k, v in node.items() if k != "children"}
        id_to_structure[node_id] = record
        acronym = node.get("acronym")
        if acronym and acronym not in abbrev_to_id:
            abbrev_to_id[acronym] = node_id
        abbrev_to_id.setdefault(str(node_id), node_id)
        normalized_name = _normalize_region_name(node.get("name"))
        if normalized_name and normalized_name not in name_to_id:
            name_to_id[normalized_name] = node_id
        for child in node.get("children", []):
            walk(child)

    if isinstance(structure_graph, list):
        for node in structure_graph:
            walk(node)
    else:
        walk(structure_graph)

    return id_to_structure, abbrev_to_id, name_to_id


def load_uberon_lookups(data_dir):
    """Build reverse lookups from data/atlases/whs_sd/uberon_mapping.json.

    Same logic as build_rat_atlas._load_uberon_lookups — kept here to avoid
    importing a leading-underscore private from the build script.
    """
    mapping_path = data_dir / "uberon_mapping.json"
    if not mapping_path.exists():
        print(f"  No UBERON mapping at {mapping_path}; resolver will skip UBERON step")
        return {}, {}
    with open(mapping_path) as f:
        mapping = json.load(f)
    label_to_id = {}
    curie_to_id = {}
    for row in mapping.values():
        whs_id = row["whs_id"]
        curie = row.get("uberon_id")
        label = row.get("uberon_label")
        if curie:
            curie_to_id[curie.upper()] = whs_id
        if label:
            normalized = _normalize_region_name(label)
            if normalized:
                label_to_id[normalized] = whs_id
    print(f"  Loaded UBERON mapping: {len(curie_to_id)} CURIEs, {len(label_to_id)} labels")
    return label_to_id, curie_to_id


def update_mesh_manifest(data_dir, data_structures, parent_map):
    """Update only the DANDI-derived fields of mesh_manifest.json.

    Preserves all_meshes, no_mesh, root_id (those come from the atlas-source
    build path). Updates data_structures (= regions any current asset
    references) and ancestor_structures (= union of ancestors of those regions).
    """
    manifest_path = data_dir / "mesh_manifest.json"
    with open(manifest_path) as f:
        manifest = json.load(f)

    data_ids = set(data_structures)
    ancestor_ids = set()
    for structure_id in data_ids:
        for ancestor in get_ancestors(structure_id, parent_map):
            ancestor_ids.add(ancestor)

    manifest["data_structures"] = sorted(data_ids)
    manifest["ancestor_structures"] = sorted(ancestor_ids - data_ids)

    with open(manifest_path, "w") as f:
        json.dump(manifest, f)


def write_last_updated(mode, asset_count):
    """Append/update last_updated.json with this atlas's refresh timestamp."""
    record = {}
    if LAST_UPDATED_FILE.exists():
        record = json.load(open(LAST_UPDATED_FILE))

    record["timestamp"] = datetime.now(timezone.utc).isoformat()
    record["mode"] = mode
    per_atlas = record.get("per_atlas", {})
    per_atlas[ATLAS_KEY] = {
        "timestamp": record["timestamp"],
        "mode": mode,
        "asset_count": asset_count,
    }
    record["per_atlas"] = per_atlas

    LAST_UPDATED_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LAST_UPDATED_FILE, "w") as f:
        json.dump(record, f, indent=2)


def load_existing_embargoed_records(data_dir):
    """Read the previously-committed dandiset_assets.json and return only
    entries for EMBARGOED_DANDISETS. CI cannot stream embargoed assets, so we
    preserve whatever was last committed by a local build_rat_atlas.py run.
    """
    assets_path = data_dir / "dandiset_assets.json"
    if not assets_path.exists():
        return {}
    with open(assets_path) as f:
        previous = json.load(f)
    return {
        dandiset_id: records
        for dandiset_id, records in previous.items()
        if dandiset_id in EMBARGOED_DANDISETS
    }


def update_rat(mode, skip_sweep, sweep_limit, sweep_max_assets):
    config = ATLAS_CONFIGS[ATLAS_KEY]
    data_dir = config["output_dir"]
    structure_graph_path = data_dir / "structure_graph.json"

    assert structure_graph_path.exists(), (
        f"Missing {structure_graph_path}. Run build_rat_atlas.py first to "
        "produce the structure graph from atlas source files."
    )

    print(f"Updating {ATLAS_KEY} (mode={mode})")
    print(f"  Reading structure tree from {structure_graph_path.relative_to(PROJECT_ROOT)}")
    structure_graph = json.load(open(structure_graph_path))
    id_to_structure, abbrev_to_id, name_to_id = reconstruct_lookups_from_graph(
        structure_graph
    )
    parent_map = build_parent_map(list(id_to_structure.values()))
    uberon_label_to_whs_id, uberon_id_to_whs_id = load_uberon_lookups(data_dir)

    if mode == "full":
        for cache_key in ("cache_file", "sweep_cache_file"):
            cache_file = config[cache_key]
            if cache_file.exists():
                print(f"  Full mode: clearing cache {cache_file.name}")
                cache_file.unlink()

    streaming_ids = [d for d in DANDISET_IDS if d not in EMBARGOED_DANDISETS]
    if streaming_ids:
        stream_assets, _, _ = fetch_rat_dandi_data(
            config, name_to_id, abbrev_to_id, id_to_structure, parent_map,
            uberon_label_to_whs_id, uberon_id_to_whs_id,
            dandiset_ids=streaming_ids,
        )
    else:
        stream_assets = {}

    preserved_embargoed = load_existing_embargoed_records(data_dir)
    if preserved_embargoed:
        preserved_count = sum(len(records) for records in preserved_embargoed.values())
        print(
            f"  Preserved {preserved_count} record(s) across "
            f"{len(preserved_embargoed)} embargoed dandiset(s) from previous run"
        )

    dandiset_assets = {**preserved_embargoed, **stream_assets}

    if not skip_sweep:
        print("Sweeping DANDI for additional rat dandisets...")
        sweep_assets = fetch_rat_dandi_sweep(
            config, name_to_id, abbrev_to_id, id_to_structure, parent_map,
            uberon_label_to_whs_id, uberon_id_to_whs_id,
            exclude_ids=set(dandiset_assets),
            limit=sweep_limit,
            max_assets_per_dandiset=sweep_max_assets if sweep_max_assets > 0 else None,
        )
        dandiset_assets.update(sweep_assets)
        print(f"  Sweep added {len(sweep_assets)} new dandiset(s)")

    dandi_regions = build_dandi_regions(dandiset_assets, id_to_structure, parent_map)
    dandisets_with_electrodes = []

    with open(data_dir / "dandiset_assets.json", "w") as f:
        json.dump(dandiset_assets, f)
    with open(data_dir / "dandisets_with_electrodes.json", "w") as f:
        json.dump(dandisets_with_electrodes, f)
    with open(data_dir / "dandi_regions.json", "w") as f:
        json.dump(dandi_regions, f)

    data_ids = {int(structure_id) for structure_id in dandi_regions.keys()}
    update_mesh_manifest(data_dir, data_ids, parent_map)

    asset_count = sum(len(records) for records in dandiset_assets.values())
    write_last_updated(mode, asset_count)
    print(
        f"  Wrote DANDI-derived files for {ATLAS_KEY} "
        f"({asset_count} assets across {len(dandiset_assets)} dandisets, "
        f"{len(dandi_regions)} regions)"
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        choices=["incremental", "full"],
        default="incremental",
        help="Update mode: incremental skips cached assets; full re-fetches all (default: incremental)",
    )
    parser.add_argument(
        "--skip-sweep",
        action="store_true",
        help="Skip the DANDI-wide rat sweep; only refresh the explicit DANDISET_IDS list.",
    )
    parser.add_argument(
        "--sweep-limit",
        type=int,
        default=None,
        help="Stop the sweep after this many rat dandisets have been processed.",
    )
    parser.add_argument(
        "--sweep-max-assets",
        type=int,
        default=1000,
        help="Skip swept dandisets with more than this many NWB assets (default 1000; pass 0 to disable).",
    )
    args = parser.parse_args()
    update_rat(args.mode, args.skip_sweep, args.sweep_limit, args.sweep_max_assets)


if __name__ == "__main__":
    main()
