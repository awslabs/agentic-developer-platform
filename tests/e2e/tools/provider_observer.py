"""Bounded, read-only facts from core provider ledgers and AWS session APIs.

This is a fact collector, not the matrix acceptance/accounting verifier. Inputs
are privately bound by the fixture controller, never taken from a tool response
alone. No facade state, provider invocation or cleanup mutation is read/called.
"""

import base64
import copy
import hashlib
import json
import math
import os
import re
import stat
import time
from datetime import datetime, timezone
from uuid import UUID


OPERATIONS = {
    "websearch___search",
    *("code_interpreter___" + action for action in ("start", "execute", "result", "file", "close")),
    *("browser___" + action for action in ("start", "step", "inspect", "close")),
}
READS = {("sts", "get_caller_identity"), ("dynamodb", "get_item"),
         ("bedrock-agentcore", "get_browser_session"),
         ("bedrock-agentcore", "get_code_interpreter_session")}
ROW_FIELDS = {
    "event_id", "arrived_at", "execution_id", "request_id", "receipt_id", "request_digest",
    "state", "result_json", "session_handle", "provider_identifier", "provider_id",
    "provider_task_id", "deadline_at", "cleanup_state", "artifact_sha256", "artifact_length",
    "operation", "generation", "digest", "result", "start_id", "session_id", "view_id",
    "execution_deadline_at", "provider_deadline_at", "provider_dispatch", "cleanup_status",
    "content", "sha256", "byte_length",
}
UNSUPPORTED = {
    "upstream_request_count": "no-upstream-attempt-audit",
    "cost_usd": "separate-accounting-required",
    "global_session_count": "owned-session-is-not-a-global-inventory",
    "invocation_audit": "ledger-state-is-not-an-invocation-audit",
    "facade_artifact": "provider-payload-is-not-the-facade-envelope",
}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def require(condition, reason="invalid-private-observer-binding"):
    if not condition:
        raise ValueError(reason)


def exact(value, fields):
    require(isinstance(value, dict) and set(value) == set(fields))


def text_id(value):
    return isinstance(value, str) and 0 < len(value) <= 256 and not any(ord(c) < 32 for c in value)


def uuid4(value):
    try:
        return isinstance(value, str) and str(UUID(value)) == value and UUID(value).version == 4
    except (ValueError, TypeError):
        return False


def hexhash(value, length=64):
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{" + str(length) + "}", value) is not None


def stamp(value):
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        raise ValueError("invalid-observation-time")
    require(parsed.tzinfo is not None, "invalid-observation-time")
    return parsed.timestamp()


def fact(status, source, **values):
    return {"status": status, "source": source, **values}


class Pending(Exception):
    """Only static reason codes may be propagated into evidence."""


class Refused(Exception):
    """Only static reason codes may be propagated into evidence."""


def check(condition, reason):
    if not condition:
        raise Refused(reason)


class SDKReader:
    """Freeze an explicitly supplied observer session; allow four read methods."""

    def __init__(self, session, region):
        import boto3
        from botocore.config import Config

        credentials = session.get_credentials().get_frozen_credentials()
        require(bool(credentials.access_key and credentials.secret_key), "observer-credentials-unavailable")
        self.session = boto3.Session(aws_access_key_id=credentials.access_key,
                                    aws_secret_access_key=credentials.secret_key,
                                    aws_session_token=credentials.token, region_name=region)
        self.config = Config(region_name=region, connect_timeout=1, read_timeout=2,
                             retries={"total_max_attempts": 1}, ignore_configured_endpoint_urls=True)
        self.clients = {}

    def __call__(self, service, operation, **arguments):
        require((service, operation) in READS, "observer-method-is-not-read-only")
        if service not in self.clients:
            self.clients[service] = self.session.client(service, config=self.config)
        return getattr(self.clients[service], operation)(**arguments)


