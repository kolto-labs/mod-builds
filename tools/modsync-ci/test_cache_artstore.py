import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import time
import types
import unittest
from unittest import mock

import artstore_client
import artstore_core as core
import test_artstore as store_fakes
import test_cache_sources as sources

cache = sources.cache

TOKENS = ("ARTSTORE_TOKEN", "GITHUB_TOKEN", "GH_TOKEN")
DAY = 86400


def fake_blob(sha):
    return {"sha256": sha, "size": 1, "parts": [{"n": 0, "size": 1, "stored_sha256": sha[::-1], "stored_size": 1}]}


class FakeStore:
    """Stands in for artstore.py with its real signatures; the index is a real artstore_core index."""

    def __init__(self, payload=None, fail=None, live=(), soon=(), days_left=3, index_error=None, client=True):
        self.payload, self.fail, self.index_error = payload, fail, index_error
        self.live, self.soon, self.days_left = tuple(live), tuple(soon), days_left
        self.client = object() if client else None
        self.calls, self.indexes, self.clients, self.index_loads = [], [], [], []

    def make_index(self):
        index = core.new_index()
        now = time.time()
        for name, shas, days in (("live", self.live, 100), ("soon", self.soon, self.days_left)):
            if shas:
                core.add_pack(index, name, [fake_blob(sha) for sha in shas], expires_at=now + days * DAY, artifact_id=1)
        return index

    def load_index(self, client=None, *, keep=3, now=None):
        self.index_loads.append(client)
        if self.index_error:
            raise self.index_error
        return self.make_index()

    def fetch(self, sha256, dest, *, client=None, index=None, workdir=None, key_env="CACHE_ENCRYPTION_KEY", workers=4):
        self.calls.append((sha256, Path(dest).name))
        self.indexes.append(index)
        self.clients.append(client)
        if self.fail:
            raise self.fail
        payload = self.payload.get(sha256) if isinstance(self.payload, dict) else self.payload
        if payload is None:
            return False
        Path(dest).write_bytes(payload)
        return True

    def module(self, **extra):
        members = dict(
            fetch=self.fetch, load_index=self.load_index, core=core,
            refresh_plan=lambda index, now, within_days=21: core.blobs_needing_refresh(index, now, within_days),
            client_mod=types.SimpleNamespace(from_environment=lambda: self.client))
        return types.SimpleNamespace(**{**members, **extra})


