"""Authenticated gateway calls for the Task API host runtime."""

from __future__ import annotations

import base64
import copy
from contextlib import contextmanager
from contextvars import ContextVar
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
_TRACEPARENT = ContextVar("adp_task_traceparent", default=None)
_TRACE_PATTERN = r"00-(?!0{32}-)[a-f0-9]{32}-(?!0{16}-)[a-f0-9]{16}-0[01]"
_MAX_RESPONSE_BYTES = 1024 * 1024
_ACTIONS = frozenset(
    {
        "bootstrap",
        "attempt",
        "report",
        "turn",
        "model",
        "cyber",
        "tool-authorize",
        "repository-source",
        "repository-publication",
        "repository-completion",
        "tool-operation",
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
        self._local_tools = {}
        self._workspace_tools = None
        self._validation_tool = None
        self._host_validation_executor = None
        self._validation_backend = os.environ.get("ADP_CODEX_VALIDATION_BACKEND", "docker-local")
        self._publication_tool = None
        self._traceparent = None
        self._tool_cleanup = json.loads(os.environ.get("ADP_TASK_TOOL_CLEANUP", "[]"))
        self._tool_routes = json.loads(os.environ.get("ADP_TASK_TOOL_ROUTES", "{}"))
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
        self.validation_stop_event = threading.Event()

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

    @contextmanager
    def trace_context(self, value):
        if value is not None and (not isinstance(value, str) or not re.fullmatch(_TRACE_PATTERN, value)):
            raise TaskRunClientError("Invalid host trace context")
        token = _TRACEPARENT.set(value)
        try:
            yield
        finally:
            _TRACEPARENT.reset(token)

    def _post(
        self,
        action: str,
        body: dict,
        *,
        run_bound: bool,
        workload_token: str | None = None,
        tool_endpoint: str | None = None,
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
        url = (tool_endpoint or self._cyber_url()) if action == "cyber" else f"{self._base}/task/{action}"
        data = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=action != "model").encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            WORKLOAD_HEADER: workload_token or read_workload_token(),
        }
        parent = _TRACEPARENT.get() or self._traceparent
        if parent:
            _, trace_id, span_id, flags = parent.split("-")
            headers["traceparent"] = parent
            headers["X-Amzn-Trace-Id"] = f"Root=1-{trace_id[:8]}-{trace_id[8:]};Parent={span_id};Sampled={1 if flags == '01' else 0}"
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
        parent = response.get("harness", {}).get("traceparent")
        if parent is not None and (not isinstance(parent, str) or not re.fullmatch(_TRACE_PATTERN, parent)):
            raise TaskRunClientError("Invalid bootstrap trace context")
        self._traceparent = parent
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
        stop_only = (action == "tool-authorize" and body.get("cleanup") is True) or action == "control" or (action == "cyber" and body.get("operation") == "cancel_jobs") or (
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

    def repository_source(self, body: dict) -> dict:
        return self._post("repository-source", body, run_bound=True)

    def repository_publication(self, body: dict) -> dict:
        return self._post("repository-publication", body, run_bound=True)

    def repository_completion(self, body: dict) -> dict:
        if self._workspace_tools is None or self._publication_tool is None:
            raise TaskRunClientError("Developer completion workspace unavailable")
        state = self._workspace_tools.workspace.state()
        if not state["clean"]:
            return {"status": "unverified"}
        result = self._post("repository-completion", body, run_bound=True)
        if result.get("status") == "unverified":
            return result
        if (result.get("status") != "verified" or result.get("local_head") != state["localHead"]
                or result.get("tree") != state["tree"] or self._workspace_tools.workspace.state() != state):
            raise TaskRunClientError("Developer completion differs from workspace")
        return result

    def tool_authorize(self, body: dict) -> dict:
        return self._post("tool-authorize", body, run_bound=True)

    def tool_operation(self, body: dict) -> dict:
        # Trusted host only. Owner tokens must not be included in child frames.
        return self._post("tool-operation", body, run_bound=True)

    def _hosted_validation(self):
        if self._validation_backend == "docker-local":
            return None
        if self._validation_backend != "kubernetes" or self._binding is None:
            raise TaskRunClientError("Host validation backend unavailable")
        if self._host_validation_executor is None:
            from lib.codex_kubernetes_validation import from_host_configuration
            self._host_validation_executor = from_host_configuration(self._binding["task_id"])
        return self._host_validation_executor

    def bind_workspace(self, *, attempt, workspace, tools):
        from lib.codex_workspace_tools import WorkspaceTools
        if self._workspace_tools is not None:
            raise TaskRunClientError("Task workspace is already bound")
        workspace_tools = WorkspaceTools(self, attempt=attempt, workspace=workspace, tools=tools)
        validation_tool = None
        if "validation.run" in tools:
            from lib.codex_validation_tool import TaskValidationTool
            response = self.tool_authorize({"schema_version": "1.0", "attempt": attempt, "tool": "validation.run"})
            identity = {**attempt["run"], "runtime_attempt_id": attempt["runtime_attempt_id"]}
            task = response.get("task", {})
            binding = task.get("repository_binding", {})
            source = binding.get("binding", {})
            if (response.get("schema_version") != "1.0"
                    or any(response.get("identity", {}).get(k) != v for k, v in identity.items())
                    or "validation.run" not in task.get("tool_grants", [])
                    or source.get("provider") != workspace.provider
                    or source.get("repository") != workspace.repository
                    or source.get("repository_id") != workspace.repository_id):
                raise TaskRunClientError("Validation workspace authority differs")
            executor = self._hosted_validation()
            if executor is not None:
                # Resolve deployment availability and prior-work cleanup before
                # starting the SDK, never after model work has already begun.
                executor._boundary()
                executor.recover()
                if any("@sha256:" not in check.get("image", "") for check in source.get("validation_checks", [])):
                    raise TaskRunClientError("Hosted validation requires registry-qualified checks")
            validation_tool = TaskValidationTool(self, {
                "schema_version": "1.0", "attempt": attempt,
                "repository_path": str(workspace.root), "repository_binding": binding,
                "checks": source.get("validation_checks", []),
            }, executor=executor)
        publication_tool = None
        if "change.create" in tools:
            from lib.codex_publication_tool import PREREQUISITES, TaskPublicationTool
            if not PREREQUISITES.issubset(tools) or validation_tool is None:
                raise TaskRunClientError("Publication requires workspace edit, commit and validation tools")
            publication_tool = TaskPublicationTool(self, attempt=attempt, workspace=workspace, binding=binding)
        # Bind atomically: a refused validation policy must not leave usable tools.
        self._workspace_tools, self._validation_tool = workspace_tools, validation_tool
        self._publication_tool = publication_tool

    def tool(self, name: str, body: dict) -> dict:
        if self._publication_tool is not None and name == "change.create":
            return self._publication_tool.invoke(body)
        if self._validation_tool is not None and name == "validation.run":
            return self._validation_tool.invoke(body)
        if self._workspace_tools is not None and name in self._workspace_tools.tools:
            return self._workspace_tools.invoke(name, body)
        # Exact host-configured registry. The child supplies a name, never a URL.
        endpoint = self._tool_routes.get(name)
        if isinstance(endpoint, str) and endpoint.startswith("local:"):
            import importlib
            target = endpoint.removeprefix("local:")
            if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_.]*", target):
                raise TaskRunClientError("Invalid local tool handler")
            with self._credential_lock:
                if target not in self._local_tools:
                    module, factory = target.rsplit(".", 1)
                    self._local_tools[target] = getattr(importlib.import_module(module), factory)(self)
                handler = self._local_tools[target]
            return handler.invoke(body)
        if not isinstance(endpoint, str) or any(c.isspace() for c in endpoint):
            raise TaskRunClientError("Tool is not configured")
        try:
            parsed = urlparse(endpoint)
            valid = (parsed.scheme == "https" and parsed.hostname and not parsed.username
                     and not parsed.password and parsed.port in (None, 443)
                     and "?" not in endpoint and "#" not in endpoint
                     and re.fullmatch(r"(?:/[A-Za-z0-9_-]+)+", parsed.path))
        except ValueError:
            valid = False
        if not valid:
            raise TaskRunClientError("Tool endpoint unavailable")
        return self._post("cyber", body, run_bound=True, tool_endpoint=endpoint)

    def cyber(self, body: dict) -> dict:
        cleanup_receipts = []
        if body.get("operation") == "cancel_jobs":
            for name in self._tool_cleanup:
                cleanup_receipts.append(self.tool(name, body))
        receipt = self._post("cyber", body, run_bound=True)
        for cleanup in cleanup_receipts:
            if any(cleanup.get(key) != receipt.get(key) for key in ("schema_version", "task_id", "operation_id")):
                raise TaskRunClientError("Tool cleanup identity differs")
            if cleanup.get("operation_status") != "confirmed" or cleanup.get("result", {}).get("status") != "confirmed" or cleanup.get("result", {}).get("pending_jobs") != []:
                return cleanup
        return receipt

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
                self.validation_stop_event.set()
        return response

    def artifact(self, body: dict) -> dict:
        return self._post("artifact", body, run_bound=True)

    def _validation_stopped(self, *, cancel: bool) -> bool:
        from lib.codex_validation_tool import TaskValidationTool

        with self._credential_lock:
            if cancel:
                self._stopping = True
                self.validation_stop_event.set()
            handlers = [self._validation_tool, *self._local_tools.values()]
        deadline = time.monotonic() + (10 if cancel else 0)
        local_stopped = all(
            handler.wait_stopped(max(0, deadline - time.monotonic()))
            for handler in handlers if isinstance(handler, TaskValidationTool)
        )
        if not local_stopped:
            return False
        try:
            # Even a replacement host with no bound workspace must inspect the
            # persistent inventory before claiming all Task work has stopped.
            executor = self._hosted_validation()
            return executor is None or executor.recover()
        except Exception:
            return False

    def finalize(self, body: dict) -> dict:
        if not self._validation_stopped(cancel=body.get("outcome") != "completed"):
            raise TaskRunClientUnavailable("Validation termination is unconfirmed")
        return self._post("finalize", body, run_bound=True)

    def settlement(self, body: dict) -> dict:
        if not self._validation_stopped(cancel=True):
            body = copy.deepcopy(body)
            body["stop_evidence"]["child_exit_confirmed"] = False
            body["stop_evidence"]["workload_terminated"] = False
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
            self.validation_stop_event.set()
            self._workspace_tools = None
            self._validation_tool = None
            self._host_validation_executor = None
            self._publication_tool = None
            self._traceparent = None
            self._local_tools.clear()
