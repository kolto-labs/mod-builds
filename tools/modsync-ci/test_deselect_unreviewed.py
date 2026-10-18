import contextlib
import io
import json
from pathlib import Path
import shutil
import tempfile
import unittest

import deselect_unreviewed as tool
import ports

GUIDE = """# Guide

## Mod List

### 4GB Patcher

### Mod One

### Mod Two

### Mod Three

### Mod Four

## Enhanced Widescreen Support Mods

### Wide One
"""

GUIDS = {
    "4GB Patcher": "90000000-0000-0000-0000-00000000000a",
    "Mod One": "90000000-0000-0000-0000-00000000000b",
    "Mod Two": "90000000-0000-0000-0000-00000000000c",
    "Mod Three": "90000000-0000-0000-0000-00000000000d",
    "Mod Four": "90000000-0000-0000-0000-00000000000e",
    "Wide One": "90000000-0000-0000-0000-00000000000f",
}


def converted(dependencies=None):
    dependencies = dependencies or {}
    blocks = ["[Config]", 'TargetGame = "KOTOR2"', ""]
    for name, guid in GUIDS.items():
        blocks += ["[[thisMod]]", 'Guid = "' + guid.upper() + '"', 'Name = "' + name + '"',
                   "Dependencies = [" + ", ".join('"' + GUIDS[each] + '"' for each in dependencies.get(name, [])) + "]",
                   ""]
    return "\n".join(blocks)


def log_line(name, prefix="2026-10-18T15:01:30.4496756Z "):
    return prefix + "ports: unreviewed draft still selected: " + GUIDS[name].upper() + " (" + name + ")"


def log(*names):
    return "\n".join(["noise", *[log_line(name) for name in names], "::error::ports: blocked"])


HAND = {"section": "Mod List", "heading": "4GB Patcher", "reason": "reviewed: 4GB"}


class ParseLogTests(unittest.TestCase):
    def test_lines_are_found_with_timestamps_lowercased_and_deduplicated(self):
        text = log("Mod One", "Mod Two") + "\n" + log("Mod Two")
        self.assertEqual(tool.parse_logs([text]), {GUIDS["Mod One"]: "Mod One", GUIDS["Mod Two"]: "Mod Two"})

    def test_several_logs_are_unioned(self):
        found = tool.parse_logs([log("Mod One"), log("Mod Two", "Wide One")])
        self.assertEqual(set(found), {GUIDS[name] for name in ("Mod One", "Mod Two", "Wide One")})

    def test_other_lines_and_nested_option_headings_are_not_components(self):
        found = tool.parse_logs(["ports: verified-draft review cleared 3 component(s); left fenced: 4\n"])
        self.assertEqual(found, {})


class GenerateEntriesTests(unittest.TestCase):
    def generate(self, names, dependencies=None, manual=(HAND,)):
        found = {GUIDS[name]: name for name in names}
        return tool.generate_entries(GUIDE, converted(dependencies), found, manual)

    def test_entries_follow_guide_order_with_section_heading_guid_kind_and_reviewed_reason(self):
        entries = self.generate(["Wide One", "Mod Two", "Mod One"])
        self.assertEqual([(entry["section"], entry["heading"], entry["guid"], entry["kind"]) for entry in entries], [
            ("Mod List", "Mod One", GUIDS["Mod One"], "unreviewed_draft"),
            ("Mod List", "Mod Two", GUIDS["Mod Two"], "unreviewed_draft"),
            ("Enhanced Widescreen Support Mods", "Wide One", GUIDS["Wide One"], "unreviewed_draft"),
        ])
        self.assertTrue(all(entry["reason"] == tool.DRAFT_REASON for entry in entries))
        self.assertIn("delegated by owner 2026-10-18", tool.DRAFT_REASON)

    def test_dependents_of_deselected_components_are_added_transitively(self):
        entries = self.generate(["Mod One"], {"Mod Three": ["Mod One"], "Mod Four": ["Mod Three"]})
        by_heading = {entry["heading"]: entry for entry in entries}
        self.assertEqual(list(by_heading), ["Mod One", "Mod Three", "Mod Four"])
        self.assertEqual(by_heading["Mod Three"]["kind"], "dependent_of_deselected")
        self.assertIn("depends on Mod One", by_heading["Mod Three"]["reason"])
        self.assertIn("depends on Mod Three", by_heading["Mod Four"]["reason"])

    def test_a_dependent_that_is_already_listed_stays_an_unreviewed_draft(self):
        entries = self.generate(["Mod One", "Mod Three"], {"Mod Three": ["Mod One"]})
        self.assertEqual([entry["kind"] for entry in entries], ["unreviewed_draft", "unreviewed_draft"])

    def test_dependents_of_a_hand_authored_skip_are_added_and_the_hand_entry_is_not_repeated(self):
        entries = self.generate(["Mod One"], {"Mod Two": ["4GB Patcher"]})
        self.assertEqual([entry["heading"] for entry in entries], ["Mod One", "Mod Two"])

    def test_a_guid_the_guide_does_not_have_fails(self):
        with self.assertRaisesRegex(tool.GenerationError, "not top-level components"):
            tool.generate_entries(GUIDE, converted(), {"90000000-0000-0000-0000-0000000000ff": "Ghost"}, ())

    def test_a_name_that_differs_from_the_guide_fails(self):
        with self.assertRaisesRegex(tool.GenerationError, "is named"):
            tool.generate_entries(GUIDE, converted(), {GUIDS["Mod One"]: "Renamed"}, ())

    def test_a_converted_file_that_does_not_line_up_with_the_guide_fails(self):
        with self.assertRaisesRegex(tool.GenerationError, "do not match"):
            tool.generate_entries(GUIDE + "\n### Extra Wide\n", converted(),
                                  {GUIDS["Mod One"]: "Mod One"}, ())

    def test_a_heading_repeated_in_the_counted_sections_fails(self):
        guide = GUIDE.replace("### Mod Two", "### Mod One")
        text = converted().replace('Name = "Mod Two"', 'Name = "Mod One"')
        with self.assertRaisesRegex(tool.GenerationError, "ambiguous"):
            tool.generate_entries(guide, text, {GUIDS["Mod One"]: "Mod One"}, ())