class Fixture(sources.Fixture):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in TOKENS:
            os.environ.pop(name, None)
        patcher = mock.patch.dict(cache.ARTSTORE_STATE, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(cache, "request", side_effect=AssertionError("must not contact the website"))
        self.no_website = patcher.start()
        self.addCleanup(patcher.stop)

    def install(self, store):
        cache.ARTSTORE_STATE["module"] = store if isinstance(store, types.SimpleNamespace) else store.module()

    def lose_object(self):
        site = sources.Site({sources.URL: self.payload})
        with mock.patch.object(cache, "request", site):
            code, report = self.run_main("refresh")
        self.assertEqual(code, 0, report)
        (self.root / "objects" / self.sha).unlink()


class TierTests(Fixture):
    def test_artstore_comes_before_the_website_and_is_reported(self):
        self.lose_object()
        store = FakeStore(self.payload)
        self.install(store)
        code, report = self.run_main("restore", "--artstore")
        self.assertEqual((code, report["entries"][0]["source"], report["sources"]), (0, "artstore", {"artstore": 1}))
        self.assertEqual(store.calls, [(self.sha, self.sha)])
        self.assertEqual(self.restored(), self.payload)

    def test_artstore_comes_before_remote_and_p2p(self):
        self.lose_object()
        self.install(FakeStore(self.payload))
        def remote(args, source, destination):
            self.assertTrue(source.endswith("/manifest.json"), source)
            return False

        with mock.patch.object(cache, "remote_copy", remote), \
                mock.patch.object(cache, "try_p2p", side_effect=AssertionError("p2p")):
            code, report = self.run_main("restore", "--artstore", "--remote", "MODCACHE:bucket")
        self.assertEqual((code, report["entries"][0]["source"]), (0, "artstore"))

    def test_a_miss_falls_through_to_p2p_then_recovered(self):
        self.lose_object()
        store = FakeStore()
        self.install(store)

        def swarm(item, dest):
            dest.write_bytes(self.payload)
            return True

        with mock.patch.object(cache, "try_p2p", swarm):
            code, report = self.run_main("restore", "--artstore")
        self.assertEqual((code, report["entries"][0]["source"]), (0, "recovered"))
        self.assertEqual(len(store.calls), 1)

    def test_a_miss_everywhere_uses_the_website(self):
        self.lose_object()
        self.install(FakeStore())
        site = sources.Site({sources.URL: self.payload})
        with mock.patch.object(cache, "request", site):
            code, report = self.run_main("restore", "--artstore")
        self.assertEqual((code, report["entries"][0]["source"]), (0, "website"))
        self.assertEqual(self.restored(), self.payload)

    def test_a_bad_blob_is_never_accepted(self):
        self.lose_object()
        self.install(FakeStore(b"not the archive"))
        site = sources.Site({sources.URL: self.payload})
        with mock.patch.object(cache, "request", site):
            code, report = self.run_main("restore", "--artstore")
        self.assertEqual((code, report["entries"][0]["source"]), (0, "website"))
        self.assertEqual(self.restored(), self.payload)
        self.assertEqual((self.root / "objects" / self.sha).read_bytes(), self.payload)

    def test_a_valid_local_object_never_asks_the_store(self):
        store = FakeStore(self.payload)
        self.install(store)
        site = sources.Site({sources.URL: self.payload})
        with mock.patch.object(cache, "request", site):
            self.run_main("refresh", "--artstore")
            code, report = self.run_main("restore", "--artstore")
        self.assertEqual((code, store.calls), (0, []))
        self.assertNotEqual(report["entries"][0]["source"], "artstore")

    def test_a_hash_the_manifest_does_not_know_never_asks_the_store(self):
        store = FakeStore(self.payload)
        self.install(store)
        site = sources.Site({sources.URL: self.payload})
        with mock.patch.object(cache, "request", site):
            code, report = self.run_main("restore", "--artstore")
        self.assertEqual((code, report["entries"][0]["source"], store.calls), (0, "website", []))

    def test_no_artstore_skips_the_store_even_with_a_token(self):
        self.lose_object()
        os.environ["GITHUB_TOKEN"] = "placeholder"
        store = FakeStore(self.payload)
        self.install(store)
        site = sources.Site({sources.URL: self.payload})
        with mock.patch.object(cache, "request", site):
            code, report = self.run_main("restore", "--no-artstore")
        self.assertEqual((code, report["entries"][0]["source"], store.calls), (0, "website", []))

    def test_default_is_on_with_any_token_and_off_without(self):
        self.lose_object()
        for name in TOKENS:
            with self.subTest(name=name):
                (self.root / "objects" / self.sha).unlink(missing_ok=True)
                os.environ[name] = "placeholder"
                self.install(FakeStore(self.payload))
                code, report = self.run_main("restore")
                del os.environ[name]
                self.assertEqual((code, report["entries"][0]["source"]), (0, "artstore"))
        (self.root / "objects" / self.sha).unlink(missing_ok=True)
        store = FakeStore(self.payload)
        self.install(store)
        site = sources.Site({sources.URL: self.payload})
        with mock.patch.object(cache, "request", site):
            code, report = self.run_main("restore")
        self.assertEqual((code, report["entries"][0]["source"], store.calls), (0, "website", []))

    def test_a_raising_store_is_warned_about_once_and_skipped(self):
        self.lose_object()
        store = FakeStore(fail=RuntimeError("secret-token-value"))
        self.install(store)
        errors = io.StringIO()
        site = sources.Site({sources.URL: self.payload})
        with mock.patch.object(cache, "request", site), contextlib.redirect_stderr(errors):
            args = cache.argparse.Namespace(artstore=True, max_bytes=10 ** 9)
            stored = {"sha256": self.sha, "size": len(self.payload)}
            self.assertFalse(cache.try_artstore(stored, self.base / "dest", args))
            self.assertFalse(cache.try_artstore(stored, self.base / "dest", args))
        self.assertEqual(len(store.calls), 1)
        self.assertEqual(errors.getvalue().count("::warning::"), 1)
        self.assertNotIn("secret-token-value", errors.getvalue())

    def test_bik_is_never_restored_from_the_store(self):
        store = FakeStore(b"x")
        self.install(store)
        args = types.SimpleNamespace(artstore=True)
        for field in ("archive_name", "listed_name"):
            stored = {"sha256": self.sha, "size": 1, field: "Movies/INTRO.BIK"}
            self.assertFalse(cache.try_artstore(stored, self.base / "dest", args))
        self.assertEqual(store.calls, [])


class SharedIndexTests(Fixture):
    URLS = (sources.URL, sources.BIG_URL)

    def setUp(self):
        super().setUp()
        self.other = sources.zip_bytes("b")
        self.other_sha = hashlib.sha256(self.other).hexdigest()
        site = sources.Site({sources.URL: self.payload, sources.BIG_URL: self.other})
        with mock.patch.object(cache, "request", site):
            code, report = self.run_main("refresh")
        self.assertEqual(code, 0, report)
        for each in (self.root / "objects").iterdir():
            each.unlink()
        self.site = sources.Site({sources.URL: self.payload, sources.BIG_URL: self.other})

    def restore(self, store):
        self.install(store)
        with mock.patch.object(cache, "request", self.site):
            return self.run_main("restore", "--artstore")

    def test_restoring_many_archives_loads_the_index_once_and_hands_it_to_every_fetch(self):
        store = FakeStore({self.sha: self.payload, self.other_sha: self.other},
                          live=[self.sha, self.other_sha])
        code, report = self.restore(store)
        self.assertEqual((code, report["sources"]), (0, {"artstore": 2}))
        self.assertEqual(len(store.calls), 2)
        self.assertEqual(len(store.index_loads), 1)
        self.assertEqual(store.index_loads, [store.client])
        self.assertEqual(store.clients, [store.client] * 2)
        self.assertIs(store.indexes[0], store.indexes[1])
        self.assertEqual(set(store.indexes[0]["blobs"]), {self.sha, self.other_sha})

    def test_an_unreadable_index_is_one_warning_then_the_website_serves_everything(self):
        store = FakeStore(self.payload, index_error=RuntimeError("secret-token-value"))
        with mock.patch.object(cache, "warn") as warn:
            code, report = self.restore(store)
        self.assertEqual((code, report["sources"]), (0, {"website": 2}))
        self.assertEqual((store.calls, len(store.index_loads)), ([], 1))
        self.assertEqual(warn.call_count, 1)
        self.assertNotIn("secret-token-value", str(warn.call_args))

    def test_a_store_without_credentials_is_one_notice_and_never_loads_or_fetches(self):
        store = FakeStore(self.payload, client=False)
        with mock.patch.object(cache, "notice") as notice, mock.patch.object(cache, "warn") as warn:
            code, report = self.restore(store)
        self.assertEqual((code, report["sources"]), (0, {"website": 2}))
        self.assertEqual((store.calls, store.index_loads), ([], []))
        self.assertEqual((notice.call_count, warn.call_count), (1, 0))


class LoaderTests(Fixture):
    def test_absent_helper_is_one_info_line_and_a_miss(self):
        helper = self.base / "helper"
        helper.mkdir()
        errors = io.StringIO()
        with mock.patch.object(cache, "__file__", str(helper / "cache.py")), contextlib.redirect_stderr(errors):
            self.assertIsNone(cache.artstore_module())
            self.assertIsNone(cache.artstore_module())
            self.assertFalse(cache.try_artstore({"sha256": self.sha, "size": 1}, self.base / "d",
                                                types.SimpleNamespace(artstore=True)))
        self.assertEqual((errors.getvalue().count("::notice::"), errors.getvalue().count("::warning::")), (1, 0))

    def test_broken_helper_is_one_warning_and_a_miss(self):
        helper = self.base / "helper"
        helper.mkdir()
        (helper / "artstore.py").write_text("raise RuntimeError('boom')\n", encoding="utf-8")
        errors = io.StringIO()
        with mock.patch.object(cache, "__file__", str(helper / "cache.py")), contextlib.redirect_stderr(errors):
            self.assertIsNone(cache.artstore_module())
            self.assertIsNone(cache.artstore_module())
        self.assertEqual(errors.getvalue().count("::warning::"), 1)

    def test_helper_without_fetch_is_a_miss(self):
        helper = self.base / "helper"
        helper.mkdir()
        (helper / "artstore.py").write_text("VALUE = 1\n", encoding="utf-8")
        with mock.patch.object(cache, "__file__", str(helper / "cache.py")), contextlib.redirect_stderr(io.StringIO()):
            self.assertIsNone(cache.artstore_module())

    def test_sibling_helper_is_loaded_and_called_with_the_hash_and_destination(self):
        helper = self.base / "helper"
        helper.mkdir()
        (helper / "artstore.py").write_text(
            "from pathlib import Path\n"
            "import types\n"
            "client_mod = types.SimpleNamespace(from_environment=lambda: object())\n"
            "def load_index(client=None, *, keep=3, now=None):\n"
            "    return {}\n"
            "def fetch(sha256, dest, *, client=None, index=None, workdir=None):\n"
            "    Path(dest).write_text(sha256)\n"
            "    return True\n", encoding="utf-8")
        with mock.patch.object(cache, "__file__", str(helper / "cache.py")):
            self.assertTrue(cache.try_artstore({"sha256": self.sha, "size": 1}, self.base / "d",
                                               types.SimpleNamespace(artstore=True)))
        self.assertEqual((self.base / "d").read_text(), self.sha)

    def test_token_detection(self):
        self.assertFalse(cache.artstore_token())
        os.environ["GH_TOKEN"] = "placeholder"
        self.assertTrue(cache.artstore_token())


class RealStoreTests(Fixture):
    URLS = (sources.URL, sources.BIG_URL)
    TOKEN = "FAKE-TOKEN-0123456789abcdef"
    KEY = "k" * 40

    def setUp(self):
        super().setUp()
        self.blob = store_fakes.FakeBlobStore()
        self.api = store_fakes.FakeGitHub(self.blob, self.TOKEN)
        self.addCleanup(self.blob.stop)
        self.addCleanup(self.api.stop)
        patcher = mock.patch.object(artstore_client.time, "sleep", lambda seconds: None)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in [name for name in os.environ if name.lower().endswith("_proxy")]:
            del os.environ[name]
        os.environ.update({"GITHUB_TOKEN": self.TOKEN, "GITHUB_REPOSITORY": "o/r", "GITHUB_API_URL": self.api.url,
                           "CACHE_ENCRYPTION_KEY": self.KEY, "GITHUB_RUN_ID": "7001", "GITHUB_RUN_ATTEMPT": "1",
                           "NO_PROXY": "127.0.0.1"})
        self.other = sources.zip_bytes("b")
        self.other_sha = hashlib.sha256(self.other).hexdigest()
        self.site = sources.Site({sources.URL: self.payload, sources.BIG_URL: self.other})

    def requests(self, path):
        return [each for each in self.api.requests if each["path"] == path]

    def test_stage_upload_then_recover_through_the_real_artstore(self):
        target = self.base / "stage.json"
        with mock.patch.object(cache, "request", self.site):
            code, report = self.run_main("refresh", "--emit-stage-manifest", str(target))
        self.assertEqual(code, 0, report)
        staged = json.loads(target.read_text())
        self.assertEqual({each["sha256"] for each in staged}, {self.sha, self.other_sha})

        store = cache.artstore_module()
        packdir = self.base / "packdir"
        result = store.stage([(each["path"], each["sha256"]) for each in staged], packdir)
        self.assertEqual({blob["sha256"] for blob in result["blobs"]}, {self.sha, self.other_sha})
        for pack in result["packs"]:
            self.api.add(pack["name"], store_fakes.zip_directory(pack["dir"]))
        index_id = self.api.add(result["index_name"], store_fakes.zip_directory(packdir / "index"))

        cache.ARTSTORE_STATE.clear()
        self.assertEqual(cache.stage_candidates(self.root, self.manifest()), [])

        for each in (self.root / "objects").iterdir():
            each.unlink()
        cache.ARTSTORE_STATE.clear()
        before = len(self.api.requests)
        with mock.patch.object(cache, "request", side_effect=AssertionError("must not contact the website")):
            code, report = self.run_main("restore", "--artstore")
        self.assertEqual((code, report["sources"]), (0, {"artstore": 2}))
        self.assertEqual((self.destination / "pack_a.zip").read_bytes(), self.payload)
        self.assertEqual((self.destination / "pack_b.zip").read_bytes(), self.other)
        for sha, data in ((self.sha, self.payload), (self.other_sha, self.other)):
            self.assertEqual((self.root / "objects" / sha).read_bytes(), data)
        index_zip = "%s/%d/zip" % (store_fakes.BASE, index_id)
        self.assertEqual(len([each for each in self.api.requests[before:] if each["path"] == index_zip]), 1)

    def test_a_blob_the_store_does_not_hold_is_staged_and_a_failed_listing_stages_nothing(self):
        with mock.patch.object(cache, "request", self.site):
            self.run_main("refresh")
        self.assertEqual({each["sha256"] for each in cache.stage_candidates(self.root, self.manifest())},
                         {self.sha, self.other_sha})
        cache.ARTSTORE_STATE.clear()
        os.environ["GITHUB_TOKEN"] = "wrong-token-0123456789"
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cache.stage_candidates(self.root, self.manifest()), [])
        self.assertIsNone(cache.ARTSTORE_STATE["session"])


