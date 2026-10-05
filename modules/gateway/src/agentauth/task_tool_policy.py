"""Generic Task tool authority; configuration names permissions, never endpoints."""

from __future__ import annotations

import json
import os
import re

TOOL_PATTERN = r"^[a-z][a-z0-9_]{0,47}\.[a-z][a-z0-9_]{0,63}$"


class TaskToolPolicyError(Exception):
    pass


def valid_tools(value):
    return (
        isinstance(value, list)
        and len(value) <= 64
        and all(isinstance(v, str) and re.fullmatch(TOOL_PATTERN, v) for v in value)
        and len(value) == len(set(value))
    )


def persona_tools(persona, env=None):
    env = os.environ if env is None else env
    raw = env.get("ADP_TASK_PERSONA_TOOLS", "{}")
    try:
        if len(raw) > 16384:
            raise ValueError()
        config = json.loads(raw)
        if not isinstance(config, dict) or len(config) > 64 or any(not isinstance(k, str) or not valid_tools(v) for k, v in config.items()):
            raise ValueError()
        return frozenset(config.get(persona, []))
    except (TypeError, ValueError):
        raise TaskToolPolicyError("task_tool_configuration_unavailable") from None


def freeze_tools(persona, policy, env=None):
    allowed = policy.get("allowed_tools", [])
    if not valid_tools(allowed):
        raise TaskToolPolicyError("task_tool_policy_unavailable")
    return tuple(sorted(set(allowed) & persona_tools(persona, env)))


def codex_tool_name(tool):
    """Stable v1 function name; permissions remain the original gateway names.

    A digest avoids delimiter collisions and the SDK's 64-character name limit.
    Tool descriptions and schemas come from reviewed implementations, not here.
    """
    import hashlib

    if not isinstance(tool, str) or not re.fullmatch(TOOL_PATTERN, tool):
        raise TaskToolPolicyError("invalid_task_tool_name")
    return "adp_" + hashlib.sha256(tool.encode()).hexdigest()[:56]
