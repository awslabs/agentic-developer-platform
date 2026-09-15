"""Pre-repository workload bootstrap, atomic refresh and refusal semantics."""

from __future__ import annotations

import json
import stat
from unittest.mock import MagicMock

import pytest

from adp_trigger import client as trigger_client
from lib.run_identity import (
    RunIdentityError,
    RunIdentitySession,
    bootstrap_run_identity,
    read_workload_token,
)


@pytest.mark.parametrize("source", ["envelope", "configuration"])
def test_claimed_work_cannot_run_on_legacy_worker(monkeypatch, source):
    monkeypatch.setenv("ADP_AGENT_AUTHORITY_ENABLED", "false")
    monkeypatch.setenv("ADP_WORK_CLAIMS_ENABLED", "true" if source == "configuration" else "false")
    with pytest.raises(RunIdentityError, match="Work ownership requires"):
        bootstrap_run_identity({"work_claim_required": source == "envelope"})


@pytest.fixture
def identity(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "ADP_AGENT_CONTROL_ENDPOINT",
        "https://gateway.execute-api.us-east-1.amazonaws.com/dev/internal/v1/agent",
    )
    monkeypatch.setenv("ADP_AGENT_AUTHORITY_ENABLED", "true")
    proof = tmp_path / "pod-token"
    proof.write_text("pod-token-one")
    monkeypatch.setenv("ADP_WORKLOAD_TOKEN_FILE", str(proof))
    envelope = {
        "message_id": "run-a",
        "tenant_id": "tenant",
        "persona": "developer",
        "source_ref": {"repo": "org/repo"},
    }
    session = RunIdentitySession(envelope=envelope, directory=tmp_path)
    yield session, proof
    session.close()


def reply(token="adpr1.first.signature", **changes):
    return {
        "credential": token,
        "invocation_id": "run-a",
        "attempt": 1,
        "credential_epoch": 1,
        **changes,
    }


def test_refresh_atomically_replaces_live_cli_file(identity, monkeypatch):
    session, _ = identity
    monkeypatch.setattr(session, "_request", lambda: reply())
    session.refresh()
    monkeypatch.setenv("ADP_RUN_CREDENTIAL_FILE", str(session.credential_path))
    old = session.credential_path.open()
    assert trigger_client.get_credential() == "adpr1.first.signature"
    monkeypatch.setattr(session, "_request", lambda: reply("adpr1.second.signature"))
    session.refresh()
    assert trigger_client.get_credential() == "adpr1.second.signature"
    assert old.read().strip() == "adpr1.first.signature"
    old.close()
    assert stat.S_IMODE(session.credential_path.stat().st_mode) == 0o600
    assert not list(session.credential_path.parent.glob("credential-*"))


@pytest.mark.parametrize(
    "changes",
    [{"invocation_id": "run-b"}, {"attempt": 2}, {"attempt": True}, {"credential": "bad token"}],
)
def test_bad_refresh_cannot_replace_live_identity(identity, monkeypatch, changes):
    session, _ = identity
    monkeypatch.setattr(session, "_request", lambda: reply())
    session.refresh()
    monkeypatch.setattr(session, "_request", lambda: reply(**changes))
    with pytest.raises(RunIdentityError):
        session.refresh()
    assert session.credential_path.read_text().strip() == "adpr1.first.signature"


def test_refresh_finishing_after_close_cannot_recreate_credential(identity, monkeypatch):
    session, _ = identity

    def late_reply():
        session.close()
        return reply()

    monkeypatch.setattr(session, "_request", late_reply)
    session.refresh()
    assert not session.credential_path.exists()


def test_workload_proof_is_reread_and_missing_proof_refuses(identity):
    _, proof = identity
    assert read_workload_token() == "pod-token-one"
    proof.write_text("pod-token-two")
    assert read_workload_token() == "pod-token-two"
    proof.unlink()
    with pytest.raises(RunIdentityError):
        read_workload_token()


