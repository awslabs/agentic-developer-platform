"""Read-only preparation of a selected native profile, never native qualification.

The provider's generic Asset file transport is documented. Its test-profile
content schema and execution-time version consumption are not yet qualified.
Do not mount this reader or turn its digest receipt into permission to submit.
"""

import asyncio
import copy
import json
from datetime import UTC, datetime
from hashlib import sha256
from urllib.parse import urlsplit

from fastapi import HTTPException

from l5_services.run_input import TestProfileRef

ADAPTER_REVISION = "devops-release-verification-v2"
QUALIFICATION_GAPS = (
    "test_profile_schema_unqualified",
    "test_profile_version_unqualified",
    "verification_api_target_unavailable",
    "native_report_schema_unqualified",
)
MAX_FILES = 20
MAX_BYTES = 1024 * 1024
MAX_METADATA_BYTES = 65536


def _deny(code):
    raise HTTPException(409, detail={"code": code})


def require_qualified_test_profile():
    # There is intentionally no caller-controlled ready/verified escape hatch.
    raise HTTPException(409, detail={
        "code": QUALIFICATION_GAPS[0], "gaps": list(QUALIFICATION_GAPS),
        "message": "Select an API test profile and qualify its content schema, consumed version, "
                   "independent HTTP API target, and native scenario report before enabling verification.",
    })


def selected_test_profile(reference, verified):
    try:
        selected = TestProfileRef.model_validate(reference).model_dump()
    except ValueError:
        _deny("native_test_profile_required")
    scope = verified.get("scope") or {}
    space = verified.get("native_space_ref") or {}
    connection = verified.get("connection_ref") or {}
    if (selected != (verified.get("trigger") or {}).get("test_profile_ref")
            or (selected["tenant_id"], selected["project_id"]) != (
                scope.get("tenant_id"), scope.get("project_id"))
            or selected["source_connection_id"] != connection.get("source_connection_id")
            or (space.get("tenant_id"), space.get("project_id"), space.get("source_connection_id")) != (
                selected["tenant_id"], selected["project_id"], selected["source_connection_id"])
            or space.get("record_type") != "agent_space" or not space.get("record_id")):
        _deny("test_profile_selection_mismatch")
    return selected


def _canonical(value, maximum):
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    except (TypeError, ValueError):
        _deny("test_profile_readback_incomplete")
    if len(encoded) > maximum:
        _deny("test_profile_readback_unbounded")
    return encoded


def _complete(value):
    if isinstance(value, dict):
        if any(value.get(key) for key in ("metadataOmitted", "contentOmitted", "truncated")):
            _deny("test_profile_readback_incomplete")
        for child in value.values():
            _complete(child)
    elif isinstance(value, list):
        for child in value:
            _complete(child)


