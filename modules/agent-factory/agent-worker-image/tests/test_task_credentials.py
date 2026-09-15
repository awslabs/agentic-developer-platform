"""Real SDK credential_process and customer profile precedence."""

import configparser
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import botocore.session
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from adp_cred.task_credentials import cmd_task_credentials, configure_task_credentials


def response():
    return {
        "Version": 1,
        "AccessKeyId": "SOURCE_TEST_KEY",
        "SecretAccessKey": "SOURCE_TEST_SECRET",
        "SessionToken": "SOURCE_TEST_SESSION",
        "Expiration": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
    }


def test_sdk_process_uses_source_credentials_without_replacing_customer_profiles(
    tmp_path, monkeypatch
):
    original = tmp_path / "original.config"
    original.write_text(
        "[profile customer-chain]\nrole_arn = arn:aws:iam::222222222222:role/customer\nsource_profile = default\nexternal_id = customer-external-id\n"
    )
    env = {
        "ADP_AGENT_AUTHORITY_ENABLED": "true",
        "AWS_ROLE_ARN": "arn:aws:iam::123456789012:role/authority-worker",
        "AWS_WEB_IDENTITY_TOKEN_FILE": "/projected/token",
        "AWS_REGION": "us-east-1",
        "AWS_CONFIG_FILE": str(original),
    }
    configure_task_credentials(env)
    generated = Path(env["AWS_CONFIG_FILE"])
    try:
        config = configparser.RawConfigParser()
        config.read(generated)
        assert config["default"]["credential_process"] == "adp-cred worker-session"
        assert config["profile customer-chain"]["external_id"] == "customer-external-id"
        assert env["ADP_WORKER_IRSA_ROLE_ARN"].endswith(":role/authority-worker")
        assert "AWS_ROLE_ARN" not in env and "AWS_WEB_IDENTITY_TOKEN_FILE" not in env
        assert generated.stat().st_mode & 0o777 == 0o600
        # Exercise the SDK's actual process provider using a disposable stand-in
        # for the broker CLI. The broker command's HTTP/output contract is below.
        command = tmp_path / "credential.py"
        command.write_text("import json\nprint(" + repr(json.dumps(response())) + ")\n")
        config["default"]["credential_process"] = f'"{sys.executable}" "{command}"'
        with generated.open("w") as stream:
            config.write(stream)
        for key in list(os.environ):
            if key.startswith("AWS_"):
                monkeypatch.delenv(key)
        monkeypatch.setenv("AWS_CONFIG_FILE", str(generated))
        monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "absent.credentials"))
        monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
        session = botocore.session.get_session()
        assert session.get_credentials().get_frozen_credentials().access_key == "SOURCE_TEST_KEY"
        assert (
            session.get_scoped_config()["credential_process"]
            == config["default"]["credential_process"]
        )
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "CUSTOMER_TEST_KEY")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "CUSTOMER_TEST_SECRET")
        assert botocore.session.get_session().get_credentials().access_key == "CUSTOMER_TEST_KEY"
    finally:
        generated.unlink()


def test_flag_off_preserves_environment_exactly():
    env = {
        "AWS_ROLE_ARN": "existing",
        "AWS_PROFILE": "customer",
        "AWS_CONFIG_FILE": "/existing/config",
    }
    before = dict(env)
    configure_task_credentials(env)
    assert env == before


def test_worker_session_outputs_only_sdk_credentials(monkeypatch, capsys):
    monkeypatch.setenv("ADP_AGENT_AUTHORITY_ENABLED", "true")
    monkeypatch.setenv("ADP_MESSAGE_ID", "run")
    value = response()
    with (
        patch(
            "adp_cred.task_credentials._get_config",
            return_value=("https://gateway.test", None, "user", "operations", "task", True),
        ),
        patch(
            "adp_cred.task_credentials._do_request",
            return_value={**value, "untrusted_extra": "discard"},
        ) as call,
    ):
        cmd_task_credentials()
    captured = capsys.readouterr()
    assert json.loads(captured.out) == value
    assert captured.err == ""
    assert call.call_args.args[-1] == {"user_id": "user", "invocation_id": "run"}


