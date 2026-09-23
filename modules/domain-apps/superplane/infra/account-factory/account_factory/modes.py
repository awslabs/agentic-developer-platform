"""Ownership modes for Account Factory requests — Issue #5530 (w6-07), EPIC #4910.

## What the legacy implementation assumed, and why that is a defect

The reference Account Factory (read as source evidence at ADP
`98ab544d52cdb96c84b724ea78a74ddb007dd864`) had exactly one story, and it was implicit.
`deploy.sh` ran `00`→`04` in sequence: validate, create IAM roles, install cluster-wide
controllers, apply resource graphs, then create a brand-new AWS account with a brand-new
VPC and a brand-new EKS cluster inside it. There was no way to express any other shape of
request, and therefore no way for an unsupported request to be *refused* — the question was
never asked.

Three consequences followed from that, all of which this module closes.

**Unsupported requests could not fail early, because they could not be stated.** If the
account already existed, `04-provision-account.sh` still applied an `Account` custom
resource naming it. If a cluster already existed, `FullAccountInfrastructure` still
declared an `EKSClusterStack`. The failure surfaced from a controller, mid-reconcile,
after a namespace, an `IAMRoleSelector`, and possibly a VPC and NAT gateway already
existed — so an unsupported request became a partial mutation to clean up by hand, rather
than a refusal.

**Three distinct kinds of ownership were collapsed into one config file.** `config.env`
held `AWS_ACCOUNT_ID=605440105851` and `CLUSTER_NAME=github-arc-runner-eks` side by side
with `ACCOUNT_NAME=superplane-test`, as though "which organization may this act in",
"which cluster runs the controllers", and "which workspace is this for" were one fact.
They are not, and conflating them is what let a run act on an organization nobody selected
using a cluster nobody named. This module keeps them as three separate required fields and
checks each against the authorization the run was given.

**The target was defaulted.** Because `config.env` supplied values, a run that supplied
nothing still acted on a specific real account belonging to someone else. There are no
defaults here: every identity is required, and validation refuses the legacy literals
outright even if they are supplied deliberately, because this module is not the owner of
that account.

## The three modes

| Mode | ADP creates the account? | ADP creates the cluster? |
|---|---|---|
| ``new-account-managed`` | yes | yes |
| ``existing-account-managed`` | no — it is adopted | yes |
| ``bring-existing-cluster`` | no | no — it is adopted |

The distinction is not cosmetic: it decides what may be *created*, and symmetrically what
may be *deleted* (see `cleanup.py`). ADP does not delete a cluster it did not create, and
it never closes an account as a consequence of removing a workspace.

## Validation reports everything, then refuses

`validate` accumulates every problem and returns them all, rather than raising on the
first. The legacy `00-validate-prerequisites.sh` used a `check` helper that counted
failures and reported them together — that part was right and is kept. What it did NOT do
is gate provisioning on the result in a way that held: `deploy.sh` invoked it under a
`trap ... ERR`, but the script's own `((FAIL++))` arithmetic and its `|| echo "UNKNOWN"`
fallbacks meant a failed check could still leave the run continuing to step 1. Here,
refusal is the return value's meaning, and nothing renders from an invalid request
(`render.py` calls `ensure_valid` before producing a single object).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

__all__ = [
    "LEGACY_FORBIDDEN_VALUES",
    "AccountFactoryRequest",
    "ClusterOwnership",
    "ModeError",
    "OwnershipMode",
    "ValidationAuthorization",
    "ensure_valid",
    "from_mapping",
    "validate",
]


class ModeError(Exception):
    """A request could not be validated, or an invalid request was used anyway.

    Raised by `ensure_valid` only. `validate` returns problems rather than raising, so a
    caller can report all of them; this exists so that a code path which *skips* validation
    still cannot proceed — the failure is loud rather than a silently permissive default.
    """


class OwnershipMode(str, Enum):
    """The supported request shapes. Anything else is refused before mutation.

    A `str` enum so a mode survives YAML/JSON round-tripping as its wire value, matching
    `OperationState` in ../../../contracts/superplane_contracts/provisioning.py.
    """

    NEW_ACCOUNT_MANAGED = "new-account-managed"
    EXISTING_ACCOUNT_MANAGED = "existing-account-managed"
    BRING_EXISTING_CLUSTER = "bring-existing-cluster"

    @property
    def creates_account(self) -> bool:
        """Whether ADP creates the AWS account in this mode."""
        return self is OwnershipMode.NEW_ACCOUNT_MANAGED

    @property
    def creates_cluster(self) -> bool:
        """Whether ADP creates the EKS cluster in this mode."""
        return self is not OwnershipMode.BRING_EXISTING_CLUSTER


class ClusterOwnership(str, Enum):
    """Who owns the cluster a workspace runs on.

    Recorded separately from `OwnershipMode` because the mode describes the REQUEST and
    this describes the resulting RESOURCE. `cleanup.py` branches on this: a cluster ADP
    adopted is never deleted by ADP, whichever request produced the adoption.
    """

    ADP_CREATED = "adp-created"
    ADOPTED = "adopted"


# The legacy target literals, refused by value.
#
# These are the defaults the reference `config.env` shipped. They are not ADP's: the
# account is upstream's management account (the same value ../../../releases/superplane.lock.yaml
# already refuses to adopt, for the same reason), the cluster is the ARC runner cluster
# that hosts core ADP workloads, and the email is a named individual's address.
#
# Refusing them by value — rather than merely not defaulting them — is deliberate. Not
# defaulting stops an ACCIDENT; refusing stops a COPY-PASTE, which is the likelier way a
# legacy value returns (someone reads the reference README and pastes its example). The
# management cluster entry also protects ADP's own cluster from being named as the
# provisioning target of a domain workspace.
LEGACY_FORBIDDEN_VALUES: dict[str, str] = {
    "605440105851": (
        "the legacy management account id from the reference config.env. It is not ADP's "
        "account; supply the account this run is authorized for"
    ),
    "github-arc-runner-eks": (
        "the legacy management cluster name from the reference config.env. That cluster "
        "hosts core ADP workloads (ARC runners); a domain workspace must not name it"
    ),
    "prsaws+aisuperplane@amazon.com": (
        "the legacy child-account email default from the reference config.env. It is a "
        "named individual's address, not a value this module may reuse"
    ),
    "superplane-test": (
        "the legacy fixed account name from the reference config.env. A fixed account "
        "name makes two independent requests collide on one target"
    ),
}

_ACCOUNT_ID_RE = re.compile(r"^\d{12}$")
_ORG_ID_RE = re.compile(r"^o-[a-z0-9]{10,32}$")
# AWS Organizational Unit id. Required for new-account-managed by #5531 (w6-08): a created
# account lands SOMEWHERE in the organization tree, and the OU decides which service control
# policies and guardrails apply to it from its first moment. Omitting it does not mean "no
# OU" — it means the organization ROOT, which is the least restricted placement available.
# So an un-stated OU is not a neutral default; it is the most permissive one, chosen by
# nobody. Required and compared, never defaulted.
_OU_ID_RE = re.compile(r"^ou-[a-z0-9]{4,32}-[a-z0-9]{8,32}$")
# Deliberately not a full RFC 5322 implementation: this rejects obviously-unusable values
# so a request fails here rather than inside the Organizations API, and nothing more.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s.]+\.[^@\s]+$")
# Kubernetes object-name shape (RFC 1123 label), because the workspace id becomes a
# namespace and a custom-resource name in the rendered set.
_NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")
_REGION_RE = re.compile(r"^[a-z]{2}(-[a-z]+)+-\d$")
_CLUSTER_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,99}$")

# Core ADP / Kubernetes namespaces a domain workspace may never occupy. Same list and same
# reasoning as `CORE_NAMESPACES` in ../scripts/check_rendered_manifests.py: the workspace
# id becomes a namespace, so an unchecked workspace id is a route into a core namespace.
_CORE_NAMESPACES = frozenset(
    {
        "adp",
        "adp-gateway",
        "adp-agent-factory",
        "adp-context",
        "adp-system",
        "bedrockgw",
        "kube-system",
        "kube-public",
        "kube-node-lease",
        "default",
        "arc-systems",
        "arc-runners",
        "kro-system",
        "ack-system",
    }
)


@dataclass(frozen=True)
class ValidationAuthorization:
    """What this run was authorized to act on.

    Compared against the request. This is the check the legacy flow had no place to perform:
    it verified that the CREDENTIALS resolved to `AWS_ACCOUNT_ID` from its own config file,
    which only ever confirmed that the config matched itself.

    ## Why `workspace_id` is here, and why it is authority-side

    A request names the workspace it acts on and the account it targets. Without the
    corresponding authorization fields, neither was ever compared against anything: a caller
    supplying every supported authorization value could render and plan cleanup for a
    workspace that was not theirs, and validation would report it as fully verified — the
    worst of the two possible failures, because "verified" and "never checked" looked alike.

    Workspace identity therefore belongs on the AUTHORIZATION side, never taken from the
    request. That is the same rule the shared provisioning contract states for the same
    reason: in ../../../contracts/superplane_contracts/provisioning.py, `ProvisioningIntent`
    deliberately carries no workspace, because the workspace an operation acts on comes from
    the binding's resolved principal — a caller cannot name the tenant it provisions for.
    `from_operation_binding` is the supported way to build one of these, so the comparison is
    against server-resolved identity rather than a value the caller also supplied.

    All fields stay optional so the same validator runs offline before any authorization has
    been issued — but an absent field means the comparison is NOT MADE, which `validate`
    records explicitly rather than counting as a pass.
    """

    organization_id: str | None = None
    management_account_id: str | None = None
    management_cluster: str | None = None
    permitted_modes: frozenset[OwnershipMode] | None = None
    # The workspace this run may act on. Authority-owned: derived from a binding's resolved
    # principal, not from the request being validated.
    workspace_id: str | None = None
    # The target accounts this run may adopt or act in, for the two modes that name one. A
    # set rather than a single value because an operator may be authorized for several; an
    # EMPTY set is meaningful and authorizes none, which is why the absent case is `None`.
    permitted_target_accounts: frozenset[str] | None = None
    # The organizational units a created account may be placed into (#5531, w6-08). Only
    # meaningful for new-account-managed, which is the only mode that places an account.
    #
    # An EMPTY set authorizes no placement at all, which is why the absent case is `None`
    # and reported as unchecked: "this run may place into no OU" and "nobody said which OUs
    # this run may place into" are different facts, and only the second one is a missing
    # check. Collapsing them would let an unauthorized placement read as a verified pass.
    permitted_organizational_units: frozenset[str] | None = None

    @classmethod
    def from_operation_binding(
        cls,
        binding: object,
        *,
        management_account_id: str | None = None,
        management_cluster: str | None = None,
        permitted_modes: frozenset[OwnershipMode] | None = None,
        permitted_target_accounts: frozenset[str] | None = None,
        permitted_organizational_units: frozenset[str] | None = None,
    ) -> ValidationAuthorization:
        """Derive authorization from a provisioning `OperationBinding`.

        Takes the organization and workspace from the binding's resolved principal — the
        fields the facade resolved server-side, which nothing in a request body can influence.
        Duck-typed (`binding.principal.workspace_id`) rather than imported, because this
        module must stay importable and testable without the contracts package on the path;
        the type is enforced by the facade that issues the binding, and constructing a binding
        object is explicitly not proof of provenance either way.

        The management account, cluster and permitted modes stay parameters: they describe
        which management cluster this run operates FROM, which the provisioning binding does
        not speak about.
        """
        principal = getattr(binding, "principal", None)
        if principal is None:
            raise ModeError(
                "an operation binding must carry a resolved principal; without one there is "
                "no server-resolved workspace to compare a request against"
            )
        workspace_id = getattr(principal, "workspace_id", None)
        organization_id = getattr(principal, "org_id", None)
        if not workspace_id or not organization_id:
            raise ModeError(
                "an operation binding's principal must resolve both a workspace and an "
                "organization"
            )
        return cls(
            organization_id=str(organization_id),
            management_account_id=management_account_id,
            management_cluster=management_cluster,
            permitted_modes=permitted_modes,
            workspace_id=str(workspace_id),
            permitted_target_accounts=permitted_target_accounts,
            permitted_organizational_units=permitted_organizational_units,
        )


@dataclass(frozen=True)
class AccountFactoryRequest:
    """One provisioning request. Carries identities, never credentials.

    Every identity is required and none is defaulted — see the module docstring. The
    three ownership questions are separate fields on purpose:

    * `organization_id` — which AWS Organization this acts in (organization ownership);
    * `management_account_id` / `management_cluster` — which account and cluster run the
      controllers doing the acting (management-cluster ownership);
    * `workspace_id` — which Superplane workspace the result serves (workspace ownership).

    `mode` is typed as `OwnershipMode`, so an unknown mode cannot be constructed at all
    from Python. A string mode arriving from a config file is converted by `from_mapping`,
    which is where an unknown value becomes a reported problem rather than an exception.
    """

    mode: OwnershipMode
    organization_id: str
    management_account_id: str
    management_cluster: str
    region: str
    workspace_id: str
    # Required in existing-account-managed and bring-existing-cluster; must be absent in
    # new-account-managed, where the account does not exist yet and an id supplied for it
    # can only be wrong.
    target_account_id: str | None = None
    # Required in new-account-managed only: the address the new account is created with.
    account_email: str | None = None
    # Required in new-account-managed only (#5531, w6-08): where in the organization tree
    # the created account is placed. Forbidden in the adopted modes, where the account
    # already sits somewhere and moving it is not this request's to do — a value supplied
    # there would either be ignored (so its author was wrong about what the request does) or
    # acted on (so a workspace request silently re-parents an existing account).
    organizational_unit_id: str | None = None
    # Required in bring-existing-cluster only: the cluster ADP adopts rather than creates.
    existing_cluster_name: str | None = None
    vpc_cidr: str | None = None
    availability_zones: tuple[str, ...] = ()
    cluster_version: str | None = None
    node_instance_type: str | None = None

    @property
    def cluster_ownership(self) -> ClusterOwnership:
        """Who owns the resulting cluster. Drives `cleanup.py`'s delete boundary."""
        return (
            ClusterOwnership.ADP_CREATED
            if self.mode.creates_cluster
            else ClusterOwnership.ADOPTED
        )

    @property
    def cluster_name(self) -> str:
        """The cluster this request results in.

        In `bring-existing-cluster` that is the adopted cluster's real name; otherwise the
        cluster ADP creates, named after the workspace. Exposed as one property so callers
        do not re-derive this rule and disagree with it.
        """
        if self.mode is OwnershipMode.BRING_EXISTING_CLUSTER:
            # Guaranteed present by validation; the fallback keeps this total for an
            # unvalidated request rather than raising from a property.
            return self.existing_cluster_name or ""
        return self.workspace_id


