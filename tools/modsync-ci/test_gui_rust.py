import argparse
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import tomllib
import unittest
from unittest import mock

import gui_diag
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

    def run_execute(self, args, completed=None, application=None):
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

        def agree(binary, driven_input, game_dir, select_all, private, **kwargs):
            document = components(Path(driven_input).read_text(encoding="utf-8"))
            chosen = [str(item["Guid"]).lower() for item in document if item["IsSelected"]]
            total = sum(item.get("WidescreenOnly") is not True for item in document)
            queried.append((select_all, chosen))
            return chosen, {"selected": len([item for item in document if item["IsSelected"]
                                             and item.get("WidescreenOnly") is not True]),
                            "total": total}

        queried = self.queried = []
        query = application or agree
        with mock.patch.object(gui_rust.shutil, "which", return_value="/bin/true"), \
                mock.patch.object(gui_rust.subprocess, "Popen", Process), \
                mock.patch.object(gui_rust, "query_application_selection", query), \
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


class ApplicationSelectionTests(unittest.TestCase):
    prepare = ExecuteTests.prepare
    run_execute = ExecuteTests.run_execute

    def completed(self, payload=None, code=0, stderr=""):
        return mock.Mock(stdout=json.dumps(payload) if payload is not None else "", stderr=stderr,
                         returncode=code)

    def query(self, outcome, select_all=False):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root)
        calls = []

        def run(command, **kwargs):
            calls.append((command, kwargs))
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        answer = gui_rust.query_application_selection(
            Path("/bin/modsync"), root / "input.toml", root / "game", select_all, root, run=run)
        return answer, calls[0], root

    def test_asks_the_real_binary_with_the_game_dir_and_the_file_selection(self):
        payload = {"counts": {"selected": 2, "total": 3, "required": 0, "optional": 2},
                   "selected": [BASE_ONE.upper(), BASE_TWO]}
        (selected, counts), (command, kwargs), root = self.query(self.completed(payload))
        self.assertEqual((selected, counts), ([BASE_ONE, BASE_TWO], {"selected": 2, "total": 3}))
        self.assertEqual(command[:2], ["/bin/modsync", "selection"])
        self.assertIn("--use-file-selection", command)
        self.assertEqual(command[command.index("--game-dir") + 1], str(root / "game"))
        self.assertNotIn("--select-visible-search", command)
        self.assertEqual(kwargs["cwd"], root)
        self.assertTrue(str(kwargs["env"]["HOME"]).startswith(str(root)))

    def test_select_all_is_the_filtered_bulk_selection_over_every_base_component(self):
        payload = {"counts": {"selected": 1, "total": 1}, "selected": [BASE_ONE]}
        _, (command, _), _ = self.query(self.completed(payload), select_all=True)
        self.assertEqual(command[command.index("--select-visible-search") + 1], "")

    def test_failed_unreadable_or_missing_binary_blocks(self):
        for outcome in (self.completed(code=2, stderr="boom"), self.completed(),
                        self.completed({"counts": {}}), OSError("gone"),
                        gui_rust.subprocess.TimeoutExpired("x", 1)):
            with self.assertRaisesRegex(gui_rust.Blocked, "application_selection_query_failed"):
                self.query(outcome)

    def plan(self):
        return gui_rust.plan_selection(
            components(document(block(BASE_ONE, "A"), block(BASE_TWO, "B"), block(BASE_THREE, "C"))),
            [BASE_THREE])

    def test_matching_application_selection_keeps_the_plan_numbers(self):
        plan, result = self.plan(), {}
        gui_rust.reconcile_selection(plan, [BASE_ONE, BASE_TWO], {"selected": 2, "total": 3}, result)
        self.assertEqual((plan["base_selected"], plan["base_total"]), (2, 3))
        self.assertEqual(result["selection_oracle"]["status"], "matched")

    def test_application_unselecting_a_planned_component_blocks_and_names_it(self):
        plan, result = self.plan(), {}
        with self.assertRaisesRegex(gui_rust.Blocked, "application_selection_differs_from_plan"):
            gui_rust.reconcile_selection(plan, [BASE_ONE], {"selected": 1, "total": 3}, result)
        oracle = result["selection_oracle"]
        self.assertEqual((oracle["unselected_by_application"], oracle["selected_by_application"]),
                         ([BASE_TWO], []))
        self.assertEqual((oracle["application_base_selected"], oracle["plan_base_selected"]), (1, 2))

    def test_application_selecting_an_unplanned_component_blocks_and_names_it(self):
        result = {}
        with self.assertRaisesRegex(gui_rust.Blocked, "application_selection_differs_from_plan"):
            gui_rust.reconcile_selection(self.plan(), [BASE_ONE, BASE_TWO, BASE_THREE],
                                         {"selected": 3, "total": 3}, result)
        self.assertEqual(result["selection_oracle"]["selected_by_application"], [BASE_THREE])

    def test_execute_blocks_before_launching_and_reports_the_oracle(self):
        _, args = self.prepare(document(block(BASE_ONE, "A"), block(BASE_TWO, "B")))
        args.deselect = [BASE_TWO]

        def application(*arguments, **kwargs):
            return [], {"selected": 0, "total": 2}

        result, error = self.run_execute(args, completed=1, application=application)
        self.assertEqual(error, "application_selection_differs_from_plan")
        self.assertEqual(result["selection_oracle"]["unselected_by_application"], [BASE_ONE])
        self.assertNotIn("worker_diagnostics", result)

    def test_execute_queries_with_the_private_copy_and_the_select_all_decision(self):
        _, args = self.prepare(document(block(BASE_ONE, "A"), block(BASE_TWO, "B"),
                                        block(WIDE_ONE, "W", widescreen=True)))
        args.exclude_widescreen = True
        result, error = self.run_execute(args, completed=2)
        self.assertIsNone(error)
        self.assertEqual(self.queried, [(True, [BASE_ONE, BASE_TWO])])
        _, args = self.prepare(document(block(BASE_ONE, "A"), block(BASE_TWO, "B")))
        args.deselect = [BASE_TWO]
        self.run_execute(args, completed=1)
        self.assertEqual(self.queried, [(False, [BASE_ONE])])

    def test_unchanged_input_does_not_query(self):
        _, args = self.prepare(document(block(BASE_ONE, "A"), block(BASE_TWO, "B")))
        _, error = self.run_execute(args, completed=2)
        self.assertIsNone(error)
        self.assertEqual(self.queried, [])


