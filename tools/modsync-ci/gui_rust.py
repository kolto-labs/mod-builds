#!/usr/bin/env python3
"""Install a supplied converted TOML through the production Rust egui desktop.

Requires Python 3.11+, xvfb-run, xauth, tesseract, pyautogui, pytesseract and
Pillow. Evidence is private under --work-dir; --report contains no OCR or mod
names. The supplied game/workspace are real writable install targets. Failures
leave application recovery state intact; this driver never retries an install.

Subset installs: --deselect GUID (repeatable, reviewed policy skips) and
--exclude-widescreen install a defined subset of the converted TOML. The
driver never edits the supplied file: it writes IsSelected into a private copy
(the GUI honors authored IsSelected when any component is selected, via
Guide::load_for_install) and skips "Select all" when that would re-select a
deselected base component. Deselected components are reported, never covered.
"""

import argparse
import faulthandler
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import tomllib

import gui_diag


WORKER_LOGS = (("worker", "driver.log", gui_diag.TAIL_LINES),)


class Blocked(Exception):
    pass


def digest(path):
    checksum = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            checksum.update(block)
    return checksum.hexdigest()


def save(path, value):
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def table_key(document):
    return "thisMod" if "thisMod" in document else "Components"


def review_required(value):
    if isinstance(value, dict):
        if "DraftReview" in value:
            review = value["DraftReview"]
            if not isinstance(review, dict) or review.get("RequiresReview") is not False:
                return True
        return any(review_required(child) for child in value.values())
    return isinstance(value, list) and any(review_required(child) for child in value)


def plan_selection(components, deselect=(), exclude_widescreen=False):
    """Decide exactly which components the GUI will install, or refuse.

    Base components not deselected are selected (what "Select all" would do).
    Reviewed-policy GUIDs are always deselected. Widescreen-only components are
    never installed: they are deselected with exclude_widescreen, otherwise any
    that the input selects makes the plan refuse. Draft review is checked on
    the components that remain selected only; the driver never authorizes it.
    """
    identities = [str(component.get("Guid", "")).lower() for component in components]
    requested = [str(value).lower() for value in deselect]
    if len(set(requested)) != len(requested):
        raise Blocked("deselect_guid_duplicate")
    if not set(requested) <= set(identities):
        raise Blocked("deselect_guid_not_in_input")
    policy = set(requested)
    widescreen = {identity for identity, component in zip(identities, components)
                  if component.get("WidescreenOnly") is True}
    not_installed = widescreen - policy
    selection = []
    for identity, component in zip(identities, components):
        if identity in policy:
            selection.append(False)
        elif identity in widescreen:
            selection.append(component.get("IsSelected") is True and not exclude_widescreen)
        else:
            selection.append(True)
    if review_required([component for component, keep in zip(components, selection) if keep]):
        raise Blocked("draft_review_receipts_require_human_source_review_driver_cannot_authorize")
    if any(keep and identity in widescreen for identity, keep in zip(identities, selection)):
        raise Blocked("full_input_contains_separate_widescreen_phase_driver_supports_single_phase_only")
    deselected = {identity for identity, keep in zip(identities, selection) if not keep}
    for component, keep in zip(components, selection):
        if keep and deselected & {str(each).lower() for each in component.get("Dependencies", [])}:
            raise Blocked("selected_component_depends_on_deselected_component")
    if not any(selection):
        raise Blocked("no_component_left_selected")
    base = [identity not in widescreen for identity in identities]
    return {
        "selection": selection,
        "expected_components": [identity for identity, keep in zip(identities, selection) if keep],
        "skipped_by_reviewed_policy": sorted(policy),
        "excluded_widescreen": sorted(not_installed),
        "widescreen_phase": "not_covered_by_gui_lane" if widescreen else "none_in_input",
        "base_total": sum(base),
        "base_selected": sum(keep and is_base for keep, is_base in zip(selection, base)),
        "subset": bool(policy or not_installed),
        "click_select_all": not any(identity in policy for identity, is_base
                                    in zip(identities, base) if is_base),
    }


