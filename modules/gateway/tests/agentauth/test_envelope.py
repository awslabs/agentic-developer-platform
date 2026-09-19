"""The listener's independent authorization check (#5028 AC5).

Every rejection named in AC5 has a test here: forged, expired, wrong issuer,
wrong audience, wrong target, wrong generation, changed action, changed body.
The signing/verification asymmetry is itself under test — a worker holding only
the public key must not be able to produce an envelope.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from src.agentauth.envelope import (
    MAX_ENVELOPE_TTL_SECONDS,
    SIGNING_KEY_ENV,
    SIGNING_KEY_ID_ENV,
    EnvelopeError,
    body_digest,
    sign_envelope,
    verify_envelope,
)

NOW = datetime(2026, 9, 13, 12, 0, 0, tzinfo=UTC)
RUN_ID = "run-developer-7"
GENERATION = 3
COMMAND_ID = "8f14e45f-ceea-467a-9b3c-1c2a3d4e5f60"
BODY = b'{"command_id":"8f14e45f-ceea-467a-9b3c-1c2a3d4e5f60","reason":"flow cancelled"}'


def _keypair():
    private = Ed25519PrivateKey.generate()
    pem = private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    return pem, private.public_key()


@pytest.fixture
def gateway():
    """The trusted control service's signing side."""
    pem, public = _keypair()
    return {
        "env": {SIGNING_KEY_ENV: pem, SIGNING_KEY_ID_ENV: "key-2026-09"},
        "public_keys": {"key-2026-09": public},
    }


def _sign(gateway, **overrides) -> str:
    kwargs = {
        "tenant_id": "org-tenant-001",
        "principal": "inv-coordinator#1",
        "target_run_id": RUN_ID,
        "target_generation": GENERATION,
        "action": "abort",
        "command_id": COMMAND_ID,
        "request_body": BODY,
        "grant_id": "grant-coordinator-1",
        "revocation_epoch": 2,
        "flow_id": "flow-42",
        "now": NOW,
        "env": gateway["env"],
    }
    kwargs.update(overrides)
    return sign_envelope(**kwargs)


def _verify(gateway, token, **overrides):
    kwargs = {
        "public_keys": gateway["public_keys"],
        "expected_run_id": RUN_ID,
        "expected_generation": GENERATION,
        "expected_action": "abort",
        "expected_command_id": COMMAND_ID,
        "request_body": BODY,
        "now": NOW,
    }
    kwargs.update(overrides)
    return verify_envelope(token, **kwargs)


class TestRoundTrip:
    def test_a_gateway_signed_envelope_verifies_against_the_public_key(self, gateway):
        env = _verify(gateway, _sign(gateway))

        assert env.target_run_id == RUN_ID
        assert env.target_generation == GENERATION
        assert env.action == "abort"
        assert env.command_id == COMMAND_ID
        assert env.grant_id == "grant-coordinator-1"
        assert env.revocation_epoch == 2

    def test_the_epoch_is_available_for_queued_action_revalidation(self, gateway):
        """AC6: a queued action re-checks this against the current grant epoch."""
        assert _verify(gateway, _sign(gateway, revocation_epoch=9)).revocation_epoch == 9

    def test_ttl_is_capped_at_the_documented_maximum(self, gateway):
        env = _verify(gateway, _sign(gateway, ttl_seconds=6000))
        assert (env.expires_at - env.not_before).total_seconds() == MAX_ENVELOPE_TTL_SECONDS


class TestWorkersCannotForge:
    """The asymmetry is the security property, so it gets an explicit test."""

    def test_the_public_key_alone_cannot_sign(self, gateway):
        """A worker holds only what is in `public_keys`; signing needs the PEM."""
        with pytest.raises(EnvelopeError, match="not configured"):
            _sign(gateway, env={SIGNING_KEY_ID_ENV: "key-2026-09"})

    def test_an_envelope_signed_by_a_different_key_is_refused(self, gateway):
        """A worker that generated its own keypair gains nothing."""
        rogue_pem, _ = _keypair()
        forged = _sign(gateway, env={SIGNING_KEY_ENV: rogue_pem, SIGNING_KEY_ID_ENV: "key-2026-09"})
        with pytest.raises(EnvelopeError, match="signature"):
            _verify(gateway, forged)

    def test_an_unknown_key_id_is_refused_rather_than_tried_against_every_key(self, gateway):
        rogue_pem, _ = _keypair()
        forged = _sign(gateway, env={SIGNING_KEY_ENV: rogue_pem, SIGNING_KEY_ID_ENV: "attacker-key"})
        with pytest.raises(EnvelopeError, match="unknown envelope key id"):
            _verify(gateway, forged)

    def test_a_tampered_payload_is_refused(self, gateway):
        import base64
        import json

        token = _sign(gateway)
        version, body_b64, sig = token.split(".")
        payload = json.loads(base64.urlsafe_b64decode(body_b64 + "==").decode())
        payload["target_run_id"] = "run-victim"
        tampered = base64.urlsafe_b64encode(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).rstrip(b"=").decode()
        with pytest.raises(EnvelopeError):
            _verify(gateway, f"{version}.{tampered}.{sig}", expected_run_id="run-victim")

    def test_a_non_ed25519_signing_key_is_refused(self, gateway):
        """An RSA key must not be used under an `ed25519` label."""
        from cryptography.hazmat.primitives.asymmetric import rsa

        rsa_pem = (
            rsa.generate_private_key(public_exponent=65537, key_size=2048)
            .private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            )
            .decode()
        )
        with pytest.raises(EnvelopeError, match="not Ed25519"):
            _sign(gateway, env={SIGNING_KEY_ENV: rsa_pem, SIGNING_KEY_ID_ENV: "k"})


