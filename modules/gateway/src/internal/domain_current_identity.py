"""Current ADP human identity for a registered domain operation producer."""

from fastapi import HTTPException
from sqlalchemy import or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from src.admin.cognito_claims import cognito_user_pool_id
from src.internal.domain_operation_store import aws_client
from src.shared.database import get_session_factory
from src.shared.identity.workspaces import PLACEMENT_VERIFICATION
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import User
from src.shared.models.vault import UserIdentity


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
    linked_ids = select(UserIdentity.user_id).where(
        UserIdentity.org_id == adp_org_id,
        UserIdentity.provider == "cognito",
        UserIdentity.provider_user_id == subject,
        UserIdentity.verification_method == PLACEMENT_VERIFICATION,
    )
    try:
        rows = (
            await db.execute(
                select(User, TenantMembership)
                .join(TenantMembership, TenantMembership.user_id == User.id)
                .where(
                    TenantMembership.tenant_id == adp_org_id,
                    or_(User.cognito_sub == subject, User.id == subject, User.id.in_(linked_ids)),
                )
                .execution_options(populate_existing=True)
            )
        ).all()
    except SQLAlchemyError:
        raise HTTPException(503, "current identity membership unavailable") from None
    if len(rows) != 1 or rows[0][1].revoked_at is not None or rows[0][0].is_shadow or rows[0][0].user_kind != "human":
        raise HTTPException(403, "current identity refused")
    return {
        "version": 1,
        "subject": subject,
        "principal_type": "human",
        "adp_org_id": adp_org_id,
        "membership_id": rows[0][1].id,
        "active": True,
        "enabled": True,
    }


async def revalidate_original_humans(operation: dict, *, adp_org_id: str) -> None:
    """Recheck the two human subjects recorded in the paid admission, not the worker."""
    requester = operation.get("requester")
    approver = operation.get("approved_by")
    if (
        not isinstance(requester, str)
        or not requester
        or not isinstance(approver, str)
        or not approver
        or requester == approver
    ):
        raise HTTPException(403, "original domain identities refused")
    try:
        async with get_session_factory()() as db:
            await current_human_identity(db, subject=requester, adp_org_id=adp_org_id)
            await current_human_identity(db, subject=approver, adp_org_id=adp_org_id)
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(503, "current identity provider unavailable") from None
