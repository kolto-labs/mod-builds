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
        raise Blocked(stage + "_failed")


def coverage(guide, converted, report=None):
    text = guide.read_text(encoding="utf-8")
    allowed_sections = {
        "Mod List",
        "Optional Widescreen & Extended Widescreen Support Mods",
        "Enhanced Widescreen Support Mods",
    }
    section = None
    headings = []
    sections = {}
    for line in text.splitlines():
        if line.startswith("# "):
            section = None
        elif line.startswith("## "):
            section = line[3:].strip()
            if section in allowed_sections:
                sections.setdefault(section, 0)
        elif line.startswith("### ") and section in allowed_sections:
            headings.append(line[4:].strip())
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
    if any(not isinstance(component, dict) or not component.get("Instructions")
           for component in components):
        raise Blocked("guide_steps_without_instructions")
    if any(component.get("Heading", component.get("Name")) != heading
           for component, heading in zip(components, headings)):
        raise Blocked("guide_component_order_or_heading_mismatch")
    return len(components)


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
        run(command + ["convert", str(guide), str(converted),
                       "--format", "toml", "--parse-directions"],
            source, private, "convert", report, env)
    else:
        if not (source / "packages/cli/src/main.ts").is_file():
            raise Blocked("typescript_source_entrypoint_missing")
        run(["pnpm", "build"], source, private, "build", report, env)
        command = ["node", str(source / "packages/cli/dist/main.js")]
        if guide.parent.name not in ("k1", "k2"):
            raise Blocked("guide_target_game_unknown")
        target_game = {"k1": "kotor1", "k2": "kotor2"}[guide.parent.name]
        run(command + ["import-md", str(guide), "--output", str(converted),
                       "--game", target_game],
            source, private, "convert", report, env)
    if digest(guide) != report["guide_sha256"]:
        raise Blocked("guide_changed_during_conversion")
    report["converted_sha256"] = digest(converted)
    report["stage"] = "coverage"
    report["component_count"] = coverage(guide, converted, report)
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
    run(command + ["validate"] + directories,
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
             "--report", str(driver_report), "--timeout", "7000"],
            source, private, "gui", report, env)
        if not driver_report.is_file() or driver_report.is_symlink():
            raise Blocked("gui_completion_report_missing")
        result = json.loads(driver_report.read_text(encoding="utf-8"))
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
                or result.get("completed_components") != report["component_count"]
                or not isinstance(result.get("verified_game_files"), int)
                or result["verified_game_files"] <= 0
                or not isinstance(result.get("receipt_sha256"), str)
                or re.fullmatch(r"[0-9a-f]{64}", result["receipt_sha256"]) is None):
            raise Blocked("gui_completion_not_verified")
        if (digest(guide) != report["guide_sha256"]
                or digest(converted) != report["converted_sha256"]):
            raise Blocked("reviewed_inputs_changed")
        report["gui"] = result
        report["proof"] = "native_gui_and_verified_completion_receipt"
        report["status"] = "installed"
        report["reason"] = result["reason"]
        return
    run(command + ["install"] + directories + ["--non-interactive", "--phase", "all"],
        source, private, "install", report, env)
    report["status"] = "installed"
    report["reason"] = "native_install_command_succeeded"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--implementation", choices=("rust", "typescript"), required=True)
    parser.add_argument("--mode", choices=("cli", "gui"), required=True)
    parser.add_argument("--review-manifest", type=Path,
                        help="separately authored hash-bound JSON review (TypeScript required)")
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
        return 0 if report["status"] == "installed" else 1
    except OSError:
        print("ports: unable to write sanitized report", file=sys.stderr)
        return 2
    finally:
        os.umask(previous_umask)


if __name__ == "__main__":
    sys.exit(main())
