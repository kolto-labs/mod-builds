import contextlib
import hashlib
import io
import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import artstore_client
import artstore_core as core
import game_backup

KEY = "k" * 40
OTHER_KEY = "z" * 40
NOW = datetime(2026, 10, 19, 12, 0, 0, tzinfo=timezone.utc).timestamp()
DAY = 86400


def iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def artifact(identifier, name="game-k1-sealed", created_days_ago=1, expires_in_days=89, expired=False):
    return {"id": identifier, "name": name, "expired": expired,
            "created_at": iso(NOW - created_days_ago * DAY),
            "expires_at": iso(NOW + expires_in_days * DAY)}


class FakeClient:
    def __init__(self, listing=None, members=None, error=None):
        self.listing = listing or []
        self.members = members
        self.error = error
        self.requested = []

    def list_artifacts(self, prefix=None, include_expired=False, name=None):
        if self.error:
            raise self.error
        return [item for item in self.listing if not name or item["name"] == name]

    def read_member(self, artifact_id, member_name, dest_path, expect_size=None):
        self.requested.append((artifact_id, member_name))
        source = Path(self.members) / member_name
        if not source.is_file():
            raise artstore_client.ArtifactMissing("member %r not found" % member_name)
        if expect_size is not None and source.stat().st_size != expect_size:
            raise artstore_client.ArtifactError("member has the wrong size")
        shutil.copyfile(source, dest_path)
        return source.stat().st_size


class SelectionTests(unittest.TestCase):
    def test_newest_live_artifact_with_the_exact_name_wins(self):
        listing = [
            artifact(1, created_days_ago=30),
            artifact(2, created_days_ago=2),
            artifact(3, created_days_ago=1, name="game-k2-sealed"),
            artifact(4, created_days_ago=0.5, name="game-k1-sealed-old"),
            artifact(5, created_days_ago=10),
        ]
        chosen = game_backup.select_artifact(listing, "game-k1-sealed", NOW)
        self.assertEqual(chosen["id"], 2)

    def test_expired_artifacts_are_skipped(self):
        listing = [
            artifact(1, created_days_ago=1, expired=True),
            artifact(2, created_days_ago=2, expires_in_days=-1),
            artifact(3, created_days_ago=60, expires_in_days=30),
        ]
        self.assertEqual(game_backup.select_artifact(listing, "game-k1-sealed", NOW)["id"], 3)

    def test_no_match_returns_none(self):
        listing = [artifact(1, name="game-k2-sealed"), artifact(2, expired=True)]
        self.assertIsNone(game_backup.select_artifact(listing, "game-k1-sealed", NOW))
        self.assertIsNone(game_backup.select_artifact([], "game-k1-sealed", NOW))

    def test_equal_creation_time_prefers_the_higher_id(self):
        listing = [artifact(7, created_days_ago=3), artifact(9, created_days_ago=3)]
        self.assertEqual(game_backup.select_artifact(listing, "game-k1-sealed", NOW)["id"], 9)

    def test_artifact_name(self):
        self.assertEqual(game_backup.artifact_name("k2"), "game-k2-sealed")


class StateTests(unittest.TestCase):
    def test_missing_without_an_artifact(self):
        self.assertEqual(game_backup.backup_state(None, NOW), "missing")

    def test_fresh_when_far_from_expiry(self):
        self.assertEqual(game_backup.backup_state(artifact(1, expires_in_days=15), NOW), "fresh")
        self.assertEqual(game_backup.backup_state(artifact(1, expires_in_days=89), NOW), "fresh")

    def test_stale_inside_the_margin(self):
        self.assertEqual(game_backup.backup_state(artifact(1, expires_in_days=14), NOW), "stale")
        self.assertEqual(game_backup.backup_state(artifact(1, expires_in_days=3), NOW), "stale")

    def test_margin_is_adjustable(self):
        item = artifact(1, expires_in_days=20)
        self.assertEqual(game_backup.backup_state(item, NOW, margin_days=21), "stale")
        self.assertEqual(game_backup.backup_state(item, NOW, margin_days=7), "fresh")

    def test_no_expiry_timestamp_counts_as_fresh(self):
        item = artifact(1)
        item["expires_at"] = None
        self.assertEqual(game_backup.backup_state(item, NOW), "fresh")


