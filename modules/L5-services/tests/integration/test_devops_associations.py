"""Offline native association recovery with scoped Product and provider fakes."""

from copy import deepcopy
from datetime import UTC, datetime

import pytest
from botocore.session import Session
from botocore.stub import Stubber

from l5_services.adapters.devops.integrations import (
    IntegrationAccessDenied, IntegrationAdapter, NativeIntegrationAssociation,
)
from test_devops_integrations import CloudWatch, GitHub, Product, fixture


class Native:
    def __init__(self, selection, *, registered=False):
        self.registrations = []
        self.associations = []
        self.association_creates = 0
        self.disassociations = 0
        self.lose_association_ack = False
        self.selection = selection
        if registered:
            self.registrations.append({"serviceId": "native-service-example", "serviceType": selection["kind"],
                                       "additionalServiceDetails": {"github": {"owner": "example-owner", "ownerType": "organization"}}})

    def list_services(self, **kwargs):
        return {"services": deepcopy(self.registrations)}

    def get_service(self, *, serviceId):
        return {"service": deepcopy(next(item for item in self.registrations if item["serviceId"] == serviceId))}

    def list_associations(self, *, agentSpaceId, **kwargs):
        return {"associations": deepcopy([item for item in self.associations
                                          if item["agentSpaceId"] == agentSpaceId])}

    def associate_service(self, *, agentSpaceId, serviceId, configuration, capabilities):
        assert all(value == {"enabled": False} for value in capabilities.values())
        self.association_creates += 1
        association = {"associationId": f"native-association-{self.association_creates}", "agentSpaceId": agentSpaceId,
                       "serviceId": serviceId, "configuration": deepcopy(configuration), "status": "valid",
                       "capabilities": deepcopy(capabilities)}
        self.associations.append(association)
        if self.lose_association_ack:
            self.lose_association_ack = False
            raise ConnectionError("response lost after native association")
        return {"association": deepcopy(association)}

    def get_association(self, *, agentSpaceId, associationId):
        return {"association": deepcopy(next(item for item in self.associations
                                               if item["associationId"] == associationId))}

    def disassociate_service(self, *, agentSpaceId, associationId):
        self.disassociations += 1
        self.associations = [item for item in self.associations if item["associationId"] != associationId]


def setup(kind="github", *, registered=False):
    fixtures = {"alpha": fixture("alpha", "901", "181", "feature-ref",
                                  "arn:aws:logs:region-a:111111111111:log-group:alpha")}
    integrations = fixtures["alpha"][0]
    selected = integrations[0 if kind == "github" else 1]
    native = Native(selected, registered=registered)
    adapter = IntegrationAdapter(Product(fixtures), GitHub(), CloudWatch())
    return fixtures, selected, native, adapter


@pytest.mark.asyncio
async def test_native_service_discovery_uses_installed_sdk_and_reports_missing_consent():
    _, selected, _, adapter = setup()
    native = Session().create_client("devops-agent", region_name="us-east-1",
                                     aws_access_key_id="testing", aws_secret_access_key="testing")
    with Stubber(native) as stub:
        stub.add_response("list_services", {"services": []},
                          {"filterServiceType": "github", "maxResults": 100})
        with pytest.raises(IntegrationAccessDenied, match="native_registration_requires_consent"):
            await adapter.reconcile("tenant-example", "product", "alpha", [selected],
                                    "space-example", "operation-example", native,
                                    allow_registration_create=True)
        stub.assert_no_pending_responses()


