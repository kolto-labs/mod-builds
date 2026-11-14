#!/usr/bin/env python3
"""Execute an externally reviewed manual reference plan (Python 3.11+, POSIX).

Contract: JSON object with guide_sha256, archive_sha256, steps, supplemental_sections.
archive_sha256 maps {archives}/file paths to reviewed SHA256 identities. Every
archive input file (including files in copied directories) must be bound.
steps are ordered {id, source_heading, operations, timeout_seconds?} objects.
source_heading is the exact Markdown line, including '### '. Every level-three
heading inside the single '## Mod List' section must occur once, in order.
A step may instead be {id, source_heading, omit: {reason, reviewed_by}} with no
operations or timeout: a visible, reviewed omission. It is never executed, appears
in the report as status "omitted" and in report["omitted_steps"], emits a ::warning::
line, and makes the run partial (report status "partial", report["partial"] true).
reason and reviewed_by must be nonempty printable strings; omit together with
operations (even an empty list) is an error, and at least one step must execute.
Every guide step is therefore executed or explicitly omitted, never silently skipped.
A partial result is NOT a full reference install: later steps run without the
omitted ones and shared .2da/.tlk rows may differ.
supplemental_sections lists {source_heading, review, step_ids} for EVERY level-two
and level-three heading outside that section, in order. review is a nonempty
human review rationale; step_ids explicitly associates any applicable mod steps
(an empty list records reviewed, non-executable guidance). No review is inferred.

Operations (unknown fields are errors):
  {type: extract, source: PATH, destination: PATH} -- ZIP/tar/RAR/7z, fresh destination.
   A "\\" in a member name is a directory separator (as Windows extractors treat
   it); absolute names, drive letters and ".." are rejected after that.
  {type: copy, source: PATH, destination: PATH, skip_existing: BOOL?} -- file or
   directory overlay, merged the way Windows does: every destination component
   (the token's own and each copied entry's) that matches an existing entry
   case-insensitively is that entry, kept under its existing name and overwritten;
   anything else is created under the name as given (a copied entry's own source
   name). Nothing is renamed or lowercased. Two existing entries, or two source
   entries, that differ only in case block. skip_existing (default false) leaves
   every destination FILE that already exists (case-insensitively) untouched and
   does not copy that source file; directories are still merged.
  {type: rename, source: GAME_PATH, destination: GAME_PATH, replace: BOOL?} -- move
   an existing file or directory within game. The destination must not exist
   unless replace is true, which replaces an existing file only (a directory on
   either side of a replacement is an error). Missing destination parents are
   created. Both paths resolve case-insensitively; an existing destination keeps
   its existing name. Source and destination may not be the same entry.
  {type: delete, path: GAME_OR_EXTRACT_PATH, missing_ok: BOOL?, if_any_exists: [GAME_PATH,...]?}
   -- path resolves case-insensitively. With if_any_exists (nonempty; requires
   missing_ok: true) the delete happens only if at least one listed path exists
   (case-insensitively); the step log records which.
Case-insensitive resolution (copy/rename/delete targets, if_any_exists and every
expected_files path) uses PyKotor's CaseAwarePath (pykotor.tools.path,
PYKOTOR_REQUIREMENT; see case_resolve for what manual.py adds: ambiguity blocks,
symlink and root checks). manual.py fails at startup if pykotor is missing.
  {type: patcher, patcher: PATH, sha256: HEX, argv: [ARG,...], cwd: PATH,
   success_log: PATH, success_markers: [EXACT_STRING,...],
   failure_regex: [REGEX,...], expected_files: {GAME_PATH: HEX,...}?,
   expected_changes: "changed_manifest"?}
Patchers require nonempty expected_files or expected_changes="changed_manifest".
Every patcher must change the game manifest. success_log must be a fresh file
under game/extract; all literal success markers must occur and no reviewed
failure regex may match (Python regex, MULTILINE, case sensitivity explicit).
There are no inferred success/error markers or exceptions for "0 errors".
  {type: gui_patcher, patcher, sha256, argv, cwd, success_log, success_markers,
   failure_regex, actions: [ACTION,...], expected_files?, expected_changes?,
   wine_registry: [{key, name, data},...]?}
For GUI-only originals (TSLPatcher.exe has no command line). Same identity, argv,
success log, marker, failure regex, expected file and manifest-change rules as
patcher. The window is driven under an X display (CI: xvfb-run) with xdotool by
the reviewed actions, run in order; nothing is inferred or defaulted. ACTION is
exactly one of:
  {wait_window: REGEX, timeout: SECONDS} -- poll for a visible window name match
  {key: [KEYSYM,...], window: REGEX}  -- focus newest match, send keysyms to it
  {type: TEXT, window: REGEX}          -- focus newest match, type TEXT
  {click: [X, Y], window: REGEX}       -- click at window-relative coordinates
  {sleep: SECONDS}                     -- at most 60
  {wait_log: SECONDS}                  -- poll success_log until every marker is
                                          present or a failure regex matches
REGEX must compile (Python syntax; xdotool matches it as a POSIX extended regex,
so keep to the common subset). SECONDS are positive finite; every wait is also
bounded by the step deadline. Keysyms are nonempty, whitespace-free and do not
start with "-". The newest match is the last window xdotool lists. TEXT and
wine_registry data expand {game_win} to the Wine path of the game root ("Z:" plus
the absolute POSIX path with "/" as "\\"). wine_registry entries run
`wine reg add KEY /v NAME /d DATA /f` before launch; KEY starts with
HKEY_LOCAL_MACHINE\\ or HKEY_CURRENT_USER\\. Each value is read first
(`wine reg query`; only REG_SZ, REG_EXPAND_SZ and REG_DWORD can be read back,
anything else blocks before a write) and put back, or deleted if it did not exist,
after the patcher stopped, also on failure; a failed restore fails a run that
otherwise succeeded. TSLPatcher stays open after
installing, so after the last action the process group is killed and
`wineserver -k` runs; the exit code is NOT evidence. The success log must be
UTF-8; for gui_patcher only, a log that is not valid UTF-8 is decoded as cp1252
(TSLPatcher writes plain ANSI RTF; markers match the raw RTF text). Undecodable
bytes are never replaced. xdotool, wine and wineserver must be on PATH and
DISPLAY set. While wait_log polls, the visible window names are logged every 30
seconds. On failure a best-effort root window screenshot (ImageMagick `import`, if
present) is stored next to the private step log and visible window names are
logged; neither masks the original error. When a patcher or gui_patcher fails, its
success log (if present; first 1 MiB) is copied next to the private step log as
installer-log-N.<ext> before rollback wipes {extract} and restores the game.
Paths start with {archives}/, {extract}/ or {game}/. Extract writes only under
{extract}; copy reads archives/extract and writes game; rename writes game; delete
writes game or extract.
Patcher and cwd must be in archives/extract. argv explicitly names the original
patcher as argv[0], or argv[1] after an absolute wine/wine64 executable. No shell,
substitution, guessed flags, download, plan generation, or automatic approval.
Tokens in argv expand literally; Windows arguments/paths must be supplied by
the reviewer. Commands are trusted external programs, NOT sandboxed.
patcher and gui_patcher run argv as given, without a shell, in a new session (the
whole process group is killed when the step ends), stdin /dev/null and stdout plus
stderr on one pipe that a thread copies into the private step log (a regular-file
stdout makes frozen Windows Pythons such as HoloPatcher abort at startup under Wine).

--game-dir must be an existing, nonempty, fresh vanilla tree supplied by the
operator (vanilla provenance cannot be inferred). --work-dir must not exist.
All roots must be disjoint. The work directory, logs, snapshots and report are
private (0700/0600). Report must be outside game/archives and not preexist.
Snapshots copy every regular file, including empty directories; symlinks and
special files are rejected. Manifests cover paths, kinds, modes, sizes and SHA256.
SHA256 digests are cached per process by (device, inode) with size, mtime_ns and
ctime_ns: an unchanged inode is not re-read, any write (even one that restores
size and mtime) moves ctime and forces a fresh hash, and files changed within
RACY_NS of a hash are never cached (see DIGEST_CACHE). Every manifest still walks
and lstats the whole tree.
Every step gets one deadline (default 1800 seconds). Extraction is bounded to
64 GiB and 100000 entries and reserves space for a full staged rollback on the
game filesystem. Each executed step has a complete pre-step snapshot in its
checkpoint, verified against the pre-step manifest of the live tree before the
step runs. With the default --keep-snapshots 1 the first snapshot is a full copy
and each later one is the previous step's snapshot advanced in place: it is moved
to the new checkpoint and only entries whose manifest entry changed are rewritten
(changed files are unlinked and copied as new inodes, never written in place; the
game tree never shares inodes with a snapshot). The previous checkpoint keeps its
before/after manifests and its receipt records snapshot_advanced_to. With
--keep-snapshots N > 1 every snapshot is a full copy and the last N successful
ones are retained. Snapshot admission budgets the bytes copied (full tree or
changed files) plus rollback space. Failed checkpoints are always preserved. A
failure while advancing leaves the live tree untouched (the previous step had
completed). Failure/interrupt
reclaims extraction scratch, then verifies a staged pre-step tree before an
atomic same-filesystem directory exchange (requires renameat2 support). If
staging cannot fit, live state and snapshot remain intact. Recovery has no
timeout. A hard kill,
power loss, uncooperative process or external tree writer needs operator recovery;
the pending report and retained snapshot are the recovery evidence, not success.
Exit 0: complete or partial (see report status); 1: failed/blocked (consult private
report). No resume or commit.
"""

