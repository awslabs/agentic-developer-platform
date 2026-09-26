"""Every route this service serves, with the authorization it requires.

Issue #5055 (U14) — the enforcement half of R5/R6. The decision *model* lives in
``superplane_auth.policy`` (issue #5044, U9) and is imported, not restated: the
permission vocabulary, the implication closure and the ADP-role mapping all come
from there so there is one definition of the rules rather than one per service.

WHY THIS FILE EXISTS SEPARATELY FROM THE POLICY'S OWN INVENTORY
---------------------------------------------------------------
``superplane_auth.policy.ENDPOINT_INVENTORY`` is written against
``/superplane/v1/...`` path templates. This service does not serve those paths —
it serves ``/workspaces/...``, ``/api/v1/research/...``, ``/orgs/...`` and
``/internal/...``. Enforcing against the policy's templates would look correct
and match nothing, which is the worst of both: a lookup that never fires reads
exactly like a lookup that always passes.

So the *paths* are recorded here, against the app's real route templates, while
the *permissions* remain the policy's enum. ``tests/test_auth.py`` walks the
mounted FastAPI app and fails if any route is absent from this table, which is
what makes this an inventory rather than a list: a new route cannot ship without
a recorded decision, because collection fails instead of defaulting.

THE THREE CLASSES, AND WHY "PUBLIC" IS ENUMERATED RATHER THAN INFERRED
----------------------------------------------------------------------
Every route is exactly one of:

* ``PUBLIC`` — unauthenticated by design (health, OpenAPI, and the two login
  routes that *establish* a credential and so cannot require one).
* ``INTERNAL`` — machine-to-machine, authenticated either by the shared
  internal token or by the endpoint family's dedicated machine credential.
* an ``(scope, Permission)`` pair — a domain operation requiring a verified
  principal and a server-held grant.

Public routes are listed explicitly instead of being derived from "has no auth
dependency today", because that derivation is what produced the bug this story
fixes: ``POST /internal/heartbeat``, ``POST /internal/cost-reconcile`` and all
twelve ``/api/v1/research/*`` routes have no auth dependency at present, and
inferring intent from that would bless the hole as the specification.
"""

from __future__ import annotations

from enum import StrEnum

from superplane_auth.policy import Permission


class RouteClass(StrEnum):
    """How a route is authenticated."""

    PUBLIC = "public"
    """No credential required. Reachable by anyone who can reach the service."""

    INTERNAL = "internal"
    """Machine credential only. Never a user-facing credential.

    Most internal routes use the shared token. Observation routes use their
    dedicated workspace-scoped credential and HMAC signature instead.
    """

    DOMAIN = "domain"
    """Verified principal plus a server-held grant."""


class Scope(StrEnum):
    """What the required authority is *over*."""

    WORKSPACE = "workspace"
    """Authority over one named workspace, resolved server-side."""

    ORGANIZATION = "organization"
    """Authority over the organization itself, or over a collection of its
    workspaces. Deliberately distinct from WORKSPACE: holding one workspace
    grant is not organization authority, and conflating the two is how a
    single-workspace member reaches an org-wide collection."""


# ---------------------------------------------------------------------------
# Where the workspace comes from, for workspace-scoped routes
# ---------------------------------------------------------------------------
#
# Always resolved by the server, from the matched route's path parameters. Every
# workspace-scoped route in this inventory carries the workspace in its path;
# there is no query-scoped workspace route today. If one is ever added, the
# selector must be a required query parameter whose absence is a denial rather
# than an unscoped read of everything — `app/domain_guard.py`'s
# `_resolve_workspace_id` already refuses a workspace-scoped route it cannot
# resolve, so the failure mode is closed rather than open.

WORKSPACE_PATH_PARAM = "workspace_id"


# ---------------------------------------------------------------------------
# The inventory
# ---------------------------------------------------------------------------
#
# Keyed by (METHOD, FastAPI path template). The template must be byte-identical
# to `route.path`, which is why the test walks the app rather than trusting
# these strings to have been typed correctly.

