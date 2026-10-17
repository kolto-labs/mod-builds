import argparse
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import tomllib
import unittest
from unittest import mock

import gui_rust

BASE_ONE = "10000000-0000-0000-0000-000000000001"
BASE_TWO = "10000000-0000-0000-0000-000000000002"
BASE_THREE = "10000000-0000-0000-0000-000000000003"
WIDE_ONE = "10000000-0000-0000-0000-0000000000a1"
WIDE_TWO = "10000000-0000-0000-0000-0000000000a2"


def block(guid, name, selected=True, widescreen=False, extra=()):
    lines = ["[[thisMod]]", 'Guid = "' + guid + '"', 'Name = "' + name + '"',
             "IsSelected = " + ("true" if selected else "false")]
    if widescreen:
        lines.append("WidescreenOnly = true")
    lines += list(extra)
    lines += ["[[thisMod.Instructions]]", 'Guid = "' + guid[:-1] + '9"', 'Action = "Move"', ""]
    return "\n".join(lines)


def document(*blocks):
    return '[metadata]\nfileFormatVersion = "2.0"\n\n' + "\n".join(blocks)


def components(text):
    return tomllib.loads(text)["thisMod"]


class PlanSelectionTests(unittest.TestCase):
    def full(self, **kwargs):
        return components(document(
            block(BASE_ONE, "A"), block(BASE_TWO, "B", selected=False),
            block(WIDE_ONE, "W", widescreen=True, **kwargs)))

    def test_plain_base_input_keeps_the_select_all_flow(self):
        plan = gui_rust.plan_selection(components(document(block(BASE_ONE, "A"), block(BASE_TWO, "B"))))
        self.assertFalse(plan["subset"])
        self.assertTrue(plan["click_select_all"])
        self.assertEqual(plan["expected_components"], [BASE_ONE, BASE_TWO])
        self.assertEqual(plan["widescreen_phase"], "none_in_input")
        self.assertEqual(plan["skipped_by_reviewed_policy"], [])

    def test_selected_widescreen_is_refused_without_the_exclusion(self):
        with self.assertRaisesRegex(gui_rust.Blocked, "single_phase_only"):
            gui_rust.plan_selection(self.full())

    def test_unselected_widescreen_is_reported_not_refused(self):
        items = components(document(block(BASE_ONE, "A"),
                                    block(WIDE_ONE, "W", selected=False, widescreen=True)))
        plan = gui_rust.plan_selection(items)
        self.assertEqual(plan["excluded_widescreen"], [WIDE_ONE])
        self.assertEqual(plan["widescreen_phase"], "not_covered_by_gui_lane")
        self.assertEqual(plan["expected_components"], [BASE_ONE])
        self.assertTrue(plan["subset"])

    def test_exclude_widescreen_deselects_and_reports_it(self):
        plan = gui_rust.plan_selection(self.full(), exclude_widescreen=True)
        self.assertEqual(plan["excluded_widescreen"], [WIDE_ONE])
        self.assertEqual(plan["expected_components"], [BASE_ONE, BASE_TWO])
        self.assertEqual((plan["base_selected"], plan["base_total"]), (2, 2))
        self.assertTrue(plan["click_select_all"])
        self.assertEqual(plan["skipped_by_reviewed_policy"], [])

    def test_policy_skip_of_base_component_stops_select_all(self):
        plan = gui_rust.plan_selection(self.full(), [BASE_TWO.upper()], exclude_widescreen=True)
        self.assertFalse(plan["click_select_all"])
        self.assertEqual(plan["skipped_by_reviewed_policy"], [BASE_TWO])
        self.assertEqual(plan["expected_components"], [BASE_ONE])
        self.assertEqual((plan["base_selected"], plan["base_total"]), (1, 2))

    def test_policy_skip_of_widescreen_component_is_not_double_counted(self):
        plan = gui_rust.plan_selection(self.full(), [WIDE_ONE])
        self.assertEqual(plan["skipped_by_reviewed_policy"], [WIDE_ONE])
        self.assertEqual(plan["excluded_widescreen"], [])
        self.assertEqual(plan["widescreen_phase"], "not_covered_by_gui_lane")
        self.assertTrue(plan["click_select_all"])

    def test_unknown_or_repeated_deselect_guid_blocks(self):
        with self.assertRaisesRegex(gui_rust.Blocked, "deselect_guid_not_in_input"):
            gui_rust.plan_selection(self.full(), [BASE_THREE])
        with self.assertRaisesRegex(gui_rust.Blocked, "deselect_guid_duplicate"):
            gui_rust.plan_selection(self.full(), [BASE_ONE, BASE_ONE.upper()], True)

    def test_selected_component_depending_on_a_deselected_one_blocks(self):
        items = components(document(
            block(BASE_ONE, "A", extra=['Dependencies = ["' + BASE_TWO + '"]']), block(BASE_TWO, "B")))
        with self.assertRaisesRegex(gui_rust.Blocked, "selected_component_depends_on_deselected_component"):
            gui_rust.plan_selection(items, [BASE_TWO])
        items = components(document(
            block(BASE_ONE, "A", extra=['Dependencies = ["' + WIDE_ONE + '"]']),
            block(WIDE_ONE, "W", widescreen=True)))
        with self.assertRaisesRegex(gui_rust.Blocked, "selected_component_depends_on_deselected_component"):
            gui_rust.plan_selection(items, exclude_widescreen=True)

    def test_everything_deselected_blocks_instead_of_letting_the_gui_select_all(self):
        items = components(document(block(BASE_ONE, "A")))
        with self.assertRaisesRegex(gui_rust.Blocked, "no_component_left_selected"):
            gui_rust.plan_selection(items, [BASE_ONE])

    def test_draft_review_still_refuses_an_installed_component(self):
        review = ["DraftReview = { RequiresReview = true, DraftInstructionCount = 1 }"]
        items = components(document(block(BASE_ONE, "A", extra=review), block(BASE_TWO, "B")))
        with self.assertRaisesRegex(gui_rust.Blocked, "draft_review_receipts_require_human_source_review"):
            gui_rust.plan_selection(items)
        reviewed = ["DraftReview = { RequiresReview = false }"]
        items = components(document(block(BASE_ONE, "A", extra=reviewed)))
        gui_rust.plan_selection(items)
        malformed = ['DraftReview = "yes"']
        with self.assertRaisesRegex(gui_rust.Blocked, "draft_review_receipts_require_human_source_review"):
            gui_rust.plan_selection(components(document(block(BASE_ONE, "A", extra=malformed))))

    def test_draft_review_on_a_deselected_component_is_not_installed_and_not_authorized(self):
        review = ["DraftReview = { RequiresReview = true, DraftInstructionCount = 0 }"]
        items = components(document(block(BASE_ONE, "A"),
                                    block(WIDE_ONE, "W", widescreen=True, extra=review)))
        plan = gui_rust.plan_selection(items, [WIDE_ONE])
        self.assertEqual(plan["expected_components"], [BASE_ONE])
        with self.assertRaisesRegex(gui_rust.Blocked, "draft_review_receipts_require_human_source_review"):
            gui_rust.plan_selection(items, exclude_widescreen=False)

    def test_draft_review_refusal_precedes_widescreen_refusal(self):
        review = ["DraftReview = { RequiresReview = true }"]
        items = components(document(block(BASE_ONE, "A", extra=review),
                                    block(WIDE_ONE, "W", widescreen=True)))
        with self.assertRaisesRegex(gui_rust.Blocked, "draft_review_receipts_require"):
            gui_rust.plan_selection(items)


