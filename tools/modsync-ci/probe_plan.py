#!/usr/bin/env python3
"""Probe the reviewed manual-lane plans' patcher operations through manual.py itself.

The full plans (ci/<game>-manual-plan.json) stop at the first failing step, so one CI
run says nothing about later patcher kinds. This tool derives small PROBE plans from a
full plan: each probe run keeps one subject step (plus any prerequisite steps it lists)
exactly as reviewed and turns every other step into a reviewed omission, so manual.py
validates and runs the real operations and reports "partial" (expected for a probe).
archive_sha256 is pruned to the archives the kept steps read. Probe plans are written
only to --out-dir; the ci/ plans are never written.

  probe_plan.py build --game k1 --plan ci/k1-manual-plan.json --out-dir DIR [--steps step-007,...]
      writes DIR/runs.json and DIR/<subject>/plan.json per run
  probe_plan.py prune-archives --runs DIR/runs.json --archives ARCHIVES
      deletes restored archive files no probe run reads (frees runner disk)
  probe_plan.py run --runs DIR/runs.json --guide content/<game>/full.md --pristine GAME0
      --game-dir GAME --archives ARCHIVES --scratch DIR --out OUT
      one manual.py run per probe run, each on a fresh copy of the pristine game at
      --game-dir (the plans' Browse-for-Folder actions navigate to that exact path).
      Collects report.json, manual.py output, step logs, failure screenshots and the
      patchers' own success logs into OUT/<subject>/, writes OUT/summary.md (also to
      $GITHUB_STEP_SUMMARY) and OUT/results.json. Exit 1 only if no selected step was
      attempted at all; individual failures are evidence, not job failures.

Run "run" under an X display (CI: xvfb-run -a -s "-screen 0 1280x1024x24").
"""

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent
PATCHER_TYPES = ("patcher", "gui_patcher")
REVIEWED_BY = "manual-probe workflow (tools/modsync-ci/probe_plan.py); probe plan, not a reference install"

# Default probe selection per game: (subject step, prerequisite steps, why). Together they
# cover every distinct patcher build (sha256) in the plan plus the TSLPatcher settings the
# GUI probes never exercised (notes from the plan expansion: namespace option selection,
# ConfirmMessage != N/A, LookupGameFolder=1, PlaintextLog=1). Patchers only need a vanilla
# game; a mod written for an earlier mod may still differ, the probe proves mechanics.
SELECTIONS = {
    "k1": [
        ("step-109", [], "TSLPatcher 013655cd baseline (no namespaces, default settings)"),
        ("step-131", [], "TSLPatcher 013655cd with LookupGameFolder=1 and a ConfirmMessage"),
        ("step-115", [], "TSLPatcher 1a267454 baseline"),
        ("step-113", [], "TSLPatcher 1a267454 with PlaintextLog=1"),
        ("step-132", [], "TSLPatcher 1a267454 namespace option selection"),
        ("step-177", [], "TSLPatcher 1a267454 with LookupGameFolder=1 and a ConfirmMessage"),
        ("step-147", [], "TSLPatcher 638aaeea baseline"),
        ("step-130", [], "TSLPatcher 638aaeea with LookupGameFolder=1"),
        ("step-048", [], "TSLPatcher 638aaeea namespace option, then a second 1a267454 patcher in one step"),
        ("step-006", [], "HoloPatcher 5cc9fc7a (K1 Community Patch), CLI"),
        ("step-085", [], "HoloPatcher 2a108d0b, CLI without namespace option"),
        ("step-111", [], "HoloPatcher 2a108d0b, CLI --namespace-option-index"),
        ("step-112", [], "HoloPatcher 7a1cc0fb, two namespace options in one step"),
        ("step-143", [], "HoloPatcher a7007ec0, CLI --namespace-option-index"),
    ],
    "k2": [
        ("step-002", [], "TSLRCM Inno Setup silent install (94c99c48)"),
        ("step-133", ["step-002"], "TSLPatcher 69b8aa25 (only use) with PlaintextLog=1 and a namespace option; mod targets TSLRCM"),
        ("step-023", [], "TSLPatcher 013655cd baseline"),
        ("step-081", [], "TSLPatcher 013655cd with a ConfirmMessage"),
        ("step-093", [], "TSLPatcher 013655cd namespace option with LookupGameFolder=1 and a ConfirmMessage"),
        ("step-127", [], "TSLPatcher 1a267454 baseline"),
        ("step-007", [], "TSLPatcher 1a267454 with PlaintextLog=1"),
        ("step-024", [], "TSLPatcher 1a267454 with LookupGameFolder=1 and a ConfirmMessage"),
        ("step-102", [], "TSLPatcher 1a267454 namespace option selection"),
        ("step-011", [], "TSLPatcher 638aaeea baseline"),
        ("step-086", [], "HoloPatcher 2a108d0b, CLI without namespace option"),
        ("step-116", [], "HoloPatcher 2a108d0b, CLI --namespace-option-index"),
        ("step-038", [], "HoloPatcher 7a1cc0fb, CLI without namespace option"),
        ("step-113", [], "HoloPatcher 7a1cc0fb, CLI --namespace-option-index"),
    ],
}


