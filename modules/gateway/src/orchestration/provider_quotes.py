"""Typed provider quote contracts, bound to the exact request they priced.

A policy-governed model request may only reach an upstream provider once its
*total* cost has been bounded. Before this module the bound was a bare
``Decimal``: correct for the one supported case (bounded Anthropic text) but
impossible to extend safely, because nothing tied the number to the request it
came from. A quote computed for one payload, model or published rate revision
could be spent against another and no type would notice.

Three rules shape everything here:

1. **A quote is bound, immutable evidence.** ``ProviderQuote`` carries the
   request-byte hash, the canonical billing model and the pricing revision that
   produced it. ``binds()`` is the gate before spending; a payload, model or
   rate-revision change fails it and must be requoted.
2. **A refusal is a value, never a zero.** ``QuoteRefusal`` carries a stable
   reason and the missing capability. It is deliberately not a ``ProviderQuote``
   with ``total_usd=0``, so no arithmetic path can sum an unbounded request into
   a budget as free.
3. **No adapter means no upstream effect.** ``adapter_for`` returns ``None``
   rather than a default. There is no estimate, no unknown/default model price
   and no client-supplied token count to fall back to.

Scope: this module owns the boundary and ports the existing full-context
Anthropic text bound behind it without reducing its safety margin. The Responses
adapter (#5226) and the media/history/tool capabilities (#5227) implement
``ProviderQuoteAdapter``; they are not defined here.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_UP, Decimal
from typing import Any, Protocol, runtime_checkable
from urllib.parse import unquote

#: USD, rounded up to the ledger's micro-dollar granularity. Rounding a bound
#: DOWN would admit a request whose true cost exceeds it, so every quantize in
#: this module uses ROUND_UP.
LEDGER_QUANTUM = Decimal("0.000001")

#: How long a quote may be held between admission and submission. Short on
#: purpose: a published rate generation can roll over, and spending against a
#: stale bound is exactly what `binds()` exists to prevent.
QUOTE_TTL_SECONDS = 120

#: Ceiling on the confirmation at the spend boundary. The one adapter here reads
#: process-local state, but the protocol admits a provider round trip (#5226),
#: and an adapter that cannot say whether a quote still holds must not be able
#: to stall the reservation path. A lapsed budget is a refusal, never a pass.
QUOTE_CONFIRM_TIMEOUT_SECONDS = 2.0


def _now() -> datetime:
    """The module's clock, indirected so tests can advance it.

    Comparing an expiry against a patchable seam rather than calling
    ``datetime.now`` inline is what lets a test reproduce a TTL lapsing at a
    specific point in a live request without sleeping through it.
    """
    return datetime.now(UTC)


class Capability:
    """The provider capability a request needs priced.

    An adapter declares the one it bounds. A request needing a capability with no
    registered adapter is refused, never estimated.
    """

    TEXT = "text"
    MEDIA = "media"  # #5227
    HISTORY = "history"  # #5227
    SERVER_TOOLS = "server_tools"  # #5227
    RESPONSES = "responses"  # #5226

    ALL = frozenset({TEXT, MEDIA, HISTORY, SERVER_TOOLS, RESPONSES})


class QuoteReason:
    """Stable refusal reasons. Values are contract — callers may branch on them."""

    NO_ADAPTER = "no_adapter"
    MALFORMED_REQUEST = "malformed_request"
    UNBOUNDED_OUTPUT = "unbounded_output"
    UNPUBLISHED_MODEL_PRICE = "unpublished_model_price"
    UNPUBLISHED_CONTEXT_BOUND = "unpublished_context_bound"
    STATEFUL_INPUT = "stateful_input"
    NON_TEXT_CONTENT = "non_text_content"
    SERVER_TOOL_COST = "server_tool_cost"
    QUOTE_EXPIRED = "quote_expired"
    REQUEST_CHANGED = "request_changed"
    #: The adapter did not answer within QUOTE_CONFIRM_TIMEOUT_SECONDS. Distinct
    #: from a rate change: nothing is known to have moved, but nothing is
    #: confirmed either, and an unconfirmed quote cannot be spent.
    ADAPTER_TIMEOUT = "adapter_timeout"

    ALL = frozenset(
        {
            NO_ADAPTER,
            MALFORMED_REQUEST,
            UNBOUNDED_OUTPUT,
            UNPUBLISHED_MODEL_PRICE,
            UNPUBLISHED_CONTEXT_BOUND,
            STATEFUL_INPUT,
            NON_TEXT_CONTENT,
            SERVER_TOOL_COST,
            QUOTE_EXPIRED,
            REQUEST_CHANGED,
            ADAPTER_TIMEOUT,
        }
    )


@dataclass(frozen=True)
class QuoteRefusal:
    """A structured refusal to bound a request's total cost.

    Not a quote. There is no ``total_usd`` here on purpose: a caller that wants a
    number must handle the refusal, and the only correct handling is to forward
    nothing upstream.
    """

    reason: str
    capability: str
    detail: str = ""

    def __post_init__(self) -> None:
        if self.reason not in QuoteReason.ALL:
            raise ValueError(f"unknown quote refusal reason: {self.reason!r}")
        if self.capability not in Capability.ALL:
            raise ValueError(f"unknown capability: {self.capability!r}")


class QuoteRefusedError(Exception):
    """Raised by adapters. Carries the structured refusal as ``.refusal``."""

    def __init__(self, refusal: QuoteRefusal):
        super().__init__(f"{refusal.reason}:{refusal.capability}")
        self.refusal = refusal


def refuse(reason: str, capability: str = Capability.TEXT, detail: str = "") -> QuoteRefusedError:
    return QuoteRefusedError(QuoteRefusal(reason=reason, capability=capability, detail=detail))


def request_digest(body: bytes) -> str:
    """SHA-256 over the EXACT bytes that will be forwarded upstream.

    Hashing the raw frames, not a reparse, is what makes the binding meaningful:
    the upstream JSON is never reserialized, so this digest identifies the
    request the provider actually receives.
    """
    return hashlib.sha256(body).hexdigest()


@dataclass(frozen=True)
class QuoteRequest:
    """The exact bytes and route to be priced."""

    body: bytes
    path: str
    now: datetime | None = None

    @property
    def digest(self) -> str:
        return request_digest(self.body)

    @property
    def issued_at(self) -> datetime:
        return self.now or _now()


@dataclass(frozen=True)
class ProviderQuote:
    """An immutable upper bound on one request's total cost.

    Frozen so a quote cannot be edited between admission and spend. The three
    identity fields — ``request_sha256``, ``billing_model_id``,
    ``pricing_revision`` — are what ``binds()`` re-checks immediately before the
    reservation, so a different payload, model or rate revision cannot consume
    this quote.
    """

    provider: str
    endpoint: str
    capability: str
    billing_model_id: str
    request_sha256: str
    pricing_revision: str
    currency: str
    issued_at: datetime
    expires_at: datetime
    max_input_tokens: int
    max_output_tokens: int
    max_tool_tokens: int
    max_cache_write_tokens: int
    total_usd: Decimal
    evidence_source: str

    def __post_init__(self) -> None:
        if self.currency != "USD":
            raise ValueError(f"unsupported quote currency: {self.currency!r}")
        if not self.total_usd.is_finite() or self.total_usd <= 0:
            # A non-finite or non-positive bound is not a cheap request; it is an
            # absent bound wearing a number's clothes.
            raise ValueError("quote total must be a finite positive amount")
        if self.expires_at <= self.issued_at:
            raise ValueError("quote must expire after it is issued")
        for name in ("max_input_tokens", "max_output_tokens", "max_tool_tokens", "max_cache_write_tokens"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")

    def expired(self, now: datetime | None = None) -> bool:
        return (now or _now()) >= self.expires_at

    def binds(self, *, request_sha256: str, billing_model_id: str, pricing_revision: str, now: datetime | None = None) -> QuoteRefusal | None:
        """``None`` when this quote may still be spent; a refusal otherwise.

        Called immediately before the reservation. Returning a refusal rather
        than a bool keeps the reason for a requote stable and reportable.
        """
        if self.expired(now):
            return QuoteRefusal(reason=QuoteReason.QUOTE_EXPIRED, capability=self.capability, detail="quote expired before reservation")
        if request_sha256 != self.request_sha256:
            return QuoteRefusal(reason=QuoteReason.REQUEST_CHANGED, capability=self.capability, detail="request bytes changed after quote")
        if billing_model_id != self.billing_model_id:
            return QuoteRefusal(reason=QuoteReason.REQUEST_CHANGED, capability=self.capability, detail="billing model changed after quote")
        if pricing_revision != self.pricing_revision:
            return QuoteRefusal(reason=QuoteReason.REQUEST_CHANGED, capability=self.capability, detail="pricing revision changed after quote")
        return None


@dataclass(frozen=True)
class TrustedUsage:
    """Provider-reported final usage, and whether it can be trusted as total.

    ``known=False`` is the load-bearing case: missing, truncated or ambiguous
    final usage must retain the reservation hold, because the true total is
    unknown. It must never reconcile to zero.
    """

    input_tokens: int
    output_tokens: int
    known: bool
    reason: str = ""

    @classmethod
    def unknown(cls, reason: str) -> TrustedUsage:
        return cls(input_tokens=0, output_tokens=0, known=False, reason=reason)


@runtime_checkable
class ProviderQuoteAdapter(Protocol):
    """The async boundary a provider must implement to be admitted.

    Deliberately narrow: bound the cost, re-verify the binding, and interpret
    the provider's own final usage. Anything a provider cannot bound is a
    refusal, which is why every method may raise ``QuoteRefusedError``.
    """

    provider: str
    capability: str

    def handles(self, path: str) -> bool:
        """Whether this adapter owns the given upstream route."""
        ...

    async def quote(self, request: QuoteRequest) -> ProviderQuote:
        """Bound the request's total cost, or raise ``QuoteRefusedError``."""
        ...

    async def validate(self, quote: ProviderQuote, request: QuoteRequest) -> None:
        """Re-verify the quote still binds this request. Raises on mismatch."""
        ...

    def current_pricing_revision(self) -> str:
        """The pricing identity a quote issued right now would carry.

        Body-free on purpose. At the reservation boundary the request bytes and
        billing model are fixed by construction — the buffered frames cannot
        change and are already hashed — so the only thing left that can
        invalidate a held quote is the published rates moving. Asking for just
        that identity avoids carrying a body (up to 16 MB) down to the budget
        layer to re-derive something already known.
        """
        ...

    async def reconcile(self, response: Any) -> TrustedUsage:
        """Interpret the provider's final usage. Untrusted input → ``unknown``."""
        ...


