import contextlib
import hashlib
import io
import json
import os
import re
import shutil
import tempfile
import threading
import time
import unittest
import urllib.parse
import zipfile
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import artstore
import artstore_client
import artstore_core as core

BASE = "/repos/o/r/actions/artifacts"
TOKEN = "FAKE-TOKEN-0123456789abcdef"
KEY = "k" * 40
OTHER_KEY = "z" * 40
DAY = 86400


def iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def send(handler, status, body=b"", headers=None):
    if isinstance(body, (dict, list)):
        body = json.dumps(body).encode()
    handler.send_response(status)
    for key, value in (headers or {}).items():
        handler.send_header(key, value)
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class FakeServer:
    def __init__(self, serve):
        self.requests = []
        self._lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def dispatch(self):
                parsed = urllib.parse.urlsplit(self.path)
                record = {
                    "method": self.command,
                    "path": parsed.path,
                    "query": urllib.parse.parse_qs(parsed.query),
                    "headers": {key.lower(): value for key, value in self.headers.items()},
                }
                with outer._lock:
                    outer.requests.append(record)
                serve(self, record)

            do_GET = dispatch
            do_DELETE = dispatch

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.url = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        self.thread = threading.Thread(
            target=self.httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )
        self.thread.start()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(5)


class FakeBlobStore(FakeServer):
    def __init__(self):
        self.blobs = {}
        self.bytes_served = 0
        super().__init__(self.serve)

    def serve(self, handler, record):
        if "authorization" in record["headers"]:
            return send(handler, 401, b"unexpected credentials")
        match = re.fullmatch(r"/blob/(\d+)", record["path"])
        if not match or int(match.group(1)) not in self.blobs:
            return send(handler, 404, b"missing")
        data = self.blobs[int(match.group(1))]
        requested = record["headers"].get("range")
        if not requested:
            self.bytes_served += len(data)
            return send(handler, 200, data)
        parsed = re.fullmatch(r"bytes=(\d+)-(\d*)", requested)
        start = int(parsed.group(1))
        if start >= len(data):
            return send(handler, 416, b"", {"Content-Range": "bytes */%d" % len(data)})
        end = min(int(parsed.group(2)) if parsed.group(2) else len(data) - 1, len(data) - 1)
        body = data[start:end + 1]
        self.bytes_served += len(body)
        send(handler, 206, body, {"Content-Range": "bytes %d-%d/%d" % (start, end, len(data))})


class FakeGitHub(FakeServer):
    def __init__(self, blob, token):
        self.blob = blob
        self.token = token
        self.artifacts = []
        self.next_id = 100
        super().__init__(self.serve)

    def add(self, name, payload, expired=False, created_at=None, expires_at=None):
        self.next_id += 1
        artifact_id = self.next_id
        now = time.time()
        self.artifacts.append({
            "id": artifact_id,
            "name": name,
            "size_in_bytes": len(payload),
            "expired": expired,
            "created_at": created_at or iso(now + artifact_id),
            "expires_at": iso(expires_at if expires_at is not None else now + 90 * DAY),
            "digest": "sha256:" + hashlib.sha256(payload).hexdigest(),
        })
        self.blob.blobs[artifact_id] = payload
        return artifact_id

    def find(self, artifact_id):
        return next((item for item in self.artifacts if item["id"] == artifact_id), None)

    def serve(self, handler, record):
        if record["headers"].get("authorization") != "Bearer " + self.token:
            return send(handler, 401, {"message": "Bad credentials"})
        path = record["path"]
        if record["method"] == "GET" and path == BASE:
            query = record["query"]
            per_page = int(query["per_page"][0])
            page = int(query["page"][0])
            window = self.artifacts[(page - 1) * per_page:page * per_page]
            return send(handler, 200, {"total_count": len(self.artifacts), "artifacts": window})
        match = re.fullmatch(BASE + r"/(\d+)/zip", path)
        if match and record["method"] == "GET":
            artifact = self.find(int(match.group(1)))
            if artifact is None:
                return send(handler, 404, {"message": "Not Found"})
            if artifact["expired"]:
                return send(handler, 410, {"message": "Artifact has expired"})
            location = "%s/blob/%d?sig=s" % (self.blob.url, artifact["id"])
            return send(handler, 302, b"", {"Location": location})
        match = re.fullmatch(BASE + r"/(\d+)", path)
        if match and record["method"] == "DELETE":
            artifact = self.find(int(match.group(1)))
            if artifact is None:
                return send(handler, 404, {"message": "Not Found"})
            self.artifacts.remove(artifact)
            return send(handler, 204)
        send(handler, 404, {"message": "Not Found"})


