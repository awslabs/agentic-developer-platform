"""Read current Product document authority and immutable native GitHub bytes."""

import base64
import binascii
import hashlib
import json
import os
import re
import time
from datetime import UTC, datetime
from urllib.parse import quote

from installed_journeys import release_readback_tokens
from journey_driver import validate_driver
from qualification import digest, timestamp


def validate_installed_sources(manifest):
    context = manifest["context"]
    selected = context.get("installed_sources")
    if not isinstance(selected, dict) or set(selected) != {"product_token_env", "github_token_env", "bindings"}:
        raise ValueError("installed sources need independent Product and GitHub readers and exact bindings")
    tokens = release_readback_tokens(manifest)
    driver = validate_driver(manifest)
    tokens.update(os.environ[driver[key]] for key in ("api_token_env", "mcp_token_env"))
    for channels in (context["action_channels"], context["readback_channels"]):
        tokens.update(os.environ[channel["token_env"]] for channel in channels.values())
    if "installed_asset" in context:
        tokens.add(os.environ[context["installed_asset"]["token_env"]])
    for field in ("product_token_env", "github_token_env"):
        name = selected.get(field)
        if (not isinstance(name, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]+", name)
                or not os.environ.get(name) or os.environ[name] in tokens):
            raise ValueError("installed source credentials must be present and independent")
        tokens.add(os.environ[name])
    scope = context.get("scope")
    sources = context.get("sources")
    bindings = selected["bindings"]
    if (not isinstance(scope, dict) or set(scope) != {"tenant_id", "product_id", "project_id"}
            or any(not isinstance(value, str) or not re.fullmatch(r"[\w-]{1,128}", value) for value in scope.values())
            or any(scope[field] != driver["scope"][field] for field in ("product_id", "project_id"))
            or not isinstance(sources, list) or not sources or len(sources) > 10
            or not isinstance(bindings, dict) or len(bindings) != len(sources)
            or any(not isinstance(value, str) or not re.fullmatch(r"[\w-]{1,128}", value) for value in bindings.values())
            or len(set(bindings.values())) != len(bindings)):
        raise ValueError("installed sources require distinct scoped Product document bindings")
    for source in sources:
        if (not isinstance(source, dict) or set(source) != {
                "tenant_id", "project_id", "source_connection_id", "record_type", "record_id", "record_revision", "sha256"}
                or any(source[field] != scope[field] for field in ("tenant_id", "project_id"))
                or source["record_type"] != "document" or not isinstance(source["source_connection_id"], str)
                or not re.fullmatch(r"[\w-]{1,128}", source["source_connection_id"])
                or not digest(source["record_revision"], 40) or not digest(source["sha256"], 64)
                or not isinstance(source["record_id"], str) or source["record_id"] not in bindings):
            raise ValueError("installed source must select an immutable GitHub document")
        repository, separator, path = source["record_id"].partition(":")
        if (not separator or not re.fullmatch(r"[1-9][0-9]{0,19}", repository)
                or not path or len(path) > 400 or "\\" in path
                or any(part in {"", ".", ".."} for part in path.split("/"))
                or any(ord(character) < 32 for character in path)):
            raise ValueError("installed source needs repository-id:relative/path, not a URL")
    if {source["record_id"] for source in sources} != set(bindings):
        raise ValueError("installed source bindings differ from the selected documents")
    return selected


def _get(target, url, token_env, *, params=None):
    target.calls += 1
    remaining = min(target.deadline - time.monotonic(),
                    (timestamp(target.manifest["authorization"]["expires_at"]) - datetime.now(UTC)).total_seconds())
    if remaining <= 0 or target.calls >= target.max_requests - 4:
        raise ValueError("installed source read exceeded the authorized deadline or request bound")
    with target.client.stream("GET", url, params=params,
                              headers={"Authorization": "Bearer " + os.environ[token_env], "Accept": "application/json"},
                              timeout=min(remaining, 15)) as response:
        if response.status_code != 200:
            raise ValueError("installed source read unavailable or redirected")
        content = bytearray()
        for chunk in response.iter_bytes():
            content.extend(chunk)
            if len(content) > 1024 * 1024:
                raise ValueError("installed source response is oversized")
    body = json.loads(content)
    if not isinstance(body, dict):
        raise ValueError("installed source read must return an object")
    return body


