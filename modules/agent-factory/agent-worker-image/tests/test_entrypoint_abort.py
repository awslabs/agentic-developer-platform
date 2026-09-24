"""Terminal finalization of an aborted run — Issue #3963 (S4).

An operator's abort lands in the Node half of the run, but everything an operator
actually *sees* is written by this half: the closing comment, the invocation
status, the check-run conclusion, and the queue acknowledgement that decides
whether the work starts again. This file tests that seam.

Three properties carry the story, and each has a specific way of going wrong:

**The abort must be proven, not merely asserted.** The sentinel is a file in
``/tmp`` and the agent holds a ``Bash`` tool, so the document alone establishes
nothing. The tests here write sentinels the way code inside the pod would — right
fields, wrong signature, or no signature at all — and require the run to fall
back to exit-code classification. ``test_an_unsigned_sentinel_cannot_finalize_a_run_as_aborted``
is the one that matters most: were it ever to pass, a shell command could delete
a live run's queue message and report a crash as a deliberate stop. The envelopes
are signed with a real Ed25519 key using the gateway's exact canonicalization,
because a fake that skipped the signature would be testing nothing — the
signature *is* the mechanism.

**The exit code must not summon a replacement pod.** The ScaledJob runs with
``backoffLimit: 2``, so an abort exits 0 even though it is not a success. That in
turn is why the status, not the exit code, is what carries the outcome — and why
the writes that derive a status from ``exit_code`` later in ``main()`` each need
an abort branch.

**An unconfirmed acknowledgement is not a successful abort.** The ordinary path
swallows a failed SQS delete because the work is already on GitHub. For an abort
that reasoning inverts: a message still on the queue means the run is due to
start again, which is the one thing the abort existed to prevent. So the delete
is retried within a bound and an unconfirmed result is reported honestly — safe
only because the redelivery is then refused, which
``tests/test_invocation_completion.py`` covers against real conditional writes.
"""

from __future__ import annotations

import base64
import hashlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import boto3
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from moto import mock_aws

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import entrypoint  # noqa: E402
from lib import abort_sentinel  # noqa: E402
from lib import invocation_completion
from lib import invocation_status  # noqa: E402
from lib.abort_sentinel import (  # noqa: E402
    ABORT_RECEIPT_ACTION,
    ABORT_RECEIPT_AUDIENCE,
    CONTROL_ENVELOPE_AUDIENCE,
    ENVELOPE_ISSUER,
    ENVELOPE_VERSION,
    MAX_ENVELOPE_TTL_SECONDS,
)

RUN_ID = "msg-abort-1"
ARRIVED_AT = "2026-09-24T10:00:00Z"
GENERATION = 4
COMMAND_ID = "cmd-abort-7"
KID = "gw-key-1"

# Signed in the recent past, relative to the real clock, because
# `_resolve_abort_outcome` deliberately exposes no `now` seam: it is the
# production caller and the time it verifies against is the time the run actually
# ended. A fixed literal would have this suite pass or fail depending on the date.
#
# The past is also where a real abort's envelope always sits. Its `exp` is 30
# seconds after signing and finalization happens later than that, so these
# envelopes are expired by wall clock and still authorize the abort — the
# historical-fact semantics `verify_abort_authorization` documents at length, and
# which `test_an_expired_envelope_still_proves_the_abort_happened` pins.
SIGNED_AT = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(minutes=5)


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


#: The operator's real HTTP request body, as the listener received it.
#:
#: Byte-exact, and never re-serialized anywhere in this file: the gateway's
#: ``body_digest`` claim is sha256 over exactly these bytes, and the sentinel records
#: them so the finalizer can supply the preimage. The reason the closing comment
#: prints is parsed out of them, which is what makes it the operator's words rather
#: than a sibling field the agent's own shell could have written.
OPERATOR_BODY = json.dumps(
    {"command_id": COMMAND_ID, "reason": "wrong branch"}, separators=(",", ":")
).encode("utf-8")


def _signed_body_base64(body: bytes = OPERATOR_BODY) -> str:
    return base64.b64encode(body).decode("ascii")


def _mint(signer: Ed25519PrivateKey, **overrides) -> str:
    """Sign a control envelope exactly as ``src/agentauth/envelope.py`` does.

    The canonicalization is part of the contract: sorted keys, no whitespace, and
    the version prefixed to the signed bytes so a body cannot be replayed under a
    different envelope version.
    """
    payload = {
        "v": ENVELOPE_VERSION,
        "iss": ENVELOPE_ISSUER,
        "aud": CONTROL_ENVELOPE_AUDIENCE,
        "alg": "ed25519",
        "kid": KID,
        "tenant_id": "acme-corp",
        "principal": "user-1",
        "target_run_id": RUN_ID,
        "target_generation": GENERATION,
        "action": "abort",
        "command_id": COMMAND_ID,
        # A genuine digest over OPERATOR_BODY. It was a placeholder while the
        # verifier ignored the claim; the claim is now required to match the recorded
        # bytes, which is what binds the printed reason to the signature.
        "body_digest": hashlib.sha256(OPERATOR_BODY).hexdigest(),
        "iat": _iso(SIGNED_AT),
        "nbf": _iso(SIGNED_AT),
        "exp": _iso(SIGNED_AT + timedelta(seconds=MAX_ENVELOPE_TTL_SECONDS)),
    }
    payload.update(overrides)
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    signature = signer.sign(ENVELOPE_VERSION.encode("ascii") + b"." + body)
    return f"{ENVELOPE_VERSION}.{_b64(body)}.{_b64(signature)}"


def _pem(signer: Ed25519PrivateKey) -> str:
    return (
        signer.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode("ascii")
    )


# One signer for the module rather than one per test. Key generation is not what any
# test here exercises, and a module-level signer is what lets the helpers below mint a
# genuine acceptance receipt by default — without it every call site would have to
# thread the fixture through purely to produce a token none of them is testing.
_GATEWAY_KEY = Ed25519PrivateKey.generate()


@pytest.fixture()
def gateway_key(monkeypatch) -> Ed25519PrivateKey:
    """The gateway's signer, with only its public half projected into the pod.

    This asymmetry is the security property: the worker image has no signing path
    at all, so a valid envelope is the one artifact in the pod that could not have
    been manufactured inside it.
    """
    monkeypatch.setenv("ADP_CONTROL_ENVELOPE_KEYS", json.dumps({KID: _pem(_GATEWAY_KEY)}))
    return _GATEWAY_KEY


def _mint_receipt(signer: Ed25519PrivateKey, **overrides) -> str:
    """Sign an ACCEPTANCE receipt — Issue #3963 review finding 1.

    A distinct audience and action from the issuance envelope, deliberately. The two
    tokens answer different questions: the envelope says an operator was authorized to
    request this abort, the receipt says the gateway accepted it against the live run
    after durable abort intent was persisted. Sharing an audience would let an
    issuance envelope be replayed as its own acceptance proof, which is precisely the
    hole the removed ``delivery: "accepted"`` literal left open.
    """
    claims = {
        "aud": ABORT_RECEIPT_AUDIENCE,
        "action": ABORT_RECEIPT_ACTION,
        **overrides,
    }
    return _mint(signer, **claims)


