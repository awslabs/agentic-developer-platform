"""Real policy SQL, Redis Lua and both production ASGI middleware boundaries."""

import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from starlette.requests import Request

from src.agentauth.model_identity import AgentModelIdentityMiddleware
from src.budget.enforcement_middleware import BudgetEnforcementMiddleware
from src.budget.enforcement_service import BudgetEnforcementService
from src.orchestration import flow_meter
from src.orchestration.flow_budget import get_flow_reservations
from src.orchestration.flow_meter import meter_target, read_flow_meter
from src.orchestration.policy_admission import load_in_force_policy
from src.orchestration.provider_quotes import request_digest
from src.shared.middleware.logging_middleware import LoggingMiddleware
from src.shared.middleware.request_identity import RequestIdentityMiddleware
from src.shared.schemas.auth import TokenContext
from tests.orchestration.test_runtime_policy import (
    assignment as assignment_fixture,
)
from tests.orchestration.test_runtime_policy import (
    engine as engine_fixture,
)
from tests.orchestration.test_runtime_policy import (
    healthy_policy_reservations as reservations_fixture,
)
from tests.orchestration.test_runtime_policy import (
    policy_budget_initializers as initializers_fixture,
)
from tests.orchestration.test_runtime_policy import (
    session as session_fixture,
)

engine, session, assignment = engine_fixture, session_fixture, assignment_fixture
healthy_policy_reservations, policy_budget_initializers = reservations_fixture, initializers_fixture


@pytest.fixture
async def model_path(session, assignment, monkeypatch):
    @asynccontextmanager
    async def sessions():
        yield session

    caller = SimpleNamespace(tenant_id=assignment.grant.tenant_id, invocation_id="worker")
    runtime = SimpleNamespace(
        authenticate=lambda *_: (None, caller, None, assignment.grant),
        validate_flow=AsyncMock(),
        store=SimpleNamespace(_read=lambda *_: assignment.execution),
    )
    monkeypatch.setattr("src.agentauth.routes.get_agent_runtime", lambda: runtime)
    monkeypatch.setattr("src.shared.database.get_session_factory", lambda: sessions)
    service = BudgetEnforcementService()
    service._reservations = get_flow_reservations()
    monkeypatch.setattr(service, "_get_session", sessions)
    monkeypatch.setattr(service, "_note_check_succeeded", AsyncMock())
    inputs = await load_in_force_policy(session, org_id=assignment.grant.tenant_id, flow_id=assignment.flow.id)
    return SimpleNamespace(service=service, policy=inputs.policy, calls=0, bodies=[], runtime=runtime, request_ids=[], quotes=[])


async def invoke(
    model_path,
    assignment,
    *,
    request_id="call",
    usage_known=True,
    during_upload=None,
    body=None,
    context=None,
    provider_error=None,
    reconcile=True,
    cancel_stream=False,
):
    body = b'{ "model": "anthropic.claude-sonnet-4-6", "max_tokens": 16, "messages": [{"role":"user","content":"hello"}] }' if body is None else body
    context = context or TokenContext(
        user_id="iam-agent:authority-worker",
        agent_registry_id="authority-worker",
        org_id="__platform__",
        team_id="",
        department_id="",
        account_type="service",
        auth_source="iam",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    scope = {
        "type": "http",
        "method": "POST",
        "scheme": "https",
        "server": ("gateway.test", 443),
        "query_string": b"",
        "path": "/v1/messages",
        "state": {"token_context": context, "request_id": request_id},
        "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()), (b"x-request-id", request_id.encode())],
    }
    frames = [{"type": "http.request", "body": body[:13], "more_body": True}, {"type": "http.request", "body": body[13:], "more_body": False}]
    sent = []

    async def receive():
        if during_upload is not None:
            await during_upload()
        return frames.pop(0)

    async def send(frame):
        sent.append(frame)

    async def provider(scope, receive, send):
        # This fake models a submitted paid call, including unknown usage/errors.
        scope["state"]["token_context"]._budget_provider_started = True
        model_path.calls += 1
        actual_request_id = scope["state"]["request_id"]
        model_path.request_ids.append(actual_request_id)
        model_path.bodies.append(await Request(scope, receive).body())
        model_path.quotes.append(scope["state"]["token_context"]._policy_quote)
        if provider_error is not None:
            # A failure AFTER the request reached the provider: the charge may
            # already exist upstream, so the hold cannot simply be released.
            raise provider_error
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"first", "more_body": True})
        if cancel_stream:
            # The client vanished mid-stream. No final usage will arrive.
            return
        if reconcile:
            await model_path.service.reconcile_reservation(
                context, actual_request_id, "anthropic.claude-sonnet-4-6", 5, 1, actual_cost_usd=Decimal("0.01"), usage_known=usage_known
            )
        await send({"type": "http.response.body", "body": b"second", "more_body": False})

    app = RequestIdentityMiddleware(AgentModelIdentityMiddleware(BudgetEnforcementMiddleware(LoggingMiddleware(provider), model_path.service)))
    await app(scope, receive, send)
    return sent, body


