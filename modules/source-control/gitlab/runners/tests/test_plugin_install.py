"""Exercise the connector install entry point without an AWS connection."""

import hashlib
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "bootstrap/install_plugin.py"


class PluginInstallTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "fleeting-plugin-aws"
        self.source.write_bytes(b"#!/bin/sh\nprintf 'synthetic connector\\n'\n")
        self.source.chmod(0o555)
        self.destination = self.root / "manager-plugin"
        self.destination.mkdir()
        self.digest = hashlib.sha256(self.source.read_bytes()).hexdigest()

    def run_installer(self, source=None, expected=None):
        return subprocess.run(
            ["python3", str(INSTALLER), str(source or self.source),
             str(self.destination), expected or self.digest],
            capture_output=True, text=True, check=False,
        )

    def test_verified_executable_is_discoverable_and_reusable(self):
        for _ in range(2):
            result = self.run_installer()
            self.assertEqual(result.returncode, 0, result.stderr)
        installed = self.destination / "fleeting-plugin-aws"
        self.assertEqual(installed.stat().st_mode & 0o777, 0o555)
        self.assertEqual(hashlib.sha256(installed.read_bytes()).hexdigest(), self.digest)
        result = subprocess.run([str(installed), "--version"], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "synthetic connector\n")

    def test_missing_altered_or_non_executable_source_denies_startup(self):
        for change in ("missing", "altered", "non-executable", "writable", "symlink"):
            with self.subTest(change=change):
                self.source.unlink(missing_ok=True)
                self.source.write_bytes(b"#!/bin/sh\nprintf 'synthetic connector\\n'\n")
                self.source.chmod(0o555)
                if change == "missing":
                    self.source.unlink()
                elif change == "altered":
                    self.source.chmod(0o600)
                    self.source.write_bytes(b"#!/bin/sh\nexit 0\n")
                    self.source.chmod(0o555)
                elif change == "non-executable":
                    self.source.chmod(0o444)
                elif change == "writable":
                    self.source.chmod(0o755)
                else:
                    self.source.rename(self.root / "real-connector")
                    self.source.symlink_to(self.root / "real-connector")
                result = self.run_installer()
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stderr.strip(), "plugin_not_verified")
                self.assertFalse((self.destination / "fleeting-plugin-aws").exists())

    def test_substituted_destination_and_invalid_digest_deny_startup(self):
        installed = self.destination / "fleeting-plugin-aws"
        installed.write_bytes(b"unreviewed")
        installed.chmod(0o555)
        result = self.run_installer()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(installed.read_bytes(), b"unreviewed")
        installed.unlink()
        self.assertNotEqual(self.run_installer(expected="0" * 64).returncode, 0)
        self.assertFalse(installed.exists())
        installed.symlink_to(self.source)
        self.assertNotEqual(self.run_installer().returncode, 0)
        self.assertTrue(installed.is_symlink())


if __name__ == "__main__":
    unittest.main()
