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
        self.assertEqual(cache.load_selections(), {key: cache.selection_value(entry)
                                                   for key, entry in reviewed["selections"].items()})

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
        self.assertIsNone(cache.reviewed_loose(item_for(OTHER_URL), self.path))
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

    def test_archive_map_lists_single_file_under_canonical_url(self):
        guide, _ = self.seed(DS_PAGE, "loose_texture.tga", self.LOOSE_BYTES)
        map_path = Path(self.directory.name, "archive-map.json")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = cache.main(["restore", "--guide", str(guide), "--cache-dir", str(self.root),
                               "--destination", str(Path(self.directory.name, "mods")),
                               "--archive-map", str(map_path)])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(map_path.read_text()), {DS_PAGE: "loose_texture.tga"})

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


class MultiSelectionSchemaTests(unittest.TestCase):
    def test_files_list_accepts_names_and_row_objects(self):
        entry = cache.review_entry("11", {"files": ["a.zip", {"file": "b.7z", "r": 42}, {"file": "c.rar", "r": "7"}]})
        self.assertEqual(entry["files"], [{"file": "a.zip", "r": None}, {"file": "b.7z", "r": "42"},
                                           {"file": "c.rar", "r": "7"}])
        self.assertEqual(entry["file"], "a.zip")
        self.assertFalse(entry["loose"])

    def test_single_file_keeps_plain_string_value(self):
        self.assertEqual(cache.selection_value(cache.review_entry("11", {"file": "a.zip"})), "a.zip")
        self.assertEqual(cache.selection_value(cache.review_entry("11", "a.zip")), "a.zip")

    def test_row_or_several_files_use_selector_list_value(self):
        self.assertEqual(cache.selection_value(cache.review_entry("11", {"file": "a.zip", "r": "5"})),
                         [{"file": "a.zip", "r": "5"}])
        self.assertEqual(cache.selection_value(cache.review_entry("11", {"files": ["a.zip", "b.zip"]})),
                         [{"file": "a.zip", "r": None}, {"file": "b.zip", "r": None}])

    def test_malformed_multi_file_selections_rejected(self):
        for value in ({"file": "a.zip", "files": ["b.zip"]}, {"files": []}, {"files": "a.zip"},
                      {"files": ["a.zip", "A.ZIP"]}, {"files": ["a.zip", "a.zip"]},
                      {"files": [{"file": "a.zip", "r": "x1"}]}, {"files": [{"file": "a.zip", "r": True}]},
                      {"files": [{"file": "a.zip", "extra": 1}]}, {"files": ["notes.txt"]},
                      {"files": [5]}, {"files": [{"r": "1"}]}, {"file": "a.zip", "r": "-1"}, 5):
            with self.assertRaises(ValueError, msg=repr(value)):
                cache.review_entry("11", value)

    def test_loose_and_tool_selections_must_be_one_file_without_row(self):
        for value in ({"files": ["a.tga", "b.tga"], "loose": True}, {"file": "a.tga", "r": "5", "loose": True}):
            with self.assertRaises(ValueError, msg=repr(value)):
                cache.review_entry("11", value)
        with self.assertRaises(ValueError):
            cache.review_entry("google-drive:X", {"files": ["a.tga", "b.tga"], "loose": True}, tool=True)

    def test_shipped_multi_file_selections(self):
        reviewed = cache.load_reviewed()["selections"]
        names = {key: [selector["file"] for selector in reviewed[key]["files"]] for key in reviewed}
        self.assertEqual(len(names["2503"]), 2)
        self.assertEqual(len(set(names["2503"])), 2)
        self.assertEqual(sum("_mods_" in name for name in names["2503"]), 1)
        self.assertEqual(len(names["2752"]), 4)
        self.assertEqual(len(names["1138"]), 2)
        self.assertEqual(reviewed["842"]["files"], [{"file": "aleema_ketos_robe_description_correction.zip",
                                                      "r": "19298"}])
        self.assertTrue(reviewed["578"]["loose"])
        self.assertTrue(reviewed["2734"]["loose"])
        for entry in reviewed.values():
            self.assertTrue(entry["files"])


class MultiChooseTests(unittest.TestCase):
    def rows(self, *names):
        return [{"name": name, "url": "https://deadlystream.com/x?r=%d" % (10 + index), "r": str(10 + index)}
                for index, name in enumerate(names)]

    def test_several_files_selected_in_order(self):
        rows = self.rows("a.zip", "b.zip", "c.zip", "d.zip")
        chosen = cache.choose_downloads(rows, "9", {"9": [{"file": "c.zip", "r": None}, {"file": "a.zip", "r": None}]})
        self.assertEqual([row["name"] for row in chosen], ["c.zip", "a.zip"])

    def test_row_id_disambiguates_duplicate_names(self):
        rows = self.rows("same.zip", "same.zip")
        chosen = cache.choose_downloads(rows, "9", {"9": [{"file": "same.zip", "r": "11"}]})
        self.assertEqual([row["r"] for row in chosen], ["11"])
        with self.assertRaises(ValueError):
            cache.choose_downloads(rows, "9", {"9": "same.zip"})
        with self.assertRaises(ValueError):
            cache.choose_downloads(rows, "9", {"9": [{"file": "same.zip", "r": "99"}]})

    def test_any_missing_selected_file_fails_closed(self):
        rows = self.rows("a.zip", "b.zip")
        with self.assertRaises(ValueError) as caught:
            cache.choose_downloads(rows, "9", {"9": [{"file": "a.zip", "r": None}, {"file": "gone.zip", "r": None}]})
        self.assertIn("matches 0 of the listed files", str(caught.exception))

    def test_row_id_must_belong_to_named_file(self):
        rows = self.rows("a.zip", "b.zip")
        with self.assertRaises(ValueError):
            cache.choose_downloads(rows, "9", {"9": [{"file": "a.zip", "r": "11"}]})

    def test_unreviewed_multi_row_page_still_fails_closed(self):
        with self.assertRaises(ValueError) as caught:
            cache.choose_downloads(self.rows("a.zip", "b.zip"), "9", {})
        self.assertIn("no reviewed selection", str(caught.exception))

    def test_page_rows_returns_every_selected_row_and_page_endpoint_refuses_several(self):
        source = selection_page("first.zip", "second.7z", "third.rar")
        item = item_for(POST_URL)
        selections = {"123": [{"file": "first.zip", "r": None}, {"file": "third.rar", "r": None}]}
        rows = cache.page_rows(FakePage(POST_URL, source), item, selections)
        self.assertEqual([(row["name"], row["r"]) for row in rows], [("first.zip", "1000"), ("third.rar", "1002")])
        with self.assertRaises(ValueError):
            cache.page_endpoint(FakePage(POST_URL, source), item, selections)


class Reply:
    def __init__(self, url, payload, content_type="application/octet-stream", disposition=None):
        self.url = url
        self.code = 200
        self.headers = {"Content-Type": content_type, "Content-Length": str(len(payload))}
        if disposition:
            self.headers["Content-Disposition"] = disposition
        self.payload = payload
        self.offset = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def close(self):
        pass

    def geturl(self):
        return self.url

    def read(self, size=-1):
        end = len(self.payload) if size is None or size < 0 else self.offset + size
        chunk = self.payload[self.offset:end]
        self.offset += len(chunk)
        return chunk


def zip_bytes(label):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("data.bin", (label * 600).encode())
    return stream.getvalue()


class MultiFileFixture(unittest.TestCase):
    NAMES = ("pack_a.zip", "pack_b.zip")

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.base = Path(self.directory.name)
        self.root = self.base / "cache"
        (self.root / "objects").mkdir(parents=True)
        self.selections = self.base / "selections.json"
        self.select({"files": list(self.NAMES)})
        patcher = mock.patch.object(cache, "SELECTIONS_PATH", self.selections)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.payloads = {name: zip_bytes(name[5]) for name in self.NAMES}
        self.requests = []

    def select(self, value):
        self.selections.write_text(json.dumps({"selections": {"123": value}}), encoding="utf-8")

    def serve(self, listed=None, delivered=None):
        listed = listed or self.NAMES
        delivered = delivered or {name: self.payloads[name] for name in listed}

        def route(url, args, headers=None):
            self.requests.append(url)
            if url == POST_URL:
                return Reply(url, selection_page(*listed).encode(), "text/html; charset=utf-8")
            row = int(url.split("r=")[1].split("&")[0]) - 1000
            name = listed[row]
            return Reply(url, delivered[name], disposition='attachment; filename="%s"' % name)
        return mock.patch.object(cache, "request", side_effect=route)

    def bundle(self):
        with self.serve():
            return cache.refresh_one(item_for(POST_URL), {}, self.root, args_for())


