#!/usr/bin/env python3
"""Regenerate the delegated TypeScript-lane review manifests ci/<game>-typescript-review.json (Python 3.11+).

The manifests are bound by SHA-256 to the converted TOML a TypeScript lane hashes: the
output of `ModSync.ts import-md` plus `ports.select_base_components`. That output depends on
the guide and on the ModSync.ts ref the workflow builds (MODSYNC_TS_REF, default
feat/physical-install), so a moved ref can make ports.py block with
review_manifest_hash_mismatch. Regenerate with a freshly built checkout of that ref:

  git clone --branch <MODSYNC_TS_REF> https://github.com/bodencrouch/ModSync.ts.git ts-ci-ref
  (cd ts-ci-ref && pnpm install --frozen-lockfile && pnpm build)
  python3 tools/modsync-ci/gen_ts_review.py --source ts-ci-ref            # both games
  python3 tools/modsync-ci/gen_ts_review.py --source ts-ci-ref --check    # exit 1 when stale

Per game this converts the guide exactly as the lane does, drafts the manifest through
draft_ts_review.build, fills every decision by documented default, strips the draft keys,
validates the result with ports.review_manifest and writes ci/<game>-typescript-review.json:

  phase       ports.component_phase (widescreen if IsWidescreen, else base)
  Patcher     "0": the first namespaces.ini entry, the namespace prompt's own default
  Execute     "yes": Execute never prompts in ModSync.ts, so the answer is inert
  Choose      no default exists: the tool refuses and a human must answer
  skipped     components of the reviewed policy skips (archive_selections.json lane_skips
              without a Rust GUID pin, plus the uncached-by-policy steps of ci.yml) get the
              assessment "omitted by reviewed policy"; they stay listed so the manifest
              covers every converted component
  reviewer    delegated-by-owner (orchestrator, 2026-10-18)

The sign-off is the owner's delegation, not a per-instruction human review; the assessments
say so. Output is deterministic.
"""

import argparse
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import tomllib

ROOT = Path(__file__).resolve().parents[2]
GAMES = ("k1", "k2")
REVIEWER = "delegated-by-owner (orchestrator, 2026-10-18)"
SKIPPED_ASSESSMENT = "omitted by reviewed policy"
DEFAULT_ANSWERS = {"Patcher": "0", "Execute": "yes"}
UNCACHED_POLICY_STEPS = {
    "k1": [("Optional Install Assistant - Quicker TSLPatching", "Quicker TSLPatching"),
           ("Optional Widescreen & Extended Widescreen Support Mods", "Remastered Cutscenes")],
    "k2": [("Optional Install Assistant - Quicker TSLPatching", "Quicker TSLPatching"),
           ("Enhanced Widescreen Support Mods", "K2 Cutscenes Rescaled")],
}
UNCACHED_POLICY_REASON = ("not cached by policy: skipped by the workflow's "
                          "--allow-uncached-by-policy restore flags")


class Refused(Exception):
    pass