class TestResolvingTheAbortOutcome:
    """``_resolve_abort_outcome``: well-formed AND authorized, or no abort."""

    def _resolve(
        self,
        monkeypatch,
        tmp_path,
        *,
        registered=True,
        registered_generation=GENERATION,
        **document,
    ):
        """Resolve against a sentinel on disk, with the generation this pod holds.

        ``registered_generation`` stands in for the value ``_setup_agent_control``
        captured from the invocation row's atomic increment. It is deliberately
        separate from the document's own ``generation`` field so the two can be
        made to disagree.
        """
        payload = {
            "version": 1,
            "run_id": RUN_ID,
            "generation": GENERATION,
            "command_id": COMMAND_ID,
            "requested_at": _iso(SIGNED_AT),
            # `abort_receipt` is the gateway's signed attestation that it ACCEPTED
            # this abort against the live run, not merely that an envelope was once
            # issued; `signed_body_base64` is the preimage of the signed digest. The
            # reason is NOT a field here — it is derived from those bytes — because an
            # unsigned `reason` beside the envelope is what allowed fabricated text to
            # be attributed to a human.
            "abort_receipt": _mint_receipt(_GATEWAY_KEY),
            "signed_body_base64": _signed_body_base64(),
        }
        payload.update(document)
        sentinel_path = tmp_path / "abort.json"
        sentinel_path.write_text(json.dumps(payload), encoding="utf-8")

        monkeypatch.setattr(entrypoint, "_registered_control_generation", registered_generation)
        monkeypatch.setattr(
            entrypoint,
            "read_abort_sentinel",
            lambda run_id, generation: abort_sentinel.read_abort_sentinel(
                run_id, generation, path=str(sentinel_path)
            ),
        )
        return entrypoint._resolve_abort_outcome(RUN_ID, registered)

    def test_a_gateway_signed_abort_resolves(self, monkeypatch, tmp_path, gateway_key):
        outcome = self._resolve(monkeypatch, tmp_path, envelope=_mint(gateway_key))

        assert outcome is not None
        assert outcome["command_id"] == COMMAND_ID
        # Derived from the signed bytes by the resolver, which is the only place the
        # operator's words enter this process. Downstream consumers — the closing
        # comment and the check-run summary — read this key.
        assert outcome["reason"] == "wrong branch"

    def test_a_worker_cannot_substitute_the_reason_under_a_genuine_envelope(
        self, monkeypatch, tmp_path, gateway_key
    ):
        # The review finding, at the boundary where it did damage. The envelope is
        # real and authorizes an abort of this run; only the recorded body differs
        # from what the operator submitted. Previously the resolver verified the
        # envelope and then read the reason from a plain field, so this published the
        # worker's sentence over the human's name.
        #
        # Refusing the whole abort — rather than resolving it with no reason — is the
        # deliberate choice: a document whose recorded body does not match the signed
        # digest is not a document this process understands, and the run still stops
        # either way because cancellation is unconditional in the agent half.
        fabricated = json.dumps(
            {"command_id": COMMAND_ID, "reason": "cleared by the release manager"},
            separators=(",", ":"),
        ).encode("utf-8")

        outcome = self._resolve(
            monkeypatch,
            tmp_path,
            envelope=_mint(gateway_key),
            signed_body_base64=_signed_body_base64(fabricated),
        )

        assert outcome is None

    def test_a_sentinel_the_gateway_never_accepted_does_not_resolve(
        self, monkeypatch, tmp_path, gateway_key
    ):
        # A signed envelope proves the gateway *issued* an authorization; it does not
        # prove the authorization survived the live recheck immediately before the
        # executor ran. `deliverAuthorized` settles the command `rejected` when the
        # grant was revoked, the epoch moved or the operator's membership lapsed —
        # and leaves the perfectly valid envelope behind. Honouring it would finalize
        # an abort the platform had explicitly just denied.
        #
        # This used to be expressed as `delivery="rejected"`, i.e. by asking the pod
        # to admit the refusal. Review finding 1: a pod that wants to fake an abort
        # simply does not write that. So the absence tested here is the absence of the
        # gateway's own acceptance token, which no code in the pod can produce.
        assert (
            self._resolve(
                monkeypatch, tmp_path, envelope=_mint(gateway_key), abort_receipt=None
            )
            is None
        )

    def test_an_issuance_envelope_replayed_as_its_own_receipt_does_not_resolve(
        self, monkeypatch, tmp_path, gateway_key
    ):
        # The envelope is gateway-signed and genuine, so signature verification alone
        # would pass it in both slots. It is refused because the receipt slot requires
        # the receipt audience and action: issuance and acceptance are separate facts,
        # and one token must not be able to certify both.
        envelope = _mint(gateway_key)
        assert (
            self._resolve(
                monkeypatch, tmp_path, envelope=envelope, abort_receipt=envelope
            )
            is None
        )

    def test_a_receipt_the_pod_signed_itself_does_not_resolve(
        self, monkeypatch, tmp_path, gateway_key
    ):
        # The threat model for the whole sentinel: the agent holds a `Bash` tool, so it
        # can write this file and could generate its own keypair. What it cannot do is
        # produce a signature the gateway's published public key verifies.
        assert (
            self._resolve(
                monkeypatch,
                tmp_path,
                envelope=_mint(gateway_key),
                abort_receipt=_mint_receipt(Ed25519PrivateKey.generate()),
            )
            is None
        )

    def test_an_unsigned_sentinel_cannot_finalize_a_run_as_aborted(
        self, monkeypatch, tmp_path, gateway_key
    ):
        # The whole threat model in one test. This document is exactly what a
        # `Bash` tool can produce: every field correct, no proof offered. If it
        # resolved, any code in the pod could delete a live run's queue message
        # and have the platform report a crash as a deliberate stop.
        assert self._resolve(monkeypatch, tmp_path) is None

    def test_a_sentinel_signed_by_a_key_the_pod_could_hold_does_not_resolve(
        self, monkeypatch, tmp_path, gateway_key
    ):
        # A well-formed envelope over correct claims, signed by the wrong key.
        # Structure is not authority; only the gateway's signature is.
        forged = _mint(Ed25519PrivateKey.generate())

        assert self._resolve(monkeypatch, tmp_path, envelope=forged) is None

    def test_an_envelope_for_a_different_action_is_not_an_abort(
        self, monkeypatch, tmp_path, gateway_key
    ):
        # Genuinely signed, genuinely for this run — and an authorization for
        # something else entirely. Without the action check, an operator's pause
        # could be replayed as an abort.
        assert (
            self._resolve(monkeypatch, tmp_path, envelope=_mint(gateway_key, action="pause"))
            is None
        )

    def test_an_envelope_for_a_different_run_is_not_this_run_s_abort(
        self, monkeypatch, tmp_path, gateway_key
    ):
        other_run = _mint(gateway_key, target_run_id="msg-somebody-else")

        assert self._resolve(monkeypatch, tmp_path, envelope=other_run) is None

    def test_a_superseded_generation_cannot_finalize_its_replacement(
        self, monkeypatch, tmp_path, gateway_key
    ):
        # The retry-pod case: an abort was authorized for generation 3, then that
        # attempt ended and this pod registered as generation 4. Honouring the
        # older authorization would finalize the attempt that *replaced* the one
        # the operator actually stopped.
        stale = _mint(gateway_key, target_generation=GENERATION - 1)

        assert self._resolve(monkeypatch, tmp_path, envelope=stale) is None

    def test_an_envelope_paired_with_another_command_s_record_is_refused(
        self, monkeypatch, tmp_path, gateway_key
    ):
        # Binds proof to record. Otherwise one valid envelope could be attached to
        # any sentinel document.
        assert (
            self._resolve(
                monkeypatch,
                tmp_path,
                command_id="cmd-something-else",
                envelope=_mint(gateway_key),
            )
            is None
        )

    def test_an_unauthorized_sentinel_is_reported_loudly(
        self, monkeypatch, tmp_path, gateway_key, caplog
    ):
        # A silent fallback is indistinguishable from "no abort was requested",
        # and those two cases deserve very different operator attention: one is
        # routine, the other is a broken handoff or an attempted forgery.
        with caplog.at_level("ERROR", logger="entrypoint"):
            self._resolve(monkeypatch, tmp_path, envelope=_mint(Ed25519PrivateKey.generate()))

        assert "abort_unauthorized" in caplog.text

    def test_a_run_that_never_registered_control_has_no_abort_to_resolve(
        self, monkeypatch, tmp_path, gateway_key
    ):
        # A flag-off run performs no control registration, so it has no generation
        # for a sentinel to be bound to — and an unbound sentinel is precisely
        # what must never be honoured. Checked before any file is read.
        assert (
            self._resolve(monkeypatch, tmp_path, registered=False, envelope=_mint(gateway_key))
            is None
        )

    def test_a_missing_generation_refuses_rather_than_guessing(
        self, monkeypatch, tmp_path, gateway_key
    ):
        assert (
            self._resolve(
                monkeypatch, tmp_path, registered_generation=None, envelope=_mint(gateway_key)
            )
            is None
        )

    def test_the_generation_does_not_come_from_the_environment(
        self, monkeypatch, tmp_path, gateway_key
    ):
        # `ADP_CONTROL_GENERATION` is placed only in the *child's* env. Sourcing
        # the generation from anywhere the agent can write would let the pod
        # choose which generation its own sentinel is validated against, which is
        # the entire staleness defence the binding provides. With the registered
        # value absent, a correct env var must not rescue the abort.
        monkeypatch.setenv("ADP_CONTROL_GENERATION", str(GENERATION))

        assert (
            self._resolve(
                monkeypatch, tmp_path, registered_generation=None, envelope=_mint(gateway_key)
            )
            is None
        )

    def test_an_expired_envelope_still_proves_the_abort_happened(
        self, monkeypatch, tmp_path, gateway_key
    ):
        # Not an oversight — the load-bearing design decision. The envelope lives
        # 30 seconds and finalization runs after the agent winds down, routinely
        # longer than that, so an expiry check here would reject every real abort
        # and make the feature inert. What is established at teardown is an
        # authenticated *historical* fact ("the gateway authorized this abort for
        # this run and generation"), not a live grant; liveness was enforced by the
        # listener at admission, within the window.
        long_expired = SIGNED_AT - timedelta(days=2)
        envelope = _mint(
            gateway_key,
            iat=_iso(long_expired),
            nbf=_iso(long_expired),
            exp=_iso(long_expired + timedelta(seconds=MAX_ENVELOPE_TTL_SECONDS)),
        )

        assert self._resolve(monkeypatch, tmp_path, envelope=envelope) is not None

    def test_an_authorization_not_yet_valid_cannot_describe_this_run(
        self, monkeypatch, tmp_path, gateway_key
    ):
        # The bound that survives dropping the expiry check. A proof that had not
        # come into force by the time the run ended cannot be a record of the run
        # ending, so `nbf` is still enforced against the clock.
        future = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(hours=1)
        envelope = _mint(
            gateway_key,
            iat=_iso(future),
            nbf=_iso(future),
            exp=_iso(future + timedelta(seconds=MAX_ENVELOPE_TTL_SECONDS)),
        )

        assert self._resolve(monkeypatch, tmp_path, envelope=envelope) is None

    def test_a_signer_cannot_claim_a_longer_life_than_the_platform_allows(
        self, monkeypatch, tmp_path, gateway_key
    ):
        # The TTL *bound* outlives the expiry check. Dropping expiry checking
        # without it would let a signed envelope assert an arbitrarily long life
        # and so overrule the platform's revocation-delay guarantee. Rejected
        # outright rather than silently truncated.
        overlong = _mint(
            gateway_key,
            exp=_iso(SIGNED_AT + timedelta(seconds=MAX_ENVELOPE_TTL_SECONDS + 1)),
        )

        assert self._resolve(monkeypatch, tmp_path, envelope=overlong) is None

    def test_no_keys_projected_means_the_abort_is_unproven(self, monkeypatch, tmp_path):
        # Fail closed, the direction the listener also takes: "cannot check" must
        # mean "refuse", never "allow".
        monkeypatch.delenv("ADP_CONTROL_ENVELOPE_KEYS", raising=False)
        monkeypatch.delenv("ADP_CONTROL_ENVELOPE_KEYS_FILE", raising=False)

        assert (
            self._resolve(monkeypatch, tmp_path, envelope=_mint(Ed25519PrivateKey.generate()))
            is None
        )

    def test_resolution_never_raises(self, monkeypatch):
        # Runs during teardown, where an escaping exception would cost the SQS
        # acknowledgement and strand the message.
        monkeypatch.setattr(entrypoint, "_registered_control_generation", GENERATION)
        monkeypatch.setattr(
            entrypoint, "read_abort_sentinel", MagicMock(side_effect=RuntimeError("disk on fire"))
        )

        assert entrypoint._resolve_abort_outcome(RUN_ID, True) is None


