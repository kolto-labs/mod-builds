import io
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import zipfile
from unittest.mock import Mock
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import launcher


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = {"SOURCE_REPOSITORY": "example/private", "SOURCE_REVISION": "a" * 40,
                    "SOURCE_TOKEN": "synthetic-secret", "SOURCE_ENTRYPOINT": "entry.py",
                    "PATH": os.environ["PATH"], "RUNNER_TEMP": self.temp.name}

    def runner(self, fetch=0, execution=0):
        def run(args, **kwargs):
            if args[0] == "gh":
                if not fetch:
                    with zipfile.ZipFile(kwargs["stdout"], "w") as archive:
                        archive.writestr("snapshot/entry.py", "print('sensitive')")
                return Mock(returncode=fetch)
            self.assertNotIn("SOURCE_TOKEN", kwargs["env"])
            kwargs["stdout"].write(b"sensitive private output")
            return Mock(returncode=execution)
        return run

    def test_success_does_not_emit_plaintext(self):
        launcher.launch(self.env, self.runner())
        self.assertEqual(list(Path(self.temp.name).iterdir()), [])

    def test_invalid_and_missing_configuration(self):
        for key, value in (("SOURCE_TOKEN", ""), ("SOURCE_REVISION", "main"),
                           ("SOURCE_ENTRYPOINT", "../bad")):
            env = dict(self.env, **{key: value})
            with self.subTest(key=key), self.assertRaises(ValueError):
                launcher.launch(env, self.runner())

    def test_fetch_and_command_failures(self):
        for run in (self.runner(fetch=1), self.runner(execution=1)):
            with self.assertRaises(ValueError):
                launcher.launch(self.env, run)
            self.assertEqual(list(Path(self.temp.name).iterdir()), [])

    def test_wrong_revision_rejected_as_fetch_failure(self):
        launcher_env = dict(self.env, SOURCE_REVISION="b" * 40)
        with self.assertRaises(ValueError):
            launcher.launch(launcher_env, self.runner(fetch=1))


if __name__ == "__main__":
    unittest.main()
