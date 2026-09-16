"""Server-side tenant resolution for draft registration (Issue #4597).

Two layers, tested separately because they fail differently:

* :class:`TestResolveDraftTenant` — the resolver itself. Every fail-closed arm, and
  the one property that makes the mechanism safe: the tenant comes off the row and
  nowhere else.
* :class:`TestRouteTenantResolution` — the route. The 403-trap regression, the
  self-defeating cross-tenant property, and the human path staying exactly as it
  was.

The route tests reuse `test_registration.py`'s harness (`app_with_router`,
`client_for`, `gateless_proposal`, ...) rather than restating it: an assertion about
this route made against a *different* app fixture is an assertion about the fixture.
"""

from __future__ import annotations

import inspect as py_inspect
import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import sqlalchemy as sa
from botocore.exceptions import ClientError
from fastapi.testclient import TestClient

from src.admin.access_control import AccessControl
from src.admin.config import AdminRole, Permission
from src.budget.run_binding import RunBindingResolver
from src.orchestration.draft_binding import DraftBindingError, resolve_draft_tenant
from src.orchestration.models import OrchestrationDecision, OrchestrationFlow
from src.orchestration.state import ActorKind
from src.shared.schemas.auth import TokenContext
from tests.orchestration import test_registration as _reg

# The harness this route already has, reused rather than duplicated: an assertion
# about this route made against a *different* app fixture is an assertion about the
# fixture. Rebound as module attributes rather than imported by name because pytest
# collects fixtures from a test module's namespace either way, while `from ... import
# session` shadows the name and trips ruff's F811.
session = _reg.session
app_with_router = _reg.app_with_router
autonomy_default_unset = _reg.autonomy_default_unset

ORG_A = _reg.ORG_A
ORG_B = _reg.ORG_B
gateless_proposal = _reg.gateless_proposal
assert_graph_is_empty = _reg.assert_graph_is_empty
get_access_control_dep = _reg.get_access_control_dep

RUN_ID = "evt-run-4597"
# The tenant on the run's ingress row. A real one, not `__platform__`.
ROW_TENANT = ORG_A
PLATFORM_ORG = "__platform__"
WORKER_USER_ID = "scaledjob-worker"

ROUTE = "/orchestration/flows/drafts"


class _StubTable:
    """A `webhook-events` table returning fixed rows. Same shape as
    `tests/budget/test_run_binding.py`'s, so both binding paths stub alike."""

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


def _row(*, tenant_id: str = ROW_TENANT, status: str = "in_progress", **extra) -> dict:
    """A `webhook-events` row as webhook-ingress writes it at ingress."""
    row = {
        "user_id": "user-123",
        "tenant_id": tenant_id,
        "root_human_id": "user-650f093f",
        "correlation_id": "chain-1",
        "status": status,
        "arrived_at": "2026-09-01T10:00:00Z",
    }
    row.update(extra)
    return row


def _resolver(table: _StubTable) -> RunBindingResolver:
    # `redis_url=None` disables caching, so each test's Query is really made.
    return RunBindingResolver(table_name="webhook-events", aws_region="us-east-1", table=table)


