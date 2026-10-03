"""Bounded Responses requests through the real production middleware stack.

Same harness as ``test_policy_model_request.py`` — real policy SQL, real Redis
Lua, both production ASGI middlewares — pointed at ``/openai/v1/responses``. The
route was already in ``ENFORCED_PATHS`` before this issue, so the middleware
quoted it and, with no adapter owning it, refused every policy-governed call.
These exercise the production callers rather than the adapter in isolation, so
they fail if registration, the reservation binding or the settlement trust check
regress.
"""

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from starlette.requests import Request

from src.agentauth.model_identity import AgentModelIdentityMiddleware
from src.budget.enforcement_middleware import BudgetEnforcementMiddleware
from src.budget.enforcement_service import BudgetEnforcementService
from src.orchestration.flow_budget import get_flow_reservations
from src.orchestration.flow_meter import meter_target, read_flow_meter
from src.orchestration.policy_admission import load_in_force_policy
from src.orchestration.provider_quotes import quote_request, request_digest
from src.orchestration.responses_quotes import RESPONSES_PATH
from src.shared.enforced_paths import ENFORCED_PATHS
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

# A model the checked-in evidence fixture records as quotable: it publishes both
# a context ceiling and rates. Its bound is large, so a tiny output cap keeps the
# reservation inside the fixture policy's cap and lets more than one call fit.
MODEL = "openai.gpt-5.6-luna"


def responses_body(**overrides) -> bytes:
    document = {"model": MODEL, "input": "hello", "max_output_tokens": 16}
    document.update(overrides)
    return json.dumps(document).encode()


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
    body=None,
    stream=False,
    usage=None,
    usage_known=True,
    provider_error=None,
    reconcile=True,
    truncate_stream=False,
):
    """Drive one Responses call through both production middlewares.

    ``stream`` sends the SSE frame shape the Mantle passthrough emits, so the
    streaming and non-streaming settlements are exercised through the same real
    admission path rather than a bypass.
    """
    body = responses_body() if body is None else body
    context = TokenContext(
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
        "path": RESPONSES_PATH,
        "state": {"token_context": context, "request_id": request_id},
        "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()), (b"x-request-id", request_id.encode())],
    }
    frames = [{"type": "http.request", "body": body[:13], "more_body": True}, {"type": "http.request", "body": body[13:], "more_body": False}]
    sent = []

    async def receive():
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
            raise provider_error
        await send({"type": "http.response.start", "status": 200, "headers": []})
        if stream:
            await send({"type": "http.response.body", "body": b'data: {"type":"response.output_text.delta"}\n\n', "more_body": True})
            if truncate_stream:
                # The stream died before response.completed, so no usage arrives.
                return
        else:
            await send({"type": "http.response.body", "body": b'{"id":"resp_1"', "more_body": True})
        if reconcile:
            reported = {"input_tokens": 5, "output_tokens": 1} if usage is None else usage
            await settle(model_path, context, actual_request_id, reported, usage_known=usage_known)
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    app = RequestIdentityMiddleware(AgentModelIdentityMiddleware(BudgetEnforcementMiddleware(LoggingMiddleware(provider), model_path.service)))
    await app(scope, receive, send)
    return sent, body


async def settle(model_path, context, request_id, reported, *, usage_known=True):
    """Settle this admitted request, as the completion hook eventually does.

    Deliberately NOT a copy of the Mantle trust rule: whether a given usage block
    may release reserved headroom is decided by the production hook, and is
    asserted against that hook in ``test_mantle_responses_settlement.py``. Here
    the settled/unsettled outcome is an input, so these tests isolate admission
    and reservation behaviour.
    """
    await model_path.service.reconcile_reservation(
        context,
        request_id,
        MODEL,
        reported.get("input_tokens", 0) if isinstance(reported, dict) else 0,
        reported.get("output_tokens", 0) if isinstance(reported, dict) else 0,
        actual_cost_usd=Decimal("0.01"),
        usage_known=usage_known,
    )


