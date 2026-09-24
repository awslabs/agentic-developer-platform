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
            "reason": "wrong approach",
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

        monkeypatch.setattr(
            entrypoint, "_handle_success", MagicMock(side_effect=handle_success)
        )
        monkeypatch.setattr(
            entrypoint, "_handle_failure", MagicMock(side_effect=handle_failure)
        )
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
