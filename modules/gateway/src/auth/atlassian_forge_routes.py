"""Admin pairing and Forge token delivery; vendor verification stays in the adapter."""

import asyncio
import base64
import hashlib
import json
import os
import re
import secrets
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import boto3
from botocore.config import Config
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.atlassian_system_routes import SERVICE, SecretSafeRoute, adapter_read, audit, cleanup_keys, connection_view, enabled, provision_key
from src.auth.dependencies import require_admin
from src.products.models import Product
from src.shared.config import get_settings
from src.shared.database import get_db
from src.shared.models.atlassian_system import AtlassianSystemConnection, AtlassianSystemKey
from src.shared.models.base import new_uuid, utcnow
from src.shared.models.vault import UserCredential


class ForgeRoute(SecretSafeRoute):
    def get_route_handler(self):
        original = super().get_route_handler()

        async def handle(request):
            data = bytearray()
            async for chunk in request.stream():
                if len(data) + len(chunk) > 65536:
                    raise HTTPException(413, detail={"error": "forge_request_too_large"})
                data.extend(chunk)
            request._body = bytes(data)
            return await original(request)

        return handle


router = APIRouter(prefix="/auth/connections/atlassian-mcp/forge", tags=["connections"], route_class=ForgeRoute)


def forge_configuration():
    app = os.environ.get("ADP_ATLASSIAN_FORGE_APP_ID", "")
    environment = os.environ.get("ADP_ATLASSIAN_FORGE_ENVIRONMENT_ID", "")
    install_url = os.environ.get("ADP_ATLASSIAN_FORGE_INSTALL_URL", "")
    if not (
        re.fullmatch(r"ari:cloud:ecosystem::app/[a-f0-9-]{36}", app)
        and re.fullmatch(r"[a-f0-9-]{36}", environment)
        and re.fullmatch(r"https://developer\.atlassian\.com/console/install/[A-Za-z0-9/_-]+", install_url)
    ):
        return None
    return {"app_id": app, "environment_id": environment, "install_url": install_url}


def admin(actor):
    enabled()
    if actor.account_type != "human" or not actor.is_admin:
        raise HTTPException(403, detail={"error": "admin_required"})


async def products(db, actor, ids):
    if not ids or len(ids) != len(set(ids)):
        raise HTTPException(422, detail={"error": "invalid_product_scope"})
    found = set(
        (await db.scalars(select(Product.id).where(Product.tenant_id == actor.org_id, Product.id.in_(ids), Product.archived_at.is_(None)))).all()
    )
    if found != set(ids):
        raise HTTPException(404, detail={"error": "product_not_found"})


