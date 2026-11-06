"""Internal credential delivery endpoints.

Issue #136: Vault Phase 3 — service-to-service endpoints + delivery paths (/internal/v1/*)

Endpoints (IAM-signed / shared-secret; internal only):
    GET  /internal/v1/user-credentials      — list credential metadata for a user+service
    POST /internal/v1/proxy-request         — HTTP proxy: inject credential, make request
    POST /internal/v1/credential-materialize — file-type credentials: return presigned URL
    POST /internal/v1/credential-raw-read   — escape hatch: return raw value (dual-gated)

Authentication:
    The canonical verify_internal_or_irsa dependency accepts edge-verified IAM
    identity with internal/platform scope, or the legacy shared key when no IAM
    identity is asserted. Protected worker brokers additionally require their
    run-bound identity. See src/internal/auth_deps.py.

Scope gating (Issue #6050):
    materialize   — requires registry-granted credential_scopes to contain
                    "credential:materialize" (verified via token_context)
    raw-read      — requires registry-granted credential_scopes to contain
                    "credential:raw-read" AND BG_VAULT_RAW_READ_ENABLED=true
                    (org-level feature flag)

Every credential access (proxy, materialize, raw-read) writes an audit_log entry
and updates UserCredential.last_used_at.
"""

from __future__ import annotations

import asyncio
import codecs
import ipaddress
import logging
import socket
import time
import uuid
from datetime import UTC, datetime

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.agentauth.broker_identity import user_credential_audit, verify_selected_user_credential, worker_tenant
from src.internal.auth_deps import verify_internal_or_irsa
from src.internal.credential_authorization import require_credential_capability
from src.internal.credential_binding import broker_actor, resolve_broker_binding
from src.internal.credential_egress import allowed_hosts_for, host_matches, is_binding_enforced
from src.internal.credential_injector import FILE_CREDENTIAL_TYPES, inject_credential, sanitize_proxy_response
from src.internal.tools_credential_auth import assert_tools_credential_fresh
from src.shared.config import Settings, get_settings
from src.shared.database import get_db
from src.shared.identity.oauth_providers import PROVIDER_SCOPES, approved_oauth_scopes
from src.shared.models.audit import AuditLog
from src.shared.models.organization import User
from src.shared.models.vault import UserCredential
from src.shared.services.canonical_user import resolve_canonical_user
from src.shared.services.credential_resolver import CredentialNotFoundError, CredentialResolver
from src.shared.services.github_pat_migration import github_pat_retired
from src.shared.services.secrets_manager import SecretsManagerHelper

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/internal/v1", tags=["internal-credentials"])

# Module-level SM helper; replaced in tests via dependency injection.
_sm_helper = SecretsManagerHelper()


def get_identity_client():
    import boto3

    return boto3.client("bedrock-agentcore", region_name=get_settings().aws_region)


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

    user_id: str | None = None
    agent_id: str | None = None
    task_id: str | None = None
    service: str
    label: str | None = None
    method: str
    url: str
    headers: dict[str, str] | None = None
    body: str | None = None
    invocation_id: str | None = None


class ProxyResponse(BaseModel):
    status: int
    headers: dict[str, str]
    body: str
    provenance_id: str


class MaterializeBody(BaseModel):
    """Body for POST /internal/v1/credential-materialize."""

    user_id: str | None = None
    agent_id: str | None = None
    task_id: str | None = None
    service: str
    label: str | None = None
    invocation_id: str | None = None


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
    invocation_id: str | None = None


class RawReadResponse(BaseModel):
    value: str
    credential_type: str
    provenance_id: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _get_user_context(user_id: str, db: AsyncSession, *, calling_endpoint: str = "unknown", expected_org: str | None = None) -> User:
    """Fetch the canonical User row or raise 404.

    Issue #700: uses canonical user resolution to ensure credential queries
    use the correct user_id and org_id even when the inbound row has drifted.
    """
    user = await resolve_canonical_user(db, user_id, calling_endpoint=calling_endpoint)
    if user is not None and expected_org is not None and user.org_id != expected_org:
        raise HTTPException(404, "not found")
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


