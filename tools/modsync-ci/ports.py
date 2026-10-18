#!/usr/bin/env python3
"""Private-source, fail-closed full-guide port runner (Python 3.11+)."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import tomllib
from urllib.parse import urldefrag
import uuid


class Blocked(Exception):
    pass


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(command, source, private, stage, report, env):
    report["stage"] = stage
    log = private / (stage + ".log")
    with log.open("xb") as output:
        process = subprocess.Popen(
            command, cwd=source, env=env, stdin=subprocess.DEVNULL,
            stdout=output, stderr=subprocess.STDOUT, start_new_session=True,
        )
        try:
            code = process.wait(timeout=7200)
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            raise Blocked("command_interrupted_or_timed_out") from None
    report["commands"].append({"stage": stage, "exit_code": code})
    if code:
        lines = log.read_text(encoding="utf-8", errors="replace").splitlines()[-80:]
        print(f"::group::{stage} output (last 80 lines)", file=sys.stderr)
        print("\n".join(lines), file=sys.stderr)
        print("::endgroup::", file=sys.stderr)
        raise Blocked(stage + "_failed")


SKIP_CAUSES = (
    ("already_had_instructions", ("already has instructions",)),
    ("no_mod_links", ("no mod links",)),
    ("mapped_file_missing", ("was not found in the source directory",)),
    ("no_archive_matched", ("no matching archive",)),
    ("patcher_path_refusal", ("patcher tree", "installer", "namespace")),
    ("reader_rejection", ("no supported", "exceeds", "unsafe", "does not exist",
                          "no extension", "not utf-8", "invalid utf-8", "non-utf8",
                          "stream did not contain valid utf-8",
                          "archive links and special files")),
)
POLICY_CAUSE = "skipped_by_reviewed_policy"


def skip_cause(reason):
    lowered = reason.lower()
    for cause, needles in SKIP_CAUSES:
        if any(needle in lowered for needle in needles):
            return cause
    return "other"


def record_convert_skips(log, report):
    """Keep the converter's per-component skip report (a JSON array on stderr, merged into the log)."""
    try:
        text = log.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    decoder = json.JSONDecoder()
    for start in (match.start() for match in re.finditer(r"^\[$", text, re.MULTILINE)):
        try:
            results, _ = decoder.raw_decode(text, start)
        except ValueError:
            continue
        if (isinstance(results, list) and results
                and all(isinstance(item, dict) and "ComponentGuid" in item for item in results)):
            break
    else:
        return
    skipped = [{"component_guid": str(item["ComponentGuid"]).lower(),
                "name": item.get("ComponentName"),
                "cause": skip_cause(str(item.get("SkipReason") or "")),
                "reason": item.get("SkipReason")}
               for item in results if item.get("Success") is not True]
    causes = {}
    for item in skipped:
        causes[item["cause"]] = causes.get(item["cause"], 0) + 1
    report["convert_skips"] = {
        "total": len(results), "generated": len(results) - len(skipped), "by_cause": causes,
        "components": [item for item in skipped if item["cause"] != "already_had_instructions"]}


REVIEW_FLAG = ("DRAFT INSTRUCTIONS: parsed from guide prose by the natural-language importer. "
               "Review before installing - never auto-trusted.")
DRAFT_REVIEW_STAGE = "review_verified_drafts"


def record_draft_review(log, report):
    """Keep the converter's {cleared, skipped} report (a JSON object on stderr, merged into the log)."""
    try:
        text = log.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    decoder = json.JSONDecoder()
    for start in (match.start() for match in re.finditer(r"^\{$", text, re.MULTILINE)):
        try:
            result, _ = decoder.raw_decode(text, start)
        except ValueError:
            continue
        if (isinstance(result, dict) and isinstance(result.get("cleared"), list)
                and isinstance(result.get("skipped"), list)
                and all(isinstance(item, dict) and "guid" in item for item in result["skipped"])):
            break
    else:
        return
    report["draft_review"] = {
        "cleared": len(result["cleared"]),
        "skipped": [{"component_guid": str(item["guid"]).lower(),
                     "name": item.get("name"), "reason": item.get("reason")}
                    for item in result["skipped"]]}


