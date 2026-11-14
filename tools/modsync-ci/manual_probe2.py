#!/usr/bin/env python3
"""Install a few mods with their ORIGINAL patchers under Wine/Xvfb and record every screen.

Evidence for authoring manual-plan gui_patcher/patcher operations (manual.py). Each case
runs against a fresh scratch COPY of --game-dir (deleted afterwards; --game-dir itself is
never written) and records window names/geometry, a root screenshot after every action
and whenever the set of windows changes, the patcher's own logs (full text), stdout and
the list of game files it added or changed. TSLPatcher cases drive the GUI through
manual.py's own run_gui_action, so a case that succeeds proves the exact action list.

Sections (--only, comma separated; default all for the game):
  extract  try every extractor on the RAR archives the inventory could not open
  tsl      TSLPatcher GUI installs (one mod without namespaces.ini, one with, option 1)
  holo     HoloPatcher v1.5.2 CLI installs (with and without --console)
  other    other installer executables (other HoloPatcher builds, URCMTP, hires_patcher)
  tslrcm   K2 only: identify and run the TSLRCM installer (tslrcm2022.exe)

  manual_probe2.py --game k1|k2 --game-dir DIR --archives DIR --scratch DIR --out DIR [--only S]
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
import manual  # noqa: E402  (the runner the plan will use; its actions are exercised as is)

SECTIONS = ("extract", "tsl", "holo", "other", "tslrcm")

TSL_FAILURE = [r"(?i)\berror\b", r"(?i)\bwarning\b"]

# Per-game cases. "archive" is a file name or glob under --archives; "exe" is relative to
# the extraction root. "title" is the main window title regex (the WindowCaption).
CASES = {
    "k1": {
        "tsl": [
            {"name": "tsl-plain", "archive": "Ajunta Pall Unique Appearance.zip", "exe": "TSLPatcher.exe",
             "option": None, "title": r"^\[K1\] Combination of Silveredge9"},
            {"name": "tsl-ns1", "archive": "Ajunta__39_s Swords.7z", "exe": "TSLPatcher.exe",
             "option": 1, "title": r"^Ajunta Pall's Swords"},
        ],
        "holo": [
            {"name": "holo", "archive": "KYR1.1.7z", "exe": "Kebla Yurt Revamp/HoloPatcher.exe",
             "variants": [["--namespace-option-index", "0", "--install"],
                          ["--namespace-option-index", "0", "--install", "--console"],
                          ["--install"]]},
        ],
        "other": [
            {"name": "holo-7a1cc0fb", "archive": "K1 Twi_lek Male NPC Diversity.7z",
             "exe": "KotOR 1 Twi'lek Male NPC Diversity/Install.exe",
             "variants": [["--namespace-option-index", "0", "--install"]]},
            {"name": "holo-a7007ec0", "archive": "Sentinel Sneak Attack-*.7z", "exe": "installer.exe",
             "variants": [["--namespace-option-index", "0", "--install"]]},
            {"name": "holo-5cc9fc7a", "archive": "K1_Community_Patch_v1.10.0.zip", "exe": "INSTALL.exe",
             "variants": [["--install"]]},
            {"name": "hires", "archive": "k1hrm-1.5.7z", "exe": "hires_patcher.exe", "kind": "inspect",
             "show": ["hires_patcher.bat", "hires_patcher.pl", "README.pdf"]},
        ],
        "extract": [],
    },
    "k2": {
        "tsl": [
            {"name": "tsl-plain", "archive": "Droid special weapons fix for TSL v2.0.7z",
             "exe": "Droid special weapons fix for TSL v2.0/TSLPatcher.exe", "option": None,
             "title": r"^Game Data Patcher for KotOR/TSL"},
            {"name": "tsl-ns1", "archive": "Alignment_Affects_Force_Powers_TSL_v101.zip",
             "exe": "Alignment_Affects_Force_Powers_TSL_v101/TSLPatcher.exe", "option": 1,
             "title": r"^Game Data Patcher for KotOR/TSL"},
        ],
        "holo": [
            {"name": "holo", "archive": "For_Mandalore_v1_2_1.zip", "exe": "For_Mandalore_v1_2_1/HoloPatcher.exe",
             "variants": [["--namespace-option-index", "0", "--install"],
                          ["--namespace-option-index", "0", "--install", "--console"]]},
        ],
        "other": [
            {"name": "holo-7a1cc0fb", "archive": "Dahnis Unique Look.7z", "exe": "Dahnis Unique Look/Install.exe",
             "variants": [["--install"]]},
            {"name": "urcmtp", "archive": "URCMTP 1.3.rar", "exe": "Complete Installer/URCMTP.exe",
             "kind": "inspect", "show": ["Readme UTP.txt"]},
        ],
        "extract": ["Extended Enclave.2.5.2.rar", "dm_qrts.rar"],
    },
}

TSLRCM = "tslrcm2022.exe"


def now():
    return round(time.monotonic(), 2)


def run(argv, timeout=60, **options):
    try:
        done = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout, **options)
        return {"argv": [str(a) for a in argv], "exit": done.returncode,
                "stdout": done.stdout.decode("utf-8", "backslashreplace")[-8000:],
                "stderr": done.stderr.decode("utf-8", "backslashreplace")[-4000:]}
    except subprocess.TimeoutExpired:
        return {"argv": [str(a) for a in argv], "timeout": timeout}
    except OSError as error:
        return {"argv": [str(a) for a in argv], "error": str(error)}


def windows():
    found = subprocess.run(["xdotool", "search", "--onlyvisible", "--name", "."], capture_output=True,
                           stdin=subprocess.DEVNULL, timeout=30)
    result = []
    for identifier in found.stdout.decode().split():
        name = subprocess.run(["xdotool", "getwindowname", identifier], capture_output=True, timeout=10)
        geometry = subprocess.run(["xdotool", "getwindowgeometry", "--shell", identifier],
                                  capture_output=True, timeout=10)
        values = dict(line.split("=", 1) for line in geometry.stdout.decode().split() if "=" in line)
        result.append({"id": identifier, "name": name.stdout.decode("utf-8", "backslashreplace").strip(),
                       "x": values.get("X"), "y": values.get("Y"),
                       "w": values.get("WIDTH"), "h": values.get("HEIGHT")})
    return result


class Recorder:
    def __init__(self, directory):
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True)
        self.events = []
        self.count = 0
        self.last = None

    def snap(self, label):
        self.count += 1
        name = "%02d-%s.png" % (self.count, re.sub(r"[^A-Za-z0-9_.-]+", "_", label)[:40])
        subprocess.run(["import", "-window", "root", str(self.directory / name)], capture_output=True,
                       stdin=subprocess.DEVNULL, timeout=60)
        current = windows()
        self.last = sorted((w["name"], w["w"], w["h"]) for w in current)
        self.events.append({"t": now(), "label": label, "windows": current,
                            "screenshot": name if (self.directory / name).is_file() else None})
        print("  [%s] %s: %s" % (self.directory.name, label, [w["name"] for w in current]), flush=True)
        return current

    def changed(self):
        current = sorted((w["name"], w["w"], w["h"]) for w in windows())
        return current != self.last

    def note(self, **values):
        values["t"] = now()
        self.events.append(values)
        print("  [%s] %s" % (self.directory.name, json.dumps(values)[:600]), flush=True)


def kill_all(process=None):
    if process is not None and process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
    subprocess.run(["wineserver", "-k"], capture_output=True, timeout=60)
    time.sleep(2)


def find_archive(archives, pattern):
    matches = sorted(path for path in archives.rglob("*") if path.is_file() and path.match(pattern))
    return matches[0] if matches else None


def extract(archive, destination):
    shutil.rmtree(destination, ignore_errors=True)
    destination.mkdir(parents=True)
    attempts = []
    for command in (["7z", "x", "-y", "-bd", "-o" + str(destination), str(archive)],
                    ["7zz", "x", "-y", "-bd", "-o" + str(destination), str(archive)],
                    ["unrar", "x", "-o+", "-y", str(archive), str(destination) + "/"],
                    ["unar", "-q", "-f", "-D", "-o", str(destination), str(archive)],
                    ["bsdtar", "-xf", str(archive), "-C", str(destination)]):
        if shutil.which(command[0]) is None:
            continue
        result = run(command, timeout=900)
        attempts.append(result)
        if result.get("exit") == 0:
            return command[0], attempts
        shutil.rmtree(destination, ignore_errors=True)
        destination.mkdir(parents=True)
    return None, attempts


def win_path(path):
    return "Z:" + str(path).replace("/", "\\")


def state(root):
    result = {}
    for directory, _, files in os.walk(root):
        for name in files:
            path = Path(directory) / name
            info = path.lstat()
            result[path.relative_to(root).as_posix()] = (info.st_size, info.st_mtime_ns)
    return result


def diff(before, after):
    added = sorted(set(after) - set(before))
    changed = sorted(name for name in set(after) & set(before) if after[name] != before[name])
    removed = sorted(set(before) - set(after))
    return {"added": added[:400], "changed": changed[:400], "removed": removed[:400],
            "counts": [len(added), len(changed), len(removed)]}


def copy_game(source, destination):
    shutil.rmtree(destination, ignore_errors=True)
    started = time.monotonic()
    subprocess.run(["cp", "-a", "--reflink=auto", str(source), str(destination)], check=True)
    return round(time.monotonic() - started, 1)


def read_log(path):
    data = path.read_bytes()
    for encoding in ("utf-8", "cp1252"):
        try:
            return {"encoding": encoding, "text": data.decode(encoding)[:60000]}
        except UnicodeDecodeError:
            continue
    return {"encoding": None, "text": data.decode("latin-1")[:60000]}


def rtf_plain(text):
    text = re.sub(r"\\par[d]?\b ?", "\n", text)
    text = re.sub(r"\\'([0-9a-f]{2})", lambda m: bytes.fromhex(m.group(1)).decode("cp1252"), text)
    text = re.sub(r"\\[a-zA-Z]+-?\d* ?", "", text)
    return re.sub(r"[{}]", "", text)


def collect_logs(roots, since):
    found = {}
    for root in roots:
        for path in Path(root).rglob("*"):
            if path.is_file() and path.stat().st_mtime >= since and (
                    path.suffix.lower() in (".rtf", ".log", ".txt") or "log" in path.name.lower()) \
                    and path.stat().st_size < 4 * 1024 * 1024:
                entry = read_log(path)
                if path.suffix.lower() == ".rtf":
                    entry["plain"] = rtf_plain(entry["text"])
                found[str(path)] = entry
    return found


# ---------------------------------------------------------------- TSLPatcher (GUI)

def tsl_actions(case):
    """The candidate plan actions under test, in manual.py's schema."""
    actions = []
    if case["option"] is not None:
        actions += [{"wait_window": "^TSLPatcher - Select installation type$", "timeout": 90},
                    {"sleep": 2}]
        if case["option"] > 0:
            actions.append({"key": ["Down"] * case["option"],
                            "window": "^TSLPatcher - Select installation type$"})
            actions.append({"sleep": 1})
        actions.append({"click": [248, 181], "window": "^TSLPatcher - Select installation type$"})
    actions += [{"wait_window": case["title"], "timeout": 90},
                {"sleep": 3},
                {"click": [502, 433], "window": case["title"]},
                {"sleep": 5}]
    return actions