def rewrite_selection(text, selection):
    """Return text with IsSelected written per top-level component, changing nothing else."""
    document = tomllib.loads(text)
    key = table_key(document)
    header = "[[" + key + "]]"
    index = -1
    inside = False
    lines = []
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if stripped.startswith("["):
            inside = stripped == header
            lines.append(line)
            if inside:
                index += 1
                if index >= len(selection):
                    raise Blocked("selection_rewrite_incomplete")
                lines.append("IsSelected = " + ("true" if selection[index] else "false") + "\n")
            continue
        if inside and re.fullmatch(r"IsSelected\s*=\s*(true|false)", stripped):
            continue
        lines.append(line)
    rewritten = "".join(lines)
    updated = tomllib.loads(rewritten)
    if index + 1 != len(selection) or len(updated[key]) != len(selection):
        raise Blocked("selection_rewrite_incomplete")
    if [component.get("IsSelected") for component in updated[key]] != selection:
        raise Blocked("selection_rewrite_mismatch")
    for part in (document, updated):
        for component in part[key]:
            component.pop("IsSelected", None)
    if updated != document:
        raise Blocked("selection_rewrite_changed_other_fields")
    return rewritten


def stop(process):
    if process is None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
    except ProcessLookupError:
        process.wait()
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()


def drive(args, private, result):
    for module in ("pyautogui", "pytesseract", "PIL"):
        if importlib.util.find_spec(module) is None:
            raise Blocked("missing_python_dependency_" + module)
    import pyautogui
    import pytesseract
    from PIL import ImageGrab

    plan = json.loads((private / "selection-plan.json").read_text(encoding="utf-8"))
    driven_input = private / "input.toml"
    deadline = time.monotonic() + args.timeout
    process = None
    events = []
    pyautogui.FAILSAFE = True
    pyautogui.PAUSE = 0.15
    progress = {"phase": "preflight"}

    def mark(phase=None, stage=None):
        progress["phase"] = phase or progress["phase"]
        gui_diag.mark_phase(private, progress["phase"], stage or result.get("stage"))

    def check_time():
        if time.monotonic() >= deadline:
            raise Blocked("gui_timeout")
        if process is not None and process.poll() is not None:
            raise Blocked("desktop_exited_before_completion")

    def observe():
        check_time()
        shot = ImageGrab.grab(xdisplay=os.environ["DISPLAY"])
        shot.save(private / "latest.png")
        data = pytesseract.image_to_data(
            shot.resize((shot.width * 2, shot.height * 2)),
            config="--psm 11", output_type=pytesseract.Output.DICT,
            timeout=max(0.1, min(20, deadline - time.monotonic())),
        )
        groups = {}
        for index, word in enumerate(data["text"]):
            if word.strip():
                key = tuple(data[field][index] for field in
                            ("block_num", "par_num", "line_num"))
                groups.setdefault(key, []).append(index)
        lines = [(" ".join(data["text"][index] for index in indices), indices)
                 for indices in groups.values()]
        text = "\n".join(line for line, _ in lines)
        (private / "latest-ocr.txt").write_text(text, encoding="utf-8")
        if progress["phase"] == "first_screenshot":
            mark("install")
        if re.search(
            r"validation (?:failed|stopped|did not complete)|installation (?:failed|stopped|review blocked)|"
            r"review remains open|requires? review|unreviewed|inputs changed|"
            r"result discarded|not applied|recovery required|\bERROR\s*:",
            text, re.IGNORECASE,
        ):
            raise Blocked("visible_review_validation_or_install_error")
        return shot, data, lines

    def locate(label, optional=False):
        limit = min(deadline, time.monotonic() + (4 if optional else args.timeout))
        pattern = r"\s*".join(re.escape(token) for token in
                                re.findall(r"\w+|[^\w\s]", label))
        while time.monotonic() < limit:
            shot, data, lines = observe()
            matches = []
            for text, indices in lines:
                match = re.search(pattern, text, re.IGNORECASE)
                if not match:
                    continue
                cursor = 0
                matching = []
                for index in indices:
                    end = cursor + len(data["text"][index])
                    if cursor < match.end() and end > match.start():
                        matching.append(index)
                    cursor = end + 1
                left = min(data["left"][index] for index in matching)
                top = min(data["top"][index] for index in matching)
                right = max(data["left"][index] + data["width"][index] for index in matching)
                bottom = max(data["top"][index] + data["height"][index] for index in matching)
                width, height = pyautogui.size()
                matches.append((len(text), (left + right) / 4 * width / shot.width,
                                (top + bottom) / 4 * height / shot.height))
            if matches:
                _, horizontal, vertical = min(matches)
                shot.save(private / f"{len(events):02d}-observed.png")
                return horizontal, vertical
            time.sleep(0.5)
        if optional:
            return None
        raise Blocked("visible_control_missing")

    def click(label, optional=False):
        result["stage"] = label
        mark()
        point = locate(label, optional)
        if point is None:
            return
        pyautogui.moveTo(*point)
        pyautogui.mouseDown()
        time.sleep(0.15)
        pyautogui.mouseUp()
        pyautogui.moveTo(5, 5)
        events.append({"control": label, "coordinates": point})
        save(private / "clicks.json", events)
        time.sleep(0.7)

    command = [str(args.binary), "gui", "--allow-multiple-instances",
               "--input", str(driven_input), "--game-dir", str(args.game_dir),
               "--source-dir", str(args.archives),
               "--settings-dir", str(private / "settings")]
    save(private / "command.json", command)
    environment = os.environ.copy()
    for name in ("HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_RUNTIME_DIR"):
        directory = private / name.lower()
        directory.mkdir(mode=0o700)
        environment[name] = str(directory)
    environment.pop("WAYLAND_DISPLAY", None)
    environment.pop("DBUS_SESSION_BUS_ADDRESS", None)
    environment["WINIT_UNIX_BACKEND"] = "x11"
    environment["LIBGL_ALWAYS_SOFTWARE"] = "1"
    try:
        with (private / "desktop.log").open("wb") as log:
            process = subprocess.Popen(command, cwd=private, env=environment,
                                       stdout=log, stderr=subprocess.STDOUT)
            mark("first_screenshot")
            click("Keep telemetry off", optional=True)
            click("Summary / selection")
            click("Pause sharing to edit", optional=True)
            if plan["click_select_all"]:
                click("Select all")
            if plan["subset"]:
                result["stage"] = "selection_summary"
                mark()
                label = "Base build: %d of %d mods selected" % (plan["base_selected"], plan["base_total"])
                if locate(label, optional=True) is None:
                    raise Blocked("selection_summary_does_not_match_expected_subset")
            click("Validation / install")
            click("Run validation")
            result["stage"] = "validation"
            mark()
            locate("Validation passed")
            click("Install selected mods")
            locate("Ready to Install")
            click("Next")
            click("Create snapshot and install")
            result["stage"] = "installation"
            mark()
            locate("Installation complete")
            result["ui_completed"] = True
            mark(stage="receipt")
            verify_receipt(args, result, check_time, plan["expected_components"])
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def verify_receipt(args, result, check_time, expected_ids):
    result["stage"] = "receipt"
    key = hashlib.sha256(os.fsencode(str(args.game_dir))).hexdigest()[:16]
    state = args.game_dir.parent / (".modsync-" + key)
    path = state / "completion.json"
    if (state / "pending").exists() or not path.is_file() or path.is_symlink():
        raise Blocked("completion_receipt_missing_or_recovery_pending")
    receipt = json.loads(path.read_text(encoding="utf-8"))
    if receipt.get("game") != str(args.game_dir) or receipt.get("mods") != str(args.archives):
        raise Blocked("completion_roots_mismatch")
    actual = receipt.get("components", [])
    actual_ids = [str(component["Guid"]).lower() for component in actual]
    expected_ids = [str(identity).lower() for identity in expected_ids]
    if len(actual_ids) != len(expected_ids) or set(actual_ids) != set(expected_ids):
        raise Blocked("completion_does_not_cover_full_input")
    files = receipt.get("files")
    if not isinstance(files, dict) or not files:
        raise Blocked("completion_manifest_empty")
    for relative, checksum in files.items():
        check_time()
        target = args.game_dir / relative
        if (not target.resolve().is_relative_to(args.game_dir)
                or target.is_symlink() or not target.is_file() or digest(target) != checksum):
            raise Blocked("completion_game_manifest_mismatch")
    observed = set()
    for directory, directories, filenames in os.walk(args.game_dir, followlinks=False):
        check_time()
        for name in directories + filenames:
            target = Path(directory) / name
            if target.is_symlink():
                raise Blocked("completion_tree_contains_symlink")
        for name in filenames:
            observed.add(str((Path(directory) / name).relative_to(args.game_dir)))
    if observed != set(files):
        raise Blocked("completion_manifest_file_set_mismatch")
    result["receipt_sha256"] = digest(path)
    result["completed_components"] = len(actual)
    result["verified_game_files"] = len(files)
    result["status"] = "installed"
    result["reason"] = "native_gui_completed_and_receipt_verified"