def draft_pending(component):
    """True when this component or option still carries an unreviewed draft.

    Mirrors the structural part of the Rust requires_review: a DraftReview that is
    not RequiresReview=false, or one whose Result still lists gaps, conditions or
    rejected drafts; with no DraftReview, the prose-draft InstallationWarning. The
    reviewed-identity receipt itself is only checked by the Rust validate step.
    """
    if "DraftReview" in component:
        review = component["DraftReview"]
        if not isinstance(review, dict) or review.get("RequiresReview") is not False:
            return True
        result = review.get("Result")
        return (not isinstance(result, dict)
                or str(result.get("ComponentGuid", "")).lower() != str(component.get("Guid", "")).lower()
                or bool(result.get("UnparsedGaps") or result.get("ConditionalDrafts")
                        or result.get("RejectedDrafts")))
    warning = component.get("InstallationWarning")
    return isinstance(warning, str) and REVIEW_FLAG in warning


def pending_drafts(converted, exempt_guids, gui):
    """Selected components (and what they install) that still have an unreviewed draft.

    exempt_guids are reviewed-policy skips and GUI widescreen exclusions: they are
    never installed, so their drafts do not matter. The GUI lane installs every other
    component and refuses any nested draft; the CLI lane installs components with
    IsSelected = true (all, when none is selected, as the Rust loader does) and only
    their selected options.
    """
    document = tomllib.loads(converted.read_text(encoding="utf-8"))
    components = [component for component in document.get("thisMod", document.get("Components", []))
                  if isinstance(component, dict)]
    default_selected = not any(component.get("IsSelected") is True for component in components)
    pending = []

    def walk(component, owner):
        heading = component.get("Heading", component.get("Name"))
        if owner is not None:
            heading = f"{owner} / {heading}"
        if draft_pending(component):
            pending.append({"component_guid": str(component.get("Guid", "")).lower(),
                            "heading": heading})
        for option in component.get("Options", []):
            if isinstance(option, dict) and (gui or option.get("IsSelected") is True):
                walk(option, heading)

    for component in components:
        if str(component.get("Guid", "")).lower() in exempt_guids:
            continue
        if gui or default_selected or component.get("IsSelected") is True:
            walk(component, None)
    return pending


def reclassify_policy_skips(report, guids):
    """Move converter skips of reviewed-policy components out of their converter cause bucket."""
    skips = report.get("convert_skips")
    if not skips or not guids:
        return
    for item in skips["components"]:
        if item["component_guid"] in guids and item["cause"] != POLICY_CAUSE:
            counts = skips["by_cause"]
            counts[item["cause"]] -= 1
            if not counts[item["cause"]]:
                del counts[item["cause"]]
            item["cause"] = POLICY_CAUSE
            counts[POLICY_CAUSE] = counts.get(POLICY_CAUSE, 0) + 1


def print_diagnostics(report):
    """Put the skip causes and every uncovered step in the CI log so one read is enough."""
    review = report.get("draft_review")
    if review:
        print(f"ports: verified-draft review cleared {review.get('cleared', 0)} component(s); "
              f"left fenced: {len(review.get('skipped', []))}", file=sys.stderr)
        for item in review.get("unreviewed", []):
            print(f"ports: unreviewed draft still selected: {item['component_guid']} "
                  f"({item['heading']})", file=sys.stderr)
    skips = report.get("convert_skips")
    if skips:
        causes = ", ".join(f"{cause}={count}" for cause, count in sorted(skips["by_cause"].items())
                           if cause != POLICY_CAUSE)
        policy = skips["by_cause"].get(POLICY_CAUSE)
        suffix = f"; reviewed-policy-skipped={policy}" if policy else ""
        print(f"ports: convert generated instructions for {skips['generated']} of {skips['total']}"
              f" components; not generated: {causes or 'none'}{suffix}", file=sys.stderr)
    uncovered = report.get("steps_without_instructions")
    if uncovered:
        print(f"::error::ports: {len(uncovered)} guide steps have no instructions", file=sys.stderr)
        for step in uncovered:
            print(f"  - {step['heading']} [{step['component_guid']}] "
                  f"{step['skip_reason'] or 'no convert skip reason recorded'} "
                  f"<{' '.join(step['urls'])}>", file=sys.stderr)


IMPLEMENTATION_SECTIONS = {
    "rust": frozenset(),
    "typescript": frozenset({"CRITICAL: PC Version Differences & Fixes"}),
}


SKIPPED_BY_POLICY = "skipped-by-reviewed-policy"
SKIPPED_REPORT_KEY = "skipped_by_reviewed_policy"
LANE_SKIPS_PATH = Path(__file__).with_name("archive_selections.json")
LANE_SKIP_GAMES = ("k1", "k2")
LANE_SKIP_KEYS = frozenset({"section", "heading", "reason"})


