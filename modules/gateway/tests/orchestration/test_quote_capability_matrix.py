"""The capability matrix (#5227) asserted against what the adapters really do.

``fixtures/quote-capability-matrix-5227.json`` is the auditable record of which
provider capabilities can be bounded, what enforces each maximum, and why each
refused row is refused. A fixture nobody checks is documentation that rots, so
every row here is executed against the real adapters: an admitted row must
produce a quote, a refused row must produce exactly its stated reason and must
leave the provider untouched.

The pricing-evidence claim is checked structurally rather than by restating the
fixture: the refusal of server-side tools rests on there being no per-call rate
anywhere in the published rate schema, and that is a property of the code.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from pricing_policy.policy import RateRow
from src.orchestration.provider_quotes import Capability, QuoteReason, QuoteRefusedError, quote_request
from src.orchestration.responses_quotes import RESPONSES_PATH

pytestmark = pytest.mark.asyncio

MATRIX = Path(__file__).parent / "fixtures" / "quote-capability-matrix-5227.json"
NOW = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)

ANTHROPIC_PATH = "/v1/messages"
ANTHROPIC_MODEL = "anthropic.claude-sonnet-4-6"
RESPONSES_MODEL = "openai.gpt-6-astra"


def matrix() -> dict:
    return json.loads(MATRIX.read_text())


def rows_by_name() -> dict[str, dict]:
    return {row["row"]: row for row in matrix()["rows"]}


def anthropic(**overrides) -> bytes:
    document = {"model": ANTHROPIC_MODEL, "messages": [{"role": "user", "content": "hello"}], "max_tokens": 64}
    document.update(overrides)
    return json.dumps(document).encode()


def responses(**overrides) -> bytes:
    document = {"model": RESPONSES_MODEL, "input": "hello", "max_output_tokens": 64}
    document.update(overrides)
    return json.dumps(document).encode()


def _anthropic_media(source: dict, **overrides) -> bytes:
    return anthropic(messages=[{"role": "user", "content": [{"type": "image", "source": source}]}], **overrides)


def _responses_media(part: dict) -> bytes:
    return responses(input=[{"role": "user", "content": [part]}])


async def refuse_reason(raw: bytes, path: str) -> tuple[str, str]:
    with pytest.raises(QuoteRefusedError) as exc:
        await quote_request(raw, path, now=NOW)
    return exc.value.refusal.reason, exc.value.refusal.capability


# ---------------------------------------------------------------------------
# The fixture itself must be complete and self-consistent
# ---------------------------------------------------------------------------


def test_every_row_states_an_enforcement_or_a_prerequisite():
    """An admitted row must name what enforces it; a refused row must name what would unblock it."""
    for row in matrix()["rows"]:
        assert row["status"] in {"admitted", "refused"}, row["row"]
        if row["status"] == "admitted":
            assert row["enforced_maximum"] in matrix()["enforcement_kinds"], row["row"]
            assert row["billable_units"] and row["max_bound_source"] and row["rate_source"] and row["usage_source"]
            assert row["trusted_count"], row["row"]
        else:
            assert row["refusal_reason"] in QuoteReason.ALL, row["row"]
            assert row["why_unboundable"] and row["prerequisite"], row["row"]
            # A refused row is only safe if it costs the caller nothing upstream.
            assert row["no_provider_effect"] is True, row["row"]


def test_no_admitted_row_trusts_a_caller_supplied_count():
    """A client must never price its own request, so no row may cite one as its bound."""
    for row in matrix()["rows"]:
        if row["status"] != "admitted":
            continue
        evidence = " ".join([row["trusted_count"], row["max_bound_source"], row["enforced_maximum"]]).lower()
        assert "client" not in evidence and "caller" not in evidence and "user-entered" not in evidence


def test_the_matrix_covers_every_capability_the_issue_named():
    """media, history and server tools must each be decided, not silently omitted."""
    decided = {row["capability"] for row in matrix()["rows"]}
    assert {"media", "history", "server_tools"} <= decided


# ---------------------------------------------------------------------------
# The pricing evidence behind the server-tool refusal
# ---------------------------------------------------------------------------


def test_every_published_money_field_is_a_per_token_rate():
    """The decisive fact for the server_tools rows.

    A hosted tool bills per call. If the rate schema cannot express a per-call
    fee, no arithmetic over it can bound one, so the row must be refused rather
    than estimated. This asserts that property of ``RateRow`` directly, so the
    day a per-call rate is published this test fails and the matrix gets revisited.
    """
    names = [f.name for f in dataclasses.fields(RateRow)]
    money = [name for name in names if "price" in name]
    assert money, "RateRow must publish some prices"
    for name in money:
        assert name.endswith("_per_1k_tokens"), f"{name} is not a per-token rate"

    forbidden = ("per_call", "per_search", "per_image", "per_hour", "per_minute", "per_session", "per_request")
    for name in names:
        assert not any(unit in name for unit in forbidden), f"{name} suggests a non-token fee"

    evidence = matrix()["pricing_evidence"]
    assert evidence["non_token_fee_fields_published"] == []
    assert sorted(evidence["money_fields_published"]) == sorted(money)


# ---------------------------------------------------------------------------
# Admitted rows really do produce a bound
# ---------------------------------------------------------------------------


async def test_the_anthropic_inline_media_row_is_really_admitted():
    row = rows_by_name()["anthropic_inline_media"]
    assert row["status"] == "admitted"
    source = {"type": "base64", "media_type": "image/png", "data": "AAAA"}
    raw = anthropic(messages=[{"role": "user", "content": [{"type": "image", "source": source}]}])
    result = await quote_request(raw, ANTHROPIC_PATH, now=NOW)
    assert result.total_usd > 0
    assert result.capability == Capability.MEDIA
    # The bound is the published ceiling, exactly as the row claims — not a count of the image.
    assert result.max_input_tokens > 0


async def test_the_responses_inline_media_row_is_really_admitted():
    row = rows_by_name()["openai_responses_inline_media"]
    assert row["status"] == "admitted"
    part = {"type": "input_image", "image_url": "data:image/png;base64,AAAA"}
    raw = responses(input=[{"role": "user", "content": [part]}])
    result = await quote_request(raw, RESPONSES_PATH, now=NOW)
    assert result.total_usd > 0 and result.capability == Capability.MEDIA


@pytest.mark.parametrize(
    "source_type, payload",
    [("base64", {"data": "AAAA"}), ("text", {"data": "notes"}), ("content", {"content": [{"type": "text", "text": "n"}]})],
)
async def test_every_accepted_anthropic_media_source_in_the_row_is_admitted(source_type, payload):
    """The row lists its accepted source types; each must actually quote."""
    accepted = rows_by_name()["anthropic_inline_media"]["accepted_input_shape"]["source_types"]
    assert source_type in accepted
    source = {"type": source_type, "media_type": "application/pdf", **payload}
    raw = anthropic(messages=[{"role": "user", "content": [{"type": "document", "source": source}]}])
    assert (await quote_request(raw, ANTHROPIC_PATH, now=NOW)).total_usd > 0


@pytest.mark.parametrize(
    "part_type, part",
    [
        ("input_image", {"type": "input_image", "image_url": "data:image/png;base64,AAAA"}),
        ("input_file", {"type": "input_file", "file_data": "data:application/pdf;base64,AAAA"}),
        ("input_audio", {"type": "input_audio", "input_audio": {"data": "UklGRg==", "format": "wav"}}),
    ],
)
async def test_every_content_part_type_the_responses_row_lists_is_admitted(part_type, part):
    """Each part type the row advertises must quote in the shape the API defines.

    The row previously listed ``input_audio`` while naming an ``audio_url`` payload
    field the Responses API does not define, so the only spec-compliant audio part
    was refused by the adapter that claimed to admit it. Binding the advertised
    part types to real quotes keeps the matrix from promising a capability the
    adapter rejects.
    """
    shape = rows_by_name()["openai_responses_inline_media"]["accepted_input_shape"]
    assert part_type in shape["content_part_types"]
    raw = responses(input=[{"role": "user", "content": [part]}])
    result = await quote_request(raw, RESPONSES_PATH, now=NOW)
    assert result.total_usd > 0 and result.capability == Capability.MEDIA


def test_the_responses_row_names_the_payload_field_the_adapter_actually_reads():
    """The row's per-part payload field must match what the adapter requires.

    The row previously named an ``audio_url`` field the adapter never read (and the
    API never defined), which is how the matrix came to advertise audio the adapter
    refused. Binding the row to the adapter's own map keeps the two in step.
    """
    from src.orchestration.quote_capabilities import MEDIA_PART_PAYLOADS

    shape = rows_by_name()["openai_responses_inline_media"]["accepted_input_shape"]
    advertised = shape["payload_field_per_part_type"]
    assert advertised == {part: field for part, (field, _kind) in MEDIA_PART_PAYLOADS.items()}
    # The part types the row lists and the map's keys are the same set.
    assert set(shape["content_part_types"]) == set(MEDIA_PART_PAYLOADS)


# ---------------------------------------------------------------------------
# Refused rows refuse for the stated reason
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "source",
    [{"type": "url", "url": "https://example.invalid/a.png"}, {"type": "file", "file_id": "file_1"}],
)
async def test_the_media_by_reference_row_refuses_on_the_anthropic_route(source):
    expected = rows_by_name()["media_by_reference"]["refusal_reason"]
    raw = anthropic(messages=[{"role": "user", "content": [{"type": "image", "source": source}]}])
    assert await refuse_reason(raw, ANTHROPIC_PATH) == (expected, Capability.MEDIA)


@pytest.mark.parametrize(
    "source",
    [{"type": "url", "url": "https://example.invalid/a.png"}, {"type": "file", "file_id": "file_1"}],
)
async def test_the_reference_row_holds_at_every_depth_it_can_be_nested(source):
    """The row's refusal must not be escapable by nesting the reference.

    An Anthropic ``content`` source carries a block list, so a refused reference
    can be placed one level inside an admitted inline source. The row claims the
    shape is refused, not that it is refused only at the top level.
    """
    expected = rows_by_name()["media_by_reference"]["refusal_reason"]
    inner = {"type": "image", "source": source}
    raw = anthropic(messages=[{"role": "user", "content": [{"type": "document", "source": {"type": "content", "content": [inner]}}]}])
    assert await refuse_reason(raw, ANTHROPIC_PATH) == (expected, Capability.MEDIA)


@pytest.mark.parametrize(
    "part",
    [
        {"type": "input_file", "file_id": "file-1"},
        {"type": "input_file", "file_url": "https://example.invalid/a.pdf"},
        {"type": "input_image", "image_url": "https://example.invalid/a.png"},
    ],
)
async def test_the_media_by_reference_row_refuses_on_the_responses_route(part):
    expected = rows_by_name()["media_by_reference"]["refusal_reason"]
    raw = responses(input=[{"role": "user", "content": [part]}])
    assert await refuse_reason(raw, RESPONSES_PATH) == (expected, Capability.MEDIA)


@pytest.mark.parametrize("field", ["previous_response_id", "context_management", "container", "mcp_servers"])
async def test_the_history_row_refuses_on_the_anthropic_route(field):
    expected = rows_by_name()["server_retained_history"]["refusal_reason"]
    assert await refuse_reason(anthropic(**{field: "x"}), ANTHROPIC_PATH) == (expected, Capability.HISTORY)


@pytest.mark.parametrize("field", ["previous_response_id", "conversation", "prompt"])
async def test_the_history_row_refuses_on_the_responses_route(field):
    expected = rows_by_name()["server_retained_history"]["refusal_reason"]
    assert await refuse_reason(responses(**{field: "srv-1"}), RESPONSES_PATH) == (expected, Capability.HISTORY)


@pytest.mark.parametrize("tool", ["web_search", "code_execution"])
async def test_the_server_tool_row_refuses_on_the_anthropic_route(tool):
    expected = rows_by_name()["server_side_tools_and_mcp"]["refusal_reason"]
    raw = anthropic(tools=[{"type": tool}])
    assert await refuse_reason(raw, ANTHROPIC_PATH) == (expected, Capability.SERVER_TOOLS)


@pytest.mark.parametrize("tool", ["web_search", "file_search", "code_interpreter", "mcp", "image_generation"])
async def test_the_server_tool_row_refuses_on_the_responses_route(tool):
    expected = rows_by_name()["server_side_tools_and_mcp"]["refusal_reason"]
    assert await refuse_reason(responses(tools=[{"type": tool}]), RESPONSES_PATH) == (expected, Capability.SERVER_TOOLS)


async def test_the_bound_is_independent_of_media_size_and_content():
    """The property that makes media admittable, and that reframes the reference refusal.

    The bound counts nothing: it reserves the whole published context window. So a
    tiny image, a 200KB image and plain text all price identically. This is worth
    pinning because it is the premise of two separate claims in the matrix — that
    admitting media adds no unbounded cost, and that the media_by_reference row is
    refused on authorization/fetcher grounds rather than on cost.
    """

    def media(payload: str) -> bytes:
        return _responses_media({"type": "input_image", "image_url": "data:image/png;base64," + payload})

    tiny = await quote_request(media("A" * 8), RESPONSES_PATH, now=NOW)
    huge = await quote_request(media("A" * 200_000), RESPONSES_PATH, now=NOW)
    text = await quote_request(responses(), RESPONSES_PATH, now=NOW)
    assert tiny.total_usd == huge.total_usd == text.total_usd
    assert tiny.max_input_tokens == huge.max_input_tokens == text.max_input_tokens


def test_the_reference_refusal_does_not_claim_a_cost_reason():
    """Guards the rationale itself against reverting to the plausible-but-wrong one.

    Since the bound is content-independent (asserted above), "we cannot count the
    bytes so the cost is unbounded" would be false. If someone reintroduces that
    wording, this fails and points them at the real reasons.
    """
    why = " ".join(rows_by_name()["media_by_reference"]["why_unboundable"]).lower()
    assert "authorization" in why and "fetcher" in why
    assert "prior count" not in why, "the bound computes no count, so it cannot be reused"


async def test_a_client_executed_tool_is_not_caught_by_the_server_tool_row():
    """The refusal must not take client-side function calling down with it."""
    raw = anthropic(tools=[{"type": "custom", "name": "lookup"}])
    assert (await quote_request(raw, ANTHROPIC_PATH, now=NOW)).total_usd > 0


# ---------------------------------------------------------------------------
# Refusing one row must not break the already-supported paths, and must leak nothing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, path",
    [
        (anthropic(tools=[{"type": "web_search"}]), ANTHROPIC_PATH),
        (anthropic(previous_response_id="resp_1"), ANTHROPIC_PATH),
        (responses(tools=[{"type": "mcp"}]), RESPONSES_PATH),
        (responses(previous_response_id="resp_1"), RESPONSES_PATH),
    ],
)
async def test_a_refusal_does_not_disturb_the_supported_text_paths(raw, path):
    """#5227 requires that refusing an unsupported combination leave text working."""
    await refuse_reason(raw, path)
    assert (await quote_request(anthropic(), ANTHROPIC_PATH, now=NOW)).total_usd > 0
    assert (await quote_request(responses(), RESPONSES_PATH, now=NOW)).total_usd > 0


