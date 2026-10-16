import contextlib
import importlib.util
import io
import json
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
DS_PAGE = "https://deadlystream.com/files/file/123-example-post/"
DS_DOWNLOAD = DS_PAGE + "?do=download&csrfKey=abc"


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
        self.assertEqual((kind, calls), ("rar", ["unrar", "7z", "bsdtar"]))

    def test_rar_positive_failure_continues_to_remaining_tools(self):
        tools = {"7z": "/f/7z", "unrar": "/f/unrar", "bsdtar": "/f/bsdtar"}
        kind, calls = self.run_with(self.rar, tools, {"unrar": 3, "7z": 2, "bsdtar": 0})
        self.assertEqual((kind, calls), ("rar", ["unrar", "7z", "bsdtar"]))

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
        self.assertIn("failed in every available tool", str(caught.exception))

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
        self.assertNotIn("--fuzzy", self.last_command)
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


POST_URL = "https://deadlystream.com/files/file/123-example-post/"


class FakePage:
    def __init__(self, url, source):
        self.url = url
        self.source = source.encode()

    def geturl(self):
        return self.url

    def read(self, size=-1):
        return self.source


def row_html(name, number):
    return ('<li class="ipsDataItem"><div class="ipsDataItem_main"><h4 class="ipsDataItem_title">'
            '<a href="/files/file/123-example-post/?do=download&amp;r=%d&amp;confirm=1&amp;t=1&amp;csrfKey=key">%s</a>'
            '</h4></div></li>' % (number, name))


def selection_page(*names, extra=""):
    rows = "".join(row_html(name, 1000 + index) for index, name in enumerate(names))
    return "<html><body>%s<ul>%s</ul></body></html>" % (extra, rows)


def endpoint_for(source, selections):
    item = item_for(POST_URL)
    return cache.page_endpoint(FakePage(POST_URL, source), item, selections)


class ArchiveSelectionTests(unittest.TestCase):
    def test_single_row_needs_no_selection(self):
        endpoint = endpoint_for(selection_page("only.zip"), {})
        self.assertIn("r=1000", endpoint)

    def test_multi_row_uses_reviewed_selection(self):
        source = selection_page("first.zip", "second.7z", "third.rar")
        endpoint = endpoint_for(source, {"123": "second.7z"})
        self.assertIn("r=1001", endpoint)
        self.assertNotIn("r=1000", endpoint)

    def test_multi_row_without_selection_fails_closed_listing_candidates(self):
        source = selection_page("first.zip", "second.7z")
        with self.assertRaises(ValueError) as caught:
            endpoint_for(source, {})
        message = str(caught.exception)
        self.assertIn("no reviewed selection", message)
        self.assertIn("first.zip", message)
        self.assertIn("second.7z", message)

    def test_selection_for_other_post_does_not_apply(self):
        source = selection_page("first.zip", "second.7z")
        with self.assertRaises(ValueError) as caught:
            endpoint_for(source, {"999": "second.7z"})
        self.assertIn("no reviewed selection", str(caught.exception))

    def test_selection_naming_missing_file_fails_closed(self):
        source = selection_page("first.zip", "second.7z")
        with self.assertRaises(ValueError) as caught:
            endpoint_for(source, {"123": "absent.zip"})
        message = str(caught.exception)
        self.assertIn("matches 0 of the listed files", message)
        self.assertIn("first.zip", message)

    def test_selection_requires_exact_name_match(self):
        source = selection_page("first.zip", "second.7z")
        with self.assertRaises(ValueError):
            endpoint_for(source, {"123": "First.zip"})

    def test_duplicate_listed_names_fail_closed(self):
        source = selection_page("same.zip", "same.zip")
        with self.assertRaises(ValueError) as caught:
            endpoint_for(source, {"123": "same.zip"})
        self.assertIn("matches 2 of the listed files", str(caught.exception))

    def test_attachment_links_not_preferred_over_download_endpoint(self):
        extra = '<a href="/applications/core/interface/file/attachment.php?id=77">preview</a>'
        endpoint = endpoint_for(selection_page("first.zip", "second.7z", extra=extra), {"123": "first.zip"})
        self.assertIn("do=download", endpoint)
        self.assertIn("r=1000", endpoint)
        self.assertNotIn("attachment.php", endpoint)

    def test_landing_page_prefers_download_over_attachment_link(self):
        source = ('<html><body>'
                  '<a href="/applications/core/interface/file/attachment.php?id=77">image</a>'
                  '<a href="/files/file/123-example-post/?do=download&amp;csrfKey=key">Download</a>'
                  '</body></html>')
        endpoint = endpoint_for(source, {})
        self.assertIn("do=download", endpoint)
        self.assertNotIn("attachment.php", endpoint)

    def test_attachment_only_page_still_resolves_when_unique(self):
        source = '<html><body><a href="/applications/core/interface/file/attachment.php?id=77">file</a></body></html>'
        self.assertIn("attachment.php", endpoint_for(source, {}))

    def test_multiple_attachment_links_without_downloads_fail_closed(self):
        source = ('<html><body><a href="/applications/core/interface/file/attachment.php?id=1">a</a>'
                  '<a href="/applications/core/interface/file/attachment.php?id=2">b</a></body></html>')
        with self.assertRaises(ValueError):
            endpoint_for(source, {})


class LoadSelectionsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name, "selections.json")

    def load(self, text):
        self.path.write_text(text, encoding="utf-8")
        return cache.load_selections(self.path)

    def test_missing_file_means_no_selections(self):
        self.assertEqual(cache.load_selections(Path(self.directory.name, "absent.json")), {})

    def test_string_and_object_forms_with_url_keys(self):
        loaded = self.load('{"selections": {"11": "a.zip", '
                           '"https://deadlystream.com/files/file/22-example-post/": {"file": "b.7z", "reason": "why"}}}')
        self.assertEqual(loaded, {"11": "a.zip", "22": "b.7z"})

    def test_non_archive_selection_rejected(self):
        with self.assertRaises(ValueError):
            self.load('{"selections": {"11": "notes.txt"}}')

    def test_bad_key_rejected(self):
        with self.assertRaises(ValueError):
            self.load('{"selections": {"not-a-post": "a.zip"}}')

    def test_shipped_selections_are_well_formed(self):
        reviewed = cache.load_reviewed()
        for key, entry in reviewed["selections"].items():
            self.assertTrue(key.isdigit())
            if entry["loose"]:
                self.assertNotRegex(entry["file"], r"\.(zip|7z|rar)$")
            else:
                self.assertRegex(entry["file"], r"\.(zip|7z|rar)$")
        for key, entry in reviewed["tools"].items():
            self.assertRegex(key, r"^(google-drive|mega):[A-Za-z0-9_-]+$")
            self.assertTrue(entry["loose"])
        self.assertEqual(cache.load_selections(), {key: entry["file"] for key, entry in reviewed["selections"].items()})

    def test_shipped_loose_entries(self):
        reviewed = cache.load_reviewed()
        for key in ("2787", "2734"):
            self.assertTrue(reviewed["selections"][key]["loose"])
        loose_tools = [key for key, entry in reviewed["tools"].items() if entry["loose"]]
        self.assertEqual(len(loose_tools), 1)
        self.assertTrue(loose_tools[0].startswith("google-drive:"))
        for table in reviewed.values():
            for entry in table.values():
                if entry["loose"]:
                    self.assertEqual(cache.safe_archive_name(entry["file"], "file"), entry["file"])


class ValidateLooseTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def write(self, name, payload):
        path = self.root / name
        path.write_bytes(payload)
        return path

    def test_loose_binary_payloads_accepted_without_archive_magic(self):
        for payload in (b"MZ" + b"\x90" * 200, b"DLG V3.28" + b"\0" * 100, b"\x00\x00\x0a" + b"\x01" * 100):
            self.assertEqual(cache.validate(self.write("blob.bin", payload), 5, loose=True), "file")

    def test_loose_flag_is_required(self):
        with self.assertRaises(ValueError):
            cache.validate(self.write("blob.bin", b"MZ" + b"\x90" * 200), 5)

    def test_loose_html_payloads_rejected(self):
        for payload in (b"<!DOCTYPE html>" + b"x" * 100, b"<html>" + b"x" * 100, b"\xef\xbb\xbf  \n<HTML>" + b"x" * 100,
                        b"\r\n\t<?xml version='1.0'?>" + b"x" * 100):
            with self.assertRaises(ValueError) as caught:
                cache.validate(self.write("page.exe", payload), 5, loose=True)
            self.assertIn("rejected", str(caught.exception))

    def test_loose_size_floor(self):
        with self.assertRaises(ValueError) as caught:
            cache.validate(self.write("tiny.dlg", b"DLG"), 5, loose=True)
        self.assertIn("minimum size", str(caught.exception))

    def test_loose_does_not_run_archive_tools(self):
        with mock.patch.object(cache.subprocess, "run", side_effect=AssertionError("no tool run")):
            self.assertEqual(cache.validate(self.write("blob.tga", b"\x00" * 128), 5, loose=True), "file")