PUBLIC_ROUTES: frozenset[tuple[str, str]] = frozenset(
    {
        # Liveness/readiness. Probed by kubelet, which holds no credential.
        ("GET", "/health"),
        ("GET", "/readyz"),
        # API description. Serves no tenant data.
        ("GET", "/openapi.json"),
        ("GET", "/docs"),
        ("GET", "/docs/oauth2-redirect"),
        ("GET", "/redoc"),
        # These two ESTABLISH a credential, so requiring one would be circular.
        # They are not unprotected: login verifies an API key against its stored
        # hash and signup is the invite-only Cognito path.
        ("POST", "/auth/login"),
        ("POST", "/auth/signup"),
    }
)

INTERNAL_ROUTES: frozenset[tuple[str, str]] = frozenset(
    {
        ("GET", "/internal/installation"),
        ("POST", "/internal/controller/reconcile"),
        ("GET", "/api/v1/workspaces/{workspace_id}/bootstrap-observation"),
        ("POST", "/internal/controller/recovery/inventory"),
        ("POST", "/internal/controller/recovery/observe"),
        ("POST", "/internal/controller/recovery/lifecycle"),
        ("POST", "/internal/controller/recovery/account-creation"),
        ("POST", "/internal/controller/recovery/bootstrap"),
        ("POST", "/internal/controller/recovery/settlement"),
        ("PATCH", "/internal/clusters/{cluster_id}/resources"),
        ("POST", "/internal/vault-sync/trigger"),
        ("POST", "/internal/workspaces/{workspace_id}/reconcile"),
        # The two that had NO authentication before this story. Both live under
        # a prefix whose module docstring already claimed "machine-to-machine
        # use only" while three of its five routes enforced that and these two
        # did not. Heartbeat writes cluster health; cost-reconcile starts a
        # reconciliation cycle across every active workspace.
        ("POST", "/internal/heartbeat"),
        ("POST", "/internal/cost-reconcile"),
        # Observation receiver routes authenticate with their own
        # workspace-scoped submitter credential and HMAC signature. They are
        # INTERNAL so the domain guard records the decision and then leaves
        # authentication to that stricter endpoint-family authenticator.
        ("POST", "/internal/observations"),
        ("POST", "/internal/observations/leases"),
        ("POST", "/internal/observations/leases/release"),
        ("GET", "/internal/observations/clusters"),
        ("GET", "/internal/observations/{cluster_id}/cost-history"),
        ("POST", "/internal/observations/{cluster_id}/events"),
        ("GET", "/internal/observations/{cluster_id}"),
        # Provider-handle recording and reconciliation (issue #5054, U11c). Same
        # class and same reason as the observation routes above: the caller is an
        # adapter or B's recovery driver holding a workspace-scoped submitter
        # credential, never a user token. Authentication is that endpoint family's
        # authenticator; workspace claims are checked against its grant. Writes
        # additionally verify live B authority for the exact stored operation.
        ("POST", "/internal/provider-operations"),
        ("POST", "/internal/provider-operations/{idempotency_key}/conclude"),
        ("GET", "/internal/provider-operations"),
        (
            "POST",
            "/internal/provider-operations/allocations/{allocation_id}"
            "/release-assessment",
        ),
    }
)


# Domain routes: (METHOD, template) -> (Scope, Permission).
#
# Permission choices worth stating, because the route name misleads in both
# directions:
#
#   * `POST /workspaces/{id}/kubeconfig` is PROVISION, not READ. It reads like a
#     getter and it hands out live cluster credentials. This is R6 acc. 1's
#     regression target: today any holder of any of the org's API keys can call
#     it for any workspace in the org.
#   * `PATCH .../quota` is SPEND, not ADMINISTER. Raising a limit is the
#     authority to spend more, which is the effect that matters.
#   * Deleting a provider credential is RENEW_CREDENTIAL, matching the policy's
#     grouping of credential lifecycle operations.
# These require domain credentials and live workspace grants, but are deliberately
# absent from the public Gateway projection. INTERNAL_ROUTES means machine auth,
# so putting a private user-authenticated route there would weaken its boundary.
PRIVATE_DOMAIN_ROUTES: dict[tuple[str, str], tuple[Scope, Permission]] = {
    (
        "GET",
        "/internal/installation/workspaces/{workspace_id}/credential-evidence/{connection_id}",
    ): (Scope.WORKSPACE, Permission.RENEW_CREDENTIAL),
}


