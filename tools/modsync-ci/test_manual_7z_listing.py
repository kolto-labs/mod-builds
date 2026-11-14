"""7z/RAR member listing checks (`7z l -slt -ba`) used before extracting with 7-Zip."""
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest

HERE = Path(__file__).resolve().parent


def load(name):
    spec = importlib.util.spec_from_file_location(name, HERE / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


manual = load("manual")

# Two members of "URCMTP 1.3.rar" (RAR5, K2 step-003) as 7-Zip 25.01 lists them.
# Every RAR5 record carries the link and stream properties, unset, and ends with
# an empty "NT Security" line.
RAR5_FILE = """Path = Complete Installer/URCMTP.exe
Folder = -
Size = 1058222
Packed Size = 971779
Modified = 2020-11-29 15:08:37.0846555
Created = 
Accessed = 
Attributes = A
Alternate Stream = -
Encrypted = -
Solid = -
Split Before = -
Split After = -
CRC = D18BD641
Host OS = Windows
Method = v6:m3:2M
Characteristics = CRC Time:M
Symbolic Link = 
Hard Link = 
Copy Link = 
Volume Index = 
Checksum = 
NT Security = 
"""

RAR5_DIRECTORY = """Path = Individual component installer/tslpatchdata/1-Kaevee_Removal/Part_2
Folder = +
Size = 0
Packed Size = 0
Modified = 2020-11-29 08:30:56.1738890
Created = 
Accessed = 
Attributes = D
Alternate Stream = -
Encrypted = -
Solid = -
Split Before = -
Split After = -
CRC = 
Host OS = Windows
Method = 
Characteristics = Time:M
Symbolic Link = 
Hard Link = 
Copy Link = 
Volume Index = 
Checksum = 
NT Security = 
"""


def listing(*records):
    return "\n".join(records) + "\n"


def member(text, **changes):
    metadata = manual.parse_7z_listing(text)[0]
    metadata.update(changes)
    return metadata


class Listing(unittest.TestCase):
    def test_rar5_members_with_unset_link_and_stream_properties_pass(self):
        records = manual.parse_7z_listing(listing(RAR5_FILE, RAR5_DIRECTORY))
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["Symbolic Link"], "")
        self.assertEqual(records[0]["Alternate Stream"], "-")
        self.assertEqual(records[1]["NT Security"], "")
        self.assertEqual(manual.check_7z_member(records[0]),
                         ("Complete Installer/URCMTP.exe", False, 1058222))
        self.assertEqual(manual.check_7z_member(records[1]),
                         ("Individual component installer/tslpatchdata/1-Kaevee_Removal/Part_2",
                          True, 0))

    def test_trailing_empty_property_survives_whitespace_trimming(self):
        records = manual.parse_7z_listing(listing(RAR5_FILE).rstrip())
        self.assertEqual(records[0]["NT Security"], "")

    def test_malformed_and_repeated_lines_are_rejected(self):
        for text in ("Path = a\nnonsense\n", "Path = a\nPath = b\n", " = x\n"):
            with self.subTest(text=text), self.assertRaisesRegex(manual.Blocked, "ambiguous"):
                manual.parse_7z_listing(text)

    def test_real_links_and_alternate_streams_are_rejected(self):
        for changes in ({"Symbolic Link": "../../etc/passwd"},
                        {"Hard Link": "Complete Installer/URCMTP.exe"},
                        {"Copy Link": "Complete Installer/URCMTP.exe"},
                        {"Alternate Stream": "+"}):
            with self.subTest(changes=changes), \
                    self.assertRaisesRegex(manual.Blocked, "7z link or stream member"):
                manual.check_7z_member(member(RAR5_FILE, **changes))

    def test_tar_hard_link_and_special_mode_are_rejected(self):
        tar = ("Path = hl\nFolder = -\nSize = 0\nPacked Size = 0\nModified = 2026-10-20 15:35:31\n"
               "Created = \nAccessed = \nMode = -rw-r--r--\nUser = \nGroup = \nUser ID = 0\n"
               "Group ID = 0\nSymbolic Link = \nHard Link = \nCharacteristics = 0 GNU ASCII\n"
               "Comment = \nDevice Major = \nDevice Minor = \n")
        self.assertEqual(manual.check_7z_member(member(tar)), ("hl", False, 0))
        with self.assertRaisesRegex(manual.Blocked, "7z link or stream member"):
            manual.check_7z_member(member(tar, **{"Hard Link": "f"}))
        with self.assertRaisesRegex(manual.Blocked, "special 7z member"):
            manual.check_7z_member(member(tar, Mode="prw-r--r--"))

    def test_unix_mode_and_windows_attributes(self):
        allowed = ("A", "RHSA", "AL", " -rw-r--r--", "A -rwxr-xr-x",
                   # High 16 bits without 7-Zip's Unix extension flag print as hex
                   # and carry no file type, even when they spell S_IFLNK.
                   "AU 12240000", "AvXPUMB 81A40000", "A A1FF0000")
        for attributes in allowed:
            with self.subTest(attributes=attributes):
                manual.check_7z_member(member(RAR5_FILE, Attributes=attributes))
        self.assertTrue(manual.check_7z_member(
            member(RAR5_DIRECTORY, Attributes="D drwxr-xr-x"))[1])
        for attributes in (" lrwxrwxrwx", "A lrwxrwxrwx", " prw-r--r--", "A crw-rw-rw-"):
            with self.subTest(attributes=attributes), \
                    self.assertRaisesRegex(manual.Blocked, "special 7z member"):
                manual.check_7z_member(member(RAR5_FILE, Attributes=attributes))

    def test_unsafe_paths_and_encryption_still_rejected(self):
        for path in ("../x", "/abs", "a/./b", "file.txt:stream"):
            with self.subTest(path=path), self.assertRaisesRegex(manual.Blocked, "unsafe"):
                manual.check_7z_member(member(RAR5_FILE, Path=path))
        with self.assertRaisesRegex(manual.Blocked, "encrypted"):
            manual.check_7z_member(member(RAR5_FILE, Encrypted="+"))


@unittest.skipUnless(shutil.which("7z"), "7z not installed")
class Extract(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        self.source = self.root / "source"
        (self.source / "sub").mkdir(parents=True)
        (self.source / "sub" / "file.txt").write_text("data")
        (self.source / "tool.exe").write_bytes(b"MZ")
        os.chmod(self.source / "tool.exe", 0o755)

    def pack(self, *extra):
        archive = self.root / "mod.7z"
        subprocess.run(["7z", "a", *extra, str(archive), "sub", "tool.exe"] +
                       (["link"] if (self.source / "link").is_symlink() else []),
                       cwd=self.source, check=True, stdout=subprocess.DEVNULL)
        return archive

    def test_unix_built_archive_extracts(self):
        destination = self.root / "out"
        manual.extract(self.pack(), destination, time.monotonic() + 120)
        self.assertEqual((destination / "sub" / "file.txt").read_text(), "data")
        self.assertEqual((destination / "tool.exe").read_bytes(), b"MZ")

    def test_stored_symlink_is_rejected(self):
        (self.source / "link").symlink_to("tool.exe")
        archive = self.pack("-snl")
        with self.assertRaisesRegex(manual.Blocked, "special 7z member"):
            manual.extract(archive, self.root / "out", time.monotonic() + 120)


if __name__ == "__main__":
    unittest.main()
