#!/usr/bin/env python3
"""Private-source, fail-closed full-guide port runner (Python 3.11+)."""

import argparse
from cache import link_key
import gui_diag
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import tomllib
from urllib.parse import unquote, urldefrag, urlsplit, urlunsplit
import uuid


class Blocked(Exception):
    pass


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


FAILURE_TAIL_LINES = 80
GUI_STAGE = "gui"
STAGE_DURATIONS_KEY = "stage_durations"
GUI_REPORT_NAME = "gui-report.json"
GUI_INSTALL_FAILURE_REASON = "backend_install_gate_or_failure"
GUI_INSTALL_FAILURES_KEY = "gui_install_failures"
GUI_TAIL_LINES = 200
GUI_TAIL_GROUP = "::group::gui worker tail"
DIAGNOSTICS_MAX_DEPTH = 4
DIAGNOSTICS_MAX_ITEMS = 400


def sanitize_diagnostics(value, depth=0):
    """Redact every string again and bound the shape of driver-supplied diagnostics."""
    if isinstance(value, str):
        redacted = gui_diag.redact(value)
        return redacted if len(redacted) <= gui_diag.LINE_LIMIT else redacted[:gui_diag.LINE_LIMIT] + "..."
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if depth >= DIAGNOSTICS_MAX_DEPTH:
        return None
    if isinstance(value, list):
        return [sanitize_diagnostics(item, depth + 1) for item in value[-DIAGNOSTICS_MAX_ITEMS:]]
    if isinstance(value, dict):
        return {sanitize_diagnostics(str(key), depth + 1): sanitize_diagnostics(item, depth + 1)
                for key, item in list(value.items())[:DIAGNOSTICS_MAX_ITEMS]}
    return None


def load_worker_diagnostics(path):
    """The driver report's `worker_diagnostics`, sanitized, or None when absent or unreadable."""
    try:
        if path.is_symlink() or not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    diagnostics = data.get("worker_diagnostics") if isinstance(data, dict) else None
    if not isinstance(diagnostics, dict) or not diagnostics:
        return None
    return sanitize_diagnostics(diagnostics)


def gui_stage_name(attempt):
    """`gui` for the first GUI attempt, `gui_retry_N` for the quarantine retries."""
    return GUI_STAGE if attempt == 0 else f"{GUI_STAGE}_retry_{attempt}"


def is_gui_stage(stage):
    return stage == GUI_STAGE or stage.startswith(GUI_STAGE + "_retry_")


def gui_report_name(stage):
    return GUI_REPORT_NAME if stage == GUI_STAGE else f"{stage}-report.json"


def reemit_gui_failure(text, driver_report, report):
    """Make a failed GUI driver visible in the plain CI log and copy its diagnostics into the lane report.

    The driver prints its own `::group::gui worker tail` block (already redacted) just before
    its final JSON line; it is re-emitted without an enclosing group because Actions groups do
    not nest. Every line is redacted again before it reaches the log.
    """
    lines = [gui_diag.CONTROL.sub("", gui_diag.redact(line))
             for line in text.splitlines()[-GUI_TAIL_LINES:]]
    print(f"gui output (last {GUI_TAIL_LINES} lines)", file=sys.stderr)
    print("\n".join(lines), file=sys.stderr)
    diagnostics = load_worker_diagnostics(driver_report)
    if diagnostics is None:
        return
    report["worker_diagnostics"] = diagnostics
    if not any(line.startswith(GUI_TAIL_GROUP) for line in lines):
        print("\n".join(gui_diag.render_group(diagnostics)), file=sys.stderr)


def progress(message):
    """One progress line in the CI log, flushed so it shows while the lane is still running."""
    print("ports: " + message, file=sys.stderr, flush=True)