def _pricing_revision(snapshot_version: str, rate_source: str, generation_id: int | None, pointer_revision: int | None) -> str:
    """A single comparable identity for "the prices I quoted with".

    Both halves matter: the bundled snapshot fixes the context bounds and the
    floor rates, while the live V2 generation can add rows that change the
    maximum. A change in either must invalidate the quote.
    """
    return f"{snapshot_version}|{rate_source}|{generation_id or 0}.{pointer_revision or 0}"


class AnthropicTextQuoteAdapter:
    """Full-context upper bound for bounded Anthropic text requests.

    This is the existing ``estimate_policy_model_cost`` bound, ported without
    reducing its safety margin. The pessimism is deliberate and must not be
    "optimized": every possible input token is priced at the most expensive
    published input *or* cache-write rate across context/geography/service-tier
    variants, and the model's ENTIRE published context capacity is reserved
    because byte counts cannot bound hidden provider framing. Output is the
    caller's requested maximum, which includes thinking tokens.

    Anything this cannot bound locally — stateful server history, server tools,
    non-text content — is refused and named, so #5227's adapters have a contract
    to replace rather than a silent fallback to undercut.
    """

    provider = "anthropic"
    capability = Capability.TEXT

    def handles(self, path: str) -> bool:
        return path == "/v1/messages" or path.startswith("/model/")

    async def quote(self, request: QuoteRequest) -> ProviderQuote:
        # The bound needs no I/O: the snapshot is a cached immutable file and the
        # rate state is a synchronous in-process read. Async is the protocol's
        # contract for adapters that DO need a provider round trip (#5226/#5227).
        return self.bound(request)

    def bound(self, request: QuoteRequest) -> ProviderQuote:
        from pricing_policy import canonical_billing_model_id, is_anthropic_model, load_snapshot
        from pricing_policy.policy import model_rate_candidates
        from src.budget.pricing_v2_reader import cached_rate_state

        document = self._parse(request.body)
        model = self._model_id(document, request.path)
        billing_model = canonical_billing_model_id(model)
        if not is_anthropic_model(billing_model):
            raise refuse(QuoteReason.UNPUBLISHED_MODEL_PRICE, detail="no published bounded quote for this model")

        output = document.get("max_tokens")
        if isinstance(output, bool) or not isinstance(output, int) or output <= 0:
            raise refuse(QuoteReason.UNBOUNDED_OUTPUT, detail="explicit positive max_tokens required")

        self._reject_unbounded_features(document)

        snapshot = load_snapshot()
        context_limit = snapshot.models.get(billing_model, {}).get("context_max_input_tokens")
        if isinstance(context_limit, bool) or not isinstance(context_limit, int) or context_limit <= 0:
            raise refuse(QuoteReason.UNPUBLISHED_CONTEXT_BOUND, detail="published model context bound unavailable")

        state = cached_rate_state()
        rows = model_rate_candidates(snapshot.rates + state.rows, billing_model)
        if not rows:
            raise refuse(QuoteReason.UNPUBLISHED_MODEL_PRICE, detail="published model pricing unavailable")

        # A non-finite published rate cannot be compared or multiplied, and a NaN
        # must never propagate into a bound. Reject the rows outright rather than
        # letting Decimal raise mid-arithmetic.
        candidates = [
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
        if any(not rate.is_finite() for rate in candidates):
            raise refuse(QuoteReason.UNPUBLISHED_MODEL_PRICE, detail="published rate is not a finite amount")

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
            raise refuse(QuoteReason.UNPUBLISHED_MODEL_PRICE, detail="model price unavailable")

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
            # The whole context window may be input OR cache writes; the rate
            # above already takes the worst of the two, so both maxima are the
            # full window rather than a split that could understate either.
            max_input_tokens=context_limit,
            max_output_tokens=output,
            # Server tools are refused above, so no tool-side cost is admitted.
            # Custom tool definitions are ordinary input, already inside the bound.
            max_tool_tokens=0,
            max_cache_write_tokens=context_limit,
            total_usd=total.quantize(LEDGER_QUANTUM, rounding=ROUND_UP),
            evidence_source=state.source,
        )

    def current_pricing_revision(self) -> str:
        """The revision a quote issued now would carry.

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

        document = self._parse(request.body)
        snapshot = load_snapshot()
        state = cached_rate_state()
        refusal = quote.binds(
            request_sha256=request.digest,
            billing_model_id=canonical_billing_model_id(self._model_id(document, request.path)),
            pricing_revision=_pricing_revision(snapshot.snapshot_version, state.source, state.generation_id, state.pointer_revision),
            now=request.now,
        )
        if refusal is not None:
            raise QuoteRefusedError(refusal)

    async def reconcile(self, response: Any) -> TrustedUsage:
        """Trust only a complete, non-negative, integral provider usage block.

        A truncated or absent block is ``unknown`` so the hold is retained. A
        client-supplied count is never accepted here — the argument is the
        provider's own response.
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
        for name in ("cache_creation_input_tokens", "cache_read_input_tokens"):
            extra = usage.get(name)
            if extra is None:
                continue
            if isinstance(extra, bool) or not isinstance(extra, int) or extra < 0:
                return TrustedUsage.unknown(f"provider usage {name} ambiguous")
            counts["input_tokens"] += extra
        return TrustedUsage(input_tokens=counts["input_tokens"], output_tokens=counts["output_tokens"], known=True)

    # -- request shape ------------------------------------------------------

    @staticmethod
    def _parse(body: bytes) -> dict[str, Any]:
        def unique_object(pairs):
            value = {}
            for key, item in pairs:
                if key in value:
                    # A duplicated field means two readers can disagree about
                    # what was requested; that is not a quotable request.
                    raise refuse(QuoteReason.MALFORMED_REQUEST, detail="ambiguous request field")
                value[key] = item
            return value

        try:
            document = json.loads(body, object_pairs_hook=unique_object)
        except (ValueError, TypeError) as exc:
            raise refuse(QuoteReason.MALFORMED_REQUEST, detail="request body is not valid JSON") from exc
        if not isinstance(document, dict):
            raise refuse(QuoteReason.MALFORMED_REQUEST, detail="unsupported model request")
        return document

    @staticmethod
    def _model_id(document: dict[str, Any], path: str) -> str:
        if path.startswith("/model/"):
            model = unquote(path[len("/model/") :].rsplit("/", 1)[0])
        elif path == "/v1/messages":
            model = document.get("model")
        else:
            raise refuse(QuoteReason.NO_ADAPTER, capability=Capability.RESPONSES, detail="provider token-count capability required")
        if not isinstance(model, str) or not model:
            raise refuse(QuoteReason.UNPUBLISHED_MODEL_PRICE, detail="request names no model")
        return model

    @classmethod
    def _reject_unbounded_features(cls, document: dict[str, Any]) -> None:
        for key in ("mcp_servers", "container", "context_management", "previous_response_id"):
            if document.get(key):
                raise refuse(QuoteReason.STATEFUL_INPUT, capability=Capability.HISTORY, detail=f"{key} cannot be bounded locally")
        tools = document.get("tools", [])
        if not isinstance(tools, list):
            raise refuse(QuoteReason.MALFORMED_REQUEST, detail="tools must be a list")
        for tool in tools:
            if not isinstance(tool, dict):
                raise refuse(QuoteReason.MALFORMED_REQUEST, detail="tool must be an object")
            if tool.get("type") not in (None, "custom"):
                raise refuse(QuoteReason.SERVER_TOOL_COST, capability=Capability.SERVER_TOOLS, detail="server tool costs require a scoped quote")
        cls._text_only(document.get("system", ""))
        messages = document.get("messages")
        if not isinstance(messages, list) or not messages:
            raise refuse(QuoteReason.MALFORMED_REQUEST, detail="explicit input required")
        for message in messages:
            if not isinstance(message, dict) or "content" not in message:
                raise refuse(QuoteReason.MALFORMED_REQUEST, detail="message content required")
            cls._text_only(message["content"])

    @classmethod
    def _text_only(cls, content: Any) -> None:
        if isinstance(content, str):
            return
        if not isinstance(content, list):
            raise refuse(QuoteReason.MALFORMED_REQUEST, detail="unsupported content")
        for block in content:
            if not isinstance(block, dict) or block.get("type") not in {"text", "thinking", "tool_use", "tool_result"}:
                raise refuse(QuoteReason.NON_TEXT_CONTENT, capability=Capability.MEDIA, detail="non-text token-count capability required")
            if block.get("type") == "tool_result":
                cls._text_only(block.get("content", ""))