async def _identity_access_token(cred: UserCredential, user: User, client) -> str:
    """Exchange the owned pointer for a user token after broker authority and egress checks."""
    provider = cred.secret_arn.rsplit(":", 1)[-1]
    expected = PROVIDER_SCOPES.get(provider)
    from src.internal.github_oauth import is_github_app_grant

    app_grant = provider == "github" and is_github_app_grant(cred.scopes)
    scopes = [] if app_grant else approved_oauth_scopes(provider, cred.scopes)
    settings = get_settings()
    workload_name = settings.oauth_workload_name
    if (
        expected is None
        or cred.user_id != user.id
        or cred.org_id != user.org_id
        or cred.service != expected[0]
        or cred.credential_type != "oauth_token"
        or cred.agent_consent is not True
        or cred.expires_at is not None
        and cred.expires_at.replace(tzinfo=UTC) <= datetime.now(UTC)
        or scopes is None
        or not workload_name.endswith("-oauth-user")
        or cred.secret_arn != f"agentcore-identity:{workload_name}:{provider}"
    ):
        raise HTTPException(status_code=403, detail={"error": "not_connected"})
    scope_snapshot = tuple(scopes)
    try:
        workload = await asyncio.to_thread(
            client.get_workload_access_token_for_user_id,
            workloadName=workload_name,
            userId=user.id,
        )
        result = await asyncio.to_thread(
            client.get_resource_oauth2_token,
            workloadIdentityToken=workload["workloadAccessToken"],
            resourceCredentialProviderName=f"{workload_name.removesuffix('-oauth-user')}-{provider}-user",
            scopes=list(scope_snapshot),
            oauth2Flow="USER_FEDERATION",
        )
    except Exception:
        raise HTTPException(status_code=502, detail={"error": "identity_unavailable"}) from None
    if result.get("sessionStatus") == "FAILED":
        raise HTTPException(status_code=502, detail={"error": "identity_unavailable"})
    if result.get("sessionStatus") == "IN_PROGRESS":
        raise HTTPException(status_code=403, detail={"error": "not_connected"})
    token = result.get("accessToken")
    if not isinstance(token, str) or not token or len(token) > 16384 or any(char.isspace() for char in token):
        raise HTTPException(status_code=403, detail={"error": "not_connected"})
    return token


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


# Issue #6050: _check_agent_scope (header-based) removed.  Replaced by
# require_credential_capability() in credential_authorization.py, which reads
# the registry-granted credential_scopes from verified token_context.


def _validate_proxy_url(url: str, settings: Settings) -> httpx.URL:
    """Validate the target URL and return a destination pinned to a public IP.

    Raises HTTPException(400) or HTTPException(403) on rejection.
    Issue #1158: SSRF + credential exfiltration mitigation.
    """
    # Authorize with the same URL parser used by the sending client.
    try:
        parsed = httpx.URL(url)
    except httpx.InvalidURL as exc:
        raise HTTPException(400, detail={"error": "invalid_url", "message": "Malformed URL"}) from exc

    # 1. Scheme check — HTTPS only when vault_proxy_require_https=True
    if settings.vault_proxy_require_https and parsed.scheme != "https":
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_url", "message": "Only https:// URLs are allowed"},
        )
    if parsed.scheme not in ("http", "https"):
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_url", "message": "Invalid URL scheme"},
        )

    hostname = parsed.host
    if not hostname:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_url", "message": "URL has no hostname"},
        )

    # 2. Reject embedded credentials in URL
    if parsed.username or parsed.password:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_url", "message": "URLs with embedded credentials are not allowed"},
        )

    # 3. FAIL-CLOSED allowlist check
    allowlist_raw = settings.vault_proxy_host_allowlist
    if not allowlist_raw:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "proxy_host_denied",
                "message": "No proxy host allowlist configured; all proxy requests denied",
            },
        )

    # Matching lives in credential_egress.host_matches so the allowlist check and
    # the credential->host binding check (#4076) cannot drift apart.
    allowed_hosts = {h.strip().lower() for h in allowlist_raw.split(",") if h.strip()}
    if host_matches(hostname, allowed_hosts):
        # Resolve once and connect to this exact address to prevent DNS rebinding.
        try:
            addresses = [ipaddress.ip_address(info[4][0]) for info in socket.getaddrinfo(hostname, None)]
        except (socket.gaierror, ValueError) as exc:
            raise HTTPException(400, detail={"error": "invalid_url", "message": "Cannot resolve proxy host"}) from exc
        if not addresses or any(not addr.is_global or addr.is_multicast for addr in addresses):
            raise HTTPException(400, detail={"error": "invalid_url", "message": "URLs targeting private/internal addresses are not allowed"})
        return parsed.copy_with(host=str(addresses[0]))

    # No match — reject
    raise HTTPException(
        status_code=403,
        detail={"error": "proxy_host_denied", "message": f"Host {hostname!r} is not in the proxy allowlist"},
    )


