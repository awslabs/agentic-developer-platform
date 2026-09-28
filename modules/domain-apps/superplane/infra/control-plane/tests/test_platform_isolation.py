"""This module declares no platform-owned resource — Issue #5042 (U3), EPIC #4910.

## The assertion Terraform cannot express

`tests/platform_isolation.tftest.hcl` proves that every resource this module DOES declare is
scoped: trust policies name specific service accounts, grants name specific ARNs, names
carry the domain prefix. The other half of the isolation requirement is a NEGATIVE — that
the module declares no VPC, no EKS cluster, no RDS instance, no gateway resource — and
`terraform test` has no way to state it. Referencing a resource the configuration does not
declare is a configuration error, not a failed assertion, so absence is unassertable from
inside a plan.

That negative is the load-bearing half. The confirmed requirement (2026-09-16) says:

    consume approved platform interfaces without importing, applying or destroying core
    platform resources — a separate state key alone does not prove resource isolation

and, on routine operations:

    routine ops must not bootstrap or apply platform/infra, or redeploy gateway, frontend
    or Agent Factory as a side effect

A module that took a platform resource into its own state would satisfy every assertion in
the .tftest.hcl file and still destroy shared infrastructure on `terraform destroy`. This
suite is what makes that impossible to add quietly.

## Why an allowlist and not a denylist

The check is "every declared resource type is in the domain-owned set", not "no declared
type is in a forbidden set". A denylist passes anything nobody thought to forbid — and the
resource types that would hurt most are the ones a future author adds for a reason that
seemed local at the time. An allowlist fails closed: adding a new type requires editing this
list, which is where the reasoning gets recorded.

## Decision 2

`aws_db_instance` and friends are absent from the allowlist deliberately, not by oversight.
Decision 2 (shared database instance vs. separate) is unresolved, and this story may not
claim an isolation, backup, retention or restore target. Provisioning a database here would
decide that by default, so the database arrives as a reference (var.database_secret_name).
When Decision 2 is made, whoever makes it adds the type to this list and states which
durability property they are claiming.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

CONTROL_PLANE = Path(__file__).resolve().parents[1]

# Resource types this module is allowed to declare. Everything here is domain-owned: it is
# created and destroyed with the Superplane domain app, and nothing else depends on it.
ALLOWED_RESOURCE_TYPES = frozenset(
    {
        # Runtime identities for the domain app's own pods.
        "aws_iam_role",
        "aws_iam_role_policy",
        "aws_iam_role_policy_attachment",
        # The domain app's own image repositories. Owned here rather than in platform's
        # shared ECR module so teardown removes them — see ecr.tf.
        "aws_ecr_repository",
        "aws_ecr_lifecycle_policy",
        # Non-secret configuration under /adp/<env>/superplane/.
        "aws_ssm_parameter",
        # A plan-time precondition carrier, not an AWS resource. `terraform_data` creates
        # nothing in the account and holds no state beyond what Terraform stores locally, so
        # destroying it cannot reach any shared infrastructure.
        #
        # Added for ecr.tf's `ecr_inventory_guard`, which fails the PLAN when the release
        # lock names two images against one ECR repository (for_each would silently
        # deduplicate them) or names a repository outside the `adp-superplane-` prefix (this
        # module would then create — and on destroy DELETE — a repository it does not own).
        # A `check` block would only warn; these must stop the lane, which is why they are
        # preconditions on a resource. See PR #5283 review finding 2.
        "terraform_data",
    }
)

# Data sources this module is allowed to read. Reading is not owning: a data source cannot
# create, modify or destroy anything, and `terraform destroy` does not touch it. These are
# the approved platform interfaces.
ALLOWED_DATA_SOURCES = frozenset(
    {
        "aws_caller_identity",
        "aws_iam_policy_document",
        # The read-only platform interface: cluster OIDC identity, per environment.
        "terraform_remote_state",
    }
)

# Types that would mean this module had taken core platform infrastructure into its own
# state. Listed for the error message only — the allowlist above is what enforces the rule.
# Each maps to the surface it would damage on destroy.
PLATFORM_OWNED_TYPES = {
    "aws_vpc": "the shared VPC (platform/infra)",
    "aws_subnet": "shared subnets (platform/infra)",
    "aws_eks_cluster": "the ADP management cluster (platform/infra)",
    "aws_eks_node_group": "management cluster capacity (platform/infra)",
    "aws_eks_addon": "cluster addons (platform/infra)",
    "aws_iam_openid_connect_provider": "the cluster OIDC provider — consume it, never declare it",
    "aws_db_instance": "a database (Decision 2 is unresolved; see the module docstring)",
    "aws_rds_cluster": "a database (Decision 2 is unresolved; see the module docstring)",
    "aws_cloudfront_distribution": "the gateway frontend",
    "aws_lb": "the gateway ALB",
    "aws_s3_bucket": "shared buckets, including Terraform state",
    "aws_secretsmanager_secret": "secrets are seeded out of band; creating one here would put its value in state",
}


def _terraform_files() -> list[Path]:
    return sorted(CONTROL_PLANE.glob("*.tf"))


def _strip_comments(text: str) -> str:
    """Drop comment lines and heredoc bodies.

    Both legitimately discuss platform resources — main.tf explains why no `kubernetes`
    provider is declared, ecr.tf explains why the repositories are not in platform's shared
    module — so raw text would flag the reasoning as the violation.
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
    """Find `resource "type" "name"` or `data "type" "name"` declarations."""
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

    A regex that stopped matching would make every isolation assertion in this file pass
    vacuously — the same "no tests ran is not tests passed" hazard the domain CI lane calls
    out, reached through an empty parametrization instead of an empty glob.
    """
    assert len(RESOURCES) >= 5, (
        f"found only {len(RESOURCES)} resource declarations, which suggests this suite's "
        "parser has stopped matching rather than that the module was emptied."
    )
    assert DATA_SOURCES, "expected at least the platform remote-state data source."


@pytest.mark.parametrize(
    ("path", "resource_type", "resource_name"),
    RESOURCES,
    ids=[f"{t}.{n}" for _, t, n in RESOURCES],
)
def test_every_resource_is_domain_owned(
    path: Path, resource_type: str, resource_name: str
) -> None:
    """No platform-owned resource may enter this module's state.

    This is the assertion that makes "destroying this module cannot reach a platform
    resource" true, rather than merely intended.
    """
    if resource_type in ALLOWED_RESOURCE_TYPES:
        return

    damage = PLATFORM_OWNED_TYPES.get(resource_type)
    if damage:
        raise AssertionError(
            f"{path.name} declares `{resource_type}.{resource_name}`, which would take "
            f"{damage} into this domain app's state. Destroying the domain app would then "
            "destroy shared infrastructure. Consume the platform interface read-only via "
            "data.terraform_remote_state.platform instead. (Platform isolation "
            "requirement, 2026-09-16.)"
        )

    raise AssertionError(
        f"{path.name} declares `{resource_type}.{resource_name}`, which is not in this "
        "module's allowed resource set. If it is genuinely domain-owned — created and "
        "destroyed with Superplane, with nothing outside the domain app depending on it — "
        "add it to ALLOWED_RESOURCE_TYPES in this file with a note saying why. The list is "
        "an allowlist so that adding a type is where the reasoning gets recorded."
    )


@pytest.mark.parametrize(
    ("path", "data_type", "data_name"),
    DATA_SOURCES,
    ids=[f"{t}.{n}" for _, t, n in DATA_SOURCES],
)
def test_every_data_source_is_an_approved_interface(
    path: Path, data_type: str, data_name: str
) -> None:
    """Reading is allowed; the set of things read is still bounded.

    A data source cannot destroy anything, so this is a weaker constraint than the resource
    allowlist — but an unbounded one would let the module grow a dependency on a platform
    internal, which is how "consume approved interfaces" erodes.
    """
    assert data_type in ALLOWED_DATA_SOURCES, (
        f"{path.name} reads `data.{data_type}.{data_name}`, which is not an approved "
        "platform interface for this module. The approved read of platform state is "
        "data.terraform_remote_state.platform; add to ALLOWED_DATA_SOURCES with a "
        "rationale if a new interface is genuinely required."
    )


def test_the_platform_remote_state_is_read_only_and_not_repointable() -> None:
    """The platform state read must derive its bucket from the caller's own account.

    If the bucket came from a variable, a tfvars edit could point this module at another
    account's platform state — reading another environment's cluster identity while writing
    this one's resources. Deriving it from `aws_caller_identity` makes that unrepresentable.
    """
    main = CONTROL_PLANE / "main.tf"
    body = _strip_comments(main.read_text())

    match = re.search(
        r'data\s+"terraform_remote_state"\s+"platform"\s*\{(.+?)\n\}', body, re.DOTALL
    )
    assert match, "main.tf must declare data.terraform_remote_state.platform."

    block = match.group(1)
    assert "data.aws_caller_identity.current.account_id" in block, (
        "the platform state bucket must be derived from "
        "data.aws_caller_identity.current.account_id, so this module cannot be pointed at "
        "another account's platform state by editing tfvars."
    )
    assert "var.environment" in block, (
        "the platform state key must be scoped by var.environment — reading dev's platform "
        "outputs while writing test's state is a subtler form of shared state."
    )


def test_no_kubernetes_or_helm_provider_is_declared() -> None:
    """Rollout is a separate lane, and that is an isolation property as well as a design one.

    A `kubernetes` or `helm` provider here would put a rollout of application pods behind the
    same apply as IAM role creation, and would make every plan — including the credential-free
    plan lane — require EKS API reachability. It would also give this module's apply the
    ability to mutate arbitrary cluster objects, including those in core ADP namespaces.
    """
    for path in _terraform_files():
        body = _strip_comments(path.read_text())
        for forbidden in ("kubernetes", "helm", "kubectl"):
            assert not re.search(
                rf'^\s*provider\s+"{forbidden}"', body, re.MULTILINE
            ), (
                f"{path.name} declares a `{forbidden}` provider. Kubernetes objects are "
                "applied by the rollout lane (superplane-k8s-deploy.yml), mirroring the "
                "cyber-infra / cyber-k8s-deploy split — see the header in main.tf."
            )


# ---------------------------------------------------------------------------
# Keeping the Terraform suite's hardcoded parameter list honest.
# ---------------------------------------------------------------------------
#
# `tests/lock_pin.tftest.hcl` asserts that no SSM parameter publishes an image reference for
# one of the three pending Superplane images. It has to enumerate the parameters by name,
# because Terraform cannot iterate the resources of a type — so that list silently stops
# being a full sweep the moment somebody adds a parameter.
#
# That is the failure this guard exists for, and it is a realistic one: adding a parameter is
# the normal way this module grows, and nothing about doing so suggests you must also edit a
# test file two directories away. Without this, the run would keep passing while checking a
# subset, which is the vacuous-pass shape that has already bitten this suite once.


def _ssm_parameter_names() -> set[str]:
    """Terraform resource NAMES (not parameter paths) of every aws_ssm_parameter declared."""
    return {name for _, rtype, name in RESOURCES if rtype == "aws_ssm_parameter"}


def test_every_ssm_parameter_is_checked_for_pins() -> None:
    """Every declared aws_ssm_parameter must appear in lock_pin.tftest.hcl's sweep."""
    lock_pin = CONTROL_PLANE / "tests" / "lock_pin.tftest.hcl"
    assert lock_pin.is_file(), (
        f"{lock_pin} is referenced by config.tf and by this test; it must exist."
    )

    body = lock_pin.read_text()
    declared = _ssm_parameter_names()
    assert declared, (
        "no aws_ssm_parameter declarations found — this guard's parser has stopped matching."
    )

    missing = sorted(
        name for name in declared if f"aws_ssm_parameter.{name}.value" not in body
    )
    assert not missing, (
        "these SSM parameters are declared but are not covered by the pending-image sweep "
        f"in tests/lock_pin.tftest.hcl: {missing}. Add each to the list in "
        '`run "no_image_reference_is_published_for_a_pending_image"`. That run cannot '
        "enumerate parameters itself (Terraform has no way to iterate resources of a type), "
        "so an unlisted parameter is one that could publish a floating tag or a fabricated "
        "digest for one of the three pending images with nothing to catch it."
    )