# The registry. An empty slot is a refusal, not a default — adding a provider
# means adding an adapter that can bound it, which is the whole point of the
# boundary. #5226 registers the Responses adapter here; #5227 the media/history/
# server-tool capabilities.
_ADAPTERS: tuple[ProviderQuoteAdapter, ...] = (AnthropicTextQuoteAdapter(),)


def adapter_for(path: str) -> ProviderQuoteAdapter | None:
    """The adapter owning this route, or ``None`` — never a fallback estimator."""
    for adapter in _ADAPTERS:
        if adapter.handles(path):
            return adapter
    return None


async def quote_request(body: bytes, path: str, *, now: datetime | None = None) -> ProviderQuote:
    """Bound a request's total cost, or raise ``QuoteRefusedError``.

    The single entry point for callers. No registered adapter for the route is
    ``NO_ADAPTER`` — the caller must forward nothing upstream.
    """
    adapter = adapter_for(path)
    if adapter is None:
        raise refuse(QuoteReason.NO_ADAPTER, capability=Capability.RESPONSES, detail=f"no quote adapter for {path}")
    return await adapter.quote(QuoteRequest(body=body, path=path, now=now))


async def revalidate_quote(quote: ProviderQuote, body: bytes, path: str, *, now: datetime | None = None) -> None:
    """Re-verify a held quote immediately before reserving against it."""
    adapter = adapter_for(path)
    if adapter is None:
        raise refuse(QuoteReason.NO_ADAPTER, capability=Capability.RESPONSES, detail=f"no quote adapter for {path}")
    await adapter.validate(quote, QuoteRequest(body=body, path=path, now=now))