class TestResolveDraftTenant:
    """The resolver: a tenant from server-written state, or a refusal."""

    async def test_the_tenant_comes_off_the_row(self):
        tenant = await resolve_draft_tenant(run_id=RUN_ID, resolver=_resolver(_StubTable([_row()])))

        assert tenant == ROW_TENANT

    async def test_it_binds_on_event_id_not_the_row_run_id_attribute(self):
        """Issue #4348's trap, restated on this surface.

        The row attribute named `run_id` is the KEDA pod name; the id a caller sends
        is the `event_id` partition key. Binding on the wrong one would make every
        legitimate registration `unknown_run`.
        """
        table = _StubTable([_row()])

        await resolve_draft_tenant(run_id=RUN_ID, resolver=_resolver(table))

        condition = table.queries[0]["KeyConditionExpression"]
        assert condition.get_expression()["values"][0].name == "event_id"
        assert condition.get_expression()["values"][1] == RUN_ID

    @pytest.mark.parametrize("blank", [None, "", "   "])
    async def test_a_missing_run_id_is_refused_without_a_lookup(self, blank):
        table = _StubTable([_row()])

        with pytest.raises(DraftBindingError) as exc:
            await resolve_draft_tenant(run_id=blank, resolver=_resolver(table))

        assert exc.value.code == "missing_run_id"
        assert table.queries == [], "a caller with no run id must not cause a lookup at all"

    async def test_an_unknown_run_is_refused(self):
        with pytest.raises(DraftBindingError) as exc:
            await resolve_draft_tenant(run_id="evt-invented", resolver=_resolver(_StubTable([])))

        assert exc.value.code == "unknown_run"

    @pytest.mark.parametrize("tenant", ["", "   ", None])
    async def test_a_row_with_no_tenant_is_refused_not_skipped(self, tenant):
        """The pre-#4337 blank-skip bug, which under a capability model IS the bypass.

        The old tenant guard was a three-way conjunction that silently skipped the
        check when either side was blank. An authority that cannot be read is an
        authority that cannot be enforced, so it denies.
        """
        with pytest.raises(DraftBindingError) as exc:
            await resolve_draft_tenant(run_id=RUN_ID, resolver=_resolver(_StubTable([_row(tenant_id=tenant)])))

        assert exc.value.code == "unbindable_run"

    @pytest.mark.parametrize("status", ["complete", "failed", "budget_stopped", "rejected"])
    async def test_a_terminal_run_is_refused(self, status):
        """A finished run's id establishes no further authority (#4337 property 3).

        Without this, rotating across one's own completed runs is unbounded.
        """
        with pytest.raises(DraftBindingError) as exc:
            await resolve_draft_tenant(run_id=RUN_ID, resolver=_resolver(_StubTable([_row(status=status)])))

        assert exc.value.code == "terminal_run"

    @pytest.mark.parametrize("status", ["", "in_progress", "webhook_received", "some_future_status"])
    async def test_a_non_terminal_or_unrecognised_status_still_binds(self, status):
        """ "Loss of contact is not evidence of exit" — `activity/liveness.py`'s rule.

        Denying on absent would deny every row whose writer never advanced the status
        (the chat writer leaves rows at `webhook_received`) and every row from a
        future producer using a status this build has not heard of.
        """
        assert await resolve_draft_tenant(run_id=RUN_ID, resolver=_resolver(_StubTable([_row(status=status)]))) == ROW_TENANT

    async def test_a_lookup_fault_refuses_rather_than_degrading(self):
        """The one place this differs from `run_binding`, deliberately.

        There, degrading preserves inference platform-wide and the hierarchy caps
        still bound spend. Here the only available degradation is trusting the
        caller's asserted org, which is the whole bypass — and the cost of refusing
        is one warning line in a closing comment.
        """
        fault = ClientError({"Error": {"Code": "InternalServerError"}}, "Query")

        with pytest.raises(DraftBindingError) as exc:
            await resolve_draft_tenant(run_id=RUN_ID, resolver=_resolver(_StubTable(error=fault)))

        assert exc.value.code == "binding_unavailable"

    async def test_it_never_returns_a_tenant_it_was_not_given_by_a_row(self):
        """The property the whole design rests on, asserted directly.

        No input other than the row can produce a tenant: there is no argument for
        one and no fallback to one. A regression here would most likely arrive as a
        well-meaning `except ...: return caller_org`.
        """
        source = _executable_source(py_inspect.getmodule(resolve_draft_tenant))

        for forbidden in ("attributed_org_id", "root_human_id", "X-Agent-OrgId", "user_identities"):
            assert forbidden not in source, f"{forbidden!r} is on the draft-binding path; the tenant must come only from the row."


