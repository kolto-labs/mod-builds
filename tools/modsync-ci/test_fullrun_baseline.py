"""Credential-free recovery/namespace tests; no Rust or game processes run."""
import contextlib
import io
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

import fullrun_baseline as baseline


NOW = 1_800_000_000


def artifact(identifier, fresh=True):
    return {"id": identifier, "name": "game-k1-sealed", "expired": False,
            "created_at": identifier, "expires_at": NOW + (30 if fresh else 2) * 86400}


class FakeClient:
    def __init__(self, repository, items):
        self.repository = repository
        self.items = items

    def list_artifacts(self, name=None):
        return self.items


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.seals = self.root / "sealed"
        self.game = self.root / "game"
        self.environ = {"RUNNER_TEMP": str(self.root), "GITHUB_REPOSITORY": "owner/current",
                        "GITHUB_TOKEN": "current-only", "ARTSTORE_READ_TOKEN": "legacy-only",
                        "ARTSTORE_REPOSITORY": "must/not-override", "CACHE_ENCRYPTION_KEY": "test-key"}
        self.listings = {"owner/current": [], "owner/legacy": []}
        self.payloads = {}
        self.clients = []
        self.fetches = []
        self.verified = []
        for target, replacement in (("_client", self.client), ("verify_seal", self.verify)):
            patcher = mock.patch.object(baseline, target, side_effect=replacement)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(baseline.game_backup, "fetch", side_effect=self.fetch)
        patcher.start()
        self.addCleanup(patcher.stop)

    def client(self, repository, token, environ):
        self.clients.append((repository, token))
        return FakeClient(repository, self.listings[repository])

    def fetch(self, game, identifier, directory, client):
        self.fetches.append((client.repository, identifier))
        payload = self.payloads[(client.repository, identifier)]
        if isinstance(payload, Exception):
            raise payload
        (directory / "baseline.seal").write_bytes(payload)

    def verify(self, game, candidate, destination, environ):
        payload = candidate.read_bytes()
        self.verified.append(payload)
        if payload.startswith(b"bad"):
            destination.mkdir()
            raise subprocess.CalledProcessError(1, ["mock-verifier"])
        destination.mkdir()
        (destination / "verified.txt").write_bytes(payload)

    def cached(self, payload=b"cache-good", name="baseline.seal"):
        self.seals.mkdir(exist_ok=True)
        (self.seals / name).write_bytes(payload)

    def backup(self, repository="owner/current", identifier=1, payload=b"artifact-good", fresh=True):
        self.listings[repository].append(artifact(identifier, fresh))
        self.payloads[(repository, identifier)] = payload

    def recover(self, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()):
            return baseline.recover("k1", self.seals, self.game, environ=self.environ, now=NOW, **kwargs)

    def test_valid_cache_is_verified_and_missing_current_backup_requires_publication(self):
        self.cached()
        result = self.recover(legacy_repository="owner/legacy")
        self.assertEqual(result, {"available": True, "origin": "cache", "backup_required": True})
        self.assertEqual((self.game / "verified.txt").read_bytes(), b"cache-good")
        self.assertEqual(self.verified, [b"cache-good"])
        self.assertEqual(self.clients, [("owner/current", "current-only")])

    def test_corrupt_cache_tries_current_before_legacy_and_preserves_other_candidates(self):
        self.cached(b"bad-cache", "old.seal")
        self.backup()
        self.backup("owner/legacy", payload=b"legacy-good")
        result = self.recover(legacy_repository="owner/legacy")
        self.assertEqual(result["origin"], "artifact:current:owner/current:1")
        self.assertFalse(result["backup_required"])
        self.assertEqual(self.fetches, [("owner/current", 1)])
        self.assertEqual((self.seals / "old.seal").read_bytes(), b"bad-cache")

    def test_corrupt_newest_current_artifact_tries_older_current_artifact(self):
        self.backup(identifier=2, payload=b"bad-newest")
        self.backup(identifier=1)
        result = self.recover(legacy_repository="owner/legacy")
        self.assertEqual(self.fetches, [("owner/current", 2), ("owner/current", 1)])
        self.assertTrue(result["available"])
        self.assertFalse(result["backup_required"])

    def test_legacy_fresh_backup_never_suppresses_current_publication(self):
        self.backup("owner/legacy", identifier=3, payload=b"legacy-good")
        result = self.recover(legacy_repository="owner/legacy")
        self.assertEqual(result["origin"], "artifact:legacy:owner/legacy:3")
        self.assertTrue(result["backup_required"])
        self.assertEqual(self.clients, [("owner/current", "current-only"), ("owner/legacy", "legacy-only")])

    def test_current_backups_are_discovered_first_after_previous_legacy_publication(self):
        self.backup(identifier=4, payload=b"current-published")
        self.backup("owner/legacy", identifier=5, payload=b"legacy-good")
        result = self.recover(legacy_repository="owner/legacy")
        self.assertEqual(result["origin"], "artifact:current:owner/current:4")
        self.assertEqual(self.clients, [("owner/current", "current-only")])

    def test_stale_current_backup_recovers_but_requires_current_refresh(self):
        self.backup(fresh=False)
        result = self.recover()
        self.assertTrue(result["available"])
        self.assertTrue(result["backup_required"])

    def test_fresh_corrupt_primary_backup_does_not_suppress_backup_for_valid_cache(self):
        self.cached()
        self.backup(payload=b"bad-artifact")
        result = self.recover()
        self.assertEqual(result["origin"], "cache")
        self.assertTrue(result["backup_required"])

    def test_identical_current_backup_uses_already_verified_cache_bytes(self):
        self.cached()
        self.backup(payload=b"cache-good")
        result = self.recover()
        self.assertFalse(result["backup_required"])
        self.assertEqual(self.verified, [b"cache-good"])

    def test_consumer_does_not_read_a_backup_after_verified_cache_recovery(self):
        self.cached()
        self.backup(payload=b"another-valid-baseline")
        result = self.recover(audit_backup=False)
        self.assertTrue(result["available"])
        self.assertEqual(self.clients, [])
        self.assertEqual(self.fetches, [])

    def test_different_valid_current_backup_does_not_preserve_selected_cache_baseline(self):
        self.cached()
        self.backup(payload=b"another-valid-baseline")
        result = self.recover()
        self.assertEqual(result["origin"], "cache")
        self.assertTrue(result["backup_required"])
        self.assertEqual((self.game / "verified.txt").read_bytes(), b"cache-good")

    def test_all_missing_or_corrupt_candidates_leave_no_game_tree(self):
        self.cached(b"bad-cache")
        self.backup(payload=OSError("inaccessible"))
        result = self.recover()
        self.assertFalse(result["available"])
        self.assertFalse(self.game.exists())
        self.assertEqual((self.seals / "baseline.seal").read_bytes(), b"bad-cache")
        self.assertEqual(list(self.root.glob("fullrun-baseline-*")), [])

    def test_local_only_never_constructs_an_artifact_client(self):
        self.cached()
        result = self.recover(local_only=True)
        self.assertTrue(result["available"])
        self.assertEqual(self.clients, [])

    def test_existing_destination_and_paths_outside_runner_temp_are_rejected(self):
        self.game.mkdir()
        with self.assertRaises(ValueError):
            self.recover()
        self.game.rmdir()
        with self.assertRaises(ValueError):
            baseline.recover("k1", self.seals, self.root.parent / "outside", environ=self.environ)

    def test_optional_missing_and_required_missing_have_distinct_exit_codes_and_outputs(self):
        output = self.root / "output"
        args = ["--game", "k1", "--seal-dir", str(self.seals), "--game-dir", str(self.game)]
        with mock.patch.dict(os.environ, {**self.environ, "GITHUB_OUTPUT": str(output)}, clear=True):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(baseline.main(args), 0)
                self.assertEqual(baseline.main(args + ["--required"]), 1)
        self.assertIn("available=false\norigin=none\nbackup_required=true\n", output.read_text())


