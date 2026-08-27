"""chain_depth counts agent generations, not webhook events — Issue #4268.

The loop guard (``spawn_persona`` Guard 5, cap ``MAX_CHAIN_DEPTH``) exists to
bound agent→agent recursion. It was reading a counter that measured something
else: the number of webhook EVENTS on the correlation chain, including events the
Lambda recorded and then discarded.

``determine_correlation`` returned ``inherited_depth + 1`` on every ingest path,
and that value is persisted on the ``webhook-events`` row for every outcome —
including ``no_op``. The next event on the chain inherits the newest row's depth,
so each discarded event permanently raised the floor. Measured on the live dev
chain ``c96212b1-fda5-49a0-806b-3e079d585c6c``: 702 rows, 696 of them ``no_op``,
679 of THOSE ``event_type_unhandled``, head ``chain_depth`` 290 against a cap of
8 — with two real agent generations (human → operations → developer). An
orchestrator inflated its own chain by posting status comments, then got refused
with ``chain_depth_exceeded``.

A safety control firing on an unrelated signal protects nothing: at 2 real
generations reading as 290 it cannot detect a runaway loop, while reliably
blocking legitimate orchestration.

The fix moves the increment to the one event that IS a generation: an authorised
dispatch. These tests assert both halves — the over-count is gone AND the guard
still bounds real recursion — because a fix that only did the first would remove
the protection rather than correct it.

CI path: under ``lambda/`` so ``webhook-ingress-ci.yml``'s
``pytest lambda/ -m "not integration"`` executes it.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

os.environ.setdefault("WEBHOOK_SECRET", "test-secret-123")
os.environ.setdefault("WEBHOOK_SECRET_ARN", "")
os.environ.setdefault(
    "SUBMIT_QUEUE_URL",
    "https://sqs.us-east-1.amazonaws.com/123456789/adp-dev-agent-submit.fifo",
)
os.environ.setdefault("IDENTITY_INDEX_TABLE", "adp-dev-identity-index")
os.environ.setdefault("RATE_LIMITS_TABLE", "adp-dev-rate-limits")
os.environ.setdefault("AWS_REGION", "us-east-1")

WEBHOOK_SECRET = "test-secret-123"
CHANNEL = "github:repo=acme/repo,issue=4196"


# =============================================================================
# Helpers
# =============================================================================


class _Identity:
    """Minimal ResolvedIdentity stand-in for determine_correlation."""

    def __init__(self, user_kind="bot", user_id="bot-orchestrator", bot_kind="operations"):
        self.user_kind = user_kind
        self.user_id = user_id
        self.bot_kind = bot_kind
        self.tenant_id = "acme"
        self.org_id = "acme"
        self.user_provisioning_mode = "strict"


def _chain_row(chain_depth: int, **overrides) -> dict[str, Any]:
    """A server-written ``webhook-events`` row as the correlation-index GSI returns it."""
    row: dict[str, Any] = {
        "event_id": "inv-parent",
        "correlation_id": "corr-orchestration",
        "root_human_id": "human-operator",
        "is_human_rooted": True,
        "chain_depth": chain_depth,
        "tenant_id": "acme",
        "repo": "acme/repo",
    }
    row.update(overrides)
    return row


def _pointer(**overrides) -> dict[str, Any]:
    row: dict[str, Any] = {
        "correlation_id": "corr-orchestration",
        "triggering_invocation_id": "inv-parent",
        "last_triggered_persona": None,
        "recent_triggered_personas": set(),
        "recent_trigger_count": 0,
    }
    row.update(overrides)
    return row


def _ingest_depth(chain_depth: int, *, pointer=None, marker_text=None) -> int:
    """Depth that ``determine_correlation`` puts on the context for an inbound event."""
    from handler import determine_correlation

    store = MagicMock()
    store.read_pointer.return_value = _pointer() if pointer is None else pointer
    with patch("handler._get_correlation_store", return_value=store):
        with patch("handler._resolve_chain_record", return_value=_chain_row(chain_depth)):
            ctx = determine_correlation({}, _Identity(), CHANNEL, marker_text=marker_text)
    return ctx["chain_depth"]


def _make_webhook_event(event_type: str, payload: dict) -> dict:
    body = json.dumps(payload)
    sig = hmac.new(
        WEBHOOK_SECRET.encode("utf-8"), body.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return {
        "headers": {
            "x-github-event": event_type,
            "content-type": "application/json",
            "x-hub-signature-256": f"sha256={sig}",
        },
        "body": body,
        "isBase64Encoded": False,
    }


def _status_comment_payload(action: str = "created", body: str = "🤖 working on it") -> dict:
    """An agent status comment — the event class that inflated the counter.

    No ``@agent-`` mention, so the intent parser declines and the handler takes
    the ``no_op`` branch.
    """
    return {
        "action": action,
        "comment": {"body": body},
        "issue": {
            "number": 4196,
            "title": "Story: wave-1",
            "html_url": "https://github.com/acme/repo/issues/4196",
        },
        "repository": {"full_name": "acme/repo"},
        "sender": {"login": "aws-e-adp-agent-dev[bot]", "id": 99, "type": "Bot"},
        "installation": {"id": 123},
    }


def _run_handler(event_type: str, payload: dict, *, chain_depth: int):
    """Drive the real ``handler()`` over one delivery on a chain at ``chain_depth``.

    Returns the mock for ``handler._capture_invocation_event`` so the test can
    inspect the depth the row would have been written with — the value the NEXT
    event on this chain inherits, which is the actual mechanism of the bug.
    """
    from common.identity_resolver import ResolvedIdentity

    resolved = ResolvedIdentity(
        tenant_id="acme",
        org_id="acme",
        user_id="bot-orchestrator",
        user_provisioning_mode="strict",
        user_kind="bot",
        bot_kind="operations",
    )
    rate = MagicMock()
    rate.allowed = True
    rate.retry_after_seconds = 0

    store = MagicMock()
    store.channel_key.side_effect = lambda prov, repo, kind, num: (
        f"{prov}:repo={repo},{kind}={num}"
    )
    store.read_pointer.return_value = _pointer()

    with (
        patch("handler._get_events_log") as mock_log,
        patch("handler._get_rate_limiter") as mock_rate,
        patch("handler._get_identity_resolver") as mock_resolver,
        patch("handler._get_signature") as mock_sig,
        patch("handler._get_correlation_store", return_value=store),
        patch("handler._resolve_chain_record", return_value=_chain_row(chain_depth)),
        patch("handler._capture_invocation_event") as mock_capture,
    ):
        mock_sig.return_value.verify_github_signature.return_value = True
        mock_resolver.return_value.resolve.return_value = (resolved, "ok")
        mock_rate.return_value.check_and_increment.return_value = rate
        mock_log.return_value.log_event = MagicMock()

        from handler import handler

        result = handler(_make_webhook_event(event_type, payload), None)
        return result, mock_capture


def _spawn_ctx(chain_depth: int, **overrides) -> dict[str, Any]:
    ctx: dict[str, Any] = {
        "correlation_id": "corr-orchestration",
        "root_human_id": "human-operator",
        "triggered_by": "bot-orchestrator",
        "is_human_rooted": True,
        "is_new_chain": False,
        "parent_invocation_id": "inv-parent",
        "chain_depth": chain_depth,
        "last_triggered_persona": None,
        "recent_triggered_personas": set(),
        "recent_trigger_count": 0,
    }
    ctx.update(overrides)
    return ctx


def _dispatch(correlation_ctx: dict):
    """Run ``spawn_persona`` to success and return the published envelope."""
    from common.spawn_persona import spawn_persona

    with (
        patch("common.spawn_persona._emit_metric"),
        patch("common.spawn_persona._write_pointer_and_provenance"),
        patch("common.spawn_persona._capture_invocation_event") as mock_capture,
        patch("common.sqs_publisher.publish_envelope", return_value="msg-1") as mock_sqs,
    ):
        result = spawn_persona(
            persona="developer",
            correlation_ctx=correlation_ctx,
            channel_key=CHANNEL,
            resolved_identity=_Identity(),
            tenant_id="acme",
            actor_user_id="bot-orchestrator",
            actor_org_id="acme",
            sender={"login": "aws-e-adp-agent-dev[bot]", "id": 99, "type": "Bot"},
            event_type="issue_comment",
            action="created",
            installation_id=123,
            repo="acme/repo",
            payload={"issue": {"number": 4196, "title": "Story"}},
            intent_trigger="mentioned",
        )
        envelope = mock_sqs.call_args[0][0] if mock_sqs.call_args else None
        return result, envelope, mock_capture


# =============================================================================
# Discarded events must not advance the counter
# =============================================================================


class TestDiscardedEventsDoNotAdvanceDepth:
    """The 696-of-702 case: rows the platform decided to do nothing with."""

    def test_no_op_event_inherits_chain_depth_unchanged(self):
        """A ``no_op`` delivery persists the depth it inherited, not depth+1.

        This is the mechanism of the bug in one assertion: the row's depth is what
        the next event inherits, so a discarded event writing inherited+1 is what
        ratcheted the chain to 290. Asserted through the real ``handler()`` on the
        row-write call, not on ``determine_correlation`` alone, because it is the
        PERSISTED value that feeds the next hop.
        """
        result, mock_capture = _run_handler(
            "issue_comment", _status_comment_payload(), chain_depth=2
        )

        assert result["statusCode"] == 200
        kwargs = mock_capture.call_args.kwargs
        assert kwargs["status"] == "no_op"
        assert kwargs["chain_depth"] == 2, (
            "a discarded event advanced the recursion counter"
        )

    def test_event_type_unhandled_does_not_increment(self):
        """The 679-row case, via the evidence table's representative row.

        ``issue_comment``/``edited``, ``no_op``, ``skip_reason:
        event_type_unhandled``, ``chain_depth: 277`` — an edited comment with no
        handler that advanced the recursion counter by one. Only
        ``issue_comment``/``created`` has a branch, so an edit falls through to the
        unhandled tail while still carrying full correlation context (which is why
        it has a real inherited depth to preserve, unlike a channel-less event).
        """
        result, mock_capture = _run_handler(
            "issue_comment", _status_comment_payload(action="edited"), chain_depth=7
        )

        assert result["statusCode"] == 200
        kwargs = mock_capture.call_args.kwargs
        assert kwargs["skip_reason"] == "event_type_unhandled"
        assert kwargs["chain_depth"] == 7

    def test_suppressed_bot_event_does_not_increment(self):
        """The other large no_op class: bot events suppressed before parsing.

        A bot-sent non-comment/non-PR event is dropped by the loop-prevention gate
        (``bot_event_ignored``) rather than the unhandled tail. Different reason,
        same requirement — it started nothing.
        """
        payload = {
            "action": "completed",
            "check_run": {"id": 1, "name": "ci"},
            "repository": {"full_name": "acme/repo"},
            "sender": {"login": "aws-e-adp-agent-dev[bot]", "id": 99, "type": "Bot"},
            "installation": {"id": 123},
        }
        result, mock_capture = _run_handler("check_run", payload, chain_depth=5)

        assert result["statusCode"] == 200
        kwargs = mock_capture.call_args.kwargs
        assert kwargs["skip_reason"] == "bot_event_ignored"
        # No issue/PR channel on this event type, so no correlation context is
        # computed at all — the row carries no depth, which cannot advance one.
        assert kwargs["chain_depth"] is None

    def test_ingest_inherits_unchanged_on_every_precedence_path(self):
        """All four ``determine_correlation`` branches, not just the one in the trace.

        The increment existed at four return sites (pointer+marker same chain,
        cross-channel marker, pointer-only, marker-only). A fix applied to some of
        them leaves the ratchet open on the others.
        """
        marker = (
            "<!-- adp-correlation:corr-orchestration adp-root-human:human-operator "
            "adp-is-human-rooted:true adp-invocation:inv-parent adp-chain-depth:3 -->"
        )
        cross_marker = (
            "<!-- adp-correlation:corr-elsewhere adp-root-human:human-operator "
            "adp-is-human-rooted:false adp-invocation:inv-x adp-chain-depth:3 -->"
        )

        # Rule 1: pointer + marker, same correlation → server chain row's depth.
        assert _ingest_depth(3, marker_text=marker) == 3
        # Rule 3: pointer only.
        assert _ingest_depth(3, marker_text=None) == 3
        # Rule 2: pointer + marker, different correlation → marker's depth.
        assert _ingest_depth(9, marker_text=cross_marker) == 3
        # Rule 4: marker only, no pointer.
        assert _ingest_depth(9, pointer=None, marker_text=cross_marker) == 3


# =============================================================================
# Real generations must still count
# =============================================================================


class TestGenuineDispatchIncrementsExactlyOnce:
    """The hop that starts an agent is the hop that counts."""

    def test_successful_dispatch_increments_by_exactly_one(self):
        """Caller at depth N → spawned run's envelope and row carry N+1."""
        result, envelope, mock_capture = _dispatch(_spawn_ctx(2))

        assert result.success is True
        assert envelope["correlation"]["chain_depth"] == 3
        assert mock_capture.call_args.kwargs["correlation_ctx"]["chain_depth"] == 3

    def test_blocked_dispatch_does_not_increment(self):
        """A guard-blocked spawn started nothing, so its row must not advance depth.

        Otherwise a chain that keeps hitting a guard would climb toward the cap on
        the strength of dispatches that never happened — the same class of
        over-count, reintroduced on the block path.
        """
        from common.spawn_persona import spawn_persona

        ctx = _spawn_ctx(2, last_triggered_persona="developer")  # trips Guard 3
        with (
            patch("common.spawn_persona._emit_metric"),
            patch("common.spawn_persona._capture_blocked_event") as mock_blocked,
        ):
            result = spawn_persona(
                persona="developer",
                correlation_ctx=ctx,
                channel_key=CHANNEL,
                resolved_identity=_Identity(),
                tenant_id="acme",
                actor_user_id="bot-orchestrator",
                actor_org_id="acme",
                sender={"login": "aws-e-adp-agent-dev[bot]", "id": 99, "type": "Bot"},
                event_type="issue_comment",
                action="created",
                installation_id=123,
                repo="acme/repo",
                payload={"issue": {"number": 4196}},
                intent_trigger="mentioned",
            )

        assert result.success is False
        assert result.block_reason == "self_re_trigger"
        assert mock_blocked.call_args.kwargs["correlation_ctx"]["chain_depth"] == 2
        # The caller's own context must not have been mutated in place either.
        assert ctx["chain_depth"] == 2

    def test_chain_rooting_spawn_starts_at_zero(self):
        """A run nothing spawned is generation 0.

        ``is_new_chain`` covers the human-initiated comment, the EventBridge rule,
        and the bot-initiated fallback chain. Depth 0 is the convention
        ``_compute_authorized_user_id`` is written against for "human-initiated".
        """
        _, envelope, _ = _dispatch(_spawn_ctx(0, is_new_chain=True))
        assert envelope["correlation"]["chain_depth"] == 0

    def test_n_genuine_generations_report_depth_n(self):
        """A chain of N real hops reports N, regardless of events in between.

        Each generation's depth becomes the next caller's inherited depth (via the
        chain row), so this walks the real steady-state loop.
        """
        depth = 0  # the root run
        for expected in range(1, 6):
            _, envelope, _ = _dispatch(_spawn_ctx(depth))
            depth = envelope["correlation"]["chain_depth"]
            assert depth == expected


