"""Strict domain auth policy and the per-workspace authorization model.

Issue #5044 (U9) — the ADP-side half of R5 and R6. This module owns two
decisions and nothing else:

1. **Is this token acceptable for a domain-API call?** (:func:`admit_token`)
2. **May this principal perform this operation on this workspace?**
   (:func:`authorize_operation`)

Enforcement is deliberately elsewhere. U14 wires these decisions into the
upstream domain API; this module is the model those call sites consult, so the
policy has one definition rather than one per entry point.

Domain ownership
----------------
This policy and its endpoint inventory are owned by the Superplane domain app.
It is a separately packaged, standard-library-only module, with no gateway
startup hook or production importer. Shared ADP token validation is unchanged.
U14 supplies the authenticated ingress and server-held authorization records.

The two properties worth stating plainly, because both are counter-intuitive
against the code that exists today:

**An access token is required, and an ID token is not "nearly" one.** The
Cognito validator accepts ``token_use`` of either ``access`` or ``id``, and
gates the client-allowlist check on ``token_use == "access"``
(``cognito_jwt.py``). An ID token therefore does not *fail* the allowlist — it
never reaches it. Populating an allowlist while accepting ID tokens buys
nothing, so the token-use check here runs *before* the client check and is
tested on its own.

**An empty allowlist is a startup failure, not a default.** The existing
validator documents ``allowed_client_ids=[]`` as "accept any client_id". That is
a reasonable default for a general-purpose validator and the wrong one for a
domain policy: a permitting default is indistinguishable from having no policy
at all, and it fails open exactly when someone forgets to configure it. So
:class:`DomainTokenPolicy` refuses to be constructed without at least one
client.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum

# ---------------------------------------------------------------------------
# Validation-path boundary (R5: "the story names which validation paths are in
# scope")
# ---------------------------------------------------------------------------
#
# Two token-validation paths exist in ADP and they are NOT equally strict:
#
#   * `modules/gateway/src/auth/cognito_jwt.py` — signature via JWKS, issuer,
#     expiry, `token_use`, and an optional client allowlist.
#   * `modules/gateway/lambda/api-authorizer/handler.py` — signature via JWKS,
#     expiry and issuer. It checks NO `token_use` and NO client allowlist, so an
#     ID token from any app client in the pool satisfies it.
#
# Naming them is load-bearing rather than documentary. If the domain API accepted
# "authenticated by any ADP path", the weaker path would satisfy the stricter
# path's policy and the allowlist would be decorative. So the in-scope set is an
# allowlist of validation paths, and `admit_token` requires the caller to say
# which path produced the claims.

TRUSTED_VALIDATION_PATH = "gateway.cognito_jwt"
"""The only validation path whose output may authorize a domain-API call."""

REJECTED_VALIDATION_PATHS: frozenset[str] = frozenset(
    {
        # Issuer + signature + expiry only. Sufficient for the ALB-gated proxy
        # routes it guards; NOT sufficient here, because it cannot distinguish an
        # ID token from an access token and enforces no client allowlist.
        "gateway.api_authorizer",
    }
)
"""Validation paths that authenticate a caller but may not admit one here."""

IN_SCOPE_VALIDATION_PATHS: frozenset[str] = (
    frozenset({TRUSTED_VALIDATION_PATH}) | REJECTED_VALIDATION_PATHS
)
"""Every path this policy has an opinion about. An unknown path is refused."""


# ---------------------------------------------------------------------------
# Identity headers stripped at ingress (R5 acc. 6-7)
# ---------------------------------------------------------------------------
#
# These carry identity when injected by a trusted ingress and are forgeries when
# they arrive from a client. The gateway already treats `X-Caller-Identity`
# presence as terminal (issue #3985) rather than falling through to the JWT path.
# The same rule is applied here to the whole family, because "strip the ones we
# thought of" is the failure mode: a header added later for a new plane is
# trusted by default unless the rule is stated as a prefix.

STRIPPED_IDENTITY_HEADER_PREFIXES: tuple[str, ...] = (
    "x-adp-",
    "x-caller-",
    "x-superplane-",
    "x-agent-",
    "x-auth-",
    "x-amzn-",
    "x-forwarded-",
)
"""Header prefixes removed from client input before any decision is made."""

STRIPPED_IDENTITY_HEADERS: frozenset[str] = frozenset(
    {
        "authorization-context",
        "x-authenticated-user",
        "x-org-id",
        "x-workspace-id",
        "x-on-behalf-of",
    }
)
"""Exact header names removed in addition to the prefixes above."""


def strip_identity_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Remove every client-supplied header that could assert identity.

    Applied to the raw request before a principal exists. Returns a new mapping
    with keys lowercased; the surviving headers are ordinary request metadata.

    This is a *removal*, not a rejection: a client sending ``X-Org-Id`` is not
    necessarily attacking (a proxy may add one), but the value must not survive
    to a decision. Rejecting instead would turn a benign intermediary into an
    outage, while keeping the value would make the header authority.
    """
    kept: dict[str, str] = {}
    for name, value in headers.items():
        lowered = name.lower()
        if lowered in STRIPPED_IDENTITY_HEADERS:
            continue
        if any(
            lowered.startswith(prefix) for prefix in STRIPPED_IDENTITY_HEADER_PREFIXES
        ):
            continue
        kept[lowered] = value
    return kept