class ProbeError(Exception):
    pass


def require(condition, message):
    if not condition:
        raise ProbeError(message)


def read_json(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")


def patcher_ops(step):
    return [op for op in step.get("operations", []) if op.get("type") in PATCHER_TYPES]


def describe(step):
    """kind/build labels of a step's patcher operations, e.g. ["gui_patcher 013655cd"]."""
    return ["%s %s" % (op["type"], op.get("sha256", "?")[:8]) for op in patcher_ops(step)]


def archive_tokens(plan, kept):
    """archive_sha256 entries read by the kept steps (a directory source binds its files)."""
    sources = []
    for step in plan["steps"]:
        if step["id"] not in kept:
            continue
        for op in step.get("operations", []):
            token = op.get("source", op.get("patcher"))
            if isinstance(token, str) and token.startswith("{archives}/"):
                sources.append(token.rstrip("/"))
    return {token: digest for token, digest in plan["archive_sha256"].items()
            if any(token == source or token.startswith(source + "/") for source in sources)}


def build_probe_plan(plan, kept, subject):
    """Copy of plan keeping the steps in kept verbatim and omitting every other step."""
    ids = [step["id"] for step in plan["steps"]]
    for name in kept:
        require(name in ids, "%s is not a step of the plan" % name)
    steps = []
    for step in plan["steps"]:
        if step["id"] in kept:
            require("omit" not in step, "%s is omitted in the reviewed plan; nothing to probe" % step["id"])
            steps.append(step)
        else:
            steps.append({"id": step["id"], "source_heading": step["source_heading"],
                          "omit": {"reason": "manual probe: only %s runs in this probe plan" % (
                                       ", ".join(name for name in ids if name in kept)),
                                   "reviewed_by": REVIEWED_BY}})
    require(subject in kept and patcher_ops(next(s for s in steps if s["id"] == subject)),
            "%s has no patcher operation" % subject)
    return {"guide_sha256": plan["guide_sha256"], "archive_sha256": archive_tokens(plan, kept),
            "steps": steps, "supplemental_sections": plan["supplemental_sections"]}


def selection(game, steps_arg, plan):
    if steps_arg:
        order = {step["id"]: index for index, step in enumerate(plan["steps"])}
        chosen = []
        for item in steps_arg.split(","):
            item = item.strip()
            if not item:
                continue
            prereqs = item.split("+")
            subject = prereqs.pop()
            require(all(name in order for name in prereqs + [subject]), "unknown step in --steps: " + item)
            chosen.append((subject, prereqs, "selected with --steps"))
        require(chosen, "--steps selects nothing")
        return chosen
    require(game in SELECTIONS, "no default selection for game " + game)
    return SELECTIONS[game]


def inside(path, directory):
    path, directory = path.resolve(), directory.resolve()
    return path == directory or path.is_relative_to(directory)


def command_build(args):
    out = args.out_dir
    forbidden_dirs = [HERE.parent.parent / "ci"]
    if args.plan.resolve().parent.name == "ci":
        forbidden_dirs.append(args.plan.resolve().parent)
    for forbidden in forbidden_dirs:
        require(not inside(out, forbidden), "--out-dir must not be inside %s (reviewed plans)" % forbidden)
    plan = read_json(args.plan)
    runs = []
    for subject, prereqs, why in selection(args.game, args.steps, plan):
        kept = set(prereqs) | {subject}
        probe = build_probe_plan(plan, kept, subject)
        path = out / subject / "plan.json"
        write_json(path, probe)
        by_id = {step["id"]: step for step in plan["steps"]}
        runs.append({"subject": subject, "steps": [s["id"] for s in plan["steps"] if s["id"] in kept],
                     "heading": by_id[subject]["source_heading"], "why": why,
                     "kinds": describe(by_id[subject]), "plan": str(path.resolve()),
                     "timeout_seconds": sum(by_id[name].get("timeout_seconds", 1800) for name in kept),
                     "archives": sorted(probe["archive_sha256"]),
                     "success_logs": [op["success_log"] for name in sorted(kept)
                                      for op in patcher_ops(by_id[name])]})
    write_json(out / "runs.json", {"game": args.game, "source_plan": str(args.plan), "runs": runs})
    for run in runs:
        print("%s %-9s %-40s %s" % (args.game, run["subject"], ", ".join(run["kinds"]), run["why"]))
    return 0


def command_prune(args):
    runs = read_json(args.runs)["runs"]
    needed = {token[len("{archives}/"):] for run in runs for token in run["archives"]}
    root = args.archives.resolve()
    freed = removed = 0
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        if path.relative_to(root).as_posix() not in needed:
            freed += path.stat().st_size
            removed += 1
            path.unlink()
    print("removed %d archive files not read by any probe run (%.1f GiB)" % (removed, freed / 2 ** 30))
    missing = sorted(name for name in needed if not (root / name).is_file())
    for name in missing:
        print("::warning title=probe archive missing::%s" % name)
    return 0


def tail(path, limit=4000):
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    return data[-limit:].decode("utf-8", "backslashreplace")


def collect(work, run, destination):
    """Copy step logs, screenshots and patcher success logs (no game files) to destination."""
    if not work.is_dir():
        return
    for checkpoint in sorted(work.glob("step-*")):
        if not checkpoint.is_dir():
            continue
        for item in checkpoint.iterdir():
            if item.is_file() and (item.name == "operations.log" or item.suffix == ".png"):
                target = destination / checkpoint.name / item.name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(item, target)
    for token in run["success_logs"]:
        if token.startswith("{extract}/"):
            source = work / "extract" / token[len("{extract}/"):]
            if source.is_file():
                target = destination / "patcher-logs" / token[len("{extract}/"):]
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)


