import base64
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock
import zipfile

spec = importlib.util.spec_from_file_location("seed_archives", Path(__file__).with_name("seed_archives.py"))
seed = importlib.util.module_from_spec(spec)
spec.loader.exec_module(seed)
cache, refresh = seed.cache, seed.refresh

BUCKET = "example-bucket"
SECRET_VALUE = "example-secret-value-1234"
CONF = f"[STAGING]\ntype = local\naccess_key_id = example-key-id-5678\nsecret_access_key = {SECRET_VALUE}\n"
ONE = "https://www.nexusmods.com/example_game/mods/1001"
TWO = "https://www.gamefront.com/games/example-game/file/example-two"
THREE = "https://www.nexusmods.com/example_game/mods/1003"


def zip_bytes(label, extra=b""):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(f"{label}.txt", label.encode() * 8 + extra)
    return buffer.getvalue()


def pin_for(url, provider, files):
    return {"url": url, "provider": provider, "provenance": cache.PIN_PROVENANCE,
            "objects": [{"archive_name": name, "sha256": cache.hashlib.sha256(data).hexdigest(),
                         "size": len(data), "extension": "zip"} for name, data in files.items()]}


class FakeRclone:
    """Maps STAGING:/MODCACHE: remote paths onto temporary directories."""

    def __init__(self, base):
        self.staging = Path(base, "staging")
        self.cache = Path(base, "r2")
        self.staging.mkdir()
        self.cache.mkdir()
        self.calls = []
        self.config_modes = []
        self.config_paths = set()

    def local(self, value):
        value = str(value)
        if value.startswith("STAGING:"):
            return self.staging / value.split("/", 1)[1]
        if value.startswith("MODCACHE:"):
            return self.cache / value.split("/modsync-archives/", 1)[1]
        return Path(value)

    def __call__(self, *args, timeout=600):
        self.calls.append(args)
        if args[0] != "copyto":
            raise AssertionError("unexpected rclone command")
        source, destination = self.local(args[1]), self.local(args[2])
        if "--config" in args:
            config = Path(args[args.index("--config") + 1])
            self.config_paths.add(config)
            self.config_modes.append(config.stat().st_mode & 0o777)
        if not source.is_file():
            raise subprocess.CalledProcessError(3, ["rclone"])
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)

    def remote_size(self, remote_path):
        path = self.local(remote_path)
        return path.stat().st_size if path.is_file() else None

    def manifest(self):
        path = self.cache / "manifest.json"
        return json.loads(path.read_text()) if path.exists() else None

    def staging_copies(self):
        return [call for call in self.calls if str(call[1]).startswith("STAGING:")]


class SeedTestCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.base = Path(self.directory.name)
        self.fake = FakeRclone(self.base)
        self.files = {"example-one.zip": zip_bytes("one"), "example-two.zip": zip_bytes("two"),
                      "example-three.zip": zip_bytes("three")}
        for name, data in self.files.items():
            (self.fake.staging / name).write_bytes(data)
        self.pins = {
            cache.key_for(ONE): pin_for(ONE, "nexus", {"example-one.zip": self.files["example-one.zip"]}),
            cache.key_for(TWO): pin_for(TWO, "gamefront", {"example-two.zip": self.files["example-two.zip"],
                                                           "example-three.zip": self.files["example-three.zip"]}),
        }
        self.guide = self.base / "full.md"
        self.guide.write_text(f"- [One]({ONE})\n- [Two]({TWO})\n- [Three]({THREE})\n")
        self.pins_path = self.base / "seed_pins.json"
        self.write_pins()
        self.environment = {
            seed.STAGING_CONF_ENV: base64.b64encode(CONF.encode()).decode(),
            "RCLONE_CONFIG_MODCACHE_ACCESS_KEY_ID": "example-write-id",
            "RCLONE_CONFIG_MODCACHE_SECRET_ACCESS_KEY": "example-write-secret",
            "RCLONE_CONFIG_MODCACHE_ENDPOINT": "https://example.invalid", "MOD_CACHE_BUCKET": BUCKET,
            "RUNNER_TEMP": str(self.base),
        }
        patches = [mock.patch.dict(os.environ, self.environment), mock.patch.object(refresh, "rclone", self.fake),
                   mock.patch.object(seed, "remote_size", self.fake.remote_size)]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        os.environ.pop("GITHUB_ACTIONS", None)
        os.environ.pop("GITHUB_STEP_SUMMARY", None)

    def write_pins(self):
        self.pins_path.write_text(json.dumps({"schema": 1, "entries": self.pins}))

    def run_main(self, *extra):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = seed.main(["--pins", str(self.pins_path), "--guide", str(self.guide),
                              "--work-dir", str(self.base / "work"), *extra])
        self.output = out.getvalue() + err.getvalue()
        return code

    def object_names(self):
        objects = self.fake.cache / "objects"
        return sorted(path.name for path in objects.iterdir()) if objects.is_dir() else []

    def assert_clean(self):
        for secret in (SECRET_VALUE, "example-key-id-5678", "example-write-secret", "example-one", "example-two",
                       "nexusmods", "gamefront", "example_game"):
            self.assertNotIn(secret, self.output)
        self.assertEqual([], list((self.base / "work").glob("*")))
        for config in self.fake.config_paths:
            self.assertFalse(config.exists())