def _binding(target, base, selected, scope, source):
    profile = _get(target, base, selected["product_token_env"])
    if any(profile.get(field) != value for field, value in scope.items()):
        raise ValueError("installed Product profile belongs to another scope")
    matches, cursors, cursor = [], set(), None
    while True:
        page = _get(target, base + "/bindings", selected["product_token_env"],
                    params={"limit": 100, **({"cursor": cursor} if cursor else {})})
        if not isinstance(page.get("items"), list) or any(not isinstance(item, dict) for item in page["items"]):
            raise ValueError("installed Product bindings are unavailable")
        matches.extend(item for item in page["items"] if item.get("id") == selected["bindings"][source["record_id"]])
        cursor = page.get("next_cursor")
        if cursor is None:
            break
        if not isinstance(cursor, str) or not re.fullmatch(r"[\w-]{1,128}", cursor) or cursor in cursors:
            raise ValueError("installed Product binding pagination did not advance")
        cursors.add(cursor)
    if len(matches) != 1:
        raise ValueError("installed source binding is missing or ambiguous")
    binding = matches[0]
    selection = binding.get("selection") or {}
    if (binding.get("project_id") != scope["project_id"] or binding.get("status") != "healthy"
            or binding.get("source_revision") != source["record_revision"]
            or type(binding.get("revision")) is not int or binding["revision"] < 1
            or type(binding.get("generation")) is not int or binding["generation"] < 1
            or selection.get("kind") != "document" or selection.get("provider") != "github"
            or selection.get("native_id") != source["record_id"]
            or selection.get("connection_ref") != {"kind": "github_installation", "id": source["source_connection_id"]}
            or not isinstance(selection.get("namespace"), str)
            or not re.fullmatch(r"[1-9][0-9]{0,19}", selection["namespace"])
            or not timestamp(binding.get("observed_at")) <= datetime.now(UTC) < timestamp(binding.get("stale_after"))):
        raise ValueError("installed source binding is foreign, revoked, stale or changed")
    return binding


def read_installed_source(target, scope, source):
    manifest = target.manifest
    selected = validate_installed_sources(manifest)
    if scope != manifest["context"]["scope"] or source not in manifest["context"]["sources"]:
        raise ValueError("installed source read requested foreign scope or source")
    driver = manifest["journey_driver"]
    base = (driver["gateway_url"].rstrip("/") + "/api/v1/products/" + quote(scope["product_id"], safe="")
            + "/projects/" + quote(scope["project_id"], safe=""))
    binding = _binding(target, base, selected, scope, source)
    repository = source["record_id"].split(":", 1)[0]
    native_url = "https://api.github.com/repositories/" + repository
    native = _get(target, native_url, selected["github_token_env"])
    owner = native.get("owner") or {}
    login, name = owner.get("login"), native.get("name")
    if (str(native.get("id")) != repository
            or str(owner.get("id")) != binding["selection"]["namespace"]
            or not isinstance(login, str) or not re.fullmatch(r"[A-Za-z0-9-]{1,39}", login)
            or not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", name)
            or name in {".", ".."}):
        raise ValueError("native repository differs from the selected Product source")
    blob_url = "https://api.github.com/repos/" + quote(login, safe="") + "/" + quote(name, safe="") + "/git/blobs/"
    blob = _get(target, blob_url + source["record_revision"], selected["github_token_env"])
    if (blob.get("sha") != source["record_revision"] or blob.get("encoding") != "base64"
            or type(blob.get("size")) is not int or not 0 < blob["size"] <= 65536
            or not isinstance(blob.get("content"), str) or len(blob["content"]) > 131072):
        raise ValueError("native document is not the selected bounded blob")
    try:
        content = base64.b64decode(blob["content"].replace("\n", ""), validate=True)
    except (ValueError, binascii.Error):
        raise ValueError("native document encoding is invalid") from None
    native_digest = hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content).hexdigest()
    if (len(content) != blob["size"] or native_digest != source["record_revision"]
            or hashlib.sha256(content).hexdigest() != source["sha256"]):
        raise ValueError("native document bytes differ from the pinned source digests")
    if _binding(target, base, selected, scope, source) != binding:
        raise ValueError("Product source authority changed during native readback")
    if (time.monotonic() >= target.deadline
            or datetime.now(UTC) >= timestamp(manifest["authorization"]["expires_at"])):
        raise ValueError("installed source read exceeded the authorized deadline")
    return {"scope": scope, "record_revision": native_digest, "content": content, "revoked": False,
            "readback": {"binding": binding, "repository_id": repository, "blob_sha": native_digest},
            "observed_at": datetime.now(UTC).isoformat()}
