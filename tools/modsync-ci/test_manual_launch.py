"""How manual.py launches original patchers, and copy overlays that PyKotor cannot name."""
import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent


def load(name):
    spec = importlib.util.spec_from_file_location(name, HERE / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


manual = load("manual")

# Stands in for "wine HoloPatcher.exe ... --install". Under Wine a frozen Windows
# Python aborts at startup when stdout/stderr is a regular file, so this fake refuses
# anything but pipes, and a stdin other than /dev/null. FAKE_MODE selects what it
# does after that.
FAKE_WINE = """#!/bin/sh
if [ ! -p /dev/stdout ] || [ ! -p /dev/stderr ]; then
  echo "Fatal Python error: init_sys_streams: can't initialize sys standard streams" >&2
  exit 1
fi
[ "$(readlink /proc/self/fd/0)" = /dev/null ] || exit 2
echo "out: $*"
echo "err: started" >&2
case "$FAKE_MODE" in
  success) printf 'Installation complete\\n' > "$FAKE_LOG"; echo patched > "$FAKE_GAME/override/new.2da" ;;
  hang) exec sleep 600 ;;
  holder) setsid sleep 3 & printf 'Installation complete\\n' > "$FAKE_LOG"
          echo patched > "$FAKE_GAME/override/new.2da" ;;
  fail) exit 3 ;;
esac
exit 0
"""


class Fixture(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        self.archives = self.root / "archives"
        self.game = self.root / "game"
        self.work = self.root / "work"
        self.extract = self.work / "extract"
        self.archives.mkdir()
        (self.game / "override").mkdir(parents=True)
        (self.game / "vanilla.txt").write_text("x", encoding="utf-8")
        self.extract.mkdir(parents=True)

    def roots(self):
        return {"game": self.game, "archives": self.archives, "extract": self.extract}


@unittest.skipUnless(Path("/proc/self/fd/0").exists(), "needs /proc")
class PatcherLaunch(Fixture):
    def setUp(self):
        super().setUp()
        self.wine = self.root / "bin" / "wine"
        self.wine.parent.mkdir()
        self.wine.write_text(FAKE_WINE, encoding="utf-8")
        self.wine.chmod(0o755)
        self.mod = self.extract / "mod"
        self.mod.mkdir()
        (self.mod / "HoloPatcher.exe").write_bytes(b"MZ frozen python")
        self.log_path = self.work / "operations.log"

    def operation(self):
        return {"type": "patcher", "patcher": "{extract}/mod/HoloPatcher.exe",
                "sha256": manual.sha256(self.mod / "HoloPatcher.exe"),
                "argv": [str(self.wine), "{extract}/mod/HoloPatcher.exe", "--install"],
                "cwd": "{extract}/mod", "success_log": "{extract}/mod/installlog.txt",
                "success_markers": ["Installation complete"], "failure_regex": ["(?m)^Error"],
                "expected_changes": "changed_manifest"}

    def run_patcher(self, mode, timeout=60):
        environment = {"FAKE_MODE": mode, "FAKE_LOG": str(self.mod / "installlog.txt"),
                       "FAKE_GAME": str(self.game)}
        with mock.patch.dict(os.environ, environment), self.log_path.open("ab") as log:
            log.write(b"[manual] before\n")
            manual.execute(self.operation(), self.roots(), log, time.monotonic() + timeout)

    def test_output_reaches_the_log_through_a_pipe(self):
        self.run_patcher("success")
        log = self.log_path.read_bytes()
        self.assertTrue(log.startswith(b"[manual] before\n"))
        self.assertIn(b"HoloPatcher.exe --install\n", log)
        self.assertIn(b"err: started\n", log)
        self.assertNotIn(b"init_sys_streams", log)
        self.assertEqual((self.game / "override" / "new.2da").read_text(encoding="utf-8"), "patched\n")

    def test_exit_code_still_counts(self):
        with self.assertRaises(manual.Blocked) as caught:
            self.run_patcher("fail")
        self.assertIn("original patcher exit code: 3", str(caught.exception))
        self.assertIn(b"HoloPatcher.exe --install\n", self.log_path.read_bytes())

    def test_deadline_kills_the_group(self):
        started = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            self.run_patcher("hang", timeout=1)
        self.assertLess(time.monotonic() - started, 15)

    def test_descendant_holding_the_pipe_does_not_block(self):
        started = time.monotonic()
        self.run_patcher("holder")
        self.assertLess(time.monotonic() - started, 2.5)


class CopyNames(Fixture):
    def copy(self, source, destination):
        manual.execute({"type": "copy", "source": source, "destination": destination},
                       self.roots(), None, time.monotonic() + 60)

    def test_names_pykotor_cannot_represent_are_copied_as_listed(self):
        source = self.extract / "mod" / "override"
        source.mkdir(parents=True)
        for name in ("trailing ", 'quote"', "back\\slash", "Plain.2DA"):
            (source / name).write_text(name, encoding="utf-8")
        (self.game / "override" / "plain.2da").write_text("old", encoding="utf-8")
        (self.game / "override" / "trailing").write_text("keep", encoding="utf-8")
        self.copy("{extract}/mod/override", "{game}/Override")
        names = sorted(child.name for child in (self.game / "override").iterdir())
        self.assertEqual(names, ["back\\slash", "plain.2da", 'quote"', "trailing", "trailing "])
        self.assertEqual((self.game / "override" / "plain.2da").read_text(encoding="utf-8"), "Plain.2DA")
        self.assertEqual((self.game / "override" / "trailing").read_text(encoding="utf-8"), "keep")
        self.assertEqual((self.game / "override" / "trailing ").read_text(encoding="utf-8"), "trailing ")

    def test_every_name_is_resolved_before_anything_is_written(self):
        source = self.extract / "mod"
        source.mkdir()
        for index in range(20):
            (source / ("f%02d.tga" % index)).write_text("x", encoding="utf-8")
        events = []
        aware = manual.case_aware_path()
        copy_file = manual.copy_file

        def resolving(path):
            events.append("resolve")
            return aware(path)

        def copying(*arguments):
            events.append("copy")
            return copy_file(*arguments)

        with mock.patch.object(manual, "PYKOTOR", [resolving, manual.PYKOTOR[1]]), \
                mock.patch.object(manual, "copy_file", copying):
            self.copy("{extract}/mod", "{game}/override")
        resolves = [index for index, event in enumerate(events) if event == "resolve"]
        copies = [index for index, event in enumerate(events) if event == "copy"]
        self.assertEqual(len(copies), 20)
        self.assertLess(max(resolves), min(copies))


if __name__ == "__main__":
    unittest.main()