class MultiFileFetchTests(MultiFileFixture):
    def test_refresh_builds_one_object_per_selected_file(self):
        entry = self.bundle()
        self.assertEqual([each["listed_name"] for each in entry["objects"]], list(self.NAMES))
        self.assertEqual([each["archive_name"] for each in entry["objects"]], list(self.NAMES))
        self.assertEqual([each["row"] for each in entry["objects"]], ["1000", "1001"])
        self.assertEqual(entry["size"], sum(len(payload) for payload in self.payloads.values()))
        self.assertEqual(len({each["sha256"] for each in entry["objects"]}), 2)
        self.assertTrue(cache.local_valid(self.root, entry))
        self.assertTrue(cache.objects_match_selection(item_for(POST_URL), entry))
        self.assertEqual(len(list((self.root / "objects").iterdir())), 2)

    def test_local_valid_requires_every_object(self):
        entry = self.bundle()
        cache.object_path(self.root, entry["objects"][1]).unlink()
        self.assertFalse(cache.local_valid(self.root, entry))
        self.assertFalse(cache.local_valid(self.root, {"objects": []}))
        self.assertFalse(cache.local_valid(self.root, {"objects": ["junk"]}))

    def test_size_budget_is_shared_across_files(self):
        limit = len(self.payloads[self.NAMES[0]]) + 5
        with self.serve():
            with self.assertRaises(ValueError) as caught:
                cache.refresh_one(item_for(POST_URL), {}, self.root, args_for(max_bytes=limit))
        self.assertIn("size limit", str(caught.exception))

    def test_unchanged_object_is_reused_by_endpoint(self):
        entry = self.bundle()
        with self.serve():
            again = cache.refresh_one(item_for(POST_URL), entry, self.root, args_for())
        self.assertEqual([each["sha256"] for each in again["objects"]], [each["sha256"] for each in entry["objects"]])

    def test_selection_is_rechecked_against_fetched_rows(self):
        entry = self.bundle()
        self.select({"files": [self.NAMES[0], "pack_c.zip"]})
        self.assertFalse(cache.objects_match_selection(item_for(POST_URL), entry))
        self.select({"files": [self.NAMES[0]]})
        self.assertIsNone(cache.multi_selectors(item_for(POST_URL)))
        self.assertTrue(cache.objects_match_selection(item_for(POST_URL), {"objects": entry["objects"][:1]}))

    def test_row_selection_pins_one_of_two_same_named_rows(self):
        self.select({"file": "pack_a.zip", "r": "1001"})
        with self.serve(listed=("pack_a.zip", "pack_a.zip"),
                        delivered={"pack_a.zip": self.payloads["pack_a.zip"]}):
            entry = cache.refresh_one(item_for(POST_URL), {}, self.root, args_for())
        self.assertEqual([(each["listed_name"], each["row"]) for each in entry["objects"]],
                         [("pack_a.zip", "1001")])
        self.assertTrue(any("r=1001" in url for url in self.requests))
        self.assertFalse(any("r=1000" in url for url in self.requests))

    def test_row_selection_through_confirmation_page_yields_one_object(self):
        self.select({"file": "pack_a.zip", "r": "1001"})
        landing = '<html><body><a href="/files/file/123-example-post/?do=download">Download</a></body></html>'
        confirm_url = POST_URL + "?do=download"

        def route(url, args, headers=None):
            self.requests.append(url)
            if url == POST_URL:
                return Reply(url, landing.encode(), "text/html; charset=utf-8")
            if url == confirm_url:
                return Reply(url, selection_page("pack_a.zip", "pack_a.zip").encode(), "text/html; charset=utf-8")
            return Reply(url, self.payloads["pack_a.zip"], disposition='attachment; filename="pack_a.zip"')

        with mock.patch.object(cache, "request", side_effect=route):
            entry = cache.refresh_one(item_for(POST_URL), {}, self.root, args_for())
        self.assertEqual([(each["listed_name"], each["row"]) for each in entry["objects"]], [("pack_a.zip", "1001")])
        self.assertNotIn("objects", entry["objects"][0])
        self.assertTrue(cache.local_valid(self.root, entry))

    def test_total_from_content_range_rejects_oversized_and_truncated_downloads(self):
        payload = self.payloads[self.NAMES[0]]
        self.assertEqual(cache.announced_size({"Content-Length": "7"}), 7)
        self.assertEqual(cache.announced_size({"Content-Range": "bytes 0-9/10"}), 10)
        for header in ("bytes 5-9/10", "bytes 0-4/10", "items 0-9/10", ""):
            self.assertIsNone(cache.announced_size({"Content-Range": header}))

        def reply(total):
            made = Reply(POST_URL + "?do=download&r=1", payload, disposition='attachment; filename="pack_a.zip"')
            del made.headers["Content-Length"]
            made.headers["Content-Range"] = "bytes 0-%d/%d" % (total - 1, total)
            return made

        item = item_for(POST_URL + "?do=download&r=1")
        with mock.patch.object(cache, "request", return_value=reply(len(payload) + 100)):
            with self.assertRaises(ValueError) as caught:
                cache.fetch_item(item, {}, self.root, args_for())
        self.assertIn("truncated", str(caught.exception))
        with mock.patch.object(cache, "request", return_value=reply(10 ** 9)) as opened:
            with self.assertRaises(ValueError) as caught:
                cache.fetch_item(item, {}, self.root, args_for(max_bytes=10 ** 6))
        self.assertIn("size limit", str(caught.exception))
        self.assertEqual(list((self.root / "objects").iterdir()), [])
        with mock.patch.object(cache, "request", return_value=reply(len(payload))):
            self.assertTrue(cache.local_valid(self.root, cache.fetch_item(item, {}, self.root, args_for())))

    def test_mismatched_result_is_refused(self):
        item = item_for(POST_URL)
        wrong = dict(self.bundle())
        wrong["objects"] = [{**wrong["objects"][0]}, {**wrong["objects"][1], "listed_name": "pack_z.zip"}]
        with mock.patch.object(cache, "fetch_item", return_value=wrong):
            with self.assertRaises(ValueError) as caught:
                cache.refresh_one(item, {}, self.root, args_for())
        self.assertIn("do not match the reviewed multi-file selection", str(caught.exception))


