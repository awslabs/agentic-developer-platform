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
import hashlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.abort_sentinel import (  # noqa: E402
    ABORT_RECEIPT_ACTION,
    ABORT_RECEIPT_AUDIENCE,
    CONTROL_ENVELOPE_AUDIENCE,
    ENVELOPE_ISSUER,
    ENVELOPE_VERSION,
    MAX_ENVELOPE_TTL_SECONDS,
    MAX_SENTINEL_ENVELOPE_BYTES,
    authorized_abort_reason,
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


#: The gateway's signer. Nothing in the worker image has its private half.
#:
#: Module-level rather than generated per test so that ``_sentinel`` can mint a
#: genuine default abort receipt (#3963 finding 1) without every one of its ~35 call
#: sites having to thread the key through. Key generation is not what any test here
#: is exercising, and a fresh key per test bought nothing: each test asserts an
#: outcome for the keys it explicitly passes.
_GATEWAY_KEY = Ed25519PrivateKey.generate()


@pytest.fixture()
def gateway_key() -> Ed25519PrivateKey:
    return _GATEWAY_KEY


@pytest.fixture()
def public_keys(gateway_key) -> dict:
    return {"gw-key-1": gateway_key.public_key()}


#: The operator's actual HTTP request body, verbatim, as the listener received it.
#:
#: Byte-exact matters twice over. The gateway's ``body_digest`` claim is sha256 of
#: these bytes, and the sentinel records them so the finalizer can supply the
#: preimage — so no test here may re-serialize the body between signing and
#: verifying. ``json.dumps`` followed by ``json.loads`` followed by ``json.dumps``
#: is not byte-preserving (key order, whitespace and number formatting all move),
#: which would fail the digest check for a reason unrelated to what was under test.
OPERATOR_BODY = json.dumps(
    {"command_id": COMMAND_ID, "reason": "wrong branch, stop before it pushes"},
    separators=(",", ":"),
).encode("utf-8")


def _digest(body: bytes) -> str:
    """The ``body_digest`` claim the gateway signs: sha256 hex of the raw bytes."""
    return hashlib.sha256(body).hexdigest()


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
        # A real digest of the real body, not a placeholder. It was ``"a" * 64``
        # while the verifier ignored the claim; once the claim is checked, a
        # placeholder would make every test in this file fail for the same
        # uninteresting reason and would leave the binding itself untested.
        "body_digest": _digest(OPERATOR_BODY),
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


def _mint_receipt(signer: Ed25519PrivateKey, **overrides) -> str:
    """Sign an abort *receipt* the way ``revalidation._accept_abort`` does — #3963.

    Same signer and same claim structure as an envelope, differing in ``aud`` and
    ``action``. That difference is the entire security property: the gateway mints a
    receipt only after the live re-check accepted the command *and* durable abort
    intent was persisted, so possessing one is evidence the abort was accepted rather
    than merely requested.

    ``principal`` follows the real minting path's ``<invocation>#<attempt>`` form.
    Nothing verifies it today, so it is here to keep the fixture honest rather than
    to satisfy a check.

    Overrides are merged into the defaults rather than passed alongside them as
    keywords, so a test may override ``aud`` or ``action`` themselves — which the
    audience/action-separation tests do. Passing them through as ``**overrides``
    beside explicit keywords raised ``TypeError: multiple values for 'aud'``, making
    those tests fail on a helper bug instead of asserting the refusal.
    """
    claims = {
        "aud": ABORT_RECEIPT_AUDIENCE,
        "action": ABORT_RECEIPT_ACTION,
        "principal": f"{RUN_ID}#1",
        **overrides,
    }
    return _mint(signer, **claims)


def _sentinel(
    envelope: object,
    *,
    command_id: str = COMMAND_ID,
    body: object = OPERATOR_BODY,
    receipt: object = None,
    signer: Ed25519PrivateKey | None = None,
) -> dict:
    """A sentinel carrying the envelope, the signed bytes, and the receipt.

    ``body`` defaults to the operator's genuine request, so the digest binding holds
    and each test below fails only for the reason it names. Pass a different value
    to attack that binding — which is what
    ``TestTheReasonIsBoundToTheSignature`` does.

    ``receipt`` defaults to a genuine one minted by ``signer`` (#3963 finding 1), so
    tests that are about some *other* property still describe a complete, acceptable
    sentinel. Pass ``_ABSENT`` to omit it or a value to attack it — which is what
    ``TestAcceptanceMustBeAttestedByTheGateway`` does.
    """
    document = {"command_id": command_id, "envelope": envelope}
    if receipt is None:
        # A genuine receipt from the real signer, bound to this command. Its
        # `body_digest` tracks the *envelope's* operator body rather than the
        # `body` argument, because the tests that vary `body` are attacking the
        # envelope's reason binding and must not be given a second, unrelated
        # failure reason from the receipt.
        receipt = _mint_receipt(signer or _GATEWAY_KEY, command_id=command_id)
    if receipt is not _ABSENT:
        document["abort_receipt"] = receipt
    if body is not _ABSENT:
        raw = body if isinstance(body, bytes) else str(body).encode("utf-8")
        document["signed_body_base64"] = base64.b64encode(raw).decode("ascii")
    return document


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

    @staticmethod
    def _document(**overrides) -> dict:
        """A stored sentinel as the writer produces it, for the real validator."""
        document = {
            "version": 1,
            "run_id": RUN_ID,
            "generation": GENERATION,
            "command_id": COMMAND_ID,
            "requested_at": "2026-09-24T12:00:00Z",
            "abort_receipt": _mint_receipt(_GATEWAY_KEY),
            "signed_body_base64": base64.b64encode(OPERATOR_BODY).decode("ascii"),
        }
        document.update(overrides)
        return document

    def test_a_validated_sentinel_carries_the_envelope_through_for_verification(
        self, gateway_key, public_keys
    ):
        token = _mint(gateway_key)

        validated = validate_abort_sentinel(self._document(envelope=token), RUN_ID, GENERATION)

        assert validated is not None
        assert validated["envelope"] == token
        # And the round trip actually verifies, which is what proves the two
        # halves compose rather than merely both existing. Note it is the
        # *validated* dict being verified, not the input: validation must preserve
        # the signed bytes byte-for-byte or the digest check downstream fails.
        assert _verify(validated, public_keys) is True
        # The composition that matters to an operator: the reason printed in the
        # closing comment comes out of the bytes the signature covers.
        assert authorized_abort_reason(validated) == "wrong branch, stop before it pushes"

    def test_validation_succeeds_on_a_sentinel_whose_authorization_will_fail(self):
        # The important asymmetry: a well-formed document with no proof is still a
        # well-formed document. It parses, and it is refused authorization.
        validated = validate_abort_sentinel(self._document(), RUN_ID, GENERATION)

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
        document = self._document(envelope=envelope)

        assert validate_abort_sentinel(document, RUN_ID, GENERATION)["envelope"] is None


class TestTheReasonIsBoundToTheSignature:
    """The regression this class exists for — Issue #3963, review finding 2.

    An earlier revision verified the envelope and then read the reason from a plain
    ``reason`` field of the same document. Both halves of that were defensible in
    isolation: the envelope genuinely proved an operator had authorized an abort of
    this run, and the reason was "just a label". Together they were a hole. The agent
    process runs with ``Bash``, so it can write the sentinel file; it cannot mint an
    envelope, but it never needed to — it could pair a *genuine* envelope with text of
    its own choosing, and the closing comment would attribute that text to the human
    named in the signature.

    A real-signature reproduction is what established this, not review by reading:
    with a live key, an abort authorized for one reason was published under another.
    The fix binds the two. The envelope's ``body_digest`` claim was always a sha256
    over the operator's exact request body; what was missing was the preimage, and the
    writer already had it. The sentinel now carries those bytes, the verifier requires
    them to hash to the signed claim, and the reason is parsed out of them rather than
    read from beside them.

    Every test below mints with a real key and requires ``False`` (or a refusal to
    derive) — a passing test here means substitution was rejected.
    """

    def test_a_substituted_reason_under_a_genuine_envelope_is_refused(
        self, gateway_key, public_keys
    ):
        # THE reproduction. The envelope is real, minted by the gateway's own key for
        # a genuine abort of this run and generation; every binding in it is correct
        # and it verifies on its own. Only the recorded body differs: the operator
        # asked to stop for one reason and the worker substituted another.
        #
        # Before the digest check this returned True and the fabricated sentence was
        # published as the operator's.
        fabricated = json.dumps(
            {"command_id": COMMAND_ID, "reason": "approved by the on-call engineer"},
            separators=(",", ":"),
        ).encode("utf-8")
        envelope = _mint(gateway_key)

        # The envelope alone is beyond reproach.
        assert _verify(_sentinel(envelope), public_keys) is True
        # The same envelope with substituted words is not.
        assert _verify(_sentinel(envelope, body=fabricated), public_keys) is False

    def test_even_a_one_byte_change_to_the_recorded_body_is_refused(self, gateway_key, public_keys):
        # sha256 is all-or-nothing, and the test says so explicitly: there is no
        # "close enough" body, so no partial-match or prefix comparison can creep in
        # here later and be mistaken for a check.
        tampered = OPERATOR_BODY.replace(b"wrong branch", b"wrong branci")
        assert tampered != OPERATOR_BODY
        assert len(tampered) == len(OPERATOR_BODY)
        # Still valid JSON, and still a plausible reason. That matters: an earlier
        # version of this test mangled the trailing bytes, so it was the JSON parser
        # that refused it and the test passed while the digest comparison could have
        # been absent entirely. Mutation-checked — deleting the digest check must make
        # this fail.
        assert json.loads(tampered)["reason"] == "wrong branci, stop before it pushes"

        assert _verify(_sentinel(_mint(gateway_key), body=tampered), public_keys) is False

    def test_adding_a_field_to_the_recorded_body_is_refused(self, gateway_key, public_keys):
        # Re-serializing the body is itself a change. The digest is over exact bytes,
        # so a writer that parsed and re-emitted the JSON — even preserving every
        # value — would break this check. That is deliberate: it is what forces the
        # writer to record the bytes off the socket verbatim rather than a
        # reconstruction of them.
        embellished = json.dumps(
            {
                "command_id": COMMAND_ID,
                "reason": "wrong branch, stop before it pushes",
                "actor": "someone-else",
            },
            separators=(",", ":"),
        ).encode("utf-8")

        assert _verify(_sentinel(_mint(gateway_key), body=embellished), public_keys) is False

    def test_reordering_the_recorded_body_is_refused(self, gateway_key, public_keys):
        # Semantically identical JSON, different bytes. Same reasoning as above, and
        # the case most likely to be "fixed" by someone comparing parsed objects
        # instead of bytes — which would reopen the hole, since an attacker controls
        # key order too.
        reordered = json.dumps(
            {"reason": "wrong branch, stop before it pushes", "command_id": COMMAND_ID},
            separators=(",", ":"),
        ).encode("utf-8")
        assert json.loads(reordered) == json.loads(OPERATOR_BODY)
        assert reordered != OPERATOR_BODY

        assert _verify(_sentinel(_mint(gateway_key), body=reordered), public_keys) is False

    @pytest.mark.parametrize(
        "body",
        [_ABSENT, b"", b"not json at all", b"[]", b'"a string"', b"null"],
        ids=["absent", "empty", "not_json", "array", "string", "json_null"],
    )
    def test_a_missing_or_unusable_recorded_body_is_refused(self, gateway_key, public_keys, body):
        # No preimage means no binding, and no binding means the reason is back to
        # being self-asserted. Refusing is the honest outcome: the abort still stopped
        # the run, it just does not get to publish a reason in the operator's name.
        assert _verify(_sentinel(_mint(gateway_key), body=body), public_keys) is False

    def test_a_body_naming_another_command_is_refused(self, gateway_key, public_keys):
        # The body carries its own `command_id`, and the envelope carries one too.
        # Without requiring them to agree, a genuine signed body from an earlier
        # command could be replayed as the preimage for a later one — the digest
        # would match the body it came from while authorizing a different abort.
        other_body = json.dumps(
            {"command_id": "cmd-abort-2", "reason": "wrong branch, stop before it pushes"},
            separators=(",", ":"),
        ).encode("utf-8")
        envelope = _mint(gateway_key, body_digest=_digest(other_body))

        assert _verify(_sentinel(envelope, body=other_body), public_keys) is False

    def test_the_derived_reason_is_the_operators_own_words(self, gateway_key, public_keys):
        # The positive side, so the negatives above cannot be satisfied by a function
        # that simply always refuses. What an operator sees in the closing comment is
        # what they typed, and it arrives having been covered by the signature.
        sentinel = _sentinel(_mint(gateway_key))

        assert _verify(sentinel, public_keys) is True
        assert authorized_abort_reason(sentinel) == "wrong branch, stop before it pushes"


class TestAcceptanceMustBeAttestedByTheGateway:
    """Issuance is not execution — Issue #3963 review finding 1.

    This is the class that exists because a previous revision got this wrong twice,
    and the distinction it defends is subtle enough to be worth stating plainly.

    An abort involves two separate facts:

    1. *An operator was authorized to abort this run.* The command **envelope** proves
       this. It is minted when the command is issued.
    2. *The gateway accepted that abort against a live run, having first recorded
       durable intent.* The **receipt** proves this, and only this fact justifies
       deleting the queue message and reporting a deliberate stop.

    Fact 1 does not imply fact 2. An envelope stays valid for its entire TTL whether
    or not the run ever took the command — including when the live re-check at
    delivery *refused* it, because the grant was revoked, the epoch moved or the
    operator's membership lapsed. A refused command leaves a perfectly valid envelope
    behind.

    The first attempt to close this gap had the writer record ``delivery: "accepted"``
    and had the reader require that string. That constrains nothing. The sentinel is a
    file at a fixed path, the agent runs with ``Bash``, and so any code in the pod can
    write eight characters beside a genuine envelope. Root's review named it exactly:
    restricting the honest writer does not constrain another process writing the file.

    So acceptance is now attested by the only party that can know it, with a signature
    the pod cannot produce. The tests below are the ones that would have caught the
    original defect: a real, structurally perfect issuance envelope, paired with every
    way a receipt can be missing or faked.
    """

    def test_a_genuine_envelope_without_a_receipt_proves_only_that_someone_asked(
        self, gateway_key, public_keys
    ):
        # THE finding-1 regression. Every binding on the envelope is correct — it is
        # minted by the gateway's own key, for this run, this generation, this command,
        # over the operator's real request body. It is a genuine authorization.
        #
        # It is still not evidence that this run accepted an abort, and a sentinel
        # carrying it alone must not finalize the run as aborted.
        envelope = _mint(gateway_key)

        assert _verify(_sentinel(envelope, receipt=_ABSENT), public_keys) is False

    def test_a_fabricated_delivery_literal_beside_a_genuine_envelope_is_refused(
        self, gateway_key, public_keys
    ):
        # The precise attack root described: valid issuance bytes plus a fabricated
        # delivery field and no actual live handoff. The pod can write the literal; it
        # cannot mint the receipt. Asserted explicitly so that reintroducing a
        # `delivery`-based shortcut fails here rather than silently re-opening the gap.
        document = _sentinel(_mint(gateway_key), receipt=_ABSENT)
        document["delivery"] = "accepted"

        assert _verify(document, public_keys) is False

    def test_a_receipt_the_pod_signed_itself_is_refused(self, gateway_key, public_keys):
        # The pod holds no signing key, but assume the worst: an attacker with their own
        # key mints a structurally perfect receipt. The signature must fail against the
        # gateway's published public key.
        attacker = Ed25519PrivateKey.generate()

        assert _verify(
            _sentinel(_mint(gateway_key), receipt=_mint_receipt(attacker)), public_keys
        ) is False

    def test_the_issuance_envelope_cannot_be_replayed_as_its_own_receipt(
        self, gateway_key, public_keys
    ):
        # Why the receipt carries a distinct audience AND action. Without that
        # separation the envelope — a real signature by the real key over a statement
        # about this very abort — would satisfy the receipt check by itself, and
        # "an operator asked" would again be indistinguishable from "the run accepted".
        envelope = _mint(gateway_key)

        assert _verify(_sentinel(envelope, receipt=envelope), public_keys) is False

    @pytest.mark.parametrize(
        "overrides",
        [
            {"aud": CONTROL_ENVELOPE_AUDIENCE},
            {"action": "abort"},
            {"action": "pause"},
        ],
        ids=["listener_audience", "abort_action", "pause_action"],
    )
    def test_a_receipt_must_carry_the_receipt_audience_and_action(
        self, gateway_key, public_keys, overrides
    ):
        # Each half of the separation, checked independently, so a future edit that
        # drops one of the two checks fails here. A token signed by the gateway with
        # the listener's audience is a real signature over a *different* statement.
        assert _verify(
            _sentinel(_mint(gateway_key), receipt=_mint_receipt(gateway_key, **overrides)),
            public_keys,
        ) is False

    @pytest.mark.parametrize(
        "overrides",
        [
            {"target_run_id": "run-someone-else"},
            {"target_generation": GENERATION + 1},
            {"command_id": "cmd-abort-2"},
        ],
        ids=["other_run", "superseded_generation", "other_command"],
    )
    def test_a_receipt_for_a_different_run_generation_or_command_is_refused(
        self, gateway_key, public_keys, overrides
    ):
        # A genuine receipt is still evidence about one specific acceptance. Without
        # these bindings, a real receipt from any other abort — an earlier attempt of
        # this run, or another run entirely — could be presented here. The bindings are
        # compared against values this process knows independently of the token.
        assert _verify(
            _sentinel(_mint(gateway_key), receipt=_mint_receipt(gateway_key, **overrides)),
            public_keys,
        ) is False

    @pytest.mark.parametrize(
        "receipt",
        [42, {"a": 1}, [], "", "not.a.token", "x" * (MAX_SENTINEL_ENVELOPE_BYTES + 1)],
        ids=["number", "dict", "list", "empty", "malformed", "oversized"],
    )
    def test_an_unusable_receipt_is_refused_rather_than_raising(
        self, gateway_key, public_keys, receipt
    ):
        # Runs on the teardown path, so a hostile value must produce a refusal rather
        # than an exception — an unhandled throw here would risk the SQS
        # acknowledgement, which is the one thing an abort must get right.
        assert _verify(_sentinel(_mint(gateway_key), receipt=receipt), public_keys) is False

    def test_both_tokens_together_are_what_prove_an_abort(self, gateway_key, public_keys):
        # The positive case, so none of the negatives above can be satisfied by a
        # function that always refuses. Envelope plus receipt — the operator's
        # authorization and the gateway's acceptance — and the reason still comes out of
        # the signed bytes.
        sentinel = _sentinel(_mint(gateway_key))

        assert _verify(sentinel, public_keys) is True
        assert authorized_abort_reason(sentinel) == "wrong branch, stop before it pushes"
