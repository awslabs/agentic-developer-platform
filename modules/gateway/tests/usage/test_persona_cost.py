"""Honest per-persona cost aggregation for PMM-08 (#5426)."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

from src.shared.schemas.auth import TokenContext
from src.usage.persona_attribution import PersonaUsageAttribution
from src.usage.persona_cost import PersonaCostStatus, get_persona_cost_report
from src.usage.service import UsageService


def _decision():
    return SimpleNamespace(
        confidence="verified",
        estimate_reasons=(),
        to_dict=lambda: {"confidence": "verified"},
        source_kind="database",
        generation_id=2,
        pointer_revision=3,
        snapshot_version="v1",
        policy_version=2,
    )


def _context(*, tenant="tenant", owner="owner", persona="architect", chain="chain"):
    context = TokenContext(
        user_id="billing-worker",
        org_id="__platform__",
        attributed_org_id=tenant,
        team_id="",
        department_id="",
        account_type="service",
        auth_source="iam",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    context._persona_usage_attribution = PersonaUsageAttribution(
        tenant_id=tenant,
        invocation_id=f"run-{persona}",
        root_invocation_id="root",
        chain_id=chain,
        persona_key=persona,
        compatibility_class="claude-agent-sdk",
        harness_contract_revision="0.3.220",
        principal_kind="service_account",
        principal_id=owner,
        snapshot_digest="a" * 64,
        policy_revision="b" * 64,
        catalogue_revision="c" * 64,
    )
    return context


async def _write(db, *, persona, model, cost, tenant="tenant", owner="owner", chain="chain", priced=True):
    context = _context(tenant=tenant, owner=owner, persona=persona, chain=chain)
    await UsageService(db).log_request(
        context=context,
        model=model,
        input_tokens=10,
        output_tokens=5,
        cost_usd=Decimal(cost),
        latency_ms=1,
        status_code=200,
        request_id=f"request-{tenant}-{owner}-{chain}-{persona}-{model}",
        agent_run_id=f"run-{persona}",
        pricing_decision=_decision() if priced else None,
    )


async def test_three_hops_are_grouped_once_and_chain_total_equals_hops(db_session):
    await _write(db_session, persona="architect", model="opus", cost="0.030000")
    await _write(db_session, persona="developer", model="sonnet", cost="0.020000")
    await _write(db_session, persona="reviewer", model="haiku", cost="0.010000")

    report = await get_persona_cost_report(
        db_session,
        org_id="tenant",
        principal_kind="service_account",
        principal_id="owner",
        chain_id="chain",
    )
    assert report.status is PersonaCostStatus.KNOWN
    assert report.amount_usd == Decimal("0.060000")
    assert report.call_count == 3
    assert sum(entry.amount_usd for entry in report.entries) == report.amount_usd
    assert {entry.persona_key for entry in report.entries} == {"architect", "developer", "reviewer"}
    assert report.principal_dimension == "preference_owner"
    assert "invoice reconciliation is not established" in report.caveat


async def test_billing_tenant_and_preference_owner_both_scope_the_query(db_session):
    await _write(db_session, persona="architect", model="opus", cost="0.010000")
    await _write(db_session, persona="architect", model="opus", cost="9.000000", tenant="other")
    await _write(db_session, persona="architect", model="opus", cost="8.000000", owner="other-owner")

    report = await get_persona_cost_report(
        db_session,
        org_id="tenant",
        principal_kind="service_account",
        principal_id="owner",
    )
    assert report.amount_usd == Decimal("0.010000")
    assert report.call_count == 1


async def test_no_rows_is_unknown_not_free(db_session):
    report = await get_persona_cost_report(
        db_session,
        org_id="tenant",
        principal_kind="human",
        principal_id="missing",
    )
    assert report.status is PersonaCostStatus.UNKNOWN
    assert report.amount_usd is None
    assert report.call_count == 0
    assert report.preferences
    assert all(entry["compatibility_class"] and entry["source"] == "system-default" for entry in report.preferences)
    assert all("class_default_status" in entry for entry in report.preferences)


async def test_missing_pricing_revision_marks_lower_bound_partial(db_session):
    await _write(db_session, persona="architect", model="opus", cost="0.010000", priced=True)
    await _write(db_session, persona="developer", model="sonnet", cost="0.000000", priced=False)
    report = await get_persona_cost_report(
        db_session,
        org_id="tenant",
        principal_kind="service_account",
        principal_id="owner",
    )
    assert report.amount_usd == Decimal("0.010000")
    assert report.partial is True
    assert report.unpriced_call_count == 1


async def test_only_unpriced_rows_are_unknown_not_measured_zero(db_session):
    await _write(db_session, persona="developer", model="sonnet", cost="0.000000", priced=False)
    report = await get_persona_cost_report(
        db_session,
        org_id="tenant",
        principal_kind="service_account",
        principal_id="owner",
    )
    assert report.status is PersonaCostStatus.UNKNOWN
    assert report.amount_usd is None
    assert report.partial is True
