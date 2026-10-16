import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import types
import unittest
from unittest import mock
import zipfile

spec = importlib.util.spec_from_file_location("cache", Path(__file__).with_name("cache.py"))
cache = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cache)

MEGA_URL = "https://mega.nz/file/AAAAAAAA#BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"
DRIVE_URL = "https://drive.google.com/file/d/FILEID123/view"


def item_for(url):
    kind, direct = cache.provider(url)
    return {"id": cache.key_for(url), "url": url, "provider": kind, "direct": direct, "guides": []}


def args_for(**overrides):
    values = dict(download_timeout=30, integrity_timeout=30, max_bytes=10 ** 9, timeout=5, retries=0)
    values.update(overrides)
    return types.SimpleNamespace(**values)


def which_table(table):
    return lambda name: table.get(name)


REAL_RUN = subprocess.run


def completed(code):
    return subprocess.CompletedProcess([], code)


class ValidateFallbackTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.seven = Path(self.directory.name, "sample.7z")
        self.seven.write_bytes(b"7z\xbc\xaf\x27\x1c" + b"\0" * 64)
        self.rar = Path(self.directory.name, "sample.rar")
        self.rar.write_bytes(b"Rar!\x1a\x07\x00" + b"\0" * 64)

    def run_with(self, path, tools, codes):
        calls = []

        def fake_run(command, **kwargs):
            calls.append(os.path.basename(command[0]))
            outcome = codes[os.path.basename(command[0])]
            if isinstance(outcome, BaseException):
                raise outcome
            return completed(outcome)

        with mock.patch.object(cache.shutil, "which", which_table(tools)), \
                mock.patch.object(cache.subprocess, "run", fake_run):
            try:
                return cache.validate(path, 5), calls
            except Exception as error:
                error.calls = calls
                raise

    def test_prefers_7zz_first(self):
        kind, calls = self.run_with(self.seven, {"7zz": "/f/7zz", "7z": "/f/7z"}, {"7zz": 0, "7z": 0})
        self.assertEqual((kind, calls), ("7z", ["7zz"]))

    def test_segfault_retries_with_alternative_tool(self):
        kind, calls = self.run_with(self.seven, {"7zz": "/f/7zz", "7z": "/f/7z"}, {"7zz": -11, "7z": 0})
        self.assertEqual((kind, calls), ("7z", ["7zz", "7z"]))

    def test_rar_falls_through_to_unrar_then_bsdtar(self):
        tools = {"7z": "/f/7z", "unrar": "/f/unrar", "bsdtar": "/f/bsdtar"}
        kind, calls = self.run_with(self.rar, tools, {"7z": -11, "unrar": -11, "bsdtar": 0})
        self.assertEqual((kind, calls), ("rar", ["7z", "unrar", "bsdtar"]))

    def test_unrar_skipped_for_7z_payload(self):
        tools = {"7z": "/f/7z", "unrar": "/f/unrar", "bsdtar": "/f/bsdtar"}
        _, calls = self.run_with(self.seven, tools, {"7z": -11, "unrar": 1, "bsdtar": 0})
        self.assertEqual(calls, ["7z", "bsdtar"])

    def test_same_binary_not_retried(self):
        with tempfile.TemporaryDirectory() as directory:
            real = Path(directory, "7zz")
            real.write_text("")
            link = Path(directory, "7z")
            link.symlink_to(real)
            with self.assertRaises(ValueError) as caught:
                self.run_with(self.seven, {"7zz": str(real), "7z": str(link)}, {"7zz": -11, "7z": -11})
            self.assertEqual(caught.exception.calls, ["7zz"])

    def test_all_tools_crash_fails_closed(self):
        with self.assertRaises(ValueError) as caught:
            self.run_with(self.seven, {"7zz": "/f/7zz", "7z": "/f/7z"}, {"7zz": -11, "7z": -6})
        self.assertIn("crashed in every available tool", str(caught.exception))

    def test_genuine_corruption_is_not_retried(self):
        with self.assertRaises(subprocess.CalledProcessError) as caught:
            self.run_with(self.seven, {"7zz": "/f/7zz", "bsdtar": "/f/bsdtar"}, {"7zz": 2, "bsdtar": 0})
        self.assertEqual(caught.exception.returncode, 2)
        self.assertEqual(caught.exception.calls, ["7zz"])

    def test_no_tool_available_is_an_error(self):
        with self.assertRaises(ValueError) as caught:
            self.run_with(self.seven, {}, {})
        self.assertIn("7z or 7zz required", str(caught.exception))

    def test_timeout_propagates(self):
        with self.assertRaises(subprocess.TimeoutExpired):
            self.run_with(self.seven, {"7zz": "/f/7zz", "7z": "/f/7z"},
                          {"7zz": subprocess.TimeoutExpired("7zz", 5), "7z": 0})

    def test_html_payload_rejected(self):
        page = Path(self.directory.name, "page.rar")
        page.write_bytes(b"<html>blocked</html>")
        with self.assertRaises(ValueError):
            cache.validate(page, 5)


