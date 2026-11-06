#!/usr/bin/env python3
"""Opt-in transport of an existing native-test session, never an issuer.

This private client guard journals every attempted send before networking and
refuses new work after an uncertain response. Actual authority, complete prices
and the retained budget remain enforced by the Gateway's original native run.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

from adp_common import _jwt_claims

ENVIRONMENT = "ADP_NATIVE_TEST_TRANSPORT_FILE"
CAPABILITY_HEADER = "x-adp-native-test-capability"
OPERATION_HEADER = "x-adp-native-test-operation"
READS = {"knowledge___" + name for name in ("search", "understand", "impact", "browse", "secure")}
CALLS = READS | {
    "atlassian___" + name for name in (
        "search_jira", "get_jira_issue", "search_confluence", "get_confluence_page",
        "create_jira_issue", "update_jira_issue", "comment_jira_issue", "transition_jira_issue",
    )
}
JIRA_CREATED_ID = "adp-generated-jira-id"
JIRA_DEPENDENTS = {"atlassian___" + name for name in (
    "get_jira_issue", "search_jira", "update_jira_issue", "comment_jira_issue", "transition_jira_issue",
)}


class NativeTransportError(ValueError):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def jira_arguments(tool, template, provider_id):
    field = "jql" if tool == "atlassian___search_jira" else "issueIdOrKey"
    placeholder = 'id = "' + JIRA_CREATED_ID + '"' if field == "jql" else JIRA_CREATED_ID
    if (tool not in JIRA_DEPENDENTS or type(template) is not dict
            or template.get(field) != placeholder or canonical(template).count(JIRA_CREATED_ID.encode()) != 1
            or not isinstance(template.get("cloudId"), str)
            or not isinstance(provider_id, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", provider_id) is None):
        raise NativeTransportError("Exact created Jira dependency required")
    return {**template, field: placeholder.replace(JIRA_CREATED_ID, provider_id)}


def created_jira_id(result):
    for _ in range(6):
        if (type(result) is not dict or result.get("isError") is True or "error" in result
                or "isError" in result and type(result["isError"]) is not bool):
            break
        if "jsonrpc" in result:
            if (result["jsonrpc"] != "2.0" or type(result.get("id")) not in {str, int}
                    or type(result.get("result")) is not dict):
                break
            result = result["result"]
            continue
        if "structuredContent" in result:
            resource = result["structuredContent"]
        elif "content" in result:
            content = result["content"]
            if (type(content) is not list or len(content) != 1 or type(content[0]) is not dict
                    or content[0].get("type") != "text" or not isinstance(content[0].get("text"), str)):
                break
            try:
                resource = json.loads(content[0]["text"], object_pairs_hook=unique)
            except ValueError:
                break
        else:
            result = result.get("result")
            continue
        if (type(resource) is dict and "jsonrpc" not in resource and "error" not in resource
                and ("isError" not in resource or resource["isError"] is False) and type(resource.get("id")) in {str, int}):
            value = str(resource["id"])
            if re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
                return value
        break
    raise NativeTransportError("Original created Jira ID is unavailable")


def uuid(value):
    if not isinstance(value, str) or str(UUID(value)) != value:
        raise NativeTransportError("Original native operation binding unavailable")
    return value


def unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise NativeTransportError("Private native transport is invalid")
        result[key] = value
    return result


def owned(metadata, *, directory=False):
    return (stat.S_ISDIR(metadata.st_mode) if directory else stat.S_ISREG(metadata.st_mode)) and (
        metadata.st_uid == os.getuid() and not metadata.st_mode & 0o077
    )


class NativeTransport:
    def __init__(self, path, *, clock=time.time):
        self.path, self.clock = Path(path), clock
        try:
            if not self.path.is_absolute() or not owned(self.path.parent.lstat(), directory=True):
                raise ValueError
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, "rb") as source:
                if not owned(os.fstat(source.fileno())):
                    raise ValueError
                raw = source.read(65537)
            if len(raw) > 65536:
                raise ValueError
            value = json.loads(raw, object_pairs_hook=unique)
            if type(value.get("version")) is int and value["version"] == 2:
                self._operator_validate(value)
                return
            if set(value) != {"version", "reference", "access_token", "capability", "started_at", "calls", "models", "max_discoveries"}:
                raise ValueError
            reference = value["reference"]
            if (
                value["version"] != 1
                or type(value["version"]) is not int
                or set(reference)
                != {
                    "run_id",
                    "tenant_id",
                    "approval_revision",
                    "deployment_id",
                    "gateway_origin",
                    "source_revision",
                    "definition_revision",
                    "github_run_id",
                    "github_run_attempt",
                }
            ):
                raise ValueError
            uuid(reference["run_id"])
            uuid(reference["approval_revision"])
            parsed = urlsplit(reference["gateway_origin"])
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.path
                or parsed.query
                or parsed.fragment
                or parsed.username
                or parsed.password
                or parsed.port not in (None, 443)
                or reference["github_run_attempt"] != "1"
                or any(not re.fullmatch(r"[a-f0-9]{40}", reference[name]) for name in ("source_revision", "definition_revision"))
                or not re.fullmatch(r"adpt1\.[a-f0-9-]{36}\.[A-Za-z0-9_-]{43}", value["capability"])
                or not isinstance(value["access_token"], str)
                or len(value["access_token"]) > 16384
                or re.fullmatch(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", value["access_token"]) is None
                or type(value["started_at"]) is not int
                or type(value["max_discoveries"]) is not int
                or not 1 <= value["max_discoveries"] <= 9
            ):
                raise ValueError
            claims = _jwt_claims(value["access_token"])
            if (
                claims.get("custom:org_id") != reference["tenant_id"]
                or not claims.get("sub")
                or type(claims.get("exp")) is not int
                or claims["exp"] <= self.clock()
            ):
                raise ValueError
            if not isinstance(value["calls"], list) or not 1 <= len(value["calls"]) <= 64:
                raise ValueError
            for call in value["calls"]:
                if (
                    set(call) not in ({"tool", "arguments_sha256", "max_requests"},
                                      {"tool", "arguments_sha256", "max_requests", "jira_dependency"})
                    or call["tool"] not in CALLS
                    or not re.fullmatch(r"[a-f0-9]{64}", call["arguments_sha256"])
                    or type(call["max_requests"]) is not int
                    or not 1 <= call["max_requests"] <= 64
                ):
                    raise ValueError
                if "jira_dependency" in call:
                    dependency = call["jira_dependency"]
                    if (type(dependency) is not dict or set(dependency) != {"create_arguments_sha256", "arguments"}
                            or call["max_requests"] != 1 or digest(dependency["arguments"]) != call["arguments_sha256"]
                            or len([parent for parent in value["calls"]
                                    if parent == {"tool": "atlassian___create_jira_issue", "max_requests": 1,
                                                  "arguments_sha256": dependency["create_arguments_sha256"]}]) != 1):
                        raise ValueError
                    jira_arguments(call["tool"], dependency["arguments"], JIRA_CREATED_ID)
            if len({(call["tool"], call["arguments_sha256"]) for call in value["calls"]}) != len(value["calls"]):
                raise ValueError
            if not isinstance(value["models"], list) or len(value["models"]) > 2:
                raise ValueError
            for model in value["models"]:
                if (
                    set(model) != {"model", "route", "max_requests", "max_request_bytes", "max_output_tokens", "max_response_bytes"}
                    or not isinstance(model["model"], str)
                    or not model["model"]
                    or model["route"] not in {"/v1/messages", "/openai/v1/responses"}
                    or any(
                        type(model[field]) is not int or not 1 <= model[field] <= ceiling
                        for field, ceiling in (
                            ("max_requests", 64),
                            ("max_request_bytes", 65536),
                            ("max_output_tokens", 8192),
                            ("max_response_bytes", 262144),
                        )
                    )
                ):
                    raise ValueError
            if (
                len({(model["model"], model["route"]) for model in value["models"]}) != len(value["models"])
                or sum(entry["max_requests"] for entry in value["calls"] + value["models"]) + value["max_discoveries"] > 120
            ):
                raise ValueError
            self.value, self.reference, self.login = value, reference, claims
            self.binding = digest(value)
            self.directory = self.path.parent / reference["run_id"]
            self.directory.mkdir(mode=0o700, exist_ok=True)
            if not owned(self.directory.lstat(), directory=True):
                raise ValueError
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            raise NativeTransportError("Private original native transport is unavailable") from None

    @classmethod
    def configured(cls):
        path = os.environ.get(ENVIRONMENT)
        return cls(path) if path else None

    def matches_call(self, call, payload, attempts):
        if call["tool"] != payload["tool"]:
            return False
        if "jira_dependency" not in call:
            return call["arguments_sha256"] == digest(payload["arguments"])
        dependency = call["jira_dependency"]
        parent_index = next(index for index, parent in enumerate(self.value["calls"])
                            if parent["tool"] == "atlassian___create_jira_issue"
                            and parent["arguments_sha256"] == dependency["create_arguments_sha256"])
        originals = [entry for entry in attempts if entry["kind"] == "call" and entry["slot"] == "call:" + str(parent_index)
                     and entry["status"] in {"response_received", "response_recovered"}]
        if len(originals) != 1 or "jira_created" not in originals[0]:
            return False
        created = originals[0]["jira_created"]
        return (created["site_id"] == dependency["arguments"]["cloudId"]
                and digest(payload["arguments"]) == digest(jira_arguments(call["tool"], dependency["arguments"], created["provider_id"])))

    def context(self):
        return {
            "url": self.reference["gateway_origin"] + "/api",
            "proxy": False,
            "headers": {"Authorization": "Bearer " + self.value["access_token"]},
            "binding": {
                "mode": "native-test",
                "run_id": self.reference["run_id"],
                "approval_revision": self.reference["approval_revision"],
                "transport_sha256": self.binding,
            },
        }

    @contextmanager
    def journal(self):
        fd = os.open(self.directory / "attempts.json", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "r+b") as handle:
            if not owned(os.fstat(handle.fileno())):
                raise NativeTransportError("Original native journal is unavailable")
            fcntl.flock(handle, fcntl.LOCK_EX)
            raw = handle.read(262145)
            state = json.loads(raw, object_pairs_hook=unique) if raw else {"binding_sha256": self.binding, "attempts": []}
            if (
                len(raw) > 262144
                or set(state) != {"binding_sha256", "attempts"}
                or state["binding_sha256"] != self.binding
                or not isinstance(state["attempts"], list)
                or len(state["attempts"]) > 248
            ):
                raise NativeTransportError("Original native journal binding changed")
            yield state
            content = canonical(state)
            if len(content) > 262144:
                raise NativeTransportError("Original native journal limit reached")
            handle.seek(0)
            handle.write(content)
            handle.truncate()
            handle.flush()
            os.fsync(handle.fileno())
            for directory in (self.directory, self.directory.parent):
                directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)

    def begin(self, method, url, body):
        """Record an original attempted send before returning any credentials."""
        if self.value["version"] == 2:
            return self._operator_begin(method, url, body)
        parsed = urlsplit(url)
        if (
            parsed.scheme + "://" + parsed.netloc != self.reference["gateway_origin"]
            or parsed.fragment
            or not parsed.path.startswith("/api/")
            or not self.value["started_at"] <= self.clock() < self.value["started_at"] + 600
            or self.login["exp"] <= self.clock()
            or len(body or b"") > 65536
        ):
            raise NativeTransportError("Original native endpoint or horizon changed")
        path = parsed.path.removeprefix("/api")
        payload = json.loads(body, object_pairs_hook=unique) if body else None
        if path == "/agent/tools" and method == "GET" and not parsed.query and payload is None:
            kind, slot, limit = "list", "list", self.value["max_discoveries"]
        elif path == "/agent/tools/call" and method == "POST" and not parsed.query:
            if (
                not isinstance(payload, dict)
                or set(payload) - {"tool", "arguments", "request_id", "execution_id", "ttl_seconds"}
                or payload.get("execution_id") is not None
                or payload.get("ttl_seconds") is not None
                or not isinstance(payload.get("arguments"), dict)
            ):
                raise NativeTransportError("Native tool request is unsupported")
            uuid(payload["request_id"])
            with self.journal() as state:
                matches = [(index, call) for index, call in enumerate(self.value["calls"])
                           if self.matches_call(call, payload, state["attempts"])]
            if len(matches) != 1:
                raise NativeTransportError("Native tool payload is outside the original plan")
            kind, slot, limit = "call", "call:" + str(matches[0][0]), matches[0][1]["max_requests"]
        elif method == "POST" and re.fullmatch(r"/agent/tools/executions/[a-f0-9-]{36}/close", path):
            execution = uuid(path.split("/")[4])
            query = parse_qs(parsed.query, strict_parsing=True)
            if set(query) != {"tool"} or len(query["tool"]) != 1 or query["tool"][0] not in CALLS or payload not in ({}, None):
                raise NativeTransportError("Original native cleanup binding changed")
            kind, slot, limit = "close", "close:" + execution, 2
        elif path == "/agent/tools/operations/recover" and method == "POST" and not parsed.query:
            if (not isinstance(payload, dict) or set(payload) != {"tool", "arguments", "request_id", "execution_id"}
                    or payload.get("tool") not in CALLS - READS or not isinstance(payload.get("arguments"), dict)):
                raise NativeTransportError("Original native receipt request is unsupported")
            uuid(payload["request_id"])
            if payload["execution_id"] is not None:
                uuid(payload["execution_id"])
            kind, slot, limit = "recover", "recover:" + payload["request_id"], 2
        elif method == "POST" and not parsed.query and isinstance(payload, dict):
            matches = [
                (index, model)
                for index, model in enumerate(self.value["models"])
                if model["route"] == path and model["model"] == payload.get("model")
            ]
            if len(matches) != 1:
                raise NativeTransportError("Native model route is outside the original plan")
            index, model = matches[0]
            maximum = "max_tokens" if path == "/v1/messages" else "max_output_tokens"
            supplied = payload.get(maximum, model["max_output_tokens"])
            if type(supplied) is not int or not 1 <= supplied <= model["max_output_tokens"]:
                raise NativeTransportError("Native output token bound exceeded")
            payload[maximum] = supplied
            body = canonical(payload)
            if len(body) > model["max_request_bytes"]:
                raise NativeTransportError("Native input byte bound exceeded")
            kind, slot, limit = "model", "model:" + str(index), model["max_requests"]
        else:
            raise NativeTransportError("Native route is unsupported; inspect the original run")
        if kind not in {"close", "recover"} and self.clock() >= self.value["started_at"] + 300:
            raise NativeTransportError("Original native dispatch horizon expired")
        if kind not in {"close", "recover"} and (self.path.parent / "native-finalized.json").exists():
            raise NativeTransportError("Original native matrix is finalized; new dispatch is prohibited")
        request_hash = digest({"method": method, "path": path, "query": parsed.query, "body": hashlib.sha256(body or b"").hexdigest()})
        with self.journal() as state:
            attempts = state["attempts"]
            recovered = {entry["operation_id"]: entry for entry in attempts
                         if entry["kind"] == "call" and entry["status"] == "response_recovered"}
            resolved_receipt_reads = {
                entry["original_operation_id"] for entry in attempts
                if entry["kind"] == "recover" and entry["status"] == "response_received"
                and (original := recovered.get(entry.get("original_operation_id"))) is not None
                and original.get("recovered_by") == entry["operation_id"]
                and all(original.get(key) == entry.get(key) for key in
                        ("receipt", "response_sha256", "response_value_sha256"))
            }
            if (
                sum(entry["slot"] == slot for entry in attempts) >= limit
                or kind not in {"close", "recover"}
                and any(entry["status"] not in {"response_received", "response_recovered"}
                        and not (entry["kind"] == "recover"
                                 and entry.get("original_operation_id") in resolved_receipt_reads)
                        for entry in attempts)
                or kind in {"model", "call"}
                and any(entry["request_sha256"] == request_hash for entry in attempts)
            ):
                raise NativeTransportError("Native operation already attempted or unresolved; inspect its original outcome")
            original = None
            if kind == "recover":
                candidates = [entry for entry in attempts if entry["kind"] == "call"
                              and entry.get("request_value_sha256") == digest({**payload, "execution_id": None})]
                if len(candidates) != 1:
                    raise NativeTransportError("Receipt lookup requires the exact original native request")
                original = candidates[0]
                if (payload["execution_id"] is not None and original.get("receipt", {}).get("execution_id")
                        not in {None, payload["execution_id"]}):
                    raise NativeTransportError("Receipt lookup changed the original execution")
            if kind == "close" and not any(
                entry["kind"] == "call"
                and entry.get("receipt", {}).get("execution_id") == execution
                and entry.get("receipt", {}).get("tool") == query["tool"][0]
                for entry in attempts
            ):
                raise NativeTransportError("Cleanup requires a confirmed original owned execution")
            operation = str(uuid4())
            entry = {
                "operation_id": operation,
                "kind": kind,
                "slot": slot,
                "request_sha256": request_hash,
                "status": "attempted",
                "started_at": self.clock(),
            }
            if kind == "call":
                entry["request_value_sha256"] = digest(payload)
                entry["request_id"] = payload["request_id"]
            if original is not None:
                entry["original_operation_id"] = original["operation_id"]
            attempts.append(entry)
        return NativeAttempt(self, entry, payload, body, model if kind == "model" else None)

    def _operator_validate(self, value):
        if (set(value) != {"version", "reference", "binding", "access_token", "capability", "started_at", "models", "max_discoveries"}
                or value["models"] != [] or type(value["max_discoveries"]) is not int or value["max_discoveries"] != 1
                or type(value["started_at"]) is not int):
            raise ValueError
        reference, binding = value["reference"], value["binding"]
        if (set(reference) != {"producer", "run_id", "tenant_id", "subject_id", "approval_revision", "deployment_id",
                              "gateway_origin", "source_revision", "contract_sha256"}
                or reference["producer"] != "operator_local" or digest(binding) != reference["contract_sha256"]
                or binding["schema_version"] != "4.0"
                or any(reference[field] != binding[field] for field in ("deployment_id", "gateway_origin", "source_revision"))
                or not re.fullmatch(r"[a-f0-9]{40}", reference["source_revision"])
                or not re.fullmatch(r"adpt1\.[a-f0-9-]{36}\.[A-Za-z0-9_-]{43}", value["capability"])
                or not isinstance(binding["execute_code"], str) or not 0 < len(binding["execute_code"].encode()) <= 8192
                or not re.fullmatch(r"/tmp/(?!\.{1,2}$)[A-Za-z0-9_.-]{1,100}", binding["file_path"])):
            raise ValueError
        uuid(reference["run_id"])
        uuid(reference["approval_revision"])
        parsed = urlsplit(reference["gateway_origin"])
        if (parsed.scheme != "https" or not parsed.hostname or parsed.path or parsed.query or parsed.fragment
                or parsed.username or parsed.password or parsed.port is not None and not 1 <= parsed.port <= 65535
                or not isinstance(value["access_token"], str) or len(value["access_token"]) > 16384
                or re.fullmatch(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", value["access_token"]) is None):
            raise ValueError
        claims = _jwt_claims(value["access_token"])
        if (claims.get("custom:org_id") != reference["tenant_id"] or claims.get("sub") != reference["subject_id"]
                or type(claims.get("exp")) is not int or claims["exp"] <= self.clock()):
            raise ValueError
        self.value, self.reference, self.login, self.binding = value, reference, claims, digest(value)
        self.directory = self.path.parent / reference["run_id"]
        self.directory.mkdir(mode=0o700, exist_ok=True)
        if not owned(self.directory.lstat(), directory=True):
            raise ValueError

    @staticmethod
    def _operator_payload(payload):
        return {"tool": payload["tool"], "arguments": payload["arguments"], "request_id": payload["request_id"],
                "execution_id": payload.get("execution_id"), "ttl_seconds": payload.get("ttl_seconds")}

    def _operator_begin(self, method, url, body):
        parsed = urlsplit(url)
        if (parsed.scheme + "://" + parsed.netloc != self.reference["gateway_origin"] or parsed.fragment
                or not parsed.path.startswith("/api/") or len(body or b"") > 65536
                or not self.value["started_at"] <= self.clock() < self.value["started_at"] + 600
                or self.login["exp"] <= self.clock()):
            raise NativeTransportError("Original operator endpoint or horizon changed")
        path = parsed.path.removeprefix("/api")
        payload = json.loads(body, object_pairs_hook=unique) if body else None
        if path == "/agent/tools" and method == "GET" and not parsed.query and payload is None:
            step, execution = "list", None
        elif method == "POST" and path in {"/agent/tools/call", "/agent/tools/operations/recover"} and not parsed.query:
            if (not isinstance(payload, dict) or set(payload) - {"tool", "arguments", "request_id", "execution_id", "ttl_seconds"}
                    or not isinstance(payload.get("arguments"), dict)):
                raise NativeTransportError("Operator tool payload unavailable")
            payload = self._operator_payload(payload)
            uuid(payload["request_id"])
            step = payload["tool"].removeprefix("code_interpreter___")
            if payload["tool"] != "code_interpreter___" + step or step not in {"start", "execute", "result", "file"}:
                raise NativeTransportError("Operator tool is outside the original plan")
            if path.endswith("/recover"):
                if step != "execute":
                    raise NativeTransportError("Only original execute recovery is funded")
                step = "execute_status"
            execution = payload["execution_id"]
            if step == "start":
                if execution is not None or payload["arguments"] or payload["ttl_seconds"] != 300:
                    raise NativeTransportError("Original Interpreter start required")
            elif payload["ttl_seconds"] is not None or uuid(execution) != execution:
                raise NativeTransportError("Original Interpreter execution required")
            body = canonical(payload)
        elif method == "POST" and re.fullmatch(r"/agent/tools/executions/[a-f0-9-]{36}/close", path):
            execution = uuid(path.split("/")[4])
            if parse_qs(parsed.query, strict_parsing=True) != {"tool": ["code_interpreter___start"]} or payload not in ({}, None):
                raise NativeTransportError("Original Interpreter cleanup required")
            step = "close"
        else:
            raise NativeTransportError("Operator route is outside the original plan")
        if step != "close" and self.clock() >= self.value["started_at"] + 300:
            raise NativeTransportError("Original operator dispatch horizon expired")
        with self.journal() as state:
            attempts = state["attempts"]
            by_step = {entry["slot"]: entry for entry in attempts}
            if sum(entry["slot"] == step for entry in attempts) >= (2 if step == "close" else 1):
                raise NativeTransportError("Original operator step already attempted")
            root = by_step.get("start", {}).get("receipt", {})
            execute = by_step.get("execute", {})
            status = by_step.get("execute_status", {})
            if step not in {"list", "close", "execute_status"} and any(entry["status"] != "response_received" for entry in attempts):
                # A recovered original execute is the only permitted uncertain predecessor.
                if not (step in {"result", "file"} and status.get("status") == "response_received"
                        and all(entry["status"] == "response_received" or entry["slot"] == "execute" for entry in attempts)):
                    raise NativeTransportError("Original operator work is unresolved")
            if step == "start" and by_step.get("list", {}).get("status") != "response_received":
                raise NativeTransportError("Original discovery required")
            if step not in {"list", "start"} and (not root or root.get("execution_id") != execution):
                raise NativeTransportError("Original Interpreter root required")
            if step in {"execute", "execute_status", "result", "file"}:
                if by_step.get("start", {}).get("status") != "response_received" or not root.get("session_id"):
                    raise NativeTransportError("Original Interpreter start is unconfirmed")
                args = payload["arguments"]
                expected = {"session_id": root["session_id"]}
                if step in {"execute", "execute_status"}:
                    expected.update(code=self.value["binding"]["execute_code"], language="python")
                elif step == "file":
                    expected["path"] = self.value["binding"]["file_path"]
                    if by_step.get("result", {}).get("status") != "response_received":
                        raise NativeTransportError("Original confirmed result required")
                else:
                    expected["provider_id"] = status.get("receipt", {}).get("provider_id")
                    if status.get("status") != "response_received" or payload["request_id"] != execute.get("request_id"):
                        raise NativeTransportError("Original confirmed execute status required")
                if args != expected:
                    raise NativeTransportError("Interpreter payload differs from original approval")
                if step == "execute_status" and (not execute or execute.get("payload_sha256") != digest(payload)):
                    raise NativeTransportError("Recovery differs from original execute")
            entry = {"operation_id": str(uuid4()), "kind": step, "slot": step, "status": "attempted",
                     "started_at": self.clock(), "execution_id": execution,
                     "request_sha256": digest({"method": method, "path": path, "query": parsed.query,
                                                "body": hashlib.sha256(body or b"").hexdigest()})}
            if step in {"start", "execute", "execute_status", "result", "file"}:
                entry.update(request_id=payload["request_id"], payload_sha256=digest(payload))
            attempts.append(entry)
        return NativeAttempt(self, entry, payload, body, None)


class NativeAttempt:
    def __init__(self, transport, entry, payload, body, model):
        self.transport, self.entry, self.payload, self.body, self.model = transport, entry, payload, body, model
        self.maximum = model["max_response_bytes"] if model else 262144
        self.headers = {
            "Authorization": "Bearer " + transport.value["access_token"],
            CAPABILITY_HEADER: transport.value["capability"],
            OPERATION_HEADER: entry.get("original_operation_id", entry["operation_id"]),
            "Accept-Encoding": "identity",
        }

    def complete(self, status, content):
        try:
            self._complete(status, content)
        except (ValueError, TypeError, KeyError, AttributeError, UnicodeError):
            # Malformed provider content remains an unresolved original send.
            # Never include the response or parser exception in client logs.
            raise NativeTransportError("Native response is unconfirmed; inspect the original run") from None

    def _complete(self, status, content):
        if self.transport.value["version"] == 2:
            return self._operator_complete(status, content)
        if status != 200 or len(content) > self.maximum:
            raise NativeTransportError("Native response is unresolved; inspect the original run")
        receipt = None
        if self.entry["kind"] == "model":
            try:
                answer = json.loads(content, object_pairs_hook=unique)
                packets = [answer]
            except ValueError:
                packets = [
                    json.loads(line[5:].strip(), object_pairs_hook=unique)
                    for line in content.splitlines()
                    if line.startswith(b"data:") and line[5:].strip() != b"[DONE]"
                ]
            terminal = "message_stop" if self.model["route"] == "/v1/messages" else "response.completed"
            if (
                not packets
                or any(
                    not isinstance(packet, dict) or packet.get("type") in {"error", "response.failed", "response.incomplete"} or packet.get("error")
                    for packet in packets
                )
                or not (
                    sum(packet.get("type") == terminal for packet in packets) == 1
                    and packets[-1].get("type") == terminal
                    or len(packets) == 1
                    and (packets[0].get("stop_reason") or packets[0].get("status") == "completed")
                )
            ):
                raise NativeTransportError("Native model completion is unconfirmed")
            if self.model["route"] == "/openai/v1/responses":
                complete = next((packet.get("response") for packet in packets if packet.get("type") == terminal), packets[-1])
                if not isinstance(complete, dict) or complete.get("status") != "completed":
                    raise NativeTransportError("Native model completion is unconfirmed")
                if complete.get("error") or complete.get("incomplete_details"):
                    raise NativeTransportError("Native model completion is unconfirmed")
                usage = complete.get("usage", {})
            elif len(packets) == 1:
                if packets[0].get("type") != "message" or not packets[0].get("stop_reason"):
                    raise NativeTransportError("Native model completion is unconfirmed")
                usage = packets[0].get("usage", {})
            else:
                starts = [packet.get("message", {}).get("usage", {}) for packet in packets if packet.get("type") == "message_start"]
                ends = [packet for packet in packets if packet.get("type") == "message_delta"]
                if (
                    len(starts) != 1
                    or not ends
                    or not isinstance(starts[0], dict)
                    or packets[0].get("type") != "message_start"
                    or not ends[-1].get("delta", {}).get("stop_reason")
                ):
                    raise NativeTransportError("Native model completion is unconfirmed")
                usage = {"input_tokens": starts[0].get("input_tokens"), "output_tokens": ends[-1].get("usage", {}).get("output_tokens")}
            maximum = "max_tokens" if self.model["route"] == "/v1/messages" else "max_output_tokens"
            if (
                not isinstance(usage, dict)
                or any(type(usage.get(field)) is not int or usage[field] < 0 for field in ("input_tokens", "output_tokens"))
                or usage["output_tokens"] > self.payload[maximum]
            ):
                raise NativeTransportError("Native model usage is unconfirmed or exceeds its original bound")
        else:
            result = json.loads(content, object_pairs_hook=unique)
            if not isinstance(result, dict):
                raise NativeTransportError("Native response is unconfirmed")
            if self.entry["kind"] == "list":
                if not isinstance(result.get("tools"), list):
                    raise NativeTransportError("Native discovery is unconfirmed")
            elif self.entry["kind"] in {"call", "recover"}:
                reservation = "tools-test-effect:" + digest(
                    {
                        "tenant_id": self.transport.reference["tenant_id"],
                        "run_id": self.transport.reference["run_id"],
                        "operation_id": self.entry.get("original_operation_id", self.entry["operation_id"]),
                    }
                )
                if (
                    result.get("status") != "confirmed"
                    or result.get("result_status") != "success"
                    or result.get("request_id") != self.payload["request_id"]
                    or result.get("receipt_id") != reservation
                    or self.payload.get("execution_id") is not None and result.get("execution_id") != self.payload["execution_id"]
                ):
                    raise NativeTransportError("Original native receipt is unconfirmed")
                receipt = {
                    "execution_id": uuid(result["execution_id"]),
                    "request_id": result["request_id"],
                    "receipt_id": reservation,
                    "tool": self.payload["tool"],
                }
            elif (
                result.get("execution_id") != self.entry["slot"].removeprefix("close:")
                or result.get("state") != "closed"
                or result.get("cleanup_status") != "confirmed"
            ):
                raise NativeTransportError("Original native cleanup is unconfirmed")
        created = None
        if (receipt is not None and self.payload["tool"] == "atlassian___create_jira_issue"
                and any("jira_dependency" in call for call in self.transport.value["calls"])):
            created = {"site_id": self.payload["arguments"]["cloudId"], "provider_id": created_jira_id(result)}
        with self.transport.journal() as state:
            matched = [entry for entry in state["attempts"] if entry["operation_id"] == self.entry["operation_id"]]
            if len(matched) != 1 or matched[0] != self.entry:
                raise NativeTransportError("Original native journal changed")
            matched[0].update(status="response_received", response_sha256=hashlib.sha256(content).hexdigest())
            if receipt is not None:
                matched[0]["receipt"] = receipt
                matched[0]["response_value_sha256"] = digest(result)
                if created is not None:
                    matched[0]["jira_created"] = created
                if self.entry["kind"] == "recover":
                    originals = [entry for entry in state["attempts"] if entry["operation_id"] == self.entry["original_operation_id"]
                                 and entry["kind"] == "call"]
                    if len(originals) != 1:
                        raise NativeTransportError("Original native request is unavailable")
                    originals[0].update(status="response_recovered", receipt=receipt, recovered_by=self.entry["operation_id"],
                                        response_sha256=hashlib.sha256(content).hexdigest(), response_value_sha256=digest(result))
                    if created is not None:
                        originals[0]["jira_created"] = created

    def _operator_complete(self, status, content):
        if status not in {200, 202} or len(content) > self.maximum:
            raise NativeTransportError("Original operator response is unresolved")
        result = json.loads(content, object_pairs_hook=unique)
        step, receipt, outcome = self.entry["slot"], None, "response_received"
        if step == "list":
            names = {row["name"] for row in result["tools"]}
            if status != 200 or not {"code_interpreter___" + name for name in ("start", "execute", "result", "file")} <= names:
                raise NativeTransportError("Operator discovery is unconfirmed")
        elif step == "close":
            if (result.get("execution_id") != self.entry["execution_id"] or result.get("state") != "closed"
                    or result.get("cleanup_status") != "confirmed"):
                raise NativeTransportError("Original Interpreter cleanup is unconfirmed")
        else:
            with self.transport.journal() as state:
                execute = next((entry for entry in state["attempts"] if entry["slot"] == "execute"), {})
            paid_operation = execute.get("operation_id") if step == "execute_status" else self.entry["operation_id"]
            reservation = "tools-test-effect:" + digest({"tenant_id": self.transport.reference["tenant_id"],
                "run_id": self.transport.reference["run_id"], "operation_id": paid_operation})
            if (result.get("request_id") != self.payload["request_id"] or result.get("receipt_id") != reservation
                    or step != "start" and result.get("execution_id") != self.entry["execution_id"]):
                raise NativeTransportError("Original operator receipt binding changed")
            receipt = {"execution_id": uuid(result["execution_id"]), "request_id": result["request_id"],
                       "receipt_id": reservation, "tool": self.payload["tool"]}
            if result.get("status") == "unknown" and step in {"start", "execute", "execute_status"}:
                outcome = "pending_received"
            elif status != 200 or result.get("status") != "confirmed" or result.get("result_status") != "success":
                raise NativeTransportError("Original operator result is unconfirmed")
            else:
                provider = result.get("result")
                for _ in range(4):
                    if not isinstance(provider, dict) or "session_id" in provider:
                        break
                    provider = provider.get("result")
                if not isinstance(provider, dict) or re.fullmatch(r"[a-f0-9]{64}", provider.get("session_id", "")) is None:
                    raise NativeTransportError("Original Interpreter session is unconfirmed")
                receipt["session_id"] = provider["session_id"]
                if step in {"execute", "execute_status", "result"}:
                    if re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", provider.get("provider_id", "")) is None:
                        raise NativeTransportError("Original Interpreter task is unconfirmed")
                    receipt["provider_id"] = provider["provider_id"]
        with self.transport.journal() as state:
            matched = [entry for entry in state["attempts"] if entry["operation_id"] == self.entry["operation_id"]]
            if len(matched) != 1 or matched[0] != self.entry:
                raise NativeTransportError("Original operator journal changed")
            matched[0].update(status=outcome, response_sha256=hashlib.sha256(content).hexdigest())
            if receipt is not None:
                matched[0]["receipt"] = receipt