async def test_first_model_call_before_any_usage_row_succeeds_and_preserves_stream(model_path, assignment):
    sent, body = await invoke(model_path, assignment)
    assert sent[0]["status"] == 200
    assert [frame.get("body") for frame in sent[1:]] == [b"first", b"second"]
    assert model_path.bodies == [body]
    meter = await read_flow_meter(org_id=assignment.grant.tenant_id, flow_id=assignment.flow.id, policy=model_path.policy)
    assert meter.total_usd == Decimal("0.01") and not meter.has_pending


@pytest.mark.parametrize("settled", [False, True])
async def test_exhausted_shared_budget_never_reaches_provider(model_path, assignment, settled):
    target = meter_target(org_id=assignment.grant.tenant_id, flow_id=assignment.flow.id, policy=model_path.policy)
    cost = model_path.policy.limits.max_spend_usd - Decimal("0.001")
    await get_flow_reservations().reserve("earlier", cost, [target])
    if settled:
        await get_flow_reservations().reconcile("earlier", cost, [target])
    sent, _ = await invoke(model_path, assignment)
    assert sent[0]["status"] == (402 if settled else 429)
    assert model_path.calls == 0


async def test_reusing_a_client_request_id_cannot_replace_a_previous_charge(model_path, assignment):
    for _ in range(2):
        sent, _ = await invoke(model_path, assignment, request_id="same-client-id")
        assert sent[0]["status"] == 200
    assert len(set(model_path.request_ids)) == 2
    assert "same-client-id" not in model_path.request_ids
    meter = await read_flow_meter(org_id=assignment.grant.tenant_id, flow_id=assignment.flow.id, policy=model_path.policy)
    assert meter.total_usd == Decimal("0.02")


async def test_missing_usage_blocks_next_model_call_until_receipt(model_path, assignment):
    assert (await invoke(model_path, assignment, usage_known=False))[0][0]["status"] == 200
    assert (await invoke(model_path, assignment, request_id="next"))[0][0]["status"] == 503
    assert model_path.calls == 1


async def test_policy_expiry_during_upload_prevents_spending(model_path, assignment, monkeypatch):
    async def expire():
        monkeypatch.setattr("src.orchestration.runtime_policy.utcnow", lambda: datetime.now(UTC) + timedelta(days=100))

    sent, _ = await invoke(model_path, assignment, during_upload=expire)
    assert sent[0]["status"] == 403
    assert model_path.calls == 0


