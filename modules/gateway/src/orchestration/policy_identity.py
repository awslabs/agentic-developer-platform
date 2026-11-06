"""Compare historical human subjects without changing accepted authority records."""

from sqlalchemy.exc import SQLAlchemyError


async def same_human_principal(session, *, org_id, recorded_id, current_id):
    """Allow only a live, tenant-scoped login-subject to workspace-user alias.

    Exact identities retain their existing semantics. Different identifiers must
    resolve to the same live human workspace account using authoritative login
    links; names, email, role and caller-supplied identity links are not proof.
    Neither the historical policy nor its receipt is normalized or rewritten.
    """
    if not org_id or not recorded_id or not current_id:
        return False
    if recorded_id == current_id:
        return True
    from src.shared.identity.workspaces import workspace_user

    try:
        # Refresh login, user and membership ORM rows, including the verified
        # org-placement fallback. Identity-link IDs are already scalar reads.
        before = await workspace_user(session, recorded_id, org_id, refresh=True)
        after = await workspace_user(session, current_id, org_id, refresh=True)
        if before is None or after is None or before.id != after.id:
            return False
        return before.org_id == org_id and before.user_kind == "human" and not before.is_shadow
    except (SQLAlchemyError, ValueError, OSError):
        return False