def _validate_credential_host_binding(cred_service: str, url: str, settings: Settings) -> None:
    """Validate that ``cred_service``'s credential may be sent to ``url``'s host.

    Issue #4076: the host allowlist answers "may we talk to this host at all?",
    not "may THIS credential go there?" — without this check a ``github``
    credential can be injected into a request to any other allowlisted host.

    ``cred_service`` MUST be the service on the *resolved* credential row, never
    the caller-supplied ``body.service``: binding on caller input would be
    self-asserted authority (the #16/#17 class) and worthless.

    Services with no entry in ``SERVICE_HOST_BINDINGS`` are **not** bound (the
    ``service`` column is deliberately free-form); they fall back to the global
    host allowlist and emit a WARN so operators can see coverage gaps.

    Raises HTTPException(403) when a bound service targets a host outside its set
    AND ``vault_enforce_credential_host_binding`` is True.  When the flag is
    False (explicit shadow-mode override) the violation is logged and allowed.
    Enforcement is the default; rollback requires an explicit config change.
    """
    hostname = httpx.URL(url).host

    if not is_binding_enforced(cred_service):
        logger.warning(
            "Credential egress binding NOT ENFORCED service=%s host=%s reason=service_unmapped",
            cred_service,
            hostname,
        )
        return

    allowed = allowed_hosts_for(cred_service)
    if host_matches(hostname, allowed):
        return

    if not settings.vault_enforce_credential_host_binding:
        logger.warning(
            "Credential egress binding VIOLATION (shadow mode, allowed) service=%s host=%s allowed=%s",
            cred_service,
            hostname,
            sorted(allowed),
        )
        return

    logger.warning(
        "Credential egress binding DENIED service=%s host=%s allowed=%s",
        cred_service,
        hostname,
        sorted(allowed),
    )
    raise HTTPException(
        status_code=403,
        detail={
            "error": "credential_host_binding_denied",
            "message": (f"A {cred_service!r} credential may not be sent to host {hostname!r}"),
        },
    )


# ---------------------------------------------------------------------------
# Endpoint: GET /internal/v1/user-credentials
# ---------------------------------------------------------------------------


