"""Validate actual owned workflow graph and pins, not copies of YAML strings."""
import json
from pathlib import Path
import re
import unittest
import yaml

ROOT = Path(__file__).resolve().parents[2]

class WorkflowGraph(unittest.TestCase):
    def test_every_local_workflow_action_and_python_entrypoint_exists(self):
        for path in (ROOT / ".github/workflows").glob("*.yml"):
            document = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
            if not path.name.startswith(("fullrun", "cache-")):
                continue
            for job in document.get("jobs", {}).values():
                uses = job.get("uses", "")
                if uses:
                    self.assertTrue(uses.startswith("./.github/workflows/"), "CI runtime must remain repository owned")
                    self.assertTrue((ROOT / uses[2:]).is_file(), (path.name, uses))
                for step in job.get("steps", []):
                    action = step.get("uses", "")
                    if action.startswith("./"):
                        self.assertTrue((ROOT / action[2:] / "action.yml").is_file())
                    for tool in re.findall(r"tools/modsync-ci/[A-Za-z0-9_-]+\.py", step.get("run", "")):
                        self.assertTrue((ROOT / tool).is_file(), tool)
    def test_matrix_has_no_fail_fast_and_honest_final_collector(self):
        main = yaml.load((ROOT / ".github/workflows/fullrun.yml").read_text(), Loader=yaml.BaseLoader)
        self.assertEqual(main["jobs"]["install"]["strategy"]["fail-fast"], "false")
        gate = main["jobs"]["result"]
        self.assertIn("always()", gate["if"])
        self.assertEqual(set(gate["needs"]), {"plan","toolkit","games","archives","install","seed","p2p"})
    def test_install_admits_skipped_producers_but_not_cancelled_or_invalid_plans(self):
        main = yaml.load((ROOT / ".github/workflows/fullrun.yml").read_text(), Loader=yaml.BaseLoader)
        expression = main["jobs"]["install"]["if"][3:-2].strip()
        for operation, plan, cancelled, expected in (("install","success",False,True),
                                                    ("full","success",False,True),
                                                    ("install","failure",False,False),
                                                    ("install","success",True,False)):
            value = expression.replace("needs.plan.outputs.operation", repr(operation)).replace("needs.plan.result", repr(plan))
            for name, result in (("success",False),("failure",False),("cancelled",cancelled)):
                value = value.replace(name+"()", repr(result))
            value = value.replace("&&", " and ").replace("||", " or ").replace("!", " not ")
            self.assertEqual(eval(value, {"__builtins__": {}}), expected)
    def test_pins_are_valid_and_variants_bind_their_own_subject(self):
        config = json.loads((ROOT / ".github/fullrun.json").read_text())
        self.assertRegex(config["counterpart_sha"], r"^[0-9a-f]{40}$")
        self.assertIn(config["role"], ("engine", "guides"))
    def test_automatic_plan_has_no_licensed_data_keys(self):
        main = yaml.load((ROOT / ".github/workflows/fullrun.yml").read_text(), Loader=yaml.BaseLoader)
        step = next(s for s in main["jobs"]["plan"]["steps"] if s.get("id") == "request")
        for key in ("CACHE_ENCRYPTION_KEY", "DIAGNOSTICS_ENCRYPTION_KEY"):
            self.assertIn("github.event_name == 'workflow_dispatch' &&", step["env"][key])
    def test_evidence_upload_requires_a_successful_new_seal(self):
        action = yaml.load((ROOT / ".github/actions/retain-fullrun-evidence/action.yml").read_text(), Loader=yaml.BaseLoader)
        seal, upload = action["runs"]["steps"]
        self.assertEqual(seal["id"], "seal")
        self.assertIn("mktemp -d", seal["run"])
        self.assertIn("steps.seal.outcome == 'success'", upload["if"])
        self.assertEqual(upload["with"]["path"], "${{ steps.seal.outputs.path }}")
    def test_no_test_target_compilation_was_added_to_fullrun_workflows(self):
        for path in (ROOT / ".github/workflows").glob("fullrun*.yml"):
            text = path.read_text()
            self.assertNotRegex(text, r"cargo(?: \+\S+)? (?:test|nextest)\b|--all-targets")

if __name__ == "__main__":
    unittest.main()
