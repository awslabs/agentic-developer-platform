"""Remember backend — adapter for session memory persistence (remember verb).

Wraps the existing personal_context/experience_tool.py to provide the
'remember' verb (save session context, decisions, learnings) and routes
search(scope="memory") to experience recall.

Provides importable functions for the Context MCP server.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)


async def remember(
    session_id: str,
    messages: list[dict[str, str]],
    outcome: str = "",
    *,
    experience_tool: Any,
    headers: dict[str, str],
) -> dict[str, Any]:
    """Save session context to long-term memory via the experience tool.

    Converts the session messages into a learning entry stored in the
    experience tool's personal context store.

    Parameters
    ----------
    session_id:
        Identifier for the session being saved.
    messages:
        List of message dicts with "role" and "content" keys.
    outcome:
        Optional summary of the session outcome.
    experience_tool:
        ExperienceTool instance.
    headers:
        Request headers (contains identity headers for the experience tool).

    Returns
    -------
    Dict with status and session_id.
    """
    # Synthesize content from messages + outcome
    content_parts: list[str] = []
    if outcome:
        content_parts.append(f"Outcome: {outcome}")

    for msg in messages[-5:]:  # Keep last 5 messages for context
        role = msg.get("role", "")
        text = msg.get("content", "")
        if text:
            content_parts.append(f"[{role}] {text[:200]}")

    content = "\n".join(content_parts) if content_parts else f"Session: {session_id}"

    # Save via experience tool as a learning entry
    try:
        result = experience_tool.handle(
            {
                "action": "save",
                "content": content,
                "persona": "developer",
                "learning_type": "session_memory",
                "context": {"session_id": session_id},
                "visibility": "private",
            },
            headers,
        )
        return {"stored": True, "session_id": session_id, "entry_id": result.get("id", "")}
    except Exception as e:
        log.warning("Failed to store session memory: %s", e)
        return {"stored": False, "session_id": session_id, "error": str(e)}


async def recall_memory(
    query: str,
    *,
    experience_tool: Any,
    headers: dict[str, str],
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Recall from long-term memory via the experience tool.

    Parameters
    ----------
    query:
        Search query for memory recall.
    experience_tool:
        ExperienceTool instance.
    headers:
        Request headers (contains identity headers).
    limit:
        Maximum number of results.

    Returns
    -------
    List of memory results.
    """
    try:
        result = experience_tool.handle(
            {
                "action": "recall",
                "query": query,
                "persona": "developer",
                "limit": limit,
                "cross_persona": True,
            },
            headers,
        )
        return result.get("results", [])
    except Exception as e:
        log.warning("Failed to recall memory: %s", e)
        return []