import argparse
import ctypes
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import re
import select
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import zipfile


SPACE_MARGIN = 16 * 1024 * 1024
EXTRACT_BYTES = 64 * 1024 ** 3
EXTRACT_ENTRIES = 100000


def tree_capacity(entries, destination):
    block = max(4096, os.statvfs(destination).f_frsize)
    return sum(((entry[2] + block - 1) // block * block if entry[0] == "file" else 0)
               + block for entry in entries.values())


def admit_capacity(path, needed, purpose):
    require(shutil.disk_usage(path).free >= needed + SPACE_MARGIN,
            "insufficient capacity for " + purpose)


def extraction_reserve(path, rollback_reserve):
    if rollback_reserve is not None:
        game_parent, needed = rollback_reserve
        if path.stat().st_dev == game_parent.stat().st_dev:
            return SPACE_MARGIN + needed
    return SPACE_MARGIN


def restore_snapshot(snapshot, game, before):
    require(manifest(snapshot) == before, "snapshot damaged; cannot restore")
    admit_capacity(game.parent, tree_capacity(before, game.parent), "staged restore; cannot restore")
    with tempfile.TemporaryDirectory(prefix=".manual-restore-", dir=game.parent) as temporary:
        staging = Path(temporary) / "game"
        copy_tree(snapshot, staging)
        require(manifest(staging) == before, "staged restore verification failed; cannot restore")
        require(game.stat().st_dev == staging.stat().st_dev, "restore requires same filesystem")
        exchange = getattr(ctypes.CDLL(None, use_errno=True), "renameat2", None)
        require(exchange is not None, "atomic directory exchange unavailable; cannot restore")
        exchange.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p,
                             ctypes.c_uint]
        exchange.restype = ctypes.c_int
        if exchange(-100, os.fsencode(staging), -100, os.fsencode(game), 2) != 0:
            error = ctypes.get_errno()
            raise Blocked("atomic restore failed; cannot restore: " + os.strerror(error))
        require(manifest(game) == before, "restored tree verification failed")
        remove_for_restore(staging)


def prune_snapshots(successful, keep, game, after):
    require(manifest(game) == after, "live tree changed; snapshots retained")
    for snapshot, receipt in successful[:-keep]:
        if snapshot.exists():
            remove_for_restore(snapshot)
            receipt["snapshot_retained"] = False


class Blocked(Exception):
    pass


def require(condition, message):
    if not condition:
        raise Blocked(message)


def fields(value, required, optional=()):
    require(isinstance(value, dict) and set(required) <= value.keys()
            and value.keys() <= set(required) | set(optional), "invalid object fields")


def check_deadline(deadline):
    if deadline is not None and time.monotonic() >= deadline:
        raise Blocked("step timed out")


# Digest cache for this process: (st_dev, st_ino) -> ((size, mtime_ns, ctime_ns), hex).
# A hit needs the open file's identity and all three stamps unchanged. ctime is set
# by the kernel on every content write, truncate, chmod, link, rename and utime (so
# a restored mtime still moves it) and cannot be set from user space; a different
# file at the same path has another inode, and a recycled inode is created after
# the entry was cached, so its ctime is newer. Entries are only stored when the
# ctime is RACY_NS older than the clock read before hashing and fstat is unchanged
# after reading: a write in the same coarse timestamp tick as the hash cannot then
# share the cached ctime. A clock that goes backwards clears the cache. Not covered
# (and not covered by a fresh hash either): a process writing the tree while it is
# being hashed, e.g. through an open writable mapping; manifests are taken only
# while no plan process runs.
DIGEST_CACHE = {}
RACY_NS = 2 * 10 ** 9
CACHE_CLOCK = [0]


def cached_digest(metadata):
    """Cached digest for an lstat/fstat result whose inode stamps are unchanged."""
    cached = DIGEST_CACHE.get((metadata.st_dev, metadata.st_ino))
    if (cached is not None and stat.S_ISREG(metadata.st_mode) and time.time_ns() >= CACHE_CLOCK[0]
            and cached[0] == (metadata.st_size, metadata.st_mtime_ns, metadata.st_ctime_ns)):
        return cached[1]
    return None


def sha256(path, deadline=None):
    started = time.time_ns()
    if started < CACHE_CLOCK[0]:
        DIGEST_CACHE.clear()
    CACHE_CLOCK[0] = started
    with path.open("rb") as source:
        opened = os.fstat(source.fileno())
        identity = (opened.st_dev, opened.st_ino)
        stamps = (opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns)
        cached = DIGEST_CACHE.get(identity)
        if cached is not None and cached[0] == stamps and stat.S_ISREG(opened.st_mode):
            return cached[1]
        digest = hashlib.sha256()
        while True:
            check_deadline(deadline)
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        closed = os.fstat(source.fileno())
    result = digest.hexdigest()
    if (stat.S_ISREG(opened.st_mode) and stamps == (closed.st_size, closed.st_mtime_ns, closed.st_ctime_ns)
            and opened.st_ctime_ns < started - RACY_NS):
        DIGEST_CACHE[identity] = (stamps, result)
    else:
        DIGEST_CACHE.pop(identity, None)
    return result


def manifest(root, deadline=None):
    require(root.is_dir() and not root.is_symlink(), "tree root is not a real directory")
    entries = {}

    def visit(path, relative):
        check_deadline(deadline)
        metadata = os.lstat(path)
        mode = stat.S_IMODE(metadata.st_mode)
        if stat.S_ISDIR(metadata.st_mode):
            entries[relative] = ["directory", mode]
            with os.scandir(path) as listing:
                names = sorted(entry.name for entry in listing)
            for name in names:
                visit(os.path.join(path, name), name if relative == "." else relative + "/" + name)
        else:
            require(stat.S_ISREG(metadata.st_mode), "symlink or special file in tree")
            digest = cached_digest(metadata)
            entries[relative] = ["file", mode, metadata.st_size,
                                 digest if digest is not None else sha256(Path(path), deadline)]

    visit(str(root), ".")
    return entries


def copy_file(source, destination, deadline=None):
    require(stat.S_ISREG(source.lstat().st_mode), "copy source must be regular")
    require(not destination.is_symlink(), "symlink destination")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as incoming, destination.open("wb") as outgoing:
        while True:
            check_deadline(deadline)
            chunk = incoming.read(1024 * 1024)
            if not chunk:
                break
            outgoing.write(chunk)
    shutil.copystat(source, destination)


def copy_tree(source, destination, deadline=None):
    """Exact copy for snapshots and restores (names as they are, no merging rules)."""
    require(not source.is_symlink(), "symlink source")
    if source.is_file():
        copy_file(source, destination, deadline)
        return
    require(source.is_dir(), "copy source does not exist")
    require(not destination.is_symlink(), "symlink destination")
    destination.mkdir(parents=True, exist_ok=True)
    for child in sorted(source.iterdir()):
        check_deadline(deadline)
        copy_tree(child, destination / child.name, deadline)
    shutil.copystat(source, destination)


# Case-insensitive paths. Windows resolves every path component case-insensitively;
# the guide's instructions ("copy to Override", "delete X", "rename X to Y") assume
# that. Plan paths are therefore resolved with PyKotor's CaseAwarePath
# (pykotor.tools.path, PYKOTOR_REQUIREMENT): a component that matches an existing
# entry case-insensitively becomes that entry under its existing name; the first
# component without a match and everything after it keep the plan's spelling. No
# name is ever rewritten or lowercased. CaseAwarePath is used as is; case_resolve
# only adds the guarantees manual.py needs that CaseAwarePath does not give:
#   - ambiguity: when two entries differ only in case, CaseAwarePath silently picks
#     the exact spelling or the closest one; manual.py blocks instead;
#   - its directory cache is keyed by mtime_ns and can be stale within one
#     timestamp tick, so it is cleared before each resolution and the result is
#     checked against a fresh directory listing;
#   - its string normalization strips quotes/whitespace and splits on backslashes, so
#     the result must have the same components (case aside) and stay under the root;
#   - no resolved component may be a symlink (as for the literal path_token path).
# The comparison key is PyKotor's (str.lower() of both names); it is only ever used
# to compare names, never to name anything.
PYKOTOR_REQUIREMENT = "pykotor==2.3.12"
PYKOTOR = []


def case_aware_path():
    """PyKotor's CaseAwarePath, imported on first use. Fails clearly without pykotor."""
    if not PYKOTOR:
        # pykotor's logger configures the root logger with console and ./logs file
        # handlers unless it already has one; manual.py wants neither.
        if not logging.getLogger().handlers:
            logging.getLogger().addHandler(logging.NullHandler())
        # Importing pykotor otherwise patches installed files in site-packages.
        previous = os.environ.get("PYKOTOR_SKIP_MDL_PATCH")
        os.environ["PYKOTOR_SKIP_MDL_PATCH"] = "1"
        try:
            from pykotor.tools.path import CaseAwarePath, clear_cache
        except ImportError as error:
            raise Blocked("pykotor is required for case-insensitive paths (CaseAwarePath); "
                          "install it with: python -m pip install " + PYKOTOR_REQUIREMENT) from error
        finally:
            if previous is None:
                os.environ.pop("PYKOTOR_SKIP_MDL_PATCH", None)
            else:
                os.environ["PYKOTOR_SKIP_MDL_PATCH"] = previous
        PYKOTOR.extend((CaseAwarePath, clear_cache))
    return PYKOTOR[0]


def case_key(name):
    return name.lower()


