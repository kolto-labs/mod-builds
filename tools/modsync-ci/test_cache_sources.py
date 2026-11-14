import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock
import zipfile

spec = importlib.util.spec_from_file_location("cache", Path(__file__).with_name("cache.py"))
cache = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cache)

URL = "https://files.example.com/pack_a.zip"
BIG_URL = "https://files.example.com/pack_b.zip"
DAY = 86400


def zip_bytes(label, repeat=600):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("data.bin", (label * repeat).encode())
    return stream.getvalue()


class Reply:
    def __init__(self, url, payload=b"", code=200, etag='"v1"'):
        self.url, self.payload, self.code, self.offset = url, payload, code, 0
        self.headers = {"Content-Type": "application/zip", "Content-Length": str(len(payload))}
        if etag:
            self.headers["ETag"] = etag

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


class Site:
    """Fake website: serves payloads, answers If-None-Match with 304, records every request."""

    def __init__(self, payloads, etag='"v1"'):
        self.payloads, self.etag, self.requests = dict(payloads), etag, []

    def __call__(self, url, args, headers=None):
        self.requests.append((url, dict(headers or {})))
        if url not in self.payloads:
            raise ValueError("HTTP 404")
        if (headers or {}).get("If-None-Match") == self.etag:
            return Reply(url, code=304, etag=None)
        return Reply(url, self.payloads[url], etag=self.etag)