@pytest.mark.parametrize("change", [None, "removed", "malformed", "new_version", "new_limit", "budget_disabled"])
async def test_authoring_rechecks_policy_after_upload(model_path, assignment, session, monkeypatch, change):
    from sqlalchemy import select

    from src.orchestration.models import OrchestrationAcceptedPlan

    assignment.grant = replace(assignment.grant, authority=replace(assignment.grant.authority, kind="replan_request"))
    plan = await session.scalar(select(OrchestrationAcceptedPlan).where(OrchestrationAcceptedPlan.flow_id == assignment.flow.id))
    original = plan.plan_document

    async def amend_during_upload():
        if change == "removed":
            # Retain the original accepted policy in history while the new plan
            # omits it; load_in_force_policy must classify withdrawal as refusal.
            if plan.superseded_at is None:
                plan.superseded_at = datetime.now(UTC)
                session.add(
                    OrchestrationAcceptedPlan(
                        org_id=plan.org_id,
                        flow_id=plan.flow_id,
                        version=plan.version + 1,
                        plan_document={key: value for key, value in original.items() if key != "execution_policy"},
                        plan_hash="withdrawn-policy-test",
                        accepted_by_decision_id=plan.accepted_by_decision_id,
                    )
                )
        elif change == "malformed":
            plan.plan_document = {**original, "execution_policy": {"invalid": True}}
        elif change == "new_version":
            plan.version = 2
        elif change == "new_limit":
            policy = {**original["execution_policy"], "limits": {**original["execution_policy"]["limits"], "max_spend_usd": "0.001"}}
            plan.plan_document = {**original, "execution_policy": policy}
        elif change == "budget_disabled":
            monkeypatch.setenv("BUDGET_ENFORCEMENT_ENABLED", "false")
        await session.flush()

    sent, _ = await invoke(model_path, assignment, during_upload=amend_during_upload)
    assert sent[0]["status"] == (200 if change in {None, "budget_disabled"} else 403)
    assert model_path.calls == (1 if change in {None, "budget_disabled"} else 0)
    meter = await read_flow_meter(org_id=assignment.grant.tenant_id, flow_id=assignment.flow.id, policy=model_path.policy)
    assert meter.total_usd == (Decimal("0.01") if change in {None, "budget_disabled"} else 0)
    assert not meter.has_pending


# ---------------------------------------------------------------------------
# Issue #5225: the reservation is bound to the exact quoted request
# ---------------------------------------------------------------------------


async def test_reservation_is_bound_to_the_quote_for_the_exact_forwarded_bytes(model_path, assignment):
    """The typed quote reaching the provider must describe what was actually sent."""
    sent, body = await invoke(model_path, assignment)
    assert sent[0]["status"] == 200
    quote = model_path.quotes[0]
    assert quote is not None
    assert quote.request_sha256 == request_digest(body)
    assert quote.request_sha256 == request_digest(model_path.bodies[0])
    assert quote.billing_model_id == "anthropic.claude-sonnet-4-6"
    assert quote.provider == "anthropic" and quote.endpoint == "/v1/messages"
    assert quote.currency == "USD" and quote.total_usd > 0
    # The reserved amount is the quote's bound, not an independently derived number.
    assert quote.total_usd == flow_meter.estimate_policy_model_cost(body, "/v1/messages")


async def test_original_wire_bytes_reach_the_upstream_unmodified(model_path, assignment):
    """Quoting buffers the body; it must not reserialize or reorder the JSON."""
    body = b'{ "max_tokens": 16,   "model": "anthropic.claude-sonnet-4-6",\n "messages": [{"role":"user","content":"hello"}] }'
    sent, _ = await invoke(model_path, assignment, body=body)
    assert sent[0]["status"] == 200
    assert model_path.bodies == [body]
    assert model_path.quotes[0].request_sha256 == request_digest(body)


async def test_an_unquotable_request_never_reaches_the_provider(model_path, assignment):
    """No adapter for a capability means no upstream effect and no reservation.

    This image block carries no ``source``, so there is nothing for the quote to
    bind. #5227 admits media that carries its own bytes, but not this.
    """
    unbounded = b'{ "model": "anthropic.claude-sonnet-4-6", "messages": [{"role":"user","content":[{"type":"image"}]}], "max_tokens": 16 }'
    sent, _ = await invoke(model_path, assignment, body=unbounded)
    assert sent[0]["status"] == 403
    assert model_path.calls == 0
    # Nothing was held against the shared meter for a request that never went out.
    meter = await read_flow_meter(org_id=assignment.grant.tenant_id, flow_id=assignment.flow.id, policy=model_path.policy)
    assert meter.total_usd == 0 and not meter.has_pending


# ---------------------------------------------------------------------------
# Inline media on the Anthropic route through the production stack (#5227)
# ---------------------------------------------------------------------------


def _anthropic_media_body(source: dict, block_type: str = "image") -> bytes:
    document = {
        "model": "anthropic.claude-sonnet-4-6",
        "messages": [{"role": "user", "content": [{"type": block_type, "source": source}]}],
        "max_tokens": 16,
    }
    return json.dumps(document).encode()