def case_matches(directory, name):
    """Names in an existing real directory equal to name case-insensitively (fresh listing)."""
    if not directory.is_dir() or directory.is_symlink():
        return []
    with os.scandir(directory) as listing:
        return sorted(entry.name for entry in listing if case_key(entry.name) == case_key(name))


def case_resolve(root, path):
    """path (under root, already checked by path_token) resolved by CaseAwarePath."""
    aware = case_aware_path()
    PYKOTOR[1]()
    wanted = path.relative_to(root).parts
    resolved = Path(str(aware(path)))
    require(resolved.is_relative_to(root), "case-aware path escapes root: " + str(path))
    actual = resolved.relative_to(root).parts
    require(len(actual) == len(wanted) and all(case_key(a) == case_key(b) for a, b in zip(actual, wanted)),
            "case-aware resolution changed the path: " + str(path))
    current, existing = root, True
    for name, found in zip(wanted, actual):
        if existing:
            matches = case_matches(current, name)
            require(len(matches) <= 1, "ambiguous path: entries differing only in case: "
                    + ", ".join(str(current / match) for match in matches))
            existing = bool(matches)
            require(found == (matches[0] if existing else name),
                    "case-aware resolution disagrees with the directory listing: " + str(path))
        else:
            require(found == name, "case-aware resolution changed the path: " + str(path))
        current /= found
        require(not current.is_symlink(), "symlink path")
    return current


def case_token(value, roots, allowed):
    """A plan path token resolved case-insensitively within its root."""
    path = path_token(value, roots, allowed)
    return case_resolve(roots[re.match(r"\{(\w+)\}", value)[1]], path)


def case_index(directory):
    """Existing children of a destination directory by case key; entries that
    differ only in case make the directory ambiguous and block."""
    index = {}
    if directory.is_dir() and not directory.is_symlink():
        with os.scandir(directory) as listing:
            for entry in listing:
                key = case_key(entry.name)
                require(key not in index, "ambiguous path: entries differing only in case in "
                        + str(directory) + ": " + entry.name + ", " + index.get(key, ""))
                index[key] = entry.name
    return index


def overlay(source, destination, deadline=None, skip_existing=False):
    """copy operation: merge source onto destination (already case-resolved) the way
    Windows does. Each child goes to the existing entry whose name matches
    case-insensitively, under that existing name, or is created with the source's own
    name. skip_existing leaves an existing destination file untouched."""
    require(not source.is_symlink(), "symlink source")
    require(not destination.is_symlink(), "symlink destination")
    if source.is_file():
        require(not destination.is_dir(), "copy destination is a directory: " + str(destination))
        if skip_existing and destination.is_file():
            return
        copy_file(source, destination, deadline)
        return
    require(source.is_dir(), "copy source does not exist")
    require(not destination.exists() or destination.is_dir(),
            "copy destination is a file: " + str(destination))
    destination.mkdir(parents=True, exist_ok=True)
    children = sorted(source.iterdir())
    keys = [case_key(child.name) for child in children]
    require(len(set(keys)) == len(keys), "copy source has entries differing only in case: " + str(source))
    index = case_index(destination)
    aware = case_aware_path()
    PYKOTOR[1]()
    # Resolve every child before writing any: each write changes the destination's
    # mtime, which would invalidate PyKotor's directory cache for every later name.
    targets = []
    for child in children:
        check_deadline(deadline)
        target = destination / index.get(case_key(child.name), child.name)
        resolved = Path(str(aware(destination / child.name)))
        # PyKotor cannot represent some names a Linux-made archive can hold (it strips
        # trailing whitespace and '"' and splits on "\\"). For those the fresh,
        # ambiguity-checked listing above is the only answer; no cross-check applies.
        if resolved.parent == destination and case_key(resolved.name) == case_key(child.name):
            require(resolved == target,
                    "case-aware resolution disagrees with the directory listing: " + str(target))
        targets.append((child, target))
    for child, target in targets:
        check_deadline(deadline)
        overlay(child, target, deadline, skip_existing)
    shutil.copystat(source, destination)


def writable_directory(path):
    mode = stat.S_IMODE(path.lstat().st_mode)
    if mode & 0o700 != 0o700:
        path.chmod(mode | 0o700)


def advance_snapshot(snapshot, game, old, new):
    """Turn a snapshot whose verified manifest is old into one of the live game tree
    whose manifest is new, rewriting only entries that differ. Changed files are
    unlinked and copied as new inodes; unchanged files keep their inode and cached
    digest. The caller verifies the result against new."""
    def same(name):
        entry = old.get(name)
        return entry is not None and name in new and entry[0] == new[name][0] and (
            entry[0] == "directory" or entry == new[name])
    for name in sorted((name for name in old if not same(name)), reverse=True):
        path = snapshot / name
        require(not path.is_symlink(), "symlink in snapshot")
        writable_directory(path.parent)
        if old[name][0] == "directory":
            writable_directory(path)
            path.rmdir()
        else:
            path.unlink()
    for name in sorted(name for name in new if not same(name)):
        path = snapshot / name
        writable_directory(path.parent)
        if new[name][0] == "directory":
            path.mkdir()
        else:
            copy_file(game / name, path)
    for name in sorted((name for name in new if new[name][0] == "directory"), reverse=True):
        path = snapshot / name
        if stat.S_IMODE(path.lstat().st_mode) != new[name][1]:
            path.chmod(new[name][1])


def remove_for_restore(path):
    if path.is_symlink() or not path.is_dir():
        path.unlink(missing_ok=True)
        return
    path.chmod(stat.S_IMODE(path.stat().st_mode) | 0o700)
    for child in path.iterdir():
        remove_for_restore(child)
    path.rmdir()


