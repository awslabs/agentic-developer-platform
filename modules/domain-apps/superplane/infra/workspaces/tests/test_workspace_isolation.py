"""This module owns a workspace, and only a workspace — Issue #5532 (w6-09), AC-01.

## The rule this module inverts, and why that is not a loophole

`../../control-plane/tests/test_platform_isolation.py` forbids the control-plane module from
declaring `aws_vpc`, `aws_eks_cluster`, `aws_subnet`, `aws_iam_openid_connect_provider` and
friends. Those belong to `platform/infra/`, and taking one into a domain module's state means
destroying the domain app destroys shared ADP infrastructure.

This module declares every one of them. That is its purpose: design item 1 requires
"versioned EKS/network/IAM resources", and design item 2 requires a "physically separate
workspace EKS". The two rules are the same rule — *a module may only own resources whose
destruction damages nothing outside its own boundary* — applied to two different boundaries.
The control plane's boundary is the domain app; this module's boundary is one tenant
workspace, in the tenant's own account.

So the allowlist here is genuinely different from the control plane's, and that difference is
the thing most at risk of being read as an escape hatch. Which is why:

  * `test_control_plane_policy_unchanged.py` asserts the control plane's refusal still holds.
    Widening the SHARED policy in `../../scripts/domain_ownership.py` would have been the
    easy way to make this module pass a check — and would simultaneously have granted the
    control plane permission to create a VPC and a cluster in ADP's own account. That is why
    this module has its own policy rather than a loosened shared one.
  * the allowlist below still excludes everything whose blast radius leaves the workspace.

## What is excluded, and what each exclusion is protecting

The forbidden set is not "platform-owned types" — those are exactly what this module creates.
It is types that reach OUTSIDE one workspace:

  * `aws_organizations_account`, `aws_organizations_organizational_unit` — account lifecycle.
    #5530's rule is that account closure is never a consequence of removing a workspace, and
    the mechanism is that this module cannot express an account at all. A `terraform destroy`
    of a workspace must not be able to close an account, whatever its state contains.
  * `aws_s3_bucket` — the Terraform state bucket is an S3 bucket. A module that can create
    one can, on destroy, delete one.
  * `aws_iam_account_password_policy`, `aws_iam_account_alias` — account-wide settings that
    every workspace in a shared account would fight over.
  * `aws_secretsmanager_secret` and its versions — a secret created here has its value in
    this module's state. Secrets are seeded out of band; the control plane's variables.tf
    records the same reasoning at length.
  * `aws_organizations_policy`, `aws_servicequotas_service_quota` — org and account-wide
    controls.

## Why an allowlist and not a denylist

Same reason as the control plane's: a denylist passes anything nobody thought to forbid, and
the types that would hurt most are the ones a future author adds for a locally sensible
reason. An allowlist fails closed — adding a type requires editing the policy, which is where
the reasoning gets recorded.

## Where the policy itself lives

In `../scripts/workspace_ownership.py`, imported below rather than restated here. The same
three lists are what that module's plan guard (design item 4) decides ownership with, and two
copies would drift — with the copy nobody ran being the one that drifted. So this suite
enforces the policy against the module's SOURCE, and the guard enforces the same policy
against a PLAN. One definition, two enforcement points.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

WORKSPACES = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(WORKSPACES / "scripts"))

from workspace_ownership import (  # noqa: E402
    ALLOWED_DATA_SOURCES,
    ALLOWED_RESOURCE_TYPES,
    BEYOND_WORKSPACE_TYPES,
)


def _terraform_files() -> list[Path]:
    return sorted(WORKSPACES.glob("*.tf"))


def _strip_comments(text: str) -> str:
    """Drop comment lines and heredoc bodies.

    This module's prose names most of BEYOND_WORKSPACE_TYPES while explaining what it does
    not own — main.tf has a whole section on it — and variables.tf's heredoc descriptions
    discuss accounts and secrets at length. Raw text would flag the reasoning as the
    violation.
    """
    out: list[str] = []
    heredoc_terminator: str | None = None

    for line in text.splitlines():
        if heredoc_terminator is not None:
            if line.strip() == heredoc_terminator:
                heredoc_terminator = None
            continue

        opening = re.search(r"<<-?([A-Za-z_][A-Za-z0-9_]*)\s*$", line)
        if opening:
            heredoc_terminator = opening.group(1)
            continue

        stripped = line.strip()
        if stripped.startswith(("#", "//")):
            continue
        out.append(line.split("#", 1)[0])

    return "\n".join(out)


def _declarations(kind: str) -> list[tuple[Path, str, str]]:
    found: list[tuple[Path, str, str]] = []
    pattern = re.compile(rf'^\s*{kind}\s+"([^"]+)"\s+"([^"]+)"\s*\{{', re.MULTILINE)
    for path in _terraform_files():
        for match in pattern.finditer(_strip_comments(path.read_text())):
            found.append((path, match.group(1), match.group(2)))
    return found


RESOURCES = _declarations("resource")
DATA_SOURCES = _declarations("data")


def test_module_declares_resources_at_all() -> None:
    """Premise check: the parametrized tests below must not collect zero cases.

    A regex that stopped matching would make every assertion here pass vacuously — an empty
    parametrization is green. "No tests ran" and "tests passed" share an exit code.
    """
    assert len(RESOURCES) >= 15, (
        f"found only {len(RESOURCES)} resource declarations, which suggests this suite's "
        f"parser has stopped matching rather than that the module was emptied. A workspace "
        f"needs at minimum a VPC, subnets, gateways, routes, a cluster, a node group and "
        f"three IAM roles."
    )
    assert DATA_SOURCES, (
        "expected at least the caller identity and policy document reads."
    )


@pytest.mark.parametrize(
    ("path", "resource_type", "resource_name"),
    RESOURCES,
    ids=[f"{t}.{n}" for _, t, n in RESOURCES],
)
def test_every_resource_is_workspace_scoped(
    path: Path, resource_type: str, resource_name: str
) -> None:
    """No resource whose destruction reaches outside this workspace may enter its state."""
    if resource_type in ALLOWED_RESOURCE_TYPES:
        return

    damage = BEYOND_WORKSPACE_TYPES.get(resource_type)
    if damage:
        raise AssertionError(
            f"{path.name} declares `{resource_type}.{resource_name}`, which would take "
            f"{damage} into one workspace's state. Destroying the workspace would then "
            f"reach beyond the workspace boundary.\n\n"
            f"This module's boundary is ONE TENANT WORKSPACE. It legitimately owns VPCs and "
            f"EKS clusters — which the control-plane module may not — precisely because "
            f"those are the workspace's own. This type is not."
        )

    raise AssertionError(
        f"{path.name} declares `{resource_type}.{resource_name}`, which is not in this "
        f"module's allowlist.\n\n"
        f"The allowlist fails closed on purpose: adding a type requires editing "
        f"ALLOWED_RESOURCE_TYPES in ../scripts/workspace_ownership.py, which is where the "
        f"reasoning gets recorded — and which is the same policy the plan guard decides "
        f"ownership with, so the two enforcement points cannot disagree. "
        f"If this type is genuinely workspace-scoped — created in the workspace's account, "
        f"named with the workspace prefix, and destroyed with the workspace without "
        f"reaching anything else — add it there with a comment saying so."
    )


@pytest.mark.parametrize(
    ("path", "data_type", "data_name"),
    DATA_SOURCES,
    ids=[f"data.{t}.{n}" for _, t, n in DATA_SOURCES],
)
def test_every_data_source_is_approved(
    path: Path, data_type: str, data_name: str
) -> None:
    """Reading is not owning, but an unreviewed read is still a coupling."""
    assert data_type in ALLOWED_DATA_SOURCES, (
        f"{path.name} reads `data.{data_type}.{data_name}`, which is not in this module's "
        f"approved read set. Add it to ALLOWED_DATA_SOURCES in "
        f"../scripts/workspace_ownership.py with the reason, so the "
        f"coupling is reviewed rather than incidental."
    )


def test_module_does_not_read_the_adp_platform_state() -> None:
    """A workspace cluster must not know the ADP management cluster's identity.

    Design item 2: tenant capacity never lands on the ADP management cluster. The control
    plane reads `data.terraform_remote_state.platform` to learn that cluster's OIDC
    provider; a workspace has its own, and reading ADP's would create the reference that
    makes mis-targeting expressible.

    Absence of the read is what makes "this module cannot place anything on the management
    cluster" true by construction rather than by intent — so it is asserted, not assumed.
    """
    for path in _terraform_files():
        text = _strip_comments(path.read_text())
        assert "terraform_remote_state" not in text, (
            f"{path.name} reads Terraform remote state. This module must not: a workspace "
            f"cluster has its own OIDC provider and no business knowing the ADP management "
            f"cluster's identity. Reading ADP's platform state introduces the reference that "
            f"makes scheduling tenant capacity onto the management cluster expressible, "
            f"which design item 2 forbids. See the section in main.tf."
        )


def test_module_declares_no_kubernetes_or_helm_provider() -> None:
    """No Kubernetes objects here — that is #5533's (w6-10) boundary.

    Declaring a `kubernetes` or `helm` provider would also make every plan require cluster
    API reachability, so a plan could not be reviewed before the cluster existed. The
    control-plane module records the same reasoning.
    """
    for path in _terraform_files():
        text = _strip_comments(path.read_text())
        for provider in ("kubernetes", "helm", "kubectl"):
            assert not re.search(
                rf'^\s*provider\s+"{provider}"\s*\{{', text, re.MULTILINE
            ), (
                f"{path.name} declares a `{provider}` provider. This module produces an "
                f"empty, reachable, encrypted, logged cluster and its identity outputs — "
                f"nothing that runs a tenant workload. Cluster bootstrap and access entries "
                f"are #5533's (w6-10). A Kubernetes provider here would also make every "
                f"plan require cluster API reachability."
            )