def tsl_case(case, archives, scratch, out, game_source):
    record = {"case": case["name"], "archive": case["archive"], "exe": case["exe"], "option": case["option"]}
    rec = Recorder(out / case["name"])
    archive = find_archive(archives, case["archive"])
    if archive is None:
        record["error"] = "archive not found"
        return record
    extract_root = scratch / ("x-" + case["name"])
    tool, attempts = extract(archive, extract_root)
    record["extracted_with"] = tool
    exe = extract_root / case["exe"]
    record["exe_sha256"] = hashlib.sha256(exe.read_bytes()).hexdigest()
    game = scratch / "game"
    record["copy_seconds"] = copy_game(game_source, game)
    before = state(game)
    tslpatchdata = exe.parent / "tslpatchdata"
    log_path = tslpatchdata / "installlog.rtf"
    roots = {"game": game, "extract": extract_root, "archives": archives}
    operation = {"success_log": "{extract}/" + log_path.relative_to(extract_root).as_posix(),
                 "success_markers": ["Done. Changes have been applied"], "failure_regex": TSL_FAILURE}
    actions = tsl_actions(case)
    record["actions_tested"] = actions
    tools = manual.gui_tools()
    stdout = open(rec.directory / "wine-stdout.txt", "wb")
    started = time.monotonic()
    process = subprocess.Popen(["wine", str(exe)], cwd=exe.parent, stdin=subprocess.DEVNULL, stdout=stdout,
                               stderr=subprocess.STDOUT, start_new_session=True)
    deadline = time.monotonic() + 1500
    steplog = open(rec.directory / "actions.log", "wb")
    record["actions_ok"] = True
    install_clicked = None
    try:
        for index, action in enumerate(actions):
            try:
                manual.run_gui_action(action, operation, roots, tools, steplog, deadline)
            except Exception as error:
                record["actions_ok"] = False
                record["action_error"] = {"index": index, "action": action, "error": str(error)}
                rec.snap("action-%d-failed" % index)
                break
            if "click" in action and action["click"] == [502, 433]:
                install_clicked = time.monotonic()
            rec.snap("after-%d-%s" % (index, next(iter(action))))
        explore(rec, record, log_path, game, case, install_clicked or time.monotonic())
    finally:
        record["process_exit"] = process.poll()
        rec.snap("final")
        kill_all(process)
        stdout.close()
        steplog.close()
    if log_path.is_file():
        record["installlog"] = read_log(log_path)
        record["installlog_plain"] = rtf_plain(record["installlog"]["text"])
        record["log_verdict"] = manual.log_verdict(record["installlog"]["text"], operation)
    record["other_logs"] = collect_logs([extract_root], started - 5)
    record["game_diff"] = diff(before, state(game))
    record["events"] = rec.events
    shutil.rmtree(game, ignore_errors=True)
    shutil.rmtree(extract_root, ignore_errors=True)
    return record


