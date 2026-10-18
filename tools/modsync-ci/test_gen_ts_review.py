import contextlib
import importlib.util
import io
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, HERE / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gen = load("gen_ts_review")
ports = gen.ports

GUIDE = """# Guide

## Optional Install Assistant - Quicker TSLPatching

### Quicker TSLPatching

## Mod List

### Alpha

### 4GB Patcher

## Optional Widescreen & Extended Widescreen Support Mods

### Remastered Cutscenes
"""

CONVERTED = """
[[thisMod]]
Guid = "{10000000-0000-0000-0000-000000000001}"
Name = "Alpha"
IsSelected = true

[[thisMod.Instructions]]
Guid = "{20000000-0000-0000-0000-000000000001}"
Action = "Patcher"
Source = ["<<modDirectory>>\\\\alpha"]

[[thisMod.Instructions]]
Guid = "{20000000-0000-0000-0000-000000000002}"
Action = "Execute"
Source = ["<<modDirectory>>\\\\alpha\\\\run.exe"]

[[thisMod]]
Guid = "{10000000-0000-0000-0000-000000000002}"
Name = "4GB Patcher"
IsSelected = true

[[thisMod.Instructions]]
Guid = "{20000000-0000-0000-0000-000000000003}"
Action = "Execute"
Source = ["<<modDirectory>>\\\\4GB Patcher\\\\patch.exe"]

[[thisMod]]
Guid = "{10000000-0000-0000-0000-000000000003}"
Name = "Remastered Cutscenes"
IsWidescreen = true
IsSelected = false

[[thisMod.Instructions]]
Guid = "{20000000-0000-0000-0000-000000000004}"
Action = "Move"
Source = ["<<modDirectory>>\\\\movies\\\\*"]
Destination = "<<kotorDirectory>>\\\\movies"
"""

SKIPPED = "10000000-0000-0000-0000-000000000002", "10000000-0000-0000-0000-000000000003"


