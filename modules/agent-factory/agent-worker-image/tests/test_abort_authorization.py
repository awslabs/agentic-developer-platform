"""The abort sentinel is a transport, not an authority — Issue #3963 (S4).

Everything in the sentinel document is *self-asserted*. The agent process runs
with a ``Bash`` tool under ``bypassPermissions``, so any code in the pod can write
a file at the sentinel path claiming this run was aborted. A finalizer that
honoured that file would delete a live run's queue message and report a crash as
a deliberate stop — on the strength of a document the run wrote about itself.

``verify_abort_authorization`` is what makes the claim checkable. It requires the
Ed25519 envelope the *gateway* minted for this exact command. The signing key
exists only in the gateway; this image holds public verification keys and has no
signing path at all. So a valid envelope is the one artifact in the pod that
could not have been produced from inside it.

The suite is built around the attack rather than the happy path. The key case is
``test_an_envelope_signed_by_a_key_the_pod_could_hold_is_refused``: it mints a
*structurally perfect* envelope with an attacker's own key, which is precisely
what a compromised agent could do, and requires a ``False``. Every other test is
a binding that stops a genuine signature being replayed to mean something it does
not — another run's abort, a superseded generation, a ``pause`` re-read as an
abort, or a model-policy decision re-read as a control command.

The signing here deliberately reproduces ``src/agentauth/envelope.py``'s exact
canonicalization (``sort_keys``, ``(",", ":")`` separators, unpadded urlsafe
base64, signature over ``version + "." + body``). If the gateway ever changes
that, these tests stop verifying and say so.
"""

from __future__ import annotations

import base64
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.abort_sentinel import (  # noqa: E402
    CONTROL_ENVELOPE_AUDIENCE,
    ENVELOPE_ISSUER,
    ENVELOPE_VERSION,
    MAX_ENVELOPE_TTL_SECONDS,
    MAX_SENTINEL_ENVELOPE_BYTES,
    validate_abort_sentinel,
    verify_abort_authorization,
)

RUN_ID = "run-developer-7"
GENERATION = 3
COMMAND_ID = "cmd-abort-1"
TENANT = "tenant-1"

#: When the gateway signed the envelope.
SIGNED_AT = datetime(2026, 9, 24, 12, 0, 0, tzinfo=timezone.utc)

#: When the run actually finalizes. Half an hour later, which is the *normal*
#: case: the envelope's life is 30 seconds and a run winds down long after. Every
#: test verifies at this instant so the suite would catch a liveness check being
#: added here by mistake — see ``test_a_genuine_abort_verifies_long_after_the_envelope_expired``.
FINALIZED_AT = SIGNED_AT + timedelta(minutes=30)


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


@pytest.fixture()
def gateway_key() -> Ed25519PrivateKey:
    """The gateway's signer. Nothing in the worker image has its private half."""
    return Ed25519PrivateKey.generate()


@pytest.fixture()
def public_keys(gateway_key) -> dict:
    return {"gw-key-1": gateway_key.public_key()}


def _mint(signer: Ed25519PrivateKey, **overrides) -> str:
    """Sign an envelope exactly the way ``src/agentauth/envelope.py`` does.

    Every claim is overridable through ``overrides`` alone — deliberately no named
    per-claim parameters, because a named one would swallow the ``_ABSENT``
    sentinel instead of removing the claim, and the "is this claim required" tests
    would then silently assert something else.
    """
    payload = {
        "v": ENVELOPE_VERSION,
        "iss": ENVELOPE_ISSUER,
        "aud": CONTROL_ENVELOPE_AUDIENCE,
        "alg": "ed25519",
        "kid": "gw-key-1",
        "tenant_id": TENANT,
        "principal": "user-1",
        "target_run_id": RUN_ID,
        "target_generation": GENERATION,
        "action": "abort",
        "command_id": COMMAND_ID,
        "body_digest": "a" * 64,
        "authority_kind": "human_session",
        "iat": _iso(SIGNED_AT),
        "nbf": _iso(SIGNED_AT),
        "exp": _iso(SIGNED_AT + timedelta(seconds=MAX_ENVELOPE_TTL_SECONDS)),
    }
    for key, value in overrides.items():
        if value is _ABSENT:
            payload.pop(key, None)
        else:
            payload[key] = value
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    signature = signer.sign(ENVELOPE_VERSION.encode("ascii") + b"." + body)
    return f"{ENVELOPE_VERSION}.{_b64(body)}.{_b64(signature)}"


_ABSENT = object()