class TestTheAbortedTerminalReport:
    """``_handle_abort``: one comment, one status, and an exit code that stops."""

    def _finalize(self, monkeypatch, sentinel, *, persisted=True):
        posted, statuses = [], []
        monkeypatch.setattr(
            entrypoint,
            "_post_comment",
            lambda repo, issue, mid, status, body, url="": posted.append((status, body)),
        )

        def write(mid, arrived, status, **kw):
            statuses.append((status, kw))
            # The real writer's return value: True only when the row was observed to
            # land. Tests that care about the unpersisted world pass persisted=False.
            return persisted

        monkeypatch.setattr(entrypoint, "update_invocation_status", write)
        code, terminal_persisted = entrypoint._handle_abort(
            "acme/app", 42, "developer", RUN_ID, ARRIVED_AT, sentinel
        )
        return code, posted, statuses, terminal_persisted

    def test_one_comment_and_one_aborted_status(self, monkeypatch):
        code, posted, statuses, _ = self._finalize(monkeypatch, {"reason": "wrong branch"})

        assert len(posted) == 1
        assert posted[0][0] == "aborted"
        assert "aborted by an operator" in posted[0][1]
        assert [status for status, _ in statuses] == ["aborted"]
        assert statuses[0][1]["stop_reason"] == "operator_aborted"
        assert code == 0

    def test_the_exit_code_does_not_invite_a_replacement_pod(self, monkeypatch):
        # The ScaledJob runs with `backoffLimit: 2` and `restartPolicy: Never`, so
        # a non-zero exit here would launch a fresh pod for a run an operator
        # deliberately stopped — the precise opposite of the request. Kubernetes
        # reads this number; the dashboard reads the `aborted` status.
        code, _, _, _ = self._finalize(monkeypatch, {"reason": None})

        assert code == 0

    def test_an_absent_reason_produces_no_empty_quote_block(self, monkeypatch):
        _, posted, _, _ = self._finalize(monkeypatch, {"reason": None})

        assert "Reason given" not in posted[0][1]

    def test_an_operator_reason_cannot_forge_comment_structure(self, monkeypatch):
        # Operator-supplied text inside a comment attributed to the platform. It
        # is bounded, but bounded text can still contain markdown, so it stays
        # inside a fenced block where it cannot append a heading or a fake status
        # line of its own.
        hostile = "see ``` ## Merged by platform"
        _, posted, _, _ = self._finalize(monkeypatch, {"reason": hostile})
        body = posted[0][1]

        assert "> ```" in body
        for line in body.splitlines():
            if hostile in line:
                assert line.startswith("> ")

    def test_the_status_it_writes_is_one_the_writer_accepts(self):
        from lib import invocation_status

        # Otherwise the abort's own terminal write is the single value the status
        # writer silently refuses, and the row keeps whatever it said before —
        # leaving the run readable as in-progress forever.
        assert "aborted" in invocation_status.ALLOWED_WRITE_STATUSES


