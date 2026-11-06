"""Finite provider command contract; all observations are local fixtures."""

from copy import deepcopy

import pytest

from l5_services.adapters.transform.execution import TransformAdapter
from l5_services.adapters.transform.provider_job import finite_provider_job
from test_tf03_adapter import pinned


def admitted():
    run, manifest, attempt = pinned()
    run.admission["transform_qualification"] = {
        **run.admission["transform_qualification"],
        "interface": "atx-custom-def-exec-v1", "definition_revision": "revision-a"}
    run.admission["transform_settings"]["definition_ref"] = {
        "tenant_id": run.tenant_id, "project_id": run.project_id,
        "source_connection_id": "provider-definition", "record_type": "transform_definition",
        "record_id": "upgrade-example", "record_revision": "revision-a"}
    run.admission["limits"] = {"deadline_seconds": 3600, "agent_minutes": 10}
    return run, manifest, attempt


class FakeControl:
    def __init__(self):
        self.launches = []

    async def launch_provider(self, *, intent, manifest, authority):
        self.launches.append((intent, manifest, authority))
        return None

    async def read_provider(self, **kwargs):
        return None

    async def collect_provider(self, **kwargs):
        return None

    async def request_stop(self, **kwargs):
        return None

    async def read_original_patch(self, **kwargs):
        return b""

    async def read_verified_trees(self, **kwargs):
        return {}

    async def read_test_baseline(self, **kwargs):
        return []

    async def read_sealed_source(self, **kwargs):
        return {}


@pytest.mark.asyncio
async def test_strict_submit_builds_finite_credential_free_job_spec_before_one_control_call():
    run, manifest, attempt = admitted()
    control = FakeControl()
    adapter = TransformAdapter(control, detached_runner=object(), retention_store=object(),
                               require_original_patch=True)
    assert (await adapter.submit(run, manifest, attempt, object())).classification == "unknown"
    assert len(control.launches) == 1
    intent, _, _ = control.launches[0]
    job = intent["provider_job"]
    assert job["image_digest"] == run.admission["transform_qualification"]["image_digest"]
    assert job["active_deadline_seconds"] == 3600
    assert job["argv"] == [
        "/usr/local/bin/atx", "custom", "def", "exec", "--code-repository-path", "/work/source",
        "--transformation-name", "upgrade-example", "--transformation-version", "revision-a",
        "--non-interactive", "--do-not-learn", "--limit", "10"]
    assert "--trust-all-tools" not in job["argv"] and "--build-command" not in job["argv"]
    assert all("secret" not in str(value).lower() for value in job.values())


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [
    "missing_definition", "foreign_definition", "wrong_revision", "unsafe_name",
    "missing_limit", "bool_limit", "over_limit", "beyond_deadline", "unqualified_tools",
    "other_source", "unsupported_interface",
])
async def test_unsafe_or_unadmitted_job_never_reaches_control(change):
    run, manifest, attempt = admitted()
    if change == "missing_definition":
        run.admission["transform_settings"].pop("definition_ref")
    elif change == "foreign_definition":
        run.admission["transform_settings"]["definition_ref"]["tenant_id"] = "another-tenant"
    elif change == "wrong_revision":
        run.admission["transform_settings"]["definition_ref"]["record_revision"] = "revision-b"
    elif change == "unsafe_name":
        run.admission["transform_settings"]["definition_ref"]["record_id"] = "--trust-all-tools"
    elif change == "missing_limit":
        run.admission["limits"].pop("agent_minutes")
    elif change == "bool_limit":
        run.admission["limits"]["agent_minutes"] = True
    elif change == "over_limit":
        run.admission["limits"]["agent_minutes"] = 61
    elif change == "beyond_deadline":
        run.admission["limits"]["agent_minutes"] = 60
        run.admission["limits"]["deadline_seconds"] = 180
    elif change == "unqualified_tools":
        run.admission["transform_qualification"]["tool_isolation"] = {
            "status": "unsupported", "reason": "file-only mediation not qualified"}
    elif change == "other_source":
        manifest["scope"] = {**manifest["scope"], "tenant_id": "another-tenant"}
    else:
        run.admission["transform_qualification"]["interface"] = "synthetic"
    control = FakeControl()
    adapter = TransformAdapter(control, detached_runner=object(), retention_store=object(),
                               require_original_patch=True)
    with pytest.raises((PermissionError, ValueError)):
        await adapter.submit(run, manifest, attempt, object())
    assert not control.launches


def test_manifest_pins_and_token_are_required_by_command_builder():
    run, manifest, attempt = admitted()
    intent = TransformAdapter(FakeControl())._intent(run, attempt)
    altered = deepcopy(intent)
    altered["attempt_token"] = "different"
    with pytest.raises(ValueError):
        finite_provider_job(altered, manifest, run.admission["transform_qualification"], run.admission["limits"])


def test_runtime_image_must_match_pinned_job_image():
    run, manifest, attempt = admitted()
    intent = TransformAdapter(FakeControl())._intent(run, attempt)
    altered = deepcopy(intent)
    altered["image_digest"] = "sha256:" + "f" * 64
    with pytest.raises(ValueError):
        finite_provider_job(altered, manifest, run.admission["transform_qualification"], run.admission["limits"])


@pytest.mark.parametrize("name", ["AWS/python-version-upgrade", "AWS/security-issue-fixer",
                                 "AWS/comprehensive-codebase-analysis", "upgrade-example"])
@pytest.mark.parametrize("interface", ["atx-custom-def-exec-v1", "atx-json-3.19.0-v1"])
def test_native_definition_identifier_is_preserved_as_one_value(name, interface):
    from l5_services.adapters.transform.provider_job import NDJSON_BINARY_SHA256

    run, manifest, attempt = admitted()
    run.admission["transform_settings"]["definition_ref"]["record_id"] = name
    run.admission["transform_qualification"].update(interface=interface, cli_version="3.19.0",
                                                  binary_sha256=NDJSON_BINARY_SHA256)
    intent = TransformAdapter(FakeControl())._intent(run, attempt)
    job = finite_provider_job(intent, manifest, run.admission["transform_qualification"], run.admission["limits"])
    if interface == "atx-json-3.19.0-v1":
        assert job["session_request"]["params"]["transformationName"] == name
    else:
        assert job["argv"][job["argv"].index("--transformation-name") + 1] == name


@pytest.mark.parametrize("name", ["/AWS/name", "AWS/", "AWS//name", "AWS/../name",
                                 "AWS/--flag", "AWS/name\n--flag", "AWS/name;true", "AWS/name/extra"])
def test_invalid_native_definition_identifier_is_rejected(name):
    run, manifest, attempt = admitted()
    run.admission["transform_settings"]["definition_ref"]["record_id"] = name
    intent = TransformAdapter(FakeControl())._intent(run, attempt)
    with pytest.raises(ValueError):
        finite_provider_job(intent, manifest, run.admission["transform_qualification"], run.admission["limits"])