def write_json(path, value):
    descriptor, name = tempfile.mkstemp(prefix=".manual-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(value, output, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_plan(path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, "duplicate JSON key")
            result[key] = value
        return result
    return json.loads(path.read_bytes(), object_pairs_hook=unique)


def guide_headings(text):
    headings = []
    fence = None
    for line in text.splitlines():
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if marker:
            run, tail = marker.groups()
            if fence is None:
                fence = run
            elif run[0] == fence[0] and len(run) >= len(fence) and not tail.strip():
                fence = None
            continue
        if fence is None and re.match(r"^#{1,6} ", line):
            headings.append(line)
    require(fence is None, "unterminated guide code fence")
    require(headings.count("## Mod List") == 1, "expected one exact ## Mod List section")
    mods, supplemental = [], []
    in_mods = False
    for heading in headings:
        if heading.startswith(("# ", "## ")):
            in_mods = heading == "## Mod List"
        if heading.startswith("### ") and in_mods:
            mods.append(heading)
        elif heading.startswith(("## ", "### ")) and heading != "## Mod List":
            supplemental.append(heading)
    require(mods and len(set(mods)) == len(mods), "missing or duplicate mod headings")
    return mods, supplemental


def timeout_value(value):
    require(type(value) in (int, float) and math.isfinite(value) and value > 0,
            "timeout must be positive finite seconds")
    return value


def validate_omission(step):
    require("operations" not in step and "timeout_seconds" not in step,
            "omitted step must not declare operations or timeout")
    omit = step["omit"]
    fields(omit, ("reason", "reviewed_by"))
    for key in ("reason", "reviewed_by"):
        require(isinstance(omit[key], str) and omit[key].strip() and omit[key].isprintable(),
                "omit " + key + " must be a nonempty printable string")


def omitted_steps(plan):
    return [{"id": step["id"], "heading": step["source_heading"],
             "reason": step["omit"]["reason"], "reviewed_by": step["omit"]["reviewed_by"]}
            for step in plan["steps"] if "omit" in step]


def annotation_text(text):
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def warn_omitted(omitted, total):
    for entry in omitted:
        print("::warning title=manual step omitted::%s" % annotation_text(
            "%s (%s) omitted, reviewed by %s: %s" % (entry["id"], entry["heading"],
                                                     entry["reviewed_by"], entry["reason"])),
              flush=True)
    if omitted:
        print("::warning title=partial manual install::%s" % annotation_text(
            "%d of %d guide steps omitted; this is a PARTIAL reference, not a full manual install"
            % (len(omitted), total)), flush=True)


def validate_plan(plan, guide, roots, default_timeout):
    fields(plan, ("guide_sha256", "archive_sha256", "steps", "supplemental_sections"))
    require(isinstance(plan["archive_sha256"], dict), "archive_sha256 must be a mapping")
    for token, digest in plan["archive_sha256"].items():
        path_token(token, roots, ("archives",))
        validate_digest(digest)
    require(plan["guide_sha256"] == sha256(guide), "guide SHA256 mismatch")
    mods, supplemental = guide_headings(guide.read_text(encoding="utf-8"))
    require(isinstance(plan["steps"], list), "steps must be a list")
    identifiers, actual = [], []
    for step in plan["steps"]:
        fields(step, ("id", "source_heading"), ("operations", "timeout_seconds", "omit"))
        require(isinstance(step["id"], str) and step["id"].strip(), "invalid step id")
        identifiers.append(step["id"])
        actual.append(step["source_heading"])
        if "omit" in step:
            validate_omission(step)
            continue
        timeout_value(step.get("timeout_seconds", default_timeout))
        require(isinstance(step.get("operations"), list) and step["operations"], "empty operations")
        for operation in step["operations"]:
            validate_operation(operation, roots)
    require(actual == mods, "mod headings must match exactly once in guide order")
    require(len(set(identifiers)) == len(identifiers), "duplicate step id")
    require(any("omit" not in step for step in plan["steps"]), "plan omits every step; nothing would be installed")
    require(isinstance(plan["supplemental_sections"], list), "supplemental_sections must be a list")
    covered = []
    for section in plan["supplemental_sections"]:
        fields(section, ("source_heading", "review", "step_ids"))
        require(isinstance(section["review"], str) and section["review"].strip(), "missing review rationale")
        require(isinstance(section["step_ids"], list)
                and all(isinstance(item, str) and item in identifiers for item in section["step_ids"])
                and len(set(section["step_ids"])) == len(section["step_ids"]), "invalid reviewed step links")
        covered.append(section["source_heading"])
    require(covered == supplemental, "supplemental headings need explicit ordered reviewed coverage")
    verify_archive_inputs(plan, roots)


def path_token(value, roots, allowed, allow_root=False):
    require(isinstance(value, str) and "\x00" not in value, "invalid path")
    match = re.fullmatch(r"\{(archives|extract|game)\}/(.*)", value)
    require(match is not None and match[1] in allowed, "path uses wrong root")
    root, suffix = roots[match[1]], match[2]
    require(not Path(suffix).is_absolute() and ".." not in Path(suffix).parts,
            "path traversal")
    path = root / suffix
    require(allow_root or path != root, "operation cannot replace root")
    current = root
    require(not current.is_symlink(), "symlink root")
    for part in Path(suffix).parts:
        current /= part
        require(not current.is_symlink(), "symlink path")
    require(path.resolve().is_relative_to(root.resolve()), "path escapes root")
    return path


def command_arguments(operation, roots):
    patcher = path_token(operation["patcher"], roots, ("archives", "extract"))
    cwd = path_token(operation["cwd"], roots, ("archives", "extract"), True)
    argv = operation["argv"]
    require(isinstance(argv, list) and argv
            and all(isinstance(arg, str) and "\x00" not in arg for arg in argv), "invalid argv")
    expanded = []
    for arg in argv:
        for name, root in roots.items():
            arg = arg.replace("{" + name + "}", str(root))
        expanded.append(arg)
    if expanded[0] != str(patcher):
        launcher = Path(expanded[0])
        require(launcher.is_absolute() and launcher.name in ("wine", "wine64")
                and len(expanded) > 1 and expanded[1] == str(patcher),
                "argv must execute original patcher directly or with explicit Wine")
    return patcher, cwd, expanded


def validate_digest(value):
    require(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None,
            "SHA256 must be 64 lowercase hexadecimal characters")


def verify_archive_inputs(plan, roots, deadline=None):
    for step in plan["steps"]:
        for operation in step.get("operations", []):
            source_token = operation.get("source", operation.get("patcher"))
            if isinstance(source_token, str) and source_token.startswith("{archives}/"):
                source = path_token(source_token, roots, ("archives",))
                require(source.exists(), "archive input missing")
                inputs = [source] if source.is_file() else [
                    source / name for name, entry in manifest(source, deadline).items()
                    if entry[0] == "file"]
                for incoming in inputs:
                    token = "{archives}/" + incoming.relative_to(roots["archives"]).as_posix()
                    require(token in plan["archive_sha256"], "archive input has no reviewed SHA256")
    for token, digest in plan["archive_sha256"].items():
        path = path_token(token, roots, ("archives",))
        require(path.is_file() and sha256(path, deadline) == digest, "archive SHA256 mismatch")


def validate_operation(operation, roots):
    require(isinstance(operation, dict), "operation must be an object")
    kind = operation.get("type")
    if kind in ("extract", "copy"):
        fields(operation, ("type", "source", "destination"),
               ("expected_files",) + (("skip_existing",) if kind == "copy" else ()))
        require(type(operation.get("skip_existing", False)) is bool, "skip_existing must be boolean")
        path_token(operation["source"], roots, ("archives",) if kind == "extract" else ("archives", "extract"))
        path_token(operation["destination"], roots, ("extract",) if kind == "extract" else ("game",))
        expected = operation.get("expected_files", {})
        require(isinstance(expected, dict), "expected_files must be a mapping")
        for token, digest in expected.items():
            path_token(token, roots, ("game",))
            validate_digest(digest)
    elif kind == "rename":
        fields(operation, ("type", "source", "destination"), ("replace",))
        source = path_token(operation["source"], roots, ("game",))
        destination = path_token(operation["destination"], roots, ("game",))
        require(type(operation.get("replace", False)) is bool, "replace must be boolean")
        require(not same_or_inside(destination, source),
                "rename destination must differ from and lie outside its source")
    elif kind == "delete":
        fields(operation, ("type", "path"), ("missing_ok", "if_any_exists"))
        path_token(operation["path"], roots, ("game", "extract"))
        require(type(operation.get("missing_ok", False)) is bool, "missing_ok must be boolean")
        if "if_any_exists" in operation:
            conditions = operation["if_any_exists"]
            require(isinstance(conditions, list) and conditions and len(set(map(str, conditions))) == len(conditions),
                    "if_any_exists must be a nonempty list of distinct paths")
            for token in conditions:
                path_token(token, roots, ("game",))
            require(operation.get("missing_ok", False) is True, "if_any_exists requires missing_ok: true")
    elif kind == "patcher":
        fields(operation, PATCHER_FIELDS, ("expected_files", "expected_changes"))
        validate_patcher_fields(operation, roots)
    elif kind == "gui_patcher":
        fields(operation, PATCHER_FIELDS + ("actions",),
               ("expected_files", "expected_changes", "wine_registry"))
        validate_patcher_fields(operation, roots)
        validate_gui_fields(operation)
    else:
        raise Blocked("unsupported operation")


def same_or_inside(path, other):
    """path equals or lies inside other, comparing names case-insensitively."""
    keys, other_keys = [case_key(part) for part in path.parts], [case_key(part) for part in other.parts]
    return keys[:len(other_keys)] == other_keys


PATCHER_FIELDS = ("type", "patcher", "sha256", "argv", "cwd",
                  "success_log", "success_markers", "failure_regex")
GUI_ACTIONS = {"wait_window": ("wait_window", "timeout"), "key": ("key", "window"),
               "type": ("type", "window"), "click": ("click", "window"),
               "sleep": ("sleep",), "wait_log": ("wait_log",)}
GUI_SLEEP_LIMIT = 60
REGISTRY_ROOTS = ("HKEY_LOCAL_MACHINE\\", "HKEY_CURRENT_USER\\")


def compile_regex(pattern, what):
    require(isinstance(pattern, str) and pattern, what + " must be a nonempty string")
    try:
        return re.compile(pattern, re.MULTILINE)
    except re.error as error:
        raise Blocked("invalid " + what) from error


def plain_text(value, what, allow_empty=False):
    require(isinstance(value, str) and (allow_empty or value) and "\x00" not in value,
            what + " must be a " + ("" if allow_empty else "nonempty ") + "string without NUL")


def validate_gui_action(action):
    require(isinstance(action, dict), "gui action must be an object")
    kinds = [kind for kind in GUI_ACTIONS if kind in action]
    require(len(kinds) == 1, "gui action must name exactly one action")
    kind = kinds[0]
    fields(action, GUI_ACTIONS[kind])
    if kind == "wait_window":
        compile_regex(action["wait_window"], "wait_window regex")
        timeout_value(action["timeout"])
    elif kind == "sleep":
        require(timeout_value(action["sleep"]) <= GUI_SLEEP_LIMIT,
                "sleep must be at most %d seconds" % GUI_SLEEP_LIMIT)
    elif kind == "wait_log":
        timeout_value(action["wait_log"])
    else:
        compile_regex(action["window"], "window regex")
        if kind == "key":
            keys = action["key"]
            require(isinstance(keys, list) and keys and all(
                isinstance(key, str) and key and not key.startswith("-")
                and not any(character.isspace() for character in key) for key in keys),
                "key must be a nonempty list of keysyms without whitespace or leading '-'")
        elif kind == "type":
            plain_text(action["type"], "type text")
        else:
            point = action["click"]
            require(isinstance(point, list) and len(point) == 2
                    and all(type(item) is int and item >= 0 for item in point),
                    "click must be [X, Y] non-negative integers")
    return kind


def validate_gui_fields(operation):
    actions = operation["actions"]
    require(isinstance(actions, list) and actions, "actions must be a nonempty list")
    for action in actions:
        validate_gui_action(action)
    registry = operation.get("wine_registry", [])
    require(isinstance(registry, list), "wine_registry must be a list")
    for entry in registry:
        fields(entry, ("key", "name", "data"))
        plain_text(entry["key"], "registry key")
        require(any(entry["key"].startswith(root) and len(entry["key"]) > len(root)
                    for root in REGISTRY_ROOTS),
                "registry key must start with HKEY_LOCAL_MACHINE\\ or HKEY_CURRENT_USER\\")
        plain_text(entry["name"], "registry value name")
        plain_text(entry["data"], "registry data", allow_empty=True)


def validate_patcher_fields(operation, roots):
    command_arguments(operation, roots)
    validate_digest(operation["sha256"])
    path_token(operation["success_log"], roots, ("game", "extract"))
    for key in ("success_markers", "failure_regex"):
        require(isinstance(operation[key], list) and operation[key]
                and all(isinstance(item, str) and item.strip() for item in operation[key]),
                key + " must be a nonempty list of nonempty strings")
    for pattern in operation["failure_regex"]:
        try:
            re.compile(pattern, re.MULTILINE)
        except re.error as error:
            raise Blocked("invalid failure regex") from error
    expected = operation.get("expected_files", {})
    require(isinstance(expected, dict), "expected_files must be a mapping")
    for token, digest in expected.items():
        path_token(token, roots, ("game",))
        validate_digest(digest)
    if "expected_changes" in operation:
        require(operation["expected_changes"] == "changed_manifest", "invalid expected_changes")
    require(expected or operation.get("expected_changes") == "changed_manifest",
            "patcher requires expected files or changed manifest proof")
    require(operation["success_log"] not in expected, "installer log is not an expected game effect")


def parse_7z_listing(text):
    """Split `7z l -slt -ba` output into one key/value mapping per member.

    7-Zip prints every property its handler supports, so an unset one appears
    as "Key = " with an empty value (RAR5 ends each record with "NT Security = ").
    """
    records = []
    for record in text.strip("\r\n").split("\n\n"):
        metadata = {}
        for line in record.splitlines():
            key, separator, value = line.partition(" = ")
            if not separator and line.endswith(" ="):
                key, separator, value = line[:-2], " = ", ""
            require(separator and key and key not in metadata, "ambiguous 7z listing")
            metadata[key] = value
        records.append(metadata)
    return records


def check_7z_member(metadata):
    """Validate one listed member; return (path, is_directory, size).

    Link and stream properties are judged by value, not by name: RAR5 and tar
    listings carry "Symbolic Link", "Hard Link", "Copy Link" and "Alternate
    Stream" on every member, empty or "-" unless the member really is one.
    Only a Unix mode string (high attribute bits with 7-Zip's Unix extension
    flag) is read as a file type; high bits without that flag are printed as
    plain hex and say nothing about the member. Windows attribute letters are
    harmless here, and anything that still materialises as a link or special
    file is rejected again after extraction.
    """
    name = metadata.get("Path", "").replace("\\", "/")
    require(name and ":" not in name
            and not any(ord(character) < 32 for character in name)
            and not Path(name).is_absolute()
            and all(part not in ("", ".", "..") for part in name.split("/")),
            "unsafe 7z archive path")
    require(not any(("link" in key.lower() or "stream" in key.lower()) and value not in ("", "-")
                    for key, value in metadata.items()), "7z link or stream member")
    attributes = metadata.get("Attributes", "")
    for field in (attributes, metadata.get("Mode", "")):
        unix_mode = re.search(r"(?:^| )([bcdlps-][rwxstST-]{9})(?: |$)", field)
        require(unix_mode is None or unix_mode[1][0] in ("-", "d"), "special 7z member")
    require(metadata.get("Encrypted", "-") == "-", "encrypted 7z member")
    directory = metadata.get("Folder") == "+" or attributes.startswith("D")
    size = metadata.get("Size", "")
    require(size.isdecimal(), "missing 7z member size")
    size = int(size)
    require(not directory or size == 0, "invalid directory size")
    return name, directory, size


def extract_7z(source, destination, deadline, rollback_reserve=None):
    executable = shutil.which("7z")
    require(executable is not None, "7z executable missing")
    deadline = min(deadline, time.monotonic() + 1800) if deadline is not None else time.monotonic() + 1800
    reserve = extraction_reserve(destination.parent, rollback_reserve)
    byte_limit = min(EXTRACT_BYTES, max(0, shutil.disk_usage(destination.parent).free - reserve))
    require(byte_limit > 0, "insufficient extraction space")

    def run(arguments, cwd, output, limit):
        process = subprocess.Popen([executable, *arguments], cwd=cwd,
                                   stdin=subprocess.DEVNULL, stdout=output,
                                   stderr=subprocess.STDOUT, start_new_session=True, shell=False)
        try:
            while True:
                check_deadline(deadline)
                require(os.fstat(output.fileno()).st_size <= limit, "7z output limit exceeded")
                require(shutil.disk_usage(cwd).free >= reserve, "insufficient extraction space")
                expanded = 0
                entries = 0
                for root, directories, files in os.walk(payload, followlinks=False):
                    check_deadline(deadline)
                    for name in directories + files:
                        entries += 1
                        require(entries <= EXTRACT_ENTRIES, "7z entry limit exceeded")
                        metadata = (Path(root) / name).lstat()
                        require(stat.S_ISDIR(metadata.st_mode) or stat.S_ISREG(metadata.st_mode),
                                "extracted link or special file")
                        if stat.S_ISREG(metadata.st_mode):
                            require(metadata.st_nlink == 1, "extracted hard link")
                            expanded += metadata.st_size
                    require(expanded <= byte_limit, "7z expanded byte limit exceeded")
                code = process.poll()
                if code is not None:
                    require(code == 0, "7z failed: " + str(code))
                    break
                time.sleep(0.05)
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()

    with tempfile.TemporaryDirectory(prefix=".manual-unpack-", dir=destination.parent) as temporary:
        staging = Path(temporary)
        payload = staging / "payload"
        payload.mkdir(mode=0o700)
        with tempfile.TemporaryFile(dir=staging) as listing:
            run(["l", "-slt", "-ba", "-sccUTF-8", "-p", "--", str(source.resolve())],
                staging, listing, 8 * 1024 * 1024)
            listing.seek(0)
            records = parse_7z_listing(listing.read().decode("utf-8", errors="strict"))
        expected = {}
        total = 0
        for metadata in records:
            check_deadline(deadline)
            name, directory, size = check_7z_member(metadata)
            require(name not in expected, "duplicate 7z member")
            total += size
            require(total <= byte_limit, "7z expanded byte limit exceeded")
            expected[name] = (directory, size)
            require(len(expected) <= EXTRACT_ENTRIES, "7z entry limit exceeded")
        allocated_paths = set()
        block = max(4096, os.statvfs(staging).f_frsize)
        allocation = 0
        for name, (directory, size) in expected.items():
            allocated_paths.add(Path(name))
            allocation += (size + block - 1) // block * block
            for parent in Path(name).parents:
                if parent != Path("."):
                    allocated_paths.add(parent)
                    require(expected.get(parent.as_posix(), (True, 0))[0], "7z member type conflict")
            require(len(allocated_paths) <= EXTRACT_ENTRIES, "7z entry limit exceeded")
        require(shutil.disk_usage(staging).free >= reserve + allocation + block * len(allocated_paths),
                "insufficient extraction space (rollback reserve protected)")
        with tempfile.TemporaryFile(dir=staging) as output:
            run(["x", "-y", "-p", "-o" + str(payload), "--", str(source.resolve())],
                staging, output, 8 * 1024 * 1024)
        split_backslash_names(payload, deadline)
        actual = {}
        for root, directories, files in os.walk(payload, followlinks=False):
            for name in directories + files:
                check_deadline(deadline)
                path = Path(root) / name
                status = path.lstat()
                require(stat.S_ISDIR(status.st_mode) or stat.S_ISREG(status.st_mode),
                        "extracted link or special file")
                require(not stat.S_ISREG(status.st_mode) or status.st_nlink == 1,
                        "extracted hard link")
                require(path.resolve().is_relative_to(payload.resolve()), "extracted traversal")
                relative = path.relative_to(payload).as_posix()
                directory = stat.S_ISDIR(status.st_mode)
                if directory and relative not in expected:
                    require(any(member.startswith(relative + "/") for member in expected),
                            "unexpected extracted directory")
                else:
                    actual[relative] = (directory, 0 if directory else status.st_size)
                path.chmod(0o700 if directory or status.st_mode & 0o111 else 0o600)
        require(actual == expected, "7z extracted entries differ from listing")
        require(shutil.disk_usage(staging).free >= reserve, "insufficient extraction space")
        payload.rename(destination)


def split_backslash_names(payload, deadline=None):
    """Move entries 7z extracted under a name containing "\\" to the nested path
    that name denotes, as Windows extractors would create it. The pieces are
    checked like listing paths; an existing entry at the new path blocks."""
    pending = [payload]
    while pending:
        directory = pending.pop()
        for child in sorted(directory.iterdir()):
            check_deadline(deadline)
            require(not child.is_symlink(), "extracted link or special file")
            target = child
            if "\\" in child.name:
                parts = child.name.split("\\")
                require(all(part not in ("", ".", "..") and ":" not in part for part in parts),
                        "unsafe 7z archive path")
                target = directory.joinpath(*parts)
                require(not target.exists() and not target.is_symlink(), "duplicate 7z member")
                for parent in reversed(target.relative_to(directory).parents[:-1]):
                    folder = directory / parent
                    require(not folder.is_symlink() and (folder.is_dir() or not folder.exists()),
                            "7z member type conflict")
                    folder.mkdir(mode=0o700, exist_ok=True)
                child.rename(target)
            if target.is_dir():
                pending.append(target)


def extract(source, destination, deadline, rollback_reserve=None):
    require(not destination.exists(), "extraction destination must be fresh")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as incoming:
        signature = incoming.read(8)
    if signature.startswith((b"7z\xbc\xaf\x27\x1c", b"Rar!\x1a\x07")):
        extract_7z(source, destination, deadline, rollback_reserve)
        return
    with tempfile.TemporaryDirectory(prefix=".manual-unpack-", dir=destination.parent) as temporary:
        payload = Path(temporary) / "payload"
        payload.mkdir(mode=0o700)
        extract_payload(source, payload, deadline, rollback_reserve)
        payload.rename(destination)


def extract_payload(source, destination, deadline, rollback_reserve=None):
    reserve = extraction_reserve(destination, rollback_reserve)
    byte_limit = min(EXTRACT_BYTES, max(0, shutil.disk_usage(destination).free - reserve))
    require(byte_limit > 0, "insufficient extraction space")
    seen = set()
    allocated_paths = set()
    block = max(4096, os.statvfs(destination).f_frsize)
    expanded = 0
    declared = 0

    def capacity(needed=0):
        require(shutil.disk_usage(destination).free >= reserve + needed,
                "insufficient extraction space (rollback reserve protected)")

    def target(name):
        require(not re.match(r"^[A-Za-z]:", name), "unsafe archive path")
        require(not Path(name).is_absolute() and ".." not in Path(name).parts,
                "archive traversal")
        result = destination / name
        require(result != destination and result.resolve().is_relative_to(destination.resolve()),
                "invalid archive member")
        require(result not in seen, "duplicate archive member")
        seen.add(result)
        relative = result.relative_to(destination)
        allocated_paths.update([relative, *relative.parents])
        require(len(allocated_paths) <= EXTRACT_ENTRIES, "archive entry limit exceeded")
        return result

    def unpack(name, directory, incoming, mode, size):
        nonlocal expanded, declared
        check_deadline(deadline)
        # Windows extractors treat "\\" in member names as a directory separator.
        name = name.replace("\\", "/")
        directory = directory or name.endswith("/")
        require(size >= 0 and (not directory or size == 0), "invalid archive member size")
        declared += size
        require(declared <= byte_limit, "archive expanded byte limit exceeded")
        output = target(name.rstrip("/"))
        capacity((size + block - 1) // block * block
                 + block * (len(output.relative_to(destination).parts) + 1))
        if directory:
            require(not output.exists() or output.is_dir(), "archive member type conflict")
            output.mkdir(parents=True, exist_ok=True)
            return
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("xb") as outgoing:
            written = 0
            while True:
                check_deadline(deadline)
                chunk = incoming.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                expanded += len(chunk)
                require(written <= size and expanded <= byte_limit,
                        "archive expanded byte limit exceeded")
                capacity(len(chunk) + block)
                outgoing.write(chunk)
            require(written == size, "archive member size mismatch")
        output.chmod(0o700 if mode & 0o111 else 0o600)

    if zipfile.is_zipfile(source):
        with zipfile.ZipFile(source) as archive:
            for member in archive.infolist():
                mode = member.external_attr >> 16
                require(stat.S_IFMT(mode) in (0, stat.S_IFREG, stat.S_IFDIR), "special ZIP member")
                with archive.open(member) as incoming:
                    unpack(member.filename, member.is_dir(), incoming, mode, member.file_size)
    else:
        with tarfile.open(source, "r:*") as archive:
            for member in archive:
                require(member.isfile() or member.isdir(), "special tar member")
                if member.isdir():
                    unpack(member.name, True, None, member.mode, member.size)
                else:
                    with archive.extractfile(member) as incoming:
                        unpack(member.name, False, incoming, member.mode, member.size)
    capacity()


def prepare_patcher(operation, roots, deadline):
    """Shared pre-launch checks: original patcher identity, fresh success log and
    the game manifest the run must change."""
    patcher, cwd, argv = command_arguments(operation, roots)
    require(patcher.is_file() and cwd.is_dir(), "patcher or working directory missing")
    success_log = path_token(operation["success_log"], roots, ("game", "extract"))
    require(not success_log.exists(), "installer success log must be fresh")
    require(sha256(patcher, deadline) == operation["sha256"], "patcher SHA256 mismatch")
    before = manifest(roots["game"], deadline)
    check_deadline(deadline)
    return cwd, argv, before


def decode_installer_log(data, encodings):
    """Strict decoding in the given order; bytes are never replaced or dropped."""
    for encoding in encodings:
        try:
            return data.decode(encoding, errors="strict")
        except UnicodeDecodeError:
            continue
    raise Blocked("installer success log is not valid " + " or ".join(encodings))


def log_verdict(text, operation):
    """(all success markers present, any reviewed failure regex matched)."""
    return (all(marker in text for marker in operation["success_markers"]),
            any(re.search(pattern, text, re.MULTILINE) for pattern in operation["failure_regex"]))


def verify_patcher_result(operation, roots, before, deadline, encodings=("utf-8",)):
    """Shared proof of a patcher run: fresh log with every marker and no failure
    match, a changed game manifest and every expected file digest."""
    success_log = path_token(operation["success_log"], roots, ("game", "extract"))
    require(success_log.is_file(), "installer success log missing")
    markers, failed = log_verdict(decode_installer_log(success_log.read_bytes(), encodings), operation)
    require(markers, "installer success marker missing")
    require(not failed, "installer failure marker matched")
    after = manifest(roots["game"], deadline)
    if success_log.is_relative_to(roots["game"]):
        after.pop(success_log.relative_to(roots["game"]).as_posix(), None)
    before_effects = {name: entry for name, entry in before.items() if entry[0] == "file"}
    after_effects = {name: entry for name, entry in after.items() if entry[0] == "file"}
    require(before_effects != after_effects, "patcher produced no game manifest changes")
    for token, digest in operation.get("expected_files", {}).items():
        target = case_token(token, roots, ("game",))
        require(target.is_file() and sha256(target, deadline) == digest,
                "patcher expected file SHA256 mismatch")
    check_deadline(deadline)


def stop_process_group(process):
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


PIPE_POLL = 0.25
PIPE_DRAIN_SECONDS = 5


class PatcherProcess:
    """The original patcher in its own session (no shell), stdin /dev/null, stdout and
    stderr on one pipe that a thread copies into the private step log.

    Not a regular file: under Wine, a frozen Windows Python (PyInstaller HoloPatcher)
    whose stdout/stderr is a regular file aborts before reading any argument ("Fatal
    Python error: init_sys_streams ... [WinError 6] Invalid handle"; Wine 9.0 in CI,
    as in Wine bug 51813), while the same program starts with pipes."""

    def __init__(self, argv, cwd, log):
        log.flush()
        self.log = log
        self.process = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT, start_new_session=True, shell=False)
        self.stopping = threading.Event()
        self.thread = threading.Thread(target=self.copy_output, daemon=True)
        self.thread.start()

    def copy_output(self):
        descriptor = self.process.stdout.fileno()
        while True:
            try:
                readable, _, _ = select.select([descriptor], [], [], PIPE_POLL)
                if not readable:
                    if self.stopping.is_set():
                        return
                    continue
                data = os.read(descriptor, 65536)
                if not data:
                    return
                self.log.write(data)
                self.log.flush()
            except (OSError, ValueError):
                return

    def poll(self):
        return self.process.poll()

    @property
    def returncode(self):
        return self.process.returncode

    def wait(self, timeout=None):
        return self.process.wait(timeout=timeout)

    def finish(self):
        """After the group is killed: copy what is left, bounded (a daemonized Wine
        helper may still hold the pipe), then close it."""
        self.stopping.set()
        self.thread.join(PIPE_DRAIN_SECONDS)
        if not self.thread.is_alive():
            self.process.stdout.close()


