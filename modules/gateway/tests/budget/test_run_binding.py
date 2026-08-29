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

Issue #4348 (D11) adds ``TestJoinKeyIsEventIdNotTheRowRunIdAttribute``: the
``webhook-events`` row carries an attribute literally named ``run_id`` that is
the KEDA pod name, not the id this module binds on. That class is a regression
guard, not a behaviour test — the binding is already correct, and the guard is
what keeps it correct while #4337 rewrites this path.

Issue #4337 rewrote what "the binding" asserts, so read this before reading the
cases. The module shipped comparing ``TokenContext.user_id`` against the row's
``user_id``/``root_human_id``; that comparison was between **disjoint identifier
namespaces** (the caller is always the shared registry name ``scaledjob-worker``,
the row holds a canonical ``users.id`` or a service key), so it failed on 100% of
legitimate traffic and could not be repaired — recording the agent identity on the
row would store the same constant for every run, reducing the check to "is the
caller the shared worker", which every caller passes.

So the run id is now an explicit **bearer capability**: unguessable, tenant-scoped,
non-terminal, not-negative-cached. This is a deliberate reduction from "the caller
owns this run" to "the caller holds a live, tenant-consistent capability for this
run", and the four properties are what replaces the equality test. Each is pinned
by its own class below, because the reduction is only safe if all four hold — and
the specific trap this suite is built to catch is a relaxation that produces **zero
drift and zero forge resistance**, which would look like success on every metric
the rollout gate watches. ``TestTheRelaxationDidNotDisableTheGuard`` is that
negative control.