# ---------------------------------------------------------------------------
# Permissions and roles (R6)
# ---------------------------------------------------------------------------


class Permission(StrEnum):
    """What a caller may do inside one workspace.

    Deliberately coarse. These are *domain* permissions, and they are separate
    from ADP's role names on purpose (see :data:`ADP_ROLE_PERMISSIONS`): the two
    products do not share a role vocabulary and assuming they did is how a role
    rename upstream silently widens access here.
    """

    READ = "workspace:read"
    """Describe the workspace, list its nodes, read cost and events."""

    SPEND = "workspace:spend"
    """Anything that consumes budget: deployments, quota increases."""

    PROVISION = "workspace:provision"
    """Create or destroy capacity, and obtain cluster credentials."""

    RENEW_CREDENTIAL = "workspace:renew_credential"
    """Register, rotate or delete a provider credential binding."""

    ADMINISTER = "workspace:administer"
    """Change the workspace's own authorization records."""


# READ is implied by every other permission — a caller who may spend may
# obviously describe. Nothing else is implied: PROVISION does not confer SPEND
# and SPEND does not confer PROVISION, because "may deploy a model" and "may
# hand out a kubeconfig" are different blast radii and R6 acc. 4 asks for a
# read-authorized caller to be unable to reach either.
_IMPLIED: Mapping[Permission, frozenset[Permission]] = {
    Permission.SPEND: frozenset({Permission.READ}),
    Permission.PROVISION: frozenset({Permission.READ}),
    Permission.RENEW_CREDENTIAL: frozenset({Permission.READ}),
    Permission.ADMINISTER: frozenset(
        {
            Permission.READ,
            Permission.SPEND,
            Permission.PROVISION,
            Permission.RENEW_CREDENTIAL,
        }
    ),
}


def expand_permissions(granted: Iterable[Permission]) -> frozenset[Permission]:
    """Close a permission set over the implications above."""
    result: set[Permission] = set()
    for permission in granted:
        result.add(permission)
        result |= _IMPLIED.get(permission, frozenset())
    return frozenset(result)


# ADP role name -> domain permissions. The mapping is explicit and total: a role
# absent from this table grants nothing, so an unrecognized or renamed role is a
# denial rather than a default. R6's design note is specific that ADP roles map
# to domain permissions "without assuming the products' role names match" — hence
# a table rather than passing the role string through.
ADP_ROLE_PERMISSIONS: Mapping[str, frozenset[Permission]] = {
    "workspace_viewer": frozenset({Permission.READ}),
    "workspace_operator": frozenset({Permission.SPEND}),
    "workspace_provisioner": frozenset(
        {Permission.PROVISION, Permission.RENEW_CREDENTIAL}
    ),
    "workspace_owner": frozenset({Permission.ADMINISTER}),
}


def permissions_for_adp_role(role: str) -> frozenset[Permission]:
    """Translate one ADP role name into domain permissions.

    An unknown role yields the empty set. This is the fail-closed direction: a
    role that ADP adds, or renames, is unprivileged here until someone decides
    what it means, rather than inheriting whatever the name resembles.
    """
    return expand_permissions(ADP_ROLE_PERMISSIONS.get(role, frozenset()))


