"""Current Product authority and native access for the two portability Products."""

import json
import os
import re
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote, urlsplit

from installed_journeys import release_readback_tokens
from qualification import timestamp


def validate_installed_access(manifest):
    selected = manifest["portability"].get("installed_access")
    projects = manifest.get("projects")
    if (not isinstance(selected, dict) or set(selected) != {"projects"}
            or not isinstance(selected["projects"], list) or len(selected["projects"]) != 2
            or not isinstance(projects, list) or len(projects) != 2):
        raise ValueError("installed portability access needs exactly two Product selections")
    address = urlsplit(manifest.get("journey_driver", {}).get("gateway_url", ""))
    if (address.scheme != "https" or not address.hostname or address.username or address.password
            or address.port not in (None, 443) or address.path not in ("", "/") or address.query or address.fragment
            or address.hostname != manifest.get("ingress_policy", {}).get("frontend_hostname")):
        raise ValueError("installed portability access requires the selected first-party Gateway")
    tokens = release_readback_tokens(manifest)
    for channels in (manifest["portability"].get("action_channels", {}),
                     manifest["portability"].get("readback_channels", {})):
        tokens.update(os.environ[channel["token_env"]] for channel in channels.values())
    driver = manifest.get("journey_driver", {})
    tokens.update(os.environ[driver[key]] for key in ("api_token_env", "mcp_token_env") if key in driver)
    scopes = []
    for entry in selected["projects"]:
        if not isinstance(entry, dict) or set(entry) != {
                "scope", "binding_id", "binding_revision", "agent_space_id", "product_token_env", "provider"}:
            raise ValueError("installed portability access requires a pinned binding and independent readers")
        scope = entry["scope"]
        if (not isinstance(scope, dict) or set(scope) != {"tenant_id", "product_id", "project_id"}
                or any(not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value)
                       for value in scope.values()) or scope in scopes):
            raise ValueError("installed portability access scope is invalid or duplicated")
        matches = [project for project in projects if project.get("scope") == scope]
        if len(matches) != 1:
            raise ValueError("installed portability access belongs to another Product")
        scopes.append(scope)
        provider = entry["provider"]
        if (not isinstance(provider, dict) or set(provider) != {
                "connection_label", "account_id", "role_arn", "region", "source_connection_id"}
                or not re.fullmatch(r"[0-9]{12}", provider.get("account_id", ""))
                or not re.fullmatch(rf"arn:aws:iam::{provider['account_id']}:role/[A-Za-z0-9+=,.@_/-]+",
                                    provider.get("role_arn", ""))
                or not re.fullmatch(r"[a-z]{2}-[a-z]+-[0-9]", provider.get("region", ""))
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", provider.get("connection_label", ""))
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", provider.get("source_connection_id", ""))
                or matches[0].get("account_ref") != provider["account_id"]
                or matches[0].get("role_ref") != provider["role_arn"]
                or any(not isinstance(entry[key], str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", entry[key])
                       for key in ("binding_id", "agent_space_id"))
                or type(entry["binding_revision"]) is not int or entry["binding_revision"] < 1):
            raise ValueError("installed portability access must pin the selected account, role and space")
        name = entry["product_token_env"]
        if (not isinstance(name, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]+", name)
                or not os.environ.get(name) or os.environ[name] in tokens):
            raise ValueError("installed portability access requires independent Product credentials")
        tokens.add(os.environ[name])
    return selected["projects"]


def read_provider_access(provider, space_id, *, deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ValueError("native access readback exceeded its deadline")
    command = ["adp-cred", "assume", "--service", "aws", "--label", provider["connection_label"],
               "--exec", sys.executable, str(Path(__file__).with_name("provider_access.py")),
               provider["account_id"], provider["role_arn"], provider["region"], space_id]
    try:
        result = subprocess.run(command, capture_output=True, timeout=min(remaining, 20), check=False)
        if result.returncode or len(result.stdout) > 65536:
            raise ValueError("selected native access readback unavailable")
        return json.loads(result.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        raise ValueError("selected native access readback unavailable") from None


def read_installed_access(target, scope):
    entries = validate_installed_access(target.manifest)
    selected = next((entry for entry in entries if entry["scope"] == scope), None)
    if selected is None:
        raise ValueError("installed access read requested foreign scope")
    expires = timestamp(target.manifest["authorization"]["expires_at"])
    deadline = min(target.deadline, time.monotonic() + (expires - datetime.now(UTC)).total_seconds())
    if target.calls + 4 > target.max_requests - target.cleanup_reserve or time.monotonic() >= deadline:
        raise ValueError("installed access read exceeded its authorized request or time bound")
    provider = selected["provider"]
    path = (f"/api/v1/products/{quote(scope['product_id'], safe='')}/projects/"
            f"{quote(scope['project_id'], safe='')}/services/devops/bindings/{quote(selected['binding_id'], safe='')}")
    url = target.manifest["journey_driver"]["gateway_url"].rstrip("/") + path

    def binding_read():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ValueError("installed access read exceeded its authorized deadline")
        target.calls += 1
        with target.client.stream("GET", url, timeout=min(remaining, 15), follow_redirects=False,
                                  headers={"Authorization": "Bearer " + os.environ[selected["product_token_env"]]}) as response:
            if response.status_code != 200:
                raise ValueError("current Product binding authority unavailable")
            content = bytearray()
            for chunk in response.iter_bytes():
                content.extend(chunk)
                if len(content) > 65536:
                    raise ValueError("installed binding access response is oversized")
        return json.loads(content)

    binding = binding_read()
    if not isinstance(binding, dict):
        raise ValueError("installed access binding unavailable")
    native = binding.get("native_resource_ref") or {}
    identity = {key: provider[key] for key in ("account_id", "role_arn", "region")}
    if (binding.get("binding_id") != selected["binding_id"] or binding.get("state") != "ready"
            or binding.get("revision") != selected["binding_revision"] or binding.get("gaps") != []
            or not isinstance(binding.get("scope"), dict)
            or any(binding["scope"].get(key) != value for key, value in scope.items())
            or binding.get("provider_identity") != identity or not isinstance(native, dict)
            or native.get("record_type") != "agent_space" or native.get("record_id") != selected["agent_space_id"]
            or native.get("tenant_id") != scope["tenant_id"] or native.get("project_id") != scope["project_id"]
            or native.get("source_connection_id") != provider["source_connection_id"]):
        raise ValueError("installed access binding is foreign, stale or not ready")
    target.calls += 2
    observed = read_provider_access(provider, selected["agent_space_id"], deadline=deadline)
    expected_role = rf"arn:aws:sts::{provider['account_id']}:assumed-role/{re.escape(provider['role_arn'].split('/')[-1])}/[^/]+"
    if (not isinstance(observed, dict) or any(observed.get(key) != value for key, value in identity.items())
            or observed.get("agent_space_id") != selected["agent_space_id"]
            or not re.fullmatch(expected_role, observed.get("assumed_role_arn", ""))
            or observed.get("requests") != 2):
        raise ValueError("native access readback differs from the selected Product binding")
    if binding_read() != binding or time.monotonic() >= deadline:
        raise ValueError("Product binding authority changed or expired during native access readback")
    return {"scope": scope, "account_ref": observed["account_id"], "role_ref": observed["role_arn"],
            "authorized": True, "readback": {"binding": binding, "native": observed},
            "observed_at": datetime.now(UTC).isoformat()}
