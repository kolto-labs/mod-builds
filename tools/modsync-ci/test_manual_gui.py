import copy
import importlib.util
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent


def load(name):
    spec = importlib.util.spec_from_file_location(name, HERE / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


manual = load("manual")

# Each fake tool appends one line per call: its name and arguments, tab separated.
RECORD = 'printf "%s\\t" "$(basename "$0")" "$@" >> "$FAKE_STATE/calls"; echo >> "$FAKE_STATE/calls"\n'

FAKE_XDOTOOL = """#!/bin/sh
""" + RECORD + """case "$1" in
  search) if [ -s "$FAKE_STATE/windows" ]; then cat "$FAKE_STATE/windows"; exit 0; fi; exit 1 ;;
  getwindowname) echo "Fake Installer"; exit 0 ;;
  key) touch "$FAKE_STATE/go" ;;
esac
exit 0
"""

# Stands in for "wine reg query/add/delete" (one file per value name under
# FAKE_STATE, holding "TYPE    DATA") and for wine running the original patcher. As
# the patcher it shows a window, waits for the replayed keystroke, writes the log
# (and, depending on FAKE_MODE, changes a game file) and then stays open.
FAKE_WINE = """#!/bin/sh
""" + RECORD + """if [ "$1" = reg ]; then
  value="$FAKE_STATE/reg-$5"
  case "$2" in
    query) [ -f "$value" ] || exit 1
           printf '%s\\r\\n    %s    %s\\r\\n\\r\\n' "$3" "$5" "$(cat "$value")"; exit 0 ;;
    add) if [ "$6" = /t ]; then printf '%s    %s' "$7" "$9" > "$value"
         else printf 'REG_SZ    %s' "$7" > "$value"; fi; exit 0 ;;
    delete) [ -z "$FAKE_REG_DELETE_FAILS" ] || exit 1; rm -f "$value"; exit 0 ;;
  esac
  exit 2
fi
if [ "$FAKE_MODE" = nowindow ]; then exec sleep 600; fi
echo 4194305 > "$FAKE_STATE/windows"
while [ ! -e "$FAKE_STATE/go" ]; do sleep 0.05; done
case "$FAKE_MODE" in
  success) printf '{\\\\rtf1 Installing caf\\351\\\\par Done. Changes applied.\\\\par}' > "$FAKE_LOG"
           echo patched > "$FAKE_GAME/override/new.2da" ;;
  failure) printf 'Done. Changes applied.\\nError: could not patch\\n' > "$FAKE_LOG"
           echo patched > "$FAKE_GAME/override/new.2da" ;;
  nochange) printf 'Done. Changes applied.\\n' > "$FAKE_LOG" ;;
esac
exec sleep 600
"""

FAKE_WINESERVER = "#!/bin/sh\n" + RECORD + "exit 0\n"
FAKE_IMPORT = "#!/bin/sh\n" + RECORD + 'eval "out=\\${$#}"; : > "$out"; exit 0\n'


def gui_operation(wine):
    return {"type": "gui_patcher",
            "patcher": "{extract}/mod/TSLPatcher.exe",
            "sha256": "0" * 64,
            "argv": [str(wine), "{extract}/mod/TSLPatcher.exe"],
            "cwd": "{extract}/mod",
            "success_log": "{extract}/mod/installlog.rtf",
            "success_markers": ["Done. Changes applied."],
            "failure_regex": ["(?m)^Error:"],
            "expected_changes": "changed_manifest",
            "wine_registry": [{"key": "HKEY_LOCAL_MACHINE\\Software\\BioWare\\SW\\KOTOR",
                               "name": "Path", "data": "{game_win}"}],
            "actions": [{"wait_window": "^Fake Installer", "timeout": 10},
                        {"type": "{game_win}\\", "window": "^Fake Installer"},
                        {"sleep": 0.1},
                        {"key": ["alt+i", "Return"], "window": "^Fake Installer"},
                        {"wait_log": 10}]}


class Fixture(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.archives = self.root / "archives"
        self.game = self.root / "game"
        self.work = self.root / "work"
        self.archives.mkdir()
        (self.game / "override").mkdir(parents=True)
        (self.game / "vanilla.txt").write_text("x", encoding="utf-8")
        (self.work / "extract").mkdir(parents=True)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.wine = self.bin / "wine"

    def roots(self):
        return {"game": self.game, "archives": self.archives, "extract": self.work / "extract"}


class GuiValidation(Fixture):
    def good(self):
        return gui_operation(self.wine)

    def assertRejected(self, operation, message):
        with self.assertRaises(manual.Blocked) as caught:
            manual.validate_operation(operation, self.roots())
        self.assertIn(message, str(caught.exception))

    def mutated(self, change):
        operation = copy.deepcopy(self.good())
        change(operation)
        return operation

    def action(self, action):
        return self.mutated(lambda operation: operation.update(actions=[action]))

    def test_good_operation_is_valid(self):
        manual.validate_operation(self.good(), self.roots())
        manual.validate_operation(self.mutated(lambda op: op.pop("wine_registry")), self.roots())
        manual.validate_operation(self.action({"click": [0, 15], "window": "x"}), self.roots())

    def test_operation_fields(self):
        self.assertRejected(self.mutated(lambda op: op.update(extra=1)), "invalid object fields")
        for key in ("actions", "patcher", "sha256", "argv", "cwd", "success_log",
                    "success_markers", "failure_regex"):
            self.assertRejected(self.mutated(lambda op: op.pop(key)), "invalid object fields")

    def test_patcher_rules_are_shared(self):
        self.assertRejected(self.mutated(lambda op: op.update(sha256="ABC")), "SHA256")
        self.assertRejected(self.mutated(lambda op: op.update(argv=["/usr/bin/env", "x"])),
                            "argv must execute original patcher")
        self.assertRejected(self.mutated(lambda op: op.update(argv=["wine", op["patcher"]])),
                            "argv must execute original patcher")
        self.assertRejected(self.mutated(lambda op: op.update(success_markers=[])), "success_markers")
        self.assertRejected(self.mutated(lambda op: op.update(failure_regex=["("])), "invalid failure regex")
        self.assertRejected(self.mutated(lambda op: op.pop("expected_changes")),
                            "expected files or changed manifest")
        self.assertRejected(self.mutated(lambda op: op.update(expected_changes="yes")),
                            "invalid expected_changes")
        self.assertRejected(self.mutated(lambda op: op.update(success_log="{archives}/log.rtf")),
                            "wrong root")

    def test_actions_must_be_a_nonempty_list(self):
        for actions in ([], None, "wait", {"sleep": 1}):
            self.assertRejected(self.mutated(lambda op: op.update(actions=actions)), "actions must be")

    def test_each_action_names_exactly_one_kind(self):
        self.assertRejected(self.action("sleep"), "gui action must be an object")
        self.assertRejected(self.action({}), "exactly one action")
        self.assertRejected(self.action({"press": ["a"]}), "exactly one action")
        self.assertRejected(self.action({"sleep": 1, "wait_log": 1}), "exactly one action")
        self.assertRejected(self.action({"sleep": 1, "window": "x"}), "invalid object fields")
        self.assertRejected(self.action({"key": ["a"]}), "invalid object fields")
        self.assertRejected(self.action({"wait_window": "x"}), "invalid object fields")

    def test_regexes_must_compile(self):
        self.assertRejected(self.action({"wait_window": "(", "timeout": 1}), "invalid wait_window regex")
        self.assertRejected(self.action({"key": ["a"], "window": "["}), "invalid window regex")
        self.assertRejected(self.action({"key": ["a"], "window": ""}), "window regex must be")
        self.assertRejected(self.action({"type": "a", "window": 5}), "window regex must be")

    def test_seconds_must_be_positive_finite(self):
        for value in (0, -1, float("inf"), float("nan"), "5", True, None):
            self.assertRejected(self.action({"wait_window": "x", "timeout": value}), "timeout must be")
            self.assertRejected(self.action({"wait_log": value}), "timeout must be")
            self.assertRejected(self.action({"sleep": value}), "timeout must be")
        self.assertRejected(self.action({"sleep": 61}), "sleep must be at most 60")

    def test_keysyms(self):
        for keys in ([], ["", "a"], ["alt i"], ["a\t"], ["-a"], [1], "Return"):
            self.assertRejected(self.action({"key": keys, "window": "x"}), "key must be")

    def test_type_and_click(self):
        for text in ("", None, 3, "a\x00b"):
            self.assertRejected(self.action({"type": text, "window": "x"}), "type text must be")
        for point in ([1], [1, 2, 3], [-1, 2], [1.5, 2], [True, 2], "1,2"):
            self.assertRejected(self.action({"click": point, "window": "x"}), "click must be")

    def test_wine_registry(self):
        def registry(entries):
            return self.mutated(lambda op: op.update(wine_registry=entries))
        self.assertRejected(registry({"key": "x"}), "wine_registry must be a list")
        self.assertRejected(registry([{"key": "HKEY_CURRENT_USER\\A", "name": "n"}]), "invalid object fields")
        self.assertRejected(registry([{"key": "HKEY_CURRENT_USER\\A", "name": "n", "data": "", "x": 1}]),
                            "invalid object fields")
        for key in ("HKCU\\A", "HKEY_CLASSES_ROOT\\A", "HKEY_LOCAL_MACHINE\\", "HKEY_CURRENT_USER", ""):
            self.assertRejected(registry([{"key": key, "name": "n", "data": ""}]), "registry key")
        self.assertRejected(registry([{"key": "HKEY_CURRENT_USER\\A", "name": "", "data": ""}]),
                            "registry value name")
        self.assertRejected(registry([{"key": "HKEY_CURRENT_USER\\A", "name": "n", "data": 1}]),
                            "registry data")


class InstallerLogDecoding(unittest.TestCase):
    def test_utf8_then_explicit_cp1252_without_replacement(self):
        self.assertEqual(manual.decode_installer_log("café".encode(), ("utf-8",)), "café")
        self.assertEqual(manual.decode_installer_log(b"caf\xe9", ("utf-8", "cp1252")), "café")
        with self.assertRaises(manual.Blocked):
            manual.decode_installer_log(b"caf\xe9", ("utf-8",))
        with self.assertRaises(manual.Blocked):
            manual.decode_installer_log(b"\x81", ("utf-8", "cp1252"))


class GuiExecution(Fixture):
    def setUp(self):
        super().setUp()
        self.state = self.root / "state"
        self.state.mkdir()
        for name, script in (("xdotool", FAKE_XDOTOOL), ("wine", FAKE_WINE),
                             ("wineserver", FAKE_WINESERVER), ("import", FAKE_IMPORT)):
            (self.bin / name).write_text(script, encoding="utf-8")
            (self.bin / name).chmod(0o755)
        self.mod = self.work / "extract" / "mod"
        self.mod.mkdir()
        (self.mod / "TSLPatcher.exe").write_bytes(b"MZ original patcher")
        self.log_path = self.work / "operations.log"

    def run_gui(self, mode, operation=None, timeout=60, path=None, digest=None):
        operation = operation or gui_operation(self.wine)
        operation["sha256"] = digest or manual.sha256(self.mod / "TSLPatcher.exe")
        environment = {"PATH": path or "%s:%s" % (self.bin, os.environ.get("PATH", "/usr/bin:/bin")),
                       "DISPLAY": ":99", "FAKE_STATE": str(self.state), "FAKE_MODE": mode,
                       "FAKE_LOG": str(self.mod / "installlog.rtf"), "FAKE_GAME": str(self.game)}
        with mock.patch.dict(os.environ, environment), self.log_path.open("ab") as log:
            manual.execute(operation, self.roots(), log, time.monotonic() + timeout)

    def calls(self):
        path = self.state / "calls"
        if not path.exists():
            return []
        return [line.split("\t")[:-1] for line in path.read_text(encoding="utf-8").splitlines()]

    def assertFails(self, mode, message, **arguments):
        with self.assertRaises(manual.Blocked) as caught:
            self.run_gui(mode, **arguments)
        self.assertIn(message, str(caught.exception))

    def test_actions_run_in_order_and_success_is_proven(self):
        self.run_gui("success")
        windows = "Z:" + str(self.game).replace("/", "\\")
        calls = [call for call in self.calls() if call[:2] != ["xdotool", "search"]]
        key = "HKEY_LOCAL_MACHINE\\Software\\BioWare\\SW\\KOTOR"
        self.assertEqual(calls[0], ["wine", "reg", "query", key, "/v", "Path"])
        self.assertEqual(calls[1], ["wine", "reg", "add", key, "/v", "Path", "/d", windows, "/f"])
        self.assertEqual(calls[2], ["wine", str(self.mod / "TSLPatcher.exe")])
        self.assertEqual(calls[3:], [
            ["xdotool", "windowfocus", "4194305"],
            ["xdotool", "type", "--delay", "30", "--", windows + "\\"],
            ["xdotool", "windowfocus", "4194305"],
            ["xdotool", "key", "--window", "4194305", "--delay", "100", "alt+i", "Return"],
            ["wineserver", "-k"],
            ["wine", "reg", "delete", key, "/v", "Path", "/f"]])
        self.assertFalse((self.state / "reg-Path").exists())
        self.assertEqual((self.game / "override" / "new.2da").read_text(encoding="utf-8"), "patched\n")
        self.assertFalse(list(self.work.glob("gui-failure-*.png")))

    def test_existing_registry_value_is_put_back(self):
        (self.state / "reg-Path").write_text("REG_EXPAND_SZ    %USERPROFILE%\\Desktop", encoding="utf-8")
        self.run_gui("success")
        self.assertEqual(self.calls()[-1], ["wine", "reg", "add", "HKEY_LOCAL_MACHINE\\Software\\BioWare\\SW\\KOTOR",
                                            "/v", "Path", "/t", "REG_EXPAND_SZ", "/d", "%USERPROFILE%\\Desktop", "/f"])
        self.assertEqual((self.state / "reg-Path").read_text(encoding="utf-8"),
                         "REG_EXPAND_SZ    %USERPROFILE%\\Desktop")
        self.assertIn(b"registry restored", self.log_path.read_bytes())

    def test_registry_is_restored_after_failure(self):
        (self.state / "reg-Path").write_text("REG_SZ    C:\\old", encoding="utf-8")
        self.assertFails("failure", "installer failure marker matched")
        self.assertEqual((self.state / "reg-Path").read_text(encoding="utf-8"), "REG_SZ    C:\\old")

    def test_registry_value_that_cannot_be_read_back_blocks_before_writing(self):
        (self.state / "reg-Path").write_text("REG_BINARY    00ff", encoding="utf-8")
        self.assertFails("success", "cannot read registry value back")
        self.assertFalse(any(call[:3] == ["wine", "reg", "add"] or call[:2] == ["wine", str(self.mod / "TSLPatcher.exe")]
                             for call in self.calls()))
        self.assertEqual((self.state / "reg-Path").read_text(encoding="utf-8"), "REG_BINARY    00ff")

    def test_failed_restore_fails_a_successful_run(self):
        with mock.patch.dict(os.environ, {"FAKE_REG_DELETE_FAILS": "1"}):
            self.assertFails("success", "wine registry restore failed")

    def test_failed_restore_does_not_mask_the_original_error(self):
        with mock.patch.dict(os.environ, {"FAKE_REG_DELETE_FAILS": "1"}):
            self.assertFails("failure", "installer failure marker matched")
        self.assertIn(b"registry restore failed", self.log_path.read_bytes())

    def test_failure_regex_match_fails(self):
        self.assertFails("failure", "installer failure marker matched")
        self.assertIn(["wineserver", "-k"], self.calls())
        self.assertTrue((self.work / "gui-failure-1.png").exists())

    def test_failure_preserves_installer_log_before_rollback(self):
        self.assertFails("failure", "installer failure marker matched")
        copy = self.work / "installer-log-1.rtf"
        self.assertEqual(copy.read_bytes(), (self.mod / "installlog.rtf").read_bytes())
        self.assertIn(b"preserved as installer-log-1.rtf", self.log_path.read_bytes())

    def test_preserved_installer_log_is_size_capped(self):
        with mock.patch.object(manual, "INSTALLER_LOG_COPY_LIMIT", 8):
            self.assertFails("failure", "installer failure marker matched")
        self.assertEqual((self.work / "installer-log-1.rtf").read_bytes(),
                         (self.mod / "installlog.rtf").read_bytes()[:8])
        self.assertIn(b"truncated to 8", self.log_path.read_bytes())

    def test_missing_installer_log_is_noted(self):
        operation = gui_operation(self.wine)
        operation["actions"][0]["timeout"] = 0.5
        self.assertFails("nowindow", "window did not appear", operation=operation)
        self.assertFalse(list(self.work.glob("installer-log-*")))
        self.assertIn(b"installer log not present after failure", self.log_path.read_bytes())

    def test_wait_log_reports_windows_and_screenshots_before_giving_up(self):
        operation = gui_operation(self.wine)
        operation["actions"][-1]["wait_log"] = 1
        with mock.patch.object(manual, "GUI_WINDOW_REPORT_SECONDS", 0.2):
            self.assertFails("nolog", "did not settle within 1 seconds", operation=operation)
        text = self.log_path.read_bytes()
        self.assertGreaterEqual(text.count(b"waiting for success log"), 2)
        self.assertIn(b"log absent", text)
        self.assertIn(b"visible window 4194305: Fake Installer", text)
        self.assertTrue((self.work / "gui-failure-1.png").exists())
        self.assertIn(["wineserver", "-k"], self.calls())

    def test_unchanged_manifest_fails(self):
        self.assertFails("nochange", "patcher produced no game manifest changes")

    def test_wait_window_timeout_fails_with_diagnostics(self):
        operation = gui_operation(self.wine)
        operation["actions"][0]["timeout"] = 0.5
        self.assertFails("nowindow", "window did not appear", operation=operation)
        calls = self.calls()
        self.assertFalse(any(call[:2] == ["xdotool", "key"] for call in calls))
        self.assertIn(["wineserver", "-k"], calls)
        self.assertTrue(any(call[0] == "import" and call[1:3] == ["-window", "root"] for call in calls))
        self.assertTrue((self.work / "gui-failure-1.png").exists())
        self.assertIn(b"[gui_patcher] action", self.log_path.read_bytes())

    def test_step_deadline_bounds_waits(self):
        operation = gui_operation(self.wine)
        operation["actions"][0]["timeout"] = 600
        started = time.monotonic()
        self.assertFails("nowindow", "step timed out", operation=operation, timeout=1)
        self.assertLess(time.monotonic() - started, 30)

    def test_wrong_patcher_identity_is_rejected_before_launch(self):
        with self.assertRaises(manual.Blocked) as caught:
            self.run_gui("success", digest="f" * 64)
        self.assertIn("patcher SHA256 mismatch", str(caught.exception))
        self.assertFalse(any(call[0] == "wine" and call[1] != "reg" for call in self.calls()))

    def test_missing_tool_fails_clearly(self):
        (self.bin / "xdotool").unlink()
        self.assertFails("success", "xdotool executable missing", path=str(self.bin))


class Rename(Fixture):
    def rename(self, source, destination, **options):
        manual.execute(dict({"type": "rename", "source": source, "destination": destination}, **options),
                       self.roots(), None, time.monotonic() + 60)

    def assertRenameBlocked(self, message, *arguments, **options):
        with self.assertRaises(manual.Blocked) as caught:
            self.rename(*arguments, **options)
        self.assertIn(message, str(caught.exception))

    def test_renames_file_and_creates_parent(self):
        (self.game / "override" / "w_ionrfl_04.mdl").write_text("m", encoding="utf-8")
        self.rename("{game}/override/w_ionrfl_04.mdl", "{game}/override/w_ionrfl_004.mdl")
        self.assertEqual(sorted(p.name for p in (self.game / "override").iterdir()), ["w_ionrfl_004.mdl"])
        self.rename("{game}/override", "{game}/backup/override")
        self.assertTrue((self.game / "backup" / "override" / "w_ionrfl_004.mdl").is_file())

    def test_existing_destination_needs_replace_and_must_be_a_file(self):
        (self.game / "override" / "a.mdl").write_text("new", encoding="utf-8")
        (self.game / "override" / "b.mdl").write_text("old", encoding="utf-8")
        self.assertRenameBlocked("rename destination exists", "{game}/override/a.mdl", "{game}/override/b.mdl")
        self.assertRenameBlocked("rename destination exists", "{game}/override/a.mdl", "{game}/override/b.mdl",
                                 replace=False)
        self.rename("{game}/override/a.mdl", "{game}/override/b.mdl", replace=True)
        self.assertEqual((self.game / "override" / "b.mdl").read_text(encoding="utf-8"), "new")
        self.assertFalse((self.game / "override" / "a.mdl").exists())
        (self.game / "dir").mkdir()
        self.assertRenameBlocked("never a directory", "{game}/override/b.mdl", "{game}/dir", replace=True)
        self.assertRenameBlocked("never a directory", "{game}/dir", "{game}/override/b.mdl", replace=True)

    def test_validation(self):
        self.assertRenameBlocked("rename source missing", "{game}/missing", "{game}/other")
        self.assertRenameBlocked("wrong root", "{archives}/a", "{game}/b")
        self.assertRenameBlocked("wrong root", "{game}/a", "{extract}/b")
        self.assertRenameBlocked("replace must be boolean", "{game}/vanilla.txt", "{game}/b", replace=1)
        self.assertRenameBlocked("invalid object fields", "{game}/vanilla.txt", "{game}/b", extra=1)
        self.assertRenameBlocked("must differ", "{game}/vanilla.txt", "{game}/vanilla.txt")
        self.assertRenameBlocked("outside its source", "{game}/override", "{game}/override/inner")
        self.assertRenameBlocked("cannot replace root", "{game}/vanilla.txt", "{game}/")


if __name__ == "__main__":
    unittest.main()