class TestRouteTenantResolution:
    """`POST /orchestration/flows/drafts` — which tenant the rows land in."""

    async def test_an_internal_caller_registers_into_the_runs_tenant(self, session, app_with_router):
        """The issue's headline: a 201, owned by the tenant, not by `__platform__`."""
        client = _internal_client(app_with_router, table=_StubTable([_row()]))

        response = client.post(ROUTE, json=gateless_proposal(org_id=ROW_TENANT).model_dump(mode="json"), headers={"X-Agent-RunId": RUN_ID})

        assert response.status_code == 201, response.text
        flow = (await session.execute(sa.select(OrchestrationFlow))).scalar_one()
        assert flow.org_id == ROW_TENANT, "the flow must be owned by the tenant, not the platform"

    async def test_the_decision_records_the_service_principal_not_a_human(self, session, app_with_router):
        """`genesis.py` sets `root_human_id=decision.actor_id`, so a human's id here
        would manufacture a dispatch-rootable decision attributed to a human who
        approved nothing. And `actor_kind` must stay SERVICE."""
        client = _internal_client(app_with_router, table=_StubTable([_row()]))

        client.post(ROUTE, json=gateless_proposal(org_id=ROW_TENANT).model_dump(mode="json"), headers={"X-Agent-RunId": RUN_ID})

        row = (await session.execute(sa.select(OrchestrationDecision))).scalar_one()
        assert row.actor_kind == ActorKind.SERVICE.value
        assert row.actor_id == WORKER_USER_ID
        assert row.actor_id != "user-650f093f", "the run's root human must never be the actor"

    async def test_the_permission_check_still_uses_the_authenticated_org(self, session, app_with_router):
        """THE 403-TRAP REGRESSION. The test that stops this fix returning 403.

        `PLAN_DRAFT` is org-scoped and the worker's `allowed_org_id` falls back to its
        own `__platform__`, so passing the run's tenant as `target_org_id` raises
        `InvalidScopeError` — trading a 422 for a 403 and leaving the feature inert.
        The two values disagree by design; this pins that they do.
        """
        client = _internal_client(app_with_router, table=_StubTable([_row()]))

        client.post(ROUTE, json=gateless_proposal(org_id=ROW_TENANT).model_dump(mode="json"), headers={"X-Agent-RunId": RUN_ID})

        access = app_with_router.dependency_overrides[get_access_control_dep()]()
        called = access.check_permission.await_args
        assert called.args[1] is Permission.PLAN_DRAFT
        assert called.kwargs["target_org_id"] == PLATFORM_ORG, (
            "the permission check must stay on the AUTHENTICATED identity. Passing the "
            "run's tenant here trips the org-scope arm and returns 403, not 201."
        )

    async def test_a_borrowed_cross_tenant_run_id_is_self_defeating(self, session, app_with_router):
        """The property that makes a non-secret run id safe.

        The run id is published in the correlation marker on every bot comment, so it
        must be assumed readable. Borrowing another tenant's run id yields only *that
        row's* tenant — never the caller's asserted one — so the caller's own proposal
        then fails Gate 2 and is refused. Forging buys nothing.
        """
        client = _internal_client(app_with_router, table=_StubTable([_row(tenant_id=ORG_B)]))

        response = client.post(
            ROUTE,
            # The attacker declares its own tenant and borrows a victim's run id.
            json=gateless_proposal(org_id=ORG_A).model_dump(mode="json"),
            headers={"X-Agent-RunId": RUN_ID, "X-Agent-OrgId": ORG_A},
        )

        assert response.status_code == 422, response.text
        await assert_graph_is_empty(session)

    async def test_the_attributed_org_header_cannot_move_the_tenant(self, session, app_with_router):
        """The #4132 pin, in this code's terms.

        `X-Agent-OrgId` names a victim tenant while the row names the caller's own.
        The rows must land in the ROW's tenant, and the header must have changed
        nothing at all.
        """
        client = _internal_client(app_with_router, table=_StubTable([_row(tenant_id=ORG_A)]))

        response = client.post(
            ROUTE,
            json=gateless_proposal(org_id=ORG_A).model_dump(mode="json"),
            headers={"X-Agent-RunId": RUN_ID, "X-Agent-OrgId": ORG_B},
        )

        assert response.status_code == 201, response.text
        flow = (await session.execute(sa.select(OrchestrationFlow))).scalar_one()
        assert flow.org_id == ORG_A, "the attribution header must not choose the tenant"

    @pytest.mark.parametrize(
        ("table", "expected_code"),
        [
            (_StubTable([]), "unknown_run"),
            (_StubTable([_row(tenant_id="")]), "unbindable_run"),
            (_StubTable([_row(status="complete")]), "terminal_run"),
            (_StubTable(error=ClientError({"Error": {"Code": "Throttling"}}, "Query")), "binding_unavailable"),
        ],
    )
    async def test_an_unresolvable_run_is_403_with_a_distinct_code(self, session, app_with_router, table, expected_code):
        """403, not 422: a 422 would be indistinguishable from the tenant-mismatch
        refusal in the worker's closing-comment warning, and they have different
        fixes. The `error` code says which arm fired."""
        client = _internal_client(app_with_router, table=table)

        response = client.post(ROUTE, json=gateless_proposal(org_id=ROW_TENANT).model_dump(mode="json"), headers={"X-Agent-RunId": RUN_ID})

        assert response.status_code == 403, response.text
        assert response.json()["detail"]["error"] == expected_code
        await assert_graph_is_empty(session)

    async def test_the_header_is_read_under_its_real_wire_name(self, session, app_with_router):
        """A regression pin for a bug that made the whole feature silently inert.

        FastAPI derives a header name from the parameter name by replacing
        underscores with hyphens, so `x_agent_run_id` binds `X-Agent-Run-Id` — NOT the
        `X-Agent-RunId` the platform actually sends. Without the explicit `alias` the
        parameter is always `None`, so every internal caller is refused
        `missing_run_id` while the worker is demonstrably sending the header: the
        423-era failure mode restated, and invisible to any test that spells the
        header the same wrong way the route does.

        Asserted through the real wire name, and paired below with a check that the
        hyphenated spelling is NOT honoured, so this cannot pass by accident.
        """
        client = _internal_client(app_with_router, table=_StubTable([_row()]))

        response = client.post(ROUTE, json=gateless_proposal(org_id=ROW_TENANT).model_dump(mode="json"), headers={"X-Agent-RunId": RUN_ID})

        assert response.status_code == 201, response.text

    async def test_the_hyphenated_spelling_is_not_the_contract(self, session, app_with_router):
        """`X-Agent-Run-Id` is a different header and must not bind.

        Pins the direction of the alias fix. If someone "simplifies" the alias away,
        this test starts passing while the one above fails — which is what tells the
        next reader the two spellings are not interchangeable.
        """
        client = _internal_client(app_with_router, table=_StubTable([_row()]))

        response = client.post(ROUTE, json=gateless_proposal(org_id=ROW_TENANT).model_dump(mode="json"), headers={"X-Agent-Run-Id": RUN_ID})

        assert response.status_code == 403, response.text
        assert response.json()["detail"]["error"] == "missing_run_id"

    async def test_the_header_name_matches_the_platforms_spelling(self):
        """The route's header constant and the proxy's read must be the same header.

        Both name the same wire header, and a drift between them is exactly the bug
        above with no test to catch it — so the spelling is asserted against the
        platform's existing reader rather than against a copy of itself.
        """
        from pathlib import Path as _Path

        from src.orchestration.draft_routes import RUN_ID_HEADER

        assert RUN_ID_HEADER.lower() == "x-agent-runid"
        proxy_source = _Path("src/proxy/routes.py").read_text()
        assert RUN_ID_HEADER.lower() in proxy_source, "the proxy reads a different header name than this route declares"

    async def test_an_internal_caller_sending_no_run_id_is_refused(self, session, app_with_router):
        """Zero rows written. The only other source of a tenant would be the header
        #4132 forbids, so there is nothing to fall back to."""
        client = _internal_client(app_with_router, table=_StubTable([_row()]))

        response = client.post(ROUTE, json=gateless_proposal(org_id=ROW_TENANT).model_dump(mode="json"))

        assert response.status_code == 403, response.text
        assert response.json()["detail"]["error"] == "missing_run_id"
        await assert_graph_is_empty(session)

    async def test_a_human_caller_still_uses_their_authenticated_org(self, session, app_with_router):
        """The human path is untouched: no run id, no lookup, tenant from `org_id`.

        This is the regression the issue's Validation section asks for. A human
        registering a draft has no `X-Agent-RunId`, and must not be made to.
        """
        table = _StubTable([_row(tenant_id=ORG_B)])
        client = _internal_client(app_with_router, table=table, scope="")  # a JWT caller

        response = client.post(ROUTE, json=gateless_proposal(org_id=ORG_A).model_dump(mode="json"))

        assert response.status_code == 201, response.text
        flow = (await session.execute(sa.select(OrchestrationFlow))).scalar_one()
        assert flow.org_id == ORG_A
        assert table.queries == [], "a human caller must not trigger a run-binding lookup"

    async def test_a_human_callers_run_id_header_is_ignored(self, session, app_with_router):
        """Belt and braces: a human who sends the header gets no benefit from it, so
        the header cannot become a second way to choose a tenant."""
        table = _StubTable([_row(tenant_id=ORG_B)])
        client = _internal_client(app_with_router, table=table, scope="")

        response = client.post(
            ROUTE,
            json=gateless_proposal(org_id=ORG_B).model_dump(mode="json"),
            headers={"X-Agent-RunId": RUN_ID},
        )

        # The document declares ORG_B, the human is in ORG_A: Gate 2 refuses. Had the
        # header been honoured, the row's ORG_B would have matched and this would 201.
        assert response.status_code == 422, response.text
        assert table.queries == []
        await assert_graph_is_empty(session)


