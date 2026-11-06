"""Two real tenants disclose only owner-endorsed facts to the current release."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from src.orchestration import l5_observation_export as exports
from src.orchestration import security_evidence_release as release
from src.orchestration.l5_observation_scope import ObservationError
from src.orchestration.operations_release_job import NativeJobRequest, NativeReleaseJob
from src.products import security_evidence_local as local
from src.products.security_evidence_store import event
from tests.orchestration.test_l5_observation_scope import product_case, scope_case  # noqa: F401
from tests.orchestration.test_security_evidence_release import security_release  # noqa: F401
from tests.products.test_security_manual_evidence import consent, http, tenants  # noqa: F401


@pytest.fixture
async def exchange_case(security_release, tenants, db_session_factory, monkeypatch):  # noqa: F811
    c = security_release
    c.guard = NativeReleaseJob(db_session_factory, provider=c.provider)
    c.job = NativeJobRequest(
        correlation=c.action.detail["correlation"],
        source_revision="d" * 40,
        dispatch_revision="e" * 40,
        workflow="l5-services-release.yml",
        role_arn="arn:aws:iam::123456789012:role/release",
        mutation_role_arn="arn:aws:iam::123456789012:role/mutation",
        contract_digest=c.action.detail["contract_digest"],
    )
    c.claims = {"run_id": "72", "run_attempt": "1", "actor_id": "789"}

    async def job_snapshot(guard, db, body, claims, **kwargs):
        assert body == c.job and claims == c.claims
        return await db.get(type(c.action), c.action.id), c.report

    c.authority = AsyncMock(side_effect=job_snapshot)
    monkeypatch.setattr(exports, "job_snapshot", c.authority)
    c.locals = tenants
    selections = [case.selection.model_copy(update={"phase": "readiness"}) for case in tenants]
    c.issue_body = release.Issue(
        job=c.job,
        fixture_sha256="a" * 64,
        fixture_cases=[
            release.FixtureCase(**{key: selected.model_dump()[key] for key in release.FixtureCase.model_fields}) for selected in selections
        ],
        fixture_expires_at=datetime.now(UTC) + timedelta(minutes=30),
        selections=selections,
        lifetime_seconds=300,
    )
    issued = await release.issue(c.guard, c.issue_body, c.claims)
    for case, selected, result in zip(tenants, selections, issued["requests"]):
        case.selection = selected
        case.request_id = result["request_id"]
    return c


async def transfer(c, case):
    body = release.Exchange(job=c.job, tenant_id=case.tenant, request_id=case.request_id)
    ticket = await release.exchange(c.guard, body, c.claims)
    await local.export_local(case.backend, http(case, "api"), case.product.id, case.project.id, ticket["ticket_id"])
    await case.db.commit()
    return await release.exchange(c.guard, body.model_copy(update={"ticket_id": ticket["ticket_id"]}), c.claims, consume=True)


async def test_two_distinct_owner_consents_and_exact_readers_are_required(exchange_case):
    c = exchange_case
    one, two = c.locals
    await consent(one)
    await one.db.commit()
    with pytest.raises(ObservationError, match="pending"):
        await transfer(c, two)
    result = await transfer(c, one)
    assert result["facts"]["request"]["selection"]["tenant_id"] == one.tenant
    assert result["release_check_completed"] is result["native_qualified"] is False
    await consent(two)
    await two.db.commit()
    other = await transfer(c, two)
    assert other["facts"]["grant"]["owner_id"] == two.actor.user_id
    assert other["facts"]["request"]["selection"]["tenant_id"] != one.tenant
    assert c.authority.await_count >= 10
    with pytest.raises(ObservationError, match="not_found"):
        await local.request_row(one.backend, two.product.id, two.project.id, two.request_id)


async def test_reader_logout_and_revoke_after_export_reject_import(exchange_case, monkeypatch):
    c = exchange_case
    case = c.locals[0]
    await consent(case)
    await case.db.commit()
    body = release.Exchange(job=c.job, tenant_id=case.tenant, request_id=case.request_id)
    ticket = await release.exchange(c.guard, body, c.claims)
    await local.export_local(case.backend, http(case, "api"), case.product.id, case.project.id, ticket["ticket_id"])
    await case.db.commit()
    await local.revoke(case.backend, http(case), case.product.id, case.project.id, case.request_id)
    await case.db.commit()
    with pytest.raises(ObservationError, match="grant_revoked"):
        await release.exchange(c.guard, body.model_copy(update={"ticket_id": ticket["ticket_id"]}), c.claims, consume=True)
    assert (await event(case.db, case.tenant, ticket["ticket_id"], "export")).document["facts"]


async def test_frozen_roster_cannot_add_third_tenant_or_change_original_expiry(exchange_case):
    c = exchange_case
    changed = c.issue_body.model_copy(update={"fixture_expires_at": c.issue_body.fixture_expires_at + timedelta(minutes=1)})
    with pytest.raises(ObservationError, match="fixture_changed"):
        await release.issue(c.guard, changed, c.claims)
    selected = c.issue_body.selections[0].model_copy(update={"tenant_id": "third-tenant"})
    changed = c.issue_body.model_copy(update={"selections": [selected]})
    with pytest.raises(ObservationError, match="fixture_changed"):
        await release.issue(c.guard, changed, c.claims)


async def test_phase_invitation_history_is_bounded_and_exact_replay_uses_no_slot(exchange_case, monkeypatch):
    c = exchange_case
    monkeypatch.setattr(release, "MAX_REQUESTS_PER_CASE_PHASE", 2)
    first = await release.issue(c.guard, c.issue_body, c.claims)
    selected = c.issue_body.selections[0].model_copy(update={"configuration_revision": 2, "configuration_hash": "b" * 64})
    changed = c.issue_body.model_copy(update={"selections": [selected]})
    second = await release.issue(c.guard, changed, c.claims)
    assert second["requests"][0]["request_id"] != first["requests"][0]["request_id"]
    assert await release.issue(c.guard, changed, c.claims) == second
    selected = selected.model_copy(update={"configuration_revision": 3, "configuration_hash": "c" * 64})
    with pytest.raises(ObservationError, match="request_limit"):
        await release.issue(c.guard, changed.model_copy(update={"selections": [selected]}), c.claims)
    # The other original tenant and other original phase retain their own bound.
    await release.issue(
        c.guard,
        c.issue_body.model_copy(
            update={"selections": [c.issue_body.selections[1].model_copy(update={"configuration_revision": 2, "configuration_hash": "b" * 64})]}
        ),
        c.claims,
    )
    await release.issue(
        c.guard, c.issue_body.model_copy(update={"selections": [c.issue_body.selections[0].model_copy(update={"phase": "edit"})]}), c.claims
    )


async def test_actual_two_tenant_exports_validate_in_original_source_adapter(exchange_case, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[3] / "L5-services" / "tests" / "live"))
    from security_manual_setup import VerifiedManualSetupEvidence

    c = exchange_case
    for case in c.locals:
        await consent(case)
        await case.db.commit()
        body = release.Exchange(job=c.job, tenant_id=case.tenant, request_id=case.request_id)
        ticket = await release.exchange(c.guard, body, c.claims)
        await local.export_local(case.backend, http(case, "api"), case.product.id, case.project.id, ticket["ticket_id"])
        await case.db.commit()
        exported = await release.exchange(c.guard, body.model_copy(update={"ticket_id": ticket["ticket_id"]}), c.claims, consume=True)
        current = exported["facts"]["request"]["release"]
        source_release = {**current, "definition_revision": current["dispatch_revision"]}
        verified = VerifiedManualSetupEvidence.from_import(
            exported,
            selected=case.selection.model_dump(),
            release=source_release,
            fixture_sha256=c.issue_body.fixture_sha256,
            ticket_id=ticket["ticket_id"],
        )
        assert verified.facts["request"]["selection"]["tenant_id"] == case.tenant
        foreign = c.locals[1] if case is c.locals[0] else c.locals[0]
        with pytest.raises(ValueError, match="differs"):
            VerifiedManualSetupEvidence.from_import(
                exported,
                selected=foreign.selection.model_dump(),
                release=source_release,
                fixture_sha256=c.issue_body.fixture_sha256,
                ticket_id=ticket["ticket_id"],
            )


async def test_committed_lost_response_reconciles_after_expiry_and_renews_export_only(exchange_case, monkeypatch):
    from fastapi import HTTPException, Response
    from l5_services.api.configuration import view

    from src.products import routes as product_routes
    from src.products import security_evidence_effect as effects
    from src.products import security_evidence_store as store

    c = exchange_case
    monkeypatch.setattr(release, "MAX_REQUESTS_PER_CASE_PHASE", 2)
    case = c.locals[0]
    selection = case.selection.model_copy(update={"phase": "edit"})
    issued = await release.issue(c.guard, c.issue_body.model_copy(update={"selections": [selection]}), c.claims)
    case.selection, case.request_id = selection, issued["requests"][0]["request_id"]
    original_id = case.request_id
    await consent(case)
    await case.db.commit()
    monkeypatch.setattr(product_routes, "get_revision", AsyncMock(return_value=case.profile))
    case.backend.profile_inputs = AsyncMock(return_value=(None, case.profile_ref, case.available))

    async def handoff(configuration, version, *_):
        configuration.handoff_state = "linked"
        await case.db.commit()
        return view(configuration, version)

    case.backend.handoff = handoff
    save = case.backend.save
    saves = []

    async def lost(*args):
        saves.append(args)
        await save(*args)
        raise HTTPException(503, "lost response after commit")

    case.backend.save = lost
    wrapper = effects.SecurityEffectBackend(case.backend, http(case, headers={effects.HEADER.lower(): case.request_id}), Response())
    settings = {"region": selection.ui_region, "overrides": [{"field": "region", "value": selection.ui_region, "reason": selection.ui_reason}]}
    with pytest.raises(HTTPException):
        await wrapper.save(
            case.product.id, case.project.id, "security", settings, selection.ui_receipt, None, '"' + selection.configuration_hash + '"'
        )
    after_expiry = datetime.now(UTC) + timedelta(minutes=6)

    class Later(datetime):
        @classmethod
        def now(cls, tz=None):
            return after_expiry if tz else after_expiry.replace(tzinfo=None)

    for module in (local, effects, release, store):
        monkeypatch.setattr(module, "datetime", Later)
    assert (await local.read_status(case.backend, http(case), case.product.id, case.project.id, original_id))["status"] == "expired"
    await effects.reconcile(case.backend, http(case, "api"), case.product.id, case.project.id, original_id)
    await case.db.commit()
    renew = c.issue_body.model_copy(update={"selections": [selection], "renew_of": release.Reference(request_id=original_id, tenant_id=case.tenant)})
    renewed = await release.issue(c.guard, renew, c.claims)
    assert await release.issue(c.guard, renew, c.claims) == renewed
    case.request_id = renewed["requests"][0]["request_id"]
    assert renewed["requests"][0]["request"]["export_only"] is True
    for slot in local.SLOTS:
        await local.register(case.backend, http(case, slot), case.product.id, case.project.id, case.request_id, slot)
    await case.db.commit()
    preview = await local.preview(case.backend, http(case), case.product.id, case.project.id, case.request_id)
    from src.products.security_evidence_contract import Consent

    await local.endorse(
        case.backend,
        http(case),
        case.product.id,
        case.project.id,
        case.request_id,
        Consent(
            observer_id=case.actor.user_id,
            snapshot_sha256=preview["snapshot_sha256"],
            expires_at=datetime.fromisoformat(preview["maximum_expires_at"]),
        ),
    )
    await case.db.commit()
    with pytest.raises(ObservationError, match="export_only"):
        await local.prepare(case.backend, http(case), case.product.id, case.project.id, case.request_id)
    result = await transfer(c, case)
    assert result["facts"]["origin_request_id"] == original_id
    assert result["facts"]["events"][-1]["document"]["after"]["configuration"]["revision"] == 2
    assert len(saves) == 1
    # Renewal consumes the same finite phase history, even after a committed
    # effect. It cannot extend invitations indefinitely or authorize another Save.
    after_expiry += timedelta(minutes=6)
    again = renew.model_copy(update={"renew_of": release.Reference(request_id=case.request_id, tenant_id=case.tenant)})
    with pytest.raises(ObservationError, match="request_limit"):
        await release.issue(c.guard, again, c.claims)
    assert len(saves) == 1
