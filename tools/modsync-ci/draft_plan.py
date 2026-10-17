#!/usr/bin/env python3
"""Emit DRAFT manual-lane plans plus a review sidecar (Python 3.11+, standard library only).

For each game this reads content/<game>/full.md read-only and writes two files:
  <out-dir>/<game>-manual-plan.draft.json   plan-shaped skeleton, every step unreviewed
  <out-dir>/<game>-manual-plan.review.json  every step with status needs-human-review
                                            and the evidence found in the guide text

A draft is NOT a plan. It carries no operations, argv, patcher, hash or success marker,
because the guide text does not state them in executable form; every steps[].operations
is empty and every supplemental review is empty, and it has a top-level "draft" key.
tools/modsync-ci/manual.py rejects it on all three counts. A human authors the real plan
and commits it as ci/<game>-manual-plan.json; this tool refuses to write that path.

Evidence is quoted from the guide only: the step heading, the links on the **Name:** line
(with a file name only when the URL path itself ends in .zip/.7z/.rar), the verbatim
**Installation Method:** text and the lines of every ::: block. Output is deterministic
(no timestamps; same guide, same bytes). Existing differing drafts are not overwritten
without --force, so hand edits are not lost.
"""

import argparse
import importlib.util
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[2]
GAMES = ("k1", "k2")
STATUS = "needs-human-review"
DRAFT_SUFFIX = "-manual-plan.draft.json"
REVIEW_SUFFIX = "-manual-plan.review.json"
REVIEWED_PLAN = re.compile(r"[^/]*-manual-plan\.json")
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
HEADING = re.compile(r"^(#{1,6}) ")
FIELD = re.compile(r"^\*\*([^*:]+):\*\*\s*(.*)$")
LINK = re.compile(r"\[([^\]]*)\]\(\s*(<[^>]+>|[^\s)]+)")
BLOCK_OPEN = re.compile(r"^:::([A-Za-z]\w*)\s*(.*)$")
BLOCK_CLOSE = re.compile(r"^:::\s*$")
CONTINUATION = re.compile(r"^:\s{0,3}")
ARCHIVE = re.compile(r"\.(zip|7z|rar)$", re.I)
NOTICE = ("DRAFT, not a reviewed plan. Operations are empty, supplemental reviews are empty and "
          "archive_sha256 is unbound. Delete this key only after a human has authored and "
          "reviewed every step.")
LINK_NOTE = ("archive_name_from_link is set only when the URL path itself ends in .zip/.7z/.rar; "
             "otherwise null, because the archive name is decided when the file is downloaded.")


class Refused(Exception):
    pass


