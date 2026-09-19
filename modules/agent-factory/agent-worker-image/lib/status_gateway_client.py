"""Authenticated status and control-registration writes through the gateway (#5028 AC4).

## Why this exists

``invocation_status.py`` writes the webhook-events row directly with the pod's IAM
credentials. Those credentials belong to the **shared platform worker role**: every
agent worker assumes the same one, and the grant that permits the write
(``DynamoDBWebhookEventsUpdate``) allows ``dynamodb:UpdateItem`` on the whole table
with no key or attribute condition. The row key ``(event_id, arrived_at)`` is
supplied by the caller, so any worker can write any other worker's row — including
its ``control_address`` and ``control_token``, which redirects that run's control
channel to a listener of the attacker's choosing.

No IAM policy can fix that, because IAM cannot express "only the row belonging to
the run this pod is actually executing" — the row key is data, and the identity that
would have to be compared against it does not exist at the IAM layer. So the write
moves behind the gateway, which *does* know: it verifies the run credential, checks
the presenting pod, and derives the row key from protected state the worker cannot
write. Once every write goes through here, the worker's table grant can be removed.

## What is sent, and what is deliberately not

Sent: the field *values* (status, summary, the control token) plus two proofs — the
short-lived run credential from :mod:`lib.run_identity` and a **freshly read**
projected workload token. Not sent: ``event_id``, ``arrived_at``, ``tenant_id``, or
the control address. There is no parameter for them, which is the point: a request
that cannot name a row cannot name someone else's.

The workload token is re-read from its projected file on every request rather than
cached, because the projection is refreshed in place by the kubelet and a cached
copy is a copy that expires while the file next to it is valid.

## No DynamoDB fallback

If authentication or transport fails here, the caller is told. It does **not** fall
back to writing DynamoDB directly. A fallback would mean the unconditioned IAM
grant must stay in place to serve it, which is the vulnerability this replaces —
and it would be exercised precisely when something is already wrong. A failed
status write degrades a dashboard; a retained table-wide write grant is a
cross-tenant control-channel hijack.
"""

from __future__ import annotations

import json
import logging
import os
from urllib.parse import urlparse

import botocore.auth
import botocore.awsrequest
import botocore.session
import requests

from lib.run_identity import (
    CREDENTIAL_FILE_ENV,
    WORKLOAD_HEADER,
    RunIdentityError,
    read_workload_token,
)

logger = logging.getLogger(__name__)

CONTROL_ENDPOINT_ENV = "ADP_AGENT_CONTROL_ENDPOINT"
AUTHORITY_ENABLED_ENV = "ADP_AGENT_AUTHORITY_ENABLED"
CREDENTIAL_HEADER = "X-Adp-Run-Credential"

_TIMEOUT_SECONDS = 10
_MAX_RESPONSE_BYTES = 8192
# The run credential is a bearer token; it must never be logged, and its file is
# read fresh per request because the refresh thread replaces it atomically.
_MAX_CREDENTIAL_BYTES = 4096


class StatusGatewayError(Exception):
    """The authenticated write could not be completed.

    Carries no response body and no token material: this is raised into fail-soft
    callers that log it, and a gateway refusal reason is not theirs to disclose.
    """


def authority_enabled() -> bool:
    """True when writes must go through the gateway.

    Defaults to **false**, so a deployment that has not enabled agent authority
    keeps the existing direct-write behaviour byte for byte. The flag is read on
    every call rather than at import so a test (and a rollback) does not depend on
    module import order.
    """
    return os.environ.get(AUTHORITY_ENABLED_ENV, "false").lower() == "true"


def _base_url() -> str:
    """The validated ``/self`` base for this run's own writes.

    Validated the same way :class:`lib.run_identity.RunIdentitySession` validates
    it, and for the same reason: this URL receives a bearer credential on every
    request, so a plaintext scheme, embedded userinfo or an attacker-appended query
    would each be a way to redirect or capture it.
    """
    base = os.environ.get(CONTROL_ENDPOINT_ENV, "").rstrip("/")
    parsed = urlparse(base)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise StatusGatewayError("agent authority endpoint is not configured")
    return base + "/self"


def _read_credential() -> str:
    """Read the current run credential from the file the refresh thread maintains.

    Read per request, never held: :meth:`RunIdentitySession.refresh` replaces this
    file via ``os.replace`` as the credential rotates, so a value cached at startup
    would keep presenting a superseded epoch until the pod exited.
    """
    path = os.environ.get(CREDENTIAL_FILE_ENV, "")
    if not path:
        raise StatusGatewayError("run credential is unavailable")
    try:
        with open(path, "rb") as source:
            raw = source.read(_MAX_CREDENTIAL_BYTES + 1)
    except OSError:
        raise StatusGatewayError("run credential is unavailable") from None
    token = raw.decode("ascii", errors="ignore").strip()
    if not token or len(raw) > _MAX_CREDENTIAL_BYTES or not token.startswith("adpr1."):
        raise StatusGatewayError("run credential is unavailable")
    return token


