#!/usr/bin/env python3
"""Emit a DRAFT TypeScript-lane review manifest plus a reviewer checklist (Python 3.11+, standard library only).

Given the converted TOML of one lane run and the guide it came from, this writes two files
(default directory ci/drafts/, never ci/):
  <game>-typescript-review.draft.json      review_manifest skeleton, mechanically prefilled
  <game>-typescript-review.checklist.md    exactly what a human must still decide

Prefilled from the inputs only: schema_version, guide_sha256, converted_sha256, and per
component (converted order) the guid, the instructions and options records copied verbatim.
phase is filled only through ports.component_phase when ports.py provides that function;
ports.py currently holds the rule inline, so phase is a TODO. assessment, reviewer, every
unresolved list and every Choose/Patcher/Execute answer are left for the reviewer.

A draft is NOT a review. It has a top-level "draft": true, an empty reviewer, non-empty
unresolved lists, an empty assessment and TODO answers, so ports.review_manifest blocks it
on several independent grounds. A human completes it, removes the draft_* keys and commits
it as ci/<game>-typescript-review.json; this tool refuses to write that path. The converted
TOML must be the exact file the lane hashes (report converted_sha256). Output is
deterministic (no timestamps). Existing differing drafts are not overwritten without --force.
"""

import argparse
import importlib.util
import json
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GAMES = ("k1", "k2")
DRAFT_SUFFIX = "-typescript-review.draft.json"
CHECKLIST_SUFFIX = "-typescript-review.checklist.md"
REVIEWED_NAME = re.compile(r"[^/]*-typescript-review\.json")
PROMPT_ACTIONS = ("Choose", "Patcher", "Execute")
PHASE_TODO = "TODO: base or widescreen"
CHOICE_TODO = "TODO: CLI answer for this prompt"
UNRESOLVED_TODO = "TODO: resolve every checklist item, then replace this list with []"
DRAFT_KEYS = ("draft", "draft_notice")
COMPONENT_DRAFT_KEYS = ("draft_context", "draft_prompts")
NOTICE = ("DRAFT, not a reviewed manifest. reviewer is empty, unresolved is non-empty and every answer "
          "is a TODO. A human completes it, deletes the draft_* keys and commits it as "
          "ci/<game>-typescript-review.json.")


class Refused(Exception):
    pass


