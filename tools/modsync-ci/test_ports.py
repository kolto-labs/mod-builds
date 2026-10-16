import argparse
import contextlib
import hashlib
import io
import json
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
        with mock.patch.object(ports, "run", fake_run):
            ports.execute(args, report)
        staged = dict(commands)
        for stage in ("validate", "install"):
            command = staged[stage]
            self.assertEqual([value for flag, value in zip(command, command[1:]) if flag == "--deselect"],
                             [PACK_A_GUID])
            self.assertNotIn(PACK_B_GUID, command)
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
        with mock.patch.object(ports, "run", fake_run):
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

    def test_rust_gui_refuses_to_run_a_driver_that_cannot_deselect(self):
        commands, _, error = self.run_policy_lane("gui")
        self.assertEqual(error, "gui_cannot_deselect_policy_skipped_components")
        self.assertNotIn("validate", dict(commands))

    def test_typescript_refuses_policy_skips(self):
        _, _, error = self.run_policy_lane("cli", implementation="typescript")
        self.assertEqual(error, "policy_skips_supported_for_rust_only")

    def test_rust_lane_without_restore_report_is_unchanged(self):
        _, args = self.prepare("k1")
        args.implementation = "rust"
        commands, report, _ = self.captured(
            args, document(component("Mod One", "30000000-0000-0000-0000-000000000001")))
        self.assertNotIn("skipped_by_reviewed_policy", report)
        self.assertNotIn("--deselect", dict(commands)["convert"])


if __name__ == "__main__":
    unittest.main()
