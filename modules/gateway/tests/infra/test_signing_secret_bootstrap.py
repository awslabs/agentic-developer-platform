"""Routine deployment must never rotate a previously created signing key."""

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/ensure-signing-secret.py"
spec = importlib.util.spec_from_file_location("signing_secret_bootstrap", SCRIPT)
bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)


@pytest.fixture
def commands(monkeypatch):
    responses = []
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        status, output = responses.pop(0)
        stderr = f"An error occurred ({output}) when calling the operation" if status else ""
        return subprocess.CompletedProcess(args, status, json.dumps(output) if not status else "", stderr)

    monkeypatch.setattr(bootstrap.subprocess, "run", run)
    monkeypatch.setattr(bootstrap.secrets, "token_hex", lambda _: "candidate-test-key")
    return responses, calls


def test_existing_key_is_reused(commands):
    responses, calls = commands
    responses.append((0, {"SecretString": "existing-test-key"}))
    assert bootstrap.ensure_secret("test-signing-key", "us-east-1") == "existing-test-key"
    assert len(calls) == 1


@pytest.mark.parametrize("code", ["AccessDeniedException", "ThrottlingException", "InternalServiceError", "CommandFailed"])
def test_failed_read_cannot_create_or_replace_a_key(commands, code):
    responses, calls = commands
    responses.append((1, code))
    with pytest.raises(bootstrap.SecretBootstrapError):
        bootstrap.ensure_secret("test-signing-key", "us-east-1")
    assert len(calls) == 1


def test_missing_key_is_created_without_secret_in_arguments(commands):
    responses, calls = commands
    responses.extend([(1, "ResourceNotFoundException"), (0, {"ARN": "test-arn"})])
    assert bootstrap.ensure_secret("test-signing-key", "eu-west-2") == "candidate-test-key"
    assert [args[2] for args, _ in calls] == ["get-secret-value", "create-secret"]
    for args, kwargs in calls:
        assert args[args.index("--region") + 1] == "eu-west-2"
        assert "candidate-test-key" not in " ".join(args)
        assert kwargs["capture_output"] is True
    assert json.loads(calls[1][1]["input"])["SecretString"] == "candidate-test-key"


def test_concurrent_creation_reuses_the_winners_key(commands):
    responses, calls = commands
    responses.extend(
        [
            (1, "ResourceNotFoundException"),
            (1, "ResourceExistsException"),
            (0, {"SecretString": "concurrent-test-key"}),
        ]
    )
    assert bootstrap.ensure_secret("test-signing-key", "us-east-1") == "concurrent-test-key"
    assert [args[2] for args, _ in calls] == ["get-secret-value", "create-secret", "get-secret-value"]


def test_creation_error_does_not_fall_back_to_rotation(commands):
    responses, calls = commands
    responses.extend([(1, "ResourceNotFoundException"), (1, "AccessDeniedException")])
    with pytest.raises(bootstrap.SecretBootstrapError):
        bootstrap.ensure_secret("test-signing-key", "us-east-1")
    assert [args[2] for args, _ in calls] == ["get-secret-value", "create-secret"]


@pytest.mark.parametrize("value", [None, "", "   ", 42])
def test_empty_or_invalid_existing_value_requires_operator_repair(commands, value):
    responses, calls = commands
    responses.append((0, {"SecretString": value}))
    with pytest.raises(bootstrap.SecretBootstrapError, match="MissingSecretString"):
        bootstrap.ensure_secret("test-signing-key", "us-east-1")
    assert len(calls) == 1


def test_deployment_paths_use_the_same_nonrotating_bootstrap():
    root = SCRIPT.parents[3]
    for path in [root / ".github/workflows/gateway-deploy.yml", root / "platform/scripts/deploy-all.sh"]:
        source = path.read_text()
        assignment = source.split("MAGIC_LINK_SECRET=$(", 1)[1].split("\n\n", 1)[0]
        assert "ensure-signing-secret.py" in assignment
        assert '--region "$AWS_REGION"' in assignment
        assert "put-secret-value" not in assignment