@pytest.mark.parametrize(
    "value",
    [
        {},
        {**response(), "Expiration": "2000-01-01T00:00:00Z"},
        {**response(), "SecretAccessKey": ""},
    ],
)
def test_worker_session_errors_never_print_credentials(monkeypatch, capsys, value):
    monkeypatch.setenv("ADP_AGENT_AUTHORITY_ENABLED", "true")
    monkeypatch.setenv("ADP_MESSAGE_ID", "run")
    with (
        patch(
            "adp_cred.task_credentials._get_config",
            return_value=("https://gateway.test", None, "user", "operations", "task", True),
        ),
        patch("adp_cred.task_credentials._do_request", return_value=value),
        pytest.raises(SystemExit),
    ):
        cmd_task_credentials()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "SOURCE_TEST" not in captured.err


@pytest.mark.parametrize("profile", ["default", "profile customer"])
def test_preconfigured_customer_credential_process_is_preserved(tmp_path, profile):
    original = tmp_path / "customer.config"
    original.write_text(
        f"[{profile}]\ncredential_process = customer-vault-helper\nregion = eu-west-1\n"
    )
    env = {
        "ADP_AGENT_AUTHORITY_ENABLED": "true",
        "AWS_CONFIG_FILE": str(original),
        "AWS_PROFILE": "customer",
    }
    path = configure_task_credentials(env)
    try:
        config = configparser.RawConfigParser()
        config.read(path)
        assert config[profile]["credential_process"] == "customer-vault-helper"
        assert config[profile]["region"] == "eu-west-1"
        assert env["AWS_PROFILE"] == "customer"
    finally:
        Path(path).unlink()


def test_real_sdk_refresh_and_customer_assume_role_options(tmp_path, monkeypatch):
    from botocore.stub import Stubber

    counter = tmp_path / "refresh-count"
    command = tmp_path / "source.py"
    command.write_text(
        "import json\nfrom pathlib import Path\nfrom datetime import datetime, timezone, timedelta\n"
        + f"p = Path({str(counter)!r})\n"
        + "n = int(p.read_text()) + 1 if p.exists() else 1\np.write_text(str(n))\n"
        + f"value = {response()!r}\n"
        + "value['AccessKeyId'] = 'SOURCE_TEST_KEY_' + str(n)\n"
        + "value['Expiration'] = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()\nprint(json.dumps(value))\n"
    )
    config = tmp_path / "config"
    config.write_text(
        f'[default]\ncredential_process = "{sys.executable}" "{command}"\nregion = eu-west-1\n'
    )
    for key in list(os.environ):
        if key.startswith("AWS_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("AWS_CONFIG_FILE", str(config))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "absent"))
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    sdk = botocore.session.get_session()
    credentials = sdk.get_credentials()
    first = credentials.get_frozen_credentials()
    assert credentials.get_frozen_credentials().access_key != first.access_key
    sts = sdk.create_client("sts")
    assert sts.meta.region_name == "eu-west-1"
    args = {
        "RoleArn": "arn:aws:iam::222222222222:role/customer",
        "RoleSessionName": "customer-deployment",
        "ExternalId": "customer-external-id",
        "SourceIdentity": "customer-user",
        "DurationSeconds": 3600,
        "Tags": [{"Key": "project", "Value": "test"}],
        "TransitiveTagKeys": ["project"],
    }
    result = {
        "Credentials": {
            "AccessKeyId": "CUSTOMER_TEST_KEY",
            "SecretAccessKey": "customer-secret",
            "SessionToken": "customer-token",
            "Expiration": datetime.now(timezone.utc) + timedelta(hours=1),
        }
    }
    with Stubber(sts) as stub:
        stub.add_response("assume_role", result, args)
        assert sts.assume_role(**args) == result
        stub.assert_no_pending_responses()
