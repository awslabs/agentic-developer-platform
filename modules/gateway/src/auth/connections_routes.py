"""User-bound AgentCore Identity consent, with only metadata retained by ADP."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import secrets
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from urllib.parse import urlparse

import boto3
import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from src.shared.config import get_settings
from src.shared.database import get_db
from src.shared.identity.oauth_providers import GITHUB_REPOSITORY_SCOPES, PROVIDER_SCOPES
from src.shared.identity.oauth_setup import get_oauth_setup
from src.shared.models.audit import AuditLog
from src.shared.models.base import new_uuid
from src.shared.models.vault import OAuthConnectionAttempt, OAuthConnectionBinding, UserCredential

from .middleware import get_current_user_context
from .vault_routes import _resolve_user_id_in_context

router = APIRouter(prefix="/auth/connections/oauth", tags=["connections"])
logger = logging.getLogger(__name__)


def get_identity_client():
    return boto3.client("bedrock-agentcore", region_name=get_settings().aws_region)


def github_revocation_pointer(pointer):
    return SimpleNamespace(
        **{
            field: getattr(pointer, field)
            for field in ("id", "org_id", "user_id", "service", "credential_type", "agent_consent", "expires_at", "secret_arn", "scopes")
        }
    )


async def revoke_github_grant(client_id: str, client_secret: str, access_token: str) -> tuple[str, int | None]:
    from src.internal.credential_routes import _validate_proxy_url

    try:
        endpoint = f"https://api.github.com/applications/{client_id}/grant"
        destination = await asyncio.to_thread(_validate_proxy_url, endpoint, get_settings())
        async with httpx.AsyncClient(timeout=httpx.Timeout(10, connect=3), trust_env=False, follow_redirects=False) as client:
            async with client.stream(
                "DELETE",
                destination,
                auth=httpx.BasicAuth(client_id, client_secret),
                json={"access_token": access_token},
                headers={"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28", "Host": "api.github.com"},
                extensions={"sni_hostname": "api.github.com"},
            ) as response:
                return ("confirmed" if response.status_code == 204 else "failed", response.status_code)
    except (httpx.HTTPError, ValueError):
        return "failed", None


async def revoke_disconnected_github(db: AsyncSession, *, pointer, identity, actor_id: str) -> str:
    from src.internal.credential_routes import _identity_access_token
    from src.internal.github_oauth import github_app_client
    from src.shared.models.organization import User

    outcome, status = "failed", None
    try:
        client = await github_app_client()
        owner = await db.scalar(select(User).where(User.id == pointer.user_id, User.org_id == pointer.org_id))
        if client is not None and owner is not None and identity is not None:
            token = await _identity_access_token(pointer, owner, identity)
            outcome, status = await revoke_github_grant(client["client_id"], client["client_secret"], token)
    except Exception:
        outcome, status = "failed", None
    db.add(
        AuditLog(
            org_id=pointer.org_id,
            actor_id=actor_id,
            event_type="github_token_revoked",
            details={"connection_id": pointer.id, "outcome": outcome, "http_status": status},
        )
    )
    await db.commit()
    if outcome == "failed":
        logger.warning("GitHub grant revocation failed for connection %s (HTTP %s); local access remains denied", pointer.id, status)
    return outcome


def _identity_config():
    settings = get_settings()
    parsed = urlparse(settings.oauth_return_url)
    if (
        not settings.oauth_workload_name.endswith("-oauth-user")
        or parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("/auth/connections/oauth/return", "/api/auth/connections/oauth/return")
    ):
        raise HTTPException(status_code=503, detail={"error": "oauth_unavailable"})
    return settings.oauth_workload_name, settings.oauth_return_url


async def _human_caller(caller, db):
    if caller.account_type != "human":
        raise HTTPException(status_code=403, detail={"error": "human_required"})
    await _resolve_user_id_in_context(caller, db)
    if not caller.org_id or not caller.user_id:
        raise HTTPException(status_code=403, detail={"error": "human_required"})
    return caller


class StartConsent(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    chat_write: bool = False
    reconnect: bool = False
    # #8555: GitHub only. Asks GitHub for `repo` and `workflow` as well as the profile so
    # the owner's Dev Box (`gh`, `git`) and `adp github token` can act as the owner.
    repository_access: bool = False


class CompleteConsent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: str
    session_uri: str


@router.get("/providers")
async def provider_status(
    caller=Depends(get_current_user_context),
    db: AsyncSession = Depends(get_db),
    setup=Depends(get_oauth_setup),
):
    await _human_caller(caller, db)
    providers = []
    for provider, (service, _) in PROVIDER_SCOPES.items():
        try:
            status = await run_in_threadpool(setup.status, provider)
        except Exception:
            status = {"provider": provider, "service": service, "status": "unavailable"}
        from src.internal.oauth_binding import connection_status

        try:
            status["connection_status"] = await connection_status(db, caller=caller, provider=provider, service=service)
        except Exception:
            status["connection_status"] = "unavailable"
        if provider == "github" and status["status"] == "registered" and status["connection_status"] == "verified":
            from src.internal.credential_routes import _identity_access_token
            from src.internal.github_oauth import github_app_client, is_github_app_grant, observe_github_app_user, selected_github_connection
            from src.shared.models.organization import User

            try:
                credential, binding, _ = await selected_github_connection(db, user_id=caller.user_id, tenant_id=caller.org_id)
                if is_github_app_grant(credential.scopes):
                    owner = await db.scalar(select(User).where(User.id == caller.user_id, User.org_id == caller.org_id))
                    client = await github_app_client()
                    if owner is None or client is None:
                        raise ValueError("GitHub App unavailable")
                    token = await _identity_access_token(credential, owner, get_identity_client())
                    observation = await observe_github_app_user(token, **client)
                    if observation.account_id != binding.provider_account_id or list(observation.resources) != binding.observed_resources:
                        raise ValueError("GitHub App account changed")
                    status["login"] = observation.profile["login"]
            except Exception:
                status["connection_status"] = "reconnect_required"
        status["tool_setup_required"] = os.environ.get(f"ADP_TOOLS_{service.upper()}_MEDIATOR_ENABLED") != "true"
        providers.append(status)
    return {"providers": providers}


def _scope_snapshot(value):
    if (
        not isinstance(value, list)
        or not 0 < len(value) <= 32
        or any(not isinstance(scope, str) or re.fullmatch(r"[\x21\x23-\x5B\x5D-\x7E]{1,128}", scope) is None for scope in value)
        or len(set(value)) != len(value)
    ):
        return None
    return list(value)


@router.post("/{provider}/start")
async def start_connection(
    provider: str,
    body: StartConsent | None = None,
    caller=Depends(get_current_user_context),
    db: AsyncSession = Depends(get_db),
    identity=Depends(get_identity_client),
):
    caller = await _human_caller(caller, db)
    if provider not in PROVIDER_SCOPES:
        raise HTTPException(status_code=404, detail={"error": "unknown_provider"})
    workload_name, return_url = _identity_config()
    service, configured_scopes = PROVIDER_SCOPES[provider]
    wants_write = body is not None and body.chat_write
    if wants_write and provider != "slack":
        raise HTTPException(status_code=400, detail={"error": "unsupported_write_consent"})
    wants_repository = body is not None and body.repository_access
    if wants_repository and provider != "github":
        raise HTTPException(status_code=400, detail={"error": "unsupported_repository_consent"})
    if wants_write and os.environ.get("ADP_TOOLS_SLACK_MEDIATOR_ENABLED") != "true":
        raise HTTPException(status_code=503, detail={"error": "oauth_unavailable"})
    from src.internal.github_oauth import github_app_client, is_github_app_grant

    app_client = await github_app_client() if provider == "github" else None
    scopes = _scope_snapshot(
        (["users:read", "chat:write"] if wants_write else ["users:read"])
        if provider == "slack"
        else (list(GITHUB_REPOSITORY_SCOPES) if wants_repository else configured_scopes)
    )
    if app_client:
        scopes = []
    if scopes is None:
        raise HTTPException(status_code=503, detail={"error": "oauth_unavailable"})
    existing = await db.scalar(
        select(UserCredential).where(
            UserCredential.org_id == caller.org_id,
            UserCredential.user_id == caller.user_id,
            UserCredential.service == service,
            UserCredential.provider == "agentcore-identity",
        )
    )
    reconnect = body is not None and body.reconnect
    if existing and not (wants_write or wants_repository or reconnect):
        raise HTTPException(status_code=409, detail={"error": "already_connected"})
    snapshot = None
    if existing:
        from src.internal.github_oauth import credential_digest

        if existing.agent_consent is not True and not (app_client and is_github_app_grant(existing.scopes)):
            raise HTTPException(409, detail={"error": "agent_consent_required", "message": "Allow agents to use this account before reconnecting."})
        previous = await db.get(OAuthConnectionBinding, existing.id)
        snapshot = {"credential_sha256": credential_digest(existing), "binding_revision": previous.revision if previous else None}
        # An ordinary reconnect retains optional permissions previously granted.
        if provider == "slack" and "chat:write" in (existing.scopes or {}).get("oauth_scopes", []):
            scopes = ["users:read", "chat:write"]
        if provider == "github" and "repo" in (existing.scopes or {}).get("oauth_scopes", []) and not app_client:
            scopes = list(GITHUB_REPOSITORY_SCOPES)
        if provider == "github" and is_github_app_grant(existing.scopes) and not app_client:
            raise HTTPException(503, detail={"error": "oauth_unavailable"})
    if provider == "slack" and "chat:write" in scopes and os.environ.get("ADP_TOOLS_SLACK_MEDIATOR_ENABLED") != "true":
        raise HTTPException(status_code=503, detail={"error": "oauth_unavailable"})
    state = secrets.token_urlsafe(32)
    provider_name = f"{workload_name.removesuffix('-oauth-user')}-{provider}-user"
    try:
        workload = await run_in_threadpool(identity.get_workload_access_token_for_user_id, workloadName=workload_name, userId=caller.user_id)
        response = await run_in_threadpool(
            identity.get_resource_oauth2_token,
            workloadIdentityToken=workload["workloadAccessToken"],
            resourceCredentialProviderName=provider_name,
            scopes=scopes,
            oauth2Flow="USER_FEDERATION",
            forceAuthentication=True,
            resourceOauth2ReturnUrl=return_url,
            customState=state,
        )
    except Exception:
        raise HTTPException(
            status_code=502,
            detail={
                "error": "identity_unavailable",
                "message": "Account connection is unavailable. Check the app setup in Connections and try again.",
            },
        ) from None
    if not response.get("authorizationUrl") or not response.get("sessionUri"):
        raise HTTPException(status_code=502, detail={"error": "identity_unavailable"})
    db.add(
        OAuthConnectionAttempt(
            state_digest=hashlib.sha256(state.encode()).hexdigest(),
            org_id=caller.org_id,
            user_id=caller.user_id,
            provider=provider,
            session_uri=response["sessionUri"],
            requested_scopes=scopes,
            connection_snapshot=(
                {
                    "previous": snapshot,
                    "github_app_grant": {
                        "schema_version": 1,
                        "repository_access": wants_repository
                        or bool(existing and is_github_app_grant(existing.scopes) and existing.scopes["github_app_grant"]["repository_access"]),
                    },
                }
                if app_client
                else snapshot
            ),
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
        )
    )
    await db.commit()
    return {"authorization_url": response["authorizationUrl"], "state": state, "session_uri": response["sessionUri"]}


@router.get("/return")
async def consent_return(request: Request):
    status = "error" if request.query_params.get("error") else "returned"
    return RedirectResponse(url=f"/settings/connections?oauth_consent={status}", status_code=303)


@router.post("/return", status_code=201)
async def complete_connection(
    body: CompleteConsent,
    caller=Depends(get_current_user_context),
    db: AsyncSession = Depends(get_db),
    identity=Depends(get_identity_client),
):
    caller = await _human_caller(caller, db)
    workload_name, _ = _identity_config()
    attempt = await db.scalar(
        select(OAuthConnectionAttempt)
        .where(
            OAuthConnectionAttempt.state_digest == hashlib.sha256(body.state.encode()).hexdigest(),
            OAuthConnectionAttempt.org_id == caller.org_id,
            OAuthConnectionAttempt.user_id == caller.user_id,
        )
        .with_for_update()
    )
    if (
        not attempt
        or attempt.consumed_at
        or attempt.expires_at.replace(tzinfo=UTC) <= datetime.now(UTC)
        or not secrets.compare_digest(attempt.session_uri, body.session_uri)
    ):
        raise HTTPException(status_code=403, detail={"error": "invalid_consent"})
    from src.internal.github_oauth import github_app_client, is_github_app_grant

    app_snapshot = attempt.connection_snapshot if attempt.provider == "github" and attempt.requested_scopes == [] else None
    app_grant = app_snapshot.get("github_app_grant") if isinstance(app_snapshot, dict) else None
    app_client = await github_app_client() if app_grant else None
    scopes = [] if app_client and is_github_app_grant({"github_app_grant": app_grant}) else _scope_snapshot(attempt.requested_scopes)
    if scopes is None or attempt.provider not in PROVIDER_SCOPES:
        raise HTTPException(status_code=403, detail={"error": "invalid_consent"})
    write_consent = attempt.provider == "slack" and scopes == ["users:read", "chat:write"]
    if attempt.provider == "slack" and scopes not in (["users:read"], ["users:read", "chat:write"]):
        raise HTTPException(status_code=403, detail={"error": "invalid_consent"})
    if attempt.provider == "github" and scopes not in ([], ["read:user"], list(GITHUB_REPOSITORY_SCOPES)):
        raise HTTPException(status_code=403, detail={"error": "invalid_consent"})
    if attempt.provider == "github" and scopes == [] and not app_client:
        raise HTTPException(status_code=403, detail={"error": "invalid_consent"})
    if write_consent and os.environ.get("ADP_TOOLS_SLACK_MEDIATOR_ENABLED") != "true":
        raise HTTPException(status_code=503, detail={"error": "oauth_unavailable"})
    service, _ = PROVIDER_SCOPES[attempt.provider]
    existing = await db.scalar(
        select(UserCredential)
        .where(
            UserCredential.org_id == caller.org_id,
            UserCredential.user_id == caller.user_id,
            UserCredential.service == service,
            UserCredential.provider == "agentcore-identity",
        )
        .with_for_update()
    )
    from src.internal.github_oauth import credential_digest

    previous_binding = await db.get(OAuthConnectionBinding, existing.id) if existing else None
    current_snapshot = (
        None
        if not existing
        else {
            "credential_sha256": credential_digest(existing),
            "binding_revision": previous_binding.revision if previous_binding else None,
        }
    )
    if current_snapshot != (app_snapshot.get("previous") if app_grant else attempt.connection_snapshot):
        raise HTTPException(409, detail={"error": "connection_changed", "message": "The connection changed. Start consent again."})
    if existing and existing.agent_consent is not True and not (app_client and is_github_app_grant(existing.scopes)):
        raise HTTPException(403, detail={"error": "agent_consent_required"})
    if attempt.provider == "atlassian" and not {"read:jira-work", "read:me"} <= set(scopes):
        raise HTTPException(403, detail={"error": "reconnect_required", "message": "Connect Jira again to approve identity verification."})
    try:
        await run_in_threadpool(identity.complete_resource_token_auth, userIdentifier={"userId": caller.user_id}, sessionUri=attempt.session_uri)
    except Exception:
        raise HTTPException(status_code=502, detail={"error": "identity_unavailable"}) from None
    credential = UserCredential(
        id=existing.id if existing else new_uuid(),
        org_id=caller.org_id,
        user_id=caller.user_id,
        service=service,
        credential_type="oauth_token",
        provider="agentcore-identity",
        label="AgentCore OAuth",
        secret_arn=f"agentcore-identity:{workload_name}:{attempt.provider}",
        scopes={"github_app_grant": app_grant} if app_client else {"oauth_scopes": scopes},
        strict=True,
        agent_consent=True,
    )
    from src.internal.credential_routes import _identity_access_token
    from src.internal.oauth_binding import observe_connection
    from src.shared.models.organization import User

    owner = await db.scalar(
        select(User).where(User.id == caller.user_id, User.org_id == caller.org_id, User.user_kind == "human", User.is_shadow.is_(False))
    )
    if owner is None:
        raise HTTPException(403, "current human required")
    token = await _identity_access_token(credential, owner, identity)
    updated_binding = await observe_connection(credential, attempt.provider, token)
    if previous_binding and previous_binding.provider_account_id != updated_binding.provider_account_id:
        raise HTTPException(
            403,
            detail={
                "error": f"{attempt.provider}_account_changed",
                "message": "Use the same provider account, or disconnect before switching accounts.",
            },
        )
    if not existing:
        db.add(credential)
        # The binding references this row by foreign key and the two mappers share no
        # relationship(), so the unit of work does not order the INSERTs: on Postgres the
        # binding went first and failed with a ForeignKeyViolation (dev 2026-10-25, first
        # live GitHub consent). SQLite tests never enforced the constraint. Flush the
        # credential before the binding is added.
        await db.flush()
    else:
        existing.scopes = credential.scopes
        existing.expires_at = None
        existing.agent_consent = credential.agent_consent
        credential = existing
    if previous_binding:
        for field in ("revision", "credential_digest", "observed_scopes", "observed_resources", "verified_at"):
            setattr(previous_binding, field, getattr(updated_binding, field))
    else:
        db.add(updated_binding)
    attempt.consumed_at = datetime.now(UTC)
    db.add(
        AuditLog(
            org_id=caller.org_id,
            actor_id=caller.user_id,
            event_type="oauth_connection_reconsented" if existing else "oauth_connection_created",
            details={"service": service, "provider": attempt.provider, "user_id": caller.user_id, "connection_id": credential.id},
        )
    )
    await db.commit()
    return {"id": credential.id, "service": service, "provider": "agentcore-identity"}


@router.post("/github/token")
async def owner_github_repository_token(
    caller=Depends(get_current_user_context),
    db: AsyncSession = Depends(get_db),
    identity=Depends(get_identity_client),
):
    """The signed-in owner's own GitHub token with repository access (#8555).

    For consumers that are not a Dev Box: ``adp github token`` and the CLI's git credential
    helper on any machine. Same qualification as the Dev Box route, under the owner's own
    session; the token is returned once, never stored by ADP, and every issue is audited.
    """
    from datetime import datetime as _datetime

    from src.crew.github_access import NO_STORE, enabled, owner_github_token

    enabled()
    caller = await _human_caller(caller, db)
    now = int(_datetime.now(UTC).timestamp())
    minted = await owner_github_token(db, org_id=caller.org_id, user_id=caller.user_id, identity=identity, now=now)
    db.add(
        AuditLog(
            org_id=caller.org_id,
            actor_id=caller.user_id,
            event_type="github_token_issued",
            details={"connection_id": minted.credential_id, "login": minted.login, "expires_at": minted.expires_at, "consumer": "owner_session"},
        )
    )
    await db.commit()
    from fastapi.responses import JSONResponse

    return JSONResponse({"token": minted.token, "login": minted.login, "expires_at": minted.expires_at}, headers=NO_STORE)


@router.delete("/{provider}")
async def disconnect_connection(
    provider: str,
    caller=Depends(get_current_user_context),
    db: AsyncSession = Depends(get_db),
):
    caller = await _human_caller(caller, db)
    if provider not in PROVIDER_SCOPES:
        raise HTTPException(status_code=404, detail={"error": "unknown_provider"})
    service, _ = PROVIDER_SCOPES[provider]
    capability = {
        "identity_token_revoked": False,
        "provider_token_revocation": "not_attempted",
        "revocation_complete": False,
        "provider_access_token_expires_at": None,
        "private_assertion_max_seconds": 30,
    }
    pointer = await db.scalar(
        select(UserCredential)
        .where(
            UserCredential.org_id == caller.org_id,
            UserCredential.user_id == caller.user_id,
            UserCredential.service == service,
            UserCredential.provider == "agentcore-identity",
        )
        .with_for_update()
    )
    cancelled = await db.execute(
        delete(OAuthConnectionAttempt).where(
            OAuthConnectionAttempt.org_id == caller.org_id,
            OAuthConnectionAttempt.user_id == caller.user_id,
            OAuthConnectionAttempt.provider == provider,
        )
    )
    from src.internal.github_oauth import is_github_app_grant

    revocation_pointer = github_revocation_pointer(pointer) if pointer and is_github_app_grant(pointer.scopes) else None
    if pointer:
        await db.execute(delete(OAuthConnectionBinding).where(OAuthConnectionBinding.credential_id == pointer.id))
        await db.delete(pointer)
        db.add(
            AuditLog(
                org_id=caller.org_id,
                actor_id=caller.user_id,
                event_type="oauth_connection_disconnected",
                details={
                    "service": service,
                    "provider": provider,
                    "user_id": caller.user_id,
                    "connection_id": pointer.id,
                    **capability,
                },
            )
        )
    elif cancelled.rowcount:
        db.add(
            AuditLog(
                org_id=caller.org_id,
                actor_id=caller.user_id,
                event_type="oauth_connection_cancelled",
                details={"service": service, "provider": provider, "user_id": caller.user_id},
            )
        )
    await db.commit()
    if revocation_pointer:
        try:
            identity = get_identity_client()
        except Exception:
            identity = None
        capability["provider_token_revocation"] = await revoke_disconnected_github(
            db, pointer=revocation_pointer, identity=identity, actor_id=caller.user_id
        )
    return {"status": "disconnected", **capability}