class ApplyEntriesTests(unittest.TestCase):
    def test_hand_authored_entries_are_kept_and_generated_ones_are_replaced(self):
        old = {"direct_links": {"x": 1}, "lane_skips": {
            "k1": [HAND, {**HAND, "heading": "Old", "guid": GUIDS["Mod One"], "kind": "unreviewed_draft"}],
            "k2": [HAND]}}
        entries = [{"section": "Mod List", "heading": "Mod Two", "reason": "r", "guid": GUIDS["Mod Two"],
                    "kind": "unreviewed_draft"}]
        result = tool.apply_entries(old, "k1", entries)
        self.assertEqual(result["lane_skips"]["k1"], [HAND] + entries)
        self.assertEqual(result["lane_skips"]["k2"], [HAND])
        self.assertEqual(result["direct_links"], {"x": 1})
        self.assertEqual(tool.apply_entries(result, "k1", entries), result)
        self.assertEqual(len(old["lane_skips"]["k1"]), 2)

    def test_an_unknown_game_fails(self):
        with self.assertRaises(tool.GenerationError):
            tool.apply_entries({}, "k3", [])


class MainTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.directory)
        self.guide = self.directory / "full.md"
        self.guide.write_text(GUIDE, encoding="utf-8")
        self.converted = self.directory / "converted.toml"
        self.converted.write_text(converted({"Mod Three": ["Mod One"]}), encoding="utf-8")
        self.log = self.directory / "install.log"
        self.log.write_text(log("Mod One", "Mod Two"), encoding="utf-8")
        self.selections = self.directory / "selections.json"
        self.selections.write_text(json.dumps({"lane_skips": {"k1": [HAND], "k2": [HAND]}}, indent=2) + "\n",
                                   encoding="utf-8")

    def run_tool(self, *extra, log_path=None):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = tool.main(["--game", "k1", "--guide", str(self.guide), "--converted", str(self.converted),
                              "--log", str(log_path or self.log), "--selections", str(self.selections), *extra])
        return code, stdout.getvalue(), stderr.getvalue()

    def test_default_prints_the_result_and_changes_nothing(self):
        before = self.selections.read_text(encoding="utf-8")
        code, out, err = self.run_tool()
        self.assertEqual(code, 0)
        self.assertEqual(self.selections.read_text(encoding="utf-8"), before)
        self.assertEqual([entry["heading"] for entry in json.loads(out)["lane_skips"]["k1"]],
                         ["4GB Patcher", "Mod One", "Mod Two", "Mod Three"])
        self.assertIn("3 lane skip(s): 2 unreviewed draft(s), 1 dependent(s)", err)

    def test_write_then_check_is_reproducible_and_the_result_loads_in_ports(self):
        self.assertEqual(self.run_tool("--check")[0], 1)
        self.assertEqual(self.run_tool("--write")[0], 0)
        self.assertEqual(self.run_tool("--check")[0], 0)
        skips = ports.load_lane_skips("k1", self.selections)
        self.assertEqual([skip["heading"] for skip in skips], ["4GB Patcher", "Mod One", "Mod Two", "Mod Three"])
        self.assertEqual(ports.load_lane_skips("k2", self.selections)[0]["heading"], "4GB Patcher")

    def test_a_log_without_unreviewed_lines_is_an_error_not_an_empty_table(self):
        empty = self.directory / "empty.log"
        empty.write_text("nothing here\n", encoding="utf-8")
        code, _, err = self.run_tool("--write", log_path=empty)
        self.assertEqual(code, 2)
        self.assertIn("no 'unreviewed draft still selected' lines", err)

    def test_generation_errors_exit_2_and_leave_the_file_alone(self):
        before = self.selections.read_text(encoding="utf-8")
        self.log.write_text(log("Mod One").replace("(Mod One)", "(Other)"), encoding="utf-8")
        code, _, err = self.run_tool("--write")
        self.assertEqual(code, 2)
        self.assertIn("is named", err)
        self.assertEqual(self.selections.read_text(encoding="utf-8"), before)


if __name__ == "__main__":
    unittest.main()
