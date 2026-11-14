#!/usr/bin/env python3
"""Assemble ci/<game>-manual-plan.json from authored step fragments and the archive inventory.

Every operation in the fragments is authored by hand from the guide and the archive
evidence (manual_inventory.py output). This tool only joins the fragments in guide order,
fills `guide_sha256` and `archive_sha256` (the SHA-256 the inventory recorded for each
`{archives}/` file an operation reads) and checks the result with manual.py's own plan
validation, so a fragment that names an unknown archive, a heading out of order or a
malformed operation fails here instead of in CI. It never adds, removes or changes an
operation.

  assemble_manual_plan.py --game k1 --parts DIR --inventory FILE [--out FILE]

DIR holds steps-*.json (each a JSON list of steps, in guide order within the file;
files are joined in name order) and supplemental.json (the supplemental_sections list).
"""

import argparse
import hashlib
import json
from pathlib import Path
import sys
import tempfile

import manual

ROOT = Path(__file__).resolve().parents[2]


def archive_tokens(steps):
    tokens = set()
    for step in steps:
        for operation in step.get("operations", []):
            for key in ("source", "patcher"):
                value = operation.get(key)
                if isinstance(value, str) and value.startswith("{archives}/"):
                    tokens.add(value)
    return tokens


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--game", choices=("k1", "k2"), required=True)
    parser.add_argument("--parts", type=Path, required=True)
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    guide = ROOT / "content" / args.game / "full.md"
    out = args.out or ROOT / "ci" / ("%s-manual-plan.json" % args.game)
    steps = []
    for part in sorted(args.parts.glob("steps-*.json")):
        loaded = json.loads(part.read_text(encoding="utf-8"))
        if not isinstance(loaded, list):
            sys.exit("%s: expected a list of steps" % part)
        steps += loaded
    supplemental = json.loads((args.parts / "supplemental.json").read_text(encoding="utf-8"))
    inventory = json.loads(args.inventory.read_text(encoding="utf-8"))
    hashes = {"{archives}/" + item["file"]: item["sha256"] for item in inventory["archives"]}
    archive_sha256 = {}
    for token in sorted(archive_tokens(steps)):
        if token not in hashes:
            sys.exit("no inventoried archive for " + token)
        archive_sha256[token] = hashes[token]
    plan = {"guide_sha256": hashlib.sha256(guide.read_bytes()).hexdigest(),
            "archive_sha256": archive_sha256, "steps": steps,
            "supplemental_sections": supplemental}
    # manual.py validation against stand-in roots: every structural rule applies; the archive
    # bytes themselves are verified by manual.py at run time against archive_sha256.
    with tempfile.TemporaryDirectory() as scratch:
        roots = {name: Path(scratch) / name for name in ("game", "archives", "extract")}
        for path in roots.values():
            path.mkdir()
        original = manual.verify_archive_inputs
        manual.verify_archive_inputs = lambda plan, roots, deadline=None: None
        try:
            manual.validate_plan(plan, guide, roots, 1800)
        except manual.Blocked as error:
            sys.exit("plan rejected: %s" % error)
        finally:
            manual.verify_archive_inputs = original
    out.write_text(json.dumps(plan, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print("wrote %s: %d steps, %d archives, %d supplemental sections"
          % (out, len(steps), len(archive_sha256), len(supplemental)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
