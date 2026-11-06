"""First-install Product setup, separate from native inquiry and final evaluation."""

import hashlib
import json
import os
import re
import time
from copy import deepcopy
from urllib.parse import quote

from fixtures import write_private


OPERATIONS = ("configure", "link", "trigger", "readback")


def validate_bootstrap(manifest):
    selected = manifest["bootstrap"]
    if (not isinstance(selected, dict) or set(selected) != {"configuration", "trigger_id", "receipts"}
            or "binding_id" in manifest or "configuration_revision" in manifest
            or "binding_id" in manifest["input"] or "configuration_revision" in manifest["input"]
            or not isinstance(selected["configuration"], dict)
            or not isinstance(selected["trigger_id"], str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", selected["trigger_id"])):
        raise ValueError("first installation requires an explicit Product setup selection")
    config = selected["configuration"]
    native = config.get("finops") or {}
    if (config.get("service_id") != "finops" or config.get("enabled") is not True
            or config.get("capabilities") != ["cost_inquiry"] or config.get("triggers")
            or not isinstance(native, dict) or native.get("agent_mode") != "adopt"
            or native.get("native_agent_ref") != (manifest.get("native_agent_ref") or {}).get("record_id")
            or native.get("native_agent_display_name") != manifest.get("native_agent_display_name")
            or native.get("billing_scope") != manifest.get("billing_scope")):
        raise ValueError("setup must adopt the authorized native agent and billing scope")
    receipts = selected["receipts"]
    if (not isinstance(receipts, dict) or set(receipts) != set(OPERATIONS)
            or any(not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9_-]{16,128}", value) is None
                   for value in receipts.values())
            or len(set(receipts.values()) | {manifest.get("receipt"), manifest.get("cleanup_receipt")}) != 6):
        raise ValueError("setup requires distinct durable receipts")


def digest(manifest):
    return hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()


def selection_path(manifest, directory):
    return directory / f"finops-{manifest['receipt']}-selection.json"


def read_selection(manifest, directory):
    from driver import private_path, validate

    selected = json.loads(private_path(selection_path(manifest, directory)).read_text())
    if (not isinstance(selected, dict) or selected.get("setup_authorization_digest") != digest(manifest)
            or any(selected.get(key) != value for key, value in manifest.items() if key not in {"bootstrap", "input"})
            or {key: value for key, value in selected.get("input", {}).items()
                if key not in {"binding_id", "configuration_revision"}} != manifest["input"]):
        raise ValueError("setup readback differs from authorized inquiry; retain original operations")
    return validate(selected)


def prepare(manifest, directory):
    from driver import FinOpsDriver

    driver = FinOpsDriver(manifest, directory, mode="setup")
    journal = driver.directory / f"finops-{manifest['receipt']}-setup.jsonl"
    try:
        driver.installation_preflight()
        if selection_path(manifest, driver.directory).exists():
            return read_selection(manifest, driver.directory)
        descriptor = os.open(journal, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        os.close(descriptor)
        setup = manifest["bootstrap"]

        def record(operation, state, observation=None):
            descriptor = os.open(journal, os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW)
            with os.fdopen(descriptor, "a") as output:
                json.dump({"operation": operation, "receipt": setup["receipts"][operation], "state": state,
                           "operation_id": (observation or {}).get("operation_id"),
                           "binding_id": (observation or {}).get("binding_id")}, output)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())

        def mutate(operation, method, suffix, body, **kwargs):
            record(operation, "prepared")
            result = driver.request(method, driver.path + suffix, body=body, token="api_token_env",
                                    receipt=setup["receipts"][operation], **kwargs)
            record(operation, "acknowledged", result)
            return result

        def link(operation, revision):
            observed = mutate(operation, "POST", "/binding-operations", {
                "action": "link", "configuration_revision": revision,
                "native_resource_ref": manifest["native_agent_ref"]})
            operation_id = observed.get("operation_id")
            if not isinstance(operation_id, str) or not operation_id:
                raise ValueError("Product did not acknowledge the setup operation; reconcile its receipt")
            while observed.get("state") in {"accepted", "running", "waiting"}:
                observed = driver.request("GET", driver.path + "/binding-operations/" + quote(operation_id, safe=""))
                if observed.get("operation_id") != operation_id:
                    raise ValueError("setup observation differs from original operation")
                if observed.get("state") != "succeeded":
                    time.sleep(min(driver.remaining(), 2))
            if observed.get("state") != "succeeded" or not observed.get("binding_id"):
                raise ValueError("native adoption is not ready; retain and observe the original setup operation")
            record(operation, "observed", observed)
            return observed["binding_id"]

        config = mutate("configure", "PUT", "/configuration", setup["configuration"], status=201,
                        preconditions={"If-None-Match": "*"})
        binding_id = link("link", config["revision"])
        current = driver.request("GET", driver.path + "/configuration")
        if (current.get("configuration_id") != config.get("configuration_id")
                or current.get("revision") != config["revision"]
                or not re.fullmatch(r"[0-9a-f]{64}", current.get("hash", ""))):
            raise ValueError("Product configuration changed during setup")
        settings = deepcopy(setup["configuration"])
        settings["triggers"] = [{"trigger_id": setup["trigger_id"], "kind": "manual", "enabled": True,
                                 "capability": "cost_inquiry", "binding_id": binding_id,
                                 "source_refs": manifest["input"].get("sources", [])}]
        config = mutate("trigger", "PUT", "/configuration", settings, status=200,
                        preconditions={"If-Match": '"' + current["hash"] + '"'})
        if link("readback", config["revision"]) != binding_id:
            raise ValueError("setup changed the selected native binding")
        selected = {key: value for key, value in manifest.items() if key != "bootstrap"} | {
            "configuration_revision": config["revision"], "binding_id": binding_id,
            "input": {**manifest["input"], "configuration_revision": config["revision"], "binding_id": binding_id},
            "setup_authorization_digest": digest(manifest),
        }
        driver.manifest = selected
        driver.preflight()
        write_private(selection_path(manifest, driver.directory), selected)
        return selected
    finally:
        driver.close()
