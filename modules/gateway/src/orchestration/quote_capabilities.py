"""What makes a media or server-history request boundable, and what does not (#5227).

#5225 built the quote contract and #5226 the bounded Responses text adapter. Both
deliberately refused three things by name, leaving them for this module to admit
or to refuse with evidence: non-text input, provider-retained conversation
history, and provider-side tool execution.

The whole matrix turns on one property of the existing bound. Neither adapter
counts the request's tokens — no deployed endpoint here publishes a token-counting
API, and a client-supplied count is not evidence about cost. Instead each reserves
the model's **entire published context window** at the dearest published
input-side rate. That is not a guess about this request; it is the ceiling the
provider itself enforces, because a request whose assembled input exceeds the
model's context window is rejected by the provider rather than served expensively.

Three consequences follow, and they decide every row. Only the first admits new
work: media is admitted, while server history and server-side tools are refused
for reasons that are not "unimplemented" but "no enforceable bound exists here
yet", each with a named prerequisite.

**Media is admittable when its bytes are in the request.** An image, document or
audio part is consumed by the provider as ordinary tokens against that same
context window, priced by the same published per-token rates — our pricing
evidence carries no separate per-image or per-minute media fee. So media tokens
are already inside the amount being reserved, and the bound is provably
independent of the media's size and content.

That last point cuts both ways, and it is worth being exact about, because the
intuitive reason for refusing a *referenced* image is wrong. Since the bound
counts nothing, a referenced file's tokens are just as enclosed by the
full-window reservation as inline bytes are; on money alone, references would be
admittable. What actually disqualifies them is **authorization** — a reference is
dereferenced by the provider under the gateway's single shared principal, so one
tenant could name another tenant's stored file — and the fact that inspecting one
would require **a URL fetcher this scope forbids**. Inline bytes raise neither
problem: they are under the quote's request digest and need no resolution by
anyone. ``MUTABLE_MEDIA_REFERENCE`` carries that refusal; the full argument is on
``EXTERNAL_RESPONSES_FIELDS`` below.

**Server-retained history stays refused, on authorization grounds rather than
cost.** The cost side actually works: a provider that reassembles a stored prior
turn does so into the same context window it enforces, so the unreadable history
would be enclosed by the full-window reservation, and history mutating after the
quote could not break a bound that never depended on its contents. That is the
issue's "bounded by an evidenced provider limit" branch, and it holds.

What does not hold is the other half of the same requirement — that history be
resolved "within the same authorization boundary". On the deployed path it is
not. Upstream Responses calls are SigV4-signed with the gateway pod's own IRSA
credentials (``src/proxy/mantle_auth.py``; the platform branch in
``MantlePassthroughService._routed_request``), which is one shared principal for
every tenant. Nothing scopes provider-side stored-response state per tenant: the
only body rewrite is a geographic prefix on ``model``, tenant identity reaches AWS
at most as a CloudTrail session tag that is explicitly *not* an authorization
input, and the optional per-destination routing override may legitimately map two
organizations to the same account.

So admitting ``previous_response_id`` would let one tenant name a response id
minted by another, with the provider seeing a single identical principal for
both. Whether that read succeeds depends on how the provider scopes response-id
lookup — an external fact this repository does not establish, test or document,
and which a quote adapter is the wrong place to assume. Bounding the *spend*
correctly would not make the *access* safe, and a cost ceiling must not be
mistaken for an authorization boundary. The row therefore stays refused with
``STATEFUL_INPUT``/``HISTORY``, exactly as #5225 and #5226 left it, and it is a
published limitation of #5175 with a named prerequisite: gateway-side ownership
of response ids, or provider evidence of per-principal scoping.

**Server-side tools cannot be admitted for any provider today.** A provider-run
web search, code execution or MCP call bills a per-call fee, may run several times
per request, and may recurse. Every money field in our published evidence is a
price *per 1,000 tokens* — there is no per-call, per-search, per-hour or
per-session rate anywhere in the snapshot or the curated table. With no published
rate there is no arithmetic that yields a ceiling, and an unbounded chargeable
side must be refused rather than approximated. Neither a caller-supplied maximum
(a client never gets to price its own request) nor a gateway-mediated step loop
fixes this: bounding the number of steps still leaves each step's fee unpriced.
That row therefore stays refused with ``SERVER_TOOL_COST`` and remains a published
limitation of #5175, not a capability quietly downgraded to look complete.

The machine-readable matrix, with the published source behind each row, lives in
``tests/orchestration/fixtures/quote-capability-matrix-5227.json`` and is asserted
against this module's real behaviour so the two cannot drift.
"""

from __future__ import annotations

#: Anthropic content-block types that are media rather than text. Only these two
#: are admitted because only these two are what the deployed route accepts as
#: non-text input; an ``audio``/``video`` block is refused as unsupported content
#: rather than priced, since the provider does not serve it here at all.
MEDIA_ANTHROPIC_BLOCKS = frozenset({"image", "document"})

