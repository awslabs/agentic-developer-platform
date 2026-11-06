"""Admin-managed Atlassian service accounts stored through AgentCore Identity."""

import asyncio
import hashlib
import time
from datetime import UTC, timedelta

import boto3
import httpx
from botocore.config import Config
from fastapi import APIRouter, Depends, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.dependencies import get_current_user, require_admin
from src.internal.tool_adapter_observation import canonical, observation_settings, signed_observation_request
from src.products.models import Product
from src.shared.config import get_settings
from src.shared.database import get_db
from src.shared.models.atlassian_system import AtlassianSystemConnection, AtlassianSystemKey
from src.shared.models.audit import AuditLog
from src.shared.models.base import new_uuid, utcnow
from src.shared.models.vault import UserCredential

SERVICE = "atlassian-mcp-system"


class SecretSafeRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()

        async def handle(request):
            try:
                return await original(request)
            except RequestValidationError:
                # FastAPI's default validation response includes rejected inputs.
                raise HTTPException(422, detail={"error": "invalid_system_connection"}) from None

        return handle


router = APIRouter(prefix="/auth/connections/atlassian-mcp/system", tags=["connections"], route_class=SecretSafeRoute)


def enabled():
    import os

    if os.environ.get("ADP_TOOLS_ATLASSIAN_SYSTEM_ENABLED") != "true":
        raise HTTPException(503, detail={"error": "jira_system_not_enabled"})


