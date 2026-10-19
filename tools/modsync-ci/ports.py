#!/usr/bin/env python3
"""Private-source, fail-closed full-guide port runner (Python 3.11+)."""

import argparse
import gui_diag
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import tomllib
from urllib.parse import unquote, urldefrag, urlsplit, urlunsplit
import uuid


class Blocked(Exception):
    pass


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


FAILURE_TAIL_LINES = 80
GUI_STAGE = "gui"
GUI_REPORT_NAME = "gui-report.json"
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
        text = log.read_text(encoding="utf-8", errors="replace")
        if stage == GUI_STAGE:
            reemit_gui_failure(text, private / GUI_REPORT_NAME, report)
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
COMPONENT_FAILURE = re.compile(
    r"(?:Error|Exception): (?:Install stopped\. Restore before retrying; snapshot: .+?: )?"
    r"(?P<component>.+?) / step (?P<step>\d+) "
    r"\((?P<action>[^)]*)\): (?P<message>.*?)\s*$")
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


def parse_component_failures(text):
    """Every distinct `Error: <Component> / step N (<Action>): <message>` line of a tool's output.

    A line repeated (the log and the final error) counts once, at its last position.
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
    return found


def parse_component_failure(text):
    """The last `Error: <Component> / step N (<Action>): <message>` line of a tool's output, or None."""
    found = None
    for line in text.splitlines():
        match = COMPONENT_FAILURE.search(ANSI_ESCAPE.sub("", line))
        if match:
            found = match
    return None if found is None else component_failure_fields(found)


def record_failure(text, stage, code, report, print_excerpt=False):
    """Write the failing component/step into the lane report and, optionally, the excerpt into the CI log."""
    failure = {"stage": stage, "exit_code": code, "component": None, "step": None,
               "action": None, "error_line": None}
    parsed = parse_component_failure(text)
    if parsed:
        failure.update(parsed)
    report["failure"] = failure
    if print_excerpt:
        excerpt = failure_excerpt(text)
        print(f"::group::{stage} failure evidence (bounded excerpt, {len(excerpt)} lines)",
              file=sys.stderr)
        print("\n".join(excerpt) if excerpt else "(no Error/Exception line or install-stopped marker in the output)",
              file=sys.stderr)
        print("::endgroup::", file=sys.stderr)
        if parsed:
            print(f"ports: {stage} failed at component '{parsed['component']}', step {parsed['step']} "
                  f"({parsed['action']})", file=sys.stderr)
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
    """Guide link as cache.py keys it: query, fragment and userinfo dropped, host lowercased."""
    text = str(url).strip()
    try:
        parts = urlsplit(text)
        host = parts.hostname or ""
        if parts.port:
            host += ":" + str(parts.port)
    except ValueError:
        return text
    return urlunsplit((parts.scheme, host, parts.path, "", ""))


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


def resolved_archive_bindings(converted, mapping, archives):
    """--resolved-archives entries for components whose links map to exactly one archive present in `archives`."""
    components = components_of(converted)
    seen = {}
    for component in components:
        seen[component.get("Guid")] = seen.get(component.get("Guid"), 0) + 1
    bindings = []
    for component in components:
        guid, name = component.get("Guid"), component_archive(component, mapping)
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


def bind_archives(command, source, guide, converted, archives, archive_map, private, report, env):
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
            converted, mapped_archives(archive_map.resolve(strict=True)), archives)
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
        "attempts": 0, "install_attempts": 0, "stopped": None}
    return report["quarantine"]


def quarantine_stop(report, phase, stage, reason, parsed, failure, cap=False):
    """Record why a failure was not quarantined and fail the lane."""
    settings = QUARANTINE_PHASES[phase]
    report["quarantine"]["stopped"] = {
        "phase": phase, "stage": stage, "reason": reason, "component": parsed and parsed["component"],
        "step": parsed and parsed["step"], "error_line": parsed and parsed["error_line"]}
    raise Blocked(settings["cap"] if cap else settings["blocked"]) from failure