class NativeTestProfileReader:
    """Requires a selected broker session and a current authority read callback.

    The callback must re-read the owner/session, configuration revision, binding,
    source access and grant revocation/expiry, returning the unchanged authority
    snapshot or raising. It runs before and after every provider read. This
    helper is deliberately not installed in the runtime while P5/P6 are open.
    """

    def __init__(self, provider_session, revalidate, *, clock=None):
        self.session = provider_session
        self.revalidate = revalidate
        self.clock = clock or (lambda: datetime.now(UTC))

    async def prepare(self, reference, verified):
        expected = copy.deepcopy(verified)
        reference = selected_test_profile(reference, expected)
        identity = expected.get("provider_identity") or {}
        if (not callable(self.revalidate)
                or (self.session.account_id, self.session.region, self.session.role_arn) != (
                    identity.get("account_id"), identity.get("region"), identity.get("role_arn"))):
            _deny("test_profile_selection_mismatch")
        client = self.session.client("devops-agent")
        space_id = expected["native_space_ref"]["record_id"]
        version = int(reference["record_revision"])
        base = {"agentSpaceId": space_id, "assetId": reference["record_id"]}

        async def read(operation, **arguments):
            for phase in ("before", "after"):
                current = await self.revalidate()
                if current != expected:
                    _deny("test_profile_authority_changed")
                if phase == "before":
                    try:
                        response = await asyncio.to_thread(getattr(client, operation), **arguments)
                    except Exception:
                        _deny("test_profile_readback_unavailable")
            if not isinstance(response, dict):
                _deny("test_profile_readback_incomplete")
            _complete(response)
            return response

        space = await read("get_agent_space", agentSpaceId=space_id)
        if (space.get("agentSpace") or {}).get("agentSpaceId") != space_id:
            _deny("test_profile_selection_mismatch")

        def asset(response):
            item = response.get("asset")
            if (not isinstance(item, dict) or item.get("assetId") != reference["record_id"]
                    or item.get("assetType") != "test_profile"
                    or type(item.get("version")) is not int or item["version"] != version
                    or not isinstance(item.get("metadata"), dict)):
                _deny("test_profile_selection_mismatch")
            metadata = item["metadata"]
            if metadata.get("test_agent_type") != "releaseApiTesting":
                _deny("test_profile_api_mode_required")
            target = metadata.get("target_url")
            try:
                url = urlsplit(target) if isinstance(target, str) else None
                valid = (url is not None and url.scheme in {"http", "https"} and url.hostname
                         and not url.username and not url.password and not url.fragment)
            except ValueError:
                valid = False
            if not valid:
                _deny("test_profile_target_unverified")
            return sha256(_canonical(metadata, MAX_METADATA_BYTES)).hexdigest()

        # These checks detect drift; they do not prove which version a future
        # ID-only trigger would consume. That remains a hard qualification gap.
        metadata_digest = asset(await read("get_asset", **base, assetVersion=version))
        if asset(await read("get_asset", **base)) != metadata_digest:
            _deny("test_profile_changed")
        files, paths, tokens, token, total = [], set(), set(), None, 0
        while True:
            page = await read("list_asset_files", **base, assetVersion=version, maxResults=MAX_FILES,
                              **({"nextToken": token} if token else {}))
            if not isinstance(page.get("items"), list) or len(page["items"]) + len(files) > MAX_FILES:
                _deny("test_profile_readback_unbounded")
            for item in page["items"]:
                path = item.get("path") if isinstance(item, dict) else None
                if (not isinstance(path, str) or not 0 < len(path) <= 512 or path in paths
                        or type(item.get("version")) is not int or item["version"] < 1):
                    _deny("test_profile_readback_incomplete")
                paths.add(path)
                response = await read("get_asset_file", **base, assetVersion=version, path=path)
                file = response.get("file")
                if (not isinstance(file, dict) or file.get("path") != path
                        or type(file.get("version")) is not int or file["version"] != item["version"]
                        or not isinstance(file.get("content"), dict)):
                    _deny("test_profile_readback_incomplete")
                content = file["content"]
                if set(content) == {"text"} and isinstance(content["text"], str):
                    data = content["text"].encode()
                elif set(content) == {"bytes"} and isinstance(content["bytes"], bytes):
                    data = content["bytes"]
                else:
                    _deny("test_profile_readback_incomplete")
                total += len(data)
                if total > MAX_BYTES:
                    _deny("test_profile_readback_unbounded")
                files.append({"path": path, "version": file["version"], "bytes": len(data),
                              "sha256": sha256(data).hexdigest(),
                              "metadata_sha256": sha256(_canonical(file.get("metadata", {}),
                                                                       MAX_METADATA_BYTES)).hexdigest()})
            token = page.get("nextToken")
            if not token:
                break
            if not isinstance(token, str) or len(token) > 2048 or token in tokens or len(tokens) >= 9:
                _deny("test_profile_readback_unbounded")
            tokens.add(token)
        if not files:
            _deny("test_profile_readback_incomplete")
        if asset(await read("get_asset", **base)) != metadata_digest:
            _deny("test_profile_changed")
        return {
            "test_profile_ref": reference, "native_space_ref": expected["native_space_ref"],
            "account_id": identity["account_id"], "region": identity["region"],
            "metadata_sha256": metadata_digest,
            "content_sha256": sha256(_canonical(sorted(files, key=lambda row: row["path"]),
                                                   MAX_METADATA_BYTES)).hexdigest(),
            "content_bytes": total, "file_count": len(files), "reader_revision": "native-test-profile-v1",
            "observed_at": self.clock().isoformat().replace("+00:00", "Z"),
            "qualification": "unqualified", "gaps": list(QUALIFICATION_GAPS),
        }