async def meter(model_path, assignment):
    return await read_flow_meter(org_id=assignment.grant.tenant_id, flow_id=assignment.flow.id, policy=model_path.policy)


# ---------------------------------------------------------------------------
# The capability is admitted: what used to 403 now completes and settles
# ---------------------------------------------------------------------------


def test_the_responses_route_is_policy_enforced():
    """The premise: enforcement already covered this route before the adapter."""
    assert RESPONSES_PATH in ENFORCED_PATHS


async def test_a_bounded_responses_request_succeeds_and_settles(model_path, assignment):
    """Before this issue this exact request was refused 403 with no adapter."""
    sent, body = await invoke(model_path, assignment)
    assert sent[0]["status"] == 200
    assert model_path.calls == 1
    assert model_path.bodies == [body]
    settled = await meter(model_path, assignment)
    assert settled.total_usd == Decimal("0.01") and not settled.has_pending


async def test_a_bounded_streaming_responses_request_succeeds_and_settles(model_path, assignment):
    """The streaming form must admit and settle through the same path."""
    sent, body = await invoke(model_path, assignment, body=responses_body(stream=True), stream=True)
    assert sent[0]["status"] == 200
    assert model_path.calls == 1
    assert model_path.bodies == [body]
    settled = await meter(model_path, assignment)
    assert settled.total_usd == Decimal("0.01") and not settled.has_pending


async def test_the_reservation_equals_the_quoted_bound(model_path, assignment):
    """The hold is the adapter's bound, not an independently derived number."""
    sent, body = await invoke(model_path, assignment, reconcile=False)
    assert sent[0]["status"] == 200
    quote = model_path.quotes[0]
    assert quote is not None and quote.provider == "openai" and quote.capability == "responses"
    assert quote.endpoint == RESPONSES_PATH and quote.billing_model_id == MODEL
    expected = await quote_request(body, RESPONSES_PATH)
    assert quote.total_usd == expected.total_usd > 0
    held = await meter(model_path, assignment)
    assert held.has_pending and held.total_usd == quote.total_usd


async def test_the_quoted_bytes_are_the_bytes_that_reach_the_provider(model_path, assignment):
    """SigV4 signs these exact bytes, so buffering must not reserialize them."""
    body = b'{ "max_output_tokens": 16,\n  "model": "openai.gpt-5.6-luna",  "input": "hello" }'
    sent, _ = await invoke(model_path, assignment, body=body)
    assert sent[0]["status"] == 200
    assert model_path.bodies == [body]
    assert model_path.quotes[0].request_sha256 == request_digest(body)


async def test_a_reasoning_request_is_admitted_within_its_output_cap(model_path, assignment):
    """Reasoning tokens bill as output under max_output_tokens, so this is bounded."""
    body = responses_body(reasoning={"effort": "medium"})
    sent, _ = await invoke(model_path, assignment, body=body)
    assert sent[0]["status"] == 200
    assert model_path.quotes[0].max_output_tokens == 16


# ---------------------------------------------------------------------------
# Inline media through the production stack (#5227)
# ---------------------------------------------------------------------------


def media_body(part: dict, **overrides) -> bytes:
    document = {"model": MODEL, "input": [{"role": "user", "content": [part]}], "max_output_tokens": 16}
    document.update(overrides)
    return json.dumps(document).encode()


INLINE_IMAGE = {"type": "input_image", "image_url": "data:image/png;base64,iVBORw0KGgo="}
INLINE_FILE = {"type": "input_file", "file_data": "data:application/pdf;base64,JVBERi0="}
# The API carries audio bytes in a nested object, not a URL field.
INLINE_AUDIO = {"type": "input_audio", "input_audio": {"data": "UklGRg==", "format": "wav"}}


