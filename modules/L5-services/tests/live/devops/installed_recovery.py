"""Recovery submission and independent readback through installed Product routes."""

import json
import os
import re
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

from journey_driver import validate_driver
from qualification import timestamp
from recovery import FAULTS


def validate_installed_recovery(manifest):
    recovery = manifest["recovery"]
    selected = recovery.get("installed_work")
    if (not isinstance(selected, dict) or set(selected) != {
            "read_token_env", "submit_token_env", "inputs", "agent_space_id", "binding_revision"}
            or not re.fullmatch(r"[\w-]{1,128}", selected.get("agent_space_id", ""))
            or type(selected.get("binding_revision")) is not int or selected["binding_revision"] < 1
            or not isinstance(selected.get("inputs"), dict) or set(selected["inputs"]) != set(FAULTS)):
        raise ValueError("installed recovery needs pinned binding and per-fault inputs")
    driver = validate_driver(manifest)
    if (any(recovery["scope"].get(field) != driver["scope"][field] for field in ("product_id", "project_id"))
            or recovery["binding_id"] != driver["scope"]["binding_id"]):
        raise ValueError("installed recovery belongs to another Product binding")
    for body in selected["inputs"].values():
        if (not isinstance(body, dict) or body.get("service_id") != "devops"
                or body.get("binding_id") != recovery["binding_id"]
                or body.get("capability") != "investigation"
                or type(body.get("configuration_revision")) is not int or body["configuration_revision"] < 1
                or not isinstance(body.get("devops_input"), dict)
                or body["devops_input"].get("kind") != "investigation"):
            raise ValueError("installed recovery needs an investigation on the selected binding")
    tokens = []
    for field in ("read_token_env", "submit_token_env"):
        name = selected.get(field)
        if not isinstance(name, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]+", name) or not os.environ.get(name):
            raise ValueError("installed recovery requires private Product credentials")
        tokens.append(os.environ[name])
    if tokens[0] == tokens[1]:
        raise ValueError("installed recovery action and readback credentials must differ")
    return selected


