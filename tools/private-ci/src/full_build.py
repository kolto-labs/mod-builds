#!/usr/bin/env python3
"""Private full-guide lane controller; no game/mod inputs or adapters are bundled.

Mechanical fixtures are not full-build evidence. Manual snapshots require human
attestation. Product adapters must stop at each component until external ack.
Only compare() grades content, against a completed human manual oracle.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import sys
import time

MODES = ("manual", "cli", "gui")
GAMES = ("k1", "k2")
MAX_METADATA = 32 * 1024 * 1024


class Blocked(Exception):
    pass


def require(condition, message):
    if not condition:
        raise Blocked(message)


def digest_bytes(data):
    return hashlib.sha256(data).hexdigest()


def json_bytes(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def file_hash(path):
    path = Path(path)
    require(path.is_file() and not path.is_symlink(), "Required input file missing or unsafe")
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path):
    path = Path(path)
    require(path.is_file() and not path.is_symlink() and path.stat().st_size <= MAX_METADATA,
            "Required receipt missing or unsafe")
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path, value):
    path = Path(path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as output:
        output.write(json_bytes(value))


def safe_name(value):
    require(isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,100}", value), "Unsafe identifier")
    return value


def safe_root(value):
    path = Path(value).expanduser().absolute()
    # System temp roots on macOS commonly contain a platform symlink (/var ->
    # /private/var). Rejecting every parent incorrectly blocks ordinary
    # workspaces; reject the supplied root itself and reparse roots instead.
    require(not path.is_symlink(), "Symlink input root refused")
    if path.exists():
        require(not (getattr(path.lstat(), "st_file_attributes", 0) & 0x400), "Reparse input root refused")
    return path.resolve()


def tree_manifest(root):
    root = safe_root(root)
    require(root.is_dir(), "Baseline or game tree missing")
    entries = []
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            path = Path(directory) / name
            relative = path.relative_to(root).as_posix()
            require(not any(ord(c) < 32 or c in "\\:" for c in relative), "Unsafe tree path")
            info = path.lstat()
            require(not stat.S_ISLNK(info.st_mode) and not (getattr(info, "st_file_attributes", 0) & 0x400),
                    "Tree links and reparse points refused")
            if stat.S_ISDIR(info.st_mode):
                entries.append({"path": relative, "type": "directory"})
            else:
                require(stat.S_ISREG(info.st_mode), "Special tree entry refused")
                entries.append({"path": relative, "type": "file", "size": info.st_size, "sha256": file_hash(path)})
    entries.sort(key=lambda entry: entry["path"].encode("utf-8"))
    return {"schema": 1, "entries": entries}


def manifest_hash(manifest):
    return digest_bytes(json_bytes(manifest))


def pinned_file(path, expected):
    path = safe_root(path)
    require(isinstance(expected, str) and re.fullmatch(r"[0-9a-f]{64}", expected), "Immutable input digest required")
    require(file_hash(path) == expected, "Immutable input digest mismatch")
    return path


def coverage_gate(receipt, config):
    require(isinstance(receipt, dict), "Coverage receipt required")
    require(receipt.get("reviewApproved") is True, "Explicit source/selection review approval required")
    require(receipt.get("sourceGuideSha256") == config["guideSha256"], "Coverage guide binding mismatch")
    require(receipt.get("cacheGenerationSha256") == config["cacheGenerationSha256"], "Coverage cache binding mismatch")
    require(receipt.get("selectedSourceOwners") == config["selectedSourceOwners"], "Selected source-owner coverage mismatch")
    require(receipt.get("instructionsResolved") is True and receipt.get("archiveCoverageComplete") is True,
            "Unresolved instructions or incomplete selected archive coverage")
    require(receipt.get("stepIds") == [step["id"] for step in config["steps"]], "Coverage component order mismatch")


def validate_config(config, game, mode, output):
    require(game in GAMES and mode in MODES, "Invalid full-build lane")
    require(isinstance(config, dict), "Private recipe missing")
    require(config.get("game") == game, "Recipe game mismatch")
    baseline = safe_root(config["baselineDir"])
    require(baseline.is_dir(), "Owned baseline missing")
    require(not output.is_relative_to(baseline) and not baseline.is_relative_to(output), "Disposable lane overlaps baseline")
    for name in ("PUBLIC_CI_WORKSPACE", "GITHUB_WORKSPACE"):
        public = os.environ.get(name)
        if public:
            require(not output.is_relative_to(safe_root(public)), "Game material cannot enter a public checkout")
    require(not any(key in config for key in ("tomlPath", "prebuiltToml", "instructionFilePath")), "Prebuilt TOML configuration refused")
    baseline_manifest = tree_manifest(baseline)
    require(manifest_hash(baseline_manifest) == config["baselineManifestSha256"], "Owned baseline digest mismatch")
    # Recognizable real resources are required; empty fabricated baselines never grade.
    require(all((baseline / name).is_file() and (baseline / name).stat().st_size > 0
                for name in ("chitin.key", "dialog.tlk")), "Owned baseline game resources missing")
    require((baseline / "modules").is_dir() and any((baseline / "modules").iterdir()), "Owned baseline modules missing")
    guide = pinned_file(config["guidePath"], config["guideSha256"])
    require(guide.name == "full.md", "Original full.md input required")
    pinned_file(config["cacheGenerationPath"], config["cacheGenerationSha256"])
    owners = config.get("selectedSourceOwners")
    require(isinstance(owners, list) and owners and len(set(owners)) == len(owners), "Explicit selected source owners required")
    steps = config.get("steps")
    require(isinstance(steps, list) and steps, "Ordered component ledger required")
    identifiers = [safe_name(step["id"]) for step in steps]
    require(len(set(identifiers)) == len(identifiers), "Duplicate component ledger entry")
    covered = [owner for step in steps for owner in step["sourceOwners"]]
    require(covered == owners, "Every selected source owner must occur once in guide order")
    archives = config.get("archives")
    require(isinstance(archives, list) and archives, "Verified private archive generation required")
    archive_ids = [safe_name(item["id"]) for item in archives]
    require(len(set(archive_ids)) == len(archive_ids), "Duplicate archive identity")
    for item in archives:
        pinned_file(item["path"], item["sha256"])
    for step in steps:
        for patcher in step.get("patchers", []):
            require(patcher["archiveId"] in archive_ids, "Included patcher archive provenance missing")
            pinned_file(patcher["path"], patcher["sha256"])
    coverage = pinned_file(config["coverageReceiptPath"], config["coverageReceiptSha256"])
    coverage_gate(read_json(coverage), config)
    if mode == "manual":
        require(isinstance(config.get("manualOperator"), str) and config["manualOperator"].strip(), "Human manual operator required")
    else:
        argv = config.get("generationCmd")
        require(isinstance(argv, list) and all(isinstance(value, str) and value for value in argv), "Real full.md generation adapter missing")
        require(all(any(marker in value for value in argv) for marker in ("{guide}", "{toml}", "{coverage}")), "Generation adapter must bind original guide, scratch TOML and coverage")
        require(not any(".toml" in value.lower() for value in argv), "Literal prebuilt TOML adapter input refused")
        install = config.get("installCmd" if mode == "cli" else "guiCmd")
        require(isinstance(install, list) and all(isinstance(value, str) and value for value in install), "Real product installation adapter missing")
        require(all(any(marker in value for value in install) for marker in ("{toml}", "{game}", "{barriers}")), "Product adapter must bind scratch TOML, game and component barriers")
        require(not any(".toml" in value.lower() for value in install), "Literal prebuilt TOML adapter input refused")
    return baseline_manifest


def substitutions(state):
    root = Path(state["output"])
    return {"guide": state["config"]["guidePath"], "toml": str(root / "scratch.toml"),
            "coverage": str(root / "generated-coverage.json"), "game": str(root / "game"),
            "archives": str(root / "archives"), "staging": str(root / "staging"), "barriers": str(root / "barriers"),
            "output": str(root), "mode": state["mode"]}


def argv_values(argv, state):
    mapping = substitutions(state)
    result = []
    for value in argv:
        for name, replacement in mapping.items():
            value = value.replace("{" + name + "}", replacement)
        require("\0" not in value, "Invalid adapter argv")
        result.append(value)
    return result


def clean_environment():
    allowed = {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "HOME", "USERPROFILE", "TEMP", "TMP", "TMPDIR", "DISPLAY", "XAUTHORITY", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS", "LANG", "LC_ALL", "CARGO_HOME", "RUSTUP_HOME", "DOTNET_ROOT"}
    return {key: value for key, value in os.environ.items() if key.upper() in allowed}


def start_adapter(argv, state, log):
    options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
    return subprocess.Popen(argv_values(argv, state), cwd=state["output"], env=clean_environment(),
                            stdin=subprocess.DEVNULL, stdout=log, stderr=log, **options)


def stop_adapter(process, log):
    if os.name == "nt":
        subprocess.run(["taskkill", "/pid", str(process.pid), "/t", "/f"], stdout=log, stderr=log, check=False)
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if process.poll() is None:
        process.kill()
    process.wait()


def unchanged_inputs(state):
    config = state["config"]
    require(manifest_hash(tree_manifest(config["baselineDir"])) == state["baselineManifestSha256"], "Original owned baseline changed")
    pinned_file(config["guidePath"], config["guideSha256"])
    pinned_file(config["cacheGenerationPath"], config["cacheGenerationSha256"])
    pinned_file(config["coverageReceiptPath"], config["coverageReceiptSha256"])
    for archive in config["archives"]:
        pinned_file(archive["path"], archive["sha256"])
        copied = Path(state["output"]) / "archives" / (archive["id"] + Path(archive["path"]).suffix)
        pinned_file(copied, archive["sha256"])
    for step in config["steps"]:
        for patcher in step.get("patchers", []):
            pinned_file(patcher["path"], patcher["sha256"])
    if state["mode"] != "manual":
        pinned_file(Path(state["output"]) / "scratch.toml", state["scratchTomlSha256"])


def prepare(config, game, mode, output):
    output = safe_root(output)
    require(not output.exists(), "Full-build lane must be fresh and disposable")
    baseline = validate_config(config, game, mode, output)
    output.mkdir(parents=True, mode=0o700)
    for name in ("receipts", "snapshots", "barriers", "archives", "staging"):
        (output / name).mkdir(mode=0o700)
    shutil.copytree(config["baselineDir"], output / "game", copy_function=shutil.copy2, symlinks=False)
    require(tree_manifest(output / "game") == baseline, "Baseline clone content mismatch")
    # Copies, never hardlinks. Inputs stay owner-controlled and read-only to recipes.
    for item in config["archives"]:
        destination = output / "archives" / (item["id"] + Path(item["path"]).suffix)
        shutil.copy2(item["path"], destination)
        require(file_hash(destination) == item["sha256"], "Archive copy digest mismatch")
    state = {"schema": 1, "game": game, "mode": mode, "output": str(output), "config": config,
             "baselineManifestSha256": manifest_hash(baseline)}
    write_json(output / "baseline-manifest.json", baseline)
    if mode != "manual":
        # No authored/prebuilt TOML is admitted: only this fresh generation output.
        require(not (output / "scratch.toml").exists(), "Preexisting scratch guide refused")
        with (output / "generation.log").open("xb") as log:
            process = start_adapter(config["generationCmd"], state, log)
            try:
                code = process.wait(timeout=config.get("timeoutSeconds", 7200))
            except BaseException:
                stop_adapter(process, log)
                raise
        require(code == 0, "Real full.md generation failed")
        toml = output / "scratch.toml"
        require(toml.is_file() and toml.stat().st_size > 0, "Generated scratch TOML missing")
        coverage_gate(read_json(output / "generated-coverage.json"), config)
        state["scratchTomlSha256"] = file_hash(toml)
    unchanged_inputs(state)
    require(tree_manifest(output / "game") == baseline, "Generation adapter modified installation tree")
    write_json(output / "state.json", state)
    return state


def load_state(output):
    output = safe_root(output)
    state = read_json(output / "state.json")
    require(state.get("output") == str(output), "Lane root binding mismatch")
    require(not (output / "complete.json").exists(), "Completed lane is immutable")
    return state


def receipt_gate(state, step, receipt):
    config = state["config"]
    require(isinstance(receipt, dict) and receipt.get("stepId") == step["id"] and receipt.get("performed") is True,
            "Actual ordered component completion receipt required")
    require(receipt.get("execution") == {"manual":"human", "cli":"cli", "gui":"native_gui"}[state["mode"]], "Lane execution evidence mismatch")
    require(receipt.get("sourceGuideSha256") == config["guideSha256"], "Step guide binding mismatch")
    require(receipt.get("archiveHashes") == {item["id"]:item["sha256"] for item in config["archives"]}, "Step archive generation mismatch")
    require(receipt.get("sourceOwners") == step["sourceOwners"], "Step source-owner mismatch")
    if state["mode"] == "manual":
        require(receipt.get("operator") == config["manualOperator"], "Named human operator attestation required")
    if state["mode"] == "gui":
        require(receipt.get("nativeUi") is True, "Native GUI interaction evidence required")
        private_log(state, receipt.get("uiLogPath"), receipt.get("uiLogSha256"))
    patchers = receipt.get("patchers", [])
    require(len(patchers) == len(step.get("patchers", [])), "Included patcher execution provenance required")
    for actual, expected in zip(patchers, step.get("patchers", [])):
        require(actual.get("executableSha256") == expected["sha256"] and actual.get("archiveId") == expected["archiveId"], "Included patcher identity mismatch")
        private_log(state, actual.get("logPath"), actual.get("logSha256"))


def private_log(state, relative, expected):
    require(isinstance(relative, str) and relative and not Path(relative).is_absolute(), "Private execution log required")
    path = safe_root(Path(state["output"]) / relative)
    require(path.is_relative_to(Path(state["output"])) and path.stat().st_size > 0, "Execution log escapes lane or is empty")
    pinned_file(path, expected)


def snapshot(output, step_id, receipt):
    state = load_state(output)
    root = Path(state["output"])
    completed = sorted((root / "receipts").glob("*.json"))
    index = len(completed)
    require(index < len(state["config"]["steps"]), "Unexpected extra component")
    step = state["config"]["steps"][index]
    require(step["id"] == step_id, "Guide order violation; restore and replay required")
    require(not (root / "failed.json").exists(), "Failed lane cannot continue; restore and replay required")
    unchanged_inputs(state)
    receipt_gate(state, step, receipt)
    before = tree_manifest(root / "game")
    write_json(root / "snapshots" / f"{index:04d}.json", before)
    write_json(root / "receipts" / f"{index:04d}.json", receipt)
    write_json(root / "barriers" / f"{index:04d}.ack.json", {"stepId":step_id, "treeManifestSha256":manifest_hash(before)})
    return before


def finalize(output):
    state = load_state(output)
    root = Path(state["output"])
    require(not (root / "failed.json").exists(), "Failed lane cannot finalize")
    receipts = sorted((root / "receipts").glob("*.json"))
    require(len(receipts) == len(state["config"]["steps"]), "Full ordered guide installation is incomplete")
    for index, step in enumerate(state["config"]["steps"]):
        receipt_gate(state, step, read_json(root / "receipts" / f"{index:04d}.json"))
        read_json(root / "snapshots" / f"{index:04d}.json")
    unchanged_inputs(state)
    final = tree_manifest(root / "game")
    require(final == read_json(root / "snapshots" / f"{len(receipts)-1:04d}.json"), "Unobserved game mutations after final component")
    write_json(root / "final-manifest.json", final)
    write_json(root / "complete.json", {"schema":1, "game":state["game"], "mode":state["mode"], "contentCompared":False,
                                       "guideSha256":state["config"]["guideSha256"], "cacheGenerationSha256":state["config"]["cacheGenerationSha256"],
                                       "finalManifestSha256":manifest_hash(final)})
    return final


def first_divergence(left, right):
    left = {entry["path"]:entry for entry in left["entries"]}
    right = {entry["path"]:entry for entry in right["entries"]}
    for path in sorted(set(left) | set(right), key=lambda name:name.encode("utf-8")):
        if left.get(path) != right.get(path):
            return {"path":path, "manual":left.get(path), "candidate":right.get(path)}
    return None


def compare(manual, candidate):
    manual, candidate = safe_root(manual), safe_root(candidate)
    a, b = read_json(manual / "state.json"), read_json(candidate / "state.json")
    require(a["mode"] == "manual" and b["mode"] in ("cli", "gui"), "Only human MANUAL may be the oracle")
    for key in ("game", "baselineManifestSha256"):
        require(a[key] == b[key], "Cross-lane input mismatch")
    for key in ("guideSha256", "cacheGenerationSha256", "selectedSourceOwners", "steps"):
        require(a["config"][key] == b["config"][key], "Cross-lane guide/selection mismatch")
    for root, state in ((manual,a), (candidate,b)):
        unchanged_inputs(state)
        complete = read_json(root / "complete.json")
        final = read_json(root / "final-manifest.json")
        require(complete["finalManifestSha256"] == manifest_hash(final) and final == tree_manifest(root / "game"), "Final lane content changed")
    for index, step in enumerate(a["config"]["steps"]):
        divergence = first_divergence(read_json(manual / "snapshots" / f"{index:04d}.json"), read_json(candidate / "snapshots" / f"{index:04d}.json"))
        if divergence:
            return {"matches":False, "stage":step["id"], "index":index, "divergence":divergence}
    divergence = first_divergence(read_json(manual / "final-manifest.json"), read_json(candidate / "final-manifest.json"))
    return {"matches":divergence is None, "stage":"final", "divergence":divergence}


def run(config, game, mode, output):
    state = prepare(config, game, mode, output)
    require(mode != "manual", "Human manual installation required; lane prepared without executing guide operations")
    root = Path(state["output"])
    with (root / "installation.log").open("xb") as log:
        command = config["installCmd" if mode == "cli" else "guiCmd"]
        process = start_adapter(command, state, log)
        started = time.monotonic()
        index = 0
        try:
            while index < len(config["steps"]):
                require(time.monotonic()-started < config.get("timeoutSeconds",7200), "Product installation adapter timed out")
                ready = root / "barriers" / f"{index:04d}.ready.json"
                if ready.exists():
                    snapshot(root, config["steps"][index]["id"], read_json(ready))
                    index += 1
                elif process.poll() is not None:
                    raise Blocked("Product stopped before completing ordered component barriers")
                else:
                    time.sleep(0.05)
            code = process.wait(timeout=max(1, config.get("timeoutSeconds",7200)-(time.monotonic()-started)))
            require(code == 0, "Product installation failed")
            return finalize(root)
        except BaseException:
            stop_adapter(process, log)
            if not (root / "failed.json").exists():
                write_json(root / "failed.json", {"index":index, "mustRestoreAndReplay":True})
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    for action in ("prepare", "run"):
        entry = sub.add_parser(action)
        entry.add_argument("--config-stdin", action="store_true", required=True)
        entry.add_argument("--game", choices=GAMES, required=True)
        entry.add_argument("--mode", choices=MODES, required=True)
        entry.add_argument("--output", type=Path, required=True)
    entry = sub.add_parser("snapshot")
    entry.add_argument("--output", type=Path, required=True)
    entry.add_argument("--step", required=True)
    entry.add_argument("--receipt-stdin", action="store_true", required=True)
    entry = sub.add_parser("finalize")
    entry.add_argument("--output", type=Path, required=True)
    entry = sub.add_parser("compare")
    entry.add_argument("--manual", type=Path, required=True)
    entry.add_argument("--candidate", type=Path, required=True)
    entry.add_argument("--private-result", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.action in ("prepare","run"):
            config = json.loads(sys.stdin.read(MAX_METADATA+1))
            (prepare if args.action == "prepare" else run)(config,args.game,args.mode,args.output)
        elif args.action == "snapshot":
            snapshot(args.output,args.step,json.loads(sys.stdin.read(MAX_METADATA+1)))
        elif args.action == "finalize":
            finalize(args.output)
        else:
            result = compare(args.manual,args.candidate)
            write_json(args.private_result,result)
            require(result["matches"], "Full-build content differs from manual oracle")
    except Exception:
        print("Full-build lane blocked or failed; inspect private inputs and receipts.")
        return 1
    print("Full-build controller operation completed; private content evidence retained.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
