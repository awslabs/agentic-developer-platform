"""Actions client checks source before checkout and never uses ambient AWS access."""

import importlib.util
import json
from pathlib import Path
import subprocess

import pytest

spec = importlib.util.spec_from_file_location(
    "deployment_cli", Path(__file__).parents[1] / "adp_cred" / "deployment.py"
)
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)


@pytest.fixture
def environment(tmp_path, monkeypatch):
    event = tmp_path / "event.json"
    event.write_text(
        json.dumps(
            {
                "inputs": {
                    "adp_correlation": "c" * 64,
                    "adp_source_revision": "a" * 40,
                    "adp_definition_revision": "b" * 40,
                    "customer_aws_label": "false",
                    "crew_controls_only": "true",
                }
            }
        )
    )
    env = tmp_path / "env"
    for key, value in {
        "GITHUB_EVENT_PATH": str(event),
        "GITHUB_ENV": str(env),
        "RUNNER_TEMP": str(tmp_path),
        "ADP_DEPLOYMENT_GATEWAY_URL": "https://gateway.test",
        "EKS_CLUSTER": "cluster",
        "NAMESPACE": "namespace",
        "ACTIONS_ID_TOKEN_REQUEST_URL": "https://oidc.test/token?run=1",
        "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "private-token",
    }.items():
        monkeypatch.setenv(key, value)
    return env


def test_exchange_uses_distinct_audience_and_preserves_nonboolean_labels(environment, monkeypatch):
    calls = []

    def read(url, **kwargs):
        calls.append((url, kwargs))
        return {"value": "signed-token"} if len(calls) == 1 else {"ok": True}

    monkeypatch.setattr(cli, "read_json", read)
    assert cli.exchange("context") == {"ok": True}
    assert calls[0][0].endswith("&audience=adp-deployment-credentials")
    assert calls[1][1]["body"]["inputs"] == {
        "customer_aws_label": "false",
        "crew_controls_only": "True",
    }
    assert calls[1][1]["token"] == "signed-token"


def test_configure_scopes_identity_before_export_and_has_no_secrets(
    environment, monkeypatch, tmp_path, capsys
):
    monkeypatch.setattr(
        cli,
        "exchange",
        lambda _: {
            "target": {
                "account_id": "123456789012",
                "region": "us-east-1",
                "resource_kind": "eks-namespace",
                "resource_id": "cluster/namespace",
            }
        },
    )
    for key in cli.CLEARED:
        monkeypatch.setenv(key, "ambient-secret")

    def identity(command, *, env, timeout):
        assert all(key not in env for key in cli.CLEARED)
        assert env["AWS_PROFILE"] == "adp-deployment"
        assert env["AWS_EC2_METADATA_DISABLED"] == "true"
        assert "credential-process" in Path(env["AWS_CONFIG_FILE"]).read_text()
        return json.dumps({"Account": "123456789012"}).encode()

    monkeypatch.setattr(subprocess, "check_output", identity)
    cli.configure()
    assert "AWS_WEB_IDENTITY_TOKEN_FILE=\n" in environment.read_text()
    assert "ambient-secret" not in environment.read_text() + capsys.readouterr().out
    assert (tmp_path / "adp-deployment-credentials/config").stat().st_mode & 0o777 == 0o600


def test_wrong_target_fails_before_aws_or_exports(environment, monkeypatch):
    monkeypatch.setattr(
        cli,
        "exchange",
        lambda _: {
            "target": {
                "account_id": "123456789012",
                "region": "us-east-1",
                "resource_kind": "eks-namespace",
                "resource_id": "other/namespace",
            }
        },
    )
    monkeypatch.setattr(
        subprocess, "check_output", lambda *a, **kw: pytest.fail("AWS must not be called")
    )
    with pytest.raises(ValueError):
        cli.configure()
    assert not environment.exists()


def test_errors_do_not_print_credential_response(environment, monkeypatch, capsys):
    def fail(_):
        raise ValueError("private-credential-response")

    monkeypatch.setattr(cli, "exchange", fail)
    with pytest.raises(SystemExit):
        cli.main(["credential-process"])
    captured = capsys.readouterr()
    assert captured.out == "" and "private-credential-response" not in captured.err