class TestConfirmedAcknowledgement:
    """``_acknowledge_abort``: bounded retry, and an honest unconfirmed answer."""

    @pytest.fixture(autouse=True)
    def no_real_sleep(self, monkeypatch):
        monkeypatch.setattr(entrypoint.time, "sleep", lambda _: None)

    def test_a_successful_delete_is_confirmed(self, monkeypatch):
        delete = MagicMock()
        monkeypatch.setattr(entrypoint, "_delete_message", delete)

        assert entrypoint._acknowledge_abort("q", "us-east-1", "receipt") is True
        assert delete.call_count == 1

    def test_a_transient_failure_is_retried_and_then_confirmed(self, monkeypatch):
        # The case the retry exists for: one throttled call must not turn into a
        # redelivered abort.
        delete = MagicMock(side_effect=[RuntimeError("throttled"), None])
        monkeypatch.setattr(entrypoint, "_delete_message", delete)

        assert entrypoint._acknowledge_abort("q", "us-east-1", "receipt") is True
        assert delete.call_count == 2

    def test_retries_are_bounded(self, monkeypatch):
        # Unbounded retry would hold the FIFO message group for the pod's whole
        # lifetime, blocking every later trigger on the same issue behind a run
        # that has already finished.
        delete = MagicMock(side_effect=RuntimeError("still down"))
        monkeypatch.setattr(entrypoint, "_delete_message", delete)

        assert entrypoint._acknowledge_abort("q", "us-east-1", "receipt") is False
        assert delete.call_count == entrypoint.ABORT_ACK_ATTEMPTS

    def test_an_unconfirmed_acknowledgement_is_not_reported_as_success(self, monkeypatch):
        # The property the story names explicitly. Returning True here would tell
        # an operator the run was stopped while its message sits on the queue,
        # ready to start it again.
        monkeypatch.setattr(
            entrypoint, "_delete_message", MagicMock(side_effect=RuntimeError("down"))
        )

        assert entrypoint._acknowledge_abort("q", "us-east-1", "receipt") is False

    def test_an_unconfirmed_acknowledgement_says_why_it_is_safe(self, monkeypatch, caplog):
        # An operator reading this line needs to know both facts: the message may
        # redeliver, and the redelivery will be refused. Either alone is alarming
        # or misleading.
        monkeypatch.setattr(
            entrypoint, "_delete_message", MagicMock(side_effect=RuntimeError("down"))
        )
        with caplog.at_level("ERROR", logger="entrypoint"):
            entrypoint._acknowledge_abort("q", "us-east-1", "receipt")

        assert "redeliver" in caplog.text
        assert "aborted" in caplog.text


class TestTheTerminalStatusIsRetried:
    """``_persist_abort_terminal_status``: bounded retry — #3963 review finding 3.

    ``update_invocation_status`` is fail-soft; it logs and returns ``False`` rather
    than raising. So a single call put the terminal ``aborted`` row one transient
    DynamoDB or gateway blip away from never existing, and that row is what
    ``is_delivery_completed`` reads to refuse a redelivery and what the dashboard
    shows the operator who asked for the stop.

    The bound matters as much as the retry. These attempts run *before* the
    DeleteMessage that actually prevents a rerun, so a long sequence here would delay
    the one write that matters most.
    """

    @pytest.fixture(autouse=True)
    def no_real_sleep(self, monkeypatch):
        monkeypatch.setattr(entrypoint.time, "sleep", lambda _: None)

    def test_a_write_that_lands_first_time_is_not_repeated(self, monkeypatch):
        write = MagicMock(return_value=True)
        monkeypatch.setattr(entrypoint, "update_invocation_status", write)

        assert entrypoint._persist_abort_terminal_status("msg-1", "t", "summary") is True
        assert write.call_count == 1

    def test_a_transient_failure_is_retried_and_then_persists(self, monkeypatch):
        # The case the retry exists for. Before this, one `False` meant the abort had
        # no terminal row at all.
        write = MagicMock(side_effect=[False, True])
        monkeypatch.setattr(entrypoint, "update_invocation_status", write)

        assert entrypoint._persist_abort_terminal_status("msg-1", "t", "summary") is True
        assert write.call_count == 2

    def test_retries_are_bounded_and_report_failure_honestly(self, monkeypatch):
        # Still `False` when every attempt fails — the narrowing must not become a
        # claim. `_finalize_abort_acknowledgement` decides the consequence, and it can
        # only do that if it is told the truth.
        write = MagicMock(return_value=False)
        monkeypatch.setattr(entrypoint, "update_invocation_status", write)

        assert entrypoint._persist_abort_terminal_status("msg-1", "t", "summary") is False
        assert write.call_count == entrypoint.ABORT_TERMINAL_WRITE_ATTEMPTS

    def test_every_attempt_writes_the_same_terminal_outcome(self, monkeypatch):
        # Idempotence, which is what makes retrying safe after an ambiguous failure: a
        # retry must not be able to produce a second or differently-worded outcome.
        write = MagicMock(side_effect=[False, False, True])
        monkeypatch.setattr(entrypoint, "update_invocation_status", write)

        entrypoint._persist_abort_terminal_status("msg-1", "2026-09-24T10:00:00Z", "summary")

        assert {call.args for call in write.call_args_list} == {
            ("msg-1", "2026-09-24T10:00:00Z", "aborted")
        }
        assert all(
            call.kwargs["stop_reason"] == "operator_aborted" for call in write.call_args_list
        )

    def test_the_status_written_is_the_one_the_guard_reads(self, monkeypatch):
        # Binds this writer to the legacy redelivery guard's contract rather than to
        # the string "aborted" appearing in two places by coincidence.
        write = MagicMock(return_value=True)
        monkeypatch.setattr(entrypoint, "update_invocation_status", write)

        entrypoint._persist_abort_terminal_status("msg-1", "t", "summary")

        assert write.call_args.args[2] == invocation_completion.ABORTED_STATUS


