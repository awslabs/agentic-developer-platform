"""Current ADP human identity for a registered domain operation producer."""

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from src.admin.cognito_claims import cognito_user_pool_id
from src.internal.domain_operation_store import aws_client
from src.shared.database import get_session_factory
from src.shared.identity.workspaces import workspace_user
from src.shared.models.onboarding import TenantMembership


def _cognito_identity(subject: str) -> tuple[bool, str]:
    pool_id = cognito_user_pool_id()
    if not pool_id:
        raise HTTPException(503, "current identity provider unavailable")
    try:
        client = aws_client("cognito-idp")
        users = client.list_users(UserPoolId=pool_id, Filter=f'sub = "{subject}"', Limit=2).get("Users", [])
        if len(users) != 1 or not users[0].get("Username"):
            raise HTTPException(403, "current identity refused")
        result = client.admin_get_user(UserPoolId=pool_id, Username=users[0]["Username"])
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(503, "current identity provider unavailable") from None
    attributes = {attribute["Name"]: attribute["Value"] for attribute in result.get("UserAttributes", [])}
    if attributes.get("sub") != subject or type(result.get("Enabled")) is not bool:
        raise HTTPException(403, "current identity refused")
    return result["Enabled"], attributes.get("custom:org_id", "")


async def current_human_identity(db: AsyncSession, *, subject: str, adp_org_id: str) -> dict:
    if not subject or '"' in subject or "\\" in subject or not adp_org_id:
        raise HTTPException(403, "current identity refused")
    enabled, selected_org = await run_in_threadpool(_cognito_identity, subject)
    if not enabled or selected_org != adp_org_id:
        raise HTTPException(403, "current identity refused")
    try:
        # Use the same proven org-placement resolution as ordinary ADP requests.
        # A legacy membership on the login's home account must not compete with
        # its organization-local account or contribute any additional authority.
        user = await workspace_user(db, subject, adp_org_id)
        membership = (
            (
                await db.execute(
                    select(TenantMembership)
                    .where(TenantMembership.tenant_id == adp_org_id, TenantMembership.user_id == user.id)
                    .execution_options(populate_existing=True)
                )
            ).scalar_one_or_none()
            if user is not None
            else None
        )
    except ValueError:
        raise HTTPException(403, "current identity refused") from None
    except SQLAlchemyError:
        raise HTTPException(503, "current identity membership unavailable") from None
    if (
        user is None
        or membership is None
        or membership.revoked_at is not None
        or membership.is_active is not True
        or user.is_shadow
        or user.user_kind != "human"
    ):
        raise HTTPException(403, "current identity refused")
    return {
        "version": 1,
        "subject": subject,
        "principal_type": "human",
        "adp_org_id": adp_org_id,
        "membership_id": membership.id,
        "active": True,
        "enabled": True,
    }


async def revalidate_original_humans(operation: dict, *, adp_org_id: str) -> None:
    """Recheck recorded humans under the original admission's approval policy."""
    requester = operation.get("requester")
    approver = operation.get("approved_by")
    distinct = operation.get("requires_distinct_approver", True)
    if (
        not isinstance(requester, str)
        or not requester
        or not isinstance(approver, str)
        or not approver
        or type(distinct) is not bool
        or (distinct and requester == approver)
    ):
        raise HTTPException(403, "original domain identities refused")
    try:
        async with get_session_factory()() as db:
            await current_human_identity(db, subject=requester, adp_org_id=adp_org_id)
            if approver != requester:
                await current_human_identity(db, subject=approver, adp_org_id=adp_org_id)
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(503, "current identity provider unavailable") from None
