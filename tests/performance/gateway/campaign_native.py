"""Native campaign transport and recovery for explicitly admitted owned workloads."""

import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from campaign_adp import ADPFixtureBackend, private_file, run_selected, sync_directory
from campaign_guard import NoRedirects
from campaign_recovery import RecoveryError


def private_reference(value):
    if not isinstance(value, str):
        raise RecoveryError("native credential reference is required")
    path = Path(value)
    if (not path.is_absolute() or path.is_symlink() or not path.parent.is_dir()
            or path.parent.is_symlink() or path.parent.stat().st_mode & 0o077):
        raise RecoveryError("native credential references require a private directory")
    return path


def validate_native_config(config):
    for field in ("gateway_admin_token_file", "aws_connection", "aws_region", "health_file", "health_owner",
                  "native_target", "native_aws_connection", "native_approved_role"):
        if not isinstance(config.get(field), str) or not config[field].strip():
            raise RecoveryError(f"native recovery configuration missing {field}")
    private_file(config["gateway_admin_token_file"])
    private_file(config["health_file"])
    for field in ("target", "native_target"):
        parsed = urllib.parse.urlsplit(config[field])
        if (parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password
                or parsed.query or parsed.fragment):
            raise RecoveryError("native targets require explicit HTTPS URLs without credentials")
    fixtures = config.get("fixtures")
    if not isinstance(fixtures, list) or not 1 <= len(fixtures) <= 200:
        raise RecoveryError("native inventory requires one to 200 owned workloads")
    identifiers, paths = set(), set()
    for fixture in fixtures:
        if not isinstance(fixture, dict) or fixture.get("kind") != "workload":
            raise RecoveryError("native inventory accepts workload fixtures only")
        for field in ("binding", "tenant", "root_human", "ownership_receipt", "workload_uid"):
            if not isinstance(fixture.get(field), str) or not fixture[field].strip():
                raise RecoveryError(f"native fixture missing {field}")
        if fixture["binding"] in identifiers:
            raise RecoveryError("duplicate native workload")
        identifiers.add(fixture["binding"])
        for field in ("attempt", "credential_epoch"):
            if type(fixture.get(field)) is not int or fixture[field] < 1:
                raise RecoveryError("native fixture requires an exact attempt and credential epoch")
        for field in ("run_credential_file", "workload_token_file", "token_file"):
            path = str(private_reference(fixture.get(field)))
            if path in paths:
                raise RecoveryError("native credentials cannot be shared across workload fixtures")
            paths.add(path)
        if fixture.get("parent") is not None and not isinstance(fixture["parent"], str):
            raise RecoveryError("native parent must be an explicitly inventoried invocation")
    by_id = {fixture["binding"]: fixture for fixture in fixtures}
    for fixture in fixtures:
        visited = {fixture["binding"]}
        parent = fixture.get("parent")
        while parent is not None:
            if parent not in by_id or parent in visited:
                raise RecoveryError("native inventory has missing ancestors or cyclic lineage")
            ancestor = by_id[parent]
            if any(ancestor[field] != fixture[field] for field in ("tenant", "root_human")):
                raise RecoveryError("native lineage crosses tenant or human ownership")
            visited.add(parent)
            parent = ancestor.get("parent")
    return config


def bound_fixture(config, identity):
    fixture = next((item for item in config["fixtures"] if item["binding"] == identity["id"]), None)
    if (identity["kind"] != "agent" or not fixture
            or any(fixture[field] != identity.get(field) for field in ("tenant", "root_human", "ownership_receipt", "token_file"))):
        raise RecoveryError("agent does not match its explicit native workload inventory")
    return fixture


def signed_request(config, fixture, method, route, payload, credentials, timeout):
    if (not isinstance(credentials, dict) or set(credentials) != {"run_credential", "workload_token"}
            or not all(isinstance(value, str) and value and "\n" not in value and "\r" not in value for value in credentials.values())
            or not credentials["run_credential"].startswith("adpr1.")):
        raise RecoveryError("native transport requires bound run and workload credentials, never human tokens")
    url = config["native_target"].rstrip("/") + route
    body = json.dumps(payload) if payload is not None else ""
    headers = {"Content-Type": "application/json", "X-Adp-Run-Credential": credentials["run_credential"],
               "X-Adp-Workload-Token": credentials["workload_token"], "X-Agent-RunId": fixture["binding"],
               "X-Agent-OrgId": fixture["tenant"]}
    document = {"url": url, "method": method, "body": body, "headers": headers,
                "region": config["aws_region"], "account": config["approved_account"], "role": config["native_approved_role"]}
    command = ["adp-cred", "assume", "--service", "aws", "--label", config["native_aws_connection"],
               "--exec", sys.executable, str(Path(__file__).with_name("campaign_signer.py"))]
    try:
        result = run_selected(command, input=json.dumps(document), timeout=timeout)
        if result.returncode:
            raise RecoveryError("selected native signer refused the request")
        signed = json.loads(result.stdout)
        if (not isinstance(signed, dict) or not signed.get("Authorization", "").startswith("AWS4-HMAC-SHA256 ")
                or any(signed.get(key) != value for key, value in headers.items())):
            raise RecoveryError("native signer returned a different request binding")
    except (OSError, ValueError, subprocess.TimeoutExpired):
        raise RecoveryError("selected native signer unavailable; no credential fallback") from None
    return urllib.request.Request(url, data=body.encode() if payload is not None else None, method=method, headers=signed)