class MultiFileRestoreTests(MultiFileFixture):
    def setUp(self):
        super().setUp()
        self.guide = self.base / "guide.md"
        self.guide.write_text("[mod](%s)\n" % POST_URL, encoding="utf-8")
        self.destination = self.base / "mods"

    def run_main(self, command, *extra):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = cache.main([command, "--guide", str(self.guide), "--cache-dir", str(self.root), *extra])
        return code, json.loads(output.getvalue())

    def restore(self):
        return self.run_main("restore", "--destination", str(self.destination), "--retries", "0")

    def seed(self, entry):
        (self.root / "manifest.json").write_text(json.dumps({"schema": 1, "entries": {item_for(POST_URL)["id"]: entry}}),
                                                 encoding="utf-8")

    def restored_names(self):
        return sorted(path.name for path in self.destination.iterdir()) if self.destination.exists() else []

    def test_restore_returns_every_file_under_one_endpoint(self):
        entry = self.bundle()
        entry.update(status="verified", evidence="download-without-upstream-metadata", checked_at=cache.time.time())
        self.seed(entry)
        with mock.patch.object(cache, "request", side_effect=AssertionError("restore must not download")):
            code, report = self.restore()
        self.assertEqual(code, 0)
        row = report["entries"][0]
        self.assertEqual(row["status"], "restored")
        self.assertEqual([each["file"] for each in row["files"]], list(self.NAMES))
        self.assertNotIn("file", row)
        self.assertEqual(self.restored_names(), sorted(self.NAMES))
        for name in self.NAMES:
            self.assertEqual((self.destination / name).read_bytes(), self.payloads[name])

    def test_missing_object_restores_nothing(self):
        entry = self.bundle()
        entry.update(status="verified", evidence="download-without-upstream-metadata", checked_at=cache.time.time())
        cache.object_path(self.root, entry["objects"][1]).unlink()
        self.seed(entry)
        with mock.patch.object(cache, "request", side_effect=ValueError("network request failed")):
            code, report = self.restore()
        self.assertEqual((code, report["entries"][0]["status"]), (1, "unresolved"))
        self.assertEqual(self.restored_names(), [])

    def test_corrupt_object_restores_nothing(self):
        entry = self.bundle()
        entry.update(status="verified", evidence="download-without-upstream-metadata", checked_at=cache.time.time())
        cache.object_path(self.root, entry["objects"][0]).write_bytes(b"corrupt")
        self.seed(entry)
        with mock.patch.object(cache, "request", side_effect=ValueError("network request failed")):
            code, report = self.restore()
        self.assertEqual((code, report["entries"][0]["status"]), (1, "unresolved"))
        self.assertEqual(self.restored_names(), [])

    def test_late_collision_unwinds_files_already_created(self):
        entry = self.bundle()
        entry.update(status="verified", evidence="download-without-upstream-metadata", checked_at=cache.time.time())
        self.seed(entry)
        self.destination.mkdir()
        (self.destination / self.NAMES[1]).write_bytes(b"someone else's file")
        code, report = self.restore()
        self.assertEqual((code, report["entries"][0]["status"]), (1, "unresolved"))
        self.assertEqual(self.restored_names(), [self.NAMES[1]])
        self.assertEqual((self.destination / self.NAMES[1]).read_bytes(), b"someone else's file")

    def test_two_objects_with_one_name_collide(self):
        entry = self.bundle()
        entry.update(status="verified", evidence="download-without-upstream-metadata", checked_at=cache.time.time())
        entry["objects"][1]["archive_name"] = entry["objects"][0]["archive_name"]
        self.seed(entry)
        code, report = self.restore()
        self.assertEqual((code, report["entries"][0]["status"]), (1, "unresolved"))
        self.assertEqual(self.restored_names(), [])

    def test_legacy_single_entry_for_multi_file_selection_is_repaired(self):
        payload = self.payloads[self.NAMES[0]]
        sha = cache.hashlib.sha256(payload).hexdigest()
        (self.root / "objects" / sha).write_bytes(payload)
        self.seed({"sha256": sha, "size": len(payload), "extension": "zip", "archive_name": self.NAMES[0],
                   "status": "verified", "evidence": "download-without-upstream-metadata",
                   "checked_at": cache.time.time(), "metadata_unknown": True})
        with self.serve():
            code, report = self.restore()
        self.assertEqual((code, report["entries"][0]["status"]), (0, "restored"))
        self.assertEqual(self.restored_names(), sorted(self.NAMES))
        manifest = json.loads((self.root / "manifest.json").read_text())
        self.assertEqual(len(manifest["entries"][item_for(POST_URL)["id"]]["objects"]), 2)

    def test_legacy_single_entry_without_selection_is_unchanged(self):
        self.selections.write_text("{}", encoding="utf-8")
        payload = zip_bytes("l")
        sha = cache.hashlib.sha256(payload).hexdigest()
        (self.root / "objects" / sha).write_bytes(payload)
        self.seed({"sha256": sha, "size": len(payload), "extension": "zip", "archive_name": "single.zip",
                   "status": "verified", "evidence": "download-without-upstream-metadata",
                   "checked_at": cache.time.time(), "metadata_unknown": True})
        with mock.patch.object(cache, "request", side_effect=AssertionError("no network")):
            code, report = self.restore()
        row = report["entries"][0]
        self.assertEqual((code, row["status"], row["file"], row["sha256"]), (0, "restored", "single.zip", sha))
        self.assertNotIn("files", row)

    def test_refresh_command_writes_bundle_manifest(self):
        with self.serve():
            code, report = self.run_main("refresh", "--retries", "0")
        self.assertEqual((code, report["entries"][0]["status"]), (0, "verified"))
        manifest = json.loads((self.root / "manifest.json").read_text())
        stored = manifest["entries"][item_for(POST_URL)["id"]]
        self.assertEqual([each["listed_name"] for each in stored["objects"]], list(self.NAMES))

    def test_import_existing_requires_exact_file_set(self):
        archives = self.base / "archives"
        archives.mkdir()
        for name in self.NAMES:
            (archives / name).write_bytes(self.payloads[name])
        item = item_for(POST_URL)
        resource = {"a": {"HandlerMetadata": {"PrimaryUrl": POST_URL}, "Files": {name: {} for name in self.NAMES}}}
        manifest = {"schema": 1, "entries": {}}
        args = args_for(apply=True, import_manifest=manifest["entries"], import_manifest_document=manifest)
        with mock.patch.object(cache, "DISK_RESERVE_BYTES", 0):
            entry = cache.import_existing(item, resource, archives, self.root, args)
        self.assertEqual(entry["status"], "stale-unknown")
        stored = manifest["entries"][item["id"]]
        self.assertEqual(sorted(each["listed_name"] for each in stored["objects"]), sorted(self.NAMES))
        self.assertTrue(cache.local_valid(self.root, stored))
        resource["a"]["Files"] = {self.NAMES[0]: {}}
        partial = cache.import_existing(item, resource, archives, self.root,
                                        args_for(apply=False, import_manifest={}, import_manifest_document={}))
        self.assertEqual(partial["status"], "ambiguous-variant")

    def test_import_existing_never_guesses_a_row(self):
        self.select({"file": self.NAMES[0], "r": "1001"})
        archives = self.base / "archives"
        archives.mkdir()
        (archives / self.NAMES[0]).write_bytes(self.payloads[self.NAMES[0]])
        resource = {"a": {"HandlerMetadata": {"PrimaryUrl": POST_URL}, "Files": {self.NAMES[0]: {}}}}
        entry = cache.import_existing(item_for(POST_URL), resource, archives, self.root,
                                      args_for(apply=False, import_manifest={}, import_manifest_document={}))
        self.assertEqual(entry["status"], "ambiguous-variant")


refresh_spec = importlib.util.spec_from_file_location("refresh_archives", Path(__file__).with_name("refresh_archives.py"))
refresh_archives = importlib.util.module_from_spec(refresh_spec)
refresh_spec.loader.exec_module(refresh_archives)