def private_manifest(path, expected_sha256):
    require(hexhash(expected_sha256), "private-manifest-pin-required")
    # Inspect the opened inode, not a preceding path stat; reject symlink races.
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        metadata = os.fstat(stream.fileno())
        require(stat.S_ISREG(metadata.st_mode) and not metadata.st_mode & 0o077
                and metadata.st_uid == os.geteuid() and metadata.st_size <= 1024 * 1024,
                "private-manifest-permissions-or-size")
        content = stream.read(1024 * 1024 + 1)
    require(len(content) <= 1024 * 1024 and hashlib.sha256(content).hexdigest() == expected_sha256,
            "private-manifest-pin-mismatch")
    return json.loads(content)


def from_manifest(path, *, expected_sha256, account, region, session=None, read=None, **options):
    """A controller supplies the pin and explicit dedicated read-only session."""
    require((session is None) != (read is None), "supply-one-explicit-observer-session-or-reader")
    manifest = private_manifest(path, expected_sha256)
    # Validate selectors before resolving credentials or constructing SDK clients.
    observer = Observer(manifest, account=account, region=region, read=read, **options)
    observer.read = read if read is not None else SDKReader(session, region)
    return observer


class Observer:
    def __init__(self, manifest, *, account, region, read, monotonic=time.monotonic,
                 now=lambda: datetime.now(timezone.utc), sleep=time.sleep):
        exact(manifest, {"version", "deployment", "authority", "limits", "operations"})
        require(type(manifest["version"]) is int and manifest["version"] == 1)
        deployment = manifest["deployment"]
        exact(deployment, {"account", "region", "observer_role_arn", "shared_table_arn", "browser_table_arn",
                           "interpreter_arn", "browser_identifier", "provider_source_revision", "release_receipt_sha256"})
        require(isinstance(account, str) and re.fullmatch(r"[0-9]{12}", account)
                and isinstance(region, str) and re.fullmatch(r"[a-z]{2}-[a-z]+-[0-9]+", region))
        require(deployment["account"] == account and deployment["region"] == region,
                "observer-deployment-account-or-region-mismatch")
        for field in ("shared_table_arn", "browser_table_arn"):
            require(isinstance(deployment[field], str) and re.fullmatch(
                rf"arn:aws:dynamodb:{region}:{account}:table/[A-Za-z0-9_.-]{{3,255}}", deployment[field]))
        require(deployment["shared_table_arn"] != deployment["browser_table_arn"])
        require(isinstance(deployment["interpreter_arn"], str) and re.fullmatch(
            rf"arn:aws:bedrock-agentcore:{region}:{account}:code-interpreter-custom/[A-Za-z0-9_-]+",
            deployment["interpreter_arn"]))
        require(isinstance(deployment["observer_role_arn"], str) and re.fullmatch(
            rf"arn:aws:iam::{account}:role/(?:[A-Za-z0-9_+=,.@-]+/)*[A-Za-z0-9_+=,.@-]+",
            deployment["observer_role_arn"]))
        require(deployment["browser_identifier"] == "aws.browser.v1"
                and hexhash(deployment["provider_source_revision"], 40)
                and hexhash(deployment["release_receipt_sha256"]))
        authority = manifest["authority"]
        exact(authority, {"principal", "run_id", "workload_id"})
        exact(authority["principal"], {"tenant_id", "user_id", "team_id"})
        require(all(text_id(v) for v in authority["principal"].values()))
        require((authority["run_id"] is None and authority["workload_id"] is None)
                or (text_id(authority["run_id"]) and text_id(authority["workload_id"])))
        limits = manifest["limits"]
        exact(limits, {"max_reads", "max_polls", "poll_seconds", "timeout_seconds", "max_item_bytes"})
        require(all(type(limits[k]) is int and 1 <= limits[k] <= upper for k, upper in
                    (("max_reads", 128), ("max_polls", 60), ("timeout_seconds", 600), ("max_item_bytes", 200000))))
        require(type(limits["poll_seconds"]) in (int, float) and math.isfinite(limits["poll_seconds"])
                and 0.1 <= limits["poll_seconds"] <= 10)
        require(isinstance(manifest["operations"], list) and 1 <= len(manifest["operations"]) <= 90)
        self.deployment, self.authority, self.limits = copy.deepcopy((deployment, authority, limits))
        self.account, self.region, self.read = account, region, read
        self.monotonic, self.now, self.sleep = monotonic, now, sleep
        self.bindings = {}
        for original in manifest["operations"]:
            binding = copy.deepcopy(original)
            self._validate_binding(binding)
            key = tuple(binding[k] for k in ("client", "operation", "operation_ref", "execution_id"))
            require(key not in self.bindings, "duplicate-private-operation-binding")
            self.bindings[key] = binding

    @staticmethod
    def capabilities(operation):
        return {"supported": operation in OPERATIONS,
                "facts": ["correlation", "durable_outcome", "provider_artifact", "session_ttl", "termination"] if operation in OPERATIONS else [],
                "unsupported": dict(UNSUPPORTED)}

    def _validate_binding(self, b):
        exact(b, {"client", "operation", "operation_ref", "execution_id", "provider_request_id",
                  "provider_receipt_id", "arguments", "root", "parent", "artifact", "cleanup"})
        require(b["client"] in {"cli", "claude-code", "codex"} and b["operation"] in OPERATIONS)
        require(all(uuid4(b[k]) for k in ("operation_ref", "execution_id", "provider_request_id"))
                and text_id(b["provider_receipt_id"]) and isinstance(b["arguments"], dict)
                and len(canonical(b)) <= 32768)
        family, action = b["operation"].split("___")
        if not (family == "code_interpreter" and action in {"result", "close"}):
            require(b["operation_ref"] == b["provider_request_id"], "operation-reference-provider-binding-mismatch")
        fields = {"execution_id", "request_id"}
        if family == "websearch":
            fields |= {"query"}
            require(b["root"] is None and b["parent"] is None and b["cleanup"] is None)
            require(isinstance(b["arguments"].get("query"), str) and 1 <= len(b["arguments"]["query"]) <= 200
                    and b["arguments"]["query"].strip())
        elif family == "browser":
            fields |= {"url"} if action == "start" else {"session_id"}
            if action in {"step", "inspect"}:
                fields |= {"view_id"}
            if action == "step":
                # The reviewed browser accepts a bounded action-specific payload.
                fields |= {"action"}
                require(fields <= set(b["arguments"]) <= fields | {"candidate_id", "url", "seconds"})
                fields = set(b["arguments"])
        else:
            if action != "start":
                fields |= {"session_id"}
            if action == "execute": fields |= {"code", "language"}
            if action == "result": fields |= {"provider_id"}
            if action == "file": fields |= {"path"}
            if action == "close": fields.remove("request_id")
        exact(b["arguments"], fields)
        require(b["arguments"].get("execution_id") == b["execution_id"]
                and ("request_id" not in fields or b["arguments"]["request_id"] == b["provider_request_id"]))
        if family != "websearch":
            root = b["root"]
            exact(root, {"request_id", "receipt_id", "arguments", "deadline_at", "ttl_seconds"})
            require(uuid4(root["request_id"]) and text_id(root["receipt_id"])
                    and type(root["deadline_at"]) is int and root["deadline_at"] > 0
                    and type(root["ttl_seconds"]) is int and 1 <= root["ttl_seconds"] <= 300)
            exact(root["arguments"], {"execution_id", "request_id"} | ({"url"} if family == "browser" else set()))
            require(root["arguments"]["execution_id"] == b["execution_id"]
                    and root["arguments"]["request_id"] == root["request_id"])
            if action == "start" or (family == "code_interpreter" and action == "close"):
                require(b["provider_request_id"] == root["request_id"]
                        and b["provider_receipt_id"] == root["receipt_id"])
            cleanup = b["cleanup"]
            exact(cleanup, {"trigger", "requested_at", "max_seconds"})
            require(cleanup["trigger"] in {"public-close", "deadline"}
                    and type(cleanup["max_seconds"]) is int and 1 <= cleanup["max_seconds"] <= 600)
            requested = stamp(cleanup["requested_at"])
            require(cleanup["trigger"] != "deadline" or requested == root["deadline_at"])
        if family == "code_interpreter" and action == "result":
            parent = b["parent"]
            exact(parent, {"operation", "request_id", "receipt_id", "arguments"})
            require(parent["operation"] == "code_interpreter___execute"
                    and parent["request_id"] == b["provider_request_id"]
                    and parent["receipt_id"] == b["provider_receipt_id"])
            exact(parent["arguments"], {"execution_id", "request_id", "session_id", "code", "language"})
            require(parent["arguments"]["execution_id"] == b["execution_id"]
                    and parent["arguments"]["request_id"] == parent["request_id"]
                    and parent["arguments"]["session_id"] == b["arguments"]["session_id"])
        else:
            require(b["parent"] is None)
        if b["artifact"] is not None:
            artifact = b["artifact"]
            exact(artifact, {"scope", "sha256", "byte_length", "offset", "length"})
            require(artifact["scope"] == "provider-payload" and hexhash(artifact["sha256"])
                    and type(artifact["byte_length"]) is int and 0 <= artifact["byte_length"] <= 128000
                    and type(artifact["offset"]) is int and 0 <= artifact["offset"] <= artifact["byte_length"]
                    and type(artifact["length"]) is int and 1 <= artifact["length"] <= 8192)

    def _call(self, budget, service, operation, **arguments):
        check((service, operation) in READS, "observer-method-is-not-read-only")
        if budget["remaining"] == 0 or self.monotonic() + 3 > budget["deadline"]:
            raise Pending("observation-read-or-time-limit")
        budget["remaining"] -= 1
        try:
            response = self.read(service, operation, **arguments)
        except Exception as error:
            code = getattr(error, "response", {}).get("Error", {}).get("Code", "")
            if code in {"AccessDenied", "AccessDeniedException", "UnauthorizedException"}:
                raise Refused("observer-read-denied") from None
            raise Pending("observer-read-unavailable") from None
        if self.monotonic() > budget["deadline"]:
            raise Pending("observation-time-limit")
        check(isinstance(response, dict), "invalid-provider-read-response")
        budget["reads"].append({"service": service, "operation": operation,
                                "aws_request_id": response.get("ResponseMetadata", {}).get("RequestId"),
                                "observed_at": self.now().isoformat()})
        return response

    def _row(self, budget, table, partition, sort_key):
        from boto3.dynamodb.types import TypeDeserializer

        names = {f"#f{i}": field for i, field in enumerate(sorted(ROW_FIELDS))}
        result = self._call(budget, "dynamodb", "get_item", TableName=table,
                            Key={"event_id": {"S": partition}, "arrived_at": {"S": sort_key}},
                            ConsistentRead=True, ProjectionExpression=", ".join(names),
                            ExpressionAttributeNames=names)
        if not result.get("Item"):
            raise Pending("provider-ledger-row-unavailable")
        item = result["Item"]
        # Enforce a wire-size bound before decoding JSON/base64 result fields.
        check(len(json.dumps(item, default=lambda v: "x" * len(v) if isinstance(v, bytes) else "invalid"))
              <= self.limits["max_item_bytes"], "provider-ledger-item-too-large")
        check(set(item) <= ROW_FIELDS, "unprojected-provider-fields")
        decode = TypeDeserializer().deserialize
        row = {key: decode(value) for key, value in item.items()}
        check(row.get("event_id") == partition and row.get("arrived_at") == sort_key,
              "provider-ledger-key-mismatch")
        return row

    def _keys(self, b):
        p = self.authority["principal"]
        owner = [p["tenant_id"], p["user_id"], p["team_id"],
                 self.authority["run_id"], self.authority["workload_id"]]
        family = b["operation"].split("___")[0]
        if family == "websearch":
            return self.deployment["shared_table_arn"], "GATEWAY_SEARCH#" + digest([owner, b["provider_request_id"]])
        prefix = "GATEWAY_CODE#" if family == "code_interpreter" else "GATEWAY_BROWSER#"
        table = "shared_table_arn" if family == "code_interpreter" else "browser_table_arn"
        return self.deployment[table], prefix + digest(owner + [b["execution_id"]])

    def _browser_digest(self, operation, arguments):
        return digest([operation, arguments, self.authority["principal"],
                       self.authority["run_id"], self.authority["workload_id"]])

    @staticmethod
    def _identity(row, b):
        check(row.get("execution_id") == b["execution_id"]
              and row.get("request_id") == b["provider_request_id"]
              and row.get("receipt_id") == b["provider_receipt_id"], "provider-correlation-mismatch")

    def _artifact(self, content, b):
        check(isinstance(content, bytes) and len(content) <= 128000, "provider-artifact-too-large")
        sha = hashlib.sha256(content).hexdigest()
        result = fact("verified", "provider-owned-ledger", sha256=sha, byte_length=len(content), scope="provider-payload")
        expected = b["artifact"]
        if expected is not None:
            check(sha == expected["sha256"] and len(content) == expected["byte_length"], "provider-artifact-binding-mismatch")
            chunk = content[expected["offset"]:expected["offset"] + expected["length"]]
            result.update(offset=expected["offset"], length=len(chunk), chunk_sha256=hashlib.sha256(chunk).hexdigest())
        return result

    def _base_facts(self, status="pending", reason="not-observed"):
        return {**{name: fact("unsupported", "capability", reason=why) for name, why in UNSUPPORTED.items()},
                **{name: fact(status, "provider-owned-ledger" if name in {"correlation", "durable_outcome", "provider_artifact"}
                              else "aws-agentcore-session", reason=reason)
                   for name in ("correlation", "durable_outcome", "provider_artifact", "session_ttl", "termination")}}

    def _once(self, b, budget):
        table, partition = self._keys(b)
        family, action = b["operation"].split("___")
        facts = self._base_facts()
        content = None
        result_status = None
        if family == "websearch":
            row = self._row(budget, table, partition, "OP")
            self._identity(row, b)
            check(row.get("request_digest") == digest({"query": b["arguments"]["query"]}), "provider-request-digest-mismatch")
            check(row.get("state") in {"pending", "unknown", "confirmed"}, "provider-ledger-state-invalid")
            if row["state"] == "confirmed":
                content = row["result_json"].encode("utf-8")
                value = json.loads(content)
                check(len(content) <= 64000 and value.get("status") in {"completed", "empty"}
                      and isinstance(value.get("results"), list), "provider-search-outcome-invalid")
                result_status = value["status"]
            for name in ("session_ttl", "termination"):
                facts[name] = fact("unsupported", "capability", reason="stateless-family-no-session-proof")
        else:
            root = b["root"]
            session = self._row(budget, table, partition, "CODE_SESSION" if family == "code_interpreter" else "SESSION")
            check(session.get("execution_id") == b["execution_id"]
                  and session.get("deadline_at") == root["deadline_at"], "provider-session-root-mismatch")
            handle = digest([partition, root["request_id"]])
            if action != "start":
                check(b["arguments"]["session_id"] == handle, "expected-session-handle-mismatch")
            if family == "code_interpreter":
                check(session.get("request_id") == root["request_id"] and session.get("receipt_id") == root["receipt_id"]
                      and session.get("session_handle") == handle
                      and session.get("provider_identifier") == self.deployment["interpreter_arn"].rsplit("/", 1)[1],
                      "provider-session-root-mismatch")
                row = session if action in {"start", "close"} else self._row(budget, table, partition, "CODE_OP#" + b["provider_request_id"])
                self._identity(row, b)
                if action not in {"start", "close"}:
                    check(row.get("session_handle") == handle, "provider-operation-session-mismatch")
                    arguments = b["parent"]["arguments"] if action == "result" else b["arguments"]
                    keys = ("session_id", "path") if action == "file" else ("session_id", "code", "language")
                    check(row.get("request_digest") == digest({k: arguments[k] for k in keys}), "provider-request-digest-mismatch")
                    if action == "file":
                        check(row.get("operation") == "file", "provider-operation-kind-mismatch")
                    elif action == "result":
                        check(row.get("provider_task_id") == b["arguments"]["provider_id"], "provider-task-mismatch")
                if action not in {"start", "close"} and row.get("state") == "confirmed":
                    check(isinstance(row.get("result_json"), str), "confirmed-provider-result-missing")
                    if action != "file":
                        check(text_id(row.get("provider_task_id")), "confirmed-provider-task-missing")
                if row.get("result_json") and row.get("state") == "confirmed":
                    content = row["result_json"].encode("utf-8")
                    value = json.loads(content)
                    if action == "file":
                        content = base64.b64decode(value["data_base64"], validate=True)
                        check(len(content) <= 8192 and hashlib.sha256(content).hexdigest() == row.get("artifact_sha256")
                              and len(content) == row.get("artifact_length")
                              and value.get("content_sha256") == row.get("artifact_sha256")
                              and value.get("byte_length") == len(content)
                              and value.get("path") == b["arguments"]["path"], "provider-file-artifact-mismatch")
                    else:
                        check(len(content) <= 24000 and value.get("status") in {"completed", "failed", "cancelled"},
                              "provider-task-outcome-invalid")
                        result_status = value["status"]
                check(row.get("state") in {"unknown", "pending", "confirmed", "failed"}, "provider-ledger-state-invalid")
            else:
                start = self._row(budget, table, partition, "OP#" + root["request_id"])
                check(start.get("execution_id") == b["execution_id"] and start.get("request_id") == root["request_id"]
                      and start.get("receipt_id") == root["receipt_id"] and start.get("operation") == "start"
                      and start.get("execution_deadline_at") == root["deadline_at"]
                      and start.get("digest") == self._browser_digest("start", root["arguments"])
                      and session.get("start_id") == root["request_id"], "provider-browser-start-mismatch")
                check(session.get("session_id") in {None, handle}, "provider-session-handle-mismatch")
                check(start.get("generation") == 1 and session.get("generation", 0) >= 1,
                      "provider-browser-generation-mismatch")
                if start.get("provider_id") and session.get("provider_id"):
                    check(start["provider_id"] == session["provider_id"], "provider-start-session-id-mismatch")
                if start.get("provider_id") or session.get("provider_id"):
                    check(start.get("provider_dispatch") == "sent" and start.get("provider_deadline_at") == root["deadline_at"],
                          "provider-browser-dispatch-unbound")
                if not session.get("provider_id") and text_id(start.get("provider_id")):
                    # Cleanup source preserves a second immutable start identity.
                    # Observe it directly without repairing/mutating SESSION.
                    session = {**session, "provider_id": start["provider_id"], "observer_identity_source": "start-ledger"}
                row = start if action == "start" else self._row(budget, table, partition, "OP#" + b["provider_request_id"])
                self._identity(row, b)
                check(row.get("operation") == action and row.get("digest") == self._browser_digest(action, b["arguments"]),
                      "provider-request-digest-mismatch")
                check(row.get("state") in {"queued", "running", "unknown", "confirmed", "failed"}, "provider-ledger-state-invalid")
                if row.get("state") == "confirmed":
                    result = row.get("result", {})
                    check(result.get("status") == "confirmed" and result.get("session_id") == handle, "provider-browser-result-mismatch")
                    if action == "close":
                        check(result.get("termination_confirmed") is True, "provider-browser-close-unconfirmed")
                    if action != "close":
                        check(text_id(result.get("view_id")), "provider-browser-view-missing")
                        if action == "inspect":
                            check(result["view_id"] == b["arguments"]["view_id"], "provider-browser-view-mismatch")
                        artifact = result.get("artifact", {})
                        check(artifact.get("artifact_id") == b["provider_request_id"], "provider-artifact-id-mismatch")
                        stored = self._row(budget, table, partition, "ARTIFACT#" + b["provider_request_id"])
                        content = bytes(stored["content"])
                        check(hashlib.sha256(content).hexdigest() == stored.get("sha256") == artifact.get("content_sha256")
                              and len(content) == stored.get("byte_length") == artifact.get("byte_length"), "provider-browser-artifact-mismatch")
                        check(json.loads(content).get("view_id") == result["view_id"], "provider-browser-artifact-view-mismatch")
            try:
                facts.update(self._session_facts(b, budget, session))
            except Pending as error:
                facts.update({name: fact("pending", "aws-agentcore-session", reason=str(error))
                              for name in ("session_ttl", "termination")})
        facts["correlation"] = fact("verified", "provider-owned-ledger", row_key_sha256=digest([table, partition, row["arrived_at"]]),
                                    provider_request_id=b["provider_request_id"], provider_receipt_id=b["provider_receipt_id"])
        state = row["state"]
        if family == "code_interpreter" and action == "close" and session.get("cleanup_state") != "closed":
            state = "pending"
        facts["durable_outcome"] = fact("verified" if state in {"confirmed", "failed"} else "pending",
                                       "provider-owned-ledger", state=state)
        if result_status is not None:
            facts["durable_outcome"]["result_status"] = result_status
        facts["durable_outcome"]["scope"] = (
            "session-admission" if family == "code_interpreter" and action == "start" else
            "parent-session-cleanup" if family == "code_interpreter" and action == "close" else
            "parent-operation" if family == "code_interpreter" and action == "result" else "operation-outcome")
        if row.get("provider_task_id"):
            check(text_id(row["provider_task_id"]), "provider-task-id-invalid")
            facts["durable_outcome"]["provider_task_id"] = row["provider_task_id"]
        facts["provider_artifact"] = self._artifact(content, b) if content is not None else fact(
            "pending" if b["artifact"] is not None else "unsupported", "provider-owned-ledger", reason="no-bound-provider-artifact")
        return facts

    def _session_facts(self, b, budget, session):
        family = b["operation"].split("___")[0]
        root, cleanup = b["root"], b["cleanup"]
        provider_id = session.get("provider_id")
        if not text_id(provider_id):
            return {name: fact("pending", "aws-agentcore-session", reason="provider-session-id-unavailable")
                    for name in ("session_ttl", "termination")}
        field = "browserIdentifier" if family == "browser" else "codeInterpreterIdentifier"
        identifier = self.deployment["browser_identifier"] if family == "browser" else self.deployment["interpreter_arn"].rsplit("/", 1)[1]
        result = self._call(budget, "bedrock-agentcore", "get_browser_session" if family == "browser" else "get_code_interpreter_session",
                            **{field: identifier, "sessionId": provider_id})
        check(result.get(field) == identifier and result.get("sessionId") == provider_id, "provider-session-identity-mismatch")
        if family == "code_interpreter":
            check(result.get("name") == "adp-" + session["session_handle"], "provider-session-name-mismatch")
        check(result.get("status") in {"READY", "TERMINATED"}, "provider-session-state-invalid")
        ttl = result.get("sessionTimeoutSeconds")
        created = stamp(result["createdAt"])
        now = self.now().timestamp()
        check(type(ttl) is int and 1 <= ttl <= root["ttl_seconds"]
              and root["deadline_at"] - root["ttl_seconds"] - 1 <= created <= now + 1
              and created + ttl <= root["deadline_at"] + 1, "provider-session-lifetime-mismatch")
        requested = stamp(cleanup["requested_at"])
        elapsed = now - requested
        check(elapsed <= cleanup["max_seconds"], "cleanup-observation-outside-approved-window")
        closed = result["status"] == "TERMINATED"
        termination = fact("verified" if closed and elapsed >= 0 else "pending", "aws-agentcore-session",
                           provider_status=result["status"], session_id=provider_id, identifier=identifier,
                           observed_at=self.now().isoformat(), cleanup_trigger=cleanup["trigger"],
                           identity_source=session.get("observer_identity_source", "session-ledger"))
        if elapsed >= 0:
            termination["cleanup_elapsed_seconds"] = elapsed
        else:
            termination["reason"] = "cleanup-trigger-not-reached"
        return {"session_ttl": fact("verified", "aws-agentcore-session", session_id=provider_id,
                                    identifier=identifier, ttl_seconds=ttl, created_at=result["createdAt"].isoformat()
                                    if isinstance(result["createdAt"], datetime) else result["createdAt"], deadline_at=root["deadline_at"]),
                "termination": termination}

    def observe(self, *, operation_ref, execution_id, operation, client, timeout):
        key = (client, operation, operation_ref, execution_id)
        require(key in self.bindings, "operation-not-in-private-observer-binding")
        require(type(timeout) in (int, float) and math.isfinite(timeout) and 0 < timeout <= self.limits["timeout_seconds"],
                "observation-time-bound-invalid")
        b = self.bindings[key]
        budget = {"deadline": self.monotonic() + timeout, "remaining": self.limits["max_reads"], "reads": []}
        facts = self._base_facts()
        try:
            identity = self._call(budget, "sts", "get_caller_identity")
            role = self.deployment["observer_role_arn"].rsplit("/", 1)[1]
            check(identity.get("Account") == self.account and isinstance(identity.get("Arn"), str)
                  and re.fullmatch(rf"arn:aws:sts::{self.account}:assumed-role/{re.escape(role)}/[A-Za-z0-9_+=,.@-]+", identity["Arn"]),
                  "observer-caller-identity-mismatch")
            for attempt in range(self.limits["max_polls"]):
                try:
                    facts = self._once(b, budget)
                except Pending as error:
                    facts = self._base_facts(reason=str(error))
                if not any(f["status"] == "pending" for f in facts.values()):
                    break
                remaining = budget["deadline"] - self.monotonic()
                if attempt + 1 == self.limits["max_polls"] or budget["remaining"] == 0 or remaining <= 3:
                    break
                self.sleep(min(self.limits["poll_seconds"], remaining - 3))
        except Pending as error:
            facts = self._base_facts(reason=str(error))
        except Refused as error:
            facts = self._base_facts("refused", str(error))
        except (KeyError, TypeError, ValueError, OverflowError, AttributeError):
            facts = self._base_facts("refused", "malformed-provider-evidence")
        return {"version": 1, "adapter": "adp-core-provider-observer-v1", "acceptance": "not-evaluated",
                "transport_kind": "sdk-reader" if isinstance(self.read, SDKReader) else "injected-reader",
                "offline_only": not isinstance(self.read, SDKReader),
                "operation": operation, "client": client, "operation_ref": operation_ref, "execution_id": execution_id,
                "binding_sha256": digest(b), "authority_sha256": digest(self.authority),
                "deployment": {k: self.deployment[k] for k in ("account", "region", "provider_source_revision", "release_receipt_sha256")},
                "facts": facts, "reads": budget["reads"], "read_count": len(budget["reads"]),
                "read_attempt_count": self.limits["max_reads"] - budget["remaining"]}