class ProviderResolveTests(unittest.TestCase):
    def test_nexus_message_is_explicit(self):
        item = item_for("https://www.nexusmods.com/kotor/mods/1234")
        with self.assertRaises(ValueError) as caught:
            cache.resolve(item, args_for())
        self.assertIn("NEXUS_API_KEY", str(caught.exception))
        self.assertIn("blocked-pending-user-action", str(caught.exception))

    def test_gamefront_still_unsupported(self):
        item = item_for("https://www.gamefront.com/games/some-game/file/some-file")
        with self.assertRaises(ValueError):
            cache.resolve(item, args_for())

    def test_mega_missing_binary_raises(self):
        with mock.patch.object(cache.shutil, "which", which_table({})):
            with self.assertRaises(ValueError) as caught:
                cache.resolve(item_for(MEGA_URL), args_for())
        self.assertIn("megatools", str(caught.exception))
        self.assertNotIn("http", str(caught.exception))

    def test_drive_missing_gdown_raises(self):
        with mock.patch.object(cache.shutil, "which", which_table({})), \
                mock.patch.object(cache.importlib.util, "find_spec", return_value=None):
            with self.assertRaises(ValueError) as caught:
                cache.resolve(item_for(DRIVE_URL), args_for())
        self.assertIn("gdown", str(caught.exception))

    def test_mega_uses_megatools_dl(self):
        with mock.patch.object(cache.shutil, "which", which_table({"megatools": "/f/megatools"})):
            self.assertEqual(cache.tool_command("mega", "/tmp/x", MEGA_URL),
                             ["/f/megatools", "dl", "--path", "/tmp/x", MEGA_URL])

    def test_mega_falls_back_to_megadl(self):
        with mock.patch.object(cache.shutil, "which", which_table({"megadl": "/f/megadl"})):
            self.assertEqual(cache.tool_command("mega", "/tmp/x", MEGA_URL),
                             ["/f/megadl", "--path", "/tmp/x", MEGA_URL])

    def test_gdown_module_fallback(self):
        with mock.patch.object(cache.shutil, "which", which_table({})), \
                mock.patch.object(cache.importlib.util, "find_spec", return_value=object()):
            command = cache.tool_command("google-drive", "/tmp/x", DRIVE_URL)
        self.assertEqual(command[1:3], ["-m", "gdown"])
        self.assertEqual(command[-2:], ["/tmp/x" + os.sep, DRIVE_URL])


class FetchWithToolTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root / "objects").mkdir()
        patcher = mock.patch.object(cache.shutil, "which",
                                    which_table({"megatools": "/f/megatools", "gdown": "/f/gdown"}))
        patcher.start()
        self.addCleanup(patcher.stop)

    def make_zip(self, path, payload=b"x" * 4096):
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("data.bin", payload)

    def fake_tool(self, filename, writer):
        def fake_run(command, **kwargs):
            if command[0] == cache.sys.executable and command[1] == "-c":
                return REAL_RUN(command, **kwargs)
            self.last_command, self.last_kwargs = command, kwargs
            directory = Path(command[command.index("--path") + 1] if "--path" in command
                             else command[command.index("-O") + 1])
            if filename:
                writer(directory / filename)
            return completed(0)
        return fake_run

    def test_mega_download_validated_and_stored(self):
        with mock.patch.object(cache.subprocess, "run", self.fake_tool("Texture Pack.zip", self.make_zip)):
            entry = cache.fetch_with_tool(item_for(MEGA_URL), self.root, args_for())
        self.assertEqual(self.last_command[1:3], ["dl", "--path"])
        self.assertEqual(self.last_kwargs["timeout"], 30)
        self.assertEqual(entry["extension"], "zip")
        self.assertEqual(entry["archive_name"], "Texture Pack.zip")
        self.assertTrue((self.root / "objects" / entry["sha256"]).is_file())
        self.assertEqual(entry["size"], (self.root / "objects" / entry["sha256"]).stat().st_size)
        self.assertNotIn("BBBB", entry["endpoint"])
        self.assertEqual([path.name for path in (self.root / "objects").iterdir()], [entry["sha256"]])

    def test_drive_download_uses_gdown(self):
        with mock.patch.object(cache.subprocess, "run", self.fake_tool("pack.zip", self.make_zip)):
            entry = cache.fetch_with_tool(item_for(DRIVE_URL), self.root, args_for())
        self.assertEqual(self.last_command[0], "/f/gdown")
        self.assertIn("--fuzzy", self.last_command)
        self.assertEqual(entry["status"], "verified")
        self.assertTrue(cache.local_valid(self.root, entry))

    def test_tiny_file_rejected_by_size_floor(self):
        def write_tiny(path):
            path.write_bytes(b"PK\x03\x04")
        with mock.patch.object(cache.subprocess, "run", self.fake_tool("a.zip", write_tiny)):
            with self.assertRaises(ValueError) as caught:
                cache.fetch_with_tool(item_for(MEGA_URL), self.root, args_for())
        self.assertIn("minimum archive size", str(caught.exception))

    def test_html_error_page_rejected(self):
        def write_html(path):
            path.write_bytes(b"<html>" + b"x" * 4096)
        with mock.patch.object(cache.subprocess, "run", self.fake_tool("a.zip", write_html)):
            with self.assertRaises(ValueError):
                cache.fetch_with_tool(item_for(DRIVE_URL), self.root, args_for())

    def test_corrupt_zip_rejected(self):
        def write_bad(path):
            path.write_bytes(b"PK\x03\x04" + b"\xff" * 4096)
        with mock.patch.object(cache.subprocess, "run", self.fake_tool("a.zip", write_bad)):
            with self.assertRaises(subprocess.CalledProcessError):
                cache.fetch_with_tool(item_for(MEGA_URL), self.root, args_for())

    def test_nonzero_exit_fails_without_leaking_url(self):
        with mock.patch.object(cache.subprocess, "run", return_value=completed(1)):
            with self.assertRaises(ValueError) as caught:
                cache.fetch_with_tool(item_for(MEGA_URL), self.root, args_for())
        self.assertNotIn("mega.nz", str(caught.exception))
        self.assertEqual(list((self.root / "objects").iterdir()), [])

    def test_no_output_file_fails(self):
        with mock.patch.object(cache.subprocess, "run", self.fake_tool(None, None)):
            with self.assertRaises(ValueError):
                cache.fetch_with_tool(item_for(MEGA_URL), self.root, args_for())

    def test_timeout_maps_to_download_timeout(self):
        with mock.patch.object(cache.subprocess, "run", side_effect=subprocess.TimeoutExpired("t", 1)):
            with self.assertRaises(ValueError) as caught:
                cache.fetch_with_tool(item_for(MEGA_URL), self.root, args_for())
        self.assertEqual(str(caught.exception), "total download timeout")

    def test_size_limit_enforced(self):
        with mock.patch.object(cache.subprocess, "run", self.fake_tool("a.zip", self.make_zip)):
            with self.assertRaises(ValueError):
                cache.fetch_with_tool(item_for(MEGA_URL), self.root, args_for(max_bytes=100))

    def test_refresh_one_dispatches_tool_providers(self):
        with mock.patch.object(cache.subprocess, "run", self.fake_tool("a.zip", self.make_zip)):
            entry = cache.refresh_one(item_for(MEGA_URL), {}, self.root, args_for())
        self.assertEqual(entry["evidence"], "download-without-upstream-metadata")


if __name__ == "__main__":
    unittest.main()
