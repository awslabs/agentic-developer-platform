"""Bounded upper-cost quote for OpenAI-family Responses requests (#5226).

``POST /openai/v1/responses`` is an enforced proxy path, so a policy-governed
request to it must carry a bound before dispatch. Until this adapter existed no
adapter claimed the route, so ``quote_request`` answered ``NO_ADAPTER`` and every
policy-governed Responses call — including a small, ordinary, perfectly bounded
text one — was refused before reaching the provider.

The bound here is deliberately pessimistic, for a reason specific to the deployed
endpoint. The gateway forwards this route to **AWS Bedrock's OpenAI-compatible
endpoint** (``bedrock-runtime.<region>.amazonaws.com``; see ``mantle_base_url``),
not to ``api.openai.com``, and that endpoint publishes no token-counting API. So
there is no trustworthy exact input count available before submission, and a
client-supplied count is not evidence — a caller must never price its own
request. What IS published, per model, is a provider-enforced full-context
ceiling and a rate table. This adapter therefore reserves the model's ENTIRE
published context window at the dearest published input-side rate, plus the
caller's requested ``max_output_tokens`` at the dearest published output rate.

That over-reserves for a small request. It is the safe direction: settlement
returns the difference as soon as the provider reports real usage, whereas a
bound below the true cost would admit an overspend that cannot be undone. A
model with no published ceiling (the ``gpt-oss`` family publishes none) is a
named capability block, not an occasion to estimate one.

Scope is the issue's initial positive set: self-contained text with an explicit
positive output maximum. Server-retained history, hosted tools, media and
background execution are refused BEFORE submission with the reason naming what
is missing; #5227 owns admitting them.

Two Responses-specific details drive the code below and differ from the
Anthropic adapter, so they are not shared with it:

* The output cap is ``max_output_tokens`` (not ``max_tokens``), and it bounds
  reasoning tokens together with visible output — which is why reasoning needs
  no separate headroom.
* Reported ``input_tokens`` is INCLUSIVE of cached input on this API, where the
  Anthropic block is additive. Folding cache reads in again here would
  double-count them against the settled total.

Provenance for every limit and rate this relies on is recorded in
``tests/orchestration/fixtures/responses-provider-contract-5226.json``.
"""

from __future__ import annotations

import json
from datetime import timedelta
from decimal import ROUND_UP, Decimal
from typing import Any

from src.orchestration.provider_quotes import (
    LEDGER_QUANTUM,
    QUOTE_TTL_SECONDS,
    Capability,
    ProviderQuote,
    QuoteReason,
    QuoteRefusedError,
    QuoteRequest,
    TrustedUsage,
    _pricing_revision,
    refuse,
)

#: The one route this adapter owns. Mirrors ``MANTLE_RESPONSES_PATH``; kept as a
#: literal so importing the quote layer does not drag in the proxy service.
RESPONSES_PATH = "/openai/v1/responses"

#: Content part types that are ordinary text tokens. Anything else — images,
#: audio, files — needs a counting capability this adapter does not have.
_TEXT_PART_TYPES = frozenset({"input_text", "output_text", "text", "summary_text", "refusal"})

#: Request fields naming state the gateway cannot read, and therefore cannot
#: count: a server-retained prior turn, a server-side conversation, or a
#: server-stored prompt template whose body never transits this proxy.
_STATEFUL_FIELDS = ("previous_response_id", "conversation", "prompt")

#: Tool types billed by the provider as ordinary input. A ``function`` tool is
#: executed by the CLIENT, so its declaration is just tokens in the request.
#: Every other type runs server-side and bills separately (#5227).
_CLIENT_TOOL_TYPES = frozenset({"function", "custom"})