@pytest.mark.parametrize("part", [INLINE_IMAGE, INLINE_FILE, INLINE_AUDIO], ids=["image", "file", "audio"])
async def test_an_inline_media_request_succeeds_and_settles(model_path, assignment, part):
    """The capability this issue adds, exercised through the real middlewares.

    Before #5227 each of these was refused 403 by the adapter's text-only guard.
    """
    sent, body = await invoke(model_path, assignment, body=media_body(part))
    assert sent[0]["status"] == 200
    assert model_path.calls == 1
    assert model_path.bodies == [body]
    settled = await meter(model_path, assignment)
    assert settled.total_usd == Decimal("0.01") and not settled.has_pending


async def test_an_inline_media_reservation_equals_the_quoted_bound(model_path, assignment):
    """The hold is the adapter's bound, and the quote names MEDIA as its capability."""
    sent, body = await invoke(model_path, assignment, body=media_body(INLINE_IMAGE), reconcile=False)
    assert sent[0]["status"] == 200
    quote = model_path.quotes[0]
    assert quote is not None and quote.capability == "media" and quote.provider == "openai"
    expected = await quote_request(body, RESPONSES_PATH)
    assert quote.total_usd == expected.total_usd > 0
    held = await meter(model_path, assignment)
    assert held.has_pending and held.total_usd == quote.total_usd


async def test_media_does_not_raise_the_bound_above_the_text_bound(model_path, assignment):
    """Media tokens sit inside the full context already reserved, so cost is unchanged.

    This is the whole reason the row is admittable: were the bound to depend on
    the media's size, it would need a count we have no trustworthy source for.
    """
    text_bound = (await quote_request(responses_body(), RESPONSES_PATH)).total_usd
    media_bound = (await quote_request(media_body(INLINE_IMAGE), RESPONSES_PATH)).total_usd
    assert media_bound == text_bound


async def test_a_large_media_payload_does_not_change_the_bound(model_path, assignment):
    """A bigger image must not buy a bigger charge, nor a smaller one a discount."""
    small = {"type": "input_image", "image_url": "data:image/png;base64," + "A" * 64}
    large = {"type": "input_image", "image_url": "data:image/png;base64," + "A" * 40_000}
    assert (await quote_request(media_body(small), RESPONSES_PATH)).total_usd == (await quote_request(media_body(large), RESPONSES_PATH)).total_usd


async def test_the_quoted_media_bytes_are_the_bytes_that_reach_the_provider(model_path, assignment):
    """The digest must cover the media exactly, or the bound is not bound to it."""
    body = media_body(INLINE_IMAGE)
    sent, _ = await invoke(model_path, assignment, body=body)
    assert sent[0]["status"] == 200
    assert model_path.bodies == [body]
    assert model_path.quotes[0].request_sha256 == request_digest(body)


async def test_swapped_media_after_the_quote_is_not_spent(model_path, assignment, monkeypatch):
    """Different media than was quoted must not be forwarded on the old quote.

    The mutable-reference refusal exists to prevent exactly this substitution; for
    inline bytes the digest is what enforces it, so it is asserted here too.
    """
    from src.orchestration import responses_quotes

    original = responses_quotes.OpenAIResponsesQuoteAdapter.bound
    swapped = {"done": False}

    def bound_then_change(self, request):
        quote = original(self, request)
        if not swapped["done"]:
            swapped["done"] = True
            other = {"type": "input_image", "image_url": "data:image/png;base64,OTHERIMAGE="}
            object.__setattr__(quote, "request_sha256", request_digest(media_body(other)))
        return quote

    monkeypatch.setattr(responses_quotes.OpenAIResponsesQuoteAdapter, "bound", bound_then_change)
    sent, _ = await invoke(model_path, assignment, body=media_body(INLINE_IMAGE))
    assert sent[0]["status"] == 403
    assert model_path.calls == 0
    held = await meter(model_path, assignment)
    assert held is None or (held.total_usd == 0 and not held.has_pending)


