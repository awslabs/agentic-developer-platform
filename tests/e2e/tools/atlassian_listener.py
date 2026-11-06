"""Standalone ADP-authenticated return for the isolated Identity MCP probe."""

from urllib.parse import urlsplit

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from tests.e2e.tools.atlassian_oauth_probe import ProbeBlocked


class ConsentReturn(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    state: str = Field(min_length=1, max_length=128)


def create_probe_app(ledger):
    """Mount only with an exact, previously approved ADP return URL."""
    path = "/auth/probes/atlassian/return"
    parsed = urlsplit(ledger.lifecycle.config.return_url)
    if parsed.path not in {path, "/api" + path} or parsed.query or parsed.fragment:
        raise ValueError("The probe return URL must point to the authenticated listener")
    from src.auth.middleware import get_current_user_context
    from src.shared.database import get_db
    from src.shared.identity.workspaces import workspace_user
    app = FastAPI()
    router = APIRouter(prefix="/auth/probes/atlassian")
    caller_dep = Depends(get_current_user_context)
    db_dep = Depends(get_db)
    state_query = Query(min_length=1, max_length=128)

    async def principal(caller=caller_dep, db: AsyncSession = db_dep):
        if caller.account_type != "human" or not caller.org_id or not caller.user_id or caller.auth_source != "jwt":
            raise HTTPException(status_code=403, detail={"error": "human_required"})
        user = await workspace_user(db, caller.user_id, caller.org_id, username=caller.cognito_username, refresh=True)
        if user is None or user.user_kind != "human" or user.is_shadow:
            raise HTTPException(status_code=403, detail={"error": "human_required"})
        return user.id, caller.org_id

    principal_dep = Depends(principal)

    async def finish(owner, tenant, state):
        try:
            await run_in_threadpool(ledger.complete, owner, state, tenant=tenant)
        except ProbeBlocked:
            raise HTTPException(status_code=403, detail={"error": "invalid_consent"}) from None
        except (BotoCoreError, ClientError, OSError, RuntimeError, ValueError):
            raise HTTPException(status_code=502, detail={"error": "identity_unavailable"}) from None
        return {"status": "identity_completion_requested"}

    @router.post("/start")
    async def start(principal_id=principal_dep):
        try:
            return await run_in_threadpool(ledger.begin, *principal_id)
        except ProbeBlocked:
            raise HTTPException(status_code=409, detail={"error": "attempt_requires_reconciliation"}) from None
        except (BotoCoreError, ClientError, OSError, RuntimeError, ValueError):
            raise HTTPException(status_code=502, detail={"error": "identity_unavailable"}) from None

    @router.get("/return")
    async def callback(request: Request, state: str = state_query, principal_id=principal_dep):
        if set(request.query_params) != {"state"}:
            raise HTTPException(status_code=403, detail={"error": "invalid_consent"})
        return await finish(*principal_id, state)

    @router.post("/return")
    async def complete(body: ConsentReturn, principal_id=principal_dep):
        return await finish(*principal_id, body.state)

    app.include_router(router)
    return app