def quarantine_failure(phase, parsed, stage, components, deselect, game, report, failure,
                       before_commit=None):
    """Quarantine the one component `parsed` names, plus its dependents, or fail the lane with a reason.

    Both stages use it, against the one budget in report["quarantine"]: the iteration cap counts
    the failing components of the validate and install lists together, and so does the component
    cap (failing components plus dependents). `before_commit` runs once every check has passed and
    before anything is recorded or deselected; the install stage restores the game there.
    """
    settings = QUARANTINE_PHASES[phase]
    policy = report["quarantine"]

    def stop(reason, cap=False):
        quarantine_stop(report, phase, stage, reason, parsed, failure, cap)

    entries = quarantined_entries(report)
    if policy["max_iterations"] <= 0:
        stop("quarantine_disabled")
    if sum(item["kind"] in QUARANTINE_FAILURE_KINDS for item in entries) >= policy["max_iterations"]:
        stop("iteration_cap_exceeded", True)
    if not game.is_dir() or not any(game.iterdir()) or GAME_MISSING.search(parsed["message"]):
        stop("game_missing")
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
    quarantined = report.setdefault(settings["key"], [])
    quarantined.append({
        "kind": settings["kind"], "name": component.get("Name", component.get("Heading")),
        "component_guid": identity, "step": parsed["step"], "action": parsed["action"],
        "error_line": parsed["error_line"], "stage": stage, "caused_by": None})
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


def validate_with_quarantine(command, directories, deselect, converted, game, source, private, report,
                             env, max_iterations=QUARANTINE_MAX_ITERATIONS,
                             max_fraction=QUARANTINE_MAX_FRACTION):
    """Run `validate`; quarantine each component it names as failing and re-run, visibly and bounded.

    `deselect` is the --deselect flag list shared with install and the GUI driver; quarantined
    components (and their dependents) are appended to it in place. A failure is only
    quarantined when its `Error: <Component> / step N (<Action>): ...` line names exactly one
    converted component that is still selected, the game directory is populated, and neither
    the iteration cap nor max_fraction of the selected components would be exceeded. Anything
    else leaves the lane failed. Every quarantined component is listed in
    report["quarantined_validate_failures"].
    """
    components = converted_components(converted)
    report.setdefault(QUARANTINE_KEY, [])
    start_quarantine(components, deselect, report, max_iterations, max_fraction)
    policy = report["quarantine"]
    iterations = 0
    while True:
        stage = "validate" if iterations == 0 else f"validate_retry_{iterations}"
        policy["attempts"] = iterations + 1
        try:
            run(command + ["validate"] + directories + deselect, source, private, stage, report, env)
        except Blocked as failure:
            if str(failure) != stage + "_failed":
                raise
            text, code = read_stage_log(private, stage, report)
            parsed = parse_component_failure(text)
            record_failure(text, stage, code, report)
            if parsed is None:
                quarantine_stop(report, "validate", stage, "error_not_attributable", None, failure)
            quarantine_failure("validate", parsed, stage, components, deselect, game, report, failure)
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


def install_with_quarantine(command, directories, deselect, converted, game, source, private, report,
                            env, max_iterations=QUARANTINE_MAX_ITERATIONS,
                            max_fraction=QUARANTINE_MAX_FRACTION):
    """Run `install`; when one component's step stops it, restore the game, omit that component and retry.

    A copy of the game directory is taken before the first install. A failed install is only
    quarantined when its output names exactly one (component, step) whose action is in
    INSTALL_QUARANTINE_ACTIONS, shows no panic or failed rollback, the game directory is
    populated, the baseline copy exists, and the budget shared with validate still allows it.
    The game directory is then restored in place and verified against the baseline, the
    component and its dependents are appended to `deselect` and the install re-runs as
    `install_retry_N`. Anything else leaves the lane failed. Every quarantined component is
    listed in report["quarantined_install_failures"]: the lane is a partial install.
    """
    components = converted_components(converted)
    policy = report.get("quarantine") or start_quarantine(
        components, deselect, report, max_iterations, max_fraction)
    report.setdefault(INSTALL_QUARANTINE_KEY, [])
    baseline = take_baseline(game, private, report) if policy["max_iterations"] > 0 else None
    attempt = 0
    while True:
        stage = "install" if attempt == 0 else f"install_retry_{attempt}"
        policy["install_attempts"] = attempt + 1
        try:
            run(command + ["install"] + directories + deselect + ["--non-interactive", "--phase", "all"],
                source, private, stage, report, env)
        except Blocked as failure:
            if str(failure) != stage + "_failed":
                raise
            text, code = read_stage_log(private, stage, report)
            record_failure(text, stage, code, report)
            failures = parse_component_failures(text)
            parsed = failures[-1] if failures else None

            def stop(reason, parsed=parsed, stage=stage, failure=failure):
                quarantine_stop(report, "install", stage, reason, parsed, failure)

            lowered = ANSI_ESCAPE.sub("", text).lower()
            if policy["max_iterations"] <= 0:
                stop("quarantine_disabled")
            if code == 101 or "panicked at" in lowered or "stack backtrace" in lowered:
                stop("panic_detected")
            if "rollback failed" in lowered:
                stop("rollback_failed")
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

            def restore(parsed=parsed, stage=stage, failure=failure):
                try:
                    restore_game(game, baseline[0], baseline[1])
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