class MultiFileRefreshJobTests(MultiFileFixture):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(refresh_archives.cache, "SELECTIONS_PATH", self.selections)
        patcher.start()
        self.addCleanup(patcher.stop)

    def bundle_entry(self, status="verified"):
        entry = self.bundle()
        entry.update(status=status, evidence="download-without-upstream-metadata", checked_at=1000.0)
        return entry

    def test_is_due_checks_the_reviewed_selection(self):
        item = item_for(POST_URL)
        entry = self.bundle_entry()
        now = 1000.0 + 60
        self.assertFalse(refresh_archives.is_due(entry, now))
        self.assertFalse(refresh_archives.is_due(entry, now, item))
        self.select({"files": [self.NAMES[0], "pack_c.zip"]})
        self.assertFalse(refresh_archives.is_due(entry, now))
        self.assertTrue(refresh_archives.is_due(entry, now, item))
        self.assertTrue(refresh_archives.is_due(entry, now + 15 * 86400, item))

    def test_selection_failures_have_their_own_reason_class(self):
        for reason in ("landing page lists multiple files and no reviewed selection exists for file 9; add",
                       "reviewed archive selection for file 9 matches 0 of the listed files; re-review",
                       "fetched files do not match the reviewed multi-file selection; re-review"):
            self.assertEqual(refresh_archives.classify(reason), ("selection-review", None))
        self.assertEqual(refresh_archives.classify("cache missing; archive refresh job must populate it")[0], "other")

    def test_missing_credentials_are_not_labelled_as_captcha(self):
        self.assertEqual(refresh_archives.classify(cache.NEXUS_REASON), ("credentials-required", None))
        self.assertEqual(refresh_archives.classify("nexus requires NEXUS_API_KEY"), ("credentials-required", None))
        self.assertEqual(refresh_archives.classify("provider challenge/CAPTCHA; blocked-pending-user-action"),
                         ("captcha-user-action", None))
        unresolved = [{"id": "4", "provider": "nexus", "status": "unresolved", "reason": cache.NEXUS_REASON},
                      {"id": "5", "provider": "deadlystream", "status": "unresolved",
                       "reason": "provider challenge/CAPTCHA; blocked-pending-user-action"}]
        summary = refresh_archives.totals(unresolved, {"entries": {}}, 0.0, "finished", True, True, 5)
        classes = {(each["provider"], each["reason_class"]) for each in summary["unresolved_classes"]}
        self.assertEqual(classes, {("nexus", "credentials-required"), ("deadlystream", "captcha-user-action")})
        self.assertEqual(summary["other_reasons"], {})

    def test_totals_count_each_object_of_a_bundle(self):
        entry = self.bundle_entry()
        entry["checked_at"] = 2000.0
        for each in entry["objects"]:
            each["evidence"] = "download-200"
        manifest = {"entries": {"1": entry, "2": {"checked_at": 1.0}}}
        unresolved = [{"id": "3", "provider": "deadlystream", "status": "unresolved",
                       "reason": "landing page lists multiple files and no reviewed selection exists for file 9"}]
        summary = refresh_archives.totals(unresolved, manifest, 1500.0, "finished", True, True, 5)
        self.assertEqual(summary["verified_downloads"], 2)
        self.assertEqual(summary["verified_bytes"], sum(len(value) for value in self.payloads.values()))
        self.assertEqual(summary["unresolved_classes"][0]["reason_class"], "selection-review")
        self.assertEqual(len(summary["other_reasons"]), 1)

    def test_item_job_restores_and_uploads_every_object(self):
        entry = self.bundle_entry()
        (self.root / "manifest.json").write_text(json.dumps({"schema": 1, "entries": {"123": entry}}), encoding="utf-8")
        remote = {each["sha256"]: self.payloads[each["listed_name"]] for each in entry["objects"]}
        for each in entry["objects"]:
            cache.object_path(self.root, each).unlink()
        fetched, uploaded = [], []

        def fake_fetch(remote_path, local_path, *extra):
            fetched.append(remote_path.rsplit("/", 1)[1])
            Path(local_path).write_bytes(remote[fetched[-1]])
            return True

        def fake_upload(path, remote_path):
            uploaded.append(Path(path).name)

        item = item_for(POST_URL)
        item["id"] = "123"
        steps = []

        def fake_main(argv):
            (self.root.parent / "item-report.json").write_text(json.dumps({"entries": [], "ok": True}))

        with mock.patch.object(refresh_archives, "fetch", fake_fetch), \
                mock.patch.object(refresh_archives, "upload_object", fake_upload), \
                mock.patch.object(refresh_archives, "rclone", lambda *a, **k: None), \
                mock.patch.object(refresh_archives, "matches", lambda path, sha, size: True), \
                mock.patch.object(refresh_archives.cache, "discover", lambda guides: []), \
                mock.patch.object(refresh_archives.cache, "main", fake_main):
            steps.extend(refresh_archives.refresh_item(item, self.root, "remote:bucket", False))
        self.assertEqual(sorted(fetched), sorted(remote))
        self.assertEqual(sorted(uploaded), sorted(remote))
        self.assertIn("persist-manifest", steps)
        self.assertEqual(steps[-1], {"entries": [], "ok": True})


OTHER_URL = "https://deadlystream.com/files/file/124-other-example-post/"


class UncachedPolicyTests(MultiFileFixture):
    REASON = "example video pack; video is never cached"

    def setUp(self):
        super().setUp()
        self.stale = self.bundle()
        self.selections.write_text(json.dumps({
            "uncached": {"123": {"reason": self.REASON}},
            "selections": {"123": {"files": list(self.NAMES)}}}), encoding="utf-8")
        patcher = mock.patch.object(refresh_archives.cache, "SELECTIONS_PATH", self.selections)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.guide = self.base / "guide.md"
        self.guide.write_text("[mod](%s)\n" % POST_URL, encoding="utf-8")
        self.destination = self.base / "mods"
        self.item = item_for(POST_URL)
        self.objects_before = sorted((self.root / "objects").iterdir())

    def run_main(self, command, *extra):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = cache.main([command, "--guide", str(self.guide), "--cache-dir", str(self.root), *extra])
        return code, json.loads(output.getvalue())

    def stale_manifest(self):
        self.stale.update(status="fresh", checked_at=cache.time.time())
        (self.root / "manifest.json").write_text(json.dumps(
            {"schema": 1, "entries": {self.item["id"]: self.stale}}), encoding="utf-8")

    def test_reason_lookup_is_limited_to_the_listed_deadlystream_file(self):
        self.assertEqual(cache.uncached_reason(item_for(POST_URL)), self.REASON)
        self.assertIsNone(cache.uncached_reason(item_for("https://deadlystream.com/files/file/124-other/")))
        self.assertIsNone(cache.uncached_reason(item_for(MEGA_URL)))

    def test_malformed_policy_is_rejected(self):
        for value in ({"uncached": []}, {"uncached": {"123": {}}}, {"uncached": {"123": {"reason": " "}}},
                      {"uncached": {"123": "no"}}, {"uncached": {"not-an-id": {"reason": "x"}}}):
            self.selections.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaises(ValueError):
                cache.load_uncached()

    def test_shipped_policy_lists_the_video_pack_with_a_reason(self):
        self.assertTrue(cache.load_uncached(Path(cache.__file__).with_name("archive_selections.json"))["2503"])

    def test_refresh_one_never_touches_the_network(self):
        with mock.patch.object(cache, "request", side_effect=AssertionError("network used")):
            with self.assertRaises(ValueError) as caught:
                cache.refresh_one(self.item, {}, self.root, args_for())
        self.assertIn("by policy", str(caught.exception))
        self.assertEqual(sorted((self.root / "objects").iterdir()), self.objects_before)

    def test_refresh_command_reports_policy_status_downloads_nothing_and_drops_stale_entry(self):
        self.stale_manifest()
        with mock.patch.object(cache, "request", side_effect=AssertionError("network used")):
            code, report = self.run_main("refresh", "--retries", "0")
        self.assertEqual(code, 0)
        self.assertEqual([entry["status"] for entry in report["entries"]], [cache.UNCACHED])
        self.assertIn(self.REASON, report["entries"][0]["reason"])
        self.assertEqual(json.loads((self.root / "manifest.json").read_text())["entries"], {})

    def test_restore_fails_closed_with_explicit_status_and_restores_nothing(self):
        self.stale_manifest()
        with mock.patch.object(cache, "request", side_effect=AssertionError("network used")):
            code, report = self.run_main("restore", "--destination", str(self.destination), "--retries", "0")
        self.assertEqual(code, 1)
        self.assertEqual([entry["status"] for entry in report["entries"]], [cache.UNCACHED])
        self.assertIn("not cached by policy", report["entries"][0]["reason"])
        self.assertFalse(self.destination.exists() and any(self.destination.iterdir()))

    WIDE = "Widescreen Section"

    def guide_with(self, base_steps="", wide_steps=""):
        self.guide.write_text(
            "# Guide\n\n## Base Section\n\n### Base Step\n\n" + base_steps
            + "\n## %s\n\n### Wide Step\n\n[mod](%s)\n\n" % (self.WIDE, POST_URL) + wide_steps,
            encoding="utf-8")

    def restore_allowing(self, *sections):
        extra = [value for section in sections for value in ("--allow-uncached-by-policy", section)]
        report_path = self.base / "report.json"
        with mock.patch.object(cache, "request", side_effect=AssertionError("network used")):
            code, report = self.run_main("restore", "--destination", str(self.destination), "--retries", "0",
                                         "--report", str(report_path), *extra)
        self.assertEqual(json.loads(report_path.read_text()), report)
        return code, report

    def test_allowed_section_skips_the_link_and_records_the_step(self):
        self.guide_with()
        code, report = self.restore_allowing(self.WIDE)
        self.assertEqual(code, 0)
        self.assertTrue(report["ok"])
        self.assertEqual([entry["status"] for entry in report["entries"]], [cache.UNCACHED_SKIPPED])
        step = {"guide": str(self.guide), "section": self.WIDE, "heading": "Wide Step"}
        self.assertEqual(report["entries"][0]["skipped_steps"], [step])
        self.assertEqual(report["skipped_steps"], [{**step, "url": POST_URL, "reason": report["entries"][0]["reason"]}])
        self.assertIn(self.REASON, report["skipped_steps"][0]["reason"])
        self.assertFalse(self.destination.exists() and any(self.destination.iterdir()))

    def test_without_the_flag_the_same_guide_still_fails(self):
        self.guide_with()
        code, report = self.run_main("restore", "--destination", str(self.destination), "--retries", "0")
        self.assertEqual(code, 1)
        self.assertEqual([entry["status"] for entry in report["entries"]], [cache.UNCACHED])
        self.assertNotIn("skipped_steps", report)

    def test_reference_outside_the_allowed_section_still_fails(self):
        self.guide_with(base_steps="[mod](%s)\n" % POST_URL)
        code, report = self.restore_allowing(self.WIDE)
        self.assertEqual(code, 1)
        self.assertEqual([entry["status"] for entry in report["entries"]], [cache.UNCACHED])
        self.assertIn("Base Step", report["entries"][0]["reason"])
        self.assertNotIn("skipped_steps", report)

    def test_other_section_name_does_not_allow_the_link(self):
        self.guide_with()
        code, report = self.restore_allowing("Base Section")
        self.assertEqual(code, 1)
        self.assertEqual([entry["status"] for entry in report["entries"]], [cache.UNCACHED])

    def test_reference_outside_any_step_still_fails(self):
        self.guide.write_text("[mod](%s)\n\n## %s\n\nprose\n" % (POST_URL, self.WIDE), encoding="utf-8")
        code, report = self.restore_allowing(self.WIDE)
        self.assertEqual(code, 1)
        self.assertEqual([entry["status"] for entry in report["entries"]], [cache.UNCACHED])

    def test_an_unrelated_unresolved_link_still_fails_when_policy_links_are_skipped(self):
        self.guide_with(base_steps="[other](%s)\n" % OTHER_URL)
        code, report = self.restore_allowing(self.WIDE)
        self.assertEqual(code, 1)
        statuses = {entry["url"]: entry["status"] for entry in report["entries"]}
        self.assertEqual(statuses, {POST_URL: cache.UNCACHED_SKIPPED, OTHER_URL: "unresolved"})

    def test_flag_is_restore_only(self):
        self.guide_with()
        for command in ("refresh", "discover", "import-existing"):
            with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                cache.main([command, "--guide", str(self.guide), "--cache-dir", str(self.root),
                            "--allow-uncached-by-policy", self.WIDE])

    def test_shipped_policy_link_is_only_in_the_k2_widescreen_section(self):
        guide = Path(cache.__file__).resolve().parents[2] / "content" / "k2" / "full.md"
        item = next(each for each in cache.discover([str(guide)])
                    if cache.uncached_reason(each, Path(cache.__file__).with_name("archive_selections.json"))
                    and "/files/file/2503" in each["url"])
        steps, outside = cache.skippable_steps(item, {"Enhanced Widescreen Support Mods"})
        self.assertEqual([step["section"] for step in steps], ["Enhanced Widescreen Support Mods"])
        self.assertTrue(steps[0]["heading"])
        self.assertEqual(outside, [])

    def test_import_existing_does_not_copy_a_policy_listed_link(self):
        entry = cache.import_existing(self.item, {}, self.base, self.root, args_for())
        self.assertEqual(entry["status"], cache.UNCACHED)

    def test_refresh_job_skips_listed_link_before_any_remote_access(self):
        def fail(*args, **kwargs):
            raise AssertionError("remote used")

        with mock.patch.object(refresh_archives, "fetch", fail), \
                mock.patch.object(refresh_archives, "upload_object", fail), \
                mock.patch.object(refresh_archives, "rclone", fail), \
                mock.patch.object(refresh_archives.cache, "main", fail):
            steps = list(refresh_archives.refresh_item(self.item, self.root, "remote:bucket", True))
        self.assertEqual(len(steps), 1)
        self.assertTrue(steps[0]["ok"])
        self.assertEqual(steps[0]["entries"][0]["status"], cache.UNCACHED)

    def test_listed_link_is_never_due_and_is_counted_separately(self):
        self.assertFalse(refresh_archives.is_due({}, 1000.0, self.item))
        self.assertTrue(refresh_archives.is_due({}, 1000.0, item_for(OTHER_URL)))
        summary = refresh_archives.totals([cache.uncached_entry(self.item, self.REASON)], {}, 0.0,
                                          "finished", True, True, 5)
        self.assertEqual((summary["uncached_by_policy"], summary["unresolved"]), (1, 0))

    def test_download_budget_is_capped_even_when_the_disk_is_huge(self):
        other = item_for(OTHER_URL)
        (self.root / "manifest.json").write_text(json.dumps({"schema": 1, "entries": {}}), encoding="utf-8")
        seen = []

        def fake_main(argv):
            seen.append(int(argv[argv.index("--max-bytes") + 1]))
            (self.root.parent / "item-report.json").write_text(json.dumps({"entries": [], "ok": True}))

        usage = types.SimpleNamespace(free=10 ** 15)
        with mock.patch.object(refresh_archives, "rclone", lambda *a, **k: None), \
                mock.patch.object(refresh_archives.shutil, "disk_usage", return_value=usage), \
                mock.patch.object(refresh_archives.cache, "main", fake_main):
            list(refresh_archives.refresh_item(other, self.root, "remote:bucket", False))
        self.assertEqual(seen, [cache.MAX_LINK_BYTES])


