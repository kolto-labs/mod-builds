"""Public guide inventory contracts, with no acquisition or provider credentials."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location("guide_inventory", Path(__file__).resolve().parents[1] / "src" / "guide_inventory.py")
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


class InventoryTests(unittest.TestCase):
    def inventory(self, text):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        source = Path(temp.name) / "full.md"
        source.write_bytes(text.encode("utf-8"))
        return module.build_inventory(source, game="k2")

    def reconcile(self, inv, catalog):
        raw = json.dumps(catalog, sort_keys=True).encode()
        return module.reconcile_catalog(inv, catalog, catalog_bytes=raw, expected_generation_digest=module.digest(raw))

    def test_preserves_entire_exact_guide_and_all_resource_owners(self):
        text = "# Build\r\n## Setup\r\n[Tool](https://example.com/tool.zip)\r\n## CRITICAL: Version Fixes\r\n### Aspyr Fix\r\n**Name:** [Fix](https://deadlystream.com/files/file/123-fix/) and [**Patch**](https://mega.nz/file/abc#public_key)\r\n:::note\r\nInstallation Instructions\r\n:   Use [extra](https://example.com/extra.7z) and [Patch again](https://mega.nz/file/abc#public_key).\r\n:::\r\n## Mod List\r\n### Main\r\n**Name:** [Main](https://www.nexusmods.com/kotor2/mods/321)\r\n## Optional Widescreen Mods\r\n### Wide\r\n**Name:** [Wide](https://example.com/wide.zip)\r\n## Epilogue\r\nKeep original notes.\r\n"
        inv = self.inventory(text)
        self.assertEqual(module.manual_checklist(inv), text)
        self.assertEqual(inv["source"]["sha256"], module.digest(text))
        self.assertEqual(len(inv["resources"]), 6)
        patch = next(resource for resource in inv["resources"] if resource["provider"] == "mega")
        self.assertEqual(patch["url"], "https://mega.nz/file/abc#public_key")
        self.assertEqual(len(patch["owners"]), 2)
        self.assertTrue(patch["owners"][0]["aspyr_variant"])
        wide = next(section for section in inv["sections"] if section["heading"] == "Wide")
        self.assertTrue(wide["optional_variant"])
        self.assertEqual(inv["coverage"]["candidate_count"], 6)
        self.assertFalse(inv["coverage"]["complete"])
        self.assertTrue(all(resource["status"] == "unresolved" for resource in inv["resources"]))
        self.assertEqual(inv["sections"][0]["heading"], "Build")

    def test_page_classification_never_claims_payload(self):
        expected = [("https://www.nexusmods.com/kotor/mods/42", "nexus", "mod_page"),
                    ("https://deadlystream.com/files/file/123-a/", "deadlystream", "mod_page"),
                    ("https://mega.nz/folder/abc#key", "mega", "folder_reference"),
                    ("https://example.com/mod.zip", "direct", "payload_candidate"),
                    ("https://deadlystream.com/uploads/monthly_2025/a.zip", "deadlystream", "payload_candidate"),
                    ("https://cf-files.nexusmods.com/a.7z", "nexus", "payload_candidate"),
                    ("https://www.gog.com/forum/game/post1", "forum", "forum_page"),
                    ("https://example.com/article", "info", "informational_page"),
                    ("#section", "internal", "internal_reference")]
        for url, provider, kind in expected:
            with self.subTest(url=url):
                result = module.classify_url(url)
                self.assertEqual((result["provider"], result["kind"]), (provider, kind))

    def test_primary_name_website_and_instruction_forum_are_unresolved_candidates(self):
        inv = self.inventory("# Guide\n## Mod List\n### Patcher\n**Name:** [4GB Patcher](https://ntcore.com/?page_id=371)\nInstall it from [this thread](https://www.gog.com/forum/game/post58).\n[Background](https://example.com/article)\n")
        self.assertEqual(inv["coverage"]["candidate_count"], 2)
        self.assertEqual([resource["download_candidate"] for resource in inv["resources"]], [True, True, False])
        self.assertFalse(inv["coverage"]["complete"])

    def test_reference_and_balanced_urls_preserve_exact_identities(self):
        inv = self.inventory("# Guide\n[one](https://example.com/a_(one).zip) [internal](../k1/full.md#mods)\nhttps://mega.nz/#!abc!key\n")
        self.assertEqual([resource["url"] for resource in inv["resources"]], ["https://example.com/a_(one).zip", "../k1/full.md#mods", "https://mega.nz/#!abc!key"])
        self.assertEqual(inv["resources"][1]["provider"], "internal")

    def test_does_not_split_fenced_or_commented_heading_text(self):
        text = "# Build\n```md\n### Not a heading\n```\n<!--\n### Not a heading either\n-->\n## Mod List\n"
        inv = self.inventory(text)
        self.assertEqual([section["heading"] for section in inv["sections"]], ["Build", "Mod List"])
        self.assertEqual(module.manual_checklist(inv), text)

    def test_requires_every_optional_and_patch_payload_to_be_pinned(self):
        inv = self.inventory("# Guide\n## Optional\n### Mod\n**Name:** [Main](https://example.com/main.zip) and [Patch](https://mega.nz/file/a#key)\n")
        source = {"id": inv["resources"][0]["id"], "url": "https://payload.example.com/main.zip", "filename": "main.zip", "sha256": "a" * 64, "size": 5}
        result = self.reconcile(inv, {"generation": "immutable", "sources": [source]})
        self.assertFalse(result["coverage"]["complete"])
        self.assertEqual(result["coverage"]["percent"], 50.0)
        patch = {"id": inv["resources"][1]["id"], "url": "https://payload.example.com/patch.zip", "filename": "patch.zip", "sha256": "b" * 64, "size": 7}
        result = self.reconcile(inv, {"generation": "immutable", "sources": [source, patch]})
        self.assertTrue(result["coverage"]["complete"])
        self.assertEqual(result["coverage"]["percent"], 100.0)
        self.assertFalse(inv["coverage"]["complete"])

    def test_does_not_claim_complete_without_verified_immutable_manifest(self):
        inv = self.inventory("# Guide\n[archive](https://example.com/main.zip)\n")
        catalog = {"generation": "opaque", "sources": [{"id": inv["resources"][0]["id"], "url": "https://payload.example.com/main.zip", "filename": "main.zip", "sha256": "a" * 64, "size": 5}]}
        self.assertFalse(module.reconcile_catalog(inv, catalog)["coverage"]["complete"])
        with self.assertRaises(ValueError):
            module.reconcile_catalog(inv, catalog, catalog_bytes=json.dumps(catalog).encode(), expected_generation_digest="0" * 64)
        self.assertTrue(self.reconcile(inv, catalog)["coverage"]["complete"])
        catalog["sources"][0]["filename"] = "CON.zip"
        self.assertFalse(self.reconcile(inv, catalog)["coverage"]["complete"])

    def test_rejects_provider_page_as_resolved_payload_and_duplicate_ids(self):
        inv = self.inventory("# Guide\n[mod](https://www.nexusmods.com/kotor/mods/42)\n")
        source = {"id": inv["resources"][0]["id"], "url": inv["resources"][0]["url"], "filename": "invented.zip", "sha256": "a" * 64, "size": 5}
        result = self.reconcile(inv, {"generation": "immutable", "sources": [source]})
        self.assertFalse(result["coverage"]["complete"])
        with self.assertRaises(ValueError):
            module.reconcile_catalog(inv, {"sources": [source, source]})

    def test_combines_guides_without_duplicate_cache_ids_or_lost_owners(self):
        first = self.inventory("# K1\n## Mod List\n### Shared\n**Name:** [Shared](https://example.com/shared.zip)\n")
        second = self.inventory("# K2\n## Optional\n### Shared\n**Name:** [Shared](https://example.com/shared.zip) [Patch](https://example.com/patch.zip)\n")
        combined = module.combine_inventories([first, second])
        self.assertEqual(len(combined["resources"]), 2)
        self.assertEqual(len(combined["resources"][0]["owners"]), 2)
        self.assertEqual(combined["coverage"]["candidate_count"], 2)
        ids = [section["id"] for section in combined["sections"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(module.manual_checklist(combined), module.manual_checklist(first) + module.manual_checklist(second))
        self.assertEqual(combined["resources"][0]["id"], first["resources"][0]["id"])
        self.assertTrue(all(resource["required_for_complete"] for resource in combined["resources"]))
        catalog = {"generation": "shared-immutable", "sources": [
            {"id": resource["id"], "url": resource["url"], "filename": f"payload-{i}.zip", "sha256": str(i + 1) * 64, "size": 5}
            for i, resource in enumerate(combined["resources"])]}
        for inventory in (first, second, combined):
            covered = self.reconcile(inventory, catalog)
            self.assertTrue(covered["coverage"]["complete"])
            self.assertEqual(covered["coverage"]["percent"], 100.0)
            self.assertEqual(covered["coverage"]["generation"], "shared-immutable")
        digests = [self.reconcile(inventory, catalog)["coverage"]["generation_digest"] for inventory in (first, second, combined)]
        self.assertEqual(len(set(digests)), 1)

    def test_actual_public_guides_all_named_and_platform_sections_included(self):
        root = Path(__file__).resolve().parents[3]
        for game in ("k1", "k2"):
            source = root / "content" / game / "full.md"
            inv = module.build_inventory(source, game=game)
            self.assertEqual(module.manual_checklist(inv).encode("utf-8"), source.read_bytes())
            self.assertGreater(sum(section["named_mod"] for section in inv["sections"]), 140)
            self.assertTrue(any(section["optional_variant"] for section in inv["sections"]))
            self.assertTrue(any(resource["provider"] == "mega" for resource in inv["resources"]))
            self.assertTrue(any(section["heading"] == "Zeroing Step" for section in inv["sections"]))
            if game == "k2":
                aspyr = [section for section in inv["sections"] if section["named_mod"] and section["aspyr_variant"]]
                self.assertEqual([section["heading"] for section in aspyr], ["3C-FD Patcher", "Water Restoration", "Stutter Fix"])
            self.assertFalse(inv["coverage"]["complete"])


if __name__ == "__main__":
    unittest.main()
