"""Issue a read capability from an actual held shared grant and bootstrap claim."""

from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy import select

from app.models.bootstrap import WorkspaceBootstrapReadToken
from app.services.bootstrap_observation import operation_connect
from app.services.leases import _as_utc


from superplane_bootstrap.read_tokens import token_digest as token_digest


async def issue_bootstrap_read_token(*, connect, grant):
    from superplane_bootstrap.read_tokens import (
        ReadTokenRefused,
        issue_bootstrap_read_token as issue,
    )

    try:
        return await issue(connect=connect, grant=grant)
    except ReadTokenRefused as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc


async def authenticate_bootstrap_token(request, db):
    raw = request.headers.get("authorization", "")
    if not raw.startswith("sp-bootstrap-read-") or len(raw) > 256:
        raise HTTPException(401, "Bootstrap read credential required")
    token = await db.scalar(
        select(WorkspaceBootstrapReadToken)
        .where(WorkspaceBootstrapReadToken.token_hash == token_digest(raw))
        .execution_options(populate_existing=True)
    )
    if token is None or _as_utc(token.expires_at) <= datetime.now(UTC):
        raise HTTPException(401, "Bootstrap read credential is unavailable")
    async with operation_connect(request)() as connection:
        current = await connection.fetchval(
            "SELECT EXISTS (SELECT 1 FROM harness_operation_leases l JOIN harness_operations o USING(operation_id) "
            "WHERE l.operation_id=$1 AND l.org_id=$2 AND l.workspace_id=$3 "
            "AND l.holder=$4 AND l.attempt_id=$5 AND l.fence_token=$6 AND l.closed_at IS NULL "
            "AND l.expires_at>clock_timestamp() AND l.runtime_deadline>clock_timestamp() "
            "AND o.cancel_requested_at IS NULL AND o.action='provision' AND o.state IN ('pending','running','retrying'))",
            token.operation_id,
            token.org_id,
            token.workspace_id,
            token.lease_holder,
            token.lease_attempt_id,
            token.lease_fence_token,
        )
    if not current:
        raise HTTPException(
            401, "Bootstrap read credential has a stale execution lease"
        )
    return token
