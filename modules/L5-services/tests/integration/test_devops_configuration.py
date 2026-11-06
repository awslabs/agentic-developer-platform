"""Mounted DevOps configuration access with isolated Product source rights."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from l5_services.models.configuration import Configuration, ConfigurationVersion
from src.l5_integration import GatewayConfigurationBackend
from src.products.models import ProductProjectBinding
from src.shared.models.vault import CredentialServiceDelegation, UserCredential
from src.internal.l5_credential_routes import L5WorkPermit, get_l5_work_authorizer
from src.products.models import ProductSourceBinding
from src.products.source_snapshots import store_snapshot
from tests.products.test_l5_snapshots import snapshot_fixture
from tests.products.conftest import source_member as source_member
from test_configuration import configuration_scenario as configuration_scenario


@pytest.fixture
async def devops_readiness_scenario(configuration_scenario):
    scenario = configuration_scenario
    client = scenario["client"]
    products = []
    for actor, label in zip(scenario["actors"], ("readiness-first", "readiness-second")):
        product = await scenario["prepare"](actor, label, "internal", ["devops"])
        environment = product["references"][0] | {"record_type": "environment"}
        profile = (await client.get(product["profile_path"])).json()
        selected = await client.patch(product["profile_path"] + "/configuration", json={"configuration": {
            **profile["configuration"], "advanced": {"environments": [environment]}}}, headers={
            "Idempotency-Key": f"devops_readiness_profile_{label}_0001",
            "If-Profile-Revision": str(profile["revision"])})
        assert selected.status_code == 200, selected.text
        products.append((product, environment, selected.json()["revision"]))
    unavailable = products[1][0]
    scenario["permissions"][(unavailable["actor"].org_id, unavailable["project_id"])].clear()
    return scenario, products


@pytest.mark.asyncio
async def test_configuration_readiness(devops_readiness_scenario):
    scenario, products = devops_readiness_scenario
    client = scenario["client"]
    for (product, _environment, profile_revision), capability, expected_sources, missing in (
        (products[0], "investigation", [products[0][1]], "investigation.needs_resources"),
        (products[1], "release_readiness", [], "release_readiness.needs_repository"),
    ):
        scenario["switch"](product["actor"])
        path = product["path"].replace("/security/", "/devops/")
        options = await client.get(path + "/options")
        assert options.status_code == 200, options.text
        assert options.json()["sources"] == expected_sources
        created = await client.put(path, json={"service_id": "devops", "capabilities": [capability],
            "enabled": False, "triggers": [], "devops": {
                "space_mode": "create", "accounts": [], "integrations": []}}, headers={
            "Idempotency-Key": "devops_readiness_draft_0001", "If-None-Match": "*"})
        assert created.status_code == 201, created.text
        configuration = created.json()
        assert configuration["scope"]["tenant_id"] == product["actor"].org_id
        assert configuration["scope"]["product_id"] == product["product_id"]
        assert configuration["scope"]["project_id"] == product["project_id"]
        assert configuration["settings"]["profile_ref"]["record_revision"] == str(profile_revision)
        assert configuration["settings"]["source_refs"] == expected_sources
        assert configuration["state"] == "disabled" and configuration["handoff_state"] == "linked"
        assert (await client.get(path)).json() == configuration
        checked = await client.post(path + "/check")
        assert checked.status_code == 200, checked.text
        expected = {"ready": False, "gaps": configuration["gaps"],
                    "provider_contacted": False, "integrations": []}
        if capability == "investigation":
            expected.update(
                gaps=["configuration_disabled_or_unlinked", *configuration["gaps"]],
                scope=configuration["scope"], configuration_id=configuration["configuration_id"],
                configuration_revision=configuration["revision"], configuration_hash=configuration["hash"],
                capability="investigation",
            )
        else:
            # Sibling capabilities retain their own readiness contract.
            assert "capability" not in checked.json() and "configuration_hash" not in checked.json()
        assert checked.json() == expected
        assert missing in configuration["gaps"]
        assert all(gap.startswith(capability + ".") for gap in configuration["gaps"])
    assert scenario["provider_attempts"] == []


@pytest.fixture
async def devops_refresh_scenario(configuration_scenario):
    scenario = configuration_scenario
    client = scenario["client"]
    product = await scenario["prepare"](scenario["actors"][0], "devops-refresh", "internal", ["devops"])
    path = product["path"].replace("/security/", "/devops/")
    original_environment = product["references"][0] | {"record_type": "environment"}
    profile = (await client.get(product["profile_path"])).json()
    updated = await client.patch(product["profile_path"] + "/configuration", json={"configuration": {
        **profile["configuration"], "advanced": {"environments": [original_environment]}}}, headers={
        "Idempotency-Key": "devops_refresh_initial_profile_0001",
        "If-Profile-Revision": str(profile["revision"])})
    assert updated.status_code == 200, updated.text
    override = {"field": "region", "value": "example-region-one", "reason": "project-selected region"}
    created = await client.put(path, json={"service_id": "devops", "capabilities": ["investigation"],
        "enabled": False, "triggers": [], "region": override["value"], "overrides": [override],
        "devops": {"space_mode": "create", "accounts": [], "integrations": []}}, headers={
        "Idempotency-Key": "devops_refresh_initial_draft_0001", "If-None-Match": "*"})
    assert created.status_code == 201, created.text
    additional_environment = product["references"][1] | {
        "record_type": "environment", "record_id": "devops-refresh-added-environment"}
    scenario["permissions"][(product["actor"].org_id, product["project_id"])].add((
        additional_environment["source_connection_id"], additional_environment["record_id"]))
    profile = (await client.get(product["profile_path"])).json()
    changed = await client.patch(product["profile_path"] + "/configuration", json={"configuration": {
        **profile["configuration"],
        "governance": {**profile["configuration"]["governance"], "data_classification": "restricted"},
        "advanced": {"environments": [original_environment, additional_environment]}}}, headers={
        "Idempotency-Key": "devops_refresh_changed_profile_0001",
        "If-Profile-Revision": str(profile["revision"])})
    assert changed.status_code == 200, changed.text
    return scenario, product, path, created, changed.json(), original_environment, additional_environment, override


@pytest.mark.asyncio
async def test_refresh_conflict(devops_refresh_scenario):
    scenario, product, path, created, changed, original_environment, additional_environment, override = devops_refresh_scenario
    client = scenario["client"]
    initial = created.json()
    next_ref = initial["settings"]["profile_ref"] | {"record_revision": str(changed["revision"])}
    assert (await client.get(path)).json() == initial
    stale_refresh = await client.post(path + "/refresh", json={"profile_ref": next_ref}, headers={
        "Idempotency-Key": "devops_refresh_stale_0001", "If-Match": '"stale"'})
    assert stale_refresh.status_code == 412
    assert (await client.get(path)).json() == initial
    refreshed = await client.post(path + "/refresh", json={"profile_ref": next_ref}, headers={
        "Idempotency-Key": "devops_refresh_current_0001", "If-Match": created.headers["etag"]})
    assert refreshed.status_code == 200, refreshed.text
    current = refreshed.json()
    assert current["revision"] == 2 and refreshed.headers["etag"] != created.headers["etag"]
    assert current["settings"]["profile_ref"] == next_ref
    assert current["settings"]["classification"] == "restricted"
    assert current["settings"]["source_refs"] == [original_environment]
    assert current["settings"]["overrides"] == [override]
    assert current["settings"]["region"] == override["value"]
    assert "source_scope_changed" in current["gaps"]
    assert (await client.get(path)).json() == current
    assert (await client.get(path + "/versions/1")).json() == initial
    profile_read = await client.get(product["profile_path"] + f'/revisions/{changed["revision"]}')
    assert profile_read.status_code == 200
    assert profile_read.json()["configuration"]["advanced"]["environments"] == [
        original_environment, additional_environment]
    widened_sources = [original_environment, additional_environment]
    widening = {"source_refs": widened_sources, "overrides": [override, {
        "field": "source_refs", "value": widened_sources, "reason": "project selects the added environment"}]}
    stale_write = await client.put(path, json=widening, headers={
        "Idempotency-Key": "devops_refresh_stale_write_0001", "If-Match": created.headers["etag"]})
    assert stale_write.status_code == 412
    assert (await client.get(path)).json() == current
    accepted = await client.put(path, json=widening, headers={
        "Idempotency-Key": "devops_refresh_explicit_widening_0001", "If-Match": refreshed.headers["etag"]})
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["revision"] == 3
    assert accepted.json()["settings"]["source_refs"] == widened_sources
    assert accepted.json()["settings"]["profile_ref"] == next_ref
    assert (await client.get(path)).json() == accepted.json()
    assert (await client.get(path + "/versions/2")).json() == current
    assert scenario["provider_attempts"] == []


@pytest.mark.asyncio
async def test_nested_source_access(configuration_scenario, db_session):
    scenario = configuration_scenario
    actor = scenario["actors"][0]
    product = await scenario["prepare"](actor, "nested", "internal", ["threat_model"])
    reference = product["references"][0]
    settings = {"profile_ref": {"tenant_id": actor.org_id, "project_id": product["project_id"],
                                "source_connection_id": "adp.profile", "record_type": "product",
                                "record_id": product["product_id"],
                                "record_revision": str(product["profile_revision"])},
                "source_refs": [], "visibility": "only_me", "enabled": False, "capabilities": ["investigation"],
                "triggers": [], "service_id": "devops", "devops": {
                    "accounts": [{"environments": [{"resource_refs": [reference]}]}]}}
    backend = GatewayConfigurationBackend(db_session, actor)
    await backend.authorize_settings(settings, product["project_id"])
    foreign = reference | {"project_id": "project-foreign"}
    with pytest.raises(HTTPException) as denied:
        await backend.authorize_settings({**settings, "devops": {
            "accounts": [{"environments": [{"resource_refs": [foreign]}]}]}}, product["project_id"])
    assert denied.value.status_code == 404
    path = product["path"].replace("/security/", "/devops/")
    forbidden = await scenario["client"].put(path, json={**settings, "devops": {
        "accounts": [{"environments": [{"resource_refs": [foreign]}]}]}}, headers={
        "Idempotency-Key": "devops_foreign_source_0001", "If-None-Match": "*"})
    assert forbidden.status_code == 404
    relationship = await db_session.scalar(select(ProductProjectBinding).where(
        ProductProjectBinding.tenant_id == actor.org_id,
        ProductProjectBinding.product_id == product["product_id"],
        ProductProjectBinding.project_id == product["project_id"],
        ProductProjectBinding.valid_to.is_(None),
    ))
    identifier = "example-configuration-a"
    db_session.add_all([
        Configuration(tenant_id=actor.org_id, configuration_id=identifier, product_id=product["product_id"],
                      project_id=product["project_id"], service_id="devops",
                      relationship_revision=str(relationship.relationship_revision), owner_id=actor.user_id,
                      current_revision=1, handoff_state="linked"),
        ConfigurationVersion(tenant_id=actor.org_id, configuration_id=identifier, revision=1, settings=settings,
                             inherited_fields=[], gaps=["investigation.needs_space"], state="disabled", hash="a" * 64),
    ])
    await db_session.commit()
    response = await scenario["client"].get(path)
    assert response.status_code == 200, response.text
    assert response.json()["settings"]["devops"]["accounts"][0]["environments"][0]["resource_refs"] == [reference]
    scenario["permissions"][(actor.org_id, product["project_id"])].clear()
    revoked = await scenario["client"].get(path)
    assert revoked.status_code == 404
    assert scenario["provider_attempts"] == []


@pytest.mark.asyncio
async def test_delegated_service_configuration_read(configuration_scenario, db_session):
    scenario = configuration_scenario
    actor = scenario["actors"][0]
    product = await scenario["prepare"](actor, "service", "internal", ["threat_model"])
    relationship = await db_session.scalar(select(ProductProjectBinding).where(
        ProductProjectBinding.tenant_id == actor.org_id,
        ProductProjectBinding.product_id == product["product_id"],
        ProductProjectBinding.project_id == product["project_id"],
        ProductProjectBinding.valid_to.is_(None),
    ))
    credential = UserCredential(org_id=actor.org_id, user_id=actor.user_id, service="document",
                                credential_type="api_key", label="synthetic-service-read",
                                secret_arn="synthetic-secret-reference")
    db_session.add(credential)
    await db_session.flush()
    grant = CredentialServiceDelegation(
        tenant_id=actor.org_id, credential_id=credential.id, service_id="registered-l5-worker",
        product_id=product["product_id"], project_id=product["project_id"], created_by=actor.user_id,
        operations=["product:read"], sources=[], capabilities=[], visibility="restricted",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    settings = {"profile_ref": {"tenant_id": actor.org_id, "project_id": product["project_id"],
                                "source_connection_id": "adp.profile", "record_type": "product",
                                "record_id": product["product_id"],
                                "record_revision": str(product["profile_revision"])},
                "source_refs": [], "visibility": "only_me", "enabled": False, "capabilities": ["investigation"],
                "triggers": [], "service_id": "devops", "devops": {"accounts": [], "integrations": []}}
    identifier = "example-service-read"
    db_session.add_all([
        grant,
        Configuration(tenant_id=actor.org_id, configuration_id=identifier, product_id=product["product_id"],
                      project_id=product["project_id"], service_id="devops",
                      relationship_revision=str(relationship.relationship_revision), owner_id=actor.user_id,
                      current_revision=1, handoff_state="linked"),
        ConfigurationVersion(tenant_id=actor.org_id, configuration_id=identifier, revision=1, settings=settings,
                             inherited_fields=[], gaps=["investigation.needs_space"], state="disabled", hash="b" * 64),
    ])
    await db_session.commit()
    service = actor.model_copy(update={"user_id": "iam-agent:registered-l5-worker",
                                       "agent_registry_id": "registered-l5-worker", "org_id": "platform-service",
                                       "account_type": "service", "auth_source": "iam", "scope": "internal"})
    scenario["switch"](service)
    path = product["path"].replace("/security/", "/devops/")
    assert (await scenario["client"].get(path)).status_code == 404
    headers = {"X-L5-Delegation-ID": grant.id}
    authorized = await scenario["client"].get(path, headers=headers)
    assert authorized.status_code == 200, authorized.text
    assert authorized.json()["settings"] == settings
    assert (await scenario["client"].get(path, headers={"X-L5-Delegation-ID": "foreign"})).status_code == 404
    assert (await scenario["client"].put(path, headers={**headers, "If-None-Match": "*",
        "Idempotency-Key": "service_cannot_write_config_0001"}, json=settings)).status_code == 404
    grant.revoked_at = datetime.now(UTC)
    await db_session.commit()
    assert (await scenario["client"].get(path, headers=headers)).status_code == 404
    assert scenario["provider_attempts"] == []


@pytest.mark.asyncio
async def test_worker_stale_attempt_and_revoked_sources_fail_before_provider(
    test_client, db_session, app, source_member, monkeypatch,
):
    monkeypatch.setenv("BG_PRODUCT_PROFILE_ENABLED", "true")
    grant, request_body, capture, path, body, clock = await snapshot_fixture(
        test_client, db_session, app, source_member, monkeypatch,
    )
    receipt, _ = await store_snapshot(db_session, grant, request_body, capture, "devops-worker-source-read")
    await db_session.commit()
    read_path = f"{path}/{receipt.snapshot_id}/read"
    permit = L5WorkPermit(
        tenant_id=grant.tenant_id, product_id=grant.product_id, project_id=grant.project_id,
        source_connection_id=grant.credential_id, operation_id=grant.operation_id,
        attempt_id=grant.attempt_id, lease_fence=grant.lease_fence,
        binding_generation=grant.binding_generation, expires_at=clock["now"] + timedelta(hours=1),
        admitted_source_snapshot_ids=frozenset({receipt.snapshot_id}),
    )
    work_checks = []

    async def verify_work(current_grant, selection):
        work_checks.append((current_grant.id, selection.attempt_id))
        return permit

    app.dependency_overrides[get_l5_work_authorizer] = lambda: verify_work
    stale = await test_client.post(read_path, json={**body, "attempt_id": "stale-attempt"})
    assert stale.status_code == 409
    assert work_checks == []
    foreign = await test_client.post(read_path.replace(grant.project_id, "foreign-project"), json=body)
    assert foreign.status_code == 404
    assert work_checks == []
    await db_session.execute(update(ProductSourceBinding).where(
        ProductSourceBinding.tenant_id == grant.tenant_id,
        ProductSourceBinding.project_id == grant.project_id,
        ProductSourceBinding.native_id == "document-a",
    ).values(status="revoked"))
    await db_session.commit()
    revoked_source = await test_client.post(read_path, json=body)
    assert revoked_source.status_code == 404
    assert "original document bytes" not in revoked_source.text
    await db_session.execute(update(CredentialServiceDelegation).where(
        CredentialServiceDelegation.id == grant.id,
    ).values(revoked_at=datetime.now(UTC)))
    await db_session.commit()
    checks_before_revocation = len(work_checks)
    revoked_grant = await test_client.post(read_path, json=body)
    assert revoked_grant.status_code == 404
    assert len(work_checks) == checks_before_revocation
    assert "original document bytes" not in revoked_grant.text


@pytest.fixture
async def devops_denial_scenario(test_client, db_session, app, source_member, monkeypatch):
    monkeypatch.setenv("BG_PRODUCT_PROFILE_ENABLED", "true")
    grant, request_body, capture, path, body, clock = await snapshot_fixture(
        test_client, db_session, app, source_member, monkeypatch,
    )
    receipt, _ = await store_snapshot(db_session, grant, request_body, capture, "devops-denial-snapshot")
    await db_session.commit()
    read_path = f"{path}/{receipt.snapshot_id}/read"
    work_checks = []
    permit = L5WorkPermit(
        tenant_id=grant.tenant_id, product_id=grant.product_id, project_id=grant.project_id,
        source_connection_id=grant.credential_id, operation_id=grant.operation_id,
        attempt_id=grant.attempt_id, lease_fence=grant.lease_fence,
        binding_generation=grant.binding_generation, expires_at=clock["now"] + timedelta(hours=1),
        admitted_source_snapshot_ids=frozenset({receipt.snapshot_id}),
    )

    async def verify_work(current_grant, selection):
        work_checks.append((current_grant.id, selection.attempt_id))
        return permit

    app.dependency_overrides[get_l5_work_authorizer] = lambda: verify_work
    return grant, read_path, receipt.snapshot_id, body, work_checks


@pytest.mark.asyncio
async def test_delegation_denial(devops_denial_scenario, test_client, db_session, monkeypatch):
    grant, read_path, snapshot_id, body, work_checks = devops_denial_scenario
    original_get = AsyncSession.get

    async def deny_credential_lookup(session, model, identifier, *args, **kwargs):
        if model is UserCredential:
            raise AssertionError("denied source read reached credential storage")
        return await original_get(session, model, identifier, *args, **kwargs)

    async def deny_provider_request(transport, request):
        raise AssertionError("denied source read reached an external provider")

    monkeypatch.setattr(AsyncSession, "get", deny_credential_lookup)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", deny_provider_request)
    authorized = await test_client.post(read_path, json=body)
    assert authorized.status_code == 200, authorized.text
    assert authorized.json()["source_ref"] == grant.sources[0]
    work_checks.clear()
    stale = await test_client.post(read_path, json={**body, "attempt_id": "old-attempt"})
    assert stale.status_code == 409 and work_checks == []
    foreign_project = await test_client.post(read_path.replace(grant.project_id, "foreign-project"), json=body)
    assert foreign_project.status_code == 404 and work_checks == []
    foreign_source = await test_client.post(read_path.replace(snapshot_id, "foreign-source-id"), json=body)
    assert foreign_source.status_code == 404 and work_checks == []
    await db_session.execute(update(ProductSourceBinding).where(
        ProductSourceBinding.tenant_id == grant.tenant_id,
        ProductSourceBinding.project_id == grant.project_id,
        ProductSourceBinding.native_id == "document-a",
    ).values(status="revoked"))
    await db_session.commit()
    revoked_source = await test_client.post(read_path, json=body)
    assert revoked_source.status_code == 404
    checks_after_source = len(work_checks)
    await db_session.execute(update(CredentialServiceDelegation).where(
        CredentialServiceDelegation.id == grant.id,
    ).values(revoked_at=datetime.now(UTC)))
    await db_session.commit()
    revoked_grant = await test_client.post(read_path, json=body)
    assert revoked_grant.status_code == 404 and len(work_checks) == checks_after_source
    assert "original document bytes" not in revoked_grant.text


@pytest.fixture
async def devops_portability_scenario(configuration_scenario, monkeypatch):
    scenario = configuration_scenario
    first_actor, second_actor = scenario["actors"]
    first = await scenario["prepare"](first_actor, "portable-first", "internal", ["devops"])
    second = await scenario["prepare"](second_actor, "portable-second", "confidential", ["devops"])

    from dataclasses import asdict
    from uuid import uuid4

    from l5_services.adapters.devops.spaces import SpaceSelection
    from l5_services.devops.resource_scope import ResourceSelection, policy_digest
    from l5_services.models.binding import Binding
    from l5_services.models.devops_space import DevopsSpaceIntent
    from src import l5_devops_access_policy, l5_integration
    from src.auth.aws_connection_authority import connection_binding

    source_connections = l5_integration.authorized_connections

    async def owned_connections(db, actor):
        credentials = (await db.scalars(select(UserCredential).where(
            UserCredential.org_id == actor.org_id, UserCredential.user_id == actor.user_id,
            UserCredential.service == "aws",
        ))).all()
        return [*await source_connections(db, actor), *[
            ({"provider": "aws", "connection_ref": {"kind": "credential", "id": row.id}}, row)
            for row in credentials]]

    monkeypatch.setattr(l5_integration, "authorized_connections", owned_connections)
    monkeypatch.setattr(l5_devops_access_policy.SecretsManagerHelper, "current_version_id", lambda *_: "revision-1")

    async def select_retained_policy(product, region, capability):
        """Select an API-produced reference backed by retained offline association evidence."""
        actor = product["actor"]
        scenario["switch"](actor)
        client = scenario["client"]
        path = product["profile_path"] + "/services/devops/configuration"
        account = "123456789012" if actor == first_actor else "210987654321"
        role = f"arn:aws:iam::{account}:role/portable-hosting"
        credential_id = str(uuid4())
        connection = {"tenant_id": actor.org_id, "project_id": product["project_id"],
                      "source_connection_id": credential_id, "record_type": "connection",
                      "record_id": credential_id, "record_revision": "revision-1"}
        scenario["permissions"][(actor.org_id, product["project_id"])].add((credential_id, credential_id))
        async with scenario["sessions"]() as db:
            credential = UserCredential(
                id=credential_id, org_id=actor.org_id, user_id=actor.user_id, service="aws",
                credential_type="aws_role", label="portable-" + product["project_id"],
                secret_arn=f"arn:aws:secretsmanager:us-east-1:{account}:secret:portable-fixture",
                aws_external_id="offline-fixture", aws_verified_at=datetime.now(UTC),
                aws_verification_attempt=str(uuid4()), aws_verified_version_id="revision-1",
                scopes={"account_id": account, "role_arn": role, "status": "verified"},
            )
            db.add(credential)
            await db.flush()
            credential.aws_verified_binding = connection_binding(credential)
            await db.commit()
        choices = {"service_id": "devops", "capabilities": [capability], "enabled": False,
                   "triggers": [], "devops": {"space_mode": "create", "region": region,
                   "hosting_connection_ref": connection, "accounts": [], "integrations": []}}
        created = await client.put(path, json=choices, headers={
            "Idempotency-Key": "portable_retained_draft_0001", "If-None-Match": "*"})
        assert created.status_code == 201, created.text
        configuration = created.json()
        selection = SpaceSelection(actor.org_id, product["product_id"], product["project_id"],
                                   str(uuid4()), account, region)
        resource = ResourceSelection(account, region, f"arn:aws:iam::{account}:role/portable-resources",
            role, {**connection, "record_type": "credential"}, "lambda",
            resource_arn=f"arn:aws:lambda:{region}:{account}:function:portable",
            telemetry_arn=f"arn:aws:logs:{region}:{account}:log-group:/aws/lambda/portable:*")
        policy_snapshot = {"version": 1, "role_arn": resource.role_arn, "selected_resources": {
            "lambda:GetFunction": [resource.resource_arn], "logs:FilterLogEvents": [resource.telemetry_arn]}}
        operation_id = str(uuid4())
        observation = {"status": "verifying", "account_id": account, "region": region,
            "role_arn": resource.role_arn, "resource_selection": asdict(resource),
            "native_space_id": "portable-space", "space_fingerprint": selection.fingerprint(),
            "scope_fingerprint": resource.fingerprint(), "association_id": str(uuid4()),
            "association_status": "valid", "effective_scope": {"trust": "selected_space",
            "policy_snapshot": policy_snapshot, "policy_sha256": policy_digest(policy_snapshot)},
            "checked_at": datetime.now(UTC).isoformat(),
            "expires_at": (datetime.now(UTC) + timedelta(minutes=10)).isoformat()}
        async with scenario["sessions"]() as db:
            db.add_all([
                Binding(tenant_id=actor.org_id, binding_id=selection.binding_id,
                    configuration_id=configuration["configuration_id"], product_id=product["product_id"],
                    project_id=product["project_id"], service_id="devops", scope=configuration["scope"],
                    revision=1, generation=1, state="ready", ownership="adopted", connection_ref=connection,
                    connection_key=credential_id, native_key=selection.binding_id,
                    native_resource_ref={"record_type": "agent_space", "source_connection_id": credential_id,
                                         "record_id": "portable-space"},
                    provider_identity={"account_id": account, "region": region, "role_arn": role}, readiness=[], gaps=[]),
                DevopsSpaceIntent(tenant_id=actor.org_id, operation_id=operation_id, selection=asdict(selection),
                    configuration_revision=configuration["revision"], intent_hash=configuration["hash"],
                    state="ready", native_space_id="portable-space", ownership="adopted",
                    scope_fingerprint=selection.fingerprint(), resource_observation=observation),
            ])
            await db.commit()
        policy_read = await client.get(product["profile_path"] +
            f"/services/devops/bindings/{selection.binding_id}/access-policy")
        assert policy_read.status_code == 200, policy_read.text
        policy = policy_read.json()["policy_ref"]
        scenario["permissions"][(actor.org_id, product["project_id"])].add((
            policy["source_connection_id"], policy["record_id"]))
        assert policy["record_id"] == operation_id and len(policy["record_revision"]) == 64
        assert policy_read.json()["native_readiness"] == "verifying"
        invented = await client.put(path, json={"devops": {**choices["devops"],
            "access_policy_ref": {**policy, "record_id": str(uuid4())}}}, headers={
            "Idempotency-Key": "portable_invented_policy_0001", "If-Match": created.headers["etag"]})
        assert invented.status_code == 404
        assert (await client.get(path)).json() == configuration
        profile = (await client.get(product["profile_path"])).json()
        selected = await client.patch(product["profile_path"] + "/configuration", json={"configuration": {
            **profile["configuration"], "advanced": {**profile["configuration"].get("advanced", {}),
                                                       "agent_policy_ref": policy}}}, headers={
            "Idempotency-Key": "portable_verified_policy_profile_" + product["project_id"],
            "If-Profile-Revision": str(profile["revision"])})
        assert selected.status_code == 200, selected.text
        refreshed = await client.post(path + "/refresh", json={"profile_ref": {
            **configuration["settings"]["profile_ref"], "record_revision": str(selected.json()["revision"])}}, headers={
            "Idempotency-Key": "portable_verified_policy_refresh_0001", "If-Match": created.headers["etag"]})
        assert refreshed.status_code == 200, refreshed.text
        saved = await client.put(path, json={"devops": {**choices["devops"], "access_policy_ref": policy}}, headers={
            "Idempotency-Key": "portable_verified_policy_select_0001", "If-Match": refreshed.headers["etag"]})
        assert saved.status_code == 200, saved.text
        return saved, policy, selected.json()["revision"], connection

    async def prepare_sibling():
        scenario["switch"](first_actor)
        client = scenario["client"]
        current_first = (await client.get(first["profile_path"])).json()
        sibling = await client.post(f"/v1/products/{first['product_id']}/projects",
            json={"mode": "create", "name": "Portable worker"}, headers={
            "Idempotency-Key": "portable_sibling_project_0001",
            "If-Profile-Revision": str(current_first["revision"])})
        assert sibling.status_code == 201, sibling.text
        sibling_id = sibling.json()["project_id"]
        sibling_profile_path = f"/v1/products/{first['product_id']}/projects/{sibling_id}"
        environment = first["references"][0] | {
            "project_id": sibling_id, "record_type": "environment", "record_id": "portable-sibling-environment"}
        scenario["permissions"][(first_actor.org_id, sibling_id)] = {
            (environment["source_connection_id"], environment["record_id"])}
        current_sibling = (await client.get(sibling_profile_path)).json()
        selected = await client.patch(sibling_profile_path + "/configuration", json={"configuration": {
            "selected_capabilities": [{"capability": "devops", "ownership_mode": "managed"}],
            "advanced": {"environments": [environment]}}}, headers={
            "Idempotency-Key": "portable_sibling_profile_0001",
            "If-Profile-Revision": str(current_sibling["revision"])})
        assert selected.status_code == 200, selected.text
        sibling_product = {"actor": first_actor, "product_id": first["product_id"],
                           "project_id": sibling_id, "profile_path": sibling_profile_path}
        return sibling_product, environment

    return scenario, first, second, prepare_sibling, select_retained_policy


@pytest.mark.asyncio
async def test_project_configuration(devops_portability_scenario):
    scenario, first, second, prepare_sibling, select_retained_policy = devops_portability_scenario
    client = scenario["client"]
    first_actor, second_actor = scenario["actors"]
    saved = []
    for product, region in ((first, "example-region-one"), (second, "example-region-two")):
        scenario["switch"](product["actor"])
        environment = product["references"][0] | {"record_type": "environment"}
        current_profile = (await client.get(product["profile_path"])).json()
        patch = await client.patch(product["profile_path"] + "/configuration", json={"configuration": {
            **current_profile["configuration"], "advanced": {
                "environments": [environment]}}}, headers={
            "Idempotency-Key": "portable_profile_source_0001",
            "If-Profile-Revision": str(current_profile["revision"])})
        assert patch.status_code == 200, patch.text
        path = product["path"].replace("/security/", "/devops/")
        options = await client.get(path + "/options")
        assert options.status_code == 200, options.text
        assert options.json()["capabilities"] == [
            "investigation", "release_readiness", "release_verification", "prevention"]
        assert options.json()["sources"] == [environment]
        assert options.json()["regions"] == []
        foreign = (second if product is first else first)["references"][0]
        denied = await client.put(path, json={"service_id": "devops",
            "capabilities": ["investigation"], "enabled": False, "triggers": [], "devops": {
                "space_mode": "create", "access_policy_ref": foreign,
                "accounts": [], "integrations": []}}, headers={
            "Idempotency-Key": "portable_foreign_source_0001", "If-None-Match": "*"})
        assert denied.status_code == 404
        assert (await client.get(path)).status_code == 404
        response, policy, policy_profile_revision, hosting = await select_retained_policy(product, region, "investigation")
        configuration = response.json()
        assert configuration["scope"]["product_id"] == product["product_id"]
        assert configuration["scope"]["project_id"] == product["project_id"]
        assert configuration["settings"]["devops"]["region"] == region
        assert configuration["settings"]["devops"]["access_policy_ref"] == policy
        assert configuration["settings"]["profile_ref"]["record_revision"] == str(policy_profile_revision)
        assert configuration["settings"]["source_refs"] == [environment]
        assert configuration["settings"]["devops"]["hosting_connection_ref"] == hosting
        assert "connection_ref" not in configuration["settings"]
        assert configuration["handoff_state"] == "linked"
        assert (await client.get(path)).json() == configuration
        forbidden_default = await client.put(path, json={"devops": {
            **configuration["settings"]["devops"], "hosting_connection_ref": {
                "tenant_id": product["actor"].org_id, "project_id": product["project_id"],
                "source_connection_id": "adp.profile", "record_type": "connection",
                "record_id": "ADP", "record_revision": "1"}}}, headers={
            "Idempotency-Key": "portable_no_platform_default_0001",
            "If-Match": response.headers["etag"]})
        assert forbidden_default.status_code == 409
        assert forbidden_default.json()["detail"]["code"] == "access_policy_selection_changed"
        unpinned_default = await client.put(path, json={"devops": {
            **{key: value for key, value in configuration["settings"]["devops"].items()
               if key != "access_policy_ref"}, "hosting_connection_ref": {
                "tenant_id": product["actor"].org_id, "project_id": product["project_id"],
                "source_connection_id": "adp.profile", "record_type": "connection",
                "record_id": "ADP", "record_revision": "1"}}}, headers={
            "Idempotency-Key": "portable_no_unpinned_default_0001", "If-Match": response.headers["etag"]})
        assert unpinned_default.status_code == 404
        assert (await client.get(path)).json() == configuration
        checked = await client.post(path + "/check")
        assert checked.json()["ready"] is False and checked.json()["provider_contacted"] is False
        assert "investigation.needs_account" in checked.json()["gaps"]
        profile = await client.get(product["profile_path"])
        assert profile.json()["configuration"]["selected_capabilities"][0]["configuration_ref"]["record_id"] == configuration["configuration_id"]
        assert profile.json()["configuration"]["advanced"]["environments"] == [environment]
        assert profile.json()["configuration"]["advanced"]["agent_policy_ref"] == policy
        saved.append((path, configuration))
    assert saved[0][1]["configuration_id"] != saved[1][1]["configuration_id"]
    scenario["switch"](first_actor)
    assert (await client.get(saved[1][0])).status_code == 404
    assert (await client.get(saved[0][0])).json()["settings"]["devops"]["region"] == "example-region-one"
    sibling, sibling_environment = await prepare_sibling()
    sibling_id = sibling["project_id"]
    sibling_path = sibling["profile_path"] + "/services/devops/configuration"
    sibling_options = await client.get(sibling_path + "/options")
    assert sibling_options.status_code == 200 and sibling_options.json()["sources"] == [sibling_environment]
    sibling_saved, sibling_policy, sibling_revision, _ = await select_retained_policy(
        sibling, "example-region-three", "release_readiness")
    assert sibling_saved.json()["handoff_state"] == "linked"
    assert sibling_saved.json()["scope"]["project_id"] == sibling_id
    assert sibling_saved.json()["settings"]["source_refs"] == [sibling_environment]
    assert sibling_saved.json()["settings"]["devops"]["access_policy_ref"] == sibling_policy
    assert sibling_saved.json()["settings"]["profile_ref"]["record_revision"] == str(sibling_revision)
    assert (await client.get(sibling_path)).json() == sibling_saved.json()
    assert sibling_environment not in saved[0][1]["settings"]["source_refs"]
    assert sibling_policy != saved[0][1]["settings"]["devops"]["access_policy_ref"]
    sibling_profile_path = sibling_path.removesuffix("/services/devops/configuration")
    pinned_sibling = await client.get(f"{sibling_profile_path}/revisions/{sibling_revision}")
    assert pinned_sibling.status_code == 200
    assert pinned_sibling.json()["configuration"]["advanced"]["environments"] == [sibling_environment]
    assert pinned_sibling.json()["configuration"]["advanced"]["agent_policy_ref"] == sibling_policy
    assert sibling_saved.json()["settings"]["devops"]["region"] == "example-region-three"
    assert "release_readiness.needs_repository" in sibling_saved.json()["gaps"]
    assert all(not gap.startswith("investigation.") for gap in sibling_saved.json()["gaps"])
    assert (await client.get(saved[0][0])).json()["settings"]["devops"]["region"] == "example-region-one"
    assert (await client.get(saved[1][0])).status_code == 404
    assert scenario["provider_attempts"] == []
    scenario["switch"](second_actor)
    assert (await client.get(saved[1][0])).json() == saved[1][1]
    assert (await client.get(sibling_path)).status_code == 404
    assert (await client.get(saved[0][0])).status_code == 404


@pytest.mark.asyncio
async def test_integration_limits_reach_gateway_configuration_check():
    from test_devops_source_readiness import readiness_fixture

    selection, evidence, native = await readiness_fixture()
    selection["event_support"] = True
    # Exercise repository/pipeline integration limits through the release path,
    # independently of Investigation's manual binding/owner-consent preflight.
    settings = {"service_id": "devops", "capabilities": ["release_readiness"], "enabled": True,
                "visibility": "only_me", "source_refs": [*selection["repository_refs"], *selection["workflow_refs"]],
                "devops": {"integrations": [selection]}}
    configuration = Configuration(
        tenant_id="tenant-example", configuration_id="integration-limits", product_id="product",
        project_id="alpha", service_id="devops", relationship_revision="1", owner_id="owner-example",
        current_revision=3, handoff_state="linked",
    )
    version = ConfigurationVersion(
        tenant_id=configuration.tenant_id, configuration_id=configuration.configuration_id, revision=3,
        state="ready", gaps=[], settings=settings, inherited_fields=[], hash="c" * 64,
    )
    backend = object.__new__(GatewayConfigurationBackend)
    backend.store = SimpleNamespace(tenant_id="tenant-example")

    async def current(product_id, project_id, service_id):
        assert (product_id, project_id, service_id) == ("product", "alpha", "devops")
        return configuration, version

    backend.current = current
    backend.devops_integration_observer = None
    missing = await backend.check("product", "alpha", "devops")
    assert missing["ready"] is False and missing["provider_contacted"] is False
    assert "capability" not in missing and "configuration_hash" not in missing
    assert missing["gaps"] == ["integration_not_ready"]
    assert missing["integrations"][0]["reason"] == "integration_readback_missing"

    calls = []

    async def observer(tenant_id, product_id, project_id, supplied_settings, revision):
        calls.append((tenant_id, product_id, project_id, revision))
        assert supplied_settings is settings
        return [{"selection": selection, "configuration_revision": revision,
                 "evidence": evidence, "native": native}]

    backend.devops_integration_observer = observer
    native["connection_mode"] = "pat"
    unsupported = await backend.check("product", "alpha", "devops")
    assert unsupported["ready"] is False and unsupported["provider_contacted"] is True
    assert unsupported["gaps"] == ["integration_not_ready"]
    assert unsupported["integrations"][0]["events"]["reason"] == "pat_events_unavailable"
    native["connection_mode"] = "app"
    ready = await backend.check("product", "alpha", "devops")
    assert ready["ready"] is True and ready["gaps"] == []
    assert "capability" not in ready and "configuration_hash" not in ready
    native["indexing"] = {"status": "uploaded"}
    unindexed = await backend.check("product", "alpha", "devops")
    assert unindexed["ready"] is False and unindexed["gaps"] == ["integration_not_ready"]
    assert calls == [("tenant-example", "product", "alpha", 3)] * 3
