import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import types
import unittest
from unittest import mock

import test_cache_sources as sources

cache = sources.cache

SECRETS = {"CACHE_ENCRYPTION_KEY": "k" * 40, "DIAGNOSTICS_ENCRYPTION_KEY": "d" * 40, "GITHUB_TOKEN": "ghs_x",
           "GH_TOKEN": "gh_x", "ARTSTORE_TOKEN": "art_x", "ACTIONS_RUNTIME_TOKEN": "run_x",
           "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "oidc_x"}
S3 = {"MOD_CACHE_S3_SECRET_ACCESS_KEY": "s3_x", "AWS_SECRET_ACCESS_KEY": "aws_x",
      "RCLONE_CONFIG_MODCACHE_SECRET_ACCESS_KEY": "rc_x"}
HARMLESS = {"MODSYNC_ENV_PROBE": "kept", "MODSYNC_ENV_DUMP": ""}


class ScrubbedEnvTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.dump = Path(self.directory.name, "env.json")
        patcher = mock.patch.dict(os.environ, {**SECRETS, **S3, **HARMLESS, "MODSYNC_ENV_DUMP": str(self.dump)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def tool(self, name):
        path = Path(self.directory.name, name)
        path.write_text("#!" + sys.executable + "\nimport json, os, sys\n"
                        "json.dump(dict(os.environ), open(os.environ['MODSYNC_ENV_DUMP'], 'w'))\n"
                        "if len(sys.argv) > 3 and sys.argv[1] == 'copyto':\n"
                        "    open(sys.argv[3], 'w').write('x')\n", encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return str(path)

    def seen(self):
        return json.loads(self.dump.read_text(encoding="utf-8"))

    def test_scrubbed_env_drops_every_secret_and_keeps_the_rest(self):
        environment = cache.scrubbed_env()
        self.assertFalse(set(SECRETS) & set(environment))
        self.assertFalse(set(S3) & set(environment))
        self.assertEqual(environment["MODSYNC_ENV_PROBE"], "kept")

    def test_keep_restores_only_the_named_prefixes(self):
        environment = cache.scrubbed_env(cache.S3_ENV)
        self.assertEqual({name: environment[name] for name in S3}, S3)
        self.assertFalse(set(SECRETS) & set(environment))

    def test_integrity_tool_child_does_not_see_secrets(self):
        archive = Path(self.directory.name, "sample.7z")
        archive.write_bytes(b"7z\xbc\xaf\x27\x1c" + b"\0" * 64)
        with mock.patch.object(cache.shutil, "which", lambda name: self.tool("7zz") if name == "7zz" else None):
            self.assertEqual(cache.validate(archive, 30), "7z")
        seen = self.seen()
        self.assertFalse((set(SECRETS) | set(S3)) & set(seen))
        self.assertEqual(seen["MODSYNC_ENV_PROBE"], "kept")

    def test_download_tool_child_does_not_see_secrets(self):
        item = {"provider": "mega", "url": "https://mega.nz/file/AAAAAAAA#BBBB"}
        args = types.SimpleNamespace(download_timeout=30, max_bytes=10 ** 9)
        with mock.patch.object(cache, "tool_command", lambda provider, directory, url: [self.tool("megadl")]), \
                self.assertRaises(ValueError):
            cache.fetch_with_tool(item, Path(self.directory.name), args)
        self.assertFalse((set(SECRETS) | set(S3)) & set(self.seen()))

    def test_rclone_child_keeps_only_the_s3_secrets(self):
        self.tool("rclone")
        args = types.SimpleNamespace(remote="MODCACHE:bucket", backend_timeout=30)
        with mock.patch.dict(os.environ, {"PATH": self.directory.name + os.pathsep + os.environ["PATH"]}):
            self.assertTrue(cache.remote_copy(args, "MODCACHE:bucket/x", Path(self.directory.name, "out")))
        seen = self.seen()
        self.assertEqual({name: seen[name] for name in S3}, S3)
        self.assertFalse(set(SECRETS) & set(seen))

    def test_p2p_helper_child_does_not_see_secrets_and_environ_is_restored(self):
        helper = Path(self.directory.name, "helper")
        helper.mkdir()
        (helper / "p2p_fetch.py").write_text(
            "import json, os, subprocess, sys\n"
            "def fetch(entry, dest):\n"
            "    subprocess.run([sys.executable, '-c', 'import json, os; "
            "json.dump(dict(os.environ), open(os.environ[\"MODSYNC_ENV_DUMP\"], \"w\"))'])\n"
            "    return True\n", encoding="utf-8")
        before = dict(os.environ)
        with mock.patch.object(cache, "__file__", str(helper / "cache.py")):
            self.assertTrue(cache.try_p2p({"sha256": "0" * 64, "p2p": {"magnet": "magnet:?x"}},
                                          Path(self.directory.name, "d")))
        self.assertFalse((set(SECRETS) | set(S3)) & set(self.seen()))
        self.assertEqual(dict(os.environ), before)


if __name__ == "__main__":
    unittest.main()
