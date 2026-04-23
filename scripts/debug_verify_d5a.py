#!/usr/bin/env python3
"""Verify D5a colour scheme in the D99 structure graph.

Ad-hoc verification script; safe to delete after reviewing output.
"""
import colorsys
import json
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPTS_DIR.parent
sys.path.insert(0, str(SCRIPTS_DIR))

from build_macaque_atlas import parse_d99_labels  # noqa: E402


def walk(node, out):
    out[node["acronym"]] = {
        "id": node["id"],
        "color": node.get("color_hex_triplet"),
        "name": node.get("name"),
    }
    for child in node.get("children", []):
        walk(child, out)


def hex_to_hsl(hx):
    r = int(hx[0:2], 16) / 255
    g = int(hx[2:4], 16) / 255
    b = int(hx[4:6], 16) / 255
    h, l, s = colorsys.rgb_to_hls(r, g, b)
    return (h * 360, s, l)


def main():
    with open(PROJECT_ROOT / "data/atlases/d99/structure_graph.json") as f:
        tree = json.load(f)
    by_abbrev = {}
    for root in tree:
        walk(root, by_abbrev)

    entries = parse_d99_labels()
    by_abbrev_entry = {e["abbreviation"]: e for e in entries}

    samples = ["F1_(4)", "3a/b", "V1", "V2", "V4", "pu", "cd", "NA"]
    print("=== D99 named samples ===")
    for abbrev in samples:
        info = by_abbrev.get(abbrev)
        entry = by_abbrev_entry.get(abbrev, {})
        if info:
            h, s, l = hex_to_hsl(info["color"])
            cat = entry.get("category", "?")
            print(
                f"  {abbrev!r:14s} id={info['id']:<4} cat={cat:20s} "
                f"color=#{info['color']} HSL=({h:6.1f}, {s:.2f}, {l:.2f})"
            )
        else:
            print(f"  {abbrev!r:14s}  NOT FOUND")

    for category, expected in [
        ("Cerebellum", "teal-green 130-170"),
        ("Thalamus", "magenta 300-340"),
        ("Basal ganglia", "warm 0-40"),
        ("Cortex", "blue 190-250"),
        ("Brainstem", "olive-yellow 40-80"),
        ("Hippocampus", "violet 260-290"),
        ("Amygdala", "red 350-10"),
    ]:
        rows = [e for e in entries if e["category"] == category][:5]
        print(f"\n=== {category} (expect {expected}) ===")
        for e in rows:
            info = by_abbrev.get(e["abbreviation"])
            if info:
                h, s, l = hex_to_hsl(info["color"])
                print(
                    f"  {e['abbreviation']!r:14s} color=#{info['color']} "
                    f"HSL=({h:6.1f}, {s:.2f}, {l:.2f})"
                )

    print("\n=== Structural nodes (should keep existing colours) ===")
    for abbrev in ["root", "outside"]:
        info = by_abbrev.get(abbrev)
        if info:
            print(f"  {abbrev!r:14s} id={info['id']} color=#{info['color']}")

    print(
        "\n=== Category synthetic nodes (CATEGORY_HUES, S=0.5 L=0.5 from existing logic) ==="
    )
    for cat in [
        "Cortex",
        "Basal_ganglia",
        "Thalamus",
        "Cerebellum",
        "Amygdala",
        "Hippocampus",
        "Brainstem",
    ]:
        info = by_abbrev.get(cat)
        if info:
            h, s, l = hex_to_hsl(info["color"])
            print(
                f"  {cat!r:20s} id={info['id']} color=#{info['color']} "
                f"HSL=({h:6.1f}, {s:.2f}, {l:.2f})"
            )


if __name__ == "__main__":
    main()