async def test_an_unsettled_media_call_retains_the_hold(model_path, assignment):
    """Unknown usage on a media call must never reconcile to zero."""
    sent, _ = await invoke(model_path, assignment, body=media_body(INLINE_IMAGE), usage_known=False)
    assert sent[0]["status"] == 200
    held = await meter(model_path, assignment)
    assert held is None or held.has_pending or held.total_usd > 0


async def test_refusing_media_by_reference_leaves_the_supported_paths_working(model_path, assignment):
    """#5227 requires one unsupported combination not to break what already worked."""
    referenced = media_body({"type": "input_file", "file_id": "file-1"})
    assert (await invoke(model_path, assignment, body=referenced))[0][0]["status"] == 403
    assert model_path.calls == 0
    # Plain text, then inline media: both still admitted after the refusal.
    assert (await invoke(model_path, assignment, request_id="text-after"))[0][0]["status"] == 200
    assert (await invoke(model_path, assignment, request_id="media-after", body=media_body(INLINE_IMAGE)))[0][0]["status"] == 200
    assert model_path.calls == 2


async def test_a_refused_media_reference_is_never_fetched(model_path, assignment, monkeypatch):
    """The refusal must not be implemented by resolving the reference first.

    #5227 forbids adding a general URL fetcher, so nothing in the quote path may
    open a socket to decide whether a reference is acceptable. Any outbound
    connection attempt fails this test loudly. ``connect`` is the chokepoint every
    stdlib and third-party HTTP client funnels through, including
    ``socket.create_connection``, which is what ``urllib`` and ``httpx`` use.
    """
    import socket

    def forbidden(*args, **kwargs):
        raise AssertionError("the quote path attempted a network connection")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    body = media_body({"type": "input_image", "image_url": "https://example.invalid/a.png"})
    sent, _ = await invoke(model_path, assignment, body=body)
    assert sent[0]["status"] == 403
    assert model_path.calls == 0


# ---------------------------------------------------------------------------
# Every refusal lands BEFORE the provider call, holding nothing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "unbounded",
    [
        pytest.param({"max_output_tokens": None}, id="no_output_cap"),
        pytest.param({"max_output_tokens": 0}, id="zero_output_cap"),
        pytest.param({"max_output_tokens": -5}, id="negative_output_cap"),
        pytest.param({"model": "openai.gpt-oss-120b"}, id="no_published_context_ceiling"),
        pytest.param({"model": "openai.not-a-real-model"}, id="no_published_price"),
        pytest.param({"previous_response_id": "resp_prior"}, id="server_side_history"),
        pytest.param({"conversation": "conv_1"}, id="server_side_conversation"),
        pytest.param({"prompt": {"id": "pmpt_1"}}, id="server_side_prompt"),
        pytest.param({"background": True}, id="background_execution"),
        # Media parts carrying no payload at all: #5227 admits media, but there is
        # nothing here to bind, so these stay refused before any upstream call.
        pytest.param({"input": [{"role": "user", "content": [{"type": "input_image"}]}]}, id="image_with_no_payload"),
        pytest.param({"input": [{"role": "user", "content": [{"type": "input_file"}]}]}, id="file_with_no_payload"),
        # Media named by reference: the bytes are outside the request, so outside
        # the digest that binds the quote. Refused rather than fetched.
        pytest.param({"input": [{"role": "user", "content": [{"type": "input_file", "file_id": "file-1"}]}]}, id="provider_stored_file"),
        pytest.param(
            {"input": [{"role": "user", "content": [{"type": "input_file", "file_url": "https://example.invalid/a.pdf"}]}]},
            id="fetched_file_url",
        ),
        pytest.param(
            {"input": [{"role": "user", "content": [{"type": "input_image", "image_url": "https://example.invalid/a.png"}]}]},
            id="fetched_image_url",
        ),
        pytest.param({"tools": [{"type": "mcp", "server_label": "s"}]}, id="hosted_mcp_server"),
        pytest.param({"tools": [{"type": "web_search"}]}, id="hosted_web_search"),
        pytest.param({"tools": [{"type": "code_interpreter"}]}, id="hosted_code_interpreter"),
        pytest.param({"usage": {"input_tokens": 1}}, id="client_supplied_usage"),
        pytest.param({"input_tokens": 1}, id="client_supplied_count"),
        pytest.param({"input": ""}, id="no_input"),
    ],
)
async def test_an_unbounded_request_never_reaches_the_provider(model_path, assignment, unbounded):
    """No trustworthy bound means no upstream effect and no reservation."""
    document = {"model": MODEL, "input": "hello", "max_output_tokens": 16}
    document.update(unbounded)
    body = json.dumps({key: value for key, value in document.items() if value is not None}).encode()
    sent, _ = await invoke(model_path, assignment, body=body)
    assert sent[0]["status"] == 403
    assert model_path.calls == 0
    held = await meter(model_path, assignment)
    assert held is None or (held.total_usd == 0 and not held.has_pending)


