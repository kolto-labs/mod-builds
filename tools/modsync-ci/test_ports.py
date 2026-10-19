import argparse
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import tomllib
import unittest
from unittest import mock

import deselect_unreviewed
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


def reviewing(fake_run):
    """Wrap a fake `ports.run` so the draft-review convert copies its input to its output."""
    def wrapped(command, source, private, stage, report, env):
        fake_run(command, source, private, stage, report, env)
        if stage == ports.DRAFT_REVIEW_STAGE:
            Path(command[3]).write_bytes(Path(command[2]).read_bytes())
    return wrapped


GUIDE_WITH_HELPER = """# Guide

## Optional Helper Section

### Helper Step

## Mod List

### Mod One

### Mod Two

## Optional Widescreen & Extended Widescreen Support Mods

### Loose Movies
"""

HELPER = ("Optional Helper Section", "Helper Step")
MOVIES = ("Optional Widescreen & Extended Widescreen Support Mods", "Loose Movies")
MOVIES_GUID = "40000000-0000-0000-0000-000000000003"


def skipped_step(guide, key, url="https://example.invalid/mods/1"):
    return {"guide": str(guide), "section": key[0], "heading": key[1], "url": url,
            "reason": "intentionally not cached by policy: test"}


def restore_report(path, guide, *keys, ok=True):
    path.write_text(json.dumps({
        "ok": ok, "entries": [],
        "skipped_steps": [skipped_step(guide, key) for key in keys]}), encoding="utf-8")
    return path


def helper_document(movies_instructions=False, dependents=False):
    blocks = [
        component("Mod One", "40000000-0000-0000-0000-000000000001"),
        component("Mod Two", "40000000-0000-0000-0000-000000000002"),
    ]
    movies = "\n".join([
        "[[thisMod]]", 'Guid = "' + MOVIES_GUID + '"', 'Name = "Loose Movies"',
        "IsSelected = true", "WidescreenOnly = true",
    ] + (["[[thisMod.Instructions]]", 'Guid = "40000000-0000-0000-0000-0000000000f9"',
          'Action = "Move"'] if movies_instructions else []))
    blocks.append(movies)
    if dependents:
        blocks[0] = blocks[0].replace(
            "IsSelected = false", 'IsSelected = false\nDependencies = ["' + MOVIES_GUID + '"]')
    return document(*blocks)


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


class PolicySkipCoverageTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.directory))
        self.guide = (self.directory / "full.md")
        self.guide.write_text(GUIDE_WITH_HELPER, encoding="utf-8")
        self.converted = self.directory / "converted.toml"
        self.converted.write_text(helper_document(), encoding="utf-8")
        self.report_path = self.directory / "restore.json"

    def skips(self, *keys, ok=True):
        restore_report(self.report_path, self.guide.resolve(), *keys, ok=ok)
        return ports.load_policy_skips(self.report_path, self.guide.resolve())

    def test_without_skip_the_component_without_instructions_blocks(self):
        with self.assertRaisesRegex(ports.Blocked, "guide_steps_without_instructions"):
            ports.coverage(self.guide, self.converted, {})

    def test_skipped_component_is_exempt_recorded_and_identified(self):
        report = {}
        count = ports.coverage(self.guide, self.converted, report, "rust",
                               self.skips(HELPER, MOVIES))
        self.assertEqual(count, 3)
        by_heading = {record["heading"]: record for record in report["skipped_by_reviewed_policy"]}
        self.assertEqual(by_heading["Loose Movies"]["status"], "skipped-by-reviewed-policy")
        self.assertEqual(by_heading["Loose Movies"]["component_guid"], MOVIES_GUID)
        self.assertIn("intentionally not cached by policy", by_heading["Loose Movies"]["reason"])
        self.assertIsNone(by_heading["Helper Step"]["component_guid"])

    def test_only_the_named_step_is_exempt(self):
        self.converted.write_text(document(
            component("Mod One", "40000000-0000-0000-0000-000000000001"),
            "[[thisMod]]\nGuid = \"40000000-0000-0000-0000-000000000002\"\nName = \"Mod Two\"\nIsSelected = true\n",
            "[[thisMod]]\nGuid = \"" + MOVIES_GUID + "\"\nName = \"Loose Movies\"\nIsSelected = true\n",
        ), encoding="utf-8")
        with self.assertRaisesRegex(ports.Blocked, "guide_steps_without_instructions"):
            ports.coverage(self.guide, self.converted, {}, "rust", self.skips(MOVIES))

    def test_step_missing_from_guide_blocks(self):
        with self.assertRaisesRegex(ports.Blocked, "policy_skipped_step_not_in_guide"):
            ports.coverage(self.guide, self.converted, {}, "rust",
                           self.skips(("Optional Helper Section", "Nonexistent")))

    def test_component_with_dependents_blocks(self):
        self.converted.write_text(helper_document(dependents=True), encoding="utf-8")
        with self.assertRaisesRegex(ports.Blocked, "policy_skipped_component_has_dependents"):
            ports.coverage(self.guide, self.converted, {}, "rust", self.skips(MOVIES))

    def test_report_must_be_ok_and_for_this_guide(self):
        with self.assertRaisesRegex(ports.Blocked, "restore_report_not_ok"):
            self.skips(MOVIES, ok=False)
        restore_report(self.report_path, self.directory / "other" / "full.md", MOVIES)
        with self.assertRaisesRegex(ports.Blocked, "restore_report_guide_mismatch"):
            ports.load_policy_skips(self.report_path, self.guide.resolve())

    def test_urls_for_one_step_merge(self):
        steps = [skipped_step(self.guide.resolve(), MOVIES, "https://example.invalid/a"),
                 skipped_step(self.guide.resolve(), MOVIES, "https://example.invalid/b")]
        self.report_path.write_text(json.dumps({"ok": True, "skipped_steps": steps}), encoding="utf-8")
        records = ports.load_policy_skips(self.report_path, self.guide.resolve())
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["urls"], ["https://example.invalid/a", "https://example.invalid/b"])


GUIDE_WITH_ALTERNATIVES = """# Guide

## Mod List

### Mod One

## Enhanced Widescreen Support Mods

### Cutscenes Pack A

### Cutscenes Pack B

### Loading Screens
"""

PACK_A = ("Enhanced Widescreen Support Mods", "Cutscenes Pack A")
PACK_A_GUID = "50000000-0000-0000-0000-000000000002"
PACK_B_GUID = "50000000-0000-0000-0000-000000000003"


def alternatives_document(pack_b_instructions=True, pack_a_extra=""):
    pack_a = "\n".join([
        "[[thisMod]]", 'Guid = "' + PACK_A_GUID + '"', 'Name = "Cutscenes Pack A"',
        "IsSelected = true", "WidescreenOnly = true", pack_a_extra,
        "[[thisMod.Options]]", 'Guid = "50000000-0000-0000-0000-0000000000a1"',
        'Name = "Option 1"', "IsSelected = false",
    ])
    pack_b = component("Cutscenes Pack B", PACK_B_GUID, WidescreenOnly="true")
    if not pack_b_instructions:
        pack_b = pack_b.split("[[thisMod.Instructions]]")[0]
    return document(
        component("Mod One", "50000000-0000-0000-0000-000000000001"),
        pack_a, pack_b,
        component("Loading Screens", "50000000-0000-0000-0000-000000000004", WidescreenOnly="true"),
    )


class AlternativeCutsceneSkipTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.directory))
        self.guide = self.directory / "full.md"
        self.guide.write_text(GUIDE_WITH_ALTERNATIVES, encoding="utf-8")
        self.converted = self.directory / "converted.toml"
        self.report_path = self.directory / "restore.json"

    def skips(self):
        restore_report(self.report_path, self.guide.resolve(), PACK_A)
        return ports.load_policy_skips(self.report_path, self.guide.resolve())

    def test_only_the_skipped_pack_is_exempt_and_its_alternative_still_needs_instructions(self):
        self.converted.write_text(alternatives_document(), encoding="utf-8")
        report = {}
        self.assertEqual(ports.coverage(self.guide, self.converted, report, "rust", self.skips()), 4)
        self.assertEqual([(record["heading"], record["component_guid"])
                          for record in report["skipped_by_reviewed_policy"]],
                         [("Cutscenes Pack A", PACK_A_GUID)])
        self.converted.write_text(alternatives_document(pack_b_instructions=False), encoding="utf-8")
        with self.assertRaisesRegex(ports.Blocked, "guide_steps_without_instructions"):
            ports.coverage(self.guide, self.converted, {}, "rust", self.skips())

    def test_skipped_pack_without_the_reviewed_flag_still_blocks(self):
        self.converted.write_text(alternatives_document(), encoding="utf-8")
        with self.assertRaisesRegex(ports.Blocked, "guide_steps_without_instructions"):
            ports.coverage(self.guide, self.converted, {}, "rust", [])

    def test_a_component_listing_the_skipped_pack_as_a_dependency_blocks(self):
        document_text = alternatives_document().replace(
            'Name = "Loading Screens"',
            'Name = "Loading Screens"\nDependencies = ["' + PACK_A_GUID.upper() + '"]')
        self.converted.write_text(document_text, encoding="utf-8")
        with self.assertRaisesRegex(ports.Blocked, "policy_skipped_component_has_dependents"):
            ports.coverage(self.guide, self.converted, {}, "rust", self.skips())

    def test_a_pack_heading_that_appears_twice_blocks(self):
        self.guide.write_text(GUIDE_WITH_ALTERNATIVES.replace(
            "### Loading Screens", "### Cutscenes Pack A"), encoding="utf-8")
        self.converted.write_text(alternatives_document().replace(
            "Loading Screens", "Cutscenes Pack A"), encoding="utf-8")
        with self.assertRaisesRegex(ports.Blocked, "policy_skipped_step_ambiguous"):
            ports.coverage(self.guide, self.converted, {}, "rust", self.skips())

    def test_rust_cli_deselects_only_the_skipped_pack(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(root))
        content = root / "content" / "k2"
        content.mkdir(parents=True)
        guide = content / "full.md"
        guide.write_text(GUIDE_WITH_ALTERNATIVES, encoding="utf-8")
        for name in ("source", "game", "archives"):
            (root / name).mkdir()
        patch = mock.patch.object(ports, "LANE_SKIPS_PATH", root / "no-lane-skips.json")
        patch.start()
        self.addCleanup(patch.stop)
        (root / "source" / "Cargo.toml").write_text("", encoding="utf-8")
        args = argparse.Namespace(
            implementation="rust", mode="cli", review_manifest=None, archive_map=None,
            restore_report=restore_report(root / "restore.json", guide.resolve(), PACK_A),
            source=root / "source", guide=guide, game_dir=root / "game",
            archives=root / "archives", work_dir=root / "work", report=root / "report.json")
        commands = []

        def fake_run(command, source, private, stage, report, env):
            commands.append((stage, list(command)))
            if stage == "convert":
                (private / "converted.toml").write_text(alternatives_document(), encoding="utf-8")

        report = {"commands": []}
        with mock.patch.object(ports, "run", reviewing(fake_run)):
            ports.execute(args, report)
        staged = dict(commands)
        for stage in ("validate", "install"):
            command = staged[stage]
            self.assertEqual([value for flag, value in zip(command, command[1:]) if flag == "--deselect"],
                             [PACK_A_GUID])
            self.assertNotIn(PACK_B_GUID, command)
            self.assertNotIn("--ci-physical-install", command)
        self.assertEqual(report["status"], "installed")


class RealK2GuideCutsceneSkipTests(unittest.TestCase):
    GUIDE = Path(ports.__file__).resolve().parents[2] / "content" / "k2" / "full.md"
    STEP = ("Enhanced Widescreen Support Mods", "K2 Cutscenes Rescaled")
    ALTERNATIVE = "K2 Remastered Cutscenes"

    def converted_from_guide(self, directory, instructed):
        section = None
        blocks = []
        for line in self.GUIDE.read_text(encoding="utf-8").splitlines():
            if line.startswith("## "):
                section = line[3:].strip()
            elif line.startswith("### ") and section in (
                    "Mod List", "Optional Widescreen & Extended Widescreen Support Mods",
                    "Enhanced Widescreen Support Mods"):
                heading = line[4:].strip()
                guid = "60000000-0000-0000-0000-%012d" % len(blocks)
                if (section, heading) == self.STEP:
                    blocks.append("\n".join([
                        "[[thisMod]]", 'Guid = "' + guid + '"', 'Name = "' + heading + '"',
                        "IsSelected = true", "WidescreenOnly = true"]))
                    self.guid = guid
                elif heading == self.ALTERNATIVE and not instructed:
                    blocks.append("\n".join([
                        "[[thisMod]]", 'Guid = "' + guid + '"', 'Name = "' + heading + '"',
                        "IsSelected = true"]))
                else:
                    blocks.append(component(heading.replace('"', "'"), guid))
        path = directory / "converted.toml"
        path.write_text(document(*blocks), encoding="utf-8")
        return path, len(blocks)

    def test_the_k2_rescaled_cutscene_step_is_the_only_exempt_component(self):
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(directory))
        converted, count = self.converted_from_guide(directory, instructed=True)
        with self.assertRaisesRegex(ports.Blocked, "guide_steps_without_instructions"):
            ports.coverage(self.GUIDE, converted, {}, "rust", [])
        report_path = restore_report(directory / "restore.json", self.GUIDE, self.STEP)
        skips = ports.load_policy_skips(report_path, self.GUIDE)
        report = {}
        self.assertEqual(ports.coverage(self.GUIDE, converted, report, "rust", skips), count)
        self.assertEqual([(record["section"], record["heading"], record["component_guid"])
                          for record in report["skipped_by_reviewed_policy"]],
                         [(self.STEP[0], self.STEP[1], self.guid)])
        converted, _ = self.converted_from_guide(directory, instructed=False)
        with self.assertRaisesRegex(ports.Blocked, "guide_steps_without_instructions"):
            ports.coverage(self.GUIDE, converted, {}, "rust", skips)


CONVERT_REPORT = [
    {"ComponentGuid": "40000000-0000-0000-0000-000000000001", "ComponentName": "Mod One",
     "Success": True, "InstructionsGenerated": 2, "SkipReason": ""},
    {"ComponentGuid": "40000000-0000-0000-0000-000000000002", "ComponentName": "Mod Two",
     "Success": False, "InstructionsGenerated": 0,
     "SkipReason": "No matching archive found for 'mod-two'"},
    {"ComponentGuid": "40000000-0000-0000-0000-000000000003", "ComponentName": "Mod Three",
     "Success": False, "InstructionsGenerated": 0,
     "SkipReason": "Mapped archive 'three.zip' for link 'https://example.invalid/3' was not found in the source directory"},
    {"ComponentGuid": "40000000-0000-0000-0000-000000000004", "ComponentName": "Mod Four",
     "Success": False, "InstructionsGenerated": 0,
     "SkipReason": "No supported game files or patcher data found in mapped archive 'four.zip'"},
    {"ComponentGuid": "40000000-0000-0000-0000-000000000005", "ComponentName": "Mod Five",
     "Success": False, "InstructionsGenerated": 0,
     "SkipReason": "Archive patcher tree has no identifiable sibling installer executable"},
    {"ComponentGuid": "40000000-0000-0000-0000-000000000006", "ComponentName": "Mod Six",
     "Success": False, "InstructionsGenerated": 0, "SkipReason": "Component already has instructions"},
]


def convert_log(path, results=CONVERT_REPORT, tail="Error: unsupported unit type\n"):
    path.write_text("warning: noise\n" + json.dumps(results, indent=2) + "\n" + tail, encoding="utf-8")
    return path


class ConvertSkipReportTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.directory))

    def test_causes_are_bucketed_and_already_had_instructions_is_only_counted(self):
        report = {}
        ports.record_convert_skips(convert_log(self.directory / "convert.log"), report)
        skips = report["convert_skips"]
        self.assertEqual((skips["total"], skips["generated"]), (6, 1))
        self.assertEqual(skips["by_cause"], {
            "no_archive_matched": 1, "mapped_file_missing": 1, "reader_rejection": 1,
            "patcher_path_refusal": 1, "already_had_instructions": 1})
        self.assertEqual([item["name"] for item in skips["components"]],
                         ["Mod Two", "Mod Three", "Mod Four", "Mod Five"])
        self.assertEqual(ports.skip_cause("something new"), "other")

    def test_reader_wording_variants_are_reader_rejections_and_unknown_stays_other(self):
        for reason in ("Archive links and special files are forbidden",
                       "entry 'a.bin' has invalid UTF-8 name",
                       "non-utf8 entry name",
                       "stream did not contain valid UTF-8"):
            self.assertEqual(ports.skip_cause(reason), "reader_rejection", reason)
        self.assertEqual(ports.skip_cause("unrecognised wording"), "other")

    def test_reviewed_policy_components_get_their_own_bucket_and_summary(self):
        guide = self.directory / "full.md"
        guide.write_text(GUIDE_WITH_HELPER, encoding="utf-8")
        converted = self.directory / "converted.toml"
        converted.write_text(document(
            component("Mod One", "40000000-0000-0000-0000-000000000001"),
            "[[thisMod]]\nGuid = \"40000000-0000-0000-0000-000000000002\"\nName = \"Mod Two\"\n"
            "IsSelected = false\n",
            "[[thisMod]]\nGuid = \"" + MOVIES_GUID + "\"\nName = \"Loose Movies\"\n"
            "IsSelected = true\nWidescreenOnly = true\n"), encoding="utf-8")
        results = [
            {"ComponentGuid": "40000000-0000-0000-0000-000000000001", "ComponentName": "Mod One",
             "Success": True, "InstructionsGenerated": 1, "SkipReason": ""},
            {"ComponentGuid": MOVIES_GUID.upper(), "ComponentName": "Loose Movies",
             "Success": False, "InstructionsGenerated": 0,
             "SkipReason": "No matching archive found for 'loose-movies'"},
            {"ComponentGuid": "40000000-0000-0000-0000-000000000002", "ComponentName": "Mod Two",
             "Success": False, "InstructionsGenerated": 0,
             "SkipReason": "No matching archive found for 'mod-two'"},
        ]
        report = {}
        ports.record_convert_skips(convert_log(self.directory / "convert.log", results), report)
        report_path = restore_report(self.directory / "restore.json", guide.resolve(), MOVIES)
        skips = ports.load_policy_skips(report_path, guide.resolve())
        with self.assertRaisesRegex(ports.Blocked, "guide_steps_without_instructions"):
            ports.coverage(guide, converted, report, "rust", skips)
        self.assertEqual(report["convert_skips"]["by_cause"],
                         {"no_archive_matched": 1, "skipped_by_reviewed_policy": 1})
        self.assertEqual([item["cause"] for item in report["convert_skips"]["components"]],
                         ["skipped_by_reviewed_policy", "no_archive_matched"])
        self.assertEqual([step["heading"] for step in report["steps_without_instructions"]],
                         ["Mod Two"])
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            ports.print_diagnostics(report)
        text = stderr.getvalue()
        self.assertIn("not generated: no_archive_matched=1; reviewed-policy-skipped=1", text)
        self.assertIn("- Mod Two [40000000-0000-0000-0000-000000000002]", text)

    def test_missing_or_unparseable_log_records_nothing(self):
        report = {}
        ports.record_convert_skips(self.directory / "absent.log", report)
        (self.directory / "bad.log").write_text("[\n  {broken\n", encoding="utf-8")
        ports.record_convert_skips(self.directory / "bad.log", report)
        self.assertEqual(report, {})

    def test_uncovered_steps_carry_heading_guid_links_and_converter_reason(self):
        guide = self.directory / "full.md"
        guide.write_text(GUIDE, encoding="utf-8")
        converted = self.directory / "converted.toml"
        converted.write_text(document(
            component("Mod One", "40000000-0000-0000-0000-000000000001"),
            "[[thisMod]]\nGuid = \"40000000-0000-0000-0000-000000000002\"\nName = \"Mod Two\"\n"
            "[thisMod.ResourceRegistry.\"https://example.invalid/files/two/\".Files]\n"
            "[thisMod.ResourceRegistry.\"https://example.invalid/files/two-patch/#fragment-key\".Files]\n",
        ).replace("Platform Fix A", "Mod One"), encoding="utf-8")
        guide.write_text(GUIDE.replace("## CRITICAL: Platform Notes\n\n### Platform Fix A\n\n", ""),
                         encoding="utf-8")
        report = {}
        ports.record_convert_skips(convert_log(self.directory / "convert.log"), report)
        with self.assertRaisesRegex(ports.Blocked, "guide_steps_without_instructions"):
            ports.coverage(guide, converted, report)
        self.assertEqual(report["steps_without_instructions"], [{
            "heading": "Mod Two", "component_guid": "40000000-0000-0000-0000-000000000002",
            "urls": ["https://example.invalid/files/two/", "https://example.invalid/files/two-patch/"],
            "skip_reason": "No matching archive found for 'mod-two'"}])
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            ports.print_diagnostics(report)
        text = stderr.getvalue()
        self.assertIn("convert generated instructions for 1 of 6 components", text)
        self.assertIn("no_archive_matched=1", text)
        self.assertIn("- Mod Two [40000000-0000-0000-0000-000000000002] No matching archive found for 'mod-two'"
                      " <https://example.invalid/files/two/ https://example.invalid/files/two-patch/>", text)

    def test_skip_report_survives_a_failed_convert_and_reaches_the_lane_report(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(root))
        content = root / "content" / "k1"
        content.mkdir(parents=True)
        (content / "full.md").write_text(GUIDE, encoding="utf-8")
        for name in ("source", "game", "archives"):
            (root / name).mkdir()
        (root / "source" / "Cargo.toml").write_text("", encoding="utf-8")
        args = argparse.Namespace(
            implementation="rust", mode="cli", review_manifest=None, archive_map=None,
            restore_report=None, source=root / "source", guide=content / "full.md",
            game_dir=root / "game", archives=root / "archives", work_dir=root / "work",
            report=root / "report.json")

        def failing_convert(command, source, private, stage, report, env):
            if stage == "convert":
                convert_log(private / "convert.log")
                raise ports.Blocked("convert_failed")

        report = {"commands": []}
        with mock.patch.object(ports, "run", failing_convert):
            with self.assertRaisesRegex(ports.Blocked, "convert_failed"):
                ports.execute(args, report)
        self.assertEqual(report["convert_skips"]["by_cause"]["no_archive_matched"], 1)


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

    def test_deselect_names_are_unselected_in_the_destination_only(self):
        text = '[[thisMod]]\nGuid = "{AAAAAAAA-0000-0000-0000-000000000001}"\nName = "a"\nIsSelected = false\n\n' \
               '[[thisMod]]\nGuid = "{AAAAAAAA-0000-0000-0000-000000000002}"\nName = "b"\nIsSelected = false\n'
        path = self.write(text)
        destination = path.with_name(path.stem + "-policy.toml")
        self.addCleanup(destination.unlink, missing_ok=True)
        count = ports.select_base_components(
            path, {"aaaaaaaa-0000-0000-0000-000000000002"}, destination)
        self.assertEqual(count, 1)
        self.assertEqual(path.read_text(encoding="utf-8"), text)
        self.assertEqual([c["IsSelected"] for c in tomllib.loads(destination.read_text())["thisMod"]],
                         [True, False])
        self.assertEqual(ports.select_base_components(path), 2)

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
        patch = mock.patch.object(ports, "LANE_SKIPS_PATH", root / "no-lane-skips.json")
        patch.start()
        self.addCleanup(patch.stop)
        (root / "source" / "Cargo.toml").write_text("", encoding="utf-8")
        (root / "source" / "packages" / "cli" / "src").mkdir(parents=True)
        (root / "source" / "packages" / "cli" / "src" / "main.ts").write_text("", encoding="utf-8")
        args = argparse.Namespace(
            implementation=None, mode="cli", review_manifest=None, archive_map=None, restore_report=None, source=root / "source",
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
        with mock.patch.object(ports, "run", reviewing(fake_run)):
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

    def run_typescript_install(self, mode):
        _, args = self.prepare("k2")
        args.implementation = "typescript"
        args.mode = mode
        args.review_manifest = args.work_dir.parent / "review.json"
        args.guide.write_text(GUIDE.replace("## CRITICAL: Platform Notes\n\n### Platform Fix A\n\n", ""),
                              encoding="utf-8")
        text = document(
            component("Mod One", "20000000-0000-0000-0000-000000000001"),
            component("Mod Two", "20000000-0000-0000-0000-000000000002"),
        )
        commands = []

        def fake_run(command, source, private, stage, report, env):
            commands.append((stage, list(command)))
            if stage == "convert":
                (private / "converted.toml").write_text(text, encoding="utf-8")
            if stage == "gui":
                raise ports.Blocked("gui_stopped_by_test")

        report = {"commands": []}
        answers = ["--choice", "20000000-0000-0000-0000-000000000009=1"]
        error = None
        with mock.patch.object(ports, "run", fake_run), \
                mock.patch.object(ports, "review_manifest", return_value=answers):
            try:
                ports.execute(args, report)
            except ports.Blocked as blocked:
                error = str(blocked)
        return commands, report, error, answers

    def test_typescript_cli_installs_both_phases_with_the_physical_install_flag(self):
        commands, report, error, answers = self.run_typescript_install("cli")
        self.assertIsNone(error)
        self.assertEqual(report["status"], "installed")
        installs = [(stage, command) for stage, command in commands if stage.startswith("install_")]
        self.assertEqual([stage for stage, _ in installs], ["install_base", "install_widescreen"])
        for phase, (stage, command) in zip(("base", "widescreen"), installs):
            self.assertEqual(ports.TYPESCRIPT_PHYSICAL_INSTALL_FLAG, "--ci-physical-install")
            self.assertEqual(command.count("--ci-physical-install"), 1)
            self.assertEqual(command[command.index("--phase") + 1], phase)
            self.assertEqual(command[command.index("install"):][-3:], answers[:2] + ["--ci-physical-install"])
            self.assertEqual(command[command.index("install") + 1:].count("--phase"), 1)

    def test_typescript_flag_is_only_on_install_commands(self):
        commands, _, error, _ = self.run_typescript_install("cli")
        self.assertIsNone(error)
        for stage, command in commands:
            if not stage.startswith("install_"):
                self.assertNotIn("--ci-physical-install", command, stage)

    def test_typescript_gui_lane_never_gets_the_physical_install_flag(self):
        commands, _, error, _ = self.run_typescript_install("gui")
        self.assertEqual(error, "gui_stopped_by_test")
        self.assertIn("gui", [stage for stage, _ in commands])
        self.assertFalse([stage for stage, _ in commands if stage.startswith("install_")])
        for stage, command in commands:
            self.assertNotIn("--ci-physical-install", command, stage)

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

    def test_rust_convert_passes_archive_map_and_typescript_ignores_it(self):
        root, args = self.prepare("k1")
        args.archive_map = root / "archive-map.json"
        args.archive_map.write_text("{}", encoding="utf-8")
        text = document(component("Mod One", "30000000-0000-0000-0000-000000000001"))
        args.implementation = "rust"
        convert = dict(self.captured(args, text)[0])["convert"]
        self.assertEqual(convert[convert.index("--archive-map") + 1], str(args.archive_map.resolve()))
        args.implementation = "typescript"
        convert = dict(self.captured(args, text)[0])["convert"]
        self.assertNotIn("--archive-map", convert)

    def test_rust_convert_without_archive_map_has_no_flag(self):
        _, args = self.prepare("k1")
        args.implementation = "rust"
        text = document(component("Mod One", "30000000-0000-0000-0000-000000000001"))
        self.assertNotIn("--archive-map", dict(self.captured(args, text)[0])["convert"])


    def run_policy_lane(self, mode, implementation="rust", keys=(HELPER, MOVIES), converted=None):
        root, args = self.prepare("k1")
        args.guide.write_text(GUIDE_WITH_HELPER, encoding="utf-8")
        args.implementation = implementation
        args.mode = mode
        args.restore_report = restore_report(root / "restore.json", args.guide.resolve(), *keys)
        commands = []

        def fake_run(command, source, private, stage, report, env):
            commands.append((stage, list(command)))
            if stage == "convert":
                (private / "converted.toml").write_text(
                    converted or helper_document(), encoding="utf-8")

        report = {"commands": []}
        error = None
        with mock.patch.object(ports, "run", reviewing(fake_run)):
            try:
                ports.execute(args, report)
            except ports.Blocked as blocked:
                error = str(blocked)
        return commands, report, error

    def test_rust_cli_deselects_exactly_the_skipped_component_for_validate_and_install(self):
        commands, report, error = self.run_policy_lane("cli")
        self.assertIsNone(error)
        staged = dict(commands)
        for stage in ("validate", "install"):
            command = staged[stage]
            self.assertEqual([value for flag, value in zip(command, command[1:]) if flag == "--deselect"],
                             [MOVIES_GUID])
        self.assertNotIn("--deselect", staged["convert"])
        self.assertEqual([record["heading"] for record in report["skipped_by_reviewed_policy"]],
                         ["Helper Step", "Loose Movies"])

    def test_rust_cli_with_only_a_non_component_step_adds_no_deselect(self):
        converted = document(
            component("Mod One", "40000000-0000-0000-0000-000000000001"),
            component("Mod Two", "40000000-0000-0000-0000-000000000002"),
            component("Loose Movies", MOVIES_GUID),
        )
        commands, report, error = self.run_policy_lane("cli", keys=(HELPER,), converted=converted)
        self.assertIsNone(error)
        self.assertNotIn("--deselect", dict(commands)["install"])
        self.assertIsNone(report["skipped_by_reviewed_policy"][0]["component_guid"])

    def run_gui_lane(self, driver, keys=(HELPER, MOVIES), converted=None, guide=GUIDE_WITH_HELPER):
        root, args = self.prepare("k1")
        args.guide.write_text(guide, encoding="utf-8")
        args.implementation = "rust"
        args.mode = "gui"
        args.restore_report = restore_report(root / "restore.json", args.guide.resolve(), *keys)
        commands = []

        def fake_run(command, source, private, stage, report, env):
            commands.append((stage, list(command)))
            if stage == "convert":
                (private / "converted.toml").write_text(
                    converted or helper_document(), encoding="utf-8")
            if stage == "build":
                (private / "target" / "debug").mkdir(parents=True)
                (private / "target" / "debug" / "modsync").write_text("binary", encoding="utf-8")
            if stage == "gui":
                converted_path = private / "converted.toml"
                binary = Path(command[command.index("--binary") + 1])
                payload = {
                    "schema_version": 1, "implementation": "rust", "mode": "gui",
                    "status": "installed", "ui_completed": True,
                    "reason": "native_gui_completed_and_receipt_verified",
                    "input_sha256": hashlib.sha256(converted_path.read_bytes()).hexdigest(),
                    "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
                    "component_count": 3, "expected_components": 2, "completed_components": 2,
                    "coverage": "subset", "skipped_by_reviewed_policy": [MOVIES_GUID],
                    "excluded_widescreen": [], "widescreen_phase": "not_covered_by_gui_lane",
                    "verified_game_files": 4, "receipt_sha256": "a" * 64}
                payload.update(driver)
                Path(command[command.index("--report") + 1]).write_text(
                    json.dumps(payload), encoding="utf-8")

        report = {"commands": []}
        error = None
        with mock.patch.object(ports, "run", reviewing(fake_run)):
            try:
                ports.execute(args, report)
            except ports.Blocked as blocked:
                error = str(blocked)
        return commands, report, error

    def test_rust_gui_deselects_policy_skips_and_expects_the_reduced_receipt(self):
        commands, report, error = self.run_gui_lane({})
        self.assertIsNone(error, report)
        staged = dict(commands)
        gui = staged["gui"]
        self.assertEqual([value for flag, value in zip(gui, gui[1:]) if flag == "--deselect"], [MOVIES_GUID])
        self.assertIn("--exclude-widescreen", gui)
        self.assertEqual(report["status"], "installed")
        self.assertEqual(report["gui_covered_components"], 2)
        self.assertEqual(report["gui_not_covered"]["widescreen_phase"], "not_covered_by_gui_lane")
        self.assertEqual(report["gui_not_covered"]["excluded_widescreen"], [])
        self.assertEqual([record["component_guid"] for record in report["skipped_by_reviewed_policy"]
                          if record["component_guid"]], [MOVIES_GUID])

    def test_rust_gui_lists_unskipped_widescreen_components_as_not_covered(self):
        wide = "40000000-0000-0000-0000-0000000000a1"
        converted = document(
            component("Mod One", "40000000-0000-0000-0000-000000000001"),
            component("Mod Two", "40000000-0000-0000-0000-000000000002"),
            component("Loose Movies", MOVIES_GUID),
            component("Wide Pack", wide, WidescreenOnly="true"))
        commands, report, error = self.run_gui_lane({
            "component_count": 4, "expected_components": 2, "completed_components": 2,
            "excluded_widescreen": [wide]}, converted=converted,
            guide=GUIDE_WITH_HELPER + "\n### Wide Pack\n")
        self.assertIsNone(error, report)
        staged = dict(commands)
        deselected = lambda command: sorted(value for flag, value in zip(command, command[1:])
                                            if flag == "--deselect")
        self.assertEqual(deselected(staged["validate"]), sorted([MOVIES_GUID, wide]))
        self.assertEqual(deselected(staged["gui"]), [MOVIES_GUID])
        self.assertIn("--exclude-widescreen", staged["gui"])
        self.assertEqual([item["component_guid"] for item in report["gui_not_covered"]["excluded_widescreen"]],
                         [wide])
        self.assertEqual(report["gui_covered_components"], 2)
        self.assertEqual(report["gui_not_covered"]["widescreen_phase"], "not_covered_by_gui_lane")

    def test_rust_gui_rejects_a_driver_report_that_omits_an_excluded_widescreen_component(self):
        wide = "40000000-0000-0000-0000-0000000000a1"
        converted = document(
            component("Mod One", "40000000-0000-0000-0000-000000000001"),
            component("Mod Two", "40000000-0000-0000-0000-000000000002"),
            component("Loose Movies", MOVIES_GUID),
            component("Wide Pack", wide, WidescreenOnly="true"))
        _, _, error = self.run_gui_lane({
            "component_count": 4, "expected_components": 2, "completed_components": 2,
            "excluded_widescreen": []}, converted=converted,
            guide=GUIDE_WITH_HELPER + "\n### Wide Pack\n")
        self.assertEqual(error, "gui_completion_not_verified")

    def test_rust_gui_rejects_a_driver_report_that_covers_the_skipped_component(self):
        _, _, error = self.run_gui_lane({"completed_components": 3, "expected_components": 3})
        self.assertEqual(error, "gui_completion_not_verified")

    def test_rust_gui_rejects_a_driver_report_with_other_skips_or_exclusions(self):
        for override in ({"skipped_by_reviewed_policy": []},
                         {"excluded_widescreen": [MOVIES_GUID]},
                         {"widescreen_phase": "none_in_input"},
                         {"coverage": "full"}):
            with self.subTest(override=override):
                _, _, error = self.run_gui_lane(override)
                self.assertEqual(error, "gui_completion_not_verified")

    def test_rust_lane_without_restore_report_is_unchanged(self):
        _, args = self.prepare("k1")
        args.implementation = "rust"
        commands, report, _ = self.captured(
            args, document(component("Mod One", "30000000-0000-0000-0000-000000000001")))
        self.assertNotIn("skipped_by_reviewed_policy", report)
        self.assertNotIn("--deselect", dict(commands)["convert"])


GUIDE_4GB = """# Guide

## Mod List

### Mod One

### 4GB Patcher

### Mod Two
"""

FOUR_GB = ("Mod List", "4GB Patcher")
FOUR_GB_GUID = "50000000-0000-0000-0000-000000000002"
SHIPPED_LANE_SKIPS = Path(ports.__file__).with_name("archive_selections.json")
REJECTION = "No supported game files or patcher data found in mapped archive"


def four_gb_document(instructed=False, dependents=False, one_instructed=True):
    patcher = ["[[thisMod]]", 'Guid = "' + FOUR_GB_GUID + '"', 'Name = "4GB Patcher"', "IsSelected = true"]
    if instructed:
        patcher += ["[[thisMod.Instructions]]", 'Guid = "50000000-0000-0000-0000-0000000000f9"',
                    'Action = "Move"']
    one = component("Mod One", "50000000-0000-0000-0000-000000000001")
    if not one_instructed:
        one = one[:one.index("[[thisMod.Instructions]]")]
    if dependents:
        one = one.replace("IsSelected = false", 'IsSelected = false\nDependencies = ["' + FOUR_GB_GUID + '"]')
    return document(one, "\n".join(patcher), component("Mod Two", "50000000-0000-0000-0000-000000000003"))


def lane_skips_file(directory, table):
    path = directory / "lane-skips.json"
    path.write_text(json.dumps({"lane_skips": table}), encoding="utf-8")
    return path


def lane_entry(**override):
    entry = {"section": FOUR_GB[0], "heading": FOUR_GB[1], "reason": "reviewed: test"}
    entry.update(override)
    return entry


def draft_block(prefix, guid, required=True, gaps=0):
    return "\n".join([
        "[" + prefix + ".DraftReview]",
        "RequiresReview = " + ("true" if required else "false"),
        "[" + prefix + ".DraftReview.Result]",
        'ComponentGuid = "' + guid + '"',
        "DraftInstructionCount = 1",
        "UnparsedGaps = [" + ", ".join(['"gap"'] * gaps) + "]",
        "ConditionalDrafts = []",
        "RejectedDrafts = 0",
        "",
    ])


def drafted(name, guid, required=True, gaps=0, selected=True, widescreen=False):
    extra = {"WidescreenOnly": "true"} if widescreen else {}
    block = component(name, guid, **extra).replace(
        "IsSelected = false", "IsSelected = " + ("true" if selected else "false"), 1)
    return block + "\n" + draft_block("thisMod", guid, required, gaps)


DRAFT_ONE = "60000000-0000-0000-0000-000000000001"
DRAFT_TWO = "60000000-0000-0000-0000-000000000002"
DRAFT_REPORT = (
    "ports: noise before the report\n"
    "{\n"
    '  "cleared": [\n    "' + DRAFT_ONE + '"\n  ],\n'
    '  "skipped": [\n    {\n      "guid": "' + DRAFT_TWO.upper() + '",\n'
    '      "name": "Mod Two",\n      "reason": "Draft was not generated from resolved archive filenames"\n    }\n  ]\n'
    "}\n")


class VerifiedDraftReviewTests(unittest.TestCase):
    prepare = ExecuteCommandTests.prepare

    def read_converted(self, text):
        path = Path(tempfile.mkdtemp()) / "converted.toml"
        self.addCleanup(lambda: __import__("shutil").rmtree(path.parent))
        path.write_text(text, encoding="utf-8")
        return path

    def test_report_is_parsed_from_the_merged_log(self):
        log = self.read_converted(DRAFT_REPORT)
        report = {}
        ports.record_draft_review(log, report)
        self.assertEqual(report["draft_review"], {"cleared": 1, "skipped": [
            {"component_guid": DRAFT_TWO, "name": "Mod Two",
             "reason": "Draft was not generated from resolved archive filenames"}]})

    def test_unrelated_or_missing_logs_record_nothing(self):
        for text in ("", "{\n  \"other\": 1\n}\n", "{\n  \"cleared\": [], \"skipped\": [1]\n}\n"):
            report = {}
            ports.record_draft_review(self.read_converted(text), report)
            self.assertEqual(report, {})
        ports.record_draft_review(Path("/nonexistent/review.log"), report := {})
        self.assertEqual(report, {})

    def test_pending_drafts_lists_every_unreviewed_selected_component(self):
        converted = self.read_converted(document(
            drafted("Mod One", DRAFT_ONE), drafted("Mod Two", DRAFT_TWO),
            drafted("Done", "60000000-0000-0000-0000-000000000003", required=False),
            component("Plain", "60000000-0000-0000-0000-000000000004")))
        found = ports.pending_drafts(converted, set(), gui=False)
        self.assertEqual([item["component_guid"] for item in found], [DRAFT_ONE, DRAFT_TWO])

    def test_a_cleared_flag_with_leftover_gaps_still_counts_as_pending(self):
        converted = self.read_converted(document(
            drafted("Mod One", DRAFT_ONE, required=False, gaps=1)))
        self.assertEqual(len(ports.pending_drafts(converted, set(), gui=False)), 1)

    def test_prose_draft_warning_counts_as_pending(self):
        converted = self.read_converted(document(
            component("Mod One", DRAFT_ONE, InstallationWarning='"' + ports.REVIEW_FLAG + '"')
            .replace("IsSelected = false", "IsSelected = true")))
        self.assertEqual(len(ports.pending_drafts(converted, set(), gui=False)), 1)

    def test_exempt_components_are_not_pending(self):
        converted = self.read_converted(document(
            drafted("Mod One", DRAFT_ONE), drafted("Mod Two", DRAFT_TWO)))
        found = ports.pending_drafts(converted, {DRAFT_ONE}, gui=False)
        self.assertEqual([item["component_guid"] for item in found], [DRAFT_TWO])

    def test_unselected_components_are_ignored_by_the_cli_but_not_the_gui(self):
        converted = self.read_converted(document(
            drafted("Mod One", DRAFT_ONE), drafted("Mod Two", DRAFT_TWO, selected=False)))
        self.assertEqual(len(ports.pending_drafts(converted, set(), gui=False)), 1)
        self.assertEqual(len(ports.pending_drafts(converted, set(), gui=True)), 2)

    def test_options_are_checked_with_their_owner_in_the_heading(self):
        option_guid = "60000000-0000-0000-0000-0000000000a1"
        text = document(component("Mod One", DRAFT_ONE).replace("IsSelected = false", "IsSelected = true")
                        + "\n[[thisMod.Options]]\n"
                        + 'Guid = "' + option_guid + '"\nName = "Opt"\nIsSelected = true\n'
                        + draft_block("thisMod.Options", option_guid))
        found = ports.pending_drafts(self.read_converted(text), set(), gui=False)
        self.assertEqual(found, [{"component_guid": option_guid, "heading": "Mod One / Opt"}])
        unselected = text.replace("IsSelected = true\n[thisMod.Options", "IsSelected = false\n[thisMod.Options")
        self.assertEqual(ports.pending_drafts(self.read_converted(unselected), set(), gui=False), [])
        self.assertEqual(len(ports.pending_drafts(self.read_converted(unselected), set(), gui=True)), 1)

    def run_lane(self, converted, mode="cli", review_log=DRAFT_REPORT):
        root, args = self.prepare("k1")
        args.implementation = "rust"
        args.mode = mode
        commands = []

        def fake_run(command, source, private, stage, report, env):
            commands.append((stage, list(command)))
            if stage == "convert":
                (private / "converted.toml").write_text(converted, encoding="utf-8")
            if stage == ports.DRAFT_REVIEW_STAGE:
                (private / (stage + ".log")).write_text(review_log, encoding="utf-8")

        report = {"commands": []}
        error = None
        with mock.patch.object(ports, "run", reviewing(fake_run)):
            try:
                ports.execute(args, report)
            except ports.Blocked as blocked:
                error = str(blocked)
        return root, commands, report, error

    def test_convert_runs_the_verified_review_on_the_converted_toml_with_the_archive_folder(self):
        converted = document(component("Mod One", "60000000-0000-0000-0000-000000000011"),
                             component("Mod Two", "60000000-0000-0000-0000-000000000012"))
        root, commands, report, _ = self.run_lane(converted)
        stages = [stage for stage, _ in commands]
        self.assertEqual(stages[:3], ["build", "convert", ports.DRAFT_REVIEW_STAGE])
        review = dict(commands)[ports.DRAFT_REVIEW_STAGE]
        private = next((root / "work").glob("ports-*"))
        self.assertEqual(review[1:], [
            "convert", str(private / "converted.toml"), str(private / "converted-reviewed.toml"),
            "--format", "toml", "--source-path", str((root / "archives").resolve()),
            "--review-verified-drafts"])
        self.assertEqual(report["draft_review"]["cleared"], 1)
        self.assertEqual(report["converted_sha256"],
                         hashlib.sha256((private / "converted.toml").read_bytes()).hexdigest())

    def test_unreviewed_drafts_block_before_validate_and_list_every_guid(self):
        converted = document(drafted("Mod One", DRAFT_ONE), drafted("Mod Two", DRAFT_TWO))
        for mode in ("cli", "gui"):
            with self.subTest(mode=mode):
                _, commands, report, error = self.run_lane(converted, mode)
                self.assertTrue(error.startswith("unreviewed_draft_instructions_remain: "))
                self.assertIn(DRAFT_ONE, error)
                self.assertIn(DRAFT_TWO, error)
                self.assertIn("Mod One", error)
                self.assertNotIn("validate", [stage for stage, _ in commands])
                self.assertEqual([item["component_guid"] for item in report["draft_review"]["unreviewed"]],
                                 [DRAFT_ONE, DRAFT_TWO])
                self.assertEqual(report["draft_review"]["cleared"], 1)

    def test_reviewed_components_let_the_lane_reach_validate(self):
        converted = document(drafted("Mod One", DRAFT_ONE, required=False),
                             drafted("Mod Two", DRAFT_TWO, required=False))
        _, commands, report, error = self.run_lane(converted)
        self.assertIsNone(error)
        self.assertIn("validate", [stage for stage, _ in commands])
        self.assertNotIn("unreviewed", report["draft_review"])

    def test_a_failed_review_convert_blocks_and_keeps_the_report(self):
        root, args = self.prepare("k1")
        args.implementation = "rust"

        def failing(command, source, private, stage, report, env):
            if stage == "convert":
                (private / "converted.toml").write_text(
                    document(drafted("Mod One", DRAFT_ONE), drafted("Mod Two", DRAFT_TWO)), encoding="utf-8")
            if stage == ports.DRAFT_REVIEW_STAGE:
                (private / (stage + ".log")).write_text(DRAFT_REPORT, encoding="utf-8")
                raise ports.Blocked(stage + "_failed")

        report = {"commands": []}
        with mock.patch.object(ports, "run", failing):
            with self.assertRaises(ports.Blocked) as caught:
                ports.execute(args, report)
        self.assertEqual(str(caught.exception), "review_verified_drafts_failed")
        self.assertEqual(report["draft_review"]["cleared"], 1)

    def test_typescript_lanes_do_not_run_the_verified_review(self):
        _, args = self.prepare("k2")
        args.implementation = "typescript"
        args.guide.write_text(GUIDE.replace("## CRITICAL: Platform Notes\n\n### Platform Fix A\n\n", ""),
                              encoding="utf-8")
        commands = []

        def fake_run(command, source, private, stage, report, env):
            commands.append(stage)
            if stage == "convert":
                (private / "converted.toml").write_text(document(
                    component("Mod One", "20000000-0000-0000-0000-000000000001"),
                    component("Mod Two", "20000000-0000-0000-0000-000000000002")), encoding="utf-8")

        with mock.patch.object(ports, "run", fake_run):
            with self.assertRaises(ports.Blocked):
                ports.execute(args, {"commands": []})
        self.assertNotIn(ports.DRAFT_REVIEW_STAGE, commands)


BIND_ONE = "70000000-0000-0000-0000-000000000001"
BIND_TWO = "70000000-0000-0000-0000-000000000002"
LINK_ONE = "https://example.invalid/files/1-one/"
LINK_TWO = "https://example.invalid/files/2-two/"
LINK_EXTRA = "https://example.invalid/files/3-extra/"


def linked(name, guid, *links, bound=False, instructed=True):
    registry = "{" + ", ".join('"' + link + '" = {}' for link in links) + "}"
    block = component(name, guid, ResourceRegistry=registry)
    if not instructed:
        block = block[:block.index("[[thisMod.Instructions]]")]
    record = draft_block("thisMod", guid)
    if bound:
        record = record.replace("RequiresReview = true\n",
                                'RequiresReview = true\nGenerator = "resolved-filenames"\n')
    return block + "\n" + record


class ArchiveBindingTests(unittest.TestCase):
    prepare = ExecuteCommandTests.prepare

    def archives(self, **files):
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(directory))
        for name, content in files.items():
            (directory / name.replace("_", ".")).write_bytes(content)
        return directory

    def write(self, text, name="converted.toml"):
        path = self.archives() / name
        path.write_text(text, encoding="utf-8")
        return path

    def test_links_are_compared_the_way_the_archive_map_keys_them(self):
        self.assertEqual(ports.canonical_link("HTTPS://User@Example.COM:8443/files/12-a/?do=download&r=1#top"),
                         "https://example.com:8443/files/12-a/")
        self.assertEqual(ports.canonical_link("https://example.invalid/?page_id=371"),
                         "https://example.invalid/")

    def test_archive_map_keeps_only_single_file_links(self):
        path = self.write(json.dumps({LINK_ONE + "?x=1": "one.zip", LINK_TWO: ["a.zip", "b.zip"],
                                      LINK_EXTRA: ["only.7z"]}))
        self.assertEqual(ports.mapped_archives(path), {
            ports.canonical_link(LINK_ONE): "one.zip", ports.canonical_link(LINK_TWO): None,
            ports.canonical_link(LINK_EXTRA): "only.7z"})

    def test_conflicting_entries_for_one_canonical_link_map_to_nothing(self):
        path = self.write(json.dumps({LINK_ONE: "one.zip", LINK_ONE + "?v=2": "other.zip"}))
        self.assertEqual(ports.mapped_archives(path), {ports.canonical_link(LINK_ONE): None})

    def bindings(self, blocks, mapping, **files):
        archives = self.archives(**files)
        path = self.write(document(*blocks))
        return ports.resolved_archive_bindings(path, mapping, archives)

    def test_only_components_with_exactly_one_mapped_present_archive_are_bound(self):
        mapping = {ports.canonical_link(LINK_ONE): "one.zip", ports.canonical_link(LINK_TWO): "two.zip",
                   ports.canonical_link(LINK_EXTRA): None}
        blocks = [
            linked("Mod One", BIND_ONE, LINK_ONE + "?download=1"),
            linked("Mod Two", BIND_TWO, LINK_TWO, LINK_EXTRA),
            linked("Mod Three", "70000000-0000-0000-0000-000000000003", LINK_EXTRA),
            linked("Mod Four", "70000000-0000-0000-0000-000000000004", "https://example.invalid/unmapped"),
            linked("Mod Five", "70000000-0000-0000-0000-000000000005", LINK_ONE, LINK_TWO),
            linked("Mod Six", "70000000-0000-0000-0000-000000000006"),
        ]
        found = self.bindings(blocks, mapping, one_zip=b"one", two_zip=b"two")
        self.assertEqual(found, [{
            "componentGuid": BIND_ONE, "filenames": ["one.zip"],
            "sha256": hashlib.sha256(b"one").hexdigest(), "sizeBytes": 3}])

    def test_unsafe_missing_unsupported_and_linked_archives_are_never_listed(self):
        names = ["a/b.zip", "..", "wild*.zip", "x.txt", "gone.zip", "link.zip"]
        blocks = [linked("Mod " + str(index), "70000000-0000-0000-0000-00000000010" + str(index),
                         "https://example.invalid/" + str(index)) for index in range(len(names))]
        mapping = {ports.canonical_link("https://example.invalid/" + str(index)): name
                   for index, name in enumerate(names)}
        archives = self.archives(x_txt=b"x")
        outside = self.archives(target_zip=b"t")
        (archives / "link.zip").symlink_to(outside / "target.zip")
        path = self.write(document(*blocks))
        self.assertEqual(ports.resolved_archive_bindings(path, mapping, archives), [])

    def test_a_guid_that_appears_twice_is_never_listed(self):
        mapping = {ports.canonical_link(LINK_ONE): "one.zip"}
        blocks = [linked("Mod One", BIND_ONE, LINK_ONE), linked("Mod Again", BIND_ONE, LINK_ONE)]
        self.assertEqual(self.bindings(blocks, mapping, one_zip=b"one"), [])

    def declared_bindings(self, rule_files, blocks, mapping=None, rule_heading="Guide Heading", **files):
        archives = self.archives(**files)
        path = self.write(document(*blocks))
        rules = {"90000000-0000-0000-0000-000000000001": {
            "heading": rule_heading, "guid": "90000000-0000-0000-0000-000000000001",
            "files": rule_files, "reason": "reviewed"}}
        return ports.resolved_archive_bindings(path, mapping or {}, archives, rules)

    def heading_block(self, guid, name="Name Field", heading="Guide Heading", instructed=False, **links):
        extra = {"Heading": '"' + heading + '"'} if heading else {}
        block = component(name, guid, ResourceRegistry="{" + ", ".join('"' + link + '" = {}' for link in links.values()) + "}",
                          **extra)
        return block if instructed else block[:block.index("[[thisMod.Instructions]]")]

    def test_a_reviewed_alias_binds_by_heading_when_the_map_cannot_choose(self):
        blocks = [self.heading_block(BIND_ONE, one=LINK_ONE)]
        mapping = {ports.canonical_link(LINK_ONE): None}
        content = b"Rar!\x1a\x07generic"
        self.assertEqual(self.declared_bindings(["generic.rar"], blocks, mapping, generic_rar=content), [{
            "componentGuid": BIND_ONE, "filenames": ["generic.rar"],
            "sha256": hashlib.sha256(content).hexdigest(), "sizeBytes": len(content)}])

    def test_a_reviewed_alias_binds_by_component_name_or_heading_when_they_differ(self):
        mapping = {ports.canonical_link(LINK_ONE): None}
        content = b"PK\x03\x04generic"
        expected = [{"componentGuid": BIND_ONE, "filenames": ["generic.zip"],
                     "sha256": hashlib.sha256(content).hexdigest(), "sizeBytes": len(content)}]
        cases = {
            "rule keyed by the Name field": ("Alias Name", "Alias Name", "Guide Heading"),
            "rule keyed by the heading": ("Guide Heading", "Alias Name", "Guide Heading"),
            "name only, no heading emitted": ("Alias Name", "Alias Name", None),
            "case and spacing ignored": ("  alias   NAME ", "Alias Name", "Guide Heading"),
        }
        for label, (rule_heading, name, heading) in cases.items():
            with self.subTest(label):
                blocks = [self.heading_block(BIND_ONE, name=name, heading=heading, one=LINK_ONE)]
                self.assertEqual(self.declared_bindings(["generic.zip"], blocks, mapping,
                                                        rule_heading=rule_heading, generic_zip=content), expected)

    def test_a_reviewed_alias_matched_by_name_stays_unbound_when_unsafe_or_ambiguous(self):
        mapping = {ports.canonical_link(LINK_ONE): None}
        good = b"PK\x03\x04generic"
        cases = {
            "name shared by another component": [
                self.heading_block(BIND_ONE, name="Alias Name", heading="Guide Heading", one=LINK_ONE),
                self.heading_block(BIND_TWO, name="Other", heading="Alias Name", one=LINK_TWO)],
            "name unrelated to the rule": [
                self.heading_block(BIND_ONE, name="Unrelated", heading="Other Heading", one=LINK_ONE)],
            "name matches but has instructions": [
                self.heading_block(BIND_ONE, name="Alias Name", instructed=True, one=LINK_ONE)],
        }
        for label, blocks in cases.items():
            with self.subTest(label):
                self.assertEqual(self.declared_bindings(["generic.zip"], blocks, mapping,
                                                        rule_heading="Alias Name", generic_zip=good), [])

    def test_a_reviewed_alias_never_binds_an_unsafe_or_ambiguous_component(self):
        mapping = {ports.canonical_link(LINK_ONE): None}
        good = b"PK\x03\x04generic"
        cases = {
            "several files": (["generic.zip", "other.zip"], [self.heading_block(BIND_ONE, one=LINK_ONE)],
                              dict(generic_zip=good, other_zip=good)),
            "has instructions": (["generic.zip"], [self.heading_block(BIND_ONE, instructed=True, one=LINK_ONE)],
                                 dict(generic_zip=good)),
            "heading shared": (["generic.zip"], [self.heading_block(BIND_ONE, one=LINK_ONE),
                                                 self.heading_block(BIND_TWO, name="Other", one=LINK_TWO)],
                               dict(generic_zip=good)),
            "heading differs": (["generic.zip"], [self.heading_block(BIND_ONE, heading="Other Heading",
                                                                    one=LINK_ONE)], dict(generic_zip=good)),
            "file missing": (["generic.zip"], [self.heading_block(BIND_ONE, one=LINK_ONE)], {}),
            "not an archive": (["generic.zip"], [self.heading_block(BIND_ONE, one=LINK_ONE)],
                               dict(generic_zip=b"text")),
        }
        for label, (rule_files, blocks, files) in cases.items():
            with self.subTest(label):
                self.assertEqual(self.declared_bindings(rule_files, blocks, mapping, **files), [])

    def test_a_mapped_archive_wins_over_a_reviewed_alias(self):
        blocks = [self.heading_block(BIND_ONE, one=LINK_ONE)]
        mapping = {ports.canonical_link(LINK_ONE): "mapped.zip"}
        found = self.declared_bindings(["generic.zip"], blocks, mapping,
                                       mapped_zip=b"PK\x03\x04m", generic_zip=b"PK\x03\x04g")
        self.assertEqual([item["filenames"] for item in found], [["mapped.zip"]])

    def test_bound_result_counts_bound_components_and_accepts_the_normal_changes(self):
        before = self.write(document(linked("Mod One", BIND_ONE, LINK_ONE, instructed=False),
                                     linked("Mod Two", BIND_TWO, LINK_TWO)))
        after = self.write(document(linked("Mod One", BIND_ONE, LINK_ONE, bound=True),
                                    linked("Mod Two", BIND_TWO, LINK_TWO)))
        self.assertEqual(ports.bound_result(before, after), (1, None))

    def test_bound_result_rejects_lost_instructions_or_drafts_or_components(self):
        full = document(linked("Mod One", BIND_ONE, LINK_ONE), linked("Mod Two", BIND_TWO, LINK_TWO))
        before = self.write(full)
        lost_instructions = self.write(document(
            linked("Mod One", BIND_ONE, LINK_ONE, instructed=False), linked("Mod Two", BIND_TWO, LINK_TWO)))
        lost_draft = self.write(document(
            component("Mod One", BIND_ONE, ResourceRegistry='{"' + LINK_ONE + '" = {}}'),
            linked("Mod Two", BIND_TWO, LINK_TWO)))
        lost_component = self.write(document(linked("Mod One", BIND_ONE, LINK_ONE)))
        self.assertEqual(ports.bound_result(before, lost_instructions)[1],
                         "bound_output_drops_instructions_or_drafts")
        self.assertEqual(ports.bound_result(before, lost_draft)[1],
                         "bound_output_drops_instructions_or_drafts")
        self.assertEqual(ports.bound_result(before, lost_component)[1], "bound_output_components_changed")

    def run_lane(self, converted, bound_output, mapping=True, fail=None, files=None, interrupt=False):
        root, args = self.prepare("k1")
        args.implementation = "rust"
        for name, content in (files if files is not None else {"one.zip": b"one", "two.zip": b"two"}).items():
            (root / "archives" / name).write_bytes(content)
        if mapping:
            args.archive_map = root / "archive-map.json"
            args.archive_map.write_text(json.dumps(
                {LINK_ONE: "one.zip", LINK_TWO: "two.zip"} if mapping is True else mapping), encoding="utf-8")
        commands, seen = [], {}

        def fake_run(command, source, private, stage, report, env):
            commands.append((stage, list(command)))
            report["stage"] = stage
            if stage == "convert":
                (private / "converted.toml").write_text(converted, encoding="utf-8")
            if stage == ports.BIND_STAGE:
                seen["plan"] = json.loads(Path(command[command.index("--resolved-archives") + 1]).read_text())
                if fail == stage:
                    raise ports.Blocked("command_interrupted_or_timed_out" if interrupt else stage + "_failed")
                Path(command[3]).write_text(converted, encoding="utf-8")
            if stage == ports.BIND_GENERATE_STAGE:
                if fail == stage:
                    raise ports.Blocked(stage + "_failed")
                Path(command[3]).write_text(bound_output, encoding="utf-8")
            if stage == ports.DRAFT_REVIEW_STAGE:
                seen["review_input"] = Path(command[2]).read_text(encoding="utf-8")
                (private / (stage + ".log")).write_text(DRAFT_REPORT, encoding="utf-8")

        report = {"commands": []}
        error = None
        with mock.patch.object(ports, "run", reviewing(fake_run)):
            try:
                ports.execute(args, report)
            except ports.Blocked as blocked:
                error = str(blocked)
        return root, commands, report, error, seen

    def documents(self):
        converted = document(linked("Mod One", BIND_ONE, LINK_ONE), linked("Mod Two", BIND_TWO, LINK_TWO))
        bound = document(linked("Mod One", BIND_ONE, LINK_ONE, bound=True), linked("Mod Two", BIND_TWO, LINK_TWO))
        return converted, bound

    def test_pass_order_commands_and_bound_count(self):
        converted, bound = self.documents()
        root, commands, report, _, seen = self.run_lane(converted, bound)
        stages = [stage for stage, _ in commands]
        self.assertEqual(stages[:5], ["build", "convert", ports.BIND_STAGE, ports.BIND_GENERATE_STAGE,
                                      ports.DRAFT_REVIEW_STAGE])
        private = next((root / "work").glob("ports-*"))
        archives = str((root / "archives").resolve())
        binding = dict(commands)[ports.BIND_STAGE]
        self.assertEqual(binding[1:], [
            "convert", str(root / "content" / "k1" / "full.md"), str(private / "converted-bound-pending.toml"),
            "--format", "toml", "--parse-directions", "--source-path", archives,
            "--resolved-archives", str(private / "resolved-archives.json")])
        self.assertNotIn("--auto-generate-local", binding)
        generate = dict(commands)[ports.BIND_GENERATE_STAGE]
        self.assertEqual(generate[1:], [
            "convert", str(private / "converted-bound-pending.toml"), str(private / "converted-bound.toml"),
            "--format", "toml", "--source-path", archives, "--auto-generate-local",
            "--archive-map", str((root / "archive-map.json").resolve())])
        self.assertEqual(dict(commands)[ports.DRAFT_REVIEW_STAGE][2], str(private / "converted-bound.toml"))
        self.assertEqual(seen["review_input"], bound)
        self.assertEqual(seen["plan"], [{
            "componentGuid": BIND_ONE, "filenames": ["one.zip"],
            "sha256": hashlib.sha256(b"one").hexdigest(), "sizeBytes": 3}, {
            "componentGuid": BIND_TWO, "filenames": ["two.zip"],
            "sha256": hashlib.sha256(b"two").hexdigest(), "sizeBytes": 3}])
        self.assertEqual(report["draft_review"]["bound"], 1)
        self.assertEqual(report["draft_review"]["cleared"], 1)
        self.assertNotIn("bind_failed", report["draft_review"])

    def test_a_failed_binding_pass_keeps_the_plain_converted_toml(self):
        converted, bound = self.documents()
        for stage in (ports.BIND_STAGE, ports.BIND_GENERATE_STAGE):
            with self.subTest(stage=stage):
                _, commands, report, _, seen = self.run_lane(converted, bound, fail=stage)
                self.assertEqual(seen["review_input"], converted)
                self.assertEqual(report["draft_review"]["bound"], 0)
                self.assertEqual(report["draft_review"]["bind_failed"], stage + "_failed")
                self.assertEqual(report["draft_review"]["cleared"], 1)
                self.assertIn(ports.DRAFT_REVIEW_STAGE, [name for name, _ in commands])

    def test_an_interrupted_binding_pass_stops_the_lane(self):
        converted, bound = self.documents()
        _, commands, _, error, _ = self.run_lane(converted, bound, fail=ports.BIND_STAGE, interrupt=True)
        self.assertEqual(error, "command_interrupted_or_timed_out")
        self.assertNotIn(ports.DRAFT_REVIEW_STAGE, [name for name, _ in commands])

    def test_output_that_loses_instructions_is_not_used(self):
        converted, _ = self.documents()
        lossy = document(linked("Mod One", BIND_ONE, LINK_ONE, bound=True, instructed=False),
                         linked("Mod Two", BIND_TWO, LINK_TWO, instructed=False))
        _, _, report, _, seen = self.run_lane(converted, lossy)
        self.assertEqual(seen["review_input"], converted)
        self.assertEqual(report["draft_review"]["bound"], 0)
        self.assertEqual(report["draft_review"]["bind_failed"], "bound_output_drops_instructions_or_drafts")

    def test_no_binding_is_attempted_without_a_map_or_a_unique_present_archive(self):
        converted, bound = self.documents()
        cases = {"no map": dict(mapping=False),
                 "multi-file entries": dict(mapping={LINK_ONE: ["a.zip", "b.zip"], LINK_TWO: ["c.zip", "d.zip"]}),
                 "archives missing": dict(files={})}
        for label, options in cases.items():
            with self.subTest(label):
                _, commands, report, _, seen = self.run_lane(converted, bound, **options)
                stages = [name for name, _ in commands]
                self.assertNotIn(ports.BIND_STAGE, stages)
                self.assertNotIn(ports.BIND_GENERATE_STAGE, stages)
                self.assertEqual(seen["review_input"], converted)
                self.assertEqual(report["draft_review"]["bound"], 0)
                self.assertNotIn("bind_failed", report["draft_review"])

    def test_an_unreadable_map_keeps_the_plain_converted_toml(self):
        root, args = self.prepare("k1")
        args.archive_map = root / "archive-map.json"
        args.archive_map.write_text("[1]", encoding="utf-8")
        review = {}
        private = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(private))
        with mock.patch.object(ports, "run", side_effect=AssertionError("must not run")):
            kept = ports.bind_archives(["modsync"], root, args.guide, private / "converted.toml",
                                       root / "archives", args.archive_map, private, {"draft_review": review}, {})
        self.assertEqual(kept, private / "converted.toml")
        self.assertEqual(review, {"bound": 0, "bind_failed": "archive_map_unreadable"})

    def test_bound_and_failure_are_printed_for_the_ci_log(self):
        report = {"draft_review": {"bound": 40, "cleared": 40, "skipped": [{}] * 111,
                                   "bind_failed": "bind_archives_failed"}}
        with contextlib.redirect_stderr(io.StringIO()) as stderr:
            ports.print_diagnostics(report)
        self.assertIn("archive binding bound 40 component(s); not used: bind_archives_failed", stderr.getvalue())
        self.assertIn("cleared 40 component(s); left fenced: 111", stderr.getvalue())
        with contextlib.redirect_stderr(io.StringIO()) as stderr:
            ports.print_diagnostics({"draft_review": {"bound": 0}})
        self.assertEqual(stderr.getvalue(), "")

    def test_typescript_lanes_never_bind(self):
        _, args = self.prepare("k2")
        args.implementation = "typescript"
        args.archive_map = args.guide.parent / "map.json"
        args.archive_map.write_text("{}", encoding="utf-8")
        args.guide.write_text(GUIDE.replace("## CRITICAL: Platform Notes\n\n### Platform Fix A\n\n", ""),
                              encoding="utf-8")
        stages = []

        def fake_run(command, source, private, stage, report, env):
            stages.append(stage)
            if stage == "convert":
                (private / "converted.toml").write_text(document(
                    component("Mod One", "20000000-0000-0000-0000-000000000001"),
                    component("Mod Two", "20000000-0000-0000-0000-000000000002")), encoding="utf-8")

        with mock.patch.object(ports, "run", fake_run):
            with self.assertRaises(ports.Blocked):
                ports.execute(args, {"commands": []})
        self.assertNotIn(ports.BIND_STAGE, stages)


