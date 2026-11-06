"""Two genuine local tenants retain and authorize independent manual facts."""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, HTTPException, Response
from l5_services.api.configuration import view
from l5_services.configuration_population import populate_settings
from l5_services.models.base import Base as L5Base
from l5_services.models.binding import Binding
from l5_services.models.configuration import Configuration, ConfigurationVersion
from l5_services.models.run import Run
from sqlalchemy import delete, select, update
from starlette.requests import Request

from src.l5_integration import GatewayConfigurationBackend
from src.orchestration import l5_observation_routes
from src.orchestration.l5_observation_scope import ObservationError, digest
from src.products import security_evidence_local as local
from src.products.models import Product, ProductProjectBinding, Project
from src.products.security_evidence_contract import Consent, Observation, Selection
from src.products.security_evidence_effect import SecurityEffectBackend, reconcile
from src.products.security_evidence_model import SecurityEvidenceEvent
from src.products.security_evidence_store import append, event
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import Department, Organization, Team, TeamMembership, User
from src.shared.schemas.auth import TokenContext


@pytest.fixture
async def tenants(db_session, test_engine, db_session_factory, monkeypatch):
    async with test_engine.begin() as connection:
        await connection.run_sync(L5Base.metadata.create_all)
        await connection.run_sync(SecurityEvidenceEvent.__table__.create, checkfirst=True)
    monkeypatch.setattr(l5_observation_routes, "require_native_launch_session", AsyncMock())
    monkeypatch.setattr(local, "require_native_launch_session_digest", AsyncMock())
    app = FastAPI()
    records = []
    for number in (1, 2):
        tenant, owner, team, department = (f"{kind}-{number}" for kind in ("tenant", "owner", "team", "department"))
        db_session.add(Organization(id=tenant, name=tenant))
        db_session.add(Department(id=department, org_id=tenant, name=department))
        await db_session.flush()
        db_session.add(Team(id=team, org_id=tenant, department_id=department, name=team))
        db_session.add(User(id=owner, org_id=tenant, team_id=team, email=owner + "@example.test", user_kind="human"))
        await db_session.flush()
        db_session.add_all(
            [
                TenantMembership(user_id=owner, tenant_id=tenant, role="member"),
                TeamMembership(user_id=owner, org_id=tenant, team_id=team, role="member"),
            ]
        )
        product = Product(
            tenant_id=tenant,
            id=f"product-{number}",
            name="Security fixture",
            purpose="Test",
            intended_users=[],
            owner_user_id=owner,
            team_id=team,
            starting_point="new",
            created_by=owner,
        )
        project = Project(tenant_id=tenant, id=f"project-{number}", name="Fixture", owner_user_id=owner)
        db_session.add_all([product, project])
        await db_session.flush()
        db_session.add(ProductProjectBinding(tenant_id=tenant, product_id=product.id, project_id=project.id, provenance={}))
        config = Configuration(
            tenant_id=tenant,
            configuration_id="same-local-configuration-id",
            product_id=product.id,
            project_id=project.id,
            service_id="security",
            relationship_revision="1",
            owner_id=owner,
            current_revision=1,
            handoff_state="linked",
        )
        profile = {
            "tenant_id": tenant,
            "product_id": product.id,
            "project_id": project.id,
            "revision": 1,
            "product": {"purpose": "Security fixture"},
            "configuration": {"basic": {}},
        }
        profile_ref = {
            "tenant_id": tenant,
            "project_id": project.id,
            "source_connection_id": "adp.profile",
            "record_type": "product",
            "record_id": product.id,
            "record_revision": "1",
        }
        available = {"coverage": "complete", "next_cursor": None, "items": []}
        settings, inherited = populate_settings(
            profile,
            profile_ref,
            available,
            {
                "region": "us-east-1",
                "capabilities": ["code_review"],
                "overrides": [{"field": "region", "value": "us-east-1", "reason": "Initial fixture choice"}],
            },
            "security",
        )
        settings["monitoring"] = {"poll_interval_seconds": 600, "poll_grace_seconds": 300}
        version = ConfigurationVersion(
            tenant_id=tenant,
            configuration_id=config.configuration_id,
            revision=1,
            settings=settings,
            inherited_fields=inherited,
            gaps=[],
            state="ready",
            hash=digest(settings),
        )
        scope = {"tenant_id": tenant, "product_id": product.id, "project_id": project.id, "relationship_revision": "1"}
        binding = Binding(
            tenant_id=tenant,
            binding_id="same-local-binding-id",
            configuration_id=config.configuration_id,
            product_id=product.id,
            project_id=project.id,
            service_id="security",
            scope=scope,
            revision=1,
            generation=1,
            state="ready",
            ownership="adp_created",
            connection_ref={},
            connection_key=f"connection-{number}",
            provider_identity={},
            readiness=[],
            gaps=[],
        )
        db_session.add_all([config, version, binding])
        await db_session.commit()
        actor = TokenContext(
            user_id=owner,
            org_id=tenant,
            team_id=team,
            department_id=department,
            account_type="human",
            token_use="access",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        backend = GatewayConfigurationBackend(db_session, actor)
        backend.sessions = db_session_factory
        selected = Selection(
            tenant_id=tenant,
            product_id=product.id,
            project_id=project.id,
            binding_id=binding.binding_id,
            configuration_id=config.configuration_id,
            configuration_revision=1,
            configuration_hash=version.hash,
            binding_revision=1,
            binding_generation=1,
            source_sha256=digest([]),
            connection_sha256=digest({}),
            provider_sha256=digest({}),
            observer_id=owner,
            phase="edit",
            ui_region="us-west-2",
            ui_reason="Selected acceptance choices",
            ui_receipt="fixture_ui_receipt_" + str(number),
        )
        records.append(
            SimpleNamespace(
                db=db_session,
                actor=actor,
                backend=backend,
                selection=selected,
                product=product,
                project=project,
                config=config,
                version=version,
                binding=binding,
                tenant=tenant,
                app=app,
                profile=profile,
                profile_ref=profile_ref,
                available=available,
                request_id=f"request-{number}",
            )
        )

    # Real Product memberships/scopes/rows above remain active. Stub only the
    # separate profile/source connector boundary; reads and commits use real ORM.
    async def current(self, product_id, project_id, service_id, **kwargs):
        await local.member_access(self.db, self.actor.org_id, product_id, project_id, self.actor.user_id)
        configuration = await self.db.scalar(
            select(Configuration)
            .where(
                Configuration.tenant_id == self.actor.org_id,
                Configuration.product_id == product_id,
                Configuration.project_id == project_id,
                Configuration.service_id == service_id,
            )
            .execution_options(populate_existing=True)
        )
        if configuration is None:
            raise HTTPException(404)
        version = await self.db.get(
            ConfigurationVersion, (self.actor.org_id, configuration.configuration_id, configuration.current_revision), populate_existing=True
        )
        if version is None:
            raise HTTPException(409)
        return configuration, version

    async def get_binding(self, product_id, project_id, service_id, binding_id):
        value = await self.db.get(Binding, (self.actor.org_id, binding_id), populate_existing=True)
        if value is None or (value.product_id, value.project_id, value.service_id) != (product_id, project_id, service_id):
            raise HTTPException(404)
        return value

    monkeypatch.setattr(GatewayConfigurationBackend, "current", current)
    monkeypatch.setattr(GatewayConfigurationBackend, "get_binding", get_binding)
    for record in records:
        now = datetime.now(UTC)
        await append(
            db_session,
            record.tenant,
            record.request_id,
            "request",
            "system:security-release",
            "service",
            {
                "selection": record.selection.model_dump(),
                "release": {"flow_id": "origin-only"},
                "fixture_sha256": "a" * 64,
                "created_at": now.isoformat(),
                "expires_at": (now + timedelta(minutes=10)).isoformat(),
                "initial_claim_lease_expires_at": (now + timedelta(minutes=20)).isoformat(),
            },
        )
    await db_session.commit()
    return records


def http(c, token="interactive", headers=None):
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/",
            "app": c.app,
            "headers": [(key.encode(), value.encode()) for key, value in [("authorization", "Bearer " + token + c.tenant), *(headers or {}).items()]],
        }
    )


