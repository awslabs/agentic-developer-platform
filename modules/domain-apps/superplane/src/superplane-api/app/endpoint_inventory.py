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
DOMAIN_ROUTES: dict[tuple[str, str], tuple[Scope, Permission]] = {
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
    # -- Quota ---------------------------------------------------------------
    ("GET", "/workspaces/{workspace_id}/quota"): (Scope.WORKSPACE, Permission.READ),
    ("PATCH", "/workspaces/{workspace_id}/quota"): (Scope.WORKSPACE, Permission.SPEND),
    # -- Cost / budget (workspace-scoped reads) ------------------------------
    ("GET", "/workspaces/{workspace_id}/cost"): (Scope.WORKSPACE, Permission.READ),
    ("GET", "/workspaces/{workspace_id}/budget"): (Scope.WORKSPACE, Permission.READ),
    # -- Events: org collection, filtered to the caller's workspaces ---------
    ("GET", "/events"): (Scope.ORGANIZATION, Permission.READ),
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
        return RouteClass.DOMAIN, DOMAIN_ROUTES[key]
    except KeyError:
        raise RouteNotInventoried(
            f"{method.upper()} {path_template} has no recorded authorization decision"
        ) from None


def all_inventoried() -> frozenset[tuple[str, str]]:
    """Every (method, template) with a recorded decision, in any class."""
    return PUBLIC_ROUTES | INTERNAL_ROUTES | frozenset(DOMAIN_ROUTES)
