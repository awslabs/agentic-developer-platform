"""Preflight must report a missing boto3 before deployment starts.

Deploy helpers such as upgrade-chat.py import boto3 only when their stage runs, so
without this check a full installation fails late in the agent-factory stage.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "platform/scripts/preflight-check.sh"
BASH = shutil.which("bash")
HARNESS = """set -euo pipefail
PASS=0; WARN=0; FAIL=0
pass() { echo "PASS: $1"; PASS=$((PASS+1)); }
warn() { echo "WARN: $1"; WARN=$((WARN+1)); }
fail() { echo "FAIL: $1"; FAIL=$((FAIL+1)); }
"""


def check_function():
    source = SCRIPT.read_text()
    start = source.index("check_python_runtime() {")
    return source[start : source.index("\n}\n", start) + 3]


class PreflightPythonRuntimeTests(unittest.TestCase):
    def run_check(self, with_boto3, with_python=True):
        with tempfile.TemporaryDirectory() as directory:
            bin_dir = Path(directory) / "bin"
            bin_dir.mkdir()
            site = Path(directory) / "site"
            if with_boto3:
                (site / "boto3").mkdir(parents=True)
                (site / "boto3/__init__.py").write_text('__version__ = "1.99.0"\n')
            if with_python:
                # Disable site-packages so the result does not depend on the host.
                wrapper = bin_dir / "python3"
                wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" -S "$@"\n')
                wrapper.chmod(0o755)
            env = dict(os.environ, PATH=str(bin_dir), PYTHONPATH=str(site))
            script = (
                HARNESS
                + check_function()
                + 'check_python_runtime\necho "FAIL_COUNT=$FAIL"'
            )
            result = subprocess.run(
                [BASH, "-c", script],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            return result.stdout

    def test_missing_boto3_fails_with_install_hint(self):
        output = self.run_check(with_boto3=False)
        self.assertIn("FAIL: Python module boto3 not installed", output)
        self.assertIn("python3 -m pip install boto3", output)
        self.assertIn("FAIL_COUNT=1", output)

    def test_available_boto3_passes(self):
        output = self.run_check(with_boto3=True)
        self.assertIn("PASS: Python boto3: 1.99.0", output)
        self.assertIn("FAIL_COUNT=0", output)

    def test_missing_python_fails(self):
        output = self.run_check(with_boto3=False, with_python=False)
        self.assertIn("FAIL: python3 not installed", output)

    def test_python_version_is_reported(self):
        output = self.run_check(with_boto3=True)
        self.assertIn(
            f"Python: {sys.version_info.major}.{sys.version_info.minor}", output
        )


if __name__ == "__main__":
    unittest.main()
