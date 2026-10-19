import hashlib
import io
import json
import os
import random
import re
import shutil
import tempfile
import threading
import time
import traceback
import unittest
import urllib.parse
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

import artstore_client
from artstore_client import (
    ArtifactError,
    ArtifactExpired,
    ArtifactMissing,
    ArtifactsClient,
    DigestMismatch,
    RangedFile,
    RateLimited,
)

BASE = "/repos/o/r/actions/artifacts"
FUTURE = "2099-01-01T00:00:00Z"


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
        self.overrides = []
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
                for override in list(outer.overrides):
                    if override(self, record):
                        return
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

    def override(self, predicate, responder, times=1):
        state = {"left": times}

        def handle(handler, record):
            if state["left"] <= 0 or not predicate(record):
                return False
            state["left"] -= 1
            responder(handler, record)
            return True

        self.overrides.append(handle)

    def hits(self, suffix):
        return [record for record in self.requests if record["path"].endswith(suffix)]


class FakeBlobStore(FakeServer):
    def __init__(self):
        self.blobs = {}
        self.valid_sigs = set()
        self.bytes_served = 0
        self.counter = 0
        super().__init__(self.serve)

    def new_sig(self):
        self.counter += 1
        sig = "sig%d" % self.counter
        self.valid_sigs.add(sig)
        return sig

    def revoke_all(self):
        self.valid_sigs.clear()

    def serve(self, handler, record):
        if "authorization" in record["headers"]:
            return send(handler, 401, b"<Error>unexpected credentials</Error>")
        match = re.fullmatch(r"/blob/(\d+)", record["path"])
        if not match or int(match.group(1)) not in self.blobs:
            return send(handler, 404, b"<Error>missing</Error>")
        if record["query"].get("sig", [""])[0] not in self.valid_sigs:
            return send(handler, 403, b"<Error>expired signature</Error>")
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
        body = data[start : end + 1]
        self.bytes_served += len(body)
        send(
            handler,
            206,
            body,
            {"Content-Range": "bytes %d-%d/%d" % (start, end, len(data))},
        )


class FakeGitHub(FakeServer):
    def __init__(self, blob, token):
        self.blob = blob
        self.token = token
        self.artifacts = []
        super().__init__(self.serve)

    def add(self, artifact_id, name, expired=False, created_at=None, expires_at=FUTURE, payload=None):
        self.artifacts.append(
            {
                "id": artifact_id,
                "name": name,
                "size_in_bytes": len(payload or b""),
                "expired": expired,
                "created_at": created_at or "2026-01-01T00:00:00Z",
                "expires_at": expires_at,
                "digest": "sha256:" + hashlib.sha256(payload or b"").hexdigest(),
                "workflow_run": {"id": 1, "head_branch": "main", "head_sha": "abc"},
            }
        )
        if payload is not None:
            self.blob.blobs[artifact_id] = payload

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
            name = query.get("name", [None])[0]
            items = [item for item in self.artifacts if name is None or item["name"] == name]
            window = items[(page - 1) * per_page : page * per_page]
            return send(handler, 200, {"total_count": len(items), "artifacts": window})
        match = re.fullmatch(BASE + r"/(\d+)/zip", path)
        if match and record["method"] == "GET":
            artifact = self.find(int(match.group(1)))
            if artifact is None:
                return send(handler, 404, {"message": "Not Found"})
            if artifact["expired"]:
                return send(handler, 410, {"message": "Artifact has expired"})
            location = "%s/blob/%d?sig=%s" % (self.blob.url, artifact["id"], self.blob.new_sig())
            return send(handler, 302, b"", {"Location": location})
        match = re.fullmatch(BASE + r"/(\d+)", path)
        if match and record["method"] == "DELETE":
            artifact = self.find(int(match.group(1)))
            if artifact is None:
                return send(handler, 404, {"message": "Not Found"})
            self.artifacts.remove(artifact)
            return send(handler, 204)
        send(handler, 404, {"message": "Not Found"})


def build_zip(members, compression=zipfile.ZIP_STORED):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression) as archive:
        for name, data in members:
            archive.writestr(zipfile.ZipInfo(name, (2026, 1, 1, 0, 0, 0)), data, compression)
    return buffer.getvalue()


