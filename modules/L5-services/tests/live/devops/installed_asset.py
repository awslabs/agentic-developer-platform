"""Join current Product binding authority with independent native asset content."""

import json
import os
import re
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

from journey_driver import validate_driver
from l5_services.devops.knowledge_contract import MAPPING_VERSION, native_contract
from qualification import digest, timestamp


def validate_installed_asset(manifest):
    context = manifest["context"]
    selected = context.get("installed_asset")
    if (not isinstance(selected, dict) or set(selected) != {
            "token_env", "agent_space_id", "binding_revision", "kind", "publication_scope", "visibility", "native_metadata"}
            or not re.fullmatch(r"[A-Z][A-Z0-9_]+", selected.get("token_env", ""))
            or not os.environ.get(selected["token_env"])
            or not re.fullmatch(r"[\w-]{1,128}", selected.get("agent_space_id", ""))
            or type(selected.get("binding_revision")) is not int or selected["binding_revision"] < 1
            or selected.get("kind") not in {"instruction", "skill"}
            or selected.get("publication_scope") not in {"global", "agent"}
            or selected.get("visibility") != "product_team"):
        raise ValueError("installed asset needs exact native selection and a Product read credential")
    native_contract(selected["kind"], selected["publication_scope"], True,
                    selected["visibility"], selected["native_metadata"])
    driver = validate_driver(manifest)
    scope = context.get("scope")
    sources = context.get("sources")
    if (not isinstance(scope, dict) or set(scope) != {"tenant_id", "product_id", "project_id"}
            or any(not isinstance(value, str) or not re.fullmatch(r"[\w-]{1,128}", value)
                   for value in scope.values())
            or any(scope[key] != driver["scope"][key] for key in ("product_id", "project_id"))
            or not re.fullmatch(r"[\w-]{1,128}", context.get("asset_ref", ""))
            or not isinstance(sources, list) or len(sources) != 1 or not isinstance(sources[0], dict)):
        raise ValueError("one native asset must belong to the selected Product and source")
    source = sources[0]
    if (set(source) != {"tenant_id", "project_id", "source_connection_id", "record_type", "record_id",
                       "record_revision", "sha256"}
            or any(not isinstance(value, str) or not value for value in source.values())
            or any(source[key] != scope[key] for key in ("tenant_id", "project_id"))
            or source["record_type"] not in {"document", "runbook", "skill"}
            or source["record_revision"].lower() in {"head", "latest", "null"}
            or not digest(source["sha256"], 64)):
        raise ValueError("installed asset requires one immutable scoped source and digest")
    tokens = {os.environ[driver[key]] for key in ("api_token_env", "mcp_token_env")}
    for channels in (manifest["readback_channels"], context["action_channels"], context["readback_channels"]):
        tokens.update(os.environ[channel["token_env"]] for channel in channels.values())
    if "installed_journeys" in manifest:
        tokens.add(os.environ[manifest["installed_journeys"]["token_env"]])
    if os.environ[selected["token_env"]] in tokens:
        raise ValueError("installed asset needs an independent Product read credential")
    return selected


