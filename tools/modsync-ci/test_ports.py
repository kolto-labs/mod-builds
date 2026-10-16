import argparse
import hashlib
from pathlib import Path
import tempfile
import tomllib
import unittest
from unittest import mock

import ports

GUIDE = """# Guide

## CRITICAL: Platform Notes

### Platform Fix A

## Mod List

### Mod One

### Mod Two

## Misc. Basegame Issues & Fixes

### Ignored Entry
"""


def component(name, guid, **extra):
    lines = [
        "[[thisMod]]",
        'Guid = "' + guid + '"',
        'Name = "' + name + '"',
        "IsSelected = false",
    ]
    lines += [key + " = " + value for key, value in extra.items()]
    lines += [
        "[[thisMod.Instructions]]",
        'Guid = "' + guid[:-1] + '9"',
        'Action = "Move"',
        "",
    ]
    return "\n".join(lines)


def document(*blocks):
    return "[Config]\nTargetGame = \"KOTOR2\"\n\n" + "\n".join(blocks)


class CoverageTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.directory))
        self.guide = self.directory / "full.md"
        self.guide.write_text(GUIDE, encoding="utf-8")

    def converted(self, *names):
        path = self.directory / "converted.toml"
        path.write_text(document(*[
            component(name, "00000000-0000-0000-0000-00000000000" + str(index))
            for index, name in enumerate(names)
        ]), encoding="utf-8")
        return path

    def test_extra_section_allowed_for_typescript_only(self):
        converted = self.converted("Platform Fix A", "Mod One", "Mod Two")
        guide = self.guide.read_text().replace(
            "CRITICAL: Platform Notes", "CRITICAL: PC Version Differences & Fixes")
        self.guide.write_text(guide, encoding="utf-8")
        self.assertEqual(ports.coverage(self.guide, converted, {}, "typescript"), 3)
        with self.assertRaisesRegex(ports.Blocked, "guide_component_coverage_mismatch"):
            ports.coverage(self.guide, converted, {}, "rust")

    def test_unlisted_section_is_never_allowed(self):
        converted = self.converted("Platform Fix A", "Mod One", "Mod Two")
        with self.assertRaisesRegex(ports.Blocked, "guide_component_coverage_mismatch"):
            ports.coverage(self.guide, converted, {}, "typescript")

    def test_typescript_extra_section_must_keep_guide_order(self):
        guide = self.guide.read_text().replace(
            "CRITICAL: Platform Notes", "CRITICAL: PC Version Differences & Fixes")
        self.guide.write_text(guide, encoding="utf-8")
        converted = self.converted("Mod One", "Mod Two", "Platform Fix A")
        with self.assertRaisesRegex(ports.Blocked, "order_or_heading_mismatch"):
            ports.coverage(self.guide, converted, {}, "typescript")

    def test_unknown_implementation_rejected(self):
        converted = self.converted("Mod One", "Mod Two")
        with self.assertRaisesRegex(ports.Blocked, "implementation_unknown"):
            ports.coverage(self.guide, converted, {}, "other")


class SelectionTests(unittest.TestCase):
    def write(self, text):
        handle = tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False, encoding="utf-8")
        self.addCleanup(lambda: Path(handle.name).unlink(missing_ok=True))
        handle.write(text)
        handle.close()
        return Path(handle.name)

    def test_selects_base_components_and_leaves_everything_else_untouched(self):
        text = document(
            component("Alpha", "10000000-0000-0000-0000-000000000001"),
            component("Beta", "10000000-0000-0000-0000-000000000002", IsWidescreen="true"),
            component("Gamma", "10000000-0000-0000-0000-000000000003", AspyrOnly="true"),
            component("Delta", "10000000-0000-0000-0000-000000000004")
            + "\n[[thisMod.Options]]\nGuid = \"10000000-0000-0000-0000-0000000000a1\"\n"
              "Name = \"Opt\"\nIsSelected = false\n",
        )
        path = self.write(text)
        self.assertEqual(ports.select_base_components(path), 2)
        result = tomllib.loads(path.read_text(encoding="utf-8"))
        self.assertEqual([c["IsSelected"] for c in result["thisMod"]],
                         [True, False, False, True])
        self.assertIs(result["thisMod"][3]["Options"][0]["IsSelected"], False)
        original = tomllib.loads(text)
        for part in (original, result):
            for item in part["thisMod"]:
                item.pop("IsSelected")
        self.assertEqual(original, result)

    def test_missing_selection_key_is_blocked(self):
        path = self.write('[Config]\nA = 1\n\n[[thisMod]]\nGuid = "x"\nName = "n"\n')
        with self.assertRaisesRegex(ports.Blocked, "selection_rewrite_incomplete"):
            ports.select_base_components(path)