NEXUS_POLICY_URL = "https://www.nexusmods.com/examplegame/mods/777?tab=description"
NEXUS_OTHER_URL = "https://www.nexusmods.com/examplegame/mods/778"


class NexusStepPolicyTests(unittest.TestCase):
    REASON = "example registry helper; documented optional and nothing consumes its output"
    SECTION = "Optional Section"

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.base = Path(self.directory.name)
        self.root = self.base / "cache"
        (self.root / "objects").mkdir(parents=True)
        self.selections = self.base / "selections.json"
        self.write_policy({"nexus:777": {"reason": self.REASON}})
        patcher = mock.patch.object(cache, "SELECTIONS_PATH", self.selections)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.guide = self.base / "guide.md"
        self.destination = self.base / "mods"

    def write_policy(self, uncached):
        self.selections.write_text(json.dumps({"uncached": uncached}), encoding="utf-8")

    def write_guide(self, text):
        self.guide.write_text(text, encoding="utf-8")

    def restore(self, *allowances):
        extra = [value for allowance in allowances for value in ("--allow-uncached-by-policy", allowance)]
        output = io.StringIO()
        with mock.patch.object(cache, "request", side_effect=AssertionError("network used")), \
                contextlib.redirect_stdout(output):
            code = cache.main(["restore", "--guide", str(self.guide), "--cache-dir", str(self.root),
                               "--destination", str(self.destination), "--retries", "0", *extra])
        return code, json.loads(output.getvalue())

    def statuses(self, report):
        return {entry["url"].rsplit("/", 1)[-1]: entry["status"] for entry in report["entries"]}

    def two_steps(self):
        self.write_guide("## %s\n\n### Step A\n\n[mod](%s)\n\n### Step B\n\n[mod](%s)\n"
                         % (self.SECTION, NEXUS_POLICY_URL, NEXUS_POLICY_URL))

    def test_nexus_lookup_is_limited_to_the_listed_mod(self):
        self.assertEqual(cache.uncached_reason(item_for(NEXUS_POLICY_URL)), self.REASON)
        self.assertEqual(cache.uncached_reason(item_for("https://www.nexusmods.com/othergame/mods/777")), self.REASON)
        self.assertIsNone(cache.uncached_reason(item_for(NEXUS_OTHER_URL)))
        self.assertIsNone(cache.uncached_reason(item_for("https://deadlystream.com/files/file/777-example/")))

    def test_game_qualified_key_matches_only_that_game(self):
        self.write_policy({"nexus:ExampleGame/777": {"reason": self.REASON}})
        self.assertEqual(cache.uncached_reason(item_for(NEXUS_POLICY_URL)), self.REASON)
        self.assertIsNone(cache.uncached_reason(item_for("https://www.nexusmods.com/othergame/mods/777")))

    def test_malformed_nexus_policy_is_rejected(self):
        for value in ({"nexus:777": {}}, {"nexus:777": {"reason": " "}}, {"nexus:777": "no"},
                      {"nexus:": {"reason": "x"}}, {"nexus:abc": {"reason": "x"}},
                      {"nexus:a/b/777": {"reason": "x"}}, {"nexus:777?tab=files": {"reason": "x"}}):
            self.write_policy(value)
            with self.assertRaises(ValueError):
                cache.load_uncached()

    def test_nexus_link_is_not_fetched_or_imported(self):
        item = item_for(NEXUS_POLICY_URL)
        with mock.patch.object(cache, "request", side_effect=AssertionError("network used")):
            with self.assertRaises(ValueError) as caught:
                cache.refresh_one(item, {}, self.root, args_for())
        self.assertIn("by policy", str(caught.exception))
        self.assertEqual(cache.import_existing(item, {}, self.base, self.root, args_for())["status"], cache.UNCACHED)

    def test_without_the_flag_the_link_fails_closed(self):
        self.two_steps()
        code, report = self.restore()
        self.assertEqual(code, 1)
        self.assertEqual(self.statuses(report), {"777": cache.UNCACHED})
        self.assertNotIn("skipped_steps", report)

    def test_step_form_skips_only_the_named_step(self):
        self.write_guide("## %s\n\n### Step A\n\n[mod](%s)\n" % (self.SECTION, NEXUS_POLICY_URL))
        code, report = self.restore(self.SECTION + "::Step A")
        self.assertEqual(code, 0)
        self.assertEqual(self.statuses(report), {"777": cache.UNCACHED_SKIPPED})
        step = {"guide": str(self.guide), "section": self.SECTION, "heading": "Step A"}
        self.assertEqual(report["entries"][0]["skipped_steps"], [step])
        self.assertEqual(report["skipped_steps"], [{**step, "url": cache.clean_url(NEXUS_POLICY_URL),
                                                    "reason": report["entries"][0]["reason"]}])

    def test_step_form_does_not_cover_a_second_referencing_step(self):
        self.two_steps()
        code, report = self.restore(self.SECTION + "::Step A")
        self.assertEqual(code, 1)
        self.assertEqual(self.statuses(report), {"777": cache.UNCACHED})
        self.assertIn("Step B", report["entries"][0]["reason"])
        self.assertNotIn("skipped_steps", report)

    def test_every_referencing_step_named_individually_is_accepted(self):
        self.two_steps()
        code, report = self.restore(self.SECTION + "::Step A", self.SECTION + "::Step B")
        self.assertEqual(code, 0)
        self.assertEqual([step["heading"] for step in report["skipped_steps"]], ["Step A", "Step B"])

    def test_bare_section_form_still_covers_every_step_in_it(self):
        self.two_steps()
        code, report = self.restore(self.SECTION)
        self.assertEqual(code, 0)
        self.assertEqual([step["heading"] for step in report["skipped_steps"]], ["Step A", "Step B"])

    def test_step_form_requires_the_exact_section_and_heading(self):
        self.write_guide("## %s\n\n### Step A\n\n[mod](%s)\n\n## Other Section\n\n### Step B\n\nprose\n"
                         % (self.SECTION, NEXUS_POLICY_URL))
        for allowance in ("Other Section::Step A", self.SECTION + "::Step B", self.SECTION + "::step a",
                          self.SECTION + "::Step"):
            code, report = self.restore(allowance)
            self.assertEqual(code, 1, allowance)
            self.assertEqual(self.statuses(report), {"777": cache.UNCACHED}, allowance)

    def test_reference_outside_any_step_is_not_covered_by_a_step_form(self):
        self.write_guide("[mod](%s)\n\n## %s\n\n### Step A\n\n[mod](%s)\n"
                         % (NEXUS_POLICY_URL, self.SECTION, NEXUS_POLICY_URL))
        code, report = self.restore(self.SECTION + "::Step A")
        self.assertEqual(code, 1)
        self.assertEqual(self.statuses(report), {"777": cache.UNCACHED})

    def test_step_form_does_not_cover_other_policy_links_in_the_section(self):
        self.write_policy({"nexus:777": {"reason": self.REASON}, "nexus:778": {"reason": self.REASON}})
        self.write_guide("## %s\n\n### Step A\n\n[mod](%s)\n\n### Step B\n\n[mod](%s)\n"
                         % (self.SECTION, NEXUS_POLICY_URL, NEXUS_OTHER_URL))
        code, report = self.restore(self.SECTION + "::Step A")
        self.assertEqual(code, 1)
        self.assertEqual(self.statuses(report), {"777": cache.UNCACHED_SKIPPED,
                                                 "778": cache.UNCACHED})

    def test_malformed_step_form_is_a_usage_error(self):
        self.two_steps()
        for allowance in (self.SECTION + "::", "::Step A", "   ", self.SECTION + "::Step A::Extra"):
            with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                self.restore(allowance)

    def test_unlisted_nexus_link_in_the_named_step_is_still_unresolved(self):
        self.write_guide("## %s\n\n### Step A\n\n[mod](%s) [other](%s)\n"
                         % (self.SECTION, NEXUS_POLICY_URL, NEXUS_OTHER_URL))
        code, report = self.restore(self.SECTION + "::Step A")
        self.assertEqual(code, 1)
        self.assertEqual(self.statuses(report), {"777": cache.UNCACHED_SKIPPED,
                                                 "778": "unresolved"})


