"""Owner-bound Identity consent and metadata for the separate Atlassian MCP connection."""

import hashlib
import os
import re
import secrets
from datetime import UTC, datetime, timedelta
from typing import Literal
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from src.auth.connections_routes import _human_caller, get_identity_client
from src.auth.middleware import get_current_user_context
from src.internal.github_oauth import credential_digest
from src.internal.tool_adapter_observation import observe_connection
from src.internal.tool_adapter_policy import PolicyProbe, observe_policy
from src.shared.database import get_db
from src.shared.identity.atlassian_readiness import observation_digest, verified_connection
from src.shared.identity.oauth_grant_revocation import enqueue_revocation
from src.shared.identity.oauth_setup import atlassian_mcp_config, get_oauth_setup
from src.shared.identity.oauth_setup_store import read_tenant_policy_observation
from src.shared.models.audit import AuditLog
from src.shared.models.base import new_uuid
from src.shared.models.oauth_setup import OAuthGrantRevocation
from src.shared.models.organization import User
from src.shared.models.vault import OAuthConnectionAttempt, OAuthConnectionBinding, UserCredential

SERVICE = "atlassian-mcp"
SCOPES = (
    "read:me",
    "read:account",
    "read:jira:agent-interface",
    "search:jira:agent-interface",
    "read:confluence:agent-interface",
    "search:confluence:agent-interface",
    "offline_access",
)
WRITE_SCOPES = (*SCOPES, "write:jira:agent-interface")


def writes_available():
    return (
        os.environ.get("ADP_TOOLS_ATLASSIAN_WRITES_ENABLED") == "true"
        and len(os.environ.get("ADP_TOOLS_ATLASSIAN_WRITES_SHA256", "")) == 64
        and all(character in "0123456789abcdef" for character in os.environ["ADP_TOOLS_ATLASSIAN_WRITES_SHA256"])
    )


router = APIRouter(prefix="/auth/connections/atlassian-mcp", tags=["connections"])