INLINE_B64 = {"type": "base64", "media_type": "image/png", "data": "iVBORw0KGgo="}


async def test_an_inline_media_request_succeeds_and_is_quoted_as_media(model_path, assignment):
    """The capability #5227 adds, through the real middlewares on this route too."""
    body = _anthropic_media_body(INLINE_B64)
    sent, _ = await invoke(model_path, assignment, body=body)
    assert sent[0]["status"] == 200
    assert model_path.calls == 1
    assert model_path.bodies == [body]
    quote = model_path.quotes[0]
    assert quote is not None and quote.capability == "media" and quote.total_usd > 0
    assert quote.request_sha256 == request_digest(body)


async def test_inline_media_costs_the_same_bound_as_text(model_path, assignment):
    """Media tokens are inside the full context window already reserved whole."""
    text = b'{"model": "anthropic.claude-sonnet-4-6", "messages": [{"role":"user","content":"hello"}], "max_tokens": 16}'
    media = _anthropic_media_body(INLINE_B64)
    assert flow_meter.estimate_policy_model_cost(media, "/v1/messages") == flow_meter.estimate_policy_model_cost(text, "/v1/messages")


@pytest.mark.parametrize(
    "source",
    [
        pytest.param({"type": "url", "url": "https://example.invalid/a.png"}, id="fetched_url"),
        pytest.param({"type": "file", "file_id": "file_1"}, id="provider_stored_file"),
    ],
)
async def test_media_by_reference_never_reaches_the_provider(model_path, assignment, source):
    """Bytes outside the request are outside the digest, so no bound can hold."""
    sent, _ = await invoke(model_path, assignment, body=_anthropic_media_body(source))
    assert sent[0]["status"] == 403
    assert model_path.calls == 0
    meter = await read_flow_meter(org_id=assignment.grant.tenant_id, flow_id=assignment.flow.id, policy=model_path.policy)
    assert meter.total_usd == 0 and not meter.has_pending


async def test_refusing_media_by_reference_leaves_text_and_inline_media_working(model_path, assignment):
    """Refusing one unsupported combination must not break the supported ones."""
    referenced = _anthropic_media_body({"type": "url", "url": "https://example.invalid/a.png"})
    assert (await invoke(model_path, assignment, body=referenced))[0][0]["status"] == 403
    assert model_path.calls == 0
    assert (await invoke(model_path, assignment, request_id="text-after"))[0][0]["status"] == 200
    inline = _anthropic_media_body(INLINE_B64)
    assert (await invoke(model_path, assignment, request_id="media-after", body=inline))[0][0]["status"] == 200
    assert model_path.calls == 2


async def test_a_rate_revision_change_during_upload_requotes_instead_of_spending(model_path, assignment, monkeypatch):
    """A published generation rolling over between quote and reservation must not spend."""
    from src.orchestration import provider_quotes

    original = provider_quotes.AnthropicTextQuoteAdapter.validate

    async def rolled_over(self, quote, request):
        # Simulate the pricing revision moving after the quote was issued.
        moved = replace(quote, pricing_revision=quote.pricing_revision + "+rolled")
        refusal = moved.binds(
            request_sha256=request.digest,
            billing_model_id=quote.billing_model_id,
            pricing_revision=quote.pricing_revision,
        )
        assert refusal is not None
        raise provider_quotes.QuoteRefusedError(refusal)

    monkeypatch.setattr(provider_quotes.AnthropicTextQuoteAdapter, "validate", rolled_over)
    sent, _ = await invoke(model_path, assignment)
    assert sent[0]["status"] == 403
    assert model_path.calls == 0
    monkeypatch.setattr(provider_quotes.AnthropicTextQuoteAdapter, "validate", original)
    # The flow is not poisoned: a fresh request still admits under the same cap.
    assert (await invoke(model_path, assignment, request_id="after"))[0][0]["status"] == 200


