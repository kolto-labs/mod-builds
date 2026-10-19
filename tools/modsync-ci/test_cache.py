import io
import os
from pathlib import Path
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

import artstore_core as core
import test_cache_artstore as artstore_tests
import test_cache_sources as sources

cache = sources.cache


def sha(number):
    return "%064x" % number


class BudgetTests(unittest.TestCase):
    def test_without_a_deadline_there_is_no_budget(self):
        args = types.SimpleNamespace()
        self.assertIsNone(cache.budget_left(args))
        self.assertFalse(cache.budget_spent(args))
        self.assertEqual(cache.bounded(args, 1800, 120), 1800)

    def test_the_deadline_cuts_timeouts_but_never_below_the_floor(self):
        args = types.SimpleNamespace(deadline=time.monotonic() + 30)
        self.assertFalse(cache.budget_spent(args))
        self.assertTrue(0 < cache.bounded(args, 1800, 5) <= 30)
        self.assertEqual(cache.bounded(args, 10, 5), 10)
        args.deadline = time.monotonic() - 1
        self.assertTrue(cache.budget_spent(args))
        self.assertEqual(cache.budget_left(args), 0)
        self.assertEqual(cache.bounded(args, 1800, 120), 120)

    def test_rclone_runs_with_tight_limits_and_the_remaining_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory, "out")
            destination.write_bytes(b"x")
            args = types.SimpleNamespace(remote="MODCACHE:bucket", backend_timeout=1800,
                                         deadline=time.monotonic() - 1)
            with mock.patch.object(cache.subprocess, "run") as run:
                self.assertTrue(cache.remote_copy(args, "MODCACHE:bucket/x", destination))
        command = run.call_args.args[0]
        for flag, value in (("--retries", "1"), ("--low-level-retries", "2"), ("--contimeout", "15s"),
                            ("--timeout", "30s")):
            self.assertEqual(command[command.index(flag) + 1], value)
        self.assertEqual(run.call_args.kwargs["timeout"], cache.RECOVERY_FLOOR_SECONDS)