@pytest.mark.parametrize(
    "raw, path, secret",
    [
        (_anthropic_media({"type": "url", "url": "https://x.invalid/s3cret.png"}), ANTHROPIC_PATH, "s3cret"),
        (_responses_media({"type": "input_file", "file_url": "https://x.invalid/s3cret.pdf"}), RESPONSES_PATH, "s3cret"),
        # Admissible media plus a refused tool: the bytes reached the parser, so this
        # is the case where a careless implementation would echo them.
        (_anthropic_media({"type": "base64", "data": "s3cretBYTES"}, tools=[{"type": "web_search"}]), ANTHROPIC_PATH, "s3cretBYTES"),
    ],
)
async def test_a_refusal_emits_only_sanitized_identifiers(raw, path, secret):
    """Evidence may name the capability and reason, never media content or prompts."""
    with pytest.raises(QuoteRefusedError) as exc:
        await quote_request(raw, path, now=NOW)
    refusal = exc.value.refusal
    rendered = " ".join(str(part) for part in (refusal.reason, refusal.capability, refusal.detail, str(refusal), str(exc.value)))
    assert secret not in rendered
    assert "hello" not in rendered  # the prompt text never appears either
    assert refusal.reason in QuoteReason.ALL
    assert refusal.capability in Capability.ALL


# ---------------------------------------------------------------------------
# What stays open on the parent
# ---------------------------------------------------------------------------


def test_the_matrix_publishes_its_remaining_limitations():
    """#5227 must hand #5175 an explicit list, not an implied one."""
    remaining = matrix()["remaining_limitations_for_5175"]
    joined = " ".join(remaining)
    assert "server_retained_history" in joined and "server_side_tools_and_mcp" in joined
    refused = {row["row"] for row in matrix()["rows"] if row["status"] == "refused"}
    # Every capability-level refusal (as opposed to a shape-level one) must be published.
    assert {"server_retained_history", "server_side_tools_and_mcp"} <= refused
