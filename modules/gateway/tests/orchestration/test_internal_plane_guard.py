"""CI guard: no internal-plane endpoint may read or write promotion state.

Issue #4196. This is the automated half of the story, and it deliberately ships
with the migration rather than after it.

**Why this guard exists.** Agent pods are trusted *internal* callers: they can
reach any `/internal/v1/*` endpoint with any HTTP method. That is not a bug
today — no internal endpoint touches promotion state. But if one ever did, agents
would gain write access to the record of what was approved with **no permission
change, no agent-side code change, and nothing to trigger a security review**.
The EPIC's central guarantee — that an agent cannot fake having been approved —
would fail silently.

A boundary that one line of config can erase is not a boundary. And retrofitting
this guard later means auditing already-merged work, which is why it is not split
out into a follow-up.

**Why it fails closed.** The route-inventory test asserts **equality** against an
allowlist rather than scanning only today's routes. A test that merely iterates
the current surface passes vacuously the moment someone adds a route — the exact
case it is supposed to catch. Equality means a NEW internal route breaks the
build until a human adds it to the allowlist, at which point they have to look at
whether it touches promotion state. Shaped after
`tests/budget/test_routes.py:344-360` (equality-against-allowlist) and
`tests/proxy/test_agent_run_id.py:94-116` (walking `route.dependant`).

Plain pytest under the existing `Test` job — **no workflow change needed**.
"""

import ast
import inspect
from pathlib import Path

import pytest

SRC_DIR = Path(__file__).resolve().parents[2] / "src"
INTERNAL_DIR = SRC_DIR / "internal"

# Every module that contributes routes to the internal plane. Equality-checked
# below, so a new internal_*_routes.py module fails the build until listed.
INTERNAL_ROUTE_MODULES = (
    "src.internal.routes",
    "src.internal.credential_routes",
    "src.internal.assume_role_routes",
    "src.internal.provenance_routes",
    "src.internal.status_callback_routes",
    "src.internal.admin_routes",
    # PMM-03 bounded-probe admission and result recording only. Reviewed at
    # 46b62cf8: it imports no orchestration package/model/table and cannot read
    # or mutate release-promotion state.
    "src.internal.persona_model_probe_routes",
)

# The full internal-plane surface as of this change, as (path, method) pairs.
# THIS IS AN ALLOWLIST AND IT IS CHECKED FOR EQUALITY. Adding an internal route
# requires adding it here — and that is the review moment where someone must
# confirm the route does not read or write promotion state.
EXPECTED_INTERNAL_ROUTES = {
    ("/internal/v1/issue-magic-link", "POST"),
    ("/internal/v1/resolve-user", "POST"),
    ("/internal/v1/resolve-installation", "POST"),
    ("/internal/v1/user-credentials", "GET"),
    ("/internal/v1/proxy-request", "POST"),
    ("/internal/v1/credential-materialize", "POST"),
    ("/internal/v1/credential-raw-read", "POST"),
    ("/internal/v1/credential-assume-role", "POST"),
    ("/internal/v1/github-installation-token", "POST"),
    ("/internal/v1/provenance", "POST"),
    ("/internal/v1/knowledge-assets/status-callback", "POST"),
    ("/internal/v1/admin/tenant-config/{tenant}", "GET"),
    ("/internal/v1/admin/audit-entries", "GET"),
    ("/internal/v1/persona-model-probes/claim", "POST"),
    ("/internal/v1/persona-model-probes/{slot_id}/start", "POST"),
    ("/internal/v1/persona-model-probes/{slot_id}/complete", "POST"),
}

# Promotion state: the tables and models this guard protects. A reference to any
# of these names from internal-plane code is a build failure.
ORCHESTRATION_TABLES = frozenset(
    {
        "orchestration_flows",
        "orchestration_nodes",
        "orchestration_edges",
        "orchestration_accepted_plans",
        "orchestration_decisions",
    }
)

ORCHESTRATION_SYMBOLS = frozenset(
    {
        "OrchestrationFlow",
        "OrchestrationNode",
        "OrchestrationEdge",
        "OrchestrationAcceptedPlan",
        "OrchestrationDecision",
        "OrchestrationRepository",
    }
)