ALIAS_RULE_GUID = "70000000-0000-0000-0000-000000000001"


def alias_rule(**override):
    rule = {"heading": "Generic Mod", "guid": ALIAS_RULE_GUID, "files": ["one.zip", "two.zip"],
            "reason": "reviewed: test"}
    rule.update(override)
    return rule


class ArchiveAliasRuleTableTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.directory))

    def table(self, table):
        path = self.directory / "aliases.json"
        path.write_text(json.dumps({"archive_aliases": table}), encoding="utf-8")
        return path

    def test_rules_are_returned_by_guid_for_the_requested_game_only(self):
        path = self.table({"k1": [alias_rule()], "k2": [alias_rule(guid=ALIAS_B, heading="Other")]})
        self.assertEqual(list(ports.load_archive_aliases("k1", path)), [ALIAS_RULE_GUID])
        self.assertEqual(ports.load_archive_aliases("k1", path)[ALIAS_RULE_GUID]["files"], ["one.zip", "two.zip"])
        self.assertEqual(list(ports.load_archive_aliases("k2", path)), [ALIAS_B])
        self.assertEqual(ports.load_archive_aliases("k3", path), {})

    def test_missing_file_or_table_declares_nothing(self):
        self.assertEqual(ports.load_archive_aliases("k1", self.directory / "absent.json"), {})
        empty = self.directory / "empty.json"
        empty.write_text("{}", encoding="utf-8")
        self.assertEqual(ports.load_archive_aliases("k1", empty), {})

    def test_malformed_tables_block_for_every_game(self):
        for table in (
            [],
            {"k3": [alias_rule()]},
            {"k2": alias_rule()},
            {"k2": ["x"]},
            {"k2": [alias_rule(extra="x")]},
            {"k2": [{key: value for key, value in alias_rule().items() if key != "reason"}]},
            {"k2": [alias_rule(reason=" ")]},
            {"k2": [alias_rule(heading=" Generic Mod")]},
            {"k2": [alias_rule(guid="{" + ALIAS_RULE_GUID.upper() + "}")]},
            {"k2": [alias_rule(files=[])]},
            {"k2": [alias_rule(files="one.zip")]},
            {"k2": [alias_rule(files=["../one.zip"])]},
            {"k2": [alias_rule(files=["a/one.zip"])]},
            {"k2": [alias_rule(files=[".."])]},
            {"k2": [alias_rule(files=[" one.zip"])]},
            {"k2": [alias_rule(files=["x" * 256])]},
            {"k2": [alias_rule(files=[3])]},
            {"k2": [alias_rule(files=["one.zip", "one.zip"])]},
            {"k2": [alias_rule(), alias_rule(heading="Other")]},
            {"k2": [alias_rule(), alias_rule(guid=ALIAS_B)]},
        ):
            with self.subTest(table=table):
                with self.assertRaisesRegex(ports.Blocked, "archive_aliases_(invalid|duplicate)"):
                    ports.load_archive_aliases("k1", self.table(table))

    def test_shipped_rules_are_valid_unique_and_reasoned_for_both_games(self):
        for game in ("k1", "k2"):
            rules = ports.load_archive_aliases(game, SHIPPED_LANE_SKIPS)
            with self.subTest(game=game):
                self.assertTrue(rules)
                self.assertEqual(len({rule["heading"] for rule in rules.values()}), len(rules))
                for rule in rules.values():
                    self.assertTrue(rule["reason"])
        self.assertEqual(
            sorted(rule["heading"] for rule in ports.load_archive_aliases("k2", SHIPPED_LANE_SKIPS).values()),
            ["Bao-Dur/Darth Maul", "Handmaiden Fit Model", "Korriban Sith Art", "Relighting TSL",
             "Replacement Loadscreens", "Ultimate Character Overhaul", "Ultimate Character Overhaul Patches",
             "Worn-Out Mando Armor"])
        self.assertEqual(
            sorted(rule["heading"] for rule in ports.load_archive_aliases("k1", SHIPPED_LANE_SKIPS).values()),
            ["HQ Skyboxes II", "Korriban Sith Art", "Ultimate Character Overhaul",
             "Ultimate Character Overhaul Patches"])


class LaneSkipTableTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.directory))

    def shipped(self, game):
        return ports.load_lane_skips(game, SHIPPED_LANE_SKIPS)

    def test_shipped_table_keeps_the_hand_authored_4gb_patcher_in_both_games(self):
        for game in ("k1", "k2"):
            hand_authored = [skip for skip in self.shipped(game) if "kind" not in skip]
            self.assertEqual([(skip["section"], skip["heading"]) for skip in hand_authored], [FOUR_GB], game)
            self.assertIn(REJECTION, hand_authored[0]["reason"])
            self.assertEqual(hand_authored[0]["urls"], [])
        self.assertEqual(ports.load_lane_skips("k3", SHIPPED_LANE_SKIPS), [])

    def test_shipped_pending_review_entries_are_pinned_visible_and_unique(self):
        counts = {"k1": (150, 0), "k2": (118, 1)}
        for game, (drafts, dependents) in counts.items():
            pinned = [skip for skip in self.shipped(game) if "kind" in skip]
            with self.subTest(game=game):
                self.assertEqual(sum(skip["kind"] == "unreviewed_draft" for skip in pinned), drafts)
                self.assertEqual(sum(skip["kind"] == "dependent_of_deselected" for skip in pinned), dependents)
                self.assertEqual(len({skip["guid"] for skip in pinned}), len(pinned))
                self.assertEqual(len({(skip["section"], skip["heading"]) for skip in pinned}), len(pinned))
                for skip in pinned:
                    self.assertIn("deselected pending human review, delegated by owner 2026-10-18",
                                  skip["reason"])

    def test_typescript_lane_entries_apply_to_the_typescript_lane_only(self):
        path = lane_skips_file(self.directory, {"k2": [
            lane_entry(), lane_entry(heading="TSLRCM", lane="typescript", reason="reviewed: installer")]})
        self.assertEqual([skip["heading"] for skip in ports.load_lane_skips("k2", path)], ["4GB Patcher"])
        self.assertEqual([skip["heading"] for skip in ports.load_lane_skips("k2", path, "typescript")],
                         ["4GB Patcher", "TSLRCM"])
        self.assertEqual(ports.load_lane_skips("k1", path, "typescript"), [])

    def test_a_typescript_lane_entry_may_share_a_step_with_a_pinned_rust_entry(self):
        pinned = lane_entry(heading="TSLRCM", guid="bee5a72b-d748-8f83-8261-81758a9bfc56",
                            kind="unreviewed_draft")
        path = lane_skips_file(self.directory, {"k2": [pinned, lane_entry(heading="TSLRCM", lane="typescript")]})
        self.assertEqual([skip.get("kind") for skip in ports.load_lane_skips("k2", path)], ["unreviewed_draft"])
        typescript = ports.load_lane_skips("k2", path, "typescript")
        self.assertEqual([(skip["heading"], skip.get("kind")) for skip in typescript], [("TSLRCM", None)])

    def test_malformed_or_clashing_lane_keys_block(self):
        pinned = {"guid": "bee5a72b-d748-8f83-8261-81758a9bfc56", "kind": "unreviewed_draft"}
        for table in (
            {"k2": [lane_entry(lane="rust")]},
            {"k2": [lane_entry(lane=3)]},
            {"k2": [lane_entry(lane="typescript", **pinned)]},
            {"k2": [lane_entry(), lane_entry(lane="typescript")]},
            {"k2": [lane_entry(lane="typescript"), lane_entry()]},
            {"k2": [lane_entry(lane="typescript"), lane_entry(lane="typescript", reason="again")]},
        ):
            with self.subTest(table=table):
                path = lane_skips_file(self.directory, table)
                with self.assertRaisesRegex(ports.Blocked, "lane_skips_(invalid|duplicate)"):
                    ports.load_lane_skips("k1", path)

    def test_shipped_k2_typescript_lane_skips_tslrcm_visibly_with_every_dependent(self):
        skips = [skip for skip in ports.load_lane_skips("k2", SHIPPED_LANE_SKIPS, "typescript")
                 if "kind" not in skip]
        self.assertEqual([(skip["section"], skip["heading"]) for skip in skips][:2], [FOUR_GB, ("Mod List", "TSLRCM")])
        self.assertEqual(len({skip["heading"] for skip in skips}), len(skips))
        self.assertIn("tslrcm2022.exe", skips[1]["reason"])
        self.assertEqual(len(skips), 2 + 18 + 3)
        self.assertEqual([skip["heading"] for skip in skips][-3:],
                         ["PFHC03 Restoration", "Alignment Affects Force Powers", "K2 Remastered Cutscenes"])
        self.assertEqual([skip["heading"] for skip in ports.load_lane_skips("k1", SHIPPED_LANE_SKIPS, "typescript")],
                         ["4GB Patcher", "CineMalak", "Bendak Non-Darkside Option",
                          "Alignment Affects Force Powers", "Character Startup Changes"])

    def test_shipped_reasons_are_the_generated_reviewed_text(self):
        for game in ("k1", "k2"):
            for skip in self.shipped(game):
                if skip.get("kind") == "unreviewed_draft":
                    self.assertEqual(skip["reason"], deselect_unreviewed.DRAFT_REASON)
                elif skip.get("kind") == "dependent_of_deselected":
                    self.assertTrue(skip["reason"].startswith("depends on "), skip["reason"])

    def test_missing_file_or_table_skips_nothing(self):
        self.assertEqual(ports.load_lane_skips("k1", self.directory / "absent.json"), [])
        empty = self.directory / "empty.json"
        empty.write_text("{}", encoding="utf-8")
        self.assertEqual(ports.load_lane_skips("k1", empty), [])

    def test_only_the_requested_game_is_returned(self):
        path = lane_skips_file(self.directory, {"k2": [lane_entry()]})
        self.assertEqual(ports.load_lane_skips("k1", path), [])
        self.assertEqual(len(ports.load_lane_skips("k2", path)), 1)

    def test_malformed_tables_block_for_every_game(self):
        for table in (
            [],
            {"k3": [lane_entry()]},
            {"k2": lane_entry()},
            {"k2": ["4GB Patcher"]},
            {"k2": [lane_entry(extra="x")]},
            {"k2": [{"section": FOUR_GB[0], "heading": FOUR_GB[1]}]},
            {"k2": [lane_entry(reason="")]},
            {"k2": [lane_entry(reason="   ")]},
            {"k2": [lane_entry(heading=" 4GB Patcher")]},
            {"k2": [lane_entry(section=3)]},
            {"k2": [lane_entry(), lane_entry(reason="again")]},
        ):
            with self.subTest(table=table):
                path = lane_skips_file(self.directory, table)
                with self.assertRaisesRegex(ports.Blocked, "lane_skips_(invalid|duplicate)"):
                    ports.load_lane_skips("k1", path)

    def test_a_restore_report_skip_for_the_same_step_is_not_doubled(self):
        guide = self.directory / "full.md"
        restored = ports.load_policy_skips(
            restore_report(self.directory / "restore.json", guide.resolve(), FOUR_GB), guide.resolve())
        lane = ports.load_lane_skips("k1", lane_skips_file(self.directory, {"k1": [lane_entry()]}))
        merged = ports.merge_lane_skips(restored, lane)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["source"], "restore_report")
        self.assertEqual(ports.merge_lane_skips([], lane), lane)


class LaneSkipLaneTests(unittest.TestCase):
    prepare = ExecuteCommandTests.prepare

    def run_lane(self, mode="cli", implementation="rust", converted=None, table="four_gb",
                 game="k1", restore=False, log=True):
        root, args = self.prepare(game)
        args.guide.write_text(GUIDE_4GB, encoding="utf-8")
        args.implementation = implementation
        args.mode = mode
        if table is not None:
            if table == "four_gb":
                table = {game: [lane_entry(reason=REJECTION)] for game in ("k1", "k2")}
            path = lane_skips_file(root, table)
            patch = mock.patch.object(ports, "LANE_SKIPS_PATH", path)
            patch.start()
            self.addCleanup(patch.stop)
        if restore:
            args.restore_report = restore_report(root / "restore.json", args.guide.resolve(), FOUR_GB)
        commands = []

        def fake_run(command, source, private, stage, report, env):
            commands.append((stage, list(command)))
            if stage == "convert":
                (private / "converted.toml").write_text(converted or four_gb_document(), encoding="utf-8")
                if log:
                    convert_log(private / "convert.log", [
                        {"ComponentGuid": FOUR_GB_GUID.upper(), "ComponentName": "4GB Patcher",
                         "Success": False, "InstructionsGenerated": 0, "SkipReason": REJECTION},
                        {"ComponentGuid": "50000000-0000-0000-0000-000000000001",
                         "ComponentName": "Mod One", "Success": True, "InstructionsGenerated": 1,
                         "SkipReason": ""}])

        report = {"commands": []}
        error = None
        with mock.patch.object(ports, "run", reviewing(fake_run)):
            try:
                ports.execute(args, report)
            except ports.Blocked as blocked:
                error = str(blocked)
        return commands, report, error

    def deselected(self, command):
        return [value for flag, value in zip(command, command[1:]) if flag == "--deselect"]

    def test_both_games_skip_and_deselect_only_the_4gb_patcher_without_a_restore_report(self):
        for game in ("k1", "k2"):
            with self.subTest(game=game):
                commands, report, error = self.run_lane(game=game)
                self.assertIsNone(error)
                staged = dict(commands)
                for stage in ("validate", "install"):
                    self.assertEqual(self.deselected(staged[stage]), [FOUR_GB_GUID])
                self.assertNotIn("--deselect", staged["convert"])
                record, = report["skipped_by_reviewed_policy"]
                self.assertEqual((record["section"], record["heading"]), FOUR_GB)
                self.assertEqual(record["status"], "skipped-by-reviewed-policy")
                self.assertEqual(record["component_guid"], FOUR_GB_GUID)
                self.assertEqual(record["source"], "lane_skips")
                self.assertIn(REJECTION, record["reason"])
                self.assertEqual(report["component_count"], 3)

    def test_the_converter_rejection_is_reported_as_reviewed_policy_not_reader_rejection(self):
        _, report, error = self.run_lane()
        self.assertIsNone(error)
        self.assertEqual(report["convert_skips"]["by_cause"], {"skipped_by_reviewed_policy": 1})
        self.assertEqual(report["convert_skips"]["components"][0]["name"], "4GB Patcher")

    def test_the_skip_is_applied_even_when_the_converter_generated_instructions(self):
        commands, report, error = self.run_lane(converted=four_gb_document(instructed=True))
        self.assertIsNone(error)
        self.assertEqual(self.deselected(dict(commands)["install"]), [FOUR_GB_GUID])
        self.assertEqual(report["skipped_by_reviewed_policy"][0]["component_guid"], FOUR_GB_GUID)

    def test_a_restore_report_entry_for_the_same_step_is_reported_once_and_deselected_once(self):
        commands, report, error = self.run_lane(restore=True)
        self.assertIsNone(error)
        self.assertEqual(len(report["skipped_by_reviewed_policy"]), 1)
        self.assertEqual(self.deselected(dict(commands)["validate"]), [FOUR_GB_GUID])

    def test_the_table_does_not_exempt_any_other_step(self):
        _, report, error = self.run_lane(converted=four_gb_document(one_instructed=False))
        self.assertEqual(error, "guide_steps_without_instructions")
        self.assertEqual([step["heading"] for step in report["steps_without_instructions"]], ["Mod One"])

    def test_without_the_table_the_4gb_patcher_still_blocks_coverage(self):
        commands, report, error = self.run_lane(table=None)
        self.assertEqual(error, "guide_steps_without_instructions")
        self.assertEqual([step["heading"] for step in report["steps_without_instructions"]],
                         ["4GB Patcher"])
        self.assertNotIn("validate", dict(commands))

    def test_a_lane_skip_for_a_step_the_guide_lacks_blocks(self):
        _, _, error = self.run_lane(table={"k1": [lane_entry(heading="Renamed Step")]})
        self.assertEqual(error, "policy_skipped_step_not_in_guide")

    def test_a_lane_skip_is_scoped_to_its_section(self):
        _, _, error = self.run_lane(table={"k1": [lane_entry(section="Enhanced Widescreen Support Mods")]})
        self.assertEqual(error, "policy_skipped_step_not_in_guide")

    def test_a_lane_skip_for_the_other_game_is_ignored(self):
        _, _, error = self.run_lane(table={"k2": [lane_entry()]})
        self.assertEqual(error, "guide_steps_without_instructions")

    def test_a_dependent_component_blocks_the_lane_skip(self):
        _, _, error = self.run_lane(converted=four_gb_document(dependents=True))
        self.assertEqual(error, "policy_skipped_component_has_dependents")

    def test_rust_gui_deselects_the_lane_skip_in_validate_and_the_driver(self):
        commands, _, error = self.run_lane(mode="gui")
        self.assertEqual(error, "gui_completion_report_missing")
        staged = dict(commands)
        for stage in ("validate", "gui"):
            command = staged[stage]
            self.assertEqual([value for flag, value in zip(command, command[1:]) if flag == "--deselect"],
                             [FOUR_GB_GUID])
        self.assertIn("--exclude-widescreen", staged["gui"])

    def test_typescript_lanes_take_the_unpinned_lane_skip_and_ignore_rust_guid_pins(self):
        pinned = lane_entry(heading="Mod Two", guid="50000000-0000-0000-0000-000000000003",
                            kind="unreviewed_draft")
        root, _ = self.prepare("k1")
        path = lane_skips_file(root, {"k1": [lane_entry(), pinned]})
        self.assertEqual([skip["heading"] for skip in ports.load_lane_skips("k1", path)],
                         ["4GB Patcher", "Mod Two"])
        typescript = ports.load_lane_skips("k1", path, implementation="typescript")
        self.assertEqual([skip["heading"] for skip in typescript], ["4GB Patcher"])
        self.assertNotIn("guid", typescript[0])

    def test_typescript_lane_validation_still_covers_pinned_entries(self):
        root, _ = self.prepare("k1")
        path = lane_skips_file(root, {"k1": [lane_entry(guid="not-a-guid", kind="unreviewed_draft")]})
        with self.assertRaisesRegex(ports.Blocked, "lane_skips_invalid"):
            ports.load_lane_skips("k1", path, implementation="typescript")

    def test_malformed_table_blocks_the_rust_lane_before_validate(self):
        commands, _, error = self.run_lane(table={"k1": [lane_entry(reason="")]})
        self.assertEqual(error, "lane_skips_invalid")
        self.assertNotIn("validate", dict(commands))


GUIDE_PENDING = """# Guide

## Mod List

### Mod One

### Mod Two

### Mod Three

## Enhanced Widescreen Support Mods

### Wide One
"""

PENDING_ONE = "80000000-0000-0000-0000-000000000001"
PENDING_TWO = "80000000-0000-0000-0000-000000000002"
PENDING_THREE = "80000000-0000-0000-0000-000000000003"
PENDING_WIDE = "80000000-0000-0000-0000-000000000004"


def pending_entry(heading, guid, kind="unreviewed_draft", section="Mod List"):
    return {"section": section, "heading": heading, "guid": guid, "kind": kind,
            "reason": "unreviewed draft: deselected pending human review, delegated by owner 2026-10-18"}


TS_ONE = "40000000-0000-0000-0000-000000000001"
TS_TWO = "40000000-0000-0000-0000-000000000002"
TS_BRACED_DEPENDENCY = "{" + MOVIES_GUID.upper() + "}"