class RewriteSelectionTests(unittest.TestCase):
    def test_writes_only_isselected_and_preserves_everything_else(self):
        text = document(
            block(BASE_ONE, "A"), block(BASE_TWO, "B", selected=False),
            block(WIDE_ONE, "W", widescreen=True))
        text = text.replace("[[thisMod.Instructions]]", "[[thisMod.Instructions]]\nIsSelected = true", 1)
        rewritten = gui_rust.rewrite_selection(text, [True, False, False])
        self.assertEqual([item["IsSelected"] for item in components(rewritten)], [True, False, False])
        self.assertIn("IsSelected = true", rewritten.split("[[thisMod.Instructions]]")[1])
        self.assertEqual(rewritten.count("IsSelected"), 4)

    def test_adds_isselected_when_the_component_has_none(self):
        text = document(block(BASE_ONE, "A")).replace("IsSelected = true\n", "")
        rewritten = gui_rust.rewrite_selection(text, [False])
        self.assertIs(components(rewritten)[0]["IsSelected"], False)

    def test_component_table_name_is_supported(self):
        text = document(block(BASE_ONE, "A")).replace("thisMod", "Components")
        self.assertIs(gui_rust.table_key(tomllib.loads(text)) == "Components", True)
        rewritten = gui_rust.rewrite_selection(text, [False])
        self.assertIs(tomllib.loads(rewritten)["Components"][0]["IsSelected"], False)

    def test_wrong_component_count_blocks(self):
        with self.assertRaisesRegex(gui_rust.Blocked, "selection_rewrite_incomplete"):
            gui_rust.rewrite_selection(document(block(BASE_ONE, "A")), [True, False])
        with self.assertRaisesRegex(gui_rust.Blocked, "selection_rewrite_incomplete"):
            gui_rust.rewrite_selection(document(block(BASE_ONE, "A"), block(BASE_TWO, "B")), [True])


