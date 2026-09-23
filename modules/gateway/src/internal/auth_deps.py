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

Issue #5653 (A01): the IRSA path now requires the assertion to pass the shared
provenance check (src/auth/caller_provenance.py) before the registry lookup runs.
An assertion the edge does not vouch for is rejected here, not passed through to
the shared-secret branch.

DELIBERATE DEVIATION from #5653's prose, which asked to make the shared secret
mandatory "in all cases" and remove the identity short-circuit. That is NOT
implemented, because this issue's own reviewed acceptance overrides it:

    "retain a proven SigV4/IAM path rather than mandating both IAM and a
     shared secret"

and because it would be an outage, not a fix. The two legitimate IRSA callers —
the scaledjob-worker pods and the platform deploy-runner (see
agent-registry-seed.tf and lambda-authorizer/main.tf) — authenticate by SigV4
through API Gateway and hold no copy of BG_INTERNAL_API_KEY. Requiring the secret
on top of IAM would 403 every worker credential fetch and every customer-deploy
credential assumption, which is the "over-broad rejection breaking legitimate
machine-to-machine traffic" row of the issue's own blast-radius table.

What actually closes the hole the prose was aiming at is the pair of controls that
do not depend on a second application credential: API Gateway blanking
X-Caller-Identity on every non-AWS_IAM route, and the caller-side egress policies
that deny the agent workloads any in-cluster route to this service (see
src/auth/caller_provenance.py for why the bypass must be closed from the caller's
namespace and not by a gateway-side ingress policy). Dual-auth remains: SigV4 for
principals that have it, shared secret for the ClusterIP callback that does not.
"""

from __future__ import annotations

import hmac
import logging
import os

from fastapi import Header, HTTPException, Request

from src.auth.caller_provenance import has_caller_identity_assertion, verified_caller_identity
from src.auth.middleware import extract_iam_identity_from_headers
from src.shared.config import get_settings

logger = logging.getLogger(__name__)

# Issue #3985 (A2): scopes permitted to act on the internal plane.
#
# Two seeded principals legitimately call /internal/*:
#   "internal" — scaledjob-worker (modules/agent-factory/infra/agent-registry-seed.tf)
#   "platform" — deploy-runner (gateway/infra/modules/lambda-authorizer/main.tf),
#                which calls POST /internal/v1/credential-assume-role on
#                customer-deploy workflows via SigV4 (Issue #1108). Omitting it
#                403s deploy-time credential assumption.
#
# Neither value is self-assignable: the agent_registry admin API constrains scope
# to ^(shared|personal)$ on both the create and update schemas
# (admin/agent_registry_schemas.py), so "internal" and "platform" are written
# only by the Terraform seeds. A registered agent that holds valid IRSA
# credentials for some *other* purpose therefore cannot reach the internal plane
# just by being registered.
#
# Deliberately NOT allowlisted: "shared" (and "personal"). Those ARE
# self-assignable through the admin API, so allowlisting either would defeat this
# control entirely. The test_agent seed carries scope "shared" and stays gated by
# design.
#
# This is enforced on the IRSA path only. The shared-secret path (agent-context
# ingestion status callback, which reaches the pod via ClusterIP and never
# transits API Gateway or the ALB) carries no registry entry and no scope, so a
# blanket /internal/* scope check would 403 it and stop ingestion platform-wide.
INTERNAL_PLANE_SCOPES = frozenset({"internal", "platform"})


def _reject_internal_key() -> HTTPException:
    """The single rejection used for every shared-secret failure.

    Issue #5656 (A05): absent, empty, wrong-length and wrong-content keys must be
    indistinguishable to the caller. Building the response in one place keeps the
    status code, error code and message identical across all of them, so a caller
    cannot tell *which* way they were wrong from the reply body either.
    """
    return HTTPException(status_code=403, detail={"error": "forbidden", "message": "Invalid internal API key"})


def _verify_internal_key(x_internal_api_key: str | None) -> None:
    """Validate the shared internal API key.

    Missing or wrong key -> 403 (not 401) so external scanners don't learn that
    the endpoint exists from a WWW-Authenticate header.

    Issue #5656 (A05): the comparison is constant-time. It previously used `!=`,
    which short-circuits at the first differing byte, so the time to reject was a
    function of how many leading bytes the caller got right. Since this one secret
    is the only gate in front of raw credential reads, credential materialisation,
    installation-token issuance and request proxying (src/internal/routes.py,
    credential_routes.py), a caller able to time many rejections could recover it
    byte-by-byte instead of brute-forcing 256 bits. hmac.compare_digest examines
    every byte regardless, matching the pattern already used for edge provenance
    (src/auth/caller_provenance.py), run-credential MACs
    (src/agentauth/run_credential.py) and the Knowledge Door service key
    (modules/agent-context/door/auth.py).

    Both sides are encoded to bytes before comparison. compare_digest rejects str
    inputs containing non-ASCII (TypeError) and raises on None, so the
    absent/empty case is handled *before* the call — a raised TypeError here would
    become a 500 on every internal request, turning a hardening change into an
    outage of the internal plane. Encoding also means a key whose stored and
    presented forms differ only in encoding fails closed rather than crashing.

    The length check that compare_digest performs internally is not a leak we can
    avoid and is not the one that mattered: it distinguishes only "wrong length"
    from "right length", not *where* the content diverges, and it is the documented
    behaviour of every constant-time comparison primitive. What it does NOT do is
    let the caller walk the secret one byte at a time, which is what `!=` allowed.
    """
    settings = get_settings()
    expected = settings.internal_api_key
    if not expected:
        logger.error("BG_INTERNAL_API_KEY is not set; all /internal/v1/* calls will be rejected")
        raise HTTPException(
            status_code=503,
            detail={"error": "not_configured", "message": "Internal API not configured"},
        )
    # Absent/empty first: compare_digest raises on None, and an empty presented key
    # can never be valid (an empty `expected` was already rejected as 503 above).
    if not x_internal_api_key:
        raise _reject_internal_key()
    if not hmac.compare_digest(x_internal_api_key.encode("utf-8"), expected.encode("utf-8")):
        raise _reject_internal_key()


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
    # Issue #5653 (A01): entry is gated on the shared provenance helper, so this
    # guard, get_current_user and TokenContextMiddleware share one definition of a
    # trustworthy assertion. An assertion that FAILS provenance is rejected below
    # rather than silently falling through to the shared secret.
    if has_caller_identity_assertion(request):
        # Settings from this module's own get_settings — the same object
        # _verify_internal_key reads the shared secret from.
        if verified_caller_identity(request, settings=get_settings()) is None:
            # Asserted an identity the edge does not vouch for. Reject rather than
            # fall back to the shared secret: falling back would mask a forgery
            # attempt behind a 200 for anyone holding the secret (the #3985
            # "presence is terminal" property), and a caller legitimately using the
            # shared secret sends no X-Caller-Identity at all.
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
        if (
            getattr(token_context, "requires_run_identity", False)
            or (os.environ.get("AGENT_AUTHORITY_ENABLED", "false").lower() == "true" and token_context.scope == "internal")
            or request.headers.get("X-Adp-Run-Credential")
            or request.headers.get("X-Adp-Workload-Token")
        ):
            from src.agentauth.broker_identity import BROKER_PATHS, verify_broker_worker

            if request.url.path in BROKER_PATHS:
                await verify_broker_worker(request)
        logger.debug(
            "Internal endpoint authenticated via IRSA: agent=%s",
            token_context.user_id,
        )
        return

    # Legacy path: validate the shared-secret header.
    if (
        os.environ.get("AGENT_AUTHORITY_ENABLED", "false").lower() == "true"
        or request.headers.get("X-Adp-Run-Credential")
        or request.headers.get("X-Adp-Workload-Token")
    ):
        from src.agentauth.broker_identity import BROKER_PATHS

        if request.url.path in BROKER_PATHS:
            raise HTTPException(403, "worker credential brokers require IAM transport")
    _verify_internal_key(x_internal_api_key)