def stop_patcher(process):
    try:
        stop_process_group(process.process)
    finally:
        process.finish()


def run_patcher(operation, roots, log, deadline):
    cwd, argv, before = prepare_patcher(operation, roots, deadline)
    process = PatcherProcess(argv, cwd, log)
    try:
        code = process.wait(timeout=max(0.001, deadline - time.monotonic()))
        require(code == 0, "original patcher exit code: " + str(code))
    finally:
        stop_patcher(process)
    verify_patcher_result(operation, roots, before, deadline)


GUI_POLL = 0.25
GUI_CLEANUP_SECONDS = 30
GUI_WINDOW_REPORT_SECONDS = 30
INSTALLER_LOG_COPY_LIMIT = 1 << 20
GUI_LOG_ENCODINGS = ("utf-8", "cp1252")


def game_win(roots):
    return "Z:" + str(roots["game"]).replace("/", "\\")


def gui_tools():
    tools = {}
    for name in ("xdotool", "wine", "wineserver"):
        tools[name] = shutil.which(name)
        require(tools[name] is not None, name + " executable missing (required by gui_patcher)")
    require(os.environ.get("DISPLAY"), "gui_patcher requires an X display (DISPLAY unset; use xvfb-run)")
    return tools


def gui_note(log, text):
    log.write(("[gui_patcher] " + text + "\n").encode("utf-8", errors="backslashreplace"))
    log.flush()