# --- helpers ------------------------------------------------------------------


def _executable_source(module) -> str:
    """A module's source with docstrings and comments stripped.

    Same helper as `test_cost.py` / `test_run_binding.py`: the source assertions
    must look at CODE only, since this module's prose deliberately explains the very
    fields it forbids reading, and a naive substring check would trip on the
    documentation that makes the trap survivable.
    """
    source = py_inspect.getsource(module)
    source = re.sub(r'"""(?:.|\n)*?"""', "", source)
    source = re.sub(r"'''(?:.|\n)*?'''", "", source)
    source = re.sub(r"#[^\n]*", "", source)
    return source


def _internal_client(app, *, table: _StubTable, scope: str = "internal") -> TestClient:
    """A TestClient authenticating as the shared worker: org `__platform__`.

    The point of the fixture. `client_for` in `test_registration.py` resolves the
    caller into `ORG_A`, which is the situation this issue exists because production
    is NOT in — the real worker's registry `org_id` is the literal `__platform__`,
    so a test that grants it a real tenant cannot see the bug or the fix.
    """
    from src.auth.dependencies import get_current_user
    from src.orchestration.draft_routes import get_run_binding_resolver

    app.dependency_overrides[get_current_user] = lambda: TokenContext(
        user_id=WORKER_USER_ID,
        org_id=PLATFORM_ORG,
        team_id="",
        department_id="",
        account_type="service",
        scope=scope,
        expires_at=_far_future(),
    )
    app.dependency_overrides[get_run_binding_resolver] = lambda: _resolver(table)

    access = MagicMock(spec=AccessControl)
    access.check_permission = AsyncMock(return_value=True)
    # What a registry-resolved agent principal really resolves to, including the
    # `allowed_org_id` fallback to its own `__platform__` that makes the 403 trap real.
    access.get_user_role = AsyncMock(return_value=(AdminRole.MEMBER, PLATFORM_ORG, None))
    app.dependency_overrides[get_access_control_dep()] = lambda: access

    if scope != "internal":
        # A human/JWT caller: real tenant, no scope.
        app.dependency_overrides[get_current_user] = lambda: TokenContext(
            user_id="cognito-sub-operator",
            org_id=ORG_A,
            team_id="",
            department_id="",
            account_type="user",
            expires_at=_far_future(),
        )
        access.get_user_role = AsyncMock(return_value=(AdminRole.MEMBER, ORG_A, None))

    return TestClient(app, raise_server_exceptions=False)


