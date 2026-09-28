"""Attributed ownership for prerequisites Terraform does not manage.

Issue #5533 (w6-10), EPIC #4910. Design item 4: "Resolve U23 static scoped-access
prerequisites against the brokered runtime contract; document and test any installer
change instead of silently broadening access."

## Why anything exists outside Terraform at all

Most of a workspace is Terraform-managed and therefore self-documenting: the state
file records what exists and `terraform destroy` removes exactly that. A few
prerequisites cannot be, because they are created during bootstrap against a cluster
whose identifiers are only known once it exists — the EKS access entry this package
establishes being the clearest case, since `../infra/workspaces/eks.tf` deliberately
sets `authentication_mode = "API"` with
`bootstrap_cluster_creator_admin_permissions = false` and declares NO
`aws_eks_access_entry`, leaving access as this story's job.

## The failure this module prevents

An object created outside Terraform and not recorded anywhere becomes permanent by
accident. Nobody deletes it because nobody knows it exists; `terraform destroy`
doesn't see it; and the next operator reading the Terraform finds no trace. For a
security-group rule or an access entry, "permanent by accident" means an access path
that outlives the workspace it was created for — and on a supplied cluster (AC-02)
it means ADP left a hole in infrastructure it does not own.

So every out-of-Terraform prerequisite is recorded here with: what it is, who created
it, which workspace it was created for, and **whether ADP may remove it**. That last
field is the one `retire.py` reads, and it is why this module is separate from
`components.py` — cluster objects and account-level objects have different cleanup
authorities.

## Why "adopted" prerequisites are recorded but never removed

A prerequisite that already existed before bootstrap is recorded as adopted. It still
belongs in the inventory — an operator needs to know the access path is in use — but
ADP must not remove it, because something else created it and may still depend on it.
Removing a pre-existing security-group rule on a supplied cluster is the same class
of harm as deleting its namespace: AC-02 requires unrelated workloads to survive.

## Why this module does not create anything

It records. Creation belongs to whatever holds the credential for that resource type
— `#5534` for provider-side objects, the installer for cluster-side ones. Keeping the
record separate from the creation means the inventory can be asserted against in an
offline test, and means a creation path that forgot to record is visible as an
inventory with a missing entry rather than as nothing at all.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from .errors import BootstrapRefused

# Ownership values, matching the vocabulary `ClusterOwnership` establishes in
# `../infra/account-factory/account_factory/modes.py` so one word does not mean two
# things in two packages.
ADP_CREATED = "adp-created"
ADOPTED = "adopted"
_OWNERSHIP_VALUES = frozenset({ADP_CREATED, ADOPTED})


@dataclass(frozen=True)
class OwnedPrerequisite:
    """One prerequisite created or adopted outside Terraform's state.

    `removable` is computed from ownership rather than stored, so no caller can
    record an adopted resource as removable. That is the single most consequential
    field here and it should not be possible to get wrong by construction.
    """

    kind: str
    identifier: str
    workspace_id: str
    ownership: str
    reason: str

    def __post_init__(self) -> None:
        for name in ("kind", "identifier", "workspace_id", "reason"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise BootstrapRefused(
                    f"OwnedPrerequisite.{name} is required; an unattributed "
                    "prerequisite is one nobody will know to remove"
                )
        if self.ownership not in _OWNERSHIP_VALUES:
            raise BootstrapRefused(
                f"unknown prerequisite ownership {self.ownership!r}; expected "
                f"{ADP_CREATED!r} or {ADOPTED!r}"
            )

    @property
    def removable(self) -> bool:
        """Whether ADP created this and may therefore remove it."""
        return self.ownership == ADP_CREATED


@dataclass(frozen=True)
class PrerequisiteInventory:
    """The complete set of out-of-Terraform prerequisites for one workspace.

    Complete is the operative word: `retire.py` treats this as the authoritative
    list of what cleanup may consider, so an object missing from it is an object
    cleanup will not remove.
    """

    workspace_id: str
    prerequisites: tuple[OwnedPrerequisite, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not isinstance(self.workspace_id, str) or not self.workspace_id.strip():
            raise BootstrapRefused("PrerequisiteInventory.workspace_id is required")
        foreign = [
            prerequisite.identifier
            for prerequisite in self.prerequisites
            if prerequisite.workspace_id != self.workspace_id
        ]
        if foreign:
            raise BootstrapRefused(
                "inventory contains prerequisites recorded for a different "
                f"workspace ({', '.join(sorted(foreign))}); a mixed inventory would "
                "let one workspace's cleanup remove another's access"
            )
        seen: set[tuple[str, str]] = set()
        for prerequisite in self.prerequisites:
            key = (prerequisite.kind, prerequisite.identifier)
            if key in seen:
                raise BootstrapRefused(
                    f"{prerequisite.kind}/{prerequisite.identifier} is recorded "
                    "twice; a duplicated entry can carry two different ownerships "
                    "and make removability ambiguous"
                )
            seen.add(key)

    @property
    def removable(self) -> tuple[OwnedPrerequisite, ...]:
        """Only what ADP created — the complete set cleanup may remove."""
        return tuple(item for item in self.prerequisites if item.removable)

    @property
    def preserved(self) -> tuple[OwnedPrerequisite, ...]:
        """Recorded but never removed by ADP. Reported, so it is not forgotten."""
        return tuple(item for item in self.prerequisites if not item.removable)


def inventory_from_mapping(value: object) -> PrerequisiteInventory:
    """Read actual ownership identities, never infer them from a recorded flag."""
    if (
        not isinstance(value, dict)
        or set(value) != {"workspace_id", "prerequisites"}
        or not isinstance(value["prerequisites"], (list, tuple))
    ):
        raise BootstrapRefused("durable prerequisite inventory is malformed")
    try:
        return PrerequisiteInventory(
            workspace_id=value["workspace_id"],
            prerequisites=tuple(
                OwnedPrerequisite(**item) for item in value["prerequisites"]
            ),
        )
    except (TypeError, ValueError) as exc:
        raise BootstrapRefused("durable prerequisite inventory is malformed") from exc


def adopt_prerequisites(
    *,
    workspace_id: str,
    prerequisites: Sequence[OwnedPrerequisite],
) -> PrerequisiteInventory:
    """Build the inventory for one workspace.

    A thin constructor on purpose: the validation belongs in the dataclasses so it
    applies however the inventory is built, and this function exists to give callers
    one obvious entry point and to refuse an empty inventory where one is expected.
    """
    if not prerequisites:
        raise BootstrapRefused(
            "no prerequisites were recorded; bootstrap establishes at least a scoped "
            "access entry outside Terraform, so an empty inventory means the record "
            "was not built rather than that nothing was created"
        )
    return PrerequisiteInventory(
        workspace_id=workspace_id, prerequisites=tuple(prerequisites)
    )