def load_policy_skips(path, guide):
    """Read cache.py restore --report and return its reviewed skipped steps.

    Each step is {guide, section, heading, url, reason}; steps naming the same
    section and heading are merged into one record carrying every URL.
    """
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or document.get("ok") is not True:
        raise Blocked("restore_report_not_ok")
    steps = document.get("skipped_steps", [])
    if not isinstance(steps, list):
        raise Blocked("restore_report_skipped_steps_invalid")
    merged = {}
    for step in steps:
        if (not isinstance(step, dict)
                or any(not isinstance(step.get(key), str) or not step[key].strip()
                       for key in ("guide", "section", "heading", "url", "reason"))):
            raise Blocked("restore_report_skipped_step_invalid")
        if Path(step["guide"]).resolve() != guide:
            raise Blocked("restore_report_guide_mismatch")
        record = merged.setdefault((step["section"], step["heading"]), {
            "section": step["section"], "heading": step["heading"],
            "reason": step["reason"], "urls": [], "source": "restore_report"})
        if step["url"] not in record["urls"]:
            record["urls"].append(step["url"])
    return list(merged.values())


def load_lane_skips(game, path=None):
    """Return the reviewed lane skips for one game from archive_selections.json.

    The `lane_skips` table maps k1/k2 to a list of {section, heading, reason}
    objects. The whole table is validated whatever the game, so a malformed
    entry for the other game still blocks. A missing file or table skips nothing.
    """
    path = Path(path or LANE_SKIPS_PATH)
    if not path.is_file():
        return []
    document = json.loads(path.read_text(encoding="utf-8"))
    table = document.get("lane_skips", {}) if isinstance(document, dict) else None
    if not isinstance(table, dict) or not table.keys() <= set(LANE_SKIP_GAMES):
        raise Blocked("lane_skips_invalid")
    result = []
    for name, entries in table.items():
        if not isinstance(entries, list):
            raise Blocked("lane_skips_invalid")
        seen = set()
        for entry in entries:
            if (not isinstance(entry, dict) or entry.keys() != LANE_SKIP_KEYS
                    or any(not isinstance(entry[key], str) or not entry[key].strip()
                           or entry[key] != entry[key].strip() for key in LANE_SKIP_KEYS)):
                raise Blocked("lane_skips_invalid")
            key = (entry["section"], entry["heading"])
            if key in seen:
                raise Blocked("lane_skips_duplicate")
            seen.add(key)
            if name == game:
                result.append({"section": entry["section"], "heading": entry["heading"],
                               "reason": entry["reason"], "urls": [], "source": "lane_skips"})
    return result


def merge_lane_skips(restore_skips, lane_skips):
    """Restore-report skips first; a lane skip naming the same step adds nothing."""
    known = {(skip["section"], skip["heading"]) for skip in restore_skips}
    return list(restore_skips) + [skip for skip in lane_skips
                                  if (skip["section"], skip["heading"]) not in known]


def uncovered_steps(report, headings, components, indexes):
    """Heading, GUID, links and the converter's skip reason for each step lacking instructions."""
    reasons = {item["component_guid"]: item["reason"]
               for item in report.get("convert_skips", {}).get("components", [])}
    steps = []
    for index in indexes:
        component = components[index] if isinstance(components[index], dict) else {}
        identity = str(component.get("Guid", "")).lower()
        steps.append({"heading": headings[index], "component_guid": identity,
                      "urls": [urldefrag(url).url for url in component.get("ResourceRegistry", {})],
                      "skip_reason": reasons.get(identity)})
    return steps