class SummaryWaitTests(unittest.TestCase):
    def setUp(self):
        self.private = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.private)
        self.plan = {"base_selected": 30, "base_total": 145}
        self.now = 0.0
        self.shots = []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds

    def observer(self, *texts):
        readings = iter(texts)

        def observe():
            self.now += 5.0
            shot = mock.Mock()
            shot.save.side_effect = lambda path: Path(path).write_bytes(b"png")
            self.shots.append(shot)
            text = next(readings)
            return shot, {}, [(line, []) for line in text.splitlines()]

        return observe

    def wait(self, *texts, window=15):
        result = {}
        try:
            gui_rust.wait_for_selection_summary(
                self.observer(*texts), self.plan, result, self.private, window=window,
                clock=self.clock, sleep=self.sleep)
            error = None
        except gui_rust.Blocked as blocked:
            error = str(blocked)
        return result["selection_summary"], error

    def test_matching_line_is_recorded_with_a_screenshot(self):
        summary, error = self.wait("Summary\nBase build: 30 of 145 mods selected - 115 mods not selected")
        self.assertIsNone(error)
        self.assertEqual((summary["matched"], summary["attempts"]), (True, 1))
        self.assertEqual(summary["screenshot"], gui_rust.SUMMARY_SCREENSHOT)
        self.assertTrue((self.private / gui_rust.SUMMARY_SCREENSHOT).is_file())
        self.assertEqual(json.loads((self.private / "selection-summary.json").read_text())["matched"], True)

    def test_ocr_noise_is_retried_until_the_expected_count_reads(self):
        summary, error = self.wait("nothing yet", "Base build: 3O of 145 mods selected",
                                   "Base build 30 of 145 mods selected")
        self.assertIsNone(error)
        self.assertEqual((summary["matched"], summary["attempts"]), (True, 3))
        self.assertIn("Base build: 3O of 145 mods selected", summary["observed"])

    def test_a_different_count_is_never_accepted_and_is_recorded_before_blocking(self):
        summary, error = self.wait(*(["Base build: 27 of 145 mods selected"] * 10))
        self.assertEqual(error, "selection_summary_does_not_match_expected_subset")
        self.assertFalse(summary["matched"])
        self.assertEqual(summary["observed"], ["Base build: 27 of 145 mods selected"])
        self.assertEqual(summary["expected"], "Base build: 30 of 145 mods selected")
        self.assertGreater(summary["attempts"], 1)
        self.assertLessEqual(self.now, 15 + 5 + 0.5)
        self.assertEqual(summary["screenshot"], gui_rust.SUMMARY_SCREENSHOT)
        self.assertTrue((self.private / gui_rust.SUMMARY_SCREENSHOT).is_file())

    def test_a_total_that_differs_is_not_accepted(self):
        _, error = self.wait(*(["Base build: 30 of 151 mods selected"] * 10))
        self.assertEqual(error, "selection_summary_does_not_match_expected_subset")

    def test_a_missing_line_records_that_nothing_was_read(self):
        summary, error = self.wait(*(["Summary / selection"] * 10))
        self.assertEqual(error, "selection_summary_does_not_match_expected_subset")
        self.assertEqual(summary["observed"], [])