class TypeScriptPolicySkipTests(unittest.TestCase):
    prepare = ExecuteCommandTests.prepare

    def run_lane(self, mode="cli", converted=None, keys=(HELPER, MOVIES), table=None, hashes=None,
                 report_guide=None):
        root, args = self.prepare("k1")
        args.guide.write_text(GUIDE_WITH_HELPER, encoding="utf-8")
        args.implementation = "typescript"
        args.mode = mode
        args.review_manifest = root / "review.json"
        if keys is not None:
            args.restore_report = restore_report(
                root / "restore.json", (report_guide or args.guide).resolve(), *keys)
        if table is not None:
            path = lane_skips_file(root, table)
            patch = mock.patch.object(ports, "LANE_SKIPS_PATH", path)
            patch.start()
            self.addCleanup(patch.stop)
        commands = []

        def fake_run(command, source, private, stage, report, env):
            commands.append((stage, list(command)))
            if stage == "convert":
                (private / "converted.toml").write_text(converted or helper_document(), encoding="utf-8")
            if stage == "gui":
                raise ports.Blocked("gui_stopped_by_test")

        def fake_review(path, guide, converted_path):
            if hashes is not None:
                hashes.append(ports.digest(converted_path))
            return []

        report = {"commands": []}
        error = None
        with mock.patch.object(ports, "run", fake_run), \
                mock.patch.object(ports, "review_manifest", fake_review):
            try:
                ports.execute(args, report)
            except ports.Blocked as blocked:
                error = str(blocked)
        return args, commands, report, error

    def selected(self, command):
        return [value for flag, value in zip(command, command[1:]) if flag == "--select"]

    def test_cli_selects_exactly_the_installable_components_and_never_all_or_deselect(self):
        _, commands, report, error = self.run_lane()
        self.assertIsNone(error)
        installs = [(stage, command) for stage, command in commands if stage.startswith("install_")]
        self.assertEqual([stage for stage, _ in installs], ["install_base", "install_widescreen"])
        for stage, command in [("validate", dict(commands)["validate"])] + installs:
            self.assertEqual(self.selected(command), [TS_ONE + "," + TS_TWO], stage)
            self.assertNotIn("--all", command, stage)
            self.assertNotIn("--deselect", command, stage)
        self.assertEqual(report["installable_component_count"], 2)
        self.assertEqual([record["component_guid"] for record in report["skipped_by_reviewed_policy"]
                          if record["component_guid"]], [MOVIES_GUID])

    def test_without_skips_every_component_is_selected(self):
        _, commands, report, error = self.run_lane(
            keys=None, converted=helper_document(movies_instructions=True))
        self.assertIsNone(error)
        self.assertEqual(self.selected(dict(commands)["validate"]),
                         [TS_ONE + "," + TS_TWO + "," + MOVIES_GUID])
        self.assertEqual(report["skipped_by_reviewed_policy"], [])

    def test_the_review_hash_does_not_depend_on_the_policy_skips(self):
        hashes = []
        args, _, report, error = self.run_lane(
            hashes=hashes, converted=helper_document(movies_instructions=True))
        self.assertIsNone(error)
        converted = next(Path(args.work_dir).glob("ports-*/converted.toml"))
        self.assertEqual(hashes, [report["converted_sha256"]])
        self.assertEqual(report["converted_sha256"], ports.digest(converted))
        without = []
        self.run_lane(keys=None, hashes=without, converted=helper_document(movies_instructions=True))
        self.assertEqual(hashes, without)

    def test_policy_skips_are_unselected_in_the_gui_input_only(self):
        args, commands, report, error = self.run_lane(mode="gui")
        self.assertEqual(error, "gui_stopped_by_test")
        private = next(Path(args.work_dir).glob("ports-*"))
        staged = dict(commands)
        self.assertEqual(Path(staged["gui"][staged["gui"].index("--input") + 1]),
                         private / "converted-policy.toml")
        loaded = tomllib.loads((private / "converted-policy.toml").read_text(encoding="utf-8"))
        self.assertEqual({item["Name"]: item["IsSelected"] for item in loaded["thisMod"]},
                         {"Mod One": True, "Mod Two": True, "Loose Movies": False})
        original = tomllib.loads((private / "converted.toml").read_text(encoding="utf-8"))
        self.assertIs(original["thisMod"][2]["IsSelected"], True)
        self.assertEqual(report["converted_sha256"], ports.digest(private / "converted.toml"))

    def test_the_unpinned_lane_skip_applies_to_typescript_and_pinned_ones_do_not(self):
        converted = four_gb_document()
        pinned = lane_entry(heading="Mod Two", guid="50000000-0000-0000-0000-000000000003",
                            kind="unreviewed_draft")
        root, args = self.prepare("k1")
        args.guide.write_text(GUIDE_4GB, encoding="utf-8")
        args.implementation = "typescript"
        args.review_manifest = root / "review.json"
        patch = mock.patch.object(ports, "LANE_SKIPS_PATH",
                                  lane_skips_file(root, {"k1": [lane_entry(), pinned]}))
        patch.start()
        self.addCleanup(patch.stop)
        commands = []

        def fake_run(command, source, private, stage, report, env):
            commands.append((stage, list(command)))
            if stage == "convert":
                (private / "converted.toml").write_text(converted, encoding="utf-8")

        report = {"commands": []}
        with mock.patch.object(ports, "run", fake_run), \
                mock.patch.object(ports, "review_manifest", return_value=[]):
            ports.execute(args, report)
        self.assertEqual(self.selected(dict(commands)["validate"]),
                         ["50000000-0000-0000-0000-000000000001,50000000-0000-0000-0000-000000000003"])
        record, = report["skipped_by_reviewed_policy"]
        self.assertEqual((record["heading"], record["source"]), ("4GB Patcher", "lane_skips"))

    def test_a_dependent_written_in_braced_form_blocks_the_skip(self):
        converted = helper_document().replace(
            "IsSelected = false", 'IsSelected = false\nDependencies = ["' + TS_BRACED_DEPENDENCY + '"]', 1)
        _, commands, _, error = self.run_lane(converted=converted)
        self.assertEqual(error, "policy_skipped_component_has_dependents")
        self.assertNotIn("validate", dict(commands))

    def test_a_restore_report_for_another_guide_still_blocks(self):
        _, _, _, error = self.run_lane(report_guide=Path(tempfile.gettempdir()) / "other" / "full.md")
        self.assertEqual(error, "restore_report_guide_mismatch")


class ReviewManifestRegenerationTests(unittest.TestCase):
    def test_hash_mismatch_says_how_to_regenerate(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(root))
        (root / "k2").mkdir()
        guide = root / "k2" / "full.md"
        guide.write_text(GUIDE, encoding="utf-8")
        converted = root / "converted.toml"
        converted.write_text(document(component("Mod One", TS_ONE)), encoding="utf-8")
        manifest = root / "review.json"
        manifest.write_text(json.dumps({
            "schema_version": 1, "reviewer": "x", "unresolved": [],
            "guide_sha256": ports.digest(guide), "converted_sha256": "0" * 64,
            "components": []}), encoding="utf-8")
        with self.assertRaises(ports.Blocked) as caught:
            ports.review_manifest(manifest, guide, converted)
        message = str(caught.exception)
        self.assertTrue(message.startswith("review_manifest_hash_mismatch: converted_sha256 "))
        self.assertNotIn("guide_sha256 differ", message)
        for expected in ("MODSYNC_TS_REF", "tools/modsync-ci/gen_ts_review.py", "--game k2",
                         "ci/k2-typescript-review.json"):
            self.assertIn(expected, message)

    def test_component_phase_is_the_rule_review_manifest_checks(self):
        self.assertEqual(ports.component_phase({}), "base")
        self.assertEqual(ports.component_phase({"IsWidescreen": False}), "base")
        self.assertEqual(ports.component_phase({"IsWidescreen": True}), "widescreen")


class PendingReviewSkipLaneTests(unittest.TestCase):
    prepare = ExecuteCommandTests.prepare

    def blocks(self, three_depends_on_two=False, three_draft=True):
        three = drafted("Mod Three", PENDING_THREE) if three_draft else component("Mod Three", PENDING_THREE)
        if three_depends_on_two:
            three = three.replace("IsSelected = true", 'IsSelected = true\nDependencies = ["' + PENDING_TWO + '"]', 1)
        return document(
            drafted("Mod One", PENDING_ONE, required=False),
            drafted("Mod Two", PENDING_TWO),
            three,
            drafted("Wide One", PENDING_WIDE, widescreen=True))

    def run_lane(self, table, mode="cli", converted=None):
        root, args = self.prepare("k1")
        args.guide.write_text(GUIDE_PENDING, encoding="utf-8")
        args.implementation = "rust"
        args.mode = mode
        path = lane_skips_file(root, table)
        patch = mock.patch.object(ports, "LANE_SKIPS_PATH", path)
        patch.start()
        self.addCleanup(patch.stop)
        commands = []

        def fake_run(command, source, private, stage, report, env):
            commands.append((stage, list(command)))
            if stage == "convert":
                (private / "converted.toml").write_text(converted or self.blocks(), encoding="utf-8")
            if stage == ports.DRAFT_REVIEW_STAGE:
                (private / (stage + ".log")).write_text(
                    json.dumps({"cleared": [PENDING_ONE], "skipped": []}, indent=2) + "\n", encoding="utf-8")

        report = {"commands": []}
        error = None
        with mock.patch.object(ports, "run", reviewing(fake_run)):
            try:
                ports.execute(args, report)
            except ports.Blocked as blocked:
                error = str(blocked)
        return commands, report, error

    def deselected(self, command):
        return [value for flag, value in zip(command, command[1:]) if flag == "--deselect"]

    def all_pending(self):
        return {"k1": [pending_entry("Mod Two", PENDING_TWO), pending_entry("Mod Three", PENDING_THREE),
                       pending_entry("Wide One", PENDING_WIDE, section="Enhanced Widescreen Support Mods")]}

    def test_listed_unreviewed_drafts_pass_the_gate_and_are_deselected_for_validate_and_install(self):
        commands, report, error = self.run_lane(self.all_pending())
        self.assertIsNone(error)
        staged = dict(commands)
        expected = [PENDING_TWO, PENDING_THREE, PENDING_WIDE]
        for stage in ("validate", "install"):
            self.assertEqual(self.deselected(staged[stage]), expected)
        self.assertNotIn("unreviewed", report["draft_review"])
        self.assertEqual(report["draft_review"]["cleared"], 1)

    def test_a_new_unreviewed_draft_that_is_not_listed_still_blocks(self):
        table = self.all_pending()
        table["k1"] = [entry for entry in table["k1"] if entry["guid"] != PENDING_THREE]
        commands, report, error = self.run_lane(table)
        self.assertTrue(error.startswith("unreviewed_draft_instructions_remain: "))
        self.assertIn(PENDING_THREE, error)
        self.assertNotIn(PENDING_TWO, error)
        self.assertNotIn("validate", [stage for stage, _ in commands])
        self.assertEqual([item["component_guid"] for item in report["draft_review"]["unreviewed"]],
                         [PENDING_THREE])

    def test_the_report_and_the_log_list_the_count_and_names_of_deselected_components(self):
        _, report, error = self.run_lane(self.all_pending())
        self.assertIsNone(error)
        pending = report["deselected_pending_review"]
        self.assertEqual(pending["count"], 3)
        self.assertEqual(pending["by_kind"], {"unreviewed_draft": 3, "dependent_of_deselected": 0})
        self.assertEqual([(item["heading"], item["component_guid"]) for item in pending["components"]],
                         [("Mod Two", PENDING_TWO), ("Mod Three", PENDING_THREE), ("Wide One", PENDING_WIDE)])
        log = io.StringIO()
        with contextlib.redirect_stderr(log):
            ports.print_diagnostics(report)
        text = log.getvalue()
        self.assertIn("3 component(s) DESELECTED pending human review", text)
        for name, guid in (("Mod Two", PENDING_TWO), ("Mod Three", PENDING_THREE), ("Wide One", PENDING_WIDE)):
            self.assertIn("deselected pending review: " + name + " [" + guid + "]", text)

    def test_hand_authored_skips_are_not_counted_as_pending_review(self):
        report = {}
        ports.coverage(*self.coverage_inputs(), report, "rust", [
            {"section": "Mod List", "heading": "Mod Two", "reason": "reviewed", "urls": [],
             "source": "lane_skips"}])
        self.assertNotIn("deselected_pending_review", report)

    def coverage_inputs(self):
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, directory)
        guide = directory / "full.md"
        guide.write_text(GUIDE_PENDING, encoding="utf-8")
        converted = directory / "converted.toml"
        converted.write_text(document(
            component("Mod One", PENDING_ONE), component("Mod Two", PENDING_TWO),
            component("Mod Three", PENDING_THREE), component("Wide One", PENDING_WIDE)), encoding="utf-8")
        return guide, converted

    def test_a_dependent_that_is_not_deselected_blocks(self):
        table = {"k1": [pending_entry("Mod Two", PENDING_TWO)]}
        _, _, error = self.run_lane(table, converted=self.blocks(three_depends_on_two=True))
        self.assertEqual(error, "policy_skipped_component_has_dependents")

    def test_a_dependent_that_is_deselected_with_it_passes_and_is_reported_by_kind(self):
        table = {"k1": [pending_entry("Mod Two", PENDING_TWO),
                        pending_entry("Mod Three", PENDING_THREE, kind="dependent_of_deselected"),
                        pending_entry("Wide One", PENDING_WIDE,
                                      section="Enhanced Widescreen Support Mods")]}
        commands, report, error = self.run_lane(table, converted=self.blocks(three_depends_on_two=True))
        self.assertIsNone(error)
        self.assertEqual(report["deselected_pending_review"]["by_kind"],
                         {"unreviewed_draft": 2, "dependent_of_deselected": 1})
        self.assertEqual(self.deselected(dict(commands)["install"]), [PENDING_TWO, PENDING_THREE, PENDING_WIDE])

    def test_a_pinned_guid_that_differs_from_the_converted_component_blocks(self):
        table = self.all_pending()
        table["k1"][0] = pending_entry("Mod Two", "80000000-0000-0000-0000-0000000000ff")
        commands, _, error = self.run_lane(table)
        self.assertEqual(error, "policy_skipped_guid_mismatch")
        self.assertNotIn("validate", dict(commands))

    def test_gui_lane_reports_a_skipped_widescreen_draft_as_skipped_not_excluded(self):
        commands, report, error = self.run_lane(self.all_pending(), mode="gui")
        self.assertEqual(error, "gui_completion_report_missing")
        self.assertEqual(report["gui_not_covered"]["excluded_widescreen"], [])
        driver = dict(commands)["gui"]
        self.assertEqual(self.deselected(driver), [PENDING_TWO, PENDING_THREE, PENDING_WIDE])

    def test_gui_lane_without_the_widescreen_entry_excludes_it_and_still_passes_the_gate(self):
        table = self.all_pending()
        table["k1"] = table["k1"][:2]
        _, report, error = self.run_lane(table, mode="gui")
        self.assertEqual(error, "gui_completion_report_missing")
        self.assertEqual([item["component_guid"] for item in report["gui_not_covered"]["excluded_widescreen"]],
                         [PENDING_WIDE])

    def test_malformed_pins_block_the_table(self):
        good = pending_entry("Mod Two", PENDING_TWO)
        for entry in (
            {key: value for key, value in good.items() if key != "kind"},
            {key: value for key, value in good.items() if key != "guid"},
            {**good, "kind": "approved"},
            {**good, "guid": "80000000-0000-0000-0000-0000000000AB"},
            {**good, "guid": "not-a-guid"},
            {**good, "guid": 7},
        ):
            with self.subTest(entry=entry):
                path = lane_skips_file(Path(tempfile.mkdtemp()), {"k1": [entry]})
                self.addCleanup(__import__("shutil").rmtree, path.parent)
                with self.assertRaisesRegex(ports.Blocked, "lane_skips_invalid"):
                    ports.load_lane_skips("k1", path)

    def test_two_entries_pinning_one_guid_block_the_table(self):
        path = lane_skips_file(Path(tempfile.mkdtemp()), {"k1": [
            pending_entry("Mod Two", PENDING_TWO), pending_entry("Mod Three", PENDING_TWO)]})
        self.addCleanup(__import__("shutil").rmtree, path.parent)
        with self.assertRaisesRegex(ports.Blocked, "lane_skips_duplicate"):
            ports.load_lane_skips("k1", path)


class RealGuideFourGbSkipTests(unittest.TestCase):
    ROOT = Path(ports.__file__).resolve().parents[2] / "content"

    def converted_from_guide(self, directory, guide, pinned=()):
        section = None
        blocks = []
        self.guid = None
        pins = {(skip["section"], skip["heading"]): skip["guid"] for skip in pinned}
        for line in guide.read_text(encoding="utf-8").splitlines():
            if line.startswith("## "):
                section = line[3:].strip()
            elif line.startswith("### ") and section in (
                    "Mod List", "Optional Widescreen & Extended Widescreen Support Mods",
                    "Enhanced Widescreen Support Mods"):
                heading = line[4:].strip()
                guid = pins.get((section, heading), "70000000-0000-0000-0000-%012d" % len(blocks))
                if (section, heading) == FOUR_GB:
                    self.guid = guid
                    blocks.append("\n".join(["[[thisMod]]", 'Guid = "' + guid + '"',
                                             'Name = "' + heading + '"', "IsSelected = true"]))
                else:
                    blocks.append(component(heading.replace('"', "'"), guid))
        path = directory / "converted.toml"
        path.write_text(document(*blocks), encoding="utf-8")
        return path

    def test_the_4gb_patcher_is_the_only_hand_authored_exempt_step_in_both_real_guides(self):
        for game in ("k1", "k2"):
            with self.subTest(game=game):
                directory = Path(tempfile.mkdtemp())
                self.addCleanup(__import__("shutil").rmtree, directory)
                guide = self.ROOT / game / "full.md"
                converted = self.converted_from_guide(directory, guide)
                with self.assertRaisesRegex(ports.Blocked, "guide_steps_without_instructions"):
                    ports.coverage(guide, converted, {}, "rust", [])
                skips = [skip for skip in ports.load_lane_skips(guide.parent.name, SHIPPED_LANE_SKIPS)
                         if "guid" not in skip]
                report = {}
                self.assertTrue(ports.coverage(guide, converted, report, "rust", skips))
                record, = report["skipped_by_reviewed_policy"]
                self.assertEqual((record["section"], record["heading"], record["component_guid"]),
                                 (*FOUR_GB, self.guid))

    def test_every_shipped_skip_resolves_to_one_counted_step_with_its_pinned_guid(self):
        expected = {"k1": 151, "k2": 120}
        for game in ("k1", "k2"):
            with self.subTest(game=game):
                directory = Path(tempfile.mkdtemp())
                self.addCleanup(__import__("shutil").rmtree, directory)
                guide = self.ROOT / game / "full.md"
                skips = ports.load_lane_skips(game, SHIPPED_LANE_SKIPS)
                converted = self.converted_from_guide(
                    directory, guide, [skip for skip in skips if "guid" in skip])
                report = {}
                self.assertTrue(ports.coverage(guide, converted, report, "rust", skips))
                self.assertEqual(len(report["skipped_by_reviewed_policy"]), expected[game])
                self.assertTrue(all(record["component_guid"] for record in report["skipped_by_reviewed_policy"]))
                pending = report["deselected_pending_review"]
                self.assertEqual(pending["count"], expected[game] - 1)
                self.assertEqual(pending["count"], len(pending["components"]))


QUARANTINE_GUIDE = "# Guide\n\n## Mod List\n\n" + "".join(
    f"### Mod {number:02d}\n\n" for number in range(1, 21))


def quarantine_guid(number):
    return f"90000000-0000-0000-0000-{number:012d}"


def quarantine_document(count=20, dependencies=None, names=None):
    dependencies = dependencies or {}
    names = names or {}
    blocks = []
    for number in range(1, count + 1):
        extra = {"Heading": f'"Mod {number:02d}"'} if number in names else {}
        block = component(names.get(number, f"Mod {number:02d}"), quarantine_guid(number), **extra)
        if number in dependencies:
            listed = ", ".join(f'"{reference}"' for reference in dependencies[number])
            block = block.replace("IsSelected = false",
                                  "IsSelected = false\nDependencies = [" + listed + "]", 1)
        blocks.append(block)
    return document(*blocks)


def validate_error(name, step=2, action="Patcher", message="File blocks destination directory: /g/003ebo.mod"):
    return (f"Ebon Hawk Downloadable Map / 1: Extract\n"
            f"[2026-10-18 16:09:50] [Error] CLI command stopped\n"
            f"[2026-10-18 16:09:50] [Error] Exception: {name} / step {step} ({action}): {message}\n"
            f"Error: {name} / step {step} ({action}): {message}\n")


class ValidateQuarantineTests(unittest.TestCase):
    prepare = ExecuteCommandTests.prepare

    def run_lane(self, failures=(), mode="cli", converted=None, populated=True, max_quarantine=None,
                 max_fraction=None):
        """Run the Rust lane with a fake `run`; `failures` are the log texts of the successive validates."""
        root, args = self.prepare("k2")
        args.guide.write_text(QUARANTINE_GUIDE, encoding="utf-8")
        args.implementation = "rust"
        args.mode = mode
        if populated:
            (args.game_dir / "swkotor2.exe").write_text("x", encoding="utf-8")
        if max_quarantine is not None:
            args.max_quarantine = max_quarantine
        if max_fraction is not None:
            args.max_quarantine_fraction = max_fraction
        pending = list(failures)
        commands = []

        def fake_run(command, source, private, stage, report, env):
            commands.append((stage, list(command)))
            if stage == "convert":
                (private / "converted.toml").write_text(
                    converted or quarantine_document(), encoding="utf-8")
            if stage.startswith("validate") and pending:
                text = pending.pop(0)
                (private / (stage + ".log")).write_text(text, encoding="utf-8")
                report["stage"] = stage
                report["commands"].append({"stage": stage, "exit_code": 1})
                raise ports.Blocked(stage + "_failed")

        report = {"commands": []}
        error = None
        with mock.patch.object(ports, "run", reviewing(fake_run)):
            try:
                ports.execute(args, report)
            except ports.Blocked as blocked:
                error = str(blocked)
        self.assertEqual(pending, [], "every scripted validate failure was consumed")
        return commands, report, error

    def deselected(self, command):
        return [value for flag, value in zip(command, command[1:]) if flag == "--deselect"]

    def validates(self, commands):
        return [(stage, command) for stage, command in commands if stage.startswith("validate")]

    def test_a_passing_validate_runs_once_and_reports_an_empty_quarantine(self):
        commands, report, error = self.run_lane()
        self.assertIsNone(error)
        self.assertEqual([stage for stage, _ in self.validates(commands)], ["validate"])
        self.assertEqual(report["quarantined_validate_failures"], [])
        self.assertEqual(report["quarantine"]["attempts"], 1)
        self.assertEqual(report["quarantine"]["max_components"], 3)
        self.assertIsNone(report["quarantine"]["stopped"])
        self.assertNotIn("failure", report)

    def test_a_named_component_is_quarantined_and_validate_reruns_without_it(self):
        commands, report, error = self.run_lane([validate_error("Mod 05")])
        self.assertIsNone(error)
        validates = self.validates(commands)
        self.assertEqual([stage for stage, _ in validates], ["validate", "validate_retry_1"])
        self.assertEqual(self.deselected(validates[0][1]), [])
        self.assertEqual(self.deselected(validates[1][1]), [quarantine_guid(5)])
        entry, = report["quarantined_validate_failures"]
        self.assertEqual((entry["kind"], entry["name"], entry["component_guid"], entry["step"],
                          entry["action"], entry["stage"], entry["caused_by"]),
                         ("validate_failure", "Mod 05", quarantine_guid(5), 2, "Patcher", "validate", None))
        self.assertEqual(entry["error_line"],
                         "Error: Mod 05 / step 2 (Patcher): File blocks destination directory: /g/003ebo.mod")
        self.assertEqual(report["quarantine"]["attempts"], 2)
        self.assertNotIn("failure", report, "a later green validate clears the stale failure")
        self.assertEqual(report["reason"], "native_install_command_succeeded")

    def test_install_receives_every_quarantined_guid_after_the_policy_deselects(self):
        commands, report, error = self.run_lane(
            [validate_error("Mod 05"), validate_error("Mod 09", step=1, action="Extract", message="boom")])
        self.assertIsNone(error)
        staged = dict(commands)
        self.assertEqual(self.deselected(staged["install"]), [quarantine_guid(5), quarantine_guid(9)])
        self.assertEqual(self.deselected(staged["validate_retry_2"]), [quarantine_guid(5), quarantine_guid(9)])
        self.assertEqual([item["name"] for item in report["quarantined_validate_failures"]],
                         ["Mod 05", "Mod 09"])
        self.assertEqual(report["reason"], "native_install_command_succeeded")

    def test_transitive_dependents_are_quarantined_with_it_and_reported_as_dependents(self):
        converted = quarantine_document(dependencies={
            7: ["{" + quarantine_guid(5).upper() + "}"], 8: [quarantine_guid(7)], 11: [quarantine_guid(2)]})
        commands, report, error = self.run_lane([validate_error("Mod 05")], converted=converted)
        self.assertIsNone(error)
        self.assertEqual(self.deselected(dict(commands)["install"]),
                         [quarantine_guid(5), quarantine_guid(7), quarantine_guid(8)])
        entries = report["quarantined_validate_failures"]
        self.assertEqual([(item["kind"], item["component_guid"], item["caused_by"]) for item in entries],
                         [("validate_failure", quarantine_guid(5), None),
                          ("dependent_of_quarantined", quarantine_guid(7), quarantine_guid(5)),
                          ("dependent_of_quarantined", quarantine_guid(8), quarantine_guid(5))])
        self.assertIsNone(entries[1]["step"])

    def test_the_component_cap_counts_dependents_and_hard_fails_without_quarantining(self):
        converted = quarantine_document(dependencies={
            7: [quarantine_guid(5)], 8: [quarantine_guid(5)], 9: [quarantine_guid(5)]})
        commands, report, error = self.run_lane([validate_error("Mod 05")], converted=converted)
        self.assertEqual(error, "validate_quarantine_cap_exceeded")
        self.assertEqual([stage for stage, _ in self.validates(commands)], ["validate"])
        self.assertEqual(report["quarantined_validate_failures"], [])
        self.assertEqual(report["quarantine"]["stopped"]["reason"], "component_cap_exceeded")
        self.assertEqual(report["quarantine"]["stopped"]["component"], "Mod 05")
        self.assertEqual(report["failure"]["component"], "Mod 05")
        self.assertNotIn("install", dict(commands))

    def test_the_fraction_cap_is_of_the_selected_components_not_the_whole_guide(self):
        root, args = self.prepare("k2")
        self.assertEqual(ports.QUARANTINE_MAX_FRACTION, 0.15)
        self.assertEqual(ports.QUARANTINE_MAX_ITERATIONS, 15)
        converted = root / "converted.toml"
        converted.write_text(quarantine_document(dependencies={3: [quarantine_guid(2)]}), encoding="utf-8")
        game = root / "game"
        (game / "swkotor2.exe").write_text("x", encoding="utf-8")
        (root / "work").mkdir()
        deselect = [flag for number in range(11, 21) for flag in ("--deselect", quarantine_guid(number))]
        report = {"commands": [], "component_count": 20}

        def failing(command, source, private, stage, report, env):
            (private / (stage + ".log")).write_text(validate_error("Mod 02"), encoding="utf-8")
            raise ports.Blocked(stage + "_failed")

        with mock.patch.object(ports, "run", failing):
            with self.assertRaisesRegex(ports.Blocked, "validate_quarantine_cap_exceeded"):
                ports.validate_with_quarantine(
                    ["modsync"], [], deselect, converted, game, root / "source", root / "work", report, {})
        self.assertEqual(report["quarantine"]["selected_components"], 10)
        self.assertEqual(report["quarantine"]["max_components"], 1)

    def test_the_iteration_cap_stops_the_loop_and_keeps_what_was_quarantined(self):
        failures = [validate_error("Mod 01"), validate_error("Mod 02"), validate_error("Mod 03")]
        commands, report, error = self.run_lane(failures, max_quarantine=2)
        self.assertEqual(error, "validate_quarantine_cap_exceeded")
        self.assertEqual([stage for stage, _ in self.validates(commands)],
                         ["validate", "validate_retry_1", "validate_retry_2"])
        self.assertEqual([item["name"] for item in report["quarantined_validate_failures"]],
                         ["Mod 01", "Mod 02"])
        self.assertEqual(report["quarantine"]["stopped"]["reason"], "iteration_cap_exceeded")
        self.assertEqual(report["failure"]["stage"], "validate_retry_2")
        self.assertEqual(report["failure"]["component"], "Mod 03")

    def test_the_default_cap_never_exceeds_fifteen_percent_of_the_selected_components(self):
        failures = [validate_error(f"Mod {number:02d}") for number in range(1, 5)]
        commands, report, error = self.run_lane(failures)
        self.assertEqual(error, "validate_quarantine_cap_exceeded")
        self.assertEqual(len(report["quarantined_validate_failures"]), 3)
        self.assertEqual(report["quarantine"]["stopped"]["component"], "Mod 04")

    def test_a_widened_fraction_allows_more(self):
        failures = [validate_error(f"Mod {number:02d}") for number in range(1, 5)]
        _, report, error = self.run_lane(failures, max_fraction=0.25)
        self.assertIsNone(error)
        self.assertEqual(len(report["quarantined_validate_failures"]), 4)

    def test_zero_disables_quarantine(self):
        commands, report, error = self.run_lane([validate_error("Mod 05")], max_quarantine=0)
        self.assertEqual(error, "validate_failed")
        self.assertEqual(report["quarantine"]["stopped"]["reason"], "quarantine_disabled")
        self.assertEqual(report["quarantined_validate_failures"], [])

    def test_an_error_that_names_no_component_hard_fails(self):
        for text in ("Error: No components are selected\n", "thread panicked at src/main.rs:1\n", ""):
            with self.subTest(text=text):
                commands, report, error = self.run_lane([text])
                self.assertEqual(error, "validate_failed")
                self.assertEqual([stage for stage, _ in self.validates(commands)], ["validate"])
                self.assertEqual(report["quarantine"]["stopped"]["reason"], "error_not_attributable")
                self.assertEqual(report["quarantined_validate_failures"], [])
                self.assertIsNone(report["failure"]["component"])
                self.assertEqual(report["failure"]["stage"], "validate")
                self.assertNotIn("install", dict(commands))

    def test_a_name_that_matches_no_component_hard_fails(self):
        _, report, error = self.run_lane([validate_error("Not In The Guide")])
        self.assertEqual(error, "validate_failed")
        self.assertEqual(report["quarantine"]["stopped"]["reason"], "component_unknown")
        self.assertEqual(report["quarantine"]["stopped"]["component"], "Not In The Guide")

    def test_a_name_shared_by_two_components_hard_fails(self):
        converted = quarantine_document(names={6: "Mod 05"})
        _, report, error = self.run_lane([validate_error("Mod 05")], converted=converted)
        self.assertEqual(error, "validate_failed")
        self.assertEqual(report["quarantine"]["stopped"]["reason"], "component_ambiguous")

    def test_a_deselected_component_failing_again_hard_fails_instead_of_looping(self):
        _, report, error = self.run_lane([validate_error("Mod 05"), validate_error("Mod 05")])
        self.assertEqual(error, "validate_failed")
        self.assertEqual(report["quarantine"]["stopped"]["reason"], "component_already_deselected")
        self.assertEqual(len(report["quarantined_validate_failures"]), 1)

    def test_a_missing_game_hard_fails(self):
        _, report, error = self.run_lane([validate_error("Mod 05")], populated=False)
        self.assertEqual(error, "validate_failed")
        self.assertEqual(report["quarantine"]["stopped"]["reason"], "game_missing")
        _, report, error = self.run_lane([validate_error(
            "Mod 05", step=1, action="Move", message="game directory not found: /g")])
        self.assertEqual(report["quarantine"]["stopped"]["reason"], "game_missing")
        self.assertEqual(report["quarantined_validate_failures"], [])

    def test_the_component_name_may_differ_in_case(self):
        _, report, error = self.run_lane([validate_error("mod 05")])
        self.assertIsNone(error)
        self.assertEqual(report["quarantined_validate_failures"][0]["component_guid"], quarantine_guid(5))

    def test_problems_before_validate_are_never_quarantined(self):
        root, args = self.prepare("k2")
        args.guide.write_text(QUARANTINE_GUIDE, encoding="utf-8")
        args.implementation = "rust"
        (args.game_dir / "swkotor2.exe").write_text("x", encoding="utf-8")
        commands = []

        def fake_run(command, source, private, stage, report, env):
            commands.append(stage)
            if stage == "convert":
                (private / "converted.toml").write_text(quarantine_document(count=19), encoding="utf-8")

        report = {"commands": []}
        with mock.patch.object(ports, "run", reviewing(fake_run)):
            with self.assertRaisesRegex(ports.Blocked, "guide_component_coverage_mismatch"):
                ports.execute(args, report)
        self.assertNotIn("validate", commands)
        self.assertNotIn("quarantined_validate_failures", report)

    def test_the_gui_lane_deselects_quarantined_components_in_the_driver_and_counts_them_as_skipped(self):
        commands, report, error = self.run_lane([validate_error("Mod 05")], mode="gui")
        self.assertEqual(error, "gui_completion_report_missing")
        staged = dict(commands)
        self.assertEqual(self.deselected(staged["validate_retry_1"]), [quarantine_guid(5)])
        self.assertEqual(self.deselected(staged["gui"]), [quarantine_guid(5)])

    def run_helper_typescript_lane(self, log, populated):
        root, args = self.prepare("k1")
        args.max_quarantine_fraction = 0.5
        args.guide.write_text(GUIDE_WITH_HELPER, encoding="utf-8")
        args.implementation = "typescript"
        args.review_manifest = root / "review.json"
        if populated:
            (args.game_dir / "chitin.key").write_text("x", encoding="utf-8")
        commands = []

        def fake_run(command, source, private, stage, report, env):
            commands.append(stage)
            if stage == "convert":
                (private / "converted.toml").write_text(helper_document(movies_instructions=True),
                                                        encoding="utf-8")
            if stage == "validate":
                (private / "validate.log").write_text(log, encoding="utf-8")
                raise ports.Blocked("validate_failed")

        report = {"commands": []}
        error = None
        with mock.patch.object(ports, "run", fake_run), \
                mock.patch.object(ports, "review_manifest", lambda *arguments: []):
            try:
                ports.execute(args, report)
            except ports.Blocked as blocked:
                error = str(blocked)
        return commands, report, error

    def test_the_typescript_lane_quarantines_only_a_single_attributed_failure_in_a_populated_game(self):
        commands, report, error = self.run_helper_typescript_lane(validate_error("Mod One"), populated=True)
        self.assertNotEqual(error, "validate_failed")
        self.assertIn("validate_retry_1", commands)
        self.assertEqual([item["name"] for item in report["quarantined_validate_failures"]], ["Mod One"])

        for log, populated, reason in (
                (validate_error("Mod One"), False, "game_missing"),
                ("Error: the converted document could not be read\n", True, "error_not_attributable")):
            with self.subTest(reason=reason):
                commands, report, error = self.run_helper_typescript_lane(log, populated)
                self.assertEqual(error, "validate_failed")
                self.assertEqual([stage for stage in commands if stage.startswith("validate")], ["validate"])
                self.assertEqual(report["quarantined_validate_failures"], [])
                self.assertEqual(report["quarantine"]["stopped"]["reason"], reason)

    def test_the_log_and_the_report_list_every_quarantined_component_as_warnings(self):
        converted = quarantine_document(dependencies={7: [quarantine_guid(5)]})
        _, report, error = self.run_lane([validate_error("Mod 05")], converted=converted)
        self.assertIsNone(error)
        log = io.StringIO()
        with contextlib.redirect_stderr(log):
            ports.print_diagnostics(report)
        text = log.getvalue()
        self.assertIn("::warning::ports: 2 component(s) QUARANTINED after validate failures "
                      "(1 failing, 1 dependent(s))", text)
        self.assertIn("PARTIAL install", text)
        self.assertIn("::warning::ports: quarantined Mod 05 [" + quarantine_guid(5)
                      + "]: validate failed at step 2 (Patcher): Error: Mod 05 / step 2 (Patcher):", text)
        self.assertIn("::warning::ports: quarantined Mod 07 [" + quarantine_guid(7)
                      + "]: depends on quarantined " + quarantine_guid(5), text)
        self.assertNotIn("::error::", text)

    def test_a_refused_quarantine_is_an_error_line_with_the_component_and_step(self):
        _, report, _ = self.run_lane([validate_error("Not In The Guide")])
        report["status"] = "blocked"
        log = io.StringIO()
        with contextlib.redirect_stderr(log):
            ports.print_diagnostics(report)
        text = log.getvalue()
        self.assertIn("::error::ports: validate failure not quarantined (component_unknown): Error: Not In", text)
        self.assertIn("::error::ports: validate failed (exit 1): component 'Not In The Guide', step 2 (Patcher)",
                      text)


