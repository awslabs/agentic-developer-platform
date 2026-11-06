"""Confirm prepared portability configurations through installed Product APIs."""

import json
import os
import re
import time
from datetime import UTC, datetime
from urllib.parse import quote

from installed_operations import validate_installed_operations
from qualification import timestamp


def validate_installed_configuration(manifest):
    if manifest["portability"].get("installed_configuration") is not True:
        raise ValueError("installed configuration must be explicitly selected")
    operations, access = validate_installed_operations(manifest)
    receipts = [project.get("configuration_receipt") for project in manifest["projects"]]
    if (any(not isinstance(receipt, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", receipt)
            for receipt in receipts) or len(set(receipts)) != len(receipts)):
        raise ValueError("installed configuration requires distinct stable receipts")
    return operations, access


def _reference(reference, scope, kinds):
    return (isinstance(reference, dict) and set(reference) == {
                "tenant_id", "project_id", "source_connection_id", "record_type", "record_id", "record_revision"}
            and all(isinstance(value, str) and value for value in reference.values())
            and all(reference[key] == scope[key] for key in ("tenant_id", "project_id"))
            and reference["record_type"] in kinds
            and reference["record_revision"].lower() not in {"head", "latest", "null"})


class InstalledConfiguration:
    def __init__(self, target, scope):
        operations, access = validate_installed_configuration(target.manifest)
        self.selected = next((entry for entry in access if entry["scope"] == scope), None)
        if self.selected is None:
            raise ValueError("installed configuration requested a foreign Product")
        self.operation = next(entry for entry in operations if entry["scope"] == scope)
        self.project = next(entry for entry in target.manifest["projects"] if entry["scope"] == scope)
        self.target, self.scope = target, scope
        self.path = (f"/api/v1/products/{quote(scope['product_id'], safe='')}/projects/"
                     f"{quote(scope['project_id'], safe='')}")
        self.service = self.path + "/services/devops"
        expires = timestamp(target.manifest["authorization"]["expires_at"])
        self.deadline = min(target.deadline, time.monotonic() + (expires - datetime.now(UTC)).total_seconds())

    def _reserve(self, requests):
        if (self.target.calls + requests > self.target.max_requests - self.target.cleanup_reserve
                or time.monotonic() >= self.deadline):
            raise ValueError("installed configuration exceeds its request or time bound")

    def _request(self, method, path):
        self._reserve(1)
        token_env = self.operation["submit_token_env"] if method == "POST" else self.selected["product_token_env"]
        headers = {"Authorization": "Bearer " + os.environ[token_env]}
        if method == "POST":
            headers["Idempotency-Key"] = self.project["configuration_receipt"]
        self.target.calls += 1
        url = self.target.manifest["journey_driver"]["gateway_url"].rstrip("/") + path
        with self.target.client.stream(method, url, headers=headers, follow_redirects=False,
                                       timeout=min(15, self.deadline - time.monotonic())) as response:
            if response.status_code != 200:
                raise ValueError("installed configuration unavailable or redirected")
            content = bytearray()
            for chunk in response.iter_bytes():
                content.extend(chunk)
                if len(content) > 1024 * 1024:
                    raise ValueError("installed configuration response is oversized")
        observed = json.loads(content)
        if not isinstance(observed, dict) or time.monotonic() >= self.deadline:
            raise ValueError("installed configuration response is invalid or expired")
        return observed

    def _profile(self):
        profile = self._request("GET", self.path)
        if (any(profile.get(key) != value for key, value in self.scope.items())
                or type(profile.get("revision")) is not int or profile["revision"] < 1
                or profile.get("archived_at") is not None or not isinstance(profile.get("product"), dict)
                or not isinstance(profile.get("project"), dict) or not isinstance(profile.get("configuration"), dict)
                or profile["product"].get("team_id") != self.project["team_ref"]
                or profile["project"].get("id") != self.scope["project_id"]
                or profile["project"].get("tenant_id") != self.scope["tenant_id"]
                or profile["project"].get("state") == "archived"):
            raise ValueError("installed Product profile is foreign, archived or changed")
        selections = profile["configuration"].get("selected_capabilities")
        if (not isinstance(selections, list) or len([
                selection for selection in selections if isinstance(selection, dict)
                and selection.get("capability") == "devops"
                and selection.get("ownership_mode") in {"managed", "integrated", "observed"}]) != 1):
            raise ValueError("DevOps is no longer selected by the Product")
        return {key: profile[key] for key in ("tenant_id", "product_id", "project_id", "revision",
                                             "product", "project", "configuration")}

    def read(self):
        self._reserve(5)
        profile = self._profile()
        configuration = self._request("GET", self.service + "/configuration")
        binding = self._request("GET", self.service + "/bindings/" + quote(self.selected["binding_id"], safe=""))
        settings = configuration.get("settings")
        if (not isinstance(settings, dict) or settings.get("service_id") != "devops"
                or not isinstance(configuration.get("scope"), dict)
                or any(configuration["scope"].get(key) != value for key, value in self.scope.items())
                or configuration.get("revision") != self.operation["input"]["configuration_revision"]
                or configuration.get("state") != "ready" or configuration.get("gaps") != []
                or configuration.get("handoff_state") != "linked" or settings.get("enabled") is not True
                or not isinstance(settings.get("capabilities"), list)
                or self.project["capability"] not in settings.get("capabilities", [])):
            raise ValueError("installed service configuration is stale, foreign or not ready")
        devops = settings.get("devops")
        provider = self.selected["provider"]
        native = binding.get("native_resource_ref")
        connection = binding.get("connection_ref")
        if (not isinstance(devops, dict) or not _reference(connection, self.scope, {"connection"})
                or devops.get("hosting_connection_ref") != connection
                or devops.get("region") != provider["region"]
                or connection["source_connection_id"] != provider["source_connection_id"]
                or binding.get("binding_id") != self.selected["binding_id"]
                or binding.get("revision") != self.selected["binding_revision"]
                or binding.get("scope") != configuration["scope"]
                or not configuration.get("configuration_id")
                or binding.get("configuration_id") != configuration["configuration_id"]
                or binding.get("state") != "ready" or binding.get("gaps") != []
                or binding.get("provider_identity") != {key: provider[key] for key in ("account_id", "role_arn", "region")}
                or not _reference(native, self.scope, {"agent_space"})
                or native["record_id"] != self.selected["agent_space_id"]
                or native["source_connection_id"] != provider["source_connection_id"]):
            raise ValueError("installed configuration binding or provider differs from its selection")
        integrations, accounts = devops.get("integrations"), devops.get("accounts")
        if (not isinstance(integrations, list) or any(not isinstance(item, dict) for item in integrations)
                or not isinstance(accounts, list) or any(not isinstance(item, dict) for item in accounts)):
            raise ValueError("installed configuration has no scoped integrations or environments")
        repositories, environments = [], []
        for integration in integrations:
            refs = integration.get("repository_refs", [])
            if not isinstance(refs, list):
                raise ValueError("installed configuration repositories unavailable")
            if integration.get("kind") == "github":
                repositories.extend(ref for ref in refs if isinstance(ref, dict)
                                    and ref.get("record_id") == self.project["repository_ref"])
        for account in accounts:
            selected = account.get("environments", [])
            if not isinstance(selected, list) or any(not isinstance(item, dict) for item in selected):
                raise ValueError("installed configuration environments unavailable")
            environments.extend((account, item) for item in selected
                                if isinstance(item.get("environment_ref"), dict)
                                and item["environment_ref"].get("record_id") == self.project["environment_ref"])
        policy = devops.get("access_policy_ref")
        if (len(repositories) != 1 or not _reference(repositories[0], self.scope, {"repository", "repository_commit"})
                or len(environments) != 1 or not _reference(environments[0][1]["environment_ref"], self.scope, {"environment"})
                or environments[0][0].get("connection_ref") != connection
                or environments[0][1].get("workload_kind") != self.project["workload_kind"]
                or not _reference(policy, self.scope, {"policy"}) or policy["record_id"] != self.project["policy_ref"]):
            raise ValueError("installed configuration source, policy or workload differs from the selected Product")
        repository, environment = repositories[0], environments[0][1]
        basic, advanced = profile["configuration"].get("basic"), profile["configuration"].get("advanced")
        if (not isinstance(basic, dict) or not isinstance(advanced, dict)
                or repository not in (basic.get("repositories") or [])
                or environment["environment_ref"] not in (advanced.get("environments") or [])
                or environment["environment_ref"] not in (settings.get("source_refs") or [])):
            raise ValueError("installed configuration sources are no longer selected by the Product")
        if (self._request("GET", self.service + "/configuration") != configuration or self._profile() != profile):
            raise ValueError("Product configuration authority changed during readback")
        return {"scope": self.scope, "repository_ref": repository["record_id"],
                "team_ref": profile["product"]["team_id"], "policy_ref": policy["record_id"],
                "environment_ref": environment["environment_ref"]["record_id"],
                "workload_kind": environment["workload_kind"],
                "account_ref": binding["provider_identity"]["account_id"],
                "role_ref": binding["provider_identity"]["role_arn"],
                "readback": {"profile": profile, "configuration": configuration, "binding": binding},
                "observed_at": datetime.now(UTC).isoformat()}

    def confirm(self, project):
        if project != self.project:
            raise ValueError("installed configuration cannot confirm a changed Product selection")
        self.target.cleanup_phase = False
        self._reserve(11)
        before = self.read()
        receipt = self.project["configuration_receipt"]
        self.target._journal("configure", "prepared", receipt, scope=self.scope)
        try:
            observed = self._request("POST", self.service + "/configuration/check")
            integrations = observed.get("integrations")
            if (observed.get("ready") is not True or observed.get("gaps") != []
                    or observed.get("provider_contacted") is not True or not isinstance(integrations, list)
                    or len(integrations) != len(before["readback"]["configuration"]["settings"]["devops"]["integrations"])
                    or any(not isinstance(item, dict) or item.get("status") != "ready"
                           or not isinstance(item.get("events", {}), dict)
                           or item.get("events", {}).get("requested_mode_unavailable") for item in integrations)):
                raise ValueError("installed configuration prerequisites are not ready")
            if self.read()["readback"] != before["readback"]:
                raise ValueError("Product configuration changed during its readiness check")
        except Exception:
            self.target._journal("configure", "unverified", receipt, scope=self.scope)
            raise
        self.target._journal("configure", "acknowledged", receipt, scope=self.scope)