@router.get(
    "/user-credentials",
    response_model=list[CredentialMetadata],
    summary="List credential metadata for a user (internal)",
    description=(
        "Returns credential metadata rows for the given user_id. "
        "When service is provided, filters to that service only (backwards compat). "
        "When service is omitted, returns all credentials the user can access "
        "(user-owned + team/org/domain-app via scope chain). "
        "secret_arn and values are NEVER returned. "
        "Intended for agents and ingest Lambdas to check available credentials before "
        "deciding which delivery path to use."
    ),
)
async def list_user_credentials(
    request: Request,
    user_id: str | None = Query(None, description="Required only for a verified worker's existing user binding"),
    service: str | None = Query(None, description="Service name filter (optional). When omitted, returns all services."),
    invocation_id: str | None = Query(None, description="Run invocation ID for credential-authorization binding"),
    db: AsyncSession = Depends(get_db),
    _: None = Depends(verify_internal_or_irsa),
) -> list[CredentialMetadata]:
    settings = get_settings()

    # Issue #3477: Credential-authorization binding.
    # Resolve the effective user from the webhook-events registry.
    binding = resolve_broker_binding(
        request,
        invocation_id=invocation_id,
        body_user_id=user_id,
        agent_id=None,
        settings=settings,
    )
    effective_user_id = binding.resolved_user_id

    # Validate user exists and resolve canonical user (Issue #700).
    user = await _get_user_context(effective_user_id, db, calling_endpoint="user-credentials", expected_org=worker_tenant(request))

    # Build query — filter by service only when provided (backwards compat).
    # Issue #700: use canonical user's id and org_id, not the inbound values.
    conditions = [
        UserCredential.org_id == user.org_id,
        UserCredential.user_id == user.id,
    ]
    if service is not None:
        conditions.append(UserCredential.service == service)

    stmt = select(UserCredential).where(*conditions)
    result = await db.execute(stmt)
    creds = result.scalars().all()

    logger.debug("Listed %d credentials", len(creds))
    authority = getattr(request.state, "agent_user_credential_authority", None)
    gateway_authority = getattr(request.state, "agent_tools_credential_authority", None)
    if gateway_authority is not None:
        creds = [credential for credential in creds if credential.id in gateway_authority.credential_ids]
    if authority is not None:
        creds = [credential for credential in creds if credential.id in authority.credential_ids]
    assert_tools_credential_fresh(request)
    return [CredentialMetadata.from_model(c) for c in creds]