def install_error(name, step=2, action="Patcher",
                  message="Built-in OdyPatcher reported errors; restore before retrying"):
    prefix = "Install stopped. Restore before retrying; snapshot: /tmp/snapshots/run 1: "
    return (f"Component A / 1: Extract\n"
            f"[2026-10-18 16:09:50] [Error] Install stopped\n"
            f"[2026-10-18 16:09:50] [Error] Exception: {prefix}{name} / step {step} ({action}): {message}\n"
            f"Error: {prefix}{name} / step {step} ({action}): {message}\n")


class InstallFailureParserTests(unittest.TestCase):
    PREFIX = "Install stopped. Restore before retrying; snapshot: /tmp/snap shots/run 1: "

    def test_the_install_stopped_prefix_is_not_part_of_the_component_name(self):
        for line in (f"Error: {self.PREFIX}Component A / step 2 (Patcher): Built-in OdyPatcher reported errors; x",
                     f"[2026-10-18 16:09:50] [Error] Exception: {self.PREFIX}Component A / step 2 (Patcher): "
                     "Built-in OdyPatcher reported errors; x",
                     f"\x1b[31mError: {self.PREFIX}Component A / step 2 (Patcher): "
                     "Built-in OdyPatcher reported errors; x\x1b[0m"):
            with self.subTest(line=line):
                parsed = ports.parse_component_failure(line + "\n")
                self.assertEqual((parsed["component"], parsed["step"], parsed["action"]),
                                 ("Component A", 2, "Patcher"))
                self.assertEqual(parsed["message"], "Built-in OdyPatcher reported errors; x")

    def test_a_component_name_with_a_colon_and_the_plain_form_are_unchanged(self):
        parsed = ports.parse_component_failure(
            f"Error: {self.PREFIX}Component A: Part 2 / step 1 (Move): boom\n")
        self.assertEqual(parsed["component"], "Component A: Part 2")
        self.assertEqual(ports.parse_component_failure("Error: Component B / step 3 (Copy): x\n")["component"],
                         "Component B")

    def test_every_distinct_failure_line_is_listed_once_in_order(self):
        text = install_error("Mod 05") + install_error("Mod 06", step=1, action="Move", message="boom")
        parsed = ports.parse_component_failures(text)
        self.assertEqual([(item["component"], item["step"]) for item in parsed],
                         [("Mod 05", 2), ("Mod 06", 1)])
        self.assertEqual(ports.parse_component_failures("Error: nothing here\n"), [])


class InstallQuarantineTests(unittest.TestCase):
    prepare = ExecuteCommandTests.prepare

    def run_lane(self, validate_failures=(), install_failures=(), converted=None, count=20, chitin=True,
                 max_quarantine=None, max_fraction=None, mode="cli"):
        """Run the Rust lane with a fake `run`; each failure is a log text or a (log text, exit code)."""
        root, args = self.prepare("k2")
        args.guide.write_text("# Guide\n\n## Mod List\n\n" + "".join(
            f"### Mod {number:02d}\n\n" for number in range(1, count + 1)), encoding="utf-8")
        args.implementation = "rust"
        args.mode = mode
        game = args.game_dir.resolve()
        (game / "swkotor2.exe").write_text("x", encoding="utf-8")
        if chitin:
            (game / "chitin.key").write_text("key", encoding="utf-8")
        (game / "modules").mkdir()
        (game / "modules" / "a.mod").write_text("module", encoding="utf-8")
        (game / "override").mkdir()
        (game / "override" / "keep.2da").write_text("0123456789", encoding="utf-8")
        self.initial = ports.tree_manifest(game)
        if max_quarantine is not None:
            args.max_quarantine = max_quarantine
        if max_fraction is not None:
            args.max_quarantine_fraction = max_fraction
        pending = {"validate": list(validate_failures), "install": list(install_failures)}
        commands = []
        self.seen = {}

        def fake_run(command, source, private, stage, report, env):
            commands.append((stage, list(command)))
            self.private = private
            reviewed = private / "converted.toml"
            self.seen[stage] = {
                "tree": ports.tree_manifest(game),
                "converted": hashlib.sha256(reviewed.read_bytes()).hexdigest() if reviewed.is_file() else None,
                "baseline": (private / "game-baseline").is_dir()}
            if stage == "convert":
                reviewed.write_text(converted or quarantine_document(count), encoding="utf-8")
            for phase, queue in pending.items():
                if stage.startswith(phase) and queue:
                    text, code = queue.pop(0) if isinstance(queue[0], tuple) else (queue.pop(0), 1)
                    if phase == "install":
                        (game / "override" / "partial.2da").write_text("half", encoding="utf-8")
                        (game / "modules" / "a.mod").write_text("changed module", encoding="utf-8")
                        (game / "override" / "keep.2da").unlink()
                        (game / "newdir").mkdir(exist_ok=True)
                        (game / "newdir" / "x.bin").write_text("x", encoding="utf-8")
                    (private / (stage + ".log")).write_text(text, encoding="utf-8")
                    report["stage"] = stage
                    report["commands"].append({"stage": stage, "exit_code": code})
                    raise ports.Blocked(stage + "_failed")

        report = {"commands": []}
        error = None
        with mock.patch.object(ports, "run", reviewing(fake_run)):
            try:
                ports.execute(args, report)
            except ports.Blocked as blocked:
                error = str(blocked)
        self.assertEqual(pending, {"validate": [], "install": []}, "every scripted failure was consumed")
        self.game = game
        return commands, report, error

    def deselected(self, command):
        return [value for flag, value in zip(command, command[1:]) if flag == "--deselect"]

    def installs(self, commands):
        return [(stage, command) for stage, command in commands if stage.startswith("install")]

    def test_a_passing_install_runs_once_and_reports_an_empty_install_quarantine(self):
        commands, report, error = self.run_lane()
        self.assertIsNone(error)
        self.assertEqual([stage for stage, _ in self.installs(commands)], ["install"])
        self.assertEqual(report["quarantined_install_failures"], [])
        self.assertEqual(report["quarantine"]["install_attempts"], 1)
        self.assertEqual(report["quarantine"]["baseline"]["status"], "copied")
        self.assertEqual(report["quarantine"]["baseline"]["files"], 4)
        self.assertTrue(self.seen["install"]["baseline"], "the baseline exists before the first install")
        self.assertFalse((self.private / "game-baseline").exists(), "the baseline is dropped once installed")
        self.assertEqual(report["reason"], "native_install_command_succeeded")

    def test_a_single_failure_restores_the_game_and_retries_without_the_component(self):
        commands, report, error = self.run_lane(install_failures=[install_error("Mod 05")])
        self.assertIsNone(error)
        installs = self.installs(commands)
        self.assertEqual([stage for stage, _ in installs], ["install", "install_retry_1"])
        self.assertEqual(self.deselected(installs[0][1]), [])
        self.assertEqual(self.deselected(installs[1][1]), [quarantine_guid(5)])
        self.assertEqual(self.seen["install_retry_1"]["tree"], self.initial,
                         "the game tree equals the baseline when the retry starts")
        self.assertTrue(self.seen["install_retry_1"]["baseline"])
        self.assertEqual(ports.tree_manifest(self.game), self.initial)
        self.assertEqual((self.game / "override" / "keep.2da").read_text(encoding="utf-8"), "0123456789")
        self.assertFalse((self.game / "override" / "partial.2da").exists())
        entry, = report["quarantined_install_failures"]
        self.assertEqual((entry["kind"], entry["name"], entry["component_guid"], entry["step"],
                          entry["action"], entry["stage"], entry["caused_by"]),
                         ("install_failure", "Mod 05", quarantine_guid(5), 2, "Patcher", "install", None))
        self.assertTrue(entry["error_line"].startswith("Error: Install stopped. Restore before retrying;"))
        self.assertIn("Mod 05 / step 2 (Patcher)", entry["error_line"])
        self.assertEqual(report["quarantined_validate_failures"], [])
        self.assertEqual(report["quarantine"]["install_attempts"], 2)
        self.assertIsNone(report["quarantine"]["stopped"])
        self.assertNotIn("failure", report, "a later green install clears the stale failure")
        self.assertEqual(report["reason"], "native_install_command_succeeded")

    def test_converted_toml_is_never_touched_by_the_quarantine(self):
        _, report, error = self.run_lane(install_failures=[install_error("Mod 05")])
        self.assertIsNone(error)
        digests = {item["converted"] for stage, item in self.seen.items()
                   if stage.startswith("install") or stage == "validate"}
        self.assertEqual(len(digests), 1)
        self.assertEqual(hashlib.sha256((self.private / "converted.toml").read_bytes()).hexdigest(),
                         digests.pop())
        self.assertEqual(report["converted_sha256"], hashlib.sha256(
            (self.private / "converted.toml").read_bytes()).hexdigest())

    def test_transitive_dependents_are_quarantined_with_it(self):
        converted = quarantine_document(dependencies={
            7: ["{" + quarantine_guid(5).upper() + "}"], 8: [quarantine_guid(7)], 11: [quarantine_guid(2)]})
        commands, report, error = self.run_lane(
            install_failures=[install_error("Mod 05")], converted=converted, max_fraction=0.25)
        self.assertIsNone(error)
        self.assertEqual(self.deselected(dict(commands)["install_retry_1"]),
                         [quarantine_guid(5), quarantine_guid(7), quarantine_guid(8)])
        entries = report["quarantined_install_failures"]
        self.assertEqual([(item["kind"], item["component_guid"], item["caused_by"], item["stage"])
                          for item in entries],
                         [("install_failure", quarantine_guid(5), None, "install"),
                          ("dependent_of_quarantined", quarantine_guid(7), quarantine_guid(5), "install"),
                          ("dependent_of_quarantined", quarantine_guid(8), quarantine_guid(5), "install")])

    def test_validate_and_install_quarantines_accumulate_into_the_retried_install(self):
        commands, report, error = self.run_lane(
            validate_failures=[validate_error("Mod 02")], install_failures=[install_error("Mod 05")])
        self.assertIsNone(error)
        self.assertEqual(self.deselected(dict(commands)["install"]), [quarantine_guid(2)])
        self.assertEqual(self.deselected(dict(commands)["install_retry_1"]),
                         [quarantine_guid(2), quarantine_guid(5)])
        self.assertEqual(len(ports.quarantined_entries(report)), 2)

    def test_the_component_cap_is_shared_with_validate(self):
        dependencies = {number: [quarantine_guid(1)] for number in (2,)}
        dependencies.update({number: [quarantine_guid(5)] for number in range(6, 11)})
        commands, report, error = self.run_lane(
            validate_failures=[validate_error("Mod 01")], install_failures=[install_error("Mod 05")],
            converted=quarantine_document(45, dependencies), count=45)
        self.assertEqual(report["quarantine"]["max_components"], 6)
        self.assertEqual(error, "install_quarantine_cap_exceeded")
        self.assertEqual(len(report["quarantined_validate_failures"]), 2)
        self.assertEqual(report["quarantined_install_failures"], [])
        self.assertEqual(report["quarantine"]["stopped"]["reason"], "component_cap_exceeded")
        self.assertEqual(report["quarantine"]["stopped"]["phase"], "install")
        self.assertEqual([stage for stage, _ in self.installs(commands)], ["install"])

    def test_the_same_failures_fit_when_the_shared_total_stays_within_the_cap(self):
        dependencies = {2: [quarantine_guid(1)], 6: [quarantine_guid(5)], 7: [quarantine_guid(5)]}
        commands, report, error = self.run_lane(
            validate_failures=[validate_error("Mod 01")], install_failures=[install_error("Mod 05")],
            converted=quarantine_document(45, dependencies), count=45)
        self.assertIsNone(error)
        self.assertEqual(len(ports.quarantined_entries(report)), 5)

    def test_the_iteration_cap_is_shared_with_validate(self):
        failures = [install_error("Mod 02"), install_error("Mod 03")]
        commands, report, error = self.run_lane(
            validate_failures=[validate_error("Mod 01")], install_failures=failures, max_quarantine=2)
        self.assertEqual(error, "install_quarantine_cap_exceeded")
        self.assertEqual([stage for stage, _ in self.installs(commands)], ["install", "install_retry_1"])
        self.assertEqual([item["name"] for item in report["quarantined_install_failures"]], ["Mod 02"])
        self.assertEqual(report["quarantine"]["stopped"]["reason"], "iteration_cap_exceeded")
        self.assertEqual(report["failure"]["stage"], "install_retry_1")
        self.assertEqual(report["failure"]["component"], "Mod 03")

    def test_hard_failures_never_quarantine_and_say_why(self):
        both = install_error("Mod 05") + install_error("Mod 06")
        cases = [
            ("panic_detected", "thread 'main' panicked at src/main.rs:1\n" + install_error("Mod 05")),
            ("panic_detected", "stack backtrace:\n" + install_error("Mod 05")),
            ("panic_detected", (install_error("Mod 05"), 101)),
            ("rollback_failed", install_error("Mod 05") + "Rollback FAILED: file busy\n"),
            ("multiple_components_failed", both),
            ("error_not_attributable", "Error: No components are selected\n"),
            ("error_not_attributable", ""),
            ("action_not_quarantinable", install_error("Mod 05", action="Execute")),
            ("action_not_quarantinable", install_error("Mod 05", action="Choose")),
            ("component_unknown", install_error("Not In The Guide")),
        ]
        for reason, failure in cases:
            with self.subTest(reason=reason, failure=failure):
                commands, report, error = self.run_lane(install_failures=[failure])
                self.assertEqual(error, "install_failed")
                self.assertEqual(report["quarantine"]["stopped"]["reason"], reason)
                self.assertEqual(report["quarantine"]["stopped"]["phase"], "install")
                self.assertEqual(report["quarantined_install_failures"], [])
                self.assertEqual([stage for stage, _ in self.installs(commands)], ["install"])
                self.assertEqual(report["failure"]["stage"], "install")
                self.assertNotEqual(ports.tree_manifest(self.game), self.initial, "no restore happened")

    def test_the_same_step_named_twice_is_still_one_failure(self):
        _, report, error = self.run_lane(
            install_failures=[install_error("Mod 05") + install_error("Mod 05", message="again")])
        self.assertIsNone(error)
        self.assertEqual(len(report["quarantined_install_failures"]), 1)

    def test_a_shared_name_hard_fails_as_ambiguous(self):
        _, report, error = self.run_lane(
            install_failures=[install_error("Mod 05")], converted=quarantine_document(names={6: "Mod 05"}))
        self.assertEqual(error, "install_failed")
        self.assertEqual(report["quarantine"]["stopped"]["reason"], "component_ambiguous")

    def test_a_component_already_deselected_by_validate_hard_fails_instead_of_looping(self):
        _, report, error = self.run_lane(
            validate_failures=[validate_error("Mod 05")], install_failures=[install_error("Mod 05")])
        self.assertEqual(error, "install_failed")
        self.assertEqual(report["quarantine"]["stopped"]["reason"], "component_already_deselected")
        self.assertEqual(report["quarantined_install_failures"], [])

    def test_a_game_directory_without_chitin_key_hard_fails(self):
        commands, report, error = self.run_lane(install_failures=[install_error("Mod 05")], chitin=False)
        self.assertEqual(error, "install_failed")
        self.assertEqual(report["quarantine"]["stopped"]["reason"], "game_missing")
        _, report, error = self.run_lane(install_failures=[install_error(
            "Mod 05", step=1, action="Move", message="game directory not found: /g")])
        self.assertEqual(report["quarantine"]["stopped"]["reason"], "game_missing")
        self.assertEqual(report["quarantined_install_failures"], [])

    def test_every_allowed_action_is_quarantinable(self):
        for action in ("Patcher", "Extract", "Move", "Copy", "Rename", "Delete"):
            with self.subTest(action=action):
                _, report, error = self.run_lane(install_failures=[install_error("Mod 05", action=action)])
                self.assertIsNone(error)
                self.assertEqual(report["quarantined_install_failures"][0]["action"], action)

    def test_a_failed_baseline_copy_runs_the_install_but_cannot_quarantine(self):
        real = ports.copy_tree

        def failing(source, destination):
            if destination.name == "game-baseline":
                raise OSError("no space left")
            return real(source, destination)

        with mock.patch.object(ports, "copy_tree", failing):
            commands, report, error = self.run_lane()
        self.assertIsNone(error)
        self.assertEqual(report["quarantine"]["baseline"], {"status": "copy_failed"})
        self.assertFalse((self.private / "game-baseline").exists())
        log = io.StringIO()
        with contextlib.redirect_stderr(log):
            ports.print_diagnostics(report)
        self.assertIn("::warning::ports: the game directory baseline copy failed", log.getvalue())
        with mock.patch.object(ports, "copy_tree", failing):
            commands, report, error = self.run_lane(install_failures=[install_error("Mod 05")])
        self.assertEqual(error, "install_failed")
        self.assertEqual(report["quarantine"]["stopped"]["reason"], "baseline_copy_failed")
        self.assertEqual([stage for stage, _ in self.installs(commands)], ["install"])

    def test_a_failed_restore_hard_fails_and_quarantines_nothing(self):
        with mock.patch.object(ports, "restore_game", side_effect=OSError("busy")):
            commands, report, error = self.run_lane(install_failures=[install_error("Mod 05")])
        self.assertEqual(error, "install_failed")
        self.assertEqual(report["quarantine"]["stopped"]["reason"], "restore_failed")
        self.assertEqual(report["quarantined_install_failures"], [])
        self.assertEqual([stage for stage, _ in self.installs(commands)], ["install"])

    def test_a_restore_that_does_not_match_the_baseline_hard_fails(self):
        real = ports.copy_tree

        def lossy(source, destination):
            real(source, destination)
            if destination.name != "game-baseline":
                (destination / "modules" / "a.mod").write_text("short", encoding="utf-8")

        with mock.patch.object(ports, "copy_tree", lossy):
            _, report, error = self.run_lane(install_failures=[install_error("Mod 05")])
        self.assertEqual(error, "install_failed")
        self.assertEqual(report["quarantine"]["stopped"]["reason"], "restore_failed")
        self.assertEqual(report["quarantined_install_failures"], [])

    def test_zero_disables_install_quarantine_and_takes_no_baseline(self):
        commands, report, error = self.run_lane(install_failures=[install_error("Mod 05")], max_quarantine=0)
        self.assertEqual(error, "install_failed")
        self.assertEqual(report["quarantine"]["stopped"]["reason"], "quarantine_disabled")
        self.assertNotIn("baseline", report["quarantine"])
        self.assertFalse(self.seen["install"]["baseline"])

    def test_the_gui_lane_never_runs_the_install_quarantine(self):
        commands, report, error = self.run_lane(mode="gui")
        self.assertEqual(error, "gui_completion_report_missing")
        self.assertEqual(self.installs(commands), [])
        self.assertNotIn("quarantined_install_failures", report)

    def test_the_baseline_is_an_independent_copy_and_restore_works_in_place(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, root)
        game = root / "game"
        (game / "sub" / "deep").mkdir(parents=True)
        (game / "a.bin").write_bytes(b"abc")
        (game / "sub" / "deep" / "b.bin").write_bytes(b"defgh")
        (game / "link").symlink_to("a.bin")
        report = {"quarantine": {}}
        baseline, manifest = ports.take_baseline(game, root, report)
        self.assertEqual(report["quarantine"]["baseline"], {"status": "copied", "files": 2, "bytes": 8})
        for relative in ("a.bin", "sub/deep/b.bin"):
            original, copied = (game / relative).stat(), (baseline / relative).stat()
            self.assertNotEqual(original.st_ino, copied.st_ino)
            self.assertEqual(copied.st_nlink, 1)
        before = os.stat(game).st_ino
        (game / "a.bin").write_bytes(b"much longer")
        (game / "sub" / "deep" / "b.bin").unlink()
        (game / "sub" / "deep").rmdir()
        (game / "sub").rmdir()
        (game / "sub").write_text("now a file", encoding="utf-8")
        (game / "extra").mkdir()
        (game / "extra" / "c.bin").write_bytes(b"c")
        ports.restore_game(game, baseline, manifest)
        self.assertEqual(os.stat(game).st_ino, before, "the game directory itself is kept")
        self.assertEqual(ports.tree_manifest(game), manifest)
        self.assertEqual((game / "a.bin").read_bytes(), b"abc")
        self.assertEqual((game / "sub" / "deep" / "b.bin").read_bytes(), b"defgh")
        self.assertTrue((game / "link").is_symlink())
        self.assertFalse((game / "extra").exists())

    def test_the_log_lists_every_install_quarantine_as_a_warning(self):
        converted = quarantine_document(dependencies={7: [quarantine_guid(5)]})
        _, report, error = self.run_lane(install_failures=[install_error("Mod 05")], converted=converted)
        self.assertIsNone(error)
        log = io.StringIO()
        with contextlib.redirect_stderr(log):
            ports.print_diagnostics(report)
        text = log.getvalue()
        self.assertIn("::warning::ports: 2 component(s) QUARANTINED after install failures "
                      "(1 failing, 1 dependent(s))", text)
        self.assertIn("PARTIAL install", text)
        self.assertIn("::warning::ports: quarantined Mod 05 [" + quarantine_guid(5)
                      + "]: install failed at step 2 (Patcher): Error: Install stopped.", text)
        self.assertIn("::warning::ports: quarantined Mod 07 [" + quarantine_guid(7)
                      + "]: depends on quarantined " + quarantine_guid(5), text)
        self.assertNotIn("after validate failures", text)
        self.assertNotIn("::error::", text)

    def test_validate_and_install_warnings_are_separate_blocks(self):
        _, report, _ = self.run_lane(
            validate_failures=[validate_error("Mod 02")], install_failures=[install_error("Mod 05")])
        log = io.StringIO()
        with contextlib.redirect_stderr(log):
            ports.print_diagnostics(report)
        text = log.getvalue()
        self.assertIn("1 component(s) QUARANTINED after validate failures", text)
        self.assertIn("1 component(s) QUARANTINED after install failures", text)

    def test_a_refused_install_quarantine_is_a_stage_aware_error_line(self):
        _, report, _ = self.run_lane(install_failures=[install_error("Mod 05") + install_error("Mod 06")])
        report["status"] = "blocked"
        log = io.StringIO()
        with contextlib.redirect_stderr(log):
            ports.print_diagnostics(report)
        text = log.getvalue()
        self.assertIn("::error::ports: install failure not quarantined (multiple_components_failed): "
                      "Error: Install stopped.", text)
        self.assertIn("::error::ports: install failed (exit 1): component 'Mod 06', step 2 (Patcher)", text)

    def test_an_old_style_stopped_record_still_reads_as_a_validate_failure(self):
        log = io.StringIO()
        with contextlib.redirect_stderr(log):
            ports.print_diagnostics({"quarantine": {"stopped": {
                "reason": "game_missing", "component": "Mod 05", "step": 1, "error_line": None}}})
        self.assertIn("::error::ports: validate failure not quarantined (game_missing)", log.getvalue())