@dataclass
class _Problems:
    """Accumulator, so validation reports every problem instead of the first."""

    items: list[str] = field(default_factory=list)

    def add(self, problem: str) -> None:
        self.items.append(problem)


def _check_legacy_values(
    request_values: dict[str, object], problems: _Problems
) -> None:
    """Refuse the legacy target literals wherever they appear."""
    for field_name, value in request_values.items():
        if not isinstance(value, str):
            continue
        for forbidden, why in LEGACY_FORBIDDEN_VALUES.items():
            if value.strip().lower() == forbidden.lower():
                problems.add(
                    f"{field_name}={value!r} is {why}. Legacy fixed targets are refused by "
                    f"value, not merely un-defaulted"
                )


def _check_shapes(request: AccountFactoryRequest, problems: _Problems) -> None:
    if not _ORG_ID_RE.match(request.organization_id or ""):
        problems.add(
            f"organization_id={request.organization_id!r} is not an AWS Organization id "
            f"(expected o-xxxxxxxxxx)"
        )
    if not _ACCOUNT_ID_RE.match(request.management_account_id or ""):
        problems.add(
            f"management_account_id={request.management_account_id!r} is not a 12-digit "
            f"AWS account id"
        )
    if not _CLUSTER_NAME_RE.match(request.management_cluster or ""):
        problems.add(
            f"management_cluster={request.management_cluster!r} is not a valid EKS cluster "
            f"name"
        )
    if not _REGION_RE.match(request.region or ""):
        problems.add(f"region={request.region!r} is not an AWS region")
    if not _NAME_RE.match(request.workspace_id or ""):
        problems.add(
            f"workspace_id={request.workspace_id!r} is not a valid Kubernetes object name; "
            f"it becomes a namespace and a custom-resource name in the rendered set"
        )
    elif request.workspace_id in _CORE_NAMESPACES:
        problems.add(
            f"workspace_id={request.workspace_id!r} is a core ADP or Kubernetes namespace. "
            f"A domain workspace owns its own namespace (platform isolation requirement)"
        )
    if request.target_account_id is not None and not _ACCOUNT_ID_RE.match(
        request.target_account_id
    ):
        problems.add(
            f"target_account_id={request.target_account_id!r} is not a 12-digit AWS "
            f"account id"
        )
    if request.account_email is not None and not _EMAIL_RE.match(request.account_email):
        problems.add(f"account_email={request.account_email!r} is not an email address")
    if request.organizational_unit_id is not None and not _OU_ID_RE.match(
        request.organizational_unit_id
    ):
        problems.add(
            f"organizational_unit_id={request.organizational_unit_id!r} is not an AWS "
            f"organizational unit id (expected ou-xxxx-xxxxxxxx). An organization ROOT id "
            f"(r-xxxx) is refused here specifically: the root is the least restricted "
            f"placement in the organization, so accepting it would put a new account outside "
            f"every guardrail an OU carries"
        )
    if request.existing_cluster_name is not None and not _CLUSTER_NAME_RE.match(
        request.existing_cluster_name
    ):
        problems.add(
            f"existing_cluster_name={request.existing_cluster_name!r} is not a valid EKS "
            f"cluster name"
        )
    for zone in request.availability_zones:
        if not zone.startswith(request.region or "\0"):
            problems.add(
                f"availability_zone {zone!r} is not in region {request.region!r}"
            )


