import contextlib
import hashlib
import io
import json
import os
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import URLError

import artstore_core as core
import test_cache_artstore as artstore_tests
import test_cache_sources as sources

cache = sources.cache

ITEM = {"id": "0123456789abcdef" * 4, "url": "https://files.example.com/mod?token=SECRET", "provider": "nexus"}


def capture(function, *arguments):
    stream = io.StringIO()
    with mock.patch.object(cache.sys, "stderr", stream):
        result = function(*arguments)
    return result, stream.getvalue()


class ReasonTests(unittest.TestCase):
    def test_http_status_and_exception_class_are_kept(self):
        self.assertEqual(cache.failure_reason(ValueError("HTTP 403")), "ValueError HTTP 403")

    def test_a_network_failure_names_only_the_cause_class(self):
        error = cache.network_error(TimeoutError("boom https://h.example/x?sig=SECRET"))
        reason = cache.failure_reason(error)
        self.assertEqual(reason, "ValueError network request failed cause=TimeoutError")
        self.assertNotIn("SECRET", reason)

    def test_request_keeps_the_cause_class_of_the_last_failed_attempt(self):
        args = types.SimpleNamespace(retries=0, timeout=1)
        with mock.patch.object(cache.HTTP, "open", side_effect=URLError("unreachable")):
            with self.assertRaises(ValueError) as caught:
                cache.request("https://files.example.com/a", args)
        self.assertEqual(str(caught.exception), "network request failed")
        self.assertEqual(caught.exception.cause, "URLError")
        self.assertTrue(cache.network_failure(caught.exception))

    def test_other_exceptions_report_their_class_never_their_text(self):
        reason = cache.failure_reason(OSError("cannot open https://h.example/p?sig=SECRET"))
        self.assertEqual(reason, "OSError")

    def test_urls_and_secret_values_are_scrubbed_and_capped(self):
        self.assertEqual(cache.scrub("see https://a.example/b?t=1 end"), "see <url> end")
        with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "ghs_supersecretvalue"}):
            self.assertEqual(cache.scrub("x ghs_supersecretvalue y"), "x *** y")
        self.assertEqual(len(cache.scrub("w" * 1000)), cache.REASON_LIMIT)


class FailureReportTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.summary = Path(directory.name) / "summary.md"
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("GITHUB_STEP_SUMMARY", None)
        patcher = mock.patch.dict(cache.RUN_STATE, {"failures": []})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_failure_is_one_flushed_line_with_the_short_id_and_host_only(self):
        row, text = capture(cache.record_failure, ITEM, "stale-unknown", ValueError("HTTP 403"))
        self.assertEqual(text, "cache: stale-unknown 01234567 host=files.example.com reason=ValueError HTTP 403\n")
        self.assertEqual(row, cache.RUN_STATE["failures"][0])
        self.assertNotIn("SECRET", text)
        self.assertNotIn(ITEM["id"], text)

    def test_every_stale_unknown_archive_gets_a_row_even_without_a_recorded_failure(self):
        cache.record_failure(ITEM, "stale-local", reason="ValueError HTTP 429", echo=False)
        entries = [{"id": ITEM["id"], "status": "stale-unknown"},
                   {"id": "f" * 64, "status": "stale-unknown"},
                   {"id": "e" * 64, "status": "fresh"}]
        rows = cache.failure_rows(entries, cache.RUN_STATE["failures"])
        self.assertEqual([(row["id"], row["outcome"], row["status"]) for row in rows],
                         [("01234567", "stale-local", "stale-unknown"), ("ffffffff", "stale-unknown", "stale-unknown")])
        self.assertIn("not rechecked", rows[1]["reason"])

    def test_the_step_summary_is_a_markdown_table_when_the_variable_is_set(self):
        os.environ["GITHUB_STEP_SUMMARY"] = str(self.summary)
        cache.record_failure(ITEM, "stale-unknown", reason="ValueError a|b", echo=False)
        rows, text = capture(cache.report_failures, [{"id": ITEM["id"], "status": "stale-unknown"}],
                             cache.RUN_STATE["failures"])
        self.assertEqual(len(rows), 1)
        self.assertIn("1 archive(s) failed the upstream check", text)
        self.assertIn("files.example.com x1", text)
        lines = self.summary.read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines[0], "### Mod cache refresh: upstream check failures (1)")
        self.assertIn("| Archive | Outcome | Status | Host | Reason |", lines)
        self.assertIn("| 01234567 | stale-unknown | stale-unknown | files.example.com | ValueError a/b |", lines)

    def test_without_the_variable_nothing_is_written_and_nothing_fails(self):
        cache.record_failure(ITEM, "stale-unknown", reason="ValueError HTTP 500", echo=False)
        _, text = capture(cache.report_failures, [{"id": ITEM["id"], "status": "stale-unknown"}],
                          cache.RUN_STATE["failures"])
        self.assertIn("reasons: ValueError HTTP 500 x1", text)
        self.assertFalse(self.summary.exists())

    def test_a_clean_run_reports_nothing(self):
        rows, text = capture(cache.report_failures, [{"id": ITEM["id"], "status": "fresh"}], [])
        self.assertEqual((rows, text), ([], ""))

    def test_the_table_is_capped(self):
        os.environ["GITHUB_STEP_SUMMARY"] = str(self.summary)
        rows = [("%08d" % number, "stale-local", "fresh", "h", "r") for number in range(cache.SUMMARY_ROWS + 5)]
        cache.append_step_summary(cache.summary_markdown("t", rows, ("a", "b", "c", "d", "e")))
        text = self.summary.read_text(encoding="utf-8")
        self.assertIn("5 more row(s) omitted", text)
        self.assertNotIn("%08d" % cache.SUMMARY_ROWS, text)


class StageTotalsTests(unittest.TestCase):
    def test_totals_count_files_and_bytes_and_land_in_the_step_summary(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ):
            root = Path(directory)
            summary = root / "summary.md"
            os.environ["GITHUB_STEP_SUMMARY"] = str(summary)
            candidates = []
            for number, size in enumerate((1000, 2500)):
                path = root / ("object%d" % number)
                path.write_bytes(b"x" * size)
                candidates.append({"path": str(path), "sha256": ("%02d" % number) * 32})
            _, text = capture(cache.report_stage, candidates)
            self.assertIn("stage manifest: 2 file(s), 3500 bytes", text)
            self.assertIn("stage manifest: 010101010101 2500 bytes", text)
            self.assertIn("2 file(s), 3500 bytes", summary.read_text(encoding="utf-8"))

    def test_human_bytes(self):
        self.assertEqual(cache.human_bytes(8_970_000_000), "8.97 GB")
        self.assertEqual(cache.human_bytes(5_000_000), "5.0 MB")


