"""Conversation identity on the cost record (#9024, epic #9023).

Every proxied request already carries what groups it into a *conversation*: Claude Code sends
``metadata.user_id`` ending in ``session_<uuid>``; Codex sends ``x-codex-turn-metadata`` (JSON with
``thread_id``) and ``prompt_cache_key``; the harness is the User-Agent (or, on the Dev Box host
route, the client user agent the loopback adapter forwards, since its own fetch says ``node``).
None of it was stored, so the owner could never see "these 40 requests were one Claude Code
conversation on my Dev Box". This module derives, totally and without raising, the four values the
usage writer persists: ``harness``, ``conversation_id``, ``origin`` and a short, redacted ``title``.
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import re
from dataclasses import dataclass

from src.proxy.client_tool import CLIENT_TOOL_KIRO_CREW, normalize_client_tool

ORIGIN_DEV_BOX = "dev_box"
ORIGIN_DIRECT = "direct"
#: Past this size the body is not parsed (ids and title stay NULL); harness and origin still land.
MAX_BODY_BYTES = 4 * 1024 * 1024
TITLE_CHARS = 120
CONVERSATION_ID_CHARS = 128
FORWARDED_USER_AGENT_HEADER = "x-adp-client-user-agent"
CODEX_TURN_HEADER = "x-codex-turn-metadata"
#: The Dev Box adapter forwards the Claude Code session it saw in `metadata.user_id` (it rebuilds the body for
#: Bedrock, which has no `metadata`); honoured only on the host route, like the forwarded user agent.
FORWARDED_SESSION_HEADER = "x-adp-client-session"
DERIVED_PREFIX = "d:"

_SESSION = re.compile(r"session_([0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})")
_IDENTIFIER = re.compile(r"[A-Za-z0-9._:-]{8,128}")
_SECRETS = (
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._-]{16,}"),
)


@dataclass(frozen=True)
class ConversationIdentity:
    harness: str | None = None
    conversation_id: str | None = None
    origin: str | None = None
    title: str | None = None


#: Set by the route dependency on every proxied request (unconditionally, like ``_current_client_tool``),
#: read at both usage write sites.
_current_conversation: contextvars.ContextVar[ConversationIdentity] = contextvars.ContextVar("_current_conversation", default=ConversationIdentity())


def claude_code_session_id(metadata: object) -> str | None:
    """``metadata.user_id`` → the ``session_<uuid>`` part Claude Code appends, or None."""
    if not isinstance(metadata, dict):
        return None
    user_id = metadata.get("user_id")
    if not isinstance(user_id, str):
        return None
    match = _SESSION.search(user_id[:512])
    return match.group(1).lower() if match else None


def codex_thread_id(headers: object, body: object) -> str | None:
    """Codex's thread (``x-codex-turn-metadata``), else its ``prompt_cache_key`` (one per session)."""
    raw = None
    try:
        raw = headers.get(CODEX_TURN_HEADER) if headers is not None else None  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001 - header mappings vary; this is a reporting field
        raw = None
    if isinstance(raw, str) and raw:
        try:
            turn = json.loads(raw[:4096])
        except ValueError:
            turn = None
        if isinstance(turn, dict):
            for key in ("thread_id", "session_id"):
                value = turn.get(key)
                if isinstance(value, str) and _IDENTIFIER.fullmatch(value):
                    return value
    if isinstance(body, dict):
        value = body.get("prompt_cache_key")
        if isinstance(value, str) and _IDENTIFIER.fullmatch(value):
            return value
    return None


def _text_of(content: object) -> str | None:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            part.get("text")
            for part in content
            if isinstance(part, dict) and part.get("type") in ("text", "input_text") and isinstance(part.get("text"), str)
        ]
        return "\n".join(parts) if parts else None
    return None


def first_user_text(body: object) -> str | None:
    """The first user turn of an Anthropic ``messages`` or OpenAI Responses ``input`` body."""
    if not isinstance(body, dict):
        return None
    messages = body.get("messages")
    if isinstance(messages, list):
        for message in messages:
            if isinstance(message, dict) and message.get("role") == "user":
                text = _text_of(message.get("content"))
                if text and text.strip():
                    return text
    user_input = body.get("input")
    if isinstance(user_input, str) and user_input.strip():
        return user_input
    if isinstance(user_input, list):
        for item in user_input:
            if isinstance(item, dict) and item.get("type") == "message" and item.get("role") == "user":
                text = _text_of(item.get("content"))
                if text and text.strip():
                    return text
    return None