# ---------------------------------------------------------------------------
# Endpoint inventory (R6 acc. 2)
# ---------------------------------------------------------------------------
#
# Every endpoint this EPIC adds, with the permission it requires. The point is
# not documentation — it is that `required_permission()` raises on an endpoint
# that is absent, so adding an endpoint without deciding its permission fails a
# test instead of shipping reachable.
#
# The existing coverage this replaces is inconsistent rather than uniformly
# missing, which is why a partial inventory would be worse than none: quota
# endpoints have no RBAC at all, some endpoints claim org-admin only in a
# docstring, and `/internal/*` endpoints have no auth dependency. And role
# enforcement is absent rather than partial — no production-issuable token
# carries a role claim, so `require_role("org-admin")` endpoints are unreachable
# by any real token. A caller cannot currently *hold* a role, so this model
# resolves permissions from a stored grant (below) instead.

WORKSPACE_PATH_PLACEHOLDER = "{workspace}"

ENDPOINT_INVENTORY: Mapping[tuple[str, str], Permission] = {
    # Workspace lifecycle. `create` and `list` are org-scoped: there is no
    # workspace yet. Their required permissions are inventoried, but U14 must
    # implement an org-scoped grant path; the workspace composer refuses them.
    ("POST", "/superplane/v1/workspaces"): Permission.PROVISION,
    ("GET", "/superplane/v1/workspaces"): Permission.READ,
    ("GET", f"/superplane/v1/workspaces/{WORKSPACE_PATH_PLACEHOLDER}"): Permission.READ,
    # Cluster credentials. R6 acc. 1's regression target: this succeeds today for
    # any org-mate of the workspace's org. A kubeconfig is cluster access, so it
    # is PROVISION, not READ — the name "get kubeconfig" reads like a read.
    (
        "GET",
        f"/superplane/v1/workspaces/{WORKSPACE_PATH_PLACEHOLDER}/kubeconfig",
    ): Permission.PROVISION,
    (
        "GET",
        f"/superplane/v1/workspaces/{WORKSPACE_PATH_PLACEHOLDER}/nodes",
    ): Permission.READ,
    # Deployments spend money.
    (
        "GET",
        f"/superplane/v1/workspaces/{WORKSPACE_PATH_PLACEHOLDER}/deployments",
    ): Permission.READ,
    (
        "POST",
        f"/superplane/v1/workspaces/{WORKSPACE_PATH_PLACEHOLDER}/deployments",
    ): Permission.SPEND,
    (
        "DELETE",
        f"/superplane/v1/workspaces/{WORKSPACE_PATH_PLACEHOLDER}/deployments/{{deployment}}",
    ): Permission.SPEND,
    # Quota. Reading a limit is READ; raising one is the authority to spend more.
    (
        "GET",
        f"/superplane/v1/workspaces/{WORKSPACE_PATH_PLACEHOLDER}/quota",
    ): Permission.READ,
    (
        "PATCH",
        f"/superplane/v1/workspaces/{WORKSPACE_PATH_PLACEHOLDER}/quota",
    ): Permission.SPEND,
    # Cost and events are workspace-scoped reads via a query parameter.
    ("GET", "/superplane/v1/cost/summary"): Permission.READ,
    ("GET", "/superplane/v1/events"): Permission.READ,
    # Cloud accounts and provider credentials.
    ("GET", "/superplane/v1/accounts"): Permission.READ,
    ("POST", "/superplane/v1/accounts"): Permission.PROVISION,
    ("DELETE", "/superplane/v1/accounts/{account}"): Permission.PROVISION,
    ("GET", "/superplane/v1/providers"): Permission.READ,
    ("POST", "/superplane/v1/providers"): Permission.RENEW_CREDENTIAL,
    ("DELETE", "/superplane/v1/providers/{credential}"): Permission.RENEW_CREDENTIAL,
}


# Scope is explicit for every inventoried endpoint. Cost/events are workspace
# queries: U14 must require a workspace selector and resolve it server-side.
# No arbitrary workspace grant may stand in for authority over org collections.
ENDPOINT_SCOPES: Mapping[tuple[str, str], str] = {
    endpoint: "workspace"
    if "{workspace}" in endpoint[1]
    or endpoint[1] in {"/superplane/v1/cost/summary", "/superplane/v1/events"}
    else "organization"
    for endpoint in ENDPOINT_INVENTORY
}


class InventoryError(LookupError):
    """An endpoint has no recorded permission.

    Raised rather than returning a default, because every default is wrong: a
    permissive one makes the endpoint reachable and a restrictive one makes it
    silently dead. Both hide the actual problem, which is that nobody decided.
    """