async def consent(c):
    for slot in local.SLOTS:
        await local.register(c.backend, http(c, slot), c.product.id, c.project.id, c.request_id, slot)
    await c.db.commit()
    preview = await local.preview(c.backend, http(c), c.product.id, c.project.id, c.request_id)
    await local.endorse(
        c.backend,
        http(c),
        c.product.id,
        c.project.id,
        c.request_id,
        Consent(
            observer_id=c.actor.user_id,
            snapshot_sha256=preview["snapshot_sha256"],
            expires_at=min(datetime.now(UTC) + timedelta(minutes=5), datetime.fromisoformat(preview["maximum_expires_at"])),
        ),
    )
    await c.db.commit()
    return await local.prepare(c.backend, http(c), c.product.id, c.project.id, c.request_id)


async def test_two_tenants_and_fixture_credentials_cannot_record_human_evidence(tenants):
    one, two = tenants
    for case in tenants:
        prepared = await consent(case)
        assert prepared["snapshot"]["configuration"]["scope"]["tenant_id"] == case.tenant
        for slot in local.SLOTS:
            with pytest.raises(HTTPException) as denied:
                await local.preview(case.backend, http(case, slot), case.product.id, case.project.id, case.request_id)
            assert denied.value.status_code == 403
    with pytest.raises(ObservationError, match="not_found"):
        await local.request_row(one.backend, two.product.id, two.project.id, two.request_id)
    with pytest.raises(ObservationError, match="not_found"):
        await local.member_access(one.db, two.tenant, two.product.id, two.project.id, one.actor.user_id)