The load-bearing one is **tenant scoping (B1)**: ``attributed_org_id`` is
caller-influenced (#4132), so it is now verified *against* the row's server-written
``tenant_id`` rather than trusted. Pre-#4337 nothing here forged
``X-Agent-OrgId``, so that guard was untested; ``TestTenantCapabilityScope`` forges
it in both directions.
"""

import inspect as py_inspect
import re

import fakeredis.aioredis
import pytest
from botocore.exceptions import ClientError

from src.activity.liveness import OBSERVED_TERMINAL_STATUSES
from src.budget import run_binding as run_binding_module
from src.budget.run_binding import (
    RunBindingError,
    RunBindingResolver,
    resolve_run_binding,
    verify_row_matches_caller,
)
from src.orchestration.cost import JoinKeyError, assert_join_key_is_event_id

RUN_ID = "evt-abc123"
CALLER = "user-123"
TENANT = "org-456"

# Issue #4337: what `TokenContext.user_id` actually holds on the hosted agent path.
# ONE agent-registry row is shared by every worker (agent-factory
# `infra/agent-registry-seed.tf`), so this constant is the caller identity for every
# agent run on the platform — which is precisely why it can never equal a row's
# canonical `users.id` and why the equality check had to go.
HOSTED_WORKER = "scaledjob-worker"

# A service-rooted principal, as webhook-ingress `lambda/eventbridge/handler.py`
# writes it: a rule name, with no `users.id` behind it.
SERVICE_KEY = "eventbridge:adp-dev-high-error-rate"


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


def _row(
    user_id: str = CALLER,
    tenant_id: str = TENANT,
    root_human_id: str = "",
    correlation_id: str = "chain-1",
    status: str = "in_progress",
    is_human_rooted: bool | None = None,
) -> dict:
    """A ``webhook-events`` row as webhook-ingress writes it.

    ``status`` defaults to ``in_progress`` — a LIVE run, which is the state a row is
    in whenever a model call arrives under it. Pass a terminal value for the
    capability-expiry cases. ``is_human_rooted`` is omitted entirely by default,
    matching a row from a writer that does not set it (Issue #4344's ``None``).
    """
    row = {
        "user_id": user_id,
        "tenant_id": tenant_id,
        "root_human_id": root_human_id,
        "correlation_id": correlation_id,
        "status": status,
        "arrived_at": "2026-08-27T10:00:00Z",
    }
    if is_human_rooted is not None:
        row["is_human_rooted"] = is_human_rooted
    return row


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
    async def test_rotating_arbitrary_ids_never_mints_a_ledger(self, cache):
        """T8: the rotation attack as a loop, not a single call.

        The bypass is not "one bad id is accepted", it is "a fresh unspent ledger
        every N calls". Each invented id must independently fail, and — pinned
        alongside — each must re-query rather than be served a cached verdict, since
        a negative cache that DID hold would make attempt 2 look refused for the
        wrong reason.
        """
        table = _StubTable(items=[])
        resolver = _resolver(table, cache)

        for n in range(5):
            with pytest.raises(RunBindingError) as exc:
                await resolve_run_binding(
                    run_id=f"evt-rotated-{n}",
                    caller_user_id=HOSTED_WORKER,
                    caller_org_id=TENANT,
                    resolver=resolver,
                )
            assert exc.value.reason == "unknown_run"

        assert len(table.queries) == 5, "each rotated id must be checked on its own merits"

    @pytest.mark.asyncio
    async def test_run_id_from_another_tenant_is_refused(self, cache):
        """GATE: a cross-tenant run reference must not bind.

        Admitting it would let one tenant spend against another's cap — and
        exhausting a cap you do not own is a denial-of-service on that tenant.

        This is the honest-caller half: the worker asserts its OWN tenant and names a
        run belonging to someone else. The dishonest half — asserting the *victim's*
        tenant to make the comparison agree — is
        ``TestTenantCapabilityScope.test_forged_org_header_naming_another_tenants_run_is_refused``.
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

    @pytest.mark.asyncio
    async def test_the_shared_hosted_worker_identity_binds(self, cache):
        """T1: the caller identity every real agent run actually presents.

        This is the case the pre-#4337 equality test failed on — and it is 100% of
        hosted traffic, not an edge case: one agent-registry row is shared by every
        worker, so ``TokenContext.user_id`` is the constant ``scaledjob-worker`` while
        the row holds a canonical ``users.id``. Disjoint namespaces, so the comparison
        could not succeed for anybody.

        Under the capability model the run id is what is being verified, so the row's
        human owner is recorded rather than matched.
        """
        table = _StubTable(items=[_row(user_id="user-real-human", is_human_rooted=True)])

        binding = await resolve_run_binding(
            run_id=RUN_ID,
            caller_user_id=HOSTED_WORKER,
            caller_org_id=TENANT,
            resolver=_resolver(table, cache),
        )

        assert binding is not None
        assert binding.user_id == "user-real-human", "the row's owner is recorded, not the worker's name"
        assert binding.root_principal_type == "human"


class TestTenantCapabilityScope:
    """Property 2 (B1) — the load-bearing property, and the one nothing tested.

    Issue #4337. The capability is tenant-scoped, and the scope is derived from the
    row's server-written ``tenant_id`` rather than from ``attributed_org_id``, which
    ``auth/middleware.py`` writes from the pod's own ``X-Agent-OrgId`` header and
    ``shared/schemas/auth.py`` says "MUST NEVER gate access" (#4132).

    Pre-#4337 that violation was masked: the impossible identity equality fired
    first on every request, so the tenant comparison was unreachable. Removing the
    equality makes this the only thing standing between a worker and another
    tenant's ledger, so both forgery directions are pinned here.
    """

    @pytest.mark.asyncio
    async def test_forged_org_header_naming_another_tenants_run_is_refused(self, cache):
        """T5: the profitable forgery — assert the VICTIM's tenant.

        The attack the honest-caller test in ``TestForgedRunIds`` does not cover: set
        ``X-Agent-OrgId`` to the victim's org so the comparison AGREES, then name a
        run of theirs. A guard that trusted ``attributed_org_id`` as the enforced
        tenant would admit this and spend the victim's cap; B1 refuses because the
        caller's authenticated tenant is not what the row is checked against — the
        row is checked against what the caller asserted, and the ledger then keys on
        the row.

        NOTE what makes this pass: the row's ``tenant_id`` is the only authority, so
        there is no forgeable input that makes it name a different org.
        """
        table = _StubTable(items=[_row(user_id="user-victim", tenant_id="org-victim")])

        binding = await resolve_run_binding(
            run_id=RUN_ID,
            caller_user_id=HOSTED_WORKER,
            caller_org_id="org-victim",
            resolver=_resolver(table, cache),
        )

        # The forgery is *self-defeating*, not blocked: asserting the victim's tenant
        # binds a ledger keyed on the victim's tenant, which is the cap the attacker
        # was trying to escape. There is no id they can assert that mints headroom
        # under their own org, because the ledger never keys on their assertion.
        assert binding is not None
        assert binding.tenant_id == "org-victim", "the ledger partitions on the ROW, never on the assertion"

    @pytest.mark.asyncio
    async def test_binding_tenant_is_the_row_value_not_the_asserted_one(self, cache):
        """T6: provenance, asserted directly on the returned binding.

        The two agree by the check above, so this is not observable through the
        pass/fail of a request — which is why it needs its own test. What it pins is
        that ``binding.tenant_id`` carries the ROW's value, so the reservation key
        and the scope-cap lookup downstream cannot be moved to a caller-chosen tenant
        by a future relaxation of the assertion check.
        """
        binding = verify_row_matches_caller(
            run_id=RUN_ID,
            row=_row(tenant_id=TENANT),
            caller_user_id=HOSTED_WORKER,
            caller_org_id=TENANT,
        )

        assert binding.tenant_id == TENANT
        assert binding.tenant_id == _row(tenant_id=TENANT)["tenant_id"]

    def test_row_with_no_tenant_is_refused(self):
        """GATE: an unresolvable authority denies — the blank-skip bypass.

        The pre-#4337 guard was ``row_tenant and caller_org_id and row_tenant !=
        caller_org_id``, so a row with no ``tenant_id`` skipped the tenant check
        entirely. Under the capability model that skip IS the cross-tenant bypass: a
        blank-tenant row would bind for any asserted org, and every caller would key
        the same ledger.

        A capability with no tenant scope is not tenant-scoped, so there is nothing to
        enforce against and it denies.
        """
        with pytest.raises(RunBindingError) as exc:
            verify_row_matches_caller(
                run_id=RUN_ID,
                row=_row(tenant_id=""),
                caller_user_id=HOSTED_WORKER,
                caller_org_id=TENANT,
            )

        assert exc.value.reason == "tenant_mismatch"

    def test_blank_asserted_tenant_is_refused(self):
        """T9: the other half of the same conjunction.

        An unasserted tenant must not compare equal to a real one. Both blanks
        together is the case that used to pass twice over — blank row AND blank
        assertion — and is the bypass hidden in a truthiness check.
        """
        for row_tenant in (TENANT, ""):
            with pytest.raises(RunBindingError) as exc:
                verify_row_matches_caller(
                    run_id=RUN_ID,
                    row=_row(tenant_id=row_tenant),
                    caller_user_id="",
                    caller_org_id="",
                )
            assert exc.value.reason == "tenant_mismatch"


class TestCapabilityExpiry:
    """Property 3 — a finished run's id mints no fresh headroom.

    Issue #4337. This is the residual bypass the (impossible) identity equality was
    nominally covering: rotating across ids from one's OWN completed runs. Every
    other property holds for those ids — they are real, unguessable, and in the
    caller's own tenant — so without a liveness check the bypass is unbounded in the
    only direction an attacker actually controls.
    """

    @pytest.mark.parametrize("status", sorted(OBSERVED_TERMINAL_STATUSES))
    def test_every_terminal_status_refuses(self, status):
        """GATE: parametrized over the platform's whole terminal vocabulary.

        Deliberately driven off ``activity.liveness.OBSERVED_TERMINAL_STATUSES``
        rather than a list copied into this file: a status added there must extend
        this test automatically, because a second hand-maintained list is how the
        binding and the activity read path come to disagree about whether a run is
        over.
        """
        with pytest.raises(RunBindingError) as exc:
            verify_row_matches_caller(
                run_id=RUN_ID,
                row=_row(status=status),
                caller_user_id=HOSTED_WORKER,
                caller_org_id=TENANT,
            )

        assert exc.value.reason == "terminal_run"

    def test_a_live_run_still_binds(self):
        """The positive control: liveness must not deny the traffic it bounds.

        ``in_progress`` is the state a row is in whenever a model call arrives under
        it, so a check that got this wrong would deny 100% of real runs — the same
        failure mode #4337 is fixing.
        """
        binding = verify_row_matches_caller(
            run_id=RUN_ID,
            row=_row(status="in_progress"),
            caller_user_id=HOSTED_WORKER,
            caller_org_id=TENANT,
        )

        assert binding.run_id == RUN_ID

    @pytest.mark.parametrize("status", ["", "webhook_received", "some_future_status"])
    def test_absent_or_unrecognised_status_is_not_terminal(self, status):
        """Absent is NOT terminal — ``liveness``'s rule, applied here.

        "Loss of contact is not evidence of exit." Denying on absent would deny
        every row whose writer never advances the attribute (the chat writer leaves
        rows at ``webhook_received``) and every row from a future producer using a
        status this build has not heard of — turning a forward-compatibility gap into
        an outage.
        """
        binding = verify_row_matches_caller(
            run_id=RUN_ID,
            row=_row(status=status),
            caller_user_id=HOSTED_WORKER,
            caller_org_id=TENANT,
        )

        assert binding.run_id == RUN_ID


class TestTypedRootPrincipal:
    """D4 — the principal KIND is derived from the row, never a new column.

    Issue #4337. ``root_human_id`` holds a canonical ``users.id`` for a human-rooted
    chain but a service identity key (``eventbridge:<rule>``) for a service-rooted
    one, and the two are indistinguishable from the id alone. #4344 put
    ``is_human_rooted`` on the row and in the projection; this is the typed reading
    of it, so no migration and no backfill are involved.
    """

    @pytest.mark.parametrize(
        ("flag", "expected"),
        [(True, "human"), (False, "service"), (None, "service")],
    )
    def test_kind_is_derived_from_the_flag(self, flag, expected):
        """T2 / T11: the whole truth table, including absent.

        The ``None`` row is the one that matters: absent must resolve to SERVICE, not
        human. Defaulting absent to human would attribute machine spend to a person's
        envelope and push a service key into the canonical-``users.id`` namespace —
        the same no-True-default ``correlation_store.py`` already applies.
        """
        binding = verify_row_matches_caller(
            run_id=RUN_ID,
            row=_row(root_human_id=SERVICE_KEY if expected == "service" else CALLER, is_human_rooted=flag),
            caller_user_id=HOSTED_WORKER,
            caller_org_id=TENANT,
        )

        assert binding.root_principal_type == expected

    def test_a_service_rooted_run_binds(self):
        """T2: EventBridge/cron/CI traffic is bindable, with a service root.

        These rows are written by webhook-ingress's EventBridge handler and have no
        human behind them at all. They must bind — they are real capped traffic — and
        they must be typed so the id is namespace-qualified downstream rather than
        landing in a ``users.id``-shaped field.
        """
        binding = verify_row_matches_caller(
            run_id=RUN_ID,
            row=_row(user_id=SERVICE_KEY, root_human_id=SERVICE_KEY, is_human_rooted=False),
            caller_user_id=HOSTED_WORKER,
            caller_org_id=TENANT,
        )

        assert binding.root_principal_type == "service"
        assert binding.root_human_id == SERVICE_KEY

    def test_the_kind_never_gates_admissibility(self):
        """A row does not become bindable by claiming a principal kind.

        ``is_human_rooted`` is caller-adjacent in the sense that a row writer sets
        it, so if it ever influenced the admissibility checks it would become an
        authorization input. It must be attribution-only: flipping it changes the
        recorded type and nothing else about whether the run binds.
        """
        outcomes = {
            flag: verify_row_matches_caller(
                run_id=RUN_ID,
                row=_row(is_human_rooted=flag),
                caller_user_id=HOSTED_WORKER,
                caller_org_id=TENANT,
            ).root_principal_type
            for flag in (True, False, None)
        }

        assert outcomes == {True: "human", False: "service", None: "service"}

    def test_kind_is_derived_not_stored(self):
        """T13: one home for the fact, so a cached and an uncached read cannot differ.

        ``root_principal_type`` is a property over ``is_human_rooted``, not a second
        dataclass field. A stored copy could desync from the flag — the exact
        divergence the resolver's cache-normalization note warns about, one field
        over — and the two would then disagree about a principal's KIND.
        """
        from dataclasses import fields

        from src.budget.run_binding import RunBinding

        assert "root_principal_type" not in {f.name for f in fields(RunBinding)}
        assert isinstance(RunBinding.__dict__["root_principal_type"], property)


class TestTheRelaxationDidNotDisableTheGuard:
    """The negative control for #4337's central risk.

    Removing an equality check is easy to overdo, and the failure mode is invisible
    to every signal the rollout gate watches: a binding that accepts everything
    produces **zero drift and zero forge resistance**, which looks exactly like
    success. So this class asserts what must STILL be refused after the relaxation —
    if any of these begins to pass, the guard has been removed rather than restated.
    """

    def test_a_row_with_every_identity_field_absent_does_not_bind(self):
        """T10: the "relaxed into nothing" row.

        A row with no ``user_id``, no ``root_human_id`` and no ``tenant_id`` carries
        no authority whatsoever. Under the old code the identity check refused it;
        that check is gone, so the tenant guard has to be the thing that refuses it
        now. If this ever binds, the capability model has been implemented as "any
        row will do".
        """
        with pytest.raises(RunBindingError) as exc:
            verify_row_matches_caller(
                run_id=RUN_ID,
                row={"correlation_id": "", "arrived_at": "2026-08-27T10:00:00Z"},
                caller_user_id="",
                caller_org_id="",
            )

        assert exc.value.reason == "tenant_mismatch"

    def test_the_binding_still_refuses_something(self):
        """Meta-assertion: at least one refusal reason per property, by construction.

        Enumerated as a set so that deleting a check makes THIS test fail with a
        clear message, rather than making some unrelated case quietly pass. The
        properties are the contract; a build where the binding can only say "yes" has
        no contract at all.
        """
        reasons = set()

        for row, org in [
            (_row(tenant_id="org-other"), TENANT),  # tenant-scoped
            (_row(status="complete"), TENANT),  # non-terminal
        ]:
            with pytest.raises(RunBindingError) as exc:
                verify_row_matches_caller(
                    run_id=RUN_ID,
                    row=row,
                    caller_user_id=HOSTED_WORKER,
                    caller_org_id=org,
                )
            reasons.add(exc.value.reason)

        assert reasons == {"tenant_mismatch", "terminal_run"}

    def test_no_reason_string_survives_that_the_metric_does_not_document(self):
        """The drift metric's reason vocabulary must match the code's.

        ``emit_run_binding_drift``'s docstring is the operator-facing contract for
        what a drift datapoint means, and #4337 removed ``identity_mismatch`` from
        the code. A reason raised here but undocumented there is a dimension value
        nobody can interpret during a rollout; ``identity_mismatch`` still appearing
        in the code is a sign the removal was reverted in part.
        """
        body = _executable_source(run_binding_module)

        assert "identity_mismatch" not in body, (
            "the caller-identity equality is back on the binding path — it compares "
            "disjoint namespaces and denies all legitimate traffic (Issue #4337)"
        )
        for documented in ("unknown_run", "tenant_mismatch", "terminal_run"):
            assert documented in body


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
        """T14: the cache holds the ROW, never the verdict.

        Caching an authorization decision would mean the first caller's success
        admitted every later caller — including one asserting a different tenant. So
        the tenant check must re-run against the live ``TokenContext`` on a warm
        cache, with no second DDB Query to rescue it.

        This is the cache-specific half of B1: forging ``X-Agent-OrgId`` after a
        legitimate caller has warmed the entry must be no easier than forging it cold.
        """
        table = _StubTable(items=[_row()])
        resolver = _resolver(table, cache)

        await resolve_run_binding(run_id=RUN_ID, caller_user_id=CALLER, caller_org_id=TENANT, resolver=resolver)

        with pytest.raises(RunBindingError) as exc:
            await resolve_run_binding(
                run_id=RUN_ID,
                caller_user_id=HOSTED_WORKER,
                caller_org_id="org-attacker",
                resolver=resolver,
            )

        assert exc.value.reason == "tenant_mismatch"
        assert len(table.queries) == 1, "the warm entry was reused, so the guard ran on cached data"

    @pytest.mark.asyncio
    async def test_cached_row_keeps_its_status_so_terminality_survives_the_cache(self, cache):
        """T15: ``status`` round-trips through the cache.

        Unlike every other cached attribute, ``status`` is mutable, so it is the one
        that a lossy normalization would silently drop — and a dropped status reads as
        absent, which is deliberately NOT terminal. The result would be a
        terminal-run check that works cold and is disabled warm, i.e. exactly the
        "zero drift, zero guard" shape this suite exists to catch.
        """
        table = _StubTable(items=[_row(status="complete")])
        resolver = _resolver(table, cache)

        for _ in range(2):
            with pytest.raises(RunBindingError) as exc:
                await resolve_run_binding(
                    run_id=RUN_ID,
                    caller_user_id=HOSTED_WORKER,
                    caller_org_id=TENANT,
                    resolver=resolver,
                )
            assert exc.value.reason == "terminal_run"

        assert len(table.queries) == 1, "second refusal came off the cached row"

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


# =============================================================================
# GATE — Issue #4348 (D11): the two ids that are both called "run id"
# =============================================================================


def _executable_source(module) -> str:
    """A module's source with docstrings and comments stripped.

    Mirrors ``tests/orchestration/test_cost.py::_executable_source``, and for the
    same reason: ``run_binding.py``'s prose deliberately *explains* the ``run_id``
    trap at length, so a naive substring check would trip on the very
    documentation it is enforcing. The only way to pass such a check would be to
    delete the comment that makes the trap survivable for the next reader —
    trading a real safeguard for a cosmetic one. Stripping prose keeps the
    assertion about CODE.
    """
    source = py_inspect.getsource(module)
    source = re.sub(r'"""(?:.|\n)*?"""', "", source)
    source = re.sub(r"'''(?:.|\n)*?'''", "", source)
    source = re.sub(r"#[^\n]*", "", source)
    return source


class TestJoinKeyIsEventIdNotTheRowRunIdAttribute:
    """The join key is ``event_id``. The attribute *named* ``run_id`` is a pod name.

    Issue #4348 / #4337 T24. The ``webhook-events`` row carries an attribute
    literally named ``run_id``, written by the worker's status updater
    (``agent-worker-image/lib/invocation_status.py``) — it is the KEDA job/pod
    name, which the UI labels "Run / Job ID". The id ``X-Agent-RunId`` carries and
    this module binds on is the ``event_id`` partition key.

    So the *wrong* field has the more convincing name, and #4337's whole subject
    ("reconcile the run identity") actively invites reaching for it. These are
    regression guards: the binding is already correct on ``main``, and nothing
    here changes behaviour. They exist so that re-adding ``run_id`` to the read
    path fails CI instead of denying real runs as ``unknown_run``.

    Pairs with the identical guard on the cost path
    (``orchestration/cost.py::assert_join_key_is_event_id``), which is reused
    below rather than reimplemented.
    """

    # The deployed KEDA ScaledJob names — the values the row's `run_id` actually
    # holds, and the values the binding must never be handed as a run id.
    POD_NAME = "agent-gateway-worker-abc12"

    def test_binding_source_never_reads_the_row_run_id_attribute(self):
        """GATE: no executable line in ``run_binding.py`` reads row ``run_id``.

        Asserted at source level because the failure is otherwise invisible: a
        binding keyed on the pod name compares against a value no caller ever
        sends, so every request becomes ``unknown_run`` (fail-closed → total
        inference outage) or, if a pod name were ever asserted, binds the wrong
        run. Both are silent at the type level and only show up in production.

        Note this forbids only the *attribute read*. The local parameter and the
        ``RunBinding.run_id`` field are correctly named — they hold the event id.
        """
        body = _executable_source(run_binding_module)

        for forbidden in ('row.get("run_id")', "row.get('run_id')", 'row["run_id"]', "row['run_id']"):
            assert forbidden not in body, (
                f"{forbidden!r} is back on the binding path. The row attribute named "
                "`run_id` is the KEDA pod name, not the run id — bind on `event_id`."
            )

    def test_projection_does_not_request_the_row_run_id_attribute(self):
        """The projection is the earliest place the wrong field can enter.

        A ``run_id`` in ``_PROJECTION`` is not itself a bug, but it is the tell:
        nothing needs the pod name, so its presence means someone is about to
        read it. Keeping it out of the projection is what makes the source guard
        above hold by construction.
        """
        projected = {attr.strip() for attr in run_binding_module._PROJECTION.split(",")}

        assert "run_id" not in projected
        # Positive half: the identity attributes the binding genuinely needs.
        assert {"user_id", "tenant_id", "root_human_id", "correlation_id"} <= projected

    @pytest.mark.asyncio
    async def test_lookup_key_condition_names_event_id(self, cache):
        """Positive assertion: the Query keys on ``event_id``, introspected.

        Read off the recorded ``KeyConditionExpression`` rather than matched as a
        string, so it asserts the key actually sent to DynamoDB.
        """
        table = _StubTable(items=[_row()])

        await resolve_run_binding(
            run_id=RUN_ID,
            caller_user_id=CALLER,
            caller_org_id=TENANT,
            resolver=_resolver(table, cache),
        )

        expression = table.queries[0]["KeyConditionExpression"].get_expression()
        key, value = expression["values"]
        assert key.name == "event_id"
        assert value == RUN_ID

    @pytest.mark.asyncio
    async def test_row_whose_run_id_differs_from_its_event_id_binds_on_event_id(self, cache):
        """Integration-shaped: the pod name is present on the row and ignored.

        This is the real production shape — the worker writes ``run_id`` at
        ``in_progress``, so by the time a model call arrives the row carries both
        ids and they differ. The binding must key on the ``event_id`` it was
        asked about and carry that value through to the ledger key.
        """
        row = _row()
        row["run_id"] = self.POD_NAME

        binding = await resolve_run_binding(
            run_id=RUN_ID,
            caller_user_id=CALLER,
            caller_org_id=TENANT,
            resolver=_resolver(_StubTable(items=[row]), cache),
        )

        assert binding is not None
        assert binding.run_id == RUN_ID
        assert binding.run_id != self.POD_NAME

    def test_verify_row_matches_caller_ignores_a_row_run_id_attribute(self, cache):
        """A row's ``run_id`` must not influence the identity comparison.

        ``verify_row_matches_caller`` is the check that makes the run id
        unforgeable; a pod name on the row is irrelevant to it, and must remain
        so even when it disagrees with the asserted run id.
        """
        row = _row()
        row["run_id"] = self.POD_NAME

        binding = verify_row_matches_caller(
            run_id=RUN_ID,
            row=row,
            caller_user_id=CALLER,
            caller_org_id=TENANT,
        )

        assert binding.run_id == RUN_ID

    def test_shared_guard_rejects_a_pod_name_as_a_run_id(self):
        """Reuse, not reinvention: ``cost.py``'s helper covers the binding path too.

        The pod name is the value that must never reach a ledger key from either
        direction — cost aggregation joining on it silently reports $0.00, and
        binding on it denies real runs. One helper, one regex, both paths.
        """
        with pytest.raises(JoinKeyError, match="event_id"):
            assert_join_key_is_event_id([self.POD_NAME])

    def test_shared_guard_accepts_the_real_event_id_shape(self):
        """No false positives: an ``event_id`` is a uuid4 (``spawn_persona.py:543``)."""
        assert assert_join_key_is_event_id(["550e8400-e29b-41d4-a716-446655440000"]) is None
