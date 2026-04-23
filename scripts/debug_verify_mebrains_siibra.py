"""Verify the MEBRAINS structure graph uses siibra colours.

Run: uv run python scripts/debug_verify_mebrains_siibra.py
"""
import json
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPTS_DIR.parent
sys.path.insert(0, str(SCRIPTS_DIR))

structure_path = PROJECT_ROOT / "data/atlases/mebrains/structure_graph.json"
struct = json.loads(structure_path.read_text())

# Flatten the tree into id -> node.
id_to_node = {}


def walk(node):
    id_to_node[node["id"]] = node
    for child in node.get("children", []):
        walk(child)


for root in struct:
    walk(root)

expected = {
    301:  ("4a - left hemisphere",  "DE26E2"),
    303:  ("4p - left hemisphere",  "5220E7"),
    313:  ("F6 - left hemisphere",  "CE7D3A"),
    1301: ("4a - right hemisphere", "DE26E2"),
    1303: ("4p - right hemisphere", "5220E7"),
    1313: ("F6 - right hemisphere", "CE7D3A"),
}

print(f"{'label':>6}  {'name':<28}  {'expected':<8}  {'actual':<8}  match")
print("-" * 70)
all_match = True
for label, (_expected_name, expected_hex) in expected.items():
    node = id_to_node.get(label)
    if node is None:
        print(f"{label:>6}  (missing node)")
        all_match = False
        continue
    actual = node["color_hex_triplet"]
    match = actual.upper() == expected_hex.upper()
    print(
        f"{label:>6}  {node['name']:<28}  {expected_hex:<8}  {actual:<8}  "
        f"{'YES' if match else 'NO'}"
    )
    if not match:
        all_match = False

print()
print("All six match expected siibra values:", all_match)

# Coverage stat
cache_path = SCRIPTS_DIR / "siibra_mebrains_palette.json"
cache = json.loads(cache_path.read_text())
siibra_ids = {int(k) for k in cache.keys()}

from build_macaque_atlas import parse_mebrains_labels  # noqa: E402

entries = parse_mebrains_labels()
mebrains_label_ids = {e["index"] for e in entries}

covered = mebrains_label_ids & siibra_ids
uncovered = mebrains_label_ids - siibra_ids
print(
    f"Siibra coverage: {len(covered)} of {len(mebrains_label_ids)} MEBRAINS "
    f"labels ({len(uncovered)} fell back to fabricated)"
)
if uncovered:
    print("Uncovered label IDs:", sorted(uncovered))