class DeadPackTests(unittest.TestCase):
    """A pack that is gone is remembered for the run: its other blobs are not tried."""

    def setUp(self):
        patcher = mock.patch.dict(cache.ARTSTORE_STATE, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.base = Path(directory.name)
        self.calls = []
        self.hits = set()

    def install(self, packs):
        """packs: {name: (artifact_id, expires_at, [sha, ...])}"""
        index = core.new_index()
        for name, (artifact_id, expires, shas) in packs.items():
            core.add_pack(index, name, [artstore_tests.fake_blob(each) for each in shas],
                          artifact_id=artifact_id, expires_at=expires)

        def fetch(sha256, dest, *, client=None, index=None, workdir=None, key_env="K", workers=4):
            self.calls.append(sha256)
            if sha256 in self.hits:
                Path(dest).write_bytes(b"x")
                return True
            return False

        cache.ARTSTORE_STATE["module"] = types.SimpleNamespace(
            fetch=fetch, load_index=lambda client=None, **options: index, core=core,
            client_mod=types.SimpleNamespace(from_environment=lambda: object()))

    def ask(self, number):
        args = types.SimpleNamespace(artstore=True)
        return cache.try_artstore({"sha256": sha(number), "size": 1}, self.base / sha(number), args)

    def test_a_pack_without_an_artifact_is_skipped_for_every_blob_with_one_warning(self):
        self.install({"pack-1": (None, None, [sha(1), sha(2), sha(3)])})
        with mock.patch.object(cache, "warn") as warn:
            self.assertEqual([self.ask(number) for number in (1, 2, 3)], [False] * 3)
        self.assertEqual(self.calls, [])
        self.assertEqual(warn.call_count, 1)
        self.assertIn("pack-1", warn.call_args.args[0])

    def test_an_expired_pack_is_skipped_without_asking_the_store(self):
        self.install({"pack-1": (7, time.time() - 10, [sha(1), sha(2)])})
        with mock.patch.object(cache, "warn") as warn:
            self.assertEqual([self.ask(1), self.ask(2)], [False, False])
        self.assertEqual((self.calls, warn.call_count), ([], 1))

    def test_a_pack_that_keeps_failing_is_given_up_after_the_limit(self):
        self.install({"pack-1": (7, time.time() + 86400, [sha(number) for number in range(1, 6)])})
        with mock.patch.object(cache, "warn") as warn:
            self.assertEqual([self.ask(number) for number in range(1, 6)], [False] * 5)
        self.assertEqual(len(self.calls), cache.PACK_FAIL_LIMIT)
        self.assertEqual(warn.call_count, 1)

    def test_a_hit_resets_the_failure_count(self):
        self.install({"pack-1": (7, time.time() + 86400, [sha(number) for number in range(1, 6)])})
        self.hits = {sha(2)}
        with mock.patch.object(cache, "warn"):
            results = [self.ask(number) for number in range(1, 6)]
        self.assertEqual(results, [False, True, False, False, False])
        self.assertEqual(self.calls, [sha(1), sha(2), sha(3), sha(4)])

    def test_other_packs_are_unaffected(self):
        self.install({"pack-1": (None, None, [sha(1)]), "pack-2": (7, time.time() + 86400, [sha(2)])})
        self.hits = {sha(2)}
        with mock.patch.object(cache, "warn"):
            self.assertEqual([self.ask(1), self.ask(2)], [False, True])
        self.assertEqual(self.calls, [sha(2)])

    def test_the_session_is_created_once_across_threads(self):
        loads = []

        def load_index(client=None, **options):
            loads.append(client)
            time.sleep(0.05)
            return core.new_index()

        cache.ARTSTORE_STATE["module"] = types.SimpleNamespace(
            load_index=load_index, fetch=lambda *a, **k: False,
            client_mod=types.SimpleNamespace(from_environment=lambda: object()))
        threads = [threading.Thread(target=cache.artstore_session) for _ in range(8)]
        for each in threads:
            each.start()
        for each in threads:
            each.join()
        self.assertEqual(len(loads), 1)


class RecoverTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        (self.root / "objects").mkdir()
        patcher = mock.patch.dict(cache.RUN_STATE, {"p2p_missed": set()})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.args = types.SimpleNamespace(artstore=False, remote=None, max_bytes=10 ** 9)
        self.stored = {"sha256": sha(1), "size": 1}
        self.calls = []

    def swarm(self, peer, dest):
        self.calls.append(peer["sha256"])
        return False

    def test_a_swarm_miss_is_remembered_for_the_run(self):
        item = {"id": "one", "p2p": {"magnet": "magnet:?x"}}
        with mock.patch.object(cache, "try_p2p", self.swarm):
            self.assertEqual(cache.recover_object(item, self.stored, self.root, self.args, {}), "")
            self.assertEqual(cache.recover_object(item, self.stored, self.root, self.args, {}), "")
        self.assertEqual(self.calls, [sha(1)])

    def test_an_archive_without_a_swarm_is_not_remembered_as_a_miss(self):
        with mock.patch.object(cache, "try_p2p", self.swarm):
            cache.recover_object({"id": "one"}, self.stored, self.root, self.args, {})
            cache.recover_object({"id": "one"}, self.stored, self.root, self.args, {})
        self.assertEqual(len(self.calls), 2)

    def test_the_remote_being_dropped_midway_is_not_an_error(self):
        args = types.SimpleNamespace(artstore=False, remote="MODCACHE:bucket", max_bytes=10 ** 9)

        def unreachable(arguments, source, destination):
            arguments.remote = None
            return False

        with mock.patch.object(cache, "remote_copy", unreachable), mock.patch.object(cache, "try_p2p", self.swarm):
            self.assertEqual(cache.recover_object({"id": "one"}, self.stored, self.root, args, {}), "")
            self.assertEqual(cache.recover_object({"id": "one"}, self.stored, self.root, args, {}), "")


class PeerEnvironmentTests(unittest.TestCase):
    def test_a_helper_that_takes_a_runner_gets_scrubbed_children_and_an_untouched_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            helper = Path(directory, "helper")
            helper.mkdir()
            (helper / "p2p_fetch.py").write_text(
                "import os, subprocess, sys\n"
                "def fetch(entry, dest, *, run=subprocess.run):\n"
                "    if os.environ.get('GITHUB_TOKEN') != 'placeholder':\n"
                "        return False\n"
                "    probe = 'import os, sys; sys.exit(7 if \"GITHUB_TOKEN\" in os.environ else 0)'\n"
                "    return run([sys.executable, '-c', probe]).returncode == 0\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "placeholder"}), \
                    mock.patch.object(cache, "__file__", str(helper / "cache.py")):
                self.assertTrue(cache.try_p2p({"sha256": sha(1), "p2p": {"magnet": "magnet:?x"}},
                                              Path(directory, "d")))
                self.assertEqual(os.environ["GITHUB_TOKEN"], "placeholder")


class ProgressTests(unittest.TestCase):
    def setUp(self):
        self.now = [0.0]
        self.progress = cache.Progress("restore", 3, clock=lambda: self.now[0])
        self.items = [{"id": each * 64} for each in "abc"]

    def lines(self, action):
        with mock.patch.object(cache, "say") as say:
            action()
        return [call.args[0] for call in say.call_args_list]

    def test_say_writes_one_flushed_line_to_stderr(self):
        class Recorder(io.StringIO):
            flushes = 0

            def flush(self):
                self.flushes += 1
                super().flush()

        stream = Recorder()
        with mock.patch.object(cache.sys, "stderr", stream):
            cache.say("restore 1/3")
        self.assertEqual(stream.getvalue(), "cache: restore 1/3\n")
        self.assertGreaterEqual(stream.flushes, 1)

    def test_quick_local_hits_stay_silent_and_slow_or_remote_ones_are_named_with_seconds(self):
        def run():
            self.progress.begin(self.items[0])
            self.now[0] = 0.5
            self.progress.finish(self.items[0], "local")
            self.progress.begin(self.items[1])
            self.now[0] = 60.5
            self.progress.finish(self.items[1], "stale-local")
            self.progress.begin(self.items[2])
            self.now[0] = 70.5
            self.progress.finish(self.items[2], "local")

        lines = self.lines(run)
        self.assertEqual(len(lines), 2)
        self.assertIn("restore 2/3 bbbbbbbb stale-local 60.0s", lines[0])
        self.assertIn("restore 3/3 cccccccc local 10.0s", lines[1])

    def test_heartbeat_names_what_is_still_running(self):
        def run():
            self.progress.begin(self.items[0])
            self.now[0] = 90.0
            self.progress.heartbeat()

        (line,) = self.lines(run)
        self.assertIn("0/3 done", line)
        self.assertIn("aaaaaaaa 90s", line)

    def test_summary_counts_sources(self):
        (line,) = self.lines(lambda: self.progress.summary({"local": 2, "stale-local": 1}))
        self.assertIn("local=2, stale-local=1", line)


class AcquisitionsTests(unittest.TestCase):
    def setUp(self):
        self.items = [{"id": each * 64} for each in "abcdef"]
        patcher = mock.patch.object(cache, "say")
        self.say = patcher.start()
        self.addCleanup(patcher.stop)

    def runs(self, workers):
        return cache.Acquisitions(None, None, {}, cache.Progress("restore", len(self.items)), workers=workers)

    def drive(self, runs, items=None):
        try:
            for item in items or self.items:
                runs.submit(item, {})
            return [runs.result(item, {}) for item in items or self.items]
        finally:
            runs.close()

    def test_independent_archives_run_concurrently_and_come_back_per_item(self):
        barrier = threading.Barrier(2, timeout=10)

        def acquire(item, old, root, args, pins):
            barrier.wait()
            return {"id": item["id"]}, "website"

        with mock.patch.object(cache, "acquire", acquire):
            results = self.drive(self.runs(2), self.items[:2])
        self.assertEqual([entry["id"] for entry, _ in results], [each["id"] for each in self.items[:2]])

    def test_concurrency_is_bounded_by_the_worker_count(self):
        active, peak, lock = [0], [0], threading.Lock()

        def acquire(item, old, root, args, pins):
            with lock:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            time.sleep(0.03)
            with lock:
                active[0] -= 1
            return {}, "website"

        with mock.patch.object(cache, "acquire", acquire):
            self.drive(self.runs(2))
        self.assertLessEqual(peak[0], 2)

    def test_one_worker_runs_in_the_callers_thread_in_order(self):
        seen = []

        def acquire(item, old, root, args, pins):
            seen.append((item["id"], threading.get_ident()))
            return {}, "local"

        with mock.patch.object(cache, "acquire", acquire):
            self.drive(self.runs(1))
        self.assertEqual([each[0] for each in seen], [each["id"] for each in self.items])
        self.assertEqual({each[1] for each in seen}, {threading.get_ident()})

    def test_what_acquire_raised_is_raised_for_that_item_only(self):
        def acquire(item, old, root, args, pins):
            if item["id"].startswith("b"):
                raise ValueError("boom")
            return {}, "website"

        runs = self.runs(3)
        with mock.patch.object(cache, "acquire", acquire):
            try:
                for item in self.items[:3]:
                    runs.submit(item, {})
                self.assertEqual(runs.result(self.items[0], {})[1], "website")
                with self.assertRaises(ValueError):
                    runs.result(self.items[1], {})
                self.assertEqual(runs.result(self.items[2], {})[1], "website")
            finally:
                runs.close()
        self.assertTrue(any("bbbbbbbb failed" in call.args[0] for call in self.say.call_args_list))

    def test_a_slow_archive_produces_heartbeats_while_the_loop_waits(self):
        def acquire(item, old, root, args, pins):
            time.sleep(0.3)
            return {}, "website"

        with mock.patch.object(cache, "acquire", acquire), mock.patch.object(cache, "HEARTBEAT_SECONDS", 0.05):
            self.drive(self.runs(2), self.items[:1])
        self.assertTrue(any("still working" in call.args[0] for call in self.say.call_args_list))


class RestoreBoundTests(sources.Fixture):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in artstore_tests.TOKENS:
            os.environ.pop(name, None)

    def test_a_spent_budget_uses_the_cached_copy_without_asking_the_website(self):
        self.populate()
        site = sources.Site({sources.URL: self.payload})
        with mock.patch.object(cache, "request", site), mock.patch.object(cache, "budget_spent", return_value=True), \
                mock.patch.object(cache, "warn") as warn:
            code, report = self.run_main("restore", "--no-artstore")
        self.assertEqual((code, report["entries"][0]["source"], site.requests), (0, "stale-local", []))
        self.assertEqual(self.restored(), self.payload)
        self.assertEqual(warn.call_count, 1)
        self.assertIn("budget", warn.call_args.args[0])

    def test_a_spent_budget_still_fetches_an_archive_that_has_no_local_copy(self):
        self.populate()
        (self.root / "objects" / self.sha).unlink()
        site = sources.Site({sources.URL: self.payload})
        with mock.patch.object(cache, "request", site), mock.patch.object(cache, "budget_spent", return_value=True):
            code, report = self.run_main("restore", "--no-artstore")
        self.assertEqual((code, report["entries"][0]["source"]), (0, "website"))
        self.assertEqual(self.restored(), self.payload)

    def test_restore_reports_per_archive_progress_and_a_summary(self):
        self.populate()
        site = sources.Site({sources.URL: self.payload})
        with mock.patch.object(cache, "request", site), mock.patch.object(cache, "say") as say:
            code, report = self.run_main("restore", "--no-artstore")
        lines = [call.args[0] for call in say.call_args_list]
        self.assertEqual(code, 0, report)
        self.assertTrue(any("restore 1/1" in line and "revalidated" in line for line in lines), lines)
        self.assertTrue(any("finished 1/1" in line for line in lines), lines)

    def test_the_revalidation_asks_with_a_short_timeout_and_one_retry(self):
        self.populate()
        seen = []

        def request(url, args, headers=None):
            seen.append((args.timeout, args.retries))
            return sources.Site({sources.URL: self.payload})(url, args, headers)

        with mock.patch.object(cache, "request", request):
            code, _ = self.run_main("restore", "--no-artstore", "--timeout", "30", "--retries", "5")
        self.assertEqual((code, seen), (0, [(cache.REVALIDATE_TIMEOUT_SECONDS, 1)]))

    def test_bad_worker_and_budget_values_are_rejected(self):
        for flags in (("--workers", "0"), ("--workers", str(cache.MAX_WORKERS + 1)),
                      ("--restore-budget-seconds", "-1")):
            with self.subTest(flags=flags), self.assertRaises(SystemExit):
                self.run_main("restore", *flags)


class HostBreakerTests(sources.Fixture):
    URLS = tuple("https://files.example.com/pack_%s.zip" % each for each in "abcd")

    def test_a_host_that_keeps_failing_is_not_asked_again_this_run(self):
        self.populate({url: sources.zip_bytes(label) for url, label in zip(self.URLS, "abcd")})
        asked = []

        def down(url, args, headers=None):
            asked.append(url)
            raise ValueError("network request failed")

        with mock.patch.object(cache, "request", down), mock.patch.object(cache, "warn"):
            code, report = self.run_main("restore", "--workers", "1")
        self.assertEqual(code, 0, report)
        self.assertEqual(len(asked), cache.HOST_FAIL_LIMIT)
        self.assertEqual(report["sources"], {"stale-local": 4})

    def test_a_missing_file_is_not_a_down_host(self):
        self.populate({url: sources.zip_bytes(label) for url, label in zip(self.URLS, "abcd")})
        asked = []

        def gone(url, args, headers=None):
            asked.append(url)
            raise ValueError("HTTP 404")

        with mock.patch.object(cache, "request", gone), mock.patch.object(cache, "warn"):
            code, report = self.run_main("restore", "--workers", "1")
        self.assertEqual((code, len(asked)), (0, 4))

    def test_failure_classification(self):
        self.assertTrue(cache.network_failure(ValueError("network request failed")))
        self.assertTrue(cache.network_failure(ValueError("HTTP 503")))
        self.assertTrue(cache.network_failure(TimeoutError()))
        self.assertFalse(cache.network_failure(ValueError("HTTP 404")))
        self.assertFalse(cache.network_failure(ValueError("truncated archive")))


if __name__ == "__main__":
    unittest.main()