def _check_mode_fields(request: AccountFactoryRequest, problems: _Problems) -> None:
    """Each mode requires exactly its own fields, and forbids the others'.

    Forbidding is as important as requiring. A `target_account_id` supplied alongside
    `new-account-managed` is not harmlessly redundant — it means the caller believes the
    account exists while the request says to create one, and one of those two beliefs is
    about to act on the wrong account.
    """
    mode = request.mode
    if mode is OwnershipMode.NEW_ACCOUNT_MANAGED:
        if request.target_account_id is not None:
            problems.add(
                "target_account_id must be absent in new-account-managed: the account does "
                "not exist yet, so any id supplied for it names a different account"
            )
        if not request.account_email:
            problems.add(
                "account_email is required in new-account-managed: AWS Organizations "
                "requires a unique address per account and this module does not default one"
            )
        if not request.organizational_unit_id:
            problems.add(
                "organizational_unit_id is required in new-account-managed: a created "
                "account is placed somewhere in the organization tree, and an unstated "
                "placement is not 'no OU' — it is the organization ROOT, the least "
                "restricted placement available. That default must be chosen deliberately "
                "or not at all, so it is required rather than defaulted"
            )
        if request.existing_cluster_name is not None:
            problems.add(
                "existing_cluster_name must be absent in new-account-managed: the cluster "
                "is created by this request"
            )
    elif mode is OwnershipMode.EXISTING_ACCOUNT_MANAGED:
        if not request.target_account_id:
            problems.add(
                "target_account_id is required in existing-account-managed: the account is "
                "adopted, so it must be named"
            )
        if request.account_email is not None:
            problems.add(
                "account_email must be absent in existing-account-managed: the account "
                "already exists and its address is not this request's to set"
            )
        if request.existing_cluster_name is not None:
            problems.add(
                "existing_cluster_name must be absent in existing-account-managed: the "
                "cluster is created by this request"
            )
        if request.organizational_unit_id is not None:
            problems.add(
                "organizational_unit_id must be absent in existing-account-managed: the "
                "account already sits somewhere in the organization tree. Acting on this "
                "value would re-parent an existing account as a side effect of a workspace "
                "request; ignoring it would mislead its author. Refused instead"
            )
    elif mode is OwnershipMode.BRING_EXISTING_CLUSTER:
        if not request.target_account_id:
            problems.add(
                "target_account_id is required in bring-existing-cluster: the cluster lives "
                "in an existing account, which must be named"
            )
        if not request.existing_cluster_name:
            problems.add(
                "existing_cluster_name is required in bring-existing-cluster: the cluster "
                "being adopted must be named"
            )
        if request.account_email is not None:
            problems.add(
                "account_email must be absent in bring-existing-cluster: no account is "
                "created"
            )
        if request.organizational_unit_id is not None:
            problems.add(
                "organizational_unit_id must be absent in bring-existing-cluster: no "
                "account is created, so there is no placement to choose"
            )
        for unusable, why in (
            ("vpc_cidr", "the VPC already exists"),
            ("cluster_version", "the cluster already exists and is not upgraded here"),
            ("node_instance_type", "the node group already exists"),
        ):
            if getattr(request, unusable) is not None:
                problems.add(
                    f"{unusable} must be absent in bring-existing-cluster: {why}, so this "
                    f"value would be silently ignored"
                )
        if request.availability_zones:
            problems.add(
                "availability_zones must be absent in bring-existing-cluster: no subnets "
                "are created, so these would be silently ignored"
            )
    else:  # pragma: no cover - defensive; OwnershipMode is exhaustive above
        problems.add(f"unhandled ownership mode: {mode!r}")

    if mode.creates_cluster:
        for required in ("vpc_cidr", "availability_zones", "cluster_version"):
            if not getattr(request, required):
                problems.add(
                    f"{required} is required when this request creates a cluster "
                    f"({mode.value})"
                )


