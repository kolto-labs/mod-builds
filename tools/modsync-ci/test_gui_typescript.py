import argparse
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock

import gui_diag
import gui_typescript

LEAK = "ghp_" + "a1B2c3D4e5" * 4


class WorkerFailureTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        for relative in ("packages/server/src/createServer.ts", "packages/web/dist/index.html",
                         "node_modules/tsx/dist/loader.mjs", "node_modules/playwright/package.json"):
            (self.root / "source" / relative).parent.mkdir(parents=True, exist_ok=True)
            (self.root / "source" / relative).write_text("", encoding="utf-8")
        for name in ("game", "archives"):
            (self.root / name).mkdir()
        (self.root / "input.toml").write_text('[[thisMod]]\nGuid = "x"\n', encoding="utf-8")
        self.args = argparse.Namespace(
            source=self.root / "source", input=self.root / "input.toml", game_dir=self.root / "game",
            archives=self.root / "archives", work_dir=self.root / "work", timeout=60.0)

    def run_execute(self, log_text, code, phase=None, report=False, expire=False):
        result = {}

        class Process:
            returncode = code
            pid = 0

            def __init__(self, command, **kwargs):
                kwargs["stdout"].write(log_text.encode())
                kwargs["stdout"].flush()
                private = Path(json.loads(Path(kwargs["env"]["MODSYNC_GUI_CONFIG"]).read_text())["private"])
                if phase:
                    gui_diag.mark_phase(private, *phase)
                if report:
                    (private / "worker-result.json").write_text(
                        json.dumps({"status": "blocked", "reason": "visible_control_disabled"}),
                        encoding="utf-8")

            def communicate(self, data, timeout=None):
                if expire:
                    raise gui_typescript.subprocess.TimeoutExpired("node", timeout)

            def wait(self, timeout=None):
                return code

        with mock.patch.object(gui_typescript.shutil, "which", return_value="/usr/bin/node"), \
                mock.patch.object(gui_typescript.subprocess, "Popen", Process), \
                mock.patch.object(gui_typescript.os, "killpg", side_effect=ProcessLookupError):
            with self.assertRaises(gui_typescript.Blocked) as caught:
                gui_typescript.execute(self.args, result)
        return result, str(caught.exception)

    def test_missing_report_adds_redacted_tail_exit_and_phase(self):
        result, reason = self.run_execute(
            "Error: browserType.launch: Executable doesn't exist\nBearer %s\n" % LEAK, 1,
            phase=("first_screenshot", "launch"))
        self.assertEqual(reason, "ui_worker_failed_see_private_driver_log")
        diagnostics = result["worker_diagnostics"]
        self.assertEqual((diagnostics["phase"], diagnostics["exit_code"]), ("first_screenshot", 1))
        self.assertFalse(diagnostics["worker_result_present"])
        self.assertEqual(result["stage"], "launch")
        tail = "\n".join(diagnostics["tails"]["worker"])
        self.assertIn("Executable doesn't exist", tail)
        self.assertNotIn(LEAK, tail)

    def test_worker_that_never_started_is_reported_as_launch(self):
        result, _ = self.run_execute("", 1)
        self.assertEqual(result["worker_diagnostics"]["phase"], "launch")
        self.assertEqual(result["stage"], "launch")

    def test_worker_report_is_kept_and_timeouts_are_marked(self):
        result, reason = self.run_execute("x\n", 0, phase=("install", "Load"), report=True)
        self.assertEqual(reason, "visible_control_disabled")
        self.assertTrue(result["worker_diagnostics"]["worker_result_present"])
        result, reason = self.run_execute("x\n", None, expire=True)
        self.assertEqual(reason, "ui_timeout_recovery_may_be_required")
        self.assertTrue(result["worker_diagnostics"]["timed_out"])


if __name__ == "__main__":
    unittest.main()