def _internal_route_modules():
    """Import every internal-plane router module."""
    from importlib import import_module

    return [import_module(path) for path in INTERNAL_ROUTE_MODULES]


def _internal_routes():
    """Every (path, method) pair on the internal plane."""
    routes = set()
    for module in _internal_route_modules():
        for route in module.router.routes:
            path = getattr(route, "path", None)
            for method in getattr(route, "methods", set()) or set():
                if path:
                    routes.add((path, method))
    return routes


def _internal_source_files():
    """Every Python file under src/internal/."""
    return sorted(p for p in INTERNAL_DIR.glob("*.py") if p.name != "__init__.py")


class TestInternalPlaneInventory:
    """Fail-closed: the internal surface cannot grow without review."""

    def test_internal_route_modules_are_exactly_the_known_set(self):
        """A new src/internal/*_routes.py module fails until it is listed.

        Without this, a whole new internal router could appear and every other
        test in this file would keep passing while never inspecting it.
        """
        discovered = {f"src.internal.{p.stem}" for p in _internal_source_files() if p.stem.endswith("routes")}
        assert discovered == set(INTERNAL_ROUTE_MODULES), (
            "internal-plane route modules changed.\n"
            "  unlisted (add to INTERNAL_ROUTE_MODULES only after confirming it does not "
            f"touch promotion state): {sorted(discovered - set(INTERNAL_ROUTE_MODULES))}\n"
            f"  listed but missing: {sorted(set(INTERNAL_ROUTE_MODULES) - discovered)}"
        )

    def test_internal_route_surface_is_exactly_the_allowlist(self):
        """Equality, not subset — a NEW internal route must break this test.

        This is the whole mechanism. Agent pods can call any internal route with
        any method, so every addition to this surface is a security-relevant
        change and has to be seen by a human. If you are here because you added a
        route: confirm it does not read or write the orchestration tables, then
        add it to EXPECTED_INTERNAL_ROUTES.
        """
        actual = _internal_routes()
        added = actual - EXPECTED_INTERNAL_ROUTES
        removed = EXPECTED_INTERNAL_ROUTES - actual

        assert actual == EXPECTED_INTERNAL_ROUTES, (
            "the internal-plane route surface changed.\n"
            f"  ADDED (must be confirmed not to touch promotion state, then allowlisted): {sorted(added)}\n"
            f"  REMOVED (delete from EXPECTED_INTERNAL_ROUTES): {sorted(removed)}"
        )


class TestNoPromotionStateOnInternalPlane:
    """The guard proper: promotion state is unreachable from the internal plane."""

    @pytest.mark.parametrize("path", _internal_source_files(), ids=lambda p: p.name)
    def test_internal_module_does_not_import_orchestration(self, path):
        """No internal-plane module imports the orchestration package.

        Static AST check, so it holds regardless of whether the import is
        reachable at runtime — an unused import today is a used one tomorrow.
        """
        tree = ast.parse(path.read_text(), filename=str(path))
        offenders = []

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if "orchestration" in alias.name:
                        offenders.append(alias.name)
            elif isinstance(node, ast.ImportFrom):
                if node.module and "orchestration" in node.module:
                    offenders.append(node.module)
                # Also catch `from src.orchestration import models`-style names.
                if node.module and node.module.endswith("orchestration"):
                    offenders.extend(a.name for a in node.names)

        assert offenders == [], (
            f"{path.name} imports orchestration code: {offenders}. "
            "Promotion state must not be reachable from the internal plane — agent "
            "pods can call every internal route with any HTTP method, so an import "
            "here silently grants agents write access to what was approved."
        )

    @pytest.mark.parametrize("path", _internal_source_files(), ids=lambda p: p.name)
    def test_internal_module_does_not_reference_orchestration_models(self, path):
        """No internal-plane module names an orchestration model class."""
        source = path.read_text()
        tree = ast.parse(source, filename=str(path))

        referenced = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name) and node.id in ORCHESTRATION_SYMBOLS}
        referenced |= {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute) and node.attr in ORCHESTRATION_SYMBOLS}

        assert referenced == set(), f"{path.name} references orchestration models: {sorted(referenced)}"

    @pytest.mark.parametrize("path", _internal_source_files(), ids=lambda p: p.name)
    def test_internal_module_does_not_name_orchestration_tables(self, path):
        """No internal-plane module contains a raw orchestration table name.

        Catches the bypass the model check misses: hand-written SQL against the
        tables, which needs no import at all.
        """
        source = path.read_text()
        found = sorted(t for t in ORCHESTRATION_TABLES if t in source)
        assert found == [], (
            f"{path.name} references orchestration tables in raw SQL: {found}. Raw SQL bypasses both the ORM append-only guard and tenant scoping."
        )

    def test_no_internal_route_handler_touches_orchestration(self):
        """Runtime check over each handler's own source.

        The file-level checks above cover the module; this narrows to the actual
        registered endpoints, following the `route.dependant`-walking shape of
        `tests/proxy/test_agent_run_id.py`. Belt and braces: a handler could be
        defined elsewhere and merely registered here.
        """
        offenders = []

        for module in _internal_route_modules():
            for route in module.router.routes:
                endpoint = getattr(route, "endpoint", None)
                callables = [endpoint] if endpoint else []
                # Include the route's dependency callables — a dependency is as
                # good a place to reach promotion state as the handler body.
                for dep in getattr(getattr(route, "dependant", None), "dependencies", []) or []:
                    if getattr(dep, "call", None):
                        callables.append(dep.call)

                for fn in callables:
                    try:
                        source = inspect.getsource(fn)
                    except (OSError, TypeError):
                        continue
                    hits = sorted({name for name in ORCHESTRATION_SYMBOLS if name in source} | {t for t in ORCHESTRATION_TABLES if t in source})
                    if hits:
                        offenders.append((getattr(route, "path", "?"), getattr(fn, "__name__", str(fn)), hits))

        assert offenders == [], f"internal-plane handlers/dependencies touch promotion state: {offenders}"


