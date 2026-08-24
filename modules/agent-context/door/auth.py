"""Shared-secret authentication for the Door (issue #4073, finding #8).

Why this exists
---------------
The Door derives every ACL decision from caller-supplied request headers
(``x-github-login``, ``x-github-teams``, ``x-tenant-id``, ``x-owner-sub`` —
see ``acl.extract_caller_principal``). Before this module, nothing
authenticated the caller. Any workload that could reach the ClusterIP could
assert an arbitrary identity and read any tenant's indexed source code, wikis
and agent memory. ``acl.py``, ``personal_context/identity.py`` and
``README.md`` all asserted that an in-cluster NetworkPolicy made the headers
trustworthy; no NetworkPolicy existed in this module (it ships alongside this
change as ``manifests/networkpolicy.yaml``). The header trust boundary was
therefore asserted but never enforced — in either layer.

Why ASGI middleware and not ``Depends()``
-----------------------------------------
``server.py`` does ``app.mount("/mcp", get_mcp_app())``. A mount is a separate
Starlette ASGI app resolved by URL prefix, so FastAPI route dependencies
declared on the parent app do **not** run for it. A ``Depends()``-based guard
would leave ``/mcp`` — the *native MCP surface actually used by agent workers,
and the one with DNS-rebinding protection relaxed — completely open. HTTP
middleware runs for every request the parent app routes, mounts included, so
it is the only placement that covers both the legacy REST verbs and ``/mcp``.

Defence in depth, not a replacement
-----------------------------------
This is one of two controls. The NetworkPolicy restricts *who can connect*;
this key authenticates *who is asking*. Neither subsumes the other: the policy
admits the whole ``adp-agents`` namespace (it cannot distinguish one agent pod
from another), and the key alone would still be reachable from anywhere in the
cluster.
"""

from __future__ import annotations

import hmac
import json
import logging

from starlette.requests import Request
from starlette.responses import Response

from .config import config

log = logging.getLogger(__name__)

# Header carrying the shared secret. Matches the gateway's internal plane
# (``gateway/src/internal/auth_deps.py``) and the client half this module
# already ships (``images/ingestion/status_callback.py`` sends the same header),
# so no caller needs a new credential type.
HEADER_API_KEY = "x-internal-api-key"

# Paths served without authentication.
#
# ``/health`` only: the kubelet issues the readiness/liveness probes in
# ``manifests/context-mcp.yaml`` and cannot present a secret. Gating it would
# fail every probe and CrashLoop the Deployment. It returns a static
# ``{"status": "ok"}`` and discloses nothing.
#
# ``/tools`` is deliberately NOT here: the tool catalogue is a disclosure
# surface (it enumerates the verbs and their parameters), and it is not on any
# probe path.
_PUBLIC_PATHS = frozenset({"/health"})


def _is_public_path(path: str) -> bool:
    """True for paths served without authentication.

    Matched exactly (modulo a trailing slash) rather than by prefix: a prefix
    test on ``/health`` would also exempt an attacker-chosen ``/healthz``, and a
    substring test would exempt anything containing it.
    """
    normalized = path.rstrip("/") or "/"
    return normalized in _PUBLIC_PATHS


def _json_error(status_code: int, error: str, message: str) -> Response:
    return Response(
        content=json.dumps({"error": error, "message": message}),
        status_code=status_code,
        media_type="application/json",
    )


def check_request_auth(request: Request) -> Response | None:
    """Authenticate one request. Returns an error Response, or None to allow.

    Returns
    -------
    ``None`` when the request may proceed; otherwise the ``Response`` to return
    immediately (the caller must not invoke the downstream app).
    """
    if not config.door_auth_enabled:
        # Explicitly disabled. Loud, because a deployed environment must never
        # run this way — it restores the #4073 unauthenticated cross-tenant read.
        log.warning(
            "Door authentication is DISABLED (DOOR_AUTH_ENABLED=false); "
            "caller identity headers are unauthenticated. Do not run a deployed "
            "environment in this state — see issue #4073."
        )
        return None

    if _is_public_path(request.url.path):
        return None

    expected = config.door_api_key
    if not expected:
        # Fail CLOSED on misconfiguration, and say so in the logs.
        #
        # The tempting alternative — allow the request when no key is
        # configured — is how the two fail-open incidents this repo already
        # carries runbooks for happened (ALLOWLIST_MODE=open without
        # ALLOW_OPEN_SIGNUP, and the budget fail-open). A key that fails to
        # land would silently reopen the vulnerability with no signal.
        #
        # The cost of this choice is a hard dependency on the secret being
        # seeded: DOOR_API_KEY reaches the pod from the K8s secret
        # ``agent-context-door-auth``, populated by agent-context-deploy.yml
        # from Secrets Manager ``adp/<env>/gateway/internal-api-key``.
        log.error(
            "DOOR_API_KEY is not set; rejecting all authenticated Door requests. "
            "Seed the agent-context-door-auth secret (agent-context-deploy.yml)."
        )
        return _json_error(503, "not_configured", "Door authentication is not configured.")

    presented = request.headers.get(HEADER_API_KEY)
    # compare_digest to avoid leaking the key through response timing. Guard the
    # None/empty case first: compare_digest raises TypeError on None.
    if not presented or not hmac.compare_digest(presented, expected):
        log.warning(
            "Rejecting unauthenticated Door request: path=%s claimed_login=%r claimed_tenant=%r",
            request.url.path,
            request.headers.get("x-github-login", ""),
            request.headers.get("x-tenant-id", ""),
        )
        # 403 rather than 401: no WWW-Authenticate challenge, matching the
        # gateway's internal plane so a scanner learns nothing about the scheme.
        # 401, per the #4073 acceptance criteria ("unauth POST /call → 401",
        # "forged x-github-login w/o key → 401", "unauth POST /mcp → 401").
        #
        # The gateway's equivalent gate returns 403 specifically to avoid
        # emitting a ``WWW-Authenticate`` challenge that would tell a scanner
        # which scheme to attack. That property is preserved here by omitting
        # the challenge header rather than by changing the status code, so the
        # semantically-correct code ("no valid credentials presented") and the
        # non-disclosure property both hold.
        return _json_error(401, "unauthorized", "Invalid or missing internal API key.")

    return None
