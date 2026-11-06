"""Workflow authority belongs to human workspace members, independent of role."""

from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.identity import workspaces


async def is_workflow_member(db: AsyncSession, *, user_id: str, org_id: str, username: str = "") -> bool:
    """Resolve live membership using the same identity rules as workspace login.

    Revoked memberships never qualify. Legacy native accounts retain their own
    workspace access. ``is_active`` selects a workspace; switching it must not
    invalidate an approval or stop an already approved flow. No role is required,
    and neither a service account nor a shadow identity can approve itself.
    """
    if not user_id or not org_id:
        return False
    _, memberships = await workspaces.memberships_for_login(db, user_id, username=username)
    pair = memberships.get(org_id)
    return pair is not None and pair[0].user_kind == "human" and not pair[0].is_shadow
