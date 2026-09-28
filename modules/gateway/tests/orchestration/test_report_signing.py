"""Secret hydration is independent of protected authority and never logs values."""

from unittest.mock import MagicMock

from src.orchestration.report_signing import signing_key


def test_tick_hydrates_same_key_when_authority_disabled(monkeypatch):
    monkeypatch.setenv("AGENT_AUTHORITY_ENABLED", "false")
    monkeypatch.delenv("AGENT_RUN_CREDENTIAL_KEY", raising=False)
    monkeypatch.setenv("AGENT_RUN_CREDENTIAL_KEY_PARAMETER", "/adp/test/gateway/run-report-signing-key")
    ssm = MagicMock()
    ssm.get_parameter.return_value = {"Parameter": {"Value": "a" * 64}}
    monkeypatch.setattr("boto3.client", lambda *args, **kwargs: ssm)
    assert signing_key() == "a" * 64
    assert signing_key() == "a" * 64
    ssm.get_parameter.assert_called_once_with(Name="/adp/test/gateway/run-report-signing-key", WithDecryption=True)


def test_failed_secret_read_never_uses_partial_or_exception_material(monkeypatch, caplog):
    monkeypatch.delenv("AGENT_RUN_CREDENTIAL_KEY", raising=False)
    monkeypatch.setenv("AGENT_RUN_CREDENTIAL_KEY_PARAMETER", "/configured/key")
    ssm = MagicMock()
    ssm.get_parameter.side_effect = RuntimeError("sensitive SDK response")
    monkeypatch.setattr("boto3.client", lambda *args, **kwargs: ssm)
    assert signing_key() == ""
    assert "sensitive SDK response" not in caplog.text