class StartConsent(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    capability_set: Literal["reads", "jira-writes"] = "reads"
    reconnect_revision: str | None = None


class CompleteConsent(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    state: str
    session_uri: str


class BrowserConsent(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    state: str = Field(min_length=32, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    session_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ResumeObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    attempt_id: str = Field(pattern=r"^[0-9a-f]{64}$")


class SelectSite(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    connection_revision: str
    site_id: str


class DisconnectConsent(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    connection_revision: str | None = None


@router.post("/policy/observe")
async def observe_connection_policy(body: PolicyProbe, caller=Depends(get_current_user_context), db: AsyncSession = Depends(get_db)):
    caller = await _human_caller(caller, db)
    credential = await _connection(db, caller)
    if credential is None:
        raise HTTPException(403, detail={"error": "not_connected"})
    return await observe_policy(caller=caller, body=body, connection_id=credential.id, db=db)


async def _consent_plan(db, caller, setup):
    policy = await read_tenant_policy_observation(db, setup_key=SERVICE, tenant_id=caller.org_id, now=datetime.now(UTC))
    try:
        status = await run_in_threadpool(setup.atlassian_mcp_status, policy_result=policy)
        if status.get("status") in {"ready", "consent_required"}:
            return atlassian_mcp_config(setup.settings, setup.secret_store)
    except Exception:
        pass
    raise HTTPException(503, detail={"error": "atlassian_mcp_unavailable"})


async def _lock_owner(db, caller):
    owner = await db.scalar(select(User).where(User.id == caller.user_id, User.org_id == caller.org_id).with_for_update())
    if owner is None:
        raise HTTPException(403, detail={"error": "human_required"})


async def _connection(db, caller, *, lock=False):
    query = select(UserCredential).where(
        UserCredential.org_id == caller.org_id,
        UserCredential.user_id == caller.user_id,
        UserCredential.service == SERVICE,
        UserCredential.provider == "agentcore-identity",
    )
    return await db.scalar(query.with_for_update() if lock else query)


async def _assert_snapshot(db, caller, attempt, plan):
    existing = await _connection(db, caller, lock=True)
    previous = await db.get(OAuthConnectionBinding, existing.id) if existing else None
    if attempt.connection_snapshot != {
        "credential_sha256": credential_digest(existing) if existing else None,
        "binding_revision": previous.revision if previous else None,
        "config_revision": plan["metadata_sha256"],
    }:
        raise HTTPException(409, detail={"error": "connection_changed"})
    return existing, previous


@router.post("/start")
async def start_connection(
    body: StartConsent,
    caller=Depends(get_current_user_context),
    db: AsyncSession = Depends(get_db),
    setup=Depends(get_oauth_setup),
    identity=Depends(get_identity_client),
):
    caller = await _human_caller(caller, db)
    plan = await _consent_plan(db, caller, setup)
    await _lock_owner(db, caller)
    existing = await _connection(db, caller)
    previous = await db.get(OAuthConnectionBinding, existing.id) if existing else None
    if existing and (previous is None or previous.revision != body.reconnect_revision):
        raise HTTPException(409, detail={"error": "connection_changed"})
    if not existing and body.reconnect_revision is not None:
        raise HTTPException(409, detail={"error": "connection_changed"})
    if body.capability_set == "jira-writes" and (
        not writes_available() or existing is None or previous is None or previous.selected_site_id is None or existing.agent_consent is not True
    ):
        raise HTTPException(503, detail={"error": "write_consent_unavailable"})
    requested_scopes = WRITE_SCOPES if body.capability_set == "jira-writes" else SCOPES
    state = secrets.token_urlsafe(32)
    workload = f"{setup.settings.oauth_workload_name.removesuffix('-oauth-user')}-atlassian-mcp-oauth-user"
    provider = f"{setup.settings.oauth_workload_name.removesuffix('-oauth-user')}-atlassian-mcp-user"
    try:
        user_token = await run_in_threadpool(identity.get_workload_access_token_for_user_id, workloadName=workload, userId=caller.user_id)
        response = await run_in_threadpool(
            identity.get_resource_oauth2_token,
            workloadIdentityToken=user_token["workloadAccessToken"],
            resourceCredentialProviderName=provider,
            scopes=list(requested_scopes),
            resources=["https://mcp.atlassian.com/v2/mcp"],
            oauth2Flow="USER_FEDERATION",
            forceAuthentication=True,
            resourceOauth2ReturnUrl=plan["return_url"],
            customState=state,
        )
        # AgentCore can omit the optional status when returning a new consent
        # URL. The authorization URL and session URI are still both required.
        if response.get("sessionStatus") not in {None, "IN_PROGRESS"} or not response.get("authorizationUrl") or not response.get("sessionUri"):
            raise ValueError("Identity session unavailable")
    except Exception:
        raise HTTPException(502, detail={"error": "identity_unavailable"}) from None
    pending = (
        await db.scalars(
            select(OAuthConnectionAttempt)
            .where(
                OAuthConnectionAttempt.org_id == caller.org_id,
                OAuthConnectionAttempt.user_id == caller.user_id,
                OAuthConnectionAttempt.provider == SERVICE,
                OAuthConnectionAttempt.consumed_at.is_(None),
                OAuthConnectionAttempt.superseded_at.is_(None),
            )
            .with_for_update()
        )
    ).all()
    for older in pending:
        older.superseded_at = datetime.now(UTC)
    attempt = OAuthConnectionAttempt(
        state_digest=hashlib.sha256(state.encode()).hexdigest(),
        org_id=caller.org_id,
        user_id=caller.user_id,
        provider=SERVICE,
        session_uri=response["sessionUri"],
        requested_scopes=list(requested_scopes),
        connection_snapshot={
            "credential_sha256": credential_digest(existing) if existing else None,
            "binding_revision": previous.revision if previous else None,
            "config_revision": plan["metadata_sha256"],
        },
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
    )
    db.add(attempt)
    await db.commit()
    return {
        "attempt_id": attempt.state_digest,
        "state": state,
        "session_uri": response["sessionUri"],
        "authorization_url": response["authorizationUrl"],
    }


@router.get("/return")
async def consent_return(request: Request):
    state = request.query_params.get("state", "")
    session = request.query_params.get("session_id", "")
    query = {"oauth_consent": "error"}
    if (
        set(request.query_params) == {"state", "session_id"}
        and all(len(request.query_params.getlist(key)) == 1 for key in ("state", "session_id"))
        and re.fullmatch(r"[A-Za-z0-9_-]{32,128}", state)
        and 0 < len(session) <= 2048
        and not any(ord(character) < 32 or ord(character) == 127 for character in session)
    ):
        query = {"oauth_consent": "returned", "atlassian_state": state, "atlassian_session_sha256": hashlib.sha256(session.encode()).hexdigest()}
    return RedirectResponse(f"/settings/connections?{urlencode(query)}", status_code=303, headers={"Referrer-Policy": "no-referrer"})


@router.post("/complete-return", status_code=201)
async def complete_browser_return(
    body: BrowserConsent,
    caller=Depends(get_current_user_context),
    db: AsyncSession = Depends(get_db),
    setup=Depends(get_oauth_setup),
    identity=Depends(get_identity_client),
):
    caller = await _human_caller(caller, db)
    attempt = await db.scalar(
        select(OAuthConnectionAttempt).where(
            OAuthConnectionAttempt.state_digest == hashlib.sha256(body.state.encode()).hexdigest(),
            OAuthConnectionAttempt.org_id == caller.org_id,
            OAuthConnectionAttempt.user_id == caller.user_id,
            OAuthConnectionAttempt.provider == SERVICE,
        )
    )
    if attempt is None or not secrets.compare_digest(hashlib.sha256(attempt.session_uri.encode()).hexdigest(), body.session_sha256):
        raise HTTPException(403, detail={"error": "invalid_consent"})
    return await complete_connection(CompleteConsent(state=body.state, session_uri=attempt.session_uri), caller, db, setup, identity)


@router.get("/attempts/{attempt_id}")
async def consent_attempt_status(attempt_id: str, caller=Depends(get_current_user_context), db: AsyncSession = Depends(get_db)):
    caller = await _human_caller(caller, db)
    attempt = await db.scalar(
        select(OAuthConnectionAttempt).where(
            OAuthConnectionAttempt.state_digest == attempt_id,
            OAuthConnectionAttempt.org_id == caller.org_id,
            OAuthConnectionAttempt.user_id == caller.user_id,
            OAuthConnectionAttempt.provider == SERVICE,
        )
    )
    if attempt is None:
        raise HTTPException(404, detail={"error": "attempt_unavailable"})
    if attempt.superseded_at is not None:
        return {"status": "superseded"}
    if attempt.consumed_at is not None:
        completed = (attempt.connection_snapshot or {}).get("completed_connection")
        credential = await _connection(db, caller)
        binding = await db.get(OAuthConnectionBinding, credential.id) if credential else None
        if not binding or completed != {"id": credential.id, "revision": binding.revision}:
            return {"status": "changed"}
        return {"status": "completed", **completed}
    if attempt.identity_completed_at is not None:
        return {"status": "observing"}
    if attempt.expires_at.replace(tzinfo=UTC) <= datetime.now(UTC):
        return {"status": "expired"}
    return {"status": "pending"}


@router.post("/resume", status_code=201)
async def resume_observation(
    body: ResumeObservation,
    caller=Depends(get_current_user_context),
    db: AsyncSession = Depends(get_db),
    setup=Depends(get_oauth_setup),
):
    """Retry provider verification only after Identity already completed consent."""
    caller = await _human_caller(caller, db)
    plan = await _consent_plan(db, caller, setup)
    await _lock_owner(db, caller)
    attempt = await db.scalar(
        select(OAuthConnectionAttempt)
        .where(
            OAuthConnectionAttempt.state_digest == body.attempt_id,
            OAuthConnectionAttempt.org_id == caller.org_id,
            OAuthConnectionAttempt.user_id == caller.user_id,
            OAuthConnectionAttempt.provider == SERVICE,
        )
        .with_for_update()
    )
    if (
        attempt is None
        or attempt.identity_completed_at is None
        or attempt.consumed_at is not None
        or attempt.superseded_at is not None
        or attempt.requested_scopes not in (list(SCOPES), list(WRITE_SCOPES))
        or attempt.requested_scopes == list(WRITE_SCOPES)
        and not writes_available()
    ):
        raise HTTPException(403, detail={"error": "invalid_consent"})
    await _assert_snapshot(db, caller, attempt, plan)
    # Only the observation window is renewed. No callback is accepted here,
    # Identity completion is never repeated, and fresh grant readback is required.
    attempt.expires_at = datetime.now(UTC) + timedelta(minutes=10)
    db.add(
        AuditLog(
            org_id=caller.org_id,
            actor_id=caller.user_id,
            event_type="oauth_connection_observation_resumed",
            details={"service": SERVICE, "attempt_id": attempt.state_digest},
        )
    )
    return await _finish_observation(attempt, caller, db, setup, plan)


@router.post("/complete", status_code=201)
async def complete_connection(
    body: CompleteConsent,
    caller=Depends(get_current_user_context),
    db: AsyncSession = Depends(get_db),
    setup=Depends(get_oauth_setup),
    identity=Depends(get_identity_client),
):
    caller = await _human_caller(caller, db)
    plan = await _consent_plan(db, caller, setup)
    await _lock_owner(db, caller)
    attempt = await db.scalar(
        select(OAuthConnectionAttempt)
        .where(
            OAuthConnectionAttempt.state_digest == hashlib.sha256(body.state.encode()).hexdigest(),
            OAuthConnectionAttempt.org_id == caller.org_id,
            OAuthConnectionAttempt.user_id == caller.user_id,
            OAuthConnectionAttempt.provider == SERVICE,
        )
        .with_for_update()
    )
    if (
        attempt is None
        or attempt.consumed_at is not None
        or attempt.superseded_at is not None
        or attempt.expires_at.replace(tzinfo=UTC) <= datetime.now(UTC)
        or not secrets.compare_digest(attempt.session_uri, body.session_uri)
        or attempt.requested_scopes not in (list(SCOPES), list(WRITE_SCOPES))
        or attempt.requested_scopes == list(WRITE_SCOPES)
        and not writes_available()
        or not isinstance(attempt.connection_snapshot, dict)
        or attempt.connection_snapshot.get("config_revision") != plan["metadata_sha256"]
    ):
        raise HTTPException(403, detail={"error": "invalid_consent"})
    await _assert_snapshot(db, caller, attempt, plan)
    if attempt.identity_completed_at is None:
        if attempt.identity_completion_started_at is not None:
            raise HTTPException(409, detail={"error": "reconnect_required"})
        attempt.identity_completion_started_at = datetime.now(UTC)
        await db.commit()
        db.expire_all()
        await _lock_owner(db, caller)
        attempt = await db.scalar(
            select(OAuthConnectionAttempt)
            .where(
                OAuthConnectionAttempt.state_digest == hashlib.sha256(body.state.encode()).hexdigest(),
                OAuthConnectionAttempt.org_id == caller.org_id,
                OAuthConnectionAttempt.user_id == caller.user_id,
                OAuthConnectionAttempt.provider == SERVICE,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if (
            attempt is None
            or attempt.consumed_at is not None
            or attempt.superseded_at is not None
            or attempt.identity_completed_at is not None
            or attempt.expires_at.replace(tzinfo=UTC) <= datetime.now(UTC)
        ):
            raise HTTPException(409, detail={"error": "connection_changed"})
        await _assert_snapshot(db, caller, attempt, plan)
        try:
            await run_in_threadpool(identity.complete_resource_token_auth, userIdentifier={"userId": caller.user_id}, sessionUri=attempt.session_uri)
        except Exception:
            raise HTTPException(502, detail={"error": "identity_unavailable"}) from None
        attempt.identity_completed_at = datetime.now(UTC)
    return await _finish_observation(attempt, caller, db, setup, plan)


async def _finish_observation(attempt, caller, db, setup, plan):
    attempt_id = attempt.state_digest
    # Release locks for adapter admission on both initial completion and retries.
    await db.commit()
    observation = await observe_connection(attempt=attempt, user_id=caller.user_id, tenant_id=caller.org_id)
    db.expire_all()
    await _lock_owner(db, caller)
    attempt = await db.scalar(
        select(OAuthConnectionAttempt)
        .where(
            OAuthConnectionAttempt.state_digest == attempt_id,
            OAuthConnectionAttempt.org_id == caller.org_id,
            OAuthConnectionAttempt.user_id == caller.user_id,
            OAuthConnectionAttempt.provider == SERVICE,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        attempt is None
        or attempt.consumed_at is not None
        or attempt.superseded_at is not None
        or attempt.identity_completed_at is None
        or attempt.expires_at.replace(tzinfo=UTC) <= datetime.now(UTC)
    ):
        raise HTTPException(409, detail={"error": "connection_changed"})
    existing, previous = await _assert_snapshot(db, caller, attempt, plan)
    if (
        observation.attempt_id != attempt.state_digest
        or observation.config_revision != plan["metadata_sha256"]
        or observation.purpose != "connection_observe"
        or not observation.provider_user_id
        or not observation.sites
        or len({site.site_id for site in observation.sites}) != len(observation.sites)
        or any(not site.site_id or len(site.site_id) > 512 for site in observation.sites)
        or any(not set(site.scopes) <= set(observation.granted_scopes) for site in observation.sites)
        or not {"read:me", "read:account"} <= set(observation.granted_scopes) <= set(attempt.requested_scopes)
        or not any(scope in attempt.requested_scopes and scope.endswith(":agent-interface") for site in observation.sites for scope in site.scopes)
        or attempt.requested_scopes == list(WRITE_SCOPES)
        and (
            "write:jira:agent-interface" not in observation.granted_scopes
            or not any("write:jira:agent-interface" in site.scopes for site in observation.sites)
        )
        or previous is not None
        and previous.provider_account_id != observation.provider_user_id
    ):
        raise HTTPException(403, detail={"error": "observation_mismatch"})
    workload = f"{setup.settings.oauth_workload_name.removesuffix('-oauth-user')}-atlassian-mcp-oauth-user"
    credential = UserCredential(
        id=existing.id if existing else new_uuid(),
        org_id=caller.org_id,
        user_id=caller.user_id,
        service=SERVICE,
        credential_type="oauth_token",
        provider="agentcore-identity",
        label="Atlassian MCP OAuth",
        secret_arn=f"agentcore-identity:{workload}:{SERVICE}",
        scopes={"oauth_scopes": list(attempt.requested_scopes)},
        strict=True,
        agent_consent=True,
    )
    if existing:
        existing.scopes = credential.scopes
        credential = existing
    else:
        db.add(credential)
        await db.flush()  # the binding below references this row; see connections_routes.complete_connection
        await db.flush()
    binding = OAuthConnectionBinding(
        credential_id=credential.id,
        org_id=caller.org_id,
        user_id=caller.user_id,
        provider=SERVICE,
        revision=new_uuid(),
        credential_digest=credential_digest(credential),
        provider_account_id=observation.provider_user_id,
        observed_scopes=list(observation.granted_scopes),
        observed_resources=[{"site_id": site.site_id, "scopes": list(site.scopes)} for site in observation.sites],
        selected_site_id=observation.sites[0].site_id if len(observation.sites) == 1 else None,
        verified_at=datetime.now(UTC),
    )
    if previous:
        for field in ("revision", "credential_digest", "observed_scopes", "observed_resources", "selected_site_id", "verified_at"):
            setattr(previous, field, getattr(binding, field))
    else:
        db.add(binding)
    attempt.consumed_at = datetime.now(UTC)
    attempt.connection_snapshot = {
        **attempt.connection_snapshot,
        "completed_connection": {"id": credential.id, "revision": binding.revision},
        "provider_observation_sha256": observation_digest(binding),
    }
    await db.commit()
    return {"id": credential.id, "revision": binding.revision, "service": SERVICE, "provider": "agentcore-identity"}


@router.post("/select-site")
async def select_site(
    body: SelectSite,
    caller=Depends(get_current_user_context),
    db: AsyncSession = Depends(get_db),
):
    caller = await _human_caller(caller, db)
    await _lock_owner(db, caller)
    credential = await _connection(db, caller, lock=True)
    binding = await db.get(OAuthConnectionBinding, credential.id) if credential else None
    if (
        binding is None
        or binding.provider != SERVICE
        or binding.org_id != caller.org_id
        or binding.user_id != caller.user_id
        or binding.credential_digest != credential_digest(credential)
        or credential.agent_consent is not True
    ):
        raise HTTPException(403, detail={"error": "not_connected"})
    if binding.revision != body.connection_revision:
        raise HTTPException(409, detail={"error": "connection_changed"})
    site = (
        next(
            (site for site in binding.observed_resources if isinstance(site, dict) and site.get("site_id") == body.site_id),
            None,
        )
        if isinstance(binding.observed_resources, list)
        else None
    )
    granted = binding.observed_scopes if isinstance(binding.observed_scopes, list) else ()
    requested = credential.scopes.get("oauth_scopes", ()) if isinstance(credential.scopes, dict) else ()
    if (
        site is None
        or not isinstance(site.get("scopes"), list)
        or not any(
            scope in WRITE_SCOPES and scope.endswith(":agent-interface") and scope in granted and scope in requested for scope in site["scopes"]
        )
    ):
        raise HTTPException(403, detail={"error": "site_not_observed"})
    binding.selected_site_id = body.site_id
    binding.revision = new_uuid()
    await db.commit()
    return {"id": credential.id, "revision": binding.revision, "selected_site_id": body.site_id}


@router.delete("")
async def disconnect_connection(
    body: DisconnectConsent | None = None,
    caller=Depends(get_current_user_context),
    db: AsyncSession = Depends(get_db),
):
    caller = await _human_caller(caller, db)
    await _lock_owner(db, caller)
    credential = await _connection(db, caller, lock=True)
    binding = await db.get(OAuthConnectionBinding, credential.id) if credential else None
    if credential and (binding is None or binding.revision != (body.connection_revision if body else None)):
        raise HTTPException(409, detail={"error": "connection_changed"})
    attempts = (
        await db.scalars(
            select(OAuthConnectionAttempt)
            .where(
                OAuthConnectionAttempt.org_id == caller.org_id,
                OAuthConnectionAttempt.user_id == caller.user_id,
                OAuthConnectionAttempt.provider == SERVICE,
                OAuthConnectionAttempt.consumed_at.is_(None),
                OAuthConnectionAttempt.superseded_at.is_(None),
            )
            .with_for_update()
        )
    ).all()
    now = datetime.now(UTC)
    for attempt in attempts:
        attempt.superseded_at = now
        await enqueue_revocation(
            db,
            tenant_id=caller.org_id,
            owner_ref=caller.user_id,
            grant_revision=attempt.state_digest,
            source_kind="attempt",
            connection_id=None,
            provider_account_id=None,
            now=now,
        )
    if credential:
        await enqueue_revocation(
            db,
            tenant_id=caller.org_id,
            owner_ref=caller.user_id,
            grant_revision=binding.revision,
            source_kind="binding",
            connection_id=credential.id,
            provider_account_id=binding.provider_account_id,
            now=now,
        )
        await db.delete(binding)
        await db.delete(credential)
    pending = await db.scalar(
        select(OAuthGrantRevocation.id)
        .where(
            OAuthGrantRevocation.tenant_id == caller.org_id,
            OAuthGrantRevocation.owner_ref == caller.user_id,
            OAuthGrantRevocation.state != "done",
        )
        .limit(1)
    )
    if credential or attempts:
        db.add(
            AuditLog(
                org_id=caller.org_id,
                actor_id=caller.user_id,
                event_type="oauth_connection_disconnected" if credential else "oauth_connection_cancelled",
                details={
                    "service": SERVICE,
                    "provider": "agentcore-identity",
                    "user_id": caller.user_id,
                    "connection_id": credential.id if credential else None,
                    "identity_token_revoked": False,
                    "provider_token_revocation": "not_attempted",
                    "revocation_complete": False,
                    "revocation_pending": pending is not None,
                },
            )
        )
    await db.commit()
    return {
        "status": "disconnected",
        "identity_token_revoked": False,
        "provider_token_revocation": "not_attempted",
        "revocation_complete": False,
        "revocation_pending": pending is not None,
    }


@router.get("/status")
async def atlassian_mcp_status(
    caller=Depends(get_current_user_context),
    db: AsyncSession = Depends(get_db),
    setup=Depends(get_oauth_setup),
):
    await _human_caller(caller, db)
    try:
        policy_result = await read_tenant_policy_observation(db, setup_key="atlassian-mcp", tenant_id=caller.org_id, now=datetime.now(UTC))
        status = dict(await run_in_threadpool(setup.atlassian_mcp_status, policy_result=policy_result))
        status.pop("connection", None)
        credential = await _connection(db, caller)
        binding = await db.get(OAuthConnectionBinding, credential.id) if credential else None
        if status.get("status") in {"ready", "consent_required"}:
            verified = await verified_connection(
                db, credential=credential, binding=binding, config_revision=getattr(setup.settings, "atlassian_metadata_sha256", None)
            )
            status = (
                {"status": "ready", "owner": None, "action": None}
                if verified
                else {"status": "consent_required", "owner": "user", "action": "Connect with Atlassian to verify your account."}
            )
        if (
            binding
            and binding.provider == SERVICE
            and binding.org_id == caller.org_id
            and binding.user_id == caller.user_id
            and binding.credential_digest == credential_digest(credential)
        ):
            status["connection"] = {
                "id": credential.id,
                "revision": binding.revision,
                "selected_site_id": binding.selected_site_id,
                "sites": [site["site_id"] for site in binding.observed_resources or []],
            }
        if status.get("status") == "consent_required":
            attempt = await db.scalar(
                select(OAuthConnectionAttempt)
                .where(
                    OAuthConnectionAttempt.org_id == caller.org_id,
                    OAuthConnectionAttempt.user_id == caller.user_id,
                    OAuthConnectionAttempt.provider == SERVICE,
                    OAuthConnectionAttempt.identity_completed_at.is_not(None),
                    OAuthConnectionAttempt.consumed_at.is_(None),
                    OAuthConnectionAttempt.superseded_at.is_(None),
                )
                .order_by(OAuthConnectionAttempt.identity_completed_at.desc())
                .limit(1)
            )
            if (
                attempt is not None
                and attempt.requested_scopes == list(SCOPES)
                and attempt.connection_snapshot
                == {
                    "credential_sha256": credential_digest(credential) if credential else None,
                    "binding_revision": binding.revision if binding else None,
                    "config_revision": getattr(setup.settings, "atlassian_metadata_sha256", None),
                }
            ):
                status["pending_observation"] = {"attempt_id": attempt.state_digest}
                status["action"] = "Your Atlassian consent is saved. Finish account verification."
        return status
    except Exception:
        return {"status": "unavailable", "owner": "platform_admin", "action": "Ask a platform administrator to check deployment setup."}
