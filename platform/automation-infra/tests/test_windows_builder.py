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


def test_terminated_builder_fails_without_waiting_for_image_deadline(tmp_path):
    action = yaml.safe_load((ROOT / "modules/domain-apps/cyber/ci/cyber-windows-image-build/action.yml").read_text())
    script = next(step["run"] for step in action["runs"]["steps"] if step["name"] == "Wait for build output in S3")
    script = script.replace("${{ env.ASSETS_BUCKET }}", "test-assets")
    aws = tmp_path / "aws"
    aws.write_text('#!/bin/sh\ncase "$*" in\n *describe-instances*) echo terminated ;;\n *) exit 0 ;;\nesac\n')
    aws.chmod(0o755)
    sleep = tmp_path / "sleep"
    sleep.write_text('#!/bin/sh\ntouch "$SLEPT"\nexit 1\n')
    sleep.chmod(0o755)
    slept = tmp_path / "slept"
    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", script],
        env=dict(os.environ, PATH=f"{tmp_path}:{os.environ['PATH']}", SLEPT=str(slept), AWS_REGION="us-east-1", BUILDER_INSTANCE_ID="i-0123456789abcdef0"),
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 1
    assert "terminated before publishing its image" in result.stdout
    assert not slept.exists()


@pytest.mark.parametrize("build_exit", [0, 17])
def test_userdata_reports_real_pipeline_exit(tmp_path, build_exit):
    source = (ROOT / "modules/domain-apps/cyber/image-builder/builder-host.tf").read_text()
    script = source.split('    bash "$WORKDIR/build-pipeline.sh"', 1)[1].split("  USERDATA", 1)[0]
    script = 'bash "$WORKDIR/build-pipeline.sh"' + script
    script = script.replace("$${PIPESTATUS[0]}", "${PIPESTATUS[0]}")
    script = script.replace("/var/log/build-pipeline.log", str(tmp_path / "build.log"))
    (tmp_path / "build-pipeline.sh").write_text(f"echo build-result\nexit {build_exit}\n")
    aws = tmp_path / "aws"
    aws.write_text('#!/bin/sh\ncat > "$FAILURE_LOG"\n')
    aws.chmod(0o755)
    failure = tmp_path / "failure"
    result = subprocess.run(
        ["bash", "-c", script],
        env=dict(os.environ, PATH=f"{tmp_path}:{os.environ['PATH']}", WORKDIR=str(tmp_path),
                 FAILURE_LOG=str(failure), ASSETS_BUCKET="test-assets", AWS_REGION="us-east-1", BUILD_DATE="2026-09-27"),
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == build_exit
    assert failure.exists() is (build_exit != 0)
    if failure.exists():
        assert "build-result" in failure.read_text()
