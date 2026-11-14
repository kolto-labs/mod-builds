import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
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
FUTURE_NS = 10 * 10 ** 9


def digest(data):
    return hashlib.sha256(data).hexdigest()


class Temporary(unittest.TestCase):
    def setUp(self):
        manual.DIGEST_CACHE.clear()
        manual.CACHE_CLOCK[0] = 0
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def aged(self):
        """Hash as if the files were written long enough ago to be cacheable."""
        real = time.time_ns
        return mock.patch.object(manual.time, "time_ns", lambda: real() + FUTURE_NS)

    def hashes(self):
        return mock.patch.object(manual.hashlib, "sha256", wraps=hashlib.sha256)


class DigestCache(Temporary):
    def test_unchanged_file_is_read_once(self):
        path = self.root / "a.bin"
        path.write_bytes(b"alpha")
        expected = digest(b"alpha")
        with self.aged(), self.hashes() as counted:
            results = [manual.sha256(path), manual.sha256(path)]
        self.assertEqual(results, [expected, expected])
        self.assertEqual(counted.call_count, 1)

    def test_in_place_rewrite_with_same_size_and_restored_mtime_is_detected(self):
        path = self.root / "a.bin"
        path.write_bytes(b"aaaa")
        with self.aged():
            self.assertEqual(manual.sha256(path), digest(b"aaaa"))
        original = path.stat()
        time.sleep(0.05)
        with path.open("r+b") as handle:
            handle.write(b"bbbb")
        os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))
        changed = path.stat()
        self.assertEqual((changed.st_ino, changed.st_size, changed.st_mtime_ns),
                         (original.st_ino, original.st_size, original.st_mtime_ns))
        if changed.st_ctime_ns == original.st_ctime_ns:
            self.skipTest("filesystem did not advance ctime")
        with self.aged():
            self.assertEqual(manual.sha256(path), digest(b"bbbb"))

    def test_replacement_file_with_same_size_and_mtime_is_detected(self):
        path = self.root / "a.bin"
        path.write_bytes(b"aaaa")
        with self.aged():
            manual.sha256(path)
        original = path.stat()
        other = self.root / "b.bin"
        other.write_bytes(b"bbbb")
        os.utime(other, ns=(original.st_atime_ns, original.st_mtime_ns))
        os.replace(other, path)
        with self.aged():
            self.assertEqual(manual.sha256(path), digest(b"bbbb"))

    def test_recently_changed_file_is_not_cached(self):
        path = self.root / "a.bin"
        path.write_bytes(b"aaaa")
        with self.hashes() as counted:
            manual.sha256(path)
            manual.sha256(path)
        self.assertEqual(counted.call_count, 2)
        self.assertEqual(manual.DIGEST_CACHE, {})

    def test_clock_going_backwards_clears_the_cache(self):
        path = self.root / "a.bin"
        path.write_bytes(b"aaaa")
        with self.aged():
            manual.sha256(path)
        self.assertEqual(len(manual.DIGEST_CACHE), 1)
        with mock.patch.object(manual.time, "time_ns", lambda: 1):
            with self.hashes() as counted:
                manual.sha256(path)
        self.assertEqual(counted.call_count, 1)

    def test_manifest_sees_in_place_change_through_the_cache(self):
        tree = self.root / "tree"
        (tree / "override").mkdir(parents=True)
        target = tree / "override" / "x.2da"
        target.write_bytes(b"row 1\n")
        (tree / "keep.bin").write_bytes(b"keep")
        with self.aged():
            first = manual.manifest(tree)
        stamps = target.stat()
        time.sleep(0.05)
        target.write_bytes(b"row 2\n")
        os.utime(target, ns=(stamps.st_atime_ns, stamps.st_mtime_ns))
        if target.stat().st_ctime_ns == stamps.st_ctime_ns:
            self.skipTest("filesystem did not advance ctime")
        with self.aged(), self.hashes() as counted:
            second = manual.manifest(tree)
        self.assertNotEqual(first["override/x.2da"], second["override/x.2da"])
        self.assertEqual(second["override/x.2da"][3], digest(b"row 2\n"))
        self.assertEqual(first["keep.bin"], second["keep.bin"])
        self.assertEqual(counted.call_count, 1)