async def test_current_owner_membership_and_revocation_control_every_export(tenants):
    c = tenants[0]
    await consent(c)
    request = await event(c.db, c.tenant, c.request_id, "request")
    grant = await local.validate_export_authority(c.db, request)
    await local.validate_export_authority(c.db, request, grant.document["credentials"]["api"])
    with pytest.raises(ObservationError, match="reader_changed"):
        await local.validate_export_authority(c.db, request, {**grant.document["credentials"]["api"], "fingerprint": "f" * 64})
    await local.revoke(c.backend, http(c), c.product.id, c.project.id, c.request_id)
    await c.db.commit()
    with pytest.raises(ObservationError, match="grant_revoked"):
        await local.validate_export_authority(c.db, request)
    assert (await local.read_status(c.backend, http(c), c.product.id, c.project.id, c.request_id))["status"] == "revoked"


@pytest.mark.parametrize("mutation", ["owner", "membership", "binding", "configuration"])
async def test_post_consent_scope_changes_refuse_fresh_transfer(tenants, mutation):
    c = tenants[0]
    await consent(c)
    if mutation == "owner":
        c.product.owner_user_id = "another-owner"
    elif mutation == "membership":
        await c.db.execute(delete(TeamMembership).where(TeamMembership.user_id == c.actor.user_id))
    elif mutation == "binding":
        c.binding.generation += 1
    else:
        c.config.current_revision += 1
    await c.db.commit()
    with pytest.raises((ObservationError, HTTPException)):
        await local.validate_export_authority(c.db, await event(c.db, c.tenant, c.request_id, "request"))


async def test_ledger_refuses_direct_and_bulk_change(tenants):
    c = tenants[0]
    row = await event(c.db, c.tenant, c.request_id, "request")
    row.kind = "changed"
    with pytest.raises(ValueError, match="append-only"):
        await c.db.flush()
    await c.db.rollback()
    for statement in (update(SecurityEvidenceEvent).values(kind="changed"), delete(SecurityEvidenceEvent)):
        with pytest.raises(ValueError, match="append-only"):
            await c.db.execute(statement)