DOMAIN_ROUTES: dict[tuple[str, str], tuple[Scope, Permission]] = {
    ("GET", "/capabilities"): (Scope.ORGANIZATION, Permission.READ),
    # -- Workspace lifecycle -------------------------------------------------
    # Create/list are ORGANIZATION-scoped: on create there is no workspace yet,
    # and list is a collection across the org. The policy's `authorize_request`
    # refuses these against a workspace grant by design; they get the
    # organization path.
    #
    # `GET /workspaces` returns every workspace in the caller's org — it does
    # NOT narrow the result set per caller. Under the conjunctive definition of
    # organization authority in app/auth.py, narrowing would be a no-op anyway
    # (passing the check means holding grants across the whole org), so the two
    # are equivalent today. Recorded because the previous comment claimed a
    # per-caller filter that no handler performs.
    ("POST", "/workspaces"): (Scope.ORGANIZATION, Permission.PROVISION),
    ("GET", "/workspaces"): (Scope.ORGANIZATION, Permission.READ),
    # Issue #6048. Organization-scoped like `GET /workspaces` above: this lists
    # shared-placement eligibility across the caller's whole organization, not
    # one workspace, so it takes the same scope for the same reason.
    ("GET", "/workspaces/{workspace_id}"): (Scope.WORKSPACE, Permission.READ),
    ("DELETE", "/workspaces/{workspace_id}"): (
        Scope.WORKSPACE,
        Permission.PROVISION,
    ),
    # -- Cluster credentials -------------------------------------------------
    ("POST", "/workspaces/{workspace_id}/kubeconfig"): (
        Scope.WORKSPACE,
        Permission.PROVISION,
    ),
    ("GET", "/workspaces/{workspace_id}/nodes"): (Scope.WORKSPACE, Permission.READ),
    # -- Deployments spend money --------------------------------------------
    ("GET", "/workspaces/{workspace_id}/deployment-profiles"): (
        Scope.WORKSPACE,
        Permission.READ,
    ),
    ("GET", "/workspaces/{workspace_id}/deployments"): (
        Scope.WORKSPACE,
        Permission.READ,
    ),
    ("POST", "/workspaces/{workspace_id}/deployments"): (
        Scope.WORKSPACE,
        Permission.SPEND,
    ),
    ("DELETE", "/workspaces/{workspace_id}/deployments/{dep_id}"): (
        Scope.WORKSPACE,
        Permission.SPEND,
    ),
    ("POST", "/workspaces/{workspace_id}/deployments/preview"): (
        Scope.WORKSPACE,
        Permission.SPEND,
    ),
    ("POST", "/workspaces/{workspace_id}/deployments/{dep_id}/teardown-preview"): (
        Scope.WORKSPACE,
        Permission.SPEND,
    ),
    ("GET", "/workspaces/{workspace_id}/batch-jobs/{job_id}/result"): (
        Scope.WORKSPACE,
        Permission.READ,
    ),
    ("GET", "/workspaces/{workspace_id}/batch-jobs/{job_id}/accounting"): (
        Scope.WORKSPACE,
        Permission.READ,
    ),
    ("GET", "/workspaces/{workspace_id}/batch-jobs/{job_id}/observation"): (
        Scope.WORKSPACE,
        Permission.READ,
    ),
    ("GET", "/workspaces/{workspace_id}/deployments/{dep_id}/accounting"): (
        Scope.WORKSPACE,
        Permission.READ,
    ),
    ("GET", "/workspaces/{workspace_id}/deployments/{dep_id}/observation"): (
        Scope.WORKSPACE,
        Permission.READ,
    ),
    # -- Quota ---------------------------------------------------------------
    ("POST", "/workspaces/{workspace_id}/deployments/{dep_id}/cancellation"): (
        Scope.WORKSPACE,
        Permission.PROVISION,
    ),
    ("POST", "/workspaces/{workspace_id}/batch-jobs/{job_id}/cancellation"): (
        Scope.WORKSPACE,
        Permission.PROVISION,
    ),
    ("GET", "/workspaces/{workspace_id}/batch-profiles"): (
        Scope.WORKSPACE,
        Permission.READ,
    ),
    ("GET", "/workspaces/{workspace_id}/batch-jobs"): (
        Scope.WORKSPACE,
        Permission.READ,
    ),
    ("GET", "/workspaces/{workspace_id}/batch-jobs/{job_id}"): (
        Scope.WORKSPACE,
        Permission.READ,
    ),
    ("POST", "/workspaces/{workspace_id}/batch-jobs"): (
        Scope.WORKSPACE,
        Permission.SPEND,
    ),
    ("POST", "/workspaces/{workspace_id}/batch-jobs/preview"): (
        Scope.WORKSPACE,
        Permission.SPEND,
    ),
    ("POST", "/workspaces/{workspace_id}/batch-jobs/{job_id}/teardown-preview"): (
        Scope.WORKSPACE,
        Permission.SPEND,
    ),
    ("DELETE", "/workspaces/{workspace_id}/batch-jobs/{job_id}"): (
        Scope.WORKSPACE,
        Permission.SPEND,
    ),
    ("GET", "/workspaces/{workspace_id}/lifecycle"): (Scope.WORKSPACE, Permission.READ),
    ("GET", "/workspaces/{workspace_id}/quota"): (Scope.WORKSPACE, Permission.READ),
    ("PATCH", "/workspaces/{workspace_id}/quota"): (Scope.WORKSPACE, Permission.SPEND),
    # -- Cost / budget (workspace-scoped reads) ------------------------------
    ("GET", "/workspaces/{workspace_id}/cost"): (Scope.WORKSPACE, Permission.READ),
    ("GET", "/workspaces/{workspace_id}/budget"): (Scope.WORKSPACE, Permission.READ),
    # -- Events: org collection, filtered to the caller's workspaces ---------
    ("GET", "/events"): (Scope.ORGANIZATION, Permission.READ),
    ("GET", "/events/workspaces/{workspace_id}"): (Scope.WORKSPACE, Permission.READ),
    ("POST", "/workspaces/preview"): (Scope.ORGANIZATION, Permission.PROVISION),
    ("POST", "/workspaces/adopt"): (Scope.ORGANIZATION, Permission.PROVISION),
    ("POST", "/workspaces/{workspace_id}/retirement/preview"): (
        Scope.WORKSPACE,
        Permission.PROVISION,
    ),
    ("POST", "/workspaces/{workspace_id}/retirement"): (
        Scope.WORKSPACE,
        Permission.PROVISION,
    ),
    ("GET", "/workspaces/{workspace_id}/lifecycle-proposals"): (
        Scope.WORKSPACE,
        Permission.PROVISION,
    ),
    ("POST", "/workspaces/{workspace_id}/lifecycle-proposals/{artifact_id}/preview"): (
        Scope.WORKSPACE,
        Permission.PROVISION,
    ),
    ("POST", "/workspaces/{workspace_id}/lifecycle-proposals/{artifact_id}/continue"): (
        Scope.WORKSPACE,
        Permission.PROVISION,
    ),
    ("GET", "/operations/{operation_id}"): (Scope.ORGANIZATION, Permission.READ),
    ("GET", "/operations/by-idempotency/{idempotency_key}"): (
        Scope.ORGANIZATION,
        Permission.READ,
    ),
    # The approval service additionally checks the requester's exact workspace
    # grant and the selected distinct human approver on every read/decision.
    ("POST", "/operation-approvals"): (Scope.ORGANIZATION, Permission.READ),
    ("GET", "/operation-approvals/{approval_id}"): (
        Scope.ORGANIZATION,
        Permission.READ,
    ),
    ("POST", "/operation-approvals/{approval_id}/decision"): (
        Scope.ORGANIZATION,
        Permission.READ,
    ),
    ("GET", "/events/{event_id}"): (Scope.ORGANIZATION, Permission.READ),
    # -- Organization records ------------------------------------------------
    ("GET", "/orgs/current"): (Scope.ORGANIZATION, Permission.READ),
    ("PATCH", "/orgs/current"): (Scope.ORGANIZATION, Permission.ADMINISTER),
    ("GET", "/orgs/current/sso"): (Scope.ORGANIZATION, Permission.ADMINISTER),
    ("PATCH", "/orgs/current/sso"): (Scope.ORGANIZATION, Permission.ADMINISTER),
    ("DELETE", "/orgs/current/sso"): (Scope.ORGANIZATION, Permission.ADMINISTER),
    ("GET", "/orgs/current/quota"): (Scope.ORGANIZATION, Permission.READ),
    ("PATCH", "/orgs/current/quota"): (Scope.ORGANIZATION, Permission.ADMINISTER),
    ("GET", "/orgs/cost"): (Scope.ORGANIZATION, Permission.READ),
    # -- API keys mint org-wide credentials ----------------------------------
    ("POST", "/auth/token"): (Scope.ORGANIZATION, Permission.ADMINISTER),
    # -- Users ---------------------------------------------------------------
    ("GET", "/users"): (Scope.ORGANIZATION, Permission.READ),
    ("POST", "/users/invite"): (Scope.ORGANIZATION, Permission.ADMINISTER),
    ("DELETE", "/users/{user_id}"): (Scope.ORGANIZATION, Permission.ADMINISTER),
    ("PATCH", "/users/{user_id}/role"): (Scope.ORGANIZATION, Permission.ADMINISTER),
    # -- Cloud accounts and provider credentials -----------------------------
    ("GET", "/accounts"): (Scope.ORGANIZATION, Permission.READ),
    ("POST", "/accounts"): (Scope.ORGANIZATION, Permission.PROVISION),
    ("DELETE", "/accounts/{account_id}"): (Scope.ORGANIZATION, Permission.PROVISION),
    ("GET", "/vault/credentials"): (Scope.ORGANIZATION, Permission.READ),
    ("POST", "/vault/credentials"): (
        Scope.ORGANIZATION,
        Permission.RENEW_CREDENTIAL,
    ),
    ("DELETE", "/vault/credentials/{credential_id}"): (
        Scope.ORGANIZATION,
        Permission.RENEW_CREDENTIAL,
    ),
    # -- Provider connections and workspace bindings (issue #5053, U7b) ------
    #
    # WORKSPACE-scoped, not ORGANIZATION, and the distinction is load-bearing twice
    # over.
    #
    # First, correctness of the check: `authorize_delegation` requires
    # `workspace:renew_credential` from the caller's server-held grant, and
    # `app/domain_guard.py` publishes that grant on `request.state.grant` ONLY for
    # WORKSPACE-scoped routes. Registered as ORGANIZATION these routes would see an
    # empty permission set and deny every caller, however privileged — a failure that
    # passes every negative test, which is why `tests/test_workspaces.py` pins a
    # positive case per route too.
    #
    # Second, the tenant boundary these routes exist to enforce: organization
    # authority is explicitly NOT a workspace binding. Accepting it as one would mean
    # delegating a single credential effectively delegated every credential to
    # everyone in the org, which is the exposure R7 acceptance 2 is about.
    #
    # RENEW_CREDENTIAL throughout, matching the policy's grouping of credential
    # lifecycle operations and the `/vault/credentials` entries above. The read is
    # READ: it returns the four validation readings and no credential material.
    # DELETE disables rather than deletes — it blocks admissions and renewals and does
    # not revoke already-delivered credentials, which the response states.
    (
        "POST",
        "/workspaces/{workspace_id}/provider-connections",
    ): (Scope.WORKSPACE, Permission.RENEW_CREDENTIAL),
    (
        "GET",
        "/workspaces/{workspace_id}/provider-connections/{connection_id}",
    ): (Scope.WORKSPACE, Permission.READ),
    (
        "POST",
        "/workspaces/{workspace_id}/provider-connections/{connection_id}/validation",
    ): (Scope.WORKSPACE, Permission.RENEW_CREDENTIAL),
    (
        "POST",
        "/workspaces/{workspace_id}/provider-connections/{connection_id}/rotation",
    ): (Scope.WORKSPACE, Permission.RENEW_CREDENTIAL),
    (
        "DELETE",
        "/workspaces/{workspace_id}/provider-connections/{connection_id}",
    ): (Scope.WORKSPACE, Permission.RENEW_CREDENTIAL),
    # -- Research surface ----------------------------------------------------
    # Twelve routes with no authentication dependency at all before this story,
    # and still the only domain routes with no LEGACY per-route dependency
    # either — so under the default `domain_auth_enforced=False` these twelve
    # are reachable unauthenticated and only this guard closes them, once the
    # flag is on. Every other domain route retains its legacy gate meanwhile.
    # `/proposals/{id}/approve` additionally took its approver from the request
    # body, so the recorded approver was whatever the caller typed.
    ("GET", "/api/v1/research/findings"): (Scope.ORGANIZATION, Permission.READ),
    ("GET", "/api/v1/research/findings/{finding_id}"): (
        Scope.ORGANIZATION,
        Permission.READ,
    ),
    ("GET", "/api/v1/research/stats"): (Scope.ORGANIZATION, Permission.READ),
    ("GET", "/api/v1/research/sources"): (Scope.ORGANIZATION, Permission.READ),
    ("GET", "/api/v1/research/cli-support"): (Scope.ORGANIZATION, Permission.READ),
    ("GET", "/api/v1/research/proposals"): (Scope.ORGANIZATION, Permission.READ),
    ("GET", "/api/v1/research/proposals/stats"): (
        Scope.ORGANIZATION,
        Permission.READ,
    ),
    ("GET", "/api/v1/research/proposals/{proposal_id}"): (
        Scope.ORGANIZATION,
        Permission.READ,
    ),
    # A scan spends provider budget; generation writes proposals.
    ("POST", "/api/v1/research/scan"): (Scope.ORGANIZATION, Permission.SPEND),
    ("POST", "/api/v1/research/proposals"): (
        Scope.ORGANIZATION,
        Permission.ADMINISTER,
    ),
    ("POST", "/api/v1/research/proposals/generate"): (
        Scope.ORGANIZATION,
        Permission.ADMINISTER,
    ),
    # Approval moves a proposal into the experiment queue, so it is the
    # authority to commit the organization to work.
    ("PATCH", "/api/v1/research/proposals/{proposal_id}/approve"): (
        Scope.ORGANIZATION,
        Permission.ADMINISTER,
    ),
    ("PATCH", "/api/v1/research/proposals/{proposal_id}/reject"): (
        Scope.ORGANIZATION,
        Permission.ADMINISTER,
    ),
}


