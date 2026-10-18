import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest import mock
import zipfile

spec = importlib.util.spec_from_file_location("cache", Path(__file__).with_name("cache.py"))
cache = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cache)
refresh_spec = importlib.util.spec_from_file_location("refresh_archives", Path(__file__).with_name("refresh_archives.py"))
refresh_archives = importlib.util.module_from_spec(refresh_spec)
refresh_spec.loader.exec_module(refresh_archives)

NEXUS_URL = "https://www.nexusmods.com/example/mods/1234"
GAMEFRONT_URL = "https://www.gamefront.com/games/example/file/pack-a"
DEADLY_URL = "https://deadlystream.com/files/file/123-example-post/"
DAY = 86400


def item_for(url):
    kind, direct = cache.provider(url)
    return {"id": cache.key_for(url), "url": url, "provider": kind, "direct": direct, "guides": []}


def zip_bytes(label):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr(label + ".txt", label * 20)
    return stream.getvalue()


def args_for(**overrides):
    values = dict(download_timeout=30, integrity_timeout=30, max_bytes=10 ** 9, timeout=5, retries=0,
                  apply=True, import_manifest={}, import_manifest_document={})
    values.update(overrides)
    return types.SimpleNamespace(**values)


class PinFixture(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.base = Path(self.directory.name)
        self.root = self.base / "cache"
        (self.root / "objects").mkdir(parents=True)
        self.pins_path = self.base / "seed_pins.json"
        for module in (cache, refresh_archives.cache):
            patcher = mock.patch.object(module, "PINS_PATH", self.pins_path)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.payload = zip_bytes("pack_a")
        self.sha = cache.hashlib.sha256(self.payload).hexdigest()

    def write_pins(self, url=NEXUS_URL, sha=None, provider=None, extra_objects=()):
        item = item_for(url)
        objects = [{"archive_name": "pack_a.zip", "sha256": sha or self.sha,
                    "size": len(self.payload), "extension": "zip"}, *extra_objects]
        self.pins_path.write_text(json.dumps({"schema": 1, "entries": {item["id"]: {
            "url": url, "provider": provider or item["provider"],
            "provenance": "operator-local-download", "objects": objects}}}), encoding="utf-8")
        return item

    def stored_entry(self, status="pinned", evidence="operator-seed", checked_at=None, store=True):
        if store:
            (self.root / "objects" / self.sha).write_bytes(self.payload)
        entry = {"sha256": self.sha, "size": len(self.payload), "extension": "zip", "archive_name": "pack_a.zip",
                 "status": status, "evidence": evidence, "metadata_unknown": True}
        if checked_at is not None:
            entry["checked_at"] = checked_at
        return entry

    def write_manifest(self, item, entry):
        (self.root / "manifest.json").write_text(
            json.dumps({"schema": 1, "entries": {item["id"]: entry}}), encoding="utf-8")

    def read_entry(self, item):
        return json.loads((self.root / "manifest.json").read_text())["entries"][item["id"]]

    def run_main(self, item, command, *extra):
        guide = self.base / "guide.md"
        guide.write_text("[mod](" + item["url"] + ")\n", encoding="utf-8")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = cache.main([command, "--guide", str(guide), "--cache-dir", str(self.root), "--retries", "0", *extra])
        return code, json.loads(output.getvalue())


class LoadPinsTests(PinFixture):
    def test_missing_file_means_no_pins(self):
        self.assertEqual(cache.load_pins(), {})

    def test_valid_pin_loads_and_invalid_pins_are_dropped(self):
        item = self.write_pins()
        self.assertEqual(set(cache.load_pins()), {item["id"]})
        self.write_pins(url=DEADLY_URL, provider="deadlystream")
        self.assertEqual(cache.load_pins(), {})
        self.write_pins(sha="Z" * 64)
        self.assertEqual(cache.load_pins(), {})
        document = json.loads(self.pins_path.read_text())
        document["entries"] = {"0" * 64: next(iter(document["entries"].values()))}
        self.pins_path.write_text(json.dumps(document))
        self.assertEqual(cache.load_pins(), {})

    def test_unsupported_schema_is_rejected(self):
        self.pins_path.write_text(json.dumps({"schema": 2, "entries": {}}))
        with self.assertRaises(ValueError):
            cache.load_pins()

    def test_is_pinned_requires_status_evidence_pin_and_matching_hashes(self):
        item = self.write_pins()
        self.assertTrue(cache.is_pinned(item, self.stored_entry()))
        self.assertFalse(cache.is_pinned(item, self.stored_entry(status="verified")))
        self.assertFalse(cache.is_pinned(item, self.stored_entry(evidence="local-import")))
        other = self.stored_entry()
        other["sha256"] = "1" * 64
        self.assertFalse(cache.is_pinned(item, other))
        extra = {"archive_name": "pack_b.zip", "sha256": "2" * 64, "size": 1, "extension": "zip"}
        self.write_pins(extra_objects=[extra])
        self.assertFalse(cache.is_pinned(item, self.stored_entry()))
        self.pins_path.unlink()
        self.assertFalse(cache.is_pinned(item, self.stored_entry()))

    def test_is_pinned_rejects_other_providers_even_with_a_pin(self):
        item = self.write_pins(url=DEADLY_URL, provider="deadlystream")
        self.assertFalse(cache.is_pinned(item, self.stored_entry()))


class PinnedImportTests(PinFixture):
    def source(self, payload=None):
        path = self.base / "pack_a.zip"
        path.write_bytes(payload or self.payload)
        return path

    def test_import_object_mismatch_writes_nothing(self):
        item = item_for(NEXUS_URL)
        status, document, sha, size = cache.import_object(
            item, "pack_a.zip", self.source(zip_bytes("other")), False, self.root, args_for(), None, False,
            expected_sha256=self.sha)
        self.assertEqual((status, document), ("pin-mismatch", None))
        self.assertNotEqual(sha, self.sha)
        self.assertEqual(list((self.root / "objects").iterdir()), [])

    def test_import_object_match_records_a_pinned_object(self):
        item = item_for(NEXUS_URL)
        status, document, _, _ = cache.import_object(
            item, "pack_a.zip", self.source(), False, self.root, args_for(), None, False, expected_sha256=self.sha)
        self.assertEqual(status, "imported")
        self.assertEqual((document["status"], document["evidence"], document["metadata_unknown"]),
                         ("pinned", "operator-seed", True))
        self.assertTrue((self.root / "objects" / self.sha).is_file())

    def test_import_object_without_expectation_is_unchanged(self):
        status, document, _, _ = cache.import_object(
            item_for(NEXUS_URL), "pack_a.zip", self.source(), False, self.root, args_for(), None, False)
        self.assertEqual((status, document["status"], document["evidence"]),
                         ("imported", "stale-unknown", "local-import"))

    def index_for(self, item):
        return {"entry": {"HandlerMetadata": {"PrimaryUrl": item["url"]}, "Files": {"pack_a.zip": {}}}}

    def test_import_existing_mismatch_reports_and_does_not_write(self):
        item = item_for(NEXUS_URL)
        archives = self.base / "archives"
        archives.mkdir()
        (archives / "pack_a.zip").write_bytes(zip_bytes("other"))
        manifest = {"schema": 1, "entries": {}}
        args = args_for(import_manifest=manifest["entries"], import_manifest_document=manifest)
        entry = cache.import_existing(item, self.index_for(item), archives, self.root, args, self.sha)
        self.assertEqual(entry["status"], "pin-mismatch")
        self.assertEqual(manifest["entries"], {})
        self.assertFalse((self.root / "manifest.json").exists())
        self.assertEqual(list((self.root / "objects").iterdir()), [])

    def test_import_existing_match_seeds_a_pinned_entry(self):
        item = item_for(NEXUS_URL)
        archives = self.base / "archives"
        archives.mkdir()
        (archives / "pack_a.zip").write_bytes(self.payload)
        manifest = {"schema": 1, "entries": {}}
        args = args_for(import_manifest=manifest["entries"], import_manifest_document=manifest)
        entry = cache.import_existing(item, self.index_for(item), archives, self.root, args, {"pack_a.zip": self.sha})
        self.assertEqual(entry["status"], "pinned")
        stored = self.read_entry(item)
        self.assertEqual((stored["status"], stored["evidence"], stored["metadata_unknown"]),
                         ("pinned", "operator-seed", True))
        self.write_pins()
        self.assertTrue(cache.is_pinned(item, stored))


class PinnedRestoreAndRefreshTests(PinFixture):
    def restore(self, item):
        destination = self.base / "mods"
        return self.run_main(item, "restore", "--destination", str(destination)), destination

    def test_pinned_entry_restores_after_thirty_days_without_upstream_check(self):
        item = self.write_pins()
        self.write_manifest(item, self.stored_entry(checked_at=cache.time.time() - 30 * DAY))
        with mock.patch.object(cache, "refresh_one", side_effect=AssertionError("no upstream check")):
            (code, report), destination = self.restore(item)
        self.assertEqual(code, 0)
        self.assertEqual(report["entries"][0]["status"], "restored")
        self.assertTrue(report["entries"][0]["metadata_unknown"])
        self.assertEqual((destination / "pack_a.zip").read_bytes(), self.payload)

    def test_pinned_gamefront_entry_restores_without_checked_at(self):
        item = self.write_pins(url=GAMEFRONT_URL)
        self.write_manifest(item, self.stored_entry())
        with mock.patch.object(cache, "refresh_one", side_effect=AssertionError("no upstream check")):
            (code, report), _ = self.restore(item)
        self.assertEqual((code, report["entries"][0]["status"]), (0, "restored"))

    def test_stale_non_pinned_entry_still_needs_refresh(self):
        item = item_for(NEXUS_URL)
        self.write_manifest(item, self.stored_entry(status="verified", evidence="download-without-upstream-metadata",
                                                    checked_at=cache.time.time() - 30 * DAY))
        with mock.patch.object(cache, "refresh_one", side_effect=ValueError(cache.NEXUS_REASON)) as refresh:
            (code, report), _ = self.restore(item)
        refresh.assert_called_once()
        self.assertEqual((code, report["entries"][0]["status"]), (0, "restored"))

    def test_stale_non_pinned_entry_with_a_missing_object_is_unresolved(self):
        item = item_for(NEXUS_URL)
        self.write_manifest(item, self.stored_entry(status="verified", evidence="download-without-upstream-metadata",
                                                    checked_at=cache.time.time() - 30 * DAY, store=False))
        with mock.patch.object(cache, "refresh_one", side_effect=ValueError(cache.NEXUS_REASON)) as refresh:
            (code, report), _ = self.restore(item)
        refresh.assert_called_once()
        self.assertEqual((code, report["entries"][0]["status"]), (1, "unresolved"))

    def test_pinned_status_without_a_matching_pin_gets_old_behavior(self):
        item = item_for(NEXUS_URL)
        self.write_manifest(item, self.stored_entry(checked_at=cache.time.time() - 30 * DAY))
        for pin_setup in (lambda: None, lambda: self.write_pins(sha="3" * 64),
                          lambda: self.write_pins(url=DEADLY_URL, provider="deadlystream")):
            pin_setup()
            with mock.patch.object(cache, "refresh_one", side_effect=ValueError(cache.NEXUS_REASON)) as refresh:
                (code, report), _ = self.restore(item)
            refresh.assert_called_once()
            self.assertEqual(report["entries"][0]["status"], "restored")

    def test_pinned_entry_for_unsupported_provider_gets_old_behavior(self):
        item = self.write_pins(url=DEADLY_URL, provider="deadlystream")
        self.write_manifest(item, self.stored_entry(checked_at=cache.time.time() - 30 * DAY))
        with mock.patch.object(cache, "refresh_one", side_effect=ValueError("HTTP 500")) as refresh:
            (_, report), _ = self.restore(item)
        refresh.assert_called_once()
        self.assertEqual(report["entries"][0]["status"], "restored")

    def test_refresh_keeps_pinned_entry_without_fetching(self):
        item = self.write_pins()
        self.write_manifest(item, self.stored_entry(checked_at=cache.time.time() - 30 * DAY))
        with mock.patch.object(cache, "refresh_one", side_effect=AssertionError("no upstream check")):
            code, report = self.run_main(item, "refresh", "--force")
        self.assertEqual((code, report["entries"][0]["status"]), (0, "pinned"))
        self.assertEqual(self.read_entry(item)["status"], "pinned")

    def test_failed_refresh_never_downgrades_a_pinned_entry(self):
        item = self.write_pins()
        self.write_manifest(item, self.stored_entry(store=False))
        with mock.patch.object(cache, "refresh_one", side_effect=ValueError(cache.NEXUS_REASON)):
            code, report = self.run_main(item, "refresh")
        self.assertEqual((code, report["entries"][0]["status"]), (1, "unresolved"))
        self.assertEqual(self.read_entry(item)["status"], "pinned")

    def test_failed_refresh_still_downgrades_a_non_pinned_entry(self):
        item = item_for(NEXUS_URL)
        self.write_manifest(item, self.stored_entry(status="verified", evidence="download-without-upstream-metadata",
                                                    checked_at=cache.time.time() - 30 * DAY, store=False))
        with mock.patch.object(cache, "refresh_one", side_effect=ValueError(cache.NEXUS_REASON)):
            self.run_main(item, "refresh")
        self.assertEqual(self.read_entry(item)["status"], "stale-unknown")

    def test_is_due_skips_pinned_entries_only(self):
        item = self.write_pins()
        now = cache.time.time()
        pinned = self.stored_entry(checked_at=now - 30 * DAY)
        self.assertFalse(refresh_archives.is_due(pinned, now, item))
        self.assertTrue(refresh_archives.is_due(pinned, now))
        self.assertFalse(refresh_archives.is_due(pinned, now, item, cache.load_pins()))
        stale = self.stored_entry(status="verified", evidence="download-without-upstream-metadata",
                                  checked_at=now - 30 * DAY)
        self.assertTrue(refresh_archives.is_due(stale, now, item))
        pinned["sha256"] = "4" * 64
        self.assertTrue(refresh_archives.is_due(pinned, now, item))


if __name__ == "__main__":
    unittest.main()
