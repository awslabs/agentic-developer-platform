"""Observe and clean selected isolated HTTP API routes for two Products."""

import re
import time
from datetime import UTC, datetime

from installed_routes import invoke_routes, validate_installed_routes
from portability import validate_projects
from qualification import timestamp


def route_manifest(manifest, project, selection):
    return {**manifest, "recovery": {
        "scope": project["scope"], "owned_routes": project["owned_assets"],
        "retained_routes": project["retained_assets"], "installed_routes": selection,
        "bounds": manifest["portability"]["limits"]}}


def cleanup_request_budget(owned, read_requests):
    """Budget two cleanup inventories, per-route deletion and independent verification."""
    return 3 * read_requests - 2 + 3 * len(owned)


def validate_installed_assets(manifest):
    selected = manifest["portability"].get("installed_assets")
    projects = validate_projects(manifest)
    if (not isinstance(selected, dict) or set(selected) != {"projects"}
            or not isinstance(selected["projects"], list) or len(selected["projects"]) != 2
            or len(projects) != 2):
        raise ValueError("installed assets require two explicitly selected Product route inventories")
    receipts = [project.get("cleanup_receipt") for project in projects]
    if (any(not isinstance(receipt, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", receipt)
            for receipt in receipts) or len(set(receipts)) != len(receipts)):
        raise ValueError("installed cleanup requires distinct stable Product receipts")
    targets, scopes, roles, labels = set(), [], set(), set()
    reserve = 4
    for entry in selected["projects"]:
        if not isinstance(entry, dict) or set(entry) != {"scope", "api_id", "reader", "actor"}:
            raise ValueError("installed assets require scoped independent route roles")
        scope = entry["scope"]
        project = next((item for item in projects if item["scope"] == scope), None)
        if project is None or scope in scopes:
            raise ValueError("installed assets requested foreign or duplicate Product scope")
        scopes.append(scope)
        selection = {key: entry[key] for key in ("api_id", "reader", "actor")}
        scoped = route_manifest(manifest, project, selection)
        validate_installed_routes(scoped)
        reader = selection["reader"]
        target = (reader["account_id"], reader["region"], selection["api_id"])
        if reader["account_id"] != project["account_ref"] or target in targets:
            raise ValueError("installed assets must use separate APIs in the selected Product accounts")
        targets.add(target)
        for name in ("reader", "actor"):
            role = selection[name]["role_arn"]
            label = selection[name]["connection_label"]
            if role in roles or label in labels:
                raise ValueError("installed asset roles must be separate across Products")
            roles.add(role)
            labels.add(label)
        reserve += cleanup_request_budget(project["owned_assets"], 4) + 3 * 4
    if reserve >= manifest["portability"]["limits"]["max_requests"]:
        raise ValueError("installed assets leave no request capacity after reserving cleanup")
    return selected["projects"]


class InstalledPortabilityAssets:
    def __init__(self, target, entry):
        self.target = target
        self.scope = entry["scope"]
        self.project = next(project for project in target.manifest["projects"] if project["scope"] == self.scope)
        self.selection = {key: entry[key] for key in ("api_id", "reader", "actor")}
        self.owned, self.retained = self.project["owned_assets"], self.project["retained_assets"]
        self.read_requests = 4
        self.pending_reads = [self.owned, self.retained, self.retained]
        self.baseline = None
        self.cleanup_started = False
        self.cleaned = False

    @property
    def reserved(self):
        cleanup = 0 if self.cleaned else cleanup_request_budget(self.owned, self.read_requests)
        return cleanup + len(self.pending_reads) * self.read_requests

    def _scope(self, scope, refs):
        if (scope != self.scope or not isinstance(refs, list) or not refs or len(set(refs)) != len(refs)
                or not set(refs) <= set(self.owned + self.retained)):
            raise ValueError("installed asset request is outside the selected Product routes")

    def _invoke(self, operation, *, cleanup_verification=False, reserved_readback=False):
        target = self.target
        cleanup = operation == "cleanup" or self.cleanup_started
        expires = (timestamp(target.manifest["authorization"]["expires_at"]) - datetime.now(UTC)).total_seconds()
        deadline = min(target.deadline + (target.cleanup_grace if cleanup else 0), time.monotonic() + expires)
        reserve = target.cleanup_reserve
        if operation == "cleanup":
            reserve -= cleanup_request_budget(self.owned, self.read_requests) - self.read_requests
        elif cleanup_verification:
            reserve -= cleanup_request_budget(self.owned, self.read_requests)
        elif reserved_readback:
            reserve -= self.read_requests
        maximum = min(100, target.max_requests - target.calls - reserve)
        if maximum < 4 or time.monotonic() >= deadline:
            raise ValueError("installed assets exceeded request or time bounds")
        prefix = self.selection["api_id"] + ":"
        refs = self.owned if operation == "cleanup" else self.owned + self.retained
        request = {"operation": operation, "api_id": self.selection["api_id"], "scope": self.scope,
                   "qualification_run_id": target.run_id, "routes": [ref[len(prefix):] for ref in refs],
                   "max_requests": maximum}
        if operation == "cleanup":
            request.update(expected_routes={ref[len(prefix):]: self.baseline["routes"][ref[len(prefix):]]
                                            for ref in self.owned},
                           previous_deployments=self.baseline["deployments"],
                           expected_stage_routes=self._expected_stages())
        try:
            observed = invoke_routes(self.selection["actor" if operation == "cleanup" else "reader"],
                                     request, deadline=deadline)
            if (not isinstance(observed, dict) or observed.get("api_id") != self.selection["api_id"]
                    or observed.get("scope") != self.scope or observed.get("qualification_run_id") != target.run_id
                    or type(observed.get("requests")) is not int or not 4 <= observed["requests"] <= maximum
                    or not isinstance(observed.get("routes"), dict)
                    or any(not isinstance(route, dict) or route.get("RouteId") != identifier
                           or not isinstance(route.get("RouteKey"), str)
                           for identifier, route in observed["routes"].items())
                    or not isinstance(observed.get("deployments"), dict)
                    or any(not isinstance(stage, str) or not stage or not isinstance(deployment, str) or not deployment
                           for stage, deployment in observed["deployments"].items())
                    or not isinstance(observed.get("stage_routes"), dict)
                    or set(observed["stage_routes"]) != set(observed["deployments"])
                    or any(not isinstance(routes, dict) for routes in observed["stage_routes"].values())
                    or time.monotonic() >= deadline):
                raise ValueError("installed asset readback is foreign, incomplete or expired")
        except Exception:
            target.calls += maximum
            raise
        target.calls += observed["requests"]
        if operation == "read" and not self.cleaned:
            self.read_requests = max(self.read_requests, observed["requests"])
        return observed

    def read(self, scope, refs):
        self._scope(scope, refs)
        reserved_readback = (self.cleaned and bool(self.pending_reads) and refs == self.pending_reads[0]
                             and (len(self.pending_reads) > 1 or all(reader.cleaned for reader in self.target.asset_readers)))
        observed = self._invoke("read", reserved_readback=reserved_readback)
        prefix = self.selection["api_id"] + ":"
        inventory = {prefix + identifier: route for identifier, route in observed["routes"].items()}
        if self.baseline is None:
            if not set(self.owned + self.retained) <= set(inventory):
                raise ValueError("installed asset baseline omits selected owned or retained routes")
            keys = {route["RouteKey"] for route in inventory.values()}
            if any(set(routes) != keys for routes in observed["stage_routes"].values()):
                raise ValueError("serving routes differ from the independent asset baseline")
            self.target._journal("asset_baseline", "observed", self.project["cleanup_receipt"],
                                 scope=self.scope, evidence=observed)
            self.baseline = observed
        if reserved_readback:
            self.pending_reads.pop(0)
        return {ref: {"owner_scope": self.scope, "native": inventory[ref]} for ref in refs if ref in inventory}

    def _expected_stages(self):
        prefix = self.selection["api_id"] + ":"
        removed = {self.baseline["routes"][ref[len(prefix):]]["RouteKey"] for ref in self.owned}
        return {stage: {key: value for key, value in routes.items() if key not in removed}
                for stage, routes in self.baseline["stage_routes"].items()}

    def cleanup(self, scope, refs):
        self._scope(scope, refs)
        if refs != self.owned or self.baseline is None or self.cleaned:
            raise ValueError("installed cleanup needs the original exact owned asset baseline")
        self.target.cleanup_phase = True
        self.cleanup_started = True
        receipt = self.project["cleanup_receipt"]
        self.target._journal("cleanup_owned", "prepared", receipt, scope=self.scope)
        try:
            acknowledged = self._invoke("cleanup")
            observed = self._invoke("read", cleanup_verification=True)
            prefix = self.selection["api_id"] + ":"
            retained = {identifier: route for identifier, route in self.baseline["routes"].items()
                        if prefix + identifier not in self.owned}
            if (observed["routes"] != retained or observed["deployments"] != acknowledged["deployments"]
                    or set(observed["deployments"]) != set(self.baseline["deployments"])
                    or observed["stage_routes"] != self._expected_stages()):
                raise ValueError("independent asset cleanup did not preserve retained and serving routes")
        except Exception:
            self.target._journal("cleanup_owned", "uncertain", receipt, scope=self.scope)
            raise
        self.target._journal("cleanup_owned", "acknowledged", receipt, scope=self.scope, evidence=observed)
        self.cleaned = True
