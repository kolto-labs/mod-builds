"""Exercise owned stage boundaries without Rust compilation or live acquisition."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import artstore_client
import fullrun_stage as stage


class ObjectOwnership(unittest.TestCase):
    def test_artifact_read_owner_is_explicit_and_does_not_relocate_the_run(self):
        env = {"GITHUB_REPOSITORY": "owner/run", "ARTSTORE_REPOSITORY": "legacy/store", "ARTSTORE_TOKEN": "fixture"}
        self.assertEqual(artstore_client.from_environment(env).repo, "legacy/store")
        self.assertEqual(env["GITHUB_REPOSITORY"], "owner/run")
        del env["ARTSTORE_REPOSITORY"]
        self.assertEqual(artstore_client.from_environment(env).repo, "owner/run")

    def test_invalid_explicit_owner_never_silently_uses_the_caller(self):
        env = {"GITHUB_REPOSITORY": "owner/run", "ARTSTORE_REPOSITORY": "invalid", "ARTSTORE_TOKEN": "fixture"}
        self.assertIsNone(artstore_client.from_environment(env))


class StageInputs(unittest.TestCase):
    def test_cache_key_ignores_timestamps_but_not_changed_content(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "manifest.json"
            path.write_text(json.dumps({"sha256": "a", "checked_at": "one"}))
            first = stage.cache_key(path)
            path.write_text(json.dumps({"sha256": "a", "checked_at": "two"}))
            self.assertEqual(first, stage.cache_key(path))
            path.write_text(json.dumps({"sha256": "b", "checked_at": "two"}))
            self.assertNotEqual(first, stage.cache_key(path))

    def test_identity_rejects_a_checkout_that_does_not_match_the_request(self):
        with tempfile.TemporaryDirectory() as root, mock.patch.object(stage.subprocess, "check_output", side_effect=["a"*40, "c"*40]):
            with self.assertRaises(ValueError):
                stage.identity("engine", "guides", "a"*40, "b"*40, Path(root)/"identity.json")

    def test_private_bucket_metadata_is_not_an_arbitrary_rclone_path(self):
        env = {"RCLONE_CONFIG_MODCACHE_ACCESS_KEY_ID": "fixture", "MOD_CACHE_BUCKET": "bucket/other"}
        with mock.patch.dict(os.environ, env), self.assertRaises(ValueError):
            stage.remote_options()

    def test_restore_failure_does_not_launch_an_installer(self):
        with tempfile.TemporaryDirectory() as root:
            args = self.arguments(Path(root))
            with mock.patch.object(stage, "checked", side_effect=ValueError("missing archives")) as checked:
                with self.assertRaises(ValueError): stage.install(args)
            self.assertEqual(checked.call_count, 1)

    def arguments(self, root):
        guides = root / "guides"
        (guides / "content/k1").mkdir(parents=True)
        (guides / "content/k1/full.md").write_text("# fixture\n")
        return argparse.Namespace(output=str(root/"evidence"), work_dir=str(root/"work"),
                                  guides=str(guides), cache=str(root/"cache"),
                                  engine=str(root/"engine"), game_dir=str(root/"game"), game="k1", mode="cli")

    def test_successful_process_with_partial_report_still_fails_coverage(self):
        with tempfile.TemporaryDirectory() as root:
            args = self.arguments(Path(root))
            def child(command, log, **kwargs):
                if "ports.py" in command[1]:
                    (Path(args.output)/"report.json").write_text(json.dumps({"status":"partial"}))
            with mock.patch.object(stage, "checked", side_effect=child), self.assertRaises(ValueError):
                stage.install(args)

    def test_work_payload_is_outside_the_uploaded_evidence_tree(self):
        with tempfile.TemporaryDirectory() as root:
            args = self.arguments(Path(root))
            commands = []
            def child(command, log, **kwargs):
                commands.append(command)
                if "ports.py" in command[1]:
                    digest = hashlib.sha256((Path(args.guides)/"content/k1/full.md").read_bytes()).hexdigest()
                    report = {"status":"installed", "implementation":"rust", "mode":"cli", "partial":False,
                              "component_count":2, "lane_errors":{"count":0}, "omitted_components":[], "guide_sha256":digest}
                    (Path(args.output)/"report.json").write_text(json.dumps(report))
            with mock.patch.object(stage, "checked", side_effect=child), mock.patch.dict(os.environ, {}, clear=True):
                stage.install(args)
            port = commands[1]
            self.assertEqual(port[port.index("--source")+1], args.engine)
            self.assertEqual(port[port.index("--archives")+1], str(Path(args.work_dir)/"archives"))
            self.assertEqual(port[port.index("--work-dir")+1], str(Path(args.work_dir)/"work"))
            self.assertEqual(json.loads((Path(args.output)/"completion.json").read_text())["status"], "complete")

    def test_installer_environment_drops_storage_keys_runtime_tokens_and_credentials(self):
        secrets = {"CACHE_ENCRYPTION_KEY":"fixture", "ARTSTORE_TOKEN":"fixture", "MODSYNC_SOURCE_TOKEN":"fixture",
                   "RCLONE_CONFIG_MODCACHE_ACCESS_KEY_ID":"fixture", "AWS_SECRET_ACCESS_KEY":"fixture",
                   "ACTIONS_RUNTIME_TOKEN":"fixture", "GIT_CONFIG_GLOBAL":"credential-file", "SSH_AUTH_SOCK":"agent"}
        with mock.patch.dict(os.environ, {**secrets, "RUSTUP_TOOLCHAIN":"1.95.0", "PATH":"/bin"}, clear=True):
            env = stage.installer_environment()
        self.assertFalse(set(secrets) & set(env))
        self.assertEqual(env["RUSTUP_TOOLCHAIN"], "1.95.0")

    def test_seed_pin_selection_uses_only_the_requested_game_inventory(self):
        import seed_archives
        with tempfile.TemporaryDirectory() as root:
            args = self.arguments(Path(root))
            with mock.patch.object(seed_archives, "load_pins", return_value={"a":"pin", "b":"pin"}), \
                 mock.patch.object(stage.cache, "discover", return_value=[{"id":"b"}]) as discover:
                self.assertEqual(stage.selected_seed_ids(args.guides, ["k1"]), ["b"])
            self.assertEqual(discover.call_args.args[0], [str(Path(args.guides)/"content/k1/full.md")])


if __name__ == "__main__":
    unittest.main()
