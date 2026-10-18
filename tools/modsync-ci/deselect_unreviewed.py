#!/usr/bin/env python3
"""Generate the reviewed lane_skips entries for components left with an unreviewed draft.

The Rust install lanes stop at the draft_review gate with one log line per component
whose draft the verified-draft review did not clear:

    ports: unreviewed draft still selected: <guid> (<name>)

This tool turns those lines into explicit, committed `lane_skips` entries in
archive_selections.json (the reviewed-policy-skip mechanism of ports.py), so the
rest of the guide installs and every omitted component stays named in the repo and
in the lane report. Nothing is reviewed or approved here: the drafts stay unreviewed
and RequiresReview is not touched.

  deselect_unreviewed.py --game k2 --guide content/k2/full.md --converted converted.toml \
      --log install-cli.log --log install-gui.log [--write | --check]

--converted is any `convert --parse-directions` output of the same guide (the GUIDs and
Dependencies do not depend on archives). Components that depend, directly or through
other skipped components, on a listed component are added with kind
`dependent_of_deselected`: the Rust selection cascade deselects them anyway, and ports.py
refuses a skip that would leave such a dependent silently behind. Entries without a
`kind` (hand-authored, e.g. 4GB Patcher) are preserved and never regenerated. Without
--write or --check the result is printed and nothing is changed.
"""

import argparse
import json
from pathlib import Path
import re
import sys
import tomllib

import ports

DELEGATION = "deselected pending human review, delegated by owner 2026-10-18"
DRAFT_REASON = ("unreviewed draft: not generated from resolved archive filenames "
                "(prose/multi-archive/executable method) and left unreviewed by the verified-draft "
                "review; " + DELEGATION)
LINE = re.compile(r"ports: unreviewed draft still selected: "
                  r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}) \((.*)\)\s*$")


class GenerationError(Exception):
    pass


def parse_logs(texts):
    """GUID -> name from the draft_review lines of one or more CI logs (GUIDs lowercased)."""
    found = {}
    for text in texts:
        for line in text.splitlines():
            match = LINE.search(line)
            if match:
                found.setdefault(match[1].lower(), match[2])
    return found


def dependency_reason(names):
    return ("depends on " + ", ".join(names) + ", which is deselected pending human review; "
            "the Rust selection cascade deselects it with its dependency, so it is listed "
            "explicitly instead of being dropped silently; " + DELEGATION)


def generate_entries(guide_text, converted_text, found, manual=()):
    """Lane-skip entries (guide order) for the found GUIDs plus their dependents.

    Fails on a GUID that is not a top-level component of the guide, a name that differs
    from the guide heading, an ambiguous heading, or a converted file that does not
    line up with the guide.
    """
    steps, _, _ = ports.guide_step_list(guide_text)
    document = tomllib.loads(converted_text)
    components = document.get("thisMod", document.get("Components", []))
    if len(components) != len(steps):
        raise GenerationError("converted components do not match the guide's counted steps")
    identities = [str(component.get("Guid", "")).lower() for component in components]
    names = [component.get("Heading", component.get("Name")) for component in components]
    for (_, heading), name in zip(steps, names):
        if heading != name:
            raise GenerationError(f"converted order differs from the guide at {heading!r}")
    unknown = sorted(set(found) - set(identities))
    if unknown:
        raise GenerationError("not top-level components of the guide: " + ", ".join(unknown))
    for identity, name in found.items():
        if names[identities.index(identity)] != name:
            raise GenerationError(f"{identity} is named {name!r} in the log, "
                                  f"{names[identities.index(identity)]!r} in the guide")
    manual_keys = {(entry["section"], entry["heading"]) for entry in manual}
    manual_guids = {identities[steps.index(key)] for key in manual_keys if key in steps}
    deselected = (set(found) | manual_guids)
    causes = {}
    changed = True
    while changed:
        changed = False
        for identity, component in zip(identities, components):
            if identity in deselected:
                continue
            blockers = [each for each in (str(item).lower() for item in component.get("Dependencies", []))
                        if each in deselected]
            if blockers:
                causes[identity] = [names[identities.index(each)] for each in blockers]
                deselected.add(identity)
                changed = True
    entries = []
    for index, (key, identity) in enumerate(zip(steps, identities)):
        if identity not in found and identity not in causes:
            continue
        if key in manual_keys:
            continue
        if steps.count(key) != 1:
            raise GenerationError(f"ambiguous guide step {key[1]!r}")
        draft = identity in found
        entries.append({"section": key[0], "heading": key[1],
                        "reason": DRAFT_REASON if draft else dependency_reason(causes[identity]),
                        "guid": identity,
                        "kind": "unreviewed_draft" if draft else "dependent_of_deselected"})
    return entries


def apply_entries(selections, game, entries):
    """Copy of the selections document whose `game` table keeps the hand-authored
    entries and replaces every kind-marked one with `entries`."""
    if game not in ports.LANE_SKIP_GAMES:
        raise GenerationError("game must be k1 or k2")
    result = json.loads(json.dumps(selections))
    table = result.setdefault("lane_skips", {})
    table[game] = [entry for entry in table.get(game, []) if "kind" not in entry] + entries
    return result


def render(selections):
    return json.dumps(selections, indent=2) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--game", choices=ports.LANE_SKIP_GAMES, required=True)
    parser.add_argument("--guide", type=Path, required=True)
    parser.add_argument("--converted", type=Path, required=True)
    parser.add_argument("--log", type=Path, action="append", required=True)
    parser.add_argument("--selections", type=Path, default=ports.LANE_SKIPS_PATH)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--write", action="store_true", help="rewrite the selections file")
    mode.add_argument("--check", action="store_true",
                      help="exit 1 when the selections file differs from the generated result")
    args = parser.parse_args(argv)
    try:
        selections = json.loads(args.selections.read_text(encoding="utf-8"))
        manual = [entry for entry in selections.get("lane_skips", {}).get(args.game, [])
                  if "kind" not in entry]
        found = parse_logs(path.read_text(encoding="utf-8") for path in args.log)
        if not found:
            raise GenerationError("no 'unreviewed draft still selected' lines in the logs")
        entries = generate_entries(args.guide.read_text(encoding="utf-8"),
                                   args.converted.read_text(encoding="utf-8"), found, manual)
        rendered = render(apply_entries(selections, args.game, entries))
    except (GenerationError, OSError, ValueError, KeyError) as error:
        print(f"deselect_unreviewed: {error}", file=sys.stderr)
        return 2
    drafts = sum(entry["kind"] == "unreviewed_draft" for entry in entries)
    print(f"{args.game}: {len(entries)} lane skip(s): {drafts} unreviewed draft(s), "
          f"{len(entries) - drafts} dependent(s); {len(found) - drafts} log GUID(s) already skipped by hand",
          file=sys.stderr)
    current = args.selections.read_text(encoding="utf-8")
    if args.check:
        if rendered != current:
            print(f"deselect_unreviewed: {args.selections} differs from the generated result",
                  file=sys.stderr)
            return 1
        return 0
    if args.write:
        if rendered != current:
            args.selections.write_text(rendered, encoding="utf-8")
        return 0
    sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    sys.exit(main())