class ReceiptTests(unittest.TestCase):
    def setUp(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root)
        self.game = (root / "game").resolve()
        self.game.mkdir()
        (self.game / "swkotor.exe").write_bytes(b"exe")
        self.args = argparse.Namespace(game_dir=self.game, archives=(root / "mods").resolve())
        key = hashlib.sha256(str(self.game).encode()).hexdigest()[:16]
        self.state = root / (".modsync-" + key)
        self.state.mkdir()

    def receipt(self, guids):
        (self.state / "completion.json").write_text(json.dumps({
            "game": str(self.game), "mods": str(self.args.archives),
            "components": [{"Guid": guid} for guid in guids],
            "files": {"swkotor.exe": hashlib.sha256(b"exe").hexdigest()}}), encoding="utf-8")

    def verify(self, expected):
        result = {}
        gui_rust.verify_receipt(self.args, result, lambda: None, expected)
        return result

    def test_receipt_must_match_the_expected_subset_exactly(self):
        self.receipt([BASE_ONE.upper()])
        result = self.verify([BASE_ONE])
        self.assertEqual((result["completed_components"], result["status"]), (1, "installed"))

    def test_receipt_that_includes_a_skipped_component_blocks(self):
        self.receipt([BASE_ONE, BASE_TWO])
        with self.assertRaisesRegex(gui_rust.Blocked, "completion_does_not_cover_full_input"):
            self.verify([BASE_ONE])

    def test_receipt_missing_an_expected_component_blocks(self):
        self.receipt([BASE_ONE])
        with self.assertRaisesRegex(gui_rust.Blocked, "completion_does_not_cover_full_input"):
            self.verify([BASE_ONE, BASE_TWO])


