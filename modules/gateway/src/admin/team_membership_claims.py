"""Commit team writes, then reconcile the selected login's refresh claims.

Postgres is authoritative. Cognito is a separate service, so this is deliberately
not an atomic transaction: a failed synchronization returns a retryable error
after saving membership. Every retry reconciles, including an idempotent delete.
Already-issued JWTs remain valid until refresh/expiry.
"""

import logging

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.workspaces import CognitoWorkspaceClaims
from src.shared.identity.workspaces import login_subject_for_user, login_user, primary_team_for_workspace
from src.shared.models.organization import User

logger = logging.getLogger(__name__)


async def commit_team_memberships(db: AsyncSession, *, user_id: str, org_id: str) -> None:
    """Used by every supported admin team-membership write endpoint.

    Commit BEFORE taking the canonical login lock, then reload the local account.
    Workspace selection uses the same lock before reading teams/writing Cognito.
    Thus a competing selection either sees the committed primary, or completes
    first and this synchronization respects its current Cognito org. Never restore
    an old snapshot after failure, or write claims for a rolled-back membership.
    """
    await db.commit()
    try:
        user = await db.scalar(select(User).where(User.id == user_id, User.org_id == org_id).execution_options(populate_existing=True))
        if user is None:
            raise ValueError("Organization account no longer exists")
        subject = await login_subject_for_user(db, user)
        if not subject:
            if user.cognito_username:
                raise ValueError("Login account has no verified Cognito subject")
            # Unprovisioned/shadow users have no claims cache to update. Do not
            # invent linkage using an email or a self-linked external identity.
            await db.commit()
            return
        login = await login_user(db, subject)
        if login is None or login.cognito_sub != subject:
            raise ValueError("Canonical login account was not found")
        login = await db.scalar(select(User).where(User.id == login.id).with_for_update().execution_options(populate_existing=True))
        if login is None or login.cognito_sub != subject:
            raise ValueError("Canonical login account changed")
        user = await db.scalar(
            select(User).where(User.id == user_id, User.org_id == org_id).with_for_update().execution_options(populate_existing=True)
        )
        if user is None or await login_subject_for_user(db, user) != subject:
            raise ValueError("Organization account linkage changed")
        team = await primary_team_for_workspace(db, user, org_id)
        await CognitoWorkspaceClaims().set_team(subject, org_id, team.id if team else "", team.department_id if team else "")
        await db.commit()
    except Exception as exc:
        await db.rollback()
        logger.exception("Team membership saved but claims reconciliation failed: user=%s org=%s", user_id, org_id)
        raise HTTPException(
            503,
            detail={
                "code": "team_membership_claims_sync_failed",
                "membership_saved": True,
                "message": "Team membership was saved, but login claims could not be synchronized. Retry this request before refreshing the session.",
            },
        ) from exc
