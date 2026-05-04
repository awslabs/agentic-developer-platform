"""Internal credential delivery endpoints.

Issue #136: Vault Phase 3 — service-to-service endpoints + delivery paths (/internal/v1/*)

Endpoints (IAM-signed / shared-secret; internal only):
    GET  /internal/v1/user-credentials      — list credential metadata for a user+service
    POST /internal/v1/proxy-request         — HTTP proxy: inject credential, make request
    POST /internal/v1/credential-materialize — file-type credentials: return presigned URL
    POST /internal/v1/credential-raw-read   — escape hatch: return raw value (dual-gated)

Authentication:
    All endpoints require the X-Internal-Api-Key shared secret.
    See src/internal/routes.py for the _verify_internal_key dependency.

Scope gating:
    materialize   — requires X-Agent-Scopes header to contain "credential:materialize"
    raw-read      — requires X-Agent-Scopes to contain "credential:raw-read" AND
                    BG_VAULT_RAW_READ_ENABLED=true (org-level feature flag)

Every credential access (proxy, materialize, raw-read) writes an audit_log entry
and updates UserCredential.last_used_at.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime, timedelta

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.internal.credential_injector import FILE_CREDENTIAL_TYPES, inject_credential
from src.internal.routes import _verify_internal_key
from src.shared.config import get_settings
from src.shared.database import get_db
from src.shared.models.audit import AuditLog
from src.shared.models.organization import User
from src.shared.models.vault import UserCredential
from src.shared.services.credential_resolver import CredentialNotFoundError, CredentialResolver
from src.shared.services.secrets_manager import SecretsManagerHelper

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/internal/v1", tags=["internal-credentials"])

# Module-level SM helper; replaced in tests via dependency injection.
_sm_helper = SecretsManagerHelper()


def get_secrets_manager() -> SecretsManagerHelper:
    """FastAPI dependency returning the shared SecretsManagerHelper.

    Override in tests to inject a mock.
    """
    return _sm_helper


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class CredentialMetadata(BaseModel):
    """Safe credential metadata — secret_arn and values are NEVER included."""

    id: str
    service: str
    label: str
    credential_type: str
    expires_at: datetime | None
    last_used_at: datetime | None

    @classmethod
    def from_model(cls, cred: UserCredential) -> CredentialMetadata:
        return cls(
            id=cred.id,
            service=cred.service,
            label=cred.label,
            credential_type=cred.credential_type,
            expires_at=cred.expires_at,
            last_used_at=cred.last_used_at,
        )


class ProxyRequestBody(BaseModel):
    """Body for POST /internal/v1/proxy-request."""

    user_id: str
    agent_id: str
    task_id: str
    service: str
    label: str | None = None
    method: str
    url: str
    headers: dict[str, str] | None = None
    body: str | None = None


class ProxyResponse(BaseModel):
    status: int
    headers: dict[str, str]
    body: str
    provenance_id: str


class MaterializeBody(BaseModel):
    """Body for POST /internal/v1/credential-materialize."""

    user_id: str
    agent_id: str
    task_id: str
    service: str
    label: str | None = None


class MaterializeResponse(BaseModel):
    materialize_url: str
    expires_at: datetime
    provenance_id: str


class RawReadBody(BaseModel):
    """Body for POST /internal/v1/credential-raw-read."""

    user_id: str
    agent_id: str
    task_id: str
    service: str
    label: str | None = None
    purpose: str | None = None


class RawReadResponse(BaseModel):
    value: str
    credential_type: str
    provenance_id: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _get_user_context(user_id: str, db: AsyncSession) -> User:
    """Fetch the User row or raise 404."""
    stmt = select(User).where(User.id == user_id)
    result = await db.execute(stmt)
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "user_not_found", "message": f"No user with id={user_id!r}"},
        )
    return user


async def _resolve_credential(
    *,
    db: AsyncSession,
    org_id: str,
    service: str,
    label: str | None,
    user_id: str,
    team_id: str | None,
) -> UserCredential:
    """Resolve credential via scope chain; raise 404 on miss."""
    resolver = CredentialResolver(db)
    try:
        return await resolver.resolve(
            org_id=org_id,
            service=service,
            label=label,
            user_id=user_id,
            team_id=team_id or None,
        )
    except CredentialNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail={"error": "credential_not_found", "message": str(exc)},
        ) from exc


async def _fetch_secret(secret_arn: str, sm: SecretsManagerHelper) -> str:
    """Fetch secret value from SM via asyncio.to_thread."""
    return await asyncio.to_thread(sm.get_secret, secret_arn)


async def _touch_last_used(cred_id: str, db: AsyncSession) -> None:
    """Update last_used_at for the credential row."""
    stmt = update(UserCredential).where(UserCredential.id == cred_id).values(last_used_at=datetime.now(UTC))
    await db.execute(stmt)


async def _write_audit(
    db: AsyncSession,
    *,
    event_type: str,
    org_id: str,
    actor_id: str | None,
    details: dict | None,
) -> None:
    log = AuditLog(
        org_id=org_id,
        event_type=event_type,
        actor_id=actor_id,
        details=details,
    )
    db.add(log)
    await db.flush()


def _check_agent_scope(x_agent_scopes: str | None, required: str) -> None:
    """Raise 403 if the required scope is missing from X-Agent-Scopes."""
    if not x_agent_scopes:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "insufficient_scope",
                "message": f"Agent manifest scope {required!r} is required for this operation.",
            },
        )
    scopes = {s.strip() for s in x_agent_scopes.split(",")}
    if required not in scopes:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "insufficient_scope",
                "message": f"Agent manifest scope {required!r} is required for this operation.",
            },
        )


# ---------------------------------------------------------------------------
# Endpoint: GET /internal/v1/user-credentials
# ---------------------------------------------------------------------------


@router.get(
    "/user-credentials",
    response_model=list[CredentialMetadata],
    summary="List credential metadata for a user+service (internal)",
    description=(
        "Returns credential metadata rows for the given user_id and service. "
        "secret_arn and values are NEVER returned. "
        "Intended for agents and ingest Lambdas to check available credentials before "
        "deciding which delivery path to use."
    ),
)
async def list_user_credentials(
    user_id: str = Query(..., description="Internal user UUID (cognito sub or shadow user id)"),
    service: str = Query(..., description="Service name, e.g. 'github'"),
    db: AsyncSession = Depends(get_db),
    _: None = Depends(_verify_internal_key),
) -> list[CredentialMetadata]:
    # Validate user exists.
    user = await _get_user_context(user_id, db)

    # Return all credential rows for this user (user-scoped).
    # Agents listing team/org-scoped creds should use the scope chain resolver.
    stmt = select(UserCredential).where(
        UserCredential.org_id == user.org_id,
        UserCredential.service == service,
        UserCredential.user_id == user_id,
    )
    result = await db.execute(stmt)
    creds = result.scalars().all()

    logger.debug(
        "Listed %d credentials user=%s service=%s",
        len(creds),
        user_id,
        service,
    )
    return [CredentialMetadata.from_model(c) for c in creds]


# ---------------------------------------------------------------------------
# Endpoint: POST /internal/v1/proxy-request
# ---------------------------------------------------------------------------


@router.post(
    "/proxy-request",
    response_model=ProxyResponse,
    summary="HTTP proxy — inject credential and forward request (internal)",
    description=(
        "Resolves the credential for (user_id, service, label?), injects it into the "
        "outbound HTTP request headers, makes the call, and returns the response. "
        "Updates last_used_at and writes an audit log entry on every call."
    ),
)
async def proxy_request(
    body: ProxyRequestBody,
    db: AsyncSession = Depends(get_db),
    sm: SecretsManagerHelper = Depends(get_secrets_manager),
    _: None = Depends(_verify_internal_key),
) -> ProxyResponse:
    provenance_id = str(uuid.uuid4())

    user = await _get_user_context(body.user_id, db)
    cred = await _resolve_credential(
        db=db,
        org_id=user.org_id,
        service=body.service,
        label=body.label,
        user_id=body.user_id,
        team_id=user.team_id,
    )

    # Fetch secret and inject.
    secret_value = await _fetch_secret(cred.secret_arn, sm)
    request_headers = inject_credential(
        cred.credential_type,
        secret_value,
        body.headers or {},
    )

    # Forward the HTTP request.
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.request(
                method=body.method.upper(),
                url=body.url,
                headers=request_headers,
                content=body.body.encode("utf-8") if body.body else None,
            )
    except httpx.RequestError as exc:
        logger.error(
            "Proxy request failed provenance_id=%s url=%s error=%s",
            provenance_id,
            body.url,
            exc,
        )
        raise HTTPException(
            status_code=502,
            detail={
                "error": "upstream_error",
                "message": f"HTTP request to upstream failed: {exc}",
                "provenance_id": provenance_id,
            },
        ) from exc

    # Update last_used_at and write audit log.
    await _touch_last_used(cred.id, db)
    await _write_audit(
        db,
        event_type="vault_proxy_request",
        org_id=user.org_id,
        actor_id=body.agent_id,
        details={
            "provenance_id": provenance_id,
            "user_id": body.user_id,
            "agent_id": body.agent_id,
            "task_id": body.task_id,
            "service": body.service,
            "label": body.label,
            "credential_id": cred.id,
            "method": body.method.upper(),
            "url": body.url,
            "response_status": response.status_code,
        },
    )
    await db.commit()

    logger.info(
        "Proxy request completed provenance_id=%s service=%s url=%s status=%d",
        provenance_id,
        body.service,
        body.url,
        response.status_code,
    )
    return ProxyResponse(
        status=response.status_code,
        headers=dict(response.headers),
        body=response.text,
        provenance_id=provenance_id,
    )


# ---------------------------------------------------------------------------
# Endpoint: POST /internal/v1/credential-materialize
# ---------------------------------------------------------------------------


@router.post(
    "/credential-materialize",
    response_model=MaterializeResponse,
    status_code=201,
    summary="Materialize a file-type credential as a presigned S3 URL (internal)",
    description=(
        "Only valid for file-oriented credential types: ssh_key, certificate, config_file. "
        "Fetches the credential from Secrets Manager, writes it to a short-lived S3 object, "
        "and returns a presigned GET URL the agent can use to write the file to its tmpfs. "
        "Requires X-Agent-Scopes: credential:materialize."
    ),
)
async def credential_materialize(
    body: MaterializeBody,
    x_agent_scopes: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
    sm: SecretsManagerHelper = Depends(get_secrets_manager),
    _: None = Depends(_verify_internal_key),
) -> MaterializeResponse:
    # Scope gate.
    _check_agent_scope(x_agent_scopes, "credential:materialize")

    provenance_id = str(uuid.uuid4())
    settings = get_settings()

    user = await _get_user_context(body.user_id, db)
    cred = await _resolve_credential(
        db=db,
        org_id=user.org_id,
        service=body.service,
        label=body.label,
        user_id=body.user_id,
        team_id=user.team_id,
    )

    # Only file-type credentials are allowed through this path.
    if cred.credential_type not in FILE_CREDENTIAL_TYPES:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_credential_type",
                "message": (
                    f"credential_type={cred.credential_type!r} cannot be materialized. File-type credentials only: {sorted(FILE_CREDENTIAL_TYPES)}"
                ),
            },
        )

    secret_value = await _fetch_secret(cred.secret_arn, sm)

    # Upload to S3 and generate presigned URL.
    bucket = settings.vault_materialization_bucket
    if not bucket:
        raise HTTPException(
            status_code=503,
            detail={
                "error": "not_configured",
                "message": "BG_VAULT_MATERIALIZATION_BUCKET is not configured.",
            },
        )

    s3_key = f"vault/materialize/{provenance_id}/{body.service}"
    expiry_seconds = 300  # 5-minute window
    expires_at = datetime.now(UTC) + timedelta(seconds=expiry_seconds)

    def _upload_and_sign() -> str:
        import boto3

        s3 = boto3.client("s3", region_name=settings.aws_region)
        s3.put_object(
            Bucket=bucket,
            Key=s3_key,
            Body=secret_value.encode("utf-8"),
            ContentType="application/octet-stream",
        )
        return s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket, "Key": s3_key},
            ExpiresIn=expiry_seconds,
        )

    materialize_url = await asyncio.to_thread(_upload_and_sign)

    await _touch_last_used(cred.id, db)
    await _write_audit(
        db,
        event_type="vault_credential_materialized",
        org_id=user.org_id,
        actor_id=body.agent_id,
        details={
            "provenance_id": provenance_id,
            "user_id": body.user_id,
            "agent_id": body.agent_id,
            "task_id": body.task_id,
            "service": body.service,
            "label": body.label,
            "credential_id": cred.id,
            "credential_type": cred.credential_type,
            "s3_key": s3_key,
        },
    )
    await db.commit()

    logger.info(
        "Credential materialized provenance_id=%s service=%s credential_type=%s",
        provenance_id,
        body.service,
        cred.credential_type,
    )
    return MaterializeResponse(
        materialize_url=materialize_url,
        expires_at=expires_at,
        provenance_id=provenance_id,
    )


# ---------------------------------------------------------------------------
# Endpoint: POST /internal/v1/credential-raw-read
# ---------------------------------------------------------------------------


@router.post(
    "/credential-raw-read",
    response_model=RawReadResponse,
    summary="Raw credential read — escape hatch (internal, dual-gated)",
    description=(
        "Returns the raw credential value. "
        "Gated by BG_VAULT_RAW_READ_ENABLED=true (per-deployment feature flag) AND "
        "X-Agent-Scopes header containing 'credential:raw-read'. "
        "Every call is audit-logged regardless of outcome."
    ),
)
async def credential_raw_read(
    body: RawReadBody,
    x_agent_scopes: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
    sm: SecretsManagerHelper = Depends(get_secrets_manager),
    _: None = Depends(_verify_internal_key),
) -> RawReadResponse:
    provenance_id = str(uuid.uuid4())
    settings = get_settings()

    # Org feature flag gate (first — fail fast before touching DB).
    if not settings.vault_raw_read_enabled:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "feature_disabled",
                "message": "credential-raw-read is disabled for this deployment.",
                "provenance_id": provenance_id,
            },
        )

    # Scope gate.
    _check_agent_scope(x_agent_scopes, "credential:raw-read")

    user = await _get_user_context(body.user_id, db)
    cred = await _resolve_credential(
        db=db,
        org_id=user.org_id,
        service=body.service,
        label=body.label,
        user_id=body.user_id,
        team_id=user.team_id,
    )

    secret_value = await _fetch_secret(cred.secret_arn, sm)

    await _touch_last_used(cred.id, db)
    await _write_audit(
        db,
        event_type="vault_credential_raw_read",
        org_id=user.org_id,
        actor_id=body.agent_id,
        details={
            "provenance_id": provenance_id,
            "user_id": body.user_id,
            "agent_id": body.agent_id,
            "task_id": body.task_id,
            "service": body.service,
            "label": body.label,
            "credential_id": cred.id,
            "credential_type": cred.credential_type,
            "purpose": body.purpose,
        },
    )
    await db.commit()

    logger.info(
        "Raw credential read provenance_id=%s service=%s agent=%s",
        provenance_id,
        body.service,
        body.agent_id,
    )
    return RawReadResponse(
        value=secret_value,
        credential_type=cred.credential_type,
        provenance_id=provenance_id,
    )
