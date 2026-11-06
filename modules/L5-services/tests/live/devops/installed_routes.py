"""Recovery route adapters using independent selected AWS read and action roles."""

import json
import re
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from qualification import timestamp


def validate_installed_routes(manifest):
    recovery = manifest["recovery"]
    selected = recovery.get("installed_routes")
    if (not isinstance(selected, dict) or set(selected) != {"api_id", "reader", "actor"}
            or not re.fullmatch(r"[a-z0-9]{1,20}", selected.get("api_id", ""))):
        raise ValueError("installed recovery routes require one isolated HTTP API")
    for name in ("reader", "actor"):
        authority = selected[name]
        if (not isinstance(authority, dict) or set(authority) != {"connection_label", "account_id", "role_arn", "region"}
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", authority.get("connection_label", ""))
                or not re.fullmatch(r"[0-9]{12}", authority.get("account_id", ""))
                or not re.fullmatch(rf"arn:aws:iam::{authority['account_id']}:role/[\w+=,.@/-]+",
                                    authority.get("role_arn", ""))
                or not re.fullmatch(r"[a-z]{2}(?:-[a-z]+)+-[0-9]", authority.get("region", ""))):
            raise ValueError("installed recovery routes require exact selected AWS roles")
    reader, actor = selected["reader"], selected["actor"]
    if (reader["connection_label"] == actor["connection_label"] or reader["role_arn"] == actor["role_arn"]
            or any(reader[field] != actor[field] for field in ("account_id", "region"))):
        raise ValueError("installed route actions and observations require independent roles in the same target")
    prefix = selected["api_id"] + ":"
    if any(not route.startswith(prefix) or not re.fullmatch(r"[a-z0-9]{1,20}", route[len(prefix):])
           for route in recovery["owned_routes"] + recovery["retained_routes"]):
        raise ValueError("recovery routes must belong to the selected HTTP API")
    if cleanup_reserve(manifest) >= recovery["bounds"]["max_requests"]:
        raise ValueError("installed routes require a separate cleanup request reserve")
    return selected


def cleanup_reserve(manifest):
    return 16 + 3 * len(manifest["recovery"]["owned_routes"])