def coverage(guide, converted, report=None, implementation="rust", skipped=()):
    text = guide.read_text(encoding="utf-8")
    if implementation not in IMPLEMENTATION_SECTIONS:
        raise Blocked("implementation_unknown")
    allowed_sections = {
        "Mod List",
        "Optional Widescreen & Extended Widescreen Support Mods",
        "Enhanced Widescreen Support Mods",
    } | IMPLEMENTATION_SECTIONS[implementation]
    section = None
    headings = []
    steps = []
    guide_steps = set()
    sections = {}
    for line in text.splitlines():
        if line.startswith("# "):
            section = None
        elif line.startswith("## "):
            section = line[3:].strip()
            if section in allowed_sections:
                sections.setdefault(section, 0)
        elif line.startswith("### "):
            guide_steps.add((section, line[4:].strip()))
            if section in allowed_sections:
                headings.append(line[4:].strip())
                steps.append((section, line[4:].strip()))
                sections[section] += 1
    if "Mod List" not in sections:
        raise Blocked("unsupported_guide_structure")
    document = tomllib.loads(converted.read_text(encoding="utf-8"))
    components = document.get("thisMod", document.get("Components", []))
    if report is not None:
        report["source_component_count"] = len(headings)
        report["source_section_counts"] = sections
        report["converted_component_count"] = (
            len(components) if isinstance(components, list) else None
        )
    if not headings or not isinstance(components, list) or len(components) != len(headings):
        raise Blocked("guide_component_coverage_mismatch")
    exempt = set()
    records = []
    for skip in skipped:
        key = (skip["section"], skip["heading"])
        if key not in guide_steps:
            raise Blocked("policy_skipped_step_not_in_guide")
        record = {"status": SKIPPED_BY_POLICY, **skip, "component_guid": None}
        if key in steps:
            if steps.count(key) != 1:
                raise Blocked("policy_skipped_step_ambiguous")
            index = steps.index(key)
            component = components[index]
            if not isinstance(component, dict) or not component.get("Guid"):
                raise Blocked("policy_skipped_component_unidentified")
            identity = guid(component["Guid"])
            if any(isinstance(other, dict) and identity in [
                    str(each).lower() for each in other.get("Dependencies", [])]
                   for other in components if other is not component):
                raise Blocked("policy_skipped_component_has_dependents")
            record["component_guid"] = identity
            exempt.add(index)
        records.append(record)
    if report is not None:
        report[SKIPPED_REPORT_KEY] = records
        reclassify_policy_skips(report, {record["component_guid"] for record in records
                                         if record["component_guid"] is not None})
    uncovered = [index for index, component in enumerate(components)
                 if index not in exempt and (not isinstance(component, dict)
                                             or not component.get("Instructions"))]
    if uncovered:
        if report is not None:
            report["steps_without_instructions"] = uncovered_steps(
                report, headings, components, uncovered)
        raise Blocked("guide_steps_without_instructions")
    if any(component.get("Heading", component.get("Name")) != heading
           for component, heading in zip(components, headings)):
        raise Blocked("guide_component_order_or_heading_mismatch")
    return len(components)


def gui_widescreen_scope(converted, policy_guids):
    """Widescreen-only components the Rust GUI lane does not install, and whether any exist.

    The GUI driver has a single install phase. Widescreen-only components that
    are not already reviewed-policy skips are excluded and reported as not
    covered; this mirrors gui_rust.plan_selection, which re-derives it.
    """
    document = tomllib.loads(converted.read_text(encoding="utf-8"))
    components = document.get("thisMod", document.get("Components", []))
    widescreen = [component for component in components
                  if isinstance(component, dict) and component.get("WidescreenOnly") is True]
    excluded = [{"component_guid": str(component.get("Guid", "")).lower(),
                 "heading": component.get("Heading", component.get("Name"))}
                for component in widescreen
                if str(component.get("Guid", "")).lower() not in policy_guids]
    return excluded, bool(widescreen)


def select_base_components(converted):
    """Write IsSelected=true for every base-phase, platform-neutral component.

    Widescreen and Aspyr-only components stay unselected: the GUI driver
    refuses them, and the CLI installs them through its own phase flow.
    """
    text = converted.read_text(encoding="utf-8")
    document = tomllib.loads(text)
    key = "thisMod" if "thisMod" in document else "Components"
    components = document.get(key)
    if not isinstance(components, list) or not components:
        raise Blocked("selection_components_missing")
    expected = [not (component.get("IsWidescreen", False)
                     or component.get("AspyrOnly", False))
                for component in components]
    header = "[[" + key + "]]"
    index = -1
    inside = False
    seen = 0
    lines = []
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if stripped.startswith("["):
            inside = stripped == header
            if inside:
                index += 1
        elif inside and re.fullmatch(r"IsSelected\s*=\s*(true|false)", stripped):
            line = "IsSelected = " + ("true" if expected[index] else "false") + "\n"
            seen += 1
        lines.append(line)
    if index + 1 != len(components) or seen != len(components):
        raise Blocked("selection_rewrite_incomplete")
    rewritten = "".join(lines)
    updated = tomllib.loads(rewritten)
    if [component.get("IsSelected") for component in updated[key]] != expected:
        raise Blocked("selection_rewrite_mismatch")
    for document_part in (document, updated):
        for component in document_part[key]:
            component.pop("IsSelected", None)
    if updated != document:
        raise Blocked("selection_rewrite_changed_other_fields")
    converted.write_text(rewritten, encoding="utf-8")
    return sum(expected)


