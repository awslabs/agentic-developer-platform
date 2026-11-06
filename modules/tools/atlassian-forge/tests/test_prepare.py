"""Private artifacts cannot overwrite source or hide a credential in a callback."""

import importlib.util
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("forge_prepare", ROOT / "prepare.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class PreparationTests(unittest.TestCase):
    def test_registered_artifact_keeps_identity_and_no_configuration_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "app"
            module.prepare({"app_id": "ari:cloud:ecosystem::app/11111111-1111-1111-1111-111111111111", "adp_base_url": "https://adp.example.com", "api_token": "never-copy"}, output)
            manifest = (output / "manifest.yml").read_text()
            self.assertIn("https://adp.example.com", manifest)
            self.assertNotIn("never-copy", manifest)
            self.assertTrue((output / "static/main.js").is_file())
            self.assertEqual((output / "manifest.yml").stat().st_mode & 0o777, 0o600)
            with self.assertRaises(FileExistsError):
                module.prepare({"app_id": "ari:cloud:ecosystem::app/11111111-1111-1111-1111-111111111111", "adp_base_url": "https://adp.example.com"}, output)

    def test_registration_has_no_placeholder_app_and_cannot_replace_registered_app(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "app"
            module.prepare({"adp_base_url": "https://adp.example.com"}, output, for_registration=True)
            self.assertNotIn("  id:", (output / "manifest.yml").read_text())
            with self.assertRaises(ValueError):
                module.prepare({"app_id": "existing", "adp_base_url": "https://adp.example.com"}, Path(directory) / "other", for_registration=True)

    def test_callback_credentials_and_source_output_are_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            for url in ("http://adp.example.com", "https://secret@adp.example.com", "https://adp.example.com/path", "https://adp.example.com?secret=value"):
                with self.subTest(url=url), self.assertRaises(ValueError):
                    module.prepare({"adp_base_url": url}, Path(directory) / "app", for_registration=True)
            with self.assertRaises(ValueError):
                module.prepare({"adp_base_url": "https://adp.example.com"}, ROOT / "generated", for_registration=True)


if __name__ == "__main__":
    unittest.main()
