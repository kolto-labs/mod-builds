import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest

HERE = Path(__file__).resolve().parent


def load(name):
    spec = importlib.util.spec_from_file_location(name, HERE / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


draft_ts_review = load("draft_ts_review")
ports = draft_ts_review.ports

GUIDE = "# Example\n\n## Mod List\n\n### Alpha\n\n### Beta\n"

CONVERTED = """
[[thisMod]]
Guid = "{10000000-0000-0000-0000-000000000001}"
Name = "Alpha"
IsSelected = true

[[thisMod.Instructions]]
Guid = "{20000000-0000-0000-0000-000000000001}"
Action = "Extract"
Source = ["<<modDirectory>>\\\\alpha*.zip"]

[[thisMod.Instructions]]
Guid = "{20000000-0000-0000-0000-000000000002}"
Action = "Patcher"
Source = ["<<modDirectory>>\\\\alpha"]
Arguments = "0"

[[thisMod.Options]]
Guid = "{30000000-0000-0000-0000-000000000001}"
Name = "Slim"
IsSelected = false

[[thisMod.Options.Instructions]]
Guid = "{20000000-0000-0000-0000-000000000003}"
Action = "Choose"
Source = ["<<modDirectory>>\\\\alpha\\\\slim\\\\*"]
Destination = "<<kotorDirectory>>\\\\Override"

[[thisMod]]
Guid = "{10000000-0000-0000-0000-000000000002}"
Name = "Beta"
IsWidescreen = true
IsSelected = false

[[thisMod.Instructions]]
Guid = "{20000000-0000-0000-0000-000000000004}"
Action = "Move"
Source = ["<<modDirectory>>\\\\beta\\\\*"]
Destination = "<<kotorDirectory>>\\\\Override"
"""

CHOICE_IDS = ["20000000-0000-0000-0000-000000000002", "20000000-0000-0000-0000-000000000003"]


class Fixture(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        (self.root / "content" / "k1").mkdir(parents=True)
        self.guide = self.root / "content" / "k1" / "full.md"
        self.guide.write_text(GUIDE, encoding="utf-8")
        self.converted = self.root / "converted.toml"
        self.converted.write_text(CONVERTED, encoding="utf-8")
        self.out = self.root / "ci" / "drafts"
        self.draft_path = self.out / ("k1" + draft_ts_review.DRAFT_SUFFIX)
        self.checklist_path = self.out / ("k1" + draft_ts_review.CHECKLIST_SUFFIX)

    def run_main(self, *extra, converted=None):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = draft_ts_review.main([str(converted or self.converted), str(self.guide),
                                         "--out-dir", str(self.out), *extra])
        return code, output.getvalue(), errors.getvalue()

    def draft(self):
        return json.loads(self.draft_path.read_text(encoding="utf-8"))

    def blocked_reason(self, path):
        with self.assertRaises(ports.Blocked) as caught:
            ports.review_manifest(path, self.guide, self.converted)
        return str(caught.exception)

    def complete(self, draft):
        """What a human would do; used only to prove the draft is one edit away from valid."""
        for key in draft_ts_review.DRAFT_KEYS:
            draft.pop(key)
        draft["reviewer"] = "Test Reviewer"
        draft["unresolved"] = []
        for entry in draft["components"]:
            for key in draft_ts_review.COMPONENT_DRAFT_KEYS:
                entry.pop(key)
            entry["phase"] = "widescreen" if entry["guid"].endswith("2") else "base"
            entry["assessment"] = "Checked against the guide."
            entry["unresolved"] = []
            entry["choices"] = {key: "1" for key in entry["choices"]}
        return draft


class DraftContent(Fixture):
    def test_prefills_only_mechanical_fields(self):
        self.assertEqual(self.run_main()[0], 0)
        draft = self.draft()
        document = ports.tomllib.loads(CONVERTED)["thisMod"]
        self.assertEqual(draft["schema_version"], 1)
        self.assertEqual(draft["guide_sha256"], ports.digest(self.guide))
        self.assertEqual(draft["converted_sha256"], ports.digest(self.converted))
        self.assertEqual([entry["guid"] for entry in draft["components"]],
                         ["10000000-0000-0000-0000-000000000001", "10000000-0000-0000-0000-000000000002"])
        for entry, component in zip(draft["components"], document):
            self.assertEqual(entry["instructions"], component.get("Instructions", []))
            self.assertEqual(entry["options"], component.get("Options", []))
            self.assertEqual(entry["assessment"], "")
            self.assertNotEqual(entry["unresolved"], [])
        self.assertEqual(draft["reviewer"], "")

    def test_phase_is_not_guessed_while_ports_has_no_rule(self):
        self.assertFalse(hasattr(ports, "component_phase"))
        self.run_main()
        self.assertEqual({entry["phase"] for entry in self.draft()["components"]},
                         {draft_ts_review.PHASE_TODO})

    def test_phase_reuses_a_ports_rule_when_one_exists(self):
        ports.component_phase = lambda component: "widescreen" if component.get("IsWidescreen") else "base"
        self.addCleanup(delattr, ports, "component_phase")
        self.run_main()
        self.assertEqual([entry["phase"] for entry in self.draft()["components"]], ["base", "widescreen"])

    def test_choices_are_todo_placeholders_for_every_prompt_including_options(self):
        self.run_main()
        first, second = self.draft()["components"]
        self.assertEqual(sorted(first["choices"]), CHOICE_IDS)
        self.assertEqual(set(first["choices"].values()), {draft_ts_review.CHOICE_TODO})
        self.assertEqual(second["choices"], {})
        locations = {prompt["instruction_guid"]: prompt["location"] for prompt in first["draft_prompts"]}
        self.assertEqual(locations[CHOICE_IDS[0]], "component")
        self.assertIn("option 30000000-0000-0000-0000-000000000001 (Slim)", locations[CHOICE_IDS[1]])

    def test_output_is_deterministic(self):
        self.run_main()
        before = (self.draft_path.read_bytes(), self.checklist_path.read_bytes())
        self.assertEqual(self.run_main()[0], 0)
        self.assertEqual(before, (self.draft_path.read_bytes(), self.checklist_path.read_bytes()))


class CannotPassAsReviewed(Fixture):
    def test_review_manifest_blocks_the_draft(self):
        self.run_main()
        self.assertEqual(self.blocked_reason(self.draft_path), "review_manifest_reviewer_required")

    def test_each_marker_blocks_on_its_own(self):
        self.run_main()
        draft = self.complete(self.draft())
        path = self.root / "completed.json"
        path.write_text(json.dumps(draft), encoding="utf-8")
        self.assertEqual(len(ports.review_manifest(path, self.guide, self.converted)), 4)
        cases = {
            "reviewer": ("reviewer", "", "review_manifest_reviewer_required"),
            "unresolved": ("unresolved", ["x"], "review_manifest_unresolved"),
        }
        for label, (key, value, reason) in cases.items():
            broken = json.loads(json.dumps(draft))
            broken[key] = value
            path.write_text(json.dumps(broken), encoding="utf-8")
            self.assertEqual(self.blocked_reason(path), reason, label)
        for key, value, reason in (
                ("phase", draft_ts_review.PHASE_TODO, "review_manifest_phase_or_assessment_unresolved"),
                ("assessment", "", "review_manifest_phase_or_assessment_unresolved"),
                ("unresolved", [draft_ts_review.UNRESOLVED_TODO], "review_manifest_phase_or_assessment_unresolved")):
            broken = json.loads(json.dumps(draft))
            broken["components"][0][key] = value
            path.write_text(json.dumps(broken), encoding="utf-8")
            self.assertEqual(self.blocked_reason(path), reason, key)

    def test_todo_choices_alone_are_not_valid_answers(self):
        self.run_main()
        draft = self.complete(self.draft())
        draft["components"][0]["choices"] = {key: "" for key in CHOICE_IDS}
        path = self.root / "empty-choices.json"
        path.write_text(json.dumps(draft), encoding="utf-8")
        self.assertEqual(self.blocked_reason(path), "review_manifest_choice_invalid")
        draft["components"][0]["choices"] = {}
        path.write_text(json.dumps(draft), encoding="utf-8")
        self.assertEqual(self.blocked_reason(path), "review_manifest_prompt_choice_missing")

    def test_draft_flag_and_notice_are_present(self):
        self.run_main()
        draft = self.draft()
        self.assertIs(draft["draft"], True)
        self.assertIn("DRAFT", draft["draft_notice"])
        self.assertEqual(list(draft)[:2], ["draft", "draft_notice"])


class Checklist(Fixture):
    def test_lists_every_decision(self):
        self.run_main()
        text = self.checklist_path.read_text(encoding="utf-8")
        self.assertIn("Choose/Patcher/Execute instructions needing an answer: 2", text)
        for identity in CHOICE_IDS:
            self.assertIn(identity, text)
        self.assertIn("Patcher", text)
        self.assertIn("option 30000000-0000-0000-0000-000000000001 (Slim)", text)
        self.assertIn(draft_ts_review.draft_plan.manual.sha256(self.guide), text)
        self.assertEqual(text.count("`phase`"), 2)
        self.assertEqual(text.count("`assessment`"), 2)
        self.assertIn("no Choose/Patcher/Execute instruction", text)
        self.assertNotIn("[x]", text)


class Refusals(Fixture):
    def test_refuses_reviewed_filenames_and_ci_directory(self):
        with self.assertRaises(draft_ts_review.Refused):
            draft_ts_review.checked_target(self.root / "ci" / "drafts" / "k1-typescript-review.json")
        with self.assertRaises(draft_ts_review.Refused):
            draft_ts_review.checked_target(self.root / "ci" / ("k1" + draft_ts_review.DRAFT_SUFFIX))
        with self.assertRaises(draft_ts_review.Refused):
            draft_ts_review.checked_target(self.root / "k1-notes.json")

    def test_main_never_creates_the_reviewed_file(self):
        self.run_main()
        self.assertFalse((self.out / "k1-typescript-review.json").exists())
        self.assertFalse((self.out.parent / "k1-typescript-review.json").exists())
        self.assertEqual(sorted(path.name for path in self.out.iterdir()),
                         ["k1-typescript-review.checklist.md", "k1-typescript-review.draft.json"])

    def test_differing_draft_needs_force(self):
        self.run_main()
        self.draft_path.write_text("{}\n", encoding="utf-8")
        code, _, errors = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("--force", errors)
        self.assertEqual(self.draft_path.read_text(encoding="utf-8"), "{}\n")
        self.assertEqual(self.run_main("--force")[0], 0)
        self.assertIs(self.draft()["draft"], True)

    def test_duplicate_instruction_guid_is_refused(self):
        broken = self.root / "dup.toml"
        broken.write_text(CONVERTED.replace("20000000-0000-0000-0000-000000000004",
                                            "20000000-0000-0000-0000-000000000001"), encoding="utf-8")
        code, _, errors = self.run_main(converted=broken)
        self.assertEqual(code, 1)
        self.assertIn("duplicate instruction GUID", errors)
        self.assertFalse(self.out.exists())

    def test_empty_missing_and_unknown_game_inputs(self):
        empty = self.root / "empty.toml"
        empty.write_text("", encoding="utf-8")
        self.assertEqual(self.run_main(converted=empty)[0], 1)
        self.assertEqual(self.run_main(converted=self.root / "missing.toml")[0], 1)
        other = self.root / "elsewhere" / "full.md"
        other.parent.mkdir()
        other.write_text(GUIDE, encoding="utf-8")
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = draft_ts_review.main([str(self.converted), str(other), "--out-dir", str(self.out)])
        self.assertEqual(code, 1)
        self.assertIn("--game", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
