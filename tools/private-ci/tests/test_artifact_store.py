import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src import artifact_store


class ArtifactStoreTests(unittest.TestCase):
    def test_stage_is_content_addressed_and_deduplicates_chunk_hashes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            encrypted = root / "payload.age"
            encrypted.write_bytes(b"abcdefgh" + b"ijklmnop" + b"abcdefgh")
            result = artifact_store.stage(encrypted, root / "chunks", 8)
            self.assertEqual(len(result), 2)
            for sha, item in result.items():
                self.assertEqual(sha, artifact_store.digest(root / "chunks" / item["file"])[0])

    def test_index_merges_old_segments_and_hashes_body(self):
        old = {"schema": 1, "chunks": {"a" * 64: {"size": 2, "artifactId": 10}}}
        new = {"b" * 64: {"size": 3, "file": "x.part"}}
        index = artifact_store.make_index(new, segment_id=11, previous=old)
        self.assertEqual(index["chunks"]["a" * 64]["artifactId"], 10)
        body = {"schema": 1, "chunks": index["chunks"]}
        self.assertEqual(index["sha256"], hashlib.sha256(artifact_store.json_bytes(body)).hexdigest())

    def test_zip_slip_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            archive = Path(temp) / "x.zip"
            import zipfile
            with zipfile.ZipFile(archive, "w") as output:
                output.writestr("../escape", b"x")
            with self.assertRaises(artifact_store.ArtifactStoreError):
                artifact_store.extract_zip(archive, Path(temp) / "out")

    def test_api_does_not_print_or_put_token_in_url(self):
        request = []
        class Response:
            status = 200
            headers = {}
            def read(self): return b"{}"
            def __enter__(self): return self
            def __exit__(self, *args): pass
        def opener(value, timeout):
            request.append(value)
            return Response()
        with patch("urllib.request.urlopen", opener):
            artifact_store.api("owner/repo", "secret-token", "/actions/artifacts")
        self.assertNotIn("secret-token", request[0].full_url)


if __name__ == "__main__":
    unittest.main()