def zip_directory(directory):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
        for path in sorted(Path(directory).iterdir()):
            if path.is_file() and not path.name.startswith("."):
                archive.writestr(zipfile.ZipInfo(path.name, (2026, 1, 1, 0, 0, 0)),
                                 path.read_bytes(), zipfile.ZIP_STORED)
    return buffer.getvalue()


def sha256_of(data):
    return hashlib.sha256(data).hexdigest()


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        self.blob = FakeBlobStore()
        self.api = FakeGitHub(self.blob, TOKEN)
        self.addCleanup(self.blob.stop)
        self.addCleanup(self.api.stop)
        patcher = mock.patch.object(artstore_client.time, "sleep", lambda seconds: None)
        patcher.start()
        self.addCleanup(patcher.stop)
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith(("GITHUB_", "GH_", "ARTSTORE_"))
                       and key != "CACHE_ENCRYPTION_KEY"}
        environment.update({"CACHE_ENCRYPTION_KEY": KEY, "GITHUB_RUN_ID": "1001",
                            "GITHUB_RUN_ATTEMPT": "1"})
        patcher = mock.patch.dict(os.environ, environment, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.client = self.make_client()
        self.files = {}

    def make_client(self, token=TOKEN):
        return artstore_client.ArtifactsClient(
            "o/r", token, api=self.api.url,
            opener=artstore_client.make_opener(trust_env=False))

    def make_file(self, name, size, seed=0):
        data = hashlib.sha256(b"seed%d" % seed).digest() * (size // 32 + 1)
        data = data[:size]
        path = self.tmp / "src" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        self.files[name] = data
        return path, sha256_of(data)

    def upload(self, result, packdir, expires_at=None, run_ids=None):
        packs = {}
        for pack in result["packs"]:
            packs[pack["name"]] = self.api.add(pack["name"], zip_directory(pack["dir"]),
                                               expires_at=expires_at)
        index_id = None
        if result["index_name"]:
            index_id = self.api.add(result["index_name"],
                                    zip_directory(Path(packdir) / "index"), expires_at=expires_at)
        return packs, index_id

    def stage_and_upload(self, pairs, run_id, expires_at=None, index=None, **options):
        packdir = self.tmp / ("pack-" + run_id)
        with mock.patch.dict(os.environ, {"GITHUB_RUN_ID": run_id}):
            result = artstore.stage(pairs, packdir, index=index, **options)
        packs, index_id = self.upload(result, packdir, expires_at)
        return result, packs, index_id

    def fetch_dest(self, name="restored.bin"):
        return self.tmp / "clean" / name


class StageTests(StoreTestCase):
    def test_stage_names_layout_and_records(self):
        first, first_sha = self.make_file("a.bin", 3000, 1)
        second, second_sha = self.make_file("b.bin", 500, 2)
        packdir = self.tmp / "pd"
        result = artstore.stage([(first, first_sha), (second, second_sha)], packdir,
                                part_size=1000)
        self.assertEqual(result["pack_name"], "artstore-pack-1001-1-0")
        self.assertEqual(result["index_name"], "artstore-index-1001-1-0")
        self.assertEqual({blob["sha256"] for blob in result["blobs"]}, {first_sha, second_sha})
        by_sha = {blob["sha256"]: blob for blob in result["blobs"]}
        self.assertEqual(len(by_sha[first_sha]["parts"]), 3)
        members = sorted(path.name for path in (packdir / result["pack_name"]).iterdir())
        expected = sorted(part["stored_sha256"] + ".part" for blob in result["blobs"]
                          for part in blob["parts"])
        self.assertEqual(members, expected)
        fragment = core.load_index(packdir / "index" / "index.json")
        self.assertEqual(set(fragment["blobs"]), {first_sha, second_sha})
        self.assertEqual(fragment["packs"][result["pack_name"]]["artifact_id"], None)

    def test_stage_skips_bik_missing_and_wrong_sha(self):
        good, good_sha = self.make_file("good.bin", 400, 3)
        bik, bik_sha = self.make_file("movie.bik", 400, 4)
        liar, _ = self.make_file("liar.bin", 400, 5)
        result = artstore.stage([(good, good_sha), (bik, bik_sha), (liar, "0" * 64),
                                 (self.tmp / "nope.bin", "1" * 64), (good, good_sha)],
                                self.tmp / "pd")
        self.assertEqual([blob["sha256"] for blob in result["blobs"]], [good_sha])
        reasons = {entry["reason"] for entry in result["skipped"]}
        self.assertEqual(reasons, {"bik", "sha256-mismatch", "missing", "duplicate"})
        self.assertEqual(sorted(path.name for path in (self.tmp / "pd").iterdir()),
                         sorted([result["pack_name"], "index"]))
        leftovers = [path for path in (self.tmp / "pd" / result["pack_name"]).iterdir()]
        self.assertEqual(len(leftovers), 1)

    def test_stage_nothing_new_creates_no_pack(self):
        path, sha = self.make_file("a.bin", 400, 1)
        result, _, _ = self.stage_and_upload([(path, sha)], "2001")
        index = artstore.load_index(self.client)
        again = artstore.stage([(path, sha)], self.tmp / "pd2", index=index)
        self.assertEqual(again["blobs"], [])
        self.assertIsNone(again["pack_name"])
        self.assertIsNone(again["index_path"])
        self.assertEqual([entry["reason"] for entry in again["skipped"]], ["present"])
        self.assertEqual(list((self.tmp / "pd2").iterdir()), [])

    def test_stage_restages_blob_with_under_30_days_left(self):
        path, sha = self.make_file("a.bin", 400, 1)
        self.stage_and_upload([(path, sha)], "2001", expires_at=time.time() + 10 * DAY)
        index = artstore.load_index(self.client)
        again, _, _ = self.stage_and_upload([(path, sha)], "2002", index=index)
        self.assertEqual([blob["sha256"] for blob in again["blobs"]], [sha])
        self.assertEqual(again["pack_name"], "artstore-pack-2002-1-0")

    def test_pack_budget_splits_packs_and_names_are_unique(self):
        pairs = [self.make_file("f%d.bin" % number, 700, number)[:2] for number in range(5)]
        packdir = self.tmp / "pd"
        result = artstore.stage(pairs, packdir, part_size=700, pack_budget=1800)
        names = [pack["name"] for pack in result["packs"]]
        self.assertGreater(len(names), 1)
        self.assertEqual(names, ["artstore-pack-1001-1-%d" % n for n in range(len(names))])
        self.assertEqual(sum(pack["blobs"] for pack in result["packs"]), 5)
        for name in names:
            self.assertTrue((packdir / name).is_dir())
        again = artstore.stage([self.make_file("extra.bin", 300, 99)[:2]], packdir)
        self.assertNotIn(again["pack_name"], names)
        self.assertNotEqual(again["index_name"], result["index_name"])
        fragment = core.load_index(packdir / "index" / "index.json")
        self.assertEqual(len(fragment["blobs"]), 6)

    def test_stage_requires_key(self):
        path, sha = self.make_file("a.bin", 100, 1)
        with mock.patch.dict(os.environ, {"CACHE_ENCRYPTION_KEY": "short"}):
            with self.assertRaises(ValueError) as caught:
                artstore.stage([(path, sha)], self.tmp / "pd")
        self.assertNotIn("short", str(caught.exception).replace("CACHE_ENCRYPTION_KEY", ""))


class FetchTests(StoreTestCase):
    def test_roundtrip_through_fake_github_in_clean_dir(self):
        big, big_sha = self.make_file("big.bin", 5000, 1)
        small, small_sha = self.make_file("small.bin", 700, 2)
        result, _, _ = self.stage_and_upload([(big, big_sha), (small, small_sha)], "3001",
                                             part_size=1000)
        shutil.rmtree(self.tmp / "src")
        for name, sha in (("big.bin", big_sha), ("small.bin", small_sha)):
            destination = self.fetch_dest(name)
            self.assertTrue(artstore.fetch(sha, destination, client=self.client))
            self.assertEqual(destination.read_bytes(), self.files[name])
        self.assertEqual([path.name for path in destination.parent.iterdir()
                          if path.name.startswith(".")], [])

    def test_fetch_is_ranged_and_only_reads_the_wanted_parts(self):
        wanted, wanted_sha = self.make_file("wanted.bin", 2000, 1)
        bulk, bulk_sha = self.make_file("bulk.bin", 1500000, 2)
        _, packs, _ = self.stage_and_upload([(wanted, wanted_sha), (bulk, bulk_sha)], "3002",
                                            part_size=300000)
        pack_size = len(self.blob.blobs[next(iter(packs.values()))])
        self.blob.bytes_served = 0
        index = artstore.load_index(self.client)
        index_bytes = self.blob.bytes_served
        self.blob.bytes_served = 0
        destination = self.fetch_dest()
        self.assertTrue(artstore.fetch(wanted_sha, destination, client=self.client, index=index))
        self.assertEqual(destination.read_bytes(), self.files["wanted.bin"])
        self.assertLess(self.blob.bytes_served, pack_size // 5)
        self.assertGreater(index_bytes, 0)

    def test_fetch_loads_index_itself_and_uses_workdir(self):
        path, sha = self.make_file("a.bin", 3500, 1)
        self.stage_and_upload([(path, sha)], "3003", part_size=1000)
        workdir = self.tmp / "scratch"
        destination = self.fetch_dest()
        self.assertTrue(artstore.fetch(sha, destination, client=self.client, workdir=workdir,
                                       workers=2))
        self.assertEqual(destination.read_bytes(), self.files["a.bin"])
        self.assertEqual(list(workdir.iterdir()), [])

    def test_missing_blob_returns_false(self):
        path, sha = self.make_file("a.bin", 400, 1)
        self.stage_and_upload([(path, sha)], "3004")
        destination = self.fetch_dest()
        self.assertFalse(artstore.fetch("a" * 64, destination, client=self.client))
        self.assertFalse(artstore.fetch("not-a-digest", destination, client=self.client))
        self.assertFalse(destination.exists())

    def test_empty_store_and_no_token_return_false(self):
        destination = self.fetch_dest()
        self.assertFalse(artstore.fetch("a" * 64, destination, client=self.client))
        self.assertEqual(artstore.load_index(self.client), core.new_index())
        self.assertIsNone(artstore_client.from_environment({}))
        self.assertFalse(artstore.fetch("a" * 64, destination))
        self.assertEqual(artstore.load_index(), core.new_index())
        self.assertFalse(destination.exists())

    def test_wrong_key_returns_false_without_raising_or_leaking(self):
        path, sha = self.make_file("a.bin", 2500, 1)
        self.stage_and_upload([(path, sha)], "3005", part_size=1000)
        destination = self.fetch_dest()
        stream = io.StringIO()
        with mock.patch.dict(os.environ, {"CACHE_ENCRYPTION_KEY": OTHER_KEY}), \
                contextlib.redirect_stderr(stream):
            self.assertFalse(artstore.fetch(sha, destination, client=self.client))
        self.assertFalse(destination.exists())
        self.assertIn("::warning::", stream.getvalue())
        for secret in (KEY, OTHER_KEY, TOKEN):
            self.assertNotIn(secret, stream.getvalue())
        with mock.patch.dict(os.environ, {"CACHE_ENCRYPTION_KEY": "short"}):
            self.assertFalse(artstore.fetch(sha, destination, client=self.client))

    def test_corrupted_pack_returns_false(self):
        path, sha = self.make_file("a.bin", 400, 1)
        result, packs, _ = self.stage_and_upload([(path, sha)], "3006")
        artifact_id = next(iter(packs.values()))
        data = bytearray(self.blob.blobs[artifact_id])
        position = len(data) // 2
        data[position] ^= 0xFF
        self.blob.blobs[artifact_id] = bytes(data)
        self.assertFalse(artstore.fetch(sha, self.fetch_dest(), client=self.client))

    def test_expired_pack_returns_false(self):
        path, sha = self.make_file("a.bin", 400, 1)
        _, packs, _ = self.stage_and_upload([(path, sha)], "3007")
        self.assertTrue(artstore.fetch(sha, self.fetch_dest("ok.bin"), client=self.client))
        self.api.find(next(iter(packs.values())))["expired"] = True
        index = artstore.load_index(self.client)
        destination = self.fetch_dest("gone.bin")
        stream = io.StringIO()
        with contextlib.redirect_stderr(stream):
            self.assertFalse(artstore.fetch(sha, destination, client=self.client, index=index))
        self.assertIn("::warning::", stream.getvalue())
        self.assertFalse(destination.exists())

    def test_pack_past_expiry_time_returns_false(self):
        path, sha = self.make_file("a.bin", 400, 1)
        self.stage_and_upload([(path, sha)], "3008", expires_at=time.time() - 3600)
        self.assertFalse(artstore.fetch(sha, self.fetch_dest(), client=self.client))

    def test_pack_deleted_after_indexing_returns_false(self):
        path, sha = self.make_file("a.bin", 400, 1)
        _, packs, _ = self.stage_and_upload([(path, sha)], "3009")
        index = artstore.load_index(self.client)
        self.api.artifacts.remove(self.api.find(next(iter(packs.values()))))
        self.assertFalse(artstore.fetch(sha, self.fetch_dest(), client=self.client, index=index))
        self.assertFalse(artstore.fetch(sha, self.fetch_dest(), client=self.client))

    def test_unreadable_index_returns_false_not_raises(self):
        self.api.add("artstore-index-1-1-0", b"not a zip")
        self.assertFalse(artstore.fetch("a" * 64, self.fetch_dest(), client=self.client))


class IndexTests(StoreTestCase):
    def test_concurrent_runs_staging_same_blob_merge_idempotently(self):
        path, sha = self.make_file("a.bin", 1500, 1)
        other, other_sha = self.make_file("b.bin", 600, 2)
        self.stage_and_upload([(path, sha)], "4001", part_size=700)
        self.stage_and_upload([(path, sha), (other, other_sha)], "4002", part_size=700)
        index = artstore.load_index(self.client)
        self.assertEqual(set(index["blobs"]), {sha, other_sha})
        self.assertEqual(len(index["blobs"][sha]["parts"]), 3)
        for name, digest in (("a.bin", sha), ("b.bin", other_sha)):
            destination = self.fetch_dest(name)
            self.assertTrue(artstore.fetch(digest, destination, client=self.client, index=index))
            self.assertEqual(destination.read_bytes(), self.files[name])
        self.assertEqual(artstore.load_index(self.client), index)

    def test_only_newest_few_indexes_are_read_and_bad_ones_skipped(self):
        paths = [self.make_file("f%d.bin" % number, 300, number) for number in range(5)]
        cumulative = None
        for number, (path, sha) in enumerate(paths):
            cumulative = artstore.load_index(self.client) if number else None
            self.stage_and_upload([(path, sha)], "50%02d" % number, index=cumulative)
        self.api.add("artstore-index-9999-1-0", b"garbage", created_at=iso(time.time() + 9999))
        del self.api.requests[:]
        index = artstore.load_index(self.client, keep=2)
        self.assertEqual(len(index["blobs"]), 5)
        touched = {r["path"] for r in self.api.requests if r["path"].endswith("/zip")}
        indexes = sorted((item for item in self.api.artifacts
                          if item["name"].startswith("artstore-index-")),
                         key=lambda item: (item["created_at"], item["id"]), reverse=True)
        newest_three = {"%s/%d/zip" % (BASE, item["id"]) for item in indexes[:3]}
        older = {"%s/%d/zip" % (BASE, item["id"]) for item in indexes[3:]}
        self.assertTrue(newest_three <= touched)
        self.assertFalse(older & touched)

    def test_expired_index_artifact_is_ignored(self):
        path, sha = self.make_file("a.bin", 300, 1)
        result, packs, index_id = self.stage_and_upload([(path, sha)], "6001")
        self.api.find(index_id)["expired"] = True
        self.assertEqual(artstore.load_index(self.client), core.new_index())

    def test_pack_ids_and_expiry_come_from_the_listing(self):
        path, sha = self.make_file("a.bin", 300, 1)
        expires = int(time.time()) + 50 * DAY
        result, packs, _ = self.stage_and_upload([(path, sha)], "6002", expires_at=expires)
        index = artstore.load_index(self.client)
        entry = index["packs"][result["pack_name"]]
        self.assertEqual(entry["artifact_id"], packs[result["pack_name"]])
        self.assertAlmostEqual(entry["expires_at"], expires, delta=2)


class RefreshTests(StoreTestCase):
    def test_refresh_plan_selects_blobs_expiring_within_window(self):
        soon, soon_sha = self.make_file("soon.bin", 300, 1)
        later, later_sha = self.make_file("later.bin", 300, 2)
        self.stage_and_upload([(soon, soon_sha)], "7001", expires_at=time.time() + 10 * DAY)
        index = artstore.load_index(self.client)
        self.stage_and_upload([(later, later_sha)], "7002", expires_at=time.time() + 80 * DAY,
                              index=index)
        index = artstore.load_index(self.client)
        now = time.time()
        self.assertEqual(artstore.refresh_plan(index, now), [soon_sha])
        self.assertEqual(artstore.refresh_plan(index, now, within_days=5), [])
        self.assertEqual(artstore.refresh_plan(index, now, within_days=100),
                         sorted([soon_sha, later_sha]))
        self.assertEqual(artstore.refresh_plan(index, datetime.now(timezone.utc)), [soon_sha])

    def test_refresh_flow_fetch_then_restage_under_new_pack_extends_retention(self):
        path, sha = self.make_file("a.bin", 1800, 1)
        self.stage_and_upload([(path, sha)], "7101", expires_at=time.time() + 10 * DAY,
                              part_size=1000)
        index = artstore.load_index(self.client)
        plan = artstore.refresh_plan(index, time.time())
        self.assertEqual(plan, [sha])
        pairs = []
        for digest in plan:
            restored = self.tmp / "refresh" / digest
            self.assertTrue(artstore.fetch(digest, restored, client=self.client, index=index))
            pairs.append((restored, digest))
        result, _, _ = self.stage_and_upload(pairs, "7102", index=index, part_size=1000)
        self.assertEqual(result["pack_name"], "artstore-pack-7102-1-0")
        merged = artstore.load_index(self.client)
        self.assertEqual(artstore.refresh_plan(merged, time.time()), [])
        self.assertEqual({part["pack"] for part in merged["blobs"][sha]["parts"]},
                         {result["pack_name"]})
        destination = self.fetch_dest()
        self.assertTrue(artstore.fetch(sha, destination, client=self.client, index=merged))
        self.assertEqual(destination.read_bytes(), self.files["a.bin"])


class PruneTests(StoreTestCase):
    def setUp(self):
        super().setUp()
        self.keep_path, self.keep_sha = self.make_file("keep.bin", 300, 1)
        self.old_path, self.old_sha = self.make_file("old.bin", 300, 2)
        old = time.time() - 3 * DAY
        self.keep_result, self.keep_packs, _ = self.stage_and_upload(
            [(self.keep_path, self.keep_sha)], "8001")
        self.orphan_id = self.api.add("artstore-pack-orphan-1-0", b"x" * 10,
                                      created_at=iso(old))
        self.expired_id = self.api.add("artstore-pack-dead-1-0", b"x" * 10, expired=True,
                                       created_at=iso(old))
        self.fresh_orphan = self.api.add("artstore-pack-fresh-1-0", b"x" * 10)
        self.other_id = self.api.add("unrelated-artifact", b"x" * 10, created_at=iso(old))
        self.index_ids = [self.api.add("artstore-index-old-%d" % number, b"x" * 10,
                                       created_at=iso(old + number)) for number in range(6)]

    def test_dry_run_is_default_and_deletes_nothing(self):
        before = [item["id"] for item in self.api.artifacts]
        index = artstore.load_index(self.client)
        result = artstore.prune(self.client, index)
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["deleted"], [])
        self.assertEqual([item["id"] for item in self.api.artifacts], before)
        self.assertFalse([r for r in self.api.requests if r["method"] == "DELETE"])
        planned = {item["name"]: item["reason"] for item in result["delete"]}
        self.assertEqual(planned["artstore-pack-orphan-1-0"], "superseded")
        self.assertEqual(planned["artstore-pack-dead-1-0"], "expired")
        self.assertNotIn("artstore-pack-fresh-1-0", planned)
        self.assertNotIn(self.keep_result["pack_name"], planned)
        self.assertNotIn("unrelated-artifact", planned)

    def test_index_artifacts_beyond_newest_five_are_planned(self):
        index = artstore.load_index(self.client)
        result = artstore.prune(self.client, index, grace_hours=0)
        indexes = [item for item in result["delete"] if item["kind"] == "index"]
        listed = sorted((item for item in self.api.artifacts
                         if item["name"].startswith("artstore-index-")),
                        key=lambda item: (item["created_at"], item["id"]), reverse=True)
        self.assertEqual({item["id"] for item in indexes}, {item["id"] for item in listed[5:]})
        self.assertEqual(len(listed), 7)

    def test_apply_deletes_planned_artifacts_only(self):
        index = artstore.load_index(self.client)
        result = artstore.prune(self.client, index, dry_run=False)
        remaining = {item["id"] for item in self.api.artifacts}
        self.assertIn(next(iter(self.keep_packs.values())), remaining)
        self.assertIn(self.fresh_orphan, remaining)
        self.assertIn(self.other_id, remaining)
        self.assertNotIn(self.orphan_id, remaining)
        self.assertNotIn(self.expired_id, remaining)
        self.assertEqual(set(result["deleted"]), {item["id"] for item in result["delete"]})
        self.assertEqual(result["failed"], [])
        self.assertTrue(artstore.fetch(self.keep_sha, self.fetch_dest(), client=self.client))

    def test_empty_index_never_deletes_live_packs(self):
        result = artstore.prune(self.client, core.new_index(), dry_run=False, grace_hours=0)
        names = {item["name"] for item in result["delete"] if item["kind"] == "pack"}
        self.assertEqual(names, {"artstore-pack-dead-1-0"})
        self.assertTrue(result["notes"])
        self.assertIn(next(iter(self.keep_packs.values())),
                      {item["id"] for item in self.api.artifacts})

    def test_failed_delete_is_reported_not_raised(self):
        index = artstore.load_index(self.client)
        with mock.patch.object(self.client, "delete_artifact",
                               side_effect=artstore_client.ArtifactError("boom", 403)):
            result = artstore.prune(self.client, index, dry_run=False)
        self.assertEqual(result["deleted"], [])
        self.assertEqual(len(result["failed"]), len(result["delete"]))


class CliTests(StoreTestCase):
    def run_main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(artstore, "_make_client", return_value=self.client), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = artstore.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_stage_fetch_list_refresh_prune_commands(self):
        path, sha = self.make_file("a.bin", 1200, 1)
        manifest = self.tmp / "manifest.json"
        manifest.write_text(json.dumps([{"path": str(path), "sha256": sha}]))
        packdir = self.tmp / "cli-pack"
        code, out, _ = self.run_main("stage", "--manifest-json", str(manifest), "--packdir",
                                     str(packdir), "--part-size", "500")
        self.assertEqual(code, 0)
        summary = json.loads(out)
        self.assertEqual(summary["staged"], [sha])
        self.assertEqual(summary["index_name"], "artstore-index-1001-1-0")
        result = {"packs": [{"name": pack["name"], "dir": pack["dir"]} for pack in summary["packs"]],
                  "index_name": summary["index_name"]}
        self.upload(result, packdir)
        destination = self.fetch_dest()
        code, out, _ = self.run_main("fetch", "--sha256", sha, "--dest", str(destination))
        self.assertEqual((code, out.strip()), (0, "hit"))
        self.assertEqual(destination.read_bytes(), self.files["a.bin"])
        code, out, err = self.run_main("fetch", "--sha256", "b" * 64, "--dest",
                                       str(self.fetch_dest("miss.bin")))
        self.assertEqual((code, out.strip()), (3, "miss"))
        code, out, err = self.run_main("list")
        self.assertEqual(code, 0)
        self.assertIn(sha, out)
        self.assertIn("1.2 KiB", out)
        for secret in (TOKEN, KEY):
            self.assertNotIn(secret, out + err)
        self.assertRegex(out, r"\b8[89]\.\d\b|\b90\.0\b")
        code, out, _ = self.run_main("refresh-plan")
        self.assertEqual((code, out.strip()), (0, ""))
        code, out, _ = self.run_main("refresh-plan", "--within-days", "200")
        self.assertEqual(out.strip(), sha)
        code, out, _ = self.run_main("prune")
        self.assertEqual(code, 0)
        self.assertIn("0 to delete", out)

    def test_cli_without_token_degrades(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = artstore.main(["fetch", "--sha256", "a" * 64, "--dest",
                                  str(self.fetch_dest())])
            listing = artstore.main(["list"])
        self.assertEqual(code, 3)
        self.assertEqual(listing, 4)
        self.assertFalse(self.fetch_dest().exists())


if __name__ == "__main__":
    unittest.main()