def remaining(deadline, limit=None):
    check_deadline(deadline)
    left = deadline - time.monotonic()
    return max(0.001, left if limit is None else min(left, limit))


def gui_command(argv, log, deadline, limit=None):
    """Run one helper command bounded by the step deadline; stderr goes to the step log."""
    gui_note(log, "run " + json.dumps(argv))
    try:
        return subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=log,
                              timeout=remaining(deadline, limit), shell=False)
    except subprocess.TimeoutExpired as error:
        check_deadline(deadline)
        raise Blocked("helper command timed out: " + argv[0]) from error


def visible_windows(tools, pattern, log, deadline):
    result = gui_command([tools["xdotool"], "search", "--onlyvisible", "--name", pattern], log, deadline)
    return result.stdout.decode("ascii", errors="strict").split() if result.returncode == 0 else []


def newest_window(tools, pattern, log, deadline):
    windows = visible_windows(tools, pattern, log, deadline)
    require(windows, "no visible window matches: " + pattern)
    return windows[-1]


def focus_window(tools, pattern, log, deadline):
    window = newest_window(tools, pattern, log, deadline)
    result = gui_command([tools["xdotool"], "windowfocus", window], log, deadline)
    require(result.returncode == 0, "cannot focus window: " + pattern)
    return window


def log_window_names(tools, log, deadline, limit=10):
    """List every visible window name in the step log (evidence only)."""
    for window in visible_windows(tools, ".", log, deadline):
        name = gui_command([tools["xdotool"], "getwindowname", window], log, deadline, limit)
        gui_note(log, "visible window %s: %s" % (window, name.stdout.decode("utf-8", "backslashreplace").strip()))


