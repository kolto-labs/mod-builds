import argparse
import contextlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import gui_diag
import gui_typescript
import ports

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
        owner = self

        class Process:
            returncode = code
            pid = 0

            def __init__(self, command, **kwargs):
                owner.worker_env = dict(kwargs["env"])
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

    def test_worker_environment_opts_in_to_the_ci_physical_install(self):
        for inherited in (None, "0"):
            with mock.patch.dict(gui_typescript.os.environ, clear=False):
                if inherited is None:
                    gui_typescript.os.environ.pop("MODSYNC_CI_PHYSICAL_INSTALL", None)
                else:
                    gui_typescript.os.environ["MODSYNC_CI_PHYSICAL_INSTALL"] = inherited
                self.run_execute("x\n", 1)
            self.assertEqual(self.worker_env["MODSYNC_CI_PHYSICAL_INSTALL"], "1", inherited)
        self.assertEqual(gui_typescript.PHYSICAL_INSTALL_ENV, "MODSYNC_CI_PHYSICAL_INSTALL")

    def test_wrapped_status_errors_are_joined_before_they_are_wrapped_or_printed(self):
        self.assertIn("text.replace(/\\s+/g, ' ')", gui_typescript.WORKER)
        error = "Independent TypeScript physical installation is unavailable\n" + "x" * 400 + "\ntrailing words"

        def fake_execute(args, result):
            result["install_failure"] = {"trigger": "backend_install_gate_or_failure", "error": error}
            raise gui_typescript.Blocked("backend_install_gate_or_failure")

        report = self.root / "report" / "gui.json"
        output = io.StringIO()
        with mock.patch.object(gui_typescript, "execute", fake_execute), \
                mock.patch.object(sys, "argv", [
                    "gui_typescript.py", "--source", str(self.args.source), "--input", str(self.args.input),
                    "--game-dir", str(self.args.game_dir), "--archives", str(self.args.archives),
                    "--work-dir", str(self.args.work_dir), "--report", str(report)]), \
                contextlib.redirect_stdout(output):
            self.assertEqual(gui_typescript.main(), 1)
        line = next(item for item in output.getvalue().splitlines() if "install failure:" in item)
        self.assertIn("error=Independent TypeScript physical installation is unavailable x", line)
        self.assertTrue(line.endswith("x trailing words"))


GUID_A = "11111111-2222-4333-8444-555555555555"
GUID_B = "66666666-7777-4888-9999-aaaaaaaaaaaa"


class ChoiceOptionTests(unittest.TestCase):
    def test_choice_pairs_are_split_on_the_first_equals_and_the_guid_is_lowercased(self):
        self.assertEqual(gui_typescript.parse_choice(GUID_A.upper() + "=0"), (GUID_A, "0"))
        self.assertEqual(gui_typescript.parse_choice(GUID_A + "=a=b"), (GUID_A, "a=b"))

    def test_malformed_choice_pairs_are_refused(self):
        for value in ("", "=0", GUID_A, GUID_A + "=", GUID_A + "=  "):
            with self.assertRaises(argparse.ArgumentTypeError, msg=value):
                gui_typescript.parse_choice(value)

    def test_duplicate_guids_must_agree(self):
        self.assertEqual(gui_typescript.collect_choices([(GUID_A, "0"), (GUID_B, "1"), (GUID_A, "0")]),
                         {GUID_A: "0", GUID_B: "1"})
        with self.assertRaises(argparse.ArgumentTypeError):
            gui_typescript.collect_choices([(GUID_A, "0"), (GUID_A, "1")])

    def run_main(self, extra):
        seen = {}

        def fake_execute(args, result):
            seen["choices"] = args.choices
            raise gui_typescript.Blocked("stop_after_parse")

        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root)
        argv = ["gui_typescript.py"]
        for name in ("source", "input", "game-dir", "archives", "work-dir"):
            argv += ["--" + name, str(root / name)]
        argv += ["--report", str(root / "report" / "gui.json")] + extra
        with mock.patch.object(gui_typescript, "execute", fake_execute), \
                mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(io.StringIO()):
            code = gui_typescript.main()
        return code, seen

    def test_repeated_choice_flags_reach_the_driver_as_a_guid_keyed_mapping(self):
        code, seen = self.run_main(["--choice", GUID_A.upper() + "=0", "--choice", GUID_B + "=yes"])
        self.assertEqual(code, 1)
        self.assertEqual(seen["choices"], {GUID_A: "0", GUID_B: "yes"})

    def test_without_choice_flags_the_mapping_is_empty(self):
        self.assertEqual(self.run_main([])[1]["choices"], {})

    def test_a_malformed_or_conflicting_choice_flag_exits_before_any_driver_work(self):
        for extra in (["--choice", "nonsense"], ["--choice", GUID_A + "=0", "--choice", GUID_A + "=1"]):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
                self.run_main(extra)
            self.assertEqual(caught.exception.code, 2)

    def test_the_limitations_no_longer_claim_prompts_always_need_an_operator(self):
        source = Path(gui_typescript.__file__).read_text(encoding="utf-8")
        self.assertNotIn("Interactive install prompts require operator choices", source)