class SeedTests(SeedTestCase):
    def test_seeds_every_pin_and_leaves_no_scratch(self):
        self.assertEqual(0, self.run_main())
        manifest = self.fake.manifest()
        self.assertEqual(set(self.pins), set(manifest["entries"]))
        expected = sorted(each["sha256"] for pin in self.pins.values() for each in pin["objects"])
        self.assertEqual(expected, self.object_names())
        pins = cache.load_pins(self.pins_path)
        for identity, entry in manifest["entries"].items():
            self.assertEqual("pinned", entry["status"])
            self.assertEqual("operator-seed", entry["evidence"])
            item = next(each for each in cache.discover([str(self.guide)]) if each["id"] == identity)
            self.assertTrue(cache.is_pinned(item, entry, pins))
        self.assertIn("objects", manifest["entries"][cache.key_for(TWO)])
        self.assertNotIn("objects", manifest["entries"][cache.key_for(ONE)])
        self.assertEqual({0o600}, set(self.fake.config_modes))
        self.assert_clean()

    def test_staging_config_is_only_passed_to_staging_copies(self):
        self.run_main()
        for call in self.fake.calls:
            self.assertEqual(str(call[1]).startswith("STAGING:"), "--config" in call)

    def test_rerun_is_idempotent_and_skips_staging(self):
        self.assertEqual(0, self.run_main())
        self.fake.calls.clear()
        self.assertEqual(0, self.run_main())
        self.assertEqual([], self.fake.staging_copies())
        self.assertIn('"already_seeded": 2', self.output)
        self.assertEqual(0, self.run_main("--force"))
        self.assertTrue(self.fake.staging_copies())

    def test_only_limits_the_run(self):
        self.assertEqual(0, self.run_main("--only", cache.key_for(ONE)))
        self.assertEqual({cache.key_for(ONE)}, set(self.fake.manifest()["entries"]))

    def test_existing_manifest_entries_are_preserved(self):
        (self.fake.cache / "manifest.json").write_text(json.dumps(
            {"schema": 1, "entries": {"a" * 64: {"status": "fresh"}}, "refresh_attempts": {"a" * 64: 1}}))
        self.assertEqual(0, self.run_main())
        manifest = self.fake.manifest()
        self.assertIn("a" * 64, manifest["entries"])
        self.assertEqual({"a" * 64: 1}, manifest["refresh_attempts"])

    def test_sha_mismatch_fails_and_publishes_nothing_for_that_pin(self):
        first = self.pins[cache.key_for(ONE)]["objects"][0]
        tampered = bytearray(self.files["example-one.zip"])
        tampered[-1] ^= 0xFF
        (self.fake.staging / "example-one.zip").write_bytes(bytes(tampered))
        self.assertEqual(len(tampered), first["size"])
        self.assertEqual(1, self.run_main())
        manifest = self.fake.manifest()
        self.assertNotIn(cache.key_for(ONE), manifest["entries"])
        self.assertIn(cache.key_for(TWO), manifest["entries"])
        self.assertNotIn(first["sha256"], self.object_names())
        self.assertIn("sha256 does not match its pin", self.output)
        self.assert_clean()

    def test_size_mismatch_fails(self):
        (self.fake.staging / "example-one.zip").write_bytes(self.files["example-one.zip"] + b"\0")
        self.assertEqual(1, self.run_main("--only", cache.key_for(ONE)))
        self.assertIsNone(self.fake.manifest())
        self.assertEqual([], self.object_names())
        self.assertIn("size does not match its pin", self.output)

    def test_missing_staged_file_fails(self):
        (self.fake.staging / "example-three.zip").unlink()
        self.assertEqual(1, self.run_main())
        self.assertNotIn(cache.key_for(TWO), self.fake.manifest()["entries"])
        self.assertIn("staged file missing", self.output)

    def test_non_archive_matching_its_hash_is_rejected(self):
        page = b"<html>error page</html>" * 8
        (self.fake.staging / "example-one.zip").write_bytes(page)
        pin = self.pins[cache.key_for(ONE)]["objects"][0]
        pin.update(sha256=cache.hashlib.sha256(page).hexdigest(), size=len(page))
        self.write_pins()
        self.assertEqual(1, self.run_main("--only", cache.key_for(ONE)))
        self.assertEqual([], self.object_names())

    def test_dry_run_verifies_but_writes_nothing_and_needs_no_cache_credentials(self):
        for name in list(os.environ):
            if name.startswith("RCLONE_CONFIG_MODCACHE") or name == "MOD_CACHE_BUCKET":
                del os.environ[name]
        self.assertEqual(0, self.run_main("--dry-run"))
        self.assertTrue(self.fake.staging_copies())
        self.assertEqual([], list(self.fake.cache.iterdir()))
        self.assert_clean()

    def test_dry_run_still_fails_on_a_mismatch(self):
        (self.fake.staging / "example-one.zip").write_bytes(b"PK\x03\x04" + b"\0" * 32)
        self.assertEqual(1, self.run_main("--dry-run"))

    def test_pins_that_left_the_guides_hard_fail(self):
        self.guide.write_text(f"- [One]({ONE})\n")
        self.assertEqual(1, self.run_main())
        self.assertEqual([], list(self.fake.cache.iterdir()))
        self.assertEqual([], self.fake.calls)

    def test_missing_configuration_is_a_usage_error(self):
        del os.environ[seed.STAGING_CONF_ENV]
        self.assertEqual(2, self.run_main())
        os.environ[seed.STAGING_CONF_ENV] = self.environment[seed.STAGING_CONF_ENV]
        del os.environ["MOD_CACHE_BUCKET"]
        self.assertEqual(2, self.run_main())
        self.assertEqual([], self.fake.calls)

    def test_unknown_only_id_is_a_usage_error(self):
        self.assertEqual(2, self.run_main("--only", "f" * 64))

    def test_interrupt_cleans_scratch_and_config(self):
        def interrupted(*args, timeout=600):
            if any(str(argument).startswith("STAGING:") for argument in args):
                raise seed.Stop("terminated")
            return self.fake(*args, timeout=timeout)

        with mock.patch.object(refresh, "rclone", interrupted):
            self.assertEqual(1, self.run_main())
        self.assertEqual([], list((self.base / "work").glob("*")))


