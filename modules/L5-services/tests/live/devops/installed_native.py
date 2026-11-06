"""Read retained portability investigations and the independently selected native task."""

import json
import math
import os
import re
import time
from datetime import UTC, datetime
from urllib.parse import quote

from installed_operations import validate_installed_operations
from installed_results import read_investigation_result
from journey_driver import read_provider_task
from native_usage import TASK_API, UNAVAILABLE_REASON, verify_native_usage
from qualification import timestamp


def validate_installed_native(manifest):
    if manifest["portability"].get("installed_native") is not True:
        raise ValueError("installed native readback must be explicitly selected")
    operations, access = validate_installed_operations(manifest)
    if any(entry["input"]["capability"] != "investigation" for entry in operations):
        raise ValueError("installed native result readback currently supports investigations")
    return operations, access


class InstalledNativeReader:
    def __init__(self, target, scope):
        operations, access = validate_installed_native(target.manifest)
        self.selected = next((entry for entry in access if entry["scope"] == scope), None)
        if self.selected is None or target.cleanup_phase:
            raise ValueError("installed native readback requires selected scope before cleanup")
        self.operation = next(entry for entry in operations if entry["scope"] == scope)
        self.project = next(entry for entry in target.manifest["projects"] if entry["scope"] == scope)
        self.target, self.scope = target, scope
        self.path = (f"/api/v1/products/{quote(scope['product_id'], safe='')}/projects/"
                     f"{quote(scope['project_id'], safe='')}/services/devops")
        self.started_calls = target.calls
        expires = timestamp(target.manifest["authorization"]["expires_at"])
        self.deadline = min(target.deadline, time.monotonic() + (expires - datetime.now(UTC)).total_seconds(),
                            time.monotonic() + self.project["limits"]["operation"]["deadline_seconds"])

    def _reserve(self, requests):
        if (self.target.calls + requests > self.target.max_requests - self.target.cleanup_reserve
                or self.target.calls - self.started_calls + requests > self.project["limits"]["operation"]["max_requests"]
                or time.monotonic() >= self.deadline):
            raise ValueError("installed native readback exceeds its request or time bound")

    def request(self, method, path, *, token_env="api_token_env", raw=False, max_bytes=65536):
        if method != "GET" or token_env != "api_token_env" or not path.startswith(self.path + "/"):
            raise ValueError("installed native reader only reads its selected Product")
        self._reserve(1)
        self.target.calls += 1
        url = self.target.manifest["journey_driver"]["gateway_url"].rstrip("/") + path
        with self.target.client.stream("GET", url, follow_redirects=False,
                                       headers={"Authorization": "Bearer " + os.environ[self.selected["product_token_env"]]},
                                       timeout=min(15, self.deadline - time.monotonic())) as response:
            if response.status_code != 200:
                raise ValueError("installed native Product readback unavailable or redirected")
            content = bytearray()
            for chunk in response.iter_bytes():
                content.extend(chunk)
                if len(content) > max_bytes:
                    raise ValueError("installed native Product readback is oversized")
        if time.monotonic() >= self.deadline:
            raise ValueError("installed native Product authority expired during readback")
        if raw:
            return bytes(content)
        observed = json.loads(content)
        if not isinstance(observed, dict):
            raise ValueError("installed native Product readback is invalid")
        return observed

    def _binding(self):
        binding = self.request("GET", self.path + "/bindings/" + quote(self.selected["binding_id"], safe=""))
        native = binding.get("native_resource_ref")
        provider = self.selected["provider"]
        if (binding.get("binding_id") != self.selected["binding_id"] or binding.get("state") != "ready"
                or binding.get("revision") != self.selected["binding_revision"] or binding.get("gaps") != []
                or type(binding.get("generation")) is not int or binding["generation"] < 1
                or not isinstance(binding.get("scope"), dict)
                or any(binding["scope"].get(key) != value for key, value in self.scope.items())
                or binding.get("provider_identity") != {key: provider[key] for key in ("account_id", "role_arn", "region")}
                or not isinstance(native, dict) or native.get("record_type") != "agent_space"
                or native.get("record_id") != self.selected["agent_space_id"]
                or any(native.get(key) != self.scope[key] for key in ("tenant_id", "project_id"))
                or native.get("source_connection_id") != provider["source_connection_id"]):
            raise ValueError("installed native binding is foreign, stale or not ready")
        return binding

    def _run(self, run_id, binding):
        run = self.request("GET", self.path + "/runs/" + quote(run_id, safe=""))
        admission = run.get("admission")
        if (run.get("run_id") != run_id or run.get("origin") != "adp_submitted"
                or run.get("input") != self.operation["input"] or run.get("scope") != binding["scope"]
                or not isinstance(admission, dict) or admission.get("scope") != run["scope"]
                or admission.get("service_id") != "devops"
                or admission.get("binding_generation") != binding["generation"]
                or admission.get("native_space_ref") != binding["native_resource_ref"]
                or admission.get("provider_identity") != binding["provider_identity"]
                or not isinstance(admission.get("connection_ref"), dict)
                or admission["connection_ref"].get("source_connection_id") != self.selected["provider"]["source_connection_id"]):
            raise ValueError("installed native run is foreign or differs from current authority")
        return run

    def read(self, run_id):
        if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", run_id):
            raise ValueError("installed native readback needs the original durable run identity")
        self._reserve(10)
        binding = self._binding()
        while True:
            self._reserve(9)
            run = self._run(run_id, binding)
            if run.get("state") == "succeeded":
                break
            if run.get("state") not in {"admitted", "materializing", "queued", "running", "collecting", "outcome_unknown"}:
                raise ValueError("installed native run did not complete; retain its original identity")
            time.sleep(min(2, self.deadline - time.monotonic()))
        if self._binding() != binding:
            raise ValueError("Product binding authority changed while waiting for native work")
        native = run.get("native_job_ref")
        if (not isinstance(native, dict) or native.get("record_type") != "native_job"
                or any(native.get(key) != self.scope[key] for key in ("tenant_id", "project_id"))
                or native.get("source_connection_id") != self.selected["provider"]["source_connection_id"]
                or any(not isinstance(native.get(key), str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", native[key])
                       for key in ("record_id", "record_revision"))):
            raise ValueError("installed native run has no scoped native task identity")
        self._reserve(2)
        self.target.calls += 2
        task = read_provider_task(self.selected["provider"], self.selected["agent_space_id"],
                                  native["record_revision"], deadline=self.deadline)
        if (not isinstance(task, dict) or task.get("agentSpaceId") != self.selected["agent_space_id"]
                or task.get("taskId") != native["record_revision"] or task.get("executionId") != native["record_id"]
                or task.get("status") != "COMPLETED" or task.get("taskType") != "INVESTIGATION"):
            raise ValueError("independent native task differs from the retained Product run")
        created, updated = timestamp(task.get("createdAt")), timestamp(task.get("updatedAt"))
        if not created <= updated <= datetime.now(UTC):
            raise ValueError("independent native task has invalid observation times")
        result = read_investigation_result(self, run, task)
        if self._run(run_id, binding) != run or self._binding() != binding:
            raise ValueError("Product run or binding authority changed during native readback")
        usage = {"requests": self.target.calls - self.started_calls, "requests_scope": "qualification_readback",
                 "elapsed_seconds": math.ceil((updated - created).total_seconds()),
                 "duration_scope": "native_task_created_to_updated", "cost_scope": "native_task",
                 "cost_coverage": "unavailable", "cost_source": TASK_API, "cost_reason": UNAVAILABLE_REASON,
                 "native_task_id": task["taskId"], "observed_at": datetime.now(UTC).isoformat()}
        verify_native_usage(usage, self.project["limits"]["operation"], minimum_requests=1)
        return {"scope": self.scope, "run_id": run_id, "status": "completed", "capability": "investigation",
                "native_job_id": native["record_id"], "result_ref": result["result_id"],
                "native_output": result["evidence_ref"], "source_sha": None, "deployment_id": None,
                "coverage": {"source_grounding": "unavailable", "deployment_identity": "unavailable"},
                "usage": usage, "readback": {"run": run, "task": task, "result": result}}
