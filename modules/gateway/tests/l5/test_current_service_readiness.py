"""Project current service readiness from owned SQL records without provider work."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from l5_services.configuration_population import configuration_options
from l5_services.models.base import Base as L5Base
from l5_services.models.binding import Binding
from l5_services.models.configuration import Configuration, ConfigurationVersion
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src import l5_integration, l5_readiness
from src.products.models import ProductChange, ProductOperation, ProductSourceBinding
from src.shared.models.base import Base as GatewayBase
from src.shared.models.vault import CredentialServiceDelegation, UserCredential

TENANT = "example-tenant"
PRODUCT = "example-product"
PROJECT = "example-project"
OWNER = "example-owner"
CONNECTION = {
    "tenant_id": TENANT,
    "project_id": PROJECT,
    "source_connection_id": "example-credential",
    "record_type": "connection",
    "record_id": "example-credential",
    "record_revision": "example-version",
}
DOCUMENT = {
    "tenant_id": TENANT,
    "project_id": PROJECT,
    "source_connection_id": "example-document-source",
    "record_type": "document",
    "record_id": "example-document",
    "record_revision": "example-revision",
}
SCOPE = {"tenant_id": TENANT, "product_id": PRODUCT, "project_id": PROJECT, "relationship_revision": "1", "owner_id": OWNER, "visibility": "only_me"}


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["current", "missing", "foreign_configuration", "other_actor", "failed", "other_change", "later_revision"])
async def test_profile_revision_requires_exact_handoff_provenance(readiness, boundary):
    configuration, version = readiness.configuration, readiness.version
    reference = {
        **version.settings["profile_ref"],
        "record_type": "l5_configuration",
        "record_id": configuration.configuration_id,
        "record_revision": str(version.revision),
    }
    operation = ProductOperation(
        tenant_id=TENANT,
        id="handoff-operation",
        product_id=PRODUCT,
        project_id=PROJECT,
        actor_id=OWNER,
        operation="linkL5Configuration",
        key_hash="c" * 64,
        request_hash="d" * 64,
        state="succeeded",
        request={
            "product_id": PRODUCT,
            "project_id": PROJECT,
            "expected_revision": 2,
            "configuration_ref": reference,
            "capabilities": version.settings["capabilities"],
        },
        receipt={"revision": 3},
        expires_at=datetime.now(UTC) - timedelta(days=1),
    )
    change = ProductChange(
        tenant_id=TENANT,
        product_id=PRODUCT,
        project_id=PROJECT,
        revision=3,
        actor_id=OWNER,
        operation_id=operation.id,
        changed_paths=["/project/configuration/selected_capabilities"],
        snapshot={},
    )
    if boundary == "foreign_configuration":
        operation.request = {**operation.request, "configuration_ref": {**reference, "record_id": "another-config"}}
    elif boundary == "other_actor":
        operation.actor_id = change.actor_id = "another-owner"
    elif boundary == "failed":
        operation.state = "failed"
    elif boundary == "other_change":
        change.changed_paths = ["/project/configuration/governance"]
    if boundary != "missing":
        readiness.db.add_all([operation, change])
        await readiness.db.commit()
    current = 4 if boundary == "later_revision" else 3
    assert await l5_readiness.current_profile_revision(readiness.db, configuration, version, {"revision": current}) is (boundary == "current")


@pytest.fixture
async def readiness(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(GatewayBase.metadata.create_all)
        await connection.run_sync(L5Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as db:
        configuration = Configuration(
            tenant_id=TENANT,
            product_id=PRODUCT,
            project_id=PROJECT,
            service_id="security",
            configuration_id="example-config",
            owner_id=OWNER,
            relationship_revision="1",
            current_revision=2,
            handoff_state="linked",
        )
        settings = {
            "enabled": True,
            "visibility": "only_me",
            "purpose": "Review",
            "classification": "internal",
            "connection_ref": CONNECTION,
            "profile_ref": {"record_revision": "2"},
            "region": "us-east-1",
            "limits": {
                "deadline_seconds": 3600,
                "max_provider_attempts": 1,
                "max_polls": 30,
                "concurrency": 1,
                "max_cost": "1.00",
                "currency": "USD",
                "policy_ref": {**CONNECTION, "record_type": "policy", "record_id": "example-budget", "record_revision": "1"},
            },
            "capabilities": ["threat_model"],
            "source_refs": [DOCUMENT],
            "triggers": [
                {"kind": "manual", "enabled": True, "capability": "threat_model", "binding_id": "example-binding", "source_refs": [DOCUMENT]}
            ],
        }
        version = ConfigurationVersion(
            tenant_id=TENANT,
            configuration_id=configuration.configuration_id,
            revision=2,
            settings=settings,
            inherited_fields=[],
            gaps=["execution_delegation"],
            state="needs_input",
            hash="a" * 64,
        )
        binding = Binding(
            tenant_id=TENANT,
            product_id=PRODUCT,
            project_id=PROJECT,
            service_id="security",
            binding_id="example-binding",
            configuration_id=configuration.configuration_id,
            revision=1,
            generation=2,
            state="ready",
            ownership="managed",
            scope=SCOPE,
            connection_ref=CONNECTION,
            connection_key="b" * 64,
            provider_identity={
                "account_id": "000000000000",
                "role_arn": "arn:aws:iam::000000000000:role/Example",
                "region": "us-east-1",
            },
            native_resource_ref={**CONNECTION, "record_type": "security_space", "record_id": "example-space"},
            readiness=[
                {
                    "kind": "code_review_source_registration",
                    "observed_at": datetime.now(UTC).isoformat(),
                    "registration": {"service_role": "arn:aws:iam::000000000000:role/Example", "bucket": "example-security-source"},
                },
                {
                    "kind": "threat_model",
                    "observed_at": datetime.now(UTC).isoformat(),
                    "native_model_ref": {**CONNECTION, "record_type": "threat_model", "record_id": "example-model"},
                },
            ],
            gaps=[],
        )
        grant = CredentialServiceDelegation(
            id="example-grant",
            tenant_id=TENANT,
            product_id=PRODUCT,
            project_id=PROJECT,
            service_id="l5-worker",
            created_by=OWNER,
            credential_id="example-credential",
            operations=["product:read", "source:pin", "source:read", "credential:session"],
            capabilities=["threat_model"],
            sources=[{**CONNECTION, "record_type": "credential"}, DOCUMENT],
            visibility="restricted",
            revision=1,
            binding_generation=2,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        credential = UserCredential(
            id="example-credential",
            org_id=TENANT,
            user_id=OWNER,
            label="Example role",
            secret_arn="example-private-ref",
            service="aws",
            credential_type="aws_role",
            aws_verified_version_id="example-version",
            scopes={"account_id": "000000000000", "role_arn": "arn:aws:iam::000000000000:role/Example"},
        )
        db.add_all([configuration, version, binding, grant, credential])
        await db.commit()
        actor = SimpleNamespace(org_id=TENANT, user_id=OWNER, account_type="human")
        publisher = AsyncMock(side_effect=AssertionError("readiness must not dispatch work"))
        backend = l5_integration.GatewayConfigurationBackend(db, actor, publish=publisher)
        profile = {"revision": 2, "configuration": {"selected_capabilities": [{"capability": "threat_model", "ownership_mode": "managed"}]}}
        backend.current = AsyncMock(return_value=(configuration, version))
        backend.profile = AsyncMock(return_value=profile)
        monkeypatch.setattr(l5_readiness, "verified_connection_evidence", lambda selected: selected.aws_verified_version_id)
        yield SimpleNamespace(
            db=db,
            backend=backend,
            profile=profile,
            grant=grant,
            binding=binding,
            credential=credential,
            version=version,
            configuration=configuration,
            publisher=publisher,
        )
    await engine.dispose()


@pytest.mark.asyncio
async def test_current_binding_and_delegation_clear_draft_time_gap(readiness):
    checked = await readiness.backend.check(PRODUCT, PROJECT, "security")
    assert checked == {"ready": True, "gaps": [], "provider_contacted": False}
    assert readiness.version.state == "needs_input" and readiness.version.gaps == ["execution_delegation"]
    readiness.publisher.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["missing_registration", "missing_model", "stale_registration", "stale_model", "foreign_model"])
async def test_threat_model_readiness_requires_current_scoped_native_readbacks(readiness, change):
    recorded = [dict(item) for item in readiness.binding.readiness]
    if change == "missing_registration":
        recorded = recorded[1:]
    elif change == "missing_model":
        recorded = recorded[:1]
    elif change == "stale_registration":
        recorded[0]["observed_at"] = (datetime.now(UTC) - timedelta(minutes=16)).isoformat()
    elif change == "stale_model":
        recorded[1]["observed_at"] = (datetime.now(UTC) - timedelta(minutes=16)).isoformat()
    else:
        recorded[1]["native_model_ref"] = {**recorded[1]["native_model_ref"], "project_id": "foreign-project"}
    readiness.binding.readiness = recorded
    await readiness.db.commit()

    checked = await readiness.backend.check(PRODUCT, PROJECT, "security")
    assert checked == {"ready": False, "gaps": ["native_unqualified"], "provider_contacted": False}
    readiness.publisher.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "service_id,worker_id,has_sources,required_operations",
    [
        ("security", "l5-worker", True, ("product:read", "source:pin", "source:read", "credential:session")),
        ("well_architected", "l5-well-architected-worker", True, ("product:read", "source:pin", "source:read", "credential:session")),
        ("finops", "l5-finops-worker", True, ("product:read", "source:pin", "source:read", "credential:session")),
        ("finops", "l5-finops-worker", False, ("product:read", "credential:session")),
    ],
)
async def test_readiness_requires_every_admission_operation(readiness, service_id, worker_id, has_sources, required_operations):
    configuration = readiness.backend.current.return_value[0]
    configuration.service_id = service_id
    readiness.binding.service_id = service_id
    readiness.grant.service_id = worker_id
    if not has_sources:
        readiness.version.settings["source_refs"] = []
        readiness.grant.sources = [{**CONNECTION, "record_type": "credential"}]
    readiness.grant.operations = list(required_operations)
    await readiness.db.commit()

    async def check():
        return await l5_readiness.project_readiness(readiness.db, configuration, readiness.version, service_id, readiness.profile)

    assert "execution_delegation" not in (await check())["gaps"]
    for missing in required_operations:
        readiness.grant.operations = [operation for operation in required_operations if operation != missing]
        await readiness.db.commit()
        checked = await check()
        assert not checked["ready"] and "execution_delegation" in checked["gaps"], missing
        assert checked["provider_contacted"] is False
    readiness.publisher.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change,reason",
    [
        ("revoked", "execution_delegation"),
        ("expired", "execution_delegation"),
        ("foreign_service", "execution_delegation"),
        ("changed_binding", "execution_delegation"),
        ("changed_connection", "connection_revision_unavailable"),
        ("capability_removed", "capability_not_selected"),
        ("profile_changed", "profile_revision_stale"),
    ],
)
async def test_current_scope_change_refuses_readiness(readiness, change, reason):
    if change == "revoked":
        readiness.grant.revoked_at = datetime.now(UTC)
    elif change == "expired":
        readiness.grant.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    elif change == "foreign_service":
        readiness.grant.service_id = "l5-devops-worker"
    elif change == "changed_binding":
        readiness.binding.generation = 3
    elif change == "changed_connection":
        readiness.credential.aws_verified_version_id = "changed-version"
    elif change == "capability_removed":
        readiness.profile["configuration"]["selected_capabilities"] = []
    else:
        readiness.profile["revision"] = 3
    await readiness.db.commit()
    checked = await readiness.backend.check(PRODUCT, PROJECT, "security")
    assert checked["ready"] is False and reason in checked["gaps"]
    readiness.publisher.assert_not_awaited()


def link_audit(revision, *, project=PROJECT, configuration_id="example-config"):
    operation = ProductOperation(
        tenant_id=TENANT,
        id=f"link-operation-{revision}",
        product_id=PRODUCT,
        project_id=project,
        actor_id=OWNER,
        operation="linkL5Configuration",
        key_hash=f"link-key-{revision}",
        request_hash="a" * 64,
        request={
            "product_id": PRODUCT,
            "project_id": project,
            "expected_revision": revision - 1,
            "configuration_ref": {
                "record_type": "l5_configuration",
                "record_id": configuration_id,
                "record_revision": "2",
            },
            "capabilities": ["threat_model"],
        },
        receipt={"revision": revision},
        state="succeeded",
        expires_at=datetime.now(UTC) - timedelta(days=1),
    )
    change = ProductChange(
        tenant_id=TENANT,
        id=f"link-change-{revision}",
        product_id=PRODUCT,
        project_id=project,
        revision=revision,
        actor_id=OWNER,
        operation_id=operation.id,
        changed_paths=["/project/configuration/selected_capabilities"],
        snapshot={},
    )
    return operation, change


@pytest.mark.asyncio
@pytest.mark.parametrize("service_id", ["security", "finops", "well_architected"])
async def test_only_service_links_advance_profile_without_invalidating_saved_inputs(readiness, service_id):
    readiness.db.add_all(
        [
            Configuration(
                tenant_id=TENANT,
                product_id=PRODUCT,
                project_id="sibling-project",
                service_id="security",
                configuration_id="sibling-config",
                owner_id=OWNER,
                relationship_revision="1",
                current_revision=2,
                handoff_state="linked",
            ),
            ConfigurationVersion(
                tenant_id=TENANT,
                configuration_id="sibling-config",
                revision=2,
                settings={"profile_ref": {"record_revision": "3"}, "capabilities": ["threat_model"]},
                inherited_fields=[],
                gaps=[],
                state="ready",
                hash="b" * 64,
            ),
            *link_audit(3),
            *link_audit(4, project="sibling-project", configuration_id="sibling-config"),
        ]
    )
    readiness.profile["revision"] = 4
    await readiness.db.commit()
    configuration = readiness.backend.current.return_value[0]
    before_settings = dict(readiness.version.settings)
    checked = await l5_readiness.project_readiness(readiness.db, configuration, readiness.version, service_id, readiness.profile)
    assert "profile_revision_stale" not in checked["gaps"]
    assert checked["provider_contacted"] is False
    assert readiness.version.settings == before_settings
    readiness.publisher.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid",
    [
        "missing_change",
        "missing_operation",
        "real_edit",
        "failed",
        "foreign_product",
        "foreign_tenant",
        "wrong_project",
        "other_paths",
        "missing_request",
        "wrong_expected_revision",
        "wrong_receipt",
    ],
)
async def test_service_link_history_must_prove_every_intervening_revision(readiness, invalid):
    operation, change = link_audit(3)
    readiness.profile["revision"] = 4
    readiness.db.add_all(link_audit(4))
    if invalid == "real_edit":
        operation.operation = "updateConfiguration"
    elif invalid == "failed":
        operation.state = "failed"
    elif invalid == "foreign_product":
        operation.product_id = "foreign-product"
    elif invalid == "foreign_tenant":
        operation.tenant_id = "foreign-tenant"
    elif invalid == "wrong_project":
        operation.project_id = "other-project"
    elif invalid == "other_paths":
        change.changed_paths = ["/product/purpose"]
    elif invalid == "missing_request":
        operation.request = {}
    elif invalid == "wrong_expected_revision":
        operation.request = {**operation.request, "expected_revision": 1}
    elif invalid == "wrong_receipt":
        operation.receipt = {"revision": 4}
    if invalid != "missing_change":
        readiness.db.add(change)
    if invalid != "missing_operation":
        readiness.db.add(operation)
    await readiness.db.commit()
    checked = await readiness.backend.check(PRODUCT, PROJECT, "security")
    assert checked["ready"] is False and "profile_revision_stale" in checked["gaps"]
    assert checked["provider_contacted"] is False
    readiness.publisher.assert_not_awaited()


@pytest.mark.asyncio
async def test_repository_revision_must_match_current_product_observation(readiness):
    readiness.binding.readiness = []
    current_sha, stale_sha = "a" * 40, "b" * 40
    repository = {**DOCUMENT, "record_type": "repository", "record_id": "example-repository", "record_revision": stale_sha}
    settings = readiness.version.settings
    settings["capabilities"] = ["code_review"]
    settings["source_refs"] = [repository]
    settings["triggers"] = [
        {"kind": "manual", "enabled": True, "capability": "code_review", "binding_id": "example-binding", "source_refs": [repository]}
    ]
    readiness.grant.capabilities = ["code_review"]
    readiness.grant.sources = [{**CONNECTION, "record_type": "credential"}, repository]
    readiness.profile["configuration"]["selected_capabilities"] = [{"capability": "code_review", "ownership_mode": "managed"}]
    source = ProductSourceBinding(
        tenant_id=TENANT,
        id="example-source-binding",
        project_id=PROJECT,
        kind="repository",
        provider="github",
        installation_id="example-document-source",
        namespace="example/repository",
        native_id="example-repository",
        status="healthy",
        generation=1,
        revision=1,
        desired={"source_revision": current_sha},
        observed={"source_revision": current_sha},
    )
    readiness.db.add(source)
    await readiness.db.commit()

    stale = await readiness.backend.check(PRODUCT, PROJECT, "security")
    assert not stale["ready"] and "source_revision_stale" in stale["gaps"]
    source.observed = {"source_revision": stale_sha}
    await readiness.db.commit()
    refreshed = await readiness.backend.check(PRODUCT, PROJECT, "security")
    assert "source_revision_stale" not in refreshed["gaps"]
    assert not refreshed["ready"] and "native_unqualified" in refreshed["gaps"]
    readiness.publisher.assert_not_awaited()


@pytest.mark.asyncio
async def test_well_architected_mapping_and_native_readback_are_required(readiness):
    readiness.backend.current.return_value[0].service_id = "well_architected"
    readiness.binding.service_id = "well_architected"
    readiness.grant.service_id = "l5-well-architected-worker"
    readiness.grant.capabilities = ["architecture_review"]
    settings = readiness.version.settings
    settings["capabilities"] = ["architecture_review"]
    settings["well_architected"] = {
        "execution_role_ref": {"record_id": "example-role"},
        "source_store_ref": {"record_id": "example-source-store"},
    }
    settings["triggers"] = [
        {"kind": "manual", "enabled": True, "capability": "architecture_review", "binding_id": "example-binding", "source_refs": [DOCUMENT]}
    ]
    snapshot = "c" * 64
    readiness.binding.native_resource_ref = {
        "record_type": "agent_profile",
        "record_revision": snapshot,
        "record_id": "arn:aws:wellarchitected:us-east-1:000000000000:agent-profile/example-profile",
        "tenant_id": TENANT,
        "project_id": PROJECT,
        "source_connection_id": "example-credential",
    }
    readiness.binding.provider_identity = {
        "account_id": "000000000000",
        "role_arn": "arn:aws:iam::000000000000:role/Example",
        "region": "us-east-1",
    }
    readiness.binding.readiness = [{"snapshot_hash": snapshot}]
    readiness.backend.wa_authority = AsyncMock()
    readiness.backend.wa_profile_client = object()
    readiness.backend.wa_qualify = AsyncMock()
    readiness.profile["configuration"]["selected_capabilities"] = [{"capability": "well_architected", "ownership_mode": "managed"}]
    await readiness.db.commit()

    options = configuration_options(
        {"tenant_id": TENANT, "project_id": PROJECT, "configuration": readiness.profile["configuration"]},
        {"coverage": "complete", "next_cursor": None, "items": [], "regions": []},
        "well_architected",
    )
    assert options["capabilities"] == ["architecture_review"]
    assert "catalog_mapping_unresolved" not in options["unavailable_reasons"]
    checked = await readiness.backend.check(PRODUCT, PROJECT, "well_architected")
    assert checked == {"ready": True, "gaps": [], "provider_contacted": False}
    native_ref = readiness.binding.native_resource_ref
    readiness.binding.native_resource_ref = {**native_ref, "record_id": native_ref["record_id"].replace(":agent-profile/", ":profile/")}
    await readiness.db.commit()
    assert "provider_unqualified" in (await readiness.backend.check(PRODUCT, PROJECT, "well_architected"))["gaps"]
    readiness.binding.native_resource_ref = native_ref
    await readiness.db.commit()
    readiness.profile.update({"tenant_id": TENANT, "project_id": PROJECT})
    readiness.backend.profile_inputs = AsyncMock(return_value=(None, None, {"coverage": "complete", "next_cursor": None, "items": [], "regions": []}))
    current_options = await readiness.backend.options(PRODUCT, PROJECT, "well_architected")
    assert "provider_unqualified" not in current_options["unavailable_reasons"]
    readiness.binding.readiness = [{"snapshot_hash": "d" * 64}]
    await readiness.db.commit()
    stale = await readiness.backend.check(PRODUCT, PROJECT, "well_architected")
    assert not stale["ready"] and "provider_unqualified" in stale["gaps"]
    stale_options = await readiness.backend.options(PRODUCT, PROJECT, "well_architected")
    assert "provider_unqualified" in stale_options["unavailable_reasons"]
    readiness.publisher.assert_not_awaited()


@pytest.mark.asyncio
async def test_finops_candidate_binding_never_clears_unqualified_provider_status(readiness, monkeypatch):
    readiness.backend.current.return_value[0].service_id = "finops"
    readiness.binding.service_id = "finops"
    readiness.grant.service_id = "l5-finops-worker"
    readiness.grant.capabilities = ["cost_inquiry"]
    settings = readiness.version.settings
    settings["capabilities"] = ["cost_inquiry"]
    settings["finops"] = {
        "billing_scope": {"account_ids": ["000000000000"], "view_ref": "example-view"},
        "data_role_ref": "example-data-role",
        "agent_mode": "adopt",
    }
    readiness.binding.native_resource_ref = {
        "record_type": "finops_agent",
        "record_id": "example-agent",
        "tenant_id": TENANT,
        "project_id": PROJECT,
        "source_connection_id": "example-credential",
    }
    readiness.binding.provider_identity = {
        "account_id": "000000000000",
        "role_arn": "arn:aws:iam::000000000000:role/Example",
        "region": "us-east-1",
    }
    readiness.binding.readiness = [{"status": "ready"}]
    readiness.backend.finops_authority = AsyncMock()
    readiness.profile["configuration"]["selected_capabilities"] = [{"capability": "finops", "ownership_mode": "managed"}]
    monkeypatch.setenv("L5_FINOPS_ADMISSION_ENABLED", "true")
    await readiness.db.commit()

    checked = await readiness.backend.check(PRODUCT, PROJECT, "finops")
    assert checked["ready"] is False and "native_unqualified" in checked["gaps"]
    assert "billing_scope_unverified" in checked["gaps"]
    readiness.publisher.assert_not_awaited()