def guid(value):
    if not isinstance(value, str):
        raise Blocked("review_manifest_guid_invalid")
    return str(uuid.UUID(value))


def review_manifest(path, guide, converted):
    """Require a separately authored JSON review, never manufacture approval.

    Version 1: schema_version=1, reviewer (nonempty string), unresolved=[],
    guide_sha256, converted_sha256, components (in converted order).
    Each component: guid, instructions (exact TOML Instructions records),
    options (exact TOML Options records, or []), phase (base/widescreen),
    assessment (nonempty string), unresolved=[], choices ({instruction GUID:
    CLI answer}). Choose/Patcher/Execute instructions require explicit answers,
    including instructions in options. Other answers may be supplied as needed.
    """
    if path is None:
        raise Blocked("review_manifest_required")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise Blocked("review_manifest_schema_invalid")
    reviewer = manifest.get("reviewer")
    if not isinstance(reviewer, str) or not reviewer.strip():
        raise Blocked("review_manifest_reviewer_required")
    if manifest.get("unresolved") != []:
        raise Blocked("review_manifest_unresolved")
    if (manifest.get("guide_sha256") != digest(guide)
            or manifest.get("converted_sha256") != digest(converted)):
        raise Blocked("review_manifest_hash_mismatch")
    document = tomllib.loads(converted.read_text(encoding="utf-8"))
    components = document.get("thisMod", document.get("Components", []))
    reviews = manifest.get("components")
    if not isinstance(reviews, list) or len(reviews) != len(components):
        raise Blocked("review_manifest_component_coverage_mismatch")
    seen = set()
    instruction_ids = set()
    answers = []
    for component, review in zip(components, reviews):
        if not isinstance(review, dict):
            raise Blocked("review_manifest_component_invalid")
        component_id = guid(component["Guid"])
        if component_id in seen or guid(review["guid"]) != component_id:
            raise Blocked("review_manifest_component_order_or_guid_mismatch")
        seen.add(component_id)
        if (review.get("instructions") != component.get("Instructions", [])
                or review.get("options") != component.get("Options", [])):
            raise Blocked("review_manifest_instructions_mismatch")
        phase = "widescreen" if component.get("IsWidescreen", False) else "base"
        if (review.get("phase") != phase or review.get("unresolved") != []
                or not isinstance(review.get("assessment"), str)
                or not review["assessment"].strip()):
            raise Blocked("review_manifest_phase_or_assessment_unresolved")
        choices = review.get("choices")
        if not isinstance(choices, dict):
            raise Blocked("review_manifest_choices_required")
        normalized = {}
        for key, answer in choices.items():
            key = guid(key)
            if key in normalized or not isinstance(answer, str) or not answer.strip():
                raise Blocked("review_manifest_choice_invalid")
            normalized[key] = answer
        instructions = list(component.get("Instructions", []))
        for option in component.get("Options", []):
            instructions.extend(option.get("Instructions", []))
        local_ids = set()
        for instruction in instructions:
            instruction_id = guid(instruction["Guid"])
            if instruction_id in instruction_ids:
                raise Blocked("review_manifest_duplicate_instruction_guid")
            instruction_ids.add(instruction_id)
            local_ids.add(instruction_id)
            if (instruction.get("Action") in ("Choose", "Patcher", "Execute")
                    and instruction_id not in normalized):
                raise Blocked("review_manifest_prompt_choice_missing")
        if not normalized.keys() <= local_ids:
            raise Blocked("review_manifest_unknown_choice_guid")
        for key, answer in normalized.items():
            answers.extend(["--choice", key + "=" + answer])
    return answers


