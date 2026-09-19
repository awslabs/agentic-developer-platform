"""The ADP-side SkyPilot-to-EKS migration adapter.

Issue #5061 (U19), EPIC #4910. R18, ADP half.

## What this package is

`repo-path-allocation.md` §"SkyPilot service and migration ownership" allocates to
U19 "the ADP EKS migration adapter and resource/state handover tests under
`modules/domain-apps/superplane/`". This is that adapter. Three modules:

| Module | What it owns |
|---|---|
| `placement.py` | Which capacity is *eligible*, whether its price is *fresh*, and whether a change needs approval |
| `eks_join.py` | provision -> join -> schedule, with the join made an explicit step |
| `handover.py` | Inventory of existing state, one owner per resource, rollback |

## What it deliberately is not

**Not a domain API.** There is no route, no schema and no persistence here, for the
reason U1's README states: a second writable ADP-hosted domain service was withdrawn
in the design's revision 3, and domain persistence stays in the upstream Superplane
API. These modules compute decisions and hand them to a caller.

**Not a live migration.** Nothing here talks to a provider, a cluster or an API
server. The SkyPilot client, the workspace EKS cluster and B's operation facade all
arrive as constructor arguments, and the tests pass fakes. Per
`validation-mapping.md` §"Inputs that remain unresolved", the account, authorized
access, spend figure, deadline and cleanup owner are all **Unresolved**, so no live
call could be authorized from here even if the code could make one. R18's three live
criteria stay open; see `construction/loop-proposal/acceptance-index.md`.

**Not a second cost model.** Placement reads prices and refuses stale ones. It
computes no spend total and holds no budget: C owns the reservation ledger, and
`accounting.py` records why A does not duplicate it.

## Why the baseline is preserved but its defects are not

The migration baseline is the user's working SkyPilot -> workspace-EKS path
(`docs/design-notes/4910-skypilot-eks-migration-amendment.md`). U12 (#5040) recorded
what that path actually does, including four `CHAIN_GAPS` — places where the Go
controller path does not do what its own field names suggest. Each gap carries a
`u19_decision_required`, and each is answered here:

| Gap | Answer, and where |
|---|---|
| `launch-task-has-no-join-step` | `eks_join.py` makes the join an explicit post-provision step. The launch task keeps the baseline's shape |
| `k8s-node-name-never-assigned` | `eks_join.py` resolves the name after the join; unresolved means `NodeReadiness.UNOBSERVED`, never ready |
| `ssm-activation-credentials-in-task-envs` | `eks_join.py` takes activation material as `SecretMaterial` and refuses to place it in task envs |
| `no-owning-controller-for-serving` | `handover.py` requires exactly one owning controller per resource, serving included |

The amendment is explicit that "functional parity does not preserve authentication
bypasses, secret exposure or false cleanup success as desired behavior". So the three
gaps that are defects are fixed, and the fourth — serving's missing owner — is
answered by naming an owner rather than by declaring serving out of scope.

## Standard library only

Like `contracts/`, this package imports nothing outside the standard library, because
`superplane-domain-ci.yml` installs the *gateway's* dependency set and runs pytest
over this tree. It does import `superplane_contracts` and `spike`, both of which are
also standard-library-only; `tests/_migration_path.py` records how they resolve.
"""

from __future__ import annotations

from .eks_join import (
    JoinRequest,
    JoinStep,
    NodeReadiness,
    ProvisionedNode,
    SkyPilotEksAdapter,
    WorkspaceClusterResolver,
    WorkloadPlacement,
)
from .handover import (
    HandoverDecision,
    HandoverPlan,
    InventoriedResource,
    OwnershipConflict,
    RollbackRecord,
    build_plan,
    rollback,
)
from .placement import (
    ApprovalRequired,
    CapacityRequest,
    EligibilityFailure,
    PlacementDecision,
    PriceQuote,
    PricingMode,
    RelocationRecord,
    eligible_candidates,
    place,
    recheck_freshness,
    relocate,
    requires_approval,
)

__all__ = [
    "ApprovalRequired",
    "CapacityRequest",
    "EligibilityFailure",
    "HandoverDecision",
    "HandoverPlan",
    "InventoriedResource",
    "JoinRequest",
    "JoinStep",
    "NodeReadiness",
    "OwnershipConflict",
    "PlacementDecision",
    "PriceQuote",
    "PricingMode",
    "ProvisionedNode",
    "RelocationRecord",
    "RollbackRecord",
    "SkyPilotEksAdapter",
    "WorkloadPlacement",
    "WorkspaceClusterResolver",
    "build_plan",
    "eligible_candidates",
    "place",
    "recheck_freshness",
    "relocate",
    "requires_approval",
    "rollback",
]
