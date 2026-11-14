#!/usr/bin/env python3
"""Probe how the mods' own patchers behave under Wine, as evidence for the manual plan.

For a few archives that ship TSLPatcher.exe or HoloPatcher.exe (from manual_inventory.py
output) it extracts the archive into scratch, starts the patcher under Wine on the
current X display (run under xvfb-run) against NO game directory, and records:
window titles and geometry (xdotool), a screenshot after it settles and after each
probe key, and for HoloPatcher the output of `--help`. Every process is killed
afterwards. Nothing is installed; the output only shows what the GUI asks for.

  manual_probe.py --inventory FILE --archives DIR --out DIR [--count N]
"""

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

import manual_inventory

SETTLE_SECONDS = 20


def run(command, **options):
    return subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                          timeout=options.pop("timeout", 60), **options)


def windows():
    found = run(["xdotool", "search", "--onlyvisible", "--name", "."])
    result = []
    for identifier in found.stdout.split():
        name = run(["xdotool", "getwindowname", identifier]).stdout.strip()
        geometry = run(["xdotool", "getwindowgeometry", "--shell", identifier]).stdout.split()
        result.append({"id": identifier, "name": name, "geometry": geometry})
    return result


def screenshot(path):
    run(["import", "-window", "root", str(path)])
    return path.name if path.is_file() else None


def kill_wine():
    run(["wineserver", "-k"], timeout=30)
    time.sleep(2)


def probe(record, archives, out, index, scratch):
    destination = Path(scratch) / ("p%d" % index)
    tool, error = manual_inventory.extract(archives / record["file"], destination)
    if not tool:
        return {"file": record["file"], "error": error}
    patcher = next(item for item in record["patchers"] if item["kind"] in ("tslpatcher", "holopatcher"))
    exe = destination / patcher["path"]
    result = {"file": record["file"], "patcher": patcher, "steps": []}
    if patcher["kind"] == "holopatcher":
        try:
            done = run(["wine", str(exe), "--help"], cwd=exe.parent, timeout=180)
            result["help"] = {"exit": done.returncode, "stdout": done.stdout[-6000:],
                              "stderr": done.stderr[-3000:]}
        except subprocess.TimeoutExpired:
            result["help"] = {"timeout": True}
        kill_wine()
    process = subprocess.Popen(["wine", str(exe)], cwd=exe.parent, stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(SETTLE_SECONDS)
        for label, keys in (("start", None), ("tab", ["Tab"]), ("return", ["Return"])):
            if keys:
                run(["xdotool", "key", "--delay", "200"] + keys)
                time.sleep(8)
            name = "%02d-%s.png" % (index, label)
            result["steps"].append({"after": label, "windows": windows(),
                                    "screenshot": screenshot(out / name)})
    finally:
        process.kill()
        kill_wine()
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--archives", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--count", type=int, default=3)
    args = parser.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)
    inventory = json.loads(args.inventory.read_text(encoding="utf-8"))
    chosen = []
    for kind in ("tslpatcher", "holopatcher"):
        chosen += [record for record in inventory["archives"]
                   if any(item["kind"] == kind for item in record.get("patchers", []))][:args.count]
    results = []
    with tempfile.TemporaryDirectory(prefix="manual-probe-") as scratch:
        for index, record in enumerate(chosen):
            print("probe: %s" % record["file"], flush=True)
            try:
                results.append(probe(record, args.archives.resolve(), args.out, index, scratch))
            except Exception as error:  # evidence gathering: record and keep going
                results.append({"file": record["file"], "error": "%s: %s" % (type(error).__name__, error)})
    (args.out / "probe.json").write_text(json.dumps(results, indent=1) + "\n", encoding="utf-8")
    return 0 if results else 1


if __name__ == "__main__":
    sys.exit(main())