# No query-scoped workspace routes exist; see WORKSPACE_PATH_PARAM above.
#
# An empty `QUERY_SCOPED_WORKSPACE_PARAM` mapping with no readers used to sit
# here, described by comments naming "two query-scoped collection reads" this
# service does not serve. An empty enforcement table reads exactly like a
# populated one at a glance, which is the failure this module exists to prevent,
# so it is removed rather than left as a placeholder. Re-add it WITH the lookup
# that consumes it, not before.


class RouteNotInventoried(LookupError):
    """A mounted route has no recorded authorization decision.

    Raised rather than defaulted. A permissive default makes the route
    reachable and a restrictive one makes it silently dead; both hide that
    nobody decided. The middleware turns this into a 403 at request time and
    the inventory test turns it into a CI failure, which is the order of
    preference — fail in CI, fail closed in production.
    """


def classify(method: str, path_template: str) -> tuple[RouteClass, object]:
    """Classify one mounted route.

    Returns ``(RouteClass.PUBLIC, None)``, ``(RouteClass.INTERNAL, None)`` or
    ``(RouteClass.DOMAIN, (Scope, Permission))``. Raises
    :class:`RouteNotInventoried` for anything unrecorded.
    """
    key = (method.upper(), path_template)
    if key in PUBLIC_ROUTES:
        return RouteClass.PUBLIC, None
    if key in INTERNAL_ROUTES:
        return RouteClass.INTERNAL, None
    try:
        requirement = (
            PRIVATE_DOMAIN_ROUTES[key]
            if key in PRIVATE_DOMAIN_ROUTES
            else DOMAIN_ROUTES[key]
        )
        return RouteClass.DOMAIN, requirement
    except KeyError:
        raise RouteNotInventoried(
            f"{method.upper()} {path_template} has no recorded authorization decision"
        ) from None


