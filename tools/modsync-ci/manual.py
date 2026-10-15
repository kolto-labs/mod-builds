#!/usr/bin/env python3
"""Execute an externally reviewed manual reference plan (Python 3.11+, POSIX).

Contract: JSON object with guide_sha256, archive_sha256, steps, supplemental_sections.
archive_sha256 maps {archives}/file paths to reviewed SHA256 identities. Every
archive input file (including files in copied directories) must be bound.
steps are ordered {id, source_heading, operations, timeout_seconds?} objects.
source_heading is the exact Markdown line, including '### '. Every level-three
heading inside the single '## Mod List' section must occur once, in order.
supplemental_sections lists {source_heading, review, step_ids} for EVERY level-two
and level-three heading outside that section, in order. review is a nonempty
human review rationale; step_ids explicitly associates any applicable mod steps
(an empty list records reviewed, non-executable guidance). No review is inferred.

Operations (unknown fields are errors):
  {type: extract, source: PATH, destination: PATH} -- ZIP/tar/RAR/7z, fresh destination
  {type: copy, source: PATH, destination: PATH} -- file or directory overlay
  {type: delete, path: PATH, missing_ok: BOOL?}
  {type: patcher, patcher: PATH, sha256: HEX, argv: [ARG,...], cwd: PATH,
   success_log: PATH, success_markers: [EXACT_STRING,...],
   failure_regex: [REGEX,...], expected_files: {GAME_PATH: HEX,...}?,
   expected_changes: "changed_manifest"?}
Patchers require nonempty expected_files or expected_changes="changed_manifest".
Every patcher must change the game manifest. success_log must be a fresh file
under game/extract; all literal success markers must occur and no reviewed
failure regex may match (Python regex, MULTILINE, case sensitivity explicit).
There are no inferred success/error markers or exceptions for "0 errors".
Paths start with {archives}/, {extract}/ or {game}/. Extract writes only under
{extract}; copy reads archives/extract and writes game; delete writes game.
Patcher and cwd must be in archives/extract. argv explicitly names the original
patcher as argv[0], or argv[1] after an absolute wine/wine64 executable. No shell,
substitution, guessed flags, download, plan generation, or automatic approval.
Tokens in argv expand literally; Windows arguments/paths must be supplied by
the reviewer. Commands are trusted external programs, NOT sandboxed.

--game-dir must be an existing, nonempty, fresh vanilla tree supplied by the
operator (vanilla provenance cannot be inferred). --work-dir must not exist.
All roots must be disjoint. The work directory, logs, snapshots and report are
private (0700/0600). Report must be outside game/archives and not preexist.
Snapshots copy every regular file, including empty directories; symlinks and
special files are rejected. Manifests cover paths, kinds, modes, sizes and SHA256.
Every step gets one deadline (default 1800 seconds). Extraction is bounded to
64 GiB and 100000 entries and reserves space for a full staged rollback on the
game filesystem. Snapshot admission budgets a full copy plus rollback space.
Only the last successful checkpoint's snapshot is retained by default
(--keep-snapshots); failed checkpoints are always preserved. Failure/interrupt
reclaims extraction scratch, then verifies a staged pre-step tree before an
atomic same-filesystem directory exchange (requires renameat2 support). If
staging cannot fit, live state and snapshot remain intact. Recovery has no
timeout. A hard kill,
power loss, uncooperative process or external tree writer needs operator recovery;
the pending report and retained snapshot are the recovery evidence, not success.
Exit 0: complete; 1: failed/blocked (consult private report). No resume or commit.
"""

import argparse
import ctypes
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
import tarfile
import tempfile
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


def sha256(path, deadline=None):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while True:
            check_deadline(deadline)
            chunk = source.read(1024 * 1024)
            if not chunk:
                return digest.hexdigest()
            digest.update(chunk)


def manifest(root, deadline=None):
    require(root.is_dir() and not root.is_symlink(), "tree root is not a real directory")
    entries = {}

    def visit(path):
        check_deadline(deadline)
        metadata = path.lstat()
        relative = path.relative_to(root).as_posix()
        mode = stat.S_IMODE(metadata.st_mode)
        if stat.S_ISDIR(metadata.st_mode):
            entries[relative] = ["directory", mode]
            for child in sorted(path.iterdir()):
                visit(child)
        else:
            require(stat.S_ISREG(metadata.st_mode), "symlink or special file in tree")
            entries[relative] = ["file", mode, metadata.st_size, sha256(path, deadline)]

    visit(root)
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
        fields(step, ("id", "source_heading", "operations"), ("timeout_seconds",))
        require(isinstance(step["id"], str) and step["id"].strip(), "invalid step id")
        identifiers.append(step["id"])
        actual.append(step["source_heading"])
        timeout_value(step.get("timeout_seconds", default_timeout))
        require(isinstance(step["operations"], list) and step["operations"], "empty operations")
        for operation in step["operations"]:
            validate_operation(operation, roots)
    require(actual == mods, "mod headings must match exactly once in guide order")
    require(len(set(identifiers)) == len(identifiers), "duplicate step id")
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
        for operation in step["operations"]:
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
        fields(operation, ("type", "source", "destination"))
        path_token(operation["source"], roots, ("archives",) if kind == "extract" else ("archives", "extract"))
        path_token(operation["destination"], roots, ("extract",) if kind == "extract" else ("game",))
    elif kind == "delete":
        fields(operation, ("type", "path"), ("missing_ok",))
        path_token(operation["path"], roots, ("game",))
        require(type(operation.get("missing_ok", False)) is bool, "missing_ok must be boolean")
    elif kind == "patcher":
        fields(operation, ("type", "patcher", "sha256", "argv", "cwd",
                           "success_log", "success_markers", "failure_regex"),
               ("expected_files", "expected_changes"))
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
    else:
        raise Blocked("unsupported operation")


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
            records = listing.read().decode("utf-8", errors="strict").strip().split("\n\n")
        expected = {}
        total = 0
        for record in records:
            check_deadline(deadline)
            metadata = {}
            for line in record.splitlines():
                key, separator, value = line.partition(" = ")
                require(separator and key not in metadata, "ambiguous 7z listing")
                metadata[key] = value
            name = metadata.get("Path", "")
            require(name and "\\" not in name and ":" not in name
                    and not any(ord(character) < 32 for character in name)
                    and not Path(name).is_absolute()
                    and all(part not in ("", ".", "..") for part in name.split("/")),
                    "unsafe 7z archive path")
            require(name not in expected, "duplicate 7z member")
            require(not any("link" in key.lower() or "stream" in key.lower()
                            for key in metadata), "7z link or stream member")
            attributes = metadata.get("Attributes", "")
            unix_mode = re.search(r"(?:^| )([bcdlps-][rwxstST-]{9})(?: |$)", attributes)
            require(unix_mode is None or unix_mode[1][0] in ("-", "d"), "special 7z member")
            require(metadata.get("Encrypted", "-") == "-", "encrypted 7z member")
            directory = metadata.get("Folder") == "+" or attributes.startswith("D")
            size = metadata.get("Size", "")
            require(size.isdecimal(), "missing 7z member size")
            size = int(size)
            require(not directory or size == 0, "invalid directory size")
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
        require("\\" not in name and not re.match(r"^[A-Za-z]:", name), "unsafe archive path")
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