REAL_K2_LOG = (
    "\x1b[32mEbon Hawk Downloadable Map / 1: Extract\x1b[0m\n"
    "[2026-10-18 16:09:50] [Error] CLI command stopped\n"
    "[2026-10-18 16:09:50] [Error] Exception: Ebon Hawk Downloadable Map / step 2 (Patcher): "
    "File blocks destination directory: /home/runner/work/_temp/game/Modules/003ebo.mod\n"
    "\x1b[31mError: Ebon Hawk Downloadable Map / step 2 (Patcher): "
    "File blocks destination directory: /home/runner/work/_temp/game/Modules/003ebo.mod\x1b[0m\n")


class FailureEvidenceTests(unittest.TestCase):
    def test_the_component_and_step_are_parsed_from_the_real_k2_validate_output(self):
        parsed = ports.parse_component_failure(REAL_K2_LOG)
        self.assertEqual((parsed["component"], parsed["step"], parsed["action"]),
                         ("Ebon Hawk Downloadable Map", 2, "Patcher"))
        self.assertEqual(parsed["message"],
                         "File blocks destination directory: /home/runner/work/_temp/game/Modules/003ebo.mod")
        self.assertTrue(parsed["error_line"].startswith("Error: Ebon Hawk Downloadable Map / step 2"))
        self.assertNotIn("\x1b", parsed["error_line"])

    def test_the_k1_install_message_is_attributed_too(self):
        parsed = ports.parse_component_failure(
            "Error: Helena Shan Improvement / step 2 (Patcher): Built-in OdyPatcher reported errors; "
            "restore before retrying\n")
        self.assertEqual((parsed["component"], parsed["step"]), ("Helena Shan Improvement", 2))

    def test_names_containing_slashes_or_parentheses_are_attributed_to_the_step_marker(self):
        parsed = ports.parse_component_failure("Error: A / B (HD) / step 10 (Move): bad\n")
        self.assertEqual((parsed["component"], parsed["step"], parsed["action"]),
                         ("A / B (HD)", 10, "Move"))

    def test_the_last_attributed_error_wins_and_plain_errors_do_not_attribute(self):
        text = "Error: One / step 1 (Move): a\nError: Two / step 3 (Extract): b\n"
        self.assertEqual(ports.parse_component_failure(text)["component"], "Two")
        self.assertIsNone(ports.parse_component_failure("Error: something broke\n"))
        self.assertIsNone(ports.parse_component_failure(""))

    def test_the_excerpt_keeps_the_error_with_sixty_lines_of_context_and_nothing_older(self):
        text = "".join(f"line {number}\n" for number in range(300)) + "Error: X / step 1 (Move): boom\n"
        excerpt = ports.failure_excerpt(text)
        self.assertEqual(excerpt[0], "line 240")
        self.assertEqual(excerpt[-1], "Error: X / step 1 (Move): boom")
        self.assertEqual(len(excerpt), 61)
        self.assertNotIn("line 239", excerpt)

    def test_the_excerpt_adds_error_and_failed_lines_after_the_stop_marker(self):
        lines = ["noise " + str(number) for number in range(100)]
        lines += ["Install stopped: component aborted", "cleanup ok", "Rollback FAILED for A",
                  "ordinary", "error while restoring B"]
        excerpt = ports.failure_excerpt("\n".join(lines))
        self.assertIn("Rollback FAILED for A", excerpt)
        self.assertIn("error while restoring B", excerpt)
        self.assertIn("Install stopped: component aborted", excerpt)
        self.assertNotIn("ordinary", excerpt)
        self.assertNotIn("cleanup ok", excerpt)

    def test_failed_lines_before_the_stop_marker_are_not_pulled_in(self):
        lines = ["old step failed long ago"] + ["noise"] * 100 + ["Install stopped", "done"]
        self.assertNotIn("old step failed long ago", ports.failure_excerpt("\n".join(lines)))

    def test_gaps_are_marked_and_the_excerpt_is_capped_at_two_hundred_lines_keeping_the_latest(self):
        lines = ["Install stopped"] + [f"step {number} failed" for number in range(500)]
        excerpt = ports.failure_excerpt("\n".join(lines))
        self.assertEqual(len(excerpt), 201)
        self.assertEqual(excerpt[0], "[301 earlier excerpt line(s) omitted]")
        self.assertEqual(excerpt[-1], "step 499 failed")
        gapped = ports.failure_excerpt("\n".join(
            ["Error: a"] + ["x"] * 80 + ["Error: b"]))
        self.assertIn("[...]", gapped)

    def test_escape_sequences_secrets_and_runner_commands_are_scrubbed(self):
        text = ("\x1b[31mError: A / step 1 (Move): failed token=abc123secret https://user:pw@host/x?sig=zzz\x1b[0m\n"
                "Error: ::set-output name=x::y Authorization: Bearer abcdefgh12345678\n"
                "Error: ghp_" + "a" * 30 + " and " + "w" * 700 + "\n")
        excerpt = "\n".join(ports.failure_excerpt(text))
        self.assertNotIn("\x1b", excerpt)
        for secret in ("abc123secret", "user:pw", "zzz", "abcdefgh12345678", "ghp_" + "a" * 30):
            self.assertNotIn(secret, excerpt)
        self.assertIn("token=***", excerpt)
        self.assertIn("[truncated]", excerpt)
        self.assertNotIn("\n::", "\n" + excerpt)

    def test_output_without_error_lines_gives_no_excerpt(self):
        self.assertEqual(ports.failure_excerpt("all fine\nstill fine\n"), [])

    def test_record_failure_writes_the_component_into_the_report(self):
        report = {}
        failure = ports.record_failure(REAL_K2_LOG, "validate", 1, report)
        self.assertIs(report["failure"], failure)
        self.assertEqual((failure["stage"], failure["exit_code"], failure["component"], failure["step"],
                          failure["action"]), ("validate", 1, "Ebon Hawk Downloadable Map", 2, "Patcher"))
        report = {}
        ports.record_failure("panic\n", "install", 101, report)
        self.assertEqual(report["failure"], {"stage": "install", "exit_code": 101, "component": None,
                                             "step": None, "action": None, "error_line": None})

    def test_a_failing_command_prints_the_evidence_into_the_ci_log_and_names_the_component(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, root)
        script = ("import sys\n" + "".join(f"print('progress {number}')\n" for number in range(100))
                  + "print('Install stopped')\nprint('step failed: oops')\n"
                  + "print('Error: Helena Shan Improvement / step 2 (Patcher): Built-in OdyPatcher "
                  + "reported errors; restore before retrying')\nsys.exit(1)\n")
        report = {"commands": []}
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), self.assertRaisesRegex(ports.Blocked, "^install_failed$"):
            ports.run([__import__("sys").executable, "-c", script], root, root, "install", report, {})
        text = stderr.getvalue()
        self.assertIn("::group::install output (last 80 lines)", text)
        self.assertIn("::group::install failure evidence (bounded excerpt,", text)
        self.assertIn("progress 42\n", text.split("failure evidence")[1])
        self.assertNotIn("progress 41\n", text.split("failure evidence")[1])
        self.assertIn("step failed: oops", text)
        self.assertIn("ports: install failed at component 'Helena Shan Improvement', step 2 (Patcher)", text)
        self.assertEqual(report["failure"]["component"], "Helena Shan Improvement")
        self.assertEqual(report["failure"]["stage"], "install")

    def test_a_failing_command_without_a_component_error_is_still_reported(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, root)
        report = {"commands": []}
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), self.assertRaises(ports.Blocked):
            ports.run([__import__("sys").executable, "-c", "import sys; print('quiet'); sys.exit(3)"],
                      root, root, "validate", report, {})
        self.assertIn("(no Error/Exception line or install-stopped marker in the output)", stderr.getvalue())
        self.assertEqual(report["failure"]["exit_code"], 3)
        self.assertIsNone(report["failure"]["component"])


class GuiFailureReemitTests(unittest.TestCase):
    TAIL = 150
    LEAK = "api_key=notarealsecretvalue123"

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, self.root)

    def diagnostics(self):
        return {"phase": "install", "stage": "base", "worker_result_present": False, "exit_code": 1,
                "signal": None, "signal_inferred": False, "timed_out": False,
                "error_type": "RuntimeError", "error_detail": "boom " + self.LEAK,
                "tails": {"worker": ["worker line %d at /home/someone/game %s" % (number, self.LEAK)
                                     for number in range(self.TAIL)]}}

    def fail_gui(self, report_document=None, log_lines=(), stage="gui"):
        script = ("import json, pathlib, sys\n"
                  "document = json.loads(sys.argv[1])\n"
                  "if document is not None:\n"
                  "    pathlib.Path(sys.argv[2]).write_text(json.dumps(document))\n"
                  "for line in json.loads(sys.argv[3]):\n"
                  "    print(line)\n"
                  "sys.exit(1)\n")
        command = [__import__("sys").executable, "-c", script, json.dumps(report_document),
                   str(self.root / "gui-report.json"), json.dumps(list(log_lines))]
        report = {"commands": []}
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), self.assertRaisesRegex(ports.Blocked, "^%s_failed$" % stage):
            ports.run(command, self.root, self.root, stage, report, {})
        return report, stderr.getvalue()

    def driver_output(self, diagnostics):
        from_driver = ports.gui_diag.render_group(diagnostics)
        return ["driver start"] + from_driver + ['{"status": "blocked", "stage": "install"}']

    def test_worker_diagnostics_are_copied_redacted_into_the_lane_report(self):
        diagnostics = self.diagnostics()
        report, _ = self.fail_gui({"status": "blocked", "worker_diagnostics": diagnostics},
                                  self.driver_output(diagnostics))
        copied = report["worker_diagnostics"]
        self.assertEqual((copied["phase"], copied["stage"], copied["exit_code"]), ("install", "base", 1))
        self.assertEqual(len(copied["tails"]["worker"]), self.TAIL)
        self.assertNotIn("notarealsecretvalue123", json.dumps(copied))
        self.assertNotIn("/home/someone", json.dumps(copied))
        self.assertIn("worker line 149", copied["tails"]["worker"][-1])
        self.assertEqual(report["failure"]["stage"], "gui")

    def test_the_driver_tail_group_is_reemitted_beyond_eighty_lines_and_redacted(self):
        diagnostics = self.diagnostics()
        _, text = self.fail_gui({"worker_diagnostics": diagnostics}, self.driver_output(diagnostics))
        self.assertIn("::group::gui worker tail", text)
        self.assertEqual(text.count("::group::gui worker tail"), 1)
        self.assertIn("[worker] worker line 0 ", text)
        self.assertIn("[worker] worker line 149 ", text)
        self.assertIn("gui worker diagnostics: phase=install stage=base exit_code=1", text)
        self.assertNotIn("::group::gui output", text)
        self.assertNotIn("notarealsecretvalue123", text)
        self.assertNotIn("/home/someone", text)

    def test_plain_log_lines_are_redacted_before_they_reach_the_ci_log(self):
        _, text = self.fail_gui(None, ["Authorization: Bearer abcdefghijklmnop", "plain " + self.LEAK])
        self.assertIn("plain api_key=[REDACTED]", text)
        self.assertNotIn("abcdefghijklmnop", text)
        self.assertNotIn("notarealsecretvalue123", text)

    def test_a_missing_tail_group_is_rendered_from_the_report_diagnostics(self):
        diagnostics = self.diagnostics()
        report, text = self.fail_gui({"worker_diagnostics": diagnostics}, ["driver crashed before output"])
        self.assertIn("driver crashed before output", text)
        self.assertEqual(text.count("::group::gui worker tail"), 1)
        self.assertIn("[worker] worker line 149 ", text)
        self.assertNotIn("notarealsecretvalue123", text)
        self.assertIn("worker_diagnostics", report)

    def test_a_missing_malformed_or_empty_driver_report_adds_no_diagnostics(self):
        for document in (None, [], {"status": "blocked"}, {"worker_diagnostics": {}},
                         {"worker_diagnostics": "text"}):
            with self.subTest(document=document):
                for name in ("gui-report.json", "gui.log"):
                    (self.root / name).unlink(missing_ok=True)
                report, text = self.fail_gui(document, ["driver output"])
                self.assertNotIn("worker_diagnostics", report)
                self.assertIn("driver output", text)
        for name in ("gui-report.json", "gui.log"):
            (self.root / name).unlink(missing_ok=True)
        (self.root / "gui-report.json").write_text("{not json")
        report = {"commands": []}
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(ports.Blocked):
            ports.run([__import__("sys").executable, "-c", "import sys; sys.exit(1)"],
                      self.root, self.root, "gui", report, {})
        self.assertNotIn("worker_diagnostics", report)

    def test_a_symlinked_driver_report_is_ignored(self):
        target = self.root / "elsewhere.json"
        target.write_text(json.dumps({"worker_diagnostics": self.diagnostics()}))
        (self.root / "gui-report.json").symlink_to(target)
        report = {"commands": []}
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(ports.Blocked):
            ports.run([__import__("sys").executable, "-c", "import sys; sys.exit(1)"],
                      self.root, self.root, "gui", report, {})
        self.assertNotIn("worker_diagnostics", report)

    def test_the_tail_is_bounded_to_the_gui_limit(self):
        lines = ["filler %d" % number for number in range(ports.GUI_TAIL_LINES + 50)]
        _, text = self.fail_gui(None, lines)
        self.assertIn("filler %d\n" % (len(lines) - 1), text)
        self.assertNotIn("filler 49\n", text)
        self.assertIn("filler 50\n", text)

    def test_other_stages_keep_the_eighty_line_tail_and_no_diagnostics(self):
        (self.root / "gui-report.json").write_text(json.dumps({"worker_diagnostics": self.diagnostics()}))
        report, text = self.fail_gui(None, ["line %d" % number for number in range(100)], stage="validate")
        self.assertIn("::group::validate output (last 80 lines)", text)
        self.assertNotIn("worker_diagnostics", report)

    def test_sanitize_diagnostics_redacts_strings_and_bounds_the_shape(self):
        sanitized = ports.sanitize_diagnostics(
            {"a": self.LEAK, "b": ["x" * 1000], "c": {"d": {"e": {"f": {"g": 1}}}}, "n": 3, "t": True})
        self.assertEqual(sanitized["a"], "api_key=[REDACTED]")
        self.assertEqual(len(sanitized["b"][0]), ports.gui_diag.LINE_LIMIT + 3)
        self.assertIsNone(sanitized["c"]["d"]["e"]["f"])
        self.assertEqual((sanitized["n"], sanitized["t"]), (3, True))
        self.assertEqual(len(ports.sanitize_diagnostics(list(range(1000)))), ports.DIAGNOSTICS_MAX_ITEMS)


class QuarantineMainTests(unittest.TestCase):
    def test_main_writes_the_failure_and_quarantine_into_the_lane_report_and_exposes_the_caps(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, root)
        for name in ("source", "game", "archives", "content/k2"):
            (root / name).mkdir(parents=True)
        (root / "content/k2/full.md").write_text("x", encoding="utf-8")
        seen = {}

        def fake_execute(args, report):
            seen.update(max=args.max_quarantine, fraction=args.max_quarantine_fraction)
            report["quarantined_validate_failures"] = [{
                "kind": "validate_failure", "name": "Mod 05", "component_guid": quarantine_guid(5),
                "step": 2, "action": "Patcher", "error_line": "Error: Mod 05 / step 2 (Patcher): x",
                "stage": "validate", "caused_by": None}]
            report["failure"] = {"stage": "install", "exit_code": 1, "component": "Mod 09", "step": 2,
                                 "action": "Patcher", "error_line": "Error: Mod 09 / step 2 (Patcher): y"}
            raise ports.Blocked("install_failed")

        argv = ["--implementation", "rust", "--mode", "cli", "--source", str(root / "source"),
                "--guide", str(root / "content/k2/full.md"), "--game-dir", str(root / "game"),
                "--archives", str(root / "archives"), "--work-dir", str(root / "work"),
                "--report", str(root / "out/report.json")]
        stderr = io.StringIO()
        with mock.patch.object(ports, "execute", fake_execute), \
                contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ports.main(argv), 1)
        self.assertEqual(seen, {"max": 15, "fraction": 0.15})
        written = json.loads((root / "out/report.json").read_text(encoding="utf-8"))
        self.assertEqual(written["reason"], "install_failed")
        self.assertEqual(written["failure"]["component"], "Mod 09")
        self.assertEqual(written["quarantined_validate_failures"][0]["component_guid"], quarantine_guid(5))
        self.assertIn("::warning::ports: 1 component(s) QUARANTINED", stderr.getvalue())
        self.assertIn("::error::ports: install failed (exit 1): component 'Mod 09', step 2 (Patcher)",
                      stderr.getvalue())

        seen.clear()
        argv[-1] = str(root / "out/report2.json")
        with mock.patch.object(ports, "execute", fake_execute), \
                contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            ports.main(argv + ["--max-quarantine", "3", "--max-quarantine-fraction", "0.5"])
        self.assertEqual(seen, {"max": 3, "fraction": 0.5})


ALIAS_A = "50000000-0000-0000-0000-00000000000a"
ALIAS_B = "50000000-0000-0000-0000-00000000000b"
ALIAS_URL_A = "https://example.invalid/files/10-alpha/"
ALIAS_URL_B = "https://example.invalid/files/20-beta/"
BRACED = "<<modDirectory>>\\Generic Mod*.{zip,rar,7z}"


def alias_component(name, identity, links, sources, selected=True, options=()):
    lines = ["[[thisMod]]", 'Guid = "' + identity + '"', 'Name = "' + name + '"',
             "IsSelected = " + ("true" if selected else "false"), "ModLink = " + json.dumps(links)]
    for source in sources:
        lines += ["[[thisMod.Instructions]]", 'Action = "Extract"', "Source = " + json.dumps([source])]
    for source in options:
        lines += ["[[thisMod.Options]]", 'Guid = "' + identity[:-1] + 'f"', 'Name = "Variant"',
                  "IsSelected = true", "[[thisMod.Options.Instructions]]", 'Action = "Extract"',
                  "Source = " + json.dumps([source])]
    return "\n".join(lines) + "\n"


class ArchiveAliasTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.root))
        self.archives = self.root / "archives"
        self.archives.mkdir()
        self.map_path = self.root / "map.json"

    def restored(self, name, data=None):
        (self.archives / name).write_bytes(b"PK\x03\x04" + name.encode() if data is None else data)
        return name

    def alias(self, blocks, mapping, selected=(ALIAS_A, ALIAS_B), write_map=True, rules=None):
        converted = self.root / "converted.toml"
        converted.write_text(document(*blocks), encoding="utf-8")
        if write_map:
            self.map_path.write_text(json.dumps(mapping), encoding="utf-8")
        before = converted.read_bytes()
        report = {}
        ports.alias_archives(converted, self.map_path if write_map else None, self.archives,
                             set(selected), report, rules)
        self.assertEqual(converted.read_bytes(), before)
        return report

    def rules(self, *files, heading="Generic Mod", identity=ALIAS_A):
        return {identity: {"heading": heading, "guid": identity, "files": list(files), "reason": "reviewed: test"}}

    def names(self):
        return sorted(path.name for path in self.archives.iterdir())

    def test_braced_glob_gets_a_name_the_validator_matches(self):
        self.restored("Mod files-1086-1-0-1590189064.7z")
        report = self.alias(
            [alias_component("Generic Mod", ALIAS_A, [ALIAS_URL_A], [BRACED])],
            {ALIAS_URL_A: "Mod files-1086-1-0-1590189064.7z"})
        self.assertEqual(report["alias_unresolved"], [])
        self.assertEqual(report["archive_aliases"]["count"], 1)
        alias = report["archive_aliases"]["aliases"][0]
        self.assertEqual(alias["source"], "Mod files-1086-1-0-1590189064.7z")
        self.assertEqual(alias["component"], "Generic Mod")
        self.assertEqual(alias["component_guid"], ALIAS_A)
        self.assertTrue(ports.glob_regex("Generic Mod*.{zip,rar,7z}").fullmatch(alias["alias"]))
        self.assertTrue(ports.archive_pattern_matches(self.archives, ["Generic Mod*.{zip,rar,7z}"]))
        self.assertEqual(alias["method"], "hardlink")
        source, linked = self.archives / alias["source"], self.archives / alias["alias"]
        self.assertTrue(os.path.samefile(source, linked))
        self.assertFalse(linked.is_symlink())

    def test_wildcard_with_a_plain_extension_uses_the_mod_name_and_restored_extension(self):
        self.restored("opaque-77.rar")
        report = self.alias(
            [alias_component("Generic: Mod?", ALIAS_A, [ALIAS_URL_A], ["<<modDirectory>>\\Generic Mod*.rar"])],
            {ALIAS_URL_A: "opaque-77.rar"})
        self.assertEqual([item["alias"] for item in report["archive_aliases"]["aliases"]], ["Generic Mod.rar"])
        self.assertEqual(self.names(), ["Generic Mod.rar", "opaque-77.rar"])

    def test_literal_basename_is_used_verbatim_and_case_insensitively_matched_files_are_left_alone(self):
        self.restored("opaque-1.zip")
        self.restored("OTHER.ZIP")
        report = self.alias(
            [alias_component("Generic Mod", ALIAS_A, [ALIAS_URL_A], ["<<modDirectory>>/Wanted Name.zip"]),
             alias_component("Second Mod", ALIAS_B, [ALIAS_URL_B], ["<<modDirectory>>\\other.zip"])],
            {ALIAS_URL_A: ["opaque-1.zip"], ALIAS_URL_B: "OTHER.ZIP"})
        self.assertEqual([item["alias"] for item in report["archive_aliases"]["aliases"]], ["Wanted Name.zip"])
        self.assertEqual(report["alias_unresolved"], [])
        self.assertEqual(self.names(), ["OTHER.ZIP", "Wanted Name.zip", "opaque-1.zip"])

    def test_nothing_is_created_for_components_the_lane_does_not_select(self):
        self.restored("opaque-1.zip")
        report = self.alias(
            [alias_component("Generic Mod", ALIAS_A, [ALIAS_URL_A], ["<<modDirectory>>\\Wanted.zip"])],
            {ALIAS_URL_A: "opaque-1.zip"}, selected=(ALIAS_B,))
        self.assertEqual(report["archive_aliases"], {"count": 0, "aliases": []})
        self.assertEqual(report["alias_unresolved"], [])
        self.assertEqual(self.names(), ["opaque-1.zip"])

    def test_selected_options_count_and_other_placeholders_are_ignored(self):
        self.restored("opaque-1.zip")
        report = self.alias(
            [alias_component("Generic Mod", ALIAS_A, [ALIAS_URL_A], ["<<kotorDirectory>>\\Override\\x.zip"],
                             options=["<<modDirectory>>\\Variant.zip"])],
            {ALIAS_URL_A: "opaque-1.zip"})
        self.assertEqual([item["alias"] for item in report["archive_aliases"]["aliases"]], ["Variant.zip"])

    def test_several_files_conflicts_and_unmapped_links_are_recorded_not_guessed(self):
        self.restored("one.zip")
        self.restored("two.zip")
        report = self.alias(
            [alias_component("Generic Mod", ALIAS_A, [ALIAS_URL_A], ["<<modDirectory>>\\Wanted A.zip"]),
             alias_component("Second Mod", ALIAS_B, [ALIAS_URL_B], ["<<modDirectory>>\\Wanted B.zip"])],
            {ALIAS_URL_A: ["one.zip", "two.zip"]})
        self.assertEqual(report["archive_aliases"]["count"], 0)
        self.assertEqual({item["component_guid"]: item["reason"] for item in report["alias_unresolved"]},
                         {ALIAS_A: "multi_file_map_entry", ALIAS_B: "no_mapped_archive"})
        self.assertEqual(self.names(), ["one.zip", "two.zip"])

    def test_two_archives_for_one_pattern_or_two_patterns_for_one_archive_are_ambiguous(self):
        self.restored("one.zip")
        self.restored("two.zip")
        report = self.alias(
            [alias_component("Generic Mod", ALIAS_A, [ALIAS_URL_A, ALIAS_URL_B], ["<<modDirectory>>\\Wanted*.zip"]),
             alias_component("Second Mod", ALIAS_B, [ALIAS_URL_B],
                             ["<<modDirectory>>\\First.zip", "<<modDirectory>>\\Second.zip"])],
            {ALIAS_URL_A: "one.zip", ALIAS_URL_B: "two.zip"})
        self.assertEqual(report["archive_aliases"]["count"], 0)
        self.assertEqual([item["reason"] for item in report["alias_unresolved"]],
                         ["ambiguous_mapped_archives"] * 2)
        self.assertEqual(len(report["alias_unresolved"][1]["patterns"]), 2)

    def test_a_declared_rule_picks_the_file_of_a_multi_file_map_entry(self):
        self.restored("one.zip")
        self.restored("two.zip")
        report = self.alias(
            [alias_component("Generic Mod", ALIAS_A, [ALIAS_URL_A], [BRACED])],
            {ALIAS_URL_A: ["one.zip", "two.zip"]}, rules=self.rules("two.zip"))
        self.assertEqual(report["alias_unresolved"], [])
        self.assertEqual([(item["alias"], item["source"], item["rule"])
                          for item in report["archive_aliases"]["aliases"]],
                         [("Generic Mod.{zip,rar,7z}", "two.zip", "archive_aliases")])
        self.assertTrue(os.path.samefile(self.archives / "two.zip", self.archives / "Generic Mod.{zip,rar,7z}"))

    def test_a_declared_rule_aliases_every_file_in_order_and_needs_no_map_entry(self):
        for name in ("a.zip", "b.zip", "c.zip"):
            self.restored(name)
        report = self.alias(
            [alias_component("Generic Mod", ALIAS_A, [ALIAS_URL_A], [BRACED])],
            {}, write_map=False, rules=self.rules("c.zip", "a.zip"))
        self.assertEqual(report["alias_unresolved"], [])
        self.assertEqual([(item["alias"], item["source"]) for item in report["archive_aliases"]["aliases"]],
                         [("Generic Mod.{zip,rar,7z}", "c.zip"), ("Generic Mod (2).{zip,rar,7z}", "a.zip")])
        self.assertNotIn("b.zip", [item["source"] for item in report["archive_aliases"]["aliases"]])

    def test_a_rule_for_another_component_changes_nothing(self):
        self.restored("one.zip")
        self.restored("two.zip")
        report = self.alias(
            [alias_component("Generic Mod", ALIAS_A, [ALIAS_URL_A], [BRACED])],
            {ALIAS_URL_A: ["one.zip", "two.zip"]},
            rules=self.rules("one.zip", heading="Second Mod", identity=ALIAS_B))
        self.assertEqual([item["reason"] for item in report["alias_unresolved"]], ["multi_file_map_entry"])

    def test_a_declared_file_that_is_missing_unsafe_or_not_an_archive_aliases_nothing(self):
        self.restored("one.zip")
        self.restored("notes.zip", b"plain text")
        (self.archives / "link.zip").symlink_to("one.zip")
        for declared, reason in ((("one.zip", "absent.zip"), "declared_mapped_archive_missing_or_unsafe"),
                                 (("one.zip", "notes.zip"), "declared_mapped_file_not_archive"),
                                 (("link.zip",), "declared_mapped_archive_missing_or_unsafe")):
            with self.subTest(declared=declared):
                report = self.alias(
                    [alias_component("Generic Mod", ALIAS_A, [ALIAS_URL_A], [BRACED])],
                    {ALIAS_URL_A: ["one.zip"]}, rules=self.rules(*declared))
                self.assertEqual(report["archive_aliases"]["count"], 0)
                self.assertEqual([item["reason"] for item in report["alias_unresolved"]], [reason])
        self.assertEqual(self.names(), ["link.zip", "notes.zip", "one.zip"])

    def test_a_declared_rule_is_refused_when_the_heading_differs_or_the_pattern_is_not_single(self):
        self.restored("one.zip")
        self.restored("two.zip")
        cases = (
            ([alias_component("Renamed Mod", ALIAS_A, [ALIAS_URL_A], [BRACED])],
             self.rules("one.zip"), "declared_heading_mismatch"),
            ([alias_component("Generic Mod", ALIAS_A, [ALIAS_URL_A],
                              ["<<modDirectory>>\\First.zip", "<<modDirectory>>\\Second.zip"])],
             self.rules("one.zip"), "declared_archives_not_applicable"),
            ([alias_component("Generic Mod", ALIAS_A, [ALIAS_URL_A], ["<<modDirectory>>\\Wanted.zip"])],
             self.rules("one.zip", "two.zip"), "declared_archives_not_applicable"),
        )
        for blocks, rules, reason in cases:
            with self.subTest(reason=reason):
                report = self.alias(blocks, {ALIAS_URL_A: ["one.zip", "two.zip"]}, rules=rules)
                self.assertEqual(report["archive_aliases"]["count"], 0)
                self.assertEqual([item["reason"] for item in report["alias_unresolved"]], [reason])
        self.assertEqual(self.names(), ["one.zip", "two.zip"])

    def test_a_literal_name_is_paired_with_the_link_that_ends_in_it(self):
        self.restored("one.zip")
        self.restored("two.zip")
        report = self.alias(
            [alias_component("Generic Mod", ALIAS_A,
                             [ALIAS_URL_A + "First%20Part.zip?x=1", ALIAS_URL_B + "Second Part.zip"],
                             ["<<modDirectory>>\\Second Part.zip", "<<modDirectory>>\\First Part.zip"])],
            {ALIAS_URL_A + "First%20Part.zip": "one.zip", ALIAS_URL_B + "Second Part.zip": "two.zip"})
        pairs = {item["alias"]: item["source"] for item in report["archive_aliases"]["aliases"]}
        self.assertEqual(pairs, {"Second Part.zip": "two.zip", "First Part.zip": "one.zip"})

    def test_a_multi_file_entry_is_split_by_which_file_names_the_whole_mod(self):
        patch = self.restored("KOTOR 1 Community Patch - Compatibility Patch-1282-4-1.rar")
        main = self.restored("Ultimate Character Overhaul -REDUX- ( LITE ) - TPC Version-1282-4-1.rar")
        nexus = "https://www.nexusmods.com/kotor/mods/1282"
        report = self.alias(
            [alias_component("Ultimate Character Overhaul", ALIAS_A, [nexus],
                             ["<<modDirectory>>\\Ultimate Character Overhaul*.{zip,rar,7z}"]),
             alias_component("Ultimate Character Overhaul Patches", ALIAS_B, [nexus + "?tab=files"],
                             ["<<modDirectory>>\\Ultimate Character Overhaul Patches*.{zip,rar,7z}"])],
            {nexus: [patch, main]})
        self.assertEqual([(item["alias"], item["source"]) for item in report["archive_aliases"]["aliases"]],
                         [("Ultimate Character Overhaul.{zip,rar,7z}", main)])
        self.assertEqual([(item["component_guid"], item["reason"]) for item in report["alias_unresolved"]],
                         [(ALIAS_B, "multi_file_map_entry")])

    def test_separate_downloads_are_told_apart_by_the_link_slug(self):
        self.restored("opaque-1.zip")
        self.restored("opaque-2.zip")
        kiosk = "https://deadlystream.com/files/file/2277-kiosk-hd/"
        fix = "https://deadlystream.com/files/file/2909-kiosk-model-fix-for-k1/"
        report = self.alias(
            [alias_component("HD Kiosk", ALIAS_A, [kiosk, fix], ["<<modDirectory>>\\HD Kiosk*.{zip,rar,7z}"])],
            {kiosk: "opaque-1.zip", fix: "opaque-2.zip"})
        self.assertEqual(report["alias_unresolved"], [])
        self.assertEqual([(item["alias"], item["source"]) for item in report["archive_aliases"]["aliases"]],
                         [("HD Kiosk.{zip,rar,7z}", "opaque-1.zip")])

    def test_every_download_the_glob_names_gets_its_own_alias(self):
        self.restored("Taris Reskin v1.zip")
        self.restored("Taris Reskin Patch.zip")
        self.restored("Unrelated.zip")
        report = self.alias(
            [alias_component("Taris Reskin", ALIAS_A, [ALIAS_URL_A, ALIAS_URL_B],
                             ["<<modDirectory>>\\Taris Reskin*.{zip,rar,7z}"])],
            {ALIAS_URL_A: ["Taris Reskin v1.zip", "Unrelated.zip"], ALIAS_URL_B: "Taris Reskin Patch.zip"})
        self.assertEqual(report["alias_unresolved"], [])
        pairs = {item["alias"]: item["source"] for item in report["archive_aliases"]["aliases"]}
        self.assertEqual(pairs, {"Taris Reskin.{zip,rar,7z}": "Taris Reskin v1.zip",
                                 "Taris Reskin (2).{zip,rar,7z}": "Taris Reskin Patch.zip"})
        matcher = ports.glob_regex("Taris Reskin*.{zip,rar,7z}")
        self.assertTrue(all(matcher.fullmatch(alias) for alias in pairs))

    def test_a_braced_glob_is_matched_only_by_the_literal_group_the_validator_sees(self):
        self.restored("Mod files-1086.7z")
        report = self.alias(
            [alias_component("Generic Mod", ALIAS_A, [ALIAS_URL_A], [BRACED])], {ALIAS_URL_A: "Mod files-1086.7z"})
        alias = report["archive_aliases"]["aliases"][0]["alias"]
        matcher = ports.glob_regex("Generic Mod*.{zip,rar,7z}")
        self.assertEqual(alias, "Generic Mod.{zip,rar,7z}")
        self.assertIsNone(matcher.fullmatch("Generic Mod.7z"))
        self.assertIsNone(matcher.fullmatch("Generic Mod.zip"))
        self.assertTrue(matcher.fullmatch(alias))

    def test_mirrors_with_identical_content_count_as_one_download(self):
        self.restored("from-deadlystream.zip", b"PK\x03\x04same")
        self.restored("from-mega.zip", b"PK\x03\x04same")
        report = self.alias(
            [alias_component("Generic Mod", ALIAS_A, [ALIAS_URL_A, ALIAS_URL_B], ["<<modDirectory>>\\Wanted*.zip"])],
            {ALIAS_URL_A: "from-mega.zip", ALIAS_URL_B: "from-deadlystream.zip"})
        self.assertEqual(report["alias_unresolved"], [])
        self.assertEqual(len(report["archive_aliases"]["aliases"]), 1)
        self.assertEqual(report["archive_aliases"]["aliases"][0]["source"], "from-deadlystream.zip")

    def test_a_mapped_file_that_is_not_an_archive_is_never_aliased(self):
        self.restored("tar02_duelorg021.dlg", b"GFF V3.2 not an archive")
        self.restored("Bendak Option.7z", b"7z\xbc\xaf\x27\x1cdata")
        loose = self.alias(
            [alias_component("Bendak Non-Darkside Option", ALIAS_A, [ALIAS_URL_A], [
                "<<modDirectory>>\\Bendak Non-Darkside Option*.{zip,rar,7z}"])],
            {ALIAS_URL_A: "tar02_duelorg021.dlg"})
        self.assertEqual(loose["archive_aliases"]["count"], 0)
        self.assertEqual(loose["alias_unresolved"][0]["reason"], "mapped_file_not_archive")
        self.assertEqual(self.names(), ["Bendak Option.7z", "tar02_duelorg021.dlg"])
        mixed = self.alias(
            [alias_component("Bendak Option", ALIAS_B, [ALIAS_URL_B], ["<<modDirectory>>\\Bendak Option*.{zip,rar,7z}"])],
            {ALIAS_URL_B: ["tar02_duelorg021.dlg", "Bendak Option.7z"]})
        self.assertEqual(mixed["alias_unresolved"], [])
        self.assertEqual([item["source"] for item in mixed["archive_aliases"]["aliases"]], ["Bendak Option.7z"])

    def test_a_literal_name_shared_by_two_matching_downloads_stays_unresolved(self):
        self.restored("Wanted Name v1.zip")
        self.restored("Wanted Name v2.zip")
        report = self.alias(
            [alias_component("Generic Mod", ALIAS_A, [ALIAS_URL_A], ["<<modDirectory>>\\Wanted Name.zip"])],
            {ALIAS_URL_A: ["Wanted Name v1.zip", "Wanted Name v2.zip"]})
        self.assertEqual(report["archive_aliases"]["count"], 0)
        self.assertEqual(report["alias_unresolved"][0]["reason"], "multi_file_map_entry")

    def test_unsafe_symlinked_missing_and_escaping_inputs_are_refused(self):
        outside = self.root / "outside.zip"
        outside.write_bytes(b"x")
        (self.archives / "link.zip").symlink_to(outside)
        report = self.alias(
            [alias_component("Generic Mod", ALIAS_A, [ALIAS_URL_A], ["<<modDirectory>>\\A.zip"]),
             alias_component("Second Mod", ALIAS_B, [ALIAS_URL_B], ["<<modDirectory>>\\B.zip"])],
            {ALIAS_URL_A: "link.zip", ALIAS_URL_B: "../outside.zip"})
        self.assertEqual([item["reason"] for item in report["alias_unresolved"]],
                         ["mapped_archive_missing_or_unsafe"] * 2)
        self.assertEqual(self.names(), ["link.zip"])
        self.assertEqual(sorted(path.name for path in self.root.iterdir()),
                         ["archives", "converted.toml", "map.json", "outside.zip"])

    def test_patterns_outside_the_top_level_are_not_aliased(self):
        self.restored("one.zip")
        report = self.alias(
            [alias_component("Generic Mod", ALIAS_A, [ALIAS_URL_A],
                             ["<<modDirectory>>\\..\\escape.zip", "<<modDirectory>>\\sub\\file.zip"])],
            {ALIAS_URL_A: "one.zip"})
        self.assertEqual([item["reason"] for item in report["alias_unresolved"][0]["patterns"]],
                         ["path_escapes_archives_dir", "nested_pattern_not_aliased"])
        self.assertEqual(self.names(), ["one.zip"])

    def test_nested_patterns_that_already_match_are_resolved(self):
        (self.archives / "sub").mkdir()
        (self.archives / "sub" / "File.ZIP").write_bytes(b"x")
        report = self.alias(
            [alias_component("Generic Mod", ALIAS_A, [ALIAS_URL_A], ["<<modDirectory>>\\sub\\file.zip"])], {})
        self.assertEqual(report["alias_unresolved"], [])
        self.assertEqual(report["archive_aliases"]["count"], 0)

    def test_colliding_wildcard_names_stay_unique_and_literal_sharing_is_reused(self):
        self.restored("one.zip")
        self.restored("two.zip")
        report = self.alias(
            [alias_component("Generic Mod", ALIAS_A, [ALIAS_URL_A], ["<<modDirectory>>\\Generic Mod*.zip"]),
             alias_component("Generic  Mod", ALIAS_B, [ALIAS_URL_B], ["<<modDirectory>>\\Generic Mod*.zip"])],
            {ALIAS_URL_A: "one.zip", ALIAS_URL_B: "two.zip"})
        aliases = report["archive_aliases"]["aliases"]
        self.assertEqual([(item["alias"], item["source"]) for item in aliases],
                         [("Generic Mod.zip", "one.zip"), ("Generic Mod (2).zip", "two.zip")])
        shared = self.alias(
            [alias_component("Generic Mod", ALIAS_A, [ALIAS_URL_A], ["<<modDirectory>>\\Shared.zip"]),
             alias_component("Second Mod", ALIAS_B, [ALIAS_URL_A], ["<<modDirectory>>\\shared.zip"])],
            {ALIAS_URL_A: "one.zip"})
        self.assertEqual(shared["alias_unresolved"], [])
        self.assertEqual([item["alias"] for item in shared["archive_aliases"]["aliases"]], ["Shared.zip"])

    def test_a_failed_hardlink_falls_back_to_a_relative_symlink_inside_the_archive_directory(self):
        self.restored("opaque-1.zip")
        with mock.patch.object(ports.os, "link", side_effect=OSError(18, "cross-device")):
            report = self.alias(
                [alias_component("Generic Mod", ALIAS_A, [ALIAS_URL_A], ["<<modDirectory>>\\Wanted.zip"])],
                {ALIAS_URL_A: "opaque-1.zip"})
        self.assertEqual(report["archive_aliases"]["aliases"][0]["method"], "symlink")
        link = self.archives / "Wanted.zip"
        self.assertTrue(link.is_symlink())
        self.assertEqual(os.readlink(link), "opaque-1.zip")
        self.assertEqual(link.read_bytes(), (self.archives / "opaque-1.zip").read_bytes())

    def test_a_missing_or_unreadable_map_records_why_nothing_was_aliased(self):
        blocks = [alias_component("Generic Mod", ALIAS_A, [ALIAS_URL_A], ["<<modDirectory>>\\Wanted.zip"])]
        report = self.alias(blocks, {}, write_map=False)
        self.assertEqual(report["alias_unresolved"][0]["reason"], "no_archive_map")
        self.map_path.write_text("[1, 2", encoding="utf-8")
        converted = self.root / "converted.toml"
        report = {}
        ports.alias_archives(converted, self.map_path, self.archives, {ALIAS_A}, report)
        self.assertEqual(report["alias_unresolved"][0]["reason"], "archive_map_unreadable")
        self.assertEqual(report["archive_alias_failed"], "archive_map_unreadable")

    def test_map_links_are_matched_without_query_fragment_or_userinfo(self):
        self.restored("opaque-1.zip")
        report = self.alias(
            [alias_component("Generic Mod", ALIAS_A, [ALIAS_URL_A + "?tab=files#top"],
                             ["<<modDirectory>>\\Wanted.zip"])],
            {"https://user@EXAMPLE.invalid/files/10-alpha/": "opaque-1.zip"})
        self.assertEqual(report["archive_aliases"]["count"], 1)

    def test_diagnostics_warn_once_per_unresolved_component(self):
        report = {"archive_aliases": {"count": 2, "aliases": []},
                  "alias_unresolved": [{"component": "Generic Mod", "component_guid": ALIAS_A,
                                        "reason": "no_mapped_archive", "patterns": []}]}
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            ports.print_diagnostics(report)
        lines = stderr.getvalue().splitlines()
        self.assertEqual(sum(line.startswith("::warning::ports: no archive alias for Generic Mod")
                             for line in lines), 1)
        self.assertIn("no_mapped_archive", stderr.getvalue())
        self.assertIn("archive aliases created for 2", stderr.getvalue())


class ArchiveAliasLaneTests(unittest.TestCase):
    def lane(self, restored, mapping, mode="cli", validate_exit=None):
        root, args = ExecuteCommandTests.prepare(self, "k2")
        args.implementation = "typescript"
        args.mode = mode
        args.review_manifest = root / "review.json"
        args.archive_map = root / "map.json"
        args.archive_map.write_text(json.dumps(mapping), encoding="utf-8")
        for name in restored:
            (args.archives / name).write_bytes(b"PK\x03\x04" + name.encode())
        args.guide.write_text(GUIDE.replace("## CRITICAL: Platform Notes\n\n### Platform Fix A\n\n", ""),
                              encoding="utf-8")
        text = document(
            alias_component("Mod One", "20000000-0000-0000-0000-000000000001", [ALIAS_URL_A],
                            ["<<modDirectory>>\\Mod One*.{zip,rar,7z}"]),
            alias_component("Mod Two", "20000000-0000-0000-0000-000000000002", [ALIAS_URL_B],
                            ["<<modDirectory>>\\Mod Two*.{zip,rar,7z}"]))
        seen = {}

        def fake_run(command, source, private, stage, report, env):
            seen.setdefault("stages", []).append(stage)
            if stage == "convert":
                (private / "converted.toml").write_text(text, encoding="utf-8")
            if stage == "validate":
                seen["at_validate"] = sorted(path.name for path in args.archives.iterdir())
                seen["validate_command"] = list(command)
                if validate_exit:
                    raise ports.Blocked("validate_failed")

        report = {"commands": []}
        error = None
        with mock.patch.object(ports, "run", fake_run), \
                mock.patch.object(ports, "review_manifest", return_value=[]):
            try:
                ports.execute(args, report)
            except ports.Blocked as blocked:
                error = str(blocked)
        return args, report, error, seen

    def test_aliases_exist_before_validate_and_the_reviewed_toml_is_untouched(self):
        args, report, error, seen = self.lane(
            ["a.7z", "b.rar"], {ALIAS_URL_A: "a.7z", ALIAS_URL_B: "b.rar"})
        self.assertIsNone(error)
        self.assertEqual(seen["at_validate"],
                         ["Mod One.{zip,rar,7z}", "Mod Two.{zip,rar,7z}", "a.7z", "b.rar"])
        self.assertEqual(report["archive_aliases"]["count"], 2)
        self.assertEqual(report["alias_unresolved"], [])
        self.assertEqual(seen["validate_command"][seen["validate_command"].index("--mods") + 1],
                         str(args.archives.resolve()))
        converted = next(Path(args.work_dir).glob("ports-*/converted.toml"))
        self.assertEqual(report["converted_sha256"], hashlib.sha256(converted.read_bytes()).hexdigest())
        self.assertEqual(report["status"], "installed")

    def test_gui_lane_gets_the_aliases_too(self):
        _, report, _, seen = self.lane(
            ["a.7z", "b.rar"], {ALIAS_URL_A: "a.7z", ALIAS_URL_B: "b.rar"}, mode="gui")
        self.assertEqual(report["archive_aliases"]["count"], 2)
        self.assertEqual(len(seen["at_validate"]), 4)

    def test_an_unresolved_component_is_recorded_and_validate_still_runs_and_can_fail(self):
        _, report, error, seen = self.lane(
            ["a.7z"], {ALIAS_URL_A: "a.7z"}, validate_exit=True)
        self.assertEqual(error, "validate_failed")
        self.assertIn("validate", seen["stages"])
        self.assertEqual(report["archive_aliases"]["count"], 1)
        self.assertEqual([(item["component"], item["reason"]) for item in report["alias_unresolved"]],
                         [("Mod Two", "no_mapped_archive")])
        self.assertNotEqual(report.get("status"), "installed")

    def test_rust_lanes_do_not_alias(self):
        root, args = ExecuteCommandTests.prepare(self, "k2")
        args.implementation = "rust"
        report = {"commands": []}
        with mock.patch.object(ports, "run", lambda *each: None), \
                mock.patch.object(ports, "alias_archives") as alias:
            with self.assertRaises(ports.Blocked):
                ports.execute(args, report)
        alias.assert_not_called()


class SharedHostMapKeyTests(unittest.TestCase):
    ROOT_ONE = "https://mega.nz/#!AAAAAAAA!first-share-key-stand-in"
    ROOT_TWO = "https://mega.nz/#!CCCCCCCC!second-share-key-stand-in"

    def test_guide_links_and_archive_map_keys_agree_for_fragment_only_links(self):
        self.assertNotEqual(ports.canonical_link(self.ROOT_ONE), ports.canonical_link(self.ROOT_TWO))
        self.assertNotEqual(ports.canonical_link(self.ROOT_ONE), "https://mega.nz/")
        self.assertEqual(ports.canonical_link(ports.canonical_link(self.ROOT_ONE)), ports.canonical_link(self.ROOT_ONE))
        self.assertEqual(ports.canonical_link("https://mega.nz/file/AAAAAAAA#key-stand-in"),
                         "https://mega.nz/file/AAAAAAAA")

    def test_mapped_archives_keeps_each_fragment_only_link_on_its_own_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "archive-map.json")
            path.write_text(json.dumps({ports.canonical_link(self.ROOT_ONE): "one.zip",
                                        ports.canonical_link(self.ROOT_TWO): "two.rar"}), encoding="utf-8")
            mapping = ports.mapped_archives(path)
        self.assertEqual(mapping[ports.canonical_link(self.ROOT_ONE)], "one.zip")
        self.assertEqual(mapping[ports.canonical_link(self.ROOT_TWO)], "two.rar")
        self.assertEqual(ports.component_archive({"ResourceRegistry": {self.ROOT_TWO: {}}}, mapping), "two.rar")


def tagged_error(name, kind="patcher-error", action="Patcher", detail="auto-generated",
                 message="[InstallList] install_folder0: references section [override] which does not exist"):
    return f"  [{kind}] {name}: {name} / {action} ({detail}): {message}"


def validate_report(*lines, info=0):
    prompts = [f"  [prompt-required] Info Stand-In {number}: Info Stand-In {number}: choose an "
               f'installation option: the installer will ask "auto-generated" (3 choices)'
               for number in range(info)]
    return "\n".join([
        "Validated /work/converted.toml: 20 mods, 20 selected, 20 in install order",
        f"ERROR ({len(lines)})", *lines, f"INFO ({info})", *prompts, "FAILED: errors found.", ""])


class ValidateTaggedErrorParsingTests(unittest.TestCase):
    def test_tagged_lines_become_component_failures_with_actions_and_kinds(self):
        text = validate_report(
            tagged_error("Mod 01"),
            tagged_error("Mod 02", message="changes ini not found: /archives/Mod 02/tslpatchdata/changes_1.ini "
                                           "(*.ini in /archives/Mod 02/tslpatchdata: )"),
            tagged_error("Mod 03", detail="auto-generated; multi-run 1 of 2 (namespace index 0)"),
            tagged_error("Mod 03", detail="auto-generated; multi-run 2 of 2 (namespace index 1)"),
            tagged_error("Mod 04", message="2DAList feat.2da: RangeError: read of 8 bytes at offset 0 "
                                           "exceeds buffer of 0 bytes"),
            tagged_error("Stand-In: Colon Name"),
            tagged_error("Mod 05", kind="missing-source", action="Execute",
                         message='executable not found: "<<modDirectory>>/Mod 05*.exe"'),
            tagged_error("Mod 06", kind="missing-archive", action="Extract",
                         message="no archive matches Mod 06*.{zip,rar,7z}"),
            info=3)
        failures, unattributed = ports.parse_validate_errors(text)
        self.assertEqual(unattributed, [])
        self.assertEqual([item["component"] for item in failures],
                         ["Mod 01", "Mod 02", "Mod 03", "Mod 03", "Mod 04", "Stand-In: Colon Name",
                          "Mod 05", "Mod 06"])
        self.assertEqual([item["kind"] for item in failures],
                         ["patcher-error"] * 6 + ["missing-source", "missing-archive"])
        self.assertEqual([item["action"] for item in failures],
                         ["Patcher"] * 6 + ["Execute", "Extract"])
        self.assertTrue(all(item["step"] is None for item in failures))
        self.assertIn("RangeError", failures[4]["message"])
        self.assertEqual(len(ports.distinct_failures(failures)), 7)

    def test_only_error_severity_lines_count(self):
        warned = ("Validated x\nWARNING (1)\n" + tagged_error("Mod 01") + "\nINFO (1)\n"
                  "  [prompt-required] Mod 02: Mod 02: choose an installation option\nFAILED: errors found.\n")
        self.assertEqual(ports.validate_error_lines(warned), [])
        tail_only = tagged_error("Mod 01") + "\n" + tagged_error("Mod 02") + "\nFAILED: errors found.\n"
        self.assertEqual(len(ports.validate_error_lines(tail_only)), 2)

    def test_an_error_line_of_another_shape_is_unattributed(self):
        text = validate_report(tagged_error("Mod 01"), "  something global went wrong",
                               "  [patcher-error] no repeated component here")
        failures, unattributed = ports.parse_validate_errors(text)
        self.assertEqual([item["component"] for item in failures], ["Mod 01"])
        self.assertEqual(unattributed, ["  something global went wrong",
                                        "  [patcher-error] no repeated component here"])

    def test_evidence_keeps_every_error_line_and_is_capped_by_bytes_not_lines(self):
        lines = [tagged_error(f"Stand-In {number:03d}") for number in range(300)]
        text = validate_report(*lines, info=100)
        excerpt = ports.validate_error_excerpt(text)
        self.assertEqual(excerpt, lines)
        small = ports.validate_error_excerpt(text, max_bytes=2000)
        self.assertEqual(small[:-1], lines[:len(small) - 1])
        self.assertLess(len(small), len(lines))
        self.assertRegex(small[-1], r"^\[\d+ further error line\(s\) omitted: 2000 byte evidence cap\]$")
        self.assertLessEqual(sum(len(line.encode()) + 1 for line in small[:-1]), 2000)

    def test_a_failed_validate_prints_all_error_lines_even_beyond_the_eighty_line_tail(self):
        lines = [tagged_error(f"Stand-In {number:03d}") for number in range(120)]
        text = validate_report(*lines, info=100)
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(root))
        report = {"commands": []}
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), self.assertRaises(ports.Blocked):
            ports.run([__import__("sys").executable, "-c",
                       "import sys; sys.stdout.write(sys.argv[1]); sys.exit(1)", text],
                      root, root, "validate", report, {})
        output = stderr.getvalue()
        evidence = output.split("validate failure evidence", 1)[1]
        for number in (0, 59, 119):
            self.assertIn(f"[patcher-error] Stand-In {number:03d}: Stand-In {number:03d} / Patcher", evidence)
        self.assertEqual(len(report["failure"]["components"]), 120)
        self.assertEqual(report["failure"]["error_line_count"], 120)
        self.assertIsNone(report["failure"]["component"])
        self.assertIn("ports: validate failed at 120 component(s) attributed", output)

    def test_one_distinct_tagged_component_is_recorded_as_the_failing_component(self):
        text = validate_report(tagged_error("Mod 01"), tagged_error("Mod 01", detail="auto-generated; multi-run 2"))
        failure = ports.record_failure(text, "validate", 1, {})
        self.assertEqual((failure["component"], failure["step"], failure["action"]),
                         ("Mod 01", None, "Patcher"))
        self.assertEqual(failure["components"], ["Mod 01"])
        self.assertEqual(ports.failure_location(failure), "component 'Mod 01' (Patcher)")

    def test_the_final_error_names_the_attributed_components(self):
        report = {"failure": {"stage": "validate", "exit_code": 1, "component": None, "step": None,
                              "action": None, "error_line": None,
                              "components": [f"Mod {number:02d}" for number in range(1, 8)]}}
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            ports.print_diagnostics(report)
        text = stderr.getvalue()
        self.assertIn("::error::ports: validate failed (exit 1): 7 component(s) attributed "
                      "('Mod 01', 'Mod 02', 'Mod 03', 'Mod 04', 'Mod 05' and 2 more)", text)
        self.assertNotIn("no component could be attributed", text)


