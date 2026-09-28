"""Caller identity cannot be forged by editing the pod environment (#5028 AC2).

The attack these tests describe is concrete: two real workers assume the same
platform IAM role, so SigV4 tells the gateway nothing about *which* run called.
Today the gateway reads the caller's own invocation ID out of the request body,
sourced from an environment variable the worker can rewrite. These tests assert
that moving that ID into a MAC'd credential removes the forgery.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from src.agentauth.run_credential import (
    CREDENTIAL_KEY_ENV,
    MAX_CREDENTIAL_TTL_SECONDS,
    CredentialError,
    mint_credential,
    verify_credential,
)

ENV = {CREDENTIAL_KEY_ENV: "test-run-credential-key-not-a-real-secret"}
OTHER_ENV = {CREDENTIAL_KEY_ENV: "a-different-key-entirely"}
NOW = datetime(2026, 9, 13, 12, 0, 0, tzinfo=UTC)


def _mint(**overrides) -> str:
    kwargs = {
        "invocation_id": "inv-coordinator-1",
        "attempt": 1,
        "tenant_id": "org-tenant-001",
        "flow_id": "flow-42",
        "persona": "operations",
        "now": NOW,
        "env": ENV,
    }
    kwargs.update(overrides)
    return mint_credential(**kwargs)


class TestRoundTrip:
    def test_verifies_and_returns_bound_identity(self):
        cred = verify_credential(_mint(), now=NOW, env=ENV)

        assert cred.invocation_id == "inv-coordinator-1"
        assert cred.attempt == 1
        assert cred.tenant_id == "org-tenant-001"
        assert cred.flow_id == "flow-42"
        assert cred.principal == "inv-coordinator-1#1"

    def test_credential_epoch_is_independent_of_run_generation(self):
        """Renewal must be able to bump the credential without touching the run.

        AC6 requires renewal that does not reset generation or the journal. That
        is only possible if the credential carries its own counter, so this
        asserts the field exists and round-trips separately.
        """
        cred = verify_credential(_mint(credential_epoch=4), now=NOW, env=ENV)
        assert cred.credential_epoch == 4

    def test_ttl_is_capped_not_rejected(self):
        cred = verify_credential(_mint(ttl_seconds=99999), now=NOW, env=ENV)
        assert cred.expires_at == NOW + timedelta(seconds=MAX_CREDENTIAL_TTL_SECONDS)


class TestImpersonationIsRefused:
    """AC2: a worker rewriting its environment gains no other identity."""

    def test_worker_cannot_mint_its_own_credential_without_the_key(self):
        with pytest.raises(CredentialError):
            mint_credential(
                invocation_id="inv-victim",
                attempt=1,
                tenant_id="org-tenant-001",
                now=NOW,
                env={},
            )

    def test_credential_signed_with_another_key_is_refused(self):
        forged = mint_credential(
            invocation_id="inv-victim",
            attempt=1,
            tenant_id="org-tenant-001",
            now=NOW,
            env=OTHER_ENV,
        )
        with pytest.raises(CredentialError):
            verify_credential(forged, now=NOW, env=ENV)

    def test_rewriting_the_invocation_id_in_the_payload_breaks_the_mac(self):
        """The concrete attack: take your own valid credential, swap the ID."""
        token = _mint()
        version, body_b64, mac = token.split(".")

        import base64

        body = json.loads(base64.urlsafe_b64decode(body_b64 + "==").decode())
        body["invocation_id"] = "inv-victim"
        tampered_body = base64.urlsafe_b64encode(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).rstrip(b"=").decode()

        with pytest.raises(CredentialError):
            verify_credential(f"{version}.{tampered_body}.{mac}", now=NOW, env=ENV)

    def test_attempt_is_bound_so_an_old_attempts_credential_is_distinguishable(self):
        """A credential from attempt 1 does not assert attempt 2's identity.

        Verification still succeeds — the credential is genuine — but it reports
        attempt 1, so a policy comparing it against a target expecting attempt 2
        sees the mismatch. The binding is what makes that comparison possible.
        """
        cred = verify_credential(_mint(attempt=1), now=NOW, env=ENV)
        assert cred.principal == "inv-coordinator-1#1"
        assert cred.principal != "inv-coordinator-1#2"

    def test_unsigned_token_is_refused_rather_than_trusted(self):
        """No graceful degradation to "unsigned means unverified but allowed"."""
        import base64

        payload = json.dumps({"v": "adpr1", "invocation_id": "inv-victim"}).encode()
        naked = f"adpr1.{base64.urlsafe_b64encode(payload).rstrip(b'=').decode()}."
        with pytest.raises(CredentialError):
            verify_credential(naked, now=NOW, env=ENV)


class TestMalformedInput:
    @pytest.mark.parametrize(
        "token",
        [
            "",
            "not-a-token",
            "adpr1.only-two-parts",
            "adpr2.abc.def",  # wrong version prefix
            "adpr1.!!!not-base64!!!.abc",
            "adpr1." + "A" * 5000 + ".abc",  # over the size bound
        ],
    )
    def test_garbage_is_refused_without_raising_anything_else(self, token):
        with pytest.raises(CredentialError):
            verify_credential(token, now=NOW, env=ENV)

    def test_missing_key_fails_closed(self):
        with pytest.raises(CredentialError, match="not configured"):
            verify_credential(_mint(), now=NOW, env={})

    @pytest.mark.parametrize("claim", ["invocation_id", "tenant_id", "attempt", "expires_at"])
    def test_a_payload_missing_a_required_claim_is_refused(self, claim):
        """A credential without a tenant must not verify as tenantless."""
        import base64
        import hashlib
        import hmac

        body = {
            "v": "adpr1",
            "invocation_id": "inv-1",
            "attempt": 1,
            "tenant_id": "org-1",
            "credential_epoch": 1,
            "issued_at": "2026-09-13T12:00:00Z",
            "not_before": "2026-09-13T12:00:00Z",
            "expires_at": "2026-09-13T12:10:00Z",
        }
        del body[claim]
        raw = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
        mac = hmac.new(ENV[CREDENTIAL_KEY_ENV].encode(), b"adpr1." + raw, hashlib.sha256).digest()
        token = "adpr1." + base64.urlsafe_b64encode(raw).rstrip(b"=").decode() + "." + base64.urlsafe_b64encode(mac).rstrip(b"=").decode()
        with pytest.raises(CredentialError):
            verify_credential(token, now=NOW, env=ENV)

    def test_boolean_attempt_does_not_coerce_to_one(self):
        """`isinstance(True, int)` is True in Python; the check must exclude bools."""
        import base64
        import hashlib
        import hmac

        body = {
            "v": "adpr1",
            "invocation_id": "inv-1",
            "attempt": True,
            "tenant_id": "org-1",
            "credential_epoch": 1,
            "issued_at": "2026-09-13T12:00:00Z",
            "not_before": "2026-09-13T12:00:00Z",
            "expires_at": "2026-09-13T12:10:00Z",
        }
        raw = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
        mac = hmac.new(ENV[CREDENTIAL_KEY_ENV].encode(), b"adpr1." + raw, hashlib.sha256).digest()
        token = "adpr1." + base64.urlsafe_b64encode(raw).rstrip(b"=").decode() + "." + base64.urlsafe_b64encode(mac).rstrip(b"=").decode()
        with pytest.raises(CredentialError):
            verify_credential(token, now=NOW, env=ENV)


class TestValidity:
    def test_expired_credential_is_refused(self):
        token = _mint(ttl_seconds=60)
        with pytest.raises(CredentialError, match="expired"):
            verify_credential(token, now=NOW + timedelta(seconds=61), env=ENV)

    def test_expiry_has_no_grace_window(self):
        """Grace on expiry is how a revoked credential works for one more window."""
        token = _mint(ttl_seconds=60)
        with pytest.raises(CredentialError, match="expired"):
            verify_credential(token, now=NOW + timedelta(seconds=60), env=ENV)

    def test_small_clock_skew_before_not_before_is_tolerated(self):
        token = _mint()
        cred = verify_credential(token, now=NOW - timedelta(seconds=20), env=ENV)
        assert cred.invocation_id == "inv-coordinator-1"

    def test_large_skew_before_not_before_is_refused(self):
        token = _mint()
        with pytest.raises(CredentialError, match="not yet valid"):
            verify_credential(token, now=NOW - timedelta(seconds=120), env=ENV)


class TestMintValidation:
    @pytest.mark.parametrize(
        "kwargs",
        [
            {"invocation_id": ""},
            {"tenant_id": ""},
            {"attempt": 0},
            {"credential_epoch": 0},
        ],
    )
    def test_incomplete_mint_is_refused(self, kwargs):
        with pytest.raises(CredentialError):
            _mint(**kwargs)
