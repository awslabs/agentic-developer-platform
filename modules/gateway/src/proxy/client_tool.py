"""Normalise a request's client identifier into a closed set of tool names.

Issue #4398 (EPIC #4324, FR-6.1–6.3): capture *which* tool made each proxied
request — Claude Code, Codex CLI, Cursor, the web chat — onto the cost record.

**Why this module exists at all.** The information exists only at request time,
so it is not back-fillable: every day it is uncaptured is a day of request
history that can never be attributed to a tool. A later retrofit could only
guess. Nothing in v1 reads this (FR-6.3, no UI) — the value is starting the
clock.

Three contracts this module exists to hold:

1. **A closed set, never the raw User-Agent.** Real agents send a version and
   often a platform triple (``claude-cli/2.1.3 (external, cli)``), so persisting
   the raw string would land the same tool under hundreds of spellings and any
   future breakdown would fragment into unusable one-row buckets. Every match
   collapses to one of the short, stable identifiers below.

2. **``None`` means "not captured", never "unknown tool"** (FR-6.2). An
   unrecognised client is indistinguishable from a pre-migration row, and both
   are genuinely *absent* data rather than a tool called "unknown". Returning a
   sentinel string like ``"unknown"`` would make those rows look like a real
   tool to any future consumer and silently corrupt the breakdown — which is
   exactly the failure the issue's impact table calls out. So: no sentinel.

3. **Total function — it cannot raise, on any input.** This runs on the proxy
   hot path, where every proxied request passes through it. A reporting field
   must never become an availability risk: a novel or malformed ``User-Agent``
   must not fail the request that carried it. Hence the defensive ``isinstance``
   check and the bare-``str`` coercion rather than trusting the caller — the
   header value arrives from the network, and ASGI header decoding is not
   something this module gets to assume the shape of.

**Matching is substring-on-lowercase, in a deliberate order.** Substring rather
than a strict parse because these UAs are not a specified format and vary across
versions and platforms; the alternative is a regex per vendor that breaks on the
next release. Order matters where one marker contains another or a client sends
two markers (see ``_MARKERS``).
"""

from __future__ import annotations

# The closed set. Short, lowercase, snake_case, and STABLE — these values land in
# a database column and any future breakdown groups by them, so renaming one
# silently splits its history into two buckets. Add new members; do not rename.
CLIENT_TOOL_CLAUDE_CODE = "claude_code"
CLIENT_TOOL_CODEX_CLI = "codex_cli"
CLIENT_TOOL_CURSOR = "cursor"
CLIENT_TOOL_WEB_CHAT = "web_chat"
CLIENT_TOOL_SDK = "sdk"
CLIENT_TOOL_KIMI_CODE = "kimi_code"

#: Every value this module can persist. Exposed so tests and any future consumer
#: can assert against one source of truth instead of restating the literals.
KNOWN_CLIENT_TOOLS: frozenset[str] = frozenset(
    {
        CLIENT_TOOL_CLAUDE_CODE,
        CLIENT_TOOL_CODEX_CLI,
        CLIENT_TOOL_CURSOR,
        CLIENT_TOOL_WEB_CHAT,
        CLIENT_TOOL_SDK,
        CLIENT_TOOL_KIMI_CODE,
    }
)

# Longest-defensible-match-first, because these are substring tests:
#
#   * ``claude-code`` and ``claude-cli`` both precede the bare ``claude`` marker
#     of the SDK check below — an ordered tuple, not a dict comprehension over an
#     unordered mapping, so this precedence is explicit and reviewable.
#   * ``cursor`` precedes the SDK markers: Cursor embeds vendor SDKs and can send
#     both, and the *editor* is the attribution we want, not its transport.
#   * ``anthropic-sdk`` / ``openai-python`` are last: they are the generic
#     fallback for "some program using a vendor SDK directly", so any more
#     specific client must win before we reach them.
_MARKERS: tuple[tuple[str, str], ...] = (
    # Claude Code ships as `claude-cli/<version> (external, cli)`; `claude-code`
    # covers the alternate spelling seen in its own telemetry.
    ("kimi-code", CLIENT_TOOL_KIMI_CODE),
    ("kimi-cli", CLIENT_TOOL_KIMI_CODE),
    ("claude-cli", CLIENT_TOOL_CLAUDE_CODE),
    ("claude-code", CLIENT_TOOL_CLAUDE_CODE),
    # Codex CLI's Rust build reports `codex_cli_rs/<version>`; `codex-cli`
    # covers the hyphenated spelling.
    ("codex_cli_rs", CLIENT_TOOL_CODEX_CLI),
    ("codex-cli", CLIENT_TOOL_CODEX_CLI),
    ("codex/", CLIENT_TOOL_CODEX_CLI),
    ("cursor", CLIENT_TOOL_CURSOR),
    # The gateway's own dashboard/chat SPA identifies itself explicitly rather
    # than being sniffed out of a browser UA — browser UA strings are a swamp and
    # "is this a browser" is not the question we are asking.
    ("adp-web", CLIENT_TOOL_WEB_CHAT),
    ("adp-chat", CLIENT_TOOL_WEB_CHAT),
    ("anthropic-sdk", CLIENT_TOOL_SDK),
    ("openai-python", CLIENT_TOOL_SDK),
    ("boto3", CLIENT_TOOL_SDK),
)

# A UA longer than this is either an attack or a proxy chain that has accreted
# every hop's identifier. We scan a bounded prefix so a pathological 10 MB header
# cannot turn into CPU burn on the hot path, once per request.
_MAX_SCAN_CHARS = 512


def normalize_client_tool(user_agent: str | None) -> str | None:
    """Map a ``User-Agent`` to a member of :data:`KNOWN_CLIENT_TOOLS`, or ``None``.

    Args:
        user_agent: The raw ``User-Agent`` header value, or ``None`` when absent.

    Returns:
        A stable short identifier from :data:`KNOWN_CLIENT_TOOLS`, or ``None``
        meaning **"not captured"** — never "unknown tool" (FR-6.2). Callers must
        not substitute a placeholder string for ``None``.

    Never raises. Any absent, malformed, non-string or unrecognised input
    resolves to ``None``, because this is called on every proxied request and a
    reporting field must not be able to fail one.
    """
    # Defensive rather than paranoid: the value comes off the wire via the ASGI
    # header mapping, and a non-str here (bytes, or a mock in a caller's test)
    # must degrade to "not captured" rather than blow up a live request in
    # `.lower()`.
    if not isinstance(user_agent, str):
        return None

    candidate = user_agent[:_MAX_SCAN_CHARS].lower()
    if not candidate.strip():
        return None

    for marker, tool in _MARKERS:
        if marker in candidate:
            return tool

    # Recognised nothing. `None` = not captured; deliberately NOT a sentinel.
    return None
