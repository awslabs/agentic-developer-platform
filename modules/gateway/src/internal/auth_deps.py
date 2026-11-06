"""Authenticate internal callers with an edge-verified, registered IAM identity.

A shared transport key conveys no caller or tenant authority and is never accepted.
Ingestion callbacks authenticate independently with their asset/attempt grant.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Literal

from fastapi import Header, HTTPException, Request

from src.auth.agent_registry import parse_assumed_role_arn
from src.auth.caller_provenance import has_caller_identity_assertion, verified_caller_identity
from src.auth.middleware import extract_iam_identity_from_headers
from src.shared.config import get_settings

logger = logging.getLogger(__name__)

INTERNAL_PLANE_SCOPES = frozenset({"internal", "platform"})


@dataclass(frozen=True)
class VerifiedInternalIdentity:
    kind: Literal["service", "agent_run", "domain_executor", "tools_assertion"]
    service_id: str | None
    tenant_id: str | None
    provenance: str


async def verify_internal_or_irsa(
    request: Request,
    x_internal_api_key: str | None = Header(default=None),
    x_caller_identity: str | None = Header(default=None),
) -> None:
    """Resolve IAM identity; sensitive operations additionally verify run authority."""
    if has_caller_identity_assertion(request):
        caller_arn = verified_caller_identity(request, settings=get_settings())
        if caller_arn is None:
            # An unvouched assertion never establishes a principal.
            logger.warning("Rejecting internal request: X-Caller-Identity failed provenance verification")
            raise HTTPException(
                status_code=403,
                detail={
                    "error": "invalid_caller_identity",
                    "message": "X-Caller-Identity did not arrive through an identity-verified route.",
                },
            )

        gateway_role = os.environ.get("ADP_TOOLS_GATEWAY_ROLE_ARN")
        if request.headers.get("x-adp-assertion") or (gateway_role and parse_assumed_role_arn(caller_arn) == gateway_role):
            from src.internal.tools_credential_auth import verify_tools_credential_request

            await verify_tools_credential_request(request)
            request.state.internal_identity = VerifiedInternalIdentity("tools_assertion", None, None, "tools_assertion")
            return

        # IRSA path: extract_iam_identity_from_headers validates the IAM ARN
        # and looks up the caller in the agent_registry DynamoDB table.
        # Raises HTTPException on unregistered agents.
        try:
            token_context = extract_iam_identity_from_headers(request)
        except HTTPException:
            # Re-raise HTTP exceptions (e.g. 403 for unregistered agent)
            raise
        except Exception as exc:
            # Convert BedrockGatewayError (e.g. UnregisteredServiceAccountError) to HTTPException
            from src.shared.exceptions import BedrockGatewayError

            if isinstance(exc, BedrockGatewayError):
                raise HTTPException(
                    status_code=exc.status_code,
                    detail={"error": exc.error, "message": exc.message},
                ) from exc
            raise

        if token_context is None:
            # Header present but no identity could be resolved from it — reject
            # rather than fall through to the shared secret.
            logger.warning("Rejecting internal request: X-Caller-Identity present but not resolvable to an identity")
            raise HTTPException(
                status_code=403,
                detail={
                    "error": "invalid_caller_identity",
                    "message": "X-Caller-Identity could not be resolved to a registered identity.",
                },
            )

        if token_context.scope not in INTERNAL_PLANE_SCOPES:
            # Registered and correctly signed, but not an internal-plane principal.
            logger.warning(
                "Rejecting internal request: agent=%s scope=%r is not an internal-plane scope",
                token_context.user_id,
                token_context.scope,
            )
            raise HTTPException(
                status_code=403,
                detail={
                    "error": "not_internal_plane",
                    "message": "Caller is not authorized for the internal plane.",
                },
            )

        request.state.token_context = token_context
        tenant = getattr(token_context, "org_id", None)
        service_id = getattr(token_context, "agent_registry_id", None)
        request.state.internal_identity = VerifiedInternalIdentity(
            "service",
            service_id if isinstance(service_id, str) and service_id else None,
            tenant if isinstance(tenant, str) and tenant and not tenant.startswith("__") else None,
            "iam",
        )
        from src.agentauth.broker_identity import BROKER_PATHS, verify_broker_worker

        if request.url.path in BROKER_PATHS:
            if request.url.path == "/internal/v1/github-installation-token" and request.headers.get("X-Adp-Report-Credential"):
                from src.agentauth.shared_review_identity import verify_shared_review_worker

                await verify_shared_review_worker(request)
            else:
                await verify_broker_worker(request)
        logger.debug(
            "Internal endpoint authenticated via IRSA: agent=%s",
            token_context.user_id,
        )
        return

    raise HTTPException(403, "verified IAM caller identity required")
