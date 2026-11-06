"""Pinned gateway-compatible connector inventory; execution remains fenced until admission."""

from copy import deepcopy

LEGACY_SCOPES = {
    "github": {"get_user": "read:user"},
    "jira": {"get_issue": "read:jira-work"},
    "slack": {"get_user": "users:read", "post_message": "chat:write"},
}

LEGACY_TOOLS = {
    "github": [
        {
            "name": "get_user",
            "description": "Read the verified connected GitHub user's profile.",
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    ],
    "jira": [
        {
            "name": "get_issue",
            "description": "Read an issue from a Jira Cloud site verified for your connected account.",
            "inputSchema": {
                "type": "object",
                "properties": {"cloudId": {"type": "string"}, "issueIdOrKey": {"type": "string"}},
                "required": ["cloudId", "issueIdOrKey"],
                "additionalProperties": False,
            },
        },
    ],
    "slack": [
        {
            "name": "get_user",
            "description": "Read one Slack user visible to this verified user connection.",
            "inputSchema": {
                "type": "object",
                "properties": {"user": {"type": "string"}},
                "required": ["user"],
                "additionalProperties": False,
            },
        },
        {
            "name": "post_message",
            "description": "Post exactly one approved, run-marked Slack test-channel fixture message.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "channel": {"type": "string"},
                    "text": {"type": "string"},
                    "run_marker": {"type": "string"},
                },
                "required": ["channel", "text", "run_marker"],
                "additionalProperties": False,
            },
        },
    ],
}


def legacy_tools(connector):
    if connector not in LEGACY_TOOLS:
        raise ValueError("unsupported legacy connector")
    return deepcopy(LEGACY_TOOLS[connector])
