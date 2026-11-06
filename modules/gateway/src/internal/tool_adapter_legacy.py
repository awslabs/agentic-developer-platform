"""Reopen existing legacy connections before admitting an adapter operation."""

from fastapi import HTTPException

from src.internal.github_oauth import is_github_app_grant, selected_github_connection
from src.internal.jira_oauth import protected_sites, selected_jira_connection
from src.internal.jira_provider import select_jira_site
from src.internal.slack_oauth import selected_slack_connection

LEGACY_SCOPES = {
    "github": {"get_user": "read:user"},
    "jira": {"get_issue": "read:jira-work"},
    "slack": {"get_user": "users:read", "post_message": "chat:write"},
}


def _deny():
    raise HTTPException(403, detail={"error": "connection_changed"})


async def current_legacy_adapter_binding(db, *, principal, connector, operation, claims):
    if (
        type(connector) is not str
        or type(operation) is not str
        or connector not in LEGACY_SCOPES
        or operation not in LEGACY_SCOPES[connector]
        or type(claims) is not dict
        or type(principal) is not dict
        or type(claims.get("connection")) is not dict
        or type(claims.get(connector + "_connection")) is not dict
        or claims["connection"] != claims[connector + "_connection"]
        or claims["connection"].get("connection_id") != claims.get("connection_id")
    ):
        _deny()
    owner_id, tenant_id = principal["user_id"], principal["tenant_id"]
    expected = claims["connection"]
    selected = {
        "github": selected_github_connection,
        "jira": selected_jira_connection,
        "slack": selected_slack_connection,
    }[connector]
    credential, binding, _ = await selected(db, user_id=owner_id, tenant_id=tenant_id, expected=expected)
    workload = credential.secret_arn.removeprefix("agentcore-identity:").rsplit(":", 1)[0]
    if not workload.endswith("-oauth-user") or binding.credential_id != credential.id:
        _deny()
    if connector == "github":
        app_grant = is_github_app_grant(credential.scopes)
        if app_grant:
            app_id = str(binding.observed_resources[0]["app_id"])
            scopes = []
            resources_sha256 = expected.get("resources_sha256")
            if type(resources_sha256) is not str:
                _deny()
        else:
            app_id = None
            resources_sha256 = None
            scopes = credential.scopes["oauth_scopes"]
        return credential, {
            "provider_account_id": binding.provider_account_id,
            "scopes": scopes,
            "observed_scopes": binding.observed_scopes,
            "resources_sha256": resources_sha256,
            "app_id": app_id,
            "site_id": None,
            "site_scopes": [],
            "workload": workload,
            "provider": workload.removesuffix("-oauth-user") + "-github-user",
        }
    if connector == "jira":
        site_id = claims.get("site_id")
        site = select_jira_site(protected_sites(binding), site_id)
        scopes = credential.scopes["oauth_scopes"]
        if not {"read:jira-work", "read:me"} <= set(scopes):
            _deny()
        return credential, {
            "provider_account_id": binding.provider_account_id,
            "scopes": scopes,
            "observed_scopes": binding.observed_scopes,
            "resources_sha256": expected["resources_sha256"],
            "app_id": None,
            "site_id": site.cloud_id,
            "site_scopes": list(site.scopes),
            "workload": workload,
            "provider": workload.removesuffix("-oauth-user") + "-atlassian-user",
        }
    scopes = credential.scopes["oauth_scopes"]
    if LEGACY_SCOPES[connector][operation] not in scopes or LEGACY_SCOPES[connector][operation] not in binding.observed_scopes:
        _deny()
    return credential, {
        "provider_account_id": binding.provider_account_id,
        "scopes": scopes,
        "observed_scopes": binding.observed_scopes,
        "resources_sha256": None,
        "app_id": None,
        "site_id": None,
        "site_scopes": [],
        "workload": workload,
        "provider": workload.removesuffix("-oauth-user") + "-slack-user",
    }