async def confirm_quote_spendable(quote: ProviderQuote, *, now: datetime | None = None) -> QuoteRefusal | None:
    """Confirm a held quote may still be spent, at the boundary that spends it.

    ``revalidate_quote`` runs in the middleware, which then hands control inward;
    the atomic hold is taken later, after the budget check has awaited a session,
    the run binding, the scope caps, the entity hierarchy and the person layer.
    Time passes in that window and a published rate generation can roll over in
    it, so a quote confirmed on the way in is not necessarily a quote that may be
    spent on the way out. This is the check at the point of spend.

    Body-free by design: the request bytes and billing model cannot change across
    that window — the buffered frames are fixed and already hashed — so what
    remains verifiable here is expiry and the pricing revision. Re-asserting the
    quote's own digest against itself would prove nothing, and threading the body
    down to the budget layer to do it would carry up to 16 MB for no gain.

    Returns ``None`` when the quote may still be spent, or the refusal explaining
    why it may not. A refusal never carries an amount: the only correct handling
    is to reserve nothing and forward nothing upstream.
    """
    adapter = adapter_for(quote.endpoint)
    if adapter is None:
        return QuoteRefusal(reason=QuoteReason.NO_ADAPTER, capability=quote.capability, detail=f"no quote adapter for {quote.endpoint}")
    try:
        # Bounded, and in a thread: the one adapter today reads process-local
        # state, but the protocol admits a provider round trip, and a hang here
        # would stall the spend path rather than fail it.
        revision = await asyncio.wait_for(
            asyncio.to_thread(adapter.current_pricing_revision),
            timeout=QUOTE_CONFIRM_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        # asyncio.TimeoutError is an alias of the builtin on 3.11+, so this one
        # clause covers `wait_for`'s timeout.
        return QuoteRefusal(reason=QuoteReason.ADAPTER_TIMEOUT, capability=quote.capability, detail="pricing revision unconfirmed before reservation")
    except QuoteRefusedError as exc:
        return exc.refusal
    except Exception:
        # An adapter fault is an unconfirmed quote, not a confirmed one.
        return QuoteRefusal(reason=QuoteReason.UNPUBLISHED_MODEL_PRICE, capability=quote.capability, detail="pricing revision unavailable")
    return quote.binds(
        request_sha256=quote.request_sha256,
        billing_model_id=quote.billing_model_id,
        pricing_revision=revision,
        now=now,
    )


__all__ = [
    "QUOTE_CONFIRM_TIMEOUT_SECONDS",
    "QUOTE_TTL_SECONDS",
    "AnthropicTextQuoteAdapter",
    "Capability",
    "ProviderQuote",
    "ProviderQuoteAdapter",
    "QuoteReason",
    "QuoteRefusal",
    "QuoteRefusedError",
    "QuoteRequest",
    "TrustedUsage",
    "adapter_for",
    "confirm_quote_spendable",
    "quote_request",
    "request_digest",
    "revalidate_quote",
]