async def test_concurrent_children_reviewers_and_restarts_cannot_exceed_one_shared_cap(model_path, assignment):
    """A child, reviewer or restart shares one accumulating cap; none resets it.

    Each admitted call holds the FULL quoted upper bound until it reconciles, so
    the number that fit is bounded by the tightest applicable cap divided by that
    bound — not by how many distinct run ids ask. Whichever cap binds first (the
    run scope here, at a lower figure than the flow's own limit), the property
    under test is the same: a fresh run id buys no fresh allowance.
    """
    quote = flow_meter.estimate_policy_model_cost(
        b'{ "model": "anthropic.claude-sonnet-4-6", "max_tokens": 16, "messages": [{"role":"user","content":"hello"}] }', "/v1/messages"
    )
    runs = ("developer", "reviewer", "child", "restarted")
    affordable = int(model_path.policy.limits.max_spend_usd / quote)
    assert affordable >= 1
    results = await asyncio.gather(*(invoke(model_path, assignment, request_id=run, reconcile=False) for run in runs))
    admitted = [sent[0]["status"] for sent, _ in results]
    # Strictly fewer than the number of runs were admitted, and never more than
    # the shared allowance affords: the cap held across all four concurrently.
    assert 0 < admitted.count(200) <= min(affordable, len(runs))
    assert admitted.count(200) < len(runs)
    assert admitted.count(429) == len(runs) - admitted.count(200)
    # Only admitted calls reached the provider, each with its OWN reservation id.
    assert model_path.calls == admitted.count(200)
    assert len(set(model_path.request_ids)) == model_path.calls
    # A further restart after the fact still finds no fresh allowance.
    assert (await invoke(model_path, assignment, request_id="restarted-again", reconcile=False))[0][0]["status"] == 429


async def test_repeated_client_ids_reserve_each_submission_separately(model_path, assignment):
    """Client request ids are trace hints; each submission gets its own hold."""
    first, _ = await invoke(model_path, assignment, request_id="same", reconcile=False)
    assert first[0]["status"] == 200
    before = await read_flow_meter(org_id=assignment.grant.tenant_id, flow_id=assignment.flow.id, policy=model_path.policy)
    second, _ = await invoke(model_path, assignment, request_id="same", reconcile=False)
    if second[0]["status"] == 200:
        after = await read_flow_meter(org_id=assignment.grant.tenant_id, flow_id=assignment.flow.id, policy=model_path.policy)
        # The second hold ADDED to the first rather than overwriting it.
        assert after.total_usd > before.total_usd
        assert len(set(model_path.request_ids)) == 2
    else:
        # Or it was denied because the first hold still occupies the cap — which
        # is equally proof that it did not silently reuse the same reservation.
        assert second[0]["status"] == 429


async def test_a_provider_error_after_submission_retains_the_hold(model_path, assignment):
    """The upstream may already have charged, so an ambiguous failure keeps the hold."""
    with pytest.raises(RuntimeError):
        await invoke(model_path, assignment, provider_error=RuntimeError("upstream timeout after submission"))
    assert model_path.calls == 1
    meter = await read_flow_meter(org_id=assignment.grant.tenant_id, flow_id=assignment.flow.id, policy=model_path.policy)
    # The reservation is still outstanding: unknown spend, not a settled zero.
    assert meter is None or meter.has_pending or meter.total_usd > 0


async def test_a_canceled_stream_without_final_usage_does_not_settle_at_zero(model_path, assignment):
    sent, _ = await invoke(model_path, assignment, cancel_stream=True)
    assert sent[0]["status"] == 200
    meter = await read_flow_meter(org_id=assignment.grant.tenant_id, flow_id=assignment.flow.id, policy=model_path.policy)
    assert meter is None or meter.has_pending or meter.total_usd > 0


# ---------------------------------------------------------------------------
# Issue #5225: the quote is confirmed at the boundary that actually SPENDS
#
# The middleware revalidates the quote and then hands control inward. The hold
# is taken later, after check_budget_hierarchy has awaited a session, the run
# binding, the scope caps, the entity hierarchy and the person layer. These
# drive the real stack and advance state INSIDE that window — after the
# middleware's own check has already passed — so they fail if the only
# validation is the upload-time one.
# ---------------------------------------------------------------------------