class SummaryNavigationConstantTests(unittest.TestCase):
    def test_nav_label_tolerates_ocr_noise_in_the_slash(self):
        for text in ("Summary / selection", "Summary | selection", "Summary I selection", "Summary/selection"):
            self.assertRegex(text, gui_rust.SUMMARY_NAV_PATTERN)

    def test_page_marker_needs_the_summary_page_not_the_welcome_page(self):
        self.assertTrue(gui_rust.SUMMARY_PAGE_MARKER.search("Base build: 30 of 145 mods selected"))
        self.assertFalse(gui_rust.SUMMARY_PAGE_MARKER.search("Installing multiple KOTOR mods is tedious"))
        self.assertFalse(gui_rust.SUMMARY_PAGE_MARKER.search("Getting started Summary / selection"))

    def test_telemetry_prompt_never_matches_sharing_text_or_the_allow_button_alone(self):
        self.assertFalse(gui_rust.TELEMETRY_PROMPT.search("Peer: Paused; KeepSharing off"))
        self.assertTrue(gui_rust.TELEMETRY_PROMPT.search("Telemetry preference"))
        self.assertRegex("Keep telemetry off", gui_rust.TELEMETRY_KEEP_PATTERN)
        self.assertNotRegex("Allow configured telemetry", gui_rust.TELEMETRY_KEEP_PATTERN)
        self.assertNotRegex("Keep sharing verified completed archives", gui_rust.TELEMETRY_KEEP_PATTERN)

    def test_later_transition_markers_do_not_match_the_page_they_leave(self):
        self.assertTrue(gui_rust.VALIDATION_PAGE_MARKER.search("Run validation"))
        self.assertFalse(gui_rust.VALIDATION_PAGE_MARKER.search("Base build: 30 of 145 mods selected"))
        self.assertTrue(gui_rust.VALIDATION_STARTED_MARKER.search("Validating…"))
        self.assertFalse(gui_rust.VALIDATION_STARTED_MARKER.search("Run validation"))
        self.assertTrue(gui_rust.INSTALL_REVIEW_MARKER.search("Ready to Install"))
        self.assertTrue(gui_rust.INSTALL_CONFIRMATION_MARKER.search("Create snapshot and install"))
        self.assertFalse(gui_rust.INSTALL_CONFIRMATION_MARKER.search("Install selected mods"))
        self.assertTrue(gui_rust.INSTALLATION_STARTED_MARKER.search("Stop Install"))
        self.assertFalse(gui_rust.INSTALLATION_STARTED_MARKER.search("Installing multiple KOTOR mods is tedious"))
        self.assertRegex("Next", gui_rust.NEXT_PATTERN)
        self.assertNotRegex("Nexus updates", gui_rust.NEXT_PATTERN)

    def test_summary_crop_keeps_the_left_edge_where_the_page_body_starts(self):
        left, top, right, bottom = gui_rust.SUMMARY_CROP
        self.assertEqual((left, right), (0.0, 1.0))
        self.assertTrue(0 < top < bottom < 1)