def invoke_routes(authority, selection, *, deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ValueError("installed route operation exceeded its deadline")
    command = ["adp-cred", "assume", "--service", "aws", "--label", authority["connection_label"],
               "--exec", sys.executable, str(Path(__file__).with_name("provider_routes.py")),
               authority["account_id"], authority["role_arn"], authority["region"]]
    try:
        result = subprocess.run(command, input=json.dumps(selection).encode(), capture_output=True,
                                timeout=min(remaining, 60), check=False)
        if result.returncode or len(result.stdout) > 1024 * 1024:
            raise ValueError("installed route operation unavailable; reconcile the private journal")
        return json.loads(result.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        raise ValueError("installed route operation unavailable; reconcile the private journal") from None


class InstalledRecoveryRoutes:
    def __init__(self, target, manifest):
        self.target = target
        self.manifest = manifest
        self.selection = validate_installed_routes(manifest)
        self.scope = manifest["recovery"]["scope"]
        self.run_id = manifest["authorization"]["run_id"]
        self.owned = manifest["recovery"]["owned_routes"]
        self.retained = manifest["recovery"]["retained_routes"]
        self.baseline = None
        self.baseline_deployments = None
        self.observed_deployments = None
        self.baseline_stage_routes = None
        self.observed_stage_routes = None
        self.read_requests = 3

    def _scope(self, scope, routes=None):
        if (scope != self.scope or routes is not None and (
                not isinstance(routes, list) or len(set(routes)) != len(routes)
                or not set(routes) <= set(self.owned + self.retained))):
            raise ValueError("installed routes requested foreign scope")

    def _invoke(self, operation, routes, *, following_reads=0):
        cleanup = self.target.cleanup_phase
        expires = (timestamp(self.manifest["authorization"]["expires_at"]) - datetime.now(UTC)).total_seconds()
        deadline = min(self.target.deadline + (self.target.cleanup_grace if cleanup else 0),
                       time.monotonic() + expires)
        remaining = self.target.max_requests - self.target.calls - (0 if cleanup else self.target.cleanup_reserve)
        if operation == "cleanup":
            remaining -= 2 * self.read_requests + self.target.cleanup_reserve - self.target.route_cleanup_reserve
        elif cleanup:
            remaining -= following_reads * self.read_requests + self.target.cleanup_reserve - self.target.route_cleanup_reserve
        if deadline <= time.monotonic() or remaining < 4:
            raise ValueError("installed routes exceeded request or time bounds")
        prefix = self.selection["api_id"] + ":"
        request = {"operation": operation, "api_id": self.selection["api_id"], "scope": self.scope,
                   "qualification_run_id": self.run_id, "routes": [route[len(prefix):] for route in routes],
                   "max_requests": min(100, remaining)}
        if operation == "cleanup":
            request["expected_routes"] = {route[len(prefix):]: self.baseline[route] for route in routes}
            request["previous_deployments"] = self.baseline_deployments
            request["expected_stage_routes"] = self.expected_stage_routes()
        try:
            observed = invoke_routes(self.selection["actor" if operation == "cleanup" else "reader"],
                                     request, deadline=deadline)
            if (not isinstance(observed, dict) or observed.get("api_id") != self.selection["api_id"]
                    or observed.get("scope") != self.scope or observed.get("qualification_run_id") != self.run_id
                    or type(observed.get("requests")) is not int or not 3 <= observed["requests"] <= remaining
                    or not isinstance(observed.get("routes"), dict)
                    or not isinstance(observed.get("deployments"), dict)
                    or not isinstance(observed.get("stage_routes"), dict)
                    or set(observed["stage_routes"]) != set(observed["deployments"])
                    or any(not isinstance(definitions, dict) for definitions in observed["stage_routes"].values())
                    or any(not isinstance(stage, str) or not stage or not isinstance(deployment, str) or not deployment
                           for stage, deployment in observed["deployments"].items())
                    or any(not isinstance(route, str) or not re.fullmatch(r"[a-z0-9]{1,20}", route)
                           or not isinstance(value, dict) or value.get("RouteId") != route
                           for route, value in observed["routes"].items())):
                raise ValueError("installed route observation is foreign or incomplete")
        except Exception:
            self.target.calls += request["max_requests"]
            raise
        self.target.calls += observed["requests"]
        self.observed_deployments = observed["deployments"]
        self.observed_stage_routes = observed["stage_routes"]
        if not cleanup:
            self.read_requests = max(self.read_requests, observed["requests"])
            reserve = 4 * self.read_requests - 2 + 3 * len(self.owned)
            increase = max(0, reserve - self.target.route_cleanup_reserve)
            self.target.route_cleanup_reserve += increase
            self.target.cleanup_reserve += increase
        return {prefix + route: value for route, value in observed["routes"].items()}

    def read_route_inventory(self, scope):
        self._scope(scope)
        inventory = self._invoke("read", self.owned + self.retained)
        if self.baseline is None:
            if not set(self.owned + self.retained) <= set(inventory):
                raise ValueError("owned and retained route baseline is incomplete")
            route_keys = {route["RouteKey"] for route in inventory.values()}
            if any(set(definitions) != route_keys for definitions in self.observed_stage_routes.values()):
                raise ValueError("serving routes differ from the control-plane baseline")
            self.target._journal("route_baseline", "observed", self.run_id + ":routes",
                                 evidence={"api_id": self.selection["api_id"], "scope": self.scope,
                                           "routes": inventory, "deployments": self.observed_deployments,
                                           "stage_routes": self.observed_stage_routes})
            self.baseline = inventory
            self.baseline_deployments = self.observed_deployments
            self.baseline_stage_routes = self.observed_stage_routes
        return inventory

    def read_routes(self, scope, routes):
        self._scope(scope, routes)
        inventory = self.read_route_inventory(scope)
        return {route: inventory[route] for route in routes if route in inventory}

    def read_route_snapshot(self, scope):
        inventory = self.read_route_inventory(scope)
        return {"inventory": inventory,
                "owned": {route: inventory[route] for route in self.owned if route in inventory},
                "retained": {route: inventory[route] for route in self.retained if route in inventory},
                "owners": {route: dict(self.scope) for route in self.owned if route in inventory}}

    def read_route_owners(self, scope, routes):
        self._scope(scope, routes)
        observed = self.read_routes(scope, routes)
        return {route: dict(self.scope) for route in observed}

    def expected_stage_routes(self):
        removed = {self.baseline[route]["RouteKey"] for route in self.owned}
        return {stage: {key: value for key, value in definitions.items() if key not in removed}
                for stage, definitions in self.baseline_stage_routes.items()}

    def cleanup_routes(self, scope, routes):
        self._scope(scope, routes)
        if routes != self.owned or self.baseline is None:
            raise ValueError("cleanup requires the exact owned route baseline")
        self.target.cleanup_phase = True
        receipt = self.run_id + ":routes"
        self.target._journal("cleanup_routes", "prepared", receipt)
        try:
            self._invoke("cleanup", routes)
            completed_deployments = self.observed_deployments
            remaining = self._invoke("read", self.owned + self.retained, following_reads=1)
            if remaining != {route: value for route, value in self.baseline.items() if route not in self.owned}:
                raise ValueError("independent route cleanup readback differs from the retained baseline")
            if (self.observed_deployments != completed_deployments
                    or set(self.observed_deployments) != set(self.baseline_deployments)
                    or self.observed_stage_routes != self.expected_stage_routes()):
                raise ValueError("independent serving routes differ from the retained baseline")
        except Exception:
            self.target._journal("cleanup_routes", "uncertain", receipt)
            raise
        self.target._journal("cleanup_routes", "acknowledged", receipt,
                             evidence={"api_id": self.selection["api_id"], "scope": self.scope,
                                       "deployments": self.observed_deployments,
                                       "stage_routes": self.observed_stage_routes})
