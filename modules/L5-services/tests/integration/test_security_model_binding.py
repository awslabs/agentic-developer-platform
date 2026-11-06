"""Persistent native model setup only in an independently verified owned space."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from hashlib import sha256

import pytest
from sqlalchemy import select

from l5_services.adapters.security.binding import SecurityBindingAdapter
from l5_services.models import Binding, BindingOperation, SetupWork
from l5_services.security_registration import current_model, current_registration
from l5_services.workers.execution import ExecutionService
from test_security_binding import uncertain_setup_scenario


NOW = datetime.now(UTC)
ROLE = "arn:aws:iam::123456789012:role/SourceReader"


async def scope_document():
    content = b"# Architecture\nA client calls the authenticated API."
    return {"content": content, "snapshot": {"snapshot_id": "snapshot-one",
            "content": {"media_type": "text/markdown", "sha256": sha256(content).hexdigest()}}}


class Native:
    def __init__(self):
        self.space = {"agentSpaceId": "space-one", "name": "adp-operation-one",
                      "description": "ADP setup " + "a" * 64, "createdAt": NOW,
                      "awsResources": {"iamRoles": [ROLE], "s3Buckets": ["example-source-staging"]}}
        self.models = {}
        self.creates = []
        self.lost_response = False

    def batch_get_agent_spaces(self, *, agentSpaceIds):
        return {"agentSpaces": [self.space] if agentSpaceIds == [self.space["agentSpaceId"]] else []}

    def list_threat_models(self, *, agentSpaceId, maxResults, **kwargs):
        assert agentSpaceId == self.space["agentSpaceId"] and maxResults == 100
        return {"threatModelSummaries": [
            {"title": model["title"], "threatModelId": model_id}
            for model_id, model in self.models.items()]}

    def add_artifact(self, *, agentSpaceId, artifactContent, artifactType, fileName):
        assert artifactContent.startswith(b"# Architecture") and artifactType == "MD"
        assert agentSpaceId == self.space["agentSpaceId"] and fileName.endswith(".md")
        return {"artifactId": "artifact-one"}

    def create_threat_model(self, *, title, description, agentSpaceId, scopeDocs, serviceRole):
        assert scopeDocs == [{"artifactId": "artifact-one"}]
        assert serviceRole == ROLE
        self.creates.append((title, description, agentSpaceId, scopeDocs))
        model_id = f"model-{len(self.creates)}"
        self.models[model_id] = {"threatModelId": model_id, "title": title,
                                 "description": description, "agentSpaceId": agentSpaceId,
                                 "scopeDocs": scopeDocs, "serviceRole": serviceRole, "createdAt": NOW}
        if self.lost_response:
            raise TimeoutError("response lost after commit")
        return {"threatModelId": model_id}

    def batch_get_threat_models(self, *, agentSpaceId, threatModelIds):
        return {"threatModels": [self.models[model_id] for model_id in threatModelIds
                                 if model_id in self.models]}


def setup():
    native = Native()
    identity = {"account_id": "123456789012", "role_arn": "arn:aws:iam::123456789012:role/Selected",
                "region": "us-east-1", "verified_at": NOW.isoformat()}
    reference = {"tenant_id": "tenant-one", "project_id": "project-one",
                 "source_connection_id": "connection-one", "record_type": "security_space",
                 "record_id": "space-one", "record_revision": NOW.isoformat()}
    receipt = {"operation_id": "operation-one", "intent_hash": "a" * 64,
               "native_resource_ref": reference, "provider_identity": identity}
    binding = SimpleNamespace(ownership="adp_created", creation_receipt=receipt,
                              native_resource_ref=reference, provider_identity=identity,
                              state="ready", gaps=[], readiness=[])
    return native, identity, reference, binding


@pytest.mark.asyncio
async def test_owned_model_is_created_and_reconciled_without_repeating_create():
    native, identity, reference, binding = setup()
    adapter = SecurityBindingAdapter(native, identity, scope_document=scope_document)
    first = await adapter.read(reference, identity, binding=binding)
    assert first["state"] == "ready"
    assert native.creates == [("adp-operation-one-threat-model", "ADP setup " + "a" * 64,
                               "space-one", [{"artifactId": "artifact-one"}])]
    binding.readiness = first["readiness"]
    assert current_registration(binding) == {"service_role": ROLE, "bucket": "example-source-staging"}
    assert current_model(binding)["record_id"] == "model-1"
    second = await adapter.read(reference, identity, binding=binding)
    assert second["readiness"][1]["native_model_ref"] == first["readiness"][1]["native_model_ref"]
    assert len(native.creates) == 1
    binding.readiness[1]["observed_at"] = (NOW - timedelta(hours=1)).isoformat()
    assert current_model(binding) is None


@pytest.mark.asyncio
async def test_lost_create_response_is_recorded_and_only_reconciled():
    native, identity, reference, binding = setup()
    native.lost_response = True
    adapter = SecurityBindingAdapter(native, identity, scope_document=scope_document)
    unknown = await adapter.read(reference, identity, binding=binding)
    assert unknown["readiness"][1]["kind"] == "threat_model_outcome_unknown"
    assert unknown["readiness"][1]["source_snapshot"]["snapshot_id"] == "snapshot-one"
    binding.readiness = unknown["readiness"]
    assert current_model(binding) is None
    native.lost_response = False
    recovered = await adapter.read(reference, identity, binding=binding)
    assert recovered["readiness"][1]["native_model_ref"]["record_id"] == "model-1"
    assert len(native.creates) == 1
    binding.readiness = recovered["readiness"]
    native.models.clear()
    missing = await adapter.read(reference, identity, binding=binding)
    assert missing["readiness"][1]["kind"] == "threat_model_outcome_unknown"
    assert missing["readiness"][1]["native_model_ref"] == recovered["readiness"][1]["native_model_ref"]
    assert len(native.creates) == 1


@pytest.mark.asyncio
async def test_existing_or_ambiguous_model_requires_explicit_selection():
    native, identity, reference, binding = setup()
    native.models["human-model"] = {"threatModelId": "human-model", "agentSpaceId": "space-one",
                                   "title": "adp-operation-one-threat-model",
                                   "description": "human-owned", "serviceRole": ROLE, "createdAt": NOW}
    adapter = SecurityBindingAdapter(native, identity)
    read = await adapter.read(reference, identity, binding=binding)
    assert read["readiness"][1]["kind"] == "threat_model_selection_required"
    assert not native.creates
    binding.readiness = read["readiness"]
    assert (await adapter.read(reference, identity, binding=binding))["readiness"][1] == read["readiness"][1]
    native.models["another-model"] = {**native.models["human-model"], "threatModelId": "another-model"}
    with pytest.raises(ValueError, match="native_model_selection_ambiguous"):
        await adapter.read(reference, identity, binding=binding)
    assert not native.creates


@pytest.mark.asyncio
async def test_linked_or_unverified_space_does_not_create_model():
    native, identity, reference, binding = setup()
    adapter = SecurityBindingAdapter(native, identity)
    binding.ownership = "linked"
    assert len((await adapter.read(reference, identity, binding=binding))["readiness"]) == 1
    binding.ownership = "adp_created"
    native.space["description"] = "human setup"
    assert len((await adapter.read(reference, identity, binding=binding))["readiness"]) == 1
    assert not native.creates


@pytest.mark.asyncio
async def test_product_check_persists_model_readiness_in_durable_binding(uncertain_setup_scenario):
    scenario = uncertain_setup_scenario
    scenario["native"].lost_response = False
    started = await scenario["client"].post(
        scenario["path"] + "/binding-operations", json={"action": "create", "configuration_revision": 1},
        headers={"Idempotency-Key": "model_binding_create_0001"})
    assert started.status_code == 202
    async with scenario["sessions"]() as session:
        work = await session.scalar(select(SetupWork).where(
            SetupWork.operation_id == started.json()["operation_id"]))
        envelope = {"schema_version": "1.0", "work_id": work.work_id, "generation": work.generation}
    worker = ExecutionService(scenario["sessions"], None, None, None, None,
                              binding_authorize=scenario["authorize"])
    assert await worker.execute(envelope)
    operation = (await scenario["client"].get(
        scenario["path"] + "/binding-operations/" + started.json()["operation_id"])).json()
    binding_id = operation["binding_id"]
    native = Native()
    native.space = scenario["native"].spaces["adp-" + started.json()["operation_id"]]
    native.space["awsResources"] = {"iamRoles": [ROLE], "s3Buckets": ["example-source-staging"]}
    scenario["adapter"].client = native
    scenario["adapter"].scope_document = scope_document

    for generation in range(2):
        binding_response = await scenario["client"].get(scenario["path"] + "/bindings/" + binding_id)
        request = {"action": "check", "configuration_revision": 1, "binding_id": binding_id}
        if generation:
            request["native_model_ref"] = persisted.readiness[1]["native_model_ref"]
        check = await scenario["client"].post(
            scenario["path"] + "/binding-operations", json=request,
            headers={"Idempotency-Key": f"model_binding_check_{generation:04d}",
                     "If-Match": binding_response.headers["ETag"]})
        assert check.status_code == 202, check.text
        async with scenario["sessions"]() as session:
            work = await session.scalar(select(SetupWork).where(
                SetupWork.operation_id == check.json()["operation_id"]))
            envelope = {"schema_version": "1.0", "work_id": work.work_id, "generation": work.generation}
        assert await worker.execute(envelope)
        async with scenario["sessions"]() as reader:
            persisted = await reader.get(Binding, ("tenant-one", binding_id))
            operation = await reader.get(BindingOperation, ("tenant-one", check.json()["operation_id"]))
            assert persisted.state == "ready", (operation.state, operation.gaps)
            assert persisted.readiness[1]["native_model_ref"]["record_id"] == "model-1"
    assert len(native.creates) == 1


@pytest.mark.asyncio
async def test_explicit_selection_requires_exact_scoped_native_revision():
    native, identity, reference, binding = setup()
    model_id = "existing-model"
    native.models[model_id] = {"threatModelId": model_id, "agentSpaceId": "space-one",
                               "title": "adp-operation-one-threat-model", "description": "ADP setup " + "a" * 64,
                               "serviceRole": ROLE, "createdAt": NOW}
    native.models["another-model"] = {**native.models[model_id], "threatModelId": "another-model"}
    selected = {field: reference[field] for field in ("tenant_id", "project_id", "source_connection_id")}
    selected.update(record_type="threat_model", record_id=model_id, record_revision=NOW.isoformat())
    adapter = SecurityBindingAdapter(native, identity)
    with pytest.raises(ValueError, match="native_model_selection_ambiguous"):
        await adapter.read(reference, identity, binding=binding)
    for invalid in ({**selected, "tenant_id": "other-tenant"},
                    {**selected, "record_revision": "invented"},
                    {**selected, "record_id": "not-listed"}):
        with pytest.raises(ValueError, match="native_model_selection_denied"):
            await adapter.read(reference, identity, binding=binding, selected_model=invalid)
    observed = await adapter.read(reference, identity, binding=binding, selected_model=selected)
    assert observed["readiness"][1]["native_model_ref"] == selected
    assert not native.creates


@pytest.mark.asyncio
async def test_model_creation_requires_real_document_and_preserves_definitive_error():
    from botocore.exceptions import ClientError

    native, identity, reference, binding = setup()
    adapter = SecurityBindingAdapter(native, identity)
    with pytest.raises(ValueError, match="security_architecture_document_required"):
        await adapter.read(reference, identity, binding=binding)
    assert not native.creates

    def rejected(**_):
        raise ClientError({"Error": {"Code": "ValidationException", "Message": "private provider text"}}, "CreateThreatModel")

    native.create_threat_model = rejected
    adapter.scope_document = scope_document
    with pytest.raises(ValueError, match="security_threat_model_create_rejected"):
        await adapter.read(reference, identity, binding=binding)