class TestOrchestrationRouterIsOperatorPlane:
    """The orchestration router exists (Issue #4200) and is operator-plane only.

    Issue #4196 shipped storage with no router at all, and asserted that. Its
    docstring set the instruction this class follows: "When a later story adds an
    operator-plane router, this test changes to assert it is NOT in UNIT_MODULES'
    internal set — it should not simply be deleted."

    #4200 added `POST /orchestration/flows/{flow_id}/amendments`, so the
    no-router assertion is now converted rather than dropped. What has to stay
    true is narrower but is the part that actually matters: the router is reachable
    only from the Cognito-authenticated operator plane, never from `/internal/v1/*`
    where agent pods can call anything with any method.
    """

    def test_orchestration_package_still_exports_no_router(self):
        """The PACKAGE (`src/orchestration/__init__.py`) must stay router-free.

        `app.py` auto-registers any `router` attribute on a listed module. The
        router lives in the `src.orchestration.routes` submodule, which is listed
        explicitly; re-exporting it from the package would create a second
        registration path that no allowlist here covers.
        """
        import src.orchestration as orchestration

        assert not hasattr(orchestration, "router"), (
            "src.orchestration (the package) now re-exports a `router`. Keep the router in "
            "src.orchestration.routes so registration stays explicit and single-path."
        )

    def test_orchestration_routes_are_registered_on_the_operator_plane(self):
        """The router is in UNIT_MODULES under its own non-internal module path."""
        from src.app import UNIT_MODULES

        assert "src.orchestration.routes" in UNIT_MODULES, "src.orchestration.routes is not registered; the amendment endpoint would 404."

    def test_draft_router_is_registered_on_the_operator_plane(self):
        """Issue #4528's draft-registration router is operator-plane, like the rest.

        It is a separate module because it carries a *weaker* permission
        (`PLAN_DRAFT`, see the sibling test below), and `routes.py`'s guarantee is
        that nothing on it is reachable below approval authority. Separate module,
        same plane: the weaker permission must not come with weaker
        authentication.
        """
        from src.app import UNIT_MODULES

        assert "src.orchestration.draft_routes" in UNIT_MODULES, "src.orchestration.draft_routes is not registered; draft registration would 404."

    def test_orchestration_is_not_registered_as_an_internal_module(self):
        """Registering orchestration routes under /internal/v1 is the failure mode."""
        from src.app import UNIT_MODULES

        internal_orchestration = [m for m in UNIT_MODULES if "orchestration" in m and "internal" in m]
        assert internal_orchestration == [], f"orchestration registered on the internal plane: {internal_orchestration}"

    def test_orchestration_router_is_not_in_the_internal_route_surface(self):
        """No orchestration path may appear under the internal-plane prefix.

        Belt and braces against the mount-path variant of the mistake: a router
        listed as an operator module could still declare an `/internal/v1` prefix
        and land on the plane agents can reach.
        """
        from src.orchestration.routes import router as orchestration_router

        internal_paths = [route.path for route in orchestration_router.routes if "/internal/" in getattr(route, "path", "")]
        assert internal_paths == [], f"orchestration router declares internal-plane paths: {internal_paths}"

        assert _internal_routes().isdisjoint(
            {(route.path, method) for route in orchestration_router.routes for method in getattr(route, "methods", set()) or set()}
        ), "an orchestration route collides with the internal-plane surface"

    def test_every_orchestration_route_requires_authentication_and_permission(self):
        """Each route depends on `get_current_user` and checks an allowlisted permission.

        The router has no router-level dependency, so a future route added here
        would be unauthenticated by default. This walks every registered endpoint
        rather than trusting that the current ones are the only ones.

        Issue #4207: this required `Permission.PLAN_APPROVE` on *every* route,
        which was right while every route wrote promotion state. `GET
        /flows/{flow_id}/cost` only reads spend, and gating a read of cost on
        approval authority would mean nobody could see what a flow cost without
        also being able to accept plans — granting strictly more than the
        operation needs.

        So the invariant is now per-route rather than uniform, and it stays
        fail-closed by **equality against an allowlist** (the same shape as
        EXPECTED_INTERNAL_ROUTES above): a new route is not covered by a default,
        it breaks this test until someone adds it here and states its permission
        out loud. Anything that touches promotion state must still be
        PLAN_APPROVE — that is asserted separately below.

        Issue #4869: the allowlist is keyed by **(path, method)**, not by path.
        It was path-keyed while every path had exactly one method, and `GET
        /orchestration/flows` (the flows list) ended that: it shares its path with
        the `POST` that creates flow, node and edge rows. Under a path-keyed dict
        the two methods cannot state different permissions, and the failure mode is
        the dangerous direction — whichever entry was written would be asserted
        against BOTH handlers, so the list read and the promotion write would be
        held to one permission and one of them would be wrong. Keying on the method
        too is strictly more precise and keeps the equality check intact: a new
        METHOD on an existing path is now also a review moment, where before it
        would have inherited its sibling's entry silently.
        """
        from src.auth.dependencies import get_current_user
        from src.orchestration.routes import router as orchestration_router

        # (path, method) -> the permission that route must check. ADDING A ROUTE
        # REQUIRES A LINE HERE, and that is the review moment: if the route reads or
        # writes promotion state, the answer is PLAN_APPROVE and nothing weaker.
        expected_permissions = {
            # Issue #4320: the engine's ingress for plan state. It CREATES flow,
            # node and edge rows, so it is promotion activity of the strongest
            # kind — PLAN_APPROVE and nothing weaker.
            ("/orchestration/flows", "POST"): "Permission.PLAN_APPROVE",
            # Issue #4869: the flows list — the engine's entry point in the UI.
            # Same path as the POST above, deliberately weaker permission, and the
            # two must not be confused for each other. This handler reads
            # `orchestration_flows` plus node and decision AGGREGATES: per-flow
            # counts, a stalled/not-stalled boolean, and wave rollups. It carries
            # USAGE_READ because that is what the operator it exists for holds, and
            # because it surfaces no acceptance record — no `actor_id`, no
            # `actor_role`, no reason text, no plan document. The `orchestration_
            # decisions` read is a COUNT of stalls per flow, not an approval.
            #
            # If a future change surfaces attribution or plan content here, the
            # permission must become PLAN_APPROVE: *who approved what* is the
            # approval record, and reading it under a spend-read permission is the
            # escalation the sibling guard below exists to stop.
            ("/orchestration/flows", "GET"): "Permission.USAGE_READ",
            ("/orchestration/flows/{flow_id}/amendments", "POST"): "Permission.PLAN_APPROVE",
            ("/orchestration/flows/{flow_id}/plans", "GET"): "Permission.PLAN_APPROVE",
            # Read-only cost rollup. Reads usage_logs, never promotion state.
            ("/orchestration/flows/{flow_id}/cost", "GET"): "Permission.USAGE_READ",
            # Issue #4212: the graph view's read. Returns the flow's nodes, edges
            # and cost — the graph's *structure and progress*, which is what the
            # sibling `plan_approve` guard below deliberately excludes from its
            # acceptance-record scan.
            #
            # It does consult `orchestration_decisions`, via `_stalled_node_ids`,
            # and that is worth stating out loud because the scan below would not
            # catch it (the read is in a helper, not in the handler body). What it
            # takes from those rows is one derived boolean per node — "was the
            # latest stall-or-halt finding a stall?" — and nothing else: no
            # `actor_id`, no `actor_role`, no `actor_kind`, no reason text, no
            # approval record. That distinction is asserted directly, on the
            # response body rather than on source text, by
            # `test_read_api.py::TestNoApprovalRecordLeak`.
            #
            # If a future change surfaces attribution here, the permission must
            # become PLAN_APPROVE: *who approved what* is the approval record,
            # and reading it under a spend-read permission is the escalation the
            # sibling guard exists to stop.
            ("/orchestration/flows/{flow_id}", "GET"): "Permission.USAGE_READ",
            # Issue #5145: the delivery ledger's read model — per node and cycle,
            # the phase, whether it is moving, the next scheduled check and the
            # typed block naming who must act. USAGE_READ, because this answers
            # "why is delivery waiting" for the operator watching it, and making
            # *progress visibility* require approval authority would push people
            # back to reading logs, which is the problem the ledger exists to fix.
            #
            # It surfaces no acceptance record, and that is enforced rather than
            # asserted: this route deliberately does NOT serve
            # `accepted_plan_version`, even though the ledger row carries it. An
            # earlier draft did, and the sibling guard below caught it — which is
            # the guard working exactly as intended, because "which approved plan
            # authorized this" is the approval record. The response also carries no
            # `actor_id`, `actor_role`, `actor_kind`, reason text or plan document.
            #
            # It likewise does not publish `claim_id`/`claim_generation`: those are
            # the authority binding `execution_store`'s fence tests, and the store
            # withholds them from a refused caller precisely so a refusal cannot
            # disclose what would satisfy it. Serving them to a browser would undo
            # that. Asserted on the response body in
            # `test_execution_read.py::test_the_claim_binding_is_never_published`.
            ("/orchestration/flows/{flow_id}/execution", "GET"): "Permission.USAGE_READ",
            # Issue #5301: attributed recovery of a *historically unbound* story —
            # a human asserting which pull request delivered work that no run ever
            # registered. PLAN_APPROVE and nothing weaker, for two reasons.
            #
            # It establishes the association that completion is then read from, so
            # under a weaker permission it would be an indirect route to advancing
            # accepted work without approval authority — the escalation this guard
            # exists to stop. And it writes an attribution record (*who* established
            # the binding), which is approval-record material by the same rule the
            # sibling entries above state.
            #
            # A delivering run registering its OWN pull request is deliberately not
            # here: that path carries no permission at all, because its authority is
            # the run credential, which is strictly narrower. See
            # `src/agentauth/pr_binding_routes.py`.
            ("/orchestration/flows/{flow_id}/nodes/{node_id}/pull-request-recovery", "POST"): "Permission.PLAN_APPROVE",
        }

        actual_routes = set()
        unguarded = []
        for route in orchestration_router.routes:
            endpoint = getattr(route, "endpoint", None)
            if endpoint is None:
                continue

            path = getattr(route, "path", "?")
            dependency_calls = {getattr(dep, "call", None) for dep in getattr(getattr(route, "dependant", None), "dependencies", []) or []}
            source = inspect.getsource(endpoint)

            # HEAD/OPTIONS are synthesised by Starlette alongside a GET handler and
            # have no handler of their own, so they are not their own review moment.
            for method in sorted(set(getattr(route, "methods", set()) or set()) - {"HEAD", "OPTIONS"}):
                actual_routes.add((path, method))
                required = expected_permissions.get((path, method))
                if get_current_user not in dependency_calls or required is None or required not in source:
                    unguarded.append((path, method))

        assert actual_routes == set(expected_permissions), (
            f"orchestration route surface changed: {sorted(actual_routes ^ set(expected_permissions))}. "
            "Add the new route to expected_permissions with the permission it enforces."
        )

        assert unguarded == [], (
            f"orchestration routes missing authentication or their required permission check: {unguarded}. "
            "Promotion state must never be reachable without an explicit approval authority."
        )

    def test_routes_that_mutate_or_read_acceptance_records_require_plan_approve(self):
        """The part of the old uniform rule that must never relax.

        `test_every_orchestration_route_requires_authentication_and_permission`
        allows a per-route permission so a pure cost read is not forced to demand
        approval authority. That flexibility must not become a hole, so the two
        cases that genuinely ARE approval authority are pinned here regardless of
        what the allowlist says:

          1. **Any non-GET method.** Writing anything on this router is promotion
             activity.
          2. **Any handler touching the acceptance records** — accepted plans and
             decisions. These are "what was approved"; reading them under a
             weaker permission leaks the approval record.

        Deliberately NOT included: `OrchestrationFlow`/`Node`/`Edge` and
        `OrchestrationRepository`. Those are the graph's *structure*, and the cost
        route reads them purely to resolve `flow_id` within the caller's org
        before querying the ledger — refusing that would force every read on this
        router back to demanding approval authority, which is the very thing this
        change fixes. The scan below is on the acceptance records only.
        """
        from src.orchestration.routes import router as orchestration_router

        # Model/table names alone would be VACUOUS here: handlers reach these
        # records through repository methods and response fields, never by naming
        # the ORM class. Verified by mutation — flipping `list_plans` to
        # USAGE_READ must make this test fail, and with only the class names in
        # this set it did not.
        acceptance_records = frozenset(
            {
                "OrchestrationAcceptedPlan",
                "OrchestrationDecision",
                "orchestration_accepted_plans",
                "orchestration_decisions",
                "list_plan_versions",
                "accepted_by_decision_id",
                "accepted_plan",
                "plan_document",
                "record_decision",
            }
        )

        offenders = []
        for route in orchestration_router.routes:
            endpoint = getattr(route, "endpoint", None)
            if endpoint is None:
                continue

            source = inspect.getsource(endpoint)
            methods = set(getattr(route, "methods", set()) or set())
            mutating = sorted(methods - {"GET", "HEAD", "OPTIONS"})
            touches = sorted(name for name in acceptance_records if name in source)

            if (mutating or touches) and "Permission.PLAN_APPROVE" not in source:
                offenders.append((getattr(route, "path", "?"), mutating, touches))

        assert offenders == [], (
            f"orchestration routes mutate state or read acceptance records without PLAN_APPROVE: {offenders}. "
            "A weaker permission on promotion state is exactly the escalation this guard exists to stop."
        )

    def test_draft_route_surface_is_exactly_the_allowlist(self):
        """Issue #4528: the draft router's surface, checked for equality.

        `routes.py`'s allowlist above cannot cover this router, because the rule it
        enforces (`PLAN_APPROVE` on every non-GET handler) is the rule draft
        registration deliberately does not satisfy. So the draft router gets its
        own equality-checked allowlist, and adding a route to it is the same review
        moment: state the permission out loud, and justify anything weaker than
        approval authority.
        """
        from src.auth.dependencies import get_current_user
        from src.orchestration.draft_routes import router as draft_router

        expected_permissions = {
            # Issue #4528: an authoring agent registering a compiled proposal as an
            # INERT draft. PLAN_DRAFT, not PLAN_APPROVE, and that is the whole
            # point: an agent that could hold PLAN_APPROVE could accept the plan it
            # just wrote, which is the self-approval this EPIC exists to prevent.
            #
            # The weaker permission is safe because inertness is structural, not
            # policy — see src/orchestration/registration.py. Two independent
            # guards: the decision row is PLAN_DRAFTED, which is absent from
            # `genesis.APPROVAL_DECISION_KINDS` so dispatch has nothing to root a
            # chain in; and the whole graph sits behind an acceptance gate created
            # in `awaiting_gate`, whose progress edges are `_HUMAN_ONLY` in
            # state.py. Both are asserted directly in test_registration.py.
            #
            # If a future change on this router makes anything it writes *executable*
            # without a separate human approval, the permission must become
            # PLAN_APPROVE.
            "/orchestration/flows/drafts": "Permission.PLAN_DRAFT",
            # Issue #4529: an authoring agent filing an AMENDMENT to an
            # already-accepted plan as a pending draft. Same permission, and inert in
            # a stronger sense than #4528's: registration writes no node, no edge, no
            # decision, no work claim and no accepted-plan version — one row holding a
            # proposal document, referenced by no graph query at all. There is
            # therefore no filter anywhere whose omission could make it executable,
            # which is asserted by row-counting in test_pending_amendments.py rather
            # than by reading the implementation.
            #
            # It is worth being explicit about why PLAN_DRAFT is still right when the
            # target is a plan of record rather than a new flow. The route cannot
            # promote, and it cannot even *choose* what it proposes against: a
            # required `request_id` must name an authoring assignment the server
            # created from a verified human `replan:`, and the presented
            # `X-Agent-RunId` must equal the `author_run_id` the server wrote on that
            # assignment. So holding PLAN_DRAFT is necessary but NOT sufficient here —
            # the server must also have commissioned this run for this request, and
            # the flow comes from the assignment rather than from the path or the
            # document (#4556). Acceptance is a separate, human-only act
            # (`@agent-engine accept amendment <draft-id>`) that runs `amend_plan`
            # under the accepting human's own context.
            #
            # The line that must not be crossed: **no acceptance endpoint may ever be
            # added to this router.** Acceptance reads and writes the record of what a
            # human approved, so it belongs on `routes.py` behind PLAN_APPROVE, and
            # an agent-reachable accept — even one restricted to "drafts this agent
            # authored" — is the self-approval the EPIC exists to prevent.
            "/orchestration/flows/{flow_id}/amendments/drafts": "Permission.PLAN_DRAFT",
        }

        actual_paths = set()
        unguarded = []
        for route in draft_router.routes:
            endpoint = getattr(route, "endpoint", None)
            if endpoint is None:
                continue

            path = getattr(route, "path", "?")
            actual_paths.add(path)
            dependency_calls = {getattr(dep, "call", None) for dep in getattr(getattr(route, "dependant", None), "dependencies", []) or []}
            source = inspect.getsource(endpoint)

            required = expected_permissions.get(path)
            if get_current_user not in dependency_calls or required is None or required not in source:
                unguarded.append(path)

        assert actual_paths == set(expected_permissions), (
            f"draft router surface changed: {sorted(actual_paths ^ set(expected_permissions))}. "
            "Add the new route to expected_permissions with the permission it enforces."
        )

        assert unguarded == [], f"draft routes missing authentication or their required permission check: {unguarded}."

    def test_draft_router_declares_no_internal_plane_path(self):
        """The mount-path variant of the mistake, for the new router too."""
        from src.orchestration.draft_routes import router as draft_router

        internal_paths = [route.path for route in draft_router.routes if "/internal/" in getattr(route, "path", "")]
        assert internal_paths == [], f"draft router declares internal-plane paths: {internal_paths}"

    def test_draft_route_never_records_an_approval_kind_decision(self):
        """The load-bearing one: PLAN_DRAFT must not be able to write an approval.

        `PLAN_DRAFT` is weaker than `PLAN_APPROVE` only because the row it writes
        cannot root a dispatch. If the draft path ever recorded a kind inside
        `APPROVAL_DECISION_KINDS`, the weaker permission would become a route to
        arming execution — the escalation, arriving through the door built to be
        harmless.

        Asserted on the DECISION KIND rather than on source text, because that is
        the property dispatch actually reads.
        """
        from src.orchestration.genesis import APPROVAL_DECISION_KINDS
        from src.orchestration.models import DecisionKind

        assert DecisionKind.PLAN_DRAFTED.value not in APPROVAL_DECISION_KINDS, (
            "PLAN_DRAFTED is now an approval kind, so a draft registered by an agent can root an engine "
            "dispatch. Registration would auto-start execution — issue #4528's first bug class."
        )
