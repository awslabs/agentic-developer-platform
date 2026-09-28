"""Routine deployment must never rotate a previously created signing key."""

import importlib.util
import json
import stat
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
        payload_path = Path(args[args.index("--cli-input-json") + 1].removeprefix("file://"))
        assert stat.S_IMODE(payload_path.stat().st_mode) == 0o600
        assert "input" not in kwargs
        calls.append((args, {**kwargs, "payload": json.loads(payload_path.read_text()), "payload_path": payload_path}))
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
    assert calls[1][1]["payload"]["SecretString"] == "candidate-test-key"
    assert all(not kwargs["payload_path"].exists() for _, kwargs in calls)


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


def test_operator_deployment_bootstraps_and_ci_only_reads_signing_secrets():
    root = SCRIPT.parents[3]
    operator = (root / "platform/scripts/deploy-all.sh").read_text()
    assignment = operator.split("MAGIC_LINK_SECRET=$(", 1)[1].split(")", 1)[0]
    assert "ensure-signing-secret.py" in assignment
    assert '--region "$AWS_REGION"' in assignment
    assert "put-secret-value" not in assignment

    workflow = (root / ".github/workflows/gateway-deploy.yml").read_text()
    for variable in ("TOKEN_SECRET", "INTERNAL_API_KEY", "MAGIC_LINK_SECRET"):
        retrieval = workflow.split(f"{variable}=$(", 1)[1].split(")", 1)[0]
        assert "aws secretsmanager get-secret-value" in retrieval
        assert "--query SecretString --output text" in retrieval
    for mutation in ("create-secret", "put-secret-value", "update-secret", "ensure-signing-secret.py"):
        assert mutation not in workflow


@pytest.mark.parametrize("error", [subprocess.TimeoutExpired("aws", 60), OSError("command unavailable")])
def test_private_payload_is_removed_when_command_raises(monkeypatch, error):
    paths = []

    def run(args, **kwargs):
        path = Path(args[args.index("--cli-input-json") + 1].removeprefix("file://"))
        paths.append(path)
        assert json.loads(path.read_text()) == {"SecretString": "test-key"}
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        raise error

    monkeypatch.setattr(bootstrap.subprocess, "run", run)
    with pytest.raises(type(error)):
        bootstrap.request("create-secret", "us-east-1", {"SecretString": "test-key"})
    assert len(paths) == 1 and not paths[0].exists()