class ShippedNexusPolicyTests(unittest.TestCase):
    ROOT = Path(cache.__file__).resolve().parents[2]
    POLICY = Path(cache.__file__).with_name("archive_selections.json")
    ASSISTANT = "Optional Install Assistant - Quicker TSLPatching::Quicker TSLPatching"
    CUTSCENES = "Optional Widescreen & Extended Widescreen Support Mods::Remastered Cutscenes"
    RESCALED = "Enhanced Widescreen Support Mods::K2 Cutscenes Rescaled"

    def policy_items(self, game):
        guide = self.ROOT / "content" / game / "full.md"
        return [item for item in cache.discover([str(guide)]) if cache.uncached_reason(item, self.POLICY)]

    def test_shipped_policy_lists_both_nexus_links_with_reasons(self):
        declared = cache.load_uncached(self.POLICY)
        for key in ("nexus:1214", "nexus:1306", "2503"):
            self.assertTrue(declared[key], key)

    def test_shipped_policy_does_not_list_the_pinned_alternative_cutscene_mod(self):
        self.assertNotIn("nexus:1066", cache.load_uncached(self.POLICY))
        guide = self.ROOT / "content" / "k2" / "full.md"
        alternative = next(each for each in cache.discover([str(guide)])
                           if "/kotor2/mods/1066" in each["url"])
        self.assertIsNone(cache.uncached_reason(alternative, self.POLICY))
        steps, outside = cache.skippable_steps(alternative, (self.RESCALED,))
        self.assertEqual([(step["section"], step["heading"]) for step in steps],
                         [("Enhanced Widescreen Support Mods", "K2 Remastered Cutscenes")])
        self.assertEqual(outside, steps)

    def test_exactly_the_reviewed_steps_are_skipped_and_nothing_else(self):
        for game, allowances in (("k1", (self.ASSISTANT, self.CUTSCENES)),
                                 ("k2", (self.ASSISTANT, self.RESCALED))):
            skipped = {}
            kept = []
            for item in self.policy_items(game):
                steps, outside = cache.skippable_steps(item, allowances)
                if steps and not outside:
                    skipped[cache.clean_url(item["url"])] = sorted((step["section"], step["heading"]) for step in steps)
                else:
                    kept.append(item["url"])
            with self.subTest(game=game):
                self.assertEqual(skipped, {
                    "https://www.nexusmods.com/kotor/mods/1214": [
                        ("Optional Install Assistant - Quicker TSLPatching", "Quicker TSLPatching")],
                    **({"https://www.nexusmods.com/kotor/mods/1306": [
                        ("Optional Widescreen & Extended Widescreen Support Mods", "Remastered Cutscenes")]}
                       if game == "k1" else {
                        "https://deadlystream.com/files/file/2503-k2-cutscenes-rescaled/": [
                            ("Enhanced Widescreen Support Mods", "K2 Cutscenes Rescaled")]})})
                self.assertEqual(kept, [])

    def test_the_assistant_flag_alone_leaves_the_cutscene_links_uncached(self):
        for item in self.policy_items("k1"):
            if item["url"].endswith("/mods/1306"):
                steps, outside = cache.skippable_steps(item, (self.ASSISTANT,))
                self.assertEqual(outside, steps)

    def test_the_k2_cutscene_flag_is_step_scoped_and_needs_the_assistant_flag_too(self):
        item = next(each for each in self.policy_items("k2") if "/files/file/2503" in each["url"])
        steps, outside = cache.skippable_steps(item, (self.ASSISTANT,))
        self.assertEqual(outside, steps)
        steps, outside = cache.skippable_steps(item, (self.RESCALED,))
        self.assertEqual(outside, [])
        steps, outside = cache.skippable_steps(item, ("Enhanced Widescreen Support Mods::K2 Remastered Cutscenes",))
        self.assertEqual(outside, steps)
        nexus = next(each for each in self.policy_items("k2") if "/kotor/mods/1214" in each["url"])
        self.assertEqual(cache.skippable_steps(nexus, (self.RESCALED,))[1],
                         cache.skippable_steps(nexus, ())[1])

    def test_restore_reports_the_reviewed_steps_skipped_in_each_real_guide(self):
        for game, allowances in (("k1", (self.ASSISTANT, self.CUTSCENES)),
                                 ("k2", (self.ASSISTANT, self.RESCALED))):
            guide = self.ROOT / "content" / game / "full.md"
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory) / "cache"
                (root / "objects").mkdir(parents=True)
                extra = [value for each in allowances for value in ("--allow-uncached-by-policy", each)]
                output = io.StringIO()
                with mock.patch.object(cache, "request", side_effect=AssertionError("network used")), \
                        contextlib.redirect_stdout(output):
                    code = cache.main(["restore", "--guide", str(guide), "--cache-dir", str(root),
                                       "--destination", str(Path(directory) / "mods"), "--retries", "0", *extra])
            report = json.loads(output.getvalue())
            with self.subTest(game=game):
                self.assertEqual(code, 1)
                skipped = sorted(entry["url"] for entry in report["entries"] if entry["status"] == cache.UNCACHED_SKIPPED)
                self.assertEqual(skipped, self.EXPECTED_SKIPPED[game])
                self.assertEqual(sorted(step["heading"] for step in report["skipped_steps"]),
                                 sorted(self.EXPECTED_HEADINGS[game]))
                kept = [entry["url"] for entry in report["entries"] if entry["status"] == cache.UNCACHED]
                self.assertEqual(kept, [])

    EXPECTED_SKIPPED = {
        "k1": ["https://www.nexusmods.com/kotor/mods/1214",
               "https://www.nexusmods.com/kotor/mods/1306"],
        "k2": ["https://deadlystream.com/files/file/2503-k2-cutscenes-rescaled/",
               "https://www.nexusmods.com/kotor/mods/1214"]}
    EXPECTED_HEADINGS = {"k1": ["Quicker TSLPatching", "Remastered Cutscenes"],
                         "k2": ["K2 Cutscenes Rescaled", "Quicker TSLPatching"]}


