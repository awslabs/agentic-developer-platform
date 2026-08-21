"""Dual-auth dependency for /internal/v1/* routes.

Issue #575: Migrate /internal/v1/* from shared-secret auth to IRSA/SigV4 via API Gateway.

During the rollout, internal endpoints accept EITHER:
  1. IRSA identity via API Gateway (X-Caller-Identity header) — new path
  2. Shared-secret (X-Internal-Api-Key header) — legacy path

Preference order: IRSA first (if X-Caller-Identity present), fallback to shared-secret.
At least one must succeed or the request is rejected with 403.

Issue #3985: X-Caller-Identity presence is terminal — see verify_internal_or_irsa.
Presenting the header commits the request to the IRSA path; it cannot fall back
to the shared secret. Shared-secret callers must send no X-Caller-Identity.
"""

from __future__ import annotations

import logging

from fastapi import Header, HTTPException, Request

from src.auth.middleware import extract_iam_identity_from_headers
from src.shared.config import get_settings

logger = logging.getLogger(__name__)


def _verify_internal_key(x_internal_api_key: str | None) -> None:
    """Validate the shared internal API key.

    Missing or wrong key -> 403 (not 401) so external scanners don't learn that
    the endpoint exists from a WWW-Authenticate header.
    """
    settings = get_settings()
    expected = settings.internal_api_key
    if not expected:
        logger.error("BG_INTERNAL_API_KEY is not set; all /internal/v1/* calls will be rejected")
        raise HTTPException(
            status_code=503,
            detail={"error": "not_configured", "message": "Internal API not configured"},
        )
    if not x_internal_api_key or x_internal_api_key != expected:
        raise HTTPException(status_code=403, detail={"error": "forbidden", "message": "Invalid internal API key"})


async def verify_internal_or_irsa(
    request: Request,
    x_internal_api_key: str | None = Header(default=None),
    x_caller_identity: str | None = Header(default=None),
) -> None:
    """Accept either shared-secret (legacy) or IRSA via API Gateway (new).

    Preference order: IRSA first (if X-Caller-Identity header is present),
    falling back to shared-secret. At least one must succeed.

    When IRSA succeeds, sets request.state.token_context with the agent's
    TokenContext (looked up from agent_registry DynamoDB table).

    When shared-secret succeeds, no token_context is set (legacy behavior).

    Issue #3985: X-Caller-Identity presence is TERMINAL. If the header is
    present, the request is authenticated as IRSA or rejected — it never falls
    back to the shared secret. Previously an unparseable ARN made
    extract_iam_identity_from_headers return None (agent_registry
    .parse_assumed_role_arn -> None), which fell through to _verify_internal_key.
    That routed a *malformed* identity assertion to the legacy path instead of
    rejecting it, so anyone holding the shared secret could send a garbage ARN
    and still be served, and a forged-but-unparseable ARN produced the same 200
    as a legitimate one — masking the attempt.

    Callers that legitimately use the shared secret (e.g. the agent-context
    ingestion status callback, which reaches the pod via ClusterIP and never
    transits API Gateway) send no X-Caller-Identity at all and are unaffected.
    """
    if x_caller_identity:
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

        request.state.token_context = token_context
        logger.debug(
            "Internal endpoint authenticated via IRSA: agent=%s",
            token_context.user_id,
        )
        return

    # Legacy path: validate the shared-secret header.
    _verify_internal_key(x_internal_api_key)
