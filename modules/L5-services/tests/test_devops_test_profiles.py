"""Known native profile contract, bounded readback and fail-closed preparation."""

import copy
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from l5_services.devops.test_profiles import (
    NativeTestProfileReader, QUALIFICATION_GAPS, require_qualified_test_profile, selected_test_profile,
)
from l5_services.devops.verification_targets import verification_description
from l5_services.run_input import TestProfileRef as ProfileReference

NOW = datetime(2026, 10, 26, 9, tzinfo=UTC)


def fixture():
    reference = {"tenant_id": "tenant-one", "project_id": "project-one", "source_connection_id": "connection-one",
                 "record_type": "test_profile", "record_id": "profile-one", "record_revision": "3"}
    verified = {"scope": {"tenant_id": "tenant-one", "project_id": "project-one", "owner_id": "owner-one"},
                "connection_ref": {**reference, "record_type": "connection", "record_id": "connection-one"},
                "native_space_ref": {**reference, "record_type": "agent_space", "record_id": "space-one"},
                "provider_identity": {"account_id": "123456789012", "region": "us-east-1", "role_arn": "example-role"},
                "trigger": {"test_profile_ref": reference}, "configuration_revision": 1,
                "delegation_version": 1, "binding_generation": 1}
    return reference, verified


class Provider:
    def __init__(self):
        self.calls = []
        self.asset = {"assetId": "profile-one", "assetType": "test_profile", "version": 3,
                      "metadata": {"test_agent_type": "releaseApiTesting", "target_url": "https://api.example.test"}}
        self.page = {"items": [{"path": "unknown-format", "version": 2}]}
        self.file = {"path": "unknown-format", "version": 2, "content": {"text": "private profile bytes"}}
        self.after_read = lambda: None

    def _response(self, operation, args, value):
        self.calls.append((operation, args))
        self.after_read()
        return copy.deepcopy(value)

    def get_agent_space(self, **args):
        return self._response("space", args, {"agentSpace": {"agentSpaceId": "space-one"}})

    def get_asset(self, **args):
        return self._response("asset", args, {"asset": self.asset})

    def list_asset_files(self, **args):
        return self._response("files", args, self.page)

    def get_asset_file(self, **args):
        return self._response("file", args, {"file": self.file})


async def prepare(provider, reference=None, verified=None, authority=None):
    selected, expected = fixture()
    reference, verified = reference or selected, verified or expected

    async def current():
        return copy.deepcopy(verified)

    session = SimpleNamespace(**verified["provider_identity"], client=lambda name: provider)
    return await NativeTestProfileReader(session, authority or current, clock=lambda: NOW).prepare(reference, verified)


@pytest.mark.asyncio
async def test_readback_is_private_unqualified_receipt_with_exact_versioned_reads():
    provider = Provider()
    receipt = await prepare(provider)
    assert receipt["qualification"] == "unqualified" and receipt["gaps"] == list(QUALIFICATION_GAPS)
    assert receipt["test_profile_ref"]["record_revision"] == "3" and receipt["file_count"] == 1
    assert len(receipt["content_sha256"]) == len(receipt["metadata_sha256"]) == 64
    assert "private profile bytes" not in json.dumps(receipt)
    assert "api.example.test" not in json.dumps(receipt)
    assert all(args["agentSpaceId"] == "space-one" for _, args in provider.calls)
    assert all(args["assetId"] == "profile-one" for name, args in provider.calls if name != "space")
    assert all(args["assetVersion"] == 3 for name, args in provider.calls if name in {"files", "file"})
    with pytest.raises(HTTPException) as caught:
        require_qualified_test_profile()
    assert caught.value.detail["gaps"] == receipt["gaps"]


@pytest.mark.parametrize("field,value", [("record_revision", "latest"), ("record_revision", "0"),
    ("record_revision", "-1"), ("record_revision", "03"), ("record_revision", 3),
    ("record_type", "skill"), ("record_id", "bad/id"), ("verified", True)])
def test_profile_selector_is_exact_and_strict(field, value):
    reference, _ = fixture()
    reference[field] = value
    with pytest.raises(ValueError):
        ProfileReference.model_validate(reference)


@pytest.mark.parametrize("change", ["tenant", "project", "connection", "space", "trigger"])
def test_profile_selection_requires_exact_current_binding(change):
    reference, verified = fixture()
    if change in {"tenant", "project", "connection"}:
        reference[{"tenant": "tenant_id", "project": "project_id", "connection": "source_connection_id"}[change]] = "other"
    elif change == "space":
        verified["native_space_ref"]["project_id"] = "other"
    else:
        verified["trigger"] = {}
    with pytest.raises(HTTPException):
        selected_test_profile(reference, verified)


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["id", "version", "type", "ui", "unknown_mode", "missing_mode",
    "metadata", "omitted", "content_omitted", "truncated", "target", "file_version", "content",
    "oversized", "duplicate", "empty"])