def _check_authorization(
    request: AccountFactoryRequest,
    authorization: ValidationAuthorization | None,
    problems: _Problems,
) -> list[str]:
    """Compare the request against what the run was authorized for.

    Returns the comparisons NOT made, so an absent authorization field is reported as
    unchecked rather than counted as a pass. That distinction is the whole reason this
    returns anything: "the management account was not verified" and "the management account
    matched" must not look alike in a report.

    Two of these comparisons decide WHOSE resources a run may touch:

    * `workspace_id` — the workspace is a namespace, a set of object names and a cleanup
      target, so an uncompared workspace lets an authorized caller render into, and plan
      deletes against, a tenant that is not theirs.
    * `target_account_id` — the account a run adopts or acts inside.

    Both are compared against authority-resolved values (see `ValidationAuthorization`),
    never against another field of the same request, which would only confirm the request
    agrees with itself — the exact shape of the legacy defect described in this module's
    docstring.
    """
    applicable_target = _target_account_applies(request)
    applicable_ou = _organizational_unit_applies(request)

    if authorization is None:
        return [
            "organization_id",
            "management_account_id",
            "management_cluster",
            "mode",
            "workspace_id",
            *(["target_account_id"] if applicable_target else []),
            *(["organizational_unit_id"] if applicable_ou else []),
        ]

    unchecked: list[str] = []

    if authorization.organization_id is None:
        unchecked.append("organization_id")
    elif authorization.organization_id != request.organization_id:
        problems.add(
            f"organization_id={request.organization_id!r} is not the organization this run "
            f"is authorized for ({authorization.organization_id!r}). Acting in another "
            f"organization is refused"
        )

    if authorization.management_account_id is None:
        unchecked.append("management_account_id")
    elif authorization.management_account_id != request.management_account_id:
        problems.add(
            f"management_account_id={request.management_account_id!r} is not the management "
            f"account this run is authorized for "
            f"({authorization.management_account_id!r}). The legacy check compared the "
            f"config file against itself and could not catch this"
        )

    if authorization.management_cluster is None:
        unchecked.append("management_cluster")
    elif authorization.management_cluster != request.management_cluster:
        problems.add(
            f"management_cluster={request.management_cluster!r} is not the cluster this run "
            f"is authorized to act from ({authorization.management_cluster!r})"
        )

    if authorization.permitted_modes is None:
        unchecked.append("mode")
    elif request.mode not in authorization.permitted_modes:
        permitted = ", ".join(
            sorted(mode.value for mode in authorization.permitted_modes)
        )
        problems.add(
            f"mode={request.mode.value!r} is not permitted for this run (permitted: "
            f"{permitted or 'none'})"
        )

    if authorization.workspace_id is None:
        unchecked.append("workspace_id")
    elif authorization.workspace_id != request.workspace_id:
        problems.add(
            f"workspace_id={request.workspace_id!r} is not the workspace this run is "
            f"authorized for ({authorization.workspace_id!r}). The workspace decides the "
            f"namespace rendered into and the objects a cleanup plan deletes, so acting on "
            f"another workspace is refused"
        )

    if not applicable_target:
        # new-account-managed names no target account (validation forbids one), so there is
        # nothing to compare. Recording it as `unchecked` would report a missing check that
        # does not exist, which is its own kind of misleading.
        pass
    elif authorization.permitted_target_accounts is None:
        unchecked.append("target_account_id")
    elif request.target_account_id not in authorization.permitted_target_accounts:
        permitted_accounts = ", ".join(sorted(authorization.permitted_target_accounts))
        problems.add(
            f"target_account_id={request.target_account_id!r} is not an account this run is "
            f"authorized to act in (permitted: {permitted_accounts or 'none'}). Adopting an "
            f"account nobody authorized is refused"
        )

    if not applicable_ou:
        # Only new-account-managed places an account; the adopted modes are required to
        # leave this field absent, so there is nothing a comparison could apply to.
        pass
    elif authorization.permitted_organizational_units is None:
        unchecked.append("organizational_unit_id")
    elif (
        request.organizational_unit_id
        not in authorization.permitted_organizational_units
    ):
        permitted_units = ", ".join(
            sorted(authorization.permitted_organizational_units)
        )
        problems.add(
            f"organizational_unit_id={request.organizational_unit_id!r} is not an "
            f"organizational unit this run may place an account into (permitted: "
            f"{permitted_units or 'none'}). The OU decides which service control policies "
            f"apply to the account from its first moment, so placing into a unit nobody "
            f"authorized is refused"
        )

    return unchecked