class ConnectSystem(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    label: str = Field(min_length=1, max_length=200)
    api_key: SecretStr = Field(min_length=1, max_length=65536)
    site_id: str = Field(pattern=r"^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$")
    expires_at: AwareDatetime
    product_ids: list[str] = Field(min_length=1, max_length=100)
    expected_revision: str | None = None


def connection_view(credential, connection):
    return {
        "id": credential.id,
        "revision": connection.revision,
        "label": credential.label,
        "site_id": connection.site_id,
        "account_id": connection.provider_account_id,
        "product_ids": connection.allowed_product_ids,
        "status": connection.status,
        "expires_at": credential.expires_at,
        "verified_at": connection.verified_at,
        "capabilities": sorted(connection.schemas),
        "connection_method": connection.connection_method or "service_account",
        "installation_id": connection.forge_installation_id,
    }


async def adapter_read(connection, operation, arguments=None, *, timeout=20, deadline_at_ms=None, forge_delivery=None):
    """Call the existing private adapter transport with an explicit system identity."""
    url, key, role, release = observation_settings()
    deadline = min(int(time.time() * 1000) + timeout * 1000, deadline_at_ms or float("inf"))
    payload = {
        "schema_version": "1.0",
        "purpose": "jira_system",
        "connection_id": connection.credential_id,
        "connection_revision": connection.revision,
        "tenant_id": connection.org_id,
        "site_id": connection.site_id,
        "workload": connection.workload_name,
        "provider": connection.provider_name,
        "connection_method": connection.connection_method or "service_account",
        "forge_app_id": connection.forge_app_id,
        "forge_environment_id": connection.forge_environment_id,
        "forge_installation_id": connection.forge_installation_id,
        "forge_action": None,
        "forge_invocation": None,
        "forge_token": None,
        "operation": operation,
        "arguments": arguments or {},
        "schemas": connection.schemas or {},
        "release": release,
        "deadline_at_ms": deadline,
    }
    if forge_delivery is not None:
        payload.update(forge_delivery)
    claims = {
        "audience": "tool-adapters:jira-system",
        "caller_role_arn": role,
        "request_sha256": hashlib.sha256(canonical(payload)).hexdigest(),
    }
    try:
        request = await asyncio.to_thread(signed_observation_request, url=url, key=key, claims=claims, payload=payload, deadline_at_ms=deadline)
        remaining = (deadline - int(time.time() * 1000)) / 1000
        if remaining <= 0:
            raise TimeoutError()
        async with httpx.AsyncClient(timeout=remaining, follow_redirects=False, trust_env=False) as client:
            response = await client.post(url, content=request.body, headers=dict(request.headers))
        if response.status_code != 200:
            status = response.status_code if response.status_code in {403, 404, 422, 429} else 503
            raise HTTPException(status, detail={"error": "jira_system_read_failed"})
        if len(response.content) > 4 * 1024 * 1024:
            raise ValueError("oversized response")
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("invalid response")
        return result
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(503, detail={"error": "jira_system_unavailable"}) from None


async def provision_key(workload, provider, key):
    control = boto3.client("bedrock-agentcore-control", config=Config(connect_timeout=2, read_timeout=5, retries={"total_max_attempts": 2}))
    try:
        await asyncio.to_thread(control.get_workload_identity, name=workload)
    except control.exceptions.ResourceNotFoundException:
        try:
            await asyncio.to_thread(control.create_workload_identity, name=workload)
        except control.exceptions.ConflictException:
            await asyncio.to_thread(control.get_workload_identity, name=workload)
    await asyncio.to_thread(control.create_api_key_credential_provider, name=provider, apiKey=key)


async def remove_key(provider):
    control = boto3.client("bedrock-agentcore-control", config=Config(connect_timeout=2, read_timeout=5, retries={"total_max_attempts": 2}))
    try:
        await asyncio.to_thread(control.delete_api_key_credential_provider, name=provider)
    except control.exceptions.ResourceNotFoundException:
        pass


async def current_system(db, credential_id, tenant_id, product_id=None, *, lock=False, write=False):
    query = (
        select(AtlassianSystemConnection)
        .where(
            AtlassianSystemConnection.credential_id == credential_id,
            AtlassianSystemConnection.org_id == tenant_id,
        )
        .execution_options(populate_existing=True)
    )
    row = await db.scalar(query.with_for_update(read=not write) if lock else query)
    credential = await db.get(UserCredential, credential_id, populate_existing=True) if row else None
    expiry = credential.expires_at if credential else None
    if expiry is not None and expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=UTC)
    if (
        row is None
        or row.status != "ready"
        or row.verified_at is None
        or credential is None
        or credential.org_id != tenant_id
        or credential.owner_scope != "org"
        or credential.service != SERVICE
        or credential.provider != "agentcore-identity"
        or credential.credential_type != "api_key"
        or credential.secret_arn != "agentcore-identity:api-key:" + row.provider_name
        or credential.strict is not True
        or credential.agent_consent is not True
        or expiry is None
        or expiry <= utcnow()
        or product_id is not None
        and product_id not in row.allowed_product_ids
    ):
        raise HTTPException(404, detail={"error": "jira_system_connection_unavailable"})
    return credential, row


