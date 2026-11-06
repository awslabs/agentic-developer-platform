"""Probe foreign Product binding access without a custom qualification server."""

import json
import os
import time
from datetime import UTC, datetime
from urllib.parse import quote

from installed_operations import validate_installed_operations
from qualification import timestamp


def validate_installed_foreign_probe(manifest):
    if manifest["portability"].get("installed_foreign_probe") is not True:
        raise ValueError("installed foreign probe must be explicitly selected")
    return validate_installed_operations(manifest)


def probe_installed_foreign(target, scope, foreign):
    operations, access = validate_installed_foreign_probe(target.manifest)
    own = next((entry for entry in access if entry["scope"] == scope), None)
    other = next((entry for entry in access if entry["scope"] == foreign), None)
    if (own is None or other is None or scope["product_id"] == foreign["product_id"]
            or target.cleanup_phase):
        raise ValueError("installed foreign probe requires two selected Products before cleanup")
    actor = next(entry for entry in operations if entry["scope"] == scope)
    expires = timestamp(target.manifest["authorization"]["expires_at"])
    deadline = min(target.deadline, time.monotonic() + (expires - datetime.now(UTC)).total_seconds())
    if target.calls + 5 > target.max_requests - target.cleanup_reserve or time.monotonic() >= deadline:
        raise ValueError("installed foreign probe exceeds its request or time bound")

    def read(entry, token_env, *, denied=False):
        remaining = deadline - time.monotonic()
        if remaining <= 0 or target.calls >= target.max_requests - target.cleanup_reserve:
            raise ValueError("installed foreign probe exceeds its request or time bound")
        selected = entry["scope"]
        path = (f"/api/v1/products/{quote(selected['product_id'], safe='')}/projects/"
                f"{quote(selected['project_id'], safe='')}/services/devops/bindings/"
                f"{quote(entry['binding_id'], safe='')}")
        url = target.manifest["journey_driver"]["gateway_url"].rstrip("/") + path
        target.calls += 1
        with target.client.stream("GET", url, timeout=min(remaining, 15), follow_redirects=False,
                                  headers={"Authorization": "Bearer " + os.environ[token_env]}) as response:
            if response.status_code != (404 if denied else 200):
                raise ValueError("installed foreign probe did not observe the expected Product access")
            content = bytearray()
            for chunk in response.iter_bytes():
                content.extend(chunk)
                if len(content) > 65536:
                    raise ValueError("installed foreign probe response is oversized")
        observed = json.loads(content)
        if time.monotonic() >= deadline:
            raise ValueError("installed foreign probe authority expired during readback")
        if denied:
            if observed != {"detail": {"code": "not_found"}}:
                raise ValueError("installed foreign probe disclosed data or returned another error")
            return observed
        if not isinstance(observed, dict):
            raise ValueError("installed foreign probe binding unavailable")
        native = observed.get("native_resource_ref")
        provider = entry["provider"]
        if (observed.get("binding_id") != entry["binding_id"] or observed.get("state") != "ready"
                or observed.get("revision") != entry["binding_revision"] or observed.get("gaps") != []
                or not isinstance(observed.get("scope"), dict)
                or any(observed["scope"].get(key) != value for key, value in selected.items())
                or observed.get("provider_identity") != {
                    key: provider[key] for key in ("account_id", "role_arn", "region")}
                or not isinstance(native, dict) or native.get("record_type") != "agent_space"
                or native.get("record_id") != entry["agent_space_id"]
                or native.get("tenant_id") != selected["tenant_id"]
                or native.get("project_id") != selected["project_id"]
                or native.get("source_connection_id") != provider["source_connection_id"]):
            raise ValueError("installed foreign probe binding is foreign, stale or not ready")
        return observed

    own_before = read(own, actor["submit_token_env"])
    other_before = read(other, other["product_token_env"])
    denial = read(other, actor["submit_token_env"], denied=True)
    if (read(own, actor["submit_token_env"]) != own_before
            or read(other, other["product_token_env"]) != other_before):
        raise ValueError("Product binding authority changed during foreign probe")
    return {"status": 404, "disclosed_resources": [],
            "readback": {"scope": scope, "foreign_scope": foreign, "denial": denial,
                         "own_binding": own_before, "foreign_binding": other_before},
            "observed_at": datetime.now(UTC).isoformat()}