def execute(args, result):
    for name in ("binary", "input", "game_dir", "archives"):
        setattr(args, name, getattr(args, name).resolve(strict=True))
    if not args.binary.is_file() or not os.access(args.binary, os.X_OK):
        raise Blocked("executable_required")
    if args.input.suffix.lower() != ".toml":
        raise Blocked("converted_full_toml_required")
    input_text = args.input.read_text(encoding="utf-8")
    document = tomllib.loads(input_text)
    components = document.get(table_key(document), [])
    if not isinstance(components, list) or not components:
        raise Blocked("converted_components_missing")
    identities = [component.get("Guid") for component in components]
    if any(not identity for identity in identities) or len(set(identities)) != len(identities):
        raise Blocked("component_identities_missing_or_duplicate")
    plan = plan_selection(components, args.deselect, args.exclude_widescreen)
    if not args.game_dir.is_dir() or not args.archives.is_dir():
        raise Blocked("real_game_and_archive_directories_required")
    args.work_dir = args.work_dir.resolve()
    roots = (args.game_dir, args.archives, args.input, args.binary)
    if any(args.work_dir.is_relative_to(root) or root.is_relative_to(args.work_dir) for root in roots):
        raise Blocked("work_directory_overlaps_inputs")
    if args.game_dir.is_relative_to(args.archives) or args.archives.is_relative_to(args.game_dir):
        raise Blocked("game_and_archive_directories_overlap")
    key = hashlib.sha256(os.fsencode(str(args.game_dir))).hexdigest()[:16]
    state = args.game_dir.parent / (".modsync-" + key)
    if (state / "completion.json").exists() or (state / "pending").exists():
        raise Blocked("existing_completion_or_pending_recovery_requires_operator_review")
    for executable in ("xvfb-run", "Xvfb", "xauth", "tesseract"):
        if shutil.which(executable) is None:
            raise Blocked("missing_dependency_" + executable)
    args.work_dir.mkdir(parents=True, exist_ok=True)
    private = Path(tempfile.mkdtemp(prefix="gui-rust-", dir=args.work_dir))
    private.chmod(0o700)
    result["evidence_directory"] = str(private)
    result["input_sha256"] = digest(args.input)
    result["binary_sha256"] = digest(args.binary)
    result["component_count"] = len(components)
    result["coverage"] = "subset" if plan["subset"] else "full"
    result["expected_components"] = len(plan["expected_components"])
    for name in ("skipped_by_reviewed_policy", "excluded_widescreen", "widescreen_phase"):
        result[name] = plan[name]
    (private / "input.toml").write_text(
        rewrite_selection(input_text, plan["selection"]) if plan["subset"] else input_text,
        encoding="utf-8")
    result["driven_input_sha256"] = digest(private / "input.toml")
    save(private / "selection-plan.json", plan)
    command = ["xvfb-run", "--auto-servernum", "--server-args=-screen 0 1600x1200x24 -nolisten tcp",
               sys.executable, str(Path(__file__).resolve()),
               *sys.argv[1:], "--private-worker", str(private)]
    process = None
    code = None
    timed_out = False
    failure = None
    try:
        with (private / "driver.log").open("wb") as log:
            process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT,
                                       start_new_session=True)
            try:
                code = process.wait(timeout=args.timeout + 15)
            except subprocess.TimeoutExpired:
                timed_out = True
                raise Blocked("isolated_desktop_timeout") from None
        worker = private / "result.json"
        if not worker.is_file():
            raise Blocked("isolated_worker_failed_before_report")
        result.update(json.loads(worker.read_text(encoding="utf-8")))
        if code or result.get("status") != "installed":
            raise Blocked(result.get("reason", "isolated_worker_failed"))
        if result.get("completed_components") != len(plan["expected_components"]):
            raise Blocked("completion_does_not_cover_expected_subset")
        if digest(args.input) != result["input_sha256"]:
            raise Blocked("input_changed_during_install")
    except BaseException as error:
        failure = error
        raise
    finally:
        stop(process)
        if failure is not None:
            result["worker_diagnostics"] = gui_diag.collect(
                private, WORKER_LOGS, code if code is not None or process is None else process.returncode,
                timed_out, (private / "result.json").is_file(), failure,
                paths={private: "$EVIDENCE", args.work_dir: "$WORK_DIR"})
            if not (private / "result.json").is_file():
                result["stage"] = (result["worker_diagnostics"]["stage"]
                                   or result["worker_diagnostics"]["phase"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("binary", "input", "game-dir", "archives", "work-dir", "report"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--deselect", action="append", default=[], metavar="GUID",
                        help="reviewed policy skip: written IsSelected=false into a private copy and "
                             "reported as skipped_by_reviewed_policy, never as covered (repeatable)")
    parser.add_argument("--exclude-widescreen", action="store_true",
                        help="deselect widescreen-only components in the private copy and report them "
                             "as not covered; without it a selected widescreen component is refused")
    parser.add_argument("--timeout", type=float, required=True, help="Total GUI time budget in seconds")
    parser.add_argument("--private-worker", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    os.umask(0o077)
    result = {"schema_version": 1, "mode": "gui", "implementation": "rust",
              "status": "blocked", "stage": "preflight", "ui_completed": False,
              "source_contract": ["src/ui/validation_page.rs", "src/ui.rs",
                                  "src/ui/installing_page.rs", "src/recovery.rs",
                                  "src/checkpoint/finalize.rs"],
              "limitations": ["Widescreen-only components are never installed: refused when selected, "
                              "otherwise reported in excluded_widescreen as not covered",
                              "Draft review cannot be authorized by this driver",
                              "Full guide coverage must be established by the caller",
                              "A failed install requires application recovery before retry"]}
    destination = args.report.absolute()
    if args.private_worker:
        destination = args.private_worker / "result.json"
    elif destination.exists() or destination.is_symlink() or any(
        destination.resolve().is_relative_to(path.resolve()) for path in
        (args.game_dir, args.archives, args.input, args.binary)
    ):
        print("gui_rust: report must be new and outside inputs", file=sys.stderr)
        return 2
    try:
        if not 0 < args.timeout < 604800:
            raise Blocked("timeout_must_be_positive_and_under_one_week")
        if args.private_worker:
            faulthandler.enable()
            gui_diag.mark_phase(args.private_worker, "preflight")
            for name in ("binary", "input", "game_dir", "archives"):
                setattr(args, name, getattr(args, name).resolve(strict=True))
            drive(args, args.private_worker, result)
        else:
            execute(args, result)
    except (Exception, KeyboardInterrupt) as error:
        result["status"] = "blocked"
        result["reason"] = str(error) if isinstance(error, Blocked) else type(error).__name__
        if args.private_worker:
            import traceback
            trace = traceback.format_exc()
            (args.private_worker / "failure.txt").write_text(trace, encoding="utf-8")
            print(trace, file=sys.stderr, flush=True)
    destination.parent.mkdir(parents=True, exist_ok=True)
    save(destination, result)
    if not args.private_worker:
        diagnostics = result.get("worker_diagnostics")
        if diagnostics:
            print("\n".join(gui_diag.render_group(diagnostics)), flush=True)
        print(json.dumps({key: value for key, value in result.items() if key != "worker_diagnostics"}
                         if diagnostics else result))
    return 0 if result["status"] == "installed" else 1


if __name__ == "__main__":
    sys.exit(main())
