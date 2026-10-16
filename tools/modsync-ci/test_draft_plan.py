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
REPO = HERE.parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, HERE / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


draft_plan = load("draft_plan")
manual = draft_plan.manual

GUIDE = """# Example Build

## Installation Notes

### Read This First

Some prose.

## Mod List

### Example Mod A

**Name:** [Example Mod A](https://example.invalid/files/file/1-example-a/) and [**Patch**](https://example.invalid/dl/example-a-patch.7z)

**Author:** Someone

**Installation Method:** TSLPatcher, Loose-File Patch

:::note
Installation Instructions
:   Run the installer and choose option 2.
    Then copy example.tga to the override.
:::

### Example Mod B

**Name:** [Example Mod B](https://example.invalid/files/file/2-example-b/)

**Installation Method:** Loose-File Mod

```
### Not A Heading
```

:::warning
Usage Warning
:   Do not combine with Example Mod A.
:::

## Misc Fixes

### Example Fix

Prose only.
"""


class Fixture(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.content = self.root / "content"
        (self.content / "k1").mkdir(parents=True)
        (self.content / "k2").mkdir(parents=True)
        for game in ("k1", "k2"):
            (self.content / game / "full.md").write_text(GUIDE, encoding="utf-8")
        self.out = self.root / "ci" / "drafts"

    def run_main(self, *extra):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = draft_plan.main(["--guide-root", str(self.content), "--out-dir", str(self.out), *extra])
        return code, output.getvalue(), errors.getvalue()

    def load_pair(self, game="k1"):
        draft = json.loads((self.out / (game + draft_plan.DRAFT_SUFFIX)).read_text(encoding="utf-8"))
        review = json.loads((self.out / (game + draft_plan.REVIEW_SUFFIX)).read_text(encoding="utf-8"))
        return draft, review

    def roots(self):
        archives, game = self.root / "archives", self.root / "game"
        archives.mkdir(exist_ok=True)
        game.mkdir(exist_ok=True)
        return {"game": game, "archives": archives, "extract": self.root / "work" / "extract"}


class DraftContent(Fixture):
    def test_steps_follow_guide_order_with_unique_ids(self):
        self.assertEqual(self.run_main()[0], 0)
        draft, review = self.load_pair()
        self.assertEqual([step["source_heading"] for step in draft["steps"]],
                         ["### Example Mod A", "### Example Mod B"])
        self.assertEqual([step["id"] for step in draft["steps"]], ["step-001", "step-002"])
        self.assertEqual([step["id"] for step in review["steps"]], ["step-001", "step-002"])
        self.assertEqual([item["source_heading"] for item in draft["supplemental_sections"]],
                         ["## Installation Notes", "### Read This First", "## Misc Fixes", "### Example Fix"])

    def test_nothing_is_invented(self):
        self.run_main()
        draft, _ = self.load_pair()
        self.assertEqual(draft["archive_sha256"], {})
        for step in draft["steps"]:
            self.assertEqual(step["operations"], [])
            self.assertEqual(set(step), {"id", "source_heading", "operations"})
        for section in draft["supplemental_sections"]:
            self.assertEqual(section["review"], "")
            self.assertEqual(section["step_ids"], [])
        text = (self.out / ("k1" + draft_plan.DRAFT_SUFFIX)).read_text(encoding="utf-8")
        for forbidden in ('"argv"', '"patcher"', '"success_markers"', '"failure_regex"', '"extract"'):
            self.assertNotIn(forbidden, text)

    def test_every_step_needs_human_review_with_guide_evidence(self):
        self.run_main()
        draft, review = self.load_pair()
        self.assertEqual(review["status"], draft_plan.STATUS)
        self.assertEqual(review["step_count"], len(review["steps"]))
        self.assertTrue(all(step["status"] == "needs-human-review" for step in review["steps"]))
        first, second = review["steps"]
        links = first["evidence"]["name_links"]
        self.assertEqual([link["text"] for link in links], ["Example Mod A", "Patch"])
        self.assertIsNone(links[0]["archive_name_from_link"])
        self.assertEqual(links[1]["archive_name_from_link"], "example-a-patch.7z")
        self.assertEqual(first["evidence"]["installation_method"]["text"], "TSLPatcher, Loose-File Patch")
        lines = [item["text"] for item in first["evidence"]["blocks"][0]["lines"]]
        self.assertEqual(lines, ["Run the installer and choose option 2.", "Then copy example.tga to the override."])
        self.assertEqual(first["evidence"]["blocks"][0]["title"], "Installation Instructions")
        self.assertEqual(second["evidence"]["blocks"][0]["kind"], "warning")
        self.assertEqual(second["evidence"]["name_links"][0]["archive_name_from_link"], None)

    def test_fenced_heading_is_not_a_step(self):
        self.run_main()
        draft, _ = self.load_pair()
        self.assertNotIn("### Not A Heading", [step["source_heading"] for step in draft["steps"]])

    def test_draft_marks_itself_and_binds_guide(self):
        self.run_main()
        draft, review = self.load_pair()
        self.assertEqual(draft["draft"]["status"], "needs-human-review")
        self.assertEqual(draft["guide_sha256"], manual.sha256(self.content / "k1" / "full.md"))
        self.assertEqual(review["guide_sha256"], draft["guide_sha256"])

    def test_deterministic(self):
        self.run_main()
        first = {path.name: path.read_bytes() for path in sorted(self.out.iterdir())}
        code, _, errors = self.run_main()
        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(first, {path.name: path.read_bytes() for path in sorted(self.out.iterdir())})
        self.assertEqual(sorted(first), ["k1-manual-plan.draft.json", "k1-manual-plan.review.json",
                                         "k2-manual-plan.draft.json", "k2-manual-plan.review.json"])

    def test_differing_existing_draft_needs_force(self):
        self.run_main()
        target = self.out / ("k1" + draft_plan.DRAFT_SUFFIX)
        target.write_text('{"hand": "edited"}\n', encoding="utf-8")
        code, _, errors = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("--force", errors)
        self.assertEqual(target.read_text(encoding="utf-8"), '{"hand": "edited"}\n')
        self.assertEqual(self.run_main("--force")[0], 0)
        self.assertNotEqual(target.read_text(encoding="utf-8"), '{"hand": "edited"}\n')

    def test_missing_guide_writes_nothing(self):
        (self.content / "k2" / "full.md").unlink()
        code, _, errors = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("guide not found", errors)
        self.assertFalse(self.out.exists())


class Refusals(Fixture):
    def test_refuses_reviewed_plan_directory(self):
        self.out = self.root / "ci"
        code, _, errors = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("refusing", errors)
        self.assertFalse((self.root / "ci").exists())

    def test_refuses_reviewed_plan_name(self):
        for name in ("k1-manual-plan.json", "k2-manual-plan.json"):
            with self.assertRaises(draft_plan.Refused):
                draft_plan.checked_target(self.root / "elsewhere" / name)
        with self.assertRaises(draft_plan.Refused):
            draft_plan.checked_target(self.root / "ci" / "k1-manual-plan.draft.json")
        with self.assertRaises(draft_plan.Refused):
            draft_plan.checked_target(self.root / "drafts" / "notes.json")

    def test_never_creates_reviewed_plan(self):
        self.run_main()
        self.assertEqual([path for path in self.root.rglob("*-manual-plan.json")], [])


class ManualGateRejectsDrafts(Fixture):
    def setUp(self):
        super().setUp()
        self.run_main()
        self.draft, _ = self.load_pair()
        self.guide = self.content / "k1" / "full.md"

    def assertBlocked(self, plan, message):
        with self.assertRaises(manual.Blocked) as caught:
            manual.validate_plan(plan, self.guide, self.roots(), 1800)
        self.assertIn(message, str(caught.exception))

    def test_draft_as_written_fails_the_gate(self):
        self.assertBlocked(self.draft, "invalid object fields")

    def test_unreviewed_steps_fail_even_without_draft_marker(self):
        stripped = copy.deepcopy(self.draft)
        del stripped["draft"]
        self.assertBlocked(stripped, "empty operations")

    def test_unreviewed_supplemental_fails_even_with_operations(self):
        stripped = copy.deepcopy(self.draft)
        del stripped["draft"]
        for step in stripped["steps"]:
            step["operations"] = [{"type": "delete", "path": "{game}/example.txt", "missing_ok": True}]
        self.assertBlocked(stripped, "missing review rationale")

    def test_executor_exits_nonzero_and_creates_nothing(self):
        plan = self.out / ("k1" + draft_plan.DRAFT_SUFFIX)
        roots = self.roots()
        (roots["game"] / "vanilla.txt").write_text("x", encoding="utf-8")
        work, report = self.root / "work", self.root / "report" / "report.json"
        result = subprocess.run(
            [sys.executable, str(HERE / "manual.py"), "--plan", str(plan), "--guide", str(self.guide),
             "--game-dir", str(roots["game"]), "--archives", str(roots["archives"]),
             "--work-dir", str(work), "--report", str(report)],
            capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 1)
        self.assertFalse(work.exists())
        self.assertEqual((roots["game"] / "vanilla.txt").read_text(encoding="utf-8"), "x")


class RealGuides(unittest.TestCase):
    def test_counts_match_manual_parser_for_both_games(self):
        for game in ("k1", "k2"):
            guide = REPO / "content" / game / "full.md"
            if not guide.is_file():
                self.skipTest("guide missing")
            mods, supplemental = manual.guide_headings(guide.read_text(encoding="utf-8"))
            draft, review = draft_plan.build(game, guide, "content/%s/full.md" % game)
            self.assertEqual([step["source_heading"] for step in draft["steps"]], mods)
            self.assertEqual(len(review["steps"]), len(mods))
            self.assertEqual([item["source_heading"] for item in draft["supplemental_sections"]], supplemental)
            self.assertTrue(all(step["status"] == "needs-human-review" for step in review["steps"]))
            self.assertTrue(all(step["operations"] == [] for step in draft["steps"]))


if __name__ == "__main__":
    unittest.main()
