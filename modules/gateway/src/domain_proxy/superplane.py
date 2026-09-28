"""Route inventoried user APIs to Superplane; authorization stays in its API.

The domain installer owns one conditional S3 registration. A short cache permits activation
and removal without a Gateway rollout. Missing/malformed registration is off.
Only namespace selection is configurable: no arbitrary upstream URL or headers.
"""

import asyncio
import json
import os
import re
import time
from pathlib import Path

import boto3
import httpx
from botocore.config import Config
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field, ValidationError, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.aws_connect_routes import _resolve_user_id
from src.auth.aws_connection_authority import (
    connection_conflict,
    connection_material,
    owned_aws_connection,
    require_active_connection,
    verified_connection_evidence,
)
from src.auth.middleware import get_current_user_context
from src.auth.org_id_resolver import resolve_effective_org_id
from src.auth.vault_routes import get_secrets_manager
from src.shared.database import get_db
from src.shared.schemas.auth import TokenContext
from src.shared.services.secrets_manager import SecretsManagerHelper

router = APIRouter(prefix="/superplane", tags=["superplane"])
MAX_BODY = 2 * 1024 * 1024
_cache: tuple[float, dict] = (0, {})
ROUTES = tuple(
    (method, re.compile("^" + re.sub(r"\{[^}]+\}", "[^/]+", path) + "$"))
    for method, path in json.loads(Path(__file__).with_name("superplane_routes.json").read_text())
)


def route_bucket() -> str:
    # The installer owns the route object and the scoped gateway read grant.
    # The base gateway only supplies its existing account identity.
    configured = os.environ.get("BG_SUPERPLANE_ROUTE_BUCKET", "")
    if configured:
        return configured
    account_id = os.environ.get("BG_PLATFORM_BEDROCK_ACCOUNT_ID", "")
    return f"adp-terraform-state-{account_id}" if re.fullmatch(r"[0-9]{12}", account_id) else ""


def registration() -> dict:
    global _cache
    now = time.monotonic()
    if now < _cache[0]:
        return _cache[1]
    environment = os.environ.get("BG_ENVIRONMENT", "")
    result = {}
    bucket = route_bucket()
    if re.fullmatch(r"[a-z][a-z0-9-]{0,39}", environment) and re.fullmatch(r"adp-terraform-state-[0-9]{12}", bucket):
        try:
            client = boto3.client("s3", config=Config(connect_timeout=1, read_timeout=1, retries={"max_attempts": 0}))
            response = client.get_object(Bucket=bucket, Key=f"domain-routes/{environment}/superplane/public-route.json")
            body = response["Body"]
            try:
                raw = body.read(4097)
            finally:
                body.close()
            value = json.loads(raw) if len(raw) <= 4096 else {}
            if (
                isinstance(value, dict)
                and value.get("version") == 2
                and value.get("enabled") is True
                and re.fullmatch(r"[0-9a-f]{24}", str(value.get("installation_id", "")))
                and re.fullmatch(r"[0-9a-f]{32}", str(value.get("revision", "")))
                and re.fullmatch(r"[a-z][a-z0-9-]{0,39}", str(value.get("namespace", "")))
                and value["namespace"] not in {"adp", "default", "kube-system", "kube-public"}
                and re.fullmatch(r"[0-9a-f]{64}", str(value.get("release_id", "")))
            ):
                result = value
        except Exception:
            # No remote exception text: it may carry credentials or a URL.
            result = {}
    _cache = (now + 5, result)
    return result


def enabled() -> bool:
    override = os.environ.get("FEATURE_SUPERPLANE_ENABLED")
    if override is not None and override.lower() != "true":
        return False
    return bool(registration())


class AccountRegistrationRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    provider: str = Field(pattern="^aws$")
    account_id: str
    adp_credential_id: str = Field(min_length=1, max_length=255)

    @field_validator("account_id")
    @classmethod
    def validate_account_id(cls, value: str) -> str:
        if len(value) != 12 or not value.isdigit():
            raise ValueError("AWS account IDs must be exactly 12 digits")
        return value

    @field_validator("adp_credential_id")
    @classmethod
    def validate_credential_id(cls, value: str) -> str:
        if "arn:" in value.lower() or "secret" in value.lower():
            raise ValueError("adp_credential_id must be an opaque ADP credential ID")
        return value


