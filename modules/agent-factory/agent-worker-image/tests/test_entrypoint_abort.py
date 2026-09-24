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
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import entrypoint  # noqa: E402
from lib import abort_sentinel  # noqa: E402
from lib.abort_sentinel import (  # noqa: E402
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
        "body_digest": "a" * 64,
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


@pytest.fixture()
def gateway_key(monkeypatch) -> Ed25519PrivateKey:
    """The gateway's signer, with only its public half projected into the pod.

    This asymmetry is the security property: the worker image has no signing path
    at all, so a valid envelope is the one artifact in the pod that could not have
    been manufactured inside it.
    """
    signer = Ed25519PrivateKey.generate()
    monkeypatch.setenv("ADP_CONTROL_ENVELOPE_KEYS", json.dumps({KID: _pem(signer)}))
    return signer


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
            "reason": "wrong branch",
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
        assert outcome["reason"] == "wrong branch"

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
        assert self._resolve(monkeypatch, tmp_path, envelope=_mint(gateway_key, action="pause")) is None

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

    def _finalize(self, monkeypatch, sentinel):
        posted, statuses = [], []
        monkeypatch.setattr(
            entrypoint,
            "_post_comment",
            lambda repo, issue, mid, status, body, url="": posted.append((status, body)),
        )
        monkeypatch.setattr(
            entrypoint,
            "update_invocation_status",
            lambda mid, arrived, status, **kw: statuses.append((status, kw)),
        )
        code = entrypoint._handle_abort("acme/app", 42, "developer", RUN_ID, ARRIVED_AT, sentinel)
        return code, posted, statuses

    def test_one_comment_and_one_aborted_status(self, monkeypatch):
        code, posted, statuses = self._finalize(monkeypatch, {"reason": "wrong branch"})

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
        code, _, _ = self._finalize(monkeypatch, {"reason": None})

        assert code == 0

    def test_an_absent_reason_produces_no_empty_quote_block(self, monkeypatch):
        _, posted, _ = self._finalize(monkeypatch, {"reason": None})

        assert "Reason given" not in posted[0][1]

    def test_an_operator_reason_cannot_forge_comment_structure(self, monkeypatch):
        # Operator-supplied text inside a comment attributed to the platform. It
        # is bounded, but bounded text can still contain markdown, so it stays
        # inside a fenced block where it cannot append a heading or a fake status
        # line of its own.
        hostile = "see ``` ## Merged by platform"
        _, posted, _ = self._finalize(monkeypatch, {"reason": hostile})
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
