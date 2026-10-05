"""Gate 0: the access prerequisites must exist, be attributed, and be recorded.

Issue #5533 (w6-10), EPIC #4910. Added by the F4 repair.

## What was optional and should never have been

The first revision took `inventory: PrerequisiteInventory | None = None` on
`bootstrap_workspace` and never looked at it. Review finding F4: the scoped EKS access
entry and the security-group rules a workspace cannot function without were an
*optional argument*, unverified by anything, so a bootstrap could report a fully
successful, registered, ready workspace having established no ownership record at all.

Two distinct harms follow, and neither is recoverable after the fact:

- **Retirement cannot revoke what was never recorded.** `retire.py` treats the
  inventory as the authoritative list of what cleanup may touch. An access entry or an
  ingress rule created during bootstrap and recorded nowhere becomes permanent by
  accident — an access path outliving the workspace it existed for, and on a supplied
  cluster (AC-02) a hole ADP left in infrastructure it does not own.
- **The workspace may not actually be reachable.** The management plane reaches the
  workspace API server through a private endpoint rule. Without it, every gate after
  this one either fails confusingly or — worse — passes against something else.

So this gate is mandatory, it runs BEFORE any cluster mutation, and its record is
required by both registration and retirement.

## Why it runs before the first mutation, not alongside it

An access prerequisite is what makes the cluster reachable at all. Verifying it after
creating a namespace means the failure arrives with an owned object already on the
cluster and a cleanup plan to execute — strictly worse than the same refusal one step
earlier, which leaves the cluster untouched. This is the cheapest gate to pass and the
cheapest to fail, so it goes first.

## Attribution is checked against authoritative reads, never against the request

Every expectation this module compares against comes from the workspace Terraform
module's published outputs (`../infra/workspaces/outputs.tf`), and every observation
comes from a read of the live resource. The caller's parameters are deliberately not a
source of truth for anything:

- The **account** must be the account the provider says we are in, not the account the
  request named. `target.py` already establishes this rule for the cluster; a
  security-group rule in the wrong account is the same class of error.
- The **VPC** must be the VPC Terraform published for this workspace. A rule attached
  to a different VPC is either a wrong-environment mistake or a rule opening a path
  into unrelated infrastructure.
- The **org and workspace attribution** must match the bound operation's principal.
  This is the identity rule the whole package follows: identity comes from
  `OperationBinding.principal`, never from a request parameter, because a request
  parameter is caller-controlled and would let one tenant record a prerequisite
  against another's workspace.
- The **exact source and target** of each rule must match, and each rule's own
  identifier must be recorded. "A rule exists between these groups" is not the same
  fact as "this rule, which ADP created, exists between these groups" — and only the
  second one can be revoked precisely at retirement.

## Created versus adopted, from the read and not from a flag

`ownership` is derived from whether the authoritative read showed the resource already
present before this bootstrap acted. A caller cannot assert `adp-created`: that value
authorizes deletion, and `inventory.OwnedPrerequisite.removable` computes from it. A
pre-existing rule is adopted, recorded, reported, and never removed — the same
asymmetry `components.py` applies to a pre-existing namespace and for the same AC-02
reason.

## This module records; it does not create

Creation belongs to whatever holds the credential for that resource type — provider
objects to #5534's provisioning provider, cluster objects to the installer. This gate
*observes* through a seam, *validates* attribution, and *records*. Keeping the two
apart is what makes a creation path that forgot to record visible as a missing
inventory entry rather than as nothing at all, and it is why the whole gate is
assertable offline.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from .errors import BootstrapRefused
from .inventory import (
    ADOPTED,
    ADP_CREATED,
    OwnedPrerequisite,
    PrerequisiteInventory,
)
from .state import BootstrapState, StateStore, record
from .target import VerifiedTarget

# The prerequisite kinds a workspace bootstrap establishes outside Terraform's state,
# each with why it cannot be Terraform-managed.
#
# `../infra/workspaces/eks.tf` sets `authentication_mode = "API"` with
# `bootstrap_cluster_creator_admin_permissions = false` and declares no
# `aws_eks_access_entry` at all, so cluster access is this story's job by explicit
# design rather than by omission.
ACCESS_ENTRY = "EksAccessEntry"

# The private-endpoint rule the management plane reaches the workspace API server
# through, and the management-to-workspace rule. Separate kinds because they have
# different sources and a refusal should name which one is wrong.
ENDPOINT_RULE = "SecurityGroupRule/cluster-endpoint"
MANAGEMENT_RULE = "SecurityGroupRule/private-sts"

REQUIRED_PREREQUISITE_KINDS: tuple[str, ...] = (
    ACCESS_ENTRY,
    ENDPOINT_RULE,
    MANAGEMENT_RULE,
)

# The EKS access policy the workspace access entry must be scoped to, and the
# namespace scope it must be confined to. An access entry scoped to the CLUSTER rather
# than to the workspace namespace is cluster-admin by another name, which would make
# every namespace boundary this package establishes advisory.
REQUIRED_ACCESS_SCOPE = "namespace"

# Rule fields a refusal must be able to name individually. Listed so a missing field
# is a refusal identifying WHICH fact was unobtainable, rather than a generic
# "malformed rule" — the operator action differs per field.
_RULE_FIELDS: tuple[str, ...] = (
    "rule_id",
    "group_id",
    "source",
    "vpc_id",
    "account_id",
    "port",
    "protocol",
)


@dataclass(frozen=True)
class ExpectedPrerequisites:
    """What Terraform published, as the authoritative expectation.

    Built from `../infra/workspaces/outputs.tf` by the caller and passed in whole, so
    this module never re-derives an expectation from a caller parameter. Every field is
    required: a blank expectation would compare successfully against anything, which is
    the failure mode an optional prerequisite already demonstrated.
    """

    account_id: str
    vpc_id: str
    cluster_security_group_id: str
    management_security_group_id: str
    node_security_group_id: str
    sts_endpoint_security_group_id: str
    sts_endpoint_vpc_id: str
    api_server_port: int = 443
    protocol: str = "tcp"
    retained_sts_rule_id: str | None = None

    def __post_init__(self) -> None:
        if self.retained_sts_rule_id is not None and (
            not isinstance(self.retained_sts_rule_id, str)
            or not self.retained_sts_rule_id.startswith("sgr-")
        ):
            raise BootstrapRefused(
                "retained private STS requires its exact reviewed rule identity"
            )
        for name in (
            "account_id",
            "vpc_id",
            "cluster_security_group_id",
            "management_security_group_id",
            "node_security_group_id",
            "sts_endpoint_security_group_id",
            "sts_endpoint_vpc_id",
            "protocol",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise BootstrapRefused(
                    f"ExpectedPrerequisites.{name} is required; a blank expectation "
                    "compares equal to anything and would verify nothing"
                )
        if not isinstance(self.api_server_port, int) or isinstance(
            self.api_server_port, bool
        ):
            raise BootstrapRefused(
                "ExpectedPrerequisites.api_server_port must be an int"
            )
        if not 1 <= self.api_server_port <= 65535:
            raise BootstrapRefused(
                f"ExpectedPrerequisites.api_server_port {self.api_server_port} is not "
                "a valid port"
            )


@runtime_checkable
class PrerequisiteAccess(Protocol):
    """Authoritative reads of the access prerequisites. Creates nothing.

    A Protocol for the same reason `ClusterAccess` is: the production implementation
    holds the credential, this package holds the decisions, and the whole gate is
    therefore assertable offline. Every method returns an observation — an identifier,
    a scope, a port — and there is no method that could return a credential.
    """

    def access_entry(
        self, cluster_arn: str, principal_arn: str
    ) -> Mapping[str, object]:
        """The EKS access entry for this principal on this cluster.

        Required keys: `exists` (bool). When it exists, also `scope` (str, `"namespace"`
        or `"cluster"`), `namespaces` (sequence of str), `policy` (str) and
        `created_by_bootstrap` (bool). Also required are `cluster_arn`,
        `principal_arn` and the immutable provider `access_entry_arn`; a cluster
        and policy alone cannot identify a principal or a recreated entry.
        A missing `exists` is an unanswered question and refuses.
        """

    def security_group_rule(
        self, group_id: str, source: str, port: int, protocol: str
    ) -> Mapping[str, object]:
        """The rule matching this exact source/target/port, as the provider reports it.

        Required key `exists` (bool). When it exists, the fields in `_RULE_FIELDS`
        must all be present — `rule_id` above all, because a rule identified only by
        its shape cannot be revoked precisely at retirement.
        """


def _require_answer(observed: Mapping[str, object], what: str) -> bool:
    """`exists` must be answered. An unanswered read is not a negative answer."""
    if not isinstance(observed, Mapping):
        raise BootstrapRefused(
            f"the authoritative read for {what} did not return an observation; "
            "refusing to treat an unreadable answer as 'absent'"
        )
    if "exists" not in observed:
        raise BootstrapRefused(
            f"the authoritative read for {what} did not report whether it exists; an "
            "unanswered question is not a negative answer, and defaulting it either "
            "way is how an unverified access path gets recorded as verified"
        )
    return bool(observed["exists"])


def _ownership(observed: Mapping[str, object]) -> str:
    """Derived from the read, never asserted by a caller.

    `created_by_bootstrap` absent or false means the resource predates this bootstrap,
    so it is adopted — recorded and reported, never removed. A caller cannot claim
    `adp-created` because that value is what `OwnedPrerequisite.removable` computes
    deletion authority from.
    """
    return ADP_CREATED if observed.get("created_by_bootstrap") is True else ADOPTED


def access_entry_identity(observed, cluster_arn, principal_arn):
    """Require the provider's immutable entry identity, not a cluster/policy alias.

    An entry recreated for the same principal has a new ARN. Recording that ARN
    keeps future grant/revoke recovery from confusing it with the old object.
    This observation does not prove creation ownership or authorize deletion.
    """
    prefix, separator, cluster_name = cluster_arn.partition(":cluster/")
    entry_arn = observed.get("access_entry_arn")
    entry_prefix = f"{prefix}:access-entry/{cluster_name}/"
    if (
        not separator
        or observed.get("cluster_arn") != cluster_arn
        or observed.get("principal_arn") != principal_arn
        or not isinstance(entry_arn, str)
        or not entry_arn.startswith(entry_prefix)
        or len(entry_arn[len(entry_prefix) :].split("/")) < 4
        or not all(entry_arn[len(entry_prefix) :].split("/"))
    ):
        raise BootstrapRefused(
            "EKS access entry identity is missing or differs from the bound target/principal"
        )
    return entry_arn


def _verify_access_entry(
    access: PrerequisiteAccess,
    target: VerifiedTarget,
    principal_arn: str,
    namespace: str,
) -> OwnedPrerequisite:
    """The scoped EKS access entry must exist and be confined to this namespace."""
    observed = access.access_entry(target.cluster_arn, principal_arn)
    if not _require_answer(observed, f"the EKS access entry on {target.cluster_arn!r}"):
        raise BootstrapRefused(
            "the scoped EKS access entry required to reach this cluster does not "
            "exist for the bootstrap principal. `infra/workspaces/eks.tf` sets "
            "authentication_mode=API with no cluster-creator admin, so without this "
            "entry nothing can act on the cluster and no later gate would be "
            "meaningful"
        )

    scope = observed.get("scope")
    if scope != REQUIRED_ACCESS_SCOPE:
        raise BootstrapRefused(
            f"the EKS access entry is scoped {scope!r}, not {REQUIRED_ACCESS_SCOPE!r}; "
            "a cluster-scoped entry is cluster-admin by another name and would make "
            "every namespace boundary this bootstrap establishes advisory"
        )

    namespaces = observed.get("namespaces")
    if not isinstance(namespaces, Sequence) or isinstance(namespaces, str):
        raise BootstrapRefused(
            "the EKS access entry did not report which namespaces it is confined to; "
            "an unbounded namespace scope cannot be distinguished from a correctly "
            "confined one"
        )
    confined = tuple(str(name) for name in namespaces)
    if confined != (namespace,):
        raise BootstrapRefused(
            f"the EKS access entry is confined to {confined!r}, not exactly "
            f"({namespace!r},); an entry covering other namespaces grants access "
            "beyond the workspace it was created for"
        )

    policy = observed.get("policy")
    if not isinstance(policy, str) or not policy.strip():
        raise BootstrapRefused(
            "the EKS access entry did not report an access policy; an entry whose "
            "policy is unknown has unknown authority"
        )

    entry_arn = access_entry_identity(observed, target.cluster_arn, principal_arn)
    return OwnedPrerequisite(
        kind=ACCESS_ENTRY,
        identifier=f"{entry_arn}#{policy}",
        workspace_id=target.workspace_id,
        ownership=_ownership(observed),
        reason=(
            f"scoped {REQUIRED_ACCESS_SCOPE} access to {namespace!r} under {policy}, "
            "established outside Terraform because eks.tf declares no access entry"
        ),
    )


def _verify_rule(
    access: PrerequisiteAccess,
    target: VerifiedTarget,
    expected: ExpectedPrerequisites,
    *,
    kind: str,
    group_id: str,
    source: str,
    purpose: str,
    vpc_id: str,
) -> OwnedPrerequisite:
    """One security-group rule, matched on its exact source, target, port and protocol.

    Every field is compared against the Terraform-published expectation rather than
    against a caller parameter, and `rule_id` is required so retirement can revoke
    exactly this rule and no other.
    """
    observed = access.security_group_rule(
        group_id, source, expected.api_server_port, expected.protocol
    )
    if not _require_answer(observed, f"{kind} on {group_id!r} from {source!r}"):
        raise BootstrapRefused(
            f"the required {kind} does not exist ({purpose}). Bootstrap cannot verify "
            "a cluster it cannot reach, and a later gate failing for this reason would "
            "be indistinguishable from the cluster being genuinely misconfigured"
        )

    missing = [name for name in _RULE_FIELDS if name not in observed]
    if missing:
        raise BootstrapRefused(
            f"the authoritative read of {kind} omitted "
            + ", ".join(sorted(missing))
            + "; a rule whose identity is incompletely observed cannot be revoked "
            "precisely at retirement"
        )

    tags = observed.get("tags", {})
    retained = kind == MANAGEMENT_RULE and expected.retained_sts_rule_id is not None
    if retained and (
        observed.get("rule_id") != expected.retained_sts_rule_id
        or observed.get("created_by_bootstrap") is not False
    ):
        raise BootstrapRefused(
            "retained private STS rule identity or ownership changed"
        )
    if not retained and (
        not isinstance(tags, Mapping)
        or tags.get("OrgId") != target.org_id
        or tags.get("WorkspaceId") != target.workspace_id
    ):
        raise BootstrapRefused(f"{kind} has no matching org/workspace attribution")
    if not isinstance(observed["rule_id"], str) or not observed["rule_id"].startswith(
        "sgr-"
    ):
        raise BootstrapRefused(f"{kind} did not return an exact security-group rule ID")
    # Attribution, field by field. Each mismatch names the field, because the operator
    # action differs: a wrong account is a wrong-credential problem, a wrong VPC is a
    # wrong-environment problem, and a wrong source is an over-broad rule.
    if str(observed["account_id"]) != expected.account_id:
        raise BootstrapRefused(
            f"{kind} exists in account {observed['account_id']!r}, not the expected "
            f"{expected.account_id!r}; a rule in another account either does not "
            "affect this cluster or opens a path into unrelated infrastructure"
        )
    if str(observed["vpc_id"]) != vpc_id:
        raise BootstrapRefused(
            f"{kind} is attached to VPC {observed['vpc_id']!r}, not the VPC Terraform "
            f"published for this workspace ({vpc_id!r})"
        )
    if str(observed["group_id"]) != group_id:
        raise BootstrapRefused(
            f"{kind} was returned for security group {observed['group_id']!r} rather "
            f"than the requested {group_id!r}; the read did not answer the question "
            "that was asked"
        )
    if str(observed["source"]) != source:
        raise BootstrapRefused(
            f"{kind} permits {observed['source']!r}, not exactly {source!r}; a rule "
            "with a broader source grants access to more than the management plane"
        )
    if int(observed["port"]) != expected.api_server_port:
        raise BootstrapRefused(
            f"{kind} permits port {observed['port']!r}, not the API server port "
            f"{expected.api_server_port}"
        )
    if str(observed["protocol"]) != expected.protocol:
        raise BootstrapRefused(
            f"{kind} permits protocol {observed['protocol']!r}, not "
            f"{expected.protocol!r}"
        )

    rule_id = str(observed["rule_id"])
    if not rule_id.strip():
        raise BootstrapRefused(
            f"{kind} reported a blank rule id; retirement revokes rules by id, and a "
            "rule matched only by shape could revoke one somebody else created"
        )

    return OwnedPrerequisite(
        kind=kind,
        identifier=rule_id,
        workspace_id=target.workspace_id,
        ownership=_ownership(observed),
        reason=(
            f"{purpose}: {source} -> {group_id} "
            f"{expected.protocol}/{expected.api_server_port} in {expected.vpc_id}"
        ),
    )


def verify_network_prerequisites(*, access, target, expected, provider_account_id):
    """Verify the private API/STS paths before granting any cluster access."""
    if not isinstance(expected, ExpectedPrerequisites):
        raise BootstrapRefused(
            "published network prerequisite expectations are required"
        )
    if (
        expected.account_id != provider_account_id
        or expected.account_id != target.account_id
    ):
        raise BootstrapRefused(
            "network prerequisites belong to a different provider account"
        )
    return [
        _verify_rule(
            access,
            target,
            expected,
            kind=ENDPOINT_RULE,
            vpc_id=expected.vpc_id,
            group_id=expected.cluster_security_group_id,
            source=expected.management_security_group_id,
            purpose=(
                "the private endpoint path the management plane reaches the workspace "
                "API server through"
            ),
        ),
        _verify_rule(
            access,
            target,
            expected,
            kind=MANAGEMENT_RULE,
            vpc_id=expected.sts_endpoint_vpc_id,
            group_id=expected.sts_endpoint_security_group_id,
            source=expected.node_security_group_id,
            purpose="workspace node access to the private STS endpoint",
        ),
    ]


def verify_prerequisites(
    *,
    access: PrerequisiteAccess,
    target: VerifiedTarget,
    expected: ExpectedPrerequisites,
    principal_arn: str,
    namespace: str,
    store: StateStore,
    state: BootstrapState,
    provider_account_id: str,
    authority=None,
) -> tuple[PrerequisiteInventory, BootstrapState]:
    """Verify and record every access prerequisite. Refuses before any cluster mutation.

    Returns the durable inventory and the updated state. Called FIRST in the gate
    sequence — before a namespace exists — so a missing access path refuses against an
    untouched cluster.

    Attribution is checked against `expected` (Terraform's published outputs) and
    against the bound operation's identity on `target`, never against a caller
    parameter. Ownership is derived from the authoritative read, so an adopted rule
    cannot be recorded as removable.
    """
    # The expectation is a required argument, so there is no default to fall back to —
    # but a caller can still pass `None` explicitly, and an operator command reading an
    # absent Terraform output is the way that happens in practice. Checked here so the
    # result is a refusal naming the missing outputs rather than an `AttributeError`
    # several comparisons later, which would read as a bug in this gate rather than as
    # the missing input it is.
    if not isinstance(expected, ExpectedPrerequisites):
        raise BootstrapRefused(
            "no published prerequisite expectation was supplied; the account, VPC and "
            "security-group ids come from `infra/workspaces/outputs.tf`, and without "
            "them there is nothing to verify the observed access path against"
        )
    if not namespace.strip():
        raise BootstrapRefused(
            "a workspace namespace is required to verify the scoped access entry; "
            "without one the entry's confinement cannot be checked against anything"
        )
    if not principal_arn.strip():
        raise BootstrapRefused(
            "the bootstrap principal ARN is required to read its access entry"
        )

    # The account comes from the provider's answer to "who am I", the same rule
    # `target.py` applies. A caller-supplied account would let a rule in the wrong
    # account verify against itself.
    if expected.account_id != provider_account_id:
        raise BootstrapRefused(
            f"the published prerequisite expectation names account "
            f"{expected.account_id!r} but the provider reports "
            f"{provider_account_id!r}; refusing to attribute prerequisites to an "
            "account this credential is not in"
        )
    if expected.account_id != target.account_id:
        raise BootstrapRefused(
            f"the published prerequisite expectation names account "
            f"{expected.account_id!r} but the verified cluster is in "
            f"{target.account_id!r}; these outputs describe a different workspace"
        )

    prerequisites = [
        authority.prerequisite()
        if authority is not None
        else _verify_access_entry(access, target, principal_arn, namespace),
        *verify_network_prerequisites(
            access=access,
            target=target,
            expected=expected,
            provider_account_id=provider_account_id,
        ),
    ]

    recorded_kinds = {item.kind for item in prerequisites}
    missing_kinds = [
        kind for kind in REQUIRED_PREREQUISITE_KINDS if kind not in recorded_kinds
    ]
    if missing_kinds:  # pragma: no cover - structural guard on the list above
        raise BootstrapRefused(
            "the prerequisite inventory is missing required kind(s): "
            + ", ".join(sorted(missing_kinds))
        )

    inventory = PrerequisiteInventory(
        workspace_id=target.workspace_id, prerequisites=tuple(prerequisites)
    )
    # Recorded before the first cluster mutation, so a process killed during
    # installation still leaves evidence that the access path was established and
    # attributed — which is what retirement needs to revoke exactly ADP's rules.
    if authority is not None:
        authority.record_prerequisites(inventory)
    state = record(
        store, state, prerequisites_recorded=True, prerequisite_inventory=inventory
    )
    return inventory, state


def require_inventory(
    inventory: PrerequisiteInventory | None,
    *,
    workspace_id: str,
    what: str,
) -> PrerequisiteInventory:
    """The F4 gate in one place: an inventory is mandatory, not optional.

    Used by registration and by retirement. Both need it for the same reason stated
    two different ways: registration must not publish a usable workspace whose access
    path was never attributed, and retirement must not be asked to revoke a set of
    rules nobody recorded.
    """
    if inventory is None:
        raise BootstrapRefused(
            f"a prerequisite inventory is required for {what}; the scoped access entry "
            "and the security-group rules are created outside Terraform, so an absent "
            "inventory means either that they were never verified or that nothing will "
            "know to revoke them"
        )
    if inventory.workspace_id != workspace_id:
        raise BootstrapRefused(
            f"the prerequisite inventory belongs to workspace "
            f"{inventory.workspace_id!r}, not {workspace_id!r}; using it for {what} "
            "could act on another workspace's access"
        )
    if not inventory.prerequisites:
        raise BootstrapRefused(
            f"the prerequisite inventory for {what} is empty; bootstrap establishes at "
            "least a scoped access entry, so an empty inventory means the record was "
            "not built rather than that nothing was created"
        )
    return inventory


def summarize(inventory: PrerequisiteInventory) -> Mapping[str, tuple[str, ...]]:
    """Removable and preserved entries, for an operator-facing report.

    Both lists, always. Reporting only what will be removed would leave the adopted
    rules invisible, and an adopted rule an operator does not know about is the same
    accidental-permanence problem in a different place.
    """
    return {
        "removable": tuple(
            f"{item.kind}/{item.identifier}" for item in inventory.removable
        ),
        "preserved": tuple(
            f"{item.kind}/{item.identifier}" for item in inventory.preserved
        ),
    }


# Re-exported so callers building an inventory by hand in tests do not have to import
# from two modules, and so the ownership vocabulary has exactly one source.
__all__ = [
    "ACCESS_ENTRY",
    "ADOPTED",
    "ADP_CREATED",
    "ENDPOINT_RULE",
    "MANAGEMENT_RULE",
    "REQUIRED_ACCESS_SCOPE",
    "REQUIRED_PREREQUISITE_KINDS",
    "ExpectedPrerequisites",
    "PrerequisiteAccess",
    "summarize",
    "require_inventory",
    "verify_prerequisites",
]