class VerificationEnvironmentTests(unittest.TestCase):
    def test_only_unpack_receives_decryption_key_and_neither_process_gets_provider_credentials(self):
        environ = {"PATH": "/test/bin", "RUNNER_TEMP": "/tmp/test", "CACHE_ENCRYPTION_KEY": "key",
                   "GITHUB_TOKEN": "token", "ARTSTORE_READ_TOKEN": "legacy", "STEAM_PASSWORD": "password",
                   "GAME_SEED_KEY_K1": "seed", "PYTHONPATH": "/host/imports"}
        with mock.patch.object(baseline.subprocess, "run") as run:
            baseline.verify_seal("k1", Path("/tmp/cache.seal"), Path("/tmp/game"), environ)
        unpack, inventory = run.call_args_list
        self.assertEqual(unpack.kwargs["env"], {"PATH": "/test/bin", "RUNNER_TEMP": "/tmp/test",
                                               "CACHE_ENCRYPTION_KEY": "key"})
        self.assertEqual(inventory.kwargs["env"], {"PATH": "/test/bin", "RUNNER_TEMP": "/tmp/test"})
        self.assertIn("--require-modules", inventory.args[0])

    def test_client_namespace_and_token_are_explicit(self):
        environ = {"ARTSTORE_REPOSITORY": "wrong/repo", "ARTSTORE_TOKEN": "wrong-token"}
        with mock.patch.object(baseline.artstore_client, "ArtifactsClient") as constructor:
            baseline._client("owner/current", "current-token", environ)
        self.assertEqual(constructor.call_args.args, ("owner/current", "current-token"))


if __name__ == "__main__":
    unittest.main()
