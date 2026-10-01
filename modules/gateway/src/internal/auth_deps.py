"""Authenticate internal callers with an edge-verified, registered IAM identity.

A shared transport key conveys no caller or tenant authority and is never accepted.
Ingestion callbacks authenticate independently with their asset/attempt grant.
"""

from __future__ import annotations

import logging

from fastapi import Header, HTTPException, Request

from src.auth.caller_provenance import has_caller_identity_assertion, verified_caller_identity
from src.auth.middleware import extract_iam_identity_from_headers
from src.shared.config import get_settings

logger = logging.getLogger(__name__)

INTERNAL_PLANE_SCOPES = frozenset({"internal", "platform"})


async def verify_internal_or_irsa(
    request: Request,
    x_internal_api_key: str | None = Header(default=None),
    x_caller_identity: str | None = Header(default=None),
) -> None:
    """Resolve IAM identity; sensitive operations additionally verify run authority."""
    if has_caller_identity_assertion(request):
        if verified_caller_identity(request, settings=get_settings()) is None:
            # An unvouched assertion never establishes a principal.
            logger.warning("Rejecting internal request: X-Caller-Identity failed provenance verification")
            raise HTTPException(
                status_code=403,
                detail={
                    "error": "invalid_caller_identity",
                    "message": "X-Caller-Identity did not arrive through an identity-verified route.",
                },
            )

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
            # Registered and correctly signed, but not an internal-plane
            # principal. Reject rather than serve: every /internal/* route
            # trusts its caller to assert org/tenant identity.
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
