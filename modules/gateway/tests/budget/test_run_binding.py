"""Server-side run identity binding — Issue #4187.

``X-Agent-RunId`` is a client-written header that nothing validated. A cap keyed
on it is not a cap: rotate the value and you mint a fresh unspent ledger. That is
the same defect #3985 removed for ``X-Agent-BudgetConfigId``, and these tests are
the ones that keep it removed.

What is asserted here is the *binding*, i.e. which asserted run ids the gateway is
willing to key a ledger on. ``test_run_spend_cap.py`` asserts the resulting
denial. The split matters: a binding that accepts anything would still make the
cap tests pass right up until someone forged a header.

Harness notes, per the #4068 gate:

* Real ``fakeredis`` for the cache, so the cache-hit path is exercised rather
  than assumed.
* A stub DDB table that records its calls, so "``Query``, not ``GetItem``" (the
  #3376 composite-key lesson) is asserted rather than hoped for.
* No ``MagicMock`` configs — nothing here reads config.
"""

import fakeredis.aioredis
import pytest
from botocore.exceptions import ClientError

from src.budget.run_binding import (
    RunBindingError,
    RunBindingResolver,
    resolve_run_binding,
    verify_row_matches_caller,
)

RUN_ID = "evt-abc123"
CALLER = "user-123"
TENANT = "org-456"


class _StubTable:
    """A DDB table that returns fixed rows and records how it was called."""

    def __init__(self, items: list[dict] | None = None, error: Exception | None = None):
        self._items = items if items is not None else []
        self._error = error
        self.queries: list[dict] = []

    def query(self, **kwargs):
        self.queries.append(kwargs)
        if self._error:
            raise self._error
        return {"Items": self._items}

    def get_item(self, **kwargs):  # pragma: no cover - must never be called
        raise AssertionError("GetItem cannot work on a composite-key table (#3376) — use Query")


def _row(user_id: str = CALLER, tenant_id: str = TENANT, root_human_id: str = "", correlation_id: str = "chain-1") -> dict:
    return {
        "user_id": user_id,
        "tenant_id": tenant_id,
        "root_human_id": root_human_id,
        "correlation_id": correlation_id,
        "arrived_at": "2026-08-27T10:00:00Z",
    }


@pytest.fixture
def cache():
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


def _resolver(table: _StubTable, cache=None) -> RunBindingResolver:
    return RunBindingResolver(
        table_name="webhook-events",
        aws_region="us-east-1",
        client=cache,
        table=table,
    )


# =============================================================================
# GATE — the forgery cases
# =============================================================================


class TestForgedRunIds:
    """A run id the caller does not own must not resolve to a ledger."""

    @pytest.mark.asyncio
    async def test_unknown_run_id_is_refused(self, cache):
        """GATE: a run id with no registry row is a denial, not "no cap applies".

        This is the rotation attack in its simplest form: invent a run id nobody
        has ever seen. Pre-fix the header was taken at face value, so this minted
        a fresh unspent ledger every time it changed.
        """
        table = _StubTable(items=[])

        with pytest.raises(RunBindingError) as exc:
            await resolve_run_binding(
                run_id="evt-invented",
                caller_user_id=CALLER,
                caller_org_id=TENANT,
                resolver=_resolver(table, cache),
            )

        assert exc.value.reason == "unknown_run"

    @pytest.mark.asyncio
    async def test_run_id_belonging_to_another_user_is_refused(self, cache):
        """GATE: naming someone else's real run must not bind.

        The subtler rotation attack: a valid run id, so a lookup-only check
        succeeds. Only comparing the row against the authenticated caller catches
        it — which is why the identity assertion is not optional.
        """
        table = _StubTable(items=[_row(user_id="user-999")])

        with pytest.raises(RunBindingError) as exc:
            await resolve_run_binding(
                run_id=RUN_ID,
                caller_user_id=CALLER,
                caller_org_id=TENANT,
                resolver=_resolver(table, cache),
            )

        assert exc.value.reason == "identity_mismatch"

    @pytest.mark.asyncio
    async def test_run_id_from_another_tenant_is_refused(self, cache):
        """GATE: a cross-tenant run reference must not bind.

        Admitting it would let one tenant spend against another's cap — and
        exhausting a cap you do not own is a denial-of-service on that tenant.
        """
        table = _StubTable(items=[_row(user_id=CALLER, tenant_id="org-other")])

        with pytest.raises(RunBindingError) as exc:
            await resolve_run_binding(
                run_id=RUN_ID,
                caller_user_id=CALLER,
                caller_org_id=TENANT,
                resolver=_resolver(table, cache),
            )

        assert exc.value.reason == "tenant_mismatch"