def all_inventoried() -> frozenset[tuple[str, str]]:
    """Every (method, template) with a recorded decision, in any class."""
    return (
        PUBLIC_ROUTES
        | INTERNAL_ROUTES
        | frozenset(DOMAIN_ROUTES)
        | frozenset(PRIVATE_DOMAIN_ROUTES)
    )


# ---------------------------------------------------------------------------
# Enumerating what the app actually serves
# ---------------------------------------------------------------------------
#
# WHY THIS LIVES HERE RATHER THAN IN THE TEST THAT USES IT
#
# Issue #5682 (A02). The inventory is only an inventory because something walks
# the mounted app and fails when a route is missing from the tables above. Three
# separate test helpers used to do that walk, each by iterating `app.routes` and
# keeping `isinstance(route, APIRoute)`.
#
# That stopped finding anything. FastAPI now stores an included router as a single
# `_IncludedRouter` entry in `app.routes` and resolves its child routes on demand,
# so `app.routes` holds four Starlette docs routes and a dozen opaque router
# objects, and NONE of them is an `APIRoute`. Every one of those walks silently
# began enumerating zero endpoints. Measured on fastapi 0.141.1: `app.routes` has
# 21 entries and `sum(isinstance(r, APIRoute) for r in app.routes)` is 0, while the
# app really serves 73 operations.
#
# The failure mode is the one this module exists to prevent, one level up. A guard
# that examines nothing passes unconditionally, and a passing guard reads as
# "every route is classified" — so the inventory could have drifted arbitrarily far
# from the app without any test objecting. `assert not (mounted - inventoried)` is
# vacuously true when `mounted` is empty.
#
# So enumeration is defined ONCE, here, next to the tables it checks, and
# `mounted_operations()` raises when it finds nothing. An empty result is now a
# loud failure rather than a quiet pass.