class WorkflowEnvironment(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        self.output = self.root / "github_output"
        patcher = mock.patch.dict(os.environ, {"GITHUB_OUTPUT": str(self.output), core.KEY_ENV: KEY})
        patcher.start()
        self.addCleanup(patcher.stop)

    def outputs(self):
        if not self.output.is_file():
            return {}
        return dict(line.split("=", 1) for line in self.output.read_text().splitlines())

    def run_main(self, *argv, client=None):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            if client is None:
                code = game_backup.main(list(argv))
            else:
                with mock.patch.object(artstore_client, "from_environment", return_value=client):
                    code = game_backup.main(list(argv))
        return code, buffer.getvalue()


class StatusTests(WorkflowEnvironment):
    def status(self, client, game="k1"):
        buffer = io.StringIO()
        args = mock.Mock(game=game)
        with contextlib.redirect_stdout(buffer):
            code = game_backup.command_status(args, client=client, now=NOW)
        return code, buffer.getvalue()

    def test_missing(self):
        code, text = self.status(FakeClient([artifact(1, name="game-k2-sealed")]))
        self.assertEqual(code, 0)
        self.assertEqual(self.outputs(), {"state": "missing", "artifact_id": ""})
        self.assertNotIn("::warning::", text)

    def test_fresh_reports_the_id(self):
        self.status(FakeClient([artifact(11, created_days_ago=5), artifact(12, created_days_ago=1)]))
        self.assertEqual(self.outputs(), {"state": "fresh", "artifact_id": "12"})

    def test_stale_reports_the_id(self):
        self.status(FakeClient([artifact(21, expires_in_days=5)]))
        self.assertEqual(self.outputs(), {"state": "stale", "artifact_id": "21"})

    def test_lookup_failure_is_a_warning_with_unknown_state(self):
        code, text = self.status(FakeClient(error=artstore_client.ArtifactError("boom", 500)))
        self.assertEqual(code, 0)
        self.assertEqual(self.outputs(), {"state": "unknown", "artifact_id": ""})
        self.assertIn("::warning::game backup:", text)

    def test_missing_credentials_is_a_warning(self):
        with mock.patch.object(artstore_client, "from_environment", return_value=None):
            code, text = self.run_main("status", "--game", "k2")
        self.assertEqual(code, 0)
        self.assertEqual(self.outputs()["state"], "unknown")
        self.assertIn("::warning::", text)

    def test_token_never_appears_in_output(self):
        token = "ghs_" + "a1b2c3d4" * 4
        with mock.patch.dict(os.environ, {"GH_TOKEN": token}):
            _, text = self.status(FakeClient(error=artstore_client.ArtifactError("denied for " + token, 403)))
        self.assertNotIn(token, text)


class ManifestTests(unittest.TestCase):
    def valid(self):
        return {"schema": 1, "game": "k1", "sha256": "a" * 64, "size": 10,
                "parts": [{"n": 0, "size": 10, "stored_sha256": "b" * 64, "stored_size": 80}]}

    def test_valid_manifest_is_accepted(self):
        data = game_backup.parse_manifest(json.dumps(self.valid()), "k1")
        self.assertEqual(data["parts"][0]["n"], 0)

    def test_wrong_game_schema_or_garbage_is_rejected(self):
        self.assertRaises(ValueError, game_backup.parse_manifest, json.dumps(self.valid()), "k2")
        bad = self.valid()
        bad["schema"] = 2
        self.assertRaises(ValueError, game_backup.parse_manifest, json.dumps(bad), "k1")
        self.assertRaises(ValueError, game_backup.parse_manifest, "[]", "k1")
        self.assertRaises(ValueError, game_backup.parse_manifest, "not json", "k1")

    def test_unsafe_part_names_are_rejected(self):
        bad = self.valid()
        bad["parts"][0]["stored_sha256"] = "../../etc/passwd"
        self.assertRaises(ValueError, game_backup.parse_manifest, json.dumps(bad), "k1")

    def test_out_of_order_or_empty_parts_are_rejected(self):
        bad = self.valid()
        bad["parts"][0]["n"] = 1
        self.assertRaises(ValueError, game_backup.parse_manifest, json.dumps(bad), "k1")
        bad = self.valid()
        bad["parts"] = []
        self.assertRaises(ValueError, game_backup.parse_manifest, json.dumps(bad), "k1")


class RoundTripTests(WorkflowEnvironment):
    def make_seal(self, size=5000):
        seal_dir = self.root / "sealed"
        seal_dir.mkdir()
        payload = os.urandom(size)
        (seal_dir / "baseline.seal").write_bytes(payload)
        return seal_dir, payload

    def test_stage_splits_into_sealed_parts_and_a_manifest(self):
        seal_dir, payload = self.make_seal()
        out = self.root / "artifact"
        manifest = game_backup.stage("k1", seal_dir, out, part_size=2000)
        self.assertEqual(len(manifest["parts"]), 3)
        self.assertEqual(manifest["sha256"], hashlib.sha256(payload).hexdigest())
        names = sorted(path.name for path in out.iterdir())
        self.assertEqual(names, sorted(["manifest.json"] + [part["stored_sha256"] + ".part"
                                                            for part in manifest["parts"]]))
        for part in manifest["parts"]:
            stored = (out / (part["stored_sha256"] + ".part")).read_bytes()
            self.assertEqual(len(stored), part["stored_size"])
            self.assertNotIn(payload[:64], stored)

    def test_fetch_restores_the_exact_seal(self):
        seal_dir, payload = self.make_seal()
        out = self.root / "artifact"
        game_backup.stage("k1", seal_dir, out, part_size=2000)
        target = self.root / "restored"
        client = FakeClient(members=out)
        manifest = game_backup.fetch("k1", 42, target, client)
        self.assertEqual((target / "baseline.seal").read_bytes(), payload)
        self.assertEqual(sorted(path.name for path in target.iterdir()), ["baseline.seal"])
        self.assertEqual(client.requested[0], (42, "manifest.json"))
        self.assertEqual(len(client.requested), 1 + len(manifest["parts"]))

    def test_fetch_with_the_wrong_key_writes_nothing(self):
        seal_dir, _ = self.make_seal()
        out = self.root / "artifact"
        game_backup.stage("k1", seal_dir, out, part_size=2000)
        target = self.root / "restored"
        with mock.patch.dict(os.environ, {core.KEY_ENV: OTHER_KEY}):
            with self.assertRaises(ValueError):
                game_backup.fetch("k1", 42, target, FakeClient(members=out))
        self.assertEqual(list(target.iterdir()), [])

    def test_fetch_rejects_a_tampered_part(self):
        seal_dir, _ = self.make_seal()
        out = self.root / "artifact"
        manifest = game_backup.stage("k1", seal_dir, out, part_size=2000)
        victim = out / (manifest["parts"][1]["stored_sha256"] + ".part")
        data = bytearray(victim.read_bytes())
        data[100] ^= 1
        victim.write_bytes(bytes(data))
        target = self.root / "restored"
        with self.assertRaises(ValueError):
            game_backup.fetch("k1", 42, target, FakeClient(members=out))
        self.assertEqual(list(target.iterdir()), [])

    def test_stage_needs_exactly_one_seal(self):
        empty = self.root / "empty"
        empty.mkdir()
        with self.assertRaises(ValueError):
            game_backup.stage("k1", empty, self.root / "artifact")
        seal_dir, _ = self.make_seal()
        (seal_dir / "second.seal").write_bytes(b"x")
        with self.assertRaises(ValueError):
            game_backup.stage("k1", seal_dir, self.root / "artifact")

    def test_stage_accepts_a_legacy_seal_name(self):
        seal_dir = self.root / "sealed"
        seal_dir.mkdir()
        (seal_dir / "legacy-name.seal").write_bytes(os.urandom(300))
        manifest = game_backup.stage("k2", seal_dir, self.root / "artifact", part_size=1000)
        self.assertEqual(len(manifest["parts"]), 1)

    def test_default_part_size_stays_under_two_gib(self):
        self.assertLess(game_backup.PART_SIZE, 2 * 1024 ** 3)

    def test_command_stage_without_a_key_warns_and_leaves_no_output(self):
        seal_dir, _ = self.make_seal()
        out = self.root / "artifact"
        with mock.patch.dict(os.environ, {core.KEY_ENV: ""}):
            code, text = self.run_main("stage", "--game", "k1", "--seal-dir", str(seal_dir), "--out-dir", str(out))
        self.assertEqual(code, 0)
        self.assertNotIn("staged", self.outputs())
        self.assertFalse(out.exists())
        self.assertIn("::warning::", text)

    def test_command_stage_success_sets_output(self):
        seal_dir, _ = self.make_seal()
        out = self.root / "artifact"
        code, text = self.run_main("stage", "--game", "k1", "--seal-dir", str(seal_dir), "--out-dir", str(out))
        self.assertEqual(code, 0)
        self.assertEqual(self.outputs().get("staged"), "true")
        self.assertNotIn(KEY, text)

    def test_command_fetch_failure_is_a_warning(self):
        target = self.root / "restored"
        empty = self.root / "empty"
        empty.mkdir()
        code, text = self.run_main("fetch", "--game", "k1", "--artifact-id", "7", "--seal-dir", str(target),
                                   client=FakeClient(members=empty))
        self.assertEqual(code, 0)
        self.assertNotIn("restored", self.outputs())
        self.assertIn("::warning::", text)
        self.assertFalse((target / "baseline.seal").exists())

    def test_command_fetch_success_sets_output(self):
        seal_dir, payload = self.make_seal()
        out = self.root / "artifact"
        game_backup.stage("k1", seal_dir, out, part_size=2000)
        target = self.root / "restored"
        code, _ = self.run_main("fetch", "--game", "k1", "--artifact-id", "7", "--seal-dir", str(target),
                                client=FakeClient(members=out))
        self.assertEqual(code, 0)
        self.assertEqual(self.outputs().get("restored"), "true")
        self.assertEqual((target / "baseline.seal").read_bytes(), payload)


if __name__ == "__main__":
    unittest.main()