def required_permission(method: str, path_template: str) -> Permission:
    """The permission an endpoint requires, or raise :class:`InventoryError`."""
    try:
        return ENDPOINT_INVENTORY[(method.upper(), path_template)]
    except KeyError:
        raise InventoryError(
            f"{method.upper()} {path_template} is not in the endpoint inventory; its required permission is undecided"
        ) from None


# ---------------------------------------------------------------------------
# Token policy (R5 ADP half)
# ---------------------------------------------------------------------------


class TokenPolicyError(ValueError):
    """The policy itself is misconfigured. Raised at construction, not per call.

    Separate from :class:`TokenRejectedError` on purpose: this is an operator error
    that should stop the process, while a rejected token is a normal 401.
    """


class TokenRejectedError(PermissionError):
    """A token is not acceptable for a domain-API call."""


@dataclass(frozen=True)
class DomainPrincipal:
    """Who the caller is, resolved entirely from verified token context.

    Every field here comes from a validated claim. Nothing on this object can be
    influenced by a request body or a client header, which is the property that
    makes it safe to pass into :func:`authorize_operation`.
    """

    subject: str
    """Opaque ADP principal id. Never parsed for meaning."""

    org_id: str
    """Verified organization, optionally resolved through an explicit server-held
    ADP-to-domain organization binding. Never a request-body ``org_id``. The API
    retains the source claim separately when applying such a binding.
    """

    client_id: str
    """The Cognito app client that minted the token."""

    account_type: str
    """``"human"`` or ``"service"``."""

    @property
    def is_service(self) -> bool:
        return self.account_type == "service"


class DomainTokenPolicy:
    """Admits ADP access tokens from an allowlisted client, and nothing else.

    Constructed once at startup. An empty or unset allowlist raises
    :class:`TokenPolicyError` here rather than at first request, so a
    misconfigured deployment fails to start instead of serving with no policy.
    """

    def __init__(
        self, allowed_client_ids: Iterable[str] | None, expected_issuer: str
    ) -> None:
        clients = frozenset(
            c.strip() for c in (allowed_client_ids or ()) if c and c.strip()
        )
        if not clients:
            raise TokenPolicyError(
                "domain auth requires a non-empty client allowlist; refusing to start. "
                "An empty allowlist would accept a token from any app client in the user pool, "
                "which is indistinguishable from having no policy."
            )
        if not expected_issuer or not expected_issuer.strip():
            raise TokenPolicyError(
                "domain auth requires an expected token issuer; refusing to start"
            )
        self.allowed_client_ids = clients
        self.expected_issuer = expected_issuer.strip()

    def admit(
        self, claims: Mapping[str, object], *, validation_path: str
    ) -> DomainPrincipal:
        """Admit a validated token, or raise :class:`TokenRejectedError`.

        ``claims`` must already be signature-verified; this method decides
        *policy*, not authenticity. ``validation_path`` names which validator
        produced them and is required — see the module note on why "authenticated
        by some path" is not sufficient.
        """
        # Validation-path boundary first. A token admitted through the weaker
        # path is refused before any claim is read, so the stricter policy cannot
        # be satisfied by the weaker one.
        if validation_path not in IN_SCOPE_VALIDATION_PATHS:
            raise TokenRejectedError(f"unknown validation path: {validation_path}")
        if validation_path != TRUSTED_VALIDATION_PATH:
            raise TokenRejectedError(
                f"validation path {validation_path} authenticates but may not admit a domain-API call; "
                f"it enforces neither token_use nor a client allowlist"
            )

        # Token use BEFORE client id. An ID token skips the client check in the
        # underlying validator, so checking the client first would let an ID
        # token through on a technicality.
        token_use = claims.get("token_use")
        if token_use != "access":
            raise TokenRejectedError(
                f"domain auth requires an access token; got token_use={token_use!r}"
            )

        issuer = claims.get("iss")
        if issuer != self.expected_issuer:
            raise TokenRejectedError("token issuer is not the expected ADP issuer")

        client_id = claims.get("client_id")
        if not isinstance(client_id, str) or client_id not in self.allowed_client_ids:
            # Does not echo the client id: a denial should not confirm which
            # values are close to allowlisted.
            raise TokenRejectedError("token client is not allowlisted for domain auth")

        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject:
            raise TokenRejectedError("token has no subject")

        # The principal's org comes from the verified claim. A caller may put any
        # `org_id` in a request body; it never reaches this object, so it can
        # never become authority.
        org_id = claims.get("custom:org_id") or claims.get("org_id") or ""
        if not isinstance(org_id, str) or not org_id:
            raise TokenRejectedError("token carries no organization claim")

        account_type = claims.get("custom:account_type") or claims.get("account_type")
        if not isinstance(account_type, str) or account_type not in {
            "human",
            "service",
        }:
            raise TokenRejectedError("token must carry a recognized account type")

        return DomainPrincipal(
            subject=subject,
            org_id=org_id,
            client_id=client_id,
            account_type=str(account_type),
        )