class SafeLooseNameTests(unittest.TestCase):
    def test_original_name_and_extension_kept(self):
        for name in ("loose_texture.tga", "Tool Setup v1.2.exe", "dialog_sample.dlg"):
            self.assertEqual(cache.safe_archive_name(name, "file"), name)

    def test_archive_and_extensionless_names_not_loose(self):
        for name in ("pack.zip", "pack.7z", "pack.RAR", "noextension", "trailing.", ".hidden", "x.toolongsuffix"):
            self.assertIsNone(cache.safe_archive_name(name, "file"))

    def test_path_components_and_reserved_names(self):
        self.assertEqual(cache.safe_archive_name("../../dir/evil.exe", "file"), "evil.exe")
        self.assertEqual(cache.safe_archive_name("NUL.tga", "file"), "_NUL.tga")

    def test_archive_names_unchanged_for_archives(self):
        self.assertEqual(cache.safe_archive_name("pack.zip", "zip"), "pack.zip")
        self.assertIsNone(cache.safe_archive_name("pack.exe", "zip"))
        self.assertIsNone(cache.safe_archive_name("pack.zip", "7z"))

    def test_archive_name_prefers_reviewed_and_checks_disposition(self):
        item = item_for(POST_URL)
        self.assertEqual(cache.archive_name(item, "file", POST_URL, None, "loose_texture.tga"), "loose_texture.tga")
        self.assertEqual(cache.archive_name(item, "file", POST_URL, 'attachment; filename="loose_texture.tga"',
                                            "loose_texture.tga"), "loose_texture.tga")
        with self.assertRaises(ValueError):
            cache.archive_name(item, "file", POST_URL, 'attachment; filename="other.tga"', "loose_texture.tga")


class LooseSelectionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name, "selections.json")

    def write(self, document):
        self.path.write_text(json.dumps(document), encoding="utf-8")
        return self.path

    def test_non_archive_name_only_accepted_when_loose(self):
        self.write({"selections": {"11": {"file": "loose_texture.tga", "loose": True}}})
        self.assertEqual(cache.load_selections(self.path), {"11": "loose_texture.tga"})
        for value in ({"file": "loose_texture.tga"}, {"file": "loose_texture.tga", "loose": False}, "loose_texture.tga"):
            self.write({"selections": {"11": value}})
            with self.assertRaises(ValueError):
                cache.load_selections(self.path)

    def test_loose_entries_must_be_plain_safe_non_archive_files(self):
        for value in ({"file": "pack.zip", "loose": True}, {"file": "noextension", "loose": True},
                      {"file": "dir/file.tga", "loose": True}, {"file": "bad:name.tga", "loose": True},
                      {"loose": True}, {"file": 5, "loose": True}, {"file": "a.tga", "loose": "yes"}):
            self.write({"selections": {"11": value}})
            with self.assertRaises(ValueError, msg=repr(value)):
                cache.load_reviewed(self.path)

    def test_tool_selections_keys_and_loose_requirement(self):
        self.write({"tool_selections": {
            "google-drive:FILEID123": {"file": "dialog_sample.dlg", "loose": True},
            DRIVE_URL.replace("FILEID123", "OTHERID456"): {"file": "second.dlg", "loose": True},
            "https://mega.nz/file/AAAAAAAA#KEY": {"file": "third.exe", "loose": True}}})
        tools = cache.load_reviewed(self.path)["tools"]
        self.assertEqual({key: entry["file"] for key, entry in tools.items()},
                         {"google-drive:FILEID123": "dialog_sample.dlg", "google-drive:OTHERID456": "second.dlg",
                          "mega:AAAAAAAA": "third.exe"})
        for document in ({"tool_selections": {"google-drive:FILEID123": {"file": "pack.zip"}}},
                         {"tool_selections": {"google-drive:FILEID123": "dialog_sample.dlg"}},
                         {"tool_selections": {"nexus:123": {"file": "a.dlg", "loose": True}}},
                         {"tool_selections": {"https://example.com/x": {"file": "a.dlg", "loose": True}}}):
            self.write(document)
            with self.assertRaises(ValueError):
                cache.load_reviewed(self.path)

    def test_tool_key(self):
        self.assertEqual(cache.tool_key(DRIVE_URL), "google-drive:FILEID123")
        self.assertEqual(cache.tool_key(MEGA_URL), "mega:AAAAAAAA")
        self.assertIsNone(cache.tool_key("https://mega.nz/#!AAAAAAAA!KEY"))
        self.assertIsNone(cache.tool_key(POST_URL))

    def test_reviewed_loose_lookup(self):
        self.write({"selections": {"123": {"file": "loose_texture.tga", "loose": True}, "124": "pack.zip"},
                    "tool_selections": {"google-drive:FILEID123": {"file": "dialog_sample.dlg", "loose": True}}})
        self.assertEqual(cache.reviewed_loose(item_for(POST_URL), self.path), "loose_texture.tga")
        self.assertEqual(cache.reviewed_loose(item_for(DRIVE_URL), self.path), "dialog_sample.dlg")
        self.assertIsNone(cache.reviewed_loose(item_for(MEGA_URL), self.path))
        self.assertIsNone(cache.reviewed_loose(item_for("https://deadlystream.com/files/file/124-other/"), self.path))
        self.assertIsNone(cache.reviewed_loose(item_for("https://deadlystream.com/files/file/999-other/"), self.path))
        self.assertIsNone(cache.reviewed_loose(item_for("https://example.com/a.zip"), self.path))

    def test_entry_loose_needs_manifest_extension_and_review(self):
        self.write({"selections": {"123": {"file": "loose_texture.tga", "loose": True}}})
        item = item_for(POST_URL)
        self.assertTrue(cache.entry_loose(item, {"extension": "file"}, self.path))
        self.assertFalse(cache.entry_loose(item, {"extension": "zip"}, self.path))
        self.write({})
        self.assertFalse(cache.entry_loose(item, {"extension": "file"}, self.path))


