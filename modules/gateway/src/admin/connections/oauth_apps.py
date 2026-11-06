"""Platform-admin registration of reusable provider apps for personal consent."""

import hashlib
import json
import secrets
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from src.admin.audit import write_admin_audit
from src.admin.audit_operation import mark_admin_effects
from src.admin.connections.routes import _app_lifecycle_lock, _SecretSafeRoute
from src.auth.dependencies import get_current_user
from src.shared.database import get_db, get_session_factory
from src.shared.identity.oauth_setup import atlassian_mcp_config, get_oauth_setup
from src.shared.identity.oauth_setup_store import enqueue_setup
from src.shared.identity.oauth_setup_worker import process_setup_once

router = APIRouter()
oauth_router = APIRouter(prefix="/admin/connections/oauth", tags=["connections"], route_class=_SecretSafeRoute)
atlassian_mcp_router = APIRouter(prefix="/admin/connections/atlassian-mcp", tags=["connections"], route_class=_SecretSafeRoute)


class RegisterOAuthApp(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_id: str = Field(min_length=1, max_length=1024, pattern=r"^[\x21-\x7e]+$")
    client_secret: SecretStr = Field(min_length=1, max_length=4096)
    replace: bool = False
    app_id: str | None = None


class CreateSlackApp(BaseModel):
    model_config = ConfigDict(extra="forbid")

    configuration_token: SecretStr = Field(min_length=1, max_length=4096)


def require_app_admin(caller=Depends(get_current_user)):
    if not caller.is_admin or caller.account_type != "human":
        raise HTTPException(403, detail="Platform administrator privileges required")
    return caller


@atlassian_mcp_router.post("/prepare", dependencies=[Depends(_app_lifecycle_lock)])
async def prepare_atlassian_mcp(
    caller=Depends(require_app_admin),
    db: AsyncSession = Depends(get_db),
    setup=Depends(get_oauth_setup),
):
    try:
        plan = await run_in_threadpool(atlassian_mcp_config, setup.settings, setup.secret_store)
        now = datetime.now(UTC)
        digest = hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest()
        await enqueue_setup(db, setup_key="atlassian-mcp", config_digest=digest, registration_mode=plan["registration_mode"], now=now)
        await db.commit()
        status = await process_setup_once(get_session_factory(), setup=setup, owner=f"admin-{secrets.token_hex(12)}", now=now)
        if status in {"resolved", "idle"}:
            result = await run_in_threadpool(setup.prepare_atlassian_mcp, before_effects=None)
        elif status in {"blocked", "uncertain", "lost_lease"}:
            result = {"status": "unavailable", "owner": "platform_admin", "action": "Reconcile uncertain setup without repeating registration."}
        else:
            result = {"status": "setup_required"}
        await write_admin_audit(
            db, actor=caller, action="connection_prepare_atlassian_mcp", target_type="oauth_app", target_id="atlassian-mcp", org_id="platform"
        )
        return result
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(503, detail={"error": "atlassian_setup_unavailable"}) from None


@oauth_router.post("/slack/app/automate", dependencies=[Depends(_app_lifecycle_lock)])
async def automate_slack_app(
    body: CreateSlackApp,
    caller=Depends(require_app_admin),
    db: AsyncSession = Depends(get_db),
    setup=Depends(get_oauth_setup),
):
    try:
        result = await run_in_threadpool(setup.automate_slack, body.configuration_token.get_secret_value(), before_effects=mark_admin_effects)
        await write_admin_audit(
            db, actor=caller, action="connection_automate_slack_app", target_type="oauth_app", target_id="slack", org_id="platform"
        )
        return result
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(
            503, detail={"error": "oauth_setup_unavailable", "message": "Slack setup did not finish. Refresh its status before resuming."}
        ) from None


@oauth_router.get("/{provider}/app")
async def app_status(provider: str, caller=Depends(require_app_admin), setup=Depends(get_oauth_setup)):
    try:
        return await run_in_threadpool(setup.status, provider, admin=True)
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(
            503, detail={"error": "oauth_setup_unavailable", "message": "App setup could not be checked. Refresh before making changes."}
        ) from None


@oauth_router.post("/{provider}/app", dependencies=[Depends(_app_lifecycle_lock)])
async def register_oauth_app(
    provider: str,
    body: RegisterOAuthApp,
    caller=Depends(require_app_admin),
    db: AsyncSession = Depends(get_db),
    setup=Depends(get_oauth_setup),
):
    try:
        result = await run_in_threadpool(
            setup.register,
            provider,
            body.client_id,
            body.client_secret.get_secret_value(),
            before_effects=mark_admin_effects,
            replace=body.replace,
            app_id=body.app_id,
        )
        await write_admin_audit(
            db, actor=caller, action="connection_register_oauth_app", target_type="oauth_app", target_id=provider, org_id="platform"
        )
        return result
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(
            503, detail={"error": "oauth_setup_unavailable", "message": "App setup could not finish. Refresh to check its status before retrying."}
        ) from None


router.include_router(oauth_router)
router.include_router(atlassian_mcp_router)
