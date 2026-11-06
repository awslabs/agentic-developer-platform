"""Reconstruct Product authority for a trusted, persisted human initiator."""

import re
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.platform_authority import apply_platform_admin_authority
from src.shared.identity.workspaces import login_subject_for_user
from src.shared.models.organization import User
from src.shared.schemas.auth import TokenContext


async def stored_product_actor(db: AsyncSession, *, user_id: str, tenant_id: str) -> TokenContext:
    """Resolve current platform authority for an already authenticated record.

    Callers must establish that user_id is the canonical User.id of their live
    run or persisted operation, and retain their own run/membership checks.
    No request header, display role, or cached admin flag is an input here.
    Use only after the ordinary Product team path denies access.

    A global administrator's account may live in another organization. Preserve
    the selected tenant and canonical initiator rather than changing either to
    match the login account used for the IdP authority check.
    """
    if not user_id or not tenant_id:
        raise HTTPException(404, "Product actor not found")
    member = await db.scalar(
        select(User)
        .where(User.id == user_id, User.user_kind == "human", User.is_shadow.is_(False))
        .execution_options(populate_existing=True)
        .with_for_update(read=True)
    )
    if member is None:
        raise HTTPException(404, "Product actor not found")
    actor = TokenContext(
        user_id=member.id,
        org_id=tenant_id,
        team_id="",
        department_id="",
        account_type="human",
        expires_at=datetime.now(UTC) + timedelta(seconds=30),
    )
    try:
        subject = await login_subject_for_user(db, member, lock=True)
    except ValueError:
        raise HTTPException(404, "Product actor not found") from None
    if not isinstance(subject, str) or not re.fullmatch(r"[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}", subject):
        return actor
    login = await db.scalar(select(User).where(User.cognito_sub == subject).execution_options(populate_existing=True).with_for_update(read=True))
    # is_admin=True requests the existing fresh check, conferring no privilege
    # itself. It requires current IdP authority and respects durable demotions.
    candidate = actor.model_copy(
        update={
            "user_id": subject,
            "cognito_username": (login.cognito_username or "") if login is not None else "",
            "is_admin": True,
        }
    )
    verified = await apply_platform_admin_authority(candidate, db)
    # Do not propagate a persisted username into signed-username workspace
    # resolution. It is only a lookup hint to the subject-checking IdP reader.
    return actor.model_copy(update={"is_admin": verified.is_admin})