def wait_for_log(operation, roots, seconds, log, deadline, tools=None):
    """Wait until the success log settles on a verdict; the verdict itself is
    checked afterwards by verify_patcher_result. Every GUI_WINDOW_REPORT_SECONDS
    the visible window names go to the step log, so a stuck or error dialog is
    named even when the wait runs out."""
    success_log = path_token(operation["success_log"], roots, ("game", "extract"))
    started = time.monotonic()
    limit = started + seconds
    report = started + GUI_WINDOW_REPORT_SECONDS
    while True:
        check_deadline(deadline)
        if tools is not None and time.monotonic() >= report:
            report = time.monotonic() + GUI_WINDOW_REPORT_SECONDS
            gui_note(log, "waiting for success log, %.0f s elapsed; log %s" % (
                time.monotonic() - started, "present" if success_log.is_file() else "absent"))
            try:
                log_window_names(tools, log, deadline)
            except Exception as error:
                check_deadline(deadline)
                gui_note(log, "window listing failed: " + str(error))
        if success_log.is_file():
            try:
                text = decode_installer_log(success_log.read_bytes(), GUI_LOG_ENCODINGS)
            except Blocked:
                text = None
            if text is not None and any(log_verdict(text, operation)):
                gui_note(log, "success log settled")
                return
        require(time.monotonic() < limit, "installer success log did not settle within %s seconds" % seconds)
        time.sleep(min(GUI_POLL, remaining(deadline)))


def run_gui_action(action, operation, roots, tools, log, deadline):
    kind = validate_gui_action(action)
    gui_note(log, "action " + json.dumps(action))
    if kind == "wait_window":
        limit = time.monotonic() + action["timeout"]
        while not visible_windows(tools, action["wait_window"], log, deadline):
            require(time.monotonic() < limit, "window did not appear within %s seconds: %s"
                    % (action["timeout"], action["wait_window"]))
            time.sleep(min(GUI_POLL, remaining(deadline)))
    elif kind == "sleep":
        time.sleep(remaining(deadline, action["sleep"]))
        check_deadline(deadline)
    elif kind == "wait_log":
        wait_for_log(operation, roots, action["wait_log"], log, deadline, tools)
    elif kind == "key":
        window = focus_window(tools, action["window"], log, deadline)
        result = gui_command([tools["xdotool"], "key", "--window", window, "--delay", "100"]
                             + action["key"], log, deadline)
        require(result.returncode == 0, "xdotool key failed")
    elif kind == "type":
        focus_window(tools, action["window"], log, deadline)
        text = action["type"].replace("{game_win}", game_win(roots))
        result = gui_command([tools["xdotool"], "type", "--delay", "30", "--", text], log, deadline)
        require(result.returncode == 0, "xdotool type failed")
    else:
        window = newest_window(tools, action["window"], log, deadline)
        result = gui_command([tools["xdotool"], "mousemove", "--window", window,
                              str(action["click"][0]), str(action["click"][1]), "click", "1"],
                             log, deadline)
        require(result.returncode == 0, "xdotool click failed")


def gui_diagnostics(tools, log):
    """Best effort failure evidence; never raises, never masks the original error."""
    unbounded = time.monotonic() + 3600
    try:
        importer = shutil.which("import")
        directory = Path(log.name).parent if isinstance(getattr(log, "name", None), str) else None
        if importer is not None and directory is not None:
            index = 1
            while (directory / ("gui-failure-%d.png" % index)).exists():
                index += 1
            gui_command([importer, "-window", "root", str(directory / ("gui-failure-%d.png" % index))],
                        log, unbounded, GUI_CLEANUP_SECONDS)
        if tools.get("xdotool"):
            log_window_names(tools, log, unbounded)
    except Exception as error:
        try:
            gui_note(log, "diagnostics incomplete: " + str(error))
        except Exception:
            pass


def stop_gui(process, tools, log):
    if process.poll() is not None:
        gui_note(log, "patcher exited early with code %s (not evidence)" % process.returncode)
    try:
        stop_process_group(process.process)
        try:
            gui_command([tools["wineserver"], "-k"], log, time.monotonic() + GUI_CLEANUP_SECONDS)
        except Exception as error:
            gui_note(log, "wineserver -k failed: " + str(error))
    finally:
        process.finish()


RESTORABLE_REGISTRY_TYPES = ("REG_SZ", "REG_EXPAND_SZ", "REG_DWORD")


def registry_value(tools, entry, log, deadline):
    """(type, data) of the value a wine_registry entry is about to overwrite, or None
    if it does not exist. Parses `wine reg query KEY /v NAME` ("    NAME    TYPE    DATA").
    A value that cannot be read back exactly blocks the operation before anything is
    written, because it could not be restored."""
    result = gui_command([tools["wine"], "reg", "query", entry["key"], "/v", entry["name"]], log, deadline)
    if result.returncode != 0:
        return None
    try:
        text = result.stdout.decode("ascii")
    except UnicodeDecodeError as error:
        raise Blocked("registry value is not ASCII, cannot restore it: " + entry["name"]) from error
    pattern = re.compile(r"    " + re.escape(entry["name"]) + r"    (REG_[A-Z_]+)    (.*)", re.IGNORECASE)
    matches = [match for line in text.replace("\r", "").split("\n") if (match := pattern.fullmatch(line))]
    require(len(matches) == 1 and matches[0].group(1) in RESTORABLE_REGISTRY_TYPES,
            "cannot read registry value back for restoring: %s\\%s" % (entry["key"], entry["name"]))
    return matches[0].group(1), matches[0].group(2)


def restore_registry(saved, tools, log, strict):
    """Put every wine_registry value back as it was before the run, newest first;
    a value that did not exist is deleted again. Runs after the patcher stopped,
    bounded by its own cleanup timeout so an expired step deadline cannot skip it.
    strict=False (an error is already propagating) only logs failures."""
    failed = []
    for entry, previous in reversed(saved):
        if previous is None:
            argv = [tools["wine"], "reg", "delete", entry["key"], "/v", entry["name"], "/f"]
        else:
            argv = [tools["wine"], "reg", "add", entry["key"], "/v", entry["name"],
                    "/t", previous[0], "/d", previous[1], "/f"]
        try:
            ok = gui_command(argv, log, time.monotonic() + GUI_CLEANUP_SECONDS).returncode == 0
        except Exception as error:
            gui_note(log, "registry restore error: " + str(error))
            ok = False
        if not ok:
            failed.append("%s\\%s" % (entry["key"], entry["name"]))
    if failed:
        gui_note(log, "registry restore failed: " + ", ".join(failed))
        require(not strict, "wine registry restore failed: " + ", ".join(failed))
    elif saved:
        gui_note(log, "registry restored")


def run_gui_patcher(operation, roots, log, deadline):
    tools = gui_tools()
    diagnosed = False
    try:
        cwd, argv, before = prepare_patcher(operation, roots, deadline)
        saved = []
        try:
            for entry in operation.get("wine_registry", []):
                saved.append((entry, registry_value(tools, entry, log, deadline)))
                data = entry["data"].replace("{game_win}", game_win(roots))
                result = gui_command([tools["wine"], "reg", "add", entry["key"], "/v", entry["name"],
                                      "/d", data, "/f"], log, deadline)
                require(result.returncode == 0, "wine reg add failed: " + entry["key"])
            check_deadline(deadline)
            process = PatcherProcess(argv, cwd, log)
            try:
                for action in operation["actions"]:
                    run_gui_action(action, operation, roots, tools, log, deadline)
            except BaseException:
                gui_diagnostics(tools, log)
                diagnosed = True
                raise
            finally:
                stop_gui(process, tools, log)
            verify_patcher_result(operation, roots, before, deadline, GUI_LOG_ENCODINGS)
        except BaseException:
            restore_registry(saved, tools, log, strict=False)
            raise
        restore_registry(saved, tools, log, strict=True)
    except BaseException:
        if not diagnosed:
            gui_diagnostics(tools, log)
        raise


def note(log, text):
    """One line in the private step log (log may be None outside a plan run)."""
    if log is not None:
        log.write(("[manual] " + text + "\n").encode("utf-8", errors="backslashreplace"))
        log.flush()


def preserve_installer_log(operation, roots, log):
    """Best effort: copy a failed patcher's own success log (at most
    INSTALLER_LOG_COPY_LIMIT bytes) next to the private step log before rollback
    wipes the extract tree and restores the game. Never raises."""
    try:
        directory = Path(log.name).parent if isinstance(getattr(log, "name", None), str) else None
        if directory is None:
            return
        source = path_token(operation["success_log"], roots, ("game", "extract"))
        if source.is_symlink() or not source.is_file():
            note(log, "installer log not present after failure: " + str(source))
            return
        index = 1
        while any(directory.glob("installer-log-%d.*" % index)):
            index += 1
        target = directory / ("installer-log-%d%s" % (index, source.suffix or ".log"))
        size = source.stat().st_size
        with source.open("rb") as incoming, target.open("xb") as outgoing:
            outgoing.write(incoming.read(INSTALLER_LOG_COPY_LIMIT))
        note(log, "installer log %s (%d bytes%s) preserved as %s" % (
            source, size, ", truncated to %d" % INSTALLER_LOG_COPY_LIMIT if size > INSTALLER_LOG_COPY_LIMIT
            else "", target.name))
    except Exception as error:
        try:
            note(log, "installer log not preserved: " + str(error))
        except Exception:
            pass


