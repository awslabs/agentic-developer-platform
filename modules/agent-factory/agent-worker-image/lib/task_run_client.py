"""Authenticated gateway calls for the Task API host runtime."""

from __future__ import annotations

import base64
import json
import os
from urllib.parse import urlparse

import botocore.auth
import botocore.awsrequest
import botocore.session
import requests
from urllib3.exceptions import HTTPError as Urllib3HTTPError

from lib.run_identity import CONTROL_ENDPOINT_ENV, WORKLOAD_HEADER, read_workload_token

RUN_CREDENTIAL_HEADER = "X-Adp-Run-Credential"
_MAX_RESPONSE_BYTES = 1024 * 1024
_ACTIONS = frozenset(
    {
        "bootstrap",
        "attempt",
        "report",
        "turn",
        "model",
        "control",
        "artifact",
        "finalize",
        "settlement",
    }
)


class TaskRunClientError(Exception):
    """A task-scoped operation was unavailable or refused."""


class TaskRunClientUnavailable(TaskRunClientError):
    """Transport failure with an unknown durable operation outcome."""


def _decode_segment(value: str) -> dict:
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        result = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        raise TaskRunClientError("workload identity unavailable") from None
    if not isinstance(result, dict):
        raise TaskRunClientError("workload identity unavailable")
    return result


def workload_identity(token: str | None = None) -> dict:
    """Return non-authoritative workload claims for the bootstrap request body.

    The same projected token is sent in the authenticated header. The gateway
    verifies it with TokenReview and compares these claims; decoding here never
    turns the body into authority.
    """

    token = token or read_workload_token()
    parts = token.split(".")
    if len(parts) != 3:
        raise TaskRunClientError("workload identity unavailable")
    claims = _decode_segment(parts[1])
    kubernetes = claims.get("kubernetes.io")
    if not isinstance(kubernetes, dict):
        raise TaskRunClientError("workload identity unavailable")
    pod = kubernetes.get("pod")
    namespace = kubernetes.get("namespace")
    if not isinstance(pod, dict) or not isinstance(namespace, str) or not namespace:
        raise TaskRunClientError("workload identity unavailable")
    uid = pod.get("uid")
    name = pod.get("name")
    if not isinstance(uid, str) or not uid:
        raise TaskRunClientError("workload identity unavailable")
    result = {"pod_uid": uid, "namespace": namespace}
    if isinstance(name, str) and name:
        result["pod_name"] = name
    return result


class TaskRunClient:
    """Strict task-route client; credentials remain in the host process only."""

    def __init__(self, *, timeout: int = 25) -> None:
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
            raise TaskRunClientError("task service endpoint unavailable")
        self._base = base
        self._timeout = timeout
        self._run_credential: str | None = None

    def _post(
        self,
        action: str,
        body: dict,
        *,
        run_bound: bool,
        workload_token: str | None = None,
    ) -> dict:
        if action not in _ACTIONS:
            raise TaskRunClientError("unsupported task operation")
        if run_bound and not self._run_credential:
            raise TaskRunClientError("task run credential unavailable")
        url = f"{self._base}/task/{action}"
        data = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            WORKLOAD_HEADER: workload_token or read_workload_token(),
        }
        if run_bound:
            headers[RUN_CREDENTIAL_HEADER] = self._run_credential or ""
        try:
            from adp_trigger.transport_identity import gateway_signing_region, worker_credentials

            credentials = worker_credentials(botocore.session.get_session())
            if credentials is None:
                raise TaskRunClientError("worker transport identity unavailable")
            request = botocore.awsrequest.AWSRequest(
                method="POST", url=url, data=data, headers=headers
            )
            botocore.auth.SigV4Auth(
                credentials.get_frozen_credentials(),
                "execute-api",
                gateway_signing_region(url),
            ).add_auth(request)
            with requests.Session() as http:
                http.trust_env = False
                with http.post(
                    url,
                    data=data,
                    headers=dict(request.headers),
                    timeout=self._timeout,
                    allow_redirects=False,
                    stream=True,
                ) as response:
                    if response.status_code >= 500:
                        raise TaskRunClientUnavailable("task service outcome unavailable")
                    if response.status_code != 200:
                        raise TaskRunClientError("task service refused operation")
                    raw = response.raw.read(_MAX_RESPONSE_BYTES + 1, decode_content=True)
                    if len(raw) > _MAX_RESPONSE_BYTES:
                        raise TaskRunClientError("task response too large")
                    value = json.loads(raw)
                    if not isinstance(value, dict):
                        raise TaskRunClientError("invalid task response")
                    return value
        except TaskRunClientError:
            raise
        except (
            requests.RequestException,
            Urllib3HTTPError,
            UnicodeDecodeError,
            ValueError,
            OSError,
            json.JSONDecodeError,
        ):
            raise TaskRunClientUnavailable("task service unavailable") from None

    def bootstrap(self, body: dict) -> dict:
        token = read_workload_token()
        response = self._post(
            "bootstrap",
            {**body, "workload": workload_identity(token)},
            run_bound=False,
            workload_token=token,
        )
        credential = response.get("run_credential")
        if not isinstance(credential, str) or not credential:
            raise TaskRunClientError("invalid task bootstrap response")
        self._run_credential = credential
        return response

    def attempt(self, body: dict) -> dict:
        return self._post("attempt", body, run_bound=True)

    def report(self, body: dict) -> dict:
        return self._post("report", body, run_bound=True)

    def turn(self, body: dict) -> dict:
        return self._post("turn", body, run_bound=True)

    def model(self, body: dict) -> dict:
        return self._post("model", body, run_bound=True)

    def control(self, body: dict) -> dict:
        return self._post("control", body, run_bound=True)

    def artifact(self, body: dict) -> dict:
        return self._post("artifact", body, run_bound=True)

    def finalize(self, body: dict) -> dict:
        return self._post("finalize", body, run_bound=True)

    def settlement(self, body: dict) -> dict:
        token = read_workload_token()
        return self._post(
            "settlement",
            {**body, "workload": workload_identity(token)},
            run_bound=False,
            workload_token=token,
        )

    def clear_credential(self) -> None:
        self._run_credential = None
