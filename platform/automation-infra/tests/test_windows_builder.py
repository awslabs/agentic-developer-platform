"""Exercise the registration wait without AWS access or real host mutation."""
import os
from pathlib import Path
import subprocess

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize("status,expected", [("Success", 0), ("Pending", 1), ("Failed", 1)])
def test_registration_requires_observed_success(tmp_path, status, expected):
    action = yaml.safe_load((ROOT / "modules/domain-apps/cyber/ci/cyber-windows-image-build/action.yml").read_text())
    script = next(step["run"] for step in action["runs"]["steps"] if step["name"] == "Register VM on CAPE host")
    script = script.replace("${{ env.CAPE_HOST_ID }}", "i-0123456789abcdef0")
    aws = tmp_path / "aws"
    aws.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$CALL_LOG"\ncase "$*" in\n *send-command*) echo test-command ;;\n *) echo "$TEST_STATUS" ;;\nesac\n')
    aws.chmod(0o755)
    sleep = tmp_path / "sleep"
    sleep.write_text("#!/bin/sh\nexit 0\n")
    sleep.chmod(0o755)
    log = tmp_path / "calls"
    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", script],
        env=dict(os.environ, PATH=f"{tmp_path}:{os.environ['PATH']}", CALL_LOG=str(log), TEST_STATUS=status, ENVIRONMENT="test", AWS_REGION="us-east-1"),
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == expected, result.stderr
    calls = log.read_text()
    assert "--document-name adp-test-register-cape-windows" in calls
    assert "AWS-RunShellScript" not in calls
    if status == "Pending":
        assert "did not complete before the deadline" in result.stdout
