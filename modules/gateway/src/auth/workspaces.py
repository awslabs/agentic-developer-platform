"""Membership-based workspace selection and strict Cognito claim synchronization."""

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.cognito_claims import cognito_user_pool_id
from src.admin.config import membership_role_to_admin_role
from src.auth.dependencies import get_current_user
from src.shared.database import get_db
from src.shared.identity.workspaces import link_login_to_workspace, linked_user_ids, login_user, memberships_for_login, primary_team_for_workspace
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import Organization, User
from src.shared.schemas.auth import TokenContext

logger = logging.getLogger(__name__)
router = APIRouter(tags=["workspaces"])


class Workspace(BaseModel):
    org_id: str
    name: str
    user_id: str
    role: str
    team_id: str = ""
    department_id: str = ""
    is_current: bool = False


class WorkspaceList(BaseModel):
    items: list[Workspace]


class SwitchWorkspaceRequest(BaseModel):
    org_id: str = Field(min_length=1, max_length=255)


class CognitoWorkspaceClaims:
    """Strict write: a switch must never claim success after a failed sync."""

    def _login(self, subject: str):
        import boto3

        from src.shared.config import get_settings

        pool_id = cognito_user_pool_id() or get_settings().cognito_user_pool_id
        if not pool_id:
            raise RuntimeError("Cognito is not configured")
        client = boto3.client("cognito-idp", region_name=get_settings().aws_region)
        users = client.list_users(UserPoolId=pool_id, Filter=f'sub = "{subject}"', Limit=1).get("Users", [])
        if not users:
            raise RuntimeError("Login account was not found")
        username = users[0]["Username"]
        response = client.admin_get_user(UserPoolId=pool_id, Username=username)
        attributes = {a["Name"]: a["Value"] for a in response.get("UserAttributes", [])}
        if attributes.get("sub") != subject:
            raise RuntimeError("Login account does not match the authenticated subject")
        return client, pool_id, username, attributes

    def _set(self, subject: str, values: dict[str, str]) -> tuple[dict[str, str], dict[str, str]]:
        client, pool_id, username, attributes = self._login(subject)
        previous = {key: attributes.get(key, "") for key in values}
        applied = dict(values)
        platform_roles = {"platform_admin", "admin"}
        if attributes.get("custom:role") in platform_roles:
            # Preserve CURRENT global authority, never a stale token's is_admin.
            applied["custom:role"] = attributes["custom:role"]
        elif applied.get("custom:role") in platform_roles:
            # Compensation after a concurrent global demotion cannot re-grant it.
            raise RuntimeError("Platform role changed during workspace selection")
        client.admin_update_user_attributes(
            UserPoolId=pool_id,
            Username=username,
            UserAttributes=[{"Name": key, "Value": value} for key, value in applied.items()],
        )
        return previous, applied

    async def set(self, subject: str, values: dict[str, str]) -> tuple[dict[str, str], dict[str, str]]:
        return await asyncio.to_thread(self._set, subject, values)

    def _set_team(self, subject: str, org_id: str, team_id: str, department_id: str) -> None:
        """Reconcile team scope only, under the canonical login's DB row lock.

        ListUsers resolves the immutable subject to a username; AdminGetUser supplies
        the current attributes (ListUsers itself is eventually consistent). Membership
        in another org must never switch the selected login or replace its other claims.
        """
        client, pool_id, username, attributes = self._login(subject)
        if attributes.get("custom:org_id") != org_id:
            return
        values = {"custom:team_id": team_id, "custom:department_id": department_id}
        if all(attributes.get(key, "") == value for key, value in values.items()):
            return
        client.admin_update_user_attributes(
            UserPoolId=pool_id,
            Username=username,
            UserAttributes=[{"Name": key, "Value": value} for key, value in values.items()],
        )

    async def set_team(self, subject: str, org_id: str, team_id: str, department_id: str) -> None:
        await asyncio.to_thread(self._set_team, subject, org_id, team_id, department_id)


def get_workspace_claims() -> CognitoWorkspaceClaims:
    return CognitoWorkspaceClaims()


def _require_human(context: TokenContext) -> None:
    if context.account_type != "human" or context.auth_source != "jwt":
        raise HTTPException(403, "Workspace selection is available to signed-in people only")


async def _memberships(db: AsyncSession, context: TokenContext):
    _require_human(context)
    try:
        return await memberships_for_login(db, context.user_id, username=context.cognito_username)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