def execute(operation, roots, log, deadline, rollback_reserve=None):
    validate_operation(operation, roots)
    check_deadline(deadline)
    kind = operation["type"]
    if kind in ("patcher", "gui_patcher"):
        try:
            (run_patcher if kind == "patcher" else run_gui_patcher)(operation, roots, log, deadline)
        except BaseException:
            preserve_installer_log(operation, roots, log)
            raise
    elif kind == "rename":
        source = case_token(operation["source"], roots, ("game",))
        destination = case_token(operation["destination"], roots, ("game",))
        require(source.exists(), "rename source missing")
        require(not same_or_inside(destination, source),
                "rename destination must differ from and lie outside its source")
        if destination.exists():
            require(operation.get("replace", False), "rename destination exists (replace not set)")
            require(destination.is_file() and source.is_file(),
                    "rename may replace only a file with a file, never a directory")
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.replace(source, destination)
        note(log, "renamed %s to %s" % (source, destination))
    elif kind == "delete":
        conditions = operation.get("if_any_exists")
        present = [str(path) for path in (case_token(token, roots, ("game",)) for token in conditions or ())
                   if path.exists()]
        if conditions is not None and not present:
            note(log, "delete skipped: none of if_any_exists is present")
        else:
            if present:
                note(log, "delete condition met: " + ", ".join(present))
            path = case_token(operation["path"], roots, ("game", "extract"))
            if path.is_dir():
                shutil.rmtree(path)
                note(log, "deleted directory " + str(path))
            elif path.exists():
                path.unlink()
                note(log, "deleted " + str(path))
            else:
                require(operation.get("missing_ok", False), "delete target missing")
                note(log, "delete target missing (missing_ok): " + str(path))
    else:
        source = path_token(operation["source"], roots, ("archives", "extract"))
        if kind == "extract":
            extract(source, path_token(operation["destination"], roots, ("extract",)),
                    deadline, rollback_reserve)
        else:
            destination = case_token(operation["destination"], roots, ("game",))
            overlay(source, destination, deadline, operation.get("skip_existing", False))
            note(log, "copied %s to %s" % (source, destination))
        for token, digest in operation.get("expected_files", {}).items():
            target = case_token(token, roots, ("game",))
            require(target.is_file() and sha256(target, deadline) == digest,
                    "non-patcher expected file SHA256 mismatch")
    check_deadline(deadline)


def interrupted(signum, frame):
    raise KeyboardInterrupt("termination requested")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    for name in ("guide", "plan", "game-dir", "archives", "work-dir", "report"):
        parser.add_argument("--" + name, required=True, type=Path)
    parser.add_argument("--step-timeout", type=float, default=1800)
    parser.add_argument("--keep-snapshots", type=int, default=1,
                        help="successful checkpoints to retain (minimum 1); failed checkpoints always retained")
    args = parser.parse_args()
    os.umask(0o077)
    report = {"status": "blocked", "steps": []}
    report_path = None
    try:
        require(os.name == "posix", "POSIX host required; use explicit Wine argv for Windows patchers")
        case_aware_path()
        timeout_value(args.step_timeout)
        require(args.keep_snapshots >= 1, "keep-snapshots must be at least 1")
        require(not args.game_dir.is_symlink() and not args.archives.is_symlink()
                and not args.work_dir.is_symlink(), "symlink root")
        roots = {"game": args.game_dir.resolve(), "archives": args.archives.resolve(),
                 "extract": args.work_dir.resolve() / "extract"}
        work = args.work_dir.resolve()
        locations = [roots["game"], roots["archives"], work]
        for index, first in enumerate(locations):
            for second in locations[index + 1:]:
                require(not first.is_relative_to(second) and not second.is_relative_to(first), "roots overlap")
        require(roots["archives"].is_dir(), "archives directory missing")
        require(not work.exists(), "work directory must be fresh")
        requested_report = args.report.resolve()
        require(not args.report.is_symlink() and not requested_report.exists(), "report already exists")
        require(not any(requested_report.is_relative_to(root) for root in locations[:2]), "report inside input tree")
        for source in (args.guide.resolve(), args.plan.resolve()):
            require(not source.is_relative_to(roots["game"]) and not source.is_relative_to(work), "input in mutable tree")
        plan_hash = sha256(args.plan)
        plan = read_plan(args.plan)
        validate_plan(plan, args.guide, roots, args.step_timeout)
        initial = manifest(roots["game"])
        require(any(value[0] == "file" for value in initial.values()), "game must contain fresh vanilla files")
        work.mkdir(mode=0o700, parents=True)
        roots["extract"].mkdir(mode=0o700)
        requested_report.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        report_path = requested_report
        omitted = omitted_steps(plan)
        report.update(status="running", guide_sha256=plan["guide_sha256"], plan_sha256=plan_hash,
                      partial=bool(omitted), omitted_steps=omitted)
        warn_omitted(omitted, len(plan["steps"]))
        write_json(work / "reviewed-plan.json", plan)
        write_json(report_path, report)
        signal.signal(signal.SIGTERM, interrupted)
        successful = []
        previous = None
        for index, step in enumerate(plan["steps"], 1):
            require(sha256(args.plan) == plan_hash and sha256(args.guide) == plan["guide_sha256"],
                    "immutable plan or guide changed")
            if "omit" in step:
                report["steps"].append({"id": step["id"], "status": "omitted",
                                        "reason": step["omit"]["reason"],
                                        "reviewed_by": step["omit"]["reviewed_by"]})
                write_json(report_path, report)
                continue
            checkpoint = work / ("step-%04d" % index)
            checkpoint.mkdir(mode=0o700)
            snapshot = checkpoint / "before"
            receipt = {"id": step["id"], "status": "snapshotting", "checkpoint": str(checkpoint)}
            report["steps"].append(receipt)
            write_json(report_path, report)
            before = manifest(roots["game"])
            restore_bytes = tree_capacity(before, roots["game"].parent)
            advancing = args.keep_snapshots == 1 and previous is not None and previous[0].is_dir()
            copied = {name: entry for name, entry in before.items()
                      if previous[1].get(name) != entry} if advancing else before
            snapshot_bytes = tree_capacity(copied, checkpoint)
            shared = checkpoint.stat().st_dev == roots["game"].parent.stat().st_dev
            admit_capacity(checkpoint, snapshot_bytes + (restore_bytes if shared else 0),
                           "snapshot and rollback reserve")
            if not shared:
                admit_capacity(roots["game"].parent, restore_bytes, "rollback reserve")
            if advancing:
                previous[0].rename(snapshot)
                previous[2]["snapshot_retained"] = False
                previous[2]["snapshot_advanced_to"] = str(checkpoint)
                receipt["snapshot_advanced_from"] = previous[2]["checkpoint"]
                write_json(report_path, report)
                advance_snapshot(snapshot, roots["game"], previous[1], before)
            else:
                copy_tree(roots["game"], snapshot)
            previous = None
            require(manifest(snapshot) == before == manifest(roots["game"]), "snapshot verification failed")
            admit_capacity(roots["game"].parent, restore_bytes, "rollback reserve")
            write_json(checkpoint / "before.json", before)
            receipt["status"] = "pending"
            receipt["snapshot_retained"] = True
            write_json(report_path, report)
            deadline = time.monotonic() + step.get("timeout_seconds", args.step_timeout)
            try:
                with (checkpoint / "operations.log").open("xb") as log:
                    for operation in step["operations"]:
                        log.write((json.dumps(operation) + "\n").encode())
                        log.flush()
                        verify_archive_inputs(plan, roots, deadline)
                        execute(operation, roots, log, deadline,
                                (roots["game"].parent, restore_bytes))
                after = manifest(roots["game"], deadline)
                check_deadline(deadline)
                require(sha256(args.plan) == plan_hash and sha256(args.guide) == plan["guide_sha256"],
                        "immutable plan or guide changed")
                write_json(checkpoint / "after.json", after)
                receipt["status"] = "complete"
                write_json(report_path, report)
            except BaseException:
                receipt["status"] = "restoring"
                signal.signal(signal.SIGINT, signal.SIG_IGN)
                signal.signal(signal.SIGTERM, signal.SIG_IGN)
                try:
                    remove_for_restore(roots["extract"])
                    roots["extract"].mkdir(mode=0o700)
                    restore_snapshot(snapshot, roots["game"], before)
                    receipt["status"] = "rolled_back"
                except BaseException as restore_error:
                    receipt["status"] = "restore_failed"
                    receipt["restore_error"] = "cannot restore: " + str(restore_error)
                    raise
                finally:
                    write_json(report_path, report)
                raise
            successful.append((snapshot, receipt))
            prune_snapshots(successful, args.keep_snapshots, roots["game"], after)
            previous = (snapshot, before, receipt)
            write_json(report_path, report)
        require([item["id"] for item in report["steps"]] == [step["id"] for step in plan["steps"]]
                and all(item["status"] in ("complete", "omitted") for item in report["steps"]),
                "every guide step must be complete or omitted")
        report["status"] = "partial" if omitted else "complete"
        write_json(report_path, report)
        if omitted:
            print("::warning title=partial manual install::%s" % annotation_text(
                "finished PARTIAL: %d of %d guide steps omitted (see report omitted_steps)"
                % (len(omitted), len(plan["steps"]))), flush=True)
        return 0
    except (Exception, KeyboardInterrupt) as error:
        report["status"] = "failed"
        report["error"] = type(error).__name__ + ": " + str(error)
        if report_path is not None:
            write_json(report_path, report)
        print("Manual executor halted; inspect the private report/checkpoints." if report_path else
              "Manual executor blocked: " + str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