class TestTheAbortReachesTheEndOfTheRun:
    """The wiring in ``main()``, driven end to end.

    Unit-testing the handlers is not enough here. Every defect this class targets
    lives in the *ordering* of writes inside ``main()``: three separate writes
    after ``_handle_abort`` derive an outcome from ``exit_code``, which is 0 for an
    abort, so each one would quietly restate the run as a success. Nothing about
    the handlers in isolation reveals that.
    """

    @pytest.fixture()
    def run(self, monkeypatch, tmp_path, gateway_key):
        """Drive the real ``main()`` with a genuinely authorized abort in place.

        Only the run's edges are stubbed — SQS, GitHub, the transcript upload and
        the Node subprocess. The terminal sequence under test is the real one.
        """
        from lib import run_report

        envelope = {
            "version": "1.0",
            "channel": "github",
            "tenant_id": "acme-corp",
            "persona": "developer",
            "message_id": RUN_ID,
            "arrived_at": ARRIVED_AT,
            "source_ref": {"installation_id": 123, "repo": "acme/app", "issue": 42},
            "actor": {"user_id": "user-1", "github_login": "operator", "is_bot": False},
            "intent": {"trigger": "issue_labeled", "label": "developer"},
        }

        monkeypatch.setattr(entrypoint.os, "environ", dict(entrypoint.os.environ))
        monkeypatch.setenv("QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/1/agent.fifo")
        monkeypatch.setenv("AWS_REGION", "us-east-1")
        monkeypatch.setenv("ADP_GH_TOKEN_BROKER_ENABLED", "0")
        monkeypatch.setenv("ADP_AGENT_AUTHORITY_ENABLED", "false")
        monkeypatch.setattr(entrypoint, "WORK_DIR", tmp_path / "repo")
        (tmp_path / "repo").mkdir()
        monkeypatch.setattr(entrypoint, "PERSONAS_DIR", tmp_path / "personas")
        monkeypatch.setattr(entrypoint, "SKILLS_DIR", tmp_path / "skills")

        for name in (
            "_load_door_api_key",
            "_stop_sigv4_proxy",
            "_start_sigv4_proxy",
            "_teardown_agent_control",
            "_record_session_id",
            "_is_already_completed",
            "is_delivery_completed",
            "record_delivery_completed",
        ):
            monkeypatch.setattr(entrypoint, name, MagicMock(return_value=False))
        monkeypatch.setattr(entrypoint, "BootstrapLogger", MagicMock())
        monkeypatch.setattr(entrypoint, "VisibilityHeartbeat", MagicMock())
        monkeypatch.setattr(entrypoint, "_read_run_reports", lambda: ("", ""))
        monkeypatch.setattr(entrypoint, "_read_result_metadata", lambda: {})
        monkeypatch.setattr(entrypoint, "_upload_transcript_to_s3", lambda *a, **k: "t/key.md")
        monkeypatch.setattr(entrypoint.shutil, "copytree", MagicMock())
        monkeypatch.setattr(
            entrypoint,
            "VaultClient",
            MagicMock(
                return_value=MagicMock(
                    get_secret=MagicMock(return_value={"app_id": "1", "private_key": "k"})
                )
            ),
        )
        monkeypatch.setattr(entrypoint, "mint_installation_token", MagicMock(return_value="tok"))
        monkeypatch.setattr(
            entrypoint,
            "create_check_run",
            MagicMock(return_value={"id": 1, "html_url": "https://example.test/c"}),
        )
        # A resolvable head SHA, because the check run is only created when one
        # exists — and the check-run conclusion is one of the outcomes under test.
        monkeypatch.setattr(
            entrypoint, "run_cmd", MagicMock(return_value=MagicMock(stdout="a" * 40, returncode=0))
        )
        # Not a handoff resume: that path returns before the terminal sequence.
        monkeypatch.setattr(entrypoint, "resume_pr_handoff", lambda: False)
        monkeypatch.setattr(
            entrypoint, "_receive_one_message", lambda *_: (json.dumps(envelope), "receipt")
        )

        # The control registration this pod performed: the generation the
        # invocation row's atomic increment returned, which is the value the
        # sentinel must be bound to.
        registered = {"value": GENERATION}

        def setup_control(agent_env, message_id, arrived_at):
            entrypoint._registered_control_generation = registered["value"]
            return registered["value"] is not None

        monkeypatch.setattr(entrypoint, "_setup_agent_control", setup_control)

        # A sentinel written where the Node worker would write it, carrying the
        # gateway's signature over this exact run and generation.
        sentinel_path = tmp_path / "abort.json"
        signer = {"key": gateway_key}
        document = {
            "version": 1,
            "run_id": RUN_ID,
            "generation": GENERATION,
            "command_id": COMMAND_ID,
            "requested_at": _iso(SIGNED_AT),
            "abort_receipt": _mint_receipt(_GATEWAY_KEY),
            # The bytes the gateway signed. The reason the closing comment shows is
            # derived from these, not from a `reason` field on the document — that
            # field is gone, because an unsigned copy of the operator's words beside
            # the envelope is what let a worker substitute its own text and have it
            # attributed to the human who authorized the abort.
            "signed_body_base64": _signed_body_base64(),
        }

        def write_sentinel():
            document["envelope"] = _mint(signer["key"])
            sentinel_path.write_text(json.dumps(document), encoding="utf-8")

        monkeypatch.setattr(
            entrypoint,
            "read_abort_sentinel",
            lambda run_id, generation: abort_sentinel.read_abort_sentinel(
                run_id, generation, path=str(sentinel_path)
            ),
        )

        # A cancelled run exits non-zero: the typed cancellation propagates out of
        # the Node worker. This is what makes the resolution order load-bearing.
        agent_exit = {"code": 1}

        def subprocess_run(command, **kwargs):
            if command[0] == "node":
                return MagicMock(returncode=agent_exit["code"], stdout="", stderr="")
            return MagicMock(
                returncode=2 if command[:2] == ["git", "ls-remote"] else 0, stdout="", stderr=""
            )

        monkeypatch.setattr(entrypoint.subprocess, "run", subprocess_run)

        events, statuses, checks = [], [], []
        monkeypatch.setattr(
            entrypoint,
            "_post_comment",
            lambda repo, issue, mid, status, body, url="": events.append(("comment", status)),
        )

        def record_status(mid, arrived, status, **kw):
            events.append(("status", status))
            statuses.append((status, kw))

        monkeypatch.setattr(entrypoint, "update_invocation_status", record_status)
        monkeypatch.setattr(
            entrypoint,
            "update_check_run",
            lambda *a, **kw: checks.append(kw) or events.append(("check", kw.get("conclusion"))),
        )

        # Stand-ins that do what the real handlers do to the operator's view: post
        # a comment and write a terminal status. Inert mocks would make the
        # "exactly one terminal handler" assertion unfalsifiable — a run that
        # called both would look identical to one that called only the abort.
        def handle_success(*args, **kwargs):
            events.append(("comment", "complete"))
            record_status(RUN_ID, ARRIVED_AT, "complete")
            return 0

        def handle_failure(*args, **kwargs):
            events.append(("comment", "failed"))
            record_status(RUN_ID, ARRIVED_AT, "failed")
            return 1

        monkeypatch.setattr(entrypoint, "_handle_success", MagicMock(side_effect=handle_success))
        monkeypatch.setattr(entrypoint, "_handle_failure", MagicMock(side_effect=handle_failure))
        acks = MagicMock()
        monkeypatch.setattr(entrypoint, "_delete_message", acks)
        monkeypatch.setattr(entrypoint.time, "sleep", lambda _: None)

        reports = []
        monkeypatch.setattr(run_report, "enabled", lambda: False)
        monkeypatch.setattr(run_report, "terminal", lambda outcome: reports.append(outcome))
        monkeypatch.setattr(run_report, "spool_undelivered_failure", lambda: None)
        monkeypatch.setattr(run_report, "begin_delivery", lambda: None)
        monkeypatch.setattr(run_report, "configure", lambda envelope: None)

        write_sentinel()
        return {
            "events": events,
            "statuses": statuses,
            "checks": checks,
            "acks": acks,
            "reports": reports,
            "agent_exit": agent_exit,
            "registered": registered,
            "signer": signer,
            "document": document,
            "sentinel_path": sentinel_path,
            "write_sentinel": write_sentinel,
            "run_report": run_report,
            "monkeypatch": monkeypatch,
        }

    def test_an_aborted_run_reports_aborted_and_not_its_exit_code(self, run):
        # The headline behaviour. The agent exited 1 because it was cancelled, and
        # the run must still be reported as the deliberate stop it was.
        assert entrypoint.main() == 0

        assert ("comment", "aborted") in run["events"]
        entrypoint._handle_failure.assert_not_called()
        entrypoint._handle_success.assert_not_called()

    def test_exactly_one_terminal_handler_runs(self, run):
        # Two comments on one issue is the visible symptom of a missed `elif`:
        # the operator sees both "aborted" and "failed with exit code 1" and has
        # no way to tell which is the truth.
        entrypoint.main()

        assert [kind for kind, _ in run["events"]].count("comment") == 1

    def test_the_final_status_is_aborted_after_every_later_write(self, run):
        # The transcript write is unconditional and derives its status from
        # `exit_code`. Without its abort branch the *last* status on the row would
        # be `complete` — established, then silently undone a few lines later.
        entrypoint.main()

        statuses = [status for kind, status in run["events"] if kind == "status"]

        assert statuses[-1] == "aborted"
        # `in_progress` at the start is the pod announcing itself, long before the
        # abort. What must not appear is a *terminal* status other than `aborted`:
        # that would mean the run ended up recorded as something else.
        assert not ({"complete", "failed", "budget_stopped"} & set(statuses))

    def test_the_abort_reason_survives_the_transcript_write(self, run):
        # A row reading `aborted` with no explanation is a worse outcome than no
        # abort handling at all: the operator cannot tell their own abort from a
        # mystery stop.
        entrypoint.main()

        aborted_writes = [kw for status, kw in run["statuses"] if status == "aborted"]
        assert aborted_writes
        assert all(kw.get("stop_reason") == "operator_aborted" for kw in aborted_writes)

    def test_the_check_run_is_cancelled_not_successful(self, run):
        # `exit_code` is 0 for an abort, so the `exit_code == 0 -> success` branch
        # would show a green check on a run an operator stopped. `cancelled` is
        # GitHub's own vocabulary for exactly this state.
        entrypoint.main()

        assert run["checks"], "the check run was never finalized"
        assert run["checks"][-1]["conclusion"] == "cancelled"

    def test_the_engine_is_never_told_an_aborted_story_completed(self, run):
        # The engine's terminal vocabulary is binary, and `complete` would advance
        # a workflow on deliberately stopped work — the one direction that cannot
        # be undone from here.
        run["monkeypatch"].setattr(run["run_report"], "enabled", lambda: True)

        entrypoint.main()

        assert run["reports"] == ["failed"]

    def test_a_confirmed_acknowledgement_reports_the_abort_as_handled(self, run):
        assert entrypoint.main() == 0

        run["acks"].assert_called_once()

    def test_an_unconfirmed_acknowledgement_does_not_report_success(self, run):
        # The honest outcome. The operator-facing record is already written, but
        # this pod did not finish handling the message, and saying otherwise would
        # strand a message nobody is accountable for.
        run["acks"].side_effect = RuntimeError("SQS unavailable")

        assert entrypoint.main() == entrypoint.AGENT_EXIT_RETRYABLE

        assert run["acks"].call_count == entrypoint.ABORT_ACK_ATTEMPTS
        # The abort itself still reached the operator, and the row is terminal —
        # which is what makes the redelivery refusable rather than a rerun.
        assert ("comment", "aborted") in run["events"]
        assert [status for kind, status in run["events"] if kind == "status"][-1] == "aborted"

    def test_an_unsigned_sentinel_falls_back_to_the_exit_code(self, run):
        # End-to-end form of the forgery defence: a run that merely *claims* to
        # have been aborted is reported as the failure it actually was.
        # The reader's own refusal, reproduced rather than stubbed: an unsigned
        # document parses into a sentinel whose `envelope` is None, which
        # `verify_abort_authorization` then declines.
        run["document"].pop("envelope", None)
        run["sentinel_path"].write_text(json.dumps(run["document"]), encoding="utf-8")

        assert entrypoint.main() == 1

        entrypoint._handle_failure.assert_called_once()
        assert ("comment", "aborted") not in run["events"]

    def test_a_run_with_no_abort_is_untouched_by_this_path(self, run):
        # The overwhelmingly common case. The agent succeeded and nothing about
        # the abort wiring may alter what it reports.
        run["agent_exit"]["code"] = 0
        run["monkeypatch"].setattr(
            entrypoint, "read_abort_sentinel", lambda run_id, generation: None
        )

        assert entrypoint.main() == 0

        entrypoint._handle_success.assert_called_once()
        assert [status for kind, status in run["events"] if kind == "status"][-1] == "complete"


