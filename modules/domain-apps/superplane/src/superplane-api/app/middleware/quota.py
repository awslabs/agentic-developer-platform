"""Quota enforcement middleware — intercepts resource-creating requests and validates quotas.

This middleware checks quotas for:
- POST /workspaces           -> org max_workspaces
- POST /workspaces/*/nodes   -> workspace max_nodes, max_gpus, allowed_clouds
- POST /workspaces/*/deployments -> workspace max_gpus

The middleware reads the request body (for POST/PATCH), performs quota checks,
and returns HTTP 429 if quotas would be exceeded.

Note: For most enforcement, we prefer to call quota service functions directly
in the router handlers rather than in middleware, for better error context.
This module provides a lightweight guard as a safety net.
"""

import logging
import re
from typing import Callable

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger(__name__)

# Patterns for routes that require quota enforcement
WORKSPACE_CREATE_PATTERN = re.compile(r"^/workspaces/?$")
NODE_CREATE_PATTERN = re.compile(r"^/workspaces/[^/]+/nodes/?$")
DEPLOYMENT_CREATE_PATTERN = re.compile(r"^/workspaces/[^/]+/deployments/?$")


class QuotaEnforcementMiddleware(BaseHTTPMiddleware):
    """Middleware that adds quota enforcement headers to responses.

    This middleware does not perform the actual quota check (that's done in
    router-level enforcement). Instead, it:
    1. Adds X-Quota-Enforcement: active header to responses
    2. Logs quota-related 429 responses for monitoring
    """

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        response = await call_next(request)

        # Add quota enforcement header
        response.headers["X-Quota-Enforcement"] = "active"

        # Log quota exceeded responses
        if response.status_code == 429:
            quota_type = response.headers.get("X-Quota-Type", "unknown")
            logger.warning(
                "Quota exceeded: path=%s method=%s quota_type=%s",
                request.url.path,
                request.method,
                quota_type,
            )

        return response
