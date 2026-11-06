"""Ingestion deployment reports the failed prerequisite without guessing it."""

import os
from pathlib import Path
import subprocess

import pytest


COMMON = Path(__file__).resolve().parents[1] / "_common.sh"
ROLE = "arn:aws:iam::123456789012:role/ingestion-receiver-example"


def resolve(tmp_path, *, output=ROLE, error=False, selected=""):
    calls = tmp_path / "aws-calls"
    result = subprocess.run(
        ["bash", "-c", '''
set -euo pipefail
source "$1"
aws() {
  printf '%s\\n' "$*" > "$CALLS"
  printf '%s\\n' "$AWS_RESPONSE"
  return "$AWS_STATUS"
}
resolve_ingestion_authority_config
printf '%s\\n' "$INGESTION_RECEIVER_ROLE_ARN" "$INGESTION_CONTROL_URL" "$INGESTION_REFRESH_URL"
''', "test", str(COMMON)],
        env={"PATH": os.defpath, "CALLS": str(calls), "AWS_RESPONSE": output,
             "AWS_STATUS": "1" if error else "0", "ENVIRONMENT": "test", "AWS_REGION": "eu-west-1",
             "INGESTION_RECEIVER_ROLE_ARN": selected},
        capture_output=True, text=True, timeout=10,
    )
    return result, calls.read_text() if calls.exists() else ""


def test_exact_parameter_read_preserves_valid_configuration(tmp_path):
    result, calls = resolve(tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[0] == ROLE
    assert "/internal/v1/knowledge-assets/ingestion-control" in result.stdout
    assert "/internal/v1/knowledge-assets/refresh" in result.stdout
    assert "--name /adp/test/agent-context/ingestion-receiver-role-arn" in calls
    assert "--region eu-west-1" in calls


@pytest.mark.parametrize("code", ["AccessDeniedException", "AccessDenied"])
def test_access_denial_cannot_claim_infrastructure_is_missing(tmp_path, code):
    result, _ = resolve(tmp_path, output=f"An error occurred ({code}) when calling GetParameter", error=True)
    assert result.returncode != 0 and result.stdout == ""
    assert "Grant the deployment identity ssm:GetParameter for this exact parameter" in result.stderr
    assert "/adp/test/agent-context/ingestion-receiver-role-arn" in result.stderr
    assert "infrastructure presence is unknown" in result.stderr
    assert "Apply" not in result.stderr


def test_missing_parameter_reports_ingestion_prerequisite(tmp_path):
    result, _ = resolve(tmp_path, output="An error occurred (ParameterNotFound) when calling GetParameter", error=True)
    assert result.returncode != 0 and result.stdout == ""
    assert "was not found" in result.stderr
    assert "Apply the reviewed ingestion isolation infrastructure before deploying ingestion workers" in result.stderr


@pytest.mark.parametrize("output", ["An error occurred (ExpiredTokenException)", "Could not connect to endpoint URL"])
def test_unavailable_read_does_not_guess_missing_infrastructure(tmp_path, output):
    result, _ = resolve(tmp_path, output=output, error=True)
    assert result.returncode != 0 and result.stdout == ""
    assert "Check AWS credentials and SSM availability" in result.stderr
    assert "infrastructure presence is unknown" in result.stderr and "Apply" not in result.stderr


def test_invalid_parameter_is_not_accepted_as_authority(tmp_path):
    result, _ = resolve(tmp_path, output="None")
    assert result.returncode != 0 and "missing or invalid" in result.stderr


def test_explicit_role_does_not_trigger_parameter_lookup(tmp_path):
    result, calls = resolve(tmp_path, selected=ROLE)
    assert result.returncode == 0 and result.stdout.splitlines()[0] == ROLE
    assert calls == ""