class Fixture(unittest.TestCase):
    URLS = (URL,)

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.base = Path(self.directory.name)
        self.root = self.base / "cache"
        self.destination = self.base / "mods"
        self.guide = self.base / "guide.md"
        self.guide.write_text("".join("[mod](%s)\n" % url for url in self.URLS), encoding="utf-8")
        self.payload = zip_bytes("a")
        self.sha = hashlib.sha256(self.payload).hexdigest()
        patcher = mock.patch.object(cache, "SELECTIONS_PATH", self.base / "none.json")
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(cache, "PINS_PATH", self.base / "no-pins.json")
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_main(self, command, *extra):
        output = io.StringIO()
        arguments = [command, "--guide", str(self.guide), "--cache-dir", str(self.root), "--retries", "0", *extra]
        if command == "restore":
            arguments += ["--destination", str(self.destination)]
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
            code = cache.main(arguments)
        return code, json.loads(output.getvalue())

    def populate(self, payloads=None, etag='"v1"'):
        site = Site(payloads or {URL: self.payload}, etag)
        with mock.patch.object(cache, "request", site):
            code, report = self.run_main("refresh")
        self.assertEqual(code, 0, report)
        return site

    def manifest(self):
        return json.loads((self.root / "manifest.json").read_text())

    def age(self, days, **changes):
        manifest = self.manifest()
        for entry in manifest["entries"].values():
            entry.update(checked_at=cache.time.time() - days * DAY, **changes)
        (self.root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    def restored(self):
        return (self.destination / "pack_a.zip").read_bytes()


class RevalidationTests(Fixture):
    def test_unchanged_file_costs_one_conditional_request_and_no_download(self):
        self.populate()
        site = Site({URL: self.payload})
        with mock.patch.object(cache, "request", site):
            code, report = self.run_main("restore")
        self.assertEqual((code, report["entries"][0]["status"], report["entries"][0]["source"]),
                         (0, "restored", "revalidated"))
        self.assertEqual([headers.get("If-None-Match") for _, headers in site.requests], ['"v1"'])
        self.assertEqual(self.restored(), self.payload)

    def test_changed_file_is_downloaded_again(self):
        self.populate()
        new = zip_bytes("b")
        with mock.patch.object(cache, "request", Site({URL: new}, etag='"v2"')):
            code, report = self.run_main("restore")
        self.assertEqual((code, report["entries"][0]["source"]), (0, "website"))
        self.assertEqual(self.restored(), new)
        self.assertEqual(self.manifest()["entries"][report["entries"][0]["id"]]["sha256"],
                         hashlib.sha256(new).hexdigest())

    def test_no_validators_inside_max_age_is_reused_without_any_request(self):
        self.populate()
        self.age(1, etag=None, last_modified=None, status="verified",
                 evidence="download-without-upstream-metadata")
        with mock.patch.object(cache, "request", side_effect=AssertionError("must not contact the website")):
            code, report = self.run_main("restore")
        self.assertEqual((code, report["entries"][0]["source"]), (0, "local"))
        self.assertEqual(self.restored(), self.payload)

    def test_no_validators_older_than_max_age_is_fetched_again(self):
        self.populate()
        self.age(30, etag=None, last_modified=None, status="verified",
                 evidence="download-without-upstream-metadata")
        site = Site({URL: self.payload})
        with mock.patch.object(cache, "request", site):
            code, report = self.run_main("restore")
        self.assertEqual((code, report["entries"][0]["source"]), (0, "website"))
        self.assertEqual(len(site.requests), 1)

    def test_unreachable_website_falls_back_to_the_last_cached_copy(self):
        self.populate()
        self.age(30)
        with mock.patch.object(cache, "request", side_effect=ValueError("network request failed")):
            code, report = self.run_main("restore")
        self.assertEqual((code, report["entries"][0]["source"]), (0, "stale-local"))
        self.assertEqual(self.restored(), self.payload)

    def test_refresh_falls_back_to_the_last_cached_copy_without_marking_it_checked(self):
        self.populate()
        self.age(30)
        before = self.manifest()
        with mock.patch.object(cache, "request", side_effect=ValueError("network request failed")):
            code, report = self.run_main("refresh")
        self.assertEqual((code, report["entries"][0]["source"]), (0, "stale-local"))
        self.assertEqual(self.manifest(), before)

    def test_refresh_with_force_does_not_hide_an_unreachable_website(self):
        self.populate()
        self.age(30)
        with mock.patch.object(cache, "request", side_effect=ValueError("network request failed")):
            code, report = self.run_main("refresh", "--force")
        self.assertEqual((code, report["entries"][0]["status"]), (1, "unresolved"))

    def test_refresh_without_a_valid_local_copy_stays_unresolved(self):
        self.populate()
        (self.root / "objects" / self.sha).unlink()
        with mock.patch.object(cache, "request", side_effect=ValueError("network request failed")):
            code, report = self.run_main("refresh")
        self.assertEqual((code, report["entries"][0]["status"]), (1, "unresolved"))


class MissingFileTests(Fixture):
    def test_a_link_the_cache_never_held_is_fetched_from_the_website(self):
        site = Site({URL: self.payload})
        with mock.patch.object(cache, "request", site):
            code, report = self.run_main("restore")
        self.assertEqual((code, report["entries"][0]["source"]), (0, "website"))
        self.assertEqual(self.restored(), self.payload)
        self.assertEqual(len(self.manifest()["entries"]), 1)

    def test_missing_everywhere_is_unresolved(self):
        with mock.patch.object(cache, "request", side_effect=ValueError("HTTP 404")):
            code, report = self.run_main("restore")
        self.assertEqual((code, report["entries"][0]["status"]), (1, "unresolved"))
        self.assertFalse(self.destination.exists() and any(self.destination.iterdir()))

    def lose_object(self):
        self.populate()
        (self.root / "objects" / self.sha).unlink()

    def test_p2p_comes_before_the_website(self):
        self.lose_object()

        def swarm(item, dest):
            self.assertEqual((item["sha256"], dest.name), (self.sha, self.sha))
            dest.write_bytes(self.payload)
            return True

        with mock.patch.object(cache, "try_p2p", swarm), \
                mock.patch.object(cache, "request", side_effect=AssertionError("must not contact the website")):
            code, report = self.run_main("restore")
        self.assertEqual((code, report["entries"][0]["source"]), (0, "recovered"))
        self.assertEqual(self.restored(), self.payload)

    def test_a_swarm_copy_with_the_wrong_hash_is_discarded_for_the_website(self):
        self.lose_object()

        def poisoned(item, dest):
            dest.write_bytes(zip_bytes("evil"))
            return True

        site = Site({URL: self.payload})
        with mock.patch.object(cache, "try_p2p", poisoned), mock.patch.object(cache, "request", site):
            code, report = self.run_main("restore")
        self.assertEqual((code, report["entries"][0]["source"]), (0, "website"))
        self.assertEqual(self.restored(), self.payload)

    def test_a_swarm_that_cannot_help_falls_through_to_the_website(self):
        self.lose_object()
        site = Site({URL: self.payload})
        with mock.patch.object(cache, "try_p2p", return_value=False), mock.patch.object(cache, "request", site):
            code, report = self.run_main("restore")
        self.assertEqual((code, report["entries"][0]["source"]), (0, "website"))
        self.assertEqual(len(site.requests), 1)

    def test_a_corrupt_local_object_is_replaced_not_restored(self):
        self.populate()
        (self.root / "objects" / self.sha).write_bytes(b"corrupt")
        with mock.patch.object(cache, "request", Site({URL: self.payload}, etag='"other"')):
            code, report = self.run_main("restore")
        self.assertEqual((code, report["entries"][0]["source"]), (0, "website"))
        self.assertEqual(self.restored(), self.payload)


class PinnedColdCacheTests(Fixture):
    NEXUS = "https://www.nexusmods.com/kotor/mods/1"

    def pin(self):
        item = {"id": cache.key_for(self.NEXUS)}
        self.guide.write_text("[mod](%s)\n" % self.NEXUS, encoding="utf-8")
        pins = self.base / "pins.json"
        pins.write_text(json.dumps({"schema": 1, "entries": {item["id"]: {
            "url": self.NEXUS, "provider": "nexus", "provenance": "operator-local-download",
            "objects": [{"archive_name": "pack_a.zip", "sha256": self.sha, "size": len(self.payload),
                         "extension": "zip"}]}}}), encoding="utf-8")
        patcher = mock.patch.object(cache, "PINS_PATH", pins)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_cold_cache_gets_a_pinned_archive_from_the_swarm(self):
        self.pin()

        def swarm(item, dest):
            dest.write_bytes(self.payload)
            return True

        with mock.patch.object(cache, "try_p2p", swarm), \
                mock.patch.object(cache, "request", side_effect=AssertionError("must not contact the website")):
            code, report = self.run_main("restore")
        self.assertEqual((code, report["entries"][0]["source"], report["entries"][0]["pinned"]), (0, "recovered", True))
        self.assertEqual(self.restored(), self.payload)

    def test_cold_cache_with_no_source_for_a_pinned_archive_is_unresolved(self):
        self.pin()
        code, report = self.run_main("restore")
        self.assertEqual((code, report["entries"][0]["status"]), (1, "unresolved"))


class P2PHookTests(Fixture):
    def test_hook_is_false_without_a_magnet(self):
        self.assertFalse(cache.try_p2p({"id": "x", "sha256": self.sha}, self.base / self.sha))

    def test_hook_calls_the_helper_with_the_hash_and_the_magnet_block(self):
        helper = self.base / "helper"
        helper.mkdir()
        (helper / "p2p_fetch.py").write_text(
            "from pathlib import Path\n"
            "def fetch(entry, dest):\n"
            "    Path(dest).write_text(entry['sha256'] + '|' + entry['p2p']['magnet'])\n"
            "    return True\n", encoding="utf-8")
        with mock.patch.object(cache, "__file__", str(helper / "cache.py")):
            self.assertTrue(cache.try_p2p({"sha256": self.sha, "p2p": {"magnet": "magnet:?xt=urn:btih:1"}},
                                          self.base / self.sha))
        self.assertEqual((self.base / self.sha).read_text(), self.sha + "|magnet:?xt=urn:btih:1")

    def test_a_broken_helper_never_raises(self):
        helper = self.base / "helper"
        helper.mkdir()
        (helper / "p2p_fetch.py").write_text("raise RuntimeError('boom')\n", encoding="utf-8")
        with mock.patch.object(cache, "__file__", str(helper / "cache.py")), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertFalse(cache.try_p2p({"sha256": self.sha, "p2p": {"magnet": "magnet:?x"}}, self.base / "d"))


REAL_RUN = subprocess.run


class FakeRclone:
    """Stands in for `rclone copyto MODCACHE:path local`; MODCACHE: maps to a directory."""

    def __init__(self, bucket, fail=False):
        self.bucket, self.fail, self.calls = bucket, fail, []

    def __call__(self, command, **kwargs):
        if command[0] != "rclone":
            return REAL_RUN(command, **kwargs)
        self.calls.append(command)
        if self.fail:
            raise subprocess.CalledProcessError(1, command)
        source = self.bucket / command[2].split(":", 1)[1]
        if not source.is_file():
            raise subprocess.CalledProcessError(3, command)
        Path(command[3]).write_bytes(source.read_bytes())
        return subprocess.CompletedProcess(command, 0)


class RemoteTests(Fixture):
    REMOTE = ("--remote", "MODCACHE:bucket/modsync-archives")

    def stage_remote(self):
        self.populate()
        self.bucket = self.base / "bucket"
        (self.bucket / "modsync-archives" / "objects").mkdir(parents=True)
        (self.bucket / "modsync-archives" / "manifest.json").write_bytes((self.root / "manifest.json").read_bytes())
        (self.bucket / "modsync-archives" / "objects" / self.sha).write_bytes(self.payload)
        for path in (self.root / "objects").iterdir():
            path.unlink()
        (self.root / "manifest.json").unlink()

    def test_remote_failure_is_a_warning_and_the_website_serves_the_file(self):
        fake = FakeRclone(self.base, fail=True)
        with mock.patch.object(cache.subprocess, "run", fake), mock.patch.object(cache, "request", Site({URL: self.payload})):
            code, report = self.run_main("restore", *self.REMOTE)
        self.assertEqual((code, report["entries"][0]["source"]), (0, "website"))
        self.assertEqual(len(fake.calls), 1)

    def test_remote_supplies_manifest_and_object_without_the_website(self):
        self.stage_remote()
        fake = FakeRclone(self.base)
        with mock.patch.object(cache.subprocess, "run", fake), \
                mock.patch.object(cache, "request", side_effect=AssertionError("must not contact the website")):
            code, report = self.run_main("restore", *self.REMOTE)
        self.assertEqual((code, report["entries"][0]["source"]), (0, "recovered"))
        self.assertEqual(self.restored(), self.payload)
        self.assertEqual([call[2].rsplit("/", 1)[1] for call in fake.calls], ["manifest.json", self.sha])

    def test_local_files_are_not_pulled_from_the_remote(self):
        self.stage_remote()
        self.populate()
        fake = FakeRclone(self.base)
        with mock.patch.object(cache.subprocess, "run", fake), mock.patch.object(cache, "request", Site({URL: self.payload})):
            code, report = self.run_main("restore", *self.REMOTE)
        self.assertEqual((code, report["entries"][0]["source"]), (0, "revalidated"))
        self.assertEqual([call[2].rsplit("/", 1)[1] for call in fake.calls], ["manifest.json"])

    def test_nothing_is_ever_written_to_the_remote(self):
        self.stage_remote()
        fake = FakeRclone(self.base)
        with mock.patch.object(cache.subprocess, "run", fake), mock.patch.object(cache, "request", Site({URL: self.payload})):
            self.run_main("restore", *self.REMOTE)
            self.run_main("refresh", *self.REMOTE)
        self.assertTrue(all(call[:2] == ["rclone", "copyto"] and call[2].startswith("MODCACHE:") for call in fake.calls))


class CacheLimitTests(Fixture):
    URLS = (URL, BIG_URL)

    def setUp(self):
        super().setUp()
        self.big = zip_bytes("big", 20000)
        self.payloads = {URL: self.payload, BIG_URL: self.big}

    def objects(self):
        return sorted(path.name for path in (self.root / "objects").iterdir())

    def test_oversize_objects_are_dropped_and_not_fetched_again(self):
        limit = str(len(self.payload) + 1)
        site = Site(self.payloads)
        with mock.patch.object(cache, "request", site):
            code, report = self.run_main("refresh", "--cache-limit-bytes", limit)
        self.assertEqual(code, 0)
        self.assertEqual(self.objects(), [self.sha])
        self.assertEqual(len(self.manifest()["entries"]), 2)
        site.requests.clear()
        with mock.patch.object(cache, "request", site):
            code, report = self.run_main("refresh", "--cache-limit-bytes", limit)
        self.assertEqual(code, 0)
        self.assertEqual(sorted(entry["status"] for entry in report["entries"]), ["fresh", cache.OVER_LIMIT])
        self.assertEqual([url for url, _ in site.requests], [URL])
        self.assertEqual(self.objects(), [self.sha])

    def test_bik_and_unreferenced_objects_are_never_kept(self):
        manifest = {"schema": 1, "entries": {
            "a": {"sha256": "1" * 64, "size": 10, "archive_name": "Movies.BIK"},
            "b": {"sha256": "2" * 64, "size": 10, "archive_name": "pack.zip"},
            "c": {"objects": [{"sha256": "3" * 64, "size": 10, "listed_name": "intro.bik"}]}}}
        self.assertEqual(cache.kept_objects(manifest, 10 ** 9), {"2" * 64})

    def test_pinned_objects_are_kept_before_smaller_ones(self):
        manifest = {"schema": 1, "entries": {
            "small": {"sha256": "1" * 64, "size": 10, "status": "verified"},
            "pin": {"objects": [{"sha256": "2" * 64, "size": 90}], "status": "pinned"}}}
        self.assertEqual(cache.kept_objects(manifest, 95), {"2" * 64})

    def test_restore_ignores_the_limit_and_never_deletes(self):
        with mock.patch.object(cache, "request", Site(self.payloads)):
            self.run_main("refresh")
            before = self.objects()
            code, _ = self.run_main("restore", "--cache-limit-bytes", "1")
        self.assertEqual((code, self.objects()), (0, before))


class RemoteManifestMergeTests(Fixture):
    def test_newer_remote_entry_wins_and_older_is_ignored(self):
        manifest = {"schema": 1, "entries": {"keep": {"checked_at": 50, "x": 1}, "swap": {"checked_at": 10}}}
        (self.base / "bucket").mkdir()
        (self.base / "bucket" / "manifest.json").write_text(json.dumps({"schema": 1, "entries": {
            "keep": {"checked_at": 5, "x": 2}, "swap": {"checked_at": 20, "x": 3}, "new": {"checked_at": 1}}}))
        self.root.mkdir()
        args = type("Args", (), {"remote": "MODCACHE:bucket", "backend_timeout": 5})()
        with mock.patch.object(cache.subprocess, "run", FakeRclone(self.base)):
            cache.pull_manifest(args, self.root, manifest)
        self.assertEqual(manifest["entries"], {"keep": {"checked_at": 50, "x": 1},
                                               "swap": {"checked_at": 20, "x": 3}, "new": {"checked_at": 1}})
        self.assertFalse((self.root / ".remote-manifest.json").exists())


if __name__ == "__main__":
    unittest.main()