def read_provider_asset(selected, space_id, asset_id, *, deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ValueError("native asset readback exceeded authorized deadline")
    command = ["adp-cred", "assume", "--service", "aws", "--label", selected["connection_label"],
               "--exec", sys.executable, str(Path(__file__).with_name("provider_asset.py")),
               selected["account_id"], selected["role_arn"], selected["region"], space_id, asset_id]
    try:
        result = subprocess.run(command, capture_output=True, timeout=min(remaining, 30), check=False)
        if result.returncode or len(result.stdout) > 65536:
            raise ValueError("selected native asset readback unavailable")
        return json.loads(result.stdout)
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        raise ValueError("selected native asset readback unavailable") from None


def read_installed_asset(target, scope, asset_ref):
    manifest = target.manifest
    context = manifest["context"]
    selected = validate_installed_asset(manifest)
    if scope != context["scope"] or asset_ref != context["asset_ref"]:
        raise ValueError("installed asset read requested foreign scope")
    driver = manifest["journey_driver"]
    provider = driver["provider_readback"]
    expires = timestamp(manifest["authorization"]["expires_at"])
    deadline = min(target.deadline, time.monotonic() + (expires - datetime.now(UTC)).total_seconds())
    target.calls += 4
    remaining = deadline - time.monotonic()
    if remaining <= 0 or target.calls > target.max_requests:
        raise ValueError("installed asset read exceeded authorized request or time bound")
    path = (f"/api/v1/products/{quote(scope['product_id'], safe='')}/projects/"
            f"{quote(scope['project_id'], safe='')}/services/devops/bindings/"
            f"{quote(driver['scope']['binding_id'], safe='')}")
    with target.client.stream("GET", driver["gateway_url"].rstrip("/") + path,
                              headers={"Authorization": "Bearer " + os.environ[selected["token_env"]]},
                              timeout=min(remaining, 15), follow_redirects=False) as response:
        if response.status_code != 200:
            raise ValueError("current Product binding authority unavailable")
        content = bytearray()
        for chunk in response.iter_bytes():
            content.extend(chunk)
            if len(content) > 65536:
                raise ValueError("installed binding readback exceeded its size bound")
        binding = json.loads(content)
    if not isinstance(binding, dict):
        raise ValueError("installed binding is unavailable")
    reference = binding.get("native_resource_ref") or {}
    binding_scope = binding.get("scope")
    if (binding.get("binding_id") != driver["scope"]["binding_id"]
            or not isinstance(binding_scope, dict) or any(binding_scope.get(key) != value for key, value in scope.items())
            or binding_scope.get("visibility") != selected["visibility"]
            or binding.get("revision") != selected["binding_revision"] or binding.get("state") != "ready"
            or binding.get("provider_identity") != {key: provider[key] for key in ("account_id", "role_arn", "region")}
            or not isinstance(reference, dict) or reference.get("record_type") != "agent_space"
            or reference.get("record_id") != selected["agent_space_id"]
            or reference.get("tenant_id") != scope["tenant_id"]
            or reference.get("project_id") != scope["project_id"]
            or reference.get("source_connection_id") != provider["source_connection_id"]):
        raise ValueError("installed native space binding is foreign, stale or not ready")
    readback = read_provider_asset(provider, selected["agent_space_id"], asset_ref, deadline=deadline)
    if not isinstance(readback, dict):
        raise ValueError("native asset readback unavailable")
    asset = readback.get("asset") or {}
    metadata = asset.get("metadata") if isinstance(asset, dict) else None
    source = context["sources"][0]
    source_ref = {key: value for key, value in source.items() if key != "sha256"}
    native_type, _, native_metadata, content_limit = native_contract(
        selected["kind"], selected["publication_scope"], True, selected["visibility"], selected["native_metadata"])
    if (not isinstance(asset, dict) or asset.get("assetId") != asset_ref
            or asset.get("assetType") != native_type
            or type(asset.get("version")) is not int or asset["version"] < 1
            or readback.get("content_version") != asset["version"]
            or readback.get("content_sha256") != source["sha256"]
            or type(readback.get("content_bytes")) is not int or not 0 <= readback["content_bytes"] <= content_limit
            or not isinstance(metadata, dict) or metadata.get("adp_source_ref") != source_ref
            or metadata.get("adp_source_hash") != source["sha256"]
            or type(metadata.get("adp_mapping_version")) is not int or metadata["adp_mapping_version"] != MAPPING_VERSION
            or any(metadata.get(key) != value for key, value in native_metadata.items())
            or metadata.get("adp_scope") != selected["publication_scope"]
            or metadata.get("adp_visibility") != selected["visibility"] or metadata.get("adp_active") is not True):
        raise ValueError("native knowledge does not match current scoped source, content or visibility")
    if time.monotonic() >= deadline:
        raise ValueError("installed asset read exceeded authorized deadline")
    return {"scope": scope, "ref": asset["assetId"], "status": "active", "version": asset["version"],
            "source_refs": [{**metadata["adp_source_ref"], "sha256": readback["content_sha256"]}],
            "readback": {"binding": binding, **readback}, "observed_at": datetime.now(UTC).isoformat()}
