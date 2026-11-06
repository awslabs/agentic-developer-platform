"""Run ordinary Product binding setup only after consumers have been enabled."""

import re
import time
from copy import deepcopy
from urllib.parse import quote

from journey_driver import FirstPartyDriver


def validate_setup(manifest):
    setup = manifest.get("installed_setup")
    if setup is None:
        return None
    if (not isinstance(setup, dict)
            or set(setup) - {"tenant_id", "request", "receipt", "if_match", "max_requests", "deadline_seconds"}
            or not isinstance(setup.get("tenant_id"), str) or not setup["tenant_id"]
            or not isinstance(setup.get("receipt"), str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", setup["receipt"])
            or type(setup.get("max_requests")) is not int or not 8 <= setup["max_requests"] <= 100
            or type(setup.get("deadline_seconds")) is not int or not 30 <= setup["deadline_seconds"] <= 900):
        raise ValueError("installed setup needs bounded Product authority and a stable receipt")
    request = setup.get("request")
    if (not isinstance(request, dict) or request.get("action") not in {"create", "link", "check"}
            or type(request.get("configuration_revision")) is not int or request["configuration_revision"] < 1):
        raise ValueError("installed setup must select an ordinary create, link or check operation")
    action = request["action"]
    fields = {"action", "configuration_revision"} | ({"native_resource_ref"} if action == "link" else
                                                    {"binding_id"} if action == "check" else set())
    scope = manifest["journey_driver"]["scope"]
    if (set(request) != fields
            or any(body["configuration_revision"] != request["configuration_revision"]
                   for body in manifest["journey_driver"]["inputs"].values())
            or (action == "check" and (request["binding_id"] != scope["binding_id"]
                or not re.fullmatch(r'"[1-9][0-9]*"', setup.get("if_match", ""))))
            or (action != "check" and (scope["binding_id"] != "pending-setup" or setup.get("if_match") is not None))
            or ("recovery" in manifest and manifest["recovery"]["binding_id"] != scope["binding_id"])):
        raise ValueError("installed setup must preserve the selected configuration and binding")
    if action == "link":
        reference = request["native_resource_ref"]
        if (not isinstance(reference, dict) or reference.get("tenant_id") != setup["tenant_id"]
                or reference.get("project_id") != scope["project_id"] or reference.get("record_type") != "agent_space"
                or reference.get("source_connection_id") != manifest["journey_driver"]["provider_readback"]["source_connection_id"]):
            raise ValueError("installed adoption must name the selected native space")
    return setup


def prepare_binding(manifest, directory, *, driver_factory=FirstPartyDriver, sleep=time.sleep):
    setup = validate_setup(manifest)
    if setup is None:
        return manifest
    driver = driver_factory(manifest, directory, deadline=time.monotonic() + setup["deadline_seconds"],
                            max_requests=setup["max_requests"])

    def complete(request, receipt, if_match=None):
        driver.journal("setup", "prepared", receipt)
        operation = driver.request("POST", driver.path + "/binding-operations", token_env="api_token_env",
                                   body=request, receipt=receipt, if_match=if_match)
        operation_id = operation.get("operation_id")
        if not isinstance(operation_id, str) or not re.fullmatch(r"[\w-]{1,128}", operation_id):
            raise ValueError("Product setup returned no durable operation identity")
        driver.journal("setup", "acknowledged", receipt, operation_id=operation_id)
        while True:
            operation = driver.request("GET", driver.path + "/binding-operations/" + quote(operation_id, safe=""),
                                       token_env="api_token_env")
            if operation.get("operation_id") != operation_id or operation.get("action") != request["action"]:
                raise ValueError("Product setup returned another operation")
            if operation.get("state") == "succeeded":
                return operation
            if operation.get("state") not in {"accepted", "running", "waiting", "outcome_unknown"}:
                raise ValueError("Product setup did not complete; retain its original receipt for recovery")
            sleep(min(2, driver.remaining()))

    try:
        scope = manifest["journey_driver"]["scope"]
        selected_scope = {"tenant_id": setup["tenant_id"], "product_id": scope["product_id"], "project_id": scope["project_id"]}
        configuration = driver.request("GET", driver.path + "/configuration", token_env="api_token_env")
        if (configuration.get("revision") != setup["request"]["configuration_revision"]
                or any(configuration.get("scope", {}).get(field) != value for field, value in selected_scope.items())
                or not configuration.get("configuration_id") or not configuration.get("hash")
                or configuration.get("handoff_state") != "linked"):
            raise ValueError("installed setup requires the current linked Product configuration")
        operation = complete(setup["request"], setup["receipt"], setup.get("if_match"))
        binding_id = operation.get("binding_id")
        if (not isinstance(binding_id, str) or not re.fullmatch(r"[\w-]{1,128}", binding_id)
                or (setup["request"]["action"] == "check" and binding_id != setup["request"]["binding_id"])):
            raise ValueError("Product setup returned another binding")
        binding = driver.request("GET", driver.path + "/bindings/" + quote(binding_id, safe=""), token_env="api_token_env")
        provider = manifest["journey_driver"]["provider_readback"]
        reference, identity = binding.get("native_resource_ref") or {}, binding.get("provider_identity") or {}
        if (binding.get("binding_id") != binding_id or binding.get("state") != "ready" or binding.get("gaps") != []
                or binding.get("configuration_id") != configuration["configuration_id"]
                or any(binding.get("scope", {}).get(field) != value for field, value in selected_scope.items())
                or reference.get("tenant_id") != setup["tenant_id"] or reference.get("project_id") != scope["project_id"]
                or reference.get("record_type") != "agent_space" or not reference.get("record_id")
                or reference.get("source_connection_id") != provider["source_connection_id"]
                or any(identity.get(field) != provider[field] for field in ("account_id", "role_arn", "region"))
                or (setup["request"]["action"] == "link" and reference != setup["request"]["native_resource_ref"])):
            raise ValueError("Product setup readback differs from the selected ready native binding")
        revision = configuration["revision"]
        if setup["request"]["action"] != "check":
            from hashlib import sha256

            settings = deepcopy(configuration["settings"])
            policies = settings.get("devops", {}).get("trigger_policies", [])
            if not any(policy.get("binding_id") == "pending-setup" for policy in policies):
                raise ValueError("Product configuration has no declared pending setup target")
            for policy in policies + settings.get("triggers", []):
                if policy.get("binding_id") == "pending-setup":
                    policy["binding_id"] = binding_id
            receipt = sha256((setup["receipt"] + ":link-configuration").encode()).hexdigest()
            driver.journal("setup_configuration", "prepared", receipt)
            updated = driver.request("PUT", driver.path + "/configuration", token_env="api_token_env", body=settings,
                                     receipt=receipt, if_match='"' + configuration["hash"] + '"')
            if (updated.get("configuration_id") != configuration["configuration_id"]
                    or updated.get("revision") != revision + 1 or updated.get("handoff_state") != "linked"
                    or updated.get("settings") != settings or updated.get("scope") != configuration["scope"]):
                raise ValueError("Product configuration handoff did not retain the selected setup settings")
            driver.journal("setup_configuration", "acknowledged", receipt)
            revision = updated["revision"]
            receipt = sha256((setup["receipt"] + ":check-binding").encode()).hexdigest()
            checked = complete({"action": "check", "configuration_revision": revision, "binding_id": binding_id},
                               receipt, '"' + str(binding["revision"]) + '"')
            if checked.get("binding_id") != binding_id:
                raise ValueError("Product setup check returned another binding")
            current = driver.request("GET", driver.path + "/bindings/" + quote(binding_id, safe=""), token_env="api_token_env")
            if (any(current.get(field) != binding.get(field) for field in (
                    "binding_id", "scope", "configuration_id", "native_resource_ref", "provider_identity", "connection_ref"))
                    or current.get("state") != "ready" or current.get("gaps") != []):
                raise ValueError("Product setup check changed the scoped native binding")
            binding = current
        selected = deepcopy(manifest)
        selected.pop("installed_setup")
        selected["journey_driver"]["scope"]["binding_id"] = binding_id
        for body in selected["journey_driver"]["inputs"].values():
            body["binding_id"] = binding_id
            body["configuration_revision"] = revision
        if "recovery" in selected:
            recovery = selected["recovery"]
            recovery["binding_id"] = binding_id
            recovery["browser"]["configuration_revision"] = revision
            if "installed_work" in recovery:
                recovery["installed_work"].update(agent_space_id=reference["record_id"], binding_revision=binding["revision"])
                for body in recovery["installed_work"]["inputs"].values():
                    body.update(binding_id=binding_id, configuration_revision=revision)
        selected["ingress_policy"]["launch_path"] = driver.path + "/bindings/" + binding_id + "/browser-launch"
        return selected
    finally:
        driver.close()
