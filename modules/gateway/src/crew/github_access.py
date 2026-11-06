"""GitHub as the owner, from the owner's connection — for a Dev Box or any other consumer (#8555).

An ADP owner connects GitHub once in Settings → Connections (AgentCore Identity holds the
provider token; ADP keeps only the pointer and the observed account). When that consent
included repository access, two consumers may ask the gateway for the owner's token:

* the owner's Dev Box, through ``POST /crew/github-token`` under the host's signed run
  identity (:mod:`src.crew.host_github`), so ``gh`` and ``git`` on the box act as the owner;
* the owner themself, through ``POST /auth/connections/oauth/github/token`` with their own
  session, so ``adp github token`` works on a laptop that never had a box.

Both paths run through :func:`owner_github_token`: the same qualification as the GitHub
tool (one owned, consented, verified connection), plus the repository grant, plus a fresh
observation of the account the token belongs to — the token is never returned for an
account other than the one the owner bound at consent. Nothing here stores the token;
the caller holds it in memory for at most :data:`TOKEN_HORIZON_SECONDS` and asks again.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.models.organization import User
from src.shared.models.vault import UserCredential

SWITCH = "ADP_GITHUB_USER_ACCESS_ENABLED"
# How long a consumer may keep a minted token before asking again. The gateway re-checks
# consent, the connection and the account on every mint, so a revoked consent stops
# new work within this horizon; the provider token itself is AgentCore's to refresh.
TOKEN_HORIZON_SECONDS = 600
NO_STORE = {"Cache-Control": "no-store"}


def enabled() -> None:
    """Mount-always, 503-when-disabled (the repo's switch pattern): off is distinguishable from a routing mistake."""
    if os.environ.get(SWITCH) != "true":
        raise HTTPException(503, detail={"error": "github_user_access_disabled"}, headers=NO_STORE)


@dataclass(frozen=True)
class OwnerGithubToken:
    token: str
    login: str
    account_id: str
    credential_id: str
    expires_at: int


async def owner_github_token(db: AsyncSession, *, org_id: str, user_id: str, identity, now: int) -> OwnerGithubToken:
    """The owner's GitHub token with repository access, or a refusal that names the next step.

    Refusals, in the order a consumer should fix them:

    * 404 ``github_not_connected`` — no GitHub connection; connect in Settings → Connections.
    * 403 ``github_consent_required`` — connected, but "Allow agents to use this account" is off.
    * 403 ``github_repository_consent_required`` — connected without repository access; grant it.
    * 409 ``github_reconnect_required`` — the connection no longer verifies (revoked at GitHub,
      account changed, identity unavailable); reconnect.
    """
    from src.internal.credential_routes import _identity_access_token
    from src.internal.github_oauth import (
        github_app_client,
        grants_repository_access,
        is_github_app_grant,
        observe_github_app_user,
        observe_github_user,
        selected_github_connection,
    )

    owner = await db.scalar(select(User).where(User.id == user_id, User.org_id == org_id, User.user_kind == "human", User.is_shadow.is_(False)))
    pointer = await db.scalar(
        select(UserCredential).where(
            UserCredential.org_id == org_id,
            UserCredential.user_id == user_id,
            UserCredential.service == "github",
            UserCredential.provider == "agentcore-identity",
        )
    )
    if owner is None or pointer is None:
        raise HTTPException(404, detail={"error": "github_not_connected"}, headers=NO_STORE)
    if pointer.agent_consent is not True:
        raise HTTPException(403, detail={"error": "github_consent_required"}, headers=NO_STORE)
    try:
        credential, binding, _ = await selected_github_connection(db, user_id=user_id, tenant_id=org_id)
    except HTTPException as exc:
        if exc.status_code != 403:
            raise
        raise HTTPException(409, detail={"error": "github_reconnect_required"}, headers=NO_STORE) from None
    if not is_github_app_grant(credential.scopes) and grants_repository_access(credential, binding):
        raise HTTPException(409, detail={"error": "github_reconnect_required"}, headers=NO_STORE)
    if not grants_repository_access(credential, binding):
        if (
            not is_github_app_grant(credential.scopes)
            and credential.scopes.get("oauth_scopes") == ["read:user", "repo", "workflow"]
            and {"repo", "workflow"} <= set(binding.observed_scopes)
        ):
            raise HTTPException(409, detail={"error": "github_reconnect_required"}, headers=NO_STORE)
        raise HTTPException(403, detail={"error": "github_repository_consent_required"}, headers=NO_STORE)
    try:
        token = await _identity_access_token(credential, owner, identity)
        if is_github_app_grant(credential.scopes):
            client = await github_app_client()
            if client is None:
                raise HTTPException(403, detail={"error": "github_reconnect_required"})
            observation = await observe_github_app_user(token, **client)
        else:
            observation = await observe_github_user(token)
    except HTTPException as exc:
        if is_github_app_grant(credential.scopes) and exc.status_code == 403 and exc.detail == {"error": "github_reconnect_required"}:
            raise HTTPException(403, detail=exc.detail, headers=NO_STORE) from None
        if exc.status_code >= 500 and exc.status_code != 502:
            raise
        detail = {"error": "github_reconnect_required"}
        if is_github_app_grant(credential.scopes):
            detail["message"] = "GitHub could not refresh or verify your connection. Reconnect GitHub from the Dev Box page."
        raise HTTPException(409, detail=detail, headers=NO_STORE) from None
    # The token must belong to the account the owner bound at consent, with the same grant.
    if (
        observation.account_id != binding.provider_account_id
        or list(observation.scopes) != list(binding.observed_scopes)
        or (is_github_app_grant(credential.scopes) and list(observation.resources) != binding.observed_resources)
    ):
        raise HTTPException(403 if is_github_app_grant(credential.scopes) else 409, detail={"error": "github_reconnect_required"}, headers=NO_STORE)
    await db.execute(update(UserCredential).where(UserCredential.id == credential.id).values(last_used_at=datetime.fromtimestamp(now, UTC)))
    return OwnerGithubToken(
        token=token,
        login=observation.profile["login"],
        account_id=observation.account_id,
        credential_id=credential.id,
        expires_at=now + TOKEN_HORIZON_SECONDS,
    )
