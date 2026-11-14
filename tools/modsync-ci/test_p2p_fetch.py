import contextlib
import hashlib
import importlib.util
import io
from pathlib import Path
import subprocess
import tempfile
import types
import unittest

spec = importlib.util.spec_from_file_location("p2p_fetch", Path(__file__).with_name("p2p_fetch.py"))
p2p_fetch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p2p_fetch)

PAYLOAD = b"archive-bytes" * 100
GOOD = hashlib.sha256(PAYLOAD).hexdigest()
MAGNET = "magnet:?xt=urn:btih:" + "a" * 40


def entry_for(sha256=GOOD, **block):
    values = {"magnet": MAGNET, "peers": ["seed.example.org:6881", "10.0.0.5:51413"]}
    values.update(block)
    return {"sha256": sha256, "p2p": values}


class FakeRun:
    def __init__(self, payload=PAYLOAD, returncode=0, error=None, write=True, stderr=""):
        self.payload, self.returncode, self.error, self.write, self.stderr = payload, returncode, error, write, stderr
        self.commands = []
        self.kwargs = []

    def __call__(self, command, **kwargs):
        self.commands.append(command)
        self.kwargs.append(kwargs)
        if self.error:
            raise self.error
        if self.write:
            Path(command[command.index("torrent-download") + 2]).write_bytes(self.payload)
        return types.SimpleNamespace(returncode=self.returncode, stdout="", stderr=self.stderr)