class VerifyTests(SeedTestCase):
    def seeded(self):
        self.assertEqual(0, self.run_main())
        for name in ("RCLONE_CONFIG_MODCACHE_ACCESS_KEY_ID", "RCLONE_CONFIG_MODCACHE_SECRET_ACCESS_KEY"):
            os.environ[name] = "example-read-credential"
        del os.environ[seed.STAGING_CONF_ENV]
        self.fake.calls.clear()

    def test_verify_restores_every_pin_without_staging(self):
        self.seeded()
        self.assertEqual(0, self.run_main("--verify"))
        self.assertEqual([], self.fake.staging_copies())
        self.assertIn('"mode": "verify"', self.output)
        self.assert_clean()

    def test_verify_fails_when_an_object_is_missing(self):
        self.seeded()
        (self.fake.cache / "objects" / self.pins[cache.key_for(ONE)]["objects"][0]["sha256"]).unlink()
        self.assertEqual(1, self.run_main("--verify"))
        self.assertIn("pinned object missing", self.output)

    def test_verify_fails_when_an_object_is_corrupt(self):
        self.seeded()
        path = self.fake.cache / "objects" / self.pins[cache.key_for(ONE)]["objects"][0]["sha256"]
        data = bytearray(path.read_bytes())
        data[-1] ^= 0xFF
        path.write_bytes(bytes(data))
        self.assertEqual(1, self.run_main("--verify"))
        self.assertIn("does not match its pin", self.output)

    def test_verify_fails_when_the_entry_is_not_pinned(self):
        self.seeded()
        manifest = self.fake.manifest()
        manifest["entries"][cache.key_for(ONE)]["status"] = "stale-unknown"
        (self.fake.cache / "manifest.json").write_text(json.dumps(manifest))
        self.assertEqual(1, self.run_main("--verify"))

    def test_verify_fails_without_a_manifest(self):
        del os.environ[seed.STAGING_CONF_ENV]
        self.assertEqual(1, self.run_main("--verify"))


