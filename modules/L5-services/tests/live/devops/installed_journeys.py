"""Read journey results from installed Product routes and the selected native API."""

import hashlib
import json
import math
import os
import re
import time
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

from journey_driver import ENTRIES, FirstPartyDriver, selected_entries, validate_driver
from native_usage import TASK_API, UNAVAILABLE_REASON, verify_native_usage
from qualification import timestamp


def validate_installed_journeys(manifest, *, entries=ENTRIES):
    selected_entries(entries)
    selected = manifest.get("installed_journeys")
    if (not isinstance(selected, dict) or set(selected) - {"token_env", "tenant_id", "started_at", "runs"}
            or not re.fullmatch(r"[A-Z][A-Z0-9_]+", selected.get("token_env", ""))
            or not os.environ.get(selected["token_env"])
            or not isinstance(selected.get("tenant_id"), str) or not selected["tenant_id"].strip()):
        raise ValueError("installed journey readback needs a scoped independent Product credential")
    start = timestamp(selected.get("started_at"))
    expires = timestamp(manifest["authorization"]["expires_at"])
    if not max(timestamp(manifest["release"]["deployed_at"]), expires - timedelta(hours=24)) <= start < expires:
        raise ValueError("installed journey readback must start within this release authorization")
    other_tokens = {os.environ[channel["token_env"]] for channel in manifest.get("readback_channels", {}).values()}
    driver = manifest.get("journey_driver", {})
    other_tokens.update(os.environ.get(driver.get(key, "")) for key in ("api_token_env", "mcp_token_env"))
    if os.environ[selected["token_env"]] in other_tokens:
        raise ValueError("installed journey observations need independent read credentials")
    if "runs" in selected:
        validate_runs(selected["runs"], entries=entries)
    validate_driver(manifest, entries=entries)
    return selected


def release_readback_tokens(manifest):
    tokens = {os.environ[channel["token_env"]] for channel in manifest["readback_channels"].values()}
    if "installed_journeys" in manifest:
        tokens.add(os.environ[manifest["installed_journeys"]["token_env"]])
    return tokens


def release_readback_budget(manifest):
    return sum(limit["max_requests"] for limit in manifest["limits"].values()) if "installed_journeys" in manifest else 4


def validate_runs(runs, *, entries=ENTRIES):
    selected_entries(entries)
    if (not isinstance(runs, dict) or set(runs) != set(entries)
            or any(not isinstance(value, str) or not re.fullmatch(r"[\w-]{1,128}", value)
                   for value in runs.values()) or len(set(runs.values())) != len(entries)):
        raise ValueError("installed journeys require distinct identities for all selected entries")
    return runs


class InstalledJourneyReader(FirstPartyDriver):
    def __init__(self, manifest, *, deadline, max_requests, entries=ENTRIES):
        self.entries = selected_entries(entries)
        self.selection = validate_installed_journeys(manifest, entries=entries)
        config = {**manifest["journey_driver"], "api_token_env": self.selection["token_env"]}
        expires = timestamp(manifest["authorization"]["expires_at"])
        deadline = min(deadline, time.monotonic() + (expires - datetime.now(UTC)).total_seconds())
        super().__init__({**manifest, "journey_driver": config}, None,
                         deadline=deadline, max_requests=max_requests)

    def request(self, method, path, **kwargs):
        if method != "GET":
            raise ValueError("installed journey reader cannot perform actions")
        response = super().request(method, path, **kwargs)
        if path.startswith(self.path + "/runs/"):
            scope = response.get("scope") or {}
            admission = response.get("admission") or {}
            if (scope.get("tenant_id") != self.selection["tenant_id"]
                    or admission.get("scope") != scope
                    or response.get("input") != self.driver["inputs"][self.entry]):
                raise ValueError("installed journey returned a foreign or swapped run")
        return response

    def read_task(self, space_id, task_id):
        self.calls += 2
        if self.calls > self.max_requests:
            raise ValueError("installed native readback exceeded its request bound")
        return super().read_task(space_id, task_id)

    def read(self, runs, *, now=None):
        runs = validate_runs(runs, entries=self.entries)
        started = timestamp(self.selection["started_at"])
        if not started <= (now or datetime.now(UTC)) < timestamp(self.manifest["authorization"]["expires_at"]):
            raise ValueError("installed journey authorization expired or has not started")
        observations = []
        native_ids = set()
        used_requests = {}
        total_bound = self.max_requests
        for entry in self.entries:
            self.entry = entry
            run_id = runs[entry]
            capability = self.driver["inputs"][entry]["capability"]
            limit = self.manifest["limits"][capability]
            self.max_requests = min(total_bound, self.calls + limit["max_requests"] - used_requests.get(capability, 0))
            before = self.calls
            native_id = self.read_native(run_id)
            observed_at = now or datetime.now(UTC)
            readback = self.native_readbacks[run_id]
            run, task = readback["run"], readback["task"]
            if (run["scope"].get("tenant_id") != self.selection["tenant_id"]
                    or run["input"] != self.driver["inputs"][entry]
                    or run["admission"].get("scope") != run["scope"]
                    or native_id in native_ids):
                raise ValueError("installed journey returned a foreign, swapped or duplicate run")
            native_ids.add(native_id)
            created, updated = timestamp(task.get("createdAt")), timestamp(task.get("updatedAt"))
            if not started <= created <= updated <= observed_at:
                raise ValueError("installed native task is stale or has an invalid observation time")
            usage = {"requests": self.calls - before, "requests_scope": "qualification_readback",
                     "elapsed_seconds": math.ceil((updated - created).total_seconds()),
                     "duration_scope": "native_task_created_to_updated",
                     "cost_scope": "native_task", "cost_coverage": "unavailable",
                     "cost_source": TASK_API, "cost_reason": UNAVAILABLE_REASON,
                     "observed_at": observed_at.isoformat(), "native_task_id": task["taskId"]}
            verify_native_usage(usage, self.manifest["limits"][run["input"]["capability"]], minimum_requests=1)
            used_requests[capability] = used_requests.get(capability, 0) + self.calls - before
            encoded = json.dumps(readback, sort_keys=True, separators=(",", ":")).encode()
            observation = {"entry_point": entry, "run_id": run_id, "native_id": native_id,
                           "scope": self.driver["scope"], "status": "completed", "usage": usage,
                           "deployment_id": self.manifest["release"]["deployment_id"],
                           "qualification_run_id": self.manifest["authorization"]["run_id"],
                           "readback_ref": self.path + "/runs/" + quote(run_id, safe=""),
                           "readback_sha256": hashlib.sha256(encoded).hexdigest(),
                           "observed_at": observed_at.isoformat(), "readback": readback}
            if run_id in self.result_readbacks:
                observation["result_readback"] = self.result_readbacks[run_id]
            observations.append(observation)
        return observations