@pytest.mark.parametrize(
    "kind",
    ["function_call", "function_call_output", "item_reference", "reasoning", "file_search_call"],
)
async def test_an_out_of_scope_input_item_carrying_content_never_reaches_the_provider(model_path, assignment, kind):
    """A client-added ``content`` key must not buy admission for a refused kind.

    These items are read from the CLIENT's request body, so this path is
    client-reachable. Before #5333's repair the guard admitted any item kind that
    carried ``content``, so each of these was quoted and forwarded even though
    the capability refuses the kind by name.
    """
    body = json.dumps({"model": MODEL, "max_output_tokens": 16, "input": [{"type": kind, "id": "existing_item", "content": "hello"}]}).encode()
    sent, _ = await invoke(model_path, assignment, body=body)
    assert sent[0]["status"] == 403
    assert model_path.calls == 0
    held = await meter(model_path, assignment)
    assert held is None or (held.total_usd == 0 and not held.has_pending)


async def test_a_malformed_body_never_reaches_the_provider(model_path, assignment):
    sent, _ = await invoke(model_path, assignment, body=b"not json at all")
    assert sent[0]["status"] == 403
    assert model_path.calls == 0


async def test_a_duplicated_field_never_reaches_the_provider(model_path, assignment):
    """Two readers could disagree about what was requested, so it is not quotable."""
    body = b'{"model": "openai.gpt-5.6-luna", "input": "a", "input": "bbbb", "max_output_tokens": 16}'
    sent, _ = await invoke(model_path, assignment, body=body)
    assert sent[0]["status"] == 403
    assert model_path.calls == 0


async def test_a_refusal_does_not_poison_the_flow(model_path, assignment):
    """A refused request must not consume allowance from the next valid one."""
    assert (await invoke(model_path, assignment, body=responses_body(background=True)))[0][0]["status"] == 403
    sent, _ = await invoke(model_path, assignment, request_id="after")
    assert sent[0]["status"] == 200
    assert model_path.calls == 1


@pytest.mark.parametrize("settled", [False, True])
async def test_an_exhausted_shared_budget_never_reaches_the_provider(model_path, assignment, settled):
    target = meter_target(org_id=assignment.grant.tenant_id, flow_id=assignment.flow.id, policy=model_path.policy)
    cost = model_path.policy.limits.max_spend_usd - Decimal("0.001")
    await get_flow_reservations().reserve("earlier", cost, [target])
    if settled:
        await get_flow_reservations().reconcile("earlier", cost, [target])
    sent, _ = await invoke(model_path, assignment)
    assert sent[0]["status"] == (402 if settled else 429)
    assert model_path.calls == 0


# ---------------------------------------------------------------------------
# Binding: the charge belongs to the exact submitted request
# ---------------------------------------------------------------------------