def model_request(config, identity, route, payload, cache, timeout):
    backend = NativeFixtureBackend(config)
    backend.deadline = time.time() + timeout
    fixture = bound_fixture(config, identity)
    credentials = {"run_credential": private_file(fixture["run_credential_file"]).read_text().strip(),
                   "workload_token": private_file(fixture["workload_token_file"]).read_text().strip()}
    current = backend.native_binding(fixture, credentials)
    backend.verify_binding(fixture, current)
    if current != cache.get("binding"):
        raise RecoveryError("native execution changed after campaign enrollment")
    return signed_request(config, fixture, "POST", "/agent" + route, payload, credentials, backend._timeout())


class NativeFixtureBackend(ADPFixtureBackend):
    retryable_http_statuses = frozenset({500, 502, 503, 504})

    def __init__(self, config):
        self.config = validate_native_config(config)
        self.fixtures = {("secret", fixture["token_file"]): {**fixture, "binding": fixture["token_file"]}
                         for fixture in config["fixtures"]}
        self.deadline = None
        self.recovery_started = None
        self.path = Path(config["journal_dir"]) / (config["campaign"] + ".operations.json")
        self.state = json.loads(private_file(str(self.path)).read_text()) if self.path.exists() else {}
        self.retired_caches = set()

    def read(self, kind, binding):
        if kind != "secret":
            raise RecoveryError("native backend does not mutate developer quotas or identities")
        self._fixture(kind, binding)
        return self._secret_snapshot(binding)[0]

    def recovery_order(self, intents):
        return reversed(intents)

    def credential_unusable(self, binding, content):
        return binding in self.retired_caches

    def native_binding(self, fixture, credentials):
        request = signed_request(self.config, fixture, "GET", "/internal/v1/agent/campaign-binding", None, credentials, self._timeout())
        with urllib.request.build_opener(NoRedirects, urllib.request.ProxyHandler({})).open(request, timeout=self._timeout()) as response:
            content = bytearray()
            while not response.isclosed():
                response.fp.raw._sock.settimeout(self._timeout())
                chunk = response.read1(65536)
                if not chunk:
                    break
                content.extend(chunk)
                if len(content) > 1048576:
                    raise RecoveryError("native binding response exceeds bound")
            result = json.loads(content)
        if not isinstance(result, dict):
            raise RecoveryError("native binding response is invalid")
        return result

    def route(self, fixture, operation, operation_id=None):
        route = "/activity/invocations/" + urllib.parse.quote(fixture["binding"], safe="") + "/campaign-" + operation
        return route + ("/" + operation_id if operation_id else "")

    def verify_binding(self, fixture, captured):
        try:
            binding = captured["binding"]
            execution, grant = binding["execution"], binding["grant"]
            expected = {"invocation_id": {"S": fixture["binding"]}, "tenant_id": {"S": fixture["tenant"]},
                        "current_attempt": {"N": str(fixture["attempt"])},
                        "current_credential_epoch": {"N": str(fixture["credential_epoch"])},
                        "workload_binding": {"S": fixture["workload_uid"]}, "status": {"S": "active"}}
            if (any(execution.get(key) != value for key, value in expected.items())
                    or grant["authority_human_id"] != {"S": fixture["root_human"]}
                    or "dispatch" in grant.get("allowed_actions", {}).get("SS", [])
                    or len(captured["version"]) != 64):
                raise ValueError()
            parent = fixture.get("parent")
            if parent:
                ancestor = next(item for item in self.config["fixtures"] if item["binding"] == parent)
                if execution.get("parent_principal") != {"S": parent + "#" + str(ancestor["attempt"])}:
                    raise ValueError()
            elif execution.get("parent_principal"):
                raise ValueError()
        except (KeyError, TypeError, ValueError, StopIteration):
            raise RecoveryError("native binding differs from the owned bounded workload inventory") from None

    def setup_native(self, journal):
        setup_deadline = self.deadline
        self.deadline = min(setup_deadline, time.time() + journal.lease_seconds / 2)
        if not self.verify_baseline():
            raise RecoveryError("native setup requires verified baseline health")
        prepared = []
        for fixture in self.config["fixtures"]:
            journal.heartbeat()
            self.deadline = min(setup_deadline, time.time() + journal.lease_seconds / 2)
            credentials = {"run_credential": private_file(fixture["run_credential_file"]).read_text().strip(),
                           "workload_token": private_file(fixture["workload_token_file"]).read_text().strip()}
            captured = self.native_binding(fixture, credentials)
            self.verify_binding(fixture, captured)
            journal.heartbeat()
            self.deadline = min(setup_deadline, time.time() + journal.lease_seconds / 2)
            if self._http("GET", self.route(fixture, "binding")) != captured:
                raise RecoveryError("native and human-owned bindings disagree")
            journal.heartbeat()
            operation_id = str(uuid.uuid4())
            intent = {"id": operation_id, "fixture": fixture["binding"], "captured": captured,
                      "ownership_receipt": fixture["ownership_receipt"], "state": "intent", "cache": fixture["token_file"]}
            prepared.append((fixture, credentials, intent))
        with journal._locked() as record:
            journal._require_owner(record)
            if record.get("native_intents"):
                raise RecoveryError("native fixture setup already started; resume cleanup")
            record["version"] = 3
            record["native_intents"] = [intent for fixture, credentials, intent in prepared]
            journal._save(record)
        for fixture, credentials, intent in prepared:
            journal.heartbeat()
            self.deadline = min(setup_deadline, time.time() + journal.lease_seconds / 2)
            operation_id, captured = intent["id"], intent["captured"]
            with journal._locked() as record:
                journal._require_owner(record)
                receipt = self._http("PUT", self.route(fixture, "enrollment", operation_id), {"expected_version": captured["version"]})
                expected = {"tenant": fixture["tenant"], "invocation": fixture["binding"], "human": fixture["root_human"],
                            "expected_version": captured["version"], "operation_id": operation_id}
                if (receipt.get("status") != "prepared" or receipt.get("operation_id") != operation_id
                        or receipt.get("request") != expected or receipt.get("binding") != captured["binding"]
                        or receipt.get("version") != captured["version"]):
                    raise RecoveryError("native enrollment has no matching protected receipt")
                if Path(fixture["token_file"]).exists():
                    raise RecoveryError("native credential cache already exists")
                self.state[operation_id] = {"kind": "secret", "binding": fixture["token_file"], "native_invocation": fixture["binding"],
                    "before": {"value": None, "version": None, "owner": self.config["owner"]}}
                self._save()
                descriptor = os.open(fixture["token_file"], os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
                with os.fdopen(descriptor, "w") as output:
                    json.dump({"binding": captured, "credentials": credentials}, output)
                    output.flush()
                    os.fsync(output.fileno())
                sync_directory(Path(fixture["token_file"]).parent)
                self.state[operation_id]["after"] = self.read("secret", fixture["token_file"])
                self._save()
                next(item for item in record["native_intents"] if item["id"] == operation_id)["state"] = "prepared"
                journal._save(record)

    def recover_native(self, record, save):
        pending, holds = list(record.get("native_intents", [])), []
        by_id = {fixture["binding"]: fixture for fixture in self.config["fixtures"]}
        for intent in pending:
            fixture = by_id.get(intent["fixture"])
            if (not fixture or fixture["ownership_receipt"] != intent["ownership_receipt"]
                    or fixture["token_file"] != intent["cache"]):
                raise RecoveryError("native recovery inventory changed")
        while pending and time.time() < self.deadline:
            next_pending = []
            for intent in pending:
                fixture = by_id[intent["fixture"]]
                try:
                    receipt = self._http("POST", self.route(fixture, "stop", intent["id"]),
                                         {"expected_version": intent["captured"]["version"]})
                    if receipt.get("status") == "stop_requested":
                        next_pending.append(intent)
                        continue
                    if (receipt.get("status") != "workload_exited" or receipt.get("operation_id") != intent["id"]
                            or receipt.get("tenant") != fixture["tenant"] or receipt.get("invocation") != fixture["binding"]
                            or receipt.get("expected_version") != intent["captured"]["version"]):
                        raise RecoveryError("native cleanup has no matching durable exit receipt")
                    intent["exit_receipt"] = receipt
                    save(record)
                    self.retired_caches.add(fixture["token_file"])
                    operation = self.state.get(intent["id"])
                    if operation is not None:
                        self.resume_restore("secret", fixture["token_file"], self.config["owner"], intent["id"])
                        current = self.read("secret", fixture["token_file"])
                        if current["value"] is not None:
                            self.compare_restore("secret", fixture["token_file"], current["version"], None,
                                                 self.config["owner"], intent["id"])
                        elif operation.get("after") and "restored" not in operation:
                            raise RecoveryError("native credential cache disappeared without its cleanup receipt")
                    intent["state"] = "restored"
                    save(record)
                except (OSError, TimeoutError):
                    next_pending.append(intent)
                except RecoveryError as error:
                    holds.append({"kind": "workload", "binding": fixture["binding"], "reason": str(error)})
            pending = next_pending
            if pending:
                time.sleep(min(0.1, max(0, self.deadline - time.time())))
        holds.extend({"kind": "workload", "binding": intent["fixture"], "reason": "native_cleanup_deadline"} for intent in pending)
        return holds
