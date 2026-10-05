"""Gateway API client for identity resolution and auto-provisioning.

Phase B.1 (Issue #402): Used by the identity resolver when a tenant has
user_provisioning_mode="auto_provision". Calls the Gateway admin API to
create a minimal user record, which writes both Postgres + DDB identity-index.

Issue #702: Added resolve_user_by_identity() to call the existing
POST /internal/v1/resolve-user endpoint as a Postgres safety-net for
canonical user_id resolution.

Issue #4046 (#2724 slice A): resolve_installation_by_id() returns distinct
states (resolved / revoked / not_found / error) so callers can distinguish
authoritative absence or revocation from gateway unavailability.

Only invoked from the webhook Lambda when:
  1. Tenant is resolved (installation is known)
  2. Sender is NOT resolved (unknown_user)
  3. Tenant's user_provisioning_mode == "auto_provision"
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request

logger = logging.getLogger(__name__)

GATEWAY_API_URL = os.environ.get("GATEWAY_API_URL", "")
GATEWAY_ADMIN_TOKEN_ARN = os.environ.get("GATEWAY_ADMIN_TOKEN_ARN", "")
INTERNAL_API_KEY_ARN = os.environ.get("INTERNAL_API_KEY_ARN", "")

_admin_token: str | None = None
_internal_api_key: str | None = None

# resolve_installation_by_id() result states (Issue #4046 / #2724 slice A).
# INSTALLATION_NOT_FOUND is authoritative (gateway 404); INSTALLATION_ERROR means
# "we could not find out" and must never be treated as "not a tenant".
INSTALLATION_RESOLVED = "resolved"
INSTALLATION_NOT_FOUND = "not_found"
INSTALLATION_ERROR = "error"
INSTALLATION_REVOKED = "revoked"

# resolve_user_state() result states (Issue #5664, A10). Same three-state contract
# as the installation resolver above, and for the same reason: a caller deciding
# whether a DDB row may still carry authority must distinguish "Postgres looked and
# this link does not exist / is not proven" (authoritative — the stale DDB row must
# not stand in for it) from "we could not ask" (an outage, which must not become a
# platform-wide deny). `resolve_user_by_identity` collapsed all of these to None,
# which is why the stale-row fallback could not be closed safely.
USER_RESOLVED = "resolved"
USER_NOT_FOUND = "not_found"
USER_AMBIGUOUS = "ambiguous"
USER_ERROR = "error"

# The authoritative answers. On either of these Postgres has spoken, so a DDB row
# that disagrees is stale and must not supply authority on its own.
USER_AUTHORITATIVE_STATES = frozenset({USER_RESOLVED, USER_NOT_FOUND, USER_AMBIGUOUS})

# Issue #2724 (slice B): organizations.created_via values. Provenance records
# WHICH path created the tenant row — the signal the auto-register gate trusts,
# because tenant *existence* is creatable by the installing party (the
# unauthenticated no-nonce install callback upserts a shell when the gateway's
# own ORG_TENANT_AUTO_CREATE is on).
CREATED_VIA_OPERATOR = "operator"
CREATED_VIA_REGISTER_FLOW = "register_flow"
CREATED_VIA_INSTALL_AUTOCREATE = "install_autocreate"

# Provenances that mean "an ADP operator or an authenticated ADP flow onboarded
# this tenant". Anything else is self-created and untrusted for auto-register.
TRUSTED_PROVENANCE = frozenset({CREATED_VIA_OPERATOR, CREATED_VIA_REGISTER_FLOW})


def _resolve_admin_token() -> str:
    """Resolve the platform admin token from Secrets Manager (cached)."""
    global _admin_token
    if _admin_token is not None:
        return _admin_token

    if GATEWAY_ADMIN_TOKEN_ARN:
        import boto3

        client = boto3.client(
            "secretsmanager",
            region_name=os.environ.get("AWS_REGION", "us-east-1"),
        )
        resp = client.get_secret_value(SecretId=GATEWAY_ADMIN_TOKEN_ARN)
        _admin_token = resp["SecretString"]
    else:
        _admin_token = os.environ.get("GATEWAY_ADMIN_TOKEN", "")

    return _admin_token


def _resolve_internal_api_key() -> str:
    """Resolve the internal API key from Secrets Manager (cached for Lambda lifetime)."""  # noqa: E501
    global _internal_api_key
    if _internal_api_key is not None:
        return _internal_api_key

    arn = os.environ.get("INTERNAL_API_KEY_ARN", "")
    if arn:
        import boto3

        client = boto3.client(
            "secretsmanager",
            region_name=os.environ.get("AWS_REGION", "us-east-1"),
        )
        resp = client.get_secret_value(SecretId=arn)
        _internal_api_key = resp["SecretString"]
    elif os.environ.get("INTERNAL_API_KEY_PARAMETER_NAME"):
        import boto3

        _internal_api_key = boto3.client(
            "ssm", region_name=os.environ.get("AWS_REGION", "us-east-1")
        ).get_parameter(
            Name=os.environ["INTERNAL_API_KEY_PARAMETER_NAME"], WithDecryption=True
        )["Parameter"]["Value"]
    else:
        _internal_api_key = os.environ.get("BG_INTERNAL_API_KEY", "")

    return _internal_api_key


def _sign_internal_lookup(request):
    """Authenticate canonical read-only lookups at the existing IAM API edge."""
    hostname = urllib.parse.urlsplit(request.full_url).hostname or ""
    if ".execute-api." not in hostname or not hostname.endswith(".amazonaws.com"):
        raise RuntimeError("Gateway lookups require the IAM execute-api endpoint")
    import boto3
    from botocore.auth import SigV4Auth
    from botocore.awsrequest import AWSRequest

    region = hostname.split(".execute-api.", 1)[1].split(".", 1)[0]
    credentials = boto3.Session().get_credentials()
    if credentials is None:
        raise RuntimeError("Gateway lookup execution credentials unavailable")
    signed = AWSRequest(
        method=request.get_method(),
        url=request.full_url,
        data=request.data,
        headers=dict(request.header_items()),
    )
    SigV4Auth(credentials.get_frozen_credentials(), "execute-api", region).add_auth(
        signed
    )
    for name, value in signed.headers.items():
        request.add_header(name, value)
    return request


class _NoLookupRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _open_internal_lookup(request, *, timeout):
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}), _NoLookupRedirect()
    ).open(request, timeout=timeout)


def resolve_user_state(
    provider: str, provider_user_id: str, org_id: str | None = None
) -> dict:
    """Resolve a canonical user, distinguishing "no such link" from "cannot ask".

    Issue #5664 (A10). Returns one of:

        {"state": "resolved", "user": {...}}   # Postgres holds a proven link
        {"state": "not_found"}                 # authoritative gateway 404
        {"state": "ambiguous"}                 # authoritative gateway 409
        {"state": "error", "reason": <str>}    # we could not find out

    Why the split matters here specifically: the webhook resolver holds a DDB row
    that may be stale and carries no provenance of its own. If the gateway
    authoritatively says this identity does not exist, or is not proven, or is
    ambiguous across tenants, then the DDB row must NOT stand in for that answer
    when authority is being granted — that is the permissive legacy fallback this
    issue removes. But if we simply could not reach the gateway, denying would turn
    a gateway blip into a platform-wide refusal, so that case stays fail-open-loud.

    ``not_found`` covers a gateway 404, which is returned both when no link exists
    and when every candidate link is unproven — from the caller's perspective those
    are the same fact: Postgres offers no proven identity here.

    Callers must branch on ``state``; all four results are truthy dicts.
    """
    if not GATEWAY_API_URL:
        logger.warning("GATEWAY_API_URL not set — cannot resolve user via gateway")
        return {"state": USER_ERROR, "reason": "gateway_url_unset"}

    url = f"{GATEWAY_API_URL}/internal/v1/resolve-user"
    body = {
        "provider": provider,
        "provider_user_id": provider_user_id,
    }
    if org_id:
        body["org_id"] = org_id

    headers = {"Content-Type": "application/json"}

    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with _open_internal_lookup(_sign_internal_lookup(req), timeout=10) as resp:
            if resp.status in (200, 201):
                data = json.loads(resp.read().decode("utf-8"))
                return {
                    "state": USER_RESOLVED,
                    "user": {
                        "user_id": data.get("user_id", ""),
                        "org_id": data.get("org_id", ""),
                        "team_id": data.get("team_id", ""),
                        "is_shadow": data.get("is_shadow", False),
                        # "" when the gateway has not been redeployed with the
                        # field. Unknown provenance, NOT proof.
                        "verification_method": data.get("verification_method", ""),
                    },
                }
            # A non-2xx status that did not raise: we do not know the answer.
            return {"state": USER_ERROR, "reason": f"http_{resp.status}"}
    except urllib.error.HTTPError as e:
        if e.code == 409:
            # The gateway refuses to guess which tenant an ambiguous identity
            # belongs to. Authoritative: the data is genuinely ambiguous, not
            # transiently unavailable, so retrying returns the same 409.
            logger.warning(
                "resolve_user_state: 409 ambiguous identity for provider=%s "
                "provider_user_id=%s org_id=%r — declining to resolve",
                provider,
                provider_user_id,
                org_id,
            )
            return {"state": USER_AMBIGUOUS}
        if e.code == 404:
            logger.info(
                "resolve_user_state: 404 for provider=%s provider_user_id=%s",
                provider,
                provider_user_id,
            )
            return {"state": USER_NOT_FOUND}
        logger.error(
            "resolve_user_state HTTP error %d for provider=%s provider_user_id=%s: %s",
            e.code,
            provider,
            provider_user_id,
            e.reason,
        )
        return {"state": USER_ERROR, "reason": f"http_{e.code}"}
    except Exception as e:
        logger.error(
            "resolve_user_state failed for provider=%s provider_user_id=%s: %s",
            provider,
            provider_user_id,
            e,
        )
        return {"state": USER_ERROR, "reason": "request_failed"}


def resolve_user_by_identity(
    provider: str, provider_user_id: str, org_id: str | None = None
) -> dict | None:
    """Call POST /internal/v1/resolve-user to resolve canonical user via Postgres.

    Returns dict with keys {user_id, org_id, team_id, is_shadow,
    verification_method} on success, or None on 404 / error.

    Issue #5664 (A10): retained as the flattened view over
    :func:`resolve_user_state` for callers that only need "did we get a user".
    A caller that must distinguish an authoritative "no proven link" from "we
    could not ask" — which any caller granting authority must — has to use
    ``resolve_user_state`` instead, because both collapse to ``None`` here.

    Issue #5664 (A10): two additions.

    ``org_id`` scopes the lookup to the tenant this webhook delivery is for.
    ``user_identities`` is uniquely indexed per ``(provider, provider_user_id,
    org_id)``, so one external account may legitimately hold rows in several
    tenants; unscoped, the gateway cannot tell which one an event belongs to and
    answers 409 rather than guessing. The installation already tells us the
    tenant, so pass it and the question becomes answerable.

    ``verification_method`` is the provenance of the link the answer rests on.
    Callers that grant authority from this result must check it — see
    ``identity_resolver``'s use of ``PROVEN_METHODS``. It is ``""`` when the
    gateway predates the field, which is unknown provenance, NOT proof.
    """
    result = resolve_user_state(provider, provider_user_id, org_id=org_id)
    return result["user"] if result["state"] == USER_RESOLVED else None


def _emit_installation_resolve_error_metric(reason: str) -> None:
    """Emit ``InstallationResolveError`` when the gateway could not be consulted.

    Issue #4046: the "loud" half of fail-open-but-loud. An ``error`` state means
    we do NOT know whether the installation is a known tenant — a future gate
    (#2724 slice B) fails open on this state, so it must be visible.

    Best-effort: never raises, never blocks the caller.
    """
    try:
        import boto3

        cw = boto3.client(
            "cloudwatch", region_name=os.environ.get("AWS_REGION", "us-east-1")
        )
        cw.put_metric_data(
            Namespace="WebhookIngress",
            MetricData=[
                {
                    "MetricName": "InstallationResolveError",
                    "Dimensions": [{"Name": "Reason", "Value": reason}],
                    "Value": 1,
                    "Unit": "Count",
                }
            ],
        )
    except Exception as e:  # noqa: BLE001
        logger.debug("Failed to emit InstallationResolveError metric: %s", e)


def _installation_error(installation_id: str, reason: str, detail: str = "") -> dict:
    """Build the ``error`` result, logging at WARN and emitting the metric."""
    logger.warning(
        "resolve_installation_by_id UNAVAILABLE for installation_id=%s "
        "reason=%s%s — caller must treat this as 'unknown', NOT as 'not a tenant'",
        installation_id,
        reason,
        f" detail={detail}" if detail else "",
    )
    _emit_installation_resolve_error_metric(reason)
    return {"state": INSTALLATION_ERROR, "reason": reason}


def resolve_installation_by_id(installation_id: str) -> dict:
    """Call POST /internal/v1/resolve-installation to resolve the owning tenant.

    Issue #2769: Postgres is authoritative for the installation_id → tenant
    mapping.

    Issue #4046 (#2724 slice A): returns explicit states instead of
    collapsing everything except success into ``None``::

        {"state": "resolved",  "tenant_id": <org_id>, "created_via": <str>}
        {"state": "revoked"}                            # authoritative gateway 410
        {"state": "not_found"}                          # authoritative gateway 404
        {"state": "error", "reason": <str>}             # we could not find out

    Issue #2724 (slice B): a ``resolved`` result also carries ``created_via``,
    the provenance of the owning organization row. ``created_via`` is ``""`` when
    the gateway predates the field (not yet redeployed) — callers must treat that
    as "unknown", never as "untrusted". Use :func:`installation_gate` rather than
    interpreting these fields directly.

    Installation admission requires ``resolved`` plus ``revocation_checked=True``.
    A 410 is durable denial; 404 is unknown ownership. Missing configuration,
    other HTTP failures, timeouts and malformed responses are ``error`` and also
    deny installation authority. An absent DDB marker cannot prove the gateway
    did not commit a revocation whose marker publication failed.
    Callers must branch on ``state``; all results are truthy dictionaries.
    """
    if not GATEWAY_API_URL:
        return _installation_error(installation_id, "gateway_url_not_configured")

    url = f"{GATEWAY_API_URL}/internal/v1/resolve-installation"
    body = {"installation_id": str(installation_id)}

    headers = {"Content-Type": "application/json"}

    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with _open_internal_lookup(_sign_internal_lookup(req), timeout=10) as resp:
            if resp.status in (200, 201):
                data = json.loads(resp.read().decode("utf-8"))
                tenant_id = data.get("tenant_id", "")
                if tenant_id:
                    return {
                        "state": INSTALLATION_RESOLVED,
                        "tenant_id": tenant_id,
                        "revocation_checked": data.get("revocation_checked") is True,
                        # Issue #2724: "" when the gateway has not been redeployed
                        # with the provenance field yet — the gate fails open on
                        # that, loudly.
                        "created_via": data.get("created_via", ""),
                    }
                # A 200 with no tenant_id is a malformed response, not an
                # authoritative "not a tenant" — the gateway signals that with 404.
                return _installation_error(installation_id, "empty_tenant_id")
            return _installation_error(
                installation_id, "unexpected_status", str(resp.status)
            )
    except urllib.error.HTTPError as e:
        if e.code == 410:
            return {"state": INSTALLATION_REVOKED}
        if e.code == 404:
            logger.info(
                "resolve_installation_by_id: 404 for installation_id=%s "
                "(authoritatively not a known tenant)",
                installation_id,
            )
            return {"state": INSTALLATION_NOT_FOUND}
        return _installation_error(installation_id, f"http_{e.code}", str(e.reason))
    except Exception as e:  # noqa: BLE001
        return _installation_error(installation_id, "transport_error", str(e))


def admit_issue_work(envelope: dict) -> bool:
    """Reserve only the invocation already written by the trusted producer.

    The gateway resolves tenant, issue and owner from protected authority. The
    shared internal API key cannot call this endpoint; use SigV4 transport.
    A transport failure or unknown outcome prevents publication.
    """
    import base64

    import botocore.auth
    import botocore.awsrequest
    import botocore.session

    endpoint = os.environ.get("ADP_AGENT_CONTROL_ENDPOINT", "").rstrip("/")
    parsed = urllib.parse.urlparse(endpoint)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        return False
    invocation = envelope.get("message_id")
    if not invocation:
        return False
    data = json.dumps({"invocation_id": invocation}).encode()
    url = endpoint + "/work/admit"
    try:
        credentials = botocore.session.get_session().get_credentials()
        if credentials is None:
            return False
        region = os.environ.get("AWS_REGION", "us-east-1")
        proof = botocore.awsrequest.AWSRequest(
            method="POST",
            url=f"https://sts.{region}.amazonaws.com/",
            data="Action=GetCallerIdentity&Version=2011-06-15",
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "x-adp-work-invocation": invocation,
            },
        )
        botocore.auth.SigV4Auth(
            credentials.get_frozen_credentials(), "sts", region
        ).add_auth(proof)
        proof_header = base64.b64encode(
            json.dumps({k.lower(): v for k, v in proof.headers.items()}).encode()
        ).decode()
        signed = botocore.awsrequest.AWSRequest(
            method="POST",
            url=url,
            data=data,
            headers={
                "Content-Type": "application/json",
                "X-Adp-Producer-Proof": proof_header,
            },
        )
        botocore.auth.SigV4Auth(
            credentials.get_frozen_credentials(),
            "execute-api",
            os.environ.get("AWS_REGION", "us-east-1"),
        ).add_auth(signed)
        request = urllib.request.Request(
            url, data=data, headers=dict(signed.headers), method="POST"
        )

        # The URL receives signed credentials. Do not inherit a proxy or follow
        # a redirect to another host.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None

        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), NoRedirect()
        )
        with opener.open(request, timeout=10) as response:
            receipt = json.loads(response.read(8193))
            return response.status == 200 and receipt.get("disposition") in {
                "admitted",
                "duplicate",
                "no_issue",
            }
    except Exception:
        logger.warning("Work admission unavailable invocation=%s", invocation)
        return False


def _open_onboarding_enabled() -> bool:
    """Whether this deployment has opted into open onboarding.

    Issue #2724: ``ORG_TENANT_AUTO_CREATE`` is the SINGLE source of truth for
    "is this deployment open-onboarding", read by both the gateway (where it
    lets the unauthenticated install callback create tenant shells) and here
    (where it lets the webhook trust those shells). A second, Lambda-only flag
    was explicitly rejected: two flags controlling one trust decision across two
    deploy units drift silently, and the drift is security-relevant.

    Read per call, not at import — the value must be flippable by an env-only
    Lambda config update (the documented instant rollback) without a code deploy.
    """
    return os.environ.get("ORG_TENANT_AUTO_CREATE", "false").lower() == "true"


# installation_gate() reasons. The two TRUSTED_GATE_REASONS are the only ones
# that mean "we KNOW this is a real ADP tenant" — callers key credential
# provisioning off that set, not off "allowed", because the gate deliberately
# allows several cases it cannot vouch for (see installation_gate's table).
GATE_TRUSTED_PROVENANCE = "trusted_provenance"
GATE_OPEN_ONBOARDING = "open_onboarding"
GATE_PROVENANCE_UNAVAILABLE = "provenance_unavailable"
GATE_UNAVAILABLE = "gate_unavailable"
GATE_NOT_A_KNOWN_TENANT = "not_a_known_tenant"
GATE_SELF_CREATED_SHELL = "self_created_shell"

TRUSTED_GATE_REASONS = frozenset({GATE_TRUSTED_PROVENANCE, GATE_OPEN_ONBOARDING})


def installation_gate(result: dict | None) -> tuple[bool, str]:
    """Decide whether an installation may be auto-registered as a tenant.

    Issue #2724 (slice B): the tenant-existence check ``_auto_register_installation``
    has promised in its docstring since #2769 but never performed. This is the
    single choke point for that decision — both write paths (the handler's
    auto-register and identity_resolver's independent DDB backfill) call it, so
    the gate cannot be circumvented by whichever path happens to fire first.

    Takes a :func:`resolve_installation_by_id` result and returns
    ``(allowed, reason)``:

    ======================================  =======  =========================
    gateway result                          allowed  reason
    ======================================  =======  =========================
    resolved, trusted provenance            True     trusted_provenance
    resolved, install_autocreate, flag on   True     open_onboarding
    resolved, install_autocreate, flag off  False    self_created_shell
    resolved, provenance missing/unknown    True     provenance_unavailable
    not_found (authoritative 404)           False    not_a_known_tenant
    error / None                            True     gate_unavailable
    ======================================  =======  =========================

    **Deny only on an authoritative answer.** ``error`` means we could not reach
    the gateway (SigV4 at the API GW edge, unconfigured URL, cold RDS, timeout) —
    denying on it would turn every gateway blip into "reject all new customer
    installations", which is the exact blast radius this issue's own impact
    analysis puts at the top of the table. Same for an unrecognised or absent
    provenance: a gateway that has not been redeployed with the field yet must
    not brick onboarding. Both fail OPEN and LOUD — the caller emits
    ``AutoRegisterGateUnavailable`` so the window is visible rather than silent.

    Note the asymmetry with ``not_found``: that IS authoritative (the gateway
    looked and no organization claims the installation), so it denies. Legitimate
    first-time installs are unaffected because the browser install-callback
    creates the Postgres row and the DDB identity row itself; a webhook that
    arrives before it has nothing to auto-register on behalf of anyone.
    """
    if not result:
        # Defensive: pre-slice-A callers could see None. Never deny on it.
        return True, GATE_UNAVAILABLE

    state = result.get("state")

    if state == INSTALLATION_REVOKED:
        return False, "installation_revoked"

    if state == INSTALLATION_NOT_FOUND:
        return False, GATE_NOT_A_KNOWN_TENANT

    if state != INSTALLATION_RESOLVED:
        # INSTALLATION_ERROR, or a state this Lambda version does not know.
        return True, GATE_UNAVAILABLE

    created_via = result.get("created_via", "")

    if created_via in TRUSTED_PROVENANCE:
        return True, GATE_TRUSTED_PROVENANCE

    if created_via == CREATED_VIA_INSTALL_AUTOCREATE:
        if _open_onboarding_enabled():
            # Deliberately-open deployment (hackathon/demo): the operator has
            # accepted that anyone who installs the App becomes a tenant.
            return True, GATE_OPEN_ONBOARDING
        return False, GATE_SELF_CREATED_SHELL

    # Empty or unrecognised provenance — gateway not yet redeployed, or a value
    # this Lambda version predates. Unknown is not untrusted.
    return True, GATE_PROVENANCE_UNAVAILABLE


def post_provenance(
    actor_user_id: str,
    triggered_by: str | None,
    root_human_id: str,
    is_human_rooted: bool,
    action_kind: str,
    source_event: dict,
    correlation_id: str,
    org_id: str,
    parent_invocation_id: str | None = None,
) -> str | None:
    """POST to gateway /internal/v1/provenance.

    Returns provenance_id on success, None on failure.

    Fail-soft: on gateway 5xx or timeout, log + emit metric, don't crash.
    Uses 5s timeout (non-blocking to webhook flow).

    # TODO: Once Phase 2-d ships and consumers rely on provenance rows,
    # evaluate fail-hard or circuit-breaker for write failures.
    """
    if not GATEWAY_API_URL:
        logger.warning("GATEWAY_API_URL not set — cannot post provenance")
        return None

    url = f"{GATEWAY_API_URL}/internal/v1/provenance"
    body = {
        "actor_user_id": actor_user_id,
        "triggered_by": triggered_by,
        "root_human_id": root_human_id,
        "is_human_rooted": is_human_rooted,
        "action_kind": action_kind,
        "source_event": source_event,
        "correlation_id": correlation_id,
        "org_id": org_id,
        "parent_invocation_id": parent_invocation_id,
    }

    api_key = _resolve_internal_api_key()
    if not api_key:
        logger.warning("Internal API key not available — cannot post provenance")
        return None

    headers = {
        "Content-Type": "application/json",
        "X-Internal-Api-Key": api_key,
    }

    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            if resp.status == 201:
                data = json.loads(resp.read().decode("utf-8"))
                return data.get("id")
            logger.warning(
                "post_provenance unexpected status %d for correlation=%s",
                resp.status,
                correlation_id,
            )
            return None
    except urllib.error.HTTPError as e:
        logger.error(
            "post_provenance HTTP error %d for correlation=%s: %s",
            e.code,
            correlation_id,
            e.reason,
        )
        return None
    except Exception as e:
        logger.error(
            "post_provenance failed for correlation=%s: %s",
            correlation_id,
            e,
        )
        return None


def auto_provision_user(
    org_id: str,
    github_id: int,
    github_login: str,
) -> bool:
    """Call Gateway admin API to create a minimal user with GitHub identity.

    POST /api/admin/identity/organizations/{org_id}/users

    Returns True if the user was created (or already exists), False on error.
    """
    if not GATEWAY_API_URL:
        logger.error("GATEWAY_API_URL env var is not set — cannot auto-provision")
        return False

    url = f"{GATEWAY_API_URL}/api/admin/identity/organizations/{org_id}/users"
    body = {
        "email": f"{github_login}@github.auto-provision.adp.internal",
        "name": github_login,
        "role": "developer",
        "identities": [
            {
                "provider": "github",
                "provider_user_id": str(github_id),
                "provider_username": github_login,
            }
        ],
        "send_invite": False,
    }

    token = _resolve_admin_token()
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }

    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            status = resp.status
            if status in (200, 201):
                logger.info(
                    "Auto-provisioned user: github_login=%s org_id=%s",
                    github_login,
                    org_id,
                )
                return True
            logger.warning(
                "Auto-provision returned unexpected status %d for %s",
                status,
                github_login,
            )
            return False
    except urllib.error.HTTPError as e:
        # 409 = user already exists — treat as success
        if e.code == 409:
            logger.info(
                "Auto-provision: user already exists github_login=%s org_id=%s",
                github_login,
                org_id,
            )
            return True
        logger.error(
            "Auto-provision HTTP error %d for github_login=%s: %s",
            e.code,
            github_login,
            e.reason,
        )
        return False
    except Exception as e:
        logger.error("Auto-provision failed for github_login=%s: %s", github_login, e)
        return False