def newest_other(known_titles):
    for window in reversed(windows()):
        if window["name"] and not any(re.search(t, window["name"]) for t in known_titles) \
                and window["w"] not in ("1", None):
            return window
    return None


def explore(rec, record, log_path, game, case, clicked):
    """After Install Mod: find the folder dialog, try to hand it the game path, answer
    confirmations, wait for the log. Every decision is recorded with screenshots."""
    known = [case["title"], r"^TSLPatcher$"]
    dialog = newest_other(known)
    record["after_install_dialog"] = dialog
    tried_path = False
    presses = 0
    limit = time.monotonic() + 600
    settled = None
    while time.monotonic() < limit:
        if log_path.is_file():
            text = read_log(log_path)["text"]
            if "Done." in text or "rror" in text:
                if settled is None:
                    settled = time.monotonic()
                    record["log_settled_seconds_after_install_click"] = round(settled - clicked, 1)
                    rec.snap("log-settled")
                if time.monotonic() - settled > 5:
                    break
        other = newest_other(known)
        if other is not None and settled is None:
            rec.snap("dialog-" + other["name"])
            if not tried_path and re.search(r"(?i)browse|folder|select|directory|game", other["name"]):
                tried_path = True
                rec.note(strategy="type game path + Return", window=other)
                subprocess.run(["xdotool", "windowfocus", other["id"]], capture_output=True)
                subprocess.run(["xdotool", "type", "--delay", "30", "--", win_path(game)], capture_output=True)
                rec.snap("typed-path")
                subprocess.run(["xdotool", "key", "--window", other["id"], "Return"], capture_output=True)
                time.sleep(4)
                rec.snap("after-path-return")
                continue
            if presses < 6:
                presses += 1
                rec.note(strategy="Return on dialog", window=other)
                subprocess.run(["xdotool", "windowfocus", other["id"]], capture_output=True)
                subprocess.run(["xdotool", "key", "--window", other["id"], "Return"], capture_output=True)
                time.sleep(4)
                rec.snap("after-return")
                continue
        elif rec.changed():
            rec.snap("windows-changed")
        time.sleep(1)
    record["auto_presses"] = presses