def read_provider_recovery(provider, selection, *, deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ValueError("native recovery exceeded its deadline")
    command = ["adp-cred", "assume", "--service", "aws", "--label", provider["connection_label"],
               "--exec", sys.executable, str(Path(__file__).with_name("provider_recovery.py")),
               provider["account_id"], provider["role_arn"], provider["region"]]
    try:
        response = subprocess.run(command, input=json.dumps(selection).encode(), capture_output=True,
                                  timeout=min(remaining, 30), check=False)
        if response.returncode or len(response.stdout) > 1024 * 1024:
            raise ValueError("selected native recovery readback unavailable")
        return json.loads(response.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        raise ValueError("selected native recovery readback unavailable") from None


class InstalledRecoveryWork:
    def __init__(self, target, manifest):
        self.target = target
        self.manifest = manifest
        self.selection = validate_installed_recovery(manifest)
        self.recovery = manifest["recovery"]
        self.driver = manifest["journey_driver"]
        scope = self.recovery["scope"]
        self.path = (f"/api/v1/products/{quote(scope['product_id'], safe='')}/projects/"
                     f"{quote(scope['project_id'], safe='')}/services/devops")
        self.runs = {}

    def remaining(self, *, after_read=False):
        expires = (timestamp(self.manifest["authorization"]["expires_at"]) - datetime.now(UTC)).total_seconds()
        cleanup = getattr(self.target, "cleanup_phase", False)
        grace = self.target.cleanup_grace if cleanup else 0
        remaining = min(expires, self.target.deadline + grace - time.monotonic())
        reserve = 0 if cleanup else getattr(self.target, "cleanup_reserve", 4)
        exhausted = (self.target.calls > self.target.max_requests - reserve if after_read
                     else self.target.calls >= self.target.max_requests - reserve)
        if remaining <= 0 or exhausted:
            raise ValueError("installed recovery exceeded its request or time bound")
        return remaining

    def request(self, method, path, *, body=None, receipt=None):
        remaining = self.remaining()
        self.target.calls += 1
        token = self.selection["submit_token_env" if method == "POST" else "read_token_env"]
        headers = {"Authorization": "Bearer " + os.environ[token]}
        if receipt:
            headers["Idempotency-Key"] = receipt
        with self.target.client.stream(method, self.driver["gateway_url"].rstrip("/") + path,
                                       headers=headers, json=body, follow_redirects=False,
                                       timeout=min(remaining, 15)) as response:
            if response.status_code not in ({200, 202} if method == "POST" else {200}):
                raise ValueError("installed Product recovery authority unavailable")
            content = bytearray()
            for chunk in response.iter_bytes():
                content.extend(chunk)
                if len(content) > 1024 * 1024:
                    raise ValueError("installed recovery response exceeded its byte bound")
        observed = json.loads(content)
        if not isinstance(observed, dict):
            raise ValueError("installed recovery returned an invalid observation")
        return observed

    def binding(self, scope):
        if scope != self.recovery["scope"]:
            raise ValueError("installed recovery requested foreign scope")
        binding = self.request("GET", self.path + "/bindings/" + quote(self.recovery["binding_id"], safe=""))
        native = binding.get("native_resource_ref") or {}
        provider = self.driver["provider_readback"]
        if (binding.get("binding_id") != self.recovery["binding_id"] or binding.get("state") != "ready"
                or binding.get("revision") != self.selection["binding_revision"]
                or not isinstance(binding.get("scope"), dict)
                or any(binding["scope"].get(field) != value for field, value in scope.items())
                or any((binding.get("provider_identity") or {}).get(field) != provider[field]
                       for field in ("account_id", "role_arn", "region"))
                or any(native.get(field) != scope[field] for field in ("tenant_id", "project_id"))
                or native.get("source_connection_id") != provider["source_connection_id"]
                or native.get("record_type") != "agent_space"
                or native.get("record_id") != self.selection["agent_space_id"]):
            raise ValueError("installed recovery binding is foreign, stale or not ready")
        return binding

    def submit(self, scope, fault, receipt):
        if getattr(self.target, "cleanup_phase", False):
            raise ValueError("recovery cannot submit during cleanup")
        if fault not in FAULTS or receipt != self.recovery["receipts"][fault]:
            raise ValueError("recovery submission differs from its authorized receipt")
        self.binding(scope)
        self.target._journal("submit", "prepared", receipt)
        try:
            run = self.request("POST", self.path + "/runs", body=self.selection["inputs"][fault], receipt=receipt)
        except self.target.httpx.TimeoutException as error:
            self.target._journal("submit", "uncertain", receipt)
            raise TimeoutError("installed recovery submission uncertain; replay the same receipt") from error
        except Exception:
            self.target._journal("submit", "uncertain", receipt)
            raise
        run_id = run.get("run_id")
        if not isinstance(run_id, str) or not re.fullmatch(r"[\w-]{1,128}", run_id):
            raise ValueError("installed recovery did not return a run identity")
        if receipt in self.runs and self.runs[receipt] != run_id:
            raise ValueError("recovery receipt resolved to another run")
        if run_id in {value for key, value in self.runs.items() if key != receipt}:
            raise ValueError("independent recovery receipts resolved to the same run")
        self.runs[receipt] = run_id
        self.target._journal("submit", "acknowledged", receipt)

    def read_work(self, scope, receipt):
        faults = [fault for fault, selected in self.recovery["receipts"].items() if selected == receipt]
        if len(faults) != 1:
            raise ValueError("unknown recovery receipt")
        fault = faults[0]
        if receipt not in self.runs:
            self.submit(scope, fault, receipt)
        binding = self.binding(scope)
        run_id = self.runs[receipt]
        while True:
            run = self.request("GET", self.path + "/runs/" + quote(run_id, safe=""))
            admission = run.get("admission") or {}
            native = run.get("native_job_ref") or {}
            if (run.get("run_id") != run_id
                    or run.get("origin") != "adp_submitted" or run.get("input") != self.selection["inputs"][fault]
                    or admission.get("binding_id") != binding["binding_id"]
                    or admission.get("binding_generation") != binding.get("generation")
                    or run.get("scope") != binding["scope"] or admission.get("scope") != binding["scope"]
                    or admission.get("native_space_ref") != binding["native_resource_ref"]
                    or admission.get("connection_ref") != binding.get("connection_ref")
                    or any((admission.get("provider_identity") or {}).get(field) != self.driver["provider_readback"][field]
                           for field in ("account_id", "role_arn", "region"))):
                raise ValueError("installed recovery run is foreign or changed")
            if run.get("state") == "succeeded":
                break
            if run.get("state") not in {"admitted", "queued", "running", "collecting", "outcome_unknown"}:
                raise ValueError("installed recovery did not complete")
            time.sleep(min(self.remaining(), 2))
        if (run.get("provider_state") != "completed" or native.get("record_type") != "native_job"
                or not all(isinstance(native.get(field), str) and native[field] for field in ("record_id", "record_revision"))
                or any(native.get(field) != scope[field] for field in ("tenant_id", "project_id"))
                or native.get("source_connection_id") != self.driver["provider_readback"]["source_connection_id"]):
            raise ValueError("installed recovery native identity is incomplete")
        if self.binding(scope) != binding:
            raise ValueError("installed recovery binding changed before native readback")
        allocation = self.target.max_requests - self.target.calls - getattr(self.target, "cleanup_reserve", 4)
        observation = read_provider_recovery(self.driver["provider_readback"], {
            "kind": "work", "space_id": self.selection["agent_space_id"], "run_id": run_id,
            "task_id": native["record_revision"], "max_requests": allocation},
            deadline=time.monotonic() + self.remaining())
        calls = observation.get("requests")
        if type(calls) is not int or not 4 <= calls <= allocation:
            raise ValueError("native recovery request coverage is unavailable")
        self.target.calls += calls
        task = observation.get("task") or {}
        description = json.loads(task.get("description", "null"))
        options = run["input"]["devops_input"]
        if (task.get("agentSpaceId") != self.selection["agent_space_id"]
                or task.get("taskId") != native["record_revision"] or task.get("executionId") != native["record_id"]
                or task.get("taskType") != "INVESTIGATION" or task.get("status") != "COMPLETED"
                or observation.get("matching_task_ids") != [native["record_revision"]]
                or not isinstance(description, dict) or description.get("run_id") != run_id
                or description.get("scope") != run["scope"]
                or description.get("binding_id") != admission["binding_id"]
                or description.get("binding_generation") != admission["binding_generation"]
                or description.get("configuration_revision") != run["input"]["configuration_revision"]
                or any(description.get(field) != options.get(field) for field in ("question", "resource_refs", "telemetry_refs"))
                or not description.get("attempt_id") or not description.get("idempotency_token")):
            raise ValueError("independent native recovery differs from the admitted run")
        created, updated = timestamp(task.get("createdAt")), timestamp(task.get("updatedAt"))
        if not timestamp(self.manifest["release"]["deployed_at"]) <= created <= updated <= datetime.now(UTC):
            raise ValueError("native recovery task observation is stale or invalid")
        self.remaining(after_read=True)
        return {"scope": scope, "receipt": receipt, "status": "completed", "work_id": run_id,
                "work_identity_scope": "admitted_run", "native_job_id": task["executionId"],
                "native_submit_count": len(observation["matching_task_ids"]),
                "submit_count_coverage": "retained_native_tasks_not_api_attempts",
                "readback": {"binding": binding, "run": run, "native": observation}}

    def read_spaces(self, scope):
        self.binding(scope)
        cleanup = getattr(self.target, "cleanup_phase", False)
        reserve = 0 if cleanup else getattr(self.target, "cleanup_reserve", 4)
        allocation = self.target.max_requests - self.target.calls - reserve
        observed = read_provider_recovery(self.driver["provider_readback"], {
            "kind": "spaces", "space_id": self.selection["agent_space_id"], "max_requests": allocation},
            deadline=time.monotonic() + self.remaining())
        calls = observed.get("requests")
        spaces = observed.get("spaces")
        if (type(calls) is not int or not 3 <= calls <= allocation
                or not isinstance(spaces, dict) or self.selection["agent_space_id"] not in spaces
                or any(not isinstance(record, dict) or not isinstance(record.get("space"), dict)
                       or record["space"].get("agentSpaceId") != identifier or not isinstance(record.get("tags"), dict)
                       for identifier, record in spaces.items())):
            raise ValueError("complete native space inventory is unavailable")
        self.target.calls += calls
        if not cleanup:
            self.target.cleanup_reserve = getattr(self.target, "route_cleanup_reserve", 4) + 1 + calls
        self.remaining(after_read=True)
        return spaces