class StartPairing(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str = Field(min_length=1, max_length=200)
    product_ids: list[str] = Field(min_length=1, max_length=100)
    connection_id: str | None = Field(default=None, max_length=36)
    expected_revision: str | None = Field(default=None, max_length=36)


class PairingCode(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    pairing_code: SecretStr = Field(min_length=32, max_length=128)


class AccessUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str = Field(min_length=1, max_length=200)
    product_ids: list[str] = Field(min_length=1, max_length=100)
    expected_revision: str = Field(min_length=1, max_length=36)


@router.post("/pairings")
async def start_pairing(body: StartPairing, actor=Depends(require_admin), db: AsyncSession = Depends(get_db)):
    admin(actor)
    configuration = forge_configuration()
    if configuration is None:
        raise HTTPException(503, detail={"error": "forge_not_configured"})
    await products(db, actor, body.product_ids)
    row = await db.get(AtlassianSystemConnection, body.connection_id, with_for_update=True) if body.connection_id else None
    if body.connection_id and (row is None or row.org_id != actor.org_id or row.connection_method != "forge_app"):
        raise HTTPException(404, detail={"error": "connection_not_found"})
    if (row.revision if row else None) != body.expected_revision:
        raise HTTPException(409, detail={"error": "connection_changed"})
    if row and row.status == "ready":
        raise HTTPException(409, detail={"error": "revoke_before_repairing"})
    revision = new_uuid()
    workload = get_settings().oauth_workload_name.removesuffix("-oauth-user") + "-atlassian-mcp-system"
    provider = workload + "-" + hashlib.sha256(actor.org_id.encode()).hexdigest()[:12] + "-" + revision
    if len(provider) > 128:
        raise HTTPException(503, detail={"error": "jira_system_configuration_invalid"})
    code = secrets.token_urlsafe(32)
    expires = utcnow() + timedelta(minutes=15)
    if row is None:
        credential = UserCredential(
            id=new_uuid(),
            org_id=actor.org_id,
            service=SERVICE,
            provider="agentcore-identity",
            credential_type="api_key",
            label=body.label,
            secret_arn="agentcore-identity:api-key:" + provider,
            strict=True,
            agent_consent=False,
            expires_at=expires,
            scopes={},
        )
        db.add(credential)
        await db.flush()
        row = AtlassianSystemConnection(
            credential_id=credential.id, org_id=actor.org_id, site_id="00000000-0000-0000-0000-000000000000", created_by=actor.user_id, schemas={}
        )
        db.add(row)
    else:
        credential = await db.get(UserCredential, row.credential_id)
    row.connection_method, row.status, row.revision = "forge_app", "unverified", revision
    row.workload_name, row.provider_name = workload, provider
    row.forge_app_id, row.forge_environment_id = configuration["app_id"], configuration["environment_id"]
    row.allowed_product_ids = body.product_ids
    row.pairing_hash, row.pairing_expires_at = hashlib.sha256(code.encode()).hexdigest(), expires
    row.forge_token_hash, row.verified_at = None, None
    credential.label, credential.secret_arn, credential.expires_at = body.label, "agentcore-identity:api-key:" + provider, expires
    credential.agent_consent = False
    db.add(AtlassianSystemKey(org_id=actor.org_id, credential_id=row.credential_id, provider_name=provider))
    audit(db, actor, "jira_forge_pairing_started", row.credential_id)
    await db.commit()
    return {"connection": connection_view(credential, row), "pairing_code": code, "expires_at": expires, "install_url": configuration["install_url"]}


@router.put("/{connection_id}/access")
async def update_access(connection_id: str, body: AccessUpdate, actor=Depends(require_admin), db: AsyncSession = Depends(get_db)):
    admin(actor)
    await products(db, actor, body.product_ids)
    row = await db.get(AtlassianSystemConnection, connection_id, with_for_update=True)
    if row is None or row.org_id != actor.org_id:
        raise HTTPException(404, detail={"error": "connection_not_found"})
    if row.revision != body.expected_revision:
        raise HTTPException(409, detail={"error": "connection_changed"})
    credential = await db.get(UserCredential, connection_id)
    row.allowed_product_ids, row.revision, credential.label = body.product_ids, new_uuid(), body.label
    audit(db, actor, "jira_system_access_changed", connection_id, product_ids=body.product_ids)
    await db.commit()
    return connection_view(credential, row)


def delivered_headers(request):
    auth, token = request.headers.getlist("authorization"), request.headers.getlist("x-forge-oauth-system")
    if len(auth) != 1 or len(token) != 1 or not auth[0].startswith("Bearer ") or not 0 < len(auth[0]) <= 16400 or not 0 < len(token[0]) <= 32768:
        raise HTTPException(403, detail={"error": "forge_delivery_refused"})
    fit = auth[0][7:]
    try:
        # Lookup hints only. The adapter authenticates the complete FIT against
        # the pinned app/environment before any token storage or activation.
        payload = fit.split(".")[1]
        untrusted = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        app = untrusted["app"]
        installation = app["installationId"]
        site = app["apiBaseUrl"].rsplit("/", 1)[1]
        if (
            not isinstance(installation, str)
            or not re.fullmatch(r"ari:cloud:ecosystem::installation/[a-f0-9-]{36}", installation)
            or not re.fullmatch(r"[a-f0-9-]{36}", site)
        ):
            raise ValueError("invalid_hints")
        return fit, token[0], installation, site
    except Exception:
        raise HTTPException(403, detail={"error": "forge_delivery_refused"}) from None


def aware(value):
    return value.replace(tzinfo=UTC) if value and value.tzinfo is None else value


async def update_token(provider, token):
    client = boto3.client("bedrock-agentcore-control", config=Config(connect_timeout=2, read_timeout=5, retries={"total_max_attempts": 2}))
    await asyncio.to_thread(client.update_api_key_credential_provider, name=provider, apiKey=token)


async def accept_delivery(request, db, pairing_code=None):
    enabled()
    configuration = forge_configuration()
    if configuration is None:
        raise HTTPException(503, detail={"error": "forge_not_configured"})
    fit, token, installation, site = delivered_headers(request)
    query = select(AtlassianSystemConnection).where(
        AtlassianSystemConnection.connection_method == "forge_app",
        AtlassianSystemConnection.forge_app_id == configuration["app_id"],
        AtlassianSystemConnection.forge_environment_id == configuration["environment_id"],
    )
    if pairing_code is not None:
        query = query.where(AtlassianSystemConnection.pairing_hash == hashlib.sha256(pairing_code.encode()).hexdigest())
    else:
        query = query.where(AtlassianSystemConnection.forge_installation_id == installation)
    row = await db.scalar(query.with_for_update().execution_options(populate_existing=True))
    if row is None or row.status == "revoked":
        raise HTTPException(403, detail={"error": "forge_delivery_refused"})
    pairing = pairing_code is not None
    if pairing and (row.status != "unverified" or not row.pairing_expires_at or aware(row.pairing_expires_at) <= utcnow()):
        raise HTTPException(403, detail={"error": "forge_pairing_expired"})
    if pairing and row.forge_installation_id and row.site_id != site:
        # Existing Product mappings refer to this site. Reinstalling the app may
        # replace its installation ID, but must not silently retarget sources.
        raise HTTPException(409, detail={"error": "create_a_connection_for_another_site"})
    if not pairing and (row.status != "ready" or row.site_id != site):
        raise HTTPException(403, detail={"error": "forge_delivery_refused"})
    expected = SimpleNamespace(**{column.name: getattr(row, column.name) for column in row.__table__.columns})
    expected.site_id = site
    if pairing:
        expected.forge_installation_id = None
    observed = await adapter_read(
        expected, "verify", forge_delivery={"forge_action": "pair" if pairing else "refresh", "forge_invocation": fit, "forge_token": token}
    )
    if (
        observed.get("site_id") != site
        or observed.get("installation_id") != installation
        or not observed.get("account_id")
        or not isinstance(observed.get("schemas"), dict)
    ):
        raise HTTPException(403, detail={"error": "forge_delivery_refused"})
    if not pairing and observed["account_id"] != row.provider_account_id:
        raise HTTPException(403, detail={"error": "forge_identity_changed"})
    expiry = datetime.fromtimestamp(observed["expires_at"], UTC)
    if not utcnow() + timedelta(seconds=30) < expiry <= utcnow() + timedelta(hours=4, minutes=1):
        raise HTTPException(403, detail={"error": "forge_token_expired"})
    credential = await db.get(UserCredential, row.credential_id)
    if not pairing and aware(credential.expires_at) >= expiry:
        # Cached or delayed deliveries cannot roll back the active credential.
        return {"status": "ready"}
    try:
        if pairing:
            try:
                await provision_key(row.workload_name, row.provider_name, token)
            except Exception as exc:
                if getattr(exc, "response", {}).get("Error", {}).get("Code") != "ConflictException":
                    raise
                # Recover a verified delivery interrupted after provider creation.
                await update_token(row.provider_name, token)
        else:
            await update_token(row.provider_name, token)
        row.site_id, row.forge_installation_id = site, installation
        row.provider_account_id, row.schemas = observed["account_id"], observed["schemas"]
        row.status, row.verified_at, row.forge_token_hash = "ready", utcnow(), hashlib.sha256(token.encode()).hexdigest()
        row.pairing_hash, row.pairing_expires_at = None, None
        credential.expires_at, credential.agent_consent = expiry, True
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, detail={"error": "forge_installation_already_connected"}) from None
    except Exception:
        await db.rollback()
        raise HTTPException(503, detail={"error": "forge_token_storage_unavailable"}) from None
    if pairing:
        await cleanup_keys(db, row.credential_id, row.org_id)
    return {"status": "ready"}


@router.post("/pair")
async def pair_installation(body: PairingCode, request: Request, db: AsyncSession = Depends(get_db)):
    return await accept_delivery(request, db, body.pairing_code.get_secret_value())


@router.post("/refresh")
async def refresh_installation(request: Request, db: AsyncSession = Depends(get_db)):
    return await accept_delivery(request, db)