class ExecuteTests(unittest.TestCase):
    def prepare(self, text):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root)
        for name in ("game", "archives"):
            (root / name).mkdir()
        binary = root / "modsync"
        binary.write_text("#!/bin/sh\n", encoding="utf-8")
        binary.chmod(0o755)
        source = root / "input.toml"
        source.write_text(text, encoding="utf-8")
        return root, argparse.Namespace(
            binary=binary, input=source, game_dir=root / "game", archives=root / "archives",
            work_dir=root / "work", report=root / "report.json", timeout=60.0,
            deselect=[], exclude_widescreen=False, private_worker=None)

    def run_execute(self, args, completed=None):
        result = {}
        launched = []

        class Process:
            def __init__(self, command, **kwargs):
                launched.append(command)
                self.pid = 0

            def wait(self, timeout=None):
                private = Path(launched[0][-1])
                payload = {"status": "installed", "reason": "native_gui_completed_and_receipt_verified",
                           "completed_components": completed}
                (private / "result.json").write_text(json.dumps(payload), encoding="utf-8")
                return 0

        with mock.patch.object(gui_rust.shutil, "which", return_value="/bin/true"), \
                mock.patch.object(gui_rust.subprocess, "Popen", Process), \
                mock.patch.object(gui_rust, "stop"):
            try:
                gui_rust.execute(args, result)
                error = None
            except gui_rust.Blocked as blocked:
                error = str(blocked)
        return result, error

    def test_subset_writes_a_private_copy_and_leaves_the_input_untouched(self):
        text = document(block(BASE_ONE, "A"), block(BASE_TWO, "B"),
                        block(WIDE_ONE, "W", widescreen=True))
        root, args = self.prepare(text)
        args.deselect = [BASE_TWO]
        args.exclude_widescreen = True
        result, error = self.run_execute(args, completed=1)
        self.assertIsNone(error)
        self.assertEqual(args.input.read_text(encoding="utf-8"), text)
        private = Path(result["evidence_directory"])
        driven = components((private / "input.toml").read_text(encoding="utf-8"))
        self.assertEqual([item["IsSelected"] for item in driven], [True, False, False])
        plan = json.loads((private / "selection-plan.json").read_text(encoding="utf-8"))
        self.assertEqual((plan["click_select_all"], plan["subset"]), (False, True))
        self.assertEqual(result["skipped_by_reviewed_policy"], [BASE_TWO])
        self.assertEqual(result["excluded_widescreen"], [WIDE_ONE])
        self.assertEqual(result["widescreen_phase"], "not_covered_by_gui_lane")
        self.assertEqual((result["coverage"], result["component_count"], result["expected_components"]),
                         ("subset", 3, 1))
        self.assertEqual(result["input_sha256"], hashlib.sha256(text.encode()).hexdigest())
        self.assertNotEqual(result["driven_input_sha256"], result["input_sha256"])

    def test_worker_report_that_covers_more_than_the_subset_blocks(self):
        text = document(block(BASE_ONE, "A"), block(BASE_TWO, "B"))
        _, args = self.prepare(text)
        args.deselect = [BASE_TWO]
        _, error = self.run_execute(args, completed=2)
        self.assertEqual(error, "completion_does_not_cover_expected_subset")

    def test_unchanged_input_keeps_the_legacy_flow(self):
        text = document(block(BASE_ONE, "A"), block(BASE_TWO, "B"))
        _, args = self.prepare(text)
        result, error = self.run_execute(args, completed=2)
        self.assertIsNone(error)
        private = Path(result["evidence_directory"])
        self.assertEqual((private / "input.toml").read_text(encoding="utf-8"), text)
        self.assertEqual(result["coverage"], "full")
        plan = json.loads((private / "selection-plan.json").read_text(encoding="utf-8"))
        self.assertTrue(plan["click_select_all"])

    def test_selected_widescreen_input_is_still_refused(self):
        _, args = self.prepare(document(block(BASE_ONE, "A"), block(WIDE_ONE, "W", widescreen=True)))
        _, error = self.run_execute(args)
        self.assertEqual(error, "full_input_contains_separate_widescreen_phase_driver_supports_single_phase_only")


class ArgumentTests(unittest.TestCase):
    def test_selection_flags_are_parsed_and_forwarded_to_the_worker(self):
        seen = {}

        def capture(args, result):
            seen["deselect"], seen["exclude"] = args.deselect, args.exclude_widescreen
            seen["argv"] = list(gui_rust.sys.argv[1:])
            result["status"] = "installed"

        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root)
        argv = ["gui_rust.py", "--binary", "b", "--input", "i", "--game-dir", "g", "--archives", "a",
                "--work-dir", "w", "--report", str(root / "report.json"), "--timeout", "5",
                "--deselect", BASE_ONE, "--deselect", BASE_TWO, "--exclude-widescreen"]
        with mock.patch.object(gui_rust.sys, "argv", argv), \
                mock.patch.object(gui_rust, "execute", capture), \
                mock.patch("builtins.print"):
            self.assertEqual(gui_rust.main(), 0)
        self.assertEqual(seen["deselect"], [BASE_ONE, BASE_TWO])
        self.assertTrue(seen["exclude"])
        self.assertIn("--exclude-widescreen", seen["argv"])
        report = json.loads((root / "report.json").read_text(encoding="utf-8"))
        self.assertTrue(any("never installed" in item for item in report["limitations"]))


if __name__ == "__main__":
    unittest.main()
