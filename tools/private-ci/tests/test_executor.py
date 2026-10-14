import contextlib
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from src import executor


REVISION = "a" * 40
RECIPIENT = "age1" + "a" * 58
TOKEN = "PRIVATE_DOWNLOAD_CREDENTIAL_SENTINEL"
PAYLOAD = "PRIVATE_COMPILER_SOURCE_SENTINEL"


def raw_config(command="true", **changes):
    config = {"privateRepository": "owner/private-project", "ref": REVISION,
              "commands": {platform: {"cli": command, "gui": command} for platform in executor.PLATFORMS},
              "artifactPath": "artifacts/evidence", "timeoutMinutes": 45}
    config.update(changes)
    return json.dumps(config)


class ExecutorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.age = self.root / "age"
        self.age.touch()
        self.archive = self.root / "snapshot.zip"
        with zipfile.ZipFile(self.archive, "w") as output:
            output.writestr("repository-root/", "")
            output.writestr("repository-root/source.txt", "private source bytes")

    def tearDown(self):
        self.temp.cleanup()

    def environment(self, command="true"):
        return {**os.environ, "PRIVATE_CI_CONFIG": raw_config(command),
                "PRIVATE_CI_TOKEN": TOKEN, "PRIVATE_CI_RECIPIENT": RECIPIENT,
                "GH_TOKEN": "UNRELATED_GH_TOKEN", "GITHUB_TOKEN": "UNRELATED_GITHUB_TOKEN",
                "ACTIONS_RUNTIME_TOKEN": "UNRELATED_RUNTIME_TOKEN", "CUSTOM_SECRET": "UNRELATED_SECRET",
                "STEAM_USERNAME": "PRIVATE_STEAM_USER", "STEAM_PASSWORD": "PRIVATE_STEAM_PASSWORD",
                "STEAM_GUARD_CODE": "PRIVATE_STEAM_GUARD",
                "GITHUB_WORKSPACE": str(self.root / "public-corpus")}

    def mock_tools(self, captured):
        def invoke(argv, **kwargs):
            if argv[0] == "gh":
                self.assertEqual(argv, ["gh", "api", f"repos/owner/private-project/zipball/{REVISION}"])
                self.assertEqual(kwargs["env"]["GH_TOKEN"], TOKEN)
                self.assertNotIn("PRIVATE_CI_CONFIG", kwargs["env"])
                self.assertNotIn(TOKEN, " ".join(argv))
                kwargs["stdout"].write(self.archive.read_bytes())
                kwargs["stderr"].write(("Private download diagnostics " + TOKEN).encode())
            else:
                self.assertEqual(argv[0], str(self.age.resolve()))
                self.assertNotIn("GH_TOKEN", kwargs["env"])
                self.assertNotIn("PRIVATE_CI_TOKEN", kwargs["env"])
                with tarfile.open(argv[-1]) as archive:
                    captured.update({member.name: archive.extractfile(member).read()
                                     for member in archive.getmembers() if member.isfile()})
                Path(argv[argv.index("--output") + 1]).write_bytes(b"age-encrypted-mock-ciphertext")
            return subprocess.CompletedProcess(argv, 0)
        return invoke

    def test_config_requires_pinned_revision_and_relative_artifact_path(self):
        for changes in [{"ref": "main"}, {"ref": "a" * 39}, {"privateRepository": "../private"},
                        {"artifactPath": "../secrets"}, {"artifactPath": "/absolute"},
                        {"artifactPath": "C:\\outside"}, {"timeoutMinutes": True}, {"timeoutMinutes": 0}, {"timeoutMinutes": 181}]:
            with self.subTest(changes=changes), self.assertRaises((ValueError, TypeError)):
                executor.parse_config(raw_config(**changes), "linux", "cli")

    def test_environment_has_no_tokens_or_arbitrary_secrets(self):
        environment = executor.build_environment(self.environment(), self.root / "empty-gh", REVISION)
        for key in ["GH_TOKEN", "GITHUB_TOKEN", "PRIVATE_CI_TOKEN", "PRIVATE_CI_CONFIG",
                    "PRIVATE_CI_RECIPIENT", "ACTIONS_RUNTIME_TOKEN", "CUSTOM_SECRET",
                    "STEAM_USERNAME", "STEAM_PASSWORD", "STEAM_GUARD_CODE"]:
            self.assertNotIn(key, environment)
        self.assertEqual(environment["PRIVATE_CI_SOURCE_SHA"], REVISION)
        self.assertEqual(environment["PUBLIC_CI_WORKSPACE"], str(self.root / "public-corpus"))
        self.assertEqual(environment.get("HOME"), os.environ.get("HOME"))

    def test_network_proxy_settings_are_preserved_while_authentication_secrets_are_removed(self):
        source = self.environment()
        proxies = {
            "HTTP_PROXY": "http://approved-network-proxy:8080",
            "https_proxy": "http://approved-network-proxy:8080",
            "All_Proxy": "socks5://approved-network-proxy:1080",
            "no_proxy": "localhost,127.0.0.1,.internal.example",
        }
        source.update(proxies)
        environment = executor.build_environment(source, self.root / "empty-gh", REVISION)
        for key, value in proxies.items():
            self.assertEqual(environment[key], value)
        for key in ("GH_TOKEN", "GITHUB_TOKEN", "PRIVATE_CI_TOKEN", "STEAM_PASSWORD", "ACTIONS_RUNTIME_TOKEN"):
            self.assertNotIn(key, environment)

    def test_snapshot_rejects_zip_slip_links_and_collisions_before_writes(self):
        bad_entries = [[("root/../escape.txt", "bad")], [("/absolute.txt", "bad")],
                       [("root/folder\\escape.txt", "bad")], [("root/safe\0hidden.exe", "bad")], [("root/file", "one"), ("root/file/child", "two")],
                       [("root/a", "one"), ("root/A", "two")], [("first/a", "one"), ("second/b", "two")]]
        for index, entries in enumerate(bad_entries):
            archive = self.root / f"bad-{index}.zip"
            with zipfile.ZipFile(archive, "w") as output:
                for name, value in entries:
                    # ZipInfo's constructor sanitizes separators on Windows.
                    # Set the raw header name afterward so this fixture remains
                    # malformed on every platform, including when re-read.
                    raw = zipfile.ZipInfo("placeholder")
                    raw.filename = name
                    raw.orig_filename = name
                    output.writestr(raw, value)
            with zipfile.ZipFile(archive) as source:
                self.assertEqual([entry.orig_filename for entry in source.infolist()], [name for name, _ in entries])
            destination = self.root / f"out-{index}"
            with self.subTest(entries=entries), self.assertRaises(ValueError):
                executor.extract_snapshot(archive, destination)
            self.assertFalse(destination.exists())
        archive = self.root / "symlink.zip"
        with zipfile.ZipFile(archive, "w") as output:
            link = zipfile.ZipInfo("root/link")
            link.create_system = 3
            link.external_attr = 0o120777 << 16
            output.writestr(link, "../outside")
        with self.assertRaises(ValueError):
            executor.extract_snapshot(archive, self.root / "link-out")

    def test_portable_paths_and_unicode_collisions_are_rejected_before_any_snapshot_write(self):
        malformed = [
            [("root/.. /escape.txt", "bad")], [("root/.. ./escape.txt", "bad")],
            [("root/folder./file.txt", "bad")], [("root/folder /file.txt", "bad")],
            [("root/CON.txt", "bad")], [("root/NUL", "bad")], [("root/COM1.log", "bad")],
            [("root/LPT².txt", "bad")], [("root/AUX .log", "bad")],
            [("root/question?.txt", "bad")], [("root/control\x7f.txt", "bad")],
            [("root/café.txt", "one"), ("root/cafe\u0301.txt", "two")],
        ]
        for index, entries in enumerate(malformed):
            archive = self.root / f"portable-{index}.zip"
            with zipfile.ZipFile(archive, "w") as output:
                # A valid earlier entry proves the whole manifest is preflighted.
                output.writestr("root/earlier.txt", "must not be written")
                for name, value in entries:
                    raw = zipfile.ZipInfo("placeholder")
                    raw.filename = name
                    raw.orig_filename = name
                    output.writestr(raw, value)
            destination = self.root / f"portable-output-{index}"
            with self.subTest(entries=entries):
                with self.assertRaises(ValueError):
                    executor.extract_snapshot(archive, destination)
                self.assertFalse(destination.exists())
                with self.assertRaises(ValueError):
                    executor.validate_trace_archive(archive)

    @unittest.skipIf(os.name == "nt", "POSIX owner-only executable permissions")
    def test_snapshot_preserves_executable_bit_with_owner_only_file_permissions(self):
        archive = self.root / "executable.zip"
        with zipfile.ZipFile(archive, "w") as output:
            script = zipfile.ZipInfo("root/script.sh")
            script.create_system = 3
            script.external_attr = 0o100755 << 16
            output.writestr(script, "#!/bin/sh\nprintf portable-executable\n")
            ordinary = zipfile.ZipInfo("root/data.txt")
            ordinary.create_system = 3
            ordinary.external_attr = 0o100644 << 16
            output.writestr(ordinary, "ordinary file")
        destination = self.root / "executable-output"
        executor.extract_snapshot(archive, destination)
        self.assertEqual(stat.S_IMODE((destination / "script.sh").stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((destination / "data.txt").stat().st_mode), 0o600)
        result = subprocess.run([str(destination / "script.sh")], capture_output=True, check=True)
        self.assertEqual(result.stdout, b"portable-executable")

    def test_raw_names_are_rejected_even_when_windows_reader_normalizes_them(self):
        archive = self.root / "raw-reader.zip"
        raw_name = "root/folder\\escape.txt"
        with zipfile.ZipFile(archive, "w") as output:
            raw = zipfile.ZipInfo("placeholder")
            raw.filename = raw_name
            raw.orig_filename = raw_name
            output.writestr(raw, "malformed raw path")
        original_init = zipfile.ZipInfo.__init__
        def windows_init(entry, *args, **kwargs):
            original_init(entry, *args, **kwargs)
            entry.filename = entry.filename.replace("\\", "/")
        # Emulate the Windows reader's normalization on Linux/macOS too.
        # The original header name remains available in orig_filename.
        with patch.object(zipfile.ZipInfo, "__init__", windows_init):
            with zipfile.ZipFile(archive) as source:
                entry = source.infolist()[0]
                self.assertEqual(entry.filename, "root/folder/escape.txt")
                self.assertEqual(entry.orig_filename, raw_name)
            destination = self.root / "raw-reader-output"
            with self.assertRaises(ValueError):
                executor.extract_snapshot(archive, destination)
            self.assertFalse(destination.exists())
            with self.assertRaises(ValueError):
                executor.validate_trace_archive(archive)

    @unittest.skipUnless(os.name != "nt" and shutil.which("bash"), "Requires native POSIX bash")
    def test_real_command_output_is_private_and_only_ciphertext_remains(self):
        child = "import os,pathlib; print('" + PAYLOAD + "'); assert 'PRIVATE_CI_TOKEN' not in os.environ; assert 'GH_TOKEN' not in os.environ; assert 'GITHUB_TOKEN' not in os.environ; p=pathlib.Path('artifacts/evidence'); p.mkdir(parents=True); (p/'result.txt').write_text('sensitive result')"
        command = shlex.quote(sys.executable) + " -c " + shlex.quote(child)
        captured = {}
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, self.environment(command), clear=True), patch.object(executor.subprocess, "run", side_effect=self.mock_tools(captured)), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = executor.main(["--platform", "linux", "--kind", "cli", "--output", str(self.root / "outputs"), "--age", str(self.age)])
        self.assertEqual(code, 0)
        self.assertEqual(stdout.getvalue(), "Private CI execution completed.\n")
        self.assertEqual(stderr.getvalue(), "")
        self.assertNotIn(TOKEN, stdout.getvalue() + stderr.getvalue())
        self.assertNotIn(PAYLOAD, stdout.getvalue() + stderr.getvalue())
        self.assertIn(PAYLOAD.encode(), captured["build.log"])
        self.assertEqual(captured["artifacts/result.txt"], b"sensitive result")
        self.assertEqual(json.loads(captured["evidence.json"])["sourceRevision"], REVISION)
        self.assertEqual([path.name for path in (self.root / "outputs").iterdir()], ["evidence.age"])

    @unittest.skipUnless(os.name != "nt" and shutil.which("bash"), "Requires native POSIX bash")
    def test_original_failure_code_is_retained_with_encrypted_evidence(self):
        captured = {}
        with patch.object(executor.subprocess, "run", side_effect=self.mock_tools(captured)):
            code = executor.execute("linux", "cli", self.root / "failed", self.age,
                                    self.environment(f"printf '{PAYLOAD}\\n' >&2; exit 23"))
        self.assertEqual(code, 23)
        self.assertEqual(json.loads(captured["evidence.json"])["exitCode"], 23)
        self.assertIn(PAYLOAD.encode(), captured["build.log"])
        self.assertTrue((self.root / "failed/evidence.age").is_file())

    @unittest.skipUnless(os.name != "nt" and shutil.which("bash"), "Requires native POSIX bash")
    def test_timeout_stops_process_group_and_records_timeout(self):
        workspace = self.root / "workspace"
        workspace.mkdir()
        config_dir = self.root / "empty-gh"
        config_dir.mkdir()
        private = self.root / "private"
        private.mkdir()
        command = shlex.quote(sys.executable) + " -c " + shlex.quote("import time; print('private timed output', flush=True); time.sleep(30)")
        config = executor.Config("owner/private-project", REVISION, command, "artifacts/evidence", 0.1)
        code, timed_out = executor.run_command(config, "linux", workspace, private,
                                              executor.build_environment(self.environment(), config_dir, REVISION))
        self.assertEqual(code, 124)
        self.assertTrue(timed_out)
        self.assertIn("private timed output", (private / "build.log").read_text())

    def test_interrupted_wait_kills_only_owned_process_tree_before_propagating(self):
        workspace = self.root / "interrupt-workspace"
        workspace.mkdir()
        private = self.root / "interrupt-private"
        private.mkdir()
        config = executor.parse_config(raw_config(), "linux", "cli")
        process = unittest.mock.Mock(pid=987654)
        process.wait.side_effect = [KeyboardInterrupt(PAYLOAD), 0, 0]
        process.poll.return_value = 0
        with patch.object(executor.subprocess, "Popen", return_value=process), patch.object(executor.subprocess, "run") as taskkill:
            if os.name == "nt":
                with self.assertRaises(KeyboardInterrupt):
                    executor.run_command(config, "linux", workspace, private, {})
                self.assertEqual(taskkill.call_args.args[0], ["taskkill", "/pid", "987654", "/t", "/f"])
                self.assertEqual(taskkill.call_args.kwargs["timeout"], 5)
            else:
                with patch.object(executor.os, "killpg") as kill_group:
                    with self.assertRaises(KeyboardInterrupt):
                        executor.run_command(config, "linux", workspace, private, {})
                    self.assertEqual(kill_group.call_args_list, [unittest.mock.call(987654, signal.SIGTERM), unittest.mock.call(987654, signal.SIGKILL)])
            self.assertGreaterEqual(process.wait.call_count, 2)
            self.assertTrue(all(call.kwargs.get("timeout") == 5 for call in process.wait.call_args_list[1:]))

    def test_sigterm_cancellation_has_generic_output_and_restores_previous_handler(self):
        previous = signal.getsignal(signal.SIGTERM)
        stdout, stderr = io.StringIO(), io.StringIO()
        def interrupt(*_args, **_kwargs):
            signal.raise_signal(signal.SIGTERM)
        with patch.object(executor, "execute", side_effect=interrupt), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = executor.main(["--platform", "linux", "--kind", "cli", "--output", str(self.root / "cancelled"), "--age", str(self.age)])
        self.assertEqual(code, 130)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "Private CI execution cancelled.\n")
        self.assertNotIn(PAYLOAD, stderr.getvalue())
        self.assertEqual(signal.getsignal(signal.SIGTERM), previous)

    def test_artifact_size_limits_and_external_symlinks_are_rejected(self):
        workspace = self.root / "bounded-workspace"
        artifact = workspace / "artifacts/evidence"
        artifact.mkdir(parents=True)
        private = self.root / "bounded-private"
        private.mkdir()
        (private / "evidence.json").write_text("{}")
        payload = artifact / "too-large.txt"
        payload.write_bytes(b"x" * 32)
        config = executor.parse_config(raw_config(), "linux", "cli")
        with patch.object(executor, "MAX_EVIDENCE_FILE_BYTES", 16), self.assertRaises(ValueError):
            executor.bundle_evidence(private, workspace, config)
        payload.unlink()
        if os.name != "nt":
            outside = self.root / "outside-secret.txt"
            outside.write_text(TOKEN)
            (artifact / "link.txt").symlink_to(outside)
            with self.assertRaises(ValueError):
                executor.bundle_evidence(private, workspace, config)
        # Logs-only fallback must not enumerate invalid external artifacts.
        with tarfile.open(executor.bundle_evidence(private, workspace, config, include_artifacts=False)) as archive:
            self.assertEqual(archive.getnames(), ["evidence.json"])

    def test_evidence_allowlist_rejects_game_bytes_and_baseline_directories(self):
        workspace = self.root / "evidence-workspace"
        artifact = workspace / "artifacts/evidence"
        artifact.mkdir(parents=True)
        private = self.root / "evidence-private"
        private.mkdir()
        config = executor.parse_config(raw_config(), "linux", "cli")
        for name in ("game/readme.txt", "Modules/receipt.json", "SteamApps/info.log", "baseline/list.json", "payload.bif", "payload.exe", "unapproved.zip"):
            payload = artifact / name
            payload.parent.mkdir(parents=True, exist_ok=True)
            payload.write_text("must not upload")
            with self.subTest(name=name), self.assertRaises(ValueError):
                executor.bundle_evidence(private, workspace, config)
            shutil.rmtree(artifact)
            artifact.mkdir()
        (artifact / "actualgameproof.json").write_text("{}")
        (artifact / "baseline-manifest.json").write_text("{}")
        with zipfile.ZipFile(artifact / "trace.zip", "w") as trace:
            trace.writestr("0.trace", '{"type":"context-options"}')
            trace.writestr("resources/screenshot.png", b"image")
        executor.bundle_evidence(private, workspace, config)
        with zipfile.ZipFile(artifact / "trace.zip", "w") as trace:
            trace.writestr("resources/game.bif", b"game bytes")
        with self.assertRaises(ValueError):
            executor.bundle_evidence(private, workspace, config)

    def test_windows_recipe_enables_native_failure_propagation_without_token_env(self):
        private = self.root / "pwsh-private"
        private.mkdir()
        workspace = self.root / "pwsh-workspace"
        workspace.mkdir()
        config = executor.parse_config(raw_config("Write-Output 'private output'"), "windows", "cli")
        process = unittest.mock.Mock()
        process.wait.return_value = 23
        with patch.object(executor.subprocess, "Popen", return_value=process) as launch:
            code, timed_out = executor.run_command(config, "windows", workspace, private,
                executor.build_environment(self.environment(), private / "empty-gh", REVISION))
        self.assertEqual(code, 23)
        self.assertFalse(timed_out)
        self.assertEqual(launch.call_args.args[0][:4], ["pwsh", "-NoLogo", "-NoProfile", "-NonInteractive"])
        self.assertNotIn("GH_TOKEN", launch.call_args.kwargs["env"])
        script = (private / "command.ps1").read_text()
        self.assertIn("$PSNativeCommandUseErrorActionPreference = $true", script)
        self.assertIn("exit $LASTEXITCODE", script)

    def test_download_failure_still_encrypts_diagnostics_without_console_leak(self):
        captured = {}
        tool = self.mock_tools(captured)
        def fail_download(argv, **kwargs):
            if argv[0] == "gh":
                kwargs["stderr"].write((TOKEN + " private failure").encode())
                return subprocess.CompletedProcess(argv, 17)
            return tool(argv, **kwargs)
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, self.environment(), clear=True), patch.object(executor.subprocess, "run", side_effect=fail_download), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = executor.main(["--platform", "linux", "--kind", "cli", "--output", str(self.root / "failure"), "--age", str(self.age)])
        self.assertEqual(code, 1)
        self.assertNotIn(TOKEN, stdout.getvalue() + stderr.getvalue())
        self.assertIn(TOKEN.encode(), captured["download.log"])
        self.assertEqual(stderr.getvalue(), "Private CI execution failed; encrypted evidence retained.\n")

    def test_encryption_failure_removes_partial_output_and_prints_only_generic_error(self):
        def fail_age(argv, **kwargs):
            if argv[0] == "gh":
                kwargs["stdout"].write(self.archive.read_bytes())
                return subprocess.CompletedProcess(argv, 0)
            Path(argv[argv.index("--output") + 1]).write_bytes(b"partial")
            kwargs["stderr"].write(PAYLOAD.encode())
            return subprocess.CompletedProcess(argv, 1)
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, self.environment(), clear=True), patch.object(executor.subprocess, "run", side_effect=fail_age), patch.object(executor, "run_command", return_value=(0, False)), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = executor.main(["--platform", "linux", "--kind", "cli", "--output", str(self.root / "partial"), "--age", str(self.age)])
        self.assertEqual(code, 1)
        self.assertEqual(stderr.getvalue(), "Private CI execution failed.\n")
        self.assertEqual(list((self.root / "partial").iterdir()), [])

    def test_encryption_timeout_also_removes_partial_ciphertext(self):
        output = self.root / "timeout-output"
        output.mkdir()
        private = self.root / "timeout-private"
        private.mkdir()
        archive = private / "evidence.tar.gz"
        archive.write_bytes(b"private evidence")
        def expire(argv, **kwargs):
            Path(argv[argv.index("--output") + 1]).write_bytes(b"partial ciphertext")
            raise subprocess.TimeoutExpired(argv, 180)
        with patch.object(executor.subprocess, "run", side_effect=expire), self.assertRaises(subprocess.TimeoutExpired):
            executor.encrypt_evidence(self.age, RECIPIENT, archive, output, private, {})
        self.assertEqual(list(output.iterdir()), [])

    def test_invalid_config_and_invocation_do_not_echo_secret_values(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, {"PRIVATE_CI_CONFIG": TOKEN}, clear=True), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = executor.main(["--platform", "linux", "--kind", "cli", "--output", str(self.root / "bad"), "--age", str(self.age)])
            bad_invocation = executor.main(["--platform", TOKEN])
        self.assertEqual(code, 1)
        self.assertEqual(bad_invocation, 1)
        self.assertNotIn(TOKEN, stdout.getvalue() + stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