class TestOrchestratorScenario:
    """The end-to-end shape from the issue: status comments between dispatches."""

    def test_k_status_comments_advance_depth_by_one_not_k(self):
        """An orchestrator posting K updates between two dispatches advances by 1.

        This is the integration test the issue asks for and the exact reproduction
        that wedged wave-1: dispatch, then chatter, then dispatch. Pre-fix, the K
        comments each incremented, so the second dispatch entered at depth+K and
        eventually tripped the cap. The orchestrator was penalised for reporting
        progress.
        """
        # Generation 1: the orchestrator dispatches its first child.
        _, envelope, _ = _dispatch(_spawn_ctx(0))
        depth_after_first = envelope["correlation"]["chain_depth"]
        assert depth_after_first == 1

        # The orchestrator posts K status comments. Each is a no_op webhook event
        # on the same chain, and each writes a row the next event inherits from.
        depth = depth_after_first
        for i in range(12):
            result, mock_capture = _run_handler(
                "issue_comment",
                _status_comment_payload(body=f"🤖 progress update {i}"),
                chain_depth=depth,
            )
            assert result["statusCode"] == 200
            persisted = mock_capture.call_args.kwargs["chain_depth"]
            if persisted is not None:
                depth = persisted

        assert depth == 1, f"12 status comments moved depth to {depth}"

        # Generation 2: the second dispatch is accepted and advances by exactly 1.
        result, envelope, _ = _dispatch(_spawn_ctx(depth))
        assert result.success is True
        assert envelope["correlation"]["chain_depth"] == 2

    def test_the_reported_chain_shape_no_longer_exceeds_the_cap(self):
        """696 discarded events + 2 generations must not exhaust a cap of 8.

        Scaled-down replay of chain ``c96212b1``: the head depth reported 290. The
        assertion is that the guard's input now reflects generations, so the
        dispatch that was refused is accepted.
        """
        from common.spawn_persona import MAX_CHAIN_DEPTH

        depth = 0
        _, envelope, _ = _dispatch(_spawn_ctx(depth))  # human → operations
        depth = envelope["correlation"]["chain_depth"]

        for _ in range(40):  # the no_op flood, scaled down
            _, mock_capture = _run_handler(
                "issue_comment", _status_comment_payload(), chain_depth=depth
            )
            persisted = mock_capture.call_args.kwargs["chain_depth"]
            if persisted is not None:
                depth = persisted

        result, envelope, _ = _dispatch(_spawn_ctx(depth))  # operations → developer
        assert result.success is True, "the dispatch that #4245 saw refused"
        assert envelope["correlation"]["chain_depth"] == 2
        assert depth < MAX_CHAIN_DEPTH