def print_diagnostics(report):
    """Put the skip causes and every uncovered step in the CI log so one read is enough."""
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
                print(f"::warning::ports: quarantined {item['name']} [{item['component_guid']}]: "
                      f"{phase} failed at step {item['step']} ({item['action']}): {item['error_line']}",
                      file=sys.stderr)
            else:
                print(f"::warning::ports: quarantined {item['name']} [{item['component_guid']}]: "
                      f"depends on quarantined {item['caused_by']}", file=sys.stderr)
    baseline = (report.get("quarantine") or {}).get("baseline")
    if baseline and baseline.get("status") == "copy_failed":
        print("::warning::ports: the game directory baseline copy failed, so an install failure "
              "could not be quarantined (no restore point)", file=sys.stderr)
    stopped = (report.get("quarantine") or {}).get("stopped")
    if stopped:
        print(f"::error::ports: {stopped.get('phase', 'validate')} failure not quarantined "
              f"({stopped['reason']})"
              + (f": {stopped['error_line']}" if stopped.get("error_line") else ""), file=sys.stderr)
    failure = report.get("failure")
    if failure and report.get("status") != "installed":
        where = (f"component '{failure['component']}', step {failure['step']} ({failure['action']})"
                 if failure.get("component") else "no component could be attributed")
        print(f"::error::ports: {failure['stage']} failed (exit {failure['exit_code']}): {where}",
              file=sys.stderr)
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


TYPESCRIPT_PHYSICAL_INSTALL_FLAG = "--ci-physical-install"
TYPESCRIPT_GAMES = {"k1": "kotor1", "k2": "kotor2"}
SKIPPED_BY_POLICY = "skipped-by-reviewed-policy"
SKIPPED_REPORT_KEY = "skipped_by_reviewed_policy"
LANE_SKIPS_PATH = Path(__file__).with_name("archive_selections.json")
LANE_SKIP_GAMES = ("k1", "k2")
LANE_SKIP_KEYS = frozenset({"section", "heading", "reason"})
LANE_SKIP_PINNED_KEYS = frozenset({"guid", "kind"})
LANE_SKIP_LANES = ("typescript",)
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
    see LANE_SKIP_KINDS). The whole table is validated whatever the game, so a
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
                    or not entry.keys() <= LANE_SKIP_KEYS | LANE_SKIP_PINNED_KEYS | {"lane"}
                    or any(not isinstance(entry[key], str) or not entry[key].strip()
                           or entry[key] != entry[key].strip()
                           for key in entry.keys() & (LANE_SKIP_KEYS | {"guid"}))):
                raise Blocked("lane_skips_invalid")
            if "lane" in entry and (entry["lane"] not in LANE_SKIP_LANES
                                    or entry.keys() & LANE_SKIP_PINNED_KEYS):
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
            exempt.add(index)
        elif "guid" in skip:
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
                                     args.archive_map, private, report, env)
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
        alias_archives(converted, args.archive_map, archives, set(installable), report,
                       load_archive_aliases(guide.parent.name))
        directories = [str(converted), "--kotor", str(game),
                       "--mods", str(archives), "--select", ",".join(installable)]
        run(command + ["validate"] + directories,
            source, private, "validate", report, env)
        if args.mode == "gui":
            if (digest(guide) != report["guide_sha256"]
                    or digest(converted) != report["converted_sha256"]):
                raise Blocked("reviewed_inputs_changed")
            driver_report = private / "gui-report.json"
            gui_work = private / "gui-work"
            run([sys.executable, str(Path(__file__).resolve().with_name("gui_typescript.py")),
                 "--source", str(source), "--input", str(selected_input),
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
            run(command + ["install"] + directories + ["--phase", phase] + choices
                + [TYPESCRIPT_PHYSICAL_INSTALL_FLAG],
                source, private, "install_" + phase, report, env)
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
                        help="Rust lanes: most validate and install failures together to quarantine "
                             "(deselect and re-run) before the lane fails; 0 disables quarantine "
                             "(default %(default)s)")
    parser.add_argument("--max-quarantine-fraction", type=float, default=QUARANTINE_MAX_FRACTION,
                        help="Rust lanes: most quarantined components (validate and install failures plus "
                             "dependents) as a fraction of the selected components (default %(default)s)")
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
