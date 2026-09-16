"""Regenerates the shared envelope test vectors (#5028 AC5).

## Why a committed fixture instead of each suite signing its own

Two implementations verify these envelopes: :mod:`src.agentauth.envelope` here and
``control-envelope.ts`` in the worker image. If each suite generates its own
envelopes with its own helper, both suites pass forever while the two verifiers
drift — the Python side could tighten a check the TypeScript side never learns
about, and the failure only shows up as a worker rejecting real traffic in
production. A single committed set of bytes that both suites must agree on is the
only thing that actually couples them.

## What is committed and what is not

The fixture contains the **public** verification key and the signed tokens. The
private key is generated fresh on each run of this script and discarded when the
process exits — it is never written anywhere. That is why the fixture is
self-consistent but not byte-reproducible: rerunning this produces a new key and
new signatures, which is fine, because every consumer reads the public key out of
the same file it reads the tokens from.

Timestamps are pinned to ``NOW`` rather than taken from the clock, so the
expiry/not-yet-valid vectors keep meaning what they say.

## The forged vectors

Several vectors need a *valid signature over invalid claims* — an envelope for
another run, a stale generation, an untrusted issuer. Those cannot be produced
through :func:`sign_envelope`, which is the point of it. So this script signs
those payloads directly with the same key. Without that, a test asserting
"generation mismatch is refused" would pass purely because the signature failed,
proving nothing about the check it names.

Run: ``python3 tests/agentauth/generate_envelope_vectors.py`` from ``modules/gateway``.
"""

from __future__ import annotations

import base64
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402

from src.agentauth.envelope import (  # noqa: E402
    ENVELOPE_AUDIENCE,
    ENVELOPE_ISSUER,
    ENVELOPE_VERSION,
    SIGNING_KEY_ENV,
    SIGNING_KEY_ID_ENV,
    body_digest,
    sign_envelope,
)

# The instant every vector is verified at. Fixed so "expired" stays expired.
NOW = datetime(2026, 9, 13, 12, 0, 0, tzinfo=UTC)

KEY_ID = "envelope-key-1"

# The verifier's own independently-known facts. Every vector is checked against
# exactly these, so a vector that should be refused is refused because of its own
# claims and not because the test moved the goalposts.
EXPECTED_RUN_ID = "run-developer-7"
EXPECTED_GENERATION = 3
EXPECTED_ACTION = "pause"
EXPECTED_COMMAND_ID = "cmd-0001"
EXPECTED_BODY = b'{"command_id":"cmd-0001","reason":"budget review"}'

TENANT = "org-tenant-001"
PRINCIPAL = "inv-coordinator#1"
GRANT_ID = "grant-coordinator-1"
FLOW_ID = "flow-42"
AUTHORITY_REF = "decision-abc"

FIXTURE = Path(__file__).resolve().parents[3] / "agent-factory" / "agent" / "src" / "__fixtures__" / "control-envelope-vectors.json"


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _base_payload(**overrides: object) -> dict:
    payload: dict[str, object] = {
        "v": ENVELOPE_VERSION,
        "iss": ENVELOPE_ISSUER,
        "aud": ENVELOPE_AUDIENCE,
        "alg": "ed25519",
        "kid": KEY_ID,
        "tenant_id": TENANT,
        "principal": PRINCIPAL,
        "target_run_id": EXPECTED_RUN_ID,
        "target_generation": EXPECTED_GENERATION,
        "action": EXPECTED_ACTION,
        "command_id": EXPECTED_COMMAND_ID,
        "body_digest": body_digest(EXPECTED_BODY),
        "grant_id": GRANT_ID,
        "revocation_epoch": 1,
        "iat": _iso(NOW),
        "nbf": _iso(NOW),
        "exp": _iso(NOW + timedelta(seconds=30)),
        "flow_id": FLOW_ID,
        "authority_reference_id": AUTHORITY_REF,
    }
    payload.update(overrides)
    return payload


def _forge(private_key: Ed25519PrivateKey, **overrides: object) -> str:
    """Sign an arbitrary payload with the real key.

    Deliberately bypasses ``sign_envelope`` so the resulting token has a genuine
    signature over claims the signer would never emit. This is how a semantic
    check is proven to fire on its own rather than being masked by a signature
    failure.
    """
    body = json.dumps(_base_payload(**overrides), sort_keys=True, separators=(",", ":")).encode("utf-8")
    signature = private_key.sign(ENVELOPE_VERSION.encode() + b"." + body)
    return f"{ENVELOPE_VERSION}.{_b64e(body)}.{_b64e(signature)}"


