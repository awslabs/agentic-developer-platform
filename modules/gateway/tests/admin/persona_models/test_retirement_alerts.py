"""Durable retired-model operator notifications for PMM-08."""

from dataclasses import replace
from datetime import UTC, datetime

from sqlalchemy import select

from src.admin.persona_models import catalogue
from src.admin.persona_models.retirement import (
    ClaimedRetirement,
    claim_retirement,
    mark_delivered,
    run_retirement_alert_pass,
)
from src.shared.models.base import new_uuid
from src.shared.models.persona_models import PersonaModelPreference, PersonaModelRetirementAlert


def _preference(*, model_id: str, org_id: str = "tenant-a", owner_id: str = "owner-a"):
    return PersonaModelPreference(
        id=new_uuid(),
        org_id=org_id,
        principal_kind="service_account",
        principal_source="agent_registry",
        principal_id=owner_id,
        persona_key="architect",
        canonical_model_id=model_id,
        requested_alias=None,
        revision=1,
        updated_by="admin",
        updated_by_source="self",
    )


async def test_two_scans_publish_once_and_persist_delivered(db_session_factory, monkeypatch):
    retired = replace(catalogue.PLATFORM_MODEL_CATALOGUE[0], lifecycle="retired")
    monkeypatch.setattr(catalogue, "PLATFORM_MODEL_CATALOGUE", (retired,))
    async with db_session_factory() as session:
        session.add(_preference(model_id=retired.canonical_model_id))
        await session.commit()
    sent = []

    first = await run_retirement_alert_pass(db_session_factory, notify_fn=lambda message: sent.append(message) or "sns-1")
    second = await run_retirement_alert_pass(db_session_factory, notify_fn=lambda message: sent.append(message) or "sns-2")

    assert (first.delivered, second.delivered, len(sent)) == (1, 0, 1)
    assert sent[0].detail["audience"] == "platform_operator"
    async with db_session_factory() as session:
        row = (await session.scalars(select(PersonaModelRetirementAlert))).one()
        assert row.state == "delivered"
        assert row.lifecycle_revision == retired.lifecycle_revision


async def test_claim_loser_never_publishes_and_wrong_token_cannot_deliver(db_session_factory, monkeypatch):
    retired = replace(catalogue.PLATFORM_MODEL_CATALOGUE[0], lifecycle="retired")
    monkeypatch.setattr(catalogue, "PLATFORM_MODEL_CATALOGUE", (retired,))
    pref = _preference(model_id=retired.canonical_model_id)
    async with db_session_factory() as session:
        session.add(pref)
        await session.commit()
    from src.admin.persona_models.retirement import RetirementCandidate

    candidate = RetirementCandidate(
        preference_id=pref.id,
        org_id=pref.org_id,
        persona_key=pref.persona_key,
        owner_kind=pref.principal_kind,
        owner_id=pref.principal_id,
        model_id=pref.canonical_model_id,
        lifecycle_revision=retired.lifecycle_revision,
    )
    winner = await claim_retirement(db_session_factory, candidate)
    assert winner is not None
    loser = await claim_retirement(db_session_factory, candidate)
    assert loser is None
    wrong = ClaimedRetirement(candidate=candidate, claim_token="wrong-token", retry=False)
    assert await mark_delivered(db_session_factory, wrong) is False
    assert await mark_delivered(db_session_factory, winner) is True


async def test_publish_failure_is_sanitized_and_retried_next_scan(db_session_factory, monkeypatch):
    retired = replace(catalogue.PLATFORM_MODEL_CATALOGUE[0], lifecycle="retired")
    monkeypatch.setattr(catalogue, "PLATFORM_MODEL_CATALOGUE", (retired,))
    async with db_session_factory() as session:
        session.add(_preference(model_id=retired.canonical_model_id))
        await session.commit()

    def fail(_message):
        raise RuntimeError("secret provider detail")

    failed = await run_retirement_alert_pass(db_session_factory, notify_fn=fail)
    assert failed.notifications_failed == 1
    async with db_session_factory() as session:
        row = (await session.scalars(select(PersonaModelRetirementAlert))).one()
        assert row.state == "claimed"
        assert row.last_error == "RuntimeError"

    delivered = await run_retirement_alert_pass(db_session_factory, notify_fn=lambda _message: "sns-retry")
    assert delivered.retries_acquired == 1
    assert delivered.delivered == 1


async def test_new_model_transition_rearms_and_tenants_remain_separate(db_session_factory, monkeypatch):
    first = replace(catalogue.PLATFORM_MODEL_CATALOGUE[0], lifecycle="retired")
    second = replace(catalogue.PLATFORM_MODEL_CATALOGUE[1], lifecycle="retired")
    monkeypatch.setattr(catalogue, "PLATFORM_MODEL_CATALOGUE", (first, second))
    pref_a = _preference(model_id=first.canonical_model_id, org_id="tenant-a", owner_id="same-owner")
    pref_b = _preference(model_id=first.canonical_model_id, org_id="tenant-b", owner_id="same-owner")
    async with db_session_factory() as session:
        session.add_all([pref_a, pref_b])
        await session.commit()
    sent = []
    report = await run_retirement_alert_pass(db_session_factory, notify_fn=lambda message: sent.append(message) or "sns")
    assert report.delivered == 2
    assert {message.org_id for message in sent} == {"tenant-a", "tenant-b"}

    async with db_session_factory() as session:
        pref_a.canonical_model_id = second.canonical_model_id
        merged = await session.merge(pref_a)
        merged.updated_at = datetime.now(UTC)
        await session.commit()
    again = await run_retirement_alert_pass(db_session_factory, notify_fn=lambda message: sent.append(message) or "sns-new")
    assert again.delivered == 1
    assert sent[-1].org_id == "tenant-a"
    assert sent[-1].detail["canonical_model_id"] == second.canonical_model_id