def _sentinel(envelope: object, *, command_id: str = COMMAND_ID) -> dict:
    return {"command_id": command_id, "envelope": envelope}


def _verify(sentinel: dict, public_keys: dict, **kwargs) -> bool:
    return verify_abort_authorization(
        sentinel,
        run_id=kwargs.get("run_id", RUN_ID),
        generation=kwargs.get("generation", GENERATION),
        public_keys=public_keys,
        now=kwargs.get("now", FINALIZED_AT),
    )


class TestTheOneCaseThatProvesAnAbort:
    def test_a_genuine_abort_verifies_long_after_the_envelope_expired(
        self, gateway_key, public_keys
    ):
        # The single most important behaviour in this module. An envelope lives 30
        # seconds; finalization happens when the run winds down, routinely much
        # later. What is being established is an authenticated *historical* fact —
        # "the gateway authorized this abort for this run and generation" — not a
        # live grant. Liveness was enforced where it belongs: the listener checked
        # the envelope inside its window and `deliverAuthorized` re-checked the
        # grant against the gateway immediately before the executor ran.
        #
        # An `exp`-against-now check here would reject every real abort and make
        # the whole feature dead on arrival.
        assert _verify(_sentinel(_mint(gateway_key)), public_keys) is True


class TestForgeryFromInsideThePod:
    """The threat this function exists for."""

    def test_an_envelope_signed_by_a_key_the_pod_could_hold_is_refused(self, public_keys):
        # A structurally flawless envelope — every claim correct, every binding
        # right — signed with a key that is not the gateway's. This is exactly
        # what a compromised agent can produce, and the *only* thing standing in
        # its way is the signature check. If this test ever passes, the sentinel
        # is back to being self-asserted and the feature is a way to delete live
        # runs' queue messages.
        attacker = Ed25519PrivateKey.generate()

        assert _verify(_sentinel(_mint(attacker)), public_keys) is False

    def test_a_sentinel_with_no_envelope_is_not_an_authorized_abort(self, public_keys):
        # The shape a hand-written /tmp file takes: plausible fields, no proof.
        assert _verify(_sentinel(None), public_keys) is False

    @pytest.mark.parametrize(
        "token",
        ["", "not-a-token", "adpe1.only-two", "wrongver.aaa.bbb", "adpe1..", "..", "a.b.c.d"],
        ids=["empty", "opaque", "two_parts", "wrong_version", "blank_parts", "dots", "four_parts"],
    )
    def test_a_malformed_token_is_refused_without_raising(self, public_keys, token):
        # Runs during teardown, where an exception could cost the SQS
        # acknowledgement and strand the message.
        assert _verify(_sentinel(token), public_keys) is False

    def test_a_truncated_token_is_refused(self, gateway_key, public_keys):
        assert _verify(_sentinel(_mint(gateway_key)[:40]), public_keys) is False

    def test_a_tampered_payload_fails_the_signature(self, gateway_key, public_keys):
        # Re-encode a *different* body under the original signature: the claim an
        # attacker would want to change is the run id.
        version, body, signature = _mint(gateway_key).split(".")
        decoded = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        decoded["target_run_id"] = RUN_ID
        decoded["action"] = "abort"
        decoded["tenant_id"] = "tenant-attacker"
        swapped = _b64(json.dumps(decoded, sort_keys=True, separators=(",", ":")).encode())

        assert _verify(_sentinel(f"{version}.{swapped}.{signature}"), public_keys) is False

    def test_an_oversized_token_is_refused_before_parsing(self, public_keys):
        assert _verify(_sentinel("x" * (MAX_SENTINEL_ENVELOPE_BYTES + 1)), public_keys) is False

    @pytest.mark.parametrize(
        "envelope",
        [42, {"token": "x"}, ["adpe1"], True],
        ids=["number", "dict", "list", "bool"],
    )
    def test_a_non_string_envelope_is_refused(self, public_keys, envelope):
        assert _verify(_sentinel(envelope), public_keys) is False


class TestFailClosedWhenItCannotCheck:
    def test_no_verification_keys_means_the_abort_is_not_proven(self, gateway_key):
        # "Cannot check" must mean "refuse", not "allow" — the same direction the
        # listener takes when it was never given keys. The opposite choice is the
        # marker-signing fail-open defect (#4128), on a larger blast radius.
        assert _verify(_sentinel(_mint(gateway_key)), {}) is False

    def test_an_unknown_key_id_is_refused_rather_than_tried_against_every_key(
        self, gateway_key, public_keys
    ):
        assert _verify(_sentinel(_mint(gateway_key, kid="rotated-away")), public_keys) is False