class StageTests(Fixture):
    def setUp(self):
        super().setUp()
        (self.root / "objects").mkdir(parents=True)
        self.shas = {}
        for label in ("a", "b", "c", "d"):
            data = (label * 50).encode()
            sha = hashlib.sha256(data).hexdigest()
            (self.root / "objects" / sha).write_bytes(data)
            self.shas[label] = (sha, len(data))
        self.manifest_value = {"schema": 1, "entries": {
            "one": {"objects": [{"sha256": self.shas["a"][0], "size": self.shas["a"][1]},
                                {"sha256": self.shas["b"][0], "size": self.shas["b"][1]},
                                {"sha256": self.shas["c"][0], "size": self.shas["c"][1],
                                 "archive_name": "Movies/x.bik"}]},
            "two": {"sha256": self.shas["d"][0], "size": self.shas["d"][1]},
            "gone": {"sha256": "e" * 64, "size": 5},
        }}

    def stage(self):
        return cache.stage_candidates(self.root, self.manifest_value)

    def staged(self):
        return sorted(each["sha256"] for each in self.stage())

    def test_missing_and_expiring_live_non_bik_objects_are_staged(self):
        a, b, c, d = (self.shas[label][0] for label in "abcd")
        store = FakeStore(live=[a], soon=[d, "f" * 64])
        self.install(store)
        result = self.stage()
        self.assertEqual(sorted(result, key=lambda each: each["sha256"]),
                         sorted([{"path": str(self.root / "objects" / sha), "sha256": sha} for sha in (b, d)],
                                key=lambda each: each["sha256"]))
        self.assertNotIn(c, [each["sha256"] for each in result])
        self.assertNotIn("e" * 64, [each["sha256"] for each in result])
        self.assertNotIn("f" * 64, [each["sha256"] for each in result])

    def test_the_expiry_window_is_21_days(self):
        a = self.shas["a"][0]
        for days_left, staged in ((20, True), (22, False)):
            with self.subTest(days_left=days_left):
                cache.ARTSTORE_STATE.clear()
                self.install(FakeStore(live=[self.shas[label][0] for label in "bd"], soon=[a], days_left=days_left))
                self.assertEqual(a in self.staged(), staged)

    def test_the_index_is_loaded_once_through_the_client_and_reused(self):
        store = FakeStore(live=[self.shas["a"][0]])
        self.install(store)
        self.stage()
        self.stage()
        self.assertEqual(store.index_loads, [store.client])

    def test_everything_is_staged_when_the_store_has_no_index_yet(self):
        self.install(FakeStore())
        self.assertEqual(self.staged(), sorted(self.shas[label][0] for label in "abd"))

    def test_unavailable_or_failing_store_stages_nothing(self):
        cache.ARTSTORE_STATE["module"] = None
        self.assertEqual(self.stage(), [])
        cache.ARTSTORE_STATE.clear()
        offline, broken = FakeStore(client=False), FakeStore(index_error=ValueError("bad"))
        for store in (offline, broken):
            with self.subTest(client=bool(store.client)):
                cache.ARTSTORE_STATE.clear()
                self.install(store)
                with contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(self.stage(), [])
        self.assertEqual((offline.index_loads, len(broken.index_loads)), ([], 1))

    def test_a_store_that_fails_midway_stages_nothing(self):
        cache.ARTSTORE_STATE["module"] = FakeStore().module(refresh_plan=mock.Mock(side_effect=ValueError("bad")))
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.stage(), [])

    def test_a_truncated_object_is_not_staged(self):
        self.install(FakeStore())
        (self.root / "objects" / self.shas["a"][0]).write_bytes(b"short")
        self.assertNotIn(self.shas["a"][0], self.staged())

    def test_flag_writes_the_stage_manifest(self):
        site = sources.Site({sources.URL: self.payload})
        self.install(FakeStore())
        target = self.base / "out" / "stage.json"
        with mock.patch.object(cache, "request", site):
            code, report = self.run_main("refresh", "--emit-stage-manifest", str(target))
        self.assertEqual(code, 0, report)
        staged = json.loads(target.read_text())
        self.assertIn(self.sha, [each["sha256"] for each in staged])
        self.assertTrue(all(set(each) == {"path", "sha256"} for each in staged))

    def test_flag_is_rejected_for_other_commands(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cache.main(["discover", "--emit-stage-manifest", str(self.base / "x.json")])


if __name__ == "__main__":
    unittest.main()