class NavigationDiagnosticsTests(unittest.TestCase):
    def test_first_navigation_window_is_longer_than_the_default(self):
        self.assertEqual(gui_rust.FIRST_NAV_WINDOW_SECONDS, 300)
        self.assertGreater(gui_rust.FIRST_NAV_WINDOW_SECONDS, gui_rust.NAV_WINDOW_SECONDS)

    def test_busy_text_reads_status_words_and_ignores_plain_labels(self):
        for text in ("Loading guide...", "Parsing instructions", "Working", "Resolving archives",
                     "Syncing peers", "Busy"):
            self.assertTrue(gui_rust.busy_text([text]), text)
        self.assertEqual(gui_rust.busy_text(["Welcome", "Summary / selection"]), "")
        self.assertEqual(gui_rust.busy_text(["Welcome", "Parsing 12 of 40"]), "Parsing 12 of 40")
        self.assertLessEqual(len(gui_rust.busy_text(["Loading " + "x" * 300])), gui_rust.BUSY_TEXT_CHARS)

    def test_top_bar_lines_keep_only_lines_near_the_top(self):
        data = {"top": [10, 12, 400, 700]}
        lines = [("Loading guide", [0, 1]), ("Welcome   page", [2]), ("Footer", [3])]
        self.assertEqual(gui_rust.top_bar_lines(data, lines, 800), ["Loading guide"])
        self.assertEqual(gui_rust.top_bar_lines(data, lines, 800, fraction=0.6),
                         ["Loading guide", "Welcome page"])

    def test_full_ocr_is_logged_on_the_first_every_nth_and_failing_attempt(self):
        read = ["line %d" % number for number in range(20)]
        relevant = read[:10]
        short = gui_rust.nav_log_lines(read, relevant, 2)
        self.assertEqual(short, relevant[:gui_rust.SUMMARY_OCR_LOG_LINES])
        self.assertEqual(gui_rust.nav_log_lines(read, relevant, 1), read)
        self.assertEqual(gui_rust.nav_log_lines(read, relevant, gui_rust.NAV_FULL_LOG_EVERY), read)
        self.assertEqual(gui_rust.nav_log_lines(read, relevant, 3, failed=True), read)
        self.assertEqual(len(gui_rust.nav_log_lines(read * 10, [], 1)), gui_rust.NAV_LOG_LINES)
        self.assertEqual(gui_rust.nav_log_lines(read, [], 2), read[:gui_rust.SUMMARY_OCR_LOG_LINES])

    def test_click_cadence_waits_for_busy_and_does_not_spam(self):
        due = gui_rust.nav_click_due
        self.assertTrue(due(0, 0, False, False, 0))
        self.assertFalse(due(5, 10, False, False, 0))
        self.assertTrue(due(10, 10, False, False, 0))
        self.assertTrue(due(5, 10, False, True, 0))
        self.assertFalse(due(100, 0, True, False, 90))
        self.assertFalse(due(100, 0, True, True, 90))
        self.assertTrue(due(120, 0, True, False, 90))

    def test_press_timing_constants_hover_before_down_and_park_after_a_delay(self):
        self.assertGreaterEqual(gui_rust.NAV_HOVER_SECONDS, 0.3)
        self.assertGreaterEqual(gui_rust.NAV_PARK_DELAY_SECONDS, 0.3)


