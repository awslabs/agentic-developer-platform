"""``GET /me/cli-capabilities`` — Issue #5621 (CLI-08).

Answers, for the signed-in caller only, "which CLI operations are supported by
this gateway, enabled on this deployment, permitted for me, and ready to run".
`contract.py` owns the semantics and explains why those four are kept separate.

**Why `/me`, with no target parameter at all.** The absence of a scope argument
is the security property, not a convenience: identity comes only from the
validated token, so there is no parameter for a caller to point at another
tenant. `src/budget/me_routes.py` documents the same choice for the same reason —
the adjacent unscoped router there is the subject of an IDOR, and a route that
accepts a scope inherits that class of bug by construction.

**No `/api` prefix.** CloudFront strips the first `/api` before the origin, so the
browser path `/api/me/cli-capabilities` is mounted here as `/me/cli-capabilities`
(guarded by `tests/test_route_prefix_convention.py`).

**Read-only.** Nothing in this module or `contract.py` writes to a database,
queue or provider, and no cost is incurred by calling it.

This endpoint is discovery, never authorization: every operation it describes
still performs its own permission check when the request actually arrives. That
is what makes publishing the document safe — a client that tampers with it can
only mislead itself.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.access_control import AccessControl
from src.admin.config import Permission
from src.admin.exceptions import AccessDeniedError
from src.admin.models import RequestLog
from src.auth.dependencies import get_current_user
from src.shared.database import get_db
from src.shared.schemas.auth import TokenContext

from . import contract

logger = logging.getLogger("bedrockgateway.cli_capabilities")

router = APIRouter(tags=["cli-capabilities"])


async def get_access_control(db: Annotated[AsyncSession, Depends(get_db)]) -> AccessControl:
    """The same permission authority the real routes use.

    Deliberately the real one, not a reimplementation: if discovery computed
    `permitted` from its own copy of the rules, the two would drift and the CLI
    would confidently predict the opposite of what the server then does.
    """
    return AccessControl(db)


@router.get(
    "/me/cli-capabilities",
    summary="Discover which CLI operations you can run on this deployment",
    description=(
        "Returns the capability contract version, this gateway's release (or an "
        "explicit unknown), and for each operation four independent states: "
        "supported, enabled, permitted and ready. Own-scope only — the response "
        "describes the authenticated caller in their own tenant and takes no "
        "target parameter. Read-only; discovery never replaces the authorization "
        "performed on the actual request."
    ),
)
async def get_my_cli_capabilities(
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    access: Annotated[AccessControl, Depends(get_access_control)],
) -> dict:
    """Describe the caller's own capabilities. Writes nothing."""
    return await contract.describe(current_user, access)


class CliRequestResponse(BaseModel):
    request_id: str
    timestamp: datetime
    method: str
    path: str
    status_code: int
    response_time_ms: int


def _not_visible() -> HTTPException:
    return HTTPException(status_code=404, detail={"error": "request_not_visible", "message": "No request with that ID is visible."})


@router.get("/me/cli-requests/{request_id}", response_model=CliRequestResponse)
async def get_my_cli_request(
    request_id: str,
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    access: Annotated[AccessControl, Depends(get_access_control)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CliRequestResponse:
    """Return redacted gateway request metadata within the caller's tenant."""
    try:
        await access.check_permission(current_user, Permission.LOGS_READ, target_org_id=current_user.org_id)
    except AccessDeniedError:
        raise _not_visible() from None
    result = await db.execute(
        select(RequestLog)
        .where(RequestLog.request_id == request_id, RequestLog.org_id == current_user.org_id)
        .order_by(RequestLog.timestamp.desc())
        .limit(1)
    )
    row = result.scalars().first()
    if row is None:
        raise _not_visible()
    return CliRequestResponse(
        request_id=row.request_id,
        timestamp=row.timestamp,
        method=row.method,
        path=row.path,
        status_code=row.status_code,
        response_time_ms=row.response_time_ms,
    )