@pytest.mark.asyncio
async def test_supported_sdk_associates_exact_repository_and_preserves_registration():
    _, selected, _, adapter = setup()
    native = Session().create_client("devops-agent", region_name="us-east-1",
                                     aws_access_key_id="testing", aws_secret_access_key="testing")
    observed_at = datetime(2026, 1, 1, tzinfo=UTC)
    service = {"serviceId": "00000000-0000-4000-8000-000000000001", "serviceType": "github",
               "createdAt": observed_at, "updatedAt": observed_at,
               "additionalServiceDetails": {"github": {"owner": "example-owner", "ownerType": "organization"}}}
    space_id = "00000000-0000-4000-8000-000000000002"
    association_id = "00000000-0000-4000-8000-000000000003"
    configuration = {"github": {"repoName": "repository-901", "repoId": "901", "owner": "example-owner",
                                 "ownerType": "organization", "instanceIdentifier": "github.com"}}
    disabled = {"RELEASE_READINESS_REVIEW": {"enabled": False},
                "RELEASE_READINESS_REVIEW_AUTOMATED_TESTING": {"enabled": False}}
    association = {"agentSpaceId": space_id, "serviceId": service["serviceId"], "associationId": association_id,
                   "createdAt": observed_at, "updatedAt": observed_at, "status": "valid",
                   "configuration": configuration,
                   "capabilities": {name: {**value, "triggerFilterGroups": [{"events": ["PULL_REQUEST_READY_FOR_REVIEW"]}]}
                                    for name, value in disabled.items()}}
    with Stubber(native) as stub:
        stub.add_response("list_services", {"services": [], "nextToken": "next-page"},
                          {"filterServiceType": "github", "maxResults": 100})
        stub.add_response("list_services", {"services": [service]},
                          {"filterServiceType": "github", "maxResults": 100, "nextToken": "next-page"})
        stub.add_response("get_service", {"service": service}, {"serviceId": service["serviceId"]})
        stub.add_response("list_associations", {"associations": []}, {"agentSpaceId": space_id, "maxResults": 100})
        stub.add_response("associate_service", {"association": association},
                          {"agentSpaceId": space_id, "serviceId": service["serviceId"],
                           "configuration": configuration, "capabilities": disabled})
        stub.add_response("get_association", {"association": association},
                          {"agentSpaceId": space_id, "associationId": association_id})
        receipt = (await adapter.reconcile("tenant-example", "product", "alpha", [selected],
                                            space_id, "operation-example", native))[0]
        assert receipt["registration_id"] == service["serviceId"]
        assert receipt["configuration"] == configuration
        assert receipt["source_ref"] == selected["repository_refs"][0]
        stub.add_response("get_association", {"association": association},
                          {"agentSpaceId": space_id, "associationId": association_id})
        stub.add_response("disassociate_service", {}, {"agentSpaceId": space_id, "associationId": association_id})
        assert NativeIntegrationAssociation(native).cleanup(receipt) == "association_removed_registration_preserved"
        stub.assert_no_pending_responses()


@pytest.mark.asyncio
@pytest.mark.parametrize("change,reason", [
    ("duplicate", "duplicate_native_registrations"),
    ("owner", "native_registration_requires_consent"),
    ("instance", "native_registration_requires_consent"),
    ("owner_type", "native_registration_requires_consent"),
    ("changed", "native_registration_readback_mismatch"),
    ("pagination", "native_discovery_incomplete"),
])
async def test_native_registration_discovery_denies_ambiguous_foreign_and_stale_services(change, reason):
    _, selected, native, adapter = setup(registered=True)
    if change == "duplicate":
        native.registrations.append({**deepcopy(native.registrations[0]), "serviceId": "another-service"})
    elif change == "owner":
        native.registrations[0]["additionalServiceDetails"]["github"]["owner"] = "another-owner"
    elif change == "instance":
        native.registrations[0]["additionalServiceDetails"]["github"]["targetUrl"] = "https://github.example.org"
    elif change == "owner_type":
        native.registrations[0]["additionalServiceDetails"]["github"]["ownerType"] = "user"
    elif change == "changed":
        native.get_service = lambda **kwargs: {"service": {**native.registrations[0], "serviceId": "another-service"}}
    else:
        native.list_services = lambda **kwargs: {"services": [], "nextToken": "repeated"}
    with pytest.raises(IntegrationAccessDenied, match=reason):
        await adapter.reconcile("tenant-example", "product", "alpha", [selected], "space-example", "operation-example", native)
    assert native.association_creates == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["missing", "id", "owner_type", "full_name", "url"])
async def test_native_association_needs_current_repository_metadata(change):
    _, selected, native, adapter = setup(registered=True)
    read = adapter.github.read_repository

    async def malformed(*args):
        result = await read(*args)
        if change == "missing":
            result.pop("repository")
        elif change == "owner_type":
            result["repository"]["owner"]["type"] = "Unknown"
        else:
            result["repository"][{"id": "id", "full_name": "full_name", "url": "html_url"}[change]] = "foreign"
        return result

    adapter.github.read_repository = malformed
    with pytest.raises(IntegrationAccessDenied, match="native_repository_unverified"):
        await adapter.reconcile("tenant-example", "product", "alpha", [selected], "space-example", "operation-example", native)
    assert native.association_creates == 0


@pytest.mark.asyncio
async def test_each_selected_repository_has_its_own_native_association():
    fixtures, selected, native, adapter = setup(registered=True)
    second = {**selected["repository_refs"][0], "record_id": "902"}
    selected["repository_refs"].append(second)
    fixtures["alpha"][1]["items"][0]["resources"].append(second)
    binding = deepcopy(fixtures["alpha"][2]["items"][0])
    binding["selection"]["native_id"] = "902"
    fixtures["alpha"][2]["items"].append(binding)
    receipts = await adapter.reconcile("tenant-example", "product", "alpha", [selected], "space-example", "operation-example", native)
    assert {receipt["configuration"]["github"]["repoId"] for receipt in receipts} == {"901", "902"}
    assert native.association_creates == 2 and len(native.registrations) == 1
    adopted = await adapter.reconcile("tenant-example", "product", "alpha", [selected], "space-example", "operation-example", native,
                                       reconcile_only=True)
    assert len(adopted) == 2 and native.association_creates == 2


