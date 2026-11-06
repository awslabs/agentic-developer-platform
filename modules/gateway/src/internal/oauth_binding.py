"""Provider verification for consent callbacks, independent of tool activation."""

from fastapi import HTTPException
from sqlalchemy import select

from src.shared.models.vault import UserCredential


async def connection_status(db, *, caller, provider, service):
    credential = await db.scalar(
        select(UserCredential).where(
            UserCredential.org_id == caller.org_id,
            UserCredential.user_id == caller.user_id,
            UserCredential.provider == "agentcore-identity",
            UserCredential.service == service,
        )
    )
    if credential is None:
        return "not_connected"
    if credential.agent_consent is not True:
        return "consent_paused"
    from src.internal.github_oauth import selected_github_connection
    from src.internal.jira_oauth import selected_jira_connection
    from src.internal.slack_oauth import selected_slack_connection

    select_connection = {"github": selected_github_connection, "atlassian": selected_jira_connection, "slack": selected_slack_connection}[provider]
    try:
        await select_connection(db, user_id=caller.user_id, tenant_id=caller.org_id)
    except HTTPException as exc:
        if exc.status_code != 403:
            raise
        return "reconnect_required"
    return "verified"


async def observe_connection(credential, provider, token):
    if provider == "github":
        from src.internal.github_oauth import github_app_client, is_github_app_grant, new_github_binding, observe_github_app_user, observe_github_user

        if is_github_app_grant(credential.scopes):
            client = await github_app_client()
            if client is None:
                raise HTTPException(403, detail={"error": "github_reconnect_required"})
            observation = await observe_github_app_user(token, **client)
            return new_github_binding(credential, observation)
        observation = await observe_github_user(token)
        # #8555: repository access is only real when GitHub granted every requested
        # scope; an owner who unticks `repo` on GitHub's screen gets a profile-only
        # connection refused here rather than a connection that silently cannot push.
        if not set(credential.scopes["oauth_scopes"]) <= set(observation.scopes):
            raise HTTPException(403, detail={"error": "requested_scope_not_granted"})
        return new_github_binding(credential, observation)
    if provider == "slack":
        from src.internal.slack_oauth import new_slack_binding
        from src.internal.slack_reader import observe_slack_identity

        observation = await observe_slack_identity(token)
        if not set(credential.scopes["oauth_scopes"]) <= set(observation.scopes):
            raise HTTPException(
                403,
                detail={"error": "write_scope_not_granted" if "chat:write" in credential.scopes["oauth_scopes"] else "requested_scope_not_granted"},
            )
        return new_slack_binding(credential, observation)
    if provider == "atlassian":
        from src.internal.jira_oauth import new_jira_binding
        from src.internal.jira_provider import observe_jira_sites, observe_jira_user

        # Both independent observations use the exact token returned for this
        # owner's consent; resource membership never supplies account identity.
        user = await observe_jira_user(token)
        sites = await observe_jira_sites(token)
        return new_jira_binding(credential, user, sites)
    raise HTTPException(403, detail={"error": "unknown_provider"})