class TestLegitimateBindings:
    """The cases that must keep working, or the cap breaks real traffic."""

    @pytest.mark.asyncio
    async def test_own_run_binds_and_carries_the_server_side_chain_id(self, cache):
        """The chain id comes off the ROW, never off the request.

        The worker does send ``x-agent-correlationid``, so binding must be seen to
        ignore it: the row's value is what the chain ledger keys on.
        """
        table = _StubTable(items=[_row(correlation_id="chain-server-side")])

        binding = await resolve_run_binding(
            run_id=RUN_ID,
            caller_user_id=CALLER,
            caller_org_id=TENANT,
            resolver=_resolver(table, cache),
        )

        assert binding is not None
        assert binding.run_id == RUN_ID
        assert binding.correlation_id == "chain-server-side"
        assert binding.tenant_id == TENANT

    @pytest.mark.asyncio
    async def test_agent_spawned_run_binds_via_root_human_id(self, cache):
        """Issue #3705: a chain run carries the bot as user_id, the human as root.

        Rejecting this would deny every agent-spawned run — the cap would take out
        the exact traffic it is meant to bound.
        """
        table = _StubTable(items=[_row(user_id="bot-adp", root_human_id=CALLER)])

        binding = await resolve_run_binding(
            run_id=RUN_ID,
            caller_user_id=CALLER,
            caller_org_id=TENANT,
            resolver=_resolver(table, cache),
        )

        assert binding is not None
        assert binding.root_human_id == CALLER

    def test_empty_caller_identity_never_matches(self):
        """An unauthenticated/blank identity must not match a blank row field.

        Both sides being empty would compare equal, which would turn a missing
        identity into a successful bind — a bypass hidden in a truthiness check.
        """
        with pytest.raises(RunBindingError) as exc:
            verify_row_matches_caller(
                run_id=RUN_ID,
                row=_row(user_id="", root_human_id=""),
                caller_user_id="",
                caller_org_id=TENANT,
            )

        assert exc.value.reason == "identity_mismatch"


class TestLookupMechanics:
    """The composite-key and caching behaviour the binding depends on."""

    @pytest.mark.asyncio
    async def test_lookup_uses_query_not_get_item(self, cache):
        """Issue #3376: ``webhook-events`` is (event_id HASH + arrived_at RANGE).

        ``GetItem`` needs both halves and the caller only has one, so it would
        fail at runtime for every request. ``_StubTable.get_item`` asserts on
        being called at all.
        """
        table = _StubTable(items=[_row()])

        await resolve_run_binding(
            run_id=RUN_ID,
            caller_user_id=CALLER,
            caller_org_id=TENANT,
            resolver=_resolver(table, cache),
        )

        assert len(table.queries) == 1
        # Newest-first + Limit 1: GitHub re-delivery writes several rows under one
        # event_id, and the latest is the one that describes this run.
        assert table.queries[0]["ScanIndexForward"] is False
        assert table.queries[0]["Limit"] == 1

    @pytest.mark.asyncio
    async def test_second_call_is_served_from_cache(self, cache):
        """One DDB Query per run, not per model call.

        A run makes hundreds of calls under one run id. Without the cache this
        binding would add a DDB round trip to every one of them, which is the
        hot-path cost the issue explicitly warns against.
        """
        table = _StubTable(items=[_row()])
        resolver = _resolver(table, cache)

        for _ in range(3):
            await resolve_run_binding(
                run_id=RUN_ID,
                caller_user_id=CALLER,
                caller_org_id=TENANT,
                resolver=resolver,
            )

        assert len(table.queries) == 1, "the row is immutable, so it is read once"

    @pytest.mark.asyncio
    async def test_cached_row_is_still_verified_against_the_caller(self, cache):
        """The cache holds the ROW, never the verdict.

        Caching an authorization decision would mean the first caller's success
        admitted every later caller. The identity comparison must re-run, so a
        different caller hitting a warm cache is still refused.
        """
        table = _StubTable(items=[_row()])
        resolver = _resolver(table, cache)

        await resolve_run_binding(run_id=RUN_ID, caller_user_id=CALLER, caller_org_id=TENANT, resolver=resolver)

        with pytest.raises(RunBindingError) as exc:
            await resolve_run_binding(run_id=RUN_ID, caller_user_id="user-999", caller_org_id=TENANT, resolver=resolver)

        assert exc.value.reason == "identity_mismatch"

    @pytest.mark.asyncio
    async def test_unknown_run_is_not_negatively_cached(self, cache):
        """A miss during the ingress-write race must not pin "unknown" run-long.

        Negative caching would turn a millisecond race into a whole run being
        denied, so the second attempt has to re-query.
        """
        table = _StubTable(items=[])
        resolver = _resolver(table, cache)

        for _ in range(2):
            with pytest.raises(RunBindingError):
                await resolve_run_binding(run_id=RUN_ID, caller_user_id=CALLER, caller_org_id=TENANT, resolver=resolver)

        assert len(table.queries) == 2