@pytest.mark.parametrize("lost", [False, True])
async def test_actual_conditional_save_and_atomic_receipt_reconcile_without_repeat(tenants, monkeypatch, lost):
    from l5_services.models.configuration import ConfigurationReceipt

    from src.products import routes as product_routes

    c = tenants[0]
    await consent(c)
    await c.db.commit()
    monkeypatch.setattr(product_routes, "get_revision", AsyncMock(return_value=c.profile))
    c.backend.profile_inputs = AsyncMock(return_value=(None, c.profile_ref, c.available))

    async def handoff(configuration, version, *_):
        configuration.handoff_state = "linked"
        await c.db.commit()
        return view(configuration, version)

    c.backend.handoff = handoff
    original = c.backend.save
    calls = []

    async def save(*args):
        calls.append(args)
        result = await original(*args)
        if lost:
            raise HTTPException(503, detail={"code": "response_lost"})
        return result

    c.backend.save = save
    wrapper = SecurityEffectBackend(c.backend, http(c, headers={"x-adp-security-evidence": c.request_id}), Response())
    settings = {"region": c.selection.ui_region, "overrides": [{"field": "region", "value": c.selection.ui_region, "reason": c.selection.ui_reason}]}
    arguments = (c.product.id, c.project.id, "security", settings, c.selection.ui_receipt, None, '"' + c.selection.configuration_hash + '"')
    if lost:
        with pytest.raises(HTTPException):
            await wrapper.save(*arguments)
    else:
        assert (await wrapper.save(*arguments))["revision"] == 2
    status = await local.read_status(c.backend, http(c), c.product.id, c.project.id, c.request_id)
    assert status["status"] == ("outcome_unknown" if lost else "ready_for_export")
    assert status["next_action"]
    committed = await event(c.db, c.tenant, c.request_id, "effect_committed")
    assert committed.document["revision"] == 2
    receipt = await c.db.get(
        ConfigurationReceipt, (c.tenant, c.product.id, c.project.id, "security", c.actor.user_id, "edit", c.selection.ui_receipt)
    )
    assert receipt.revision == 2
    await reconcile(c.backend, http(c, "api"), c.product.id, c.project.id, c.request_id)
    await c.db.commit()
    facts = await event(c.db, c.tenant, c.request_id, "effect_reconciled")
    assert facts.document["after"]["configuration"]["revision"] == 2
    assert (await local.read_status(c.backend, http(c), c.product.id, c.project.id, c.request_id))["status"] == "ready_for_export"
    with pytest.raises(HTTPException):
        await wrapper.save(*arguments)
    assert len(calls) == 1
    assert (await local.historical_records(c.db, await event(c.db, c.tenant, c.request_id, "request")))[2][-1].kind == "effect_reconciled"


async def test_in_flight_save_blocks_a_second_request_before_the_first_returns(tenants):
    c = tenants[0]
    await consent(c)
    await c.db.commit()
    started, finish = asyncio.Event(), asyncio.Event()
    calls = []

    async def uncertain_save(*args):
        calls.append(args)
        started.set()
        await finish.wait()
        raise HTTPException(503, detail={"code": "outcome_unknown"})

    c.backend.save = uncertain_save
    wrapper = SecurityEffectBackend(c.backend, http(c, headers={"x-adp-security-evidence": c.request_id}), Response())
    settings = {"region": c.selection.ui_region, "overrides": [{"field": "region", "value": c.selection.ui_region, "reason": c.selection.ui_reason}]}
    arguments = (c.product.id, c.project.id, "security", settings, c.selection.ui_receipt, None, '"' + c.selection.configuration_hash + '"')
    first = asyncio.create_task(wrapper.save(*arguments))
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        with pytest.raises(HTTPException) as duplicate:
            await wrapper.save(*arguments)
        assert duplicate.value.detail["code"] == "security_evidence_already_attempted"
        assert len(calls) == 1
    finally:
        finish.set()
        with pytest.raises(HTTPException):
            await first
    with pytest.raises(ObservationError):
        await reconcile(c.backend, http(c, "api"), c.product.id, c.project.id, c.request_id)
    assert (await local.read_status(c.backend, http(c), c.product.id, c.project.id, c.request_id))["status"] == "outcome_unknown"


@pytest.mark.parametrize("subject", ["canonical", "signed"])
async def test_native_designation_blocks_a_separate_token_for_the_same_fixture_subject(tenants, subject):
    c = tenants[0]
    c.actor._authenticated_subject_id = "signed-subject"
    designated = c.actor.user_id if subject == "canonical" else c.actor._authenticated_subject_id
    c.app.state.native_test_runtime = SimpleNamespace(
        configured=True, store=SimpleNamespace(designation=lambda value: {"subject": value} if value == designated else None)
    )
    with pytest.raises(HTTPException) as denied:
        await local.preview(c.backend, http(c, "fresh-token"), c.product.id, c.project.id, c.request_id)
    assert denied.value.status_code == 403
    assert denied.value.detail["code"] == "security_evidence_interactive_session_required"


async def test_unreadable_native_designation_registry_fails_closed(tenants):
    c = tenants[0]
    c.app.state.native_test_runtime = SimpleNamespace(configured=True, store=None)
    with pytest.raises(HTTPException) as unavailable:
        await local.preview(c.backend, http(c), c.product.id, c.project.id, c.request_id)
    assert unavailable.value.status_code == 503


