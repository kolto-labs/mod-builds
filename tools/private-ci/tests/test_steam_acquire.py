"""Credential-free admission and controlled SteamCMD process tests."""
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

SPEC = importlib.util.spec_from_file_location("steam_acquire", Path(__file__).parents[1] / "src" / "steam_acquire.py")
steam = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(steam)


def installed(directory, app_id, flags="4"):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "steamapps").mkdir(exist_ok=True)
    (directory / "steamapps" / f"appmanifest_{app_id}.acf").write_text(
        f'"AppState" {{ "appid" "{app_id}" "StateFlags" "{flags}" }}')
    executable = "swkotor.exe" if app_id == steam.APPS["k1"] else "swkotor2.exe"
    for name in ("chitin.key", "dialog.tlk", executable):
        (directory / name).write_bytes(b"controlled-resource")
    (directory / "modules").mkdir(exist_ok=True)
    (directory / "modules" / "controlled.mod").write_bytes(b"controlled-resource")


class SteamAcquireTests(unittest.TestCase):
    def test_scripts_force_windows_and_fixed_owned_apps(self):
        for game, app_id in steam.APPS.items():
            content = steam.script(game, Path("game with spaces"), "user", "p a;ss", "A1B2C")
            self.assertIn('@sSteamCmdForcePlatformType windows\n', content)
            self.assertIn(f'app_update {app_id} validate\n', content)
            self.assertIn('login "user" "p a;ss" "A1B2C"\n', content)
            self.assertLess(content.index("force_install_dir"), content.index("login"))
            self.assertTrue(content.endswith("quit\n"))

    def test_command_injection_refused(self):
        for value in ('x\nquit', 'x\rquit', 'x" quit', 'x\\"', 'x\0', ''):
            with self.subTest(value=repr(value)), self.assertRaises(ValueError):
                steam.token(value)
        for code in ('123456', 'A B C', 'ABCD\n'):
            with self.assertRaises(ValueError):
                steam.script("k1", Path("game"), "user", "password", code)

    def test_missing_credentials_and_mac_fail_before_network(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(steam, "bootstrap") as bootstrap:
            root = Path(directory)
            for environment, host in (({}, "linux"), ({"STEAM_USERNAME":"u", "STEAM_PASSWORD":"p"}, "macos")):
                with self.assertRaises(ValueError):
                    steam.acquire(root / "work", root / "private.log", host, environment)
            bootstrap.assert_not_called()

    def test_manifest_and_actual_resources_required(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            installed(root / "k1", 32370)
            steam.verify_install("k1", root / "k1", root / "steam")
            (root / "k1" / "dialog.tlk").unlink()
            with self.assertRaises(RuntimeError):
                steam.verify_install("k1", root / "k1", root / "steam")
            installed(root / "k1", 32370, "1024")
            with self.assertRaises(RuntimeError):
                steam.verify_install("k1", root / "k1", root / "steam")

    def test_secrets_only_private_script_and_private_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            calls = []
            def bootstrap(host, directory):
                directory.mkdir(parents=True)
                command = directory / "steamcmd.sh"
                command.write_text("inert")
                return command
            def run(command, script_path, log, environment, timeout):
                arguments = [str(command), "+runscript", str(script_path)]
                calls.append(arguments)
                self.assertNotIn("secret-password", str(arguments))
                self.assertNotIn("secret-password", str(environment))
                self.assertNotIn("also-secret", str(environment))
                self.assertFalse(any(key.startswith("STEAM_") or key == "GH_TOKEN" for key in environment))
                self.assertEqual(Path(environment["HOME"]), root / "steam-state" / "home")
                self.assertEqual(log.name, os.devnull)
                script_path = Path(arguments[-1])
                if os.name != "nt":
                    self.assertEqual(script_path.stat().st_mode & 0o777, 0o600)
                content = script_path.read_text()
                self.assertIn('"secret-password"', content)
                game = "k1" if "app_update 32370" in content else "k2"
                baseline = root / "steam-state" / f"baseline-{game}"
                self.assertIn(f"force_install_dir {steam.token(str(baseline.resolve()), path=True)}\n", content)
                installed(baseline, steam.APPS[game])
                return 0
            with patch.object(steam, "bootstrap", bootstrap), patch.object(steam, "run_steam", run):
                result = steam.acquire(root, root / "private.log", "linux",
                                       {"PATH":os.environ.get("PATH", ""), "STEAM_USERNAME":"user", "STEAM_PASSWORD":"secret-password", "GH_TOKEN":"also-secret"})
            self.assertEqual(len(calls), 2)
            self.assertEqual(set(result), {"k1", "k2"})
            self.assertTrue(all(not Path(arguments[-1]).exists() for arguments in calls))
            self.assertFalse(list((root / "steam-state").glob("request-*")))
            self.assertTrue(all(path.is_dir() for path in result.values()))
            for game, destination in result.items():
                report = json.loads((root / f"{game}-acquisition.json").read_text())
                self.assertTrue(report["success"])
                self.assertEqual(steam.inventory(destination), report["files"])
                self.assertFalse((destination / "steamapps").exists())
            for path in root.rglob("*"):
                if path.is_file():
                    self.assertNotIn(b"secret-password", path.read_bytes())
                    self.assertNotIn(b"also-secret", path.read_bytes())
            if os.name != "nt":
                self.assertEqual((root / "private.log").stat().st_mode & 0o777, 0o600)

    def test_failed_login_never_claims_installation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            def bootstrap(host, directory):
                directory.mkdir(parents=True)
                return directory / "steamcmd.sh"
            with patch.object(steam, "bootstrap", bootstrap), patch.object(steam, "run_steam", return_value=1), patch.object(steam, "verify_install") as verify:
                with self.assertRaises(RuntimeError):
                    steam.acquire(root, root / "private.log", "linux", {"STEAM_USERNAME":"user", "STEAM_PASSWORD":"password"})
                verify.assert_not_called()
            self.assertFalse(list((root / "steam-state").glob("request-*")))
            self.assertFalse((root / "games").exists())

    @unittest.skipIf(os.name == "nt", "POSIX process-group ownership")
    def test_timeout_stops_owned_process_group(self):
        process = Mock(pid=123456, stdout=io.BytesIO(b"Controlled output\n"))
        process.wait.return_value = 0
        process.poll.return_value = None
        log = io.BytesIO()
        with patch.object(steam.subprocess, "Popen", return_value=process) as popen, patch.object(steam.os, "killpg") as killpg, patch("time.monotonic", side_effect=[0, 2]):
            with self.assertRaises(subprocess.TimeoutExpired):
                steam.run_steam(Path("steamcmd.sh"), Path("private.script"), log, {"PATH":"safe"}, 1)
            self.assertTrue(popen.call_args.kwargs["start_new_session"])
            self.assertEqual(popen.call_args.kwargs["stdout"], subprocess.PIPE)
            self.assertEqual(popen.call_args.kwargs["stderr"], subprocess.STDOUT)
            self.assertEqual(popen.call_args.kwargs["stdin"], subprocess.DEVNULL)
            killpg.assert_called_once_with(123456, steam.signal.SIGKILL)
            process.kill.assert_called_once()
            process.wait.assert_called_once()
            self.assertTrue(process.stdout.closed)
            self.assertEqual(log.getvalue(), b"Controlled output\n")

    @unittest.skipIf(os.name == "nt", "POSIX process-group ownership")
    def test_prompts_and_login_failures_stop_owned_process_group(self):
        for output, category in (
            (b"Enter Steam Guard code:", "guard_challenge"),
            (b"Please approve login in the Steam mobile app", "confirmation_required"),
            (b"Invalid password", "invalid_login"),
            (b"No subscription", "no_subscription"),
        ):
            with self.subTest(category=category):
                process = Mock(pid=123456, stdout=io.BytesIO(output))
                process.poll.return_value = None
                log = io.BytesIO()
                with patch.object(steam.subprocess, "Popen", return_value=process), patch.object(steam.os, "killpg") as killpg:
                    with self.assertRaisesRegex(RuntimeError, f"^SteamCMD: {category}$"):
                        steam.run_steam(Path("steamcmd.sh"), Path("private.script"), log, {"PATH":"safe"}, 1)
                killpg.assert_called_once_with(123456, steam.signal.SIGKILL)
                process.kill.assert_called_once()
                process.wait.assert_called_once()
                self.assertTrue(process.stdout.closed)
                self.assertEqual(log.getvalue(), output)

    def test_bootstrap_traversal_refused(self):
        for path in ("../secret", "/secret", "linux32/../../secret", "linux32\\secret"):
            with self.assertRaises(ValueError):
                steam.archive_path(path, Path("private"))


if __name__ == "__main__":
    unittest.main()