async def test_a_payload_changed_after_the_quote_is_not_spent(model_path, assignment, monkeypatch):
    """The prompt must not be swapped between quoting and forwarding."""
    from src.orchestration import responses_quotes

    original = responses_quotes.OpenAIResponsesQuoteAdapter.bound
    swapped = {"done": False}

    def bound_then_change(self, request):
        quote = original(self, request)
        if not swapped["done"]:
            swapped["done"] = True
            # The quote now describes bytes other than those about to be sent.
            object.__setattr__(quote, "request_sha256", request_digest(responses_body(input="a much longer prompt")))
        return quote

    monkeypatch.setattr(responses_quotes.OpenAIResponsesQuoteAdapter, "bound", bound_then_change)
    sent, _ = await invoke(model_path, assignment)
    assert sent[0]["status"] == 403
    assert model_path.calls == 0
    held = await meter(model_path, assignment)
    assert held is None or (held.total_usd == 0 and not held.has_pending)


async def test_a_rate_generation_rolling_over_requotes_instead_of_spending(model_path, assignment, monkeypatch):
    """A published revision moving between quote and reservation must not spend."""
    from src.orchestration import responses_quotes

    async def hook():
        monkeypatch.setattr(
            responses_quotes.OpenAIResponsesQuoteAdapter, "current_pricing_revision", lambda self: "rolled-over-generation", raising=False
        )

    original = model_path.service._note_check_succeeded

    async def note():
        await hook()
        await original()

    model_path.service._note_check_succeeded = note
    sent, _ = await invoke(model_path, assignment)
    assert sent[0]["status"] == 503
    assert model_path.calls == 0
    held = await meter(model_path, assignment)
    assert held is None or (held.total_usd == 0 and not held.has_pending)


async def test_repeated_submissions_are_charged_separately(model_path, assignment):
    """Two upstream submissions are two charges; neither overwrites the other."""
    first, _ = await invoke(model_path, assignment, request_id="same-client-id")
    second, _ = await invoke(model_path, assignment, request_id="same-client-id")
    assert first[0]["status"] == 200 and second[0]["status"] == 200
    # Each submission was reserved under its OWN generated id, not the client's.
    assert len(set(model_path.request_ids)) == 2
    assert "same-client-id" not in model_path.request_ids
    settled = await meter(model_path, assignment)
    assert settled.total_usd == Decimal("0.02")


async def test_concurrent_submissions_share_one_cap(model_path, assignment):
    """A fresh run id buys no fresh allowance on this route either."""
    body = responses_body()
    bound = (await quote_request(body, RESPONSES_PATH)).total_usd
    runs = ("developer", "reviewer", "child", "restarted")
    affordable = int(model_path.policy.limits.max_spend_usd / bound)
    results = await asyncio.gather(*(invoke(model_path, assignment, request_id=run, reconcile=False) for run in runs))
    admitted = [sent[0]["status"] for sent, _ in results]
    assert 0 < admitted.count(200) <= min(affordable, len(runs))
    assert model_path.calls == admitted.count(200)
    assert len(set(model_path.request_ids)) == model_path.calls


# ---------------------------------------------------------------------------
# Settlement: an untrusted total keeps the hold, never releases it as free
# ---------------------------------------------------------------------------


async def test_a_truncated_stream_does_not_settle_at_zero(model_path, assignment):
    """No response.completed arrived, so no usage exists; the hold must stand."""
    sent, _ = await invoke(model_path, assignment, body=responses_body(stream=True), stream=True, truncate_stream=True)
    assert sent[0]["status"] == 200
    assert model_path.calls == 1
    held = await meter(model_path, assignment)
    assert held is None or held.has_pending or held.total_usd > 0


async def test_a_provider_error_after_submission_retains_the_hold(model_path, assignment):
    """The upstream may already have charged, so an ambiguous failure holds."""
    with pytest.raises(RuntimeError):
        await invoke(model_path, assignment, provider_error=RuntimeError("upstream timeout after submission"))
    assert model_path.calls == 1
    held = await meter(model_path, assignment)
    assert held is None or held.has_pending or held.total_usd > 0