@pytest.mark.parametrize("observation", ["displayed", "mismatch"])
async def test_display_confirmation_pins_the_actual_run_and_preserves_a_reported_mismatch(tenants, monkeypatch, observation):
    c = tenants[0]
    run_input = {"binding_id": c.binding.binding_id, "configuration_revision": 1, "sources": []}
    run = Run(
        tenant_id=c.tenant,
        run_id="original-mcp-run",
        product_id=c.product.id,
        project_id=c.project.id,
        service_id="security",
        configuration_id=c.config.configuration_id,
        configuration_revision=1,
        binding_id=c.binding.binding_id,
        binding_generation=1,
        trigger_id="manual",
        revision=1,
        input=run_input,
        admission={"scope": c.binding.scope, "configuration_id": c.config.configuration_id, "configuration_revision": 1},
        scope=c.binding.scope,
        deadline_at=datetime.now(UTC) + timedelta(minutes=15),
    )
    c.db.add(run)
    original = await event(c.db, c.tenant, c.request_id, "request")
    c.request_id = "display-original-mcp-run"
    c.selection = c.selection.model_copy(update={"phase": "display", "run_id": run.run_id, "run_revision": 1, "run_input_sha256": digest(run_input)})
    await append(
        c.db, c.tenant, c.request_id, "request", "system:security-release", "service", {**original.document, "selection": c.selection.model_dump()}
    )
    await c.db.commit()

    async def get_run(backend, product_id, project_id, service_id, run_id):
        await local.member_access(backend.db, backend.actor.org_id, product_id, project_id, backend.actor.user_id)
        actual = await backend.db.get(Run, (backend.actor.org_id, run_id), populate_existing=True)
        if actual is None or (actual.product_id, actual.project_id, actual.service_id) != (product_id, project_id, service_id):
            raise HTTPException(404)
        return actual

    monkeypatch.setattr(GatewayConfigurationBackend, "get_run", get_run)
    prepared = await consent(c)
    confirmed = Observation(
        snapshot_sha256=prepared["snapshot_sha256"],
        observation=observation,
        configuration_revision=1,
        binding_revision=1,
        run_id=run.run_id,
        run_revision=1,
    )
    with pytest.raises(ObservationError, match="display_mismatch"):
        await local.observe(c.backend, http(c), c.product.id, c.project.id, c.request_id, confirmed.model_copy(update={"run_revision": 2}))
    await local.observe(c.backend, http(c), c.product.id, c.project.id, c.request_id, confirmed)
    await c.db.commit()
    status = await local.read_status(c.backend, http(c), c.product.id, c.project.id, c.request_id)
    assert status["status"] == ("ready_for_export" if observation == "displayed" else "observation_mismatch")
    changed = confirmed.model_copy(update={"observation": "mismatch" if observation == "displayed" else "displayed"})
    with pytest.raises(ObservationError, match="already_attempted"):
        await local.observe(c.backend, http(c), c.product.id, c.project.id, c.request_id, changed)
    run.revision = 2
    await c.db.commit()
    with pytest.raises(ObservationError, match="scope_changed"):
        await local.validate_export_authority(c.db, await event(c.db, c.tenant, c.request_id, "request"))


async def test_registered_credentials_replay_after_consent_but_cannot_rotate(tenants):
    c = tenants[0]
    await consent(c)
    for slot in local.SLOTS:
        await local.register(c.backend, http(c, slot), c.product.id, c.project.id, c.request_id, slot)
        with pytest.raises(ObservationError, match="reader_changed"):
            await local.register(c.backend, http(c, "replacement-" + slot), c.product.id, c.project.id, c.request_id, slot)


async def test_owner_selection_does_not_give_observer_private_configuration_access(tenants):
    c = tenants[0]
    observer = "separate-observer"
    c.db.add(User(id=observer, org_id=c.tenant, team_id=c.actor.team_id, email="observer@example.test", user_kind="human"))
    await c.db.flush()
    c.db.add_all(
        [
            TenantMembership(user_id=observer, tenant_id=c.tenant, role="member"),
            TeamMembership(user_id=observer, org_id=c.tenant, team_id=c.actor.team_id, role="member"),
        ]
    )
    original = await event(c.db, c.tenant, c.request_id, "request")
    c.request_id = "request-private-observer"
    selected = c.selection.model_copy(update={"observer_id": observer})
    await append(
        c.db, c.tenant, c.request_id, "request", "system:security-release", "service", {**original.document, "selection": selected.model_dump()}
    )
    for slot in local.SLOTS:
        await local.register(c.backend, http(c, slot), c.product.id, c.project.id, c.request_id, slot)
    await c.db.commit()
    with pytest.raises(ObservationError, match="observer_access_revoked"):
        await local.preview(c.backend, http(c), c.product.id, c.project.id, c.request_id, observer)