async def test_profile_readback_denies_incomplete_or_unsupported_observations(fault):
    provider = Provider()
    if fault in {"id", "version", "type"}:
        key, value = {"id": ("assetId", "other"), "version": ("version", 4), "type": ("assetType", "skill")}[fault]
        provider.asset[key] = value
    elif fault in {"ui", "unknown_mode", "missing_mode"}:
        provider.asset["metadata"]["test_agent_type"] = {"ui": "releaseUiTesting", "unknown_mode": "qaApiTesting", "missing_mode": None}[fault]
    elif fault == "metadata":
        provider.asset.pop("metadata")
    elif fault in {"omitted", "content_omitted", "truncated"}:
        provider.asset[{"omitted": "metadataOmitted", "content_omitted": "contentOmitted", "truncated": "truncated"}[fault]] = True
    elif fault == "target":
        provider.asset["metadata"]["target_url"] = "https://user:secret@api.example.test"
    elif fault == "file_version":
        provider.file["version"] = 3
    elif fault == "content":
        provider.file["content"] = {"text": "partial", "bytes": b"ambiguous"}
    elif fault == "oversized":
        provider.file["content"] = {"text": "x" * (1024 * 1024 + 1)}
    elif fault == "duplicate":
        provider.page["items"] *= 2
    else:
        provider.page["items"] = []
    with pytest.raises(HTTPException):
        await prepare(provider)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["target_url", "test_agent_type", "credential", "spec"])
async def test_profile_metadata_changes_during_read_are_denied(change):
    provider = Provider()

    def drift():
        if provider.calls[-1][0] == "file":
            provider.asset["metadata"][change] = "changed"

    provider.after_read = drift
    with pytest.raises(HTTPException):
        await prepare(provider)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["owner", "grant", "configuration", "binding", "expiry"])
async def test_revocation_during_provider_io_never_returns_proof(change):
    provider = Provider()
    _, verified = fixture()
    calls = 0

    async def authority():
        nonlocal calls
        calls += 1
        if calls == 2:
            if change == "expiry":
                raise HTTPException(403, detail={"code": "expired"})
            return {**verified, change: "revoked"}
        return copy.deepcopy(verified)

    with pytest.raises(HTTPException):
        await prepare(provider, verified=verified, authority=authority)
    assert len(provider.calls) == 1


@pytest.mark.asyncio
async def test_repeating_pagination_and_unavailable_readback_fail_closed():
    provider = Provider()
    provider.page = {"items": [], "nextToken": "repeated-token"}
    with pytest.raises(HTTPException) as caught:
        await prepare(provider)
    assert caught.value.detail["code"] == "test_profile_readback_unbounded"
    assert len([call for call in provider.calls if call[0] == "files"]) == 2

    def unavailable(**args):
        raise RuntimeError("private provider details")

    provider.get_asset = unavailable
    with pytest.raises(HTTPException) as caught:
        await prepare(provider)
    assert caught.value.detail == {"code": "test_profile_readback_unavailable"}


def test_envelope_preserves_original_correlation_and_nonempty_requirement():
    profile, authority = fixture()
    verification = {key: {"record_id": key} for key in ("source_ref", "deployment_ref", "target_ref", "credential_ref")}
    verification["scenario_specs"] = [{"max_requests": 2, "timeout_seconds": 30}]
    args = dict(run_id="run-one", attempt_id="attempt-one", token="a" * 64)
    legacy = json.loads(verification_description(authority["scope"], verification, [], **args))
    wire = verification_description(authority["scope"], verification, [], test_profile_ref=profile, **args)
    envelope = json.loads(wire)
    assert set(envelope) == {"testProfileId", "webhookEventMessage"}
    assert envelope["testProfileId"] == profile["record_id"]
    message = json.loads(envelope["webhookEventMessage"])
    assert all(message[key] == value for key, value in legacy.items())
    assert message["test_profile_ref"] == profile and message["requirement"]
    assert wire == verification_description(authority["scope"], verification, [], test_profile_ref=profile, **args)
    with pytest.raises(ValueError, match="provider limit"):
        verification_description(authority["scope"], verification, ["x" * 10000], test_profile_ref=profile, **args)
