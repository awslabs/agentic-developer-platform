"""Deployments must stop before provisioning when a selected stage needs boto3.

Helpers import boto3 only when their stage runs (upgrade-chat.py in the
agent-factory stage, gateway-role-policy-cutover.py in gateway updates), so a
host without it used to fail after earlier stages had been applied.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
DEPLOY_ALL = (ROOT / "platform/scripts/deploy-all.sh").read_text()
PREFLIGHT = (ROOT / "platform/scripts/preflight-check.sh").read_text()
BASH = shutil.which("bash")


class PythonRuntime:
    """A PATH holding only a python3 whose site-packages are disabled."""

    def __init__(self, with_boto3, with_python=True):
        self.with_boto3 = with_boto3
        self.with_python = with_python

    def __enter__(self):
        self.directory = tempfile.TemporaryDirectory()
        base = Path(self.directory.name)
        (base / "bin").mkdir()
        if self.with_boto3:
            (base / "site/boto3").mkdir(parents=True)
            (base / "site/boto3/__init__.py").write_text('__version__ = "1.99.0"\n')
        if self.with_python:
            wrapper = base / "bin/python3"
            wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" -S "$@"\n')
            wrapper.chmod(0o755)
        return dict(os.environ, PATH=str(base / "bin"), PYTHONPATH=str(base / "site"))

    def __exit__(self, *exc):
        self.directory.cleanup()


class SelectedStageModuleTests(unittest.TestCase):
    def gate(self, with_boto3, factory, gateway, update, destroy="false"):
        start = DEPLOY_ALL.index("# Helpers for the selected stages import boto3")
        end = DEPLOY_ALL.index(
            "# Pin fresh installs and updates to the source being deployed", start
        )
        script = (
            'set -euo pipefail\nfail() { echo "FAIL: $1" >&2; exit 1; }\n'
            f"DESTROY={destroy}; DEPLOY_FACTORY={factory}; DEPLOY_GATEWAY={gateway}; UPDATE_MODE={update}\n"
            + DEPLOY_ALL[start:end]
            + "echo CONTINUES\n"
        )
        with PythonRuntime(with_boto3) as env:
            return subprocess.run(
                [BASH, "-c", script],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

    def test_full_install_without_boto3_stops_before_provisioning(self):
        result = self.gate(
            with_boto3=False, factory="true", gateway="true", update="false"
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("FAIL: Python module boto3 is not installed", result.stderr)
        self.assertIn("python3 -m pip install boto3", result.stderr)
        self.assertNotIn("CONTINUES", result.stdout)

    def test_gateway_update_without_boto3_stops(self):
        result = self.gate(
            with_boto3=False, factory="false", gateway="true", update="true"
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("FAIL: Python module boto3 is not installed", result.stderr)

    def test_scopes_that_never_import_boto3_are_not_blocked(self):
        # Fresh gateway-only installs, non-gateway partial updates and teardown.
        for factory, gateway, update, destroy in (
            ("false", "true", "false", "false"),
            ("false", "false", "true", "false"),
            ("true", "true", "false", "true"),
        ):
            with self.subTest(
                factory=factory, gateway=gateway, update=update, destroy=destroy
            ):
                result = self.gate(False, factory, gateway, update, destroy)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("CONTINUES", result.stdout)

    def test_available_boto3_continues_for_every_scope(self):
        for factory, gateway, update in (
            ("true", "true", "false"),
            ("true", "true", "true"),
            ("false", "true", "true"),
        ):
            with self.subTest(factory=factory, gateway=gateway, update=update):
                result = self.gate(True, factory, gateway, update)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("CONTINUES", result.stdout)

    def test_gate_runs_after_scope_resolution_and_before_the_first_phase(self):
        gate = DEPLOY_ALL.index("# Helpers for the selected stages import boto3")
        self.assertLess(DEPLOY_ALL.rindex("resolve_deploy_scope", 0, gate), gate)
        self.assertLess(gate, DEPLOY_ALL.index("if deploy_phase_begin bootstrap; then"))

    def test_boto3_importers_are_the_ones_the_gate_names(self):
        for helper in (
            "platform/scripts/upgrade-chat.py",
            "modules/gateway/scripts/gateway-role-policy-cutover.py",
        ):
            self.assertIn("import boto3", (ROOT / helper).read_text(), helper)
            self.assertIn(Path(helper).name, DEPLOY_ALL)


class StandalonePreflightTests(unittest.TestCase):
    def check(self, with_boto3, with_python=True):
        start = PREFLIGHT.index("check_python_runtime() {")
        function = PREFLIGHT[start : PREFLIGHT.index("\n}\n", start) + 3]
        script = (
            "set -euo pipefail\nPASS=0; WARN=0; FAIL=0\n"
            'pass() { echo "PASS: $1"; PASS=$((PASS+1)); }\n'
            'warn() { echo "WARN: $1"; WARN=$((WARN+1)); }\n'
            'fail() { echo "FAIL: $1"; FAIL=$((FAIL+1)); }\n'
            + function
            + 'check_python_runtime\necho "FAIL_COUNT=$FAIL WARN_COUNT=$WARN"\n'
        )
        with PythonRuntime(with_boto3, with_python) as env:
            result = subprocess.run(
                [BASH, "-c", script],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_missing_boto3_is_reported_without_failing_preflight(self):
        output = self.check(with_boto3=False)
        self.assertIn("WARN: Python module boto3 not installed", output)
        self.assertIn("python3 -m pip install boto3", output)
        self.assertIn("FAIL_COUNT=0", output)

    def test_available_boto3_is_reported(self):
        self.assertIn("PASS: Python boto3: 1.99.0", self.check(with_boto3=True))

    def test_python_version_is_reported_and_older_versions_only_warn(self):
        output = self.check(with_boto3=True)
        version = f"{sys.version_info.major}.{sys.version_info.minor}"
        if sys.version_info >= (3, 12):
            self.assertIn(f"PASS: Python: {version}", output)
        else:
            self.assertIn(
                f"WARN: Python {version} is older than the documented requirement",
                output,
            )
        self.assertIn("FAIL_COUNT=0", output)

    def test_missing_python_fails(self):
        output = self.check(with_boto3=False, with_python=False)
        self.assertIn("FAIL: python3 not installed", output)
        self.assertIn("FAIL_COUNT=1", output)


if __name__ == "__main__":
    unittest.main()
