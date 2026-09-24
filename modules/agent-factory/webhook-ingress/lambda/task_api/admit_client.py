"""Producer-authenticated forwarding to the gateway task-admission adapter.

The internal call carries three independent things:

* ``Authorization`` — the ingress role's own SigV4 signature, proving which
  AWS principal is calling ``execute-api``.
* ``X-Adp-Task-Caller-Token`` — the *external* caller's bearer token, which the
  gateway validates with the same canonical-principal service used by task
  reads and controls. This Lambda never interprets it and never resolves
  identity itself; it holds no SQL connection and no identity cache, so there
  is no second identity directory that could disagree with the gateway's.
* ``X-Adp-Producer-Proof`` — a signed STS attestation bound to *this exact
  request*, extending the existing pattern in
  ``common/persona_model_client.py``. Because the binding covers the method,
  route, token digest, idempotency key and exact request bytes, a proof
  captured from one submission cannot admit a different one.

Any transport failure, timeout, redirect or response this module cannot fully
validate raises ``TaskApiError`` — never a partial success. Because a response
can be lost after a durable commit, that refusal reports an unknown outcome and
requires a retry with the original idempotency key.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request

from . import contract, errors

#: The gateway is only reachable over its private ``execute-api`` internal
#: route. Anything else (plaintext, a userinfo or query component, an
#: arbitrary host) is a readiness failure rather than a reason to relax
#: authentication.
_ENDPOINT_PATTERN = re.compile(
    r"^https://[a-z0-9]+\.execute-api\.([a-z0-9-]+)\.amazonaws\.com(?:\.cn)?"
    r"(/[A-Za-z0-9_-]+)?$"
)

#: API Gateway gives the Lambda 29 seconds; admission is a short call and a
#: slow gateway must surface as retryable unavailability, not a hung request.
_TOTAL_BUDGET_SECONDS = 10.0
_MAX_RESPONSE_BYTES = 65536

_STS_BODY = "Action=GetCallerIdentity&Version=2011-06-15"


def _endpoint() -> tuple[str, str]:
    raw = os.environ.get("ADP_TASK_ADMIT_ENDPOINT", "").rstrip("/")
    match = _ENDPOINT_PATTERN.fullmatch(raw)
    if not match:
        raise errors.prerequisite_unavailable()
    return raw, match.group(1)


def binding_digest(
    *, method: str, route: str, caller_token: str, idempotency_key: str, body: bytes
) -> str:
    """Bind the producer proof to this exact admission request.

    The five components are length-delimited under a version tag rather than
    concatenated, so no combination of field values can be rearranged into the
    same digest (for example a token ending in a key prefix cannot impersonate
    a different token/key pair).

    The token is included only as its own digest, so the proof never carries
    or reveals the caller's credential.
    """
    parts = [
        contract.PROOF_BINDING_VERSION.encode(),
        method.encode(),
        route.encode(),
        hashlib.sha256(caller_token.encode()).hexdigest().encode(),
        idempotency_key.encode(),
        body,
    ]
    encoded = b"".join(len(p).to_bytes(8, "big") + p for p in parts)
    return hashlib.sha256(encoded).hexdigest()


def _frozen_credentials():
    import botocore.session

    credentials = botocore.session.get_session().get_credentials()
    if credentials is None:
        raise errors.prerequisite_unavailable()
    return credentials.get_frozen_credentials()


def _producer_proof(frozen, region: str, binding: str) -> str:
    import botocore.auth
    import botocore.awsrequest

    proof = botocore.awsrequest.AWSRequest(
        method="POST",
        url=f"https://sts.{region}.amazonaws.com/",
        data=_STS_BODY,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            contract.PROOF_BINDING_HEADER: binding,
        },
    )
    botocore.auth.SigV4Auth(frozen, "sts", region).add_auth(proof)
    return base64.b64encode(
        json.dumps(
            {k.lower(): v for k, v in proof.headers.items()},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).decode()


def admit(
    *, submit: dict, idempotency_key: str, caller_token: str, request_body: bytes
) -> dict:
    """Forward an admission request and return the gateway's receipt.

    Returns the validated ``submit_response`` receipt. Raises ``TaskApiError``
    for every other outcome, including an unknown one.
    """
    import botocore.auth
    import botocore.awsrequest

    started = time.monotonic()
    endpoint, region = _endpoint()

    payload = {
        "schema_version": contract.SCHEMA_VERSION,
        "submit": submit,
        "idempotency_key": idempotency_key,
        "caller_token": caller_token,
        "producer_proof": "",
    }

    frozen = _frozen_credentials()
    binding = binding_digest(
        method=contract.SUBMIT_METHOD,
        route=contract.SUBMIT_RESOURCE,
        caller_token=caller_token,
        idempotency_key=idempotency_key,
        body=request_body,
    )
    proof = _producer_proof(frozen, region, binding)
    payload["producer_proof"] = proof
    data = json.dumps(payload, separators=(",", ":")).encode()

    url = endpoint + contract.ADMIT_ROUTE
    signed = botocore.awsrequest.AWSRequest(
        method="POST",
        url=url,
        data=data,
        headers={
            "Content-Type": "application/json",
            contract.PRODUCER_PROOF_HEADER: proof,
            contract.CALLER_TOKEN_HEADER: caller_token,
        },
    )
    botocore.auth.SigV4Auth(frozen, "execute-api", region).add_auth(signed)

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        # The signed credentials travel in headers; following a redirect would
        # hand them, and the caller's token, to another host.
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    remaining = _TOTAL_BUDGET_SECONDS - (time.monotonic() - started)
    if remaining <= 0:
        raise errors.prerequisite_unavailable()

    request = urllib.request.Request(
        url, data=data, headers=dict(signed.headers), method="POST"
    )
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), _NoRedirect()
    )
    try:
        with opener.open(request, timeout=remaining) as response:
            status = response.status
            raw = response.read(_MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        status = exc.code
        try:
            raw = exc.read(_MAX_RESPONSE_BYTES + 1)
        except Exception:  # noqa: BLE001 - an unreadable error body is simply absent
            raw = b""
    except Exception:  # noqa: BLE001 - see below; the catch must be total
        # Deliberately broad. Any failure reaching the gateway — DNS, TLS,
        # timeout, socket reset, or a botocore error while signing — means the
        # outcome cannot be proven. Narrowing this would let an unanticipated
        # exception escape instead of telling the caller to retry the same
        # idempotent intent. The exception is dropped rather than chained so no transport
        # detail or credential can reach a log or response.
        raise errors.prerequisite_unavailable() from None

    if len(raw) > _MAX_RESPONSE_BYTES:
        raise errors.prerequisite_unavailable()
    try:
        receipt = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise errors.prerequisite_unavailable() from None

    if status == 202:
        return receipt
    raise _relay(status, receipt)


def _relay(status: int, receipt: object) -> errors.TaskApiError:
    """Relay a gateway refusal without inventing or widening it.

    The gateway owns identity, scope, ownership and idempotency decisions, so
    its refusal code is passed through when it is one this route may return.
    An unrecognised refusal becomes unavailable rather than being guessed at —
    a wrong denial code is a wrong answer, not a safe default.
    """
    if not isinstance(receipt, dict):
        return errors.prerequisite_unavailable()
    code = receipt.get("code")
    if code not in contract.ERROR_STATUS or contract.ERROR_STATUS[code] != status:
        return errors.prerequisite_unavailable()
    messages = {
        "invalid_request": "The task submission request is invalid.",
        "invalid_credential": "A valid access token is required.",
        "disallowed_scope": "The credential is not authorized to submit tasks.",
        "disallowed_persona": "The requested task persona is not allowed.",
        "not_found": "No such resource.",
        "idempotency_conflict": "The Idempotency-Key was used for another request.",
        "payload_too_large": "The task submission is too large.",
        "rate_limited": "The task submission rate limit was reached.",
        "queue_full": "Task capacity is currently full.",
        "prerequisite_unavailable": (
            "Task submission outcome is unavailable; retry with the same "
            "Idempotency-Key."
        ),
    }
    retry_after_ms = receipt.get("retry_after_ms")
    if (
        not isinstance(retry_after_ms, int)
        or isinstance(retry_after_ms, bool)
        or retry_after_ms < 0
    ):
        retry_after_ms = None
    return errors.TaskApiError(code, messages[code], retry_after_ms=retry_after_ms)