async def _workspace(db: AsyncSession, context: TokenContext, org: Organization, user: User, membership: TenantMembership | None) -> Workspace:
    try:
        team = await primary_team_for_workspace(db, user, org.id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return Workspace(
        org_id=org.id,
        name=org.name,
        user_id=user.id,
        role=membership_role_to_admin_role(membership.role if membership else "member").value,
        team_id=team.id if team else "",
        department_id=team.department_id if team else "",
        is_current=org.id == context.org_id,
    )


@router.get("/workspaces", response_model=WorkspaceList)
async def list_workspaces(current_user: TokenContext = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> WorkspaceList:
    _, memberships = await _memberships(db, current_user)
    orgs = (await db.scalars(select(Organization).where(Organization.id.in_(memberships)).order_by(Organization.name, Organization.id))).all()
    return WorkspaceList(items=[await _workspace(db, current_user, org, *memberships[org.id]) for org in orgs])


async def _reconcile_after_failed_switch(
    db: AsyncSession, context: TokenContext, subject: str, previous_org: str, claims: CognitoWorkspaceClaims
) -> None:
    """Restore committed scope, never a snapshot that may predate another write.

    Rollback releases the login lock. A later switch (including A -> B -> A) or
    primary-team edit can win before we reacquire it. Re-read active membership and
    team under the same canonical lock before writing. Legacy placements could mark
    several local rows active; only in that case use the previously selected org,
    with its CURRENT team and authority. A successful switch leaves one active org.
    """
    login = await db.scalar(select(User).where(User.cognito_sub == subject).with_for_update().execution_options(populate_existing=True))
    if login is None:
        raise RuntimeError("Canonical login no longer exists")
    # Avoid SQLAlchemy identity-map snapshots surviving rollback/test sessions.
    db.expire_all()
    _, memberships = await _memberships(db, context)
    active = [org_id for org_id, (_, membership) in memberships.items() if membership and membership.is_active]
    org_id = active[0] if len(active) == 1 else previous_org
    pair = memberships.get(org_id)
    if not pair or (active and org_id not in active):
        raise RuntimeError("Previous workspace no longer has an active membership")
    user, membership = pair
    user = await db.scalar(select(User).where(User.id == user.id).with_for_update().execution_options(populate_existing=True))
    if user is None:
        raise RuntimeError("Workspace account no longer exists")
    if membership:
        membership = await db.scalar(
            select(TenantMembership).where(TenantMembership.id == membership.id).with_for_update().execution_options(populate_existing=True)
        )
        if membership is None or membership.revoked_at is not None or not membership.is_active:
            raise RuntimeError("Workspace membership no longer exists or is inactive")
    org = await db.get(Organization, org_id)
    if org is None:
        raise RuntimeError("Workspace organization no longer exists")
    selected = await _workspace(db, context, org, user, membership)
    await claims.set(
        subject,
        {
            "custom:org_id": selected.org_id,
            "custom:team_id": selected.team_id,
            "custom:department_id": selected.department_id,
            "custom:role": selected.role,
        },
    )
    await db.commit()


async def select_workspace(db: AsyncSession, context: TokenContext, org_id: str, claims: CognitoWorkspaceClaims) -> Workspace:
    _require_human(context)
    login = await login_user(db, context.user_id)
    if not login or not login.cognito_sub:
        raise HTTPException(403, "You do not have membership in that organization")
    # Serialize switches for a login before resolving mutable memberships. Lock
    # and reload the target membership too: a removal while waiting must fail.
    login = await db.scalar(select(User).where(User.id == login.id).with_for_update().execution_options(populate_existing=True))
    if not login:
        raise HTTPException(403, "Login account no longer exists")
    subject = login.cognito_sub
    _, memberships = await _memberships(db, context)
    pair = memberships.get(org_id)
    org = await db.get(Organization, org_id) if pair else None
    if not pair or not org:
        raise HTTPException(403, "You do not have membership in that organization")
    user, membership = pair
    user = await db.scalar(select(User).where(User.id == user.id).with_for_update().execution_options(populate_existing=True))
    if not user:
        raise HTTPException(403, "Membership no longer exists")
    if membership:
        membership = await db.scalar(
            select(TenantMembership).where(TenantMembership.id == membership.id).with_for_update().execution_options(populate_existing=True)
        )
        if not membership or membership.revoked_at is not None:
            raise HTTPException(403, "Membership no longer exists")
    selected = await _workspace(db, context, org, user, membership)
    previous_claims = None
    try:
        await link_login_to_workspace(db, login, user)
        ids = await linked_user_ids(db, login, username=context.cognito_username)
        await db.execute(update(TenantMembership).where(TenantMembership.user_id.in_(ids)).values(is_active=False))
        if membership:
            membership.is_active = True
        else:
            db.add(TenantMembership(user_id=user.id, tenant_id=org_id, role="member", is_active=True, joined_via="workspace_selection"))
        await db.flush()
        previous_claims, applied = await claims.set(
            subject,
            {
                "custom:org_id": selected.org_id,
                "custom:team_id": selected.team_id,
                "custom:department_id": selected.department_id,
                "custom:role": selected.role,
            },
        )
        await db.commit()
    except Exception as exc:
        await db.rollback()
        if previous_claims is not None:
            try:
                await _reconcile_after_failed_switch(db, context, subject, previous_claims.get("custom:org_id", ""), claims)
            except Exception:
                await db.rollback()
                logger.exception("Failed to restore workspace claims after database failure")
        logger.exception("Workspace selection failed for subject=%s org=%s", subject, org_id)
        raise HTTPException(503, "Could not save your workspace selection. Please try again.") from exc
    return selected.model_copy(update={"is_current": True, "role": applied["custom:role"]})


@router.post("/workspaces/select", response_model=Workspace)
async def switch_workspace(
    body: SwitchWorkspaceRequest,
    current_user: TokenContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    claims: CognitoWorkspaceClaims = Depends(get_workspace_claims),
) -> Workspace:
    return await select_workspace(db, current_user, body.org_id, claims)


class TenantContextRequest(BaseModel):
    model_config = {"extra": "forbid"}
    org_id: str = Field(min_length=1, max_length=255)
    expected_membership: str | None = Field(default=None, max_length=300)


@router.post("/workspaces/context")
async def tenant_context_exchange(
    body: TenantContextRequest,
    current_user: TokenContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Issue a process-local tenant lease without changing global selection."""
    from fastapi.encoders import jsonable_encoder
    from fastapi.responses import JSONResponse

    from src.auth.tenant_context import issue_context

    result = await issue_context(db, current_user, body.org_id, body.expected_membership)
    return JSONResponse(jsonable_encoder(result), headers={"Cache-Control": "no-store"})