class SiblingLinkRestoreNameTests(unittest.TestCase):
    PAGE_URL = "https://www.gamefront.com/games/example/file/example-mod"
    PATCH_URL = "https://www.dropbox.com/s/abc123/Example%20Mod%20Patch.zip?dl=0"
    SHARED = "Example Mod Patch.zip"

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.base = Path(self.directory.name)
        self.root = self.base / "cache"
        (self.root / "objects").mkdir(parents=True)
        self.destination = self.base / "mods"
        self.payloads = {"page": zip_bytes("p"), "patch": zip_bytes("q"), "other": zip_bytes("r")}
        self.entries = {}
        self.pins = {}

    def stage(self, url, key, name, pinned=False):
        payload = self.payloads[key]
        sha = cache.hashlib.sha256(payload).hexdigest()
        (self.root / "objects" / sha).write_bytes(payload)
        item = item_for(url)
        entry = {"sha256": sha, "size": len(payload), "extension": "zip", "archive_name": name,
                 "status": "verified", "evidence": "download-without-upstream-metadata",
                 "checked_at": cache.time.time(), "metadata_unknown": True}
        if pinned:
            entry.update(status="pinned", evidence="operator-seed")
            self.pins[item["id"]] = {"provider": item["provider"], "url": url, "provenance": cache.PIN_PROVENANCE,
                                     "objects": [{"archive_name": name, "sha256": sha, "size": len(payload),
                                                  "extension": "zip"}]}
        self.entries[item["id"]] = entry
        return sha

    def restore(self, *blocks):
        guide = self.base / "guide.md"
        guide.write_text("".join("### %s\n\n%s\n\n" % (heading, "\n".join("[x](%s)" % url for url in urls))
                                 for heading, urls in blocks), encoding="utf-8")
        (self.root / "manifest.json").write_text(json.dumps({"schema": 1, "entries": self.entries}),
                                                 encoding="utf-8")
        output = io.StringIO()
        with mock.patch.object(cache, "load_pins", return_value=self.pins), contextlib.redirect_stdout(output):
            code = cache.main(["restore", "--guide", str(guide), "--cache-dir", str(self.root),
                               "--destination", str(self.destination), "--retries", "0"])
        return code, {each["id"]: each for each in json.loads(output.getvalue())["entries"]}

    def test_pinned_link_keeps_its_name_and_sibling_gets_a_distinct_one_in_either_order(self):
        page_id, patch_id = item_for(self.PAGE_URL)["id"], item_for(self.PATCH_URL)["id"]
        for urls in ((self.PAGE_URL, self.PATCH_URL), (self.PATCH_URL, self.PAGE_URL)):
            self.entries.clear()
            self.pins.clear()
            page_sha = self.stage(self.PAGE_URL, "page", self.SHARED, pinned=True)
            patch_sha = self.stage(self.PATCH_URL, "patch", self.SHARED)
            if self.destination.exists():
                for path in self.destination.iterdir():
                    path.unlink()
            code, report = self.restore(("Example Mod", urls))
            self.assertEqual(code, 0)
            self.assertEqual(report[page_id]["file"], self.SHARED)
            self.assertEqual(report[patch_id]["file"], "Example Mod Patch-%s.zip" % patch_id[:8])
            self.assertEqual(cache.digest(self.destination / self.SHARED), page_sha)
            self.assertEqual(cache.digest(self.destination / report[patch_id]["file"]), patch_sha)
            self.assertEqual(len(list(self.destination.iterdir())), 2)

    def test_unpinned_siblings_prefer_the_link_whose_own_name_it_is(self):
        page_id, patch_id = item_for(self.PAGE_URL)["id"], item_for(self.PATCH_URL)["id"]
        self.stage(self.PAGE_URL, "page", self.SHARED)
        self.stage(self.PATCH_URL, "patch", self.SHARED)
        code, report = self.restore(("Example Mod", (self.PAGE_URL, self.PATCH_URL)))
        self.assertEqual(code, 0)
        self.assertEqual(report[patch_id]["file"], self.SHARED)
        self.assertEqual(report[page_id]["file"], "Example Mod Patch-%s.zip" % page_id[:8])

    def test_names_without_a_collision_are_unchanged(self):
        page_id = item_for(self.PAGE_URL)["id"]
        self.stage(self.PAGE_URL, "page", "example_page.zip", pinned=True)
        self.stage(self.PATCH_URL, "patch", self.SHARED)
        code, report = self.restore(("Example Mod", (self.PAGE_URL, self.PATCH_URL)))
        self.assertEqual(code, 0)
        self.assertEqual(report[page_id]["file"], "example_page.zip")
        self.assertEqual(sorted(path.name for path in self.destination.iterdir()),
                         ["Example Mod Patch.zip", "example_page.zip"])

    def test_same_name_from_different_mods_still_collides(self):
        self.stage(self.PAGE_URL, "page", self.SHARED, pinned=True)
        self.stage(self.PATCH_URL, "patch", self.SHARED)
        code, report = self.restore(("Example Mod", (self.PAGE_URL,)), ("Other Mod", (self.PATCH_URL,)))
        self.assertEqual(code, 1)
        self.assertEqual({row["status"] for row in report.values()}, {"restored", "unresolved"})
        self.assertEqual(len(list(self.destination.iterdir())), 1)

    def test_unpinned_siblings_both_owning_a_name_fall_back_to_guide_order(self):
        other = "https://www.dropbox.com/s/zzz999/Example%20Mod%20Patch.zip?dl=0"
        self.stage(self.PATCH_URL, "patch", self.SHARED)
        self.stage(other, "other", self.SHARED)
        code, report = self.restore(("Example Mod", (self.PATCH_URL, other)))
        self.assertEqual(code, 0)
        self.assertEqual(sorted(path.name for path in self.destination.iterdir()),
                         sorted([self.SHARED, "Example Mod Patch-%s.zip" % item_for(other)["id"][:8]]))