# ---------------------------------------------------------------- CLI installers

def cli_case(case, archives, scratch, out, game_source):
    record = {"case": case["name"], "archive": case["archive"], "exe": case["exe"], "runs": []}
    rec = Recorder(out / case["name"])
    archive = find_archive(archives, case["archive"])
    if archive is None:
        record["error"] = "archive not found"
        return record
    extract_root = scratch / ("x-" + case["name"])
    tool, attempts = extract(archive, extract_root)
    record["extracted_with"] = tool
    exe = extract_root / case["exe"]
    record["exe_sha256"] = hashlib.sha256(exe.read_bytes()).hexdigest()
    record["help"] = run(["wine", str(exe), "--help"], timeout=180, cwd=exe.parent)
    rec.note(help=record["help"])
    kill_all()
    for index, tail in enumerate(case["variants"]):
        game = scratch / "game"
        copy_game(game_source, game)
        before = state(game)
        mod_before = state(extract_root)
        argv = ["wine", str(exe), "--game-dir", win_path(game), "--tslpatchdata", win_path(exe.parent)] + tail
        entry = {"argv": argv}
        output = rec.directory / ("run-%d-output.txt" % index)
        started = time.monotonic()
        wall = time.time()
        with open(output, "wb") as stream:
            process = subprocess.Popen(argv, cwd=exe.parent, stdin=subprocess.DEVNULL, stdout=stream,
                                       stderr=subprocess.STDOUT, start_new_session=True)
            rec.snap("run-%d-start" % index)
            while process.poll() is None and time.monotonic() - started < 900:
                time.sleep(2)
                if rec.changed():
                    rec.snap("run-%d-windows" % index)
            entry["exited_by_itself"] = process.poll() is not None
            entry["exit"] = process.poll()
            entry["seconds"] = round(time.monotonic() - started, 1)
            if process.poll() is None:
                rec.snap("run-%d-still-running" % index)
            kill_all(process)
        entry["output"] = output.read_bytes().decode("utf-8", "backslashreplace")[-20000:]
        entry["game_diff"] = diff(before, state(game))
        entry["mod_diff"] = diff(mod_before, state(extract_root))
        entry["logs"] = collect_logs([extract_root, game], wall - 5)
        record["runs"].append(entry)
        rec.note(run=index, exit=entry["exit"], exited=entry["exited_by_itself"], seconds=entry["seconds"],
                 game_counts=entry["game_diff"]["counts"], mod_added=entry["mod_diff"]["added"][:20])
        shutil.rmtree(game, ignore_errors=True)
    record["events"] = rec.events
    shutil.rmtree(extract_root, ignore_errors=True)
    return record