def load_module(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ports = load_module("ports")
draft_ts_review = load_module("draft_ts_review")


def policy_skips(game, lane_skips_path=None):
    uncached = [{"section": section, "heading": heading, "reason": UNCACHED_POLICY_REASON,
                 "urls": [], "source": "restore_report"}
                for section, heading in UNCACHED_POLICY_STEPS[game]]
    return ports.merge_lane_skips(uncached, ports.load_lane_skips(
        game, path=lane_skips_path, implementation="typescript"))


def convert(source, guide, destination):
    """Run import-md and select_base_components exactly as ports.execute does."""
    entrypoint = source / "packages/cli/dist/main.js"
    if not entrypoint.is_file():
        raise Refused("ModSync.ts is not built (missing %s); run `pnpm install --frozen-lockfile "
                      "&& pnpm build` in it" % entrypoint)
    command = ports.typescript_import_command(["node", str(entrypoint)], guide, destination)
    result = subprocess.run(command, cwd=source, stdin=subprocess.DEVNULL, capture_output=True,
                            text=True, timeout=1800, check=False)
    if result.returncode:
        raise Refused("import-md failed (exit %d): %s" % (result.returncode, result.stderr.strip()[-500:]))
    ports.select_base_components(destination)


def fill(draft, skipped_guids, game):
    """Turn the draft into a complete manifest using only documented defaults."""
    for key in draft_ts_review.DRAFT_KEYS:
        draft.pop(key)
    draft["reviewer"] = REVIEWER
    draft["unresolved"] = []
    for entry in draft["components"]:
        prompts = entry.pop("draft_prompts")
        entry.pop("draft_context")
        entry["unresolved"] = []
        entry["choices"] = {}
        for prompt in prompts:
            answer = DEFAULT_ANSWERS.get(prompt["action"])
            if answer is None:
                raise Refused("%s: %s %s has no documented default answer; answer it by hand"
                              % (game, prompt["action"], prompt["instruction_guid"]))
            entry["choices"][prompt["instruction_guid"]] = answer
        if entry["guid"] in skipped_guids:
            entry["assessment"] = SKIPPED_ASSESSMENT
            continue
        entry["assessment"] = (
            "Delegated by the owner (2026-10-18) and not reviewed by hand: instructions and options "
            "are the byte-reproducible ModSync.ts import-md conversion of the guide; phase follows "
            "IsWidescreen; " + (
                "answers are the documented defaults (Patcher '0' = the first namespaces.ini entry, "
                "Execute 'yes' = inert, Execute never prompts)." if prompts else "no prompts."))
    return draft


def generate(game, source, root, converted=None, lane_skips_path=None):
    guide = root / "content" / game / "full.md"
    if not guide.is_file():
        raise Refused("guide not found: " + str(guide))
    with tempfile.TemporaryDirectory(prefix="gen-ts-review-") as directory:
        work = Path(directory)
        target = work / "converted.toml"
        if converted is None:
            convert(source, guide, target)
        else:
            target.write_bytes(Path(converted).read_bytes())
        report = {}
        ports.coverage(guide, target, report, "typescript", policy_skips(game, lane_skips_path))
        skipped_guids = {record["component_guid"] for record in report[ports.SKIPPED_REPORT_KEY]
                         if record["component_guid"] is not None}
        draft = draft_ts_review.build(target, guide)
        manifest = fill(draft, skipped_guids, game)
        content = json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"
        candidate = work / "manifest.json"
        candidate.write_text(content, encoding="utf-8")
        ports.review_manifest(candidate, guide, target)
        document = tomllib.loads(target.read_text(encoding="utf-8"))
        return {"content": content, "converted_sha256": manifest["converted_sha256"],
                "guide_sha256": manifest["guide_sha256"], "skipped": len(skipped_guids),
                "components": len(document.get("thisMod", document.get("Components", [])))}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", type=Path, help="built ModSync.ts checkout of the MODSYNC_TS_REF the workflow uses")
    parser.add_argument("--game", action="append", choices=GAMES, help="repeatable; default: k1 and k2")
    parser.add_argument("--root", type=Path, default=ROOT, help="repository root holding content/<game>/full.md")
    parser.add_argument("--out-dir", type=Path, help="default: <root>/ci")
    parser.add_argument("--converted", type=Path,
                        help="use this select_base_components-processed TOML instead of converting (single game)")
    parser.add_argument("--check", action="store_true", help="write nothing; exit 1 when a manifest is missing or stale")
    args = parser.parse_args(argv)
    games = args.game or list(GAMES)
    try:
        if args.converted is not None and len(games) != 1:
            raise Refused("--converted needs exactly one --game")
        if args.converted is None and args.source is None:
            raise Refused("--source is required unless --converted is given")
        out_dir = args.out_dir or args.root / "ci"
        stale = False
        for game in games:
            result = generate(game, args.source.resolve() if args.source else None,
                              args.root.resolve(), args.converted)
            target = out_dir / (game + "-typescript-review.json")
            current = target.read_text(encoding="utf-8") if target.is_file() else None
            print("%s: %d components, %d omitted by policy, guide_sha256 %s, converted_sha256 %s" % (
                game, result["components"], result["skipped"], result["guide_sha256"],
                result["converted_sha256"]))
            if args.check:
                if current != result["content"]:
                    stale = True
                    print("  STALE or missing: " + str(target), file=sys.stderr)
                continue
            if current != result["content"]:
                out_dir.mkdir(parents=True, exist_ok=True)
                target.write_text(result["content"], encoding="utf-8")
                print("  wrote " + str(target))
            else:
                print("  unchanged " + str(target))
        return 1 if stale else 0
    except (Refused, ports.Blocked, draft_ts_review.Refused, OSError, subprocess.SubprocessError,
            tomllib.TOMLDecodeError) as error:
        print("gen_ts_review: " + str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