class _DomainAccountRegistration(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    provider: str = Field(pattern="^aws$")
    account_id: str = Field(min_length=1, max_length=255)
    role_arn: str = Field(min_length=1, max_length=512)
    external_id: str = Field(min_length=1, max_length=255)
    adp_credential_ids: list[str]


def _redact_account_values(value, sensitive: tuple[str, ...]):
    if isinstance(value, dict):
        return {key: _redact_account_values(item, sensitive) for key, item in value.items() if str(key).lower() not in {"role_arn", "external_id"}}
    if isinstance(value, list):
        return [_redact_account_values(item, sensitive) for item in value]
    if isinstance(value, str):
        for secret in sensitive:
            value = value.replace(secret, "[redacted]")
    return value


def _public_account_response(response: Response, *, role_arn: str, external_id: str) -> Response:
    try:
        payload = json.loads(bytes(response.body))
    except (TypeError, ValueError):
        payload = {"detail": "Superplane returned an invalid account response"}
        status_code = 502
    else:
        payload = _redact_account_values(payload, (role_arn, external_id))
        status_code = response.status_code
    headers = {key: value for key, value in response.headers.items() if key.lower() == "x-superplane-release"}
    return Response(
        json.dumps(payload).encode(),
        status_code=status_code,
        headers=headers,
        media_type="application/json",
    )


@router.get("/installation-support")
async def installation_support():
    # Capability discovery does not expose a domain route or any configuration.
    return {
        "version": 2,
        "transport": "s3-conditional-domain-registration",
        "cache_seconds": 5,
        "features": ["account-vault-reference-v1"],
        "configured": bool(
            re.fullmatch(r"adp-terraform-state-[0-9]{12}", route_bucket())
            and re.fullmatch(r"[a-z][a-z0-9-]{0,39}", os.environ.get("BG_ENVIRONMENT", ""))
        ),
    }


async def _proxy_to_domain(request: Request, native_path: str, *, content: bytes | None = None, before_send=None) -> Response:
    active = await asyncio.to_thread(enabled)
    config = await asyncio.to_thread(registration)
    if not active or not config:
        raise HTTPException(404, "Not found")
    authorization = request.headers.get("authorization", "")
    if not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "ADP bearer token required")
    if content is None:
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > MAX_BODY:
                raise HTTPException(413, "Request too large")
        content = bytes(body)
    target = f"http://superplane-api.{config['namespace']}.svc.cluster.local:8000{native_path}"
    headers = {
        "Authorization": authorization,
        "Content-Type": request.headers.get("content-type", "application/json"),
    }
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=False, trust_env=False) as client:
            if before_send is not None:
                await before_send()
            async with client.stream(
                request.method,
                target,
                params=request.query_params.multi_items(),
                headers=headers,
                content=content,
            ) as upstream:
                payload = bytearray()
                async for chunk in upstream.aiter_bytes():
                    payload.extend(chunk)
                    if len(payload) > MAX_BODY:
                        raise HTTPException(502, "Domain response too large")
                if upstream.status_code in {301, 302, 303, 307, 308}:
                    raise HTTPException(502, "Unexpected domain redirect")
                return Response(
                    bytes(payload),
                    status_code=upstream.status_code,
                    headers={
                        "Content-Type": upstream.headers.get("content-type", "application/json"),
                        "Cache-Control": "no-store",
                        "X-Superplane-Release": config["release_id"],
                    },
                )
    except httpx.HTTPError:
        raise HTTPException(503, "Superplane is unavailable") from None