@unittest.skipUnless(shutil.which("node"), "node is required to run the worker's matcher")
class ReviewedAnswerMatcherTests(unittest.TestCase):
    def match(self, prompt, choices):
        script = gui_typescript.REVIEWED_CHOICE_JS + (
            "const input = JSON.parse(process.argv[1]);"
            "process.stdout.write(JSON.stringify(reviewedAnswer(input.prompt, input.choices)));")
        done = subprocess.run(
            [shutil.which("node"), "--input-type=module", "-e", script,
             json.dumps({"prompt": prompt, "choices": choices})],
            capture_output=True, text=True, check=True)
        return json.loads(done.stdout)

    def prompt(self, **extra):
        value = {"id": "prompt-1", "kind": "choose-option", "title": "Pick", "instructionGuid": GUID_A.upper(),
                 "choices": [{"id": "alpha", "label": "First Option"}, {"id": "0", "label": "Second Option"}]}
        value.update(extra)
        return value

    def test_an_answer_matching_a_choice_id_is_posted_with_that_id(self):
        self.assertEqual(self.match(self.prompt(), {GUID_A: "0"}), {"promptId": "prompt-1", "choiceId": "0"})

    def test_an_answer_matching_a_label_ignoring_case_resolves_to_the_choice_id(self):
        self.assertEqual(self.match(self.prompt(), {GUID_A: "first option"}),
                         {"promptId": "prompt-1", "choiceId": "alpha"})

    def test_the_instruction_guid_is_compared_lowercased(self):
        self.assertIsNotNone(self.match(self.prompt(instructionGuid=GUID_A.upper()), {GUID_A: "alpha"}))

    def test_a_prompt_without_a_reviewed_answer_is_not_answered(self):
        self.assertIsNone(self.match(self.prompt(), {}))
        self.assertIsNone(self.match(self.prompt(), {GUID_B: "0"}))
        self.assertIsNone(self.match(self.prompt(instructionGuid=None), {GUID_A: "0"}))
        self.assertIsNone(self.match({"id": "prompt-1", "choices": []}, {GUID_A: "0"}))

    def test_an_answer_matching_no_choice_is_never_replaced_by_a_default(self):
        prompt = self.prompt(choices=[{"id": "alpha", "label": "First", "isDefault": True}])
        self.assertIsNone(self.match(prompt, {GUID_A: "missing"}))
        self.assertIsNone(self.match(self.prompt(choices=[]), {GUID_A: "0"}))

    def test_prototype_names_are_not_treated_as_answers(self):
        self.assertIsNone(self.match(self.prompt(instructionGuid="constructor"), {GUID_A: "0"}))


class WorkerWiringTests(unittest.TestCase):
    def test_the_worker_embeds_the_matcher_and_posts_to_the_answer_route(self):
        self.assertIn(gui_typescript.REVIEWED_CHOICE_JS, gui_typescript.WORKER)
        self.assertNotIn("__REVIEWED_CHOICE__", gui_typescript.WORKER)
        self.assertNotIn("__PROMPT_POLL_LIMIT__", gui_typescript.WORKER)
        self.assertIn("/api/install/answer", gui_typescript.WORKER)
        self.assertIn("deliberate_install_choice_requires_operator", gui_typescript.WORKER)

    def test_choices_are_written_to_the_worker_config(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root)
        for relative in ("packages/server/src/createServer.ts", "packages/web/dist/index.html",
                         "node_modules/tsx/dist/loader.mjs", "node_modules/playwright/package.json"):
            (root / "source" / relative).parent.mkdir(parents=True, exist_ok=True)
            (root / "source" / relative).write_text("", encoding="utf-8")
        for name in ("game", "archives"):
            (root / name).mkdir()
        (root / "input.toml").write_text('[[thisMod]]\nGuid = "x"\n', encoding="utf-8")
        args = argparse.Namespace(
            source=root / "source", input=root / "input.toml", game_dir=root / "game",
            archives=root / "archives", work_dir=root / "work", timeout=60.0, choices={GUID_A: "0"})
        written = {}

        class Process:
            returncode = 1
            pid = 0

            def __init__(self, command, **kwargs):
                config = Path(kwargs["env"]["MODSYNC_GUI_CONFIG"])
                written.update(json.loads(config.read_text()))

            def communicate(self, data, timeout=None):
                return None

            def wait(self, timeout=None):
                return 1

        with mock.patch.object(gui_typescript.shutil, "which", return_value="/usr/bin/node"), \
                mock.patch.object(gui_typescript.subprocess, "Popen", Process), \
                mock.patch.object(gui_typescript.os, "killpg", side_effect=ProcessLookupError), \
                self.assertRaises(gui_typescript.Blocked):
            gui_typescript.execute(args, {})
        self.assertEqual(written["choices"], {GUID_A: "0"})


class PortsForwardsChoicesTests(unittest.TestCase):
    def drive(self, choices):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root)
        converted = root / "converted.toml"
        converted.write_text('[[thisMod]]\nGuid = "%s"\nName = "Stand-in"\n' % GUID_A, encoding="utf-8")
        private, game, archives = root / "private", root / "game", root / "archives"
        for directory in (private, game, archives):
            directory.mkdir()
        commands = []

        def fake_run(command, source, private_dir, stage, report, env):
            commands.append(command)
            report["commands"].append({"stage": stage, "exit_code": 0})

        report = {"commands": []}
        options = {} if choices is None else {"choices": choices}
        with mock.patch.object(ports, "run", fake_run), \
                mock.patch.object(ports, "verify_typescript_gui_result", return_value={"reason": "ok"}):
            ports.install_gui_with_quarantine(
                ["node", "cli.js"], root, private, game, archives, converted, converted, set(), [],
                {ports.normalized_reference(GUID_A)}, report, {}, 0, 0.5, **options)
        return commands

    def test_the_reviewed_choices_are_appended_to_the_driver_command(self):
        choices = ["--choice", GUID_A + "=0", "--choice", GUID_B + "=yes"]
        command = self.drive(choices)[0]
        self.assertEqual(command[-4:], choices)
        self.assertEqual(command[command.index("--timeout") + 1], "7000")

    def test_no_choices_adds_no_choice_flags(self):
        for choices in (None, []):
            self.assertNotIn("--choice", self.drive(choices)[0])


if __name__ == "__main__":
    unittest.main()
