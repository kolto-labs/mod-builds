"""Credential-free CI contracts; no Rust harnesses or application tests."""
import copy
import json
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

SPEC = importlib.util.spec_from_file_location("fullrun_contract", Path(__file__).with_name("fullrun_contract.py"))
ci = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ci)
OWN = "a" * 40
OTHER = "b" * 40

def config(role):
    return {"schema_version": 1, "role": role, "repository": "kolto-labs/ModSync.rs" if role == "engine" else "kolto-labs/mod-builds",
            "engine_repository": "kolto-labs/ModSync.rs", "guide_repository": "kolto-labs/mod-builds", "counterpart_sha": OTHER}

def report(mode="cli"):
    value = {"status": "installed", "implementation": "rust", "mode": mode, "partial": False,
             "omitted_components": [], "component_count": 20, "lane_errors": {"count": 0}, "guide_sha256": "c" * 64}
    if mode == "gui":
        value.update(gui={"coverage": "full"}, gui_not_covered={"excluded_widescreen": [], "widescreen_phase": "none_in_input"})
    return value

class Plans(unittest.TestCase):
    def test_engine_variant_tests_trigger_commit_and_pinned_guides(self):
        c = config("engine")
        p = ci.plan(c, c["repository"], OWN)
        self.assertEqual((p["engine_sha"], p["guide_sha"]), (OWN, OTHER))
    def test_guide_variant_tests_trigger_guides_and_pinned_engine(self):
        c = config("guides")
        p = ci.plan(c, c["repository"], OWN)
        self.assertEqual((p["engine_sha"], p["guide_sha"]), (OTHER, OWN))
    def test_counterpart_override_is_exact_and_changes_plan_identity(self):
        c = config("engine")
        self.assertNotEqual(ci.plan(c, c["repository"], OWN)["plan_id"],
                            ci.plan(c, c["repository"], OWN, "c" * 40)["plan_id"])
        for ref in ("main", "", "a" * 39, "A" * 40, "a" * 40 + "\n"):
            if ref == "":
                continue
            with self.subTest(ref=ref), self.assertRaises(ValueError):
                ci.plan(c, c["repository"], OWN, ref)
    def test_cross_repository_execution_is_not_shared_ci(self):
        with self.assertRaisesRegex(ValueError, "owning repository"):
            ci.plan(config("engine"), "other/caller", OWN)
    def test_owner_role_cannot_substitute_another_repository(self):
        c = config("engine"); c["engine_repository"] = "other/engine"
        with self.assertRaises(ValueError): ci.plan(c, c["repository"], OWN)
    def test_pr_and_push_cannot_admit_secret_bearing_operations(self):
        c = config("guides")
        for event in ("pull_request", "push", "workflow_run"):
            with self.subTest(event=event), self.assertRaises(ValueError):
                ci.plan(c, c["repository"], OWN, operation="full", event=event)
    def test_invalid_modes_and_writer_types_fail_closed(self):
        c = config("engine")
        for kw in ({"game":"k3"}, {"mode":"manual"}, {"operation":"renew"}, {"write_cache":"false"}):
            with self.subTest(kw=kw), self.assertRaises(ValueError):
                ci.plan(c, c["repository"], OWN, **kw)
    def test_nonproducer_operations_ignore_the_producer_checkbox(self):
        c = config("engine")
        for operation in ("check", "install", "seed", "p2p"):
            self.assertFalse(ci.plan(c, c["repository"], OWN, operation=operation, write_cache=True)["cache_writer"])

class CompleteCoverage(unittest.TestCase):
    def test_complete_cli_and_gui_are_single_lane_evidence_only(self):
        for mode in ("cli", "gui"):
            self.assertEqual(ci.require_complete(report(mode), mode)["cross_lane_parity"], "unverified")
    def test_missing_partial_omitted_and_quarantined_reports_fail(self):
        for patch in ({"status":"partial"}, {"status":"blocked"}, {"partial":True},
                      {"partial":None}, {"omitted_components":[{}]}, {"omitted_components":None},
                      {"component_count":0}, {"component_count":True}, {"lane_errors":{"count":1}},
                      {"lane_errors":None}, {"mode":"gui"}):
            r = report(); r.update(patch)
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                ci.require_complete(r, "cli")
    def test_guide_mismatch_fails(self):
        with self.assertRaisesRegex(ValueError, "digest"):
            ci.require_complete(report(), "cli", "d" * 64)
    def test_gui_subset_and_widescreen_omission_fail(self):
        for patch in ({"gui":{"coverage":"subset"}}, {"gui":None},
                      {"gui_not_covered":{"excluded_widescreen":[{}],"widescreen_phase":"not_covered_by_gui_lane"}}):
            r = report("gui"); r.update(patch)
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                ci.require_complete(r, "gui")

class Aggregate(unittest.TestCase):
    def test_failure_retains_a_failed_verdict_and_exits_nonzero(self):
        with tempfile.TemporaryDirectory() as root:
            output = Path(root) / "result.json"
            with mock.patch.dict(os.environ, {"NEEDS_JSON": "{}"}):
                self.assertEqual(ci.main(["aggregate", "--operation", "full", "--output", str(output)]), 1)
            self.assertEqual(json.loads(output.read_text())["status"], "failed")
    def test_failure_skip_cancel_and_missing_all_fail(self):
        for state in ("failure", "skipped", "cancelled", None):
            with self.subTest(state=state), self.assertRaises(ValueError):
                ci.aggregate("install", {"plan":{"result":"success"}, "toolkit":{"result":"success"},
                                         "install":{"result":state}})
    def test_requested_scope_is_explicit(self):
        self.assertEqual(ci.aggregate("check", {"plan":{"result":"success"}, "toolkit":{"result":"success"}})["operation"], "check")
        with self.assertRaises(ValueError): ci.aggregate("full", {"plan":{"result":"success"}, "toolkit":{"result":"success"}})

if __name__ == "__main__":
    unittest.main()