# ---------------------------------------------------------------------------
# Environment state assertion (R5 acc. 5)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EnforcementState:
    """The credential-binding flags as configured in a *target environment*.

    Asserted, never inferred from code defaults. Both flags are per-environment
    and neither default is the enforcing value in every environment:
    ``ENFORCE_CREDENTIAL_BINDING`` has a shadow mode that stays reachable by
    configuration, and ``VAULT_ENFORCE_CREDENTIAL_HOST_BINDING`` is unenforced by
    default. Reading the dataclass defaults in this repository and calling that
    "the environment's state" is exactly the inference acc. 5 forbids, so both
    fields are required arguments with no defaults.
    """

    environment: str
    enforce_credential_binding: bool
    vault_enforce_credential_host_binding: bool

    def unenforced(self) -> tuple[str, ...]:
        """Flags not enforcing in this environment. Empty means fully enforcing."""
        gaps = []
        if not self.enforce_credential_binding:
            gaps.append("ENFORCE_CREDENTIAL_BINDING")
        if not self.vault_enforce_credential_host_binding:
            gaps.append("VAULT_ENFORCE_CREDENTIAL_HOST_BINDING")
        return tuple(gaps)


def assert_enforcement_state(
    state: EnforcementState, *, require: Iterable[str]
) -> None:
    """Assert named flags enforce in ``state``, or raise :class:`TokenPolicyError`.

    ``require`` is explicit so a caller states which flags its operation depends
    on, rather than this function deciding that every flag must enforce
    everywhere — shadow mode is a legitimate per-environment configuration.
    """
    required = set(require)
    unknown = required - {
        "ENFORCE_CREDENTIAL_BINDING",
        "VAULT_ENFORCE_CREDENTIAL_HOST_BINDING",
    }
    if unknown:
        raise TokenPolicyError(f"unknown enforcement flags: {sorted(unknown)}")
    missing = sorted(required.intersection(state.unenforced()))
    if missing:
        raise TokenPolicyError(
            f"environment {state.environment!r} does not enforce {missing}"
        )


# ---------------------------------------------------------------------------
# Authorization model (R6 ADP half)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WorkspaceGrant:
    """A server-held record that a principal may act on one workspace.

    This record — not the caller's org, and not the session that authenticated
    it — is the authority. Its existence is the thing R6 adds: no membership
    concept exists on either side today, and handlers filter on
    ``Workspace.org_id == org_id`` alone, so every org-mate reaches every
    workspace.
    """

    workspace_id: str
    org_id: str
    principal: str
    permissions: frozenset[Permission]

    def allows(self, permission: Permission) -> bool:
        return permission in self.permissions


@dataclass(frozen=True)
class OperationAuthorization:
    """A server-held record that one operation was authorized.

    R5 acc. 6-7: authentication is not authorization, and a service token's
    ability to authenticate does not delegate any user's authority to it. So a
    sensitive operation binds to a record the server issued and holds. A
    fabricated envelope arriving with the request is not one of these, which is
    the whole point of requiring the object rather than a claim in the payload.
    """

    operation_id: str
    workspace_id: str
    principal: str
    permission: Permission


class AuthorizationDeniedError(PermissionError):
    """The operation is not authorized. Carries a caller-safe reason."""