class AdvanceSnapshot(Temporary):
    def test_snapshot_follows_every_kind_of_change_and_keeps_unchanged_inodes(self):
        game, snapshot = self.root / "game", self.root / "snapshot"
        for name, data in (("same.bin", b"same"), ("edit.bin", b"old"), ("gone.bin", b"gone"),
                           ("dir/inner.bin", b"inner"), ("kind", b"file"), ("mode.bin", b"m"),
                           ("oldtree/a/b.bin", b"b")):
            (game / name).parent.mkdir(parents=True, exist_ok=True)
            (game / name).write_bytes(data)
        (game / "kind2").mkdir()
        (game / "kind2" / "c.bin").write_bytes(b"c")
        manual.copy_tree(game, snapshot)
        old = manual.manifest(snapshot)
        self.assertEqual(old, manual.manifest(game))
        same_inode = (snapshot / "same.bin").stat().st_ino

        (game / "edit.bin").write_bytes(b"new")
        (game / "gone.bin").unlink()
        manual.remove_for_restore(game / "oldtree")
        (game / "kind").unlink()
        (game / "kind").mkdir()
        (game / "kind" / "nested.bin").write_bytes(b"nested")
        manual.remove_for_restore(game / "kind2")
        (game / "kind2").write_bytes(b"now a file")
        (game / "mode.bin").chmod(0o600)
        (game / "dir").chmod(0o750)
        (game / "fresh" / "deep").mkdir(parents=True)
        (game / "fresh" / "deep" / "f.bin").write_bytes(b"f")
        (game / "empty").mkdir()
        new = manual.manifest(game)

        manual.advance_snapshot(snapshot, game, old, new)
        self.assertEqual(manual.manifest(snapshot), new)
        self.assertEqual((snapshot / "same.bin").stat().st_ino, same_inode)
        self.assertNotEqual((snapshot / "edit.bin").stat().st_ino, (game / "edit.bin").stat().st_ino)

    def test_read_only_directories_are_updated_and_restored(self):
        game, snapshot = self.root / "game", self.root / "snapshot"
        (game / "ro").mkdir(parents=True)
        (game / "ro" / "a.bin").write_bytes(b"a")
        (game / "ro").chmod(0o555)
        self.addCleanup((game / "ro").chmod, 0o755)
        manual.copy_tree(game, snapshot)
        self.addCleanup(lambda: (snapshot / "ro").exists() and (snapshot / "ro").chmod(0o755))
        old = manual.manifest(snapshot)
        (game / "ro").chmod(0o755)
        (game / "ro" / "a.bin").write_bytes(b"changed")
        (game / "ro" / "b.bin").write_bytes(b"b")
        (game / "ro").chmod(0o555)
        new = manual.manifest(game)
        manual.advance_snapshot(snapshot, game, old, new)
        self.assertEqual(manual.manifest(snapshot), new)


def rename_step(index, source, destination, extra=()):
    return {"id": "step-%03d" % index, "source_heading": "### Mod %d" % index,
            "operations": [{"type": "rename", "source": "{game}/" + source,
                            "destination": "{game}/" + destination}, *extra]}


class ExecutorSnapshots(Temporary):
    def setUp(self):
        super().setUp()
        self.guide = self.root / "guide.md"
        self.guide.write_text("# Build\n\n## Mod List\n\n### Mod 1\n\n### Mod 2\n\n### Mod 3\n",
                              encoding="utf-8")
        self.archives, self.game = self.root / "archives", self.root / "game"
        self.archives.mkdir()
        (self.game / "override").mkdir(parents=True)
        (self.game / "override" / "a.txt").write_text("a", encoding="utf-8")
        (self.game / "chitin.key").write_bytes(b"\0" * 4096)

    def run_executor(self, steps, keep=None):
        plan = {"guide_sha256": manual.sha256(self.guide), "archive_sha256": {}, "steps": steps,
                "supplemental_sections": []}
        plan_path = self.root / "plan.json"
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        work, report = self.root / "work", self.root / "out" / "report.json"
        command = [sys.executable, str(HERE / "manual.py"), "--plan", str(plan_path),
                   "--guide", str(self.guide), "--game-dir", str(self.game),
                   "--archives", str(self.archives), "--work-dir", str(work), "--report", str(report)]
        if keep is not None:
            command += ["--keep-snapshots", str(keep)]
        result = subprocess.run(command, capture_output=True, text=True, timeout=120)
        return result, json.loads(report.read_text(encoding="utf-8")), work

    def test_default_advances_one_snapshot_through_the_checkpoints(self):
        steps = [rename_step(1, "override/a.txt", "override/b.txt"),
                 rename_step(2, "override/b.txt", "override/c.txt"),
                 rename_step(3, "override/c.txt", "override/d.txt")]
        result, report, work = self.run_executor(steps)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([step["snapshot_retained"] for step in report["steps"]], [False, False, True])
        self.assertEqual(report["steps"][0]["snapshot_advanced_to"], str(work / "step-0002"))
        self.assertEqual(report["steps"][2]["snapshot_advanced_from"], str(work / "step-0002"))
        self.assertFalse((work / "step-0001" / "before").exists())
        before = json.loads((work / "step-0003" / "before.json").read_text(encoding="utf-8"))
        self.assertEqual(manual.manifest(work / "step-0003" / "before"), before)
        self.assertIn("override/c.txt", before)

    def test_failed_step_restores_from_an_advanced_snapshot(self):
        failing = [{"type": "delete", "path": "{game}/missing.txt"}]
        steps = [rename_step(1, "override/a.txt", "override/b.txt"),
                 rename_step(2, "override/b.txt", "override/c.txt"),
                 rename_step(3, "chitin.key", "moved.key", failing)]
        result, report, work = self.run_executor(steps)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(report["steps"][2]["status"], "rolled_back")
        self.assertEqual(sorted(path.relative_to(self.game).as_posix() for path in self.game.rglob("*")),
                         ["chitin.key", "override", "override/c.txt"])
        self.assertEqual(manual.manifest(self.game),
                         json.loads((work / "step-0003" / "before.json").read_text(encoding="utf-8")))

    def test_keep_more_than_one_uses_full_copies(self):
        steps = [rename_step(1, "override/a.txt", "override/b.txt"),
                 rename_step(2, "override/b.txt", "override/c.txt"),
                 rename_step(3, "override/c.txt", "override/d.txt")]
        result, report, work = self.run_executor(steps, keep=2)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([step["snapshot_retained"] for step in report["steps"]], [False, True, True])
        self.assertNotIn("snapshot_advanced_to", report["steps"][0])
        for index in (2, 3):
            checkpoint = work / ("step-%04d" % index)
            self.assertEqual(manual.manifest(checkpoint / "before"),
                             json.loads((checkpoint / "before.json").read_text(encoding="utf-8")))


if __name__ == "__main__":
    unittest.main()