def _target_account_applies(request: AccountFactoryRequest) -> bool:
    """Whether this request names a target account that a comparison could apply to.

    False for new-account-managed, where `_check_mode_fields` requires the id to be ABSENT
    because the account does not exist yet. Kept as one function so `validate` and the
    `authorization is None` branch agree about which comparisons are applicable; two copies
    of this rule would eventually disagree, and the disagreement would show up as a check
    reported when it was not made.
    """
    return request.mode is not OwnershipMode.NEW_ACCOUNT_MANAGED and bool(
        request.target_account_id
    )


def _organizational_unit_applies(request: AccountFactoryRequest) -> bool:
    """Whether this request names an OU placement a comparison could apply to.

    True only for new-account-managed, the one mode that places an account in the
    organization tree. Kept as one function for the same reason as
    `_target_account_applies`: `validate` and the `authorization is None` branch must agree
    about which comparisons are applicable, or a check will be reported as missing when it
    never existed — which misleads in the opposite direction from reporting an unmade check
    as a pass, but misleads nonetheless.
    """
    return request.mode is OwnershipMode.NEW_ACCOUNT_MANAGED and bool(
        request.organizational_unit_id
    )


def validate(
    request: AccountFactoryRequest,
    authorization: ValidationAuthorization | None = None,
) -> tuple[list[str], list[str]]:
    """Validate a request without mutating anything.

    Returns ``(problems, unchecked)``. An empty ``problems`` list means the request may be
    rendered. ``unchecked`` names authorization comparisons that were not performed because
    no authorization value was supplied for them — reported so a caller can state what was
    verified rather than implying everything was.

    This performs no AWS or Kubernetes call. It is deliberately usable with no credentials,
    because the point is to refuse before anything can be mutated.
    """
    problems = _Problems()

    if not isinstance(request.mode, OwnershipMode):
        # Reachable when a caller bypasses `from_mapping` and passes a raw string.
        problems.add(
            f"mode={request.mode!r} is not a supported ownership mode. Supported: "
            f"{', '.join(mode.value for mode in OwnershipMode)}"
        )
        return problems.items, []

    _check_legacy_values(
        {
            "organization_id": request.organization_id,
            "management_account_id": request.management_account_id,
            "management_cluster": request.management_cluster,
            "workspace_id": request.workspace_id,
            "target_account_id": request.target_account_id,
            "account_email": request.account_email,
            "existing_cluster_name": request.existing_cluster_name,
            "organizational_unit_id": request.organizational_unit_id,
        },
        problems,
    )
    _check_shapes(request, problems)
    _check_mode_fields(request, problems)
    unchecked = _check_authorization(request, authorization, problems)

    return problems.items, unchecked


