"""Case-insensitive plan paths (PyKotor's CaseAwarePath) and archive member names."""
import hashlib
import importlib.util
import io
from pathlib import Path
import tempfile
import time
import unittest
import zipfile

HERE = Path(__file__).resolve().parent


def load(name):
    spec = importlib.util.spec_from_file_location(name, HERE / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


manual = load("manual")


class Fixture(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        self.archives = self.root / "archives"
        self.game = self.root / "game"
        self.extract = self.root / "work" / "extract"
        self.archives.mkdir()
        (self.game / "override").mkdir(parents=True)
        (self.game / "vanilla.txt").write_text("x", encoding="utf-8")
        self.extract.mkdir(parents=True)

    def roots(self):
        return {"game": self.game, "archives": self.archives, "extract": self.extract}

    def run_operation(self, operation, log=None):
        manual.execute(operation, self.roots(), log, time.monotonic() + 60)

    def blocked(self, message, operation):
        with self.assertRaises(manual.Blocked) as caught:
            self.run_operation(operation)
        self.assertIn(message, str(caught.exception))

    def write(self, path, text):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def read(self, path):
        return path.read_text(encoding="utf-8")

    def names(self, directory):
        return sorted(child.name for child in directory.iterdir())


class Copy(Fixture):
    def copy(self, source, destination, **options):
        self.run_operation(dict({"type": "copy", "source": source, "destination": destination}, **options))

    def test_overwrites_differently_cased_file_and_keeps_its_name(self):
        self.write(self.game / "override" / "Appearance.2DA", "old")
        self.write(self.archives / "mod" / "Override" / "appearance.2da", "new")
        self.copy("{archives}/mod/Override", "{game}/OVERRIDE")
        self.assertEqual(self.names(self.game), ["override", "vanilla.txt"])
        self.assertEqual(self.names(self.game / "override"), ["Appearance.2DA"])
        self.assertEqual(self.read(self.game / "override" / "Appearance.2DA"), "new")

    def test_new_entries_keep_source_case(self):
        self.write(self.archives / "mod" / "New.TGA", "tga")
        self.write(self.archives / "mod" / "Sub" / "X.Txt", "x")
        self.copy("{archives}/mod", "{game}/override")
        self.assertEqual(self.names(self.game / "override"), ["New.TGA", "Sub"])
        self.assertEqual(self.names(self.game / "override" / "Sub"), ["X.Txt"])
        self.write(self.archives / "two" / "sub" / "x.TXT", "y")
        self.copy("{archives}/two", "{game}/Override")
        self.assertEqual(self.names(self.game / "override" / "Sub"), ["X.Txt"])
        self.assertEqual(self.read(self.game / "override" / "Sub" / "X.Txt"), "y")

    def test_new_destination_keeps_given_name(self):
        self.write(self.archives / "mod" / "Intro.BIK", "bik")
        self.copy("{archives}/mod", "{game}/Movies")
        self.assertEqual(self.names(self.game), ["Movies", "override", "vanilla.txt"])
        self.assertEqual(self.names(self.game / "Movies"), ["Intro.BIK"])
        self.write(self.archives / "file" / "Dialog.TLK", "tlk")
        self.write(self.game / "dialog.tlk", "old")
        self.copy("{archives}/file/Dialog.TLK", "{game}/DIALOG.tlk")
        self.assertEqual(self.read(self.game / "dialog.tlk"), "tlk")
        self.assertNotIn("DIALOG.tlk", self.names(self.game))

    def test_ambiguous_existing_entries_block(self):
        self.write(self.game / "override" / "x.txt", "1")
        self.write(self.game / "override" / "X.txt", "2")
        self.write(self.archives / "mod" / "x.TXT", "3")
        self.blocked("differing only in case", {"type": "copy", "source": "{archives}/mod",
                                                "destination": "{game}/override"})
        self.blocked("differing only in case", {"type": "copy", "source": "{archives}/mod/x.TXT",
                                                "destination": "{game}/override/x.txt"})
        self.assertEqual(self.read(self.game / "override" / "x.txt"), "1")
        (self.game / "Override").mkdir()
        self.write(self.archives / "other" / "y.txt", "y")
        self.blocked("differing only in case", {"type": "copy", "source": "{archives}/other",
                                                "destination": "{game}/override"})

    def test_ambiguous_source_entries_block(self):
        self.write(self.archives / "mod" / "a.txt", "1")
        self.write(self.archives / "mod" / "A.TXT", "2")
        self.blocked("copy source has entries differing only in case",
                     {"type": "copy", "source": "{archives}/mod", "destination": "{game}/override"})

    def test_no_name_is_lowercased(self):
        self.write(self.archives / "mod" / "MixedCase.MDL", "m")
        self.copy("{archives}/mod", "{game}/override")
        self.assertEqual(self.names(self.game / "override"), ["MixedCase.MDL"])
        with self.assertRaises(manual.Blocked) as caught:
            manual.validate_operation({"type": "copy", "source": "{archives}/mod",
                                       "destination": "{game}/override", "lowercase_names": True}, self.roots())
        self.assertIn("invalid object fields", str(caught.exception))

    def test_type_conflicts_and_symlinks_block(self):
        self.write(self.archives / "mod" / "override", "file")
        self.blocked("copy destination is a directory", {"type": "copy", "source": "{archives}/mod/override",
                                                         "destination": "{game}/Override"})
        self.write(self.archives / "dir" / "Vanilla.txt" / "inner", "i")
        self.blocked("copy destination is a file", {"type": "copy", "source": "{archives}/dir",
                                                    "destination": "{game}/vanilla.TXT"})
        (self.game / "link").symlink_to(self.game / "override")
        self.write(self.archives / "x" / "a.txt", "a")
        self.blocked("symlink path", {"type": "copy", "source": "{archives}/x", "destination": "{game}/LINK"})

    def test_expected_files_resolve_case_insensitively(self):
        self.write(self.archives / "mod" / "A.txt", "a")
        digest = hashlib.sha256(b"a").hexdigest()
        self.copy("{archives}/mod", "{game}/override", expected_files={"{game}/OVERRIDE/a.TXT": digest})
        self.blocked("SHA256 mismatch", {"type": "copy", "source": "{archives}/mod", "destination": "{game}/override",
                                         "expected_files": {"{game}/override/a.txt": "0" * 64}})


class SkipExisting(Fixture):
    def copy(self, source, destination, **options):
        self.run_operation(dict({"type": "copy", "source": source, "destination": destination}, **options))

    def test_existing_files_are_left_untouched_and_directories_merged(self):
        self.write(self.game / "override" / "Keep.2DA", "vanilla")
        self.write(self.archives / "mod" / "KEEP.2da", "mod")
        self.write(self.archives / "mod" / "Sub" / "New.tga", "tga")
        self.copy("{archives}/mod", "{game}/override", skip_existing=True)
        self.assertEqual(self.names(self.game / "override"), ["Keep.2DA", "Sub"])
        self.assertEqual(self.read(self.game / "override" / "Keep.2DA"), "vanilla")
        self.assertEqual(self.read(self.game / "override" / "Sub" / "New.tga"), "tga")

    def test_single_file_and_default_overwrite(self):
        self.write(self.game / "override" / "a.txt", "vanilla")
        self.write(self.archives / "mod" / "A.txt", "mod")
        self.copy("{archives}/mod/A.txt", "{game}/override/A.TXT", skip_existing=True)
        self.assertEqual(self.read(self.game / "override" / "a.txt"), "vanilla")
        self.copy("{archives}/mod/A.txt", "{game}/override/A.TXT")
        self.assertEqual(self.names(self.game / "override"), ["a.txt"])
        self.assertEqual(self.read(self.game / "override" / "a.txt"), "mod")

    def test_skip_existing_must_be_boolean(self):
        with self.assertRaises(manual.Blocked) as caught:
            manual.validate_operation({"type": "copy", "source": "{archives}/mod",
                                       "destination": "{game}/override", "skip_existing": "yes"}, self.roots())
        self.assertIn("skip_existing must be boolean", str(caught.exception))


class Delete(Fixture):
    def delete(self, path, log=None, **options):
        self.run_operation(dict({"type": "delete", "path": path}, **options), log)

    def test_delete_is_case_insensitive(self):
        self.write(self.game / "override" / "Foo.TGA", "f")
        self.write(self.game / "override" / "Sub" / "x.txt", "x")
        self.delete("{game}/OVERRIDE/foo.tga")
        self.delete("{game}/override/SUB")
        self.assertEqual(self.names(self.game / "override"), [])
        self.blocked("delete target missing", {"type": "delete", "path": "{game}/override/foo.tga"})
        self.delete("{game}/override/foo.tga", missing_ok=True)

    def test_ambiguous_delete_blocks(self):
        self.write(self.game / "override" / "a.txt", "1")
        self.write(self.game / "override" / "A.txt", "2")
        self.blocked("differing only in case", {"type": "delete", "path": "{game}/override/a.TXT"})
        self.blocked("differing only in case", {"type": "delete", "path": "{game}/override/a.txt"})
        self.assertEqual(self.names(self.game / "override"), ["A.txt", "a.txt"])

    def test_extract_paths_can_be_deleted(self):
        self.write(self.extract / "mod" / "tslpatchdata" / "Appearance.2da", "a")
        self.delete("{extract}/mod/TSLPatchData/appearance.2DA")
        self.assertEqual(self.names(self.extract / "mod" / "tslpatchdata"), [])
        with self.assertRaises(manual.Blocked) as caught:
            manual.validate_operation({"type": "delete", "path": "{archives}/a"}, self.roots())
        self.assertIn("wrong root", str(caught.exception))

    def test_if_any_exists_deletes_when_one_condition_is_present(self):
        self.write(self.game / "override" / "Old.tga", "o")
        self.write(self.game / "override" / "Marker.2DA", "m")
        log = io.BytesIO()
        self.delete("{game}/override/old.TGA", log, missing_ok=True,
                    if_any_exists=["{game}/override/missing.txt", "{game}/Override/marker.2da"])
        self.assertEqual(self.names(self.game / "override"), ["Marker.2DA"])
        text = log.getvalue().decode()
        self.assertIn("delete condition met: " + str(self.game / "override" / "Marker.2DA"), text)
        self.assertIn("deleted " + str(self.game / "override" / "Old.tga"), text)

    def test_if_any_exists_skips_when_nothing_is_present(self):
        self.write(self.extract / "mod" / "dialog.tlk", "d")
        log = io.BytesIO()
        self.delete("{extract}/mod/dialog.tlk", log, missing_ok=True, if_any_exists=["{game}/override/absent.2da"])
        self.assertTrue((self.extract / "mod" / "dialog.tlk").is_file())
        self.assertIn("delete skipped: none of if_any_exists is present", log.getvalue().decode())

    def test_if_any_exists_validation(self):
        operation = {"type": "delete", "path": "{game}/override/a", "missing_ok": True}
        for value, message in ((["{game}/x"], None), ([], "nonempty list"), ("{game}/x", "nonempty list"),
                               (["{game}/x", "{game}/x"], "distinct"), (["{extract}/x"], "wrong root"),
                               (["{game}/../x"], "path traversal")):
            if message is None:
                manual.validate_operation(dict(operation, if_any_exists=value), self.roots())
                continue
            with self.assertRaises(manual.Blocked) as caught:
                manual.validate_operation(dict(operation, if_any_exists=value), self.roots())
            self.assertIn(message, str(caught.exception))
        for missing_ok in (False, None):
            candidate = dict(operation, if_any_exists=["{game}/x"])
            if missing_ok is None:
                del candidate["missing_ok"]
            else:
                candidate["missing_ok"] = missing_ok
            with self.assertRaises(manual.Blocked) as caught:
                manual.validate_operation(candidate, self.roots())
            self.assertIn("if_any_exists requires missing_ok: true", str(caught.exception))


class Rename(Fixture):
    def rename(self, source, destination, **options):
        self.run_operation(dict({"type": "rename", "source": source, "destination": destination}, **options))

    def test_source_resolves_case_insensitively_and_new_name_is_kept(self):
        self.write(self.game / "override" / "W_IonRfl_04.MDL", "m")
        self.rename("{game}/override/w_ionrfl_04.mdl", "{game}/Override/w_ionrfl_004.mdl")
        self.assertEqual(self.names(self.game / "override"), ["w_ionrfl_004.mdl"])
        self.assertEqual(self.names(self.game), ["override", "vanilla.txt"])

    def test_existing_destination_matches_case_insensitively(self):
        self.write(self.game / "override" / "a.mdl", "new")
        self.write(self.game / "override" / "B.mdl", "old")
        self.blocked("rename destination exists", {"type": "rename", "source": "{game}/override/a.mdl",
                                                   "destination": "{game}/override/b.MDL"})
        self.rename("{game}/override/A.mdl", "{game}/override/b.MDL", replace=True)
        self.assertEqual(self.names(self.game / "override"), ["B.mdl"])
        self.assertEqual(self.read(self.game / "override" / "B.mdl"), "new")

    def test_case_only_rename_and_ambiguity_block(self):
        self.write(self.game / "override" / "Foo.mdl", "f")
        self.blocked("must differ", {"type": "rename", "source": "{game}/override/Foo.mdl",
                                     "destination": "{game}/override/foo.mdl"})
        self.blocked("outside its source", {"type": "rename", "source": "{game}/Override",
                                            "destination": "{game}/override/inner"})
        self.write(self.game / "override" / "foo.MDL", "g")
        self.blocked("differing only in case", {"type": "rename", "source": "{game}/override/Foo.mdl",
                                                "destination": "{game}/override/bar.mdl"})


class ExtractBackslashes(Fixture):
    def make_zip(self, names):
        path = self.archives / "mod.zip"
        with zipfile.ZipFile(path, "w") as archive:
            for name, data in names:
                archive.writestr(zipfile.ZipInfo(name), data)
        return path

    def unpack(self):
        self.run_operation({"type": "extract", "source": "{archives}/mod.zip", "destination": "{extract}/mod"})

    def test_backslashes_are_directory_separators(self):
        self.make_zip([("Mod Folder\\", b""), ("Mod Folder\\Override\\A.tga", b"a"),
                       ("Mod Folder\\tslpatchdata\\changes.ini", b"c"), ("top.txt", b"t")])
        self.unpack()
        mod = self.extract / "mod"
        self.assertEqual(self.names(mod), ["Mod Folder", "top.txt"])
        self.assertEqual(self.names(mod / "Mod Folder"), ["Override", "tslpatchdata"])
        self.assertEqual((mod / "Mod Folder" / "Override" / "A.tga").read_bytes(), b"a")
        self.assertEqual((mod / "Mod Folder" / "tslpatchdata" / "changes.ini").read_bytes(), b"c")

    def test_unsafe_names_are_rejected_after_normalization(self):
        for name, message in (("..\\evil.txt", "archive traversal"), ("a\\..\\..\\evil.txt", "archive traversal"),
                              ("\\abs.txt", "archive traversal"), ("C:\\evil.txt", "unsafe archive path"),
                              ("c:evil.txt", "unsafe archive path")):
            with self.subTest(name=name):
                self.make_zip([(name, b"x")])
                self.blocked(message, {"type": "extract", "source": "{archives}/mod.zip",
                                       "destination": "{extract}/mod"})
                self.assertFalse((self.extract / "mod").exists())
                self.assertFalse((self.root / "evil.txt").exists())

    def test_backslash_and_slash_spellings_of_one_member_are_duplicates(self):
        self.make_zip([("a/b.txt", b"1"), ("a\\b.txt", b"2")])
        self.blocked("duplicate archive member", {"type": "extract", "source": "{archives}/mod.zip",
                                                  "destination": "{extract}/mod"})

    def test_7z_extracted_backslash_names_are_split(self):
        payload = self.root / "payload"
        payload.mkdir()
        self.write(payload / "Mod\\Override\\A.tga", "a")
        (payload / "Mod\\Sub").mkdir()
        self.write(payload / "Mod\\Sub" / "x\\y.txt", "y")
        manual.split_backslash_names(payload)
        self.assertEqual(self.names(payload), ["Mod"])
        self.assertEqual(self.read(payload / "Mod" / "Override" / "A.tga"), "a")
        self.assertEqual(self.read(payload / "Mod" / "Sub" / "x" / "y.txt"), "y")
        self.write(payload / "Mod\\Override\\A.tga", "b")
        with self.assertRaises(manual.Blocked) as caught:
            manual.split_backslash_names(payload)
        self.assertIn("duplicate 7z member", str(caught.exception))
        (payload / "Mod\\Override\\A.tga").unlink()
        self.write(payload / "..\\up.txt", "u")
        with self.assertRaises(manual.Blocked) as caught:
            manual.split_backslash_names(payload)
        self.assertIn("unsafe 7z archive path", str(caught.exception))


class PykotorRequired(unittest.TestCase):
    def test_missing_pykotor_fails_clearly(self):
        saved = list(manual.PYKOTOR)
        manual.PYKOTOR.clear()
        self.addCleanup(lambda: (manual.PYKOTOR.clear(), manual.PYKOTOR.extend(saved)))
        import builtins
        real_import = builtins.__import__

        def no_pykotor(name, *args, **kwargs):
            if name.startswith("pykotor"):
                raise ImportError("No module named 'pykotor'")
            return real_import(name, *args, **kwargs)

        from unittest import mock
        with mock.patch("builtins.__import__", no_pykotor):
            with self.assertRaises(manual.Blocked) as caught:
                manual.case_aware_path()
        self.assertIn("pip install pykotor==2.3.12", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
