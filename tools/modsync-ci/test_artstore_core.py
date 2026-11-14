import copy
import hashlib
import importlib.util
import itertools
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location("artstore_core", Path(__file__).with_name("artstore_core.py"))
core = importlib.util.module_from_spec(spec)
spec.loader.exec_module(core)

KEY = "k" * 40
OTHER_KEY = "z" * 40
PAYLOAD = bytes(range(256)) * 10 + b"tail"
DAY = 86400
NOW = 1_800_000_000


def blob(sha, packs, size=10):
    return {"sha256": sha, "size": size,
            "parts": [{"n": n, "size": 5, "stored_sha256": "%s%02d" % (sha[:-2], n),
                       "stored_size": 80} for n, _ in enumerate(packs)]}


def index_with(*entries):
    index = core.new_index()
    for pack_name, sha, created, expires, artifact in entries:
        core.add_pack(index, pack_name, [blob(sha, [pack_name])], None,
                      created_at=created, expires_at=expires, artifact_id=artifact)
    return index


class SealedFixture(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        patcher = mock.patch.dict(os.environ, {"CACHE_ENCRYPTION_KEY": KEY})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.directory.cleanup)
        self.source = self.root / "source.bin"
        self.source.write_bytes(PAYLOAD)


class PartTests(SealedFixture):
    def seal_one(self):
        sealed = self.root / "one.sealed"
        record = core.seal_part(self.source, sealed)
        return sealed, record

    def test_round_trip_and_record(self):
        sealed, record = self.seal_one()
        data = sealed.read_bytes()
        self.assertEqual(record["stored_size"], len(data))
        self.assertEqual(record["stored_sha256"], hashlib.sha256(data).hexdigest())
        self.assertTrue(data.startswith(core.MAGIC))
        self.assertNotIn(PAYLOAD[:64], data)
        plain = self.root / "plain.bin"
        self.assertIsNone(core.open_part(sealed, plain))
        self.assertEqual(plain.read_bytes(), PAYLOAD)

    def test_each_seal_uses_fresh_salt(self):
        first, _ = self.seal_one()
        second = self.root / "two.sealed"
        core.seal_part(self.source, second)
        self.assertNotEqual(first.read_bytes(), second.read_bytes())

    def test_magic_is_not_seal_envelope(self):
        spec_seal = importlib.util.spec_from_file_location("seal", Path(__file__).with_name("seal.py"))
        seal = importlib.util.module_from_spec(spec_seal)
        spec_seal.loader.exec_module(seal)
        self.assertNotEqual(core.MAGIC, seal.MAGIC)
        sealed, _ = self.seal_one()
        with self.assertRaises(ValueError):
            seal.unseal(sealed, self.root / "tree")

    def test_tamper_is_rejected_without_releasing_plaintext(self):
        sealed, _ = self.seal_one()
        data = bytearray(sealed.read_bytes())
        for position in (len(core.MAGIC) + 3, len(data) // 2, len(data) - 1):
            mutated = bytearray(data)
            mutated[position] ^= 0x01
            sealed.write_bytes(bytes(mutated))
            plain = self.root / "plain.bin"
            with self.assertRaises(ValueError):
                core.open_part(sealed, plain)
            self.assertFalse(plain.exists())
        self.assertEqual([p.name for p in self.root.iterdir() if p.name.startswith(".artstore-")], [])

    def test_trailing_garbage_is_rejected(self):
        sealed, _ = self.seal_one()
        sealed.write_bytes(sealed.read_bytes() + b"x")
        with self.assertRaises(ValueError):
            core.open_part(sealed, self.root / "plain.bin")

    def test_truncated_part_is_rejected(self):
        sealed, _ = self.seal_one()
        data = sealed.read_bytes()
        plain = self.root / "plain.bin"
        for cut in (1, 16, 33, len(data) - len(core.MAGIC) - 10, len(data) - 5):
            sealed.write_bytes(data[:len(data) - cut] if cut < len(data) else b"")
            with self.assertRaises(ValueError):
                core.open_part(sealed, plain)
            self.assertFalse(plain.exists())
        sealed.write_bytes(b"")
        with self.assertRaises(ValueError):
            core.open_part(sealed, plain)

    def test_wrong_key_is_rejected(self):
        sealed, _ = self.seal_one()
        plain = self.root / "plain.bin"
        with mock.patch.dict(os.environ, {"CACHE_ENCRYPTION_KEY": OTHER_KEY}):
            with self.assertRaises(ValueError):
                core.open_part(sealed, plain)
        self.assertFalse(plain.exists())

    def test_short_or_missing_key_is_rejected(self):
        with mock.patch.dict(os.environ, {"CACHE_ENCRYPTION_KEY": "short"}):
            with self.assertRaises(ValueError):
                core.seal_part(self.source, self.root / "x.sealed")
        env = dict(os.environ)
        env.pop("CACHE_ENCRYPTION_KEY")
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(ValueError):
                core.seal_part(self.source, self.root / "x.sealed")

    def test_custom_key_env(self):
        with mock.patch.dict(os.environ, {"ARTSTORE_TEST_KEY": OTHER_KEY}):
            sealed = self.root / "custom.sealed"
            core.seal_part(self.source, sealed, key_env="ARTSTORE_TEST_KEY")
            with self.assertRaises(ValueError):
                core.open_part(sealed, self.root / "bad.bin")
            core.open_part(sealed, self.root / "ok.bin", key_env="ARTSTORE_TEST_KEY")
        self.assertEqual((self.root / "ok.bin").read_bytes(), PAYLOAD)


class SplitJoinTests(SealedFixture):
    def split(self, part_size=1000):
        outdir = self.root / "pack"
        return outdir, core.split_and_seal(self.source, outdir, part_size)

    def test_multi_part_round_trip(self):
        outdir, record = self.split(1000)
        self.assertEqual(record["sha256"], hashlib.sha256(PAYLOAD).hexdigest())
        self.assertEqual(record["size"], len(PAYLOAD))
        self.assertEqual([part["n"] for part in record["parts"]], [0, 1, 2])
        self.assertEqual([part["size"] for part in record["parts"]], [1000, 1000, len(PAYLOAD) - 2000])
        self.assertEqual(sorted(path.name for path in outdir.iterdir()),
                         sorted(part["stored_sha256"] + ".part" for part in record["parts"]))
        for part in record["parts"]:
            stored = outdir / (part["stored_sha256"] + ".part")
            self.assertEqual(hashlib.sha256(stored.read_bytes()).hexdigest(), part["stored_sha256"])
            self.assertEqual(stored.stat().st_size, part["stored_size"])
        plains = []
        for part in record["parts"]:
            plain = self.root / ("plain%d" % part["n"])
            core.open_part(outdir / (part["stored_sha256"] + ".part"), plain)
            plains.append(plain)
        dest = self.root / "joined.bin"
        core.join_parts(plains, dest, record["sha256"], record["size"])
        self.assertEqual(dest.read_bytes(), PAYLOAD)

    def test_join_sealed_parts_directly(self):
        outdir, record = self.split(1000)
        paths = [outdir / (part["stored_sha256"] + ".part") for part in record["parts"]]
        dest = self.root / "joined.bin"
        core.join_parts(paths, dest, record["sha256"], record["size"], sealed=True)
        self.assertEqual(dest.read_bytes(), PAYLOAD)

    def test_part_size_boundaries(self):
        for part_size, expected in ((len(PAYLOAD), 1), (len(PAYLOAD) + 1, 1), (len(PAYLOAD) - 1, 2)):
            record = core.split_and_seal(self.source, self.root / ("p%d" % part_size), part_size)
            self.assertEqual(len(record["parts"]), expected)
            self.assertEqual(sum(part["size"] for part in record["parts"]), len(PAYLOAD))

    def test_default_part_size(self):
        self.assertEqual(core.PART_SIZE_DEFAULT, 256 * 1024 * 1024)
        record = core.split_and_seal(self.source, self.root / "pack")
        self.assertEqual(len(record["parts"]), 1)

    def test_invalid_part_size(self):
        for bad in (0, -1, 1.5):
            with self.assertRaises(ValueError):
                core.split_and_seal(self.source, self.root / "pack", bad)

    def test_bik_refused(self):
        for name in ("movie.bik", "MOVIE.BIK", "a.Bik"):
            path = self.root / name
            path.write_bytes(b"BIKi")
            with self.assertRaises(ValueError):
                core.split_and_seal(path, self.root / "pack", 100)
        self.assertFalse((self.root / "pack").exists())

    def test_non_file_refused(self):
        with self.assertRaises(ValueError):
            core.split_and_seal(self.root, self.root / "pack", 100)
        with self.assertRaises(ValueError):
            core.split_and_seal(self.root / "missing.bin", self.root / "pack", 100)

    def test_join_mismatch_raises_and_leaves_no_dest(self):
        outdir, record = self.split(1000)
        plains = []
        for part in record["parts"]:
            plain = self.root / ("plain%d" % part["n"])
            core.open_part(outdir / (part["stored_sha256"] + ".part"), plain)
            plains.append(plain)
        dest = self.root / "joined.bin"
        with self.assertRaises(ValueError):
            core.join_parts(plains, dest, "0" * 64, record["size"])
        with self.assertRaises(ValueError):
            core.join_parts(plains, dest, record["sha256"], record["size"] + 1)
        with self.assertRaises(ValueError):
            core.join_parts(plains, dest, record["sha256"], record["size"] - 1)
        with self.assertRaises(ValueError):
            core.join_parts(plains[:-1], dest, record["sha256"], record["size"])
        self.assertFalse(dest.exists())
        self.assertEqual([p.name for p in self.root.iterdir() if p.name.startswith(".artstore-")], [])

    def test_join_preserves_existing_dest_on_failure(self):
        outdir, record = self.split(1000)
        paths = [outdir / (part["stored_sha256"] + ".part") for part in record["parts"]]
        dest = self.root / "joined.bin"
        dest.write_bytes(b"old")
        with self.assertRaises(ValueError):
            core.join_parts(paths, dest, "0" * 64, record["size"], sealed=True)
        self.assertEqual(dest.read_bytes(), b"old")

    def test_join_rejects_tampered_sealed_part(self):
        outdir, record = self.split(1000)
        paths = [outdir / (part["stored_sha256"] + ".part") for part in record["parts"]]
        data = bytearray(paths[1].read_bytes())
        data[len(data) // 2] ^= 0xFF
        paths[1].write_bytes(bytes(data))
        with self.assertRaises(ValueError):
            core.join_parts(paths, self.root / "joined.bin", record["sha256"], record["size"], sealed=True)
        self.assertFalse((self.root / "joined.bin").exists())


class IndexTests(unittest.TestCase):
    def test_new_index_and_add_pack(self):
        index = core.new_index()
        self.assertEqual(index["schema"], core.INDEX_SCHEMA)
        record = blob("a" * 64, ["p"])
        core.add_pack(index, "artstore-pack-1", [record], {record["parts"][0]["stored_sha256"]: "m"},
                      created_at=NOW, expires_at=NOW + 90 * DAY)
        found = core.lookup(index, "a" * 64)
        self.assertEqual(found["parts"][0]["pack"], "artstore-pack-1")
        self.assertEqual(index["packs"]["artstore-pack-1"],
                         {"artifact_id": None, "created_at": NOW, "expires_at": NOW + 90 * DAY, "size": 80})
        self.assertIsNone(core.lookup(index, "b" * 64))
        self.assertNotIn("pack", record["parts"][0])

    def test_add_pack_rejects_unmapped_part(self):
        with self.assertRaises(ValueError):
            core.add_pack(core.new_index(), "p", [blob("a" * 64, ["p"])], {})

    def test_set_pack_artifact(self):
        index = index_with(("p", "a" * 64, NOW, None, None))
        core.set_pack_artifact(index, "p", 12345, NOW + DAY)
        self.assertEqual(index["packs"]["p"]["artifact_id"], 12345)
        self.assertEqual(index["packs"]["p"]["expires_at"], NOW + DAY)
        with self.assertRaises(ValueError):
            core.set_pack_artifact(index, "missing", 1, 1)

    def test_dump_load_round_trip_sorted_and_atomic(self):
        index = index_with(("p2", "b" * 64, NOW, NOW + DAY, 2), ("p1", "a" * 64, NOW, NOW + DAY, 1))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sub" / "index.json"
            core.dump_index(index, path)
            self.assertEqual(core.load_index(path), index)
            text = path.read_text()
            self.assertEqual(text, json.dumps(json.loads(text), sort_keys=True, indent=1) + "\n")
            self.assertEqual([p.name for p in path.parent.iterdir()], ["index.json"])

    def test_unknown_schema_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "index.json"
            path.write_text(json.dumps({"schema": 99, "blobs": {}, "packs": {}}))
            with self.assertRaises(ValueError):
                core.load_index(path)
            path.write_text(json.dumps({"blobs": {}, "packs": {}}))
            with self.assertRaises(ValueError):
                core.load_index(path)
            with self.assertRaises(ValueError):
                core.dump_index({"schema": 2, "blobs": {}, "packs": {}}, path)
            with self.assertRaises(ValueError):
                core.merge_indexes([{"schema": 2, "blobs": {}, "packs": {}}])

    def test_merge_union(self):
        merged = core.merge_indexes([index_with(("p1", "a" * 64, NOW, NOW + DAY, 1)),
                                     index_with(("p2", "b" * 64, NOW, NOW + DAY, 2))])
        self.assertEqual(sorted(merged["blobs"]), ["a" * 64, "b" * 64])
        self.assertEqual(sorted(merged["packs"]), ["p1", "p2"])

    def test_merge_order_independent(self):
        sha = "c" * 64
        variants = [
            index_with(("old", sha, NOW - 50 * DAY, NOW + 40 * DAY, 1), ("only", "d" * 64, NOW, NOW + DAY, None)),
            index_with(("new", sha, NOW - DAY, NOW + 89 * DAY, 2), ("old", sha, NOW - 50 * DAY, None, None)),
            index_with(("old", "e" * 64, NOW - 50 * DAY, NOW - DAY, 1)),
            index_with(("new", sha, NOW - DAY, NOW + 89 * DAY, None)),
        ]
        results = {json.dumps(core.merge_indexes(list(order), now_epoch=NOW), sort_keys=True)
                   for order in itertools.permutations(variants)}
        self.assertEqual(len(results), 1)
        results = {json.dumps(core.merge_indexes(list(order)), sort_keys=True)
                   for order in itertools.permutations(variants)}
        self.assertEqual(len(results), 1)

    def test_merge_carries_deferred_with_the_oldest_since_and_drops_stored_blobs(self):
        first, second, third = "a" * 64, "b" * 64, "c" * 64
        one = core.new_index()
        core.set_deferred(one, {first: {"since": 200, "size": 1}, second: {"since": 100, "size": 2}})
        two = index_with(("p", second, 1, None, 1))
        core.set_deferred(two, {first: {"since": 50, "size": 1}, third: {"since": 70, "size": 3}})
        merged = core.merge_indexes([one, two])
        self.assertEqual(core.deferred_map(merged), {first: {"since": 50, "size": 1}, third: {"since": 70, "size": 3}})
        self.assertEqual(merged, core.merge_indexes([two, one]))

    def test_deferred_round_trips_and_malformed_rows_are_ignored(self):
        index = core.new_index()
        core.set_deferred(index, {"d" * 64: {"since": 5, "size": 9}})
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "index.json"
            core.dump_index(index, path)
            self.assertEqual(core.deferred_map(core.load_index(path)), {"d" * 64: {"since": 5, "size": 9}})
        index["deferred"] = {"short": {"since": 1}, "e" * 64: {"since": True}, "f" * 64: "x", "1" * 64: {"since": 3}}
        self.assertEqual(core.deferred_map(index), {"1" * 64: {"since": 3, "size": 0}})
        core.set_deferred(index, {})
        self.assertNotIn("deferred", index)

    def test_merge_does_not_mutate_inputs(self):
        first = index_with(("p1", "a" * 64, NOW, NOW + DAY, 1))
        snapshot = copy.deepcopy(first)
        merged = core.merge_indexes([first])
        merged["packs"]["p1"]["artifact_id"] = 99
        self.assertEqual(first, snapshot)

    def test_merge_prefers_pack_with_artifact_id_then_later_created(self):
        sha = "a" * 64
        merged = core.merge_indexes([index_with(("p", sha, NOW, None, None)),
                                     index_with(("p", sha, NOW - 10, NOW + DAY, 7))])
        self.assertEqual(merged["packs"]["p"]["artifact_id"], 7)
        merged = core.merge_indexes([index_with(("p", sha, NOW, NOW + 5 * DAY, 7)),
                                     index_with(("p", sha, NOW + 1, NOW + 6 * DAY, 8))])
        self.assertEqual(merged["packs"]["p"]["artifact_id"], 8)

    def test_merge_prefers_non_expired_blob_record(self):
        sha = "a" * 64
        stale = index_with(("stale", sha, NOW - 95 * DAY, NOW - 5 * DAY, 1))
        fresh = index_with(("fresh", sha, NOW - 10 * DAY, NOW + 80 * DAY, 2))
        for order in ([stale, fresh], [fresh, stale]):
            merged = core.merge_indexes(order, now_epoch=NOW)
            self.assertEqual(core.lookup(merged, sha)["parts"][0]["pack"], "fresh")
            merged = core.merge_indexes(order)
            self.assertEqual(core.lookup(merged, sha)["parts"][0]["pack"], "fresh")

    def test_blobs_needing_refresh(self):
        index = core.new_index()
        for pack_name, sha, expires in (("p1", "1" * 64, NOW + 40 * DAY), ("p2", "2" * 64, NOW + 5 * DAY),
                                        ("p3", "3" * 64, NOW - DAY), ("p4", "4" * 64, NOW + 7 * DAY),
                                        ("p5", "5" * 64, None)):
            core.add_pack(index, pack_name, [blob(sha, [pack_name])], None, created_at=NOW - DAY,
                          expires_at=expires, artifact_id=1)
        self.assertEqual(core.blobs_needing_refresh(index, NOW, 7), ["2" * 64, "3" * 64, "4" * 64])
        self.assertEqual(core.blobs_needing_refresh(index, NOW, 0), ["3" * 64])
        self.assertEqual(core.blobs_needing_refresh(index, NOW, 60), ["1" * 64, "2" * 64, "3" * 64, "4" * 64])

    def test_blobs_needing_refresh_unknown_pack(self):
        index = index_with(("p", "a" * 64, NOW, NOW + 90 * DAY, 1))
        del index["packs"]["p"]
        self.assertEqual(core.blobs_needing_refresh(index, NOW, 1), ["a" * 64])

    def test_blobs_missing(self):
        index = index_with(("p", "a" * 64, NOW, NOW + DAY, 1))
        self.assertEqual(core.blobs_missing(index, ["a" * 64, "b" * 64, "c" * 64, "b" * 64]),
                         ["b" * 64, "c" * 64])
        self.assertEqual(core.blobs_missing(index, []), [])

    def test_drop_expired(self):
        index = core.new_index()
        core.add_pack(index, "dead", [blob("a" * 64, ["dead"])], None, created_at=1, expires_at=NOW - 1, artifact_id=1)
        core.add_pack(index, "live", [blob("b" * 64, ["live"])], None, created_at=1, expires_at=NOW + DAY, artifact_id=2)
        core.add_pack(index, "forever", [blob("c" * 64, ["forever"])], None, created_at=1)
        core.drop_expired(index, NOW)
        self.assertEqual(sorted(index["packs"]), ["forever", "live"])
        self.assertEqual(sorted(index["blobs"]), ["b" * 64, "c" * 64])


if __name__ == "__main__":
    unittest.main()