def subject_result(run, report):
    """(status, error) for the probe subject from a manual.py report."""
    if report is None:
        return "no report", ""
    error = report.get("error", "")
    receipts = {item["id"]: item for item in report.get("steps", [])}
    for name in run["steps"]:
        receipt = receipts.get(name)
        if receipt is None:
            return ("not reached" if receipts else "blocked"), error
        if receipt["status"] != "complete":
            prefix = "" if name == run["subject"] else "prerequisite %s " % name
            return prefix + receipt["status"], error
    return "complete", error


def attempted(report, run):
    if report is None:
        return False
    return any(item["id"] in run["steps"] and item["status"] != "omitted" for item in report.get("steps", []))


def cell(text):
    return " ".join(str(text).split()).replace("|", "\\|")[:300]


def summary(game, results):
    lines = ["### manual probe (%s)" % game, "",
             "One manual.py run per row on a fresh vanilla copy; every other plan step is a probe omission, "
             "so the report status is `partial` when the probed step completes.", "",
             "| step | heading | kind / build | why | report | step status | seconds | error |",
             "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for item in results:
        lines.append("| %s | %s | %s | %s | %s | %s | %d | %s |" % (
            cell("+".join(item["steps"])), cell(item["heading"]), cell(", ".join(item["kinds"])),
            cell(item["why"]), cell(item["report_status"]), cell(item["status"]), item["seconds"],
            cell(item["error"])))
    done = sum(item["status"] == "complete" for item in results)
    lines += ["", "%d of %d probed steps complete; artifact `manual-probe-%s` has each run's report, "
              "manual.py output, step logs, failure screenshots and patcher logs." % (done, len(results), game)]
    return "\n".join(lines) + "\n"


def command_run(args):
    data = read_json(args.runs)
    game = data["game"]
    require(args.pristine.is_dir(), "pristine game tree missing")
    out = args.out
    out.mkdir(parents=True, exist_ok=True)
    results = []
    python = sys.executable
    for run in data["runs"]:
        subject = run["subject"]
        destination = out / subject
        destination.mkdir(parents=True, exist_ok=True)
        work = args.scratch / subject / "work"
        report_path = args.scratch / subject / "report.json"
        print("::group::probe %s %s (%s)" % (game, subject, ", ".join(run["kinds"])), flush=True)
        started = time.monotonic()
        report = None
        try:
            for stale in (args.game_dir, args.scratch / subject):
                if stale.exists():
                    shutil.rmtree(stale)
            usage = shutil.disk_usage(args.pristine.parent)
            print("free disk before copy: %.1f GiB" % (usage.free / 2 ** 30), flush=True)
            subprocess.run(["cp", "-a", str(args.pristine), str(args.game_dir)], check=True)
            (args.scratch / subject).mkdir(parents=True)
            command = [python, str(HERE / "manual.py"), "--plan", run["plan"], "--guide", str(args.guide),
                       "--game-dir", str(args.game_dir), "--archives", str(args.archives),
                       "--work-dir", str(work), "--report", str(report_path)]
            with (destination / "manual-output.log").open("wb") as output:
                try:
                    process = subprocess.run(command, stdout=output, stderr=subprocess.STDOUT,
                                             timeout=run["timeout_seconds"] + 1800)
                    print("manual.py exit %d" % process.returncode, flush=True)
                except subprocess.TimeoutExpired:
                    print("::warning::manual.py run for %s timed out" % subject, flush=True)
            # The full output is in the artifact; skip the per-omission annotations (one per plan step).
            print("\n".join(line for line in tail(destination / "manual-output.log").splitlines()
                            if not line.startswith("::warning title=manual step omitted::")), flush=True)
            if report_path.is_file():
                shutil.copyfile(report_path, destination / "report.json")
                report = read_json(report_path)
            collect(work, run, destination)
        except Exception as error:  # evidence collection continues with the next run
            print("::warning::probe %s harness error: %s" % (subject, error), flush=True)
        finally:
            if shutil.which("wineserver"):
                subprocess.run(["wineserver", "-k"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               check=False)
            for leftover in (args.scratch / subject, args.game_dir):
                shutil.rmtree(leftover, ignore_errors=True)
            print("::endgroup::", flush=True)
        status, error = subject_result(run, report)
        results.append({"subject": subject, "steps": run["steps"], "heading": run["heading"],
                        "kinds": run["kinds"], "why": run["why"],
                        "report_status": report.get("status", "?") if report else "none",
                        "status": status, "error": error, "attempted": attempted(report, run),
                        "seconds": int(time.monotonic() - started)})
        if status != "complete":
            print("::warning title=probe %s %s::%s %s" % (game, subject, status, error[:500]), flush=True)
    write_json(out / "results.json", {"game": game, "results": results})
    text = summary(game, results)
    (out / "summary.md").write_text(text, encoding="utf-8")
    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        with open(step_summary, "a", encoding="utf-8") as handle:
            handle.write(text)
    print(text)
    if not any(item["attempted"] for item in results):
        print("::error title=manual probe ran nothing::no selected %s step reached execution" % game)
        return 1
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build")
    build.add_argument("--game", required=True)
    build.add_argument("--plan", required=True, type=Path)
    build.add_argument("--out-dir", required=True, type=Path)
    build.add_argument("--steps", default="",
                       help="comma-separated subjects instead of the default selection; "
                            "prerequisites as PRE+SUBJECT (e.g. step-002+step-133)")
    prune = commands.add_parser("prune-archives")
    prune.add_argument("--runs", required=True, type=Path)
    prune.add_argument("--archives", required=True, type=Path)
    run = commands.add_parser("run")
    for name in ("runs", "guide", "pristine", "game-dir", "archives", "scratch", "out"):
        run.add_argument("--" + name, required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        return {"build": command_build, "prune-archives": command_prune, "run": command_run}[args.command](args)
    except ProbeError as error:
        print("probe_plan: " + str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