@router.post("/v1/accounts")
async def register_account(
    body: AccountRegistrationRequest,
    request: Request,
    token_context: TokenContext = Depends(get_current_user_context),
    db: AsyncSession = Depends(get_db),
    secrets: SecretsManagerHelper = Depends(get_secrets_manager),
) -> Response:
    if token_context.account_type != "human":
        raise HTTPException(
            403,
            detail={
                "error": "human_user_required",
                "message": "A human user is required",
            },
        )
    db_user_id = await _resolve_user_id(
        token_context.user_id,
        db,
        org_id=token_context.org_id,
        username=token_context.cognito_username,
    )
    org_id = await resolve_effective_org_id(token_context, db)
    credential = await owned_aws_connection(db, body.adp_credential_id, db_user_id, org_id)
    evidence = verified_connection_evidence(credential)
    scopes = credential.scopes or {}
    secret_arn = credential.secret_arn
    version = evidence[1]
    try:
        if await asyncio.to_thread(secrets.current_version_id, secret_arn) != version:
            raise connection_conflict()
        material, served_version = await asyncio.to_thread(secrets.get_secret_at_version, secret_arn, version)
        if served_version != version or await asyncio.to_thread(secrets.current_version_id, secret_arn) != version:
            raise connection_conflict()
        secret = json.loads(material)
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(
            503,
            detail={
                "error": "connection_unavailable",
                "message": "The AWS connection could not be resolved. Retry later.",
            },
        ) from None
    role_arn = secret.get("role_arn") if isinstance(secret, dict) else None
    external_id = secret.get("external_id") if isinstance(secret, dict) else None
    stored_account_id = secret.get("account_id") if isinstance(secret, dict) else None
    try:
        connection_material(secret, scopes)
    except HTTPException:
        raise HTTPException(
            409,
            detail={"error": "connection_metadata_mismatch", "message": "The verified AWS role does not match this account."},
        ) from None
    if (
        stored_account_id != body.account_id
        or scopes.get("account_id") != body.account_id
        or not isinstance(role_arn, str)
        or not role_arn
        or role_arn != scopes.get("role_arn")
        or not isinstance(external_id, str)
        or not external_id
    ):
        raise HTTPException(
            409,
            detail={
                "error": "connection_metadata_mismatch",
                "message": "The verified AWS connection does not match this account or lacks required trust metadata.",
            },
        )
    try:
        domain_body = _DomainAccountRegistration(
            name=body.name,
            provider="aws",
            account_id=body.account_id,
            role_arn=role_arn,
            external_id=external_id,
            adp_credential_ids=[str(credential.id)],
        ).model_dump(mode="json")
    except ValidationError:
        raise HTTPException(
            409,
            detail={
                "error": "connection_metadata_mismatch",
                "message": "The verified AWS connection contains trust metadata that Superplane cannot accept.",
            },
        ) from None
    # Refresh after all secret I/O. Serialize this admission against verification,
    # PATCH and DELETE until the domain has accepted the registration.
    try:
        current = await owned_aws_connection(db, body.adp_credential_id, db_user_id, org_id, lock=True)
        if verified_connection_evidence(current) != evidence:
            raise connection_conflict()

        async def before_send():
            # Route discovery also performs I/O. Recheck the version and clock at
            # the actual forward boundary while the metadata lock remains held.
            try:
                current_version = await asyncio.to_thread(secrets.current_version_id, secret_arn)
            except Exception:
                current_version = None
            if current_version != version:
                raise connection_conflict()
            require_active_connection(current)

        response = await _proxy_to_domain(request, "/accounts", content=json.dumps(domain_body).encode(), before_send=before_send)
        return _public_account_response(response, role_arn=role_arn, external_id=external_id)
    finally:
        await db.rollback()


@router.api_route(
    "/v1/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
)
async def forward(path: str, request: Request):
    raw_path = request.scope.get("raw_path", b"")
    native_path = "/" + path
    if (
        b"%" in raw_path
        or "\\" in path
        or any(part in {".", "..", ""} for part in path.split("/"))
        or not any(method == request.method and pattern.fullmatch(native_path) for method, pattern in ROUTES)
    ):
        raise HTTPException(404, "Not found")
    return await _proxy_to_domain(request, native_path)
