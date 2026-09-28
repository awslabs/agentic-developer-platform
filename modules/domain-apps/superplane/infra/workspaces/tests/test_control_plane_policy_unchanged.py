"""The control plane still may not create a VPC or a cluster — Issue #5532 (w6-09), AC-01.

## Why a test in THIS module asserts something about ANOTHER module's policy

This story needed a module that declares `aws_vpc` and `aws_eks_cluster`. The shared ownership
policy at `../../scripts/domain_ownership.py` lists both in `PLATFORM_OWNED_TYPES` and refuses
them. There were two ways to resolve that:

  1. Widen the shared policy to admit VPCs and EKS clusters.
  2. Give this module its own policy (`test_workspace_isolation.py`) and leave the shared one
     alone.

Option 1 is a one-line change and it is the wrong one. `domain_ownership.py` governs the
CONTROL-PLANE module, which runs against ADP's own account. Admitting `aws_vpc` there would
grant the control plane permission to create — and on destroy, delete — a VPC and an EKS
cluster in ADP's account. The blast radius is the ADP management cluster and the shared VPC
that `platform/infra/` owns: exactly the protection the platform isolation requirement
(2026-09-16) was added for, removed as a side effect of provisioning tenant workspaces.

So option 2 was taken. This file is the guard on that decision.

## What makes this test worth having rather than a comment

A comment explaining why not to widen the shared policy is advice. This is a check: if someone
later hits a workspace-shaped need in the control-plane module and reaches for the one-line
fix, the failure arrives here, in the module whose existence created the temptation, with the
reasoning attached.

That is the specific failure mode being guarded — not malice, but a future author who sees the
workspaces module creating VPCs, concludes the shared prohibition is stale, and relaxes it.

## Scope

This file asserts the shared policy's REFUSAL is intact. It does not test
`domain_ownership.py`'s behaviour generally — that module has its own suite, and duplicating
it here would create two places to update.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SUPERPLANE_INFRA = Path(__file__).resolve().parents[2]
DOMAIN_OWNERSHIP = SUPERPLANE_INFRA / "scripts" / "domain_ownership.py"
CONTROL_PLANE_ISOLATION = (
    SUPERPLANE_INFRA / "control-plane" / "tests" / "test_platform_isolation.py"
)


def _load(path: Path, name: str):
    """Import a module by path.

    By path rather than by package import: `scripts/` and `control-plane/tests/` are not
    importable packages from here (`control-plane` is not a valid identifier), and adding
    __init__.py files to make them so would change how the existing lanes collect them.
    """
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader, f"could not load {path}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_shared_ownership_policy_exists() -> None:
    """Premise check: the files this suite makes assertions about must be present.

    Without this, deleting `domain_ownership.py` outright would make the tests below error
    rather than fail — and a suite that cannot run is not a suite that passes, but it is easy
    to mistake one for the other in a CI summary.
    """
    assert DOMAIN_OWNERSHIP.exists(), (
        f"{DOMAIN_OWNERSHIP} is missing. The shared domain ownership policy is what keeps "
        f"the control-plane module from taking ADP's shared VPC and management cluster into "
        f"its state. If it moved, update this suite's path rather than deleting the check."
    )
    assert CONTROL_PLANE_ISOLATION.exists(), (
        f"{CONTROL_PLANE_ISOLATION} is missing — see above; same reasoning."
    )


# The types whose refusal matters most to this story, because they are exactly the ones this
# module creates and would therefore be the ones a future author is tempted to unblock.
WORKSPACE_TYPES_THAT_MUST_STAY_FORBIDDEN_FOR_THE_CONTROL_PLANE = [
    "aws_vpc",
    "aws_subnet",
    "aws_eks_cluster",
    "aws_eks_node_group",
    "aws_iam_openid_connect_provider",
]


@pytest.mark.parametrize(
    "resource_type", WORKSPACE_TYPES_THAT_MUST_STAY_FORBIDDEN_FOR_THE_CONTROL_PLANE
)
def test_shared_policy_still_refuses_workspace_resource_types(
    resource_type: str,
) -> None:
    """`domain_ownership.py` must not have been widened to admit what #5532 needed."""
    policy = _load(DOMAIN_OWNERSHIP, "_adp_domain_ownership_under_test")

    assert resource_type not in policy.ALLOWED_RESOURCE_TYPES, (
        f"`{resource_type}` has been added to ALLOWED_RESOURCE_TYPES in "
        f"scripts/domain_ownership.py.\n\n"
        f"That policy governs the CONTROL-PLANE module, which runs against ADP's own "
        f"account — so this grants the control plane permission to create, and on destroy "
        f"delete, {resource_type} in ADP's account. The blast radius is the ADP management "
        f"cluster and the shared VPC that platform/infra owns.\n\n"
        f"If a workspace-shaped resource is needed, it belongs in "
        f"modules/domain-apps/superplane/infra/workspaces/, whose boundary is one tenant "
        f"workspace in the tenant's own account and whose policy is "
        f"tests/test_workspace_isolation.py. Issue #5532 took that route deliberately "
        f"rather than widening this one."
    )

    assert resource_type in policy.PLATFORM_OWNED_TYPES, (
        f"`{resource_type}` has been removed from PLATFORM_OWNED_TYPES in "
        f"scripts/domain_ownership.py. That map is what turns a refusal into an error "
        f"message naming the damage; without the entry the refusal still happens but stops "
        f"explaining itself, which is how a check gets suppressed instead of heeded."
    )


