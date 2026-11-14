import contextlib
import copy
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parent


def load(name):
    spec = importlib.util.spec_from_file_location(name, HERE / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


manual = load("manual")

GUIDE = """# Example Build

## Mod List

### Example Mod A

Prose.

### Example Mod B

Prose.

### Example Mod C

Prose.
"""

HEADINGS = ["### Example Mod A", "### Example Mod B", "### Example Mod C"]


def delete_step(name, heading):
    return {"id": name, "source_heading": heading,
            "operations": [{"type": "delete", "path": "{game}/" + name + ".txt", "missing_ok": True}]}


def omit_step(name, heading, reason="no archive available", reviewed_by="Example Reviewer"):
    return {"id": name, "source_heading": heading,
            "omit": {"reason": reason, "reviewed_by": reviewed_by}}


class Fixture(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.guide = self.root / "guide.md"
        self.guide.write_text(GUIDE, encoding="utf-8")
        self.archives = self.root / "archives"
        self.game = self.root / "game"
        self.archives.mkdir()
        self.game.mkdir()
        (self.game / "vanilla.txt").write_text("x", encoding="utf-8")

    def roots(self):
        return {"game": self.game, "archives": self.archives, "extract": self.root / "work" / "extract"}

    def plan(self, steps):
        return {"guide_sha256": manual.sha256(self.guide), "archive_sha256": {},
                "steps": steps, "supplemental_sections": []}

    def mixed(self):
        return self.plan([delete_step("step-001", HEADINGS[0]),
                          omit_step("step-002", HEADINGS[1]),
                          delete_step("step-003", HEADINGS[2])])

    def validate(self, plan):
        manual.validate_plan(plan, self.guide, self.roots(), 1800)

    def assertBlocked(self, plan, message):
        with self.assertRaises(manual.Blocked) as caught:
            self.validate(plan)
        self.assertIn(message, str(caught.exception))


class ValidatorAcceptsReviewedOmission(Fixture):
    def test_omitted_step_without_operations_is_valid(self):
        self.validate(self.mixed())

    def test_omitted_steps_are_listed_in_guide_order(self):
        plan = self.plan([omit_step("step-001", HEADINGS[0], "first reason", "Reviewer One"),
                          delete_step("step-002", HEADINGS[1]),
                          omit_step("step-003", HEADINGS[2], "third reason", "Reviewer Two")])
        self.validate(plan)
        self.assertEqual(manual.omitted_steps(plan), [
            {"id": "step-001", "heading": HEADINGS[0], "reason": "first reason", "reviewed_by": "Reviewer One"},
            {"id": "step-003", "heading": HEADINGS[2], "reason": "third reason", "reviewed_by": "Reviewer Two"}])

    def test_plan_without_omissions_reports_none(self):
        plan = self.plan([delete_step("step-%03d" % (index + 1), heading) for index, heading in enumerate(HEADINGS)])
        self.validate(plan)
        self.assertEqual(manual.omitted_steps(plan), [])


class ValidatorRejectsBadOmission(Fixture):
    def mutate(self, change):
        plan = self.mixed()
        change(plan["steps"][1])
        return plan

    def test_omit_with_operations_is_rejected(self):
        plan = self.mutate(lambda step: step.update(operations=delete_step("x", "").get("operations")))
        self.assertBlocked(plan, "omitted step must not declare operations")

    def test_omit_with_empty_operations_list_is_rejected(self):
        self.assertBlocked(self.mutate(lambda step: step.update(operations=[])),
                           "omitted step must not declare operations")

    def test_omit_with_timeout_is_rejected(self):
        self.assertBlocked(self.mutate(lambda step: step.update(timeout_seconds=5)),
                           "omitted step must not declare operations or timeout")

    def test_empty_or_blank_reason_is_rejected(self):
        for reason in ("", "   ", None, 7, ["reason"]):
            plan = self.mutate(lambda step: step["omit"].update(reason=reason))
            self.assertBlocked(plan, "omit reason must be a nonempty printable string")

    def test_empty_or_blank_reviewer_is_rejected(self):
        for reviewer in ("", "\t ", None, 7):
            plan = self.mutate(lambda step: step["omit"].update(reviewed_by=reviewer))
            self.assertBlocked(plan, "omit reviewed_by must be a nonempty printable string")

    def test_missing_or_unknown_omit_fields_are_rejected(self):
        self.assertBlocked(self.mutate(lambda step: step["omit"].pop("reviewed_by")), "invalid object fields")
        self.assertBlocked(self.mutate(lambda step: step["omit"].pop("reason")), "invalid object fields")
        self.assertBlocked(self.mutate(lambda step: step["omit"].update(extra="x")), "invalid object fields")
        self.assertBlocked(self.mutate(lambda step: step.update(omit="skip")), "invalid object fields")
        self.assertBlocked(self.mutate(lambda step: step.update(omit=True)), "invalid object fields")

    def test_control_characters_cannot_reach_log_annotations(self):
        for text in ("line one\nline two", "carriage\rreturn", "bell\x07"):
            self.assertBlocked(self.mutate(lambda step: step["omit"].update(reason=text)),
                               "omit reason must be a nonempty printable string")
            self.assertBlocked(self.mutate(lambda step: step["omit"].update(reviewed_by=text)),
                               "omit reviewed_by must be a nonempty printable string")

    def test_step_without_operations_or_omit_is_still_rejected(self):
        plan = self.mixed()
        plan["steps"][1] = {"id": "step-002", "source_heading": HEADINGS[1]}
        self.assertBlocked(plan, "empty operations")
        plan["steps"][1]["operations"] = []
        self.assertBlocked(plan, "empty operations")

    def test_plan_that_omits_every_step_is_rejected(self):
        plan = self.plan([omit_step("step-%03d" % (index + 1), heading) for index, heading in enumerate(HEADINGS)])
        self.assertBlocked(plan, "plan omits every step")

    def test_every_guide_step_must_still_be_accounted_for(self):
        plan = self.mixed()
        del plan["steps"][1]
        self.assertBlocked(plan, "mod headings must match exactly once in guide order")

    def test_omitted_step_must_keep_guide_order_and_unique_id(self):
        plan = self.mixed()
        plan["steps"][1], plan["steps"][2] = plan["steps"][2], plan["steps"][1]
        self.assertBlocked(plan, "mod headings must match exactly once in guide order")
        plan = self.mixed()
        plan["steps"][1]["id"] = "step-001"
        self.assertBlocked(plan, "duplicate step id")


class AnnotationText(unittest.TestCase):
    def test_percent_and_line_breaks_are_escaped(self):
        self.assertEqual(manual.annotation_text("100% a\r\nb"), "100%25 a%0D%0Ab")

    def test_warnings_name_each_step_and_label_partial(self):
        omitted = [{"id": "step-002", "heading": "### Example Mod B", "reason": "no archive", "reviewed_by": "Reviewer"}]
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            manual.warn_omitted(omitted, 3)
        lines = output.getvalue().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(all(line.startswith("::warning") for line in lines))
        self.assertIn("step-002", lines[0])
        self.assertIn("no archive", lines[0])
        self.assertIn("Reviewer", lines[0])
        self.assertIn("PARTIAL", lines[1])
        self.assertIn("1 of 3", lines[1])

    def test_nothing_is_emitted_without_omissions(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            manual.warn_omitted([], 3)
        self.assertEqual(output.getvalue(), "")


class ExecutorReport(Fixture):
    def run_executor(self, plan):
        plan_path = self.root / "plan.json"
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        work, report = self.root / "work", self.root / "out" / "report.json"
        result = subprocess.run(
            [sys.executable, str(HERE / "manual.py"), "--plan", str(plan_path), "--guide", str(self.guide),
             "--game-dir", str(self.game), "--archives", str(self.archives),
             "--work-dir", str(work), "--report", str(report)],
            capture_output=True, text=True, timeout=120)
        return result, report, work

    def test_partial_run_is_reported_warned_and_exits_zero(self):
        result, report_path, work = self.run_executor(self.mixed())
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(report["status"], "partial")
        self.assertIs(report["partial"], True)
        self.assertEqual(report["omitted_steps"], [
            {"id": "step-002", "heading": HEADINGS[1], "reason": "no archive available",
             "reviewed_by": "Example Reviewer"}])
        self.assertEqual([(step["id"], step["status"]) for step in report["steps"]],
                         [("step-001", "complete"), ("step-002", "omitted"), ("step-003", "complete")])
        self.assertEqual(report["steps"][1]["reason"], "no archive available")
        self.assertFalse((work / "step-0002").exists())
        self.assertTrue((work / "step-0001").is_dir())
        self.assertTrue((work / "step-0003").is_dir())
        warnings = [line for line in result.stdout.splitlines() if line.startswith("::warning")]
        self.assertGreaterEqual(len(warnings), 2)
        self.assertTrue(any("step-002" in line for line in warnings))
        self.assertTrue(any("PARTIAL" in line for line in warnings))

    def test_run_without_omissions_stays_complete_and_silent(self):
        plan = self.plan([delete_step("step-%03d" % (index + 1), heading) for index, heading in enumerate(HEADINGS)])
        result, report_path, _ = self.run_executor(plan)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(report["status"], "complete")
        self.assertIs(report["partial"], False)
        self.assertEqual(report["omitted_steps"], [])
        self.assertNotIn("::warning", result.stdout)

    def test_invalid_omission_blocks_before_anything_is_created(self):
        plan = copy.deepcopy(self.mixed())
        plan["steps"][1]["omit"]["reason"] = " "
        result, report_path, work = self.run_executor(plan)
        self.assertEqual(result.returncode, 1)
        self.assertFalse(work.exists())
        self.assertEqual((self.game / "vanilla.txt").read_text(encoding="utf-8"), "x")


if __name__ == "__main__":
    unittest.main()
