#!/usr/bin/env python3
"""Inventory the restored mod archives of one guide as evidence for authoring a manual plan.

Writes one JSON document: for every file under --archives its SHA-256 and size, every
member it extracts to (relative path, size, SHA-256), the patcher executables it ships
(TSLPatcher.exe, HoloPatcher.exe and any other .exe, each hashed), the text of every
TSLPatcher/HoloPatcher configuration file (namespaces.ini in full, changes.ini's [Settings]
section and section list) and of every small readme/script (.txt, .bat, .ini, ...). The archive map from `cache.py restore --archive-map` ties
every guide link to its restored file names.

It reads, extracts into a private scratch directory and hashes; it never decides which
operation a step needs. Extraction uses 7z, then bsdtar. An archive that neither can open
is recorded with its error, not skipped.

  manual_inventory.py --archives DIR --archive-map FILE --out FILE
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

INI_LIMIT = 256 * 1024
TEXT_LIMIT = 64 * 1024
TEXT_SUFFIXES = (".txt", ".bat", ".cmd", ".sh", ".ini", ".md", ".nfo")
EXTRACT_TIMEOUT = 900


def digest(path):
    hasher = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def read_text(path):
    data = path.read_bytes()[:INI_LIMIT]
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")


def ini_sections(text):
    return [match.group(1) for match in re.finditer(r"^\s*\[([^\]\r\n]+)\]", text, re.MULTILINE)]


def ini_section(text, name):
    match = re.search(r"^\s*\[" + re.escape(name) + r"\][^\n]*\n(.*?)(?=^\s*\[|\Z)", text,
                      re.MULTILINE | re.DOTALL | re.IGNORECASE)
    return match.group(1).strip() if match else None


def extract(archive, destination):
    errors = []
    for command in (["7z", "x", "-y", "-bd", "-o" + str(destination), str(archive)],
                    ["bsdtar", "-xf", str(archive), "-C", str(destination)]):
        shutil.rmtree(destination, ignore_errors=True)
        destination.mkdir(parents=True)
        try:
            done = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                  timeout=EXTRACT_TIMEOUT)
        except (OSError, subprocess.TimeoutExpired) as error:
            errors.append("%s: %s" % (command[0], error))
            continue
        if done.returncode == 0:
            return command[0], None
        errors.append("%s exit %d: %s" % (command[0], done.returncode,
                                           (done.stderr or done.stdout).strip()[-400:]))
    shutil.rmtree(destination, ignore_errors=True)
    return None, "; ".join(errors)


def describe_tree(root):
    members, patchers, configs, texts = [], [], [], []
    for directory, directories, filenames in os.walk(root):
        directories.sort()
        for name in sorted(filenames):
            path = Path(directory) / name
            if path.is_symlink() or not path.is_file():
                members.append({"path": path.relative_to(root).as_posix(), "kind": "special"})
                continue
            relative = path.relative_to(root).as_posix()
            members.append({"path": relative, "size": path.stat().st_size, "sha256": digest(path)})
            lowered = name.lower()
            if lowered.endswith(".exe"):
                patchers.append({"path": relative, "sha256": members[-1]["sha256"],
                                 "kind": ("tslpatcher" if "tslpatcher" in lowered
                                          else "holopatcher" if "holopatcher" in lowered else "other")})
            if lowered.endswith(TEXT_SUFFIXES) and path.stat().st_size <= TEXT_LIMIT \
                    and lowered not in ("namespaces.ini", "changes.ini"):
                texts.append({"path": relative, "text": read_text(path)})
            if lowered in ("namespaces.ini", "changes.ini"):
                text = read_text(path)
                entry = {"path": relative, "sections": ini_sections(text)}
                if lowered == "namespaces.ini":
                    entry["text"] = text
                else:
                    entry["settings"] = ini_section(text, "Settings")
                configs.append(entry)
    return members, patchers, configs, texts


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--archives", type=Path, required=True)
    parser.add_argument("--archive-map", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    archives = args.archives.resolve(strict=True)
    document = {"schema_version": 1, "archive_map": None, "archives": []}
    if args.archive_map and args.archive_map.is_file():
        document["archive_map"] = json.loads(args.archive_map.read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory(prefix="manual-inventory-") as scratch:
        for path in sorted(archives.rglob("*")):
            if path.is_symlink() or not path.is_file():
                continue
            relative = path.relative_to(archives).as_posix()
            record = {"file": relative, "size": path.stat().st_size, "sha256": digest(path)}
            destination = Path(scratch) / "x"
            tool, error = extract(path, destination)
            if tool:
                record["extracted_with"] = tool
                (record["members"], record["patchers"], record["configs"],
                 record["texts"]) = describe_tree(destination)
            else:
                record["extract_error"] = error
            shutil.rmtree(destination, ignore_errors=True)
            document["archives"].append(record)
            print("inventory: %s %s" % (relative, "ok" if tool else "NOT EXTRACTED"), flush=True)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(document, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    failed = sum("extract_error" in item for item in document["archives"])
    print("inventory: %d archive file(s), %d not extracted" % (len(document["archives"]), failed),
          flush=True)
    return 0 if document["archives"] else 1


if __name__ == "__main__":
    sys.exit(main())