def execute(args, report):
    source = args.source.resolve(strict=True)
    guide = args.guide.resolve(strict=True)
    game = args.game_dir.resolve(strict=True)
    archives = args.archives.resolve(strict=True)
    work = args.work_dir.resolve()
    if not source.is_dir() or not game.is_dir() or not archives.is_dir():
        raise Blocked("required_directory_missing")
    if not guide.is_file() or guide.name != "full.md":
        raise Blocked("full_markdown_guide_required")
    protected = (source, game, archives, guide.parent)
    if any(work == path or work.is_relative_to(path) or path.is_relative_to(work)
           for path in protected):
        raise Blocked("work_directory_overlaps_inputs")
    if any(game == path or game.is_relative_to(path) or path.is_relative_to(game)
           for path in (source, archives, guide.parent)):
        raise Blocked("game_directory_overlaps_inputs")
    work.mkdir(parents=True, exist_ok=True)
    private = Path(tempfile.mkdtemp(prefix="ports-", dir=work))
    private.chmod(0o700)
    report["guide_sha256"] = digest(guide)
    env = os.environ.copy()
    env["CARGO_TARGET_DIR"] = str(private / "target")
    converted = private / "converted.toml"
    if args.implementation == "rust":
        if not (source / "Cargo.toml").is_file():
            raise Blocked("rust_source_manifest_missing")
        run(["cargo", "build", "--locked", "--bin", "modsync"],
            source, private, "build", report, env)
        command = [str(private / "target/debug/modsync")]
        archive_map = []
        if args.archive_map:
            archive_map = ["--archive-map", str(args.archive_map.resolve(strict=True))]
        try:
            run(command + ["convert", str(guide), str(converted),
                           "--format", "toml", "--parse-directions",
                           "--auto-generate-local", "--source-path", str(archives)] + archive_map,
                source, private, "convert", report, env)
        finally:
            record_convert_skips(private / "convert.log", report)
        reviewed = private / "converted-reviewed.toml"
        try:
            run(command + ["convert", str(converted), str(reviewed), "--format", "toml",
                           "--source-path", str(archives), "--review-verified-drafts"],
                source, private, DRAFT_REVIEW_STAGE, report, env)
        finally:
            record_draft_review(private / (DRAFT_REVIEW_STAGE + ".log"), report)
        if not reviewed.is_file() or reviewed.is_symlink():
            raise Blocked("review_verified_drafts_output_missing")
        reviewed.replace(converted)
    else:
        if not (source / "packages/cli/src/main.ts").is_file():
            raise Blocked("typescript_source_entrypoint_missing")
        run(["pnpm", "build"], source, private, "build", report, env)
        command = ["node", str(source / "packages/cli/dist/main.js")]
        if guide.parent.name not in ("k1", "k2"):
            raise Blocked("guide_target_game_unknown")
        target_game = {"k1": "kotor1", "k2": "kotor2"}[guide.parent.name]
        run(command + ["import-md", str(guide), "--output", str(converted),
                       "--game", target_game,
                       "--source-url", "content/" + guide.parent.name + "/full.md"],
            source, private, "convert", report, env)
        report["selected_component_count"] = select_base_components(converted)
    if digest(guide) != report["guide_sha256"]:
        raise Blocked("guide_changed_during_conversion")
    report["converted_sha256"] = digest(converted)
    report["stage"] = "coverage"
    skipped = []
    if args.restore_report is not None:
        if args.implementation != "rust":
            raise Blocked("policy_skips_supported_for_rust_only")
        skipped = load_policy_skips(args.restore_report.resolve(strict=True), guide)
    if args.implementation == "rust":
        skipped = merge_lane_skips(skipped, load_lane_skips(guide.parent.name))
    report["component_count"] = coverage(
        guide, converted, report, args.implementation, skipped)
    deselect = []
    for record in report.get(SKIPPED_REPORT_KEY, []):
        if record["component_guid"] is not None:
            deselect += ["--deselect", record["component_guid"]]
    policy_guids = {record["component_guid"] for record in report.get(SKIPPED_REPORT_KEY, [])
                    if record["component_guid"] is not None}
    gui_rust = args.implementation == "rust" and args.mode == "gui"
    excluded, has_widescreen = ([], False)
    if gui_rust:
        excluded, has_widescreen = gui_widescreen_scope(converted, policy_guids)
        report["gui_not_covered"] = {
            "widescreen_phase": "not_covered_by_gui_lane" if has_widescreen else "none_in_input",
            "excluded_widescreen": excluded}
        deselect += [flag for item in excluded for flag in ("--deselect", item["component_guid"])]
    if args.implementation == "rust":
        report["stage"] = "draft_review"
        pending = pending_drafts(
            converted, policy_guids | {item["component_guid"] for item in excluded}, gui_rust)
        if pending:
            report.setdefault("draft_review", {})["unreviewed"] = pending
            raise Blocked("unreviewed_draft_instructions_remain: " + "; ".join(
                f"{item['component_guid']} ({item['heading']})" for item in pending))
    if args.implementation == "typescript":
        report["stage"] = "review"
        choices = review_manifest(args.review_manifest, guide, converted)
        directories = [str(converted), "--kotor", str(game),
                       "--mods", str(archives), "--all"]
        run(command + ["validate"] + directories,
            source, private, "validate", report, env)
        if args.mode == "gui":
            if (digest(guide) != report["guide_sha256"]
                    or digest(converted) != report["converted_sha256"]):
                raise Blocked("reviewed_inputs_changed")
            driver_report = private / "gui-report.json"
            gui_work = private / "gui-work"
            run([sys.executable, str(Path(__file__).resolve().with_name("gui_typescript.py")),
                 "--source", str(source), "--input", str(converted),
                 "--game-dir", str(game), "--archives", str(archives),
                 "--work-dir", str(gui_work), "--report", str(driver_report),
                 "--timeout", "7000"],
                source, private, "gui", report, env)
            if not driver_report.is_file() or driver_report.is_symlink():
                raise Blocked("gui_completion_report_missing")
            result = json.loads(driver_report.read_text(encoding="utf-8"))
            if (not isinstance(result, dict)
                    or result.get("schema_version") != 1
                    or result.get("implementation") != "typescript"
                    or result.get("mode") != "gui"
                    or result.get("runtime") != "playwright-web"
                    or result.get("status") != "installed"
                    or result.get("reason") != "real_ui_install_and_checkpoint_hashes_verified"
                    or result.get("ui_completed") is not True
                    or result.get("input_sha256") != report["converted_sha256"]
                    or type(result.get("verified_game_files")) is not int
                    or result["verified_game_files"] <= 0):
                raise Blocked("gui_completion_not_verified")
            receipts = result.get("checkpoint_receipts")
            if (not isinstance(receipts, dict) or "session.json" not in receipts
                    or len(receipts) < 3 or any(
                        not isinstance(checksum, str)
                        or re.fullmatch(r"[0-9a-f]{64}", checksum) is None
                        for checksum in receipts.values())):
                raise Blocked("gui_checkpoint_receipts_missing")
            evidence = Path(result["evidence_directory"])
            if evidence.is_symlink() or not evidence.resolve().is_relative_to(gui_work.resolve()):
                raise Blocked("gui_evidence_directory_invalid")
            for name, key in (("install-status.json", "receipt_sha256"),
                              ("verified-output-hashes.json", "output_manifest_sha256")):
                artifact = evidence / name
                if (artifact.is_symlink() or not artifact.is_file()
                        or digest(artifact) != result.get(key)):
                    raise Blocked("gui_completion_hash_mismatch")
            outputs = json.loads((evidence / "verified-output-hashes.json").read_text())
            if not isinstance(outputs, dict) or len(outputs) != result["verified_game_files"]:
                raise Blocked("gui_output_manifest_invalid")
            for relative, checksum in outputs.items():
                target = game / relative
                if (Path(relative).is_absolute() or ".." in Path(relative).parts
                        or target.is_symlink() or not target.resolve().is_relative_to(game)
                        or not target.is_file() or digest(target) != checksum):
                    raise Blocked("gui_output_hash_mismatch")
            if (digest(guide) != report["guide_sha256"]
                    or digest(converted) != report["converted_sha256"]):
                raise Blocked("reviewed_inputs_changed")
            report["gui"] = result
            report["proof"] = "native_gui_and_verified_completion_receipt"
            report["status"] = "installed"
            report["reason"] = result["reason"]
            return
        for phase in ("base", "widescreen"):
            if (digest(guide) != report["guide_sha256"]
                    or digest(converted) != report["converted_sha256"]):
                raise Blocked("reviewed_inputs_changed")
            run(command + ["install"] + directories + ["--phase", phase] + choices,
                source, private, "install_" + phase, report, env)
        report["status"] = "installed"
        report["reason"] = "native_install_commands_succeeded"
        return
    directories = ["--input", str(converted), "--game-dir", str(game),
                   "--source-dir", str(archives)]
    run(command + ["validate"] + directories + deselect,
        source, private, "validate", report, env)
    if args.mode == "gui":
        if (digest(guide) != report["guide_sha256"]
                or digest(converted) != report["converted_sha256"]):
            raise Blocked("reviewed_inputs_changed")
        driver_report = private / "gui-report.json"
        run([sys.executable, str(Path(__file__).resolve().with_name("gui_rust.py")),
             "--binary", command[0], "--input", str(converted),
             "--game-dir", str(game), "--archives", str(archives),
             "--work-dir", str(private / "gui-work"),
             "--report", str(driver_report), "--timeout", "7000", "--exclude-widescreen"]
            + [flag for guid_ in sorted(policy_guids) for flag in ("--deselect", guid_)],
            source, private, "gui", report, env)
        if not driver_report.is_file() or driver_report.is_symlink():
            raise Blocked("gui_completion_report_missing")
        result = json.loads(driver_report.read_text(encoding="utf-8"))
        expected_completed = (report["component_count"] - len(policy_guids) - len(excluded))
        if (not isinstance(result, dict)
                or result.get("schema_version") != 1
                or result.get("implementation") != "rust"
                or result.get("mode") != "gui"
                or result.get("status") != "installed"
                or result.get("reason") != "native_gui_completed_and_receipt_verified"
                or result.get("ui_completed") is not True
                or result.get("input_sha256") != report["converted_sha256"]
                or result.get("binary_sha256") != digest(Path(command[0]))
                or result.get("component_count") != report["component_count"]
                or result.get("completed_components") != expected_completed
                or result.get("expected_components") != expected_completed
                or result.get("skipped_by_reviewed_policy") != sorted(policy_guids)
                or result.get("excluded_widescreen") != sorted(
                    item["component_guid"] for item in excluded)
                or result.get("widescreen_phase") != report["gui_not_covered"]["widescreen_phase"]
                or result.get("coverage") != ("subset" if policy_guids or excluded else "full")
                or not isinstance(result.get("verified_game_files"), int)
                or result["verified_game_files"] <= 0
                or not isinstance(result.get("receipt_sha256"), str)
                or re.fullmatch(r"[0-9a-f]{64}", result["receipt_sha256"]) is None):
            raise Blocked("gui_completion_not_verified")
        if (digest(guide) != report["guide_sha256"]
                or digest(converted) != report["converted_sha256"]):
            raise Blocked("reviewed_inputs_changed")
        report["gui"] = result
        report["gui_covered_components"] = expected_completed
        report["proof"] = "native_gui_and_verified_completion_receipt"
        report["status"] = "installed"
        report["reason"] = result["reason"]
        return
    run(command + ["install"] + directories + deselect
        + ["--non-interactive", "--phase", "all"],
        source, private, "install", report, env)
    report["status"] = "installed"
    report["reason"] = "native_install_command_succeeded"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--implementation", choices=("rust", "typescript"), required=True)
    parser.add_argument("--mode", choices=("cli", "gui"), required=True)
    parser.add_argument("--review-manifest", type=Path,
                        help="separately authored hash-bound JSON review (TypeScript required)")
    parser.add_argument("--archive-map", type=Path,
                        help="cache.py restore --archive-map JSON passed to the Rust convert (ignored by TypeScript)")
    parser.add_argument("--restore-report", type=Path,
                        help="cache.py restore --report JSON; its reviewed skipped_steps are exempted from "
                             "coverage, recorded in the report and deselected (Rust only; TypeScript is refused). "
                             "Rust lanes also apply the reviewed lane_skips of archive_selections.json")
    for name in ("source", "guide", "game-dir", "archives", "work-dir", "report"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args(argv)
    report = {"schema_version": 1, "implementation": args.implementation,
              "mode": args.mode, "status": "blocked", "stage": "preflight",
              "commands": [], "proof": "native_commands_only"}
    previous_umask = os.umask(0o077)
    try:
        destination = args.report.absolute()
        inputs = [path.resolve() for path in
                  (args.source, args.game_dir, args.archives, args.guide.parent)]
        if destination.is_symlink() or destination.exists() or any(
            destination.resolve().is_relative_to(path) for path in inputs
        ):
            print("ports: report destination must be new and outside inputs", file=sys.stderr)
            return 2
        try:
            execute(args, report)
        except Blocked as error:
            report["reason"] = str(error)
        except (OSError, ValueError, TypeError, KeyError):
            report["reason"] = "input_tool_or_document_error"
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("x", encoding="utf-8") as output:
            json.dump(report, output, indent=2)
            output.write("\n")
        print(json.dumps(report))
        print_diagnostics(report)
        return 0 if report["status"] == "installed" else 1
    except OSError:
        print("ports: unable to write sanitized report", file=sys.stderr)
        return 2
    finally:
        os.umask(previous_umask)


if __name__ == "__main__":
    sys.exit(main())
