"""Credential-free admission and controlled SteamCMD process tests."""
import importlib.util
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
    for name in ("chitin.key", "dialog.tlk"):
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
                directory.mkdir()
                command = directory / "steamcmd.sh"
                command.write_text("inert")
                return command
            def run(command, script_path, log, environment, timeout):
                arguments = [str(command), "+runscript", str(script_path)]
                kwargs = {"env": environment, "stdin": subprocess.DEVNULL, "stdout": log, "stderr": log}
                calls.append(arguments)
                self.assertNotIn("secret-password", str(arguments))
                self.assertNotIn("secret-password", str(kwargs["env"]))
                self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
                self.assertIs(kwargs["stdout"], kwargs["stderr"])
                script_path = Path(arguments[-1])
                if os.name != "nt":
                    self.assertEqual(script_path.stat().st_mode & 0o777, 0o600)
                content = script_path.read_text()
                self.assertIn('"secret-password"', content)
                game = "k1" if "app_update 32370" in content else "k2"
                installed(root / "games" / game, steam.APPS[game])
                return 0
            with patch.object(steam, "bootstrap", bootstrap), patch.object(steam, "run_steam", run):
                result = steam.acquire(root, root / "private.log", "linux",
                                       {"PATH":os.environ.get("PATH", ""), "STEAM_USERNAME":"user", "STEAM_PASSWORD":"secret-password", "GH_TOKEN":"also-secret"})
            self.assertEqual(len(calls), 2)
            self.assertEqual(set(result), {"k1", "k2"})
            self.assertFalse(list(root.glob("steam-private-*")))
            self.assertTrue(all(path.is_dir() for path in result.values()))
            if os.name != "nt":
                self.assertEqual((root / "private.log").stat().st_mode & 0o777, 0o600)

    def test_failed_login_never_claims_installation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            def bootstrap(host, directory):
                directory.mkdir()
                return directory / "steamcmd.sh"
            with patch.object(steam, "bootstrap", bootstrap), patch.object(steam, "run_steam", return_value=1), patch.object(steam, "verify_install") as verify:
                with self.assertRaises(RuntimeError):
                    steam.acquire(root, root / "private.log", "linux", {"STEAM_USERNAME":"user", "STEAM_PASSWORD":"password"})
                verify.assert_not_called()
            self.assertFalse(list(root.glob("steam-private-*")))

    @unittest.skipIf(os.name == "nt", "POSIX process-group ownership")
    def test_timeout_stops_owned_process_group(self):
        process = Mock(pid=123456)
        process.wait.side_effect = [subprocess.TimeoutExpired("steamcmd", 1), 0]
        process.poll.return_value = None
        log = Mock()
        with patch.object(steam.subprocess, "Popen", return_value=process) as popen, patch.object(steam.os, "killpg") as killpg:
            with self.assertRaises(subprocess.TimeoutExpired):
                steam.run_steam(Path("steamcmd.sh"), Path("private.script"), log, {"PATH":"safe"}, 1)
            self.assertTrue(popen.call_args.kwargs["start_new_session"])
            self.assertIs(popen.call_args.kwargs["stdout"], log)
            self.assertEqual(popen.call_args.kwargs["stdin"], subprocess.DEVNULL)
            killpg.assert_called_once_with(123456, steam.signal.SIGKILL)
            process.kill.assert_called_once()

    def test_bootstrap_traversal_refused(self):
        for path in ("../secret", "/secret", "linux32/../../secret", "linux32\\secret"):
            with self.assertRaises(ValueError):
                steam.archive_path(path, Path("private"))


if __name__ == "__main__":
    unittest.main()
