"""Both verifiers agree on the same bytes (#5028 AC5).

``test_envelope.py`` proves the Python verifier is correct. This proves the
Python verifier and the worker's TypeScript verifier are correct about *the same
envelopes* — the property that actually matters in production, where the gateway
signs and the worker verifies.

The fixture is consumed by this suite and by
``modules/agent-factory/agent/src/control-envelope.test.ts``. Neither suite may
generate its own envelopes for these cases: two suites with two generators pass
forever while the verifiers drift, and the drift only surfaces as a worker
rejecting real authorized traffic.

If a check is tightened on one side, the vector for it must be added here and the
other side goes red until it agrees. That red test is the whole point.
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from src.agentauth.envelope import EnvelopeError, verify_envelope

_FIXTURE = Path(__file__).resolve().parents[3] / "agent-factory" / "agent" / "src" / "__fixtures__" / "control-envelope-vectors.json"

# The TypeScript verifier returns a machine-readable reason; the Python verifier
# raises a message. This maps one to the other so a single fixture can drive both
# without either side adopting the other's error style.
_REASON_TO_MESSAGE = {
    "malformed": "invalid envelope",
    "unsupported_algorithm": "unsupported envelope algorithm",
    "untrusted_issuer": "untrusted envelope issuer",
    "audience_mismatch": "envelope audience mismatch",
    "unknown_key": "unknown envelope key id",
    "bad_signature": "invalid envelope signature",
    "target_mismatch": "envelope target mismatch",
    "generation_mismatch": "envelope generation mismatch",
    "action_mismatch": "envelope action mismatch",
    "command_mismatch": "envelope command mismatch",
    "body_mismatch": "envelope body mismatch",
    "expired": "envelope has expired",
    "not_yet_valid": "envelope is not yet valid",
    "validity_too_long": "envelope validity exceeds policy",
}


@pytest.fixture(scope="module")
def fixture() -> dict:
    assert _FIXTURE.exists(), (
        f"{_FIXTURE} is missing. It is committed, not generated at test time — regenerate with "
        "python3 tests/agentauth/generate_envelope_vectors.py and commit the result so the "
        "TypeScript suite verifies the same bytes."
    )
    return json.loads(_FIXTURE.read_text())


@pytest.fixture(scope="module")
def public_keys(fixture) -> dict[str, bytes]:
    return {kid: base64.b64decode(value) for kid, value in fixture["public_keys"].items()}


@pytest.fixture(scope="module")
def now(fixture) -> datetime:
    return datetime.strptime(fixture["now"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def _verify(vector, fixture, public_keys, now):
    expected = fixture["expected"]
    return verify_envelope(
        vector["token"],
        public_keys=public_keys,
        expected_run_id=expected["run_id"],
        expected_generation=expected["generation"],
        expected_action=expected["action"],
        expected_command_id=expected["command_id"],
        request_body=expected["body"].encode("utf-8"),
        now=now,
    )


def _vector_ids(fixture_path: Path) -> list[str]:
    return [v["name"] for v in json.loads(fixture_path.read_text())["vectors"]]


def _vectors() -> list[dict]:
    if not _FIXTURE.exists():
        return []
    return json.loads(_FIXTURE.read_text())["vectors"]


class TestSharedVectors:
    def test_the_fixture_carries_no_private_key(self, fixture):
        """A committed signing key would hand every reader the authority itself.

        The generator keeps the private key in memory for one process and writes
        only the public half; this asserts that stayed true, because the failure
        mode is silent and permanent once committed.
        """
        raw = _FIXTURE.read_text()
        for marker in ("PRIVATE KEY", "private_key", "BEGIN OPENSSH", "signing_key"):
            assert marker not in raw, f"{marker!r} appears in the shared fixture — the signing key must never be committed"

    @pytest.mark.parametrize("vector", _vectors(), ids=_vector_ids(_FIXTURE) if _FIXTURE.exists() else [])
    def test_vector(self, vector, fixture, public_keys, now):
        """Each vector's outcome, with its own reason for existing in the message."""
        if vector["accept"]:
            envelope = _verify(vector, fixture, public_keys, now)
            assert envelope.target_run_id == fixture["expected"]["run_id"]
            assert envelope.action == fixture["expected"]["action"]
            assert envelope.command_id == fixture["expected"]["command_id"]
            return

        with pytest.raises(EnvelopeError) as exc:
            _verify(vector, fixture, public_keys, now)

        expected_message = _REASON_TO_MESSAGE[vector["reason"]]
        assert str(exc.value) == expected_message, (
            f"vector {vector['name']!r} ({vector['note']}) was refused as {str(exc.value)!r} but the "
            f"fixture says the reason is {vector['reason']!r} ({expected_message!r}). Either the check "
            "moved or the vector is now being caught by an earlier check — an earlier catch means the "
            "check this vector was written to exercise is no longer proven to work."
        )

    def test_every_refusal_reason_has_at_least_one_vector(self, fixture):
        """A reason with no vector is a check neither verifier is held to."""
        covered = {v["reason"] for v in fixture["vectors"] if not v["accept"]}
        missing = set(_REASON_TO_MESSAGE) - covered
        assert not missing, f"no shared vector exercises {sorted(missing)}; the TypeScript verifier could drop those checks undetected"

    def test_the_accepted_vector_exists(self, fixture):
        """Refusal vectors alone would be satisfied by a verifier that refuses everything."""
        assert [v for v in fixture["vectors"] if v["accept"]], "the fixture needs at least one envelope both verifiers accept"
