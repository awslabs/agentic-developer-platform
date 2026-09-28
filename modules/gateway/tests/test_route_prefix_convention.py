"""App-wide guard: no router may be registered under an `/api/...` prefix.

Issue #4330. CloudFront fronts the gateway with an `/api/*` cache behavior whose
viewer-request function (`bedrockgw-<env>-strip-api-prefix`) removes the **first**
leading `/api` from the URI before forwarding to the origin. That makes `/api` a
front-door-only segment: the browser calls `/api/auth/me`, the origin serves
`/auth/me`. Every operator-plane router therefore carries no `/api` prefix
(`/auth`, `/admin`, `/budgets`).

A router that declares `prefix="/api/..."` is unreachable through the dashboard:
the strip turns `/api/orchestration/flows` into `/orchestration/flows`, which is
not registered → 404 for a fully authenticated operator. That was issue #4330,
and it survived two verification waves because the routes were exercised against
the internal ALB and the deployed OpenAPI — neither of which runs the strip
function. This test is the cheap, durable replacement for a live-CloudFront check:
it asserts the *shipped click path* is consistent with the mount, in CI, with no
AWS dependency.

The assertion is fail-closed by equality against a quarantine allowlist rather
than a blanket "zero `/api/` routes". Three routers predate this test; two of them
are known to be *load-bearing* in their current broken-looking form (see below), so
silently "fixing" them here would break working screens. Equality means a NEW
`/api/...` router breaks CI immediately, while the known set is documented in one
place with its reason.
"""

from fastapi import FastAPI

# Routers that already carry an `/api` prefix, with the reason each is still here.
# DO NOT add to this set to make a new route pass — the fix is to drop `/api` from
# the router prefix. Removing an entry requires verifying the real browser path
# end-to-end (through CloudFront), not just the OpenAPI document or the ALB.
#
# `/api/agent-context/*` (knowledge assets + GitHub repo picker) and
# `/api/admin/identity/*` are reachable today only because of a compensating
# double prefix on the client: `frontend/src/services/api.ts` sets the apiClient
# base URL to `/api`, and those services pass paths that themselves begin with
# `/api`, so the browser emits `/api/api/agent-context/assets` and CloudFront
# strips it back to `/api/agent-context/assets` — which matches the mount. Fixing
# the router prefix alone would break those screens; both ends must change in the
# same commit. Tracked as a follow-up to #4330; deliberately NOT changed there
# because those call paths were unverified.
QUARANTINED_API_PREFIXED_PATHS = {
    "/api/admin/identity/organizations",
    "/api/admin/identity/organizations/{org_id}",
    "/api/admin/identity/organizations/{org_id}/users",
    "/api/admin/identity/organizations/{org_id}/users/{user_id}",
    "/api/admin/identity/users/{user_id}/identities",
    "/api/admin/identity/users/{user_id}/identities/{identity_id}",
    "/api/agent-context/assets",
    "/api/agent-context/assets/bulk",
    "/api/agent-context/assets/bulk/commit",
    "/api/agent-context/assets/{asset_id}",
    "/api/agent-context/assets/{asset_id}/reindex",
    "/api/agent-context/assets/{asset_id}/status",
    "/api/agent-context/github/accessible-repos",
}


def _app() -> FastAPI:
    """Build the real app so this reflects what actually gets mounted.

    Importing `create_app` and calling it exercises the `UNIT_MODULES`
    auto-registration loop — the same code path the pod runs. Asserting against a
    hand-listed set of routers would miss exactly the mistake this test exists to
    catch.
    """
    from src.app import create_app

    return create_app()


def _mounted_paths(app: FastAPI) -> set[str]:
    return {path for route in app.routes if (path := getattr(route, "path", ""))}


def _declared_paths() -> set[str]:
    """Every path any known router declares, independent of feature flags.

    `src.knowledge.routes` and `src.knowledge.github_repos` only export `router`
    when `AGENT_CONTEXT_ENABLED=true`, evaluated at import time, so those paths are
    absent from the mounted app in a default test run. Reading their `_router`
    objects directly keeps the staleness check from flipping with the environment —
    otherwise this test would pass or fail depending on an env var, which is the
    kind of conditional signal that trains people to ignore it.
    """
    from src.knowledge import github_repos
    from src.knowledge import routes as knowledge_routes

    paths = _mounted_paths(_app())
    for module in (knowledge_routes, github_repos):
        # `route.path` on an APIRouter already includes the router's prefix.
        paths |= {path for route in module._router.routes if (path := getattr(route, "path", ""))}
    return paths


class TestNoRouterIsMountedUnderApi:
    """The `/api` segment belongs to CloudFront, not to any router prefix."""

    def test_no_new_route_is_registered_under_api(self):
        """Only the documented quarantine set may start with `/api/`.

        This is the bug-class assertion: any router that declares an `/api`
        prefix from now on fails here, before it can ship a 404 to an operator.
        """
        api_prefixed = {path for path in _declared_paths() if path.startswith("/api/")}

        unexpected = api_prefixed - QUARANTINED_API_PREFIXED_PATHS
        assert unexpected == set(), (
            f"Route(s) mounted under /api/: {sorted(unexpected)}. CloudFront strips the first "
            "/api before the origin, so these are unreachable through the dashboard (404 for an "
            "authenticated operator). Drop '/api' from the router prefix — do NOT add the path "
            "to QUARANTINED_API_PREFIXED_PATHS, and do NOT change the CloudFront function "
            "(that is the login-critical /api/* behavior). See issue #4330."
        )

    def test_quarantine_list_has_no_stale_entries(self):
        """A quarantined path that no longer exists must leave the allowlist.

        Without this, the allowlist silently grows into a permanent exemption
        that would let a re-introduced `/api` prefix pass unnoticed.
        """
        stale = QUARANTINED_API_PREFIXED_PATHS - _declared_paths()
        assert stale == set(), f"QUARANTINED_API_PREFIXED_PATHS lists paths that are no longer mounted: {sorted(stale)}. Remove them."


class TestOrchestrationIsReachableThroughTheFrontDoor:
    """Issue #4330 specifically: the operator-plane orchestration routes."""

    def test_orchestration_routes_are_not_api_prefixed(self):
        """No orchestration path may carry `/api` — the regression under fix."""
        offenders = sorted(path for path in _mounted_paths(_app()) if "orchestration" in path and path.startswith("/api/"))
        assert offenders == [], f"orchestration routes are mounted under /api/ and will 404 through CloudFront: {offenders}"

    def test_flow_create_is_mounted_at_the_stripped_path(self):
        """`POST /api/orchestration/flows` from a browser must land here.

        Asserted as an exact path rather than a prefix scan so that renaming the
        route (or reinstating `/api`) is a deliberate, visible change.
        """
        assert "/orchestration/flows" in _mounted_paths(_app()), (
            "POST /orchestration/flows is not mounted; the browser's /api/orchestration/flows call would 404 after CloudFront strips /api."
        )

    def test_every_orchestration_route_is_under_the_orchestration_prefix(self):
        """The whole router moved, not just the one path in the bug report."""
        from src.orchestration.routes import router

        assert router.prefix == "/orchestration", f"orchestration router prefix is {router.prefix!r}, expected '/orchestration'"

        bad = sorted(path for route in router.routes if not (path := getattr(route, "path", "")).startswith("/orchestration/"))
        assert bad == [], f"orchestration routes outside the /orchestration prefix: {bad}"
