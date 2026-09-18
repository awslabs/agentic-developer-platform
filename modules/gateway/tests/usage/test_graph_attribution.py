"""`usage_logs.graph_address` is written from verified server state only (#4898).

The orchestration cost readback (`orchestration/cost.py`) groups `usage_logs` by
`graph_address`. It shipped correct and reported `unknown` / `no_usage_rows` for
every flow, because nothing wrote the column. These tests cover the write side and,
just as importantly, the cases that must NOT produce an address.

Two failure modes are specifically pinned here because both are silent in
production:

  - **A lost usage row.** Both production callers wrap `log_request` in a broad
    `except Exception` that logs a warning and continues, so anything that raises
    during attribution does not surface as an error — it drops the whole usage row,
    leaving real spend unmetered behind an HTTP 200. So enrichment must degrade to
    a NULL address, never to a missing row.
  - **A forged or crossed address.** A wrong address is worse than no address: the
    cost query would report it as `known` and present someone else's spend (or a
    guess) with the authority of a measurement.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from src.orchestration.dispatch import GraphAttribution
from src.shared.models.usage import UsageLog
from src.shared.schemas.auth import TokenContext
from src.usage.service import UsageService

ADDRESS = "delivery-loop/epic-1/wave-2/story-3"
RUN_ID = "orch:32cbd2eb-c002-50ef-a38f-271d6f32bd88"


def _context(**overrides) -> TokenContext:
    fields = dict(
        user_id="authority-worker",
        org_id="__platform__",
        team_id="",
        department_id="",
        account_type="service",
        auth_source="iam",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    fields.update(overrides)
    return TokenContext(**fields)


def _attribution(**overrides) -> GraphAttribution:
    fields = dict(org_id="tenant", flow_id="flow", node_id="node", node_attempt=1, address=ADDRESS, run_id=RUN_ID)
    fields.update(overrides)
    return GraphAttribution(**fields)


def _attributed_context(**overrides) -> TokenContext:
    context = _context()
    context.attributed_org_id = "tenant"
    context._graph_attribution = _attribution(**overrides)
    return context


# --- the resolver's contract ------------------------------------------------


def test_verified_assignment_is_persisted():
    assert UsageService._graph_address_for(_attributed_context(), RUN_ID) == ADDRESS


def test_no_assignment_is_null_not_a_placeholder():
    """Human/CLI/chat traffic and any degraded lookup. NULL means "unattributable".

    Never "unknown", never "", never a guessed address — migration 031's contract,
    and what keeps its partial index (`WHERE graph_address IS NOT NULL`) small.
    """
    assert UsageService._graph_address_for(_context(), None) is None


def test_absent_run_id_still_attributes_on_the_verified_assignment():
    """The assignment is the evidence; the run id is only a consistency check.

    Withholding an address merely because the row carries no run id would lose
    real attribution the server had already proven.
    """
    assert UsageService._graph_address_for(_attributed_context(), None) == ADDRESS


def test_contradictory_run_id_withholds_the_address():
    """A row asserting one run while attribution proves another is self-contradictory.

    NULL rather than persisting a mismatch a reader could not reconcile.
    """
    assert UsageService._graph_address_for(_attributed_context(), "orch:some-other-run") is None


@pytest.mark.parametrize("hostile", ["", "EVIL/e/w/n", None, 12345, {"S": "x"}, ["a"]])
def test_attribution_cannot_be_supplied_through_the_constructor(hostile):
    """The security property, asserted rather than assumed.

    pydantic does not populate private attributes from constructor input, so no
    `TokenContext(**caller_data)` site — and therefore no request header, body
    field or query parameter — can select the persisted address. Unforgeable by
    construction, not by a validation someone must remember to write (#3985,
    `draft_binding.py`).
    """
    context = _context(_graph_attribution=hostile)
    assert UsageService._graph_address_for(context, RUN_ID) is None
    assert "_graph_attribution" not in context.model_dump()


def test_resolver_cannot_raise_on_a_malformed_attribution():
    """Enrichment failure must degrade to NULL, never to a lost usage row.

    An object lacking the expected attributes is not a realistic production
    value; the point is that the resolver reaches for state defensively, so no
    future shape change can turn it into the swallowed exception that silently
    drops a row of real metered spend.
    """
    context = _context()
    context._graph_attribution = object()
    with pytest.raises(AttributeError):
        # Documents that the malformed object genuinely lacks the attribute —
        # so the None below is the resolver degrading, not the object cooperating.
        _ = context._graph_attribution.address
    assert UsageService._graph_address_for(context, RUN_ID) is None


def test_concurrent_contexts_hold_independent_addresses():
    """Attribution is request-owned, so parallel calls cannot exchange it.

    This is why the value lives on the per-request context rather than in a
    contextvar or a shared map: two nodes billing at once have no common state.
    """
    first = _attributed_context(address="flow-a/e1/w1/n1", run_id="orch:a")
    second = _attributed_context(address="flow-b/e9/w9/n9", run_id="orch:b")
    assert UsageService._graph_address_for(first, "orch:a") == "flow-a/e1/w1/n1"
    assert UsageService._graph_address_for(second, "orch:b") == "flow-b/e9/w9/n9"
    assert UsageService._graph_address_for(first, "orch:a") == "flow-a/e1/w1/n1"


# --- through the real writer, against a real session -----------------------


async def _log(session, context, **overrides):
    kwargs = dict(
        context=context,
        model="claude-sonnet-4",
        input_tokens=100,
        output_tokens=50,
        cost_usd=Decimal("0.001500"),
        latency_ms=1200,
        status_code=200,
        request_id="req-1",
        agent_run_id=RUN_ID,
    )
    kwargs.update(overrides)
    await UsageService(session).log_request(**kwargs)


async def _rows(session):
    from sqlalchemy import select

    return (await session.execute(select(UsageLog))).scalars().all()


async def test_writer_persists_the_address_on_the_row(db_session):
    """THE production-writer guard.

    A reader-only implementation of the cost story cannot satisfy this: it asserts
    the column is populated by the shared writer that both proxies actually call.
    That is the specific regression #4898 exists to correct — the read side was
    fully built and shipped against a column no writer ever set. Keep this test.
    """
    await _log(db_session, _attributed_context())
    rows = await _rows(db_session)
    assert len(rows) == 1
    assert rows[0].graph_address == ADDRESS
    assert rows[0].org_id == "tenant"


async def test_unattributed_call_still_persists_its_usage_row(db_session):
    """Ordinary metering is preserved when attribution is unavailable.

    The row must exist with its real tokens and cost; only the address is NULL. A
    missing row would be unmetered spend.
    """
    await _log(db_session, _context(), agent_run_id=None)
    rows = await _rows(db_session)
    assert len(rows) == 1
    assert rows[0].graph_address is None
    assert (rows[0].input_tokens, rows[0].output_tokens) == (100, 50)
    assert rows[0].cost_usd == Decimal("0.001500")


async def test_repeated_submissions_at_one_address_each_persist(db_session):
    """Retries are real spend and must sum — no deduplication is introduced.

    The cost query sums rows per address, so two genuine submissions at one node
    must produce two rows. Collapsing them would under-report.
    """
    context = _attributed_context()
    await _log(db_session, context, request_id="req-1")
    await _log(db_session, context, request_id="req-2")
    rows = await _rows(db_session)
    assert len(rows) == 2
    assert [row.graph_address for row in rows] == [ADDRESS, ADDRESS]
    assert sum(row.cost_usd for row in rows) == Decimal("0.003000")


async def test_measured_spend_reaches_the_existing_cost_readback(db_session):
    """End-to-end: written by the real writer, read by the shipped cost query.

    No hand-seeded addressed rows — the row under test is produced by
    `log_request`, which is what both proxies call. Before this change the same
    query returned `unknown` / `no_usage_rows` for exactly this input.
    """
    from src.orchestration.cost import CostStatus, get_cost_by_address

    await _log(db_session, _attributed_context())
    await db_session.commit()

    costs = await get_cost_by_address(db_session, org_id="tenant", address_prefix="delivery-loop")
    assert len(costs) == 1
    assert costs[0].address == ADDRESS
    assert costs[0].status is CostStatus.KNOWN
    assert costs[0].amount_usd == Decimal("0.001500")
    assert costs[0].call_count == 1
    assert costs[0].total_tokens == 150


async def test_another_tenants_query_cannot_see_the_address(db_session):
    """Attribution does not leak across tenants: the readback is org-scoped in SQL."""
    from src.orchestration.cost import get_cost_by_address

    await _log(db_session, _attributed_context())
    await db_session.commit()

    assert await get_cost_by_address(db_session, org_id="other-tenant", address_prefix="delivery-loop") == []


async def test_mixed_provider_sequence_at_one_node_sums_into_one_total(db_session):
    """A Codex/Responses delegation must land in the SAME graph total as Claude.

    Both production callers (`proxy/service.py` and `proxy/mantle_service.py`) go
    through this one writer with the same request context, so a node that used both
    providers reports one combined figure. Attributing only Bedrock would make such
    a node report a confidently PARTIAL cost — which reads as authoritative and is
    worse than `unknown`.
    """
    from src.orchestration.cost import CostStatus, get_cost_by_address

    context = _attributed_context()
    await _log(db_session, context, model="claude-sonnet-4", cost_usd=Decimal("0.002000"), request_id="bedrock-1")
    await _log(db_session, context, model="gpt-5-codex", cost_usd=Decimal("0.000500"), request_id="mantle-1")
    await db_session.commit()

    costs = await get_cost_by_address(db_session, org_id="tenant", address_prefix=ADDRESS)
    assert len(costs) == 1
    assert costs[0].status is CostStatus.KNOWN
    assert costs[0].amount_usd == Decimal("0.002500")
    assert costs[0].call_count == 2


async def test_unattributed_rows_leave_the_flow_unknown_not_zero(db_session):
    """Absent attribution stays absent from the rollup — never a measured zero.

    "No rows" and "measured zero" are different answers, and preserving that
    distinction is what makes the cost report honest.
    """
    from src.orchestration.cost import get_cost_by_address

    await _log(db_session, _context(), agent_run_id=None)
    await db_session.commit()

    assert await get_cost_by_address(db_session, org_id="tenant", address_prefix="delivery-loop") == []