class LooseCachePipelineTests(unittest.TestCase):
    LOOSE_BYTES = b"DLG V3.28" + bytes(range(256)) * 8

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name, "cache")
        (self.root / "objects").mkdir(parents=True)
        self.selections = Path(self.directory.name, "selections.json")
        self.selections.write_text(json.dumps({
            "selections": {"123": {"file": "loose_texture.tga", "loose": True}},
            "tool_selections": {"google-drive:FILEID123": {"file": "dialog_sample.dlg", "loose": True}}}),
            encoding="utf-8")
        for patcher in (mock.patch.object(cache, "SELECTIONS_PATH", self.selections),
                        mock.patch.object(cache.shutil, "which", which_table({"gdown": "/f/gdown"}))):
            patcher.start()
            self.addCleanup(patcher.stop)

    def fake_tool(self, filename, payload):
        def fake_run(command, **kwargs):
            (Path(command[command.index("-O") + 1]) / filename).write_bytes(payload)
            return completed(0)
        return fake_run

    def test_tool_download_keeps_name_and_records_sha(self):
        with mock.patch.object(cache.subprocess, "run", self.fake_tool("dialog_sample.dlg", self.LOOSE_BYTES)):
            entry = cache.refresh_one(item_for(DRIVE_URL), {}, self.root, args_for())
        self.assertEqual(entry["extension"], "file")
        self.assertEqual(entry["archive_name"], "dialog_sample.dlg")
        self.assertEqual(entry["sha256"], cache.hashlib.sha256(self.LOOSE_BYTES).hexdigest())
        self.assertEqual(entry["size"], len(self.LOOSE_BYTES))
        self.assertTrue(cache.local_valid(self.root, entry))

    def test_tool_download_with_different_name_rejected(self):
        with mock.patch.object(cache.subprocess, "run", self.fake_tool("renamed.dlg", self.LOOSE_BYTES)):
            with self.assertRaises(ValueError):
                cache.fetch_with_tool(item_for(DRIVE_URL), self.root, args_for())
        self.assertEqual(list((self.root / "objects").iterdir()), [])

    def test_tool_download_html_and_tiny_rejected(self):
        for payload in (b"<!DOCTYPE html>" + b"x" * 4096, b"DLG"):
            with mock.patch.object(cache.subprocess, "run", self.fake_tool("dialog_sample.dlg", payload)):
                with self.assertRaises(ValueError):
                    cache.fetch_with_tool(item_for(DRIVE_URL), self.root, args_for())
            self.assertEqual(list((self.root / "objects").iterdir()), [])

    def test_tool_download_size_limit_enforced(self):
        with mock.patch.object(cache.subprocess, "run", self.fake_tool("dialog_sample.dlg", self.LOOSE_BYTES)):
            with self.assertRaises(ValueError):
                cache.fetch_with_tool(item_for(DRIVE_URL), self.root, args_for(max_bytes=100))

    def test_unreviewed_loose_download_still_rejected(self):
        self.selections.write_text("{}", encoding="utf-8")
        with mock.patch.object(cache.subprocess, "run", self.fake_tool("dialog_sample.dlg", self.LOOSE_BYTES)):
            with self.assertRaises(ValueError):
                cache.fetch_with_tool(item_for(DRIVE_URL), self.root, args_for())

    def http_response(self, payload, disposition=None, content_type="application/octet-stream"):
        headers = {"Content-Type": content_type, "Content-Length": str(len(payload))}
        if disposition:
            headers["Content-Disposition"] = disposition
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.code = 200
        response.headers = headers
        response.geturl.return_value = DS_DOWNLOAD
        stream = [payload, b""]
        response.read.side_effect = lambda size=-1: stream.pop(0) if stream else b""
        return response

    def test_http_download_keeps_original_file_name_and_extension(self):
        response = self.http_response(b"\x00\x01" * 100, 'attachment; filename="loose_texture.tga"')
        with mock.patch.object(cache, "request", return_value=response):
            entry = cache.refresh_one(item_for(DS_DOWNLOAD), {}, self.root, args_for())
        self.assertEqual((entry["extension"], entry["archive_name"]), ("file", "loose_texture.tga"))
        self.assertTrue(cache.local_valid(self.root, entry))

    def test_http_download_without_disposition_uses_reviewed_name(self):
        response = self.http_response(b"\x00\x01" * 100)
        with mock.patch.object(cache, "request", return_value=response):
            entry = cache.refresh_one(item_for(DS_DOWNLOAD), {}, self.root, args_for())
        self.assertEqual(entry["archive_name"], "loose_texture.tga")

    def test_http_download_with_other_disposition_name_rejected(self):
        response = self.http_response(b"\x00\x01" * 100, 'attachment; filename="other.tga"')
        with mock.patch.object(cache, "request", return_value=response):
            with self.assertRaises(ValueError):
                cache.refresh_one(item_for(DS_DOWNLOAD), {}, self.root, args_for())
        self.assertEqual(list((self.root / "objects").iterdir()), [])

    def test_http_html_body_with_binary_content_type_rejected(self):
        response = self.http_response(b"<!DOCTYPE html>" + b"x" * 200, 'attachment; filename="loose_texture.tga"')
        with mock.patch.object(cache, "request", return_value=response):
            with self.assertRaises(ValueError):
                cache.refresh_one(item_for(DS_DOWNLOAD), {}, self.root, args_for())
        self.assertEqual(list((self.root / "objects").iterdir()), [])

    def test_http_tiny_body_rejected(self):
        response = self.http_response(b"MZ", 'attachment; filename="loose_texture.tga"')
        with mock.patch.object(cache, "request", return_value=response):
            with self.assertRaises(ValueError):
                cache.refresh_one(item_for(DS_DOWNLOAD), {}, self.root, args_for())

    def run_main(self, *argv):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = cache.main(list(argv))
        return code, json.loads(output.getvalue())

    def seed(self, guide_url, name, payload, extension="file"):
        sha = cache.hashlib.sha256(payload).hexdigest()
        (self.root / "objects" / sha).write_bytes(payload)
        guide = Path(self.directory.name, "guide.md")
        guide.write_text("[mod](" + guide_url + ")\n", encoding="utf-8")
        item = item_for(guide_url)
        (self.root / "manifest.json").write_text(json.dumps({"schema": 1, "entries": {item["id"]: {
            "sha256": sha, "size": len(payload), "extension": extension, "archive_name": name,
            "status": "verified", "evidence": "download-without-upstream-metadata",
            "checked_at": cache.time.time(), "metadata_unknown": True}}}), encoding="utf-8")
        return guide, sha

    def restore(self, guide):
        destination = Path(self.directory.name, "mods")
        return self.run_main("restore", "--guide", str(guide), "--cache-dir", str(self.root),
                             "--destination", str(destination)), destination

    def test_restore_returns_loose_files_intact_with_original_names(self):
        for url, name in ((DRIVE_URL, "dialog_sample.dlg"), (DS_PAGE, "loose_texture.tga")):
            guide, sha = self.seed(url, name, self.LOOSE_BYTES)
            (code, report), destination = self.restore(guide)
            self.assertEqual(code, 0)
            self.assertEqual(report["entries"][0]["status"], "restored")
            self.assertEqual(report["entries"][0]["file"], name)
            self.assertEqual((destination / name).read_bytes(), self.LOOSE_BYTES)
            self.assertEqual(cache.digest(destination / name), sha)
            self.assertEqual([path.name for path in destination.iterdir()], [name])
            (destination / name).unlink()

    def test_restore_rejects_loose_entry_that_is_no_longer_reviewed(self):
        guide, _ = self.seed(DRIVE_URL, "dialog_sample.dlg", self.LOOSE_BYTES)
        self.selections.write_text("{}", encoding="utf-8")
        (code, report), destination = self.restore(guide)
        self.assertEqual(code, 1)
        self.assertEqual(report["entries"][0]["status"], "unresolved")
        self.assertFalse(destination.exists() and any(destination.iterdir()))

    def test_restore_rejects_loose_entry_with_other_name_or_html_body(self):
        guide, _ = self.seed(DRIVE_URL, "renamed.dlg", self.LOOSE_BYTES)
        (code, report), _ = self.restore(guide)
        self.assertEqual((code, report["entries"][0]["status"]), (1, "unresolved"))
        guide, _ = self.seed(DRIVE_URL, "dialog_sample.dlg", b"<html>" + b"x" * 200)
        (code, report), _ = self.restore(guide)
        self.assertEqual((code, report["entries"][0]["status"]), (1, "unresolved"))

    def test_restore_archives_unchanged(self):
        payload = io.BytesIO()
        with zipfile.ZipFile(payload, "w") as archive:
            archive.writestr("data.bin", b"x" * 4096)
        guide, _ = self.seed(MEGA_URL, "pack.zip", payload.getvalue(), extension="zip")
        (code, report), destination = self.restore(guide)
        self.assertEqual((code, report["entries"][0]["status"]), (0, "restored"))
        self.assertEqual((destination / "pack.zip").read_bytes(), payload.getvalue())

    def test_import_existing_accepts_reviewed_loose_file(self):
        archives = Path(self.directory.name, "archives")
        archives.mkdir()
        (archives / "dialog_sample.dlg").write_bytes(self.LOOSE_BYTES)
        item = item_for(DRIVE_URL)
        resource = {"a": {"HandlerMetadata": {"PrimaryUrl": DRIVE_URL}, "Files": {"dialog_sample.dlg": {}}}}
        manifest = {"schema": 1, "entries": {}}
        args = args_for(apply=True, import_manifest=manifest["entries"], import_manifest_document=manifest)
        with mock.patch.object(cache, "DISK_RESERVE_BYTES", 0):
            entry = cache.import_existing(item, resource, archives, self.root, args)
        self.assertEqual(entry["status"], "stale-unknown")
        self.assertEqual(manifest["entries"][item["id"]]["extension"], "file")
        self.assertEqual(manifest["entries"][item["id"]]["archive_name"], "dialog_sample.dlg")
        (archives / "dialog_sample.dlg").unlink()
        (archives / "unreviewed.dlg").write_bytes(self.LOOSE_BYTES)
        resource["a"]["Files"] = {"unreviewed.dlg": {}}
        entry = cache.import_existing(item_for(DRIVE_URL), resource, archives, self.root,
                                      args_for(apply=False, import_manifest={}, import_manifest_document={}))
        self.assertEqual(entry["status"], "unresolved")


if __name__ == "__main__":
    unittest.main()