async def test_an_unsettled_call_retains_the_hold(model_path, assignment):
    sent, _ = await invoke(model_path, assignment, usage_known=False)
    assert sent[0]["status"] == 200
    held = await meter(model_path, assignment)
    assert held is None or held.has_pending or held.total_usd > 0


async def test_cached_input_settles_without_being_double_counted(model_path, assignment):
    """input_tokens already includes cached input on this API; adding would overcharge."""
    reported = {"input_tokens": 500, "output_tokens": 10, "input_tokens_details": {"cached_tokens": 480}}
    sent, _ = await invoke(model_path, assignment, usage=reported)
    assert sent[0]["status"] == 200
    settled = await meter(model_path, assignment)
    assert settled.total_usd == Decimal("0.01") and not settled.has_pending


async def test_reasoning_usage_settles_within_the_output_total(model_path, assignment):
    reported = {"input_tokens": 5, "output_tokens": 16, "output_tokens_details": {"reasoning_tokens": 12}}
    sent, _ = await invoke(model_path, assignment, usage=reported)
    assert sent[0]["status"] == 200
    settled = await meter(model_path, assignment)
    assert settled.total_usd == Decimal("0.01") and not settled.has_pending


async def test_unsettled_usage_blocks_the_next_call_until_receipt(model_path, assignment):
    """Unknown spend is not free allowance for the following request."""
    assert (await invoke(model_path, assignment, usage_known=False))[0][0]["status"] == 200
    assert (await invoke(model_path, assignment, request_id="next"))[0][0]["status"] == 503
    assert model_path.calls == 1


async def test_protected_budget_approval_unblocks_same_run_without_resetting_spend(model_path, assignment, session, monkeypatch):
    from sqlalchemy import select

    from src.budget.config import budget_config
    from src.orchestration.compile import ApprovalContext
    from src.orchestration.continuation import digest
    from src.orchestration.models import OrchestrationAcceptedPlan
    from src.orchestration.shared_budget import BudgetIncreaseRequest, accept_budget_increase, preview_budget_increase

    monkeypatch.setattr(budget_config, "budget_run_cap_usd", Decimal("0.01"))
    sent, _ = await invoke(model_path, assignment)
    assert sent[0]["status"] == 402 and model_path.calls == 0
    plan = await session.scalar(select(OrchestrationAcceptedPlan).where(OrchestrationAcceptedPlan.flow_id == assignment.flow.id))
    plan.plan_hash = digest(plan.plan_document)
    plan.accepted_by_decision_id = assignment.grant.authority.reference_id
    await session.flush()
    actor = ApprovalContext(org_id=assignment.flow.org_id, actor_id=assignment.grant.authority.human_id, actor_role="platform_admin")
    request = BudgetIncreaseRequest(
        expected_plan_version=plan.version,
        expected_plan_hash=plan.plan_hash,
        limits={"max_spend_usd": 50, "max_run_spend_usd": 50, "max_chain_spend_usd": 50},
        reason="Owner authorizes run headroom within the existing flow total.",
    )
    result = await preview_budget_increase(session, flow_id=assignment.flow.id, actor=actor, request=request)
    await accept_budget_increase(
        session,
        flow_id=assignment.flow.id,
        actor=actor,
        request=request.model_copy(update={"expected_snapshot": result["snapshot"]}),
    )
    for _ in range(2):
        assert (await invoke(model_path, assignment))[0][0]["status"] == 200
    assert model_path.calls == 2
    assert (await meter(model_path, assignment)).total_usd == Decimal("0.02")
    target = meter_target(org_id=assignment.flow.org_id, flow_id=assignment.flow.id, policy=model_path.policy)
    await get_flow_reservations().reserve("prior-spend", Decimal("49.98"), [target])
    await get_flow_reservations().reconcile("prior-spend", Decimal("49.98"), [target])
    assert (await invoke(model_path, assignment))[0][0]["status"] == 402
    assert model_path.calls == 2