@pytest.mark.parametrize(
    "resource_type", WORKSPACE_TYPES_THAT_MUST_STAY_FORBIDDEN_FOR_THE_CONTROL_PLANE
)
def test_control_plane_isolation_suite_still_refuses_workspace_types(
    resource_type: str,
) -> None:
    """The same assertion against the control plane's own test-side allowlist.

    There are two enforcement points, not one: `domain_ownership.py` (used by the plan-safety
    tooling) and the control-plane test suite's own ALLOWED_RESOURCE_TYPES. Widening either
    alone would let a VPC into the control-plane module while the other still refused it, so
    both are pinned.
    """
    suite = _load(CONTROL_PLANE_ISOLATION, "_adp_control_plane_isolation_under_test")

    assert resource_type not in suite.ALLOWED_RESOURCE_TYPES, (
        f"`{resource_type}` has been added to ALLOWED_RESOURCE_TYPES in "
        f"control-plane/tests/test_platform_isolation.py, which lets the control-plane "
        f"module declare it. See the message in the companion test above: workspace-shaped "
        f"resources belong in infra/workspaces/, not in the module that runs against ADP's "
        f"own account."
    )


def test_the_two_policies_disagree_on_purpose() -> None:
    """The workspace and control-plane allowlists must NOT have converged.

    This is the anti-vacuous check for this file, and it runs in the opposite direction from
    every assertion above. Those all pass if `domain_ownership.py` refuses VPCs — including
    in the degenerate case where THIS module also refuses them, i.e. where the workspaces
    module has been emptied or its allowlist reduced to the control plane's. Then the
    protection is intact and the feature is gone, and nothing else here would notice.

    So: the workspace policy must admit at least the types the control-plane policy refuses.
    The divergence is the design, and it is asserted in both directions.
    """
    workspace_suite = _load(
        Path(__file__).parent / "test_workspace_isolation.py",
        "_adp_workspace_isolation_under_test",
    )
    control_plane = _load(DOMAIN_OWNERSHIP, "_adp_domain_ownership_divergence_check")

    for resource_type in WORKSPACE_TYPES_THAT_MUST_STAY_FORBIDDEN_FOR_THE_CONTROL_PLANE:
        assert resource_type in workspace_suite.ALLOWED_RESOURCE_TYPES, (
            f"`{resource_type}` is missing from the WORKSPACE module's allowlist. This "
            f"module exists to create workspace networks and clusters (design items 1 and "
            f"2); if it no longer may, the refusal assertions in this file are passing "
            f"against a module that does nothing, and the separate-policy decision they "
            f"guard has no subject."
        )

    overlap = workspace_suite.ALLOWED_RESOURCE_TYPES & frozenset(
        control_plane.PLATFORM_OWNED_TYPES
    )
    assert overlap, (
        "the workspace allowlist and the control plane's platform-owned set no longer "
        "overlap at all, which means the two policies have converged. Their divergence is "
        "the point: the same rule (own only what your boundary contains) produces different "
        "answers for a tenant workspace and for ADP's own account."
    )