@pytest.mark.asyncio
async def test_native_creation_does_not_accept_unrequested_automation():
    _, selected, native, adapter = setup(registered=True)
    original = native.get_association

    def automatic(**kwargs):
        result = original(**kwargs)
        result["association"]["capabilities"]["RELEASE_READINESS_REVIEW"]["enabled"] = True
        return result

    native.get_association = automatic
    with pytest.raises(IntegrationAccessDenied, match="native_automation_unverified"):
        await adapter.reconcile("tenant-example", "product", "alpha", [selected], "space-example", "operation-example", native)


@pytest.mark.asyncio
async def test_durable_setup_reports_missing_registration_and_resumes_after_consent(tmp_path):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from l5_services.adapters.devops.spaces import SpaceSelection
    from l5_services.devops.lifecycle import SpaceLifecycle
    from l5_services.models.base import Base
    from l5_services.models.devops_space import DevopsSpaceIntent

    _, selected, native, adapter = setup()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'registration.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    scope = SpaceSelection("tenant-example", "product", "alpha", "binding-example", "123456789012", "us-east-1")
    lifecycle = SpaceLifecycle(sessions, None)
    try:
        await lifecycle.prepare_create(scope, "operation-example", 1)
        async with sessions() as session:
            receipt = await session.get(DevopsSpaceIntent, (scope.tenant_id, "operation-example"))
            receipt.state = "succeeded"
            receipt.native_space_id = "space-example"
            await session.commit()
        with pytest.raises(IntegrationAccessDenied, match="native_registration_requires_consent"):
            await lifecycle.verify_integration_associations(scope, "operation-example", [selected], adapter, native)
        async with sessions() as session:
            receipt = await session.get(DevopsSpaceIntent, (scope.tenant_id, "operation-example"))
            assert receipt.integration_observation["status"] == "blocked"
            assert receipt.integration_observation["reason"] == "native_registration_requires_consent"
        assert native.association_creates == 0
        native.registrations = Native(selected, registered=True).registrations
        resumed = await SpaceLifecycle(sessions, None).verify_integration_associations(
            scope, "operation-example", [selected], adapter, native)
        assert resumed["status"] == "ready" and native.association_creates == 1
        await lifecycle.verify_integration_associations(scope, "operation-example", [selected], adapter, native)
        assert native.association_creates == 1
        native.registrations = []
        with pytest.raises(IntegrationAccessDenied, match="native_registration_requires_consent"):
            await lifecycle.verify_integration_associations(scope, "operation-example", [selected], adapter, native)
        native.registrations = Native(selected, registered=True).registrations
        native.registrations[0]["serviceId"] = "replacement-service"
        native.associations[0]["serviceId"] = "replacement-service"
        with pytest.raises(IntegrationAccessDenied, match="native_association_changed"):
            await lifecycle.verify_integration_associations(scope, "operation-example", [selected], adapter, native)
        async with sessions() as session:
            receipt = await session.get(DevopsSpaceIntent, (scope.tenant_id, "operation-example"))
            assert receipt.integration_observation["associations"] == resumed["associations"]
        assert native.association_creates == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_association_reconciles_after_lost_ack_without_duplicate():
    _, selected, native, adapter = setup(registered=True)
    native.lose_association_ack = True
    with pytest.raises(IntegrationAccessDenied, match="association_outcome_unknown"):
        await adapter.reconcile("tenant-example", "product", "alpha", [selected],
                                "space-example", "operation-example", native)
    receipts = await adapter.reconcile("tenant-example", "product", "alpha", [selected],
                                       "space-example", "operation-example", native, reconcile_only=True)
    assert receipts[0]["status"] == "ready"
    assert receipts[0]["registration_id"] == "native-service-example"
    assert receipts[0]["registration_id"] != selected["connection_ref"]["record_id"]
    assert len(native.registrations) == native.association_creates == 1
    assert NativeIntegrationAssociation(native).cleanup(receipts[0]) == "preserved"
    assert native.disassociations == 0


