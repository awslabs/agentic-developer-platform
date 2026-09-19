"""Own-pod task transport before bootstrap; no shared queue credentials."""

from __future__ import annotations

import json
import os
import time
from urllib.parse import urlparse

import botocore.auth
import botocore.awsrequest
import botocore.session
import requests

from lib.run_identity import (
    CONTROL_ENDPOINT_ENV,
    WORKLOAD_HEADER,
    RunIdentityError,
    read_workload_token,
)


class TaskGatewayError(Exception):
    pass


def _post(action: str) -> dict:
    if action not in {"acquire", "heartbeat", "ack"}:
        raise TaskGatewayError("unsupported task operation")
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
        raise TaskGatewayError("task service endpoint unavailable")
    url = base + "/task/" + action
    try:
        from adp_trigger.transport_identity import gateway_signing_region, worker_credentials

        credentials = worker_credentials(botocore.session.get_session())
        if credentials is None:
            raise TaskGatewayError("worker transport identity unavailable")
        # Only a verified workload is available before bootstrap. The gateway
        # permanently assigns that pod one task; callers supply no task selector.
        request = botocore.awsrequest.AWSRequest(
            method="POST",
            url=url,
            data=b"{}",
            headers={"Content-Type": "application/json", WORKLOAD_HEADER: read_workload_token()},
        )
        botocore.auth.SigV4Auth(
            credentials.get_frozen_credentials(), "execute-api", gateway_signing_region(url)
        ).add_auth(request)
        with requests.Session() as http:
            http.trust_env = False
            with http.post(
                url,
                data=b"{}",
                headers=dict(request.headers),
                timeout=25,
                allow_redirects=False,
                stream=True,
            ) as response:
                if response.status_code == 409:
                    raise TaskGatewayError("busy")
                if response.status_code != 200:
                    raise TaskGatewayError("task service refused operation")
                raw = response.raw.read(1024 * 1024 + 1, decode_content=True)
                if len(raw) > 1024 * 1024:
                    raise TaskGatewayError("task response too large")
                value = json.loads(raw)
                if not isinstance(value, dict):
                    raise TaskGatewayError("invalid task response")
                return value
    except (RunIdentityError, requests.RequestException, ValueError, OSError):
        raise TaskGatewayError("task service unavailable") from None


def own_task() -> str | None:
    # A concurrent/lost receive response can leave a short server reservation.
    # Retry only busy, with a fresh workload proof; never fall back to SQS.
    for attempt in range(5):
        try:
            result = _post("acquire")
            break
        except TaskGatewayError as error:
            if str(error) != "busy" or attempt == 4:
                raise
            time.sleep(8)
    if set(result) != {"body"} or (
        result["body"] is not None and not isinstance(result["body"], str)
    ):
        raise TaskGatewayError("invalid task response")
    return result["body"]


def heartbeat_task() -> None:
    if _post("heartbeat") != {"accepted": True}:
        raise TaskGatewayError("task heartbeat was not accepted")


def acknowledge_task() -> None:
    if _post("ack") != {"accepted": True}:
        raise TaskGatewayError("task acknowledgement was not accepted")