class TestFaultVersusForgery:
    """The deliberate asymmetry: a fault degrades, a forgery denies."""

    @pytest.mark.asyncio
    async def test_ddb_fault_degrades_instead_of_denying(self, cache):
        """An unreachable DynamoDB must not take down all inference.

        A caller cannot induce a DDB outage selectively to escape their cap, and
        the hierarchy caps still apply — so denying here buys nothing and costs a
        total outage. Same policy ``reservations.py`` applies to Redis.
        """
        table = _StubTable(error=ClientError({"Error": {"Code": "ProvisionedThroughputExceededException"}}, "Query"))

        binding = await resolve_run_binding(
            run_id=RUN_ID,
            caller_user_id=CALLER,
            caller_org_id=TENANT,
            resolver=_resolver(table, cache),
        )

        assert binding is None, "a fault yields None (degrade), never a binding and never an exception"

    @pytest.mark.asyncio
    async def test_cache_fault_falls_back_to_ddb(self):
        """A broken cache must not break the binding — correctness is in DDB."""

        class _BrokenCache:
            async def get(self, key):
                raise RuntimeError("redis down")

            async def set(self, key, value, ex=None):
                raise RuntimeError("redis down")

        table = _StubTable(items=[_row()])
        binding = await resolve_run_binding(
            run_id=RUN_ID,
            caller_user_id=CALLER,
            caller_org_id=TENANT,
            resolver=_resolver(table, _BrokenCache()),
        )

        assert binding is not None
        assert len(table.queries) == 1


class TestNoCapWithoutABinding:
    """A run id that cannot be bound must never silently become "unlimited"."""

    @pytest.mark.asyncio
    async def test_refusal_is_an_exception_not_a_permissive_none(self, cache):
        """The two failure shapes must be distinguishable at the call site.

        If a forgery also returned ``None``, the caller could not tell it from a
        DDB blip and would degrade — i.e. the forged run id would get no cap,
        which is the whole bypass. This test pins the contract that keeps the two
        apart.
        """
        table = _StubTable(items=[])

        with pytest.raises(RunBindingError):
            await resolve_run_binding(
                run_id=RUN_ID,
                caller_user_id=CALLER,
                caller_org_id=TENANT,
                resolver=_resolver(table, cache),
            )

    def test_binding_fields_are_all_server_side(self):
        """Nothing on a RunBinding may be caller-supplied except the id itself."""
        binding = verify_row_matches_caller(
            run_id=RUN_ID,
            row=_row(user_id=CALLER, root_human_id="human-1", correlation_id="chain-9"),
            caller_user_id=CALLER,
            caller_org_id=TENANT,
        )

        assert (binding.tenant_id, binding.user_id, binding.root_human_id, binding.correlation_id) == (
            TENANT,
            CALLER,
            "human-1",
            "chain-9",
        )


class TestPeriodTypeGuard:
    """A run cap is lifetime-scoped; a calendar period would merge run ledgers."""

    def test_run_period_has_no_calendar_window(self):
        """``get_period_start_end`` must refuse ``PeriodType.RUN``.

        Silently returning today's date would put every run on a given day into
        one reservation key, so run A's spend would exhaust run B's cap — a cap
        that punishes the wrong run and lets the guilty one continue.
        """
        from src.budget.utils import get_period_start_end
        from src.shared.schemas.budget import PeriodType

        with pytest.raises(ValueError, match="lifetime-scoped"):
            get_period_start_end(PeriodType.RUN)

    def test_calendar_periods_still_work(self):
        """Regression: the three real period types are untouched."""
        from datetime import date

        from src.budget.utils import get_period_start_end
        from src.shared.schemas.budget import PeriodType

        start, end = get_period_start_end(PeriodType.DAILY, date(2026, 8, 27))
        assert (start, end) == (date(2026, 8, 27), date(2026, 8, 27))
