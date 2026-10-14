"""CLI contract tests use mocked gh only; no external writes or real credentials."""
from contextlib import redirect_stderr, redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("private_ci_cli", Path(__file__).resolve().parents[1] / "src" / "cli.py")
cli = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cli)
SHA = "a" * 40
SOURCE_SHA = "b" * 40


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="private-ci-test-")
        self.root = Path(self.temp.name)
        self.target = self.root / "companion"
        self.target.mkdir()
        self.private_output = self.root / "private-config.json"
        self.arguments = ["init", "--library", "owner/mod-builds", "--ref", SHA,
                          "--private-repo", "owner/private-source", "--private-ref", SOURCE_SHA,
                          "--command-cli", "private-cli-secret-command", "--command-gui", "private-gui-secret-command",
                          "--directory", str(self.target), "--config-output", str(self.private_output)]

    def tearDown(self):
        self.temp.cleanup()

    def invoke(self, arguments):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = cli.main(arguments)
        return result, stdout.getvalue(), stderr.getvalue()

    def test_init_writes_six_pinned_callers_without_private_configuration(self):
        with patch.object(cli, "gh") as gh:
            result, out, err = self.invoke(self.arguments)
        self.assertEqual(result, 0, err)
        gh.assert_not_called()
        files = sorted((self.target / ".github" / "workflows").glob("*.yml"))
        self.assertEqual(len(files), 6)
        for path in files:
            content = path.read_text()
            self.assertIn(f"owner/mod-builds/.github/workflows/modsync-private-job.yml@{SHA}", content)
            self.assertIn('tool-repository: "owner/mod-builds"', content)
            self.assertIn(f'tool-ref: "{SHA}"', content)
            self.assertIn("PRIVATE_CI_CONFIG: ${{ secrets.PRIVATE_CI_CONFIG }}", content)
            self.assertNotIn("owner/private-source", content)
            self.assertNotIn("secret-command", content)
            self.assertNotIn(SOURCE_SHA, content)
        self.assertNotIn("secret-command", out + err)
        self.assertEqual(json.loads(self.private_output.read_text())["artifactPath"], "artifacts/evidence")
        self.assertEqual(json.loads(self.private_output.read_text())["commands"]["macos"]["gui"], "private-gui-secret-command")
        if os.name != "nt":
            self.assertEqual(self.private_output.stat().st_mode & 0o777, 0o600)

    def test_per_platform_config_and_vendored_library_paths(self):
        config = self.root / "input.json"
        config.write_text(json.dumps({"privateRepository": "owner/private-source", "ref": SOURCE_SHA,
                                      "commands": {platform: {kind: f"{platform}-{kind}" for kind in cli.KINDS} for platform in cli.PLATFORMS}}))
        arguments = ["init", "--library", "owner/mod-builds", "--ref", SHA, "--config", str(config),
                     "--directory", str(self.target), "--config-output", str(self.private_output),
                     "--workflow-path", ".github/workflows/modsync-private-job.yml", "--tool-path", "tools/private-ci",
                     "--runner", "self-hosted"]
        result, _, err = self.invoke(arguments)
        self.assertEqual(result, 0, err)
        content = (self.target / ".github/workflows/private-ci-gui-windows.yml").read_text()
        self.assertIn("modsync-private-job.yml@" + SHA, content)
        self.assertIn('tool-path: "tools/private-ci"', content)
        self.assertIn("default: self-hosted", content)
        self.assertEqual(json.loads(self.private_output.read_text())["commands"]["windows"]["gui"], "windows-gui")

    def test_rejects_mutable_library_ref_before_writes(self):
        arguments = self.arguments.copy()
        arguments[arguments.index("--ref") + 1] = "main"
        result, _, err = self.invoke(arguments)
        self.assertEqual(result, 2)
        self.assertIn("immutable", err)
        self.assertFalse((self.target / ".github").exists())
        self.assertFalse(self.private_output.exists())

    def test_rejects_private_config_output_inside_public_repo(self):
        arguments = self.arguments.copy()
        arguments[arguments.index("--config-output") + 1] = str(self.target / "private.json")
        result, _, err = self.invoke(arguments)
        self.assertEqual(result, 2)
        self.assertIn("outside", err)
        self.assertEqual(list(self.target.iterdir()), [])

    def test_existing_caller_failure_does_not_write_other_workflows(self):
        workflows = self.target / ".github/workflows"
        workflows.mkdir(parents=True)
        existing = workflows / "private-ci-cli-linux.yml"
        existing.write_text("preserve this\n")
        result, _, _ = self.invoke(self.arguments)
        self.assertEqual(result, 2)
        self.assertEqual(existing.read_text(), "preserve this\n")
        self.assertEqual(list(workflows.iterdir()), [existing])
        self.assertFalse(self.private_output.exists())

    def test_secret_configuration_only_explicit_and_values_only_on_stdin(self):
        token_file = self.root / "token.txt"
        token = "FAKE_TOKEN_FOR_TEST_ONLY"
        token_file.write_text(token)
        calls = []
        with patch.object(cli, "gh", side_effect=lambda arguments, secret_input=None: calls.append((arguments, secret_input)) or ""):
            result, out, err = self.invoke(self.arguments + ["--configure-secrets", "--repo", "owner/mod-builds",
                                                            "--token-file", str(token_file), "--recipient", "age1-public-recipient"])
        self.assertEqual(result, 0, err)
        self.assertEqual([call[0][2] for call in calls], ["PRIVATE_CI_CONFIG", "PRIVATE_CI_TOKEN", "PRIVATE_CI_RECIPIENT"])
        self.assertEqual(calls[1][1], token)
        self.assertTrue(all(token not in " ".join(call[0]) for call in calls))
        self.assertNotIn(token, out + err)
        self.assertNotIn("private-cli-secret-command", out + err)

    def test_missing_secret_inputs_fail_before_local_changes(self):
        with patch.object(cli, "gh") as gh:
            result, _, _ = self.invoke(self.arguments + ["--configure-secrets", "--repo", "owner/mod-builds"])
        self.assertEqual(result, 2)
        gh.assert_not_called()
        self.assertFalse((self.target / ".github").exists())

    def test_run_dispatches_exact_public_companion_workflow(self):
        with patch.object(cli, "gh", return_value="") as gh:
            result, _, err = self.invoke(["run", "--repo", "owner/mod-builds", "--platform", "macos", "--kind", "gui",
                                         "--runner", "self-hosted", "--branch", "reviewed-branch"])
        self.assertEqual(result, 0, err)
        gh.assert_called_once_with(["workflow", "run", "private-ci-gui-macos.yml", "--repo", "owner/mod-builds",
                                    "--field", "runner-mode=self-hosted", "--ref", "reviewed-branch"])

    def test_dispatch_explicit_implementation_only_for_opted_in_callers(self):
        for implementation in ["rust", "typescript", "both"]:
            with self.subTest(implementation=implementation), patch.object(cli, "gh", return_value="") as gh:
                result, _, err = self.invoke(["run", "--repo", "owner/mod-builds", "--platform", "linux", "--kind", "cli",
                                             "--implementation", implementation])
            self.assertEqual(result, 0, err)
            self.assertEqual(gh.call_args.args[0][-2:], ["--field", f"implementation={implementation}"])

    def test_run_rejects_repository_injection_before_gh(self):
        with patch.object(cli, "gh") as gh:
            result, _, _ = self.invoke(["run", "--repo", "owner/repo;echo token", "--platform", "linux", "--kind", "cli"])
        self.assertEqual(result, 2)
        gh.assert_not_called()

    def test_gh_failure_does_not_relay_secret_stderr(self):
        completed = subprocess.CompletedProcess(["gh"], 1, stdout="FAKE_SECRET", stderr="FAKE_SECRET")
        with patch.object(cli.subprocess, "run", return_value=completed):
            result, out, err = self.invoke(["run", "--repo", "owner/mod-builds", "--platform", "linux", "--kind", "cli"])
        self.assertEqual(result, 2)
        self.assertNotIn("FAKE_SECRET", out + err)

    def test_status_filters_exact_caller_and_returns_json(self):
        with patch.object(cli, "gh", return_value='[{"databaseId":123,"status":"completed","conclusion":"success"}]') as gh:
            result, out, err = self.invoke(["status", "--repo", "owner/mod-builds", "--platform", "windows", "--kind", "cli"])
        self.assertEqual(result, 0, err)
        self.assertEqual(json.loads(out)[0]["databaseId"], 123)
        self.assertEqual(gh.call_args.args[0][-2:], ["--workflow", "private-ci-cli-windows.yml"])

    def test_rejects_artifact_paths_outside_executor_receipt_allowlist(self):
        for value in ["artifacts", "../source", "/tmp/evidence", "C:\\source", "artifacts/evidence/../source"]:
            with self.subTest(value=value), patch.object(cli, "gh") as gh:
                result, _, _ = self.invoke(self.arguments + ["--artifact-path", value])
            self.assertEqual(result, 2)
            gh.assert_not_called()
            self.assertFalse((self.target / ".github").exists())
            self.assertFalse(self.private_output.exists())

    def test_rejects_nul_commands_without_printing_payload(self):
        arguments = self.arguments.copy()
        arguments[arguments.index("--command-cli") + 1] = "private-payload\0command"
        result, out, err = self.invoke(arguments)
        self.assertEqual(result, 2)
        self.assertNotIn("private-payload", out + err)
        self.assertFalse(self.private_output.exists())

    def test_rejects_mutable_private_source_ref_before_writes(self):
        arguments = self.arguments.copy()
        arguments[arguments.index("--private-ref") + 1] = "main"
        result, _, _ = self.invoke(arguments)
        self.assertEqual(result, 2)
        self.assertFalse((self.target / ".github").exists())
        self.assertFalse(self.private_output.exists())

    def test_rejects_vendored_tool_path_expressions_and_traversal(self):
        for value in ["../private-ci", "tools/../private-ci", "${{ secrets.TOKEN }}", "/tools/private-ci"]:
            with self.subTest(value=value):
                result, _, _ = self.invoke(self.arguments + ["--tool-path", value])
                self.assertEqual(result, 2)
                self.assertFalse((self.target / ".github").exists())

    def test_status_requires_a_complete_filter(self):
        with patch.object(cli, "gh") as gh:
            result, _, _ = self.invoke(["status", "--repo", "owner/mod-builds", "--platform", "linux"])
        self.assertEqual(result, 2)
        gh.assert_not_called()


if __name__ == "__main__":
    unittest.main()