class WorkerFailureTests(unittest.TestCase):
    prepare = ExecuteTests.prepare
    run_execute = ExecuteTests.run_execute
    LEAK = "ghp_" + "a1B2c3D4e5" * 4

    def fail(self, log_text, code, phase=None, report=False, expire=False):
        _, args = self.prepare(document(block(BASE_ONE, "A"), block(BASE_TWO, "B")))
        result = {}
        launched = []

        class Process:
            returncode = code

            def __init__(self, command, **kwargs):
                launched.append(command)
                self.pid = 0
                private = Path(command[-1])
                (private / "driver.log").write_text(log_text, encoding="utf-8")
                if phase:
                    gui_diag.mark_phase(private, *phase)
                if report:
                    (private / "result.json").write_text(
                        json.dumps({"status": "blocked", "reason": "gui_timeout"}), encoding="utf-8")

            def wait(self, timeout=None):
                if expire:
                    raise gui_rust.subprocess.TimeoutExpired("xvfb-run", timeout)
                return code

        with mock.patch.object(gui_rust.shutil, "which", return_value="/bin/true"), \
                mock.patch.object(gui_rust.subprocess, "Popen", Process), \
                mock.patch.object(gui_rust, "stop"):
            with self.assertRaises(gui_rust.Blocked) as caught:
                gui_rust.execute(args, result)
        return result, str(caught.exception), args

    def test_missing_report_keeps_the_reason_and_adds_redacted_tail_exit_and_phase(self):
        log = "\n".join(["xvfb-run: error: Xvfb failed to start", "token=" + self.LEAK,
                         "open /home/runner/work/_temp/gui/x"]) + "\n"
        result, reason, args = self.fail(log, 1)
        self.assertEqual(reason, "isolated_worker_failed_before_report")
        diagnostics = result["worker_diagnostics"]
        self.assertEqual((diagnostics["phase"], diagnostics["exit_code"], diagnostics["signal"]),
                         ("launch", 1, None))
        self.assertFalse(diagnostics["worker_result_present"])
        self.assertEqual(result["stage"], "launch")
        tail = "\n".join(diagnostics["tails"]["worker"])
        self.assertIn("Xvfb failed to start", tail)
        self.assertNotIn(self.LEAK, tail)
        self.assertNotIn(str(args.work_dir), json.dumps(diagnostics))
        json.dumps(result)

    def test_tail_is_capped_and_signal_and_phase_are_reported(self):
        log = "".join("line %d\n" % number for number in range(1000))
        result, _, _ = self.fail(log, -9, phase=("first_screenshot", "Keep telemetry off"))
        diagnostics = result["worker_diagnostics"]
        self.assertEqual(len(diagnostics["tails"]["worker"]), gui_diag.TAIL_LINES)
        self.assertEqual(diagnostics["tails"]["worker"][-1], "line 999")
        self.assertEqual((diagnostics["signal"], diagnostics["phase"], diagnostics["stage"]),
                         ("SIGKILL", "first_screenshot", "Keep telemetry off"))
        self.assertEqual(result["stage"], "Keep telemetry off")

    def test_worker_report_is_kept_and_still_gets_diagnostics(self):
        result, reason, _ = self.fail("Traceback x\n", 1, phase=("install", "Run validation"), report=True)
        self.assertEqual(reason, "gui_timeout")
        self.assertTrue(result["worker_diagnostics"]["worker_result_present"])
        self.assertEqual(result["worker_diagnostics"]["phase"], "install")

    def test_selection_summary_in_the_worker_report_reaches_the_diagnostics(self):
        summary = {"expected": "Base build: 30 of 145 mods selected",
                   "observed": ["Base build: 27 of 145 mods selected"], "attempts": 4,
                   "matched": False, "screenshot": gui_rust.SUMMARY_SCREENSHOT}
        _, args = self.prepare(document(block(BASE_ONE, "A"), block(BASE_TWO, "B")))
        launched = []

        class Process:
            returncode = 1

            def __init__(self, command, **kwargs):
                launched.append(command)
                self.pid = 0

            def wait(self, timeout=None):
                private = Path(launched[0][-1])
                (private / "result.json").write_text(json.dumps({
                    "status": "blocked", "reason": "selection_summary_does_not_match_expected_subset",
                    "stage": "selection_summary", "selection_summary": summary}), encoding="utf-8")
                return 1

        result = {}
        with mock.patch.object(gui_rust.shutil, "which", return_value="/bin/true"), \
                mock.patch.object(gui_rust.subprocess, "Popen", Process), \
                mock.patch.object(gui_rust, "stop"):
            with self.assertRaises(gui_rust.Blocked):
                gui_rust.execute(args, result)
        self.assertEqual(result["selection_summary"], summary)
        self.assertEqual(result["worker_diagnostics"]["selection_summary"], summary)
        self.assertEqual(result["stage"], "selection_summary")

    def test_overall_timeout_is_marked(self):
        result, reason, _ = self.fail("still running\n", None, expire=True)
        self.assertEqual(reason, "isolated_desktop_timeout")
        self.assertTrue(result["worker_diagnostics"]["timed_out"])

    def test_successful_run_has_no_diagnostics(self):
        _, args = self.prepare(document(block(BASE_ONE, "A"), block(BASE_TWO, "B")))
        result, error = self.run_execute(args, completed=2)
        self.assertIsNone(error)
        self.assertNotIn("worker_diagnostics", result)

    def test_main_prints_a_group_and_a_json_line_without_the_tail_but_saves_it_in_the_report(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root)
        report = root / "report.json"
        diagnostics = gui_diag.collect(root, (("worker", "driver.log", 5),), code=1)

        def execute(args, result):
            result["worker_diagnostics"] = dict(diagnostics, tails={"worker": ["boom"]})
            raise gui_rust.Blocked("isolated_worker_failed_before_report")

        argv = ["gui_rust.py", "--binary", "b", "--input", "i", "--game-dir", "g", "--archives", "a",
                "--work-dir", "w", "--report", str(report), "--timeout", "5"]
        printed = []
        with mock.patch.object(gui_rust.sys, "argv", argv), mock.patch.object(gui_rust, "execute", execute), \
                mock.patch("builtins.print", lambda *a, **k: printed.append(a[0])):
            self.assertEqual(gui_rust.main(), 1)
        self.assertTrue(printed[0].startswith("::group::gui worker tail"))
        self.assertIn("[worker] boom", printed[0])
        self.assertTrue(printed[0].rstrip().splitlines()[-2:][0].startswith("::endgroup::"))
        line = json.loads(printed[1])
        self.assertNotIn("worker_diagnostics", line)
        self.assertEqual(line["reason"], "isolated_worker_failed_before_report")
        saved = json.loads(report.read_text(encoding="utf-8"))
        self.assertEqual(saved["worker_diagnostics"]["tails"]["worker"], ["boom"])


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