def load_manual():
    spec = importlib.util.spec_from_file_location("modsync_manual", Path(__file__).with_name("manual.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


manual = load_manual()


def split_sections(text):
    sections = []
    fence = None
    for number, line in enumerate(text.splitlines(), 1):
        marker = FENCE.match(line)
        if marker:
            run, tail = marker.groups()
            if fence is None:
                fence = run
            elif run[0] == fence[0] and len(run) >= len(fence) and not tail.strip():
                fence = None
        elif fence is None and HEADING.match(line):
            sections.append({"heading": line, "line": number, "body": []})
            continue
        if sections:
            sections[-1]["body"].append((number, line))
    return sections


def classify(sections):
    steps, supplemental = [], []
    in_mods = False
    for section in sections:
        heading = section["heading"]
        if heading.startswith(("# ", "## ")):
            in_mods = heading == "## Mod List"
        if heading.startswith("### ") and in_mods:
            steps.append(section)
        elif heading.startswith(("## ", "### ")) and heading != "## Mod List":
            supplemental.append(section)
    return steps, supplemental


def link_evidence(line):
    links = []
    for text, target in LINK.findall(line):
        url = target[1:-1] if target.startswith("<") else target
        parts = urlsplit(url)
        name = unquote(parts.path.rsplit("/", 1)[-1])
        links.append({
            "text": text.replace("*", "").strip(),
            "url": url,
            "host": (parts.hostname or "").lower(),
            "archive_name_from_link": name if ARCHIVE.search(name) else None,
        })
    return links


def blocks_of(body):
    blocks, current = [], None
    for number, line in body:
        opened = BLOCK_OPEN.match(line)
        if current is None and opened:
            current = {"kind": opened[1], "title": opened[2].strip() or None, "lines": []}
        elif current is not None and BLOCK_CLOSE.match(line):
            blocks.append(current)
            current = None
        elif current is not None and line.strip():
            if current["title"] is None and not line.startswith(":"):
                current["title"] = line.strip()
            else:
                current["lines"].append({"line": number, "text": CONTINUATION.sub("", line, count=1).strip()})
    if current is not None:
        blocks.append(current)
    return blocks


def step_evidence(section):
    fields = {}
    name_links = []
    for number, line in section["body"]:
        field = FIELD.match(line)
        if field and field[1].strip() not in fields:
            fields[field[1].strip()] = {"line": number, "text": field[2].strip()}
            if field[1].strip() == "Name":
                name_links = link_evidence(line)
    return {
        "heading_line": section["line"],
        "name_links": name_links,
        "installation_method": fields.get("Installation Method"),
        "blocks": blocks_of(section["body"]),
    }


def build(game, guide, label):
    text = guide.read_text(encoding="utf-8")
    mods, outside = manual.guide_headings(text)
    steps, supplemental = classify(split_sections(text))
    if [step["heading"] for step in steps] != mods or [item["heading"] for item in supplemental] != outside:
        raise Refused("draft headings disagree with manual.py heading parser")
    width = max(3, len(str(len(steps))))
    identifiers = ["step-%0*d" % (width, index) for index in range(1, len(steps) + 1)]
    digest = manual.sha256(guide)
    draft = {
        "draft": {"status": STATUS, "review_file": game + REVIEW_SUFFIX, "notice": NOTICE},
        "guide_sha256": digest,
        "archive_sha256": {},
        "steps": [{"id": identifier, "source_heading": step["heading"], "operations": []}
                  for identifier, step in zip(identifiers, steps)],
        "supplemental_sections": [{"source_heading": item["heading"], "review": "", "step_ids": []}
                                  for item in supplemental],
    }
    review = {
        "schema": "modsync-manual-plan-review/1",
        "game": game,
        "guide": label,
        "guide_sha256": digest,
        "draft_file": game + DRAFT_SUFFIX,
        "status": STATUS,
        "notice": NOTICE,
        "evidence_note": LINK_NOTE,
        "step_count": len(steps),
        "supplemental_count": len(supplemental),
        "steps": [{"id": identifier, "source_heading": step["heading"], "status": STATUS,
                   "evidence": step_evidence(step)}
                  for identifier, step in zip(identifiers, steps)],
        "supplemental_sections": [{"source_heading": item["heading"], "status": STATUS,
                                   "evidence": {"heading_line": item["line"],
                                                "body_lines": sum(1 for _, line in item["body"] if line.strip())}}
                                  for item in supplemental],
    }
    return draft, review


def render(value):
    return json.dumps(value, indent=2, ensure_ascii=False) + "\n"


def checked_target(path):
    resolved = path.resolve()
    if REVIEWED_PLAN.fullmatch(resolved.name) and not resolved.name.endswith((DRAFT_SUFFIX, REVIEW_SUFFIX)):
        raise Refused("refusing to write a reviewed plan path: " + str(path))
    if not resolved.name.endswith((DRAFT_SUFFIX, REVIEW_SUFFIX)):
        raise Refused("draft outputs must end in " + DRAFT_SUFFIX + " or " + REVIEW_SUFFIX)
    if resolved.parent.name == "ci":
        raise Refused("refusing to write drafts into the reviewed-plan directory ci/: " + str(path))
    return resolved


def write_atomic(path, content):
    descriptor, name = tempfile.mkstemp(prefix=".draft-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def emit(games, guide_root, out_dir, force=False):
    pending, summary = [], []
    for game in games:
        guide = guide_root / game / "full.md"
        if not guide.is_file():
            raise Refused("guide not found: " + str(guide))
        draft, review = build(game, guide, guide_root.name + "/" + game + "/full.md")
        for suffix, value in ((DRAFT_SUFFIX, draft), (REVIEW_SUFFIX, review)):
            pending.append((checked_target(out_dir / (game + suffix)), render(value)))
        summary.append((game, len(draft["steps"]), len(draft["supplemental_sections"])))
    for target, content in pending:
        if target.exists() and not force and target.read_text(encoding="utf-8") != content:
            raise Refused("existing draft differs, pass --force to overwrite: " + str(target))
    out_dir.mkdir(parents=True, exist_ok=True)
    for target, content in pending:
        write_atomic(target, content)
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--game", choices=GAMES + ("all",), default="all")
    parser.add_argument("--guide-root", type=Path, default=ROOT / "content")
    parser.add_argument("--out-dir", type=Path, default=ROOT / "ci" / "drafts")
    parser.add_argument("--force", action="store_true", help="overwrite differing existing drafts")
    args = parser.parse_args(argv)
    try:
        games = GAMES if args.game == "all" else (args.game,)
        for game, steps, sections in emit(games, args.guide_root, args.out_dir, args.force):
            print("%s: drafted %d steps, %d supplemental sections, all %s" % (game, steps, sections, STATUS))
        return 0
    except (Refused, manual.Blocked, OSError, UnicodeDecodeError) as error:
        print("draft_plan: " + str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