#: Anthropic ``source.type`` values that carry the media's bytes in the request
#: body. ``base64`` is the encoded payload itself; ``text`` and ``content`` are
#: literal inline document bodies. All three are covered by the quote's request
#: digest, so they cannot change after the quote is issued.
INLINE_ANTHROPIC_SOURCES = frozenset({"base64", "text", "content"})

#: Anthropic ``source.type`` values naming content held elsewhere: ``url`` is
#: fetched by the provider at submission time, ``file`` names provider-stored
#: content. Neither ever transits this proxy. See ``EXTERNAL_RESPONSES_FIELDS``
#: for why that is refused — the reason is *not* that the money bound would be
#: exceeded.
EXTERNAL_ANTHROPIC_SOURCES = frozenset({"url", "file"})

#: Responses part fields naming content held elsewhere: a ``file_id`` is
#: provider-stored, a ``file_url`` is fetched at submission time.
#:
#: Why these are refused, stated precisely, because the obvious reason is wrong.
#: It is tempting to say "we cannot count the bytes, so we cannot bound the
#: cost". But this quote layer counts nothing: it reserves the model's entire
#: published context window regardless of input, so a referenced file's tokens
#: either fit inside the amount already reserved or the provider rejects the
#: request for exceeding the window. On *money*, a reference is therefore just as
#: bounded as inline bytes.
#:
#: The refusal rests on the other two requirements instead:
#:
#: 1. **Authorization.** A reference is resolved by the provider under the
#:    gateway pod's own IRSA credentials — one shared principal for every tenant
#:    (see the history discussion in this module's docstring). A ``file_id`` is
#:    a name in that shared namespace, so honouring one would let a tenant read
#:    content uploaded by another. This is the same defect that blocks server
#:    history, and a cost bound does not fix it.
#: 2. **No fetcher.** Resolving a ``file_url`` to inspect what was named would
#:    mean adding a general URL fetcher to the quote path, turning a pricing
#:    component into an SSRF surface aimed at caller-controlled URLs. #5227
#:    excludes that, so the reference is refused undereferenced — asserted by a
#:    test that fails if the quote path opens a socket at all.
#:
#: Inline bytes raise neither problem: they are in the body, under the request
#: digest, and need no resolution by anyone.
EXTERNAL_RESPONSES_FIELDS = ("file_id", "file_url")

#: The scheme that makes a Responses ``image_url`` inline rather than external.
#: A ``data:`` URI is the base64 payload spelled as a URI — its bytes are in the
#: body and under the digest. Any other scheme is a fetch we cannot bound, and we
#: deliberately add no fetcher of our own to resolve one.
INLINE_DATA_URI_PREFIX = "data:"

#: Where each media part carries its bytes, and how that payload is spelled.
#:
#: The payload requirement is keyed to the part's declared ``type`` on purpose. A
#: shared "did any payload field look inline?" tally is not a proof about THIS
#: part: an ``input_image`` whose bytes the provider reads from ``image_url`` is
#: not made inline by an unrelated audio object sitting beside it. Keying the
#: requirement to the type means each part must carry its own readable bytes.
#:
#: ``input_image`` spells its bytes in ``image_url`` ("a fully qualified URL or
#: base64 encoded image in a data URL") and ``input_file`` in ``file_data``, both
#: as ``data:`` URI strings. ``input_audio`` is the one part whose payload is a
#: nested OBJECT — ``{"data": "<base64>", "format": "wav"|"mp3"}``. The API
#: defines no ``audio_url``, so an adapter that scanned only string fields refused
#: the sole spec-compliant way to send audio while the matrix advertised it as
#: admitted. Nested audio carries raw base64 with no URL field, so readable base64
#: there is structurally inline: in the body, under the quote's request digest.
MEDIA_PART_PAYLOADS = {
    "input_image": ("image_url", "string"),
    "input_file": ("file_data", "string"),
    "input_audio": ("input_audio", "nested"),
}

#: Responses content-part types that are media rather than text. Derived from the
#: map above so the set the adapter dispatches on and the map that says where each
#: part's bytes live cannot drift apart — a part type in the set but missing from
#: the map would reach the payload lookup with no entry.
MEDIA_RESPONSES_PARTS = frozenset(MEDIA_PART_PAYLOADS)

#: The key carrying the base64 payload inside a nested media object.
NESTED_PAYLOAD_DATA_KEY = "data"

#: Keys a media part may carry besides its own payload field. Anything else is
#: refused rather than ignored: an unrecognised key is either a payload field
#: belonging to a different part type (so this part has no verified bytes of its
#: own) or a reference the provider would dereference under the gateway's shared
#: principal. Both reach the provider, because the body is forwarded byte-for-byte,
#: so an ALLOWLIST is the only safe shape here — enumerating known-bad names lets
#: the next undocumented ``*_url``/``*_id`` field through untouched.
ALLOWED_MEDIA_PART_KEYS = frozenset({"type", "detail", "filename", "prompt_cache_breakpoint"})

#: Keys permitted inside a nested media payload object, for the same reason.
ALLOWED_NESTED_PAYLOAD_KEYS = frozenset({"data", "format"})

