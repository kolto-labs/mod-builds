import contextlib
import hashlib
import io
import os
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import game_seed

URL = "https://seed.example.invalid/blob?token=abc"
KEY = "passphrase-value"
DATA = b"seed bytes"
DIGEST = hashlib.sha256(DATA).hexdigest()


def environ(**extra):
    values = {"GAME_SEED_URL_K1": URL, "GAME_SEED_KEY_K1": KEY}
    values.update(extra)
    return values


def fake_download(data=DATA, ok=True):
    def download(seed, archive):
        if ok:
            Path(archive).write_bytes(data)
        return ok
    return download


def fake_pipeline(chitin=True, codes=(0, 0, 0)):
    calls = []

    def pipeline(commands, env):
        calls.append(commands)
        if chitin and not any(codes):
            target = Path(commands[-1][-1])
            target.mkdir(parents=True, exist_ok=True)
            (target / "chitin.key").write_bytes(b"k")
        return list(codes), ["openssl exit %d" % codes[0]]
    pipeline.calls = calls
    return pipeline


class ConfigTests(unittest.TestCase):
    def test_names_uppercase_the_game(self):
        self.assertEqual(game_seed.env_names("k2"), {
            "url": "GAME_SEED_URL_K2", "key": "GAME_SEED_KEY_K2", "sha256": "GAME_SEED_SHA256_K2"})

    def test_complete_environment(self):
        seed, reason = game_seed.read_config("k1", environ(GAME_SEED_SHA256_K1=DIGEST.upper()))
        self.assertEqual(reason, "")
        self.assertEqual(seed, game_seed.Seed(url=URL, key_env="GAME_SEED_KEY_K1", sha256=DIGEST))

    def test_sha256_is_optional(self):
        seed, _ = game_seed.read_config("k1", environ())
        self.assertEqual(seed.sha256, "")

    def test_missing_names_are_reported_without_values(self):
        seed, reason = game_seed.read_config("k1", {"GAME_SEED_URL_K1": URL})
        self.assertIsNone(seed)
        self.assertIn("GAME_SEED_KEY_K1", reason)
        self.assertNotIn(URL, reason)
        seed, reason = game_seed.read_config("k2", environ())
        self.assertIsNone(seed)
        self.assertIn("GAME_SEED_URL_K2", reason)
        self.assertIn("GAME_SEED_KEY_K2", reason)

    def test_rejects_plain_http_and_whitespace(self):
        for url in ("http://seed.example.invalid/x", "https://seed.example.invalid/a b", "ftp://x"):
            seed, reason = game_seed.read_config("k1", environ(GAME_SEED_URL_K1=url))
            self.assertIsNone(seed)
            self.assertNotIn(url, reason)

    def test_rejects_malformed_sha256(self):
        seed, reason = game_seed.read_config("k1", environ(GAME_SEED_SHA256_K1="abc"))
        self.assertIsNone(seed)
        self.assertIn("GAME_SEED_SHA256_K1", reason)

    def test_valid_sha256(self):
        self.assertTrue(game_seed.valid_sha256(DIGEST))
        self.assertFalse(game_seed.valid_sha256(DIGEST[:-1]))
        self.assertFalse(game_seed.valid_sha256("g" * 64))


class CommandTests(unittest.TestCase):
    def test_curl_command_keeps_url_out_of_argv(self):
        command = game_seed.curl_command("/tmp/seed.bin")
        self.assertNotIn(URL, " ".join(command))
        self.assertEqual(command[:2], ["curl", "-fL"])
        self.assertIn("--retry", command)
        self.assertEqual(command[-2:], ["--config", "-"])

    def test_curl_config_escapes(self):
        self.assertEqual(game_seed.curl_config('https://x/a"b\\c'), 'url = "https://x/a\\"b\\\\c"\n')

    def test_decrypt_pipeline(self):
        openssl, zstd, tar = game_seed.decrypt_commands("/tmp/seed.bin", "GAME_SEED_KEY_K1", "/tmp/game")
        self.assertEqual(openssl[:8], ["openssl", "enc", "-d", "-aes-256-cbc", "-pbkdf2", "-iter", "200000", "-pass"])
        self.assertEqual(openssl[8], "env:GAME_SEED_KEY_K1")
        self.assertNotIn(KEY, " ".join(openssl))
        self.assertEqual(zstd[:2], ["zstd", "-d"])
        self.assertEqual(tar, ["tar", "-x", "-C", "/tmp/game"])


