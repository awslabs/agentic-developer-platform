"""Explicit administrator assignment to the existing GitHub broker login.

This does not merge native Cognito accounts or add a new authentication method.
"""

from __future__ import annotations

import asyncio
import re
from typing import Literal

import httpx
from fastapi import HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.cognito_service import CognitoService
from src.admin.identity.cognito_sync import cognito_identity
from src.admin.identity.identity_index_writer import IdentityIndexWriter
from src.admin.memberships import project_member_org_ids, upsert_tenant_membership
from src.admin.org_members import add_user_to_org
from src.admin.team_memberships import add_membership
from src.shared.identity.verification import ADMIN_ATTESTED
from src.shared.models.base import utcnow
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import Organization, Team, User
from src.shared.models.vault import UserIdentity


class GitHubEnrollmentRequest(BaseModel):
    github_username: str = Field(min_length=1, max_length=39, pattern=r"^[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?$")
    team_id: str = Field(min_length=1, max_length=255)
    role: Literal["member", "org_admin"] = "member"


async def resolve_github_user(username: str) -> dict:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(f"https://api.github.com/users/{username}", headers={"Accept": "application/vnd.github+json"})
    except httpx.HTTPError as exc:
        raise HTTPException(503, "GitHub user lookup is unavailable. Retry later.") from exc
    if response.status_code == 404:
        raise HTTPException(404, "GitHub user not found. Enter a personal GitHub username.")
    if response.status_code != 200:
        raise HTTPException(503, "GitHub user lookup is unavailable. Retry later.")
    data = response.json()
    if data.get("type") != "User" or type(data.get("id")) is not int or data["id"] <= 0 or not isinstance(data.get("login"), str):
        raise HTTPException(422, "Enter a personal GitHub account, not an organization or bot.")
    if data["login"].lower() != username.lower():
        raise HTTPException(409, "GitHub returned a different username. Verify the current account name and retry.")
    return {"id": str(data["id"]), "login": data["login"]}


def ensure_broker_login(github_id: str) -> dict:
    """Reserve only the broker's immutable username; never reset any password."""
    if not re.fullmatch(r"[0-9]+", github_id):
        raise ValueError("GitHub numeric ID required")
    cognito = CognitoService()
    if not cognito.user_pool_id:
        raise HTTPException(503, "Cognito login is not configured.")
    username = f"GitHub_{github_id}"
    try:
        result = cognito.client.admin_get_user(UserPoolId=cognito.user_pool_id, Username=username)
    except cognito.client.exceptions.UserNotFoundException:
        try:
            result = cognito.client.admin_create_user(UserPoolId=cognito.user_pool_id, Username=username, MessageAction="SUPPRESS")["User"]
        except cognito.client.exceptions.UsernameExistsException:
            result = cognito.client.admin_get_user(UserPoolId=cognito.user_pool_id, Username=username)
    subject, actual_username = cognito_identity(result)
    if actual_username.lower() != username.lower() or result.get("Enabled") is False:
        raise HTTPException(409, "The GitHub login is disabled or conflicts with the expected identity.")
    return {"sub": subject, "username": actual_username}


def initialize_login_claims(login: dict, org_id: str, team: Team, role: str) -> None:
    """Initialize an unselected GitHub login; preserve every existing selection."""
    cognito = CognitoService()
    current = cognito.client.admin_get_user(UserPoolId=cognito.user_pool_id, Username=login["username"])
    subject, _ = cognito_identity(current)
    if subject != login["sub"] or current.get("Enabled") is False:
        raise HTTPException(409, "GitHub login changed while assigning access. Retry after review.")
    attrs = {a["Name"]: a["Value"] for a in current.get("UserAttributes", [])}
    if attrs.get("custom:org_id") or attrs.get("custom:role") in {"admin", "platform_admin"}:
        return
    cognito.client.admin_update_user_attributes(
        UserPoolId=cognito.user_pool_id,
        Username=login["username"],
        UserAttributes=[
            {"Name": key, "Value": value}
            for key, value in {
                "custom:org_id": org_id,
                "custom:team_id": team.id,
                "custom:department_id": team.department_id,
                "custom:role": role,
            }.items()
        ],
    )