class ValidateMultiComponentQuarantineTests(unittest.TestCase):
    def validate(self, logs, converted=None, deselect=None, report=None, **options):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(root))
        converted_path = root / "converted.toml"
        converted_path.write_text(converted or quarantine_document(), encoding="utf-8")
        game = root / "game"
        game.mkdir()
        (game / "chitin.key").write_text("x", encoding="utf-8")
        (root / "work").mkdir()
        pending = list(logs)
        seen = []

        def fake_run(command, source, private, stage, report, env):
            seen.append((stage, list(command)))
            if pending:
                (private / (stage + ".log")).write_text(pending.pop(0), encoding="utf-8")
                report["commands"].append({"stage": stage, "exit_code": 1})
                raise ports.Blocked(stage + "_failed")
            report["commands"].append({"stage": stage, "exit_code": 0})

        deselect = [] if deselect is None else deselect
        report = {"commands": []} if report is None else report
        error = None
        with mock.patch.object(ports, "run", fake_run):
            try:
                ports.validate_with_quarantine(["modsync"], ["converted"], deselect, converted_path, game,
                                               root / "source", root / "work", report, {}, **options)
            except ports.Blocked as blocked:
                error = str(blocked)
        self.assertEqual(pending, [])
        return seen, report, deselect, error

    def test_every_distinct_failing_component_is_quarantined_in_one_pass(self):
        log = validate_report(tagged_error("Mod 03"), tagged_error("Mod 07"),
                              tagged_error("Mod 07", detail="auto-generated; multi-run 2 of 2"), info=2)
        seen, report, deselect, error = self.validate([log])
        self.assertIsNone(error)
        self.assertEqual([stage for stage, _ in seen], ["validate", "validate_retry_1"])
        self.assertEqual([item["name"] for item in report["quarantined_validate_failures"]],
                         ["Mod 03", "Mod 07"])
        self.assertEqual({item["kind"] for item in report["quarantined_validate_failures"]},
                         {ports.VALIDATE_FAILURE_KIND})
        self.assertEqual(deselect, ["--deselect", quarantine_guid(3), "--deselect", quarantine_guid(7)])
        self.assertEqual(report["quarantine"]["attempts"], 2)
        self.assertIsNone(report["quarantine"]["stopped"])
        self.assertNotIn("failure", report)

    def test_a_failing_component_and_its_dependent_in_one_pass_are_recorded_once(self):
        converted = quarantine_document(dependencies={2: [quarantine_guid(1)]})
        log = validate_report(tagged_error("Mod 01"), tagged_error("Mod 02"))
        _, report, deselect, error = self.validate([log], converted=converted)
        self.assertIsNone(error)
        self.assertEqual([(item["name"], item["kind"]) for item in report["quarantined_validate_failures"]],
                         [("Mod 01", ports.VALIDATE_FAILURE_KIND), ("Mod 02", ports.QUARANTINE_DEPENDENT_KIND)])
        self.assertEqual(deselect.count(quarantine_guid(2)), 1)

    def test_the_cap_is_shared_with_install_and_a_refused_pass_quarantines_nothing(self):
        installed = [{"kind": ports.INSTALL_FAILURE_KIND, "component_guid": quarantine_guid(number),
                      "name": f"Mod {number}"} for number in (19, 20)]
        log = validate_report(tagged_error("Mod 01"), tagged_error("Mod 02"))
        seen, report, deselect, error = self.validate(
            [log], report={"commands": [], ports.INSTALL_QUARANTINE_KEY: installed})
        self.assertEqual(error, "validate_quarantine_cap_exceeded")
        self.assertEqual([stage for stage, _ in seen], ["validate"])
        self.assertEqual(report["quarantine"]["stopped"]["reason"], "component_cap_exceeded")
        self.assertEqual(report["quarantine"]["stopped"]["component"], "Mod 02")
        self.assertEqual(report[ports.QUARANTINE_KEY], [])
        self.assertEqual(deselect, [])
        self.assertEqual(report["failure"]["components"], ["Mod 01", "Mod 02"])

    def test_more_components_than_fifteen_percent_are_refused_whole(self):
        log = validate_report(*[tagged_error(f"Mod {number:02d}") for number in range(1, 5)])
        seen, report, deselect, error = self.validate([log])
        self.assertEqual(error, "validate_quarantine_cap_exceeded")
        self.assertEqual(report[ports.QUARANTINE_KEY], [])
        self.assertEqual(deselect, [])
        self.assertEqual(report["quarantine"]["max_components"], 3)

    def test_a_missing_download_is_never_quarantined(self):
        for kind, action in (("missing-source", "Execute"), ("missing-archive", "Extract")):
            with self.subTest(kind=kind):
                log = validate_report(tagged_error("Mod 03"), tagged_error(
                    "Mod 04", kind=kind, action=action, message="nothing found for the download"))
                seen, report, deselect, error = self.validate([log])
                self.assertEqual(error, "validate_failed")
                self.assertEqual([stage for stage, _ in seen], ["validate"])
                stopped = report["quarantine"]["stopped"]
                self.assertEqual((stopped["reason"], stopped["component"]),
                                 ("missing_download_not_quarantinable", "Mod 04"))
                self.assertEqual(report[ports.QUARANTINE_KEY], [])
                self.assertEqual(deselect, [])
                self.assertEqual(report["failure"]["components"], ["Mod 03", "Mod 04"])

    def test_a_missing_download_already_covered_by_policy_is_not_a_reason_to_refuse(self):
        log = validate_report(tagged_error("Mod 03"), tagged_error(
            "Mod 05", kind="missing-source", action="Execute", message="executable not found"))
        covered = ["--deselect", quarantine_guid(5)]
        _, report, deselect, error = self.validate([log], deselect=list(covered))
        self.assertIsNone(error)
        self.assertEqual([item["name"] for item in report[ports.QUARANTINE_KEY]], ["Mod 03"])
        self.assertEqual(deselect, covered + ["--deselect", quarantine_guid(3)])

    def test_a_global_or_ambiguous_error_line_refuses_the_whole_pass(self):
        log = validate_report(tagged_error("Mod 03"), "  game directory is not a KOTOR install")
        seen, report, deselect, error = self.validate([log])
        self.assertEqual(error, "validate_failed")
        self.assertEqual(report["quarantine"]["stopped"]["reason"], "unattributed_error_lines")
        self.assertEqual(report[ports.QUARANTINE_KEY], [])
        self.assertEqual(deselect, [])

    def test_an_unlisted_action_refuses_the_whole_pass(self):
        log = validate_report(tagged_error("Mod 03"), tagged_error("Mod 04", action="Execute"))
        _, report, deselect, error = self.validate([log])
        self.assertEqual(error, "validate_failed")
        stopped = report["quarantine"]["stopped"]
        self.assertEqual((stopped["reason"], stopped["component"]), ("action_not_quarantinable", "Mod 04"))
        self.assertEqual(deselect, [])

    def test_a_name_in_no_converted_component_refuses_the_whole_pass(self):
        log = validate_report(tagged_error("Mod 03"), tagged_error("Not A Component"))
        _, report, deselect, error = self.validate([log])
        self.assertEqual(error, "validate_failed")
        self.assertEqual(report["quarantine"]["stopped"]["reason"], "component_unknown")
        self.assertEqual(report[ports.QUARANTINE_KEY], [])
        self.assertEqual(deselect, [])


class TypeScriptValidateQuarantineTests(unittest.TestCase):
    prepare = ExecuteCommandTests.prepare

    def run_lane(self, failures=(), mode="cli", max_fraction=None):
        root, args = self.prepare("k1")
        args.guide.write_text(QUARANTINE_GUIDE, encoding="utf-8")
        args.implementation = "typescript"
        args.mode = mode
        args.review_manifest = root / "review.json"
        (args.game_dir / "chitin.key").write_text("x", encoding="utf-8")
        if max_fraction is not None:
            args.max_quarantine_fraction = max_fraction
        pending = list(failures)
        commands = []
        inputs = {}

        def fake_run(command, source, private, stage, report, env):
            commands.append((stage, list(command)))
            if stage == "convert":
                (private / "converted.toml").write_text(quarantine_document(), encoding="utf-8")
            if stage.startswith("validate") and pending:
                (private / (stage + ".log")).write_text(pending.pop(0), encoding="utf-8")
                report["stage"] = stage
                report["commands"].append({"stage": stage, "exit_code": 1})
                raise ports.Blocked(stage + "_failed")
            if stage == "gui":
                inputs["gui"] = tomllib.loads(
                    Path(command[command.index("--input") + 1]).read_text(encoding="utf-8"))
                raise ports.Blocked("gui_stopped_by_test")

        report = {"commands": []}
        error = None
        with mock.patch.object(ports, "run", fake_run), \
                mock.patch.object(ports, "review_manifest", return_value=[]):
            try:
                ports.execute(args, report)
            except ports.Blocked as blocked:
                error = str(blocked)
        self.assertEqual(pending, [])
        return commands, report, error, inputs

    def selected(self, command):
        return [value for flag, value in zip(command, command[1:]) if flag == "--select"][0].split(",")

    def test_the_typescript_validate_quarantines_every_named_component_and_installs_the_rest(self):
        log = validate_report(tagged_error("Mod 03"), tagged_error("Mod 07"), info=4)
        commands, report, error, _ = self.run_lane([log])
        self.assertIsNone(error)
        stages = [stage for stage, _ in commands if stage.startswith(("validate", "install"))]
        self.assertEqual(stages, ["validate", "validate_retry_1", "install_base", "install_widescreen"])
        by_stage = dict(commands)
        everyone = [quarantine_guid(number) for number in range(1, 21)]
        self.assertEqual(self.selected(by_stage["validate"]), everyone)
        remaining = [guid for guid in everyone if guid not in (quarantine_guid(3), quarantine_guid(7))]
        for stage in ("validate_retry_1", "install_base", "install_widescreen"):
            self.assertEqual(self.selected(by_stage[stage]), remaining, stage)
            self.assertNotIn("--deselect", by_stage[stage], stage)
        self.assertEqual([item["name"] for item in report["quarantined_validate_failures"]],
                         ["Mod 03", "Mod 07"])
        self.assertEqual(report["installable_before_quarantine_count"], 20)
        self.assertEqual(report["installable_component_count"], 18)
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            ports.print_diagnostics(report)
        text = stderr.getvalue()
        self.assertIn("::warning::ports: 2 component(s) QUARANTINED after validate failures", text)
        self.assertIn("PARTIAL install", text)
        self.assertIn("::warning::ports: quarantined Mod 03 [", text)
        self.assertNotIn("step None", text)

    def test_the_gui_input_leaves_the_quarantined_components_unselected(self):
        log = validate_report(tagged_error("Mod 03"))
        _, report, error, inputs = self.run_lane([log], mode="gui")
        self.assertEqual(error, "gui_stopped_by_test")
        selected = {item["Guid"]: item["IsSelected"] for item in inputs["gui"]["thisMod"]}
        self.assertFalse(selected[quarantine_guid(3)])
        self.assertTrue(all(value for guid, value in selected.items() if guid != quarantine_guid(3)))
        self.assertEqual(report["selected_after_policy_count"], 19)

    def test_a_missing_download_keeps_the_lane_red_with_visible_evidence(self):
        log = validate_report(tagged_error("Mod 03"), tagged_error(
            "Mod 04", kind="missing-source", action="Execute", message="executable not found"))
        commands, report, error, _ = self.run_lane([log])
        self.assertEqual(error, "validate_failed")
        self.assertEqual([stage for stage, _ in commands if stage.startswith(("validate", "install"))],
                         ["validate"])
        self.assertEqual(report["quarantined_validate_failures"], [])
        self.assertEqual(report["quarantine"]["stopped"]["reason"], "missing_download_not_quarantinable")
        self.assertEqual(report["failure"]["components"], ["Mod 03", "Mod 04"])
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            ports.print_diagnostics(report)
        text = stderr.getvalue()
        self.assertIn("2 component(s) attributed ('Mod 03', 'Mod 04')", text)
        self.assertIn("validate failure not quarantined (missing_download_not_quarantinable)", text)

    def test_the_cap_applies_to_the_installable_components_in_one_pass(self):
        log = validate_report(*[tagged_error(f"Mod {number:02d}") for number in range(1, 5)])
        commands, report, error, _ = self.run_lane([log])
        self.assertEqual(error, "validate_quarantine_cap_exceeded")
        self.assertEqual(report["quarantined_validate_failures"], [])
        self.assertEqual(report["quarantine"]["max_components"], 3)
        self.assertNotIn("install_base", dict(commands))

    def test_a_passing_typescript_validate_runs_once_and_reports_an_empty_quarantine(self):
        commands, report, error, _ = self.run_lane()
        self.assertIsNone(error)
        self.assertEqual([stage for stage, _ in commands if stage.startswith("validate")], ["validate"])
        self.assertEqual(report["quarantined_validate_failures"], [])
        self.assertNotIn("installable_before_quarantine_count", report)


class ValidateQuarantinePassAccountingTests(unittest.TestCase):
    validate = ValidateMultiComponentQuarantineTests.validate

    def names(self, numbers):
        return [f"Mod {number:02d}" for number in numbers]

    def pass_log(self, numbers):
        return validate_report(*[tagged_error(name) for name in self.names(numbers)])

    def test_one_pass_attributing_more_components_than_iterations_is_not_an_iteration_failure(self):
        seen, report, deselect, error = self.validate(
            [self.pass_log(range(1, 19))], converted=quarantine_document(130))
        self.assertIsNone(error)
        self.assertEqual(report["quarantine"]["max_components"], 19)
        self.assertEqual(report["quarantine"]["iterations_used"], 1)
        self.assertEqual(len(report["quarantined_validate_failures"]), 18)
        self.assertEqual(len(deselect), 36)
        self.assertEqual([stage for stage, _ in seen], ["validate", "validate_retry_1"])
        self.assertIsNone(report["quarantine"]["stopped"])

    def test_a_pass_over_the_component_cap_reports_the_cap_with_every_attributed_component(self):
        numbers = range(1, 27)
        seen, report, deselect, error = self.validate(
            [self.pass_log(numbers)], converted=quarantine_document(130))
        self.assertEqual(error, "validate_quarantine_cap_exceeded")
        stopped = report["quarantine"]["stopped"]
        self.assertEqual(stopped["reason"], "component_cap_exceeded")
        self.assertEqual(stopped["attributed_count"], 26)
        self.assertEqual(stopped["component_cap"], 19)
        self.assertEqual(stopped["already_quarantined"], 0)
        self.assertEqual(stopped["attributed_components"], self.names(numbers))
        self.assertEqual(report["quarantine"]["iterations_used"], 0)
        self.assertEqual(report[ports.QUARANTINE_KEY], [])
        self.assertEqual(deselect, [])
        self.assertEqual([stage for stage, _ in seen], ["validate"])

    def test_the_final_error_lists_every_attributed_component_not_only_the_first(self):
        numbers = range(1, 27)
        _, report, _, error = self.validate([self.pass_log(numbers)], converted=quarantine_document(130))
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            ports.print_diagnostics(report)
        output = stderr.getvalue()
        self.assertIn("attributed 26 component(s) against a component cap of 19", output)
        for name in self.names(numbers):
            self.assertIn(repr(name), output)

    def test_iterations_count_passes_across_validate_passes(self):
        logs = [self.pass_log(range(1, 4)), self.pass_log(range(4, 6)), self.pass_log([6])]
        seen, report, deselect, error = self.validate(
            logs, converted=quarantine_document(130), max_iterations=2)
        self.assertEqual(error, "validate_quarantine_cap_exceeded")
        stopped = report["quarantine"]["stopped"]
        self.assertEqual(stopped["reason"], "iteration_cap_exceeded")
        self.assertNotIn("attributed_components", stopped)
        self.assertEqual(report["quarantine"]["iterations_used"], 2)
        self.assertEqual(len(report[ports.QUARANTINE_KEY]), 5)
        self.assertEqual([stage for stage, _ in seen], ["validate", "validate_retry_1", "validate_retry_2"])

    def test_a_multi_component_pass_and_install_failures_each_consume_the_shared_budget_once(self):
        installed = [{"kind": ports.INSTALL_FAILURE_KIND, "component_guid": quarantine_guid(number),
                      "name": f"Mod {number:02d}"} for number in (101, 102)]
        seen, report, deselect, error = self.validate(
            [self.pass_log(range(1, 6))], converted=quarantine_document(130), max_iterations=1,
            report={"commands": [], ports.INSTALL_QUARANTINE_KEY: installed})
        self.assertIsNone(error)
        self.assertEqual(report["quarantine"]["iterations_used"], 1)
        self.assertEqual(len(ports.quarantined_entries(report)), 7)

    def test_a_refused_pass_restores_the_iteration_count(self):
        installed = [{"kind": ports.INSTALL_FAILURE_KIND, "component_guid": quarantine_guid(number),
                      "name": f"Mod {number:02d}"} for number in range(101, 119)]
        _, report, _, error = self.validate(
            [self.pass_log(range(1, 4))], converted=quarantine_document(130),
            report={"commands": [], ports.INSTALL_QUARANTINE_KEY: installed})
        self.assertEqual(error, "validate_quarantine_cap_exceeded")
        stopped = report["quarantine"]["stopped"]
        self.assertEqual((stopped["attributed_count"], stopped["component_cap"], stopped["already_quarantined"]),
                         (3, 19, 18))
        self.assertEqual(report["quarantine"]["iterations_used"], 0)


UNFINISHED_INSTALL_TEXT = ("[2026-10-18 16:20:01] [Error] An unfinished install exists. Run restore before "
                           "retrying. Recovery: /tmp/work/.modsync-0123456789abcdef\n"
                           "Error: An unfinished install exists. Run restore before retrying. "
                           "Recovery: /tmp/work/.modsync-0123456789abcdef\n")


def fake_engine_restore(events, status="restored"):
    """A stand-in for ports.engine_restore that records the stage and appends the real record shape."""
    def fake(command, directories, source, private, stage, report, env):
        events.append(("engine_restore", stage, list(directories)))
        record = {"stage": stage, "status": status, "seconds": 0.0}
        report["quarantine"].setdefault(ports.ENGINE_RESTORE_KEY, []).append(record)
        return record
    return fake


class InstallRecoveryTests(unittest.TestCase):
    prepare = ExecuteCommandTests.prepare
    run_lane = InstallQuarantineTests.run_lane
    installs = InstallQuarantineTests.installs
    deselected = InstallQuarantineTests.deselected

    def lane(self, events, install_failures, status="restored", **extra):
        real_restore = ports.restore_game

        def recording_restore(game, baseline, manifest):
            events.append(("baseline_restore",))
            return real_restore(game, baseline, manifest)

        with mock.patch.object(ports, "engine_restore", fake_engine_restore(events, status)), \
                mock.patch.object(ports, "restore_game", recording_restore):
            return self.run_lane(install_failures=install_failures, **extra)

    def test_the_engine_restore_runs_before_the_baseline_restore_of_every_retry(self):
        events = []
        commands, report, error = self.lane(events, [install_error("Mod 05"), install_error("Mod 06")])
        self.assertIsNone(error)
        self.assertEqual([stage for stage, _ in self.installs(commands)],
                         ["install", "install_retry_1", "install_retry_2"])
        self.assertEqual([event[0] for event in events],
                         ["engine_restore", "baseline_restore", "engine_restore", "baseline_restore"])
        self.assertEqual([event[1] for event in events if event[0] == "engine_restore"],
                         ["engine_restore_1", "engine_restore_2"])
        directories = events[0][2]
        self.assertEqual(directories[directories.index("--game-dir") + 1], str(self.game))
        self.assertIn("--source-dir", directories)
        self.assertEqual([item["stage"] for item in report["quarantine"][ports.ENGINE_RESTORE_KEY]],
                         ["engine_restore_1", "engine_restore_2"])
        self.assertEqual(self.seen["install_retry_1"]["tree"], self.initial)

    def test_a_quarantine_the_engine_refuses_to_restore_never_retries(self):
        events = []
        with mock.patch.object(ports, "remove_engine_state", side_effect=OSError("not plain")):
            commands, report, error = self.lane(events, [install_error("Mod 05")], status="failed")
        self.assertEqual(error, "install_failed")
        self.assertEqual(report["quarantine"]["stopped"]["reason"], "restore_failed")
        self.assertEqual([event[0] for event in events], ["engine_restore"])
        self.assertEqual(report["quarantined_install_failures"], [])
        self.assertEqual([stage for stage, _ in self.installs(commands)], ["install"])

    def test_a_failed_engine_restore_falls_back_to_removing_only_this_games_recovery_directory(self):
        events = []
        with mock.patch.object(ports, "remove_engine_state", return_value="removed") as removal:
            commands, report, error = self.lane(events, [install_error("Mod 05")], status="unavailable")
        self.assertIsNone(error)
        removal.assert_called_once()
        self.assertEqual(removal.call_args.args[0], self.game)
        record, = report["quarantine"][ports.ENGINE_RESTORE_KEY]
        self.assertEqual((record["status"], record["fallback"]), ("unavailable", "state_removed"))
        self.assertEqual([event[0] for event in events], ["engine_restore", "baseline_restore"])

    def test_a_retry_refused_as_an_unfinished_install_keeps_the_original_failure(self):
        events = []
        commands, report, error = self.lane(events, [install_error("Mod 05"), UNFINISHED_INSTALL_TEXT])
        self.assertEqual(error, "install_failed")
        self.assertEqual([stage for stage, _ in self.installs(commands)], ["install", "install_retry_1"])
        stopped = report["quarantine"]["stopped"]
        self.assertEqual((stopped["phase"], stopped["stage"], stopped["reason"]),
                         ("install", "install_retry_1", ports.UNFINISHED_INSTALL_REASON))
        self.assertNotEqual(stopped["reason"], "error_not_attributable")
        self.assertIn("An unfinished install exists", stopped["error_line"])
        original = stopped["original_failure"]
        self.assertEqual((original["name"], original["step"], original["action"], original["stage"]),
                         ("Mod 05", 2, "Patcher", "install"))
        self.assertEqual(stopped["recovery"]["stage"], "engine_restore_1")
        self.assertTrue(report["failure"]["unfinished_install"])
        self.assertEqual(report["failure"]["original_failure"], original)
        self.assertEqual([item["name"] for item in report["quarantined_install_failures"]], ["Mod 05"])

    def test_the_log_names_the_unfinished_install_and_the_original_failure(self):
        _, report, _ = self.lane([], [install_error("Mod 05"), UNFINISHED_INSTALL_TEXT])
        report["status"] = "blocked"
        log = io.StringIO()
        with contextlib.redirect_stderr(log):
            ports.print_diagnostics(report)
        text = log.getvalue()
        self.assertIn("install failure not quarantined (unfinished_install_not_recovered): ", text)
        self.assertIn("An unfinished install exists. Run restore before retrying", text)
        self.assertNotIn("error_not_attributable", text)
        self.assertIn("install_retry_1 was refused as an unfinished install, not a new component failure", text)
        self.assertIn("the original failure was component 'Mod 05', step 2 (Patcher) in install:", text)

    def test_an_unattributable_failure_without_the_unfinished_message_keeps_its_reason(self):
        _, report, error = self.lane([], [install_error("Mod 05"), "Error: something else broke\n"])
        self.assertEqual(error, "install_failed")
        self.assertEqual(report["quarantine"]["stopped"]["reason"], "error_not_attributable")
        self.assertNotIn("unfinished_install", report["failure"])

    def test_the_first_installs_snapshot_message_is_not_an_unfinished_install(self):
        self.assertNotIn(ports.UNFINISHED_INSTALL, install_error("Mod 05").lower())


class EngineStateTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, self.root)
        self.game = self.root / "game"
        self.game.mkdir()
        self.archives = self.root / "archives"
        self.archives.mkdir()
        self.private = self.root / "private"
        self.private.mkdir()
        self.report = {"quarantine": {}}

    def state(self, pending=True):
        state = ports.engine_state_dir(self.game)
        (state / "snapshot" / "game").mkdir(parents=True)
        (state / "snapshot.json").write_text("{}", encoding="utf-8")
        if pending:
            (state / "pending").write_text("pending\n", encoding="utf-8")
        return state

    def directories(self):
        return ["--input", "converted.toml", "--game-dir", str(self.game), "--source-dir", str(self.archives)]

    def test_the_recovery_directory_is_named_like_the_engine_names_it(self):
        state = ports.engine_state_dir(Path("/tmp/ports-fixture/game"))
        self.assertEqual(state, Path("/tmp/ports-fixture/.modsync-3bcc505ec6734337"))
        self.assertRegex(state.name, r"^\.modsync-[0-9a-f]{16}$")

    def test_removal_takes_only_this_games_recovery_sibling(self):
        state = self.state()
        other = self.root / ".modsync-0123456789abcdef"
        other.mkdir()
        (other / "pending").write_text("another game\n", encoding="utf-8")
        self.assertEqual(ports.remove_engine_state(self.game, (self.archives,)), "removed")
        self.assertFalse(state.exists())
        self.assertTrue((other / "pending").is_file())
        self.assertTrue(self.game.is_dir() and self.archives.is_dir())
        self.assertEqual(ports.remove_engine_state(self.game, (self.archives,)), "absent")

    def test_removal_refuses_a_symlink_a_plain_file_and_protected_paths(self):
        state = ports.engine_state_dir(self.game)
        target = self.root / "precious"
        target.mkdir()
        (target / "keep").write_text("x", encoding="utf-8")
        state.symlink_to(target, target_is_directory=True)
        with self.assertRaises(OSError):
            ports.remove_engine_state(self.game)
        self.assertTrue((target / "keep").is_file())
        state.unlink()
        state.write_text("file", encoding="utf-8")
        with self.assertRaises(OSError):
            ports.remove_engine_state(self.game)
        self.assertTrue(state.is_file())
        state.unlink()
        self.state()
        with self.assertRaises(OSError):
            ports.remove_engine_state(self.game, (ports.engine_state_dir(self.game),))
        with self.assertRaises(OSError):
            ports.remove_engine_state(self.game, (self.root,))
        self.assertTrue((ports.engine_state_dir(self.game) / "pending").is_file())

    def test_restore_arguments_come_from_the_install_directories(self):
        self.assertEqual(ports.restore_arguments(self.directories()),
                         ["--game-dir", str(self.game), "--source-dir", str(self.archives)])
        self.assertIsNone(ports.restore_arguments(["--input", "x", "--game-dir", str(self.game)]))
        self.assertIsNone(ports.restore_arguments(["--game-dir"]))

    def engine(self, returncode=0, output=b"", error=None):
        calls = []

        def fake(command, **kwargs):
            calls.append((command, kwargs))
            if error is not None:
                raise error
            kwargs["stdout"].write(output)
            return mock.Mock(returncode=returncode)

        with mock.patch.object(ports.subprocess, "run", fake), contextlib.redirect_stderr(io.StringIO()):
            record = ports.engine_restore(["/bin/modsync"], self.directories(), self.root, self.private,
                                          "engine_restore_1", self.report, {})
        return record, calls

    def test_the_cli_restore_subcommand_is_run_with_the_game_and_source_directories(self):
        record, calls = self.engine()
        self.assertEqual(record["status"], "restored")
        command, kwargs = calls[0]
        self.assertEqual(command, ["/bin/modsync", "restore", "--game-dir", str(self.game),
                                   "--source-dir", str(self.archives)])
        self.assertEqual(kwargs["cwd"], self.root)
        self.assertEqual(self.report["quarantine"][ports.ENGINE_RESTORE_KEY], [record])
        self.assertIn("engine_restore_1", self.report[ports.STAGE_DURATIONS_KEY])

    def test_no_unfinished_install_to_restore_is_not_a_failure(self):
        record, _ = self.engine(1, b"Error: There is no unfinished install to restore\n")
        self.assertEqual(record["status"], "nothing_pending")

    def test_other_failures_a_missing_binary_and_timeouts_are_reported(self):
        self.assertEqual(self.engine(1, b"Error: Snapshot hashes differ; refusing to restore\n")[0]["status"],
                         "failed")
        self.assertEqual(self.engine(error=FileNotFoundError("modsync"))[0]["status"], "unavailable")
        self.assertEqual(self.engine(error=ports.subprocess.TimeoutExpired("modsync", 1))[0]["status"], "failed")

    def test_without_directory_arguments_the_engine_restore_is_unavailable(self):
        record = ports.engine_restore(["/bin/modsync"], ["--input", "x"], self.root, self.private,
                                      "engine_restore_1", self.report, {})
        self.assertEqual(record["status"], "unavailable")

    def recover(self, **engine):
        calls = []

        def fake(command, **kwargs):
            calls.append(command)
            for child in ports.engine_state_dir(self.game).glob("pending"):
                if engine.get("clears"):
                    child.unlink()
            if engine.get("missing"):
                raise FileNotFoundError("modsync")
            return mock.Mock(returncode=engine.get("code", 0))

        with mock.patch.object(ports.subprocess, "run", fake), contextlib.redirect_stderr(io.StringIO()):
            ports.recover_engine_state(["/bin/modsync"], self.directories(), self.game, self.root,
                                       self.private, "engine_restore_1", self.report, {}, (self.archives,))
        return calls

    def test_a_successful_engine_restore_that_clears_the_marker_leaves_the_directory_alone(self):
        state = self.state()
        self.recover(clears=True)
        self.assertTrue(state.is_dir())
        self.assertFalse((state / "pending").exists())
        self.assertNotIn("fallback", self.report["quarantine"][ports.ENGINE_RESTORE_KEY][0])

    def test_a_missing_binary_falls_back_to_removing_the_recovery_directory(self):
        state = self.state()
        self.recover(missing=True)
        self.assertFalse(state.exists())
        self.assertEqual(self.report["quarantine"][ports.ENGINE_RESTORE_KEY][0]["fallback"], "state_removed")

    def test_a_restore_that_exits_zero_but_leaves_the_marker_raises(self):
        self.state()
        with self.assertRaises(OSError):
            self.recover()


class StageProgressTests(unittest.TestCase):
    class Counting(io.StringIO):
        flushes = 0

        def flush(self):
            self.flushes += 1
            super().flush()

    def run_stage(self, script, stage="validate"):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, root)
        report = {"commands": []}
        stderr = self.Counting()
        with contextlib.redirect_stderr(stderr):
            try:
                ports.run([__import__("sys").executable, "-c", script], root, root, stage, report, {})
            except ports.Blocked:
                pass
        return report, stderr

    def test_every_stage_logs_a_flushed_start_and_finish_line_and_records_its_duration(self):
        report, stderr = self.run_stage("print('ok')", "build")
        text = stderr.getvalue()
        self.assertIn("ports: build: started\n", text)
        self.assertRegex(text, r"ports: build: finished in [0-9.]+s \(exit 0\)\n")
        self.assertGreaterEqual(stderr.flushes, 2)
        self.assertGreaterEqual(report[ports.STAGE_DURATIONS_KEY]["build"], 0.0)
        self.assertIsInstance(report[ports.STAGE_DURATIONS_KEY]["build"], float)

    def test_a_failed_stage_still_records_its_duration_before_the_failure_evidence(self):
        report, stderr = self.run_stage("import sys; sys.exit(3)", "install_retry_1")
        text = stderr.getvalue()
        self.assertIn("install_retry_1", report[ports.STAGE_DURATIONS_KEY])
        self.assertLess(text.index("ports: install_retry_1: finished"), text.index("::group::install_retry_1"))
        self.assertIn("(exit 3)", text)

    def test_stages_accumulate_in_run_order(self):
        report = {"commands": []}
        for stage in ("build", "convert", "validate", "install", "install_retry_1"):
            ports.finish_stage(report, stage, 0.0, "exit 0")
        self.assertEqual(list(report[ports.STAGE_DURATIONS_KEY]),
                         ["build", "convert", "validate", "install", "install_retry_1"])

    def test_durations_are_formatted_for_a_person(self):
        self.assertEqual(ports.format_duration(3.04), "3.0s")
        self.assertEqual(ports.format_duration(125), "2m05s")
        self.assertEqual(ports.format_duration(3725), "1h02m05s")
        self.assertEqual(ports.format_duration(-1), "0.0s")

    def test_the_diagnostics_carry_a_stage_duration_summary(self):
        report = {"status": "installed", ports.STAGE_DURATIONS_KEY: {"build": 125.0, "install": 3.0}}
        log = io.StringIO()
        with contextlib.redirect_stderr(log):
            ports.print_diagnostics(report)
        self.assertIn("ports: stage durations: build 2m05s, install 3.0s; total 2m08s", log.getvalue())
        quiet = io.StringIO()
        with contextlib.redirect_stderr(quiet):
            ports.print_diagnostics({"status": "installed"})
        self.assertNotIn("stage durations", quiet.getvalue())


if __name__ == "__main__":
    unittest.main()