def build() -> dict:
    private_key = Ed25519PrivateKey.generate()
    public_raw = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    env = {
        SIGNING_KEY_ENV: private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode("utf-8"),
        SIGNING_KEY_ID_ENV: KEY_ID,
    }

    # The one vector produced through the real signing path, so the fixture also
    # proves the producer and the two verifiers agree end to end.
    accepted = sign_envelope(
        tenant_id=TENANT,
        principal=PRINCIPAL,
        target_run_id=EXPECTED_RUN_ID,
        target_generation=EXPECTED_GENERATION,
        action=EXPECTED_ACTION,
        command_id=EXPECTED_COMMAND_ID,
        request_body=EXPECTED_BODY,
        grant_id=GRANT_ID,
        revocation_epoch=1,
        flow_id=FLOW_ID,
        authority_reference_id=AUTHORITY_REF,
        now=NOW,
        env=env,
    )

    tampered = accepted.rsplit(".", 1)[0] + "." + _b64e(b"\x00" * 64)

    unknown_key = Ed25519PrivateKey.generate()
    wrong_signer = unknown_key.sign(ENVELOPE_VERSION.encode() + b"." + json.dumps(_base_payload(), sort_keys=True, separators=(",", ":")).encode())
    body_b64 = _b64e(json.dumps(_base_payload(), sort_keys=True, separators=(",", ":")).encode())

    vectors = [
        {
            "name": "valid",
            "accept": True,
            "token": accepted,
            "note": "produced by sign_envelope; both verifiers must accept it",
        },
        {
            "name": "signature_tampered",
            "accept": False,
            "reason": "bad_signature",
            "token": tampered,
            "note": "AC5 forged: signature replaced with zero bytes",
        },
        {
            "name": "signed_by_a_key_the_listener_does_not_trust",
            "accept": False,
            "reason": "bad_signature",
            "token": f"{ENVELOPE_VERSION}.{body_b64}.{_b64e(wrong_signer)}",
            "note": "AC5 forged: correct kid, wrong signer — a worker minting its own envelope",
        },
        {
            "name": "unknown_key_id",
            "accept": False,
            "reason": "unknown_key",
            "token": _forge(private_key, kid="envelope-key-does-not-exist"),
            "note": "kid selects the key; an unknown kid must not fall back to trying every key",
        },
        {
            "name": "wrong_target_run",
            "accept": False,
            "reason": "target_mismatch",
            "token": _forge(private_key, target_run_id="run-someone-else"),
            "note": "AC5 wrong-target: a genuine envelope replayed at a different run",
        },
        {
            "name": "stale_generation",
            "accept": False,
            "reason": "generation_mismatch",
            "token": _forge(private_key, target_generation=EXPECTED_GENERATION - 1),
            "note": "AC5 wrong-target: replay at a restarted pod",
        },
        {
            "name": "swapped_action",
            "accept": False,
            "reason": "action_mismatch",
            "token": _forge(private_key, action="abort"),
            "note": "an authorized pause must not be usable as an abort",
        },
        {
            "name": "different_command_id",
            "accept": False,
            "reason": "command_mismatch",
            "token": _forge(private_key, command_id="cmd-9999"),
            "note": "AC5 replay: authorization detached from the journal entry that dedupes it",
        },
        {
            "name": "changed_body",
            "accept": False,
            "reason": "body_mismatch",
            "token": _forge(private_key, body_digest=body_digest(b'{"command_id":"cmd-0001","instruction":"rm -rf"}')),
            "note": "AC5 changed-body: instruction edited after authorization",
        },
        {
            "name": "expired",
            "accept": False,
            "reason": "expired",
            "token": _forge(
                private_key,
                iat=_iso(NOW - timedelta(seconds=120)),
                nbf=_iso(NOW - timedelta(seconds=120)),
                exp=_iso(NOW - timedelta(seconds=90)),
            ),
            "note": "AC5 expired",
        },
        {
            "name": "not_yet_valid",
            "accept": False,
            "reason": "not_yet_valid",
            "token": _forge(
                private_key,
                iat=_iso(NOW + timedelta(seconds=60)),
                nbf=_iso(NOW + timedelta(seconds=60)),
                exp=_iso(NOW + timedelta(seconds=80)),
            ),
            "note": "no skew allowance on an authorization envelope, unlike the run credential's nbf",
        },
        {
            "name": "validity_longer_than_policy",
            "accept": False,
            "reason": "validity_too_long",
            "token": _forge(private_key, exp=_iso(NOW + timedelta(hours=6))),
            "note": "a signer claiming a longer life is refused, not truncated — the TTL bounds revocation delay",
        },
        {
            "name": "untrusted_issuer",
            "accept": False,
            "reason": "untrusted_issuer",
            "token": _forge(private_key, iss="some-other-service"),
            "note": "an envelope minted for a different service replayed here",
        },
        {
            "name": "wrong_audience",
            "accept": False,
            "reason": "audience_mismatch",
            "token": _forge(private_key, aud="adp-gateway-internal"),
            "note": "an envelope for a different surface replayed at the listener",
        },
        {
            "name": "alg_none",
            "accept": False,
            "reason": "unsupported_algorithm",
            "token": _forge(private_key, alg="none"),
            "note": "the classic alg-confusion attempt; alg is an allowlist check, never a dispatch",
        },
        {
            "name": "alg_hmac",
            "accept": False,
            "reason": "unsupported_algorithm",
            "token": _forge(private_key, alg="hs256"),
            "note": "downgrade to a symmetric algorithm whose key a worker might hold",
        },
        {
            "name": "missing_required_claim",
            "accept": False,
            "reason": "malformed",
            "token": _forge(private_key, grant_id=""),
            "note": "an empty grant_id must not verify as grantless",
        },
        {
            "name": "revocation_epoch_zero",
            "accept": False,
            "reason": "malformed",
            "token": _forge(private_key, revocation_epoch=0),
            "note": "epoch 0 would compare as older than every live grant",
        },
        {
            "name": "generation_as_string",
            "accept": False,
            "reason": "malformed",
            "token": _forge(private_key, target_generation=str(EXPECTED_GENERATION)),
            "note": "a string generation must not coerce into a match",
        },
        # --- malformed claim TYPES ------------------------------------------
        # These carry every required claim, non-empty, with a genuine signature —
        # so they get past the presence check and reach the code that consumes the
        # claim. Both verifiers previously threw a raw language exception here
        # instead of refusing: Python raised `TypeError: unhashable type: 'list'`
        # hashing `alg` into a frozenset, and TypeScript threw
        # `TypeError: Cannot convert object to primitive value` on `String(kid)`.
        # Neither admitted anything, but a refusal path that throws is a refusal
        # the caller cannot observe uniformly — the listener answers 500 instead of
        # its opaque authorization refusal, which distinguishes this input.
        #
        # They live in the shared fixture rather than in one suite because the
        # coercion each language would otherwise apply DIFFERS: `String(["k1"])` is
        # `"k1"` in JS but `str(["k1"])` is `"['k1']"` in Python. Two verifiers
        # that coerce disagree about what a malformed envelope says, and only a
        # shared vector catches that.
        {
            "name": "alg_not_a_string",
            "accept": False,
            "reason": "malformed",
            "token": _forge(private_key, alg=[]),
            "note": "alg as a list reached a set-membership test and raised instead of refusing",
        },
        {
            "name": "kid_not_a_string",
            "accept": False,
            "reason": "malformed",
            "token": _forge(private_key, kid={"toString": 0}),
            "note": "kid as an object with a non-callable toString threw on coercion in TypeScript",
        },
        {
            "name": "kid_as_array",
            "accept": False,
            "reason": "malformed",
            "token": _forge(private_key, kid=[KEY_ID]),
            "note": "coercing this would yield the real key id in JS but not in Python",
        },
        {
            "name": "target_run_id_as_array",
            "accept": False,
            "reason": "malformed",
            "token": _forge(private_key, target_run_id=[EXPECTED_RUN_ID]),
            "note": "same cross-language coercion hazard on the binding the target check depends on",
        },
        {
            "name": "revocation_epoch_as_bool",
            "accept": False,
            "reason": "malformed",
            "token": _forge(private_key, revocation_epoch=True),
            "note": "bool is an int subclass in Python; True must not pass as epoch 1",
        },
        {
            "name": "generation_as_bool",
            "accept": False,
            "reason": "malformed",
            "token": _forge(private_key, target_generation=True),
            "note": "and must not pass as generation 1 either",
        },
        {
            "name": "body_digest_as_number",
            "accept": False,
            "reason": "malformed",
            "token": _forge(private_key, body_digest=0),
            "note": "a non-string digest must be refused before the comparison, not coerced into one",
        },
        {
            "name": "wrong_version_prefix",
            "accept": False,
            "reason": "malformed",
            "token": "adpe0." + accepted.split(".", 1)[1],
            "note": "version is checked before anything is parsed",
        },
        {
            "name": "not_three_parts",
            "accept": False,
            "reason": "malformed",
            "token": f"{ENVELOPE_VERSION}.{body_b64}",
            "note": "structural check before base64 decoding",
        },
        {
            "name": "payload_not_an_object",
            "accept": False,
            "reason": "malformed",
            "token": f"{ENVELOPE_VERSION}.{_b64e(b'[1,2,3]')}.{_b64e(b'x' * 64)}",
            "note": "a JSON array must not be indexed as claims",
        },
    ]

    return {
        "note": (
            "Shared vectors for the #5028 control authorization envelope. Verified by "
            "modules/gateway/tests/agentauth/test_envelope_vectors.py and "
            "modules/agent-factory/agent/src/control-envelope.test.ts. Regenerate with "
            "modules/gateway/tests/agentauth/generate_envelope_vectors.py. Contains a public "
            "verification key only; the signing key is generated per run and never written."
        ),
        "version": ENVELOPE_VERSION,
        "now": _iso(NOW),
        "public_keys": {KEY_ID: base64.b64encode(public_raw).decode("ascii")},
        "expected": {
            "run_id": EXPECTED_RUN_ID,
            "generation": EXPECTED_GENERATION,
            "action": EXPECTED_ACTION,
            "command_id": EXPECTED_COMMAND_ID,
            "body": EXPECTED_BODY.decode("utf-8"),
        },
        "vectors": vectors,
    }


if __name__ == "__main__":
    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE.write_text(json.dumps(build(), indent=2) + "\n")
    print(f"wrote {FIXTURE}")