class ExecuteCommandTests(unittest.TestCase):
    def prepare(self, game_folder):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(root))
        content = root / "content" / game_folder
        content.mkdir(parents=True)
        guide = content / "full.md"
        guide.write_text(GUIDE, encoding="utf-8")
        for name in ("source", "game", "archives"):
            (root / name).mkdir()
        (root / "source" / "Cargo.toml").write_text("", encoding="utf-8")
        (root / "source" / "packages" / "cli" / "src").mkdir(parents=True)
        (root / "source" / "packages" / "cli" / "src" / "main.ts").write_text("", encoding="utf-8")
        args = argparse.Namespace(
            implementation=None, mode="cli", review_manifest=None, source=root / "source",
            guide=guide, game_dir=root / "game", archives=root / "archives",
            work_dir=root / "work", report=root / "report.json")
        return root, args

    def captured(self, args, converted_text):
        commands = []

        def fake_run(command, source, private, stage, report, env):
            commands.append((stage, list(command)))
            if stage == "convert":
                (private / "converted.toml").write_text(converted_text, encoding="utf-8")

        report = {"commands": []}
        with mock.patch.object(ports, "run", fake_run):
            with self.assertRaises(ports.Blocked) as caught:
                ports.execute(args, report)
        return commands, report, str(caught.exception)

    def test_typescript_import_uses_stable_source_url_and_hashes_selected_toml(self):
        _, args = self.prepare("k2")
        args.implementation = "typescript"
        text = document(
            component("Mod One", "20000000-0000-0000-0000-000000000001"),
            component("Mod Two", "20000000-0000-0000-0000-000000000002"),
        )
        guide_text = GUIDE.replace("## CRITICAL: Platform Notes\n\n### Platform Fix A\n\n", "")
        args.guide.write_text(guide_text, encoding="utf-8")
        commands, report, reason = self.captured(args, text)
        convert = dict(commands)["convert"]
        index = convert.index("--source-url")
        self.assertEqual(convert[index + 1], "content/k2/full.md")
        self.assertEqual(reason, "review_manifest_required")
        self.assertEqual(report["selected_component_count"], 2)
        converted = next(Path(args.work_dir).glob("ports-*/converted.toml"))
        self.assertEqual(report["converted_sha256"],
                         hashlib.sha256(converted.read_bytes()).hexdigest())
        loaded = tomllib.loads(converted.read_text(encoding="utf-8"))
        self.assertTrue(all(item["IsSelected"] for item in loaded["thisMod"]))

    def test_rust_convert_infers_local_instructions_from_archives(self):
        root, args = self.prepare("k1")
        args.implementation = "rust"
        text = document(component("Mod One", "30000000-0000-0000-0000-000000000001"))
        commands, _, _ = self.captured(args, text)
        convert = dict(commands)["convert"]
        self.assertIn("--auto-generate-local", convert)
        self.assertEqual(convert[convert.index("--source-path") + 1],
                         str((root / "archives").resolve()))
        self.assertIn("--parse-directions", convert)
        for flag in ("--review-draft", "--best-effort", "--ignore-errors"):
            self.assertNotIn(flag, convert)


if __name__ == "__main__":
    unittest.main()
