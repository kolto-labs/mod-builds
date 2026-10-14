import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import bootstrap_age


class BootstrapTests(unittest.TestCase):
    def test_modified_download_never_installs_binary(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "age"
            with patch("urllib.request.urlopen", return_value=io.BytesIO(b"modified download")):
                with self.assertRaisesRegex(ValueError, "checksum"):
                    bootstrap_age.install(target, "Linux", "x86_64")
            self.assertFalse(target.exists())

    def test_unsupported_architecture_never_downloads(self):
        with tempfile.TemporaryDirectory() as temporary, patch("urllib.request.urlopen") as network:
            with self.assertRaisesRegex(ValueError, "Unsupported"):
                bootstrap_age.install(Path(temporary), "Windows", "arm64")
            network.assert_not_called()


if __name__ == "__main__":
    unittest.main()