class TestBindingChecks:
    """AC5: wrong target, generation, action or body must all be refused."""

    def test_replaying_a_valid_envelope_at_another_run_is_refused(self, gateway):
        token = _sign(gateway)
        with pytest.raises(EnvelopeError, match="target mismatch"):
            _verify(gateway, token, expected_run_id="run-other")

    def test_an_old_generation_envelope_is_refused_after_a_restart(self, gateway):
        """A restarted pod has a new generation; the old authorization is stale."""
        token = _sign(gateway, target_generation=2)
        with pytest.raises(EnvelopeError, match="generation mismatch"):
            _verify(gateway, token, expected_generation=3)

    def test_swapping_the_action_is_refused(self, gateway):
        """An envelope authorizing `pause` must not admit an `abort`."""
        token = _sign(gateway, action="pause")
        with pytest.raises(EnvelopeError, match="action mismatch"):
            _verify(gateway, token, expected_action="abort")

    def test_changing_the_body_after_authorization_is_refused(self, gateway):
        """The steer-instruction swap: same envelope, different instruction."""
        token = _sign(gateway)
        changed = BODY.replace(b"flow cancelled", b"exfiltrate secrets")
        with pytest.raises(EnvelopeError, match="body mismatch"):
            _verify(gateway, token, request_body=changed)

    def test_a_mismatched_command_id_is_refused(self, gateway):
        token = _sign(gateway)
        with pytest.raises(EnvelopeError, match="command mismatch"):
            _verify(gateway, token, expected_command_id="00000000-0000-4000-8000-000000000000")

    def test_body_digest_is_over_raw_bytes_not_a_reparsed_object(self, gateway):
        """Two wire bodies that parse alike must not share a digest."""
        assert body_digest(b'{"a":1,"b":2}') != body_digest(b'{"b":2,"a":1}')


class TestIssuerAudienceAndAlgorithm:
    def test_a_wrong_issuer_is_refused(self, gateway):
        token = _forge_claim(gateway, "iss", "some-other-service")
        with pytest.raises(EnvelopeError, match="untrusted envelope issuer"):
            _verify(gateway, token)

    def test_a_wrong_audience_is_refused(self, gateway):
        token = _forge_claim(gateway, "aud", "adp-gateway-browser")
        with pytest.raises(EnvelopeError, match="audience mismatch"):
            _verify(gateway, token)

    def test_an_unlisted_algorithm_is_refused_before_any_signature_work(self, gateway):
        """No `alg`-confusion foothold: the field is checked against an allowlist."""
        token = _forge_claim(gateway, "alg", "none")
        with pytest.raises(EnvelopeError, match="unsupported envelope algorithm"):
            _verify(gateway, token)


class TestValidity:
    @pytest.mark.parametrize("ttl", [0, -1, -3600, True, False, None, "1", "invalid", 0.5, 1.5, float("nan"), float("inf")])
    def test_signer_refuses_invalid_lifetimes(self, gateway, ttl):
        with pytest.raises(EnvelopeError, match="lifetime"):
            _sign(gateway, ttl_seconds=ttl)

    def test_one_second_lifetime_is_valid(self, gateway):
        proof = _verify(gateway, _sign(gateway, ttl_seconds=1))
        assert (proof.expires_at - proof.issued_at).total_seconds() == 1

    def test_an_expired_envelope_is_refused(self, gateway):
        token = _sign(gateway, ttl_seconds=10)
        with pytest.raises(EnvelopeError, match="expired"):
            _verify(gateway, token, now=NOW + timedelta(seconds=11))

    def test_an_envelope_from_the_future_is_refused(self, gateway):
        token = _sign(gateway, now=NOW + timedelta(minutes=5))
        with pytest.raises(EnvelopeError, match="not yet valid"):
            _verify(gateway, token)

    def test_an_envelope_claiming_a_longer_life_than_policy_is_refused_not_truncated(self, gateway):
        """Accepting it would let the signer overrule our revocation-delay bound."""
        token = _forge_claim(gateway, "exp", "2026-09-14T12:00:00Z")
        with pytest.raises(EnvelopeError, match="validity exceeds policy"):
            _verify(gateway, token)


class TestMalformed:
    @pytest.mark.parametrize("token", ["", "junk", "adpe1.only-two", "adpe2.a.b", "adpe1.!!!.!!!"])
    def test_garbage_is_refused(self, gateway, token):
        with pytest.raises(EnvelopeError):
            _verify(gateway, token)

    @pytest.mark.parametrize("claim", ["tenant_id", "grant_id", "command_id", "body_digest"])
    def test_a_missing_required_claim_is_refused(self, gateway, claim):
        token = _forge_claim(gateway, claim, None, drop=True)
        with pytest.raises(EnvelopeError, match="invalid envelope"):
            _verify(gateway, token)


def _forge_claim(gateway, claim: str, value, *, drop: bool = False) -> str:
    """Re-sign an envelope with one claim altered.

    Signed with the *real* key on purpose: these tests must prove the semantic
    checks fire even when the signature is genuine. Otherwise a passing test
    could be passing only because the signature broke.
    """
    import base64
    import json

    token = _sign(gateway)
    payload = json.loads(base64.urlsafe_b64decode(token.split(".")[1] + "==").decode())
    if drop:
        payload.pop(claim, None)
    else:
        payload[claim] = value

    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    private = serialization.load_pem_private_key(gateway["env"][SIGNING_KEY_ENV].encode(), password=None)
    signature = private.sign(b"adpe1." + raw)
    return "adpe1." + base64.urlsafe_b64encode(raw).rstrip(b"=").decode() + "." + base64.urlsafe_b64encode(signature).rstrip(b"=").decode()