class DigestTests(unittest.TestCase):
    def test_match_and_mismatch(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "seed.bin"
            path.write_bytes(DATA)
            self.assertEqual(game_seed.verify_sha256(path, DIGEST), DIGEST)
            self.assertEqual(game_seed.verify_sha256(path, DIGEST.upper()), DIGEST)
            with self.assertRaises(game_seed.SeedIntegrityError):
                game_seed.verify_sha256(path, "0" * 64)


class DownloadTests(unittest.TestCase):
    def test_retries_then_succeeds_and_feeds_url_on_stdin(self):
        results = [SimpleNamespace(returncode=18), SimpleNamespace(returncode=0)]
        runner = mock.Mock(side_effect=results)
        seed = game_seed.Seed(URL, "GAME_SEED_KEY_K1", "")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(game_seed.download(seed, "/tmp/seed.bin", runner=runner))
        self.assertEqual(runner.call_count, 2)
        for call in runner.call_args_list:
            self.assertIn(URL, call.kwargs["input"])
            self.assertNotIn(URL, " ".join(call.args[0]))

    def test_gives_up_and_never_prints_url(self):
        runner = mock.Mock(return_value=SimpleNamespace(returncode=22))
        seed = game_seed.Seed(URL, "GAME_SEED_KEY_K1", "")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertFalse(game_seed.download(seed, "/tmp/seed.bin", runner=runner, attempts=2))
        self.assertEqual(runner.call_count, 2)
        self.assertNotIn(URL, out.getvalue())

    def test_range_error_drops_partial_file(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "seed.bin"
            archive.write_bytes(b"partial")
            runner = mock.Mock(return_value=SimpleNamespace(returncode=game_seed.CURL_RANGE_ERROR))
            seed = game_seed.Seed(URL, "GAME_SEED_KEY_K1", "")
            with contextlib.redirect_stdout(io.StringIO()):
                game_seed.download(seed, archive, runner=runner, attempts=1)
            self.assertFalse(archive.exists())


class ZstdTests(unittest.TestCase):
    def test_present(self):
        runner = mock.Mock()
        self.assertTrue(game_seed.ensure_zstd(runner=runner, which=lambda name: "/usr/bin/zstd"))
        runner.assert_not_called()

    def test_installs_when_missing(self):
        found = iter([None, "/usr/bin/zstd"])
        runner = mock.Mock()
        self.assertTrue(game_seed.ensure_zstd(runner=runner, which=lambda name: next(found)))
        self.assertEqual(runner.call_args.args[0][:2], ["sudo", "apt-get"])

    def test_still_missing(self):
        self.assertFalse(game_seed.ensure_zstd(runner=mock.Mock(), which=lambda name: None))


class RestoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.game_dir = self.root / "game"

    def run_restore(self, env, **kwargs):
        options = {"download_fn": fake_download(), "pipeline_fn": fake_pipeline(), "zstd_fn": lambda: True,
                   "work_dir": self.root}
        options.update(kwargs)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            result = game_seed.restore("k1", self.game_dir, env, **options)
        return result, out.getvalue()

    def test_success_masks_url_and_unpacks(self):
        pipeline = fake_pipeline()
        result, output = self.run_restore(environ(GAME_SEED_SHA256_K1=DIGEST), pipeline_fn=pipeline)
        self.assertTrue(result)
        self.assertIn("::add-mask::" + URL, output)
        self.assertEqual(output.count(URL), 1)
        self.assertNotIn(KEY, output)
        self.assertTrue((self.game_dir / "chitin.key").is_file())
        self.assertEqual(pipeline.calls[0][-1], ["tar", "-x", "-C", str(self.game_dir)])
        self.assertEqual([entry.name for entry in self.root.iterdir()], ["game"])

    def test_missing_secrets_warn_and_fall_through(self):
        result, output = self.run_restore({})
        self.assertFalse(result)
        self.assertIn("::warning::", output)
        self.assertIn("GAME_SEED_URL_K1", output)
        self.assertFalse(self.game_dir.exists())

    def test_download_failure_warns(self):
        result, output = self.run_restore(environ(), download_fn=fake_download(ok=False))
        self.assertFalse(result)
        self.assertIn("::warning::", output)
        self.assertEqual(output.count(URL), 1)

    def test_digest_mismatch_raises_and_cleans_up(self):
        with self.assertRaises(game_seed.SeedIntegrityError):
            self.run_restore(environ(GAME_SEED_SHA256_K1="0" * 64))
        self.assertEqual(list(self.root.iterdir()), [])

    def test_decrypt_failure_clears_game_dir(self):
        pipeline = fake_pipeline(codes=(1, 0, 0))
        result, output = self.run_restore(environ(), pipeline_fn=pipeline)
        self.assertFalse(result)
        self.assertIn("::warning::", output)
        self.assertFalse(self.game_dir.exists())

    def test_missing_chitin_key_clears_game_dir(self):
        result, output = self.run_restore(environ(), pipeline_fn=fake_pipeline(chitin=False))
        self.assertFalse(result)
        self.assertIn("chitin.key", output)
        self.assertFalse(self.game_dir.exists())

    def test_missing_zstd_warns(self):
        result, output = self.run_restore(environ(), zstd_fn=lambda: False)
        self.assertFalse(result)
        self.assertIn("zstd", output)


class CommandLineTests(unittest.TestCase):
    def test_restore_output_and_exit_codes(self):
        with tempfile.TemporaryDirectory() as directory:
            output_file = Path(directory) / "output"
            args = Namespace(game="k1", game_dir=str(Path(directory) / "game"))
            with mock.patch.dict(os.environ, {"GITHUB_OUTPUT": str(output_file)}):
                with mock.patch.object(game_seed, "restore", return_value=True):
                    self.assertEqual(game_seed.command_restore(args), 0)
                self.assertEqual(output_file.read_text(), "restored=true\n")
                with mock.patch.object(game_seed, "restore", return_value=False):
                    self.assertEqual(game_seed.command_restore(args), 0)
                self.assertEqual(output_file.read_text(), "restored=true\n")
                out = io.StringIO()
                with mock.patch.object(game_seed, "restore", side_effect=game_seed.SeedIntegrityError("bad")):
                    with contextlib.redirect_stdout(out):
                        self.assertEqual(game_seed.command_restore(args), 1)
                self.assertIn("::error::", out.getvalue())


if __name__ == "__main__":
    unittest.main()