@pytest.mark.parametrize("irsa", [False, True])
def test_request_signs_workload_proof_and_binds_full_envelope(identity, monkeypatch, irsa):
    session, _ = identity
    credentials = MagicMock()
    credentials.access_key = "test-access"
    credentials.secret_key = "test-secret"
    credentials.token = None
    sdk_session = MagicMock()
    sdk_session.get_credentials.return_value.get_frozen_credentials.return_value = credentials
    if irsa:
        monkeypatch.setenv("AWS_ROLE_ARN", "arn:aws:iam::123456789012:role/authority-worker")
        provider = sdk_session.get_component.return_value.get_provider.return_value
        provider.load.return_value.get_frozen_credentials.return_value = credentials
    else:
        monkeypatch.delenv("AWS_ROLE_ARN", raising=False)
    monkeypatch.setattr("lib.run_identity.botocore.session.get_session", lambda: sdk_session)
    response = MagicMock()
    response.status_code = 200
    response.raw.read.return_value = json.dumps(reply()).encode()
    response.__enter__.return_value = response
    http = MagicMock()
    http.__enter__.return_value = http
    http.post.return_value = response
    monkeypatch.setattr("lib.run_identity.requests.Session", lambda: http)
    assert session._request()["invocation_id"] == "run-a"
    if irsa:
        sdk_session.get_credentials.assert_not_called()
    args, kwargs = http.post.call_args
    assert args[0].endswith("/internal/v1/agent/bootstrap")
    assert kwargs["headers"]["X-Adp-Workload-Token"] == "pod-token-one"
    assert "x-adp-workload-token" in kwargs["headers"]["Authorization"]
    assert kwargs["allow_redirects"] is False
    assert http.trust_env is False
    assert json.loads(kwargs["data"]) == {
        "invocation_id": "run-a",
        "envelope_digest": session._digest,
    }


def test_start_refuses_before_launching_background_thread(identity, monkeypatch):
    session, _ = identity

    def refused():
        raise RunIdentityError("refused")

    monkeypatch.setattr(session, "_request", refused)
    with pytest.raises(RunIdentityError):
        session.start()
    assert session._thread is None
    assert not session.credential_path.exists()


def test_disabled_mode_preserves_existing_startup(monkeypatch):
    monkeypatch.delenv("ADP_AGENT_AUTHORITY_ENABLED", raising=False)
    assert bootstrap_run_identity({}) is None


def test_refresh_failure_keeps_existing_expiry_and_does_not_log_credentials(
    identity, monkeypatch, caplog
):
    session, _ = identity
    monkeypatch.setattr(session, "_request", lambda: reply())
    session.refresh()
    before = session.credential_path.read_bytes()

    def unavailable():
        raise RuntimeError("sensitive-token-must-not-be-logged")

    monkeypatch.setattr(session, "refresh", unavailable)
    calls = iter([False, True])
    monkeypatch.setattr(session._stop, "wait", lambda _: next(calls))
    session._renew()
    assert session.credential_path.read_bytes() == before
    assert "sensitive-token" not in caplog.text


def test_authorized_child_waits_without_credential_before_start(identity, monkeypatch):
    from lib.run_identity import WorkOwnershipPending

    session, _ = identity
    requests = iter([WorkOwnershipPending("waiting"), reply()])

    def request():
        response = next(requests)
        if isinstance(response, Exception):
            assert not session.credential_path.exists()
            raise response
        return response

    monkeypatch.setattr(session, "_request", request)
    monkeypatch.setattr(session._stop, "wait", lambda _: False)
    monkeypatch.setattr("lib.run_identity.threading.Thread", MagicMock())
    session.start()
    assert session.credential_path.read_text().strip() == "adpr1.first.signature"


def test_pending_child_startup_has_bounded_wait(identity, monkeypatch):
    from lib.run_identity import WorkOwnershipPending

    session, _ = identity
    monkeypatch.setattr(session, "_request", MagicMock(side_effect=WorkOwnershipPending("waiting")))
    monkeypatch.setattr("lib.run_identity.time.monotonic", MagicMock(side_effect=[0, 1801]))
    with pytest.raises(RunIdentityError, match="startup deadline exceeded"):
        session.start()
    assert not session.credential_path.exists()