class TestTheTerminalRowIsObservedNotAssumed:
    """Review finding 3: a fail-soft write is not evidence of a durable row.

    ``_handle_abort`` previously called ``update_invocation_status`` — which logs and
    returns on every failure — and its caller then behaved as though a terminal
    ``aborted`` row existed. Two things depend on that row actually being there: the
    dashboard's honest outcome, and (the load-bearing one) the completion guard that
    refuses the redelivered message when the queue acknowledgement fails. If the
    write was lost and the ack also failed, nothing refused the redelivery and the
    run an operator stopped executed again.

    These tests use a real emulated DynamoDB table rather than a mock writer, because
    the property under test is precisely whether a row is *there* afterwards. A
    ``MagicMock`` writer would have returned whatever it was told to and proved
    nothing about persistence; faults are injected into the real main path instead.
    """

    TABLE = "adp-test-webhook-events"

    @pytest.fixture
    def table(self, monkeypatch):
        """A real (emulated) webhook-events table with this run's row seeded."""
        monkeypatch.setenv("ADP_AGENT_AUTHORITY_ENABLED", "false")
        monkeypatch.setenv("AWS_REGION", "us-east-1")
        monkeypatch.setenv("WEBHOOK_EVENTS_TABLE", self.TABLE)
        with mock_aws():
            client = boto3.client("dynamodb", region_name="us-east-1")
            client.create_table(
                TableName=self.TABLE,
                BillingMode="PAY_PER_REQUEST",
                KeySchema=[
                    {"AttributeName": "event_id", "KeyType": "HASH"},
                    {"AttributeName": "arrived_at", "KeyType": "RANGE"},
                ],
                AttributeDefinitions=[
                    {"AttributeName": name, "AttributeType": "S"}
                    for name in ("event_id", "arrived_at")
                ],
            )
            client.put_item(
                TableName=self.TABLE,
                Item={
                    "event_id": {"S": RUN_ID},
                    "arrived_at": {"S": ARRIVED_AT},
                    "tenant_id": {"S": "tenant-a"},
                    "repo": {"S": "acme/app"},
                    "persona": {"S": "developer"},
                    "status": {"S": "in_progress"},
                },
            )
            monkeypatch.setattr(invocation_status, "_ddb", client)
            monkeypatch.setattr(invocation_status, "_table_name", self.TABLE)
            yield client

    def _status(self, client):
        item = client.get_item(
            TableName=self.TABLE,
            Key={"event_id": {"S": RUN_ID}, "arrived_at": {"S": ARRIVED_AT}},
        ).get("Item", {})
        return item.get("status", {}).get("S")

    def _abort(self, monkeypatch, reason="wrong branch"):
        monkeypatch.setattr(entrypoint, "_post_comment", lambda *a, **k: None)
        return entrypoint._handle_abort(
            "acme/app", 42, "developer", RUN_ID, ARRIVED_AT, {"reason": reason}
        )

    def test_a_landed_write_is_observed_as_a_durable_aborted_row(self, monkeypatch, table):
        # The positive half, and it reads the row back rather than trusting the
        # return value: the claim is "a durable terminal transition happened", so the
        # evidence has to be the stored row, not the function's own word for it.
        code, persisted = self._abort(monkeypatch)

        assert code == 0
        assert persisted is True
        assert self._status(table) == "aborted"

    def test_a_row_that_vanished_is_reported_unpersisted(self, monkeypatch, table):
        # Fault injection on the real path: the conditional write requires the row to
        # exist (`attribute_exists(event_id)`), so deleting it makes the genuine
        # ConditionalCheckFailedException fire inside the real writer. No mock writer
        # is involved — this is the actual DynamoDB expression failing.
        table.delete_item(
            TableName=self.TABLE,
            Key={"event_id": {"S": RUN_ID}, "arrived_at": {"S": ARRIVED_AT}},
        )
        monkeypatch.setattr(entrypoint.time, "sleep", lambda _: None)

        code, persisted = self._abort(monkeypatch)

        # Still exits 0 — a non-zero exit would start the replacement pod the abort
        # exists to prevent — but it no longer *claims* a durable terminal row.
        assert code == 0
        assert persisted is False
        assert self._status(table) is None

    def test_an_unavailable_transport_is_reported_unpersisted(self, monkeypatch, table):
        # The other real-world shape: storage reachable for the seed, then failing at
        # the moment of the terminal write.
        def explode(*args, **kwargs):
            raise RuntimeError("dynamodb unavailable")

        monkeypatch.setattr(table, "update_item", explode)

        code, persisted = self._abort(monkeypatch)

        assert code == 0
        assert persisted is False
        # The pre-abort status is untouched, which is the honest record: no terminal
        # transition happened, so the row must not imply one did.
        assert self._status(table) == "in_progress"

    def test_the_operator_still_gets_a_comment_when_the_row_write_fails(self, monkeypatch, table):
        # The comment is posted before the row write and does not depend on it. An
        # operator who asked for a stop should see it acknowledged even when the
        # dashboard row is stale; what must NOT happen is the comment being taken as
        # proof the redelivery guard is armed.
        posted = []
        monkeypatch.setattr(
            entrypoint,
            "_post_comment",
            lambda repo, issue, mid, status, body, url="": posted.append(status),
        )
        monkeypatch.setattr(table, "update_item", MagicMock(side_effect=RuntimeError("down")))

        _, persisted = entrypoint._handle_abort(
            "acme/app", 42, "developer", RUN_ID, ARRIVED_AT, {"reason": "wrong branch"}
        )

        assert posted == ["aborted"]
        assert persisted is False

    def test_a_refused_status_value_is_not_reported_as_persisted(self, monkeypatch, table):
        # The writer's allowlist rejects unknown statuses before either transport. If
        # `aborted` were ever dropped from that set, this pair must report False
        # rather than silently writing nothing and claiming success.
        monkeypatch.setattr(
            invocation_status,
            "ALLOWED_WRITE_STATUSES",
            frozenset({"in_progress", "complete", "failed"}),
        )

        code, persisted = self._abort(monkeypatch)

        assert code == 0
        assert persisted is False
        assert self._status(table) == "in_progress"


