"""Authenticated gateway calls for the Task API host runtime."""

from __future__ import annotations

import base64
import copy
from datetime import datetime
import json
import os
import re
import threading
import time
from urllib.parse import urlparse

import botocore.auth
import botocore.awsrequest
import botocore.session
import requests
from urllib3.exceptions import HTTPError as Urllib3HTTPError

from lib.run_identity import CONTROL_ENDPOINT_ENV, WORKLOAD_HEADER, read_workload_token

CYBER_TOOLS_ENDPOINT_ENV = "ADP_CYBER_TOOLS_ENDPOINT"
RUN_CREDENTIAL_HEADER = "X-Adp-Run-Credential"
_MAX_RESPONSE_BYTES = 1024 * 1024
_ACTIONS = frozenset(
    {
        "bootstrap",
        "attempt",
        "report",
        "turn",
        "model",
        "cyber",
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

    def __init__(self, *, timeout: int = 25, clock=time.time) -> None:
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
        self._cyber_endpoint = os.environ.get(CYBER_TOOLS_ENDPOINT_ENV, "")
        self._base = base
        self._timeout = timeout
        self._run_credential: str | None = None
        self._clock = clock
        self._credential_lock = threading.RLock()
        self._bootstrap_body = None
        self._binding = None
        self._credential_expiry = 0.0
        self._deadline = 0.0
        self._stopping = False

    def _cyber_url(self) -> str:
        endpoint = self._cyber_endpoint
        try:
            parsed = urlparse(endpoint)
            valid = (
                parsed.scheme == "https"
                and bool(parsed.hostname)
                and parsed.username is None
                and parsed.password is None
                and parsed.port in (None, 443)
                and not parsed.query
                and not parsed.fragment
                and "?" not in endpoint
                and "#" not in endpoint
                and not any(character.isspace() for character in endpoint)
                and bool(re.fullmatch(r"(?:/[A-Za-z0-9_-]+)*/tools/cyber", parsed.path))
            )
        except ValueError:
            valid = False
        if not valid:
            raise TaskRunClientError("cyber tools endpoint unavailable")
        return endpoint

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
        if run_bound:
            self._renew_for(action, body)
        if run_bound and not self._run_credential:
            raise TaskRunClientError("task run credential unavailable")
        # The target is selected only from host configuration, never a child
        # operation body. Missing cyber service configuration must not fall back
        # to the retired gateway domain broker route.
        url = self._cyber_url() if action == "cyber" else f"{self._base}/task/{action}"
        data = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=action != "model").encode("utf-8")
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
                        raise TaskRunClientUnavailable(f"task {action} outcome unavailable (HTTP {response.status_code})")
                    expected_status = 201 if action == "artifact" and body.get("operation") != "read" else 200
                    if response.status_code != expected_status:
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

    def _accept_bootstrap(self, body, response, *, renewal):
        try:
            binding = {key: response[key] for key in
                       ("task_id", "invocation_id", "generation", "persona", "deadline_at")}
            if (response.get("schema_version") != "1.0"
                    or binding["task_id"] != body["task_id"]
                    or binding["invocation_id"] != body["invocation_id"]
                    or type(binding["generation"]) is not int or binding["generation"] < 1
                    or (renewal and binding != self._binding)):
                raise ValueError("binding")
            expiry_time = datetime.fromisoformat(response["run_credential_expires_at"].replace("Z", "+00:00"))
            deadline_time = datetime.fromisoformat(binding["deadline_at"].replace("Z", "+00:00"))
            if expiry_time.tzinfo is None or deadline_time.tzinfo is None:
                raise ValueError("timezone")
            expiry, deadline = expiry_time.timestamp(), deadline_time.timestamp()
            credential = response["run_credential"]
            now = self._clock()
            if (not isinstance(credential, str) or not credential or
                    not now < expiry <= min(deadline, now + 900)):
                raise ValueError("expiry")
        except (KeyError, TypeError, ValueError, AttributeError):
            raise TaskRunClientError("invalid task bootstrap binding or expiry") from None
        self._binding, self._deadline = binding, deadline
        self._run_credential, self._credential_expiry = credential, expiry

    def _bootstrap(self, body, *, renewal):
        token = read_workload_token()
        response = self._post("bootstrap", {**body, "workload": workload_identity(token)},
                              run_bound=False, workload_token=token)
        self._accept_bootstrap(body, response, renewal=renewal)
        return response

    def bootstrap(self, body: dict) -> dict:
        with self._credential_lock:
            if self._stopping or (self._binding is not None and self._clock() >= self._deadline):
                raise TaskRunClientError("task no longer admits credential renewal")
            if self._bootstrap_body is not None and body != self._bootstrap_body:
                raise TaskRunClientError("task bootstrap identity changed")
            response = self._bootstrap(body, renewal=self._binding is not None)
            self._bootstrap_body = copy.deepcopy(body)
            return response

    def _renew_for(self, action, body):
        stop_only = action == "control" or (action == "cyber" and body.get("operation") == "cancel_jobs") or (
            action == "finalize" and body.get("outcome") != "completed")
        if stop_only:
            return
        with self._credential_lock:
            if self._bootstrap_body is None:
                return
            if self._stopping or self._clock() >= self._deadline:
                raise TaskRunClientError("task no longer admits credential renewal")
            if self._clock() < self._credential_expiry - 60:
                return
            self._bootstrap(self._bootstrap_body, renewal=True)

    def attempt(self, body: dict) -> dict:
        return self._post("attempt", body, run_bound=True)

    def report(self, body: dict) -> dict:
        return self._post("report", body, run_bound=True)

    def turn(self, body: dict) -> dict:
        return self._post("turn", body, run_bound=True)

    def model(self, body: dict) -> dict:
        return self._post("model", body, run_bound=True)

    def cyber(self, body: dict) -> dict:
        return self._post("cyber", body, run_bound=True)

    def control(self, body: dict) -> dict:
        # A control read creates no model/tool work. Retry only its transient
        # transport failures, keeping the same attempt/cursor and a 300ms total
        # backoff. Refusals and exhausted reads still fail closed. In-flight
        # model receipts remain owned by the host; never replay them here.
        for attempt in range(3):
            try:
                response = self._post("control", body, run_bound=True)
                break
            except TaskRunClientUnavailable:
                if attempt == 2:
                    raise
                time.sleep(0.1 * (attempt + 1))
        if response.get("cancel_requested") is True or response.get("attempt_valid") is False:
            with self._credential_lock:
                self._stopping = True
        return response

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
        with self._credential_lock:
            self._run_credential = None
            self._bootstrap_body = None
            self._binding = None
            self._stopping = True