# =============================================================================
# The guard must still bound real recursion (#4128 hardening preserved)
# =============================================================================


class TestGuardStillProtects:
    """The fix must correct the counter, not disable the protection."""

    def test_at_max_chain_depth_spawn_is_still_refused(self):
        """``MAX_CHAIN_DEPTH`` genuine generations → ``chain_depth_exceeded``."""
        from common.spawn_persona import MAX_CHAIN_DEPTH, spawn_persona

        with patch("common.spawn_persona._emit_metric"):
            with patch("common.spawn_persona._capture_blocked_event"):
                result = spawn_persona(
                    persona="developer",
                    correlation_ctx=_spawn_ctx(MAX_CHAIN_DEPTH),
                    channel_key=CHANNEL,
                    resolved_identity=_Identity(),
                    tenant_id="acme",
                    actor_user_id="bot-orchestrator",
                    actor_org_id="acme",
                    sender={"login": "aws-e-adp-agent-dev[bot]", "id": 99, "type": "Bot"},
                    event_type="issue_comment",
                    action="created",
                    installation_id=123,
                    repo="acme/repo",
                    payload={"issue": {"number": 4196}},
                    intent_trigger="mentioned",
                )

        assert result.success is False
        assert result.block_reason == "chain_depth_exceeded"

    def test_unbounded_recursion_hits_the_cap(self):
        """Genuine agent recursion is still bounded, not merely counted.

        The under-count failure mode from the issue's impact table: if a real hop
        stopped counting, this loop would never terminate at the cap.
        """
        from common.spawn_persona import MAX_CHAIN_DEPTH, spawn_persona

        depth = 0
        generations = 0
        while generations < MAX_CHAIN_DEPTH + 5:
            with (
                patch("common.spawn_persona._emit_metric"),
                patch("common.spawn_persona._write_pointer_and_provenance"),
                patch("common.spawn_persona._capture_invocation_event"),
                patch("common.spawn_persona._capture_blocked_event"),
                patch("common.sqs_publisher.publish_envelope", return_value="m") as mock_sqs,
            ):
                result = spawn_persona(
                    persona="developer",
                    correlation_ctx=_spawn_ctx(depth),
                    channel_key=CHANNEL,
                    resolved_identity=_Identity(),
                    tenant_id="acme",
                    actor_user_id="bot-orchestrator",
                    actor_org_id="acme",
                    sender={"login": "aws-e-adp-agent-dev[bot]", "id": 99, "type": "Bot"},
                    event_type="issue_comment",
                    action="created",
                    installation_id=123,
                    repo="acme/repo",
                    payload={"issue": {"number": 4196}},
                    intent_trigger="mentioned",
                )
                if not result.success:
                    assert result.block_reason == "chain_depth_exceeded"
                    break
                depth = mock_sqs.call_args[0][0]["correlation"]["chain_depth"]
            generations += 1

        assert generations == MAX_CHAIN_DEPTH, (
            f"guard fired after {generations} generations, expected {MAX_CHAIN_DEPTH}"
        )

    def test_credential_depth_policy_still_bounded(self):
        """#3174: vault authority still expires with generations, not events.

        The counter feeds ``_compute_authorized_user_id`` too. Over-counting cut
        credentials off early; under-counting would extend a human's authority
        further down the chain than the policy allows.
        """
        from common.spawn_persona import _compute_authorized_user_id

        under = _compute_authorized_user_id(
            correlation_ctx=_spawn_ctx(2), cognito_sub="", max_credential_chain_depth=5
        )
        assert under == "human-operator"

        over = _compute_authorized_user_id(
            correlation_ctx=_spawn_ctx(5), cognito_sub="", max_credential_chain_depth=5
        )
        assert over == ""