@dataclass
class WorkspaceAuthorizationModel:
    """The per-workspace authorization decision, re-checked at each operation.

    Grants are keyed by ``(workspace_id, principal)``. The store is injected as a
    plain mapping so this model is testable and so U14 can back it with the
    upstream table without this logic changing.
    """

    grants: dict[tuple[str, str], WorkspaceGrant] = field(default_factory=dict)

    def record_grant(self, grant: WorkspaceGrant) -> None:
        self.grants[(grant.workspace_id, grant.principal)] = grant

    def grant_for(self, workspace_id: str, principal: str) -> WorkspaceGrant | None:
        return self.grants.get((workspace_id, principal))

    def authorize(
        self,
        principal: DomainPrincipal,
        workspace_id: str,
        permission: Permission,
        *,
        workspace_org_id: str,
    ) -> WorkspaceGrant:
        """Decide whether ``principal`` may exercise ``permission`` on a workspace.

        ``workspace_org_id`` is the workspace's *stored* org, used only to refuse
        a cross-org grant that should not exist. It is never sufficient on its
        own: matching it is a precondition, and the grant is the authority.
        """
        grant = self.grant_for(workspace_id, principal.subject)
        if grant is None:
            # The org-mate case. The principal authenticated for this org and the
            # workspace belongs to it, and it is still a denial — this single
            # branch is what R6 acc. 1 asks for.
            raise AuthorizationDeniedError(
                "no workspace authorization record for this principal"
            )

        # Defence in depth against a grant stored against the wrong org.
        if grant.org_id != workspace_org_id or principal.org_id != workspace_org_id:
            raise AuthorizationDeniedError(
                "workspace authorization record does not match the workspace's organization"
            )

        if not grant.allows(permission):
            # Names the missing permission but not what the caller does hold:
            # enumerating their grant is information about the estate.
            raise AuthorizationDeniedError(
                f"workspace authorization does not include {permission.value}"
            )

        return grant

    def authorize_operation(
        self,
        principal: DomainPrincipal,
        authorization: OperationAuthorization | None,
        workspace_id: str,
        permission: Permission,
        *,
        workspace_org_id: str,
    ) -> WorkspaceGrant:
        """Re-check authority at the operation, against a server-held record.

        Called at admission, at each sensitive operation and at credential
        renewal — R6's "re-checked at the operation" is a *repeat* of this call,
        not a single check whose result is cached on a session. A cached result
        is authority inherited from the session, which is the thing being
        removed.

        ``authorization`` must be the record the server issued for *this*
        operation. Passing ``None`` is refused: an operation with no server-held
        authorization record has nothing binding it to a principal.
        """
        if authorization is None:
            raise AuthorizationDeniedError(
                "operation has no server-held authorization record"
            )
        if authorization.workspace_id != workspace_id:
            raise AuthorizationDeniedError(
                "authorization record is for a different workspace"
            )
        if authorization.principal != principal.subject:
            # A service token holding a record issued for a human principal is
            # the delegation R5 acc. 6-7 refuses.
            raise AuthorizationDeniedError(
                "authorization record was issued to a different principal"
            )
        if authorization.permission != permission:
            raise AuthorizationDeniedError(
                "authorization record does not cover this operation"
            )
        return self.authorize(
            principal, workspace_id, permission, workspace_org_id=workspace_org_id
        )


def authorize_request(
    policy: DomainTokenPolicy,
    model: WorkspaceAuthorizationModel,
    *,
    claims: Mapping[str, object],
    validation_path: str,
    method: str,
    path_template: str,
    workspace_id: str,
    workspace_org_id: str,
    authorization: OperationAuthorization | None,
    headers: Mapping[str, str] | None = None,
) -> tuple[DomainPrincipal, WorkspaceGrant, dict[str, str]]:
    """The whole decision, in the order the properties depend on each other.

    Provided so an entry point makes one call and cannot accidentally do the
    steps out of order or skip one. Returns principal, grant and sanitized headers;
    ingress consumers must forward only that returned mapping. The input mapping
    remains untouched. Org-scoped routes are deliberately refused until U14 supplies
    their separate grant/operation path. Workspace IDs and ownership must be resolved
    by the server, including query-scoped cost/events and nested resource ownership.
    Header stripping comes first because the
    later steps must not be able to read a client-supplied identity even by
    mistake; the endpoint's permission is resolved from the inventory rather than
    passed in, so an endpoint missing from the inventory raises here.
    """
    safe_headers = strip_identity_headers(headers or {})
    principal = policy.admit(claims, validation_path=validation_path)
    permission = required_permission(method, path_template)
    if ENDPOINT_SCOPES[(method.upper(), path_template)] != "workspace":
        raise AuthorizationDeniedError(
            "endpoint requires an org-scoped authorization path; a workspace grant is insufficient"
        )
    grant = model.authorize_operation(
        principal,
        authorization,
        workspace_id,
        permission,
        workspace_org_id=workspace_org_id,
    )
    return principal, grant, safe_headers