class TestTheUnprotectedAbortIsNotReportedAsClean:
    """Review finding 3: persistence failure AND ack failure together.

    The three combinations are what matter, because only one of them leaves the
    stopped run genuinely able to restart:

    ==================  ===========  =========================================
    terminal row        ack          consequence
    ==================  ===========  =========================================
    persisted           confirmed    clean abort; nothing to redeliver
    persisted           unconfirmed  message redelivers, guard refuses it
    NOT persisted       unconfirmed  nothing refuses it — the dangerous case
    ==================  ===========  =========================================

    A run that reported the third case as a clean abort is the defect. The exit code
    cannot carry the distinction (non-zero would summon a replacement pod), so the
    requirement is that it is reported honestly as retryable and logged loudly.
    """

    def _main_tail(self, monkeypatch, *, persisted, ack, calls, repair=None, identify=True):
        """Drive the real acknowledgement branch from `main`'s teardown.

        ``repair`` is what a post-acknowledgement terminal-status repair returns, and
        ``None`` means assert it is never attempted. ``identify=False`` drops the row
        identity, standing in for the callers that do not supply one.
        """
        monkeypatch.setattr(
            entrypoint,
            "_acknowledge_abort",
            lambda *a, **k: (calls.append("ack"), ack)[1],
        )
        monkeypatch.setattr(
            entrypoint,
            "_delete_message",
            lambda *a, **k: calls.append("plain_delete"),
        )

        def _repair(*_a, **_k):
            calls.append("repair")
            assert repair is not None, "repair attempted where the test forbids it"
            return repair

        monkeypatch.setattr(entrypoint, "_persist_abort_terminal_status", _repair)
        return entrypoint._finalize_abort_acknowledgement(
            queue_url="q",
            region="us-east-1",
            receipt_handle="receipt",
            exit_code=0,
            terminal_persisted=persisted,
            message_id="msg-1" if identify else "",
            arrived_at="2026-09-24T10:00:00Z",
            summary="Agent `developer` was aborted by an operator.",
        )

    def test_a_confirmed_ack_is_a_clean_abort(self, monkeypatch):
        calls = []
        code = self._main_tail(monkeypatch, persisted=True, ack=True, calls=calls)

        assert code == 0
        assert calls == ["ack"]

    def test_a_lost_row_is_repaired_once_the_acknowledgement_is_confirmed(self, monkeypatch):
        # Review finding 3: a successful delete used to be reported as a clean abort on
        # its own, leaving an operator looking at a dashboard that still showed the run
        # as active. A confirmed acknowledgement proves the run cannot restart; it says
        # nothing about whether the outcome was reported, and both are required.
        calls = []
        code = self._main_tail(monkeypatch, persisted=False, ack=True, calls=calls, repair=True)

        assert code == 0
        # Order is the requirement, not merely that both happened. The repair runs
        # AFTER the delete: attempting it first would spend retry budget before the one
        # write that actually prevents a rerun.
        assert calls == ["ack", "repair"]

    def test_a_row_that_already_landed_is_not_rewritten(self, monkeypatch):
        # The control on the above. `repair=None` makes the helper fail if a repair is
        # attempted, so a future edit that unconditionally rewrites the row — turning
        # every clean abort into an extra gateway write — fails here.
        calls = []
        code = self._main_tail(monkeypatch, persisted=True, ack=True, calls=calls)

        assert code == 0
        assert calls == ["ack"]

    def test_a_repair_that_also_fails_stays_clean_and_says_the_row_is_stale(
        self, monkeypatch, caplog
    ):
        # Still exit 0, deliberately. The run is stopped and acknowledged, so a non-zero
        # exit would start a pod that can only pick up an unrelated message — it cannot
        # repair this row, and it would be the replacement pod the abort exists to
        # prevent. The honest signal is a log an operator can find.
        calls = []
        with caplog.at_level("ERROR"):
            code = self._main_tail(
                monkeypatch, persisted=False, ack=True, calls=calls, repair=False
            )

        assert code == 0
        assert calls == ["ack", "repair"]
        # Must distinguish itself from the unprotected case below: this one carries no
        # rerun risk, and an operator triaging the two needs to know that.
        assert any(
            "stale" in record.message.lower() and "cannot execute again" in record.message.lower()
            for record in caplog.records
        ), "a stopped-and-acknowledged run with a lost row must be reported as stale, not as a rerun risk"

    def test_no_repair_is_attempted_without_a_row_identity(self, monkeypatch):
        # `message_id` defaults to empty for callers that do not supply one. Repairing
        # from an empty key would write to the wrong row or fail obscurely, so the
        # repair is skipped and the pre-existing clean-exit behaviour is kept.
        calls = []
        code = self._main_tail(
            monkeypatch, persisted=False, ack=True, calls=calls, identify=False
        )

        assert code == 0
        assert calls == ["ack"]

    def test_an_unconfirmed_ack_with_a_durable_row_is_retryable(self, monkeypatch):
        calls = []
        code = self._main_tail(monkeypatch, persisted=True, ack=False, calls=calls)

        assert code == entrypoint.AGENT_EXIT_RETRYABLE

    def test_the_unprotected_combination_is_retryable_and_logged(self, monkeypatch, caplog):
        # The case the review asked to be proven. Both halves failed, so nothing
        # refuses the redelivered message.
        calls = []
        with caplog.at_level("ERROR"):
            code = self._main_tail(monkeypatch, persisted=False, ack=False, calls=calls)

        assert code == entrypoint.AGENT_EXIT_RETRYABLE
        # Not merely non-zero: an operator reading logs has to be able to tell this
        # apart from the benign unconfirmed-ack case above, because only this one can
        # end with the aborted work running again.
        assert any("unprotected" in record.message.lower() for record in caplog.records), (
            "the both-failed case must say that nothing refuses the redelivery"
        )

    def test_it_never_falls_through_to_the_plain_delete_path(self, monkeypatch):
        # The ordinary path swallows delete failures because the work is already on
        # GitHub. An abort must not reach it: that reasoning is what made an
        # unconfirmed acknowledgement look successful.
        # `repair=True` because the acknowledged-but-unpersisted combination now
        # attempts a terminal repair. That is a status write, not a queue operation, so
        # it does not weaken what this test is about: no combination may reach the
        # fail-soft `_delete_message` path.
        for persisted, ack in ((True, True), (True, False), (False, False), (False, True)):
            calls = []
            self._main_tail(monkeypatch, persisted=persisted, ack=ack, calls=calls, repair=True)
            assert "plain_delete" not in calls