def _walk(routes: list, out: set[tuple[str, str]]) -> None:
    """Collect (METHOD, template) from a routing table, descending into routers."""
    from fastapi.routing import APIRoute

    try:
        from fastapi.routing import _IncludedRouter
    except ImportError:  # pragma: no cover - older FastAPI without lazy routers
        _IncludedRouter = ()  # type: ignore[assignment]

    for route in routes:
        if _IncludedRouter and isinstance(route, _IncludedRouter):
            # `effective_candidates()` is how the router resolves its own children
            # for matching, so it yields exactly what the app can actually route to
            # — including routes hidden from the OpenAPI schema, which a
            # schema-based enumeration would miss. A hidden route is precisely the
            # kind that most needs a recorded decision.
            for candidate in route.effective_candidates():
                if isinstance(candidate, _IncludedRouter):
                    _walk([candidate], out)
                else:
                    for method in candidate.methods or ():
                        if method not in {"HEAD", "OPTIONS"}:
                            out.add((method.upper(), candidate.path))
            continue
        path = getattr(route, "path", None)
        if path is None:
            continue
        if isinstance(route, APIRoute) or path in _STARLETTE_DOC_ROUTES:
            for method in getattr(route, "methods", None) or ():
                if method not in {"HEAD", "OPTIONS"}:
                    out.add((method.upper(), path))
        nested = getattr(route, "routes", None)
        if nested:
            _walk(list(nested), out)


