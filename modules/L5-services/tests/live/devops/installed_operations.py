"""Admit portability work through maintained, scoped Product run endpoints."""

import json
import os
import re
import time
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from urllib.parse import quote

from installed_portability import validate_installed_access
from installed_journeys import release_readback_tokens
from qualification import timestamp


def validate_installed_operations(manifest):
    from fastapi import HTTPException
    from l5_services.run_input import validate_run_input

    access = validate_installed_access(manifest)
    selected = manifest["portability"].get("installed_operations")
    if (not isinstance(selected, dict) or set(selected) != {"projects"}
            or not isinstance(selected["projects"], list) or len(selected["projects"]) != 2):
        raise ValueError("installed operations require two scoped Product selections")
    tokens = release_readback_tokens(manifest)
    driver = manifest.get("journey_driver", {})
    tokens.update(os.environ[driver[key]] for key in ("api_token_env", "mcp_token_env") if key in driver)
    tokens.update(os.environ[entry["product_token_env"]] for entry in access)
    for group in ("action_channels", "readback_channels"):
        tokens.update(os.environ[channel["token_env"]]
                      for channel in manifest["portability"][group].values())
    scopes, receipts = [], set()
    for entry in selected["projects"]:
        if not isinstance(entry, dict) or set(entry) != {"scope", "submit_token_env", "input"}:
            raise ValueError("installed operations need a selected input and private submit credential")
        scope = entry["scope"]
        binding = next((item for item in access if item["scope"] == scope), None)
        if binding is None or scope in scopes:
            raise ValueError("installed operations requested foreign or duplicate Product scope")
        scopes.append(scope)
        project = next(item for item in manifest["projects"] if item["scope"] == scope)
        body = entry["input"]
        try:
            validate_run_input(body)
        except (HTTPException, TypeError, AttributeError):
            raise ValueError("installed operation input does not match the Product contract") from None
        if (body.get("service_id") != "devops" or body.get("binding_id") != binding["binding_id"]
                or body.get("capability") != project["capability"]):
            raise ValueError("installed operation input differs from the selected binding or capability")
        options = body["devops_input"]
        references = options.get("resource_refs", []) + options.get("telemetry_refs", [])
        references += [options[key] for key in ("repository_ref", "eligibility_ref", "source_ref",
                                               "deployment_ref", "target_ref", "credential_ref") if options.get(key) is not None]
        if any(any(reference[key] != scope[key] for key in ("tenant_id", "project_id"))
               for reference in references):
            raise ValueError("installed operation input contains foreign Product references")
        name = entry["submit_token_env"]
        receipt = project.get("operation_receipt")
        if (not isinstance(name, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]+", name)
                or not os.environ.get(name) or os.environ[name] in tokens):
            raise ValueError("installed operations require independent submit credentials")
        if (not isinstance(receipt, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", receipt)
                or receipt in receipts):
            raise ValueError("installed operations require distinct stable receipts")
        receipts.add(receipt)
        tokens.add(os.environ[name])
    return selected["projects"], access


def submit_installed_operation(target, scope, capability, limits):
    entries, access = validate_installed_operations(target.manifest)
    entry = next((item for item in entries if item["scope"] == scope), None)
    if entry is None or target.cleanup_phase:
        raise ValueError("installed operation cannot submit foreign scope or during cleanup")
    project = next(item for item in target.manifest["projects"] if item["scope"] == scope)
    if capability != project["capability"] or limits != project["limits"]["operation"]:
        raise ValueError("installed operation differs from its authorized capability or bounds")
    selected = next(item for item in access if item["scope"] == scope)
    body = entry["input"]
    receipt = project["operation_receipt"]
    provider = selected["provider"]
    expires = timestamp(target.manifest["authorization"]["expires_at"])
    deadline = min(target.deadline, time.monotonic() + (expires - datetime.now(UTC)).total_seconds())
    if target.calls + 4 > target.max_requests - target.cleanup_reserve or time.monotonic() >= deadline:
        raise ValueError("installed operation exceeds its request or time bound")
    path = (f"/api/v1/products/{quote(scope['product_id'], safe='')}/projects/"
            f"{quote(scope['project_id'], safe='')}/services/devops")

    def request(method, suffix, *, payload=None):
        remaining = deadline - time.monotonic()
        if remaining <= 0 or target.calls >= target.max_requests - target.cleanup_reserve:
            raise ValueError("installed operation exceeds its request or time bound")
        target.calls += 1
        headers = {"Authorization": "Bearer " + os.environ[
            entry["submit_token_env"] if method == "POST" else selected["product_token_env"]]}
        if method == "POST":
            headers["Idempotency-Key"] = receipt
        url = target.manifest["journey_driver"]["gateway_url"].rstrip("/") + path + suffix
        with target.client.stream(method, url, headers=headers, json=payload,
                                  follow_redirects=False, timeout=min(remaining, 15)) as response:
            if response.status_code != (202 if method == "POST" else 200):
                raise ValueError("installed Product operation unavailable or redirected")
            content = bytearray()
            for chunk in response.iter_bytes():
                content.extend(chunk)
                if len(content) > 1024 * 1024:
                    raise ValueError("installed Product operation response is oversized")
        observed = json.loads(content)
        if not isinstance(observed, dict) or time.monotonic() >= deadline:
            raise ValueError("installed Product operation response is invalid or expired")
        return observed

    configuration = request("GET", "/configuration")
    settings = configuration.get("settings") or {}
    configured_limits = settings.get("limits") or {}
    try:
        allocation = Decimal(str(configured_limits["max_cost"]))
        maximum = Decimal(str(limits["max_spend_usd"]))
    except (KeyError, InvalidOperation):
        raise ValueError("installed operation lacks a positive admission allocation") from None
    for key in ("max_provider_attempts", "max_polls"):
        if type(configured_limits.get(key)) is not int or not 1 <= configured_limits[key] <= limits["max_requests"]:
            raise ValueError("installed operation attempts or polls exceed the selected bound")
    if (configuration.get("revision") != body["configuration_revision"]
            or not isinstance(configuration.get("scope"), dict)
            or any(configuration["scope"].get(key) != value for key, value in scope.items())
            or configuration.get("state") != "ready" or configuration.get("gaps") != []
            or configuration.get("handoff_state") != "linked" or settings.get("enabled") is not True
            or capability not in settings.get("capabilities", [])
            or configured_limits.get("currency") != "USD"
            or not allocation.is_finite() or not maximum.is_finite() or not 0 < allocation <= maximum
            or type(configured_limits.get("deadline_seconds")) is not int
            or not 0 < configured_limits["deadline_seconds"] <= limits["deadline_seconds"]
            or type(configured_limits.get("concurrency")) is not int
            or not 1 <= configured_limits["concurrency"] <= 5):
        raise ValueError("installed operation configuration is stale, foreign or outside allocation")
    binding = request("GET", "/bindings/" + quote(selected["binding_id"], safe=""))
    native = binding.get("native_resource_ref") or {}
    if (binding.get("binding_id") != body["binding_id"] or binding.get("state") != "ready"
            or binding.get("revision") != selected["binding_revision"] or binding.get("gaps") != []
            or binding.get("scope") != configuration["scope"]
            or binding.get("configuration_id") != configuration.get("configuration_id")
            or binding.get("provider_identity") != {key: provider[key] for key in ("account_id", "role_arn", "region")}
            or native.get("record_type") != "agent_space" or native.get("record_id") != selected["agent_space_id"]
            or any(native.get(key) != scope[key] for key in ("tenant_id", "project_id"))
            or native.get("source_connection_id") != provider["source_connection_id"]):
        raise ValueError("installed operation binding is foreign, stale or not ready")
    target._journal("run_operation", "prepared", receipt, scope=scope)
    try:
        accepted = request("POST", "/runs", payload=body)
        run_id = accepted.get("run_id")
        if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", run_id):
            raise ValueError("installed operation returned no durable run identity")
        target._journal("run_operation", "acknowledged", receipt, scope=scope, operation_id=run_id)
        observed = request("GET", "/runs/" + quote(run_id, safe=""))
        admission = observed.get("admission") or {}
        if (observed.get("run_id") != run_id or observed.get("input") != body
                or observed.get("scope") != configuration["scope"] or admission.get("scope") != observed["scope"]
                or observed.get("origin") != "adp_submitted" or admission.get("service_id") != "devops"
                or admission.get("native_space_ref") != native
                or admission.get("provider_identity") != binding["provider_identity"]
                or admission.get("limits") != configured_limits):
            raise ValueError("installed operation readback differs from the admitted scope or input")
        return {"run_id": run_id}
    except Exception as error:
        target._journal("run_operation", "uncertain", receipt, scope=scope)
        if isinstance(error, target.httpx.TimeoutException):
            raise TimeoutError("installed operation uncertain; reconcile the original receipt") from None
        raise
