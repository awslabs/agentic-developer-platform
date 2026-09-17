"""Audit middleware — automatically logs create/update/delete API calls to events table."""

import logging
import uuid

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from app.database import async_session_factory
from app.services.audit import (
    HTTP_METHOD_TO_ACTION,
    extract_resource_from_path,
    log_event,
)

logger = logging.getLogger(__name__)

# Paths to skip for audit logging (health checks, docs, etc.)
SKIP_PATHS = frozenset(
    {
        "/health",
        "/healthz",
        "/readyz",
        "/docs",
        "/redoc",
        "/openapi.json",
    }
)

# Only audit mutating methods by default (GET /events is read-only, handled by router)
AUDITABLE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


class AuditMiddleware(BaseHTTPMiddleware):
    """Middleware that logs mutating API calls (POST/PUT/PATCH/DELETE) to the events table.

    Extracts org_id from the JWT token in the request state or Authorization header,
    then records the action after the response is generated.
    """

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        """Intercept request, execute handler, then log audit event."""
        # Skip non-auditable methods and excluded paths
        if request.method not in AUDITABLE_METHODS:
            return await call_next(request)

        path = request.url.path
        if path in SKIP_PATHS:
            return await call_next(request)

        # Execute the actual request handler
        response = await call_next(request)

        # Only log successful mutations (2xx and 3xx status codes)
        if response.status_code >= 400:
            return response

        # Try to extract org_id from JWT (best effort)
        org_id = await self._extract_org_id(request)
        if org_id is None:
            # Cannot log without org_id — skip silently
            return response

        # Extract resource info from path
        resource_type, resource_id = extract_resource_from_path(path)
        action = HTTP_METHOD_TO_ACTION.get(request.method, request.method.lower())
        user_id = str(org_id)
        source_ip = request.client.host if request.client else None

        # Log audit event in a separate session (fire-and-forget within request lifecycle)
        try:
            async with async_session_factory() as session:
                await log_event(
                    db=session,
                    org_id=org_id,
                    user_id=user_id,
                    action=action,
                    resource_type=resource_type,
                    resource_id=resource_id,
                    event_type="api_call",
                    message=f"{request.method} {path}",
                    source_ip=source_ip,
                    request_path=path,
                    http_status=response.status_code,
                )
        except Exception:
            # Audit logging should never break the request
            logger.exception(
                "Failed to log audit event for %s %s", request.method, path
            )

        return response

    @staticmethod
    async def _extract_org_id(request: Request) -> uuid.UUID | None:
        """Try to extract org_id from the Authorization header JWT.

        This is best-effort — if the token is missing or invalid, returns None.
        """
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return None

        token = auth_header[7:]
        try:
            from app.middleware.auth import decode_token

            token_data = decode_token(token)
            return token_data.org_id
        except Exception:
            return None
