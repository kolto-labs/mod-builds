import json
from pathlib import Path
import shutil
import tempfile
import unittest

import gui_diag

HOME = "/home/someone"
ENV = {"HOME": HOME, "RUNNER_TEMP": "/home/runner/work/_temp", "GITHUB_TOKEN": "tokenvalue-12345678"}
GITHUB_PAT = "github_pat_" + "A1b2C3d4E5" * 5
CLASSIC = "ghp_" + "a1B2c3D4e5" * 4
AWS = "AKIA" + "ABCDEFGHIJKLMNOP"
BLOB = "Zm9vYmFyYmF6" * 6


def scrub(text, **kwargs):
    return gui_diag.redact(text, home=HOME, env=ENV, **kwargs)


class RedactTests(unittest.TestCase):
    def test_home_and_runner_paths_are_shortened(self):
        self.assertEqual(scrub("open /home/someone/.cache/x failed"), "open ~/.cache/x failed")
        self.assertEqual(scrub("at /home/runner/work/_temp/gui-work/a.png"), "at $RUNNER_TEMP/gui-work/a.png")
        self.assertEqual(scrub("cd /home/runner/work/mod-builds/mod-builds/src"), "cd $WORKSPACE/src")
        self.assertEqual(scrub("see /Users/dev/Library/x"), "see ~/Library/x")

    def test_known_paths_use_their_labels_longest_first(self):
        paths = {"/mnt/work": "$WORK_DIR", "/mnt/work/gui-rust-ab12": "$EVIDENCE"}
        self.assertEqual(scrub("x /mnt/work/gui-rust-ab12/driver.log y /mnt/work/other", paths=paths),
                         "x $EVIDENCE/driver.log y $WORK_DIR/other")

    def test_a_path_that_only_shares_a_prefix_is_left_alone(self):
        self.assertEqual(gui_diag.redact("/mnt/workshop/x", paths={"/mnt/work": "$WORK_DIR"}, env={}),
                         "/mnt/workshop/x")

    def test_token_shapes_are_masked(self):
        for secret in (GITHUB_PAT, CLASSIC, AWS):
            masked = scrub("fatal: bad credential %s in header" % secret)
            self.assertNotIn(secret, masked)
            self.assertIn(gui_diag.REDACTED, masked)

    def test_key_value_secrets_are_masked_and_the_name_is_kept(self):
        for name in ("key", "api_key", "token", "secret", "password", "ACCESS_TOKEN"):
            masked = scrub("%s=%s next" % (name, BLOB))
            self.assertNotIn(BLOB, masked)
            self.assertIn(name + "=" + gui_diag.REDACTED, masked)
        self.assertNotIn(BLOB, scrub('{"token": "%s"}' % BLOB))

    def test_authorization_headers_are_masked_whole(self):
        for line in ("Authorization: Bearer abcdef0123456789", "authorization: Basic dXNlcjpwYXNz",
                     "> Authorization: token ghs_whatever", '"Authorization": "Bearer abcdef0123456789"'):
            masked = scrub(line)
            self.assertIn(gui_diag.REDACTED, masked)
            self.assertNotRegex(masked, r"abcdef0123456789|dXNlcjpwYXNz|whatever")
        self.assertEqual(scrub("curl -H x Bearer abcdef0123456789"), "curl -H x Bearer " + gui_diag.REDACTED)

    def test_url_credentials_are_masked(self):
        masked = scrub("fetch https://user:hunter2hunter2@example.org/a")
        self.assertNotIn("hunter2", masked)
        self.assertIn("example.org/a", masked)

    def test_secret_environment_values_are_masked_anywhere(self):
        self.assertEqual(scrub("value=tokenvalue-12345678!").count("tokenvalue"), 0)

    def test_ordinary_text_is_untouched(self):
        line = "xvfb-run: error: Xvfb failed to start (screen 0 1600x1200x24), basic information"
        self.assertEqual(scrub(line), line)
        self.assertEqual(scrub("key=short"), "key=short")

    def test_redaction_is_idempotent(self):
        once = scrub("token=%s at /home/someone/x %s" % (BLOB, CLASSIC))
        self.assertEqual(scrub(once), once)


class TailTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)

    def test_tail_is_bounded_redacted_and_keeps_the_end(self):
        log = self.root / "driver.log"
        log.write_text("\n".join("line %d /home/someone/f" % number for number in range(500)) + "\n",
                       encoding="utf-8")
        lines = gui_diag.tail_lines(log, home=HOME, env=ENV)
        self.assertEqual(len(lines), gui_diag.TAIL_LINES)
        self.assertEqual(lines[-1], "line 499 ~/f")
        self.assertEqual(lines[0], "line %d ~/f" % (500 - gui_diag.TAIL_LINES))

    def test_secret_split_by_the_read_window_never_leaks(self):
        log = self.root / "driver.log"
        log.write_bytes(b"x" * (gui_diag.READ_LIMIT + 10) + b"\nok " + CLASSIC.encode() + b"\n")
        text = "\n".join(gui_diag.tail_lines(log, env=ENV))
        self.assertNotIn(CLASSIC, text)
        self.assertNotIn("ghp_", text.replace("[REDACTED]", ""))

    def test_long_lines_are_truncated_and_control_bytes_dropped(self):
        log = self.root / "driver.log"
        log.write_bytes(b"a\x1b[31mb\x00c\r\n" + b"z" * 5000 + b"\n")
        first, second = gui_diag.tail_lines(log, env={})
        self.assertEqual(first, "a[31mbc")
        self.assertEqual(len(second), gui_diag.LINE_LIMIT + 3)

    def test_missing_or_symlinked_logs_give_no_lines(self):
        self.assertEqual(gui_diag.tail_lines(self.root / "absent.log"), [])
        target = self.root / "real.log"
        target.write_text("secret-ish\n", encoding="utf-8")
        link = self.root / "link.log"
        link.symlink_to(target)
        self.assertEqual(gui_diag.tail_lines(link), [])


class PhaseAndExitTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)

    def test_no_phase_file_means_the_worker_died_during_launch(self):
        self.assertEqual(gui_diag.read_phase(self.root), ("launch", None))

    def test_phase_and_stage_round_trip(self):
        gui_diag.mark_phase(self.root, "install", "Run validation")
        self.assertEqual(gui_diag.read_phase(self.root), ("install", "Run validation"))
        gui_diag.mark_phase(self.root, "first_screenshot")
        self.assertEqual(gui_diag.read_phase(self.root), ("first_screenshot", None))

    def test_exit_status_decoding(self):
        self.assertEqual(gui_diag.describe_exit(0)["signal"], None)
        self.assertEqual(gui_diag.describe_exit(1)["signal"], None)
        killed = gui_diag.describe_exit(-9)
        self.assertEqual((killed["signal"], killed["signal_inferred"]), ("SIGKILL", False))
        shell = gui_diag.describe_exit(137)
        self.assertEqual((shell["signal"], shell["signal_inferred"]), ("SIGKILL", True))
        self.assertEqual(gui_diag.describe_exit(139)["signal"], "SIGSEGV")
        self.assertIsNone(gui_diag.describe_exit(None)["signal"])
        self.assertTrue(gui_diag.describe_exit(None, timed_out=True)["timed_out"])

    def test_collect_and_render_are_json_safe_bounded_and_inert(self):
        (self.root / "driver.log").write_text(
            "::error::boom\nxvfb-run: error: Xvfb failed to start\n" + "t=%s\n" % CLASSIC, encoding="utf-8")
        gui_diag.mark_phase(self.root, "preflight")
        diagnostics = gui_diag.collect(
            self.root, (("worker", "driver.log", 120),), code=1, worker_result=False,
            error=RuntimeError("bad token=%s" % BLOB), env=ENV)
        json.dumps(diagnostics)
        self.assertEqual((diagnostics["phase"], diagnostics["exit_code"]), ("preflight", 1))
        self.assertEqual(diagnostics["error_type"], "RuntimeError")
        lines = gui_diag.render_group(diagnostics)
        self.assertEqual(lines[0], "::group::gui worker tail")
        self.assertIn("::endgroup::", lines)
        body = "\n".join(lines)
        self.assertNotIn(CLASSIC, body)
        self.assertNotIn(BLOB, body)
        self.assertIn("[worker] ::error::boom", lines)
        self.assertFalse([line for line in lines[1:] if line.startswith("::") and line != "::endgroup::"])
        self.assertIn("phase=preflight", lines[1])
        self.assertIn("worker_result=absent", lines[1])


if __name__ == "__main__":
    unittest.main()