class Fixture(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        (self.root / "content" / "k1").mkdir(parents=True)
        (self.root / "content" / "k1" / "full.md").write_text(GUIDE, encoding="utf-8")
        self.converted = self.root / "converted.toml"
        self.converted.write_text(CONVERTED, encoding="utf-8")
        self.lane_skips = self.root / "lane-skips.json"
        self.lane_skips.write_text(json.dumps({"lane_skips": {"k1": [
            {"section": "Mod List", "heading": "4GB Patcher", "reason": "reviewed: test"}]}}),
            encoding="utf-8")

    def generate(self, text=None):
        if text is not None:
            self.converted.write_text(text, encoding="utf-8")
        return gen.generate("k1", None, self.root, self.converted, self.lane_skips)

    def run_main(self, *extra):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch.object(gen.ports, "LANE_SKIPS_PATH", self.lane_skips):
            code = gen.main(["--root", str(self.root), "--game", "k1", "--converted",
                             str(self.converted)] + list(extra))
        return code, out.getvalue(), err.getvalue()


class GenerateTests(Fixture):
    def test_manifest_is_complete_and_passes_review_manifest(self):
        result = self.generate()
        manifest = json.loads(result["content"])
        for key in ("draft", "draft_notice"):
            self.assertNotIn(key, manifest)
        self.assertEqual(manifest["reviewer"], "delegated-by-owner (orchestrator, 2026-10-18)")
        self.assertEqual(manifest["unresolved"], [])
        self.assertEqual(manifest["converted_sha256"], ports.digest(self.converted))
        self.assertEqual(manifest["guide_sha256"], ports.digest(self.root / "content/k1/full.md"))
        self.assertEqual([entry["guid"] for entry in manifest["components"]],
                         ["10000000-0000-0000-0000-00000000000" + each for each in "123"])
        for entry in manifest["components"]:
            self.assertFalse([key for key in entry if key.startswith("draft")])
            self.assertEqual(entry["unresolved"], [])
        path = self.root / "manifest.json"
        path.write_text(result["content"], encoding="utf-8")
        self.assertEqual(len(ports.review_manifest(path, self.root / "content/k1/full.md", self.converted)), 6)

    def test_defaults_phase_and_skipped_assessment(self):
        alpha, patcher, cutscenes = json.loads(self.generate()["content"])["components"]
        self.assertEqual(alpha["choices"], {"20000000-0000-0000-0000-000000000001": "0",
                                            "20000000-0000-0000-0000-000000000002": "yes"})
        self.assertEqual([alpha["phase"], patcher["phase"], cutscenes["phase"]],
                         ["base", "base", "widescreen"])
        self.assertIn("documented defaults", alpha["assessment"])
        self.assertEqual(patcher["assessment"], "omitted by reviewed policy")
        self.assertEqual(cutscenes["assessment"], "omitted by reviewed policy")
        self.assertEqual(patcher["choices"], {"20000000-0000-0000-0000-000000000003": "yes"})
        self.assertEqual(cutscenes["choices"], {})

    def test_skipped_components_come_from_policy_not_from_the_restore_report(self):
        self.assertEqual(self.generate()["skipped"], 2)

    def test_choose_instructions_have_no_default_and_are_refused(self):
        text = CONVERTED.replace('Action = "Move"', 'Action = "Choose"')
        with self.assertRaisesRegex(gen.Refused, "no documented default"):
            self.generate(text)

    def test_the_guide_must_still_match_the_converted_components(self):
        (self.root / "content/k1/full.md").write_text(GUIDE + "\n### Extra\n", encoding="utf-8")
        with self.assertRaises(ports.Blocked):
            self.generate()

    def test_output_is_deterministic(self):
        self.assertEqual(self.generate()["content"], self.generate()["content"])


class CommandTests(Fixture):
    def test_writes_then_check_passes_then_detects_a_stale_manifest(self):
        code, out, _ = self.run_main()
        self.assertEqual(code, 0, out)
        target = self.root / "ci" / "k1-typescript-review.json"
        self.assertTrue(target.is_file())
        self.assertEqual(self.run_main("--check")[0], 0)
        self.assertIn("unchanged", self.run_main()[1])
        stale = json.loads(target.read_text(encoding="utf-8"))
        stale["converted_sha256"] = "0" * 64
        target.write_text(json.dumps(stale), encoding="utf-8")
        code, _, err = self.run_main("--check")
        self.assertEqual(code, 1)
        self.assertIn("STALE", err)
        self.assertEqual(json.loads(target.read_text(encoding="utf-8"))["converted_sha256"], "0" * 64)
        self.assertEqual(self.run_main()[0], 0)
        self.assertEqual(self.run_main("--check")[0], 0)

    def test_missing_manifest_is_stale_under_check(self):
        self.assertEqual(self.run_main("--check")[0], 1)

    def test_source_is_required_without_converted(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = gen.main(["--root", str(self.root), "--game", "k1"])
        self.assertEqual(code, 1)
        self.assertIn("--source", err.getvalue())

    def test_convert_runs_the_lane_command_and_selects_base_components(self):
        source = self.root / "source"
        (source / "packages/cli/dist").mkdir(parents=True)
        (source / "packages/cli/dist/main.js").write_text("", encoding="utf-8")
        seen = {}

        def fake_run(command, **kwargs):
            seen["command"], seen["cwd"] = command, kwargs["cwd"]
            Path(command[command.index("--output") + 1]).write_text(
                CONVERTED.replace("IsSelected = true", "IsSelected = false"), encoding="utf-8")
            return mock.Mock(returncode=0, stderr="")

        with mock.patch.object(gen.subprocess, "run", fake_run):
            result = gen.generate("k1", source, self.root, None, self.lane_skips)
        command = seen["command"]
        self.assertEqual(command[0], "node")
        self.assertEqual(command[command.index("--game") + 1], "kotor1")
        self.assertEqual(command[command.index("--source-url") + 1], "content/k1/full.md")
        self.assertEqual(seen["cwd"], source)
        selected = CONVERTED.replace("IsSelected = true", "IsSelected = false")
        reference = self.root / "reference.toml"
        reference.write_text(selected, encoding="utf-8")
        ports.select_base_components(reference)
        self.assertEqual(result["converted_sha256"], ports.digest(reference))

    def test_unbuilt_source_is_refused(self):
        with self.assertRaisesRegex(gen.Refused, "not built"):
            gen.generate("k1", self.root / "missing", self.root, None, self.lane_skips)


class WorkflowAgreementTests(unittest.TestCase):
    def test_uncached_policy_steps_match_the_workflow_flags(self):
        text = (REPO / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        flags = re.findall(r'--allow-uncached-by-policy "([^"]+)"', text)
        expected = [section + "::" + heading
                    for steps in gen.UNCACHED_POLICY_STEPS.values() for section, heading in steps]
        self.assertEqual(sorted(set(flags)), sorted(set(expected)))

    def test_typescript_lanes_get_the_policy_flags_report_and_wine(self):
        text = (REPO / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        self.assertNotIn("== rust ]]; then\n            policy", text)
        self.assertIn('review=(--restore-report "$RUNNER_TEMP/restore-report.json")', text)
        wine = text[text.index("Install Wine for legacy patchers and Execute"):]
        self.assertIn("if: matrix.implementation == 'typescript'", wine[:200])
        self.assertIn("--no-install-recommends wine", wine[:400])


class CommittedManifestTests(unittest.TestCase):
    """Guide-bound facts only; converted_sha256 depends on the MODSYNC_TS_REF build and is
    checked by the lane (or `gen_ts_review.py --check` with a built checkout)."""

    def test_each_game_has_a_complete_delegated_manifest_for_the_committed_guide(self):
        for game in gen.GAMES:
            with self.subTest(game=game):
                path = REPO / "ci" / (game + "-typescript-review.json")
                self.assertTrue(path.is_file(), "regenerate with tools/modsync-ci/gen_ts_review.py")
                manifest = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(manifest["guide_sha256"], ports.digest(REPO / "content" / game / "full.md"))
                self.assertEqual(manifest["reviewer"], gen.REVIEWER)
                self.assertEqual(manifest["unresolved"], [])
                self.assertRegex(manifest["converted_sha256"], r"^[0-9a-f]{64}$")
                self.assertFalse([key for key in manifest if key.startswith("draft")])
                omitted = [entry for entry in manifest["components"]
                           if entry["assessment"] == gen.SKIPPED_ASSESSMENT]
                self.assertEqual(len(omitted), 2)
                for entry in manifest["components"]:
                    self.assertEqual(entry["unresolved"], [])
                    self.assertEqual(sorted(set(entry["choices"].values()) - {"0", "yes"}), [])


if __name__ == "__main__":
    unittest.main()