@router.get("")
async def list_system_connections(actor=Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    enabled()
    if actor.account_type != "human":
        raise HTTPException(403, detail={"error": "human_required"})
    if not actor.is_admin:
        raise HTTPException(403, detail={"error": "admin_required"})
    rows = (
        await db.execute(
            select(UserCredential, AtlassianSystemConnection)
            .join(
                AtlassianSystemConnection,
                AtlassianSystemConnection.credential_id == UserCredential.id,
            )
            .where(UserCredential.org_id == actor.org_id, AtlassianSystemConnection.org_id == actor.org_id)
        )
    ).all()
    # Only administrators browse the shared credential registry. Product members
    # discover their assigned connections through the Product source endpoints.
    if not actor.is_admin:
        raise HTTPException(403, detail={"error": "admin_required"})
    from src.auth.atlassian_forge_routes import forge_configuration

    return {"connections": [connection_view(*row) for row in rows], "forge_available": forge_configuration() is not None}


def audit(db, actor, event, credential_id, **details):
    db.add(AuditLog(org_id=actor.org_id, actor_id=actor.user_id, event_type=event, details={"connection_id": credential_id, **details}))


async def cleanup_keys(db, credential_id, tenant_id, *, providers=None):
    # Only touch journaled providers, and never delete the active provider.
    row = await db.scalar(
        select(AtlassianSystemConnection)
        .where(
            AtlassianSystemConnection.credential_id == credential_id,
            AtlassianSystemConnection.org_id == tenant_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    keys = (
        await db.scalars(
            select(AtlassianSystemKey).where(
                AtlassianSystemKey.credential_id == credential_id,
                AtlassianSystemKey.org_id == tenant_id,
            )
        )
    ).all()
    pending = False
    for key in keys:
        if providers is not None and key.provider_name not in providers:
            continue
        if row.status == "ready" and key.provider_name == row.provider_name:
            continue
        try:
            await remove_key(key.provider_name)
        except Exception:
            pending = True
        # Retain the journal even after deletion: a concurrent provisioning
        # call may finish after a revocation. Subsequent cleanup remains safe.
    await db.commit()
    return pending


async def save_connection(body, actor, db, credential_id=None):
    enabled()
    if actor.account_type != "human" or not actor.is_admin:
        raise HTTPException(403, detail={"error": "admin_required"})
    if not utcnow() < body.expires_at <= utcnow() + timedelta(days=365) or len(body.product_ids) != len(set(body.product_ids)):
        raise HTTPException(422, detail={"error": "invalid_connection_scope"})
    allowed = set(
        (
            await db.scalars(
                select(Product.id).where(
                    Product.tenant_id == actor.org_id,
                    Product.id.in_(body.product_ids),
                    Product.archived_at.is_(None),
                )
            )
        ).all()
    )
    if allowed != set(body.product_ids):
        raise HTTPException(404, detail={"error": "product_not_found"})
    existing = await db.get(AtlassianSystemConnection, credential_id, with_for_update=True) if credential_id else None
    if credential_id and (existing is None or existing.org_id != actor.org_id):
        raise HTTPException(404, detail={"error": "connection_not_found"})
    if existing and existing.connection_method == "forge_app":
        raise HTTPException(409, detail={"error": "use_forge_connection_setup"})
    if (existing.revision if existing else None) != body.expected_revision:
        raise HTTPException(409, detail={"error": "connection_changed"})
    credential_id = credential_id or new_uuid()
    revision = new_uuid()
    workload = get_settings().oauth_workload_name.removesuffix("-oauth-user") + "-atlassian-mcp-system"
    provider = workload + "-" + hashlib.sha256(actor.org_id.encode()).hexdigest()[:12] + "-" + revision
    if len(provider) > 128:
        raise HTTPException(503, detail={"error": "jira_system_configuration_invalid"})
    candidate = AtlassianSystemConnection(
        credential_id=credential_id,
        org_id=actor.org_id,
        revision=revision,
        site_id=body.site_id,
        workload_name=workload,
        provider_name=provider,
        schemas={},
        allowed_product_ids=body.product_ids,
        created_by=actor.user_id,
        status="unverified",
    )
    if existing is None:
        db.add(
            UserCredential(
                id=credential_id,
                org_id=actor.org_id,
                service=SERVICE,
                provider="agentcore-identity",
                credential_type="api_key",
                label=body.label,
                secret_arn="agentcore-identity:api-key:" + provider,
                strict=True,
                agent_consent=False,
                expires_at=body.expires_at,
                scopes={},
            )
        )
        await db.flush()
        db.add(candidate)
    # Journal before touching Identity. An interrupted setup remains visible and
    # revocable in Connections, with no secret stored in this database.
    db.add(AtlassianSystemKey(org_id=actor.org_id, credential_id=credential_id, provider_name=provider))
    audit(db, actor, "jira_system_setup_started", credential_id)
    expected_revision = existing.revision if existing else revision
    await db.commit()
    try:
        await provision_key(workload, provider, body.api_key.get_secret_value())
        observed = await adapter_read(candidate, "verify")
        if not isinstance(observed.get("account_id"), str) or not observed["account_id"] or not isinstance(observed.get("schemas"), dict):
            raise ValueError("provider verification failed")
        current = await db.scalar(
            select(AtlassianSystemConnection)
            .where(
                AtlassianSystemConnection.credential_id == credential_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if current is None or current.revision != expected_revision:
            raise HTTPException(409, detail={"error": "connection_changed"})
        # An existing source must never silently switch to a different Jira site.
        if existing and current.site_id != body.site_id:
            raise HTTPException(409, detail={"error": "create_a_connection_for_another_site"})
        credential = await db.get(UserCredential, credential_id, populate_existing=True)
        old_provider = current.provider_name
        current.revision, current.provider_name = revision, provider
        current.schemas, current.provider_account_id = observed["schemas"], observed["account_id"]
        current.allowed_product_ids = body.product_ids
        current.status, current.verified_at = "ready", utcnow()
        credential.label, credential.expires_at = body.label, body.expires_at
        credential.secret_arn = "agentcore-identity:api-key:" + provider
        credential.strict, credential.agent_consent = True, True
        audit(db, actor, "jira_system_connected", credential_id, revision=revision, product_ids=body.product_ids)
        await db.commit()
        view = connection_view(credential, current)
    except Exception as error:
        await db.rollback()
        await cleanup_keys(db, credential_id, actor.org_id, providers={provider})
        if isinstance(error, HTTPException):
            raise
        raise HTTPException(503, detail={"error": "jira_system_setup_failed", "connection_id": credential_id}) from None
    if old_provider != provider:
        view["provider_cleanup_pending"] = await cleanup_keys(db, credential_id, actor.org_id, providers={old_provider})
    return view


@router.post("")
async def create_system_connection(body: ConnectSystem, actor=Depends(require_admin), db: AsyncSession = Depends(get_db)):
    return await save_connection(body, actor, db)


@router.put("/{credential_id}")
async def rotate_system_connection(credential_id: str, body: ConnectSystem, actor=Depends(require_admin), db: AsyncSession = Depends(get_db)):
    return await save_connection(body, actor, db, credential_id)


@router.post("/{credential_id}/verify")
async def verify_system_connection(credential_id: str, actor=Depends(require_admin), db: AsyncSession = Depends(get_db)):
    enabled()
    if actor.account_type != "human" or not actor.is_admin:
        raise HTTPException(403, detail={"error": "admin_required"})
    credential, connection = await current_system(db, credential_id, actor.org_id, lock=True, write=True)
    observed = await adapter_read(connection, "verify")
    if observed.get("account_id") != connection.provider_account_id or not isinstance(observed.get("schemas"), dict):
        raise HTTPException(409, detail={"error": "jira_system_identity_changed"})
    connection.schemas, connection.revision, connection.verified_at = observed["schemas"], new_uuid(), utcnow()
    audit(db, actor, "jira_system_verified", credential_id, revision=connection.revision)
    await db.commit()
    return connection_view(credential, connection)


@router.delete("/{credential_id}")
async def disconnect_system_connection(credential_id: str, actor=Depends(require_admin), db: AsyncSession = Depends(get_db)):
    enabled()
    if actor.account_type != "human" or not actor.is_admin:
        raise HTTPException(403, detail={"error": "admin_required"})
    row = await db.get(AtlassianSystemConnection, credential_id, with_for_update=True)
    if row is None or row.org_id != actor.org_id:
        raise HTTPException(404, detail={"error": "connection_not_found"})
    credential = await db.get(UserCredential, credential_id)
    row.status, row.revision = "revoked", new_uuid()
    row.pairing_hash, row.pairing_expires_at = None, None
    credential.agent_consent = False
    await db.commit()
    audit(db, actor, "jira_system_disconnected", credential_id)
    await db.commit()
    pending = await cleanup_keys(db, credential_id, actor.org_id)
    return {"status": "revoked", "provider_cleanup_pending": pending}