def strings_of(path, patterns):
    data = path.read_bytes()
    found = {}
    for pattern in patterns:
        hits = sorted(set(m.group(0).decode("latin-1") for m in re.finditer(pattern.encode(), data)))
        if hits:
            found[pattern] = hits[:20]
    return found


INSTALLER_PATTERNS = [r"Nullsoft[^\x00]{0,40}", r"NSIS[^\x00]{0,30}", r"Inno Setup[^\x00]{0,40}",
                      r"7-Zip[^\x00]{0,20}", r"WinRAR[^\x00]{0,20}", r"TSLPatcher[^\x00]{0,30}",
                      r"HoloPatcher[^\x00]{0,30}", r"InstallShield[^\x00]{0,20}", r"PAR::[A-Za-z]+",
                      r"perl5\d+\.dll", r"Setup Factory[^\x00]{0,20}", r"WiX[^\x00]{0,20}",
                      r"Smart Install Maker[^\x00]{0,20}", r"pyinstaller|PyInstaller|_MEIPASS",
                      r"Clickteam[^\x00]{0,20}", r"InstallAware[^\x00]{0,20}", r"Ghost Installer"]


def inspect_case(case, archives, scratch, out, game_source):
    record = {"case": case["name"], "archive": case["archive"], "exe": case["exe"]}
    rec = Recorder(out / case["name"])
    archive = find_archive(archives, case["archive"])
    if archive is None:
        record["error"] = "archive not found"
        return record
    extract_root = scratch / ("x-" + case["name"])
    tool, attempts = extract(archive, extract_root)
    record["extracted_with"] = tool
    record["extract_attempts"] = attempts
    record["members"] = sorted(p.relative_to(extract_root).as_posix() for p in extract_root.rglob("*")
                               if p.is_file())[:300]
    exe = extract_root / case["exe"]
    record["exe_sha256"] = hashlib.sha256(exe.read_bytes()).hexdigest()
    record["strings"] = strings_of(exe, INSTALLER_PATTERNS)
    record["shown"] = {}
    for name in case.get("show", []):
        path = extract_root / name
        if path.suffix.lower() == ".pdf" and shutil.which("pdftotext"):
            record["shown"][name] = run(["pdftotext", "-layout", str(path), "-"], timeout=60).get("stdout")
        elif path.is_file():
            record["shown"][name] = read_log(path)["text"][:20000]
    for args in (["--help"], ["/?"], []):
        result = run(["wine", str(exe)] + args, timeout=60, cwd=exe.parent)
        record.setdefault("runs", []).append(result)
        kill_all()
    # GUI look: start without arguments and record the first screens.
    process = subprocess.Popen(["wine", str(exe)], cwd=exe.parent, stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    time.sleep(20)
    rec.snap("gui-start")
    kill_all(process)
    record["events"] = rec.events
    shutil.rmtree(extract_root, ignore_errors=True)
    return record


def tslrcm_case(archives, scratch, out, game_source):
    record = {"case": "tslrcm"}
    rec = Recorder(out / "tslrcm")
    exe = find_archive(archives, TSLRCM)
    if exe is None:
        record["error"] = "tslrcm2022.exe not found"
        return record
    record["sha256"] = hashlib.sha256(exe.read_bytes()).hexdigest()
    record["strings"] = strings_of(exe, INSTALLER_PATTERNS)
    record["innoextract_info"] = run(["innoextract", "--info", str(exe)], timeout=120)
    record["innoextract_list"] = run(["innoextract", "--list", str(exe)], timeout=300)
    record["7z_list"] = run(["7z", "l", str(exe)], timeout=300)
    inno = "Inno Setup" in json.dumps(record["strings"]) or record["innoextract_info"].get("exit") == 0
    nsis = "Nullsoft" in json.dumps(record["strings"])
    record["detected"] = "inno" if inno else "nsis" if nsis else "unknown"
    game = scratch / "game"
    copy_game(game_source, game)
    before = state(game)
    setup_log = scratch / "tslrcm-setup.log"
    if inno:
        argv = ["wine", str(exe), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-",
                "/DIR=" + win_path(game), "/LOG=" + win_path(setup_log)]
    elif nsis:
        argv = ["wine", str(exe), "/S", "/D=" + win_path(game)]
    else:
        argv = ["wine", str(exe)]
    record["argv"] = argv
    started = time.monotonic()
    output = rec.directory / "output.txt"
    with open(output, "wb") as stream:
        process = subprocess.Popen(argv, cwd=exe.parent, stdin=subprocess.DEVNULL, stdout=stream,
                                   stderr=subprocess.STDOUT, start_new_session=True)
        last_snap = 0
        while process.poll() is None and time.monotonic() - started < 1500:
            time.sleep(3)
            if rec.changed() or time.monotonic() - last_snap > 60:
                rec.snap("tslrcm")
                last_snap = time.monotonic()
        record["exited_by_itself"] = process.poll() is not None
        record["exit"] = process.poll()
        record["seconds"] = round(time.monotonic() - started, 1)
        rec.snap("tslrcm-end")
        kill_all(process)
    record["output"] = output.read_bytes().decode("utf-8", "backslashreplace")[-8000:]
    if setup_log.is_file():
        record["setup_log"] = read_log(setup_log)["text"][-30000:]
    record["game_diff"] = diff(before, state(game))
    record["events"] = rec.events
    shutil.rmtree(game, ignore_errors=True)
    return record


def extract_case(names, archives, scratch):
    results = []
    for name in names:
        archive = find_archive(archives, name)
        if archive is None:
            results.append({"archive": name, "error": "not found"})
            continue
        entry = {"archive": name, "size": archive.stat().st_size, "tools": []}
        for command in (["7z", "x", "-y", "-bd"], ["7zz", "x", "-y", "-bd"], ["unrar", "x", "-o+", "-y"],
                        ["unar", "-q", "-f", "-D", "-o"], ["bsdtar", "-xf"]):
            destination = scratch / "rar-test"
            shutil.rmtree(destination, ignore_errors=True)
            destination.mkdir(parents=True)
            if shutil.which(command[0]) is None:
                entry["tools"].append({"tool": command[0], "missing": True})
                continue
            if command[0] in ("7z", "7zz"):
                argv = command + ["-o" + str(destination), str(archive)]
            elif command[0] == "unrar":
                argv = command + [str(archive), str(destination) + "/"]
            elif command[0] == "unar":
                argv = command + [str(destination), str(archive)]
            else:
                argv = command + [str(archive), "-C", str(destination)]
            started = time.monotonic()
            result = run(argv, timeout=900)
            files = [p.relative_to(destination).as_posix() for p in destination.rglob("*") if p.is_file()]
            result.update({"tool": command[0], "seconds": round(time.monotonic() - started, 1),
                           "files": len(files), "sample": sorted(files)[:40],
                           "version": run([command[0]] + (["--version"] if command[0] == "bsdtar" else []),
                                          timeout=30).get("stdout", "")[:300]})
            entry["tools"].append(result)
            print("  extract %s with %s: exit %s files %d" % (name, command[0], result.get("exit"), len(files)),
                  flush=True)
        shutil.rmtree(scratch / "rar-test", ignore_errors=True)
        results.append(entry)
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--game", choices=sorted(CASES), required=True)
    parser.add_argument("--game-dir", type=Path, required=True)
    parser.add_argument("--archives", type=Path, required=True)
    parser.add_argument("--scratch", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--only", default="")
    args = parser.parse_args(argv)
    only = [item.strip() for item in args.only.split(",") if item.strip()] or list(SECTIONS)
    if args.game != "k2" and "tslrcm" in only:
        only.remove("tslrcm")
    args.out.mkdir(parents=True, exist_ok=True)
    args.scratch.mkdir(parents=True, exist_ok=True)
    game_source = args.game_dir.resolve(strict=True)
    archives = args.archives.resolve(strict=True)
    scratch = args.scratch.resolve()
    cases = CASES[args.game]
    report = {"game": args.game, "sections": only, "tools": {}}
    for tool in ("wine", "7z", "7zz", "unrar", "unar", "bsdtar", "innoextract"):
        report["tools"][tool] = shutil.which(tool)
    report["wine_version"] = run(["wine", "--version"]).get("stdout", "").strip()
    started = time.monotonic()
    report["wineboot"] = run(["wineboot", "-i"], timeout=600)
    run(["wineserver", "-w"], timeout=600)
    report["wineboot_seconds"] = round(time.monotonic() - started, 1)

    def save():
        (args.out / "result.json").write_text(json.dumps(report, indent=1, default=str) + "\n", encoding="utf-8")

    def guarded(label, function, *values):
        print("probe: %s" % label, flush=True)
        try:
            return function(*values)
        except Exception as error:  # evidence gathering: record and continue
            kill_all()
            return {"case": label, "exception": "%s: %s" % (type(error).__name__, error)}
        finally:
            save()

    if "extract" in only and cases["extract"]:
        report["extract"] = guarded("extract", extract_case, cases["extract"], archives, scratch)
    if "tsl" in only:
        report["tsl"] = [guarded(case["name"], tsl_case, case, archives, scratch, args.out, game_source)
                         for case in cases["tsl"]]
    if "holo" in only:
        report["holo"] = [guarded(case["name"], cli_case, case, archives, scratch, args.out, game_source)
                          for case in cases["holo"]]
    if "other" in only:
        report["other"] = [guarded(case["name"],
                                   inspect_case if case.get("kind") == "inspect" else cli_case,
                                   case, archives, scratch, args.out, game_source)
                           for case in cases["other"]]
    if "tslrcm" in only:
        report["tslrcm"] = guarded("tslrcm", tslrcm_case, archives, scratch, args.out, game_source)
    save()
    return 0


if __name__ == "__main__":
    sys.exit(main())