def redact(text: str) -> str:
    for pattern in _SECRETS:
        text = pattern.sub("[redacted]", text)
    return text


_INJECTED_CONTEXT = re.compile(r"<system-reminder>.*?</system-reminder>|<environment_context>.*?</environment_context>", re.S)


def conversation_title(body: object) -> str | None:
    text = first_user_text(body)
    if not text:
        return None
    # Claude Code and Codex prepend machine context to the first user turn (dev 2026-10-27: every title read
    # "<system-reminder> Codebase and user instructions…"); the person's own words come after it.
    stripped = _INJECTED_CONTEXT.sub(" ", text)
    collapsed = re.sub(r"\s+", " ", stripped if stripped.strip() else text).strip()
    if not collapsed:
        return None
    title = redact(collapsed[: TITLE_CHARS * 4])[:TITLE_CHARS]
    return title or None


def forwarded_session_id(headers: object) -> str | None:
    try:
        value = headers.get(FORWARDED_SESSION_HEADER) if headers is not None else None  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        return None
    if isinstance(value, str):
        match = _SESSION.search(value[:512]) or re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", value.strip())
        if match:
            return (match.group(1) if match.lastindex else match.group(0)).lower()
    return None


def _system_head(body: dict) -> str:
    system = body.get("system")
    if isinstance(system, str):
        return system[:200]
    if isinstance(system, list):
        return (_text_of(system) or "")[:200]
    instructions = body.get("instructions")
    return instructions[:200] if isinstance(instructions, str) else ""


def derived_conversation_id(principal: str | None, harness: str | None, body: object, day: str | None) -> str | None:
    """A stable id for harnesses that carry none on the wire (Claude Code in Bedrock mode sends no metadata).

    One conversation re-sends its whole history on every turn, so its first user message and the head of
    its system prompt are constant for its lifetime; the same prompt by the same person on another day is
    another conversation. Marked with DERIVED_PREFIX so readers can tell it from a harness-issued id.
    """
    text = first_user_text(body)
    if not text:
        return None
    seed = "|".join((principal or "", harness or "", day or "", _system_head(body) if isinstance(body, dict) else "", text[:4000]))
    return DERIVED_PREFIX + hashlib.sha256(seed.encode("utf-8", "replace")).hexdigest()[:32]


def identify(
    *,
    user_agent: str | None,
    forwarded_user_agent: str | None,
    dev_box: bool,
    headers: object,
    body_bytes: bytes | None,
    principal: str | None = None,
    day: str | None = None,
) -> ConversationIdentity:
    """Derive the identity for one request. Total: any malformed input yields None fields."""
    try:
        agent = forwarded_user_agent if dev_box and forwarded_user_agent else user_agent
        harness = normalize_client_tool(agent)
        if harness is None and dev_box:
            harness = CLIENT_TOOL_KIRO_CREW
        origin = ORIGIN_DEV_BOX if dev_box else ORIGIN_DIRECT
        body: object = None
        if body_bytes and len(body_bytes) <= MAX_BODY_BYTES:
            try:
                body = json.loads(body_bytes)
            except ValueError:
                body = None
        conversation_id = claude_code_session_id(body.get("metadata")) if isinstance(body, dict) else None
        if conversation_id is None and dev_box:
            conversation_id = forwarded_session_id(headers)
        if conversation_id is None:
            conversation_id = codex_thread_id(headers, body)
        if conversation_id is None:
            conversation_id = derived_conversation_id(principal, harness, body, day)
        if conversation_id is not None:
            conversation_id = conversation_id[:CONVERSATION_ID_CHARS]
        return ConversationIdentity(harness=harness, conversation_id=conversation_id, origin=origin, title=conversation_title(body))
    except Exception:  # noqa: BLE001 - a reporting field must never fail a request
        return ConversationIdentity(origin=ORIGIN_DEV_BOX if dev_box else ORIGIN_DIRECT)