@pytest.mark.asyncio
async def test_association_denies_revoked_source_before_native_effect():
    fixtures, selected, native, adapter = setup(registered=True)
    await adapter.reconcile("tenant-example", "product", "alpha", [selected],
                            "space-example", "operation-example", native)
    fixtures["alpha"][1]["items"][0]["resources"] = []
    with pytest.raises(IntegrationAccessDenied, match="repository_not_authorized"):
        await adapter.reconcile("tenant-example", "product", "alpha", [selected],
                                "space-example", "operation-example", native, reconcile_only=True)
    with pytest.raises(IntegrationAccessDenied):
        await adapter.read("tenant-example", "product", "alpha", [selected])
    assert native.association_creates == 1


@pytest.mark.asyncio
async def test_cleanup_preserves_shared_registration_and_adopted_association():
    _, selected, native, adapter = setup(registered=True)
    first = (await adapter.reconcile("tenant-example", "product", "alpha", [selected],
                                     "space-example", "operation-example", native))[0]
    assert first["registration_ownership"] == "shared"
    assert first["association_ownership"] == "adp_created"
    adopted = (await adapter.reconcile("tenant-example", "product", "alpha", [selected],
                                       "space-example", "operation-example", native))[0]
    assert adopted["association_ownership"] == "shared"
    assert NativeIntegrationAssociation(native).cleanup(adopted) == "preserved"
    assert len(native.associations) == 1 and len(native.registrations) == 1
    assert NativeIntegrationAssociation(native).cleanup(first) == "association_removed_registration_preserved"
    assert native.associations == [] and len(native.registrations) == 1


@pytest.mark.asyncio
async def test_native_registration_requires_consent_even_when_creation_requested():
    _, selected, native, adapter = setup()
    with pytest.raises(IntegrationAccessDenied, match="native_registration_requires_consent"):
        await adapter.reconcile("tenant-example", "product", "alpha", [selected],
                                "space-example", "operation-example", native, allow_registration_create=True)
    assert native.registrations == native.associations == []
    cloudwatch = setup("cloudwatch", registered=True)[1]
    with pytest.raises(IntegrationAccessDenied, match="unsupported_native_registration_kind"):
        NativeIntegrationAssociation(native).reconcile("space-example", cloudwatch, "operation-example")


@pytest.mark.asyncio
async def test_provider_revocation_denies_read_before_reassociation():
    _, selected, native, adapter = setup(registered=True)
    await adapter.reconcile("tenant-example", "product", "alpha", [selected],
                            "space-example", "operation-example", native)

    async def revoked_repository(*args):
        return {"repository_id": "901", "revision": "commit-alpha", "content_readable": False}

    adapter.github.read_repository = revoked_repository
    with pytest.raises(IntegrationAccessDenied, match="repository_readback_mismatch"):
        await adapter.reconcile("tenant-example", "product", "alpha", [selected],
                                "space-example", "operation-example", native, reconcile_only=True)
    assert native.association_creates == 1


@pytest.mark.asyncio
async def test_shared_native_registration_is_reused_across_project_scopes():
    _, selected, native, adapter = setup(registered=True)
    first = (await adapter.reconcile("tenant-example", "product", "alpha", [selected],
                                     "space-alpha", "operation-alpha", native))[0]
    second_selection = deepcopy(selected)
    second_selection["connection_ref"]["project_id"] = "beta"
    for reference in [*second_selection["repository_refs"], *second_selection["workflow_refs"]]:
        reference["project_id"] = "beta"
    repository = deepcopy((await adapter.read("tenant-example", "product", "alpha", [selected]))[0]["repositories"][0])
    repository["source_ref"]["project_id"] = "beta"
    second = NativeIntegrationAssociation(native).reconcile("space-beta", second_selection, "operation-beta", repository=repository)
    assert first["registration_id"] == second["registration_id"] == "native-service-example"
    assert first["configuration"] == second["configuration"]
    assert first["source_ref"] != second["source_ref"] and first["space_id"] != second["space_id"]
    assert len(native.registrations) == 1 and native.association_creates == 2
    NativeIntegrationAssociation(native).cleanup(second)
    assert len(native.registrations) == len(native.associations) == 1
    assert native.associations[0]["agentSpaceId"] == "space-alpha"


def test_native_sdk_requires_existing_github_service_and_aws_uses_a_different_association():
    model = Session().get_service_model("devops-agent")
    register = model.operation_model("RegisterService").input_shape.members["service"]
    assert "github" not in register.metadata["enum"]
    assert "cloudwatch" not in register.metadata["enum"]
    supported = model.operation_model("ListServices").input_shape.members["filterServiceType"].metadata["enum"]
    assert "github" in supported and "cloudwatch" not in supported
    configuration = model.operation_model("AssociateService").input_shape.members["configuration"]
    assert {"github", "aws"} <= set(configuration.members)
    assert "connection_ref" not in configuration.members