class P2PFetchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dest = Path(self.tmp.name) / "out" / "mod.zip"

    def call(self, entry, fake, **kwargs):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            result = p2p_fetch.fetch(entry, self.dest, binary="/bin/modsync", run=fake, **kwargs)
        return result, stderr.getvalue()

    def leftovers(self):
        return [each for each in Path(self.tmp.name).rglob("*") if each.is_file() and each != self.dest]

    def test_success_moves_verified_file_and_builds_command(self):
        fake = FakeRun()
        result, log = self.call(entry_for(torrent_info="QUJD"), fake)
        self.assertTrue(result)
        self.assertEqual(self.dest.read_bytes(), PAYLOAD)
        self.assertEqual(self.leftovers(), [])
        self.assertEqual(log, "")
        command = fake.commands[0]
        self.assertEqual(command[:3], ["/bin/modsync", "torrent-download", MAGNET])
        self.assertEqual(Path(command[3]).parent, self.dest.parent)
        self.assertNotEqual(Path(command[3]), self.dest)
        self.assertEqual(command[command.index("--sha256") + 1], GOOD)
        self.assertIn("--no-proxy", command)
        self.assertEqual(command[command.index("--torrent-info") + 1], "QUJD")
        peers = [command[index + 1] for index, each in enumerate(command) if each == "--peer"]
        self.assertEqual(peers, ["seed.example.org:6881", "10.0.0.5:51413"])
        self.assertEqual(fake.kwargs[0]["timeout"], 150)

    def test_uppercase_expected_hash_is_accepted(self):
        result, _ = self.call(entry_for(sha256=GOOD.upper()), FakeRun())
        self.assertTrue(result)

    def test_wrong_hash_output_is_rejected_and_cleaned(self):
        result, log = self.call(entry_for(), FakeRun(payload=b"tampered"))
        self.assertFalse(result)
        self.assertFalse(self.dest.exists())
        self.assertEqual(self.leftovers(), [])
        self.assertIn("sha256 mismatch", log)
        self.assertEqual(len(log.strip().splitlines()), 1)

    def test_wrong_hash_does_not_clobber_existing_dest(self):
        self.dest.parent.mkdir(parents=True)
        self.dest.write_bytes(b"previous")
        result, _ = self.call(entry_for(), FakeRun(payload=b"tampered"))
        self.assertFalse(result)
        self.assertEqual(self.dest.read_bytes(), b"previous")

    def test_nonzero_exit_cleans_partial_output(self):
        result, log = self.call(entry_for(), FakeRun(returncode=1, stderr="no peers"))
        self.assertFalse(result)
        self.assertFalse(self.dest.exists())
        self.assertEqual(self.leftovers(), [])
        self.assertIn("exited 1", log)

    def test_zero_exit_without_output_fails(self):
        result, _ = self.call(entry_for(), FakeRun(write=False))
        self.assertFalse(result)
        self.assertEqual(self.leftovers(), [])

    def test_timeout_is_swallowed_and_cleaned(self):
        error = subprocess.TimeoutExpired(["modsync"], 150)
        result, log = self.call(entry_for(), FakeRun(error=error), timeout=7)
        self.assertFalse(result)
        self.assertFalse(self.dest.exists())
        self.assertIn("timed out after 7s", log)

    def test_unexpected_exception_never_raises(self):
        result, log = self.call(entry_for(), FakeRun(error=OSError("boom")))
        self.assertFalse(result)
        self.assertIn("OSError", log)

    def test_missing_configuration_spawns_nothing(self):
        cases = [
            {"sha256": GOOD},
            {"sha256": GOOD, "p2p": {}},
            {"sha256": GOOD, "p2p": {"peers": ["a:1"]}},
            {"sha256": GOOD, "p2p": {"magnet": "http://not-a-magnet"}},
            {"p2p": {"magnet": MAGNET}},
            entry_for(sha256="not-a-hash"),
            {"sha256": GOOD, "p2p": "magnet"},
            None,
        ]
        for entry in cases:
            fake = FakeRun()
            result, _ = self.call(entry, fake)
            self.assertFalse(result, entry)
            self.assertEqual(fake.commands, [], entry)
            self.assertFalse(self.dest.exists())

    def test_missing_binary_spawns_nothing(self):
        fake = FakeRun()
        with mock_environ({}), contextlib.chdir(self.tmp.name):
            result = p2p_fetch.fetch(entry_for(), self.dest, run=fake)
        self.assertFalse(result)
        self.assertEqual(fake.commands, [])

    def test_binary_from_environment(self):
        fake = FakeRun()
        with mock_environ({"MODSYNC_BINARY": "/opt/modsync"}):
            self.assertTrue(p2p_fetch.fetch(entry_for(), self.dest, run=fake))
        self.assertEqual(fake.commands[0][0], "/opt/modsync")

    def test_binary_from_ci_path(self):
        built = Path(self.tmp.name) / ".modsync-source/target/debug/modsync"
        built.parent.mkdir(parents=True)
        built.write_bytes(b"")
        fake = FakeRun()
        with mock_environ({}), contextlib.chdir(self.tmp.name):
            self.assertTrue(p2p_fetch.fetch(entry_for(), self.dest, run=fake))
        self.assertEqual(fake.commands[0][0], str(p2p_fetch.CI_BINARY))

    def test_invalid_peers_are_dropped(self):
        fake = FakeRun()
        entry = entry_for(peers=["--evil", "nohost", "ok.example:80", 5])
        self.assertTrue(self.call(entry, fake)[0])
        command = fake.commands[0]
        peers = [command[index + 1] for index, each in enumerate(command) if each == "--peer"]
        self.assertEqual(peers, ["ok.example:80"])

    def test_describe(self):
        self.assertEqual(p2p_fetch.describe(entry_for()), "p2p-configured")
        self.assertEqual(p2p_fetch.describe({"sha256": GOOD}), "p2p-unconfigured")
        self.assertEqual(p2p_fetch.describe({"p2p": {"magnet": MAGNET}}), "p2p-unconfigured")
        self.assertEqual(p2p_fetch.describe(None), "p2p-unconfigured")


@contextlib.contextmanager
def mock_environ(values):
    import os
    saved = dict(os.environ)
    os.environ.clear()
    os.environ.update(values)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)


if __name__ == "__main__":
    unittest.main()