def _proxy_total_timeout(request: Request) -> float:
    assert_tools_credential_fresh(request)
    authority = getattr(request.state, "agent_tools_credential_authority", None)
    return min(30.0, authority.assertion_expires_at - time.time() if authority is not None else 30.0)


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
    request: Request,
    db: AsyncSession = Depends(get_db),
    sm: SecretsManagerHelper = Depends(get_secrets_manager),
    _: None = Depends(verify_internal_or_irsa),
) -> ProxyResponse:
    provenance_id = str(uuid.uuid4())
    settings = get_settings()

    # Issue #3477: Credential-authorization binding — resolve BEFORE URL validation.
    # If the caller isn't authorized, don't reveal whether the URL is allowlisted.
    binding = resolve_broker_binding(
        request,
        invocation_id=body.invocation_id,
        body_user_id=body.user_id,
        agent_id=body.agent_id,
        task_id=body.task_id,
        settings=settings,
    )
    effective_user_id = binding.resolved_user_id

    async def _audit_denial(exc: HTTPException, user: User | None = None) -> None:
        """Best-effort denial audit row, shared by every proxy-request rejection.

        Issue #4076: the credential->host binding 403 must land in the SAME audit
        path as the URL-validation 403, or an operator sees a denial with no row.
        ``user`` is passed when it is already resolved (post-#465 callers) and
        resolved here otherwise.
        """
        reason = exc.detail.get("error") if isinstance(exc.detail, dict) else str(exc.detail)
        logger.warning(
            "Proxy request DENIED provenance_id=%s reason=%s",
            provenance_id,
            reason,
        )
        try:
            resolved = user or await _get_user_context(effective_user_id, db, calling_endpoint="proxy-request", expected_org=worker_tenant(request))
            await _write_audit(
                db,
                event_type="vault_proxy_request_denied",
                org_id=resolved.org_id,
                actor_id=broker_actor(request, body.agent_id),
                details={
                    "provenance_id": provenance_id,
                    "user_id": body.user_id,
                    "authorized_user_id": effective_user_id,
                    "binding_from_registry": binding.from_registry,
                    "binding_drift_detected": binding.drift_detected,
                    "agent_id": body.agent_id,
                    "task_id": body.task_id,
                    "service": body.service,
                    "reason": reason,
                },
            )
            await db.commit()
        except Exception:
            # Don't let audit-log failures mask the security rejection.
            logger.warning("credential denial audit write failed")

    # Issue #1158: Validate target URL before resolving credentials or making requests.
    try:
        target_url = await asyncio.to_thread(_validate_proxy_url, body.url, settings)
    except HTTPException as exc:
        await _audit_denial(exc)
        raise

    user = await _get_user_context(effective_user_id, db, calling_endpoint="proxy-request", expected_org=worker_tenant(request))
    # Issue #700: use canonical user's id and org_id for credential resolution.
    try:
        cred = await _resolve_credential(
            db=db,
            org_id=user.org_id,
            service=body.service,
            label=body.label,
            user_id=user.id,
            team_id=user.team_id,
        )
    except HTTPException as exc:
        if (
            exc.status_code == 404
            and body.service in {service for service, _ in PROVIDER_SCOPES.values()}
            and body.label in (None, "AgentCore OAuth")
        ):
            denied = HTTPException(status_code=403, detail={"error": "not_connected"})
            await _audit_denial(denied, user)
            raise denied from exc
        raise
    try:
        await verify_selected_user_credential(request, cred, db=db)
    except HTTPException as exc:
        if isinstance(exc.detail, dict) and exc.detail.get("error") == "not_consented":
            await _audit_denial(exc, user=user)
        raise

    if await github_pat_retired(db, cred, grace_days=settings.github_pat_grace_days):
        denied = HTTPException(status_code=403, detail={"error": "github_pat_retired", "message": "Connect GitHub in Settings > Credentials."})
        await _audit_denial(denied, user)
        raise denied

    # Issue #4076: credential->host binding. Runs on the RESOLVED cred.service
    # (never body.service — caller input must not decide this) and BEFORE
    # _fetch_secret, so a rejected request never pulls the secret out of Secrets
    # Manager at all.
    try:
        _validate_credential_host_binding(cred.service, body.url, settings)
    except HTTPException as exc:
        await _audit_denial(exc, user=user)
        raise

    try:
        await verify_selected_user_credential(request, cred, db=db, revalidate=True)
    except HTTPException as exc:
        if isinstance(exc.detail, dict) and exc.detail.get("error") == "not_consented":
            await _audit_denial(exc, user=user)
        raise

    # Resolve only a validated user-owned Identity pointer; legacy PATs still use SM.
    assert_tools_credential_fresh(request)
    if cred.provider == "agentcore-identity":
        secret_value = await _identity_access_token(cred, user, get_identity_client())
    elif cred.secret_arn.startswith("agentcore-identity:"):
        raise HTTPException(status_code=403, detail={"error": "not_connected"})
    else:
        secret_value = await _fetch_secret(cred.secret_arn, sm)
    try:
        await verify_selected_user_credential(request, cred, db=db, revalidate=True)
    except HTTPException as exc:
        if isinstance(exc.detail, dict) and exc.detail.get("error") == "not_consented":
            await _audit_denial(exc, user=user)
        raise
    if cred.provider == "agentcore-identity":
        still_connected = await db.scalar(
            select(UserCredential.id).where(
                UserCredential.id == cred.id,
                UserCredential.org_id == user.org_id,
                UserCredential.user_id == user.id,
                UserCredential.provider == "agentcore-identity",
            )
        )
        if not still_connected:
            raise HTTPException(status_code=403, detail={"error": "not_connected"})
    proxy_headers = {
        key: value
        for key, value in (body.headers or {}).items()
        if key.lower() not in {"authorization", "proxy-authorization", "cookie", "host", "accept-encoding"}
    }
    request_headers = inject_credential(
        cred.credential_type,
        secret_value,
        proxy_headers,
    )

    # Preserve the authorized virtual host and TLS identity when connecting to
    # the validated IP. Caller-supplied Host must not select another service.
    original_url = httpx.URL(body.url)
    request_headers = {key: value for key, value in request_headers.items() if key.lower() != "host"}
    request_headers["Host"] = original_url.netloc.decode("ascii")

    # Environment proxies would bypass IP pinning; redirects must not escape it.
    request_headers["Accept-Encoding"] = "identity"
    try:
        async with asyncio.timeout(_proxy_total_timeout(request)):
            async with httpx.AsyncClient(timeout=30.0, trust_env=False, follow_redirects=False) as client:
                async with client.stream(
                    method=body.method.upper(),
                    url=target_url,
                    headers=request_headers,
                    extensions={"sni_hostname": original_url.host},
                    content=body.body.encode("utf-8") if body.body else None,
                ) as response:
                    if response.headers.get("content-encoding", "identity").strip().lower() != "identity":
                        raise HTTPException(502, "credential proxy response encoding unsupported")
                    content_length = response.headers.get("content-length")
                    if content_length is not None and content_length.isdecimal() and (len(content_length) > 7 or int(content_length) > 1048576):
                        raise HTTPException(502, "credential proxy response too large")
                    received = 0
                    decoded_size = 0
                    chunks = []
                    decoder = codecs.getincrementaldecoder(response.encoding or "utf-8")(errors="replace")
                    async for chunk in response.aiter_raw():
                        received += len(chunk)
                        if received > 1048576:
                            raise HTTPException(502, "credential proxy response too large")
                        decoded = decoder.decode(chunk)
                        decoded_size += len(decoded.encode("utf-8"))
                        if decoded_size > 1048576:
                            raise HTTPException(502, "credential proxy response too large")
                        chunks.append(decoded)
                    final = decoder.decode(b"", final=True)
                    if decoded_size + len(final.encode("utf-8")) > 1048576:
                        raise HTTPException(502, "credential proxy response too large")
                    response_headers = dict(response.headers)
                    response_body = "".join(chunks) + final
    except (httpx.RequestError, TimeoutError):
        logger.error(
            "Proxy request failed provenance_id=%s",
            provenance_id,
        )
        raise HTTPException(
            status_code=502,
            detail={
                "error": "upstream_error",
                "message": "HTTP request to upstream failed",
                "provenance_id": provenance_id,
            },
        ) from None

    await verify_selected_user_credential(request, cred, db=db, revalidate=True)
    assert_tools_credential_fresh(request)

    # Update last_used_at and write audit log.
    await _touch_last_used(cred.id, db)
    await _write_audit(
        db,
        event_type="vault_proxy_request",
        org_id=user.org_id,
        actor_id=broker_actor(request, body.agent_id),
        details={
            "provenance_id": provenance_id,
            "user_id": body.user_id,
            "authorized_user_id": effective_user_id,
            "agent_id": body.agent_id,
            "task_id": body.task_id,
            "service": body.service,
            "label": body.label,
            "credential_id": cred.id,
            **user_credential_audit(request),
            "method": body.method.upper(),
            "destination_host": original_url.host,
            "response_status": response.status_code,
            "invocation_id": body.invocation_id,
            "binding_from_registry": binding.from_registry,
            "binding_drift_detected": binding.drift_detected,
        },
    )
    await db.commit()

    logger.info(
        "Proxy request completed provenance_id=%s status=%d",
        provenance_id,
        response.status_code,
    )
    response_headers, response_body = sanitize_proxy_response(cred.credential_type, secret_value, response_headers, response_body)
    await verify_selected_user_credential(request, cred, db=db, revalidate=True)
    assert_tools_credential_fresh(request)
    return ProxyResponse(
        status=response.status_code,
        headers=response_headers,
        body=response_body,
        provenance_id=provenance_id,
    )