async def during_budget_work(model_path, action):
    """Run `action` inside the awaited budget work, just before reserving.

    `_note_check_succeeded` is the last awaited call before
    `_reserve_or_degrade`, which makes it the seam that represents "time passed
    while the budget check was doing I/O".
    """
    original = model_path.service._note_check_succeeded

    async def hook():
        await action()
        await original()

    model_path.service._note_check_succeeded = hook


async def test_a_quote_expiring_during_budget_io_is_not_spent(model_path, assignment, monkeypatch):
    """A TTL lapsing between admission and the reservation must not be spent.

    The upload-time revalidation passes here — the clock only moves afterwards,
    while the budget check awaits its own I/O. Nothing may be reserved or sent.
    """
    from src.orchestration import provider_quotes

    async def expire():
        moved = datetime.now(UTC) + timedelta(seconds=provider_quotes.QUOTE_TTL_SECONDS + 1)
        monkeypatch.setattr(provider_quotes, "_now", lambda: moved)

    await during_budget_work(model_path, expire)
    sent, _ = await invoke(model_path, assignment)
    assert sent[0]["status"] == 503
    assert model_path.calls == 0
    # Nothing was submitted, so this is a definite pre-submission failure: no
    # hold may be left behind on the shared meter.
    meter = await read_flow_meter(org_id=assignment.grant.tenant_id, flow_id=assignment.flow.id, policy=model_path.policy)
    assert meter is None or (meter.total_usd == 0 and not meter.has_pending)


async def test_a_rate_generation_rolling_over_during_budget_io_is_not_spent(model_path, assignment, monkeypatch):
    """A published generation moving inside the same window must not be spent."""
    from src.orchestration import provider_quotes

    async def roll_over():
        adapter = provider_quotes.AnthropicTextQuoteAdapter

        def moved(self):
            return "rolled-over-generation"

        monkeypatch.setattr(adapter, "current_pricing_revision", moved)

    await during_budget_work(model_path, roll_over)
    sent, _ = await invoke(model_path, assignment)
    assert sent[0]["status"] == 503
    assert model_path.calls == 0
    meter = await read_flow_meter(org_id=assignment.grant.tenant_id, flow_id=assignment.flow.id, policy=model_path.policy)
    assert meter is None or (meter.total_usd == 0 and not meter.has_pending)


async def test_an_adapter_that_cannot_confirm_in_time_has_no_upstream_effect(model_path, assignment, monkeypatch):
    """An adapter timing out at the spend boundary is a refusal, never a pass."""
    from src.orchestration import provider_quotes

    async def hang():
        def never_answers(self):
            import time

            time.sleep(provider_quotes.QUOTE_CONFIRM_TIMEOUT_SECONDS * 3)
            return "too-late"

        monkeypatch.setattr(provider_quotes.AnthropicTextQuoteAdapter, "current_pricing_revision", never_answers)

    await during_budget_work(model_path, hang)
    sent, _ = await invoke(model_path, assignment)
    assert sent[0]["status"] == 503
    assert model_path.calls == 0
    meter = await read_flow_meter(org_id=assignment.grant.tenant_id, flow_id=assignment.flow.id, policy=model_path.policy)
    assert meter is None or (meter.total_usd == 0 and not meter.has_pending)


async def test_a_confirmed_quote_still_admits_and_reserves_normally(model_path, assignment):
    """The confirmation is a gate, not a new failure mode on the healthy path."""
    sent, body = await invoke(model_path, assignment)
    assert sent[0]["status"] == 200
    assert model_path.bodies == [body]
    quote = model_path.quotes[0]
    assert quote is not None and quote.request_sha256 == request_digest(body)


async def test_lost_reservation_state_never_reinitializes_the_flow_at_zero(model_path, assignment):
    """Redis loss is unknown usage, never a fresh allowance."""
    assert (await invoke(model_path, assignment, reconcile=False))[0][0]["status"] == 200
    await get_flow_reservations()._get_client()
    store = get_flow_reservations()
    await store._client.flushdb()
    assert await read_flow_meter(org_id=assignment.grant.tenant_id, flow_id=assignment.flow.id, policy=model_path.policy) is None
    # A request after the loss cannot be admitted against a zeroed meter.
    sent, _ = await invoke(model_path, assignment, request_id="after-loss")
    assert sent[0]["status"] == 503
    assert model_path.calls == 1