#: Values an allowlisted enum-valued media key may take. A key being *permitted*
#: is not the same as its value being harmless: ``format`` and ``detail`` are
#: forwarded byte-for-byte like everything else, so an unvalidated
#: ``format: "https://attacker/probe.wav"`` is a caller-controlled string reaching
#: the provider inside an admitted request. The API defines a closed set for each,
#: so anything outside it is refused rather than passed through.
ALLOWED_AUDIO_FORMATS = frozenset({"wav", "mp3"})
ALLOWED_IMAGE_DETAILS = frozenset({"auto", "low", "high"})

#: Keys an Anthropic media ``source`` may carry, and keys a content block may
#: carry, for exactly the reason ``ALLOWED_MEDIA_PART_KEYS`` gives on the Responses
#: side. Validating ``source.type``/``source.data`` while leaving the object's
#: other keys unenumerated let a reference ride along beside verified inline bytes
#: — ``{"type": "base64", "data": "AAAA", "url": ..., "file_id": ...}`` and the
#: Bedrock-native ``s3Location`` were both admitted, and the body is forwarded
#: byte-for-byte, so each still reached the provider.
ALLOWED_ANTHROPIC_SOURCE_KEYS = frozenset({"type", "media_type", "data", "content", "cache_control"})
ALLOWED_ANTHROPIC_MEDIA_BLOCK_KEYS = frozenset({"type", "source", "title", "context", "citations", "cache_control"})


def is_inline_base64(value: str) -> bool:
    """Whether ``value`` really is a base64 payload we hold the bytes of.

    A character-class test is not enough for this job. The base64 alphabet contains
    ``+``, ``/`` and ``=``, so path- and key-shaped references such as
    ``file/VICTIMTENANTB/secret==`` pass it while naming content we do not have;
    only the ``:`` in ``s3://`` was ever being caught. Decoding is the actual
    question — "can these characters be read as the bytes they claim to be" — so it
    is asked directly, with ``validate=True`` to reject stray characters rather
    than silently discard them.
    """
    import base64
    import binascii

    stripped = "".join(value.split())
    if not stripped or len(stripped) % 4:
        return False
    try:
        base64.b64decode(stripped, validate=True)
    except (binascii.Error, ValueError):
        return False
    return True


def is_inline_data_uri(value: str) -> bool:
    """Whether ``value`` is a ``data:`` URI carrying its own base64 payload.

    ``startswith("data:")`` checks the scheme and nothing else, so
    ``data://attacker.example/probe.png`` and ``data:,https://attacker/x`` both
    passed it while still naming somewhere else. The inline form this layer relies
    on is ``data:<mediatype>;base64,<payload>``: require that shape and require the
    payload to decode, so what is admitted is bytes under the request digest rather
    than a URI that merely opens with the right five characters.
    """
    if not value.startswith(INLINE_DATA_URI_PREFIX):
        return False
    header, _, payload = value[len(INLINE_DATA_URI_PREFIX) :].partition(",")
    if not header.endswith(";base64") or "/" not in header:
        return False
    return is_inline_base64(payload)


def resolve_capability(base: str, exercised: frozenset[str] | set[str]) -> str:
    """The capability to record on a quote that admitted media.

    ``base`` is the adapter's own capability (``TEXT`` for Anthropic messages,
    ``RESPONSES`` for the Responses route); ``exercised`` is what the request
    actually used. Recording ``MEDIA`` when media was admitted is what makes a
    quote's own evidence say which capability it relied on, rather than every
    quote claiming to be plain text.

    A text-only request keeps its base capability, so every quote #5225 and #5226
    already issued is labelled exactly as before. Only ``MEDIA`` is considered
    here because it is the only capability this module admits — history and
    server tools are refused, so no quote can be issued carrying them.

    ``Capability`` is imported inside the function because both quote adapters
    import this module at module scope while it needs a name defined in
    ``provider_quotes``; deferring the import keeps that graph acyclic.
    """
    from src.orchestration.provider_quotes import Capability

    return Capability.MEDIA if Capability.MEDIA in exercised else base


__all__ = [
    "ALLOWED_ANTHROPIC_MEDIA_BLOCK_KEYS",
    "ALLOWED_ANTHROPIC_SOURCE_KEYS",
    "ALLOWED_AUDIO_FORMATS",
    "ALLOWED_IMAGE_DETAILS",
    "ALLOWED_MEDIA_PART_KEYS",
    "ALLOWED_NESTED_PAYLOAD_KEYS",
    "EXTERNAL_ANTHROPIC_SOURCES",
    "EXTERNAL_RESPONSES_FIELDS",
    "INLINE_ANTHROPIC_SOURCES",
    "INLINE_DATA_URI_PREFIX",
    "MEDIA_ANTHROPIC_BLOCKS",
    "MEDIA_PART_PAYLOADS",
    "MEDIA_RESPONSES_PARTS",
    "NESTED_PAYLOAD_DATA_KEY",
    "is_inline_base64",
    "is_inline_data_uri",
    "resolve_capability",
]
