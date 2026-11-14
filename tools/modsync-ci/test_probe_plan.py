import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
FIXTURES = HERE / "fixtures"


def load(name):
    spec = importlib.util.spec_from_file_location(name, HERE / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


manual = load("manual")
probe = load("probe_plan")

GUIDE = """# Example Build

## Mod List

### Mod A

### Mod B

### Mod C
"""


def patcher_step(name, heading, archive):
    return {"id": name, "source_heading": heading, "operations": [
        {"type": "extract", "source": "{archives}/" + archive, "destination": "{extract}/" + name + "/1"},
        {"type": "patcher", "patcher": "{extract}/" + name + "/1/HoloPatcher.exe", "sha256": "a" * 64,
         "argv": ["/usr/bin/wine", "{extract}/" + name + "/1/HoloPatcher.exe", "--install"],
         "cwd": "{extract}/" + name + "/1", "success_log": "{extract}/" + name + "/1/installlog.txt",
         "success_markers": ["done"], "failure_regex": ["(?i)error"], "expected_changes": "changed_manifest"}]}


class Synthetic(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.guide = self.root / "guide.md"
        self.guide.write_text(GUIDE, encoding="utf-8")
        self.archives = self.root / "archives"
        (self.archives / "dir").mkdir(parents=True)
        for name in ("a.zip", "b.zip", "dir/x.txt", "dir/y.txt"):
            (self.archives / name).write_text(name, encoding="utf-8")
        copy_step = {"id": "step-003", "source_heading": "### Mod C", "operations": [
            {"type": "copy", "source": "{archives}/dir", "destination": "{game}/override"}]}
        self.plan = {"guide_sha256": manual.sha256(self.guide),
                     "archive_sha256": {"{archives}/" + n: manual.sha256(self.archives / n)
                                        for n in ("a.zip", "b.zip", "dir/x.txt", "dir/y.txt")},
                     "steps": [patcher_step("step-001", "### Mod A", "a.zip"),
                               patcher_step("step-002", "### Mod B", "b.zip"), copy_step],
                     "supplemental_sections": []}
        self.roots = {"game": self.root / "game", "archives": self.archives,
                      "extract": self.root / "work" / "extract"}

    def test_keeps_subject_omits_rest_and_validates(self):
        built = probe.build_probe_plan(self.plan, {"step-002"}, "step-002")
        self.assertEqual(built["steps"][1], self.plan["steps"][1])
        for step in (built["steps"][0], built["steps"][2]):
            self.assertEqual(set(step), {"id", "source_heading", "omit"})
            self.assertIn("step-002", step["omit"]["reason"])
        self.assertEqual(list(built["archive_sha256"]), ["{archives}/b.zip"])
        manual.validate_plan(built, self.guide, self.roots, 1800)
        self.assertEqual([e["id"] for e in manual.omitted_steps(built)], ["step-001", "step-003"])

    def test_directory_source_keeps_every_bound_file(self):
        built = probe.build_probe_plan(self.plan, {"step-001", "step-003"}, "step-001")
        self.assertEqual(sorted(built["archive_sha256"]),
                         ["{archives}/a.zip", "{archives}/dir/x.txt", "{archives}/dir/y.txt"])
        manual.validate_plan(built, self.guide, self.roots, 1800)

    def test_source_plan_untouched(self):
        before = json.dumps(self.plan, sort_keys=True)
        probe.build_probe_plan(self.plan, {"step-001"}, "step-001")
        self.assertEqual(json.dumps(self.plan, sort_keys=True), before)

    def test_rejects_omitted_unknown_and_non_patcher_subjects(self):
        omitted = json.loads(json.dumps(self.plan))
        omitted["steps"][0] = {"id": "step-001", "source_heading": "### Mod A",
                               "omit": {"reason": "r", "reviewed_by": "x"}}
        with self.assertRaises(probe.ProbeError):
            probe.build_probe_plan(omitted, {"step-001"}, "step-001")
        with self.assertRaises(probe.ProbeError):
            probe.build_probe_plan(self.plan, {"step-009"}, "step-009")
        with self.assertRaises(probe.ProbeError):
            probe.build_probe_plan(self.plan, {"step-003"}, "step-003")

    def test_build_refuses_ci_directory(self):
        plan_path = self.root / "ci" / "k1-manual-plan.json"
        plan_path.parent.mkdir()
        plan_path.write_text(json.dumps(self.plan), encoding="utf-8")
        for out in (plan_path.parent, plan_path.parent / "probe", REPO / "ci" / "probe"):
            self.assertEqual(probe.main(["build", "--game", "k1", "--plan", str(plan_path),
                                         "--out-dir", str(out), "--steps", "step-001"]), 2)
        self.assertEqual(sorted(p.name for p in plan_path.parent.iterdir()), ["k1-manual-plan.json"])
        self.assertFalse((REPO / "ci" / "probe").exists())

    def test_build_writes_runs_and_prune_keeps_needed(self):
        plan_path = self.root / "plan.json"
        plan_path.write_text(json.dumps(self.plan), encoding="utf-8")
        out = self.root / "probe"
        self.assertEqual(probe.main(["build", "--game", "k1", "--plan", str(plan_path), "--out-dir", str(out),
                                     "--steps", "step-001,step-003+step-002"]), 0)
        runs = json.loads((out / "runs.json").read_text(encoding="utf-8"))["runs"]
        self.assertEqual([r["subject"] for r in runs], ["step-001", "step-002"])
        self.assertEqual(runs[1]["steps"], ["step-002", "step-003"])
        self.assertEqual(runs[0]["kinds"], ["patcher " + "a" * 8])
        self.assertEqual(runs[0]["success_logs"], ["{extract}/step-001/1/installlog.txt"])
        manual.validate_plan(json.loads(Path(runs[1]["plan"]).read_text(encoding="utf-8")),
                             self.guide, self.roots, 1800)
        (self.archives / "unused.rar").write_text("u", encoding="utf-8")
        self.assertEqual(probe.main(["prune-archives", "--runs", str(out / "runs.json"),
                                     "--archives", str(self.archives)]), 0)
        self.assertEqual(sorted(p.relative_to(self.archives).as_posix()
                                for p in self.archives.rglob("*") if p.is_file()),
                         ["a.zip", "b.zip", "dir/x.txt", "dir/y.txt"])


class Results(unittest.TestCase):
    RUN = {"subject": "step-133", "steps": ["step-002", "step-133"]}

    def test_subject_result(self):
        self.assertEqual(probe.subject_result(self.RUN, None), ("no report", ""))
        self.assertEqual(probe.subject_result(self.RUN, {"status": "blocked", "steps": [], "error": "E"}),
                         ("blocked", "E"))
        failed = {"status": "failed", "error": "boom", "steps": [
            {"id": "step-001", "status": "omitted"}, {"id": "step-002", "status": "rolled_back"}]}
        self.assertEqual(probe.subject_result(self.RUN, failed), ("prerequisite step-002 rolled_back", "boom"))
        self.assertTrue(probe.attempted(failed, self.RUN))
        partial = {"status": "partial", "steps": [{"id": "step-002", "status": "complete"},
                                                  {"id": "step-133", "status": "complete"}]}
        self.assertEqual(probe.subject_result(self.RUN, partial), ("complete", ""))
        self.assertFalse(probe.attempted({"status": "blocked", "steps": []}, self.RUN))

    def test_summary_table(self):
        text = probe.summary("k2", [{"steps": ["step-002"], "heading": "### TSLRCM", "kinds": ["patcher 94c99c48"],
                                     "why": "w", "report_status": "failed", "status": "rolled_back",
                                     "seconds": 3, "error": "a|b\nc"}])
        self.assertIn("| step-002 | ### TSLRCM | patcher 94c99c48 | w | failed | rolled_back | 3 | a\\|b c |", text)
        self.assertIn("0 of 1 probed steps complete", text)


class RealPlans(unittest.TestCase):
    """The default selections cover every patcher build of the reviewed plans and validate."""

    def test_default_selection(self):
        for game in ("k1", "k2"):
            plan_path = FIXTURES / "ci" / (game + "-manual-plan.json")
            if not plan_path.is_file():
                self.skipTest("reviewed plan missing")
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            builds = {op["sha256"] for step in plan["steps"] for op in probe.patcher_ops(step)}
            covered = set()
            roots = {"game": Path("/nonexistent/game"), "archives": Path("/nonexistent/archives"),
                     "extract": Path("/nonexistent/work/extract")}
            for subject, prereqs, why in probe.SELECTIONS[game]:
                self.assertTrue(why)
                kept = set(prereqs) | {subject}
                built = probe.build_probe_plan(plan, kept, subject)
                covered |= {op["sha256"] for step in built["steps"] for op in probe.patcher_ops(step)}
                with mock.patch.object(manual, "verify_archive_inputs"):
                    manual.validate_plan(built, FIXTURES / "content" / game / "full.md", roots, 1800)
                for step in built["steps"]:
                    if step["id"] in kept:
                        self.assertIn(step, plan["steps"])
            self.assertEqual(covered, builds, game)


if __name__ == "__main__":
    unittest.main()