def load_module(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ports = load_module("ports")
draft_plan = load_module("draft_plan")


def normalized_guid(value, what):
    try:
        return ports.guid(value)
    except (ports.Blocked, ValueError):
        raise Refused(what + " has no valid GUID: " + repr(value)) from None


def inferred_phase(component):
    rule = getattr(ports, "component_phase", None)
    return rule(component) if rule is not None else PHASE_TODO


def component_list(document):
    components = document.get("thisMod", document.get("Components", []))
    if not isinstance(components, list) or not components:
        raise Refused("converted TOML has no thisMod/Components entries")
    if not all(isinstance(component, dict) for component in components):
        raise Refused("converted TOML has a non-table component entry")
    return components


def prompt_records(component):
    """Choose/Patcher/Execute instructions in the order review_manifest walks them."""
    groups = [("component", component.get("Instructions", []))]
    for option in component.get("Options", []):
        label = "option %s (%s)" % (normalized_guid(option.get("Guid"), "an option"), option.get("Name"))
        groups.append((label, option.get("Instructions", [])))
    records, seen = [], []
    for location, instructions in groups:
        for instruction in instructions:
            identity = normalized_guid(instruction.get("Guid"), "an instruction")
            seen.append(identity)
            if instruction.get("Action") in PROMPT_ACTIONS:
                records.append({"instruction_guid": identity, "action": instruction["Action"],
                                "location": location, "source": instruction.get("Source"),
                                "destination": instruction.get("Destination"),
                                "arguments": instruction.get("Arguments")})
    return records, seen


def build(converted, guide):
    document = tomllib.loads(converted.read_text(encoding="utf-8"))
    components = component_list(document)
    instruction_ids, component_ids, entries = set(), set(), []
    for position, component in enumerate(components, 1):
        identity = normalized_guid(component.get("Guid"), "component %d" % position)
        if identity in component_ids:
            raise Refused("duplicate component GUID " + identity)
        component_ids.add(identity)
        prompts, local = prompt_records(component)
        for instruction_id in local:
            if instruction_id in instruction_ids:
                raise Refused("duplicate instruction GUID " + instruction_id)
            instruction_ids.add(instruction_id)
        instructions, options = component.get("Instructions", []), component.get("Options", [])
        try:
            copied = json.loads(json.dumps([instructions, options]))
        except TypeError:
            raise Refused("component %s holds values JSON cannot represent exactly" % identity) from None
        if copied != [instructions, options]:
            raise Refused("component %s does not survive a JSON round trip" % identity)
        entries.append({
            "guid": identity,
            "instructions": instructions,
            "options": options,
            "phase": inferred_phase(component),
            "assessment": "",
            "unresolved": [UNRESOLVED_TODO],
            "choices": {prompt["instruction_guid"]: CHOICE_TODO for prompt in prompts},
            "draft_context": {"position": position,
                              "name": component.get("Heading", component.get("Name")),
                              "IsWidescreen": component.get("IsWidescreen"),
                              "instruction_count": len(local)},
            "draft_prompts": prompts,
        })
    return {
        "draft": True,
        "draft_notice": NOTICE,
        "schema_version": 1,
        "reviewer": "",
        "unresolved": [UNRESOLVED_TODO],
        "guide_sha256": ports.digest(guide),
        "converted_sha256": ports.digest(converted),
        "components": entries,
    }


def code(value):
    text = " ".join(str(value).split())
    fence = "`" * (max([len(run) for run in re.findall(r"`+", text)] + [0]) + 1)
    return fence + (" " + text + " " if "`" in text else text) + fence


def checklist(game, draft, converted, guide):
    entries = draft["components"]
    prompts = sum(len(entry["draft_prompts"]) for entry in entries)
    lines = [
        "# TypeScript review checklist: " + game,
        "",
        "DRAFT. Nothing here is approved; every box is a decision for the human reviewer.",
        "Generated by `tools/modsync-ci/draft_ts_review.py`; the matching skeleton is "
        "`%s%s`." % (game, DRAFT_SUFFIX),
        "",
        "- Guide: %s (sha256 %s)" % (code(guide), draft["guide_sha256"]),
        "- Converted TOML: %s (sha256 %s)" % (code(converted), draft["converted_sha256"]),
        "- Components: %d; Choose/Patcher/Execute instructions needing an answer: %d" % (len(entries), prompts),
        "",
        "## Manifest-wide",
        "",
        "- [ ] The converted TOML above is the exact file the lane hashes (report `converted_sha256`). "
        "If the converter output changes, regenerate this draft and review again.",
        "- [ ] `reviewer`: your name (nonempty).",
        "- [ ] Top-level `unresolved`: `[]`, only once every item below is resolved.",
        "- [ ] Delete `draft`, `draft_notice` and each component's `draft_context` and `draft_prompts`.",
        "- [ ] Save as `ci/%s-typescript-review.json`. This tool never writes that path." % game,
        "",
        "## Components",
    ]
    for entry in entries:
        context = entry["draft_context"]
        lines += [
            "",
            "### %d. %s" % (context["position"], code(context["name"])),
            "",
            "- GUID: `%s`; instructions (including options): %d; options: %d"
            % (entry["guid"], context["instruction_count"], len(entry["options"])),
            "- `IsWidescreen` in the converted TOML: %s" % code(json.dumps(context["IsWidescreen"])),
            "- [ ] `phase`: `base` or `widescreen`.",
            "- [ ] `assessment`: nonempty written judgement that the instructions and options are right "
            "for this guide step.",
            "- [ ] `unresolved`: `[]`.",
        ]
        if not entry["draft_prompts"]:
            lines.append("- `choices`: no Choose/Patcher/Execute instruction; leave `{}`.")
            continue
        lines.append("- [ ] `choices`: one non-empty CLI answer for each instruction GUID below.")
        for prompt in entry["draft_prompts"]:
            lines.append("  - [ ] `%s` %s in %s: source %s, destination %s, arguments %s" % (
                prompt["instruction_guid"], prompt["action"], prompt["location"],
                code(json.dumps(prompt["source"])), code(json.dumps(prompt["destination"])),
                code(json.dumps(prompt["arguments"]))))
    return "\n".join(lines) + "\n"


def checked_target(path):
    resolved = path.resolve()
    if REVIEWED_NAME.fullmatch(resolved.name):
        raise Refused("refusing to write a reviewed manifest path: " + str(path))
    if not resolved.name.endswith((DRAFT_SUFFIX, CHECKLIST_SUFFIX)):
        raise Refused("outputs must end in " + DRAFT_SUFFIX + " or " + CHECKLIST_SUFFIX)
    if resolved.parent.name == "ci":
        raise Refused("refusing to write drafts into the reviewed-manifest directory ci/: " + str(path))
    return resolved


def emit(game, converted, guide, out_dir, force=False):
    if game not in GAMES:
        raise Refused("game must be one of " + ", ".join(GAMES))
    for label, path in (("converted TOML", converted), ("guide", guide)):
        if not path.is_file():
            raise Refused(label + " not found: " + str(path))
    draft = build(converted, guide)
    pending = [(checked_target(out_dir / (game + DRAFT_SUFFIX)), draft_plan.render(draft)),
               (checked_target(out_dir / (game + CHECKLIST_SUFFIX)),
                checklist(game, draft, str(converted), str(guide)))]
    for target, content in pending:
        if target.exists() and not force and target.read_text(encoding="utf-8") != content:
            raise Refused("existing draft differs, pass --force to overwrite: " + str(target))
    out_dir.mkdir(parents=True, exist_ok=True)
    for target, content in pending:
        draft_plan.write_atomic(target, content)
    prompts = sum(len(entry["draft_prompts"]) for entry in draft["components"])
    return [target for target, _ in pending], len(draft["components"]), prompts


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("converted", type=Path, help="converted TOML exactly as the lane hashes it")
    parser.add_argument("guide", type=Path, help="content/<game>/full.md the TOML was converted from")
    parser.add_argument("--game", choices=GAMES, help="default: name of the guide's parent directory")
    parser.add_argument("--out-dir", type=Path, default=ROOT / "ci" / "drafts")
    parser.add_argument("--force", action="store_true", help="overwrite differing existing drafts")
    args = parser.parse_args(argv)
    try:
        game = args.game or args.guide.resolve().parent.name
        if game not in GAMES:
            raise Refused("cannot infer the game from the guide path, pass --game k1|k2")
        targets, components, prompts = emit(game, args.converted, args.guide, args.out_dir, args.force)
        print("%s: drafted %d components, %d prompt instructions need an answer" % (game, components, prompts))
        for target in targets:
            print("  " + str(target))
        return 0
    except (Refused, ports.Blocked, OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        print("draft_ts_review: " + str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
