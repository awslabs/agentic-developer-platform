"""STS attribution for genuine worker or verified native human broker calls."""

from __future__ import annotations

import hashlib
import re


def broker_session_context(*, user_id: str, agent_id: str | None, task_id: str | None, human_receipt_id: str | None = None):
    """A human receipt is an audit reference, never a Task or worker identity."""
    if human_receipt_id is not None:
        if (
            agent_id is not None
            or task_id is not None
            or not isinstance(user_id, str)
            or not user_id.strip()
            or not isinstance(human_receipt_id, str)
            or re.fullmatch(r"[A-Za-z0-9_+=,.@:/-]{1,256}", human_receipt_id) is None
        ):
            raise ValueError("invalid human broker identity")
        suffix = hashlib.sha256(human_receipt_id.encode()).hexdigest()[:16]
        return f"adp-human-{suffix}", [
            {"Key": "adp:user_id", "Value": user_id},
            {"Key": "adp:origin_kind", "Value": "human"},
            {"Key": "adp:receipt_id", "Value": human_receipt_id},
        ]
    if any(not isinstance(value, str) or not value.strip() for value in (user_id, agent_id, task_id)):
        raise ValueError("worker broker identity required")
    return f"adp-{agent_id}-{task_id[:8]}"[:64], [
        {"Key": "adp:user_id", "Value": user_id},
        {"Key": "adp:agent_id", "Value": agent_id},
        {"Key": "adp:task_id", "Value": task_id},
        {"Key": "adp:persona", "Value": agent_id},
    ]
