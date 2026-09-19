"""Pre-repository workload bootstrap, atomic refresh and refusal semantics."""

from __future__ import annotations

import base64
import hashlib
import json
import stat
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from adp_trigger import client as trigger_client
from lib.run_identity import (
    ModelPolicyReport,
    ModelPolicyVerificationError,
    RunIdentityError,
    RunIdentitySession,
    bootstrap_run_identity,
    parse_model_policy_report,
    read_workload_token,
)


POLICY_PRIVATE_KEY = Ed25519PrivateKey.generate()
POLICY_PUBLIC_PEM = (
    POLICY_PRIVATE_KEY.public_key()
    .public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    .decode("ascii")
)
POLICY_KEYS = {"policy-key": POLICY_PRIVATE_KEY.public_key()}


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
    monkeypatch.setenv(
        "ADP_CONTROL_ENVELOPE_KEYS",
        json.dumps({"policy-key": POLICY_PUBLIC_PEM}),
    )
    envelope = {
        "message_id": "run-a",
        "tenant_id": "tenant",
        "persona": "developer",
        "correlation": {"correlation_id": "chain-a"},
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


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def policy_reply(*, now=None, envelope_changes=None, **decision_changes):
    issued = (now or datetime.now(timezone.utc)).replace(microsecond=0)
    decision = {
        "schema_version": 1,
        "tenant_id": "tenant",
        "invocation_id": "run-a",
        "correlation_id": "chain-a",
        "persona": "developer",
        "compatibility_class": "claude-agent-sdk",
        "harness_contract_revision": "0.3.220",
        "runtime_posture": "report_only",
        "posture_revision": 7,
        "requested_model_id": "sonnet46",
        "resolved_model_id": "global.anthropic.claude-sonnet-4-6",
        "resolution_source": "explicit-direct",
        "snapshot_digest": "a" * 64,
        "policy_revision": "policy-7",
        "catalogue_revision": "catalogue-4",
        "snapshot_allowlist_policy_revision": "allowlist-snapshot-3",
        "live_allowlist_policy_revision": "allowlist-live-4",
        "allowlist_policy_drift": True,
        **decision_changes,
    }
    body = json.dumps(decision, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    envelope = {
        "v": "adpe1",
        "iss": "adp-gateway-control",
        "aud": "adp-agent-model-policy",
        "alg": "ed25519",
        "kid": "policy-key",
        "tenant_id": "tenant",
        "principal": "run-a#1",
        "target_run_id": "run-a",
        "target_generation": 1,
        "action": "resolve_model",
        "command_id": decision["snapshot_digest"],
        "body_digest": hashlib.sha256(body).hexdigest(),
        "grant_id": "grant-a",
        "revocation_epoch": 1,
        "chain_id": "chain-a",
        "iat": issued.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "nbf": issued.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "exp": (issued + timedelta(seconds=30)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    envelope.update(envelope_changes or {})
    encoded = json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode()
    signature = POLICY_PRIVATE_KEY.sign(b"adpe1." + encoded)
    return {
        "posture": "report_only",
        "status": "proposed",
        "decision": decision,
        "assertion": f"adpe1.{_b64(encoded)}.{_b64(signature)}",
    }


def test_report_only_policy_is_sanitized_and_compared_without_selecting_model():
    report = parse_model_policy_report(
        policy_reply(),
        invocation_id="run-a",
        tenant_id="tenant",
        correlation_id="chain-a",
        public_keys=POLICY_KEYS,
    )

    assert report == ModelPolicyReport(
        status="proposed",
        requested_model_id="sonnet46",
        resolved_model_id="global.anthropic.claude-sonnet-4-6",
        resolution_source="explicit-direct",
        snapshot_digest="a" * 64,
        policy_revision="policy-7",
        catalogue_revision="catalogue-4",
        snapshot_allowlist_policy_revision="allowlist-snapshot-3",
        live_allowlist_policy_revision="allowlist-live-4",
        allowlist_policy_drift=True,
        posture_revision=7,
        assertion_key_id="policy-key",
    )
    evidence = report.environment("global.anthropic.claude-opus-5")
    assert evidence["ADP_MODEL_POLICY_PROPOSED_MODEL"] == "global.anthropic.claude-sonnet-4-6"
    assert evidence["ADP_MODEL_POLICY_LEGACY_MODEL"] == "global.anthropic.claude-opus-5"
    assert evidence["ADP_MODEL_POLICY_MATCH"] == "false"
    assert evidence["ADP_MODEL_POLICY_SNAPSHOT_ALLOWLIST_REVISION"] == "allowlist-snapshot-3"
    assert evidence["ADP_MODEL_POLICY_LIVE_ALLOWLIST_REVISION"] == "allowlist-live-4"
    assert evidence["ADP_MODEL_POLICY_ALLOWLIST_DRIFT"] == "true"
    assert "ANTHROPIC_MODEL" not in evidence


@pytest.mark.parametrize(
    "policy",
    [
        policy_reply(invocation_id="another-run"),
        policy_reply(runtime_posture="enforcing"),
        policy_reply(resolution_source="invented"),
        policy_reply(snapshot_digest="not-a-digest"),
        policy_reply(resolved_model_id="bad\nlog"),
    ],
)
def test_invalid_policy_report_never_becomes_worker_environment(policy):
    with pytest.raises((ValueError, ModelPolicyVerificationError)):
        parse_model_policy_report(
            policy,
            invocation_id="run-a",
            tenant_id="tenant",
            correlation_id="chain-a",
            public_keys=POLICY_KEYS,
        )


def test_unavailable_report_exposes_bounded_reason_only():
    report = parse_model_policy_report(
        {"posture": "report_only", "status": "unavailable", "reason": "snapshot_expired"},
        invocation_id="run-a",
    )
    assert report.environment("legacy") == {
        "ADP_MODEL_POLICY_POSTURE": "report_only",
        "ADP_MODEL_POLICY_STATUS": "unavailable",
        "ADP_MODEL_POLICY_REASON": "snapshot_expired",
    }


def test_unavailable_report_preserves_allowlist_drift_evidence():
    report = parse_model_policy_report(
        {
            "posture": "report_only",
            "status": "unavailable",
            "reason": "not_permitted",
            "evidence": {
                "snapshot_allowlist_policy_revision": "allowlist-snapshot-3",
                "live_allowlist_policy_revision": "allowlist-live-4",
                "allowlist_policy_drift": True,
            },
        },
        invocation_id="run-a",
    )
    assert report.environment("legacy") == {
        "ADP_MODEL_POLICY_POSTURE": "report_only",
        "ADP_MODEL_POLICY_STATUS": "unavailable",
        "ADP_MODEL_POLICY_REASON": "not_permitted",
        "ADP_MODEL_POLICY_SNAPSHOT_ALLOWLIST_REVISION": "allowlist-snapshot-3",
        "ADP_MODEL_POLICY_LIVE_ALLOWLIST_REVISION": "allowlist-live-4",
        "ADP_MODEL_POLICY_ALLOWLIST_DRIFT": "true",
    }


@pytest.mark.parametrize(
    ("reply_value", "reason"),
    [
        (
            lambda now: policy_reply(now=now, envelope_changes={"alg": "none"}),
            "decision_algorithm_unsupported",
        ),
        (
            lambda now: policy_reply(now=now, envelope_changes={"tenant_id": "tenant-b"}),
            "decision_cross_tenant",
        ),
        (
            lambda now: policy_reply(now=now, envelope_changes={"chain_id": "chain-b"}),
            "decision_chain_mismatch",
        ),
        (
            lambda now: policy_reply(now=now, envelope_changes={"command_id": "b" * 64}),
            "decision_snapshot_mismatch",
        ),
        (
            lambda now: policy_reply(
                now=now - timedelta(minutes=1),
            ),
            "decision_expired",
        ),
    ],
)
def test_signed_decision_refuses_adversarial_bindings(reply_value, reason):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    with pytest.raises(ModelPolicyVerificationError, match=reason):
        parse_model_policy_report(
            reply_value(now),
            invocation_id="run-a",
            tenant_id="tenant",
            correlation_id="chain-a",
            public_keys=POLICY_KEYS,
            now=now,
        )


def test_signed_assertion_covers_the_exact_decision_bytes():
    response = policy_reply()
    response["decision"]["resolved_model_id"] = "global.anthropic.claude-opus-5"
    with pytest.raises(ModelPolicyVerificationError, match="decision_altered"):
        parse_model_policy_report(
            response,
            invocation_id="run-a",
            tenant_id="tenant",
            correlation_id="chain-a",
            public_keys=POLICY_KEYS,
        )


def test_unsupported_policy_revision_is_reported_and_never_blocks_legacy_identity(
    identity,
    monkeypatch,
):
    session, _ = identity
    unsupported = policy_reply(schema_version=2)
    monkeypatch.setattr(
        session,
        "_request",
        lambda: reply(model_policy=unsupported),
    )

    session.refresh()

    assert session.credential_path.read_text().strip() == "adpr1.first.signature"
    assert session.model_policy_report == ModelPolicyReport(
        status="unavailable",
        reason="snapshot_unsupported_revision",
    )


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


def test_refresh_retains_first_report_only_proposal_as_immutable_comparison(identity, monkeypatch):
    session, _ = identity
    monkeypatch.setattr(session, "_request", lambda: reply(model_policy=policy_reply()))
    session.refresh()
    assert session.model_policy_report is not None
    assert session.model_policy_report.resolved_model_id == "global.anthropic.claude-sonnet-4-6"

    changed = policy_reply(resolved_model_id="global.anthropic.claude-opus-5")
    monkeypatch.setattr(session, "_request", lambda: reply(model_policy=changed))
    session.refresh()
    assert session.model_policy_report.resolved_model_id == "global.anthropic.claude-sonnet-4-6"


def test_refresh_can_observe_policy_after_old_gateway_response(identity, monkeypatch):
    session, _ = identity
    replies = iter([reply(), reply(model_policy=policy_reply())])
    monkeypatch.setattr(session, "_request", lambda: next(replies))

    session.refresh()
    assert session.model_policy_report is None
    session.refresh()
    assert session.model_policy_report is not None
    assert session.model_policy_report.resolved_model_id == "global.anthropic.claude-sonnet-4-6"


def test_refresh_retries_transient_unverifiable_report_before_pinning(identity, monkeypatch):
    session, _ = identity
    invalid = policy_reply()
    invalid["assertion"] = "adpe1.invalid.signature"
    replies = iter(
        [
            reply(model_policy=invalid),
            reply(model_policy=policy_reply()),
        ]
    )
    monkeypatch.setattr(session, "_request", lambda: next(replies))

    session.refresh()
    assert session.model_policy_report is not None
    assert session.model_policy_report.status == "unavailable"
    session.refresh()
    assert session.model_policy_report is not None
    assert session.model_policy_report.status == "proposed"


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


@pytest.mark.parametrize("preserved", [False, True])
def test_request_signs_workload_proof_and_binds_full_envelope(identity, monkeypatch, preserved):
    session, _ = identity
    credentials = MagicMock()
    credentials.access_key = "test-access"
    credentials.secret_key = "test-secret"
    credentials.token = None
    sdk_session = MagicMock()
    sdk_session.get_credentials.return_value.get_frozen_credentials.return_value = credentials
    for key in (
        "AWS_ROLE_ARN",
        "AWS_WEB_IDENTITY_TOKEN_FILE",
        "ADP_WORKER_IRSA_ROLE_ARN",
        "ADP_WORKER_IRSA_TOKEN_FILE",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv(
        "ADP_WORKER_IRSA_ROLE_ARN" if preserved else "AWS_ROLE_ARN",
        "arn:aws:iam::123456789012:role/authority-worker",
    )
    irsa_token = session._directory / "irsa-token"
    irsa_token.write_text("worker-web-identity")
    monkeypatch.setenv(
        "ADP_WORKER_IRSA_TOKEN_FILE" if preserved else "AWS_WEB_IDENTITY_TOKEN_FILE",
        str(irsa_token),
    )
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    sdk_session.create_client.return_value.assume_role_with_web_identity.return_value = {
        "Credentials": {
            "AccessKeyId": "test-access",
            "SecretAccessKey": "test-secret",
            "SessionToken": "test-session",
            "Expiration": datetime.now(timezone.utc) + timedelta(hours=1),
        }
    }
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
    sdk_session.get_credentials.assert_not_called()
    assert (
        sdk_session.create_client.return_value.assume_role_with_web_identity.call_args.kwargs[
            "WebIdentityToken"
        ]
        == "worker-web-identity"
    )
    args, kwargs = http.post.call_args
    assert args[0].endswith("/internal/v1/agent/bootstrap")
    assert kwargs["headers"]["X-Adp-Workload-Token"] == "pod-token-one"
    assert "x-adp-workload-token" in kwargs["headers"]["Authorization"]
    assert "/us-east-1/execute-api/aws4_request" in kwargs["headers"]["Authorization"]
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