def run_patcher(operation, roots, log, deadline):
    patcher, cwd, argv = command_arguments(operation, roots)
    require(patcher.is_file() and cwd.is_dir(), "patcher or working directory missing")
    success_log = path_token(operation["success_log"], roots, ("game", "extract"))
    require(not success_log.exists(), "installer success log must be fresh")
    before = manifest(roots["game"], deadline)
    require(sha256(patcher, deadline) == operation["sha256"], "patcher SHA256 mismatch")
    check_deadline(deadline)
    process = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.DEVNULL, stdout=log,
                               stderr=subprocess.STDOUT, start_new_session=True, shell=False)
    try:
        code = process.wait(timeout=max(0.001, deadline - time.monotonic()))
        require(code == 0, "original patcher exit code: " + str(code))
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
    success_log = path_token(operation["success_log"], roots, ("game", "extract"))
    require(success_log.is_file(), "installer success log missing")
    log_text = success_log.read_text(encoding="utf-8", errors="strict")
    require(all(marker in log_text for marker in operation["success_markers"]),
            "installer success marker missing")
    require(not any(re.search(pattern, log_text, re.MULTILINE)
                    for pattern in operation["failure_regex"]), "installer failure marker matched")
    after = manifest(roots["game"], deadline)
    if success_log.is_relative_to(roots["game"]):
        after.pop(success_log.relative_to(roots["game"]).as_posix(), None)
    before_effects = {name: entry for name, entry in before.items() if entry[0] == "file"}
    after_effects = {name: entry for name, entry in after.items() if entry[0] == "file"}
    require(before_effects != after_effects, "patcher produced no game manifest changes")
    for token, digest in operation.get("expected_files", {}).items():
        target = path_token(token, roots, ("game",))
        require(target.is_file() and sha256(target, deadline) == digest,
                "patcher expected file SHA256 mismatch")
    check_deadline(deadline)


def execute(operation, roots, log, deadline, rollback_reserve=None):
    validate_operation(operation, roots)
    check_deadline(deadline)
    kind = operation["type"]
    if kind == "patcher":
        run_patcher(operation, roots, log, deadline)
    elif kind == "delete":
        path = path_token(operation["path"], roots, ("game",))
        if path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()
        else:
            require(operation.get("missing_ok", False), "delete target missing")
    else:
        source = path_token(operation["source"], roots, ("archives", "extract"))
        destination = path_token(operation["destination"], roots, ("extract", "game"))
        if kind == "extract":
            extract(source, destination, deadline, rollback_reserve)
        else:
            copy_tree(source, destination, deadline)
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
        report.update(status="running", guide_sha256=plan["guide_sha256"], plan_sha256=plan_hash)
        write_json(work / "reviewed-plan.json", plan)
        write_json(report_path, report)
        signal.signal(signal.SIGTERM, interrupted)
        successful = []
        for index, step in enumerate(plan["steps"], 1):
            require(sha256(args.plan) == plan_hash and sha256(args.guide) == plan["guide_sha256"],
                    "immutable plan or guide changed")
            checkpoint = work / ("step-%04d" % index)
            checkpoint.mkdir(mode=0o700)
            snapshot = checkpoint / "before"
            receipt = {"id": step["id"], "status": "snapshotting", "checkpoint": str(checkpoint)}
            report["steps"].append(receipt)
            write_json(report_path, report)
            before = manifest(roots["game"])
            restore_bytes = tree_capacity(before, roots["game"].parent)
            snapshot_bytes = tree_capacity(before, checkpoint)
            shared = checkpoint.stat().st_dev == roots["game"].parent.stat().st_dev
            admit_capacity(checkpoint, snapshot_bytes + (restore_bytes if shared else 0),
                           "snapshot and rollback reserve")
            if not shared:
                admit_capacity(roots["game"].parent, restore_bytes, "rollback reserve")
            copy_tree(roots["game"], snapshot)
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
            write_json(report_path, report)
        report["status"] = "complete"
        write_json(report_path, report)
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