class TestBindingsThatStopAGenuineSignatureBeingReplayed:
    """Each of these is a real gateway signature over a *different* statement."""

    def test_another_runs_abort_does_not_abort_this_run(self, gateway_key, public_keys):
        # The leftover-file and cross-run cases. A neighbouring run's genuine
        # abort must not finalize this one.
        token = _mint(gateway_key, target_run_id="run-somebody-else")

        assert _verify(_sentinel(token), public_keys) is False

    @pytest.mark.parametrize("generation", [GENERATION - 1, GENERATION + 1])
    def test_a_different_control_generation_is_refused(self, gateway_key, public_keys, generation):
        # Equality, not "at least": an abort aimed at an attempt that already
        # ended must not finalize the attempt that replaced it.
        token = _mint(gateway_key, target_generation=generation)

        assert _verify(_sentinel(token), public_keys) is False

    @pytest.mark.parametrize("action", ["pause", "resume", "steer", "ABORT", ""])
    def test_an_envelope_for_another_verb_is_not_an_abort_authorization(
        self, gateway_key, public_keys, action
    ):
        # A pause is authorized far more freely than an abort. Without this check
        # a genuine pause envelope would be a way to terminate a run.
        assert _verify(_sentinel(_mint(gateway_key, action=action)), public_keys) is False

    def test_a_model_policy_envelope_is_not_a_control_authorization(self, gateway_key, public_keys):
        # Same signer, same key, different audience. Audience separation is the
        # only thing that stops one signed decision being replayed as the other.
        token = _mint(gateway_key, aud="adp-agent-model-policy")

        assert _verify(_sentinel(token), public_keys) is False

    def test_an_envelope_from_an_untrusted_issuer_is_refused(self, gateway_key, public_keys):
        assert _verify(_sentinel(_mint(gateway_key, iss="not-the-gateway")), public_keys) is False

    def test_the_proof_must_name_the_command_the_sentinel_reports(self, gateway_key, public_keys):
        # Binds proof to record, so an envelope cannot be paired with a different
        # command's sentinel.
        token = _mint(gateway_key, command_id="cmd-some-other")

        assert _verify(_sentinel(token), public_keys) is False

    def test_a_missing_command_id_on_the_sentinel_cannot_match_a_proof(
        self, gateway_key, public_keys
    ):
        sentinel = {"envelope": _mint(gateway_key)}

        assert _verify(sentinel, public_keys) is False


class TestAlgorithmAndClaimHygiene:
    @pytest.mark.parametrize("alg", ["none", "None", "HS256", "rsa", ""])
    def test_alg_is_an_allowlist_and_never_a_dispatch_table(self, gateway_key, public_keys, alg):
        # `alg: "none"` is simply not in the list. The claim is compared, never
        # used to choose a verifier.
        assert _verify(_sentinel(_mint(gateway_key, alg=alg)), public_keys) is False

    @pytest.mark.parametrize(
        "claim",
        [
            "iss",
            "aud",
            "alg",
            "kid",
            "tenant_id",
            "principal",
            "target_run_id",
            "target_generation",
            "action",
            "command_id",
            "iat",
            "nbf",
            "exp",
        ],
    )
    def test_every_required_claim_is_required(self, gateway_key, public_keys, claim):
        assert _verify(_sentinel(_mint(gateway_key, **{claim: _ABSENT})), public_keys) is False

    @pytest.mark.parametrize(
        "generation",
        ["3", 3.0, True, None, [3]],
        ids=["string", "float", "bool", "none", "list"],
    )
    def test_the_generation_claim_must_be_a_real_integer(
        self, gateway_key, public_keys, generation
    ):
        # `isinstance(True, int)` is True in Python, so a JSON `true` would
        # otherwise compare equal to generation 1. Types are required rather than
        # coerced: `str(value)` on hostile JSON is a silent accept, and the two
        # runtimes disagree about what it produces.
        token = _mint(gateway_key, target_generation=generation)

        assert _verify(_sentinel(token), public_keys) is False

    @pytest.mark.parametrize("claim", ["iss", "aud", "kid", "action", "command_id"])
    def test_a_non_string_where_a_string_is_required_is_refused(
        self, gateway_key, public_keys, claim
    ):
        assert _verify(_sentinel(_mint(gateway_key, **{claim: ["x"]})), public_keys) is False

    def test_a_non_object_payload_is_refused(self, gateway_key, public_keys):
        body = _b64(b'"just-a-string"')
        signature = _b64(gateway_key.sign(ENVELOPE_VERSION.encode() + b"." + b'"just-a-string"'))

        assert _verify(_sentinel(f"{ENVELOPE_VERSION}.{body}.{signature}"), public_keys) is False