# ---------------------------------------------------------------------------
# Endpoint: POST /internal/v1/credential-materialize
# ---------------------------------------------------------------------------


@router.post(
    "/credential-materialize",
    response_model=MaterializeResponse,
    status_code=201,
    summary="File credential materialization is unavailable (internal)",
    description=(
        "Checks binding, credential:materialize capability and file credential selection, then returns 503. "
        "No secret is fetched or uploaded until a private recipient and durable cleanup are available."
    ),
)
async def credential_materialize(
    body: MaterializeBody,
    request: Request,
    db: AsyncSession = Depends(get_db),
    sm: SecretsManagerHelper = Depends(get_secrets_manager),
    _: None = Depends(verify_internal_or_irsa),
) -> MaterializeResponse:
    settings = get_settings()

    # Issue #3477: Credential-authorization binding — resolve BEFORE scope gate.
    # If the caller isn't bound to a valid run, fail fast before checking scopes.
    binding = resolve_broker_binding(
        request,
        invocation_id=body.invocation_id,
        body_user_id=body.user_id,
        agent_id=body.agent_id,
        settings=settings,
    )
    effective_user_id = binding.resolved_user_id

    # Issue #6050: registry-based scope gate (replaces header-based _check_agent_scope).
    require_credential_capability(request, "credential:materialize")

    user = await _get_user_context(effective_user_id, db, calling_endpoint="credential-materialize", expected_org=worker_tenant(request))
    # Issue #700: use canonical user's id and org_id for credential resolution.
    cred = await _resolve_credential(
        db=db,
        org_id=user.org_id,
        service=body.service,
        label=body.label,
        user_id=user.id,
        team_id=user.team_id,
    )
    await verify_selected_user_credential(request, cred, db=db)

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

    raise HTTPException(
        status_code=503,
        detail={
            "error": "credential_materialization_cleanup_unavailable",
            "message": "Private file delivery requires a bound recipient and durable cleanup.",
        },
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
        "registry-granted credential:raw-read capability. "
        "Every call is audit-logged regardless of outcome."
    ),
)
async def credential_raw_read(
    body: RawReadBody,
    request: Request,
    db: AsyncSession = Depends(get_db),
    sm: SecretsManagerHelper = Depends(get_secrets_manager),
    _: None = Depends(verify_internal_or_irsa),
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

    # Issue #6050: registry-based scope gate (replaces header-based _check_agent_scope).
    require_credential_capability(request, "credential:raw-read")

    # Issue #3175: Credential-authorization binding (S2).
    # Resolve the effective user from the webhook-events registry.
    binding = resolve_broker_binding(
        request,
        invocation_id=body.invocation_id,
        body_user_id=body.user_id,
        agent_id=body.agent_id,
        settings=settings,
    )
    effective_user_id = binding.resolved_user_id

    user = await _get_user_context(effective_user_id, db, calling_endpoint="credential-raw-read", expected_org=worker_tenant(request))
    # Issue #700: use canonical user's id and org_id for credential resolution.
    cred = await _resolve_credential(
        db=db,
        org_id=user.org_id,
        service=body.service,
        label=body.label,
        user_id=user.id,
        team_id=user.team_id,
    )
    await verify_selected_user_credential(request, cred, db=db)
    if await github_pat_retired(db, cred, grace_days=settings.github_pat_grace_days):
        raise HTTPException(status_code=403, detail={"error": "github_pat_retired", "message": "Connect GitHub in Settings > Credentials."})

    secret_value = await _fetch_secret(cred.secret_arn, sm)
    await verify_selected_user_credential(request, cred, db=db, revalidate=True)

    await _touch_last_used(cred.id, db)
    await _write_audit(
        db,
        event_type="vault_credential_raw_read",
        org_id=user.org_id,
        actor_id=body.agent_id,
        details={
            "provenance_id": provenance_id,
            "user_id": body.user_id,
            "authorized_user_id": effective_user_id,
            "agent_id": body.agent_id,
            "task_id": body.task_id,
            "service": body.service,
            "label": body.label,
            "credential_id": cred.id,
            **user_credential_audit(request),
            "credential_type": cred.credential_type,
            "purpose": body.purpose,
            "invocation_id": body.invocation_id,
            "binding_from_registry": binding.from_registry,
            "binding_drift_detected": binding.drift_detected,
        },
    )
    await db.commit()

    logger.info(
        "Raw credential read provenance_id=%s service=%s agent=%s binding_from_registry=%s",
        provenance_id,
        body.service,
        body.agent_id,
        binding.from_registry,
    )
    await verify_selected_user_credential(request, cred, db=db, revalidate=True)
    return RawReadResponse(
        value=secret_value,
        credential_type=cred.credential_type,
        provenance_id=provenance_id,
    )