def _far_future():
    from datetime import UTC, datetime, timedelta

    return datetime.now(UTC) + timedelta(hours=1)


class TestAccessControlNeverReadsAttribution:
    """The highest-value guard, per the architect's review (§8).

    Nothing else asserts that the authorization path does not read the attribution
    and lineage fields. The #4132 invariant is verified end-to-end only through
    `require_organization_access`; `AccessControl` — which every `check_permission`
    call on the platform goes through — is protected by convention alone.

    This is specifically the guard that would have caught the mechanism rejected for
    this issue: fabricating a `TokenContext` from a lineage row and running it
    through `get_user_role` works *accidentally*, because
    `or_(User.id == context.user_id)` matches a canonical `users.id`, and hands back
    the run's root human's real role and permissions. `shared/schemas/auth.py` names
    that failure in advance — "If this is ever read as authz, a sub-agent can act as
    the human who triggered it" — and no test enforced it.
    """

    # Attribution-only fields. Reading any of these to make an authorization
    # decision converts attribution into authority.
    FORBIDDEN = ("attributed_org_id", "attributed_user_id", "root_human_id")

    def test_access_control_never_reads_an_attribution_field(self):
        from src.admin import access_control

        source = _executable_source(access_control)

        found = sorted(name for name in self.FORBIDDEN if name in source)
        assert found == [], (
            f"src/admin/access_control.py reads attribution/lineage fields: {found}. "
            "These are attribution ONLY (#4132/#4300). `org_id` is the sole field an "
            "authorization path may read — reading one of these here lets a sub-agent "
            "act as the human who triggered it."
        )

    def test_the_guard_is_looking_at_the_right_file(self):
        """A source guard that silently reads the wrong file passes forever.

        Asserts the file exists, is non-trivial, and really does contain the authz
        entry point — so a rename cannot turn this guard into a no-op that keeps
        reporting success.
        """
        from src.admin import access_control

        path = Path(py_inspect.getfile(access_control))
        source = _executable_source(access_control)

        assert path.name == "access_control.py"
        assert "def check_permission" in source, "the authz entry point is not in the file this guard reads"
        assert "org_id" in source, "the guard's own subject string is absent; it would pass vacuously"