class ConfigTests(unittest.TestCase):
    def encode(self, text):
        return base64.b64encode(text.encode()).decode()

    def test_unset_config_is_none(self):
        self.assertIsNone(seed.staging_config({}))
        self.assertIsNone(seed.staging_config({seed.STAGING_CONF_ENV: "  "}))

    def test_rejects_bad_configs(self):
        for value in ("not base64!!", self.encode("   "), self.encode("[OTHER]\ntype = local\n"),
                      self.encode("no section header\n"), self.encode("[STAGING]\n" + "x = 1\n" * 20000)):
            with self.assertRaises(ValueError):
                seed.staging_config({seed.STAGING_CONF_ENV: value})

    def test_accepts_a_staging_remote(self):
        self.assertEqual(CONF, seed.staging_config({seed.STAGING_CONF_ENV: self.encode(CONF)}))

    def test_masks_credentials_only_on_github_actions(self):
        for environ, expected in (({}, ""), ({"GITHUB_ACTIONS": "true"}, "")):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                seed.mask_config_secrets(CONF, environ)
            masked = out.getvalue()
            if environ:
                self.assertIn(f"::add-mask::{SECRET_VALUE}", masked)
                self.assertIn("::add-mask::example-key-id-5678", masked)
                self.assertNotIn("::add-mask::local", masked)
            else:
                self.assertEqual(expected, masked)

    def test_private_write_refuses_to_overwrite_and_is_0600(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "staging.conf")
            seed.write_private(path, CONF)
            self.assertEqual(0o600, path.stat().st_mode & 0o777)
            with self.assertRaises(FileExistsError):
                seed.write_private(path, CONF)


class PinTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name, "seed_pins.json")
        self.data = zip_bytes("one")
        self.pin = pin_for(ONE, "nexus", {"example-one.zip": self.data})

    def write(self, entries):
        self.path.write_text(json.dumps({"schema": 1, "entries": entries}))

    def test_valid_pins_load(self):
        self.write({cache.key_for(ONE): self.pin})
        self.assertEqual({cache.key_for(ONE)}, set(seed.load_pins(self.path)))

    def test_invalid_pins_are_hard_errors_not_silently_dropped(self):
        wrong_id = {"b" * 64: self.pin}
        bad_provenance = {cache.key_for(ONE): {**self.pin, "provenance": "scraped"}}
        no_objects = {cache.key_for(ONE): {**self.pin, "objects": []}}
        for entries in (wrong_id, bad_provenance, no_objects, {}):
            self.write(entries)
            with self.assertRaises(ValueError):
                seed.load_pins(self.path)

    def test_missing_or_malformed_file_is_an_error(self):
        with self.assertRaises(ValueError):
            seed.load_pins(self.path)
        self.path.write_text("{")
        with self.assertRaises(ValueError):
            seed.load_pins(self.path)

    def test_one_archive_name_cannot_hold_two_contents(self):
        other = pin_for(THREE, "nexus", {"example-one.zip": zip_bytes("different")})
        self.write({cache.key_for(ONE): self.pin, cache.key_for(THREE): other})
        with self.assertRaises(ValueError):
            seed.load_pins(self.path)

    def test_unsafe_archive_name_and_empty_size_are_rejected(self):
        for change in ({"archive_name": "../escape.zip"}, {"size": 0}):
            pin = json.loads(json.dumps(self.pin))
            pin["objects"][0].update(change)
            self.write({cache.key_for(ONE): pin})
            with self.assertRaises(ValueError):
                seed.load_pins(self.path)


if __name__ == "__main__":
    unittest.main()