def ensure_valid(
    request: AccountFactoryRequest,
    authorization: ValidationAuthorization | None = None,
) -> list[str]:
    """Validate, raising `ModeError` on any problem. Returns the unchecked comparisons.

    Called by `render.py` and `cleanup.py` before they produce anything, so an invalid
    request cannot reach a rendered object set or a delete plan even if a caller forgot to
    check `validate`'s result.
    """
    problems, unchecked = validate(request, authorization)
    if problems:
        detail = "\n".join(f"  - {problem}" for problem in problems)
        raise ModeError(
            f"the request was refused before any mutation ({len(problems)} problem(s)):\n"
            f"{detail}"
        )
    return unchecked


def from_mapping(data: object) -> AccountFactoryRequest:
    """Build a request from parsed configuration (YAML/JSON), refusing unknown fields.

    Unknown keys are refused rather than ignored, because a misspelled
    ``existing_cluster_nmae`` silently ignored is a request that provisions a NEW cluster
    while its author believed they were adopting one.

    An unknown ``mode`` raises `ModeError` here with the supported set named — this is the
    "unsupported mode fails before mutation" path for config-file input.
    """
    if not isinstance(data, dict):
        raise ModeError(f"a request must be a mapping, not {type(data).__name__}")

    known = {
        "mode",
        "organization_id",
        "management_account_id",
        "management_cluster",
        "region",
        "workspace_id",
        "target_account_id",
        "account_email",
        "organizational_unit_id",
        "existing_cluster_name",
        "vpc_cidr",
        "availability_zones",
        "cluster_version",
        "node_instance_type",
    }
    unknown = sorted(set(data) - known)
    if unknown:
        raise ModeError(
            f"unknown request field(s): {', '.join(unknown)}. Refused rather than ignored: "
            f"a misspelled field silently dropped changes what gets created"
        )

    missing = sorted(
        name
        for name in (
            "mode",
            "organization_id",
            "management_account_id",
            "management_cluster",
            "region",
            "workspace_id",
        )
        if not data.get(name)
    )
    if missing:
        raise ModeError(
            f"missing required request field(s): {', '.join(missing)}. None of these is "
            f"defaulted — a defaulted target is how a run acts on an account nobody chose"
        )

    raw_mode = data["mode"]
    try:
        mode = OwnershipMode(raw_mode)
    except ValueError as exc:
        supported = ", ".join(mode.value for mode in OwnershipMode)
        raise ModeError(
            f"unknown ownership mode {raw_mode!r}. Supported: {supported}. Refused before "
            f"any mutation"
        ) from exc

    zones = data.get("availability_zones") or ()
    if isinstance(zones, str):
        raise ModeError(
            "availability_zones must be a list, not a string: a comma-joined string would "
            "be read as one zone name"
        )
    if not all(isinstance(zone, str) for zone in zones):
        raise ModeError("availability_zones must be a list of strings")

    def _optional_str(name: str) -> str | None:
        value = data.get(name)
        if value is None:
            return None
        if not isinstance(value, str):
            raise ModeError(f"{name} must be a string, not {type(value).__name__}")
        return value

    return AccountFactoryRequest(
        mode=mode,
        organization_id=str(data["organization_id"]),
        management_account_id=str(data["management_account_id"]),
        management_cluster=str(data["management_cluster"]),
        region=str(data["region"]),
        workspace_id=str(data["workspace_id"]),
        target_account_id=_optional_str("target_account_id"),
        account_email=_optional_str("account_email"),
        organizational_unit_id=_optional_str("organizational_unit_id"),
        existing_cluster_name=_optional_str("existing_cluster_name"),
        vpc_cidr=_optional_str("vpc_cidr"),
        availability_zones=tuple(zones),
        cluster_version=_optional_str("cluster_version"),
        node_instance_type=_optional_str("node_instance_type"),
    )
