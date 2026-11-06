"""Default consent requests and accepted persisted grants for user-owned OAuth."""

PROVIDER_SCOPES = {
    "github": ("github", ["read:user"]),
    "atlassian": ("jira", ["read:jira-work", "read:me", "offline_access"]),
    "slack": ("slack", ["users:read"]),
}

# Repository access for the owner's own tools (#8555): a Dev Box's `gh`/`git`, or the
# `adp github token` CLI on any machine, act as the owner through a token the gateway
# fetches from the owner's AgentCore Identity connection. Requested only when the owner
# asks for repository access at consent time; never part of the default request.
GITHUB_REPOSITORY_SCOPES = ["read:user", "repo", "workflow"]

# Optional scopes are accepted only when already recorded by an approved owned
# connection. This allowlist does not add them to the default consent request.
PROVIDER_ALLOWED_SCOPES = {
    "github": frozenset(GITHUB_REPOSITORY_SCOPES),
    "atlassian": frozenset({"read:jira-work", "read:me", "offline_access"}),
    "slack": frozenset({"users:read", "chat:write"}),
}

VENDOR_TOOL_SCOPES = {
    "github___get_user": "read:user",
    "jira___get_issue": "read:jira-work",
    "slack___get_user": "users:read",
    "slack___post_message": "chat:write",
}


# Where a provider's optional scopes are only ever requested as a reviewed set, the saved
# grant must be exactly one of those sets: `["repo"]` alone was never a consent screen the
# owner saw, so it is not a grant this platform acts on (#8555).
PROVIDER_SCOPE_SNAPSHOTS = {
    "github": (["read:user"], list(GITHUB_REPOSITORY_SCOPES)),
}


def approved_oauth_scopes(provider, persisted):
    """Read the exact saved grant; never infer scopes from provider defaults."""
    allowed = PROVIDER_ALLOWED_SCOPES.get(provider)
    if not allowed or not isinstance(persisted, dict) or set(persisted) != {"oauth_scopes"}:
        return None
    values = persisted["oauth_scopes"]
    if (
        not isinstance(values, list)
        or not values
        or any(not isinstance(value, str) or value not in allowed for value in values)
        or len(set(values)) != len(values)
    ):
        return None
    snapshots = PROVIDER_SCOPE_SNAPSHOTS.get(provider)
    if snapshots is not None and values not in snapshots:
        return None
    return list(values)