class TestLifetimeBounds:
    def test_a_signer_may_not_claim_a_longer_life_than_the_platform_allows(
        self, gateway_key, public_keys
    ):
        # Rejected rather than truncated: accepting it would let the signer
        # overrule the revocation-delay bound the platform documents.
        token = _mint(gateway_key, exp=_iso(SIGNED_AT + timedelta(hours=2)))

        assert _verify(_sentinel(token), public_keys) is False

    def test_an_envelope_issued_after_its_own_validity_start_is_refused(
        self, gateway_key, public_keys
    ):
        token = _mint(gateway_key, iat=_iso(SIGNED_AT + timedelta(seconds=10)))

        assert _verify(_sentinel(token), public_keys) is False

    def test_a_proof_not_yet_valid_when_the_run_ended_cannot_describe_it(
        self, gateway_key, public_keys
    ):
        # The one time comparison that is still made. A future-dated envelope
        # cannot be authorization for a run that already finished.
        future = FINALIZED_AT + timedelta(hours=1)
        token = _mint(
            gateway_key,
            iat=_iso(future),
            nbf=_iso(future),
            exp=_iso(future + timedelta(seconds=MAX_ENVELOPE_TTL_SECONDS)),
        )

        assert _verify(_sentinel(token), public_keys) is False

    @pytest.mark.parametrize(
        "stamp",
        ["", "not-a-date", "2026-09-24", "2026-09-24T12:00:00+00:00", 0],
        ids=["blank", "words", "date_only", "offset_form", "number"],
    )
    def test_an_unparseable_timestamp_is_refused(self, gateway_key, public_keys, stamp):
        assert _verify(_sentinel(_mint(gateway_key, nbf=stamp)), public_keys) is False


class TestValidationAndAuthorizationStaySeparate:
    """Two questions, two answers — deliberately not collapsed into one.

    ``validate_abort_sentinel`` answers "is this a well-formed sentinel for this
    run"; ``verify_abort_authorization`` answers "did the gateway authorize it".
    Collapsing them would make an unauthorized abort indistinguishable from a
    corrupt file, and those call for different handling: one is a security
    refusal worth an operator-visible warning, the other is a lost signal.
    """

    def test_a_validated_sentinel_carries_the_envelope_through_for_verification(
        self, gateway_key, public_keys
    ):
        token = _mint(gateway_key)
        document = {
            "version": 1,
            "run_id": RUN_ID,
            "generation": GENERATION,
            "command_id": COMMAND_ID,
            "requested_at": "2026-09-24T12:00:00Z",
            "reason": "wrong branch",
            "envelope": token,
        }

        validated = validate_abort_sentinel(document, RUN_ID, GENERATION)

        assert validated is not None
        assert validated["envelope"] == token
        # And the round trip actually verifies, which is what proves the two
        # halves compose rather than merely both existing.
        assert _verify(validated, public_keys) is True

    def test_validation_succeeds_on_a_sentinel_whose_authorization_will_fail(self):
        # The important asymmetry: a well-formed document with no proof is still a
        # well-formed document. It parses, and it is refused authorization.
        document = {
            "version": 1,
            "run_id": RUN_ID,
            "generation": GENERATION,
            "command_id": COMMAND_ID,
            "requested_at": "2026-09-24T12:00:00Z",
            "reason": None,
        }

        validated = validate_abort_sentinel(document, RUN_ID, GENERATION)

        assert validated is not None
        assert validated["envelope"] is None
        assert _verify(validated, {"gw-key-1": Ed25519PrivateKey.generate().public_key()}) is False

    @pytest.mark.parametrize(
        "envelope",
        [42, {"a": 1}, [], "", "x" * (MAX_SENTINEL_ENVELOPE_BYTES + 1)],
        ids=["number", "dict", "list", "empty", "oversized"],
    )
    def test_an_unusable_envelope_normalizes_to_none_rather_than_a_new_shape(self, envelope):
        # So "no proof" is one unambiguous state the consumer tests once.
        document = {
            "version": 1,
            "run_id": RUN_ID,
            "generation": GENERATION,
            "command_id": COMMAND_ID,
            "requested_at": "2026-09-24T12:00:00Z",
            "envelope": envelope,
        }

        assert validate_abort_sentinel(document, RUN_ID, GENERATION)["envelope"] is None