async def enroll_github_user(db: AsyncSession, org_id: str, request: GitHubEnrollmentRequest) -> dict:
    github = await resolve_github_user(request.github_username)
    # Serialize the same provider identity across organizations, not just one org.
    if db.bind is not None and db.bind.dialect.name == "postgresql":
        await db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:identity, 0))"), {"identity": "github-enroll:" + github["id"]})
    org = await db.scalar(select(Organization).where(Organization.id == org_id).with_for_update())
    team = await db.scalar(select(Team).where(Team.id == request.team_id, Team.org_id == org_id).with_for_update())
    if org is None or team is None:
        raise HTTPException(404, "Organization or team not found in the selected organization.")
    login = await asyncio.to_thread(ensure_broker_login, github["id"])
    holders = (
        await db.execute(
            select(UserIdentity, User)
            .join(User, User.id == UserIdentity.user_id)
            .where(
                UserIdentity.provider == "github",
                UserIdentity.provider_user_id == github["id"],
            )
        )
    ).all()
    canonical = await db.scalar(select(User).where(User.cognito_sub == login["sub"]).with_for_update())
    for identity, holder in holders:
        if holder.cognito_sub and holder.cognito_sub != login["sub"]:
            raise HTTPException(
                409, "This GitHub account is linked to a different login. Use account-linking review; the existing login was not changed."
            )
        if not canonical and not holder.cognito_sub:
            # Do not adopt legacy/channel identities without a verified login binding.
            raise HTTPException(409, "This GitHub identity already has an ADP assignment without a login binding. Ask an administrator to review it.")
    if canonical is None:
        canonical = User(
            org_id=org_id,
            team_id="",
            email="",
            name=github["login"],
            role=request.role,
            cognito_sub=login["sub"],
            cognito_username=login["username"],
            is_shadow=False,
        )
        db.add(canonical)
        await db.flush()
        user = canonical
    elif canonical.org_id == org_id:
        user = canonical
    else:
        user = await add_user_to_org(db, user_id=canonical.id, org_id=org_id, role=request.role)
    # A rerun may add access, but must not silently change an existing primary team.
    if user.team_id and user.team_id != team.id:
        raise HTTPException(409, "This person already has a different primary team. Use Manage teams to change it explicitly.")
    identity = await db.scalar(
        select(UserIdentity).where(UserIdentity.org_id == org_id, UserIdentity.provider == "github", UserIdentity.provider_user_id == github["id"])
    )
    if identity is not None and identity.user_id != user.id:
        raise HTTPException(409, "The GitHub identity belongs to another person in this organization.")
    if identity is None:
        identity = UserIdentity(
            org_id=org_id,
            team_id=team.id,
            user_id=user.id,
            provider="github",
            provider_user_id=github["id"],
            provider_username=github["login"],
            verification_method=ADMIN_ATTESTED,
            verified_at=utcnow(),
        )
        db.add(identity)
    membership = await upsert_tenant_membership(db, user_id=user.id, tenant_id=org_id, role=request.role, joined_via="admin_github_assignment")
    await add_membership(db, user_id=user.id, team_id=team.id, org_id=org_id, is_primary=True)
    await db.commit()
    # DB assignment is durable before projecting login eligibility and claims.
    # Failures return an explicit partial result; retry reuses the same identity.
    try:
        writer = IdentityIndexWriter()
        await writer.put_user_identity(
            provider_user_id=github["id"],
            user_id=user.id,
            org_id=org_id,
            provider="github",
            provider_username=github["login"],
            verification_method=ADMIN_ATTESTED,
        )
        if not await project_member_org_ids(db, user_id=user.id, writer=writer):
            raise RuntimeError("Login eligibility projection failed")
        # Coordinate claim initialization with the existing organization switch lock.
        locked_login = await db.scalar(select(User).where(User.id == canonical.id).with_for_update().execution_options(populate_existing=True))
        current_membership = await db.scalar(
            select(TenantMembership).where(TenantMembership.id == membership.id).with_for_update().execution_options(populate_existing=True)
        )
        current_user = await db.scalar(select(User).where(User.id == user.id).with_for_update().execution_options(populate_existing=True))
        if (
            not locked_login
            or not current_user
            or not current_membership
            or current_membership.revoked_at is not None
            or current_user.team_id != team.id
        ):
            raise RuntimeError("Assignment changed while synchronizing login")
        if locked_login.org_id == org_id:
            await asyncio.to_thread(initialize_login_claims, login, org_id, team, current_membership.role)
        await db.commit()
    except Exception as exc:
        raise HTTPException(
            503, "Assignment saved, but login synchronization is incomplete. Retry the same GitHub username, organization and team."
        ) from exc
    return {
        "id": user.id,
        "org_id": org_id,
        "team_id": team.id,
        "github_id": github["id"],
        "github_username": github["login"],
        "message": "Assigned. Sign in with GitHub to use this organization and team. Existing login selections are preserved.",
    }