class DeferredPriorityTests(artstore_tests.Fixture):
    def setUp(self):
        super().setUp()
        (self.root / "objects").mkdir(parents=True)
        self.shas = []
        for label in ("a", "b"):
            data = (label * 40).encode()
            sha = hashlib.sha256(data).hexdigest()
            (self.root / "objects" / sha).write_bytes(data)
            self.shas.append((sha, len(data)))
        self.shas.sort()
        self.manifest_value = {"schema": 1, "entries": {
            "low": {"sha256": self.shas[0][0], "size": self.shas[0][1]},
            "high": {"sha256": self.shas[1][0], "size": self.shas[1][1]}}}

    def store(self, deferred):
        store = artstore_tests.FakeStore()
        make_index = store.make_index

        def with_deferred():
            index = make_index()
            return core.set_deferred(index, {sha: {"since": 1, "size": 1} for sha in deferred})

        store.make_index = with_deferred
        self.install(store)

    def test_deferred_objects_are_staged_first_then_the_rest_by_hash(self):
        self.store([self.shas[1][0]])
        result = cache.stage_candidates(self.root, self.manifest_value)
        self.assertEqual([each["sha256"] for each in result], [self.shas[1][0], self.shas[0][0]])

    def test_without_deferred_state_the_order_is_by_hash(self):
        self.store([])
        result = cache.stage_candidates(self.root, self.manifest_value)
        self.assertEqual([each["sha256"] for each in result], [self.shas[0][0], self.shas[1][0]])

    def test_archives_holding_a_deferred_object_are_processed_first_with_a_notice(self):
        items = [{"id": "low"}, {"id": "high"}, {"id": "none"}]
        ordered, text = capture(cache.prioritise_deferred, items, self.manifest_value["entries"],
                                {self.shas[1][0]})
        self.assertEqual([item["id"] for item in ordered], ["high", "low", "none"])
        self.assertIn("::notice::1 archive(s) with objects deferred by an earlier run", text)

    def test_no_deferred_state_leaves_the_order_and_stays_silent(self):
        items = [{"id": "low"}, {"id": "high"}]
        ordered, text = capture(cache.prioritise_deferred, items, self.manifest_value["entries"], set())
        self.assertEqual((ordered, text), (items, ""))

    def test_an_unavailable_store_defers_nothing(self):
        cache.ARTSTORE_STATE["session"] = None
        self.assertEqual(cache.deferred_shas(), set())


class RefreshDiagnosticsTests(sources.Fixture):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in ("ARTSTORE_TOKEN", "GITHUB_TOKEN", "GH_TOKEN"):
            os.environ.pop(name, None)
        self.summary = self.base / "summary.md"
        os.environ["GITHUB_STEP_SUMMARY"] = str(self.summary)

    def refresh(self, *extra):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = cache.main(["refresh", "--guide", str(self.guide), "--cache-dir", str(self.root),
                               "--retries", "0", *extra])
        return code, json.loads(output.getvalue()), errors.getvalue()

    def test_a_failed_upstream_check_logs_its_reason_and_lands_in_the_table(self):
        self.populate()
        self.age(30)
        failure = cache.network_error(TimeoutError("slow"))
        with mock.patch.object(cache, "request", side_effect=failure):
            code, report, errors = self.refresh()
        self.assertEqual((code, report["entries"][0]["source"]), (0, "stale-local"))
        self.assertIn("(ValueError network request failed cause=TimeoutError)", errors)
        table = self.summary.read_text(encoding="utf-8")
        self.assertIn("| %s | stale-local |" % cache.key_for(sources.URL)[:8], table)
        self.assertIn("cause=TimeoutError", table)

    def test_a_forced_refresh_that_cannot_reach_upstream_names_the_status_and_marks_stale_unknown(self):
        self.populate()
        self.age(30)
        with mock.patch.object(cache, "request", side_effect=ValueError("HTTP 403")):
            code, report, errors = self.refresh("--force")
        short = cache.key_for(sources.URL)[:8]
        self.assertEqual(self.manifest()["entries"][cache.key_for(sources.URL)]["status"], "stale-unknown")
        self.assertIn("cache: stale-unknown %s host=" % short, errors)
        self.assertIn("reason=ValueError HTTP 403", errors)
        self.assertIn("| %s | stale-unknown |" % short, self.summary.read_text(encoding="utf-8"))
        self.assertNotIn(sources.URL, errors)

    def test_a_bik_archive_is_never_cached_or_left_in_the_manifest(self):
        payload = self.payload
        with mock.patch.object(cache, "request", sources.Site({sources.URL: payload})), \
                mock.patch.object(cache, "archive_name", return_value="Movies/intro.bik"):
            code, report, _ = self.refresh()
        self.assertEqual(report["entries"][0]["status"], cache.UNCACHED)
        self.assertFalse((self.root / "objects" / self.sha).exists())
        path = self.root / "manifest.json"
        entries = json.loads(path.read_text(encoding="utf-8"))["entries"] if path.exists() else {}
        self.assertNotIn(cache.key_for(sources.URL), entries)


if __name__ == "__main__":
    unittest.main()