class ArchiveMapTests(MultiFileRestoreTests):
    def restore_map(self):
        map_path = self.base / "archive-map.json"
        code, report = self.run_main("restore", "--destination", str(self.destination), "--retries", "0",
                                     "--archive-map", str(map_path))
        return code, report, json.loads(map_path.read_text())

    def test_bundle_maps_canonical_url_to_every_restored_file(self):
        entry = self.bundle()
        entry.update(status="verified", evidence="download-without-upstream-metadata", checked_at=cache.time.time())
        self.seed(entry)
        code, _, mapping = self.restore_map()
        self.assertEqual((code, mapping), (0, {POST_URL: list(self.NAMES)}))

    def test_unresolved_link_is_left_out_of_the_map(self):
        code, report, mapping = self.restore_map()
        self.assertEqual((code, report["entries"][0]["status"]), (1, "unresolved"))
        self.assertEqual(mapping, {})

    def test_archive_map_requires_restore(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            cache.main(["refresh", "--guide", str(self.guide), "--archive-map", str(self.base / "m.json")])


class DirectLinkTests(unittest.TestCase):
    PAGE = "https://example.com/?page_id=7"
    DIRECT = "https://example.com/files/example_tool.zip"

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.base = Path(self.directory.name)
        self.root = self.base / "cache"
        (self.root / "objects").mkdir(parents=True)
        self.payload = self.zip_bytes()
        self.selections = self.base / "selections.json"
        self.write_links({self.PAGE: self.link()})
        patcher = mock.patch.object(cache, "SELECTIONS_PATH", self.selections)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.guide = self.base / "guide.md"
        self.guide.write_text("[Tool](%s)\n[Other](https://example.com/about)\n" % self.PAGE, encoding="utf-8")

    def zip_bytes(self):
        path = self.base / "payload.zip"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("tool.exe", b"MZ" + bytes(range(200)))
        return path.read_bytes()

    def link(self, **overrides):
        value = {"url": self.DIRECT, "file": "example_tool.zip",
                 "sha256": cache.hashlib.sha256(self.payload).hexdigest(), "size": len(self.payload),
                 "reason": "static vendor zip"}
        value.update(overrides)
        return value

    def write_links(self, links):
        self.selections.write_text(json.dumps({"direct_links": links}), encoding="utf-8")

    def response(self, payload, url=None):
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.code = 200
        response.headers = {"Content-Type": "application/zip", "Content-Length": str(len(payload))}
        response.geturl.return_value = url or self.DIRECT
        stream = [payload, b""]
        response.read.side_effect = lambda size=-1: stream.pop(0) if stream else b""
        return response

    def test_reviewed_landing_link_is_an_archive_link_and_others_are_not(self):
        self.assertTrue(cache.archive_link(self.PAGE))
        self.assertFalse(cache.archive_link("https://example.com/?page_id=8"))
        self.assertFalse(cache.archive_link("https://example.com/about"))

    def test_unreviewed_landing_link_is_dropped_without_the_table(self):
        self.selections.write_text("{}", encoding="utf-8")
        self.assertFalse(cache.archive_link(self.PAGE))
        self.assertEqual(cache.discover([self.guide]), [])

    def test_discover_keeps_only_the_reviewed_link(self):
        items = cache.discover([self.guide])
        self.assertEqual([(item["url"], item["provider"]) for item in items], [(self.PAGE, "http")])

    def test_resolve_returns_the_reviewed_direct_url_without_fetching_the_page(self):
        with mock.patch.object(cache, "request", side_effect=AssertionError("page must not be fetched")):
            self.assertEqual(cache.resolve(item_for(self.PAGE), args_for()), self.DIRECT)

    def test_refresh_stores_the_reviewed_file_name_and_hash(self):
        with mock.patch.object(cache, "request", return_value=self.response(self.payload)) as opened:
            entry = cache.refresh_one(item_for(self.PAGE), {}, self.root, args_for())
        self.assertEqual(opened.call_args.args[0], self.DIRECT)
        self.assertEqual((entry["archive_name"], entry["extension"], entry["size"]),
                         ("example_tool.zip", "zip", len(self.payload)))
        self.assertEqual(entry["sha256"], self.link()["sha256"])
        self.assertTrue(cache.local_valid(self.root, entry))

    def test_refresh_rejects_content_that_differs_from_the_reviewed_hash(self):
        changed = self.zip_bytes() + b"\x00"
        with mock.patch.object(cache, "request", return_value=self.response(changed)):
            with self.assertRaisesRegex(ValueError, "reviewed direct link"):
                cache.refresh_one(item_for(self.PAGE), {}, self.root, args_for())
        self.assertEqual(list((self.root / "objects").iterdir()), [])

    def test_refresh_rejects_a_size_that_differs_from_the_review(self):
        self.write_links({self.PAGE: self.link(size=len(self.payload) + 1)})
        with mock.patch.object(cache, "request", return_value=self.response(self.payload)):
            with self.assertRaisesRegex(ValueError, "reviewed direct link"):
                cache.refresh_one(item_for(self.PAGE), {}, self.root, args_for())

    def test_restore_writes_the_archive_map_entry_for_the_link(self):
        with mock.patch.object(cache, "request", return_value=self.response(self.payload)):
            entry = cache.refresh_one(item_for(self.PAGE), {}, self.root, args_for())
        item = item_for(self.PAGE)
        (self.root / "manifest.json").write_text(json.dumps({"schema": 1, "entries": {item["id"]: entry}}),
                                                 encoding="utf-8")
        map_path = self.base / "archive-map.json"
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = cache.main(["restore", "--guide", str(self.guide), "--cache-dir", str(self.root),
                               "--destination", str(self.base / "mods"), "--archive-map", str(map_path)])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(map_path.read_text()), {cache.clean_url(self.PAGE): "example_tool.zip"})
        self.assertEqual((self.base / "mods" / "example_tool.zip").read_bytes(), self.payload)

    def test_schema_rejects_unsafe_or_incomplete_entries(self):
        for override in ({"url": "http://example.com/files/example_tool.zip"},
                         {"url": "https://other.example.org/files/example_tool.zip"},
                         {"url": "https://example.com/files/example_tool.zip?x=1"},
                         {"url": "https://example.com/files/page.html"},
                         {"file": "../example_tool.zip"}, {"file": "example_tool.exe"},
                         {"sha256": "A" * 64}, {"sha256": "abc"}, {"size": 0}, {"size": True},
                         {"reason": " "}):
            self.write_links({self.PAGE: self.link(**override)})
            with self.assertRaises(ValueError, msg=str(override)):
                cache.load_direct_links()
        incomplete = self.link()
        del incomplete["reason"]
        self.write_links({self.PAGE: incomplete})
        with self.assertRaises(ValueError):
            cache.load_direct_links()
        self.write_links({"ftp://example.com/x": self.link()})
        with self.assertRaises(ValueError):
            cache.load_direct_links()

    def test_other_providers_never_use_the_table(self):
        self.write_links({DS_PAGE: self.link(url="https://deadlystream.com/files/a.zip")})
        self.assertIsNone(cache.reviewed_direct(item_for(DS_PAGE)))


class ShippedDirectLinkTests(unittest.TestCase):
    ROOT = Path(cache.__file__).resolve().parents[2]
    POLICY = Path(cache.__file__).with_name("archive_selections.json")

    def test_every_shipped_direct_link_is_valid_and_reaches_discovery_in_both_games(self):
        shipped = cache.load_direct_links(self.POLICY)
        self.assertTrue(shipped)
        for game in ("k1", "k2"):
            found = {item["id"]: item for item in cache.discover([str(self.ROOT / "content" / game / "full.md")])}
            for identity, link in shipped.items():
                self.assertIn(identity, found, game)
                self.assertEqual(found[identity]["provider"], "http")
                self.assertEqual(cache.resolve(found[identity], args_for()), link["url"])

    def test_shipped_direct_links_are_hash_pinned_archives(self):
        for link in cache.load_direct_links(self.POLICY).values():
            self.assertRegex(link["sha256"], r"^[a-f0-9]{64}$")
            self.assertTrue(cache.ARCHIVE.search(link["file"]))
            self.assertGreater(link["size"], 0)


class DropboxTests(unittest.TestCase):
    def test_dropbox_preview_link_resolves_to_direct_download(self):
        url = "https://www.dropbox.com/scl/fi/abc/pack.zip?rlkey=KEY&dl=0"
        self.assertEqual(cache.provider(url), ("dropbox", False))
        direct = cache.dropbox_direct(url)
        self.assertIn("dl=1", direct)
        self.assertIn("rlkey=KEY", direct)
        self.assertNotIn("dl=0", direct)



class CommentFormRecaptchaTests(unittest.TestCase):
    def test_guest_comment_form_is_not_a_challenge(self):
        page = '<html><title>x</title><textarea id="g-recaptcha-response" class="g-recaptcha-response"></textarea></html>'
        self.assertIsNone(cache.re.search(r"cf-turnstile|g-recaptcha(?!-response)|h-captcha", page, cache.re.I))
        self.assertIsNotNone(cache.re.search(r"cf-turnstile|g-recaptcha(?!-response)|h-captcha",
                                             '<div class="g-recaptcha" data-sitekey="k"></div>', cache.re.I))


if __name__ == "__main__":
    unittest.main()