class TestDepthCannotBeResetOrForged:
    """Regression guard for #4128 / #4129 — the hardening is untouched."""

    def test_pod_forged_pointer_depth_is_ignored(self):
        """A pointer row claiming depth 0 on a deep chain does not reset it.

        The pointer table is pod-writable, so #4129 sources depth from the
        server-written ``webhook-events`` row. #4268 changed WHEN the counter
        advances, never WHERE it is read from.
        """
        depth = _ingest_depth(9, pointer=_pointer(chain_depth=0))
        assert depth == 9

    def test_agent_trigger_rejects_absent_depth(self):
        """``/agent/trigger`` still 422s rather than resetting to 0."""
        from agent_trigger import _resolve_chain_depth

        record = _chain_row(0)
        del record["chain_depth"]
        depth, error = _resolve_chain_depth(record, None, "corr-orchestration")

        assert error is not None
        assert error["statusCode"] == 422
        assert json.loads(error["body"])["error"] == "invalid_chain_depth"

    def test_agent_trigger_rejects_malformed_depth(self):
        from agent_trigger import _resolve_chain_depth

        _, error = _resolve_chain_depth(
            _chain_row("not-a-number"), None, "corr-orchestration"
        )
        assert error is not None
        assert error["statusCode"] == 422

    def test_agent_trigger_rejects_negative_depth(self):
        """A negative depth would buy extra hops under the cap."""
        from agent_trigger import _resolve_chain_depth

        _, error = _resolve_chain_depth(_chain_row(-5), None, "corr-orchestration")
        assert error is not None
        assert error["statusCode"] == 422

    def test_caller_cannot_present_a_continuation_as_a_fresh_root(self):
        """``is_new_chain`` is server-decided, so depth 0 is not caller-reachable.

        ``_advance_chain_depth`` treats ``is_new_chain`` as "generation 0". Every
        chain-continuation branch of ``determine_correlation`` sets it False, and
        ``/agent/trigger`` hardcodes False — a bot on an existing chain cannot
        route itself down the rooting path.
        """
        assert _ingest_depth(6, marker_text=None) == 6

        from handler import determine_correlation

        store = MagicMock()
        store.read_pointer.return_value = _pointer()
        with patch("handler._get_correlation_store", return_value=store):
            with patch("handler._resolve_chain_record", return_value=_chain_row(6)):
                ctx = determine_correlation({}, _Identity(), CHANNEL, marker_text=None)
        assert ctx["is_new_chain"] is False

        _, envelope, _ = _dispatch(_spawn_ctx(6, is_new_chain=False))
        assert envelope["correlation"]["chain_depth"] == 7