# The OpenAPI/docs routes Starlette mounts directly. They are plain `Route`
# objects, not `APIRoute`s, so they are named rather than type-matched — and they
# are in `PUBLIC_ROUTES`, so omitting them would make the inventory look stale.
_STARLETTE_DOC_ROUTES = frozenset(
    {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}
)


class NoRoutesEnumerated(RuntimeError):
    """Enumeration found no routes, so any check built on it proves nothing.

    Raised rather than returning an empty set, because an empty set makes every
    "no route escaped the inventory" assertion pass. This is the guard on the
    guard: it turns a framework change that breaks enumeration into an immediate,
    named failure instead of a test suite that goes quietly green.
    """


def mounted_operations(app) -> frozenset[tuple[str, str]]:
    """Every (METHOD, template) the given FastAPI app actually serves.

    Excludes HEAD and OPTIONS: Starlette synthesizes those, and they carry no
    authorization decision of their own.

    Raises :class:`NoRoutesEnumerated` if nothing is found — see that class for
    why an empty result must never be reported as success.
    """
    found: set[tuple[str, str]] = set()
    _walk(list(app.routes), found)
    if not found:
        raise NoRoutesEnumerated(
            "no routes could be enumerated from the app; the inventory checks "
            "built on this would pass without examining anything. The routing "
            "internals this walk depends on have probably changed — fix "
            "app/endpoint_inventory.py::_walk rather than the callers."
        )
    return frozenset(found)