def _post(path: str, body: dict, *, success_statuses: tuple[int, ...] = (200,)) -> dict:
    """Send one signed, authenticated request and return its parsed response.

    Three layers of authentication, each covering a different gap:

    - **SigV4** proves the caller is a platform worker at all (transport).
    - **The run credential** proves *which run and attempt* is calling. SigV4
      cannot: every worker shares the role.
    - **The workload token** proves *which pod* is presenting that credential,
      which is what makes a leaked credential useless elsewhere.
    """
    return _post_bytes(path, json.dumps(body).encode(), content_type="application/json", success_statuses=success_statuses)


def _post_bytes(path: str, data: bytes, *, content_type: str, success_statuses: tuple[int, ...] = (200,), timeout_seconds: int = _TIMEOUT_SECONDS) -> dict:
    url = _base_url() + path
    try:
        workload_token = read_workload_token()
    except RunIdentityError:
        raise StatusGatewayError("workload identity is unavailable") from None

    session = botocore.session.get_session()
    from adp_trigger.transport_identity import gateway_signing_region, worker_credentials

    credentials = worker_credentials(session)
    if credentials is None:
        raise StatusGatewayError("worker transport identity unavailable")

    headers = {
        "Content-Type": content_type,
        WORKLOAD_HEADER: workload_token,
        CREDENTIAL_HEADER: _read_credential(),
    }
    signed = botocore.awsrequest.AWSRequest(method="POST", url=url, data=data, headers=headers)
    botocore.auth.SigV4Auth(
        credentials.get_frozen_credentials(),
        "execute-api",
        gateway_signing_region(url),
    ).add_auth(signed)

    try:
        with requests.Session() as http:
            # trust_env=False: no proxy from the environment. A proxy on this path
            # would see the run credential and the control token in plaintext.
            http.trust_env = False
            with http.post(
                url,
                data=data,
                headers=dict(signed.headers),
                timeout=timeout_seconds,
                allow_redirects=False,
                stream=True,
            ) as response:
                if response.status_code not in success_statuses:
                    # The status code is safe to log; the body is not — it is the
                    # gateway's refusal reason, and the gateway deliberately keeps
                    # those uniform to the caller.
                    raise StatusGatewayError(
                        f"gateway refused the write (status {response.status_code})"
                    )
                raw = response.raw.read(_MAX_RESPONSE_BYTES + 1, decode_content=True)
                if len(raw) > _MAX_RESPONSE_BYTES:
                    raise StatusGatewayError("gateway response was oversized")
                return json.loads(raw or b"{}")
    except StatusGatewayError:
        raise
    except (requests.RequestException, ValueError, OSError):
        # Never let the underlying exception through: request exceptions stringify
        # to include the full URL and can include headers.
        raise StatusGatewayError("agent authority service unavailable") from None


def record_status(status: str, fields: dict[str, str]) -> None:
    """Write a status transition for this run.

    No row key is passed. The gateway derives it from the protected execution
    record, so this cannot address another run even if the caller wanted to.
    """
    payload = {"status": status}
    payload.update({name: value for name, value in fields.items() if value})
    _post("/status", payload)


def register_control(*, token: str, token_expires_at: str) -> int:
    """Register this pod's control listener and return the assigned generation.

    The address and port are **not** parameters. The gateway uses the IP of the
    pod it verified through TokenReview, because a caller-supplied address is
    exactly the control-channel redirect this path removes.

    The generation still comes from the row's atomic counter, so it continues to
    differ between attempts; the gateway makes a retry idempotent, returning the
    same generation rather than incrementing again.
    """
    result = _post(
        "/control/registration",
        {"control_token": token, "control_token_expires_at": token_expires_at},
    )
    generation = result.get("control_generation")
    if type(generation) is not int or generation < 1:
        # A guessed generation would make the listener reject every command the
        # gateway sends, which is indistinguishable from an attack.
        raise StatusGatewayError("gateway returned no usable control generation")
    return generation


def post_self(path: str, body: dict) -> dict:
    """Send an authenticated write to this run's own ``/self`` surface (#5301).

    The public form of :func:`_post`, for callers whose payload has no
    purpose-built helper here. Same three-layer authentication and the same
    property that matters: the caller never names the row it is writing, so a
    ``/self`` path can only ever address the execution the run credential and
    workload token together identify.

    Exposed so :mod:`lib.pr_binding` does not import the private ``_post``, which
    would put the authentication contract's only enforcement point behind a name
    nothing is obliged to keep stable.
    """
    return _post(path, body, success_statuses=(200, 201))


def clear_control(generation: int) -> None:
    """Remove this attempt's control registration at teardown.

    The generation is sent so the gateway can refuse a late teardown from a
    superseded attempt: clearing a newer attempt's registration would silently
    remove control from a run that is still going.
    """
    _post("/control/registration/clear", {"control_generation": generation})


def upload_transcript(content: str) -> str:
    """Archive only this run's transcript; no direct S3 fallback on refusal."""
    import hashlib

    data = content.encode("utf-8")
    if not authority_enabled() or not 0 < len(data) <= 8 * 1024 * 1024:
        raise StatusGatewayError("transcript upload unavailable (maximum 8 MiB)")
    result = _post_bytes("/artifacts/transcript", data, content_type="application/octet-stream", timeout_seconds=35)
    key = result.get("key")
    if not isinstance(key, str) or not key.startswith("runs/") or result.get("sha256") != hashlib.sha256(data).hexdigest():
        raise StatusGatewayError("invalid transcript upload receipt")
    return key
