import contextlib
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.request
import urllib.error

from src.mod_cache import (AwsCliS3ObjectStore, CacheError, LocalObjectStore, ModCache,
                          PrivateRedirectHandler, REFRESH_INTERVAL, main)


class ModCacheTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.objects = {"/first": {"body": b"PK\x03\x04original first payload", "etag": '"first-v1"'},
                        "/second": {"body": b"PK\x03\x04original second payload", "etag": '"second-v1"'}}
        self.requests = []
        self.conditional_304 = True
        test = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def respond(self, head=False):
                test.requests.append((self.command, self.path, dict(self.headers)))
                item = test.objects.get(self.path)
                if not item:
                    self.send_response(404)
                    self.end_headers()
                    return
                status = item.get("status", 200)
                if status == 200 and head and test.conditional_304 and self.headers.get("If-None-Match") == item["etag"]:
                    status = 304
                self.send_response(status)
                self.send_header("ETag", item["etag"])
                self.send_header("Last-Modified", "Mon, 01 Jan 2024 00:00:00 GMT")
                self.send_header("Content-Type", item.get("type", "application/octet-stream"))
                if status != 304:
                    self.send_header("Content-Length", str(len(item["body"])))
                self.end_headers()
                if not head:
                    self.wfile.write(item["body"])

            def do_HEAD(self):
                self.respond(True)

            def do_GET(self):
                self.respond(False)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.sources = [{"id": name, "url": self.base + "/" + name,
                         "filename": name + ".zip", "provider": "direct", "revision": "r1"}
                        for name in ("first", "second")]
        self.now = 1_800_000_000.0
        self.store = LocalObjectStore(self.root / "private-cache")
        self.cache = ModCache(self.store, minimum_request_interval=0, clock=lambda: self.now)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.directory.cleanup()

    def seed(self):
        result = self.cache.refresh(self.sources)
        self.assertTrue(result["ok"], result["blockers"])
        self.requests.clear()
        return result

    def get_requests(self):
        return [(method, path) for method, path, _ in self.requests if method == "GET"]

    def test_conditional_304_refresh_has_no_get_and_fourteen_day_gate_reuses_generation(self):
        first = self.seed()
        skipped = self.cache.refresh(self.sources)
        self.assertTrue(skipped["ok"])
        self.assertTrue(skipped["skipped"])
        self.assertEqual(skipped["generation"], first["generation"])
        self.assertEqual(self.requests, [])
        self.now += REFRESH_INTERVAL + 1
        refreshed = self.cache.refresh(self.sources)
        self.assertTrue(refreshed["ok"], refreshed["blockers"])
        self.assertEqual(refreshed["changed"], [])
        self.assertEqual(refreshed["unchanged"], ["first", "second"])
        self.assertEqual(self.get_requests(), [])
        self.assertTrue(all(headers.get("If-None-Match") for method, _, headers in self.requests if method == "HEAD"))
        self.assertNotEqual(refreshed["generation"], first["generation"])
        self.assertEqual(self.cache.generation(first["generation"], first["generationDigest"])["sources"][0]["revision"], "r1")

    def test_unchanged_200_strong_validators_have_no_get_and_modified_source_has_single_get(self):
        self.seed()
        self.conditional_304 = False
        unchanged = self.cache.refresh(self.sources, force=True)
        self.assertTrue(unchanged["ok"], unchanged["blockers"])
        self.assertEqual(self.get_requests(), [])
        self.requests.clear()
        self.objects["/second"] = {"body": b"PK\x03\x04changed second payload", "etag": '"second-v2"'}
        changed = self.cache.refresh(self.sources, force=True)
        self.assertTrue(changed["ok"], changed["blockers"])
        self.assertEqual(changed["changed"], ["second"])
        self.assertEqual(changed["unchanged"], ["first"])
        self.assertEqual(self.get_requests(), [("GET", "/second")])

    def test_partial_failure_preserves_previous_catalog_and_publishes_no_new_cas(self):
        first = self.seed()
        original_pointer = self.store.read("catalog.json")
        original_cas = set((self.store.root / "cas").rglob("*"))
        self.objects["/first"] = {"body": b"PK\x03\x04new first", "etag": '"first-v2"'}
        self.objects["/second"]["status"] = 403
        failed = self.cache.refresh(self.sources, force=True)
        self.assertFalse(failed["ok"])
        self.assertTrue(failed["blockers"])
        self.assertEqual(failed["generation"], first["generation"])
        self.assertEqual(self.store.read("catalog.json"), original_pointer)
        self.assertEqual(set((self.store.root / "cas").rglob("*")), original_cas)
        self.assertEqual(json.loads(original_pointer)["lastSuccess"], json.loads(self.store.read("catalog.json"))["lastSuccess"])

    def test_all_six_offline_installs_reuse_one_frozen_generation_without_network(self):
        result = self.seed()
        self.server.shutdown()
        for index in range(6):
            receipt = self.cache.materialize(result["generation"], self.root / f"offline-{index}",
                                             generation_digest=result["generationDigest"])
            self.assertTrue(receipt["offline"])
            for name in ("first", "second"):
                self.assertEqual((self.root / f"offline-{index}" / (name + ".zip")).read_bytes(), self.objects["/" + name]["body"])
        self.assertEqual(self.requests, [])

    def test_duplicate_payloads_deduplicate_by_digest(self):
        self.objects["/second"] = dict(self.objects["/first"])
        result = self.seed()
        files = [path for path in (self.store.root / "cas").rglob("*") if path.is_file()]
        self.assertEqual(len(files), 1)
        catalog = self.cache.generation(result["generation"], result["generationDigest"])
        self.assertEqual(catalog["sources"][0]["sha256"], catalog["sources"][1]["sha256"])

    def test_html_and_expected_hash_mismatch_never_publish_a_catalog(self):
        self.objects["/first"]["body"] = b"<html>private login credentials required</html>"
        result = self.cache.refresh(self.sources)
        self.assertFalse(result["ok"])
        self.assertIsNone(self.store.read("catalog.json"))
        self.assertFalse((self.store.root / "cas").exists())
        self.objects["/first"]["body"] = b"PK\x03\x04valid bytes"
        pinned = [dict(self.sources[0], sha256="0" * 64)]
        result = self.cache.refresh(pinned)
        self.assertFalse(result["ok"])
        self.assertIsNone(self.store.read("catalog.json"))

    def test_offline_materialization_preflights_all_digests_before_directory_publication(self):
        result = self.seed()
        catalog = self.cache.generation(result["generation"], result["generationDigest"])
        second = catalog["sources"][1]
        self.store.path(f"cas/{second['sha256'][:2]}/{second['sha256']}").write_bytes(b"corrupt")
        destination = self.root / "corrupt-offline"
        with self.assertRaises(CacheError):
            self.cache.materialize(result["generation"], destination, generation_digest=result["generationDigest"])
        self.assertFalse(destination.exists())
        with self.assertRaises(CacheError):
            self.cache.materialize(result["generation"], destination, generation_digest="0" * 64)

    def test_unresolved_sources_and_offline_filename_collisions_block_publication(self):
        unresolved = self.cache.refresh([{"id": "unknown", "status": "unresolved"}])
        self.assertFalse(unresolved["ok"])
        same_name = [dict(source, filename="same.zip") for source in self.sources]
        result = self.cache.refresh(same_name)
        self.assertFalse(result["ok"])
        self.assertIsNone(self.store.read("catalog.json"))

    def test_revision_change_forces_download_even_when_head_validator_matches(self):
        self.seed()
        changed = [dict(self.sources[0], revision="r2"), self.sources[1]]
        result = self.cache.refresh(changed, force=True)
        self.assertTrue(result["ok"], result["blockers"])
        self.assertEqual(self.get_requests(), [("GET", "/first")])

    def test_cli_never_prints_private_source_urls_or_blocker_details(self):
        marker = "PRIVATE_SOURCE_CREDENTIAL_SENTINEL"
        source = self.root / "private-sources.json"
        source.write_text(json.dumps([dict(self.sources[0], url=self.base + "/" + marker)]))
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(["refresh", "--cache", str(self.root / "cli-cache"), "--sources", str(source), "--receipt", str(self.root / "private-result.json")])
        self.assertEqual(code, 1)
        self.assertNotIn(marker, stdout.getvalue() + stderr.getvalue())
        self.assertEqual(stdout.getvalue(), "Private mod cache operation blocked.\n")

    def test_redirects_remove_credentials_on_cross_origin_and_reject_downgrades(self):
        handler = PrivateRedirectHandler()
        request = urllib.request.Request("https://original.example/archive", headers={"Authorization": "private", "Cookie": "private", "X-Api-Key": "private"})
        redirected = handler.redirect_request(request, None, 302, "Found", {}, "https://cdn.example/archive")
        self.assertTrue(all(name.lower() not in {"authorization", "cookie", "x-api-key"} for name in redirected.headers))
        with self.assertRaises(CacheError):
            handler.redirect_request(request, None, 302, "Found", {}, "http://127.0.0.1/archive")

    def test_s3_backend_refuses_unverified_public_bucket_before_any_object_operation(self):
        with patch.object(AwsCliS3ObjectStore, "_run", return_value={"PublicAccessBlockConfiguration": {}}) as run:
            with self.assertRaises(CacheError):
                AwsCliS3ObjectStore("private-cache-bucket", "cache", self.root / "private-s3.log")
            self.assertEqual(run.call_count, 1)

    def test_s3_compatible_private_probe_supports_unsupported_aws_api_and_readonly_consumers(self):
        calls = []
        def run(_store, arguments, key=None):
            calls.append((arguments, key))
            if arguments[0] == "get-public-access-block":
                return None  # S3-compatible provider does not implement AWS API.
            if arguments[0] == "put-object":
                self.assertNotIn("--server-side-encryption", arguments)
                return {"ETag": '"probe"'}
            return {}
        with patch.object(AwsCliS3ObjectStore, "_run", run), patch.object(AwsCliS3ObjectStore, "_assert_private") as privacy:
            store = AwsCliS3ObjectStore("private-bucket", "prefix", self.root / "provider.log", endpoint_url="https://private.example", sse="none")
            self.assertEqual(privacy.call_count, 1)
            self.assertTrue(any(arguments[0] == "put-object" and key.startswith("privacy-probe/") for arguments, key in calls))
            self.assertTrue(any(arguments[0] == "delete-object" for arguments, _ in calls))
            calls.clear()
            reader = AwsCliS3ObjectStore("private-bucket", "prefix", self.root / "reader.log", endpoint_url="https://private.example", sse="none", read_only=True)
            self.assertFalse(any(arguments[0] == "put-object" for arguments, _ in calls))
            with self.assertRaises(CacheError):
                reader.write("catalog.json", b"private metadata")
            self.assertEqual(store.endpoint_url, "https://private.example")

    def test_anonymous_probe_rejects_public_objects_and_unverified_network_errors(self):
        with patch.object(AwsCliS3ObjectStore, "_run", return_value=None):
            with self.assertRaises(CacheError):
                AwsCliS3ObjectStore("private-bucket", "prefix", self.root / "failed-probe.log", endpoint_url="https://private.example", sse="none")
        # Exercise unsigned probe independently, without any live bucket or data.
        probe = AwsCliS3ObjectStore.__new__(AwsCliS3ObjectStore)
        probe.endpoint_url = "https://private.example"
        probe.bucket, probe.prefix = "private-bucket", "prefix"
        opener = unittest.mock.Mock()
        opener.open.return_value.__enter__ = unittest.mock.Mock(return_value=opener)
        opener.open.return_value.__exit__ = unittest.mock.Mock(return_value=False)
        with patch.object(urllib.request, "build_opener", return_value=opener), self.assertRaises(CacheError):
            probe._assert_private("known-existing-object")
        for status in (403, 404):
            opener.open.side_effect = urllib.error.HTTPError("https://private.example", status, "Denied", {}, None)
            with patch.object(urllib.request, "build_opener", return_value=opener):
                probe._assert_private("known-existing-object")

    def test_local_store_rejects_public_checkout_paths_and_object_traversal(self):
        with patch.dict(os.environ, {"PUBLIC_CI_WORKSPACE": str(self.root)}):
            with self.assertRaises(CacheError):
                LocalObjectStore(self.root / "public-cache")
        with self.assertRaises((CacheError, ValueError)):
            self.store.read("../outside")


if __name__ == "__main__":
    unittest.main()