class OpenAIResponsesQuoteAdapter:
    """Full-context upper bound for bounded OpenAI-family Responses text calls.

    Implements ``ProviderQuoteAdapter`` for ``RESPONSES``. Every method may raise
    ``QuoteRefusedError``: anything this cannot bound from published evidence is
    refused and named rather than estimated.
    """

    provider = "openai"
    capability = Capability.RESPONSES

    def handles(self, path: str) -> bool:
        return path == RESPONSES_PATH

    async def quote(self, request: QuoteRequest) -> ProviderQuote:
        # No I/O: the snapshot is a cached immutable file and the live rate state
        # is a synchronous in-process read. Async satisfies the protocol, which
        # admits adapters that do need a provider round trip.
        return self.bound(request)

    def bound(self, request: QuoteRequest) -> ProviderQuote:
        from pricing_policy import canonical_billing_model_id, load_snapshot
        from pricing_policy.policy import is_openai_model, model_rate_candidates
        from src.budget.pricing_v2_reader import cached_rate_state

        document = _parse(request.body)
        billing_model = canonical_billing_model_id(_model_id(document))
        if not is_openai_model(billing_model):
            # This route serves the OpenAI families only. A non-OpenAI id here has
            # no published Responses rate, so there is nothing to bound it with.
            raise refuse(QuoteReason.UNPUBLISHED_MODEL_PRICE, self.capability, "no published bounded Responses quote for this model")

        output = document.get("max_output_tokens")
        if isinstance(output, bool) or not isinstance(output, int) or output <= 0:
            # Without an explicit positive cap the output side is unbounded, and
            # an unbounded side cannot be reserved against a fixed budget.
            raise refuse(QuoteReason.UNBOUNDED_OUTPUT, self.capability, "explicit positive max_output_tokens required")

        self._reject_unbounded_features(document)

        snapshot = load_snapshot()
        state = cached_rate_state()
        rows = model_rate_candidates(snapshot.rates + state.rows, billing_model)
        if not rows:
            raise refuse(QuoteReason.UNPUBLISHED_MODEL_PRICE, self.capability, "published model pricing unavailable")

        context_limit = _published_context_ceiling(snapshot.models.get(billing_model, {}), rows)
        if context_limit is None:
            # The gpt-oss family lands here: priced, but with no published
            # context ceiling, so the input side has no evidenced maximum. That
            # is a capability block, not a number to invent.
            raise refuse(QuoteReason.UNPUBLISHED_CONTEXT_BOUND, self.capability, "published model context ceiling unavailable")

        # A non-finite published rate cannot be compared or multiplied, and must
        # never propagate into a bound. Reject the rows rather than let Decimal
        # raise partway through the arithmetic.
        published = [
            rate
            for row in rows
            for rate in (
                row.input_price_per_1k_tokens,
                row.output_price_per_1k_tokens,
                row.cache_write_price_per_1k_tokens,
                row.cache_write_1h_price_per_1k_tokens,
            )
            if rate is not None
        ]
        if any(not rate.is_finite() for rate in published):
            raise refuse(QuoteReason.UNPUBLISHED_MODEL_PRICE, self.capability, "published rate is not a finite amount")

        # Worst case across every published variant of this model: context tier,
        # geography and service tier all included. Taking the maximum is what
        # makes the bound hold whichever variant the provider actually serves,
        # so a tier or region change cannot exceed it.
        input_rate = max(
            max(
                row.input_price_per_1k_tokens,
                row.cache_write_price_per_1k_tokens or Decimal(0),
                row.cache_write_1h_price_per_1k_tokens or Decimal(0),
            )
            for row in rows
        )
        output_rate = max(row.output_price_per_1k_tokens for row in rows)
        total = (Decimal(context_limit) * input_rate + Decimal(output) * output_rate) / 1000
        if not total.is_finite() or total <= 0:
            raise refuse(QuoteReason.UNPUBLISHED_MODEL_PRICE, self.capability, "model price unavailable")

        issued_at = request.issued_at
        return ProviderQuote(
            provider=self.provider,
            endpoint=request.path,
            capability=self.capability,
            billing_model_id=billing_model,
            request_sha256=request.digest,
            pricing_revision=_pricing_revision(snapshot.snapshot_version, state.source, state.generation_id, state.pointer_revision),
            currency="USD",
            issued_at=issued_at,
            expires_at=issued_at + timedelta(seconds=QUOTE_TTL_SECONDS),
            # The whole window may arrive as fresh input OR as cache writes, and
            # the rate above already takes the dearer of the two, so both maxima
            # are the full window rather than a split that could understate one.
            max_input_tokens=context_limit,
            # Reasoning tokens bill as output and sit under this same cap, so the
            # caller's requested maximum covers them.
            max_output_tokens=output,
            # Server-side tools are refused above, so no tool-side cost is
            # admitted. Client function declarations are ordinary input, already
            # inside the context bound.
            max_tool_tokens=0,
            max_cache_write_tokens=context_limit,
            total_usd=total.quantize(LEDGER_QUANTUM, rounding=ROUND_UP),
            evidence_source=state.source,
        )

    def current_pricing_revision(self) -> str:
        """The revision a quote issued right now would carry.

        Reads the same two sources ``bound`` does — the bundled snapshot and the
        cached live rate state — so a rollover in either is visible here.
        """
        from pricing_policy import load_snapshot
        from src.budget.pricing_v2_reader import cached_rate_state

        snapshot = load_snapshot()
        state = cached_rate_state()
        return _pricing_revision(snapshot.snapshot_version, state.source, state.generation_id, state.pointer_revision)

    async def validate(self, quote: ProviderQuote, request: QuoteRequest) -> None:
        from pricing_policy import canonical_billing_model_id, load_snapshot
        from src.budget.pricing_v2_reader import cached_rate_state

        document = _parse(request.body)
        snapshot = load_snapshot()
        state = cached_rate_state()
        refusal = quote.binds(
            request_sha256=request.digest,
            billing_model_id=canonical_billing_model_id(_model_id(document)),
            pricing_revision=_pricing_revision(snapshot.snapshot_version, state.source, state.generation_id, state.pointer_revision),
            now=request.now,
        )
        if refusal is not None:
            raise QuoteRefusedError(refusal)

    async def reconcile(self, response: Any) -> TrustedUsage:
        """Interpret the provider's own final usage block.

        Trusted only when complete and internally consistent. Absent, negative,
        non-integral or contradictory counts return ``unknown``, which retains
        the reservation — a truncated stream or a failed call must never settle
        as free.

        Unlike the Anthropic block, ``input_tokens`` here already INCLUDES cached
        input, so cached counts are cross-checked for consistency and never
        added on top.
        """
        usage = response.get("usage") if isinstance(response, dict) else None
        if not isinstance(usage, dict):
            return TrustedUsage.unknown("provider usage absent")

        counts: dict[str, int] = {}
        for name in ("input_tokens", "output_tokens"):
            value = usage.get(name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                return TrustedUsage.unknown(f"provider usage {name} missing or not a count")
            counts[name] = value

        # Cached input is a subset of input_tokens on this API, never an addend.
        # A cache count that cannot sit inside the total it is part of means the
        # counters disagree, and a total we cannot reconcile is not a total we
        # can settle a reservation against.
        #
        # The evidence read here must be the SAME evidence production pricing
        # reads, or this gate is bypassable by spelling. `normalize_usage` takes
        # cache reads and writes from either a top-level counter or the nested
        # details block, and CLAMPS a counter that will not fit — recording
        # `valid=False` while still returning a billable total. Checking only one
        # of those spellings would leave the others releasing holds against
        # clamped totals, which is the defect this gate exists to prevent.
        details = usage.get("input_tokens_details")
        if details is not None and not isinstance(details, dict):
            return TrustedUsage.unknown("provider usage input_tokens_details ambiguous")
        nested = details if isinstance(details, dict) else {}
        cache_total = 0
        for name, nested_name in (("cache_read_input_tokens", "cached_tokens"), ("cache_creation_input_tokens", "cache_write_tokens")):
            # Same precedence as normalize_usage: top-level counter first, then
            # the nested spelling.
            reported = usage[name] if name in usage else nested.get(nested_name)
            if reported is None:
                continue
            if isinstance(reported, bool) or not isinstance(reported, int) or reported < 0:
                return TrustedUsage.unknown(f"provider usage {name} ambiguous")
            cache_total += reported
        # Cache reads and writes are disjoint parts of the inclusive input total,
        # so it is their SUM that must fit — each one fitting alone is not enough.
        if cache_total > counts["input_tokens"]:
            return TrustedUsage.unknown("provider usage cache counters exceed input_tokens")

        # Reasoning tokens are a subset of output_tokens, checked the same way
        # and likewise never added on top.
        output_details = usage.get("output_tokens_details")
        if output_details is not None:
            if not isinstance(output_details, dict):
                return TrustedUsage.unknown("provider usage output_tokens_details ambiguous")
            reasoning = output_details.get("reasoning_tokens")
            if reasoning is not None:
                if isinstance(reasoning, bool) or not isinstance(reasoning, int) or reasoning < 0:
                    return TrustedUsage.unknown("provider usage reasoning_tokens ambiguous")
                if reasoning > counts["output_tokens"]:
                    return TrustedUsage.unknown("provider usage reasoning_tokens exceeds output_tokens")

        return TrustedUsage(input_tokens=counts["input_tokens"], output_tokens=counts["output_tokens"], known=True)

    # -- request shape ------------------------------------------------------

    @classmethod
    def _reject_unbounded_features(cls, document: dict[str, Any]) -> None:
        """Refuse everything outside the bounded text scope, naming what is missing."""
        for key in _STATEFUL_FIELDS:
            if document.get(key):
                raise refuse(QuoteReason.STATEFUL_INPUT, Capability.HISTORY, f"{key} cannot be counted locally")

        # Background execution completes server-side after this request returns,
        # so no trusted final usage arrives in band to settle against.
        if document.get("background"):
            raise refuse(QuoteReason.STATEFUL_INPUT, Capability.HISTORY, "background execution settles out of band")

        # A caller-supplied usage or token count is not evidence about cost. The
        # only trusted counts are the provider's own, at settlement.
        for key in ("usage", "input_tokens", "max_input_tokens"):
            if key in document:
                raise refuse(QuoteReason.MALFORMED_REQUEST, Capability.RESPONSES, f"client-supplied {key} is not accepted as a count")

        tools = document.get("tools", [])
        if not isinstance(tools, list):
            raise refuse(QuoteReason.MALFORMED_REQUEST, Capability.RESPONSES, "tools must be a list")
        for tool in tools:
            if not isinstance(tool, dict):
                raise refuse(QuoteReason.MALFORMED_REQUEST, Capability.RESPONSES, "tool must be an object")
            if tool.get("type") not in _CLIENT_TOOL_TYPES:
                raise refuse(QuoteReason.SERVER_TOOL_COST, Capability.SERVER_TOOLS, "server-side tool costs require a scoped quote")

        cls._text_only(document.get("instructions", ""))

        if "input" not in document:
            raise refuse(QuoteReason.MALFORMED_REQUEST, Capability.RESPONSES, "explicit input required")
        cls._input_text_only(document["input"])

    @classmethod
    def _input_text_only(cls, value: Any) -> None:
        """``input`` is either a bare string or a list of typed items."""
        if isinstance(value, str):
            if not value:
                raise refuse(QuoteReason.MALFORMED_REQUEST, Capability.RESPONSES, "explicit input required")
            return
        if not isinstance(value, list) or not value:
            raise refuse(QuoteReason.MALFORMED_REQUEST, Capability.RESPONSES, "explicit input required")
        for item in value:
            if not isinstance(item, dict):
                raise refuse(QuoteReason.MALFORMED_REQUEST, Capability.RESPONSES, "input item must be an object")
            # The item TYPE alone decides whether this is text this adapter can
            # bound. A non-message item references output or tool state produced
            # earlier (function_call, reasoning, item_reference,
            # file_search_call, ...); its cost profile is not this capability's,
            # so it is refused by name. Deciding on a `content` key instead would
            # let any of those kinds in simply by carrying one — a client-supplied
            # field — which is exactly the pre-submission refusal this capability
            # owes. #5227 owns admitting these kinds properly.
            kind = item.get("type", "message")
            if kind != "message":
                raise refuse(QuoteReason.STATEFUL_INPUT, Capability.HISTORY, f"input item type {kind!r} cannot be counted locally")
            cls._text_only(item.get("content", ""))

    @classmethod
    def _text_only(cls, content: Any) -> None:
        if isinstance(content, str):
            return
        if not isinstance(content, list):
            raise refuse(QuoteReason.MALFORMED_REQUEST, Capability.RESPONSES, "unsupported content")
        for part in content:
            if not isinstance(part, dict) or part.get("type") not in _TEXT_PART_TYPES:
                raise refuse(QuoteReason.NON_TEXT_CONTENT, Capability.MEDIA, "non-text token-count capability required")


def _published_context_ceiling(model_entry: dict[str, Any], rows: tuple[Any, ...]) -> int | None:
    """The largest provider-documented full-context ceiling, or ``None``.

    Two published sources carry it, and they are read together on purpose. The
    model card's ``context_tiers`` is the documented window per tier; the rate
    rows carry ``max_input_tokens`` for the variants actually priced. Taking the
    maximum across both keeps the bound above whichever the provider enforces —
    the conservative direction. ``None`` means no source published one at all,
    which must be refused rather than defaulted.
    """
    ceilings: list[int] = []
    tiers = model_entry.get("context_tiers")
    if isinstance(tiers, dict):
        ceilings.extend(value for value in tiers.values() if not isinstance(value, bool) and isinstance(value, int) and value > 0)
    ceilings.extend(
        row.max_input_tokens
        for row in rows
        if not isinstance(row.max_input_tokens, bool) and isinstance(row.max_input_tokens, int) and row.max_input_tokens > 0
    )
    return max(ceilings) if ceilings else None


def _parse(body: bytes) -> dict[str, Any]:
    """Parse the exact forwarded bytes, rejecting anything ambiguous."""

    def unique_object(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                # A duplicated field means two readers can disagree about what
                # was requested; that is not a quotable request.
                raise refuse(QuoteReason.MALFORMED_REQUEST, Capability.RESPONSES, "ambiguous request field")
            value[key] = item
        return value

    try:
        document = json.loads(body, object_pairs_hook=unique_object)
    except (ValueError, TypeError) as exc:
        raise refuse(QuoteReason.MALFORMED_REQUEST, Capability.RESPONSES, "request body is not valid JSON") from exc
    if not isinstance(document, dict):
        raise refuse(QuoteReason.MALFORMED_REQUEST, Capability.RESPONSES, "unsupported model request")
    return document


def _model_id(document: dict[str, Any]) -> str:
    """The requested model.

    Unlike the Bedrock ``/model/{id}/invoke`` route there is no server-derived
    model in the path here — this route reads it from the body, and so must the
    quote, or the two could price different models.
    """
    model = document.get("model")
    if not isinstance(model, str) or not model:
        raise refuse(QuoteReason.UNPUBLISHED_MODEL_PRICE, Capability.RESPONSES, "request names no model")
    return model


__all__ = ["RESPONSES_PATH", "OpenAIResponsesQuoteAdapter"]
