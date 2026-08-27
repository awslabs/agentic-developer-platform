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
    ("/internal/v1/provenance", "POST"),
    ("/internal/v1/knowledge-assets/status-callback", "POST"),
    ("/internal/v1/admin/tenant-config/{tenant}", "GET"),
    ("/internal/v1/admin/audit-entries", "GET"),
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


class TestOrchestrationExposesNoRouter:
    """The strongest form of the guarantee for this story: there is no route.

    This story ships storage only. No orchestration router exists, so promotion
    state is not reachable over HTTP from any plane. When a later story adds an
    operator-plane router, this test changes to assert it is NOT in UNIT_MODULES'
    internal set — it should not simply be deleted.
    """

    def test_orchestration_package_has_no_router_attribute(self):
        import src.orchestration as orchestration

        assert not hasattr(orchestration, "router"), (
            "src.orchestration now exposes a `router`. app.py auto-registers any "
            "module attribute named `router`, so confirm this is operator-plane "
            "(Cognito-authenticated), NOT internal-plane, then update this test."
        )

    def test_orchestration_is_not_registered_as_an_internal_module(self):
        """Registering orchestration routes under /internal/v1 is the failure mode."""
        from src.app import UNIT_MODULES

        internal_orchestration = [m for m in UNIT_MODULES if "orchestration" in m and "internal" in m]
        assert internal_orchestration == [], f"orchestration registered on the internal plane: {internal_orchestration}"