def format_duration(seconds):
    seconds = max(float(seconds), 0.0)
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, rest = divmod(round(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m{rest:02d}s" if hours else f"{minutes}m{rest:02d}s"


def finish_stage(report, stage, started, outcome):
    """Record a stage's wall-clock seconds in report["stage_durations"] and log the finish line."""
    seconds = time.monotonic() - started
    report.setdefault(STAGE_DURATIONS_KEY, {})[stage] = round(seconds, 3)
    progress(f"{stage}: finished in {format_duration(seconds)} ({outcome})")
    return seconds


def run(command, source, private, stage, report, env):
    report["stage"] = stage
    log = private / (stage + ".log")
    started = time.monotonic()
    progress(f"{stage}: started")
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
            finish_stage(report, stage, started, "interrupted or timed out")
            raise Blocked("command_interrupted_or_timed_out") from None
    finish_stage(report, stage, started, f"exit {code}")
    report["commands"].append({"stage": stage, "exit_code": code})
    if code:
        text = log.read_text(encoding="utf-8", errors="replace")
        if is_gui_stage(stage):
            reemit_gui_failure(text, private / gui_report_name(stage), report)
        else:
            lines = text.splitlines()[-FAILURE_TAIL_LINES:]
            print(f"::group::{stage} output (last {FAILURE_TAIL_LINES} lines)", file=sys.stderr)
            print("\n".join(lines), file=sys.stderr)
            print("::endgroup::", file=sys.stderr)
        record_failure(text, stage, code, report, print_excerpt=True)
        raise Blocked(stage + "_failed")


ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
EVIDENCE_LINES_BEFORE = 60
EVIDENCE_LINE_CAP = 200
EVIDENCE_LINE_WIDTH = 600
ERROR_LINE = re.compile(r"(?:\bError|\bException):|\[Error\]")
STOP_MARKER = re.compile(r"(?:install(?:ation)?|cli command)\s+stopped", re.IGNORECASE)
FAILURE_WORDS = re.compile(r"error|failed", re.IGNORECASE)
VALIDATE_EVIDENCE_MAX_BYTES = 128 * 1024
VALIDATE_DOWNLOAD_KINDS = frozenset(("missing-source", "missing-archive"))
VALIDATE_SECTION = re.compile(r"^(?P<severity>[A-Za-z]+) \((?P<count>\d+)\)\s*$")
VALIDATE_ERROR_TAG = re.compile(r"^\s*\[(?:patcher-error|missing-source|missing-archive)\]\s")
VALIDATE_COMPONENT_ERROR = re.compile(
    r"^\s*\[(?P<kind>patcher-error|missing-source|missing-archive)\]\s+"
    r"(?P<component>.+?): (?P=component)"
    r"(?: / (?P<action>[^\s(:]+)(?: \((?P<detail>.*?)\))?)?: (?P<message>.*?)\s*$")
COMPONENT_FAILURE = re.compile(
    r"(?:Error|Exception): (?:Install stopped\. Restore before retrying; snapshot: .+?: )?"
    r"(?P<component>.+?) / step (?P<step>\d+) "
    r"\((?P<action>[^)]*)\): (?P<message>.*?)\s*$")
TS_LOG_PREFIX = r"(?:\d{4}-\d\d-\d\dT[\d:.]+Z\s+)?"
TS_MOD_PROGRESS = re.compile(
    r"^" + TS_LOG_PREFIX + r"\[(?P<index>\d+)/(?P<total>\d+)\]\s+(?P<name>\S.*?)\s*$")
TS_STEP_PROGRESS = re.compile(r"^" + TS_LOG_PREFIX + r"\s+(?P<step>\d+)\.\s+(?P<action>[A-Za-z]+):")
TS_INSTRUCTION_FAILURE = re.compile(
    r"\bERROR: Instruction (?P<guid>[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12})"
    r" failed \((?P<code>[^)]*)\)(?:; installation stopped with recovery pending)?(?:: (?P<message>.*?))?\s*$")
TS_CHECKPOINT_ID = re.compile(r"\[checkpoint (?P<id>[0-9A-Za-z-]+)\]")
TS_CHECKPOINT_SESSION = re.compile(r"^" + TS_LOG_PREFIX + r"Checkpoint session: (?P<id>[0-9A-Za-z-]+)\s*$")
SECRET_PATTERNS = (
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}"), r"\1 ***"),
    (re.compile(r"(?i)\b([\w-]*(?:token|secret|passw(?:or)?d|api[_-]?key|access[_-]?key|authorization"
                r"|credential|signature)[\w-]*)\s*[=:]\s*\S+"), r"\1=***"),
    (re.compile(r"://[^/\s:@]+:[^/\s@]+@"), "://***@"),
    (re.compile(r"(?i)([?&](?:token|key|sig|signature|x-amz-[a-z-]+)=)[^&\s]+"), r"\1***"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"), "***"),
)


def clean_line(line):
    """One log line without terminal escapes, secrets, or text a CI runner would read as a command."""
    line = ANSI_ESCAPE.sub("", line).rstrip()
    for pattern, replacement in SECRET_PATTERNS:
        line = pattern.sub(replacement, line)
    if len(line) > EVIDENCE_LINE_WIDTH:
        line = line[:EVIDENCE_LINE_WIDTH] + " ...[truncated]"
    if line.startswith(("::", "##[")):
        line = " " + line
    return line


def failure_excerpt(text, before=EVIDENCE_LINES_BEFORE, cap=EVIDENCE_LINE_CAP):
    """A bounded excerpt of a failed tool's output: its Error/Exception lines with context.

    Keeps each Error:/Exception: line with up to `before` lines ahead of it and, after the first
    install-stopped marker (kept too), every line that mentions an error or a failure. At most `cap` lines
    (the last ones win); gaps are marked and the result is secret-scrubbed.
    """
    lines = [ANSI_ESCAPE.sub("", line) for line in text.splitlines()]
    keep = set()
    for index, line in enumerate(lines):
        if ERROR_LINE.search(line):
            keep.update(range(max(0, index - before), index + 1))
    marker = next((index for index, line in enumerate(lines) if STOP_MARKER.search(line)), None)
    if marker is not None:
        keep.add(marker)
        keep.update(index for index in range(marker, len(lines)) if FAILURE_WORDS.search(lines[index]))
    ordered = sorted(keep)
    omitted = max(0, len(ordered) - cap)
    ordered = ordered[omitted:]
    excerpt = []
    if omitted:
        excerpt.append(f"[{omitted} earlier excerpt line(s) omitted]")
    previous = None
    for index in ordered:
        if previous is not None and index != previous + 1:
            excerpt.append("[...]")
        excerpt.append(clean_line(lines[index]))
        previous = index
    return excerpt


def component_failure_fields(match):
    return {"component": match.group("component").strip(), "step": int(match.group("step")),
            "action": match.group("action").strip(),
            "message": clean_line(match.group("message")),
            "error_line": clean_line(match.group(0))}


def parse_typescript_install_failures(text):
    """Every `ERROR: Instruction <guid> failed (<Code>)...` line of a TypeScript CLI install log.

    The TypeScript CLI names the failing instruction by GUID, not the component. The component and
    step are the ones in force on that line: the last `[i/N] <Component>` progress line and the last
    `  N. <Action>: ...` step line after it. `component` is None when no progress line precedes the
    error; `action` is "" when no step line follows the progress line. A line repeated counts once.
    """
    found = []
    seen = set()
    component = step = action = None
    for raw in text.splitlines():
        line = ANSI_ESCAPE.sub("", raw).rstrip()
        progress_line = TS_MOD_PROGRESS.match(line)
        if progress_line:
            component, step, action = progress_line.group("name").strip(), None, ""
            continue
        step_line = TS_STEP_PROGRESS.match(line)
        if step_line:
            step, action = int(step_line.group("step")), step_line.group("action")
            continue
        match = TS_INSTRUCTION_FAILURE.search(line)
        if match is None:
            continue
        fields = {"component": component, "step": step, "action": action or "",
                  "message": clean_line(match.group("message") or match.group("code")),
                  "error_line": clean_line(match.group(0)),
                  "instruction_guid": normalized_reference(match.group("guid")),
                  "result_code": match.group("code")}
        key = (fields["instruction_guid"], fields["component"], fields["message"])
        if key not in seen:
            seen.add(key)
            found.append(fields)
    return found


def parse_component_failures(text):
    """Every distinct `Error: <Component> / step N (<Action>): <message>` line of a tool's output.

    A line repeated (the log and the final error) counts once, at its last position. A log with no
    such line is read as a TypeScript CLI install log (`parse_typescript_install_failures`).
    """
    found = []
    seen = set()
    for line in text.splitlines():
        match = COMPONENT_FAILURE.search(ANSI_ESCAPE.sub("", line))
        if match:
            fields = component_failure_fields(match)
            key = (fields["component"], fields["step"], fields["action"], fields["message"])
            if key in seen:
                found = [item for item in found
                         if (item["component"], item["step"], item["action"], item["message"]) != key]
            seen.add(key)
            found.append(fields)
    return found or [item for item in parse_typescript_install_failures(text) if item["component"]]


def parse_component_failure(text):
    """The last `Error: <Component> / step N (<Action>): <message>` line of a tool's output, or None.

    Falls back to the last TypeScript `ERROR: Instruction <guid> failed` line that has a progress line.
    """
    found = None
    for line in text.splitlines():
        match = COMPONENT_FAILURE.search(ANSI_ESCAPE.sub("", line))
        if match:
            found = match
    if found is not None:
        return component_failure_fields(found)
    attributed = [item for item in parse_typescript_install_failures(text) if item["component"]]
    return attributed[-1] if attributed else None


def validate_error_lines(text):
    """Every error-severity line of a TypeScript `validate` report, in order.

    The report groups lines under `ERROR (n)` / `INFO (n)` headings: everything under an ERROR
    heading is an error line, and a tagged line (`[patcher-error]`, `[missing-source]`,
    `[missing-archive]`) is one wherever no other severity heading is in force. The `FAILED:`
    trailer closes the section. Nothing is dropped by count, only by the caller's byte cap.
    """
    lines = []
    severity = None
    for raw in text.splitlines():
        line = ANSI_ESCAPE.sub("", raw).rstrip()
        section = VALIDATE_SECTION.match(line)
        if section:
            severity = section.group("severity").upper()
            continue
        if line.startswith("FAILED"):
            severity = None
            continue
        if not line.strip():
            continue
        if VALIDATE_ERROR_TAG.match(line):
            if severity is None or severity.startswith("ERROR"):
                lines.append(line)
        elif severity is not None and severity.startswith("ERROR"):
            lines.append(line)
    return lines


def validate_error_excerpt(text, max_bytes=VALIDATE_EVIDENCE_MAX_BYTES):
    """All validate error-severity lines, scrubbed, capped by bytes (not by line count)."""
    lines = [clean_line(line) for line in validate_error_lines(text)]
    kept = []
    used = 0
    for index, line in enumerate(lines):
        size = len(line.encode("utf-8")) + 1
        if used + size > max_bytes:
            kept.append(f"[{len(lines) - index} further error line(s) omitted: {max_bytes} byte evidence cap]")
            break
        kept.append(line)
        used += size
    return kept


def parse_validate_errors(text):
    """(failures, unattributed) of a validate report's error lines.

    A failure is a `[kind] <Component>: <Component> / <Action> (<detail>): <message>` line; its step
    is None. An error line of any other shape (a global or ambiguous error) is returned as-is
    in `unattributed`.
    """
    failures = []
    unattributed = []
    for line in validate_error_lines(text):
        match = VALIDATE_COMPONENT_ERROR.match(line)
        if match is None:
            unattributed.append(clean_line(line))
            continue
        failures.append({
            "component": match.group("component").strip(), "step": None,
            "action": match.group("action"), "kind": match.group("kind"),
            "message": clean_line(match.group("message")),
            "error_line": clean_line(line.strip())})
    return failures, unattributed


def distinct_failures(failures):
    """The first failure of each distinct component name (case-insensitive), in order."""
    found = {}
    for item in failures:
        found.setdefault(item["component"].casefold(), item)
    return list(found.values())


def failure_location(failure):
    """Where a recorded failure happened, for the CI log."""
    if failure.get("component"):
        step = f", step {failure['step']}" if failure.get("step") is not None else ""
        action = f" ({failure['action']})" if failure.get("action") else ""
        return f"component '{failure['component']}'{step}{action}"
    names = failure.get("components")
    if names:
        shown = ", ".join(f"'{clean_line(name)}'" for name in names[:5])
        more = f" and {len(names) - 5} more" if len(names) > 5 else ""
        return f"{len(names)} component(s) attributed ({shown}{more})"
    return "no component could be attributed"


def record_failure(text, stage, code, report, print_excerpt=False):
    """Write the failing component/step into the lane report and, optionally, the excerpt into the CI log."""
    failure = {"stage": stage, "exit_code": code, "component": None, "step": None,
               "action": None, "error_line": None}
    parsed = parse_component_failure(text)
    tagged, unattributed = parse_validate_errors(text) if stage.startswith("validate") else ([], [])
    if parsed:
        failure.update(parsed)
    if tagged:
        names = [item["component"] for item in distinct_failures(tagged)]
        failure["components"] = names
        failure["error_line_count"] = len(tagged) + len(unattributed)
        if not parsed and len(names) == 1:
            first = tagged[0]
            failure.update({"component": first["component"], "action": first["action"],
                            "error_line": first["error_line"]})
    report["failure"] = failure
    if print_excerpt:
        excerpt = failure_excerpt(text)
        if stage.startswith("validate"):
            excerpt += validate_error_excerpt(text)
        print(f"::group::{stage} failure evidence (bounded excerpt, {len(excerpt)} lines)",
              file=sys.stderr)
        print("\n".join(excerpt) if excerpt
              else "(no Error/Exception line or install-stopped marker in the output)",
              file=sys.stderr)
        print("::endgroup::", file=sys.stderr)
        if parsed:
            print(f"ports: {stage} failed at component '{parsed['component']}', step {parsed['step']} "
                  f"({parsed['action']})", file=sys.stderr)
        elif failure.get("component") or failure.get("components"):
            print(f"ports: {stage} failed at {failure_location(failure)}", file=sys.stderr)
    return failure


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
    report.setdefault("draft_review", {}).update(
        cleared=len(result["cleared"]),
        skipped=[{"component_guid": str(item["guid"]).lower(),
                  "name": item.get("name"), "reason": item.get("reason")}
                 for item in result["skipped"]])


BIND_STAGE = "bind_archives"
BIND_GENERATE_STAGE = "bind_generate_local"
BIND_GENERATOR = "resolved-filenames"
BIND_SUFFIXES = (".zip", ".7z", ".rar")
BIND_UNSAFE_NAME = re.compile(r"[/\\*?\[\]\x00-\x1f\x7f]")
INTERRUPTED = "command_interrupted_or_timed_out"


def canonical_link(url):
    """Guide link as cache.py keys it (cache.link_key): query, fragment and userinfo dropped, host lowercased;
    a mega link identified only by its fragment keeps a digest of it."""
    return link_key(url)


def mapped_archives(path):
    """archive_map.json as {canonical link: file name}; a link with several or conflicting files maps to None."""
    mapping = {}
    for link, value in json.loads(path.read_text(encoding="utf-8")).items():
        if isinstance(value, list):
            value = value[0] if len(value) == 1 else None
        name = value if isinstance(value, str) else None
        key = canonical_link(link)
        mapping[key] = name if mapping.get(key, name) == name else None
    return mapping


def components_of(path):
    document = tomllib.loads(path.read_text(encoding="utf-8"))
    return [component for component in document.get("thisMod", document.get("Components", []))
            if isinstance(component, dict)]


def component_archive(component, mapping):
    """The one archive file all of a component's links map to; None if unmapped, partly mapped or several."""
    registry = component.get("ResourceRegistry")
    names = set()
    for link in registry if isinstance(registry, dict) else ():
        names.add(mapping.get(canonical_link(link)))
    return names.pop() if len(names) == 1 and None not in names else None


def alias_key(text):
    """A heading or name as alias rules compare it: whitespace collapsed, case-insensitive; None if blank."""
    folded = " ".join(text.split()).casefold() if isinstance(text, str) else ""
    return folded or None


def component_alias_keys(component):
    """The normalized guide heading and component Name of a converted component, each once.

    The Rust converter writes `Heading` only when the guide `###` heading differs from the Name
    field, so a component can be known by either one; a reviewed rule may be keyed by both.
    """
    keys = []
    for field in ("Heading", "Name"):
        key = alias_key(component.get(field))
        if key is not None and key not in keys:
            keys.append(key)
    return keys


def declared_archive(component, counts, rules, archives):
    """The one file of the reviewed `archive_aliases` rule for this component, else None.

    The Rust converter derives GUIDs differently from the TypeScript one, so the rule (keyed by
    the TypeScript GUID) is found by the component's guide heading or its Name field, normalized,
    and exactly one rule must match. Each of the component's keys that a rule names must identify
    exactly one component (`counts`). Only a single-file rule for a component without
    instructions applies, so the binding keeps its sha256/size check; the file must be a restored
    archive.
    """
    matched = {}
    for key in component_alias_keys(component):
        for rule in rules.get(key, ()):
            matched[rule["guid"]] = rule
            if counts.get(key) != 1:
                return None
    if len(matched) != 1:
        return None
    rule = next(iter(matched.values()))
    if (component.get("Instructions") or len(rule["files"]) != 1
            or alias_file_problem(archives, rule["files"][0])):
        return None
    return rule["files"][0]


def resolved_archive_bindings(converted, mapping, archives, rules=None):
    """--resolved-archives entries for components whose links map to exactly one archive present in `archives`.

    A component the map cannot resolve falls back to its reviewed `archive_aliases` rule
    (`load_archive_aliases`), matched by heading or Name (`declared_archive`).
    """
    components = components_of(converted)
    seen, counts = {}, {}
    for component in components:
        seen[component.get("Guid")] = seen.get(component.get("Guid"), 0) + 1
        for key in component_alias_keys(component):
            counts[key] = counts.get(key, 0) + 1
    by_key = {}
    for rule in (rules or {}).values():
        by_key.setdefault(alias_key(rule["heading"]), []).append(rule)
    bindings = []
    for component in components:
        guid, name = component.get("Guid"), component_archive(component, mapping)
        if name is None and by_key:
            name = declared_archive(component, counts, by_key, archives)
        if (not isinstance(guid, str) or not guid or seen[guid] != 1 or name is None
                or not 0 < len(name) <= 255 or name in (".", "..") or BIND_UNSAFE_NAME.search(name)
                or not name.lower().endswith(BIND_SUFFIXES)):
            continue
        path = archives / name
        if path.is_symlink() or not path.is_file():
            continue
        with path.open("rb") as handle:
            sha256 = hashlib.file_digest(handle, "sha256").hexdigest()
        bindings.append({"componentGuid": guid, "filenames": [name], "sha256": sha256,
                         "sizeBytes": path.stat().st_size})
    return bindings


def bound_result(before, after):
    """(components bound by the resolved-filename pass, problem): problem names anything `after` lost from `before`."""
    def flatten(components, found):
        for component in components:
            record = component.get("DraftReview") or {}
            found[str(component.get("Guid", "")).lower()] = (
                len(component.get("Instructions") or []), "DraftReview" in component,
                bool(component.get("InstallationWarning")), record.get("Generator") == BIND_GENERATOR)
            flatten([option for option in component.get("Options") or [] if isinstance(option, dict)], found)
        return found

    old, new = components_of(before), components_of(after)
    if [item.get("Guid") for item in old] != [item.get("Guid") for item in new]:
        return 0, "bound_output_components_changed"
    kept = flatten(new, {})
    for guid, (count, drafted, warned, _) in flatten(old, {}).items():
        if guid not in kept:
            if count or drafted or warned:
                return 0, "bound_output_drops_instructions_or_drafts"
            continue
        new_count, new_drafted, new_warned, new_bound = kept[guid]
        if ((count and not new_count) or (drafted and not new_drafted) or (warned and not new_warned)
                or (new_count < count and not new_bound)):
            return 0, "bound_output_drops_instructions_or_drafts"
    return sum(1 for item in new
               if (item.get("DraftReview") or {}).get("Generator") == BIND_GENERATOR), None


BIND_DIAGNOSTICS_KEY = "bind_diagnostics"
BIND_DIAGNOSTIC_KINDS = (
    ("loose-file method remains unresolved", "loose_file_unresolved"),
    ("patcher method remains unresolved", "patcher_unresolved"),
)
BIND_DIAGNOSTIC_PRINT_CAP = 50


def component_names(converted):
    """{lowercase GUID: Name} of a converted TOML; empty when it cannot be read."""
    try:
        document = tomllib.loads(Path(converted).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    components = document.get("thisMod", document.get("Components", []))
    return {str(each["Guid"]).lower(): each.get("Name") or each.get("Heading")
            for each in components if isinstance(each, dict) and each.get("Guid")}


def bind_diagnostic_kind(reason):
    lowered = reason.lower()
    for prefix, kind in BIND_DIAGNOSTIC_KINDS:
        if lowered.startswith(prefix):
            return kind
    return "unresolved"


def unresolved_diagnostics(text):
    """(component GUID or None, reason) for each 'unresolved' diagnostic a bind stage log carries.

    The converter prints its per-component reports as pretty JSON arrays on stderr: the resolved
    archive pass as {componentGuid, diagnostics: [...]}, the local generation pass as
    {ComponentGuid, Success, SkipReason}. A log with no such array falls back to its raw
    'remains unresolved' lines.
    """
    found = []
    decoder = json.JSONDecoder()
    for start in (match.start() for match in re.finditer(r"^\[$", text, re.MULTILINE)):
        try:
            results, _ = decoder.raw_decode(text, start)
        except ValueError:
            continue
        for item in results if isinstance(results, list) else ():
            if not isinstance(item, dict):
                continue
            identity = item.get("componentGuid", item.get("ComponentGuid"))
            reasons = list(item["diagnostics"]) if isinstance(item.get("diagnostics"), list) else []
            if item.get("Success") is False:
                reasons.append(item.get("SkipReason"))
            found += [(str(identity).lower() if identity else None, reason) for reason in reasons
                      if isinstance(reason, str) and "unresolved" in reason.lower()]
    if not found:
        found = [(None, line.strip()) for line in ANSI_ESCAPE.sub("", text).splitlines()
                 if "remains unresolved" in line.lower()]
    return found


def record_bind_diagnostics(private, report, names=None):
    """Keep and print, flushed, the unresolved diagnostics of the archive binding stages.

    The converter exits 0 when it refuses to bind a component (for example a loose-file layout it
    cannot place), so the only trace is a diagnostic in the stage log. They go to
    report["bind_diagnostics"] as {stage, component, component_guid, kind, reason}.
    """
    names = names or {}
    entries, seen = [], set()
    for stage in (BIND_STAGE, BIND_GENERATE_STAGE):
        try:
            text = (private / (stage + ".log")).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for identity, reason in unresolved_diagnostics(text):
            if (stage, identity, reason) in seen:
                continue
            seen.add((stage, identity, reason))
            entries.append({"stage": stage, "component": names.get(identity), "component_guid": identity,
                            "kind": bind_diagnostic_kind(reason), "reason": reason})
    report[BIND_DIAGNOSTICS_KEY] = entries
    for entry in entries[:BIND_DIAGNOSTIC_PRINT_CAP]:
        print(f"::warning::ports: {entry['stage']}: {entry['component'] or 'unnamed component'} "
              f"[{entry['component_guid'] or 'no guid'}]: {entry['reason'][:600]}",
              file=sys.stderr, flush=True)
    if len(entries) > BIND_DIAGNOSTIC_PRINT_CAP:
        print(f"::warning::ports: {len(entries) - BIND_DIAGNOSTIC_PRINT_CAP} more bind diagnostics "
              f"are in the report under {BIND_DIAGNOSTICS_KEY}", file=sys.stderr, flush=True)


def bind_archives(command, source, guide, converted, archives, archive_map, private, report, env,
                  rules=None):
    """Bind method-only loose-file components to their one mapped archive (convert --resolved-archives).

    --resolved-archives only touches components without instructions and conflicts with
    --auto-generate-local, so it runs from the guide (--parse-directions) and the local
    generation pass then runs on its output. That branch is used only when both exit 0 and
    nothing the plain convert produced is lost; otherwise the plain converted.toml is kept
    and the failure is recorded. Returns the TOML the verified review should read.
    """
    review = report.setdefault("draft_review", {})
    review["bound"] = 0
    if archive_map is None:
        return converted
    try:
        bindings = resolved_archive_bindings(
            converted, mapped_archives(archive_map.resolve(strict=True)), archives, rules)
    except (OSError, ValueError, AttributeError):
        review["bind_failed"] = "archive_map_unreadable"
        return converted
    if not bindings:
        return converted
    plan = private / "resolved-archives.json"
    plan.write_text(json.dumps(bindings), encoding="utf-8")
    bound, staged = private / "converted-bound.toml", private / "converted-bound-pending.toml"
    try:
        run(command + ["convert", str(guide), str(staged), "--format", "toml", "--parse-directions",
                       "--source-path", str(archives), "--resolved-archives", str(plan)],
            source, private, BIND_STAGE, report, env)
        run(command + ["convert", str(staged), str(bound), "--format", "toml",
                       "--source-path", str(archives), "--auto-generate-local",
                       "--archive-map", str(archive_map.resolve(strict=True))],
            source, private, BIND_GENERATE_STAGE, report, env)
    except Blocked as failure:
        if str(failure) == INTERRUPTED:
            raise
        review["bind_failed"] = str(failure)
        return converted
    finally:
        record_bind_diagnostics(private, report, component_names(converted))
    if not bound.is_file() or bound.is_symlink():
        review["bind_failed"] = "bind_output_missing"
        return converted
    try:
        count, problem = bound_result(converted, bound)
    except (OSError, ValueError):
        count, problem = 0, "bound_output_unreadable"
    if problem:
        review["bind_failed"] = problem
        return converted
    review["bound"] = count
    return bound if count else converted


ALIAS_MOD_DIRECTORY = re.compile(r"<<modDirectory>>[\\/]+", re.IGNORECASE)
ALIAS_UNSAFE_NAME = re.compile(r"[/\\\x00-\x1f\x7f]")
ALIAS_STEM_STRIP = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]+')
ALIAS_STEM_LIMIT = 100
ALIAS_COLLISION_LIMIT = 50
ALIAS_FAILED_KEY = "archive_alias_failed"
ALIAS_RULE_KEYS = frozenset({"heading", "guid", "files", "reason"})


def safe_archive_name(name):
    return (isinstance(name, str) and name == name.strip() and name not in ("", ".", "..")
            and len(name) <= 255 and not ALIAS_UNSAFE_NAME.search(name))


def load_archive_aliases(game, path=None):
    """The reviewed archive_aliases rules of one game from archive_selections.json, keyed by GUID.

    The `archive_aliases` table maps k1/k2 to a list of {heading, guid, files, reason}: the
    component with that GUID takes exactly the listed restored files for its Extract glob,
    where the map evidence alone cannot choose (a link that maps to several files, or names
    that share no tokens with the glob). The whole table is validated whatever the game, so a
    malformed entry for the other game still blocks. A missing file or table declares nothing.
    """
    path = Path(path or LANE_SKIPS_PATH)
    if not path.is_file():
        return {}
    document = json.loads(path.read_text(encoding="utf-8"))
    table = document.get("archive_aliases", {}) if isinstance(document, dict) else None
    if not isinstance(table, dict) or not table.keys() <= set(LANE_SKIP_GAMES):
        raise Blocked("archive_aliases_invalid")
    result = {}
    for name, entries in table.items():
        if not isinstance(entries, list):
            raise Blocked("archive_aliases_invalid")
        seen_guids, seen_headings = set(), set()
        for entry in entries:
            if (not isinstance(entry, dict) or entry.keys() != ALIAS_RULE_KEYS
                    or any(not isinstance(entry[key], str) or not entry[key].strip()
                           or entry[key] != entry[key].strip() for key in ("heading", "guid", "reason"))
                    or not canonical_guid(entry["guid"])
                    or not isinstance(entry["files"], list) or not entry["files"]
                    or not all(safe_archive_name(each) for each in entry["files"])):
                raise Blocked("archive_aliases_invalid")
            if (len(set(entry["files"])) != len(entry["files"])
                    or entry["guid"] in seen_guids or entry["heading"] in seen_headings):
                raise Blocked("archive_aliases_duplicate")
            seen_guids.add(entry["guid"])
            seen_headings.add(entry["heading"])
            if name == game:
                result[entry["guid"]] = entry
    return result


def glob_regex(segment):
    """One path segment as the validator matches it: only `*` and `?` are wildcards, case-insensitive.

    ModSync.ts resolveWildcards runs picomatch with nobrace/nobracket/noextglob, so
    `{zip,rar,7z}` and `[x]` are literal characters, not alternatives.
    """
    parts = []
    for char in segment:
        if char == "*":
            if not parts or parts[-1] != "[^/]*":
                parts.append("[^/]*")
        else:
            parts.append("[^/]" if char == "?" else re.escape(char))
    return re.compile("".join(parts), re.IGNORECASE | re.DOTALL)


def archive_pattern_matches(directory, segments, visited=None):
    """True when some path below `directory` matches the glob `segments` (files and folders count)."""
    if not segments:
        return True
    try:
        entries = list(os.scandir(directory))
    except OSError:
        return False
    head, rest = segments[0], segments[1:]
    if head == "**":
        if not rest:
            return bool(entries)
        if archive_pattern_matches(directory, rest):
            return True
        visited = set() if visited is None else visited
        visited.add(os.path.realpath(directory))
        return any(entry.is_dir() and os.path.realpath(entry.path) not in visited
                   and archive_pattern_matches(entry.path, segments, visited)
                   for entry in entries)
    matcher = glob_regex(head)
    return any(matcher.fullmatch(entry.name) and (
        not rest or (entry.is_dir() and archive_pattern_matches(entry.path, rest))) for entry in entries)


def link_basename(link):
    try:
        return unquote(urlsplit(str(link)).path.rsplit("/", 1)[-1])
    except ValueError:
        return ""


def extract_patterns(component):
    """[(raw source, path relative to the mod directory)] of the Extract sources the lane will validate.

    Covers the component's own instructions and those of its options that are selected in the
    converted TOML (`--select` turns options on only through their own GUIDs).
    """
    records = list(component.get("Instructions") or [])
    for option in component.get("Options") or []:
        if isinstance(option, dict) and option.get("IsSelected") is True:
            records += option.get("Instructions") or []
    found = []
    for record in records:
        if not isinstance(record, dict) or str(record.get("Action", "")).lower() != "extract":
            continue
        sources = record.get("Source")
        for raw in [sources] if isinstance(sources, str) else sources if isinstance(sources, list) else ():
            prefix = ALIAS_MOD_DIRECTORY.match(raw) if isinstance(raw, str) else None
            if prefix and all(raw != seen for seen, _ in found):
                found.append((raw, raw[prefix.end():].replace("\\", "/")))
    return found


def component_links(component):
    links = component.get("ModLink")
    return [links] if isinstance(links, str) else [
        link for link in links if isinstance(link, str)] if isinstance(links, list) else []


def alias_stem(name):
    """The mod name as ModSync.ts sanitizeFileName cleans it (used for the guessed archive glob)."""
    cleaned = " ".join(ALIAS_STEM_STRIP.sub("", str(name or "")).split()).strip(" .")
    return cleaned[:ALIAS_STEM_LIMIT].rstrip(" .") or "mod"


def alias_candidates(segment, matcher, stem, source_name):
    """Names that satisfy `segment`, best first: the literal basename, else `<mod name>.<ext>`.

    When the validator reads the pattern literally (a `{zip,rar,7z}` group is not expanded),
    `<mod name>.<ext>` does not match; the pattern with every `*` removed does by construction.
    """
    if "*" not in segment and "?" not in segment:
        yield segment
        return
    extension = os.path.splitext(source_name)[1]
    if re.fullmatch(r"\.[A-Za-z0-9]{1,5}", extension) is None:
        extension = ""
    for base in (stem + extension, segment.replace("*", "").replace("?", "_")):
        if matcher.fullmatch(base):
            root, suffix = os.path.splitext(base)
            yield base
            for number in range(2, ALIAS_COLLISION_LIMIT + 1):
                numbered = f"{root} ({number}){suffix}"
                if not matcher.fullmatch(numbered):
                    break
                yield numbered


ALIAS_TOKEN = re.compile(r"[a-z0-9]+")
ALIAS_NOISE_TOKEN = re.compile(r"[0-9]+|v[0-9]+|zip|rar|7z|exe")
ALIAS_ARCHIVE_SUFFIX = re.compile(r"\.(?:zip|rar|7z|exe)$", re.IGNORECASE)
ARCHIVE_MAGIC = (b"PK\x03\x04", b"PK\x05\x06", b"Rar!\x1a\x07", b"7z\xbc\xaf\x27\x1c", b"MZ")


def mapped_archive_choices(path):
    """archive_map.json as {canonical link: [file names]}: every file a link maps to, in map order."""
    choices = {}
    for link, value in json.loads(path.read_text(encoding="utf-8")).items():
        names = choices.setdefault(canonical_link(link), [])
        for name in value if isinstance(value, list) else [value]:
            if isinstance(name, str) and name not in names:
                names.append(name)
    return choices


def link_slug(link):
    """Last non-empty path segment of a guide link ('.../2277-kiosk-hd/' -> '2277-kiosk-hd')."""
    try:
        segments = [part for part in unquote(urlsplit(str(link)).path).split("/") if part]
    except ValueError:
        return ""
    return segments[-1] if segments else ""


def alias_tokens(text):
    """Lower-case word tokens of a name; versions, numbers, archive extensions and plural 's' ignored."""
    tokens = set()
    for token in ALIAS_TOKEN.findall(str(text).casefold()):
        if not ALIAS_NOISE_TOKEN.fullmatch(token):
            tokens.add(token[:-1] if len(token) > 3 and token.endswith("s") else token)
    return tokens


def alias_reference(segment):
    """The mod-name text of an Extract glob: wildcards, a `{..}` group and the archive extension removed."""
    text = re.sub(r"\{[^{}]*\}", "", segment).replace("*", " ").replace("?", " ")
    return ALIAS_ARCHIVE_SUFFIX.sub("", text.strip(" .")).strip(" .")


def alias_precision(reference, evidence):
    """Share of the evidence tokens that are reference tokens, or None unless every reference token is present."""
    wanted = alias_tokens(reference)
    best = None
    for text in evidence:
        found = alias_tokens(text)
        if wanted and wanted <= found:
            best = max(best or 0.0, len(wanted) / len(found))
    return best


def alias_file_problem(archives, name):
    """None when `name` is a restored regular archive that can be linked; otherwise the reason it cannot."""
    source = archives / name
    if (ALIAS_UNSAFE_NAME.search(name) or name in (".", "..") or not 0 < len(name) <= 255
            or source.is_symlink() or not source.is_file()):
        return "mapped_archive_missing_or_unsafe"
    try:
        with open(source, "rb") as handle:
            header = handle.read(8)
    except OSError:
        return "mapped_archive_missing_or_unsafe"
    return None if header.startswith(ARCHIVE_MAGIC) else "mapped_file_not_archive"


def alias_identical_groups(archives, names):
    """Group `names` by file content, so mirrors of one download count once."""
    by_size = {}
    for name in names:
        by_size.setdefault((archives / name).stat().st_size, []).append(name)
    groups = []
    for same in by_size.values():
        if len(same) == 1:
            groups.append(same)
            continue
        by_content = {}
        for name in same:
            hasher = hashlib.sha256()
            with open(archives / name, "rb") as handle:
                for chunk in iter(lambda: handle.read(1 << 20), b""):
                    hasher.update(chunk)
            by_content.setdefault(hasher.digest(), []).append(name)
        groups.extend(by_content.values())
    return groups


def declared_source_names(pending, literal, declared, archives):
    """([declared file names], None) when every one is a restored archive, else (None, reason)."""
    if pending != 1 or (literal is not None and len(declared) != 1):
        return None, "declared_archives_not_applicable"
    for name in declared:
        problem = alias_file_problem(archives, name)
        if problem:
            return None, "declared_" + problem
    return list(declared), None


def alias_source_names(pending, links, choices, reference, literal, archives, declared=None):
    """([restored file names], None) for one unmatched pattern, or (None, reason).

    `pending` is how many distinct patterns of the component are unmatched, `choices` is
    `mapped_archive_choices`, `reference` the glob's mod-name text. Files that are missing,
    unsafe or not archives are dropped; mirrors (identical content) count once. Then:
    a literal basename is paired with the file or link URL that ends in that name; one file
    serves one pattern; otherwise the files whose name (or, for a link that maps to just
    that file, URL slug) contain every token of `reference` are the matches. A wildcard
    pattern really matches all of them, so each is returned; a literal one needs exactly
    one. Anything else stays unresolved with the reason, never guessed.

    `declared` is the reviewed `archive_aliases` file list of the component: it replaces the
    inference, and every listed file must be a restored archive or nothing is aliased.
    """
    if declared is not None:
        return declared_source_names(pending, literal, declared, archives)
    origins = {}
    multi = False
    for link in links:
        names = choices.get(canonical_link(link), ())
        multi = multi or len(names) > 1
        for name in names:
            origins.setdefault(name, []).append((link, len(names) == 1))
    if not origins:
        return None, "no_mapped_archive"
    problems = {name: alias_file_problem(archives, name) for name in origins}
    usable = [name for name in origins if not problems[name]]
    if not usable:
        return None, problems[next(iter(origins))]
    if literal is not None:
        paired = [name for name in usable
                  if name.casefold() == literal.casefold() or any(
                      single and link_basename(link).casefold() == literal.casefold()
                      for link, single in origins[name])]
        if len(paired) == 1:
            return paired, None
    precision = {name: alias_precision(
        reference, [name] + [link_slug(link) for link, single in origins[name] if single])
        for name in usable}
    ordered = sorted(usable, key=lambda name: (-(precision[name] or 0.0), name))
    groups = alias_identical_groups(archives, ordered)
    representatives = sorted((min(group, key=ordered.index) for group in groups), key=ordered.index)
    if len(representatives) == 1 and pending == 1:
        return representatives, None
    strong = [name for name in representatives if precision[name] is not None]
    if strong and (literal is None or len(strong) == 1):
        return strong, None
    return None, "multi_file_map_entry" if multi else "ambiguous_mapped_archives"


def link_alias(source, alias):
    """Hardlink `alias` to `source` (same directory), falling back to a relative symlink."""
    try:
        os.link(source, alias)
        return "hardlink"
    except FileExistsError:
        raise
    except OSError:
        os.symlink(source.name, alias)
        return "symlink"


def alias_archives(converted, archive_map, archives, selected, report, rules=None):
    """Give restored archives names the converted TOML's Extract globs match (TypeScript lanes).

    ModSync.ts guesses archive globs from mod names or URL basenames while cache.py restore
    stores archives under unrelated names, so `validate` finds nothing. For each selected
    component with an Extract source under `<<modDirectory>>` that matches nothing in
    `archives`, the one restored archive its ModLinks map to (`archive_map`) gets a hardlink
    (symlink if linking fails) in `archives` named so the glob matches. converted.toml is
    never changed. Links that cannot be made are recorded in `alias_unresolved`; validate
    then reports the missing download as before.

    The validator reads `{zip,rar,7z}` literally, so a braced glob is only matched by a name
    that ends in that literal group (`Name.{zip,rar,7z}`); it still opens the file by content,
    which is why the source must be a real archive (`alias_file_problem`).

    `rules` is `load_archive_aliases`: a component with a rule takes exactly its declared files,
    each under its own alias name, independent of what the map says.
    """
    aliases, unresolved = [], []
    rules = rules or {}
    report["archive_aliases"] = {"count": 0, "aliases": aliases}
    report["alias_unresolved"] = unresolved
    choices, map_problem = {}, None
    if archive_map is None:
        map_problem = "no_archive_map"
    else:
        try:
            choices = mapped_archive_choices(archive_map.resolve(strict=True))
        except (OSError, ValueError, AttributeError):
            map_problem = "archive_map_unreadable"
            report[ALIAS_FAILED_KEY] = map_problem
    needs = []
    for component in components_of(converted):
        try:
            identity = guid(component.get("Guid"))
        except (Blocked, ValueError, AttributeError):
            continue
        if identity not in selected:
            continue
        missing = [(raw, relative) for raw, relative in extract_patterns(component)
                   if not archive_pattern_matches(
                       archives, [part for part in relative.split("/") if part not in ("", ".")])]
        if missing:
            needs.append((component, identity, missing))
    taken = {name.casefold() for name in os.listdir(archives)}
    created = {}
    for component, identity, missing in needs:
        links = component_links(component)
        name = component.get("Name", component.get("Heading", ""))
        rule = rules.get(identity)
        declared = None
        if rule is not None:
            declared = rule["files"] if rule["heading"] == name else []
        failures = []
        for raw, relative in missing:
            segments = [part for part in relative.split("/") if part not in ("", ".")]
            if not segments or ".." in segments:
                failures.append({"pattern": raw, "reason": "path_escapes_archives_dir"})
                continue
            if len(segments) > 1:
                failures.append({"pattern": raw, "reason": "nested_pattern_not_aliased"})
                continue
            segment = segments[0]
            wildcard = "*" in segment or "?" in segment
            if declared == []:
                failures.append({"pattern": raw, "reason": "declared_heading_mismatch"})
                continue
            source_names, reason = alias_source_names(
                len(missing), links, choices, alias_reference(segment),
                None if wildcard else segment, archives, declared)
            if source_names is None:
                failures.append({"pattern": raw, "reason": reason if rule is not None else map_problem or reason})
                continue
            matcher = glob_regex(segment)
            for source_name in source_names:
                source = archives / source_name
                placed = None
                for candidate in alias_candidates(segment, matcher, alias_stem(name), source_name):
                    if ALIAS_UNSAFE_NAME.search(candidate) or not 0 < len(candidate) <= 255 \
                            or candidate in (".", ".."):
                        break
                    key = candidate.casefold()
                    if key in taken:
                        if not wildcard and created.get(key) == source_name:
                            placed = ""
                            break
                        continue
                    try:
                        method = link_alias(source, archives / candidate)
                    except FileExistsError:
                        taken.add(key)
                        continue
                    except OSError:
                        break
                    taken.add(key)
                    created[key] = source_name
                    record = {"component": name, "component_guid": identity,
                              "alias": candidate, "source": source_name, "method": method}
                    if rule is not None:
                        record["rule"] = "archive_aliases"
                    aliases.append(record)
                    placed = candidate
                    break
                if placed is None:
                    failures.append({"pattern": raw, "reason": "alias_not_created"})
        if failures:
            unresolved.append({"component": name, "component_guid": identity,
                               "reason": failures[0]["reason"], "patterns": failures})
    report["archive_aliases"]["count"] = len(aliases)


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


QUARANTINE_KEY = "quarantined_validate_failures"
QUARANTINE_MAX_ITERATIONS = 15
QUARANTINE_MAX_FRACTION = 0.15
VALIDATE_FAILURE_KIND = "validate_failure"
QUARANTINE_DEPENDENT_KIND = "dependent_of_quarantined"
QUARANTINE_CAP_REASON = "validate_quarantine_cap_exceeded"
INSTALL_QUARANTINE_KEY = "quarantined_install_failures"
INSTALL_FAILURE_KIND = "install_failure"
INSTALL_QUARANTINE_CAP_REASON = "install_quarantine_cap_exceeded"
QUARANTINE_FAILURE_KINDS = (VALIDATE_FAILURE_KIND, INSTALL_FAILURE_KIND)
QUARANTINE_PHASES = {
    "validate": {"key": QUARANTINE_KEY, "kind": VALIDATE_FAILURE_KIND,
                 "cap": QUARANTINE_CAP_REASON, "blocked": "validate_failed"},
    "install": {"key": INSTALL_QUARANTINE_KEY, "kind": INSTALL_FAILURE_KIND,
                "cap": INSTALL_QUARANTINE_CAP_REASON, "blocked": "install_failed"},
}
INSTALL_QUARANTINE_ACTIONS = frozenset(
    action.casefold() for action in ("Patcher", "Extract", "Move", "Copy", "Rename", "Delete"))
BASELINE_COPY_TIMEOUT = 7200
GAME_MISSING = re.compile(
    r"\b(?:game|kotor)\s+(?:directory|folder|install(?:ation)?)\b[^:]{0,40}"
    r"\b(?:not found|does not exist|is missing|missing)\b|chitin\.key",
    re.IGNORECASE)


def deselected_guids(flags):
    """The GUIDs named by the --deselect flags of a command line, normalized."""
    return {normalized_reference(value) for flag, value in zip(flags, flags[1:]) if flag == "--deselect"}


def converted_components(converted):
    document = tomllib.loads(converted.read_text(encoding="utf-8"))
    components = document.get("thisMod", document.get("Components", []))
    return [component for component in components if isinstance(component, dict)]


def find_failing_component(components, name):
    """The one converted component a validate error names, or (None, why it cannot be attributed)."""
    matches = {}
    for component in components:
        if name in (component.get("Name"), component.get("Heading")):
            matches[normalized_reference(component.get("Guid"))] = component
    if not matches:
        folded = name.casefold()
        for component in components:
            if folded in (str(component.get("Name", "")).casefold(),
                          str(component.get("Heading", "")).casefold()):
                matches[normalized_reference(component.get("Guid"))] = component
    if not matches:
        return None, "component_unknown"
    if len(matches) > 1:
        return None, "component_ambiguous"
    return next(iter(matches.items())), None


def instruction_index(components):
    """{normalized instruction GUID: [(component GUID, instruction record)]} over the converted components.

    Covers each component's own instructions and those of its options.
    """
    index = {}
    for component in components:
        identity = normalized_reference(component.get("Guid"))
        records = list(component.get("Instructions") or [])
        for option in component.get("Options") or []:
            if isinstance(option, dict):
                records += option.get("Instructions") or []
        for record in records:
            if isinstance(record, dict) and record.get("Guid"):
                index.setdefault(normalized_reference(record["Guid"]), []).append((identity, record))
    return index


def resolve_instruction_failures(failures, components):
    """(failures pinned to converted components by instruction GUID, or None; why not).

    A failure carrying an `instruction_guid` takes its component from the converted instruction set
    (`component_guid`, converted `component` name, and the instruction's Action when the log had no
    step line). A GUID that several components own, or that belongs to a different component than the
    log's `[i/N]` progress line names, refuses the pass. A GUID the converted set does not know keeps
    the progress-line attribution. A failure with neither is dropped as unattributable.
    """
    index = instruction_index(components)
    by_guid = {normalized_reference(component.get("Guid")): component for component in components}
    resolved = []
    for item in failures:
        guid_ = item.get("instruction_guid")
        if guid_ is None:
            resolved.append(item)
            continue
        owners = index.get(guid_, [])
        if len({identity for identity, _ in owners}) > 1:
            return None, "instruction_ambiguous"
        item = dict(item)
        if owners:
            identity, record = owners[0]
            named = item.get("component")
            if named:
                match, _ = find_failing_component(components, named)
                if match is not None and match[0] != identity:
                    return None, "instruction_component_mismatch"
            component = by_guid[identity]
            item["component_guid"] = identity
            item["component"] = str(component.get("Name", component.get("Heading")))
            item["action"] = item.get("action") or str(record.get("Action") or "")
        if item.get("component"):
            resolved.append(item)
    return resolved, None


def transitive_dependents(components, root, excluded):
    """Every not-yet-excluded component that depends, directly or through others, on `root`."""
    found = {}
    frontier = {root}
    while frontier:
        reached = set()
        for component in components:
            identity = normalized_reference(component.get("Guid"))
            if identity == root or identity in found or identity in excluded:
                continue
            if frontier & {normalized_reference(each) for each in component.get("Dependencies", [])}:
                found[identity] = component
                reached.add(identity)
        frontier = reached
    return found


def quarantined_entries(report):
    """Every quarantine record of the lane, validate failures first, then install failures."""
    return list(report.get(QUARANTINE_KEY) or ()) + list(report.get(INSTALL_QUARANTINE_KEY) or ())


def start_quarantine(components, deselect, report, max_iterations, max_fraction):
    """Open the quarantine budget that validate and install share; returns the policy record."""
    taken = deselected_guids(deselect)
    selected = sum(normalized_reference(component.get("Guid")) not in taken for component in components)
    max_components = min(selected, math.floor(selected * max_fraction + 1e-9)) if max_fraction > 0 else 0
    report["quarantine"] = {
        "max_iterations": max_iterations, "max_fraction": max_fraction,
        "selected_components": selected, "max_components": max_components,
        "attempts": 0, "install_attempts": 0, "iterations_used": 0, "stopped": None}
    return report["quarantine"]


def quarantine_stop(report, phase, stage, reason, parsed, failure, cap=False, details=None):
    """Record why a failure was not quarantined and fail the lane."""
    settings = QUARANTINE_PHASES[phase]
    report["quarantine"]["stopped"] = {
        "phase": phase, "stage": stage, "reason": reason, "component": parsed and parsed["component"],
        "step": parsed and parsed["step"], "error_line": parsed and parsed["error_line"]}
    report["quarantine"]["stopped"].update(details or {})
    raise Blocked(settings["cap"] if cap else settings["blocked"]) from failure


def quarantine_failure(phase, parsed, stage, components, deselect, game, report, failure,
                       before_commit=None, new_pass=True):
    """Quarantine the one component `parsed` names, plus its dependents, or fail the lane with a reason.

    Both stages use it, against the one budget in report["quarantine"]: the iteration cap counts
    passes (validate runs and install attempts together) and the component cap counts every
    quarantined component (failing components plus dependents). A pass that attributes several
    components calls this once per component and passes `new_pass=False` after the first, so it
    consumes one iteration and one component each. `before_commit` runs once every check has
    passed and before anything is recorded or deselected; the install stage restores the game there.
    """
    settings = QUARANTINE_PHASES[phase]
    policy = report["quarantine"]

    def stop(reason, cap=False):
        quarantine_stop(report, phase, stage, reason, parsed, failure, cap)

    entries = quarantined_entries(report)
    if policy["max_iterations"] <= 0:
        stop("quarantine_disabled")
    if new_pass and policy.get("iterations_used", 0) >= policy["max_iterations"]:
        stop("iteration_cap_exceeded", True)
    if not game.is_dir() or not any(game.iterdir()) or GAME_MISSING.search(parsed["message"]):
        stop("game_missing")
    hinted = parsed.get("component_guid")
    by_guid = {normalized_reference(each.get("Guid")): each for each in components}
    if hinted in by_guid:
        match, problem = (hinted, by_guid[hinted]), None
    else:
        match, problem = find_failing_component(components, parsed["component"])
    if problem:
        stop(problem)
    identity, component = match
    taken = deselected_guids(deselect)
    if identity in taken:
        stop("component_already_deselected")
    dependents = transitive_dependents(components, identity, taken)
    if len(entries) + 1 + len(dependents) > policy["max_components"]:
        stop("component_cap_exceeded", True)
    if before_commit is not None:
        before_commit()
    if new_pass:
        policy["iterations_used"] = policy.get("iterations_used", 0) + 1
    quarantined = report.setdefault(settings["key"], [])
    quarantined.append({
        "kind": settings["kind"], "name": component.get("Name", component.get("Heading")),
        "component_guid": identity, "step": parsed["step"], "action": parsed["action"],
        "error_line": parsed["error_line"], "stage": stage, "caused_by": None})
    if parsed.get("instruction_guid"):
        quarantined[-1]["instruction_guid"] = parsed["instruction_guid"]
    for dependent_guid, dependent in dependents.items():
        quarantined.append({
            "kind": QUARANTINE_DEPENDENT_KIND,
            "name": dependent.get("Name", dependent.get("Heading")),
            "component_guid": dependent_guid, "step": None, "action": None,
            "error_line": None, "stage": stage, "caused_by": identity})
    for each in (identity, *dependents):
        deselect += ["--deselect", each]


def read_stage_log(private, stage, report):
    """The text of a failed stage's log and its exit code."""
    try:
        text = (private / (stage + ".log")).read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    code = report["commands"][-1].get("exit_code") if report.get("commands") else None
    return text, code


def attribute_validate_failures(text, components, deselect, report, stage, failure):
    """The distinct failing components a validate log names, or a recorded quarantine stop.

    A log with no tagged lines keeps the one-component rule (the last `Error: <Component> /
    step N (<Action>)` line). A log with tagged lines (`[patcher-error]`, `[missing-source]`,
    `[missing-archive]`) names every failing component in one pass. Nothing is attributed, and
    nothing quarantined, when any error line has another shape (a global or ambiguous error), when a
    missing download is not already covered by the reviewed policy, or when a tagged line's action
    is not in INSTALL_QUARANTINE_ACTIONS.
    """
    tagged, unattributed = parse_validate_errors(text)
    if not tagged:
        parsed = parse_component_failure(text)
        if parsed is None:
            quarantine_stop(report, "validate", stage, "error_not_attributable", None, failure)
        return [parsed]
    if unattributed:
        quarantine_stop(report, "validate", stage, "unattributed_error_lines", None, failure)
    taken = deselected_guids(deselect)
    candidates = []
    for item in tagged:
        if item["kind"] in VALIDATE_DOWNLOAD_KINDS:
            match, _ = find_failing_component(components, item["component"])
            if match is not None and match[0] in taken:
                continue
            quarantine_stop(report, "validate", stage, "missing_download_not_quarantinable", item, failure)
        if (item["action"] or "").casefold() not in INSTALL_QUARANTINE_ACTIONS:
            quarantine_stop(report, "validate", stage, "action_not_quarantinable", item, failure)
        candidates.append(item)
    candidates += parse_component_failures(text)
    if not candidates:
        quarantine_stop(report, "validate", stage, "error_not_attributable", None, failure)
    return distinct_failures(candidates)


def validate_with_quarantine(command, directories, deselect, converted, game, source, private, report,
                             env, max_iterations=QUARANTINE_MAX_ITERATIONS,
                             max_fraction=QUARANTINE_MAX_FRACTION, arguments=None, selectable=None):
    """Run `validate`; quarantine each component it names as failing and re-run, visibly and bounded.

    `deselect` is the --deselect flag list shared with install and the GUI driver; quarantined
    components (and their dependents) are appended to it in place. A failure is only
    quarantined when its `Error: <Component> / step N (<Action>): ...` line names exactly one
    converted component that is still selected, the game directory is populated, and neither
    the iteration cap nor max_fraction of the selected components would be exceeded. Anything
    else leaves the lane failed. Every quarantined component is listed in
    report["quarantined_validate_failures"].

    When the log is a TypeScript validate report, every distinct component its tagged error lines
    name is quarantined in the same pass, one `quarantine_failure` per component against the same
    budget, all or none (a refused pass rolls back what it had recorded and deselected). A pass
    consumes one iteration (the iteration cap counts validate passes) and one unit of the
    component budget per quarantined component or dependent. A pass over the component cap stops
    with `component_cap_exceeded` and records every attributed component, their count and the cap
    in report["quarantine"]["stopped"]. The pass is refused as a whole for global or ambiguous
    errors, for missing downloads, and for actions outside INSTALL_QUARANTINE_ACTIONS (see
    `attribute_validate_failures`).

    `arguments(deselect)` replaces `directories + deselect` as the validate arguments (TypeScript
    selects with --select, not --deselect) and `selectable` limits the components the budget and
    the name lookup see to those GUIDs.
    """
    components = converted_components(converted)
    if selectable is not None:
        components = [component for component in components
                      if normalized_reference(component.get("Guid")) in selectable]
    report.setdefault(QUARANTINE_KEY, [])
    start_quarantine(components, deselect, report, max_iterations, max_fraction)
    policy = report["quarantine"]
    iterations = 0
    while True:
        stage = "validate" if iterations == 0 else f"validate_retry_{iterations}"
        policy["attempts"] = iterations + 1
        try:
            run(command + ["validate"] + (arguments(deselect) if arguments else directories + deselect),
                source, private, stage, report, env)
        except Blocked as failure:
            if str(failure) != stage + "_failed":
                raise
            text, code = read_stage_log(private, stage, report)
            record_failure(text, stage, code, report)
            failing = attribute_validate_failures(text, components, deselect, report, stage, failure)
            recorded = list(report[QUARANTINE_KEY])
            flags = list(deselect)
            already = deselected_guids(deselect)
            used = policy["iterations_used"]
            counted = False
            try:
                for parsed in failing:
                    match, _ = find_failing_component(components, parsed["component"])
                    if match is not None and match[0] in deselected_guids(deselect) - already:
                        continue
                    quarantine_failure("validate", parsed, stage, components, deselect, game, report, failure,
                                       new_pass=not counted)
                    counted = True
            except Blocked:
                report[QUARANTINE_KEY][:] = recorded
                deselect[:] = flags
                policy["iterations_used"] = used
                stopped = policy.get("stopped")
                if stopped and stopped["reason"] == "component_cap_exceeded":
                    stopped.update({
                        "attributed_count": len(failing), "component_cap": policy["max_components"],
                        "already_quarantined": len(quarantined_entries(report)),
                        "attributed_components": [item["component"] for item in failing]})
                raise
            iterations += 1
            continue
        report.pop("failure", None)
        return


def game_is_populated(game):
    """The game directory exists, is not empty and still has its chitin.key."""
    try:
        return game.is_dir() and any(child.name.casefold() == "chitin.key" for child in game.iterdir())
    except OSError:
        return False


def tree_manifest(root):
    """Every entry under `root`: relative path -> (kind, size or link target)."""
    manifest = {}
    pending = [root]
    while pending:
        with os.scandir(pending.pop()) as entries:
            for entry in entries:
                relative = Path(entry.path).relative_to(root).as_posix()
                if entry.is_symlink():
                    manifest[relative] = ("link", os.readlink(entry.path))
                elif entry.is_dir(follow_symlinks=False):
                    manifest[relative] = ("dir", 0)
                    pending.append(entry.path)
                else:
                    manifest[relative] = ("file", entry.stat(follow_symlinks=False).st_size)
    return manifest


def copy_tree(source, destination):
    """Copy the contents of `source` into `destination` as independent files (never hardlinks)."""
    destination.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(["cp", "-a", "--reflink=auto", "--", str(source) + "/.", str(destination) + "/"],
                       check=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=BASELINE_COPY_TIMEOUT)
    except FileNotFoundError:
        shutil.copytree(source, destination, symlinks=True, dirs_exist_ok=True)


def take_baseline(game, private, report):
    """Copy the game directory to the lane's private directory before the first install.

    Returns (baseline directory, manifest) or None when the copy failed or does not match the
    game; the outcome is recorded under report["quarantine"]["baseline"] either way.
    """
    baseline = private / "game-baseline"
    try:
        copy_tree(game, baseline)
        manifest = tree_manifest(baseline)
        if manifest != tree_manifest(game):
            raise OSError("baseline copy differs from the game directory")
    except (OSError, subprocess.SubprocessError, shutil.Error):
        shutil.rmtree(baseline, ignore_errors=True)
        report["quarantine"]["baseline"] = {"status": "copy_failed"}
        return None
    report["quarantine"]["baseline"] = {
        "status": "copied", "files": sum(kind == "file" for kind, _ in manifest.values()),
        "bytes": sum(size for kind, size in manifest.values() if kind == "file")}
    return baseline, manifest


def restore_game(game, baseline, manifest):
    """Put the game directory back to the baseline in place and verify the file list and sizes."""
    for child in game.iterdir():
        if child.is_symlink() or not child.is_dir():
            child.unlink()
        else:
            shutil.rmtree(child)
    copy_tree(baseline, game)
    if tree_manifest(game) != manifest:
        raise OSError("restored game directory differs from the baseline")


ENGINE_STATE_NAME = re.compile(r"\.modsync-[0-9a-f]{16}")
UNFINISHED_INSTALL = "an unfinished install exists"
TS_UNFINISHED_INSTALL = "unfinished checkpoint"
UNFINISHED_INSTALL_MARKERS = (UNFINISHED_INSTALL, TS_UNFINISHED_INSTALL)
TYPESCRIPT_INSTALL_PHASES = ("base", "widescreen")
TYPESCRIPT_FIRST_CHECKPOINT = "0"
NOTHING_TO_RESTORE = "no unfinished install to restore"
ENGINE_RESTORE_KEY = "engine_restores"
ENGINE_RESTORE_TIMEOUT = BASELINE_COPY_TIMEOUT
UNFINISHED_INSTALL_REASON = "unfinished_install_not_recovered"


def engine_state_dir(game):
    """The engine's recovery directory for `game`: a `.modsync-<16 hex of sha256(game path)>` sibling."""
    key = hashlib.sha256(str(game).encode("utf-8", "replace")).hexdigest()
    return game.parent / (".modsync-" + key[:16])


def remove_engine_state(game, protected=()):
    """Delete only this game's `.modsync-<hash>` sibling (snapshot + pending marker); returns what happened.

    Refuses (OSError) anything but a real directory, directly beside the game directory, named
    exactly as the engine names it, that is not one of `protected` or inside/around one.
    """
    state = engine_state_dir(game)
    if state.parent != game.parent or not ENGINE_STATE_NAME.fullmatch(state.name):
        raise OSError("recovery directory is not a sibling of the game directory")
    for path in (game, *protected):
        if state == path or state.is_relative_to(path) or path.is_relative_to(state):
            raise OSError("recovery directory overlaps a protected directory")
    try:
        info = state.lstat()
    except FileNotFoundError:
        return "absent"
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise OSError("recovery path is not a plain directory")
    shutil.rmtree(state)
    return "removed"


def restore_arguments(directories):
    """`--game-dir X --source-dir Y` taken from an install's directory arguments, or None."""
    found = []
    for flag in ("--game-dir", "--source-dir"):
        if flag not in directories or directories.index(flag) + 1 >= len(directories):
            return None
        found += [flag, directories[directories.index(flag) + 1]]
    return found


def engine_restore(command, directories, source, private, stage, report, env, invocation=None):
    """Run the CLI's own `restore` for the game; returns the record appended to engine_restores.

    `status` is restored, nothing_pending (the engine had no unfinished install), failed or
    unavailable (no binary or no directory arguments). `invocation` replaces the Rust
    `restore --game-dir X --source-dir Y` arguments (the TypeScript CLI's `checkpoints restore ...`).
    """
    record = {"stage": stage, "status": "unavailable", "seconds": 0.0}
    report["quarantine"].setdefault(ENGINE_RESTORE_KEY, []).append(record)
    arguments = restore_arguments(directories) if invocation is None else invocation
    if arguments is None:
        return record
    log = private / (stage + ".log")
    started = time.monotonic()
    progress(f"{stage}: started")
    status = "failed"
    try:
        with log.open("wb") as output:
            completed = subprocess.run(
                command + (["restore"] + arguments if invocation is None else arguments), cwd=source, env=env, stdin=subprocess.DEVNULL,
                stdout=output, stderr=subprocess.STDOUT, timeout=ENGINE_RESTORE_TIMEOUT)
        text = log.read_text(encoding="utf-8", errors="replace")
        if completed.returncode == 0:
            status = "restored"
        elif NOTHING_TO_RESTORE in ANSI_ESCAPE.sub("", text).lower():
            status = "nothing_pending"
        else:
            lines = text.splitlines()[-FAILURE_TAIL_LINES:]
            print(f"::group::{stage} output (last {FAILURE_TAIL_LINES} lines)", file=sys.stderr)
            print("\n".join(lines), file=sys.stderr)
            print("::endgroup::", file=sys.stderr)
    except (FileNotFoundError, PermissionError):
        status = "unavailable"
    except (OSError, subprocess.SubprocessError):
        status = "failed"
    record["status"] = status
    record["seconds"] = round(finish_stage(report, stage, started, status), 3)
    return record


def recover_engine_state(command, directories, game, source, private, stage, report, env, protected=()):
    """Clear the engine's unfinished-install state so the next `install` is allowed to start.

    The engine keeps its pre-install snapshot and `pending` marker in `.modsync-<hash>` beside the
    game directory, outside what `restore_game` empties and refills. The engine's own `restore`
    is run first (it also puts the source directory back); when it is unavailable or fails, only
    this game's recovery sibling is removed. Raises OSError when the marker is still there after.
    """
    record = engine_restore(command, directories, source, private, stage, report, env)
    if record["status"] not in ("restored", "nothing_pending"):
        record["fallback"] = "state_" + remove_engine_state(game, protected)
    if (engine_state_dir(game) / "pending").exists():
        raise OSError("the engine's unfinished-install marker is still present")


def checkpoint_session(text):
    """The last `Checkpoint session: <id>` a TypeScript install log printed, or None."""
    found = None
    for line in text.splitlines():
        match = TS_CHECKPOINT_SESSION.match(ANSI_ESCAPE.sub("", line).rstrip())
        if match:
            found = match.group("id")
    return found


def recover_typescript_state(command, game, text, source, private, stage, report, env):
    """Ask the TypeScript CLI to roll the game back to its session's anchor before the baseline restore.

    The TypeScript engine records a pending mutation in `<game>/.modsync/checkpoints` and refuses to
    start another session until it is resolved ("... requires recovery"). Its own recovery is
    `checkpoints restore <session> 0 --kotor <game> --ci-physical-install`, with the session printed
    in the failed install's log. It is best effort, recorded in engine_restores: when it is unavailable
    (no session in the log) or fails, `fallback` says the baseline restore carries the recovery, which
    empties the game directory, and with it the checkpoint store and any restore intent in it.
    """
    session = checkpoint_session(text)
    invocation = None if session is None else [
        "checkpoints", "restore", session, TYPESCRIPT_FIRST_CHECKPOINT, "--kotor", str(game),
        TYPESCRIPT_PHYSICAL_INSTALL_FLAG]
    record = engine_restore(command, [], source, private, stage, report, env, invocation)
    if record["status"] != "restored":
        record["fallback"] = "baseline_restore"
    return record


def install_with_quarantine(command, directories, deselect, converted, game, source, private, report,
                            env, max_iterations=QUARANTINE_MAX_ITERATIONS,
                            max_fraction=QUARANTINE_MAX_FRACTION, implementation="rust",
                            arguments=None, selectable=None, extra=(), before_phase=None):
    """Run `install`; when one component's step stops it, restore the game, omit that component and retry.

    TypeScript (`implementation="typescript"`): each attempt runs `install --phase base` then
    `--phase widescreen` (`install_base`, `install_widescreen`; retries `install_base_retry_N`, ...)
    with `arguments(deselect)` as the directory/selection arguments and `extra` after the phase.
    The TypeScript CLI names the failing instruction by GUID
    (`ERROR: Instruction <guid> failed (PatcherError); installation stopped with recovery pending`)
    after its `[i/N] <Component>` and `  N. <Action>:` progress lines: the component is the one
    owning that instruction GUID in the converted TOML (see `resolve_instruction_failures`), the
    step and action come from the progress lines. The retry restore is `recover_typescript_state`
    then the baseline restore; a failure in either phase restores the whole baseline and reruns
    both phases. `before_phase()` runs before every phase command.

    A copy of the game directory is taken before the first install. A failed install is only
    quarantined when its output names exactly one (component, step) whose action is in
    INSTALL_QUARANTINE_ACTIONS, shows no panic or failed rollback, the game directory is
    populated, the baseline copy exists, and the budget shared with validate still allows it.
    Before every retry the engine's own `restore` runs for the game (its snapshot and `pending`
    marker live in a `.modsync-<hash>` sibling that the game restore does not touch; see
    recover_engine_state), then the game directory is restored in place and verified against the
    baseline, the component and its dependents are appended to `deselect` and the install re-runs
    as `install_retry_N`. A retry the engine still refuses as an unfinished install is reported as
    `unfinished_install_not_recovered` together with the original attributed failure. Anything
    else leaves the lane failed. Every quarantined component is
    listed in report["quarantined_install_failures"]: the lane is a partial install.
    """
    typescript = implementation == "typescript"
    components = converted_components(converted)
    if selectable is not None:
        components = [component for component in components
                      if normalized_reference(component.get("Guid")) in selectable]
    policy = report.get("quarantine") or start_quarantine(
        components, deselect, report, max_iterations, max_fraction)
    report.setdefault(INSTALL_QUARANTINE_KEY, [])
    started = time.monotonic()
    if policy["max_iterations"] > 0:
        progress("baseline_copy: started")
        baseline = take_baseline(game, private, report)
        finish_stage(report, "baseline_copy", started, (report["quarantine"].get("baseline") or {}).get("status"))
    else:
        baseline = None
    source_dir = directories[directories.index("--source-dir") + 1] if "--source-dir" in directories else None
    protected = (Path(source_dir),) if source_dir else ()
    attempt = 0
    while True:
        suffix = "" if attempt == 0 else f"_retry_{attempt}"
        policy["install_attempts"] = attempt + 1
        stage = "install" + suffix
        try:
            for phase in (TYPESCRIPT_INSTALL_PHASES if typescript else (None,)):
                stage = ("install" if phase is None else "install_" + phase) + suffix
                if before_phase is not None:
                    before_phase()
                if typescript:
                    phase_arguments = arguments(deselect) + ["--phase", phase] + list(extra)
                else:
                    phase_arguments = directories + deselect + ["--non-interactive", "--phase", "all"]
                run(command + ["install"] + phase_arguments, source, private, stage, report, env)
        except Blocked as failure:
            if str(failure) != stage + "_failed":
                raise
            text, code = read_stage_log(private, stage, report)
            record_failure(text, stage, code, report)
            problem = None
            if typescript:
                failures, problem = resolve_instruction_failures(
                    parse_typescript_install_failures(text), components)
                failures = failures or []
            else:
                failures = parse_component_failures(text)
            parsed = failures[-1] if failures else None

            def stop(reason, parsed=parsed, stage=stage, failure=failure):
                quarantine_stop(report, "install", stage, reason, parsed, failure)

            lowered = ANSI_ESCAPE.sub("", text).lower()
            if policy["max_iterations"] <= 0:
                stop("quarantine_disabled")
            if any(marker in lowered for marker in UNFINISHED_INSTALL_MARKERS) and not failures:
                original = next((item for item in reversed(report.get(INSTALL_QUARANTINE_KEY) or ())
                                 if item["kind"] == INSTALL_FAILURE_KIND), None)
                unfinished = next((clean_line(line) for line in text.splitlines()
                                   if any(marker in line.lower() for marker in UNFINISHED_INSTALL_MARKERS)),
                                  None)
                report["failure"].update({
                    "unfinished_install": True, "error_line": unfinished,
                    "original_failure": original and {
                        key: original[key] for key in ("name", "component_guid", "step", "action",
                                                       "error_line", "stage")}})
                quarantine_stop(
                    report, "install", stage, UNFINISHED_INSTALL_REASON, None, failure,
                    details={"error_line": unfinished,
                             "original_failure": report["failure"]["original_failure"],
                             "recovery": (policy.get(ENGINE_RESTORE_KEY) or [None])[-1]})
            if code == 101 or "panicked at" in lowered or "stack backtrace" in lowered:
                stop("panic_detected")
            if "rollback failed" in lowered:
                stop("rollback_failed")
            if problem:
                stop(problem)
            if not failures:
                stop("error_not_attributable")
            if len({(item["component"].casefold(), item["step"]) for item in failures}) > 1:
                stop("multiple_components_failed")
            if parsed["action"].casefold() not in INSTALL_QUARANTINE_ACTIONS:
                stop("action_not_quarantinable")
            if not game_is_populated(game):
                stop("game_missing")
            if baseline is None:
                stop("baseline_copy_failed")

            def restore(parsed=parsed, stage=stage, failure=failure, text=text):
                number = attempt + 1
                try:
                    if typescript:
                        recover_typescript_state(command, game, text, source, private,
                                                 f"engine_restore_{number}", report, env)
                    else:
                        recover_engine_state(command, directories, game, source, private,
                                             f"engine_restore_{number}", report, env, protected)
                    started = time.monotonic()
                    progress(f"baseline_restore_{number}: started")
                    restore_game(game, baseline[0], baseline[1])
                    finish_stage(report, f"baseline_restore_{number}", started, "verified")
                except (OSError, subprocess.SubprocessError, shutil.Error):
                    quarantine_stop(report, "install", stage, "restore_failed", parsed, failure)

            quarantine_failure("install", parsed, stage, components, deselect, game, report, failure,
                               before_commit=restore)
            attempt += 1
            continue
        report.pop("failure", None)
        if baseline is not None:
            shutil.rmtree(baseline[0], ignore_errors=True)
        return


def load_gui_driver_failure(path):
    """(driver reason, sanitized install failure) of a failed TypeScript GUI driver report.

    `(None, None)` when the report is absent, a symlink or unreadable. The install failure is the
    one `gui_typescript.py` captured from `/api/install/status` and `/api/logs` (error text,
    recovery details, failed instruction and mod, last log entries); it is None for a driver
    failure that did not happen during installation.
    """
    try:
        if path.is_symlink() or not path.is_file():
            return None, None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, None
    if not isinstance(data, dict):
        return None, None
    return gui_diag.bounded_text(data.get("reason"), 120), gui_diag.sanitize_install_failure(
        data.get("install_failure"))


GUI_ACTION_NAME = re.compile(r"[A-Za-z]+")


def gui_failure_log(install_failure):
    """The lines the TypeScript CLI would have printed for a failure the GUI worker captured.

    `[i/N] <Mod>`, `  N. <Action>:` and `ERROR: <summary.error>` (the web server's install error is
    the CLI's `Instruction <guid> failed (<Code>); installation stopped with recovery pending: ...`
    text), so the GUI failure goes through `parse_typescript_install_failures` unchanged.
    """
    lines = []
    name = install_failure.get("mod_name")
    if name:
        lines.append("[%d/%d] %s" % (int(install_failure.get("mod_index") or 0),
                                     int(install_failure.get("mod_total") or 0), name))
        action = install_failure.get("action") or ""
        if install_failure.get("step") is not None and GUI_ACTION_NAME.fullmatch(action):
            lines.append("  %d. %s: " % (int(install_failure["step"]), action))
    if install_failure.get("error"):
        lines.append("ERROR: " + install_failure["error"])
    return lines


def attribute_gui_install_failure(install_failure, components):
    """(failures, problem): a GUI install failure pinned to a converted component, like the CLI's.

    The failure is attributed only when the web server's install error names an
    `Instruction <guid> failed (<Code>)`: the component is the one that owns that instruction GUID
    in the converted TOML (`resolve_instruction_failures`: ambiguous or mismatching owners refuse),
    and the web UI's own mod GUID must agree with it (`instruction_component_mismatch` otherwise).
    Anything else (no error text, a checkpoint or lock error, a prompt, a missing mod) returns
    ([], None): not attributable, so the caller fails the lane.
    """
    if not install_failure:
        return [], None
    failures = parse_typescript_install_failures("\n".join(gui_failure_log(install_failure)))
    resolved, problem = resolve_instruction_failures(failures, components)
    if problem:
        return [], problem
    mod_guid = install_failure.get("mod_guid")
    for item in resolved:
        if mod_guid and item.get("component_guid") and normalized_reference(mod_guid) != item["component_guid"]:
            return [], "instruction_component_mismatch"
    return resolved, None


def verify_typescript_gui_result(driver_report, selected_input, gui_work, game, reviewed_inputs_unchanged):
    """The TypeScript GUI driver's completion report, after every receipt and output hash is checked."""
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
            or result.get("input_sha256") != digest(selected_input)
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
    reviewed_inputs_unchanged()
    return result


def install_gui_with_quarantine(command, source, private, game, archives, converted, selected_input,
                                policy_guids, deselect, selectable, report, env,
                                max_iterations=QUARANTINE_MAX_ITERATIONS,
                                max_fraction=QUARANTINE_MAX_FRACTION, reviewed_inputs_unchanged=None):
    """Run the TypeScript GUI driver; when one component's install step stops it, restore, omit it and retry.

    The same loop as `install_with_quarantine`, with the GUI driver as the install: stages `gui`,
    `gui_retry_1`, ... against the one budget shared with validate and the CLI lane, one baseline
    copy of the game taken before the first attempt, and the same refusals. The failure is read
    from the driver report's `install_failure` (the web server's install error, recovery details,
    failed instruction and mod, last log entries; see `attribute_gui_install_failure`), so only a
    driver failure with reason `backend_install_gate_or_failure` whose error names an
    `Instruction <guid> failed (<Code>)` of exactly one converted component, with an action in
    INSTALL_QUARANTINE_ACTIONS, is quarantined. Before each retry the CLI's own `checkpoints
    restore` for the failed session runs, then the game directory is restored to the baseline and
    verified, and the component plus its dependents are unselected in a new copy of the input
    (`converted-gui_retry_N.toml`). Any other driver failure (no install failure captured, a
    prompt, a missing game, a failed rollback, a budget cap) is re-raised or recorded in
    report["quarantine"]["stopped"]; nothing is skipped silently. Every quarantined component is
    listed in report["quarantined_install_failures"]: the lane is a partial install. Returns the
    verified driver report of the attempt that installed.
    """
    components = [component for component in converted_components(converted)
                  if normalized_reference(component.get("Guid")) in selectable]
    policy = report.get("quarantine") or start_quarantine(
        components, deselect, report, max_iterations, max_fraction)
    report.setdefault(INSTALL_QUARANTINE_KEY, [])
    reviewed_inputs_unchanged = reviewed_inputs_unchanged or (lambda: None)
    baseline = None
    if policy["max_iterations"] > 0:
        started = time.monotonic()
        progress("baseline_copy: started")
        baseline = take_baseline(game, private, report)
        finish_stage(report, "baseline_copy", started, (policy.get("baseline") or {}).get("status"))
    attempt = 0
    while True:
        stage = gui_stage_name(attempt)
        policy["install_attempts"] = attempt + 1
        if attempt:
            omitted = policy_guids | {item["component_guid"] for item in quarantined_entries(report)}
            selected_input = private / f"converted-{stage}.toml"
            report["selected_after_policy_count"] = select_base_components(
                converted, omitted, selected_input)
        reviewed_inputs_unchanged()
        driver_report = private / gui_report_name(stage)
        gui_work = private / ("gui-work" if attempt == 0 else f"gui-work-{stage}")
        try:
            run([sys.executable, str(Path(__file__).resolve().with_name("gui_typescript.py")),
                 "--source", str(source), "--input", str(selected_input),
                 "--game-dir", str(game), "--archives", str(archives),
                 "--work-dir", str(gui_work), "--report", str(driver_report),
                 "--timeout", "7000"],
                source, private, stage, report, env)
        except Blocked as failure:
            if str(failure) != stage + "_failed":
                raise
            recorded = report.setdefault("failure", {
                "stage": stage, "exit_code": None, "component": None, "step": None,
                "action": None, "error_line": None})
            reason, install_failure = load_gui_driver_failure(driver_report)
            if reason:
                recorded["driver_reason"] = reason
            if install_failure is None or reason != GUI_INSTALL_FAILURE_REASON:
                progress(f"{stage}: driver failure ({reason or 'no driver report'}) is not an "
                         f"attributable install failure; not quarantined")
                raise
            report.setdefault(GUI_INSTALL_FAILURES_KEY, []).append({"stage": stage, **install_failure})
            failures, problem = attribute_gui_install_failure(install_failure, components)
            parsed = failures[-1] if failures else None
            error_line = clean_line(install_failure.get("error") or "")
            if parsed:
                recorded.update({key: parsed[key] for key in ("component", "step", "action", "error_line")})
                recorded["instruction_guid"] = parsed.get("instruction_guid")
                progress(f"{stage} failed at component '{parsed['component']}', step {parsed['step']} "
                         f"({parsed['action']}), instruction {parsed.get('instruction_guid')}")
            else:
                recorded["error_line"] = error_line or None
                progress(f"{stage} failed with an install error that names no converted component: "
                         f"{error_line or 'no error text captured'}")

            def stop(reason, parsed=parsed, stage=stage, failure=failure, error_line=error_line):
                quarantine_stop(report, "install", stage, reason, parsed, failure,
                                details=None if parsed else {"error_line": error_line})

            lowered = ANSI_ESCAPE.sub("", install_failure.get("error") or "").lower()
            if policy["max_iterations"] <= 0:
                stop("quarantine_disabled")
            if any(marker in lowered for marker in UNFINISHED_INSTALL_MARKERS) and not failures:
                original = next((item for item in reversed(report.get(INSTALL_QUARANTINE_KEY) or ())
                                 if item["kind"] == INSTALL_FAILURE_KIND), None)
                recorded.update({
                    "unfinished_install": True, "error_line": error_line,
                    "original_failure": original and {
                        key: original[key] for key in ("name", "component_guid", "step", "action",
                                                       "error_line", "stage")}})
                quarantine_stop(
                    report, "install", stage, UNFINISHED_INSTALL_REASON, None, failure,
                    details={"error_line": error_line,
                             "original_failure": recorded["original_failure"],
                             "recovery": (policy.get(ENGINE_RESTORE_KEY) or [None])[-1]})
            if "rollback failed" in lowered:
                stop("rollback_failed")
            if problem:
                stop(problem)
            if not failures:
                stop("error_not_attributable")
            if parsed["action"].casefold() not in INSTALL_QUARANTINE_ACTIONS:
                stop("action_not_quarantinable")
            if not game_is_populated(game):
                stop("game_missing")
            if baseline is None:
                stop("baseline_copy_failed")

            def restore(parsed=parsed, stage=stage, failure=failure, install_failure=install_failure):
                number = attempt + 1
                session = install_failure.get("checkpoint_session_id") or ""
                text = f"Checkpoint session: {session}\n" if re.fullmatch(r"[0-9A-Za-z-]+", session) else ""
                try:
                    recover_typescript_state(command, game, text, source, private,
                                             f"engine_restore_{number}", report, env)
                    started = time.monotonic()
                    progress(f"baseline_restore_{number}: started")
                    restore_game(game, baseline[0], baseline[1])
                    finish_stage(report, f"baseline_restore_{number}", started, "verified")
                except (OSError, subprocess.SubprocessError, shutil.Error):
                    quarantine_stop(report, "install", stage, "restore_failed", parsed, failure)

            quarantine_failure("install", parsed, stage, components, deselect, game, report, failure,
                               before_commit=restore)
            attempt += 1
            continue
        result = verify_typescript_gui_result(
            driver_report, selected_input, gui_work, game, reviewed_inputs_unchanged)
        report.pop("failure", None)
        if baseline is not None:
            shutil.rmtree(baseline[0], ignore_errors=True)
        return result


def print_diagnostics(report):
    """Put the skip causes and every uncovered step in the CI log so one read is enough."""
    durations = report.get(STAGE_DURATIONS_KEY)
    if durations:
        print("ports: stage durations: " + ", ".join(
            f"{stage} {format_duration(seconds)}" for stage, seconds in durations.items())
            + f"; total {format_duration(sum(durations.values()))}", file=sys.stderr)
    aliased = report.get("archive_aliases")
    if aliased and aliased.get("count"):
        print(f"ports: archive aliases created for {aliased['count']} Extract source(s) "
              f"(restored archives linked under the names the converted globs match)", file=sys.stderr)
    for item in report.get("alias_unresolved") or ():
        print(f"::warning::ports: no archive alias for {item['component']} [{item['component_guid']}]: "
              f"{item['reason']}; validate will report its download as missing", file=sys.stderr)
    review = report.get("draft_review")
    if review and (review.get("bound") or review.get("bind_failed")):
        print(f"ports: archive binding bound {review.get('bound', 0)} component(s)"
              + (f"; not used: {review['bind_failed']}" if review.get("bind_failed") else ""),
              file=sys.stderr)
    if review and ("cleared" in review or "unreviewed" in review):
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
    for record in report.get(SKIPPED_REPORT_KEY) or ():
        if record.get("bind_diagnostic"):
            print(f"::warning::ports: OMITTED {record['heading']} [{record['component_guid']}] from this "
                  f"lane (reviewed lane skip, temporary, so a green lane is a PARTIAL install): "
                  f"{record['reason']}; converter diagnostic: {record.get('bind_diagnostic_reason')}",
                  file=sys.stderr)
        elif record.get("source") == "lane_skips" and not record.get("kind"):
            print(f"::warning::ports: OMITTED {record['heading']} [{record['component_guid']}] from this "
                  f"lane (reviewed lane skip, so a green lane is a PARTIAL install): {record['reason']}",
                  file=sys.stderr)
    for item in report.get(UNAPPLIED_BIND_SKIPS_KEY) or ():
        outcome = ("the converter now gives it instructions, so it is NOT skipped; remove the entry"
                   if item["state"] == "stale"
                   else "no matching bind diagnostic was recorded, so the coverage gap stands")
        print(f"::warning::ports: lane skip for {item['heading']} [{item['component_guid']}] "
              f"({item['bind_diagnostic']}) not applied: {outcome}", file=sys.stderr)
    pending = report.get(PENDING_REVIEW_KEY)
    if pending:
        print(f"::warning::ports: {pending['count']} component(s) DESELECTED pending human review "
              f"(reviewed policy skips; not installed, not verified): "
              f"{pending['by_kind']['unreviewed_draft']} unreviewed draft(s), "
              f"{pending['by_kind']['dependent_of_deselected']} dependent(s) of a deselected component",
              file=sys.stderr)
        for item in pending["components"]:
            print(f"  - deselected pending review: {item['heading']} [{item['component_guid']}] "
                  f"({item['kind']})", file=sys.stderr)
    for phase, label, outcome in (
            ("validate", "validate failures", "not installed and not verified"),
            ("install", "install failures", "omitted from the retried install and not verified")):
        settings = QUARANTINE_PHASES[phase]
        quarantined = report.get(settings["key"])
        if not quarantined:
            continue
        failing = [item for item in quarantined if item["kind"] == settings["kind"]]
        print(f"::warning::ports: {len(quarantined)} component(s) QUARANTINED after {label} "
              f"({len(failing)} failing, {len(quarantined) - len(failing)} dependent(s)): they are "
              f"{outcome}, so a green lane is a PARTIAL install", file=sys.stderr)
        for item in quarantined:
            if item["kind"] == settings["kind"]:
                at_step = f" at step {item['step']}" if item["step"] is not None else ""
                print(f"::warning::ports: quarantined {item['name']} [{item['component_guid']}]: "
                      f"{phase} failed{at_step} ({item['action']}): {item['error_line']}",
                      file=sys.stderr)
            else:
                print(f"::warning::ports: quarantined {item['name']} [{item['component_guid']}]: "
                      f"depends on quarantined {item['caused_by']}", file=sys.stderr)
    for item in report.get(GUI_INSTALL_FAILURES_KEY) or ():
        print("\n".join(gui_diag.render_install_failure(
            {key: value for key, value in item.items() if key not in ("stage", "log_tail")},
            title=f"{item['stage']} install failure (summary; the log tail is in the gui worker output above)")),
            file=sys.stderr)
    for item in (report.get("quarantine") or {}).get(ENGINE_RESTORE_KEY) or ():
        if item["status"] not in ("restored", "nothing_pending"):
            print(f"::warning::ports: {item['stage']}: engine restore {item['status']}"
                  + (f", fell back to {item['fallback']}" if item.get("fallback") else ""), file=sys.stderr)
    baseline = (report.get("quarantine") or {}).get("baseline")
    if baseline and baseline.get("status") == "copy_failed":
        print("::warning::ports: the game directory baseline copy failed, so an install failure "
              "could not be quarantined (no restore point)", file=sys.stderr)
    stopped = (report.get("quarantine") or {}).get("stopped")
    if stopped:
        print(f"::error::ports: {stopped.get('phase', 'validate')} failure not quarantined "
              f"({stopped['reason']})"
              + (f": {stopped['error_line']}" if stopped.get("error_line") else ""), file=sys.stderr)
        if stopped.get("attributed_components"):
            print(f"::error::ports: the {stopped.get('stage', 'validate')} pass attributed "
                  f"{stopped['attributed_count']} component(s) against a component cap of "
                  f"{stopped['component_cap']} ({stopped['already_quarantined']} already quarantined); "
                  f"none were quarantined: " + ", ".join(repr(name) for name in stopped["attributed_components"]),
                  file=sys.stderr)
    failure = report.get("failure")
    if failure and report.get("status") != "installed":
        where = failure_location(failure)
        if failure.get("driver_reason"):
            where += f" [driver reason: {failure['driver_reason']}]"
        if failure.get("error_line") and not failure.get("component"):
            where += f" [error: {failure['error_line']}]"
        print(f"::error::ports: {failure['stage']} failed (exit {failure['exit_code']}): {where}",
              file=sys.stderr)
        if failure.get("unfinished_install"):
            original = failure.get("original_failure")
            cause = (f"the original failure was component '{original['name']}', step {original['step']} "
                     f"({original['action']}) in {original['stage']}: {original['error_line']}"
                     if original else "no earlier attributed failure was recorded")
            print(f"::error::ports: {failure['stage']} was refused as an unfinished install, not a new "
                  f"component failure; the engine's recovery state was not cleared before the retry; "
                  f"{cause}", file=sys.stderr)
    uncovered = report.get("steps_without_instructions")
    if uncovered:
        print(f"::error::ports: {len(uncovered)} guide steps have no instructions", file=sys.stderr)
        for step in uncovered:
            print(f"  - {step['heading']} [{step['component_guid']}] "
                  f"{step['skip_reason'] or 'no convert skip reason recorded'} "
                  f"<{' '.join(step['urls'])}>", file=sys.stderr)
            if step.get("bind_diagnostic"):
                print(f"    bind diagnostic: {step['bind_diagnostic']}", file=sys.stderr)


IMPLEMENTATION_SECTIONS = {
    "rust": frozenset(),
    "typescript": frozenset({"CRITICAL: PC Version Differences & Fixes"}),
}


TYPESCRIPT_PHYSICAL_INSTALL_FLAG = "--ci-physical-install"
TYPESCRIPT_GAMES = {"k1": "kotor1", "k2": "kotor2"}
SKIPPED_BY_POLICY = "skipped-by-reviewed-policy"
SKIPPED_REPORT_KEY = "skipped_by_reviewed_policy"
LANE_SKIPS_PATH = Path(__file__).with_name("archive_selections.json")
LANE_SKIP_GAMES = ("k1", "k2")
LANE_SKIP_KEYS = frozenset({"section", "heading", "reason"})
LANE_SKIP_PINNED_KEYS = frozenset({"guid", "kind"})
LANE_SKIP_LANES = ("typescript", "rust")
LANE_SKIP_BIND_DIAGNOSTICS = ("loose_file_unresolved", "patcher_unresolved")
UNAPPLIED_BIND_SKIPS_KEY = "unapplied_bind_skips"
LANE_SKIP_KINDS = ("unreviewed_draft", "dependent_of_deselected")
PENDING_REVIEW_KEY = "deselected_pending_review"


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


def load_lane_skips(game, path=None, implementation="rust"):
    """Return the reviewed lane skips for one game from archive_selections.json.

    The `lane_skips` table maps k1/k2 to a list of {section, heading, reason}
    objects, each optionally pinned with a `guid` and a `kind` (both or neither;
    see LANE_SKIP_KINDS). An unpinned entry with `"lane": "rust"` and a `bind_diagnostic`
    (see LANE_SKIP_BIND_DIAGNOSTICS) is a Rust-only skip that coverage applies only while the
    archive binding stage recorded that diagnostic for the component and the component still has
    no instructions (see bind_skip_state). The whole table is validated whatever the game, so a
    malformed entry for the other game still blocks. A missing file or table
    skips nothing. Entries pinned with a `guid`/`kind` record the Rust converter's
    unreviewed-draft state and name Rust GUIDs, so the TypeScript lanes apply only
    the unpinned entries (the policy skips such as the 4GB Patcher). An unpinned entry
    with `"lane": "typescript"` applies to the TypeScript lanes alone, and may share a
    step with a pinned Rust entry.
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
        seen = {"rust": set(), "typescript": set()}
        seen_guids = set()
        for entry in entries:
            if (not isinstance(entry, dict) or not LANE_SKIP_KEYS <= entry.keys()
                    or not entry.keys() <= LANE_SKIP_KEYS | LANE_SKIP_PINNED_KEYS | {"lane", "bind_diagnostic"}
                    or any(not isinstance(entry[key], str) or not entry[key].strip()
                           or entry[key] != entry[key].strip()
                           for key in entry.keys() & (LANE_SKIP_KEYS | {"guid"}))):
                raise Blocked("lane_skips_invalid")
            if "lane" in entry and (entry["lane"] not in LANE_SKIP_LANES
                                    or entry.keys() & LANE_SKIP_PINNED_KEYS):
                raise Blocked("lane_skips_invalid")
            if "bind_diagnostic" in entry and (entry["bind_diagnostic"] not in LANE_SKIP_BIND_DIAGNOSTICS
                                               or entry.get("lane") != "rust"):
                raise Blocked("lane_skips_invalid")
            if entry.keys() & LANE_SKIP_PINNED_KEYS:
                if (entry.keys() & LANE_SKIP_PINNED_KEYS != LANE_SKIP_PINNED_KEYS
                        or entry["kind"] not in LANE_SKIP_KINDS or not canonical_guid(entry["guid"])):
                    raise Blocked("lane_skips_invalid")
                if entry["guid"] in seen_guids:
                    raise Blocked("lane_skips_duplicate")
                seen_guids.add(entry["guid"])
            lanes = ({entry["lane"]} if "lane" in entry
                     else {"rust"} if "guid" in entry else {"rust", "typescript"})
            key = (entry["section"], entry["heading"])
            if any(key in seen[lane] for lane in lanes):
                raise Blocked("lane_skips_duplicate")
            for lane in lanes:
                seen[lane].add(key)
            if name == game and implementation in lanes:
                skip = {"section": entry["section"], "heading": entry["heading"],
                        "reason": entry["reason"], "urls": [], "source": "lane_skips"}
                if "guid" in entry:
                    skip.update(guid=entry["guid"], kind=entry["kind"])
                if "bind_diagnostic" in entry:
                    skip["bind_diagnostic"] = entry["bind_diagnostic"]
                result.append(skip)
    return result


def normalized_reference(value):
    """A GUID reference in the lowercase hyphenated form, whether written {BRACED} or bare."""
    try:
        return str(uuid.UUID(str(value)))
    except ValueError:
        return str(value).lower()


def canonical_guid(value):
    """True when value is a lowercase hyphenated GUID, the form the reports and --deselect use."""
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except ValueError:
        return False


def bind_skip_state(report, identity, kind, component):
    """Whether a lane skip bound to a bind diagnostic applies, and the diagnostic that confirms it.

    "confirmed": the binding stage recorded this diagnostic kind for the component and it still has
    no instructions. "stale": it has instructions (the converter now binds it, so the skip would drop
    an installable component). "unconfirmed": no instructions and no such diagnostic, so the gap is
    not the one that was reviewed and coverage must still fail.
    """
    if isinstance(component, dict) and component.get("Instructions"):
        return "stale", None
    for item in (report or {}).get(BIND_DIAGNOSTICS_KEY) or ():
        if item.get("component_guid") == identity and item.get("kind") == kind:
            return "confirmed", item["reason"]
    return "unconfirmed", None


def merge_lane_skips(restore_skips, lane_skips):
    """Restore-report skips first; a lane skip naming the same step adds nothing."""
    known = {(skip["section"], skip["heading"]) for skip in restore_skips}
    return list(restore_skips) + [skip for skip in lane_skips
                                  if (skip["section"], skip["heading"]) not in known]


def uncovered_steps(report, headings, components, indexes):
    """Heading, GUID, links and the converter's skip reason for each step lacking instructions."""
    reasons = {item["component_guid"]: item["reason"]
               for item in report.get("convert_skips", {}).get("components", [])}
    diagnostics = {}
    for item in report.get(BIND_DIAGNOSTICS_KEY) or ():
        if item.get("component_guid"):
            diagnostics.setdefault(item["component_guid"], item["reason"])
    steps = []
    for index in indexes:
        component = components[index] if isinstance(components[index], dict) else {}
        identity = str(component.get("Guid", "")).lower()
        step = {"heading": headings[index], "component_guid": identity,
                "urls": [urldefrag(url).url for url in component.get("ResourceRegistry", {})],
                "skip_reason": reasons.get(identity)}
        if identity in diagnostics:
            step["bind_diagnostic"] = diagnostics[identity]
        steps.append(step)
    return steps


def guide_step_list(text, implementation="rust"):
    """The counted steps of a guide: (section, heading) in order, every step, and per-section counts."""
    if implementation not in IMPLEMENTATION_SECTIONS:
        raise Blocked("implementation_unknown")
    allowed_sections = {
        "Mod List",
        "Optional Widescreen & Extended Widescreen Support Mods",
        "Enhanced Widescreen Support Mods",
    } | IMPLEMENTATION_SECTIONS[implementation]
    section = None
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
                steps.append((section, line[4:].strip()))
                sections[section] += 1
    return steps, guide_steps, sections


def coverage(guide, converted, report=None, implementation="rust", skipped=()):
    text = guide.read_text(encoding="utf-8")
    steps, guide_steps, sections = guide_step_list(text, implementation)
    headings = [heading for _, heading in steps]
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
    unapplied = []
    for skip in skipped:
        key = (skip["section"], skip["heading"])
        if key not in guide_steps:
            raise Blocked("policy_skipped_step_not_in_guide")
        record = {"status": SKIPPED_BY_POLICY,
                  **{name: value for name, value in skip.items() if name != "guid"},
                  "component_guid": None}
        if key in steps:
            if steps.count(key) != 1:
                raise Blocked("policy_skipped_step_ambiguous")
            index = steps.index(key)
            component = components[index]
            if not isinstance(component, dict) or not component.get("Guid"):
                raise Blocked("policy_skipped_component_unidentified")
            identity = guid(component["Guid"])
            if "guid" in skip and skip["guid"] != identity:
                raise Blocked("policy_skipped_guid_mismatch")
            record["component_guid"] = identity
            if "bind_diagnostic" in skip:
                state, reason = bind_skip_state(report, identity, skip["bind_diagnostic"], component)
                if state != "confirmed":
                    unapplied.append({"section": skip["section"], "heading": skip["heading"],
                                      "component_guid": identity,
                                      "bind_diagnostic": skip["bind_diagnostic"], "state": state})
                    continue
                record["bind_diagnostic_reason"] = reason
            exempt.add(index)
        elif "guid" in skip or "bind_diagnostic" in skip:
            raise Blocked("policy_skipped_guid_unmatched")
        records.append(record)
    skipped_guids = {record["component_guid"] for record in records
                     if record["component_guid"] is not None}
    for other in (component for index, component in enumerate(components) if index not in exempt):
        if isinstance(other, dict) and skipped_guids & {
                normalized_reference(each) for each in other.get("Dependencies", [])}:
            raise Blocked("policy_skipped_component_has_dependents")
    if report is not None:
        report[SKIPPED_REPORT_KEY] = records
        if unapplied:
            report[UNAPPLIED_BIND_SKIPS_KEY] = unapplied
        pending = [{"component_guid": record["component_guid"], "heading": record["heading"],
                    "kind": record["kind"]} for record in records if record.get("kind")]
        if pending:
            report[PENDING_REVIEW_KEY] = {
                "count": len(pending),
                "by_kind": {kind: sum(item["kind"] == kind for item in pending)
                            for kind in LANE_SKIP_KINDS},
                "components": pending}
        reclassify_policy_skips(report, skipped_guids)
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


def component_phase(component):
    """The install phase a converted component belongs to: widescreen or base."""
    return "widescreen" if component.get("IsWidescreen", False) else "base"


def select_base_components(converted, deselect=(), destination=None):
    """Write IsSelected=true for every base-phase, platform-neutral component.

    Widescreen and Aspyr-only components stay unselected: the GUI driver
    refuses them, and the CLI installs them through its own phase flow.
    Components named in `deselect` (GUIDs) are also written unselected. The
    result replaces `converted`, or is written to `destination` when given.
    """
    deselect = {normalized_reference(each) for each in deselect}
    text = converted.read_text(encoding="utf-8")
    document = tomllib.loads(text)
    key = "thisMod" if "thisMod" in document else "Components"
    components = document.get(key)
    if not isinstance(components, list) or not components:
        raise Blocked("selection_components_missing")
    expected = [not (component.get("IsWidescreen", False)
                     or component.get("AspyrOnly", False)
                     or normalized_reference(component.get("Guid")) in deselect)
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
    (destination or converted).write_text(rewritten, encoding="utf-8")
    return sum(expected)


def typescript_import_command(command, guide, output):
    """The ModSync.ts import-md invocation whose output the lanes and the review hash."""
    name = guide.parent.name
    if name not in TYPESCRIPT_GAMES:
        raise Blocked("guide_target_game_unknown")
    return command + ["import-md", str(guide), "--output", str(output),
                      "--game", TYPESCRIPT_GAMES[name],
                      "--source-url", "content/" + name + "/full.md"]


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
    stale = [name for name, path in (("guide_sha256", guide), ("converted_sha256", converted))
             if manifest.get(name) != digest(path)]
    if stale:
        raise Blocked(
            "review_manifest_hash_mismatch: " + ", ".join(stale) + " differ from this run. The converted "
            "TOML depends on the guide and on the ModSync.ts ref (MODSYNC_TS_REF) that built it. Regenerate with: "
            "python3 tools/modsync-ci/gen_ts_review.py --source <ModSync.ts checkout of that ref, built with "
            "`pnpm install --frozen-lockfile && pnpm build`> --game " + guide.parent.name
            + ", then review the diff of ci/" + guide.parent.name + "-typescript-review.json")
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
        if (review.get("phase") != component_phase(component) or review.get("unresolved") != []
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
        review_input = bind_archives(command, source, guide, converted, archives,
                                     args.archive_map, private, report, env,
                                     load_archive_aliases(guide.parent.name))
        try:
            run(command + ["convert", str(review_input), str(reviewed), "--format", "toml",
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
        run(typescript_import_command(command, guide, converted),
            source, private, "convert", report, env)
        report["selected_component_count"] = select_base_components(converted)
    if digest(guide) != report["guide_sha256"]:
        raise Blocked("guide_changed_during_conversion")
    report["converted_sha256"] = digest(converted)
    report["stage"] = "coverage"
    skipped = []
    if args.restore_report is not None:
        skipped = load_policy_skips(args.restore_report.resolve(strict=True), guide)
    skipped = merge_lane_skips(skipped, load_lane_skips(
        guide.parent.name, implementation=args.implementation))
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
        selected_input = converted
        if policy_guids:
            selected_input = private / "converted-policy.toml"
            report["selected_after_policy_count"] = select_base_components(
                converted, policy_guids, selected_input)
        document = tomllib.loads(converted.read_text(encoding="utf-8"))
        installable = [guid(component["Guid"])
                       for component in document.get("thisMod", document.get("Components", []))
                       if guid(component["Guid"]) not in policy_guids]
        report["installable_component_count"] = len(installable)
        all_installable = list(installable)
        alias_archives(converted, args.archive_map, archives, set(installable), report,
                       load_archive_aliases(guide.parent.name))
        def validate_arguments(taken):
            blocked = deselected_guids(taken)
            chosen = [each for each in installable if normalized_reference(each) not in blocked]
            return [str(converted), "--kotor", str(game),
                    "--mods", str(archives), "--select", ",".join(chosen)]

        directories = validate_arguments(deselect)
        validate_with_quarantine(
            command, directories, deselect, converted, game, source, private, report, env,
            getattr(args, "max_quarantine", QUARANTINE_MAX_ITERATIONS),
            getattr(args, "max_quarantine_fraction", QUARANTINE_MAX_FRACTION),
            arguments=validate_arguments,
            selectable={normalized_reference(each) for each in installable})
        quarantined_guids = {item["component_guid"] for item in quarantined_entries(report)}
        if quarantined_guids:
            report["installable_before_quarantine_count"] = len(installable)
            installable = [each for each in installable
                           if normalized_reference(each) not in quarantined_guids]
            report["installable_component_count"] = len(installable)
            directories = validate_arguments(deselect)
            selected_input = private / "converted-policy.toml"
            report["selected_after_policy_count"] = select_base_components(
                converted, policy_guids | quarantined_guids, selected_input)
        def reviewed_inputs_unchanged():
            if (digest(guide) != report["guide_sha256"]
                    or digest(converted) != report["converted_sha256"]):
                raise Blocked("reviewed_inputs_changed")

        if args.mode == "gui":
            result = install_gui_with_quarantine(
                command, source, private, game, archives, converted, selected_input,
                policy_guids, deselect, {normalized_reference(each) for each in all_installable},
                report, env, getattr(args, "max_quarantine", QUARANTINE_MAX_ITERATIONS),
                getattr(args, "max_quarantine_fraction", QUARANTINE_MAX_FRACTION),
                reviewed_inputs_unchanged)
            if report.get(INSTALL_QUARANTINE_KEY):
                omitted = {item["component_guid"] for item in quarantined_entries(report)}
                report.setdefault("installable_before_quarantine_count", len(all_installable))
                report["installable_component_count"] = len(
                    [each for each in all_installable if normalized_reference(each) not in omitted])
            report["gui"] = result
            report["proof"] = "native_gui_and_verified_completion_receipt"
            report["status"] = "installed"
            report["reason"] = result["reason"]
            return
        install_with_quarantine(
            command, directories, deselect, converted, game, source, private, report, env,
            getattr(args, "max_quarantine", QUARANTINE_MAX_ITERATIONS),
            getattr(args, "max_quarantine_fraction", QUARANTINE_MAX_FRACTION),
            implementation="typescript", arguments=validate_arguments,
            selectable={normalized_reference(each) for each in all_installable},
            extra=choices + [TYPESCRIPT_PHYSICAL_INSTALL_FLAG], before_phase=reviewed_inputs_unchanged)
        if report.get(INSTALL_QUARANTINE_KEY):
            omitted = {item["component_guid"] for item in quarantined_entries(report)}
            report.setdefault("installable_before_quarantine_count", len(all_installable))
            report["installable_component_count"] = len(
                [each for each in all_installable if normalized_reference(each) not in omitted])
        report["status"] = "installed"
        report["reason"] = "native_install_commands_succeeded"
        return
    directories = ["--input", str(converted), "--game-dir", str(game),
                   "--source-dir", str(archives)]
    validate_with_quarantine(
        command, directories, deselect, converted, game, source, private, report, env,
        getattr(args, "max_quarantine", QUARANTINE_MAX_ITERATIONS),
        getattr(args, "max_quarantine_fraction", QUARANTINE_MAX_FRACTION))
    quarantined_guids = {item["component_guid"] for item in quarantined_entries(report)}
    gui_skipped = policy_guids | quarantined_guids
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
            + [flag for guid_ in sorted(gui_skipped) for flag in ("--deselect", guid_)],
            source, private, "gui", report, env)
        if not driver_report.is_file() or driver_report.is_symlink():
            raise Blocked("gui_completion_report_missing")
        result = json.loads(driver_report.read_text(encoding="utf-8"))
        expected_completed = (report["component_count"] - len(gui_skipped) - len(excluded))
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
                or result.get("skipped_by_reviewed_policy") != sorted(gui_skipped)
                or result.get("excluded_widescreen") != sorted(
                    item["component_guid"] for item in excluded)
                or result.get("widescreen_phase") != report["gui_not_covered"]["widescreen_phase"]
                or result.get("coverage") != ("subset" if gui_skipped or excluded else "full")
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
    install_with_quarantine(
        command, directories, deselect, converted, game, source, private, report, env,
        getattr(args, "max_quarantine", QUARANTINE_MAX_ITERATIONS),
        getattr(args, "max_quarantine_fraction", QUARANTINE_MAX_FRACTION))
    report["status"] = "installed"
    report["reason"] = "native_install_command_succeeded"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--implementation", choices=("rust", "typescript"), required=True)
    parser.add_argument("--mode", choices=("cli", "gui"), required=True)
    parser.add_argument("--review-manifest", type=Path,
                        help="separately authored hash-bound JSON review (TypeScript required)")
    parser.add_argument("--archive-map", type=Path,
                        help="cache.py restore --archive-map JSON passed to the Rust convert; TypeScript lanes use it "
                             "to link restored archives under the names the converted Extract globs match")
    parser.add_argument("--restore-report", type=Path,
                        help="cache.py restore --report JSON; its reviewed skipped_steps are exempted from "
                             "coverage, recorded in the report and left out of the install (Rust --deselect, "
                             "TypeScript --select of the rest). Both implementations also apply the reviewed "
                             "lane_skips of archive_selections.json (TypeScript: the unpinned entries only)")
    parser.add_argument("--max-quarantine", type=int, default=QUARANTINE_MAX_ITERATIONS,
                        help="most validate and install failures together to quarantine (Rust), or validate "
                             "failures (TypeScript) "
                             "(deselect and re-run) before the lane fails; 0 disables quarantine "
                             "(default %(default)s)")
    parser.add_argument("--max-quarantine-fraction", type=float, default=QUARANTINE_MAX_FRACTION,
                        help="most quarantined components (validate and install failures plus "
                             "dependents) as a fraction of the selected components (default %(default)s)")
    for name in ("source", "guide", "game-dir", "archives", "work-dir", "report"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(line_buffering=True)
        except (AttributeError, ValueError, OSError):
            pass
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
        print(json.dumps(report), flush=True)
        print_diagnostics(report)
        sys.stderr.flush()
        return 0 if report["status"] == "installed" else 1
    except OSError:
        print("ports: unable to write sanitized report", file=sys.stderr)
        return 2
    finally:
        os.umask(previous_umask)


if __name__ == "__main__":
    sys.exit(main())
