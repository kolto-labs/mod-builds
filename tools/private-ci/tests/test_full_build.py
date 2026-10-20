"""Mechanical controller fixtures only; no original-guide/full-game acceptance."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("full_build", Path(__file__).parents[1]/"src"/"full_build.py")
full = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(full)


class FullBuildMechanicalTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.baseline = self.root/"controlled-baseline"
        self.baseline.mkdir()
        (self.baseline/"modules").mkdir()
        for name in ("chitin.key","dialog.tlk","modules/controlled.mod"):
            (self.baseline/name).write_bytes(b"mechanical-fixture-not-game-data")
        self.guide = self.root/"full.md"
        self.guide.write_text("# Controlled controller fixture\nNo actual full-build acceptance.\n")
        self.cache = self.root/"generation.json"
        self.cache.write_text('{"controlled":true}')
        self.archive = self.root/"controlled.zip"
        self.archive.write_bytes(b"mechanical-fixture-not-mod-data")
        self.steps = [{"id":"first","sourceOwners":["first-source"]}, {"id":"second","sourceOwners":["second-source"]}]
        self.config = {"game":"k1","baselineDir":str(self.baseline),
                       "baselineManifestSha256":full.manifest_hash(full.tree_manifest(self.baseline)),
                       "guidePath":str(self.guide),"guideSha256":full.file_hash(self.guide),
                       "cacheGenerationPath":str(self.cache),"cacheGenerationSha256":full.file_hash(self.cache),
                       "selectedSourceOwners":["first-source","second-source"],"steps":self.steps,
                       "archives":[{"id":"payload","path":str(self.archive),"sha256":full.file_hash(self.archive)}],
                       "manualOperator":"controlled-human-attestation", "timeoutSeconds":10}
        self.coverage = {"reviewApproved":True,"sourceGuideSha256":self.config["guideSha256"],
                         "cacheGenerationSha256":self.config["cacheGenerationSha256"],
                         "selectedSourceOwners":self.config["selectedSourceOwners"],"instructionsResolved":True,
                         "archiveCoverageComplete":True,"stepIds":["first","second"]}
        self.coverage_path = self.root/"coverage.json"
        self.coverage_path.write_text(json.dumps(self.coverage))
        self.config.update(coverageReceiptPath=str(self.coverage_path),coverageReceiptSha256=full.file_hash(self.coverage_path))
        generator = self.root/"controlled-generator.py"
        generator.write_text("import pathlib,shutil,sys\npathlib.Path(sys.argv[1]).read_bytes()\npathlib.Path(sys.argv[2]).write_text('[controlled_fixture]\\naccepted = false\\n')\nshutil.copyfile(sys.argv[4],sys.argv[3])\n")
        self.config["generationCmd"] = [sys.executable,str(generator),"{guide}","{toml}","{coverage}",str(self.coverage_path)]
        self.config["installCmd"] = ["inert-adapter","{toml}","{game}","{barriers}"]
        self.config["guiCmd"] = ["inert-native-adapter","{toml}","{game}","{barriers}"]

    def receipt(self, index, mode="manual"):
        step=self.steps[index]
        return {"stepId":step["id"],"performed":True,"execution":{"manual":"human","cli":"cli","gui":"native_gui"}[mode],
                "sourceGuideSha256":self.config["guideSha256"],"sourceOwners":step["sourceOwners"],
                "archiveHashes":{"payload":self.config["archives"][0]["sha256"]},
                "operator":"controlled-human-attestation"}

    def finish(self, root, mode, content):
        full.prepare(self.config,"k1",mode,root)
        for index in range(2):
            (root/"game"/"result.txt").write_text(content[index])
            full.snapshot(root,self.steps[index]["id"],self.receipt(index,mode))
        full.finalize(root)

    def test_manual_prepares_only_without_product_adapter(self):
        out=self.root/"manual"
        with patch.object(full,"start_adapter",side_effect=AssertionError("Manual must not execute product")):
            state=full.prepare(self.config,"k1","manual",out)
        self.assertEqual(state["mode"],"manual")
        self.assertFalse((out/"scratch.toml").exists())
        self.assertEqual(full.tree_manifest(out/"game"),full.tree_manifest(self.baseline))
        self.assertNotEqual((out/"game"/"chitin.key").stat().st_ino,(self.baseline/"chitin.key").stat().st_ino)
        with self.assertRaises(full.Blocked):
            full.finalize(out)

    def test_manual_run_blocks_until_human_operations(self):
        out=self.root/"human-wait"
        with patch.object(full,"start_adapter",side_effect=AssertionError("No automation may be called")), self.assertRaises(full.Blocked):
            full.run(self.config,"k1","manual",out)
        self.assertTrue((out/"state.json").exists())
        self.assertFalse((out/"complete.json").exists())

    def test_missing_inputs_and_unresolved_coverage_fail_before_clone(self):
        for key,value in (("guideSha256","0"*64),("selectedSourceOwners",["missing-owner"]),("baselineDir",str(self.root/"missing"))):
            config=dict(self.config);config[key]=value
            out=self.root/("blocked-"+key)
            with self.assertRaises(full.Blocked):
                full.prepare(config,"k1","manual",out)
            self.assertFalse(out.exists())
        coverage=dict(self.coverage);coverage["instructionsResolved"]=False
        with self.assertRaises(full.Blocked):
            full.coverage_gate(coverage,self.config)

    def test_symlink_inputs_and_live_overlap_refused(self):
        link=self.baseline/"unsafe"
        try:link.symlink_to(self.archive)
        except OSError:self.skipTest("Symlink capability unavailable")
        with self.assertRaises(full.Blocked):full.tree_manifest(self.baseline)
        link.unlink()
        with self.assertRaises(full.Blocked):full.prepare(self.config,"k1","manual",self.baseline/"lane")

    def test_order_and_authentic_lane_receipt_required(self):
        out=self.root/"manual"
        full.prepare(self.config,"k1","manual",out)
        with self.assertRaises(full.Blocked):full.snapshot(out,"second",self.receipt(1))
        bad=self.receipt(0);bad["execution"]="cli"
        with self.assertRaises(full.Blocked):full.snapshot(out,"first",bad)
        self.assertFalse(list((out/"receipts").glob("*.json")))
        full.snapshot(out,"first",self.receipt(0))
        self.assertTrue((out/"barriers"/"0000.ack.json").is_file())

    def test_original_baseline_and_guide_unchanged_gate(self):
        out=self.root/"manual"
        full.prepare(self.config,"k1","manual",out)
        self.guide.write_text("changed")
        with self.assertRaises(full.Blocked):full.snapshot(out,"first",self.receipt(0))
        self.assertFalse(list((out/"receipts").glob("*.json")))

    def test_scratch_toml_generated_once_and_pinned(self):
        out=self.root/"cli"
        state=full.prepare(self.config,"k1","cli",out)
        self.assertEqual(full.file_hash(out/"scratch.toml"),state["scratchTomlSha256"])
        with self.assertRaises(full.Blocked):full.prepare(self.config,"k1","cli",out)
        (out/"scratch.toml").write_text("changed after generation")
        with self.assertRaises(full.Blocked):full.snapshot(out,"first",self.receipt(0,"cli"))

    def test_independent_external_first_divergence_from_manual(self):
        manual,candidate=self.root/"manual",self.root/"cli"
        self.finish(manual,"manual",["manual-first","same-final"])
        self.finish(candidate,"cli",["product-first","same-final"])
        result=full.compare(manual,candidate)
        self.assertFalse(result["matches"])
        self.assertEqual(result["stage"],"first")
        self.assertEqual(result["divergence"]["path"],"result.txt")
        with self.assertRaises(full.Blocked):full.compare(candidate,manual)

    def test_post_final_mutation_invalidates_comparison(self):
        manual,candidate=self.root/"manual",self.root/"cli"
        self.finish(manual,"manual",["same","same"])
        self.finish(candidate,"cli",["same","same"])
        self.assertTrue(full.compare(manual,candidate)["matches"])
        (candidate/"game"/"later.txt").write_text("unobserved")
        with self.assertRaises(full.Blocked):full.compare(manual,candidate)

    def test_gui_requires_native_evidence_and_private_log(self):
        out=self.root/"gui"
        full.prepare(self.config,"k1","gui",out)
        receipt=self.receipt(0,"gui")
        with self.assertRaises(full.Blocked):full.snapshot(out,"first",receipt)
        log=out/"native-ui.log";log.write_text("controlled mechanical log, no native acceptance")
        receipt.update(nativeUi=True,uiLogPath=log.name,uiLogSha256=full.file_hash(log))
        full.snapshot(out,"first",receipt)

    def test_real_controlled_adapter_waits_for_external_component_snapshots(self):
        # An inert Python adapter exercises protocol mechanics, not ModSync/game parity.
        adapter=self.root/"controlled-adapter.py"
        receipts=self.root/"controlled-receipts.json"
        receipts.write_text(json.dumps([self.receipt(index,"cli") for index in range(2)]))
        adapter.write_text("import json,pathlib,sys,time\ngame=pathlib.Path(sys.argv[2]);barriers=pathlib.Path(sys.argv[3])\nreceipts=json.loads(pathlib.Path(sys.argv[4]).read_text())\nfor index,receipt in enumerate(receipts):\n (game/'result.txt').write_text('controlled-step-'+str(index))\n ready=barriers/f'{index:04d}.ready.json'\n ready.write_text(json.dumps(receipt))\n while not (barriers/f'{index:04d}.ack.json').exists():time.sleep(0.01)\n")
        config=dict(self.config)
        config["installCmd"]=[sys.executable,str(adapter),"{toml}","{game}","{barriers}",str(receipts)]
        out=self.root/"controlled-cli"
        full.run(config,"k1","cli",out)
        snapshots=[full.read_json(out/"snapshots"/f"{index:04d}.json") for index in range(2)]
        self.assertNotEqual(snapshots[0],snapshots[1])
        self.assertTrue((out/"complete.json").is_file())
        self.assertFalse(full.read_json(out/"complete.json")["contentCompared"])

    def test_private_checkout_and_prebuilt_toml_admission(self):
        with patch.dict(full.os.environ,{"GITHUB_WORKSPACE":str(self.root)}), self.assertRaises(full.Blocked):
            full.prepare(self.config,"k1","manual",self.root/"public-lane")
        config=dict(self.config);config["tomlPath"]="already-derived.toml"
        with self.assertRaises(full.Blocked):full.prepare(config,"k1","cli",self.root/"prebuilt")

    def test_included_patcher_provenance_required(self):
        patcher=self.root/"controlled-patcher.exe";patcher.write_bytes(b"inert fixture")
        self.steps[0]["patchers"]=[{"path":str(patcher),"sha256":full.file_hash(patcher),"archiveId":"payload"}]
        out=self.root/"manual"
        full.prepare(self.config,"k1","manual",out)
        with self.assertRaises(full.Blocked):full.snapshot(out,"first",self.receipt(0))
        log=out/"patcher.log";log.write_text("controlled fixture, no actual executable run")
        receipt=self.receipt(0);receipt["patchers"]=[{"executableSha256":full.file_hash(patcher),"archiveId":"payload","logPath":log.name,"logSha256":full.file_hash(log)}]
        full.snapshot(out,"first",receipt)


if __name__ == "__main__":unittest.main()