class ClientTestCase(unittest.TestCase):
    TOKEN = "FAKE-TOKEN"

    def setUp(self):
        self.blob = FakeBlobStore()
        self.api = FakeGitHub(self.blob, self.TOKEN)
        self.addCleanup(self.blob.stop)
        self.addCleanup(self.api.stop)
        self.sleeps = []
        patcher = mock.patch.object(artstore_client.time, "sleep", side_effect=self.sleeps.append)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.client = self.make_client()

    def make_client(self, token=None, **options):
        return ArtifactsClient(
            "o/r",
            token or self.TOKEN,
            api=self.api.url,
            opener=artstore_client.make_opener(trust_env=False),
            **options,
        )

    def dest(self, name="out.bin"):
        return os.path.join(self.tmp, name)

    def assert_blob_saw_no_credentials(self):
        self.assertTrue(self.blob.requests)
        for record in self.blob.requests:
            self.assertNotIn("authorization", record["headers"])


class ListingTests(ClientTestCase):
    def test_pagination_collects_all_pages_newest_first(self):
        for index in range(1, 251):
            self.api.add(
                index,
                "blob-%d" % index,
                created_at="2026-02-01T%02d:%02d:%02dZ" % (index // 3600, index // 60 % 60, index % 60),
            )
        random.Random(3).shuffle(self.api.artifacts)
        result = self.client.list_artifacts()
        self.assertEqual([item["id"] for item in result], list(range(250, 0, -1)))
        pages = [r["query"]["page"] for r in self.api.hits("/artifacts")]
        self.assertEqual(pages, [["1"], ["2"], ["3"]])
        self.assertTrue(all(r["query"]["per_page"] == ["100"] for r in self.api.requests))

    def test_exact_multiple_of_page_size_stops_without_extra_call(self):
        for index in range(1, 201):
            self.api.add(index, "blob-%d" % index)
        self.assertEqual(len(self.client.list_artifacts()), 200)
        self.assertEqual(len(self.api.requests), 2)

    def test_prefix_filter_is_client_side(self):
        self.api.add(1, "blob-aa")
        self.api.add(2, "meta-aa")
        self.api.add(3, "blob-bb")
        result = self.client.list_artifacts(prefix="blob-")
        self.assertEqual(sorted(item["id"] for item in result), [1, 3])
        self.assertTrue(all("name" not in r["query"] for r in self.api.requests))

    def test_exact_name_uses_server_side_filter(self):
        self.api.add(1, "blob-aa")
        self.api.add(2, "blob-aab")
        result = self.client.list_artifacts(name="blob-aa")
        self.assertEqual([item["id"] for item in result], [1])
        self.assertEqual(self.api.requests[0]["query"]["name"], ["blob-aa"])

    def test_expired_filtering(self):
        self.api.add(1, "a", expired=True)
        self.api.add(2, "a", expires_at="2000-01-01T00:00:00Z")
        self.api.add(3, "a")
        self.assertEqual([item["id"] for item in self.client.list_artifacts()], [3])
        everything = self.client.list_artifacts(include_expired=True)
        self.assertEqual(sorted(item["id"] for item in everything), [1, 2, 3])


class RedirectAndDownloadTests(ClientTestCase):
    def test_resolve_url_does_not_follow_redirect(self):
        self.api.add(7, "blob", payload=b"hello")
        url = self.client.resolve_url(7)
        self.assertTrue(url.startswith(self.blob.url + "/blob/7?sig="))
        self.assertEqual(self.blob.requests, [])
        self.assertEqual(self.api.requests[0]["headers"]["authorization"], "Bearer " + self.TOKEN)

    def test_download_strips_auth_on_second_hop(self):
        payload = random.Random(1).randbytes(200_000)
        self.api.add(7, "blob", payload=payload)
        digest = self.client.download_zip(7, self.dest(), expect_digest="sha256:" + hashlib.sha256(payload).hexdigest())
        with open(self.dest(), "rb") as handle:
            self.assertEqual(handle.read(), payload)
        self.assertEqual(digest, "sha256:" + hashlib.sha256(payload).hexdigest())
        self.assert_blob_saw_no_credentials()
        self.assertEqual(os.listdir(self.tmp), ["out.bin"])

    def test_digest_mismatch_is_atomic(self):
        self.api.add(7, "blob", payload=b"payload")
        with open(self.dest(), "wb") as handle:
            handle.write(b"old")
        with self.assertRaises(DigestMismatch):
            self.client.download_zip(7, self.dest(), expect_digest="sha256:" + "0" * 64)
        with open(self.dest(), "rb") as handle:
            self.assertEqual(handle.read(), b"old")
        self.assertEqual(os.listdir(self.tmp), ["out.bin"])

    def test_malformed_digest_rejected(self):
        self.api.add(7, "blob", payload=b"payload")
        with self.assertRaises(ArtifactError):
            self.client.download_zip(7, self.dest(), expect_digest="md5:abc")

    def test_download_reresolves_after_rejected_blob_url(self):
        self.api.add(7, "blob", payload=b"payload")
        self.blob.override(lambda r: True, lambda h, r: send(h, 403, b"<Error/>"), times=1)
        self.client.download_zip(7, self.dest())
        self.assertEqual(len(self.api.hits("/7/zip")), 2)
        with open(self.dest(), "rb") as handle:
            self.assertEqual(handle.read(), b"payload")


class StatusMappingTests(ClientTestCase):
    def test_410_maps_to_expired(self):
        self.api.add(1, "a", expired=True)
        with self.assertRaises(ArtifactExpired) as ctx:
            self.client.resolve_url(1)
        self.assertEqual(ctx.exception.status, 410)
        with self.assertRaises(ArtifactExpired):
            self.client.download_zip(1, self.dest())
        self.assertEqual(os.listdir(self.tmp), [])

    def test_404_maps_to_missing(self):
        with self.assertRaises(ArtifactMissing) as ctx:
            self.client.resolve_url(999)
        self.assertEqual(ctx.exception.status, 404)

    def test_delete(self):
        self.api.add(1, "a")
        self.assertIsNone(self.client.delete_artifact(1))
        self.assertIsNone(self.api.find(1))
        with self.assertRaises(ArtifactMissing):
            self.client.delete_artifact(1)

    def test_exception_hierarchy(self):
        for cls in (ArtifactExpired, ArtifactMissing, RateLimited, DigestMismatch):
            self.assertTrue(issubclass(cls, ArtifactError))

    def test_non_numeric_id_rejected_before_any_request(self):
        with self.assertRaises(ValueError):
            self.client.resolve_url("1/../../x")
        self.assertEqual(self.api.requests, [])


class RangedReadTests(ClientTestCase):
    def setUp(self):
        super().setUp()
        rng = random.Random(11)
        self.big = rng.randbytes(3 * 1024 * 1024)
        self.small = rng.randbytes(20_000)
        self.zip_bytes = build_zip([("big.bin", self.big), ("small.bin", self.small), ("meta.json", b"{}")])
        self.api.add(100, "blob-x", payload=self.zip_bytes)

    def test_member_read_transfers_only_a_fraction(self):
        written = self.client.read_member(100, "small.bin", self.dest(), expect_size=len(self.small))
        self.assertEqual(written, len(self.small))
        with open(self.dest(), "rb") as handle:
            self.assertEqual(handle.read(), self.small)
        self.assertLess(self.blob.bytes_served, len(self.zip_bytes) * 0.1)
        self.assert_blob_saw_no_credentials()
        self.assertEqual(os.listdir(self.tmp), ["out.bin"])

    def test_large_member_read_uses_few_requests(self):
        self.client.read_member(100, "big.bin", self.dest())
        with open(self.dest(), "rb") as handle:
            self.assertEqual(handle.read(), self.big)
        self.assertLessEqual(len(self.blob.requests), 6)

    def test_size_mismatch_is_rejected_before_transfer(self):
        with self.assertRaises(ArtifactError):
            self.client.read_member(100, "small.bin", self.dest(), expect_size=5)
        self.assertLess(self.blob.bytes_served, len(self.zip_bytes) * 0.1)
        self.assertEqual(os.listdir(self.tmp), [])

    def test_missing_member(self):
        with self.assertRaises(ArtifactMissing):
            self.client.read_member(100, "nope.bin", self.dest())

    def test_deflated_member_falls_back_to_full_download(self):
        compressible = b"abc" * 100_000
        self.api.add(101, "blob-z", payload=build_zip([("z.bin", compressible)], zipfile.ZIP_DEFLATED))
        self.blob.bytes_served = 0
        self.client.read_member(101, "z.bin", self.dest())
        with open(self.dest(), "rb") as handle:
            self.assertEqual(handle.read(), compressible)
        self.assertEqual(sorted(os.listdir(self.tmp)), ["out.bin"])

    def test_zipfile_works_directly_on_ranged_file(self):
        with RangedFile(self.client, 100) as ranged:
            self.assertEqual(ranged.size, len(self.zip_bytes))
            with zipfile.ZipFile(ranged) as archive:
                self.assertEqual(archive.namelist(), ["big.bin", "small.bin", "meta.json"])
                self.assertEqual(archive.read("meta.json"), b"{}")
                self.assertEqual(archive.read("small.bin"), self.small)
            self.assertEqual(ranged.seek(-4, 2), len(self.zip_bytes) - 4)
            self.assertEqual(ranged.read(), self.zip_bytes[-4:])
            self.assertEqual(ranged.read(10), b"")

    def test_reresolve_after_403(self):
        ranged = RangedFile(self.client, 100)
        self.addCleanup(ranged.close)
        self.assertEqual(ranged.read(10), self.zip_bytes[:10])
        self.assertEqual(ranged.resolves, 1)
        self.blob.revoke_all()
        ranged.seek(2_000_000)
        self.assertEqual(ranged.read(100), self.zip_bytes[2_000_000:2_000_100])
        self.assertEqual(ranged.resolves, 2)
        self.assertEqual(len(self.api.hits("/100/zip")), 2)
        self.assert_blob_saw_no_credentials()

    def test_persistent_403_gives_up(self):
        ranged = RangedFile(self.client, 100)
        self.addCleanup(ranged.close)
        self.blob.override(lambda r: True, lambda h, r: send(h, 403, b"<Error/>"), times=1000)
        with self.assertRaises(ArtifactError):
            ranged.read(10)
        self.assertLessEqual(len(self.api.hits("/100/zip")), 3)

    def test_expired_artifact_surfaces_from_ranged_read(self):
        self.api.find(100)["expired"] = True
        with self.assertRaises(ArtifactExpired):
            RangedFile(self.client, 100).read(1)

    def test_url_older_than_ttl_is_refreshed(self):
        ranged = RangedFile(self.client, 100, url_ttl=0.0)
        self.addCleanup(ranged.close)
        ranged.read(10)
        ranged.seek(2_000_000)
        ranged.read(10)
        self.assertGreaterEqual(ranged.resolves, 2)


class RateLimitTests(ClientTestCase):
    def limit(self, status, headers=None, message="slow down", times=1):
        self.api.override(
            lambda r: r["path"] == BASE,
            lambda h, r: send(h, status, {"message": message}, headers),
            times=times,
        )

    def test_retry_after_is_honored(self):
        self.api.add(1, "a")
        self.limit(429, {"Retry-After": "3"})
        self.assertEqual(len(self.client.list_artifacts()), 1)
        self.assertEqual(self.sleeps, [3.0])

    def test_primary_limit_waits_until_reset(self):
        self.api.add(1, "a")
        reset = str(int(time.time()) + 5)
        self.limit(403, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": reset}, "API rate limit exceeded")
        self.assertEqual(len(self.client.list_artifacts()), 1)
        self.assertEqual(len(self.sleeps), 1)
        self.assertTrue(4.0 <= self.sleeps[0] <= 7.0, self.sleeps)

    def test_secondary_limit_uses_exponential_backoff(self):
        self.api.add(1, "a")
        self.limit(403, message="You have exceeded a secondary rate limit.", times=2)
        self.assertEqual(len(self.client.list_artifacts()), 1)
        self.assertEqual(self.sleeps, [1.0, 2.0])

    def test_backoff_is_bounded(self):
        self.limit(429, times=1000)
        with self.assertRaises(RateLimited):
            self.client.list_artifacts()
        self.assertEqual(self.sleeps, [1.0, 2.0, 4.0, 8.0])
        self.assertEqual(len(self.api.requests), 5)

    def test_long_reset_raises_without_sleeping(self):
        self.limit(429, {"Retry-After": "100000"}, times=1000)
        with self.assertRaises(RateLimited) as ctx:
            self.client.list_artifacts()
        self.assertEqual(ctx.exception.retry_after, 100000.0)
        self.assertEqual(self.sleeps, [])
        self.assertEqual(len(self.api.requests), 1)

    def test_plain_403_is_not_a_rate_limit(self):
        self.limit(403, message="Resource not accessible by integration", times=1000)
        with self.assertRaises(ArtifactError) as ctx:
            self.client.list_artifacts()
        self.assertNotIsInstance(ctx.exception, RateLimited)
        self.assertEqual(ctx.exception.status, 403)
        self.assertEqual(self.sleeps, [])

    def test_transient_5xx_is_retried(self):
        self.api.add(1, "a")
        self.limit(502, times=1)
        self.assertEqual(len(self.client.list_artifacts()), 1)
        self.assertEqual(self.sleeps, [1.0])

    def test_delete_honors_rate_limit(self):
        self.api.add(1, "a")
        self.api.override(
            lambda r: r["method"] == "DELETE",
            lambda h, r: send(h, 429, {"message": "slow"}, {"Retry-After": "2"}),
        )
        self.client.delete_artifact(1)
        self.assertEqual(self.sleeps, [2.0])


class SecretHandlingTests(ClientTestCase):
    SECRET = "ghp_SECRETSECRETSECRET1234567890"

    def exception_text(self, exc):
        parts = ["".join(traceback.format_exception(exc)), repr(exc), str(exc), repr(exc.args)]
        cause = exc.__cause__ or exc.__context__
        while cause is not None:
            parts.append(repr(cause))
            parts.append(str(cause))
            cause = cause.__cause__ or cause.__context__
        return "\n".join(parts)

    def test_token_never_in_exception_text_even_if_server_echoes_it(self):
        client = self.make_client(token=self.SECRET)
        self.api.add(1, "a", payload=b"x")
        for status in (401, 403, 429, 500):
            self.api.overrides.clear()
            self.api.override(
                lambda r: True,
                lambda h, r, status=status: send(
                    h, status, {"message": "echo " + r["headers"].get("authorization", "")}
                ),
                times=1000,
            )
            calls = [
                client.list_artifacts,
                lambda: client.resolve_url(1),
                lambda: client.delete_artifact(1),
                lambda: client.download_zip(1, self.dest()),
                lambda: client.read_member(1, "x", self.dest()),
            ]
            for call in calls:
                with self.assertRaises(ArtifactError) as ctx:
                    call()
                self.assertNotIn(self.SECRET, self.exception_text(ctx.exception))
        self.assertNotIn(self.SECRET, repr(client))
        self.assertNotIn(self.SECRET, str(client))
        self.assertNotIn(self.SECRET, repr(client._token))

    def test_network_failure_text_has_no_token_or_url(self):
        client = ArtifactsClient(
            "o/r",
            self.SECRET,
            api="http://127.0.0.1:1",
            opener=artstore_client.make_opener(trust_env=False),
            timeout=2,
        )
        with self.assertRaises(ArtifactError) as ctx:
            client.list_artifacts()
        text = self.exception_text(ctx.exception)
        self.assertNotIn(self.SECRET, text)
        self.assertEqual(len(self.sleeps), 4)

    def test_signed_blob_url_not_in_exception_text(self):
        self.api.add(1, "a", payload=b"x")
        self.blob.override(lambda r: True, lambda h, r: send(h, 403, b"<Error/>"), times=1000)
        with self.assertRaises(ArtifactError) as ctx:
            self.client.download_zip(1, self.dest())
        self.assertNotIn("sig=", self.exception_text(ctx.exception))

    def test_constructor_errors_do_not_echo_token(self):
        with self.assertRaises(ValueError) as ctx:
            ArtifactsClient("not-a-repo", self.SECRET)
        self.assertNotIn(self.SECRET, str(ctx.exception))

    def test_credentials_are_never_sent_to_non_api_urls(self):
        with self.assertRaises(ArtifactError):
            self.client._request("GET", self.blob.url + "/blob/1", auth=True)
        self.assertEqual(self.blob.requests, [])


class FromEnvironmentTests(unittest.TestCase):
    def test_returns_none_without_token_or_repo(self):
        self.assertIsNone(artstore_client.from_environment({"GITHUB_REPOSITORY": "o/r"}))
        self.assertIsNone(artstore_client.from_environment({"GITHUB_TOKEN": "t"}))
        self.assertIsNone(artstore_client.from_environment({"GITHUB_REPOSITORY": "o/r", "GITHUB_TOKEN": "  "}))
        self.assertIsNone(artstore_client.from_environment({"GITHUB_REPOSITORY": "bad", "GITHUB_TOKEN": "t"}))

    def test_token_precedence(self):
        env = {"GITHUB_REPOSITORY": "o/r", "GH_TOKEN": "gh", "GITHUB_TOKEN": "gt", "ARTSTORE_TOKEN": "as"}
        self.assertEqual(artstore_client.from_environment(env)._token.reveal(), "as")
        del env["ARTSTORE_TOKEN"]
        self.assertEqual(artstore_client.from_environment(env)._token.reveal(), "gt")
        del env["GITHUB_TOKEN"]
        client = artstore_client.from_environment(env)
        self.assertEqual(client._token.reveal(), "gh")
        self.assertEqual(client.repo, "o/r")
        self.assertEqual(client.api, "https://api.github.com")
        self.assertNotIn("gh", repr(client).replace("github", ""))

    def test_reads_process_environment_and_api_url(self):
        env = {"GITHUB_REPOSITORY": "o/r", "GH_TOKEN": "gh", "GITHUB_API_URL": "https://ghe.example/api/v3/"}
        with mock.patch.dict(os.environ, env, clear=True):
            client = artstore_client.from_environment()
        self.assertEqual(client.api, "https://ghe.example/api/v3")


if __name__ == "__main__":
    unittest.main()
