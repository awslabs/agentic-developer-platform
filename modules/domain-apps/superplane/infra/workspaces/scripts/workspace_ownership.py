"""What one workspace owns, and what a plan may do to it — Issue #5532 (w6-09), design item 4.

## What this adds that `../../scripts/domain_ownership.py` does not

That module governs the CONTROL-PLANE module, which runs against ADP's own account. Its
allowlist refuses `aws_vpc` and `aws_eks_cluster` outright, because a domain app taking
either into its state means destroying the domain app destroys shared ADP infrastructure.
This module's subject creates both, deliberately, in a tenant's account —
`../tests/test_control_plane_policy_unchanged.py` records why that needed a SEPARATE policy
rather than a widened shared one.

So this is the same shape applied to a different boundary, and it keeps the three properties
that made the original fail closed (checkpoint review of `84e3f7ee`):

1.  **Anything unparseable denies.** A malformed plan, an unknown action word, a resource
    whose ownership cannot be positively attributed. A guard that reports "nothing to worry
    about" when it could not read its input is the fail-open shape both modules exist to
    remove.
2.  **Deletion is read from `change.actions` as a set membership test.** A replacement is
    `["delete","create"]` or `["create","delete"]` depending on lifecycle, and the shell
    guard this pattern replaced reported "No destroys" for both.
3.  **Type-allowed is not instance-owned.** `aws_iam_role` is a type this module creates;
    `adp-dev-spw-tenant-beta-cluster-role` is a DIFFERENT workspace's role. Ownership is
    decided on identifying values, anchored, per type.

## The three refusals this module adds, which the domain policy has no reason to have

*   **Cross-workspace.** Two workspaces can live in one account. Their resource names differ
    only in the workspace segment, so a plan run with the wrong `-var-file` is shaped exactly
    like a correct one. `validate_plan(..., workspace_name=...)` refuses a resource belonging
    to any workspace other than the one named. This is design item 2's "separate state and
    ownership from ... other workspaces" made checkable.

*   **Ownership change.** Design item 3 allows supplied networking but forbids silently
    adopting its lifecycle. Two plan-visible events would do exactly that, and neither is a
    delete, so a destructive-change guard alone would pass them:
      - `change.importing` present on a network-owning type — an import moves a supplier's
        VPC into this state, after which `terraform destroy` deletes it. The count gate in
        `network.tf` does not help: an imported resource is a managed resource.
      - the `NetworkOwnership` tag flipping between `supplied` and `adp-created` — which is
        what a mode switch on an existing workspace looks like from the plan's side.

*   **An address the module does not declare.** For `aws_route_table_association`,
    `aws_iam_role_policy_attachment` and `aws_vpc_security_group_egress_rule` there is no
    identifying value at plan time: every field is a reference to another resource's
    AWS-assigned id, which is unknown. Denying them for being unattributable would deny every
    real plan; accepting them unconditionally would accept anything. So for those types
    ownership is decided on the ADDRESS, against the set of names the module's own `.tf`
    source declares — see `declared_addresses`.

## What this module deliberately does NOT decide

Whether an apply is *authorized*. `check_workspace_plan.py` owns that, and it requires the
destroying plan's address set to match an operator-supplied authorization EXACTLY, in both
directions. Nothing here reads a label, a PR or the network.

Nothing in this file mutates anything, reaches AWS, or needs a credential.
"""

from __future__ import annotations

import re
from workspace_identity import infrastructure_id, validate_identity_ids
from dataclasses import dataclass, field
from pathlib import Path

WORKSPACES_DIR = Path(__file__).resolve().parents[1]

# ---------------------------------------------------------------------------
# The policy: what a workspace may own.
#
# Every type here is scoped to ONE workspace — created in the workspace's account, named
# with the workspace's prefix, and destroyed with the workspace without reaching anything
# else. `../tests/test_workspace_isolation.py` imports these three names rather than keeping
# its own copy: one policy, enforced against the source by that suite and against a plan by
# this module. Two lists would drift, and the one that drifted would be the one nobody ran.
# ---------------------------------------------------------------------------
ALLOWED_RESOURCE_TYPES = frozenset(
    {
        # --- Network (owned mode only; every one is gated on local.owns_network, which
        # ../tests/test_networking_modes.py enforces by type category).
        "aws_vpc",
        "aws_subnet",
        "aws_internet_gateway",
        "aws_nat_gateway",
        "aws_vpc_endpoint",
        "aws_eip",
        "aws_route_table",
        "aws_route_table_association",
        # --- The workspace's own cluster. This is the "physically separate workspace EKS"
        # of design item 2. It is not, and cannot be, the ADP management cluster: main.tf
        # deliberately reads no platform remote state, so no expression in this module
        # resolves to it.
        "aws_eks_cluster",
        "aws_eks_node_group",
        "aws_eks_addon",
        # The node group's launch template. Workspace-scoped in the same sense as the node
        # group itself: named with the workspace prefix, referenced by nothing outside this
        # module's state, and destroyed with the workspace.
        #
        # It exists because EKS's DEFAULT managed-node template gives nodes a 20 GiB root
        # volume that is either unencrypted or encrypted with whatever the account default
        # happens to be — so without this template the module's customer-key encryption claim
        # is false for node disks, which is review finding W9-02. `encryption_config` on the
        # cluster covers Kubernetes Secrets in etcd, a different store entirely.
        #
        # Destroying it reaches nothing else: a launch template is a specification, not
        # capacity. Deleting it does not terminate a running node, and the node group that
        # references it is in this same state.
        "aws_launch_template",
        "aws_security_group",
        "aws_vpc_security_group_egress_rule",
        "aws_vpc_security_group_ingress_rule",
        # --- The workspace's identities. Least-privilege scoping is asserted separately by
        # ../tests/test_least_privilege.py; this list only says the TYPE is workspace-scoped.
        "aws_iam_role",
        "aws_iam_role_policy",
        "aws_iam_role_policy_attachment",
        # THIS cluster's trust anchor. Forbidden in the control plane, which must CONSUME
        # the management cluster's provider rather than declare one. Here it is required:
        # without it, a workspace pod uses the node role, so every pod on a node shares
        # those permissions and a compromise of one is a compromise of all.
        "aws_iam_openid_connect_provider",
        # --- Encryption and audit, both per-workspace.
        "aws_kms_key",
        "aws_kms_alias",
        # Declared explicitly because the group EKS creates implicitly has NEVER-EXPIRE
        # retention — an unbounded cost and an unmade compliance decision.
        "aws_cloudwatch_log_group",
        # --- A plan-time precondition carrier, not an AWS resource. Creates nothing in the
        # account, so destroying it cannot reach anything. Used by main.tf's target account
        # guard, which fails the PLAN when the named account and the caller's real identity
        # disagree.
        "terraform_data",
    }
)

# Data sources this module may read. Reading is not owning: a data source cannot create,
# modify or destroy, and `terraform destroy` does not touch it.
ALLOWED_DATA_SOURCES = frozenset(
    {
        "aws_caller_identity",
        "aws_security_group",
        "aws_vpc_security_group_rule",
        "aws_vpc_security_group_rules",
        "aws_partition",
        "aws_iam_session_context",
        "aws_iam_policy_document",
        "aws_vpc_endpoint",
        # The supplied VPC, in supplied networking mode. Reading it is precisely what keeps
        # it out of this module's lifecycle (design item 3).
        "aws_vpc",
        "aws_subnet",
        "aws_availability_zones",
        "aws_route_tables",
        "aws_route_table",
        "aws_nat_gateway",
        "aws_internet_gateway",
        # The OIDC issuer's CA thumbprint. Read rather than hardcoded: a pinned thumbprint
        # goes stale when AWS rotates the chain, breaking every IRSA role at once.
        "tls_certificate",
    }
)

# Types whose destruction would reach outside this workspace. Listed for the error message;
# the allowlist above is what enforces the rule.
BEYOND_WORKSPACE_TYPES = {
    "aws_organizations_account": (
        "an AWS account. Account lifecycle is #5531's, and #5530's rule is that account "
        "closure is never a consequence of removing a workspace — enforced by this module "
        "being unable to express an account at all"
    ),
    "aws_organizations_organizational_unit": "the organization's OU structure",
    "aws_organizations_policy": "an organization-wide policy affecting every account",
    "aws_organizations_account_parent": "an account's placement in the organization",
    "aws_s3_bucket": (
        "an S3 bucket — and the Terraform state backend is an S3 bucket, so a module that "
        "can create one can delete one on destroy"
    ),
    "aws_dynamodb_table": "a DynamoDB table — the Terraform state lock table is one",
    "aws_iam_account_password_policy": "an account-wide password policy",
    "aws_iam_account_alias": "the account's alias, which every workspace in it shares",
    "aws_iam_user": (
        "an IAM user, i.e. a long-lived credential. Workspace access goes through an "
        "assumable role with an MFA condition (see iam.tf)"
    ),
    "aws_iam_access_key": (
        "a long-lived access key, whose secret would be stored in this module's state"
    ),
    "aws_secretsmanager_secret": (
        "a secret whose value would enter Terraform state. Secrets are seeded out of band"
    ),
    "aws_secretsmanager_secret_version": "a secret value in Terraform state",
    "aws_servicequotas_service_quota": "an account-wide service quota",
    "aws_cloudfront_distribution": "the ADP gateway frontend",
    "aws_lb": "a load balancer outside this workspace's cluster",
    "aws_db_instance": "a database this story makes no durability claim about",
    "aws_rds_cluster": "a database this story makes no durability claim about",
}

# Types whose creation or deletion in supplied networking mode would act on a network ADP
# does not own. Importing one, or flipping its ownership tag, is the adoption design item 3
# forbids — so these are the types the ownership-change checks apply to.
#
# Kept in step with `../tests/test_networking_modes.py`'s NETWORK_OWNING_TYPES by a test, not
# by hope: that suite enforces the source-level gate on the same category, and a type in one
# list and not the other is a gap in whichever check was not updated.
NETWORK_OWNING_TYPES = frozenset(
    {
        "aws_vpc",
        "aws_subnet",
        "aws_internet_gateway",
        "aws_egress_only_internet_gateway",
        "aws_nat_gateway",
        "aws_eip",
        "aws_route_table",
        "aws_route",
        "aws_route_table_association",
        "aws_vpc_endpoint",
        "aws_vpc_peering_connection",
        "aws_vpn_gateway",
        "aws_transit_gateway_vpc_attachment",
        "aws_default_route_table",
        "aws_default_security_group",
        "aws_network_acl",
        "aws_flow_log",
    }
)

# Any Terraform action set containing one of these deletes something. A set membership test
# rather than a string match on the joined actions, because the orderings differ: a
# replacement is ["delete","create"] (destroy-then-create) or ["create","delete"]
# (create-before-destroy), and the shell guard this pattern replaced reported "No destroys in
# plan" for both.
DESTRUCTIVE_ACTIONS = frozenset({"delete"})

# Terraform's complete action vocabulary for `resource_changes[].change.actions`. Anything
# outside it means the document is not a plan this guard understands — see the refusal in
# `validate_plan`. Without this check a misspelled `"destroy"` would simply fail to intersect
# DESTRUCTIVE_ACTIONS and be reported as a safe change: deletion must be detected by
# recognising the vocabulary, not by failing to recognise it.
VALID_ACTIONS = frozenset({"no-op", "create", "read", "update", "delete"})

# Resource types this module replaces at a cost that is not a Terraform-level detail. A
# replacement of any of these is a destroy: the cluster's workloads, the workspace's private
# network, or the key that decrypts its Secrets. Called out by name so the guard's output says
# what a reviewer is actually approving rather than "1 replacement".
REPLACEMENT_IS_DESTRUCTION = {
    "aws_eks_cluster": (
        "replacing the cluster destroys every workload on it and issues a NEW OIDC issuer "
        "URL, so every IRSA role trusting the old one stops working"
    ),
    "aws_vpc": "replacing the VPC destroys the workspace's whole private network",
    "aws_kms_key": (
        "replacing the key makes every Secret and EBS snapshot encrypted under the old one "
        "permanently unreadable"
    ),
    "aws_eks_node_group": "replacing the node group terminates every node and evicts its pods",
    "aws_iam_openid_connect_provider": (
        "replacing the OIDC provider breaks every workload identity in the workspace at once"
    ),
}

# The tag whose value records which side of design item 3 produced this workspace's network.
# main.tf sets it from `local.owns_network`; a change to it between `before` and `after` is a
# lifecycle ownership change, which is the thing the mode gate exists to prevent.
NETWORK_OWNERSHIP_TAG = "NetworkOwnership"
NETWORK_OWNERSHIP_VALUES = frozenset({"adp-created", "supplied"})


class WorkspaceOwnershipError(Exception):
    """A plan artifact could not be validated. Always fail closed on this."""


@dataclass(frozen=True)
class Violation:
    address: str
    resource_type: str
    reason: str

    def __str__(self) -> str:
        return f"{self.address} [{self.resource_type}]: {self.reason}"


@dataclass
class PlanReport:
    """Outcome of validating one workspace plan."""

    violations: list[Violation] = field(default_factory=list)
    # Addresses this plan deletes or replaces, as plain address strings. Plain, because
    # `check_workspace_plan.py` compares this set to an operator's authorization and a
    # decorated string would never match.
    destructive: list[str] = field(default_factory=list)
    # Human-readable "<address> (actions: ...)" lines for the same set, plus the named
    # consequence when the type is in REPLACEMENT_IS_DESTRUCTION.
    destructive_detail: list[str] = field(default_factory=list)
    # `address -> the exact action list` for each destructive change. Carried separately from
    # `destructive` because review finding W9-04 requires authorization to name the intended
    # ACTIONS and not merely the addresses: a plain delete and a delete-then-create replacement
    # are the same address with very different consequences, so an approval of one must not
    # silently cover the other.
    destructive_actions: dict[str, list[str]] = field(default_factory=dict)
    checked: int = 0

    @property
    def ok(self) -> bool:
        return not self.violations

    @property
    def has_destructive_changes(self) -> bool:
        return bool(self.destructive)


# ---------------------------------------------------------------------------
# Address handling
# ---------------------------------------------------------------------------
def leaf_type_and_name(address: str) -> tuple[str, str]:
    """Reduce a Terraform address to its leaf resource type and name.

    Walks through arbitrary module nesting, so `module.core.aws_vpc.main[0]` yields
    `("aws_vpc", "main")` rather than hiding the type behind a `module.` prefix — which is
    how the guard this pattern replaced let a VPC destroy through (PR #5283 finding 3).

    This module declares no child modules, so a nested address is itself suspicious; it is
    still parsed rather than rejected here, because the type-level checks downstream give a
    better message than "malformed address" would.

    A `data.` prefix is reported as type `data.<type>`: reads are harmless but must be
    distinguishable from managed resources.
    """
    if not address or not isinstance(address, str):
        raise WorkspaceOwnershipError(f"malformed resource address: {address!r}")

    remainder = address
    while remainder.startswith("module."):
        parts = remainder.split(".", 2)
        if len(parts) < 3:
            raise WorkspaceOwnershipError(f"malformed module address: {address!r}")
        remainder = parts[2]

    is_data = remainder.startswith("data.")
    if is_data:
        remainder = remainder[len("data.") :]

    segments = remainder.split(".")
    if len(segments) < 2:
        raise WorkspaceOwnershipError(
            f"cannot determine resource type from address: {address!r}"
        )

    resource_type = segments[0]
    resource_name = re.sub(r"\[.*\]$", "", segments[1])

    if is_data:
        resource_type = f"data.{resource_type}"
    return resource_type, resource_name


def _strip_comments(text: str) -> str:
    """Drop comment lines and heredoc bodies before parsing declarations.

    This module's prose names most of BEYOND_WORKSPACE_TYPES while explaining what it does
    not own, and variables.tf's heredoc descriptions quote resource names at length. Raw text
    would read the explanation as a declaration. The same reason
    `../../control-plane/tests/test_platform_isolation.py` strips before matching.
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


_DECLARED_CACHE: frozenset[tuple[str, str]] | None = None


def declared_addresses(module_dir: Path | None = None) -> frozenset[tuple[str, str]]:
    """The `(type, name)` pairs this module's own `.tf` source declares as resources.

    ## Why ownership sometimes has to be decided on the address

    `aws_route_table_association`, `aws_iam_role_policy_attachment` and
    `aws_vpc_security_group_egress_rule` carry no identifying value at plan time: every
    field is a reference to another resource's AWS-assigned id, so `after` is unknown. There
    are only two honest options — deny them, which denies every real plan and gets the guard
    deleted, or decide on the address.

    Deciding on the address is sound here for a reason specific to this module: its state is
    one workspace's, it declares no child modules, and a root module's state can only
    legitimately contain addresses the root module declares. So an address with no
    declaration behind it is either a stale entry from a removed declaration — which
    `terraform destroy` still acts on — or a plan from a different configuration.

    Read from source rather than hardcoded for the reason `source_derived_names.py` records
    for the control plane: a hand-written list of addresses IS the assumption it is supposed
    to be checking, so a rename makes the list wrong and nothing fails.

    Raises if the source cannot be read or declares nothing. A guard whose reference set
    silently came back empty would accept every address.
    """
    global _DECLARED_CACHE
    if module_dir is None and _DECLARED_CACHE is not None:
        return _DECLARED_CACHE

    directory = module_dir or WORKSPACES_DIR
    files = sorted(directory.glob("*.tf"))
    if not files:
        raise WorkspaceOwnershipError(
            f"no .tf files found in {directory}: cannot determine which addresses this "
            f"module declares, and a guard with an empty reference set accepts everything."
        )

    opener = re.compile(r'^resource\s+"([^"]+)"\s+"([^"]+)"\s*\{', re.MULTILINE)
    found: set[tuple[str, str]] = set()
    for path in files:
        try:
            text = _strip_comments(path.read_text(encoding="utf-8"))
        except (
            OSError
        ) as exc:  # pragma: no cover - unreadable source is an environment fault
            raise WorkspaceOwnershipError(f"could not read {path}: {exc}") from exc
        for match in opener.finditer(text):
            found.add((match.group(1), match.group(2)))

    if not found:
        raise WorkspaceOwnershipError(
            f"parsed {len(files)} .tf file(s) in {directory} and found no resource "
            f"declarations. Either the module was emptied or this parser has stopped "
            f"matching; both must deny rather than accept every address."
        )

    result = frozenset(found)
    if module_dir is None:
        _DECLARED_CACHE = result
    return result


# ---------------------------------------------------------------------------
# EXACT expected names, read from the source's own name expressions.
#
# ## The defect this replaced (review finding W9-03)
#
# Ownership used to be decided by a prefix pattern:
# `^adp-<env>-spw-<workspace>(-[a-z0-9][a-z0-9-]*)?$`. The optional suffix group accepts
# ARBITRARY trailing text, and workspace names are themselves `[a-z0-9-]`, so for
# workspace `alpha` in `dev` the prefix is `adp-dev-spw-alpha` and the pattern accepts
# `adp-dev-spw-alpha-prod` — which is workspace `alpha-prod`'s cluster. The guard's own
# docstring claimed "two workspaces in one account differ only in this segment", and the
# pattern was blind to exactly that segment whenever one workspace's name is a prefix of
# another's. Confirmed by reproduction on the reviewed head.
#
# ## Why expected names are DERIVED rather than listed
#
# A hardcoded table of expected names IS the assumption it is meant to check: rename a
# resource in the Terraform and the table is silently wrong, in the direction that denies
# every real plan. So the name EXPRESSION is read from each declaration and the prefix
# interpolation is resolved with this plan's environment and workspace. `main.tf` remains the
# single source of truth for what things are called, and a rename fails the premise tests in
# ../tests/ rather than passing quietly.
#
# Only the two interpolations this module actually uses are resolved — `local.name_prefix`
# and `local.cluster_name` (equal to it), plus `local.cluster_log_group_name`. An expression
# containing anything else yields no expected name, which DENIES the resource rather than
# falling back to a prefix match: a name this parser cannot resolve is a name it cannot
# attribute, and W9-03 is what "attribute loosely instead" costs.
# ---------------------------------------------------------------------------
_NAME_ATTRIBUTE = re.compile(
    r'^\s*(?:name|node_group_name|addon_name)\s*=\s*("(?:[^"\\]|\\.)*"|[a-z_][a-z0-9_.]*)\s*$',
    re.MULTILINE,
)

_EXPECTED_NAMES_CACHE: dict[tuple[str, str], dict[tuple[str, str], str]] = {}


def _resolve_name_expression(
    expression: str, prefix: str, log_group_name: str
) -> str | None:
    """Resolve one HCL name expression to the literal name it produces, or None.

    None means "this parser cannot resolve it", which the caller treats as a denial. That
    direction is deliberate: an unresolvable expression must not degrade to a loose match.
    """
    # A bare local reference, e.g. `name = local.cluster_name`.
    if not expression.startswith('"'):
        if expression == "local.cluster_name":
            return prefix
        if expression == "local.cluster_log_group_name":
            return log_group_name
        return None

    body = expression[1:-1]

    # Only `${local.name_prefix}` is substituted. Any other interpolation — a variable, a
    # resource attribute, a function call — leaves a `${` behind and is refused below.
    body = body.replace("${local.name_prefix}", prefix)
    body = body.replace("${local.cluster_name}", prefix)
    if "${" in body:
        return None
    return body


def expected_names(
    environment: str,
    workspace_name: str,
    module_dir: Path | None = None,
    *,
    org_id: str,
    workspace_id: str,
) -> dict[tuple[str, str], str]:
    """`(type, name) -> the exact name that declaration produces for this workspace`.

    Declarations whose name expression this parser cannot resolve, and those with no name
    attribute at all, are absent from the mapping. `validate_identity` denies a
    name-identified resource that is absent from it, so an unparsed declaration fails loudly
    at validation time instead of silently widening what counts as owned.
    """
    cache_key = (environment, org_id, workspace_id)
    if module_dir is None and cache_key in _EXPECTED_NAMES_CACHE:
        return _EXPECTED_NAMES_CACHE[cache_key]

    prefix = name_prefix(
        environment, workspace_name, org_id=org_id, workspace_id=workspace_id
    )
    log_group_name = f"/aws/eks/{prefix}/cluster"

    directory = module_dir or WORKSPACES_DIR
    files = sorted(directory.glob("*.tf"))
    if not files:
        raise WorkspaceOwnershipError(
            f"no .tf files found in {directory}: cannot determine the names this module "
            f"gives its resources, and a guard with an empty reference set either accepts "
            f"everything or denies everything."
        )

    opener = re.compile(r'^resource\s+"([^"]+)"\s+"([^"]+)"\s*\{', re.MULTILINE)
    resolved: dict[tuple[str, str], str] = {}

    for path in files:
        try:
            text = _strip_comments(path.read_text(encoding="utf-8"))
        except (
            OSError
        ) as exc:  # pragma: no cover - unreadable source is an environment fault
            raise WorkspaceOwnershipError(f"could not read {path}: {exc}") from exc

        matches = list(opener.finditer(text))
        for index, match in enumerate(matches):
            end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
            block = text[match.end() : end]
            name_match = _NAME_ATTRIBUTE.search(block)
            if not name_match:
                continue
            literal = _resolve_name_expression(
                name_match.group(1).strip(), prefix, log_group_name
            )
            if literal is not None:
                resolved[(match.group(1), match.group(2))] = literal

    if not resolved:
        raise WorkspaceOwnershipError(
            f"parsed {len(files)} .tf file(s) in {directory} and resolved no resource names. "
            f"Either the module was emptied or this parser has stopped matching; both must "
            f"deny rather than accept every name."
        )

    if module_dir is None:
        _EXPECTED_NAMES_CACHE[cache_key] = resolved
    return resolved


# ---------------------------------------------------------------------------
# Identity: which field names a resource, and what that name must look like
# ---------------------------------------------------------------------------
def name_prefix(
    environment: str, workspace_name: str, *, org_id: str, workspace_id: str
) -> str:
    """`adp-<env>-spw-<immutable identifier>` — the prefix main.tf builds every name from.

    `spw` rather than the control plane's `superplane` so IAM role names stay inside AWS's
    64-character limit with a workspace name appended; variables.tf enforces the length
    budget that assumes this prefix.
    """
    return f"adp-{environment}-spw-{infrastructure_id(org_id, workspace_id)}"


# The plan attribute that carries each type's name.
#
# Only the ATTRIBUTE is recorded here now. What the value must BE is no longer described by a
# per-type pattern kind (`_PREFIXED`, `_ALIASED`, `_LOG_PATH`) — those existed because a
# pattern had to approximate the shape of a name, and approximating is what admitted another
# workspace's resources (W9-03). `expected_names()` reads the exact name each declaration
# produces from the source expression itself, so the alias's `alias/` prefix and the log
# group's `/aws/eks/<cluster>/cluster` path come from main.tf and eks.tf rather than from a
# second description of them here that could drift.
IDENTITY_FIELDS: dict[str, str] = {
    "aws_eks_cluster": "name",
    "aws_eks_addon": "addon_name",
    "aws_eks_node_group": "node_group_name",
    # Identified by its own name, not by tags. A launch template's `tags_all` describes the
    # TEMPLATE, while its `tag_specifications` blocks describe the instances and volumes it
    # will later launch — so a template could carry another workspace's tag_specifications
    # while its own tags looked correct. The name is the field that cannot be that ambiguous.
    "aws_launch_template": "name",
    "aws_iam_role": "name",
    "aws_iam_role_policy": "name",
    "aws_security_group": "name",
    "aws_kms_alias": "name",
    "aws_cloudwatch_log_group": "name",
}

# Types with no name of their own whose `tags_all` carries the workspace, because the
# provider's `default_tags` applies `local.common_tags` to everything it can tag.
#
# `tags_all` and not `tags`: `tags` holds only the resource-level block, which for these is
# just `Name`. default_tags land in `tags_all`, and the Workspace tag is in default_tags.
TAG_IDENTIFIED_TYPES = frozenset(
    {
        "aws_vpc",
        "aws_subnet",
        "aws_internet_gateway",
        "aws_nat_gateway",
        "aws_vpc_endpoint",
        "aws_eip",
        "aws_route_table",
        "aws_kms_key",
        "aws_iam_openid_connect_provider",
    }
)

# Types decided on the address, for the reason `declared_addresses` documents.
ADDRESS_IDENTIFIED_TYPES = frozenset(
    {
        "aws_route_table_association",
        "aws_iam_role_policy_attachment",
        "aws_vpc_security_group_egress_rule",
        "aws_vpc_security_group_ingress_rule",
        # Carries a plan-time precondition and creates nothing in the account. It has no
        # AWS identity to check because it has no AWS existence.
        "terraform_data",
    }
)

# ---------------------------------------------------------------------------
# WHAT AN ATTACHMENT-SHAPED RESOURCE POINTS AT (review finding W9-03, third reproduction)
#
# ## The defect
#
# The three types above carry no name of their own, so ownership was decided PURELY on the
# Terraform address — and an address is a label this module's source chooses, not a fact about
# AWS. So `aws_iam_role_policy_attachment.node_worker` was accepted because a declaration with
# that address exists, while its `role` field named `unrelated-production-node-role`: a
# resource in a foreign account's IAM, attached by a plan that the guard called owned.
# Reproduced on the reviewed head.
#
# Destroying such an attachment is not harmless. Removing an AWS-managed policy from a
# production role is a live permission change to something this workspace does not own.
#
# ## The repair, and why it still is not a name check
#
# These resources genuinely have no workspace-scoped name. What they DO have is a reference to
# the thing they attach to, and that thing is either a resource this same plan has already
# verified as owned, or it is foreign. So the address check is kept (it is a real property: a
# root module's state may only contain addresses the root module declares) and a TARGET check
# is added on top of it.
#
# The target is resolved against the names this plan verified, not against a pattern — which
# is what makes it immune to the prefix bug that produced the first reproduction.
#
# ## Unknown is not the same as foreign
#
# On a CREATE, every one of these fields is a reference to an AWS-assigned id that does not
# exist yet, so the plan records it as unknown. That is normal and must not be denied, or the
# guard denies every legitimate first apply. But a DESTRUCTIVE change has a `before`, and a
# `before` whose target cannot be resolved is refused: for the one class of change that cannot
# be undone, "I could not tell whose this is" must not resolve to "proceed".
# ---------------------------------------------------------------------------
# ## The follow-up defect: one untyped pool of owned names (W9-03, follow-up 1)
#
# The first repair resolved each target against the set of names this plan verified — but that
# set was UNTYPED. `_owned_identifiers` put EKS cluster names, launch-template names,
# security-group names and IAM role names into one pool, and a `role` field was accepted if it
# matched ANY of them. So an owned cluster named `adp-dev-spw-alpha` authorized detaching a
# managed policy from an IAM ROLE named `adp-dev-spw-alpha` — a role this module never declares
# and never verified. A same-spelled object of another kind must not confer ownership, so each
# field now names the resource TYPES that may vouch for it.
#
# ## Inline role policies belong here too (W9-03, follow-up 2)
#
# `aws_iam_role_policy` has a name of its own, so it was identified by name ALONE and never had
# its `role` checked. But an inline policy's name is scoped to its role: deleting
# `adp-dev-spw-alpha-admin-eks-access` from `unrelated-production-node-role` carried a correct
# policy name while changing a foreign role, and was accepted. Matching the policy name is not
# enough — the relationship it bears has to be checked as well, which is why this type appears
# in BOTH IDENTITY_FIELDS and here.
# ---------------------------------------------------------------------------
# Each field maps to the resource types whose verified identities may satisfy it, and whether
# it holds a NAME or an AWS-assigned ID. `None` for types means "any owned id", used only where
# the field is a generic id whose kind the plan does not distinguish.
RELATIONSHIP_TARGET_FIELDS: dict[str, dict[str, tuple[str, ...]]] = {
    "aws_eks_addon": {
        "cluster_name": ("aws_eks_cluster",),
        "service_account_role_arn": ("aws_iam_role",),
    },
    "aws_eks_cluster": {
        "role_arn": ("aws_iam_role",),
        "vpc_config[].subnet_ids[]": ("aws_subnet",),
        "vpc_config[].security_group_ids[]": ("aws_security_group",),
    },
    "aws_eks_node_group": {
        "cluster_name": ("aws_eks_cluster",),
        "node_role_arn": ("aws_iam_role",),
        "subnet_ids[]": ("aws_subnet",),
        "launch_template[].id": ("aws_launch_template",),
    },
    "aws_security_group": {"vpc_id": ("aws_vpc",)},
    "aws_vpc_endpoint": {
        "vpc_id": ("aws_vpc",),
        "subnet_ids[]": ("aws_subnet",),
        "security_group_ids[]": ("aws_security_group",),
    },
    "aws_vpc_security_group_ingress_rule": {
        "security_group_id": ("aws_security_group",),
        "referenced_security_group_id": ("workspace_eks_managed_group",),
    },
    # `role` holds the role NAME (not ARN), and only an IAM ROLE may vouch for it.
    # `policy_arn` is deliberately NOT checked: it names an AWS-managed policy in the `aws`
    # account (arn:aws:iam::aws:policy/...), which no workspace owns and which _check_account
    # would otherwise refuse.
    "aws_iam_role_policy_attachment": {"role": ("aws_iam_role",)},
    # The inline-policy case above. Same field, same single legitimate voucher.
    "aws_iam_role_policy": {"role": ("aws_iam_role",)},
    # AWS-assigned ids, each resolved against the ids of its OWN kind: a subnet id must be a
    # verified subnet's, a route-table id a verified route table's. Before typing, either was
    # satisfied by the other — or by a VPC.
    "aws_route_table_association": {
        "subnet_id": ("aws_subnet",),
        "route_table_id": ("aws_route_table",),
    },
    "aws_vpc_security_group_egress_rule": {
        "security_group_id": ("aws_security_group",)
    },
}

# Which of those fields hold a NAME rather than an AWS-assigned id. Names and ids live in
# separate indexes because they are separate namespaces: a name is derived from the module's
# source and knowable offline, while an id exists only once AWS has assigned it.
NAME_VALUED_TARGET_FIELDS = frozenset({"role", "cluster_name"})


def _values(side: object) -> dict:
    """The value map from one side of a change, or `{}` if that side is absent.

    `before` is null on a create and `after` is null on a delete; neither is an error.
    """
    return side if isinstance(side, dict) else {}


def _tag_value(values: dict, tag: str) -> str | None:
    for key in ("tags_all", "tags"):
        tags = values.get(key)
        if isinstance(tags, dict) and isinstance(tags.get(tag), str):
            return tags[tag]
    return None


def _check_exact_name(value: str, expected: str) -> str | None:
    """Return a reason string unless `value` is EXACTLY the name this declaration produces.

    Exact equality, not a pattern. The pattern this replaced accepted an arbitrary suffix
    after the workspace prefix, so workspace `alpha`'s guard accepted `alpha-prod`'s cluster:
    `^adp-dev-spw-alpha(-[a-z0-9][a-z0-9-]*)?$` matches `adp-dev-spw-alpha-prod`, and
    `alpha-prod` is a valid workspace name. Since the module builds every name from the prefix
    deterministically, there is exactly ONE correct string per declaration, and anything else
    is either another workspace's resource or a resource this module did not create.
    """
    if value == expected:
        return None
    return (
        f"name {value!r} is not the name this declaration produces for this workspace, "
        f"which is exactly {expected!r}. A type this module creates is not the same as an "
        f"instance this workspace owns: two workspaces in one account differ only in the "
        f"workspace segment, so a plan run against the wrong workspace is shaped exactly "
        f"like a correct one. Note in particular that one workspace's name can be a PREFIX "
        f"of another's, which is why this is an equality test and not a prefix match."
    )


def _present_sides(before: object, after: object) -> tuple[tuple[str, dict], ...]:
    """The sides of this change that EXIST, each labelled — review follow-up 3.

    A create legitimately has no `before` and a delete no `after`; those absences are facts about
    the action, not missing evidence. But a REPLACEMENT has both, and each refers to a DIFFERENT
    AWS object: the `before` is the thing that gets destroyed and the `after` is the thing that
    replaces it. Attributing such a change from whichever side happened to carry an identity is
    what review follow-up 3 reproduced — a replacement whose `after` carried this workspace's
    tags authorized destroying a `before` that carried none, so the destroyed object was never
    attributed to anybody.

    So every side that is present must carry its own evidence, and this returns them separately
    rather than merging them into one value map. `before` is listed first because it is the side
    whose misattribution is irreversible.
    """
    return tuple(
        (label, values)
        for label, values in (("before", before), ("after", after))
        if isinstance(values, dict)
    )


def validate_identity(
    address: str,
    before: object,
    after: object,
    *,
    environment: str,
    workspace_name: str,
    org_id: str,
    workspace_id: str,
    account_id: str | None = None,
) -> list[Violation]:
    """Decide whether one changed resource belongs to this workspace.

    Anything that cannot be positively attributed is a violation. `environment` and
    `workspace_name` are required rather than optional, along with immutable IDs: these establish the
    cross-workspace refusal, and a guard that silently skipped its central check when a
    caller forgot an argument would pass every plan.
    """
    resource_type, resource_name = leaf_type_and_name(address)

    if resource_type.startswith("data."):
        bare = resource_type.removeprefix("data.")
        if bare not in ALLOWED_DATA_SOURCES:
            return [
                Violation(
                    address,
                    resource_type,
                    f"reads a data source outside this module's approved read set "
                    f"({sorted(ALLOWED_DATA_SOURCES)}). Reading is not owning, but an "
                    f"unreviewed read is still a coupling.",
                )
            ]
        return []

    if resource_type not in ALLOWED_RESOURCE_TYPES:
        damage = BEYOND_WORKSPACE_TYPES.get(resource_type)
        if damage:
            return [
                Violation(
                    address,
                    resource_type,
                    f"would take {damage} into one workspace's state, so destroying the "
                    f"workspace would reach beyond the workspace boundary. This module "
                    f"legitimately owns VPCs and EKS clusters — which the control-plane "
                    f"module may not — precisely because those are the workspace's own. "
                    f"This type is not.",
                )
            ]
        return [
            Violation(
                address,
                resource_type,
                "resource type is not in this workspace module's allowlist. The allowlist "
                "fails closed on purpose: a denylist passes whatever nobody thought to "
                "forbid, and the types that would hurt most are the ones a future author "
                "adds for a locally sensible reason.",
            )
        ]

    if (resource_type, resource_name) not in declared_addresses():
        return [
            Violation(
                address,
                resource_type,
                f'no `resource "{resource_type}" "{resource_name}"` declaration exists '
                f"in this module's source. A root module's state can only legitimately "
                f"contain addresses the root module declares, so this is either a stale "
                f"entry from a removed declaration — which `terraform destroy` still acts "
                f"on — or a plan taken against a different configuration.",
            )
        ]

    violations: list[Violation] = []

    # Values from whichever side has them. On a delete, `after` is null and `before` is the
    # resource being destroyed — which is precisely the change whose ownership matters most,
    # so both sides are consulted rather than only `after`.
    after_values = _values(after)
    before_values = _values(before)

    if resource_type in IDENTITY_FIELDS:
        field_name = IDENTITY_FIELDS[resource_type]
        names = expected_names(
            environment, workspace_name, org_id=org_id, workspace_id=workspace_id
        )
        expected = names.get((resource_type, resource_name))
        if expected is None:
            # The declaration exists (checked above) but its name expression could not be
            # resolved to a literal. Denying rather than falling back to a looser test is the
            # whole point of W9-03: a name this guard cannot predict is a name it cannot
            # verify, and approximating is what admitted another workspace's cluster.
            return [
                Violation(
                    address,
                    resource_type,
                    f"this module declares `{resource_type}.{resource_name}` but its `name` "
                    f"expression could not be resolved to the exact literal it produces for "
                    f"workspace {workspace_name!r} in {environment!r}, so ownership cannot be "
                    f"decided. Name it from `local.name_prefix` (see main.tf), or teach "
                    f"`_resolve_name_expression` in workspace_ownership.py the new form — do "
                    f"not relax the comparison, which is the defect W9-03 reported.",
                )
            ]

        # EVERY side that exists is checked INDEPENDENTLY, and each must carry its own name.
        # The earlier rule pooled both sides and required at least one name among them, so a
        # replacement whose `after` was correctly named satisfied the check for a `before` that
        # carried no name at all — and the `before` is the object being destroyed. See
        # `_present_sides`.
        for side, values in _present_sides(before, after):
            value = values.get(field_name)
            if not isinstance(value, str) or not value:
                fate = "destroyed" if side == "before" else "created"
                violations.append(
                    Violation(
                        address,
                        resource_type,
                        f"its {side} side carries no {field_name!r}, so the object on that side "
                        f"cannot be attributed to a workspace. Unattributable is denied: a "
                        f"guard that cannot tell whose resource this is must not conclude it is "
                        f"ours. Each side of a change is a DIFFERENT AWS object — on a "
                        f"replacement the {side} side is the one that gets {fate} — so the other "
                        f"side's name does not attribute this one.",
                    )
                )
                continue
            reason = _check_exact_name(value, expected)
            if reason:
                violations.append(
                    Violation(address, resource_type, f"on its {side} side, {reason}")
                )

    elif resource_type in TAG_IDENTIFIED_TYPES:
        # BOTH identity tags are checked, on BOTH sides. The Workspace tag alone was the
        # second W9-03 reproduction: a VPC tagged Workspace=tenant-alpha, Environment=prod was
        # accepted by a plan for environment=dev, because nothing compared the environment.
        # The two tags are separate facts — one workspace name can exist in several
        # environments, and `dev` and `prod` are different blast radii.
        #
        # Per tag: EVERY side that exists must carry it, and must carry the right value. Not
        # "at least one side", which is what the reviewed head did and what review follow-up 3
        # reproduced: a replacement whose `after` carried Workspace and Environment authorized
        # destroying a `before` that carried neither, so the object actually destroyed was never
        # attributed to this workspace at all. Each side is a different AWS object (see
        # `_present_sides`), and a create's absent `before` / a delete's absent `after` are the
        # only absences that are facts about the action rather than missing evidence.
        for tag, expected_value, consequence in (
            ("OrgId", org_id, "A different organization never owns this workspace."),
            (
                "WorkspaceId",
                workspace_id,
                "Two workspaces can live in one account; this is another one's resource.",
            ),
            (
                "Environment",
                environment,
                "One workspace name can exist in several environments, and they are different "
                "blast radii — a plan for dev must not act on prod's resources. This is the "
                "cross-environment case the Workspace tag alone cannot detect.",
            ),
        ):
            for side, values in _present_sides(before, after):
                value = _tag_value(values, tag)
                if not value:
                    # A create's tags ARE known at plan time (they are literals from
                    # local.common_tags), so a missing identity tag here means either the tag was
                    # dropped from main.tf — which would make per-workspace cost attribution
                    # impossible, design item 4's whole basis — or this is not our resource.
                    violations.append(
                        Violation(
                            address,
                            resource_type,
                            f"carries no `{tag}` tag on its {side} side. That tag is part of "
                            f"what makes one workspace's resources and spend separable from "
                            f"another's (design item 4), and without it the object on that side "
                            f"cannot be attributed at all. The other side's tags do not "
                            f"attribute this one: on a replacement they describe a DIFFERENT "
                            f"AWS object, so tagging the new resource would otherwise "
                            f"retroactively authorize destroying an unattributed old one.",
                        )
                    )
                    continue
                if value != expected_value:
                    violations.append(
                        Violation(
                            address,
                            resource_type,
                            f"is tagged {tag}={value!r} on its {side} side, but this plan is "
                            f"for {tag}={expected_value!r}. {consequence}",
                        )
                    )

    elif resource_type not in ADDRESS_IDENTIFIED_TYPES:
        # Reachable only by adding a type to ALLOWED_RESOURCE_TYPES without saying how it is
        # identified. Denying is the fail-closed answer: the alternative is a type that is
        # allowed and never attributed, which is exactly failure direction 1.
        violations.append(
            Violation(
                address,
                resource_type,
                "is in ALLOWED_RESOURCE_TYPES but has no identity rule, so ownership cannot "
                "be decided for it. Add it to IDENTITY_FIELDS, TAG_IDENTIFIED_TYPES or "
                "ADDRESS_IDENTIFIED_TYPES in workspace_ownership.py with the reasoning.",
            )
        )

    if resource_type == "aws_iam_role":
        for side, values in _present_sides(before, after):
            arn = values.get("arn")
            if isinstance(arn, str) and arn.rsplit("/", 1)[-1] != values.get("name"):
                violations.append(
                    Violation(
                        address,
                        resource_type,
                        f"{side}.arn contradicts the verified IAM role name",
                    )
                )

    # Names never override explicit contradictory immutable ownership tags. Check
    # both maps: tags_all must not conceal a conflicting resource-level tags entry.
    for side, values in _present_sides(before, after):
        for tag_map in ("tags", "tags_all"):
            tags = values.get(tag_map)
            if not isinstance(tags, dict):
                continue
            for tag, expected_value in (
                ("OrgId", org_id),
                ("WorkspaceId", workspace_id),
                ("Environment", environment),
            ):
                if tag in tags and tags[tag] != expected_value:
                    violations.append(
                        Violation(
                            address,
                            resource_type,
                            f"{side}.{tag_map}.{tag} contradicts immutable ownership: "
                            f"expected {expected_value!r}, got {tags[tag]!r}",
                        )
                    )

    # The environment is checked directly — inside the derived name for name-identified types,
    # and as its own tag for tag-identified ones. It is NOT left to the prefix alone: the
    # earlier version relied on that and accepted a VPC tagged Environment=prod under a dev
    # plan, because a tag-identified resource's environment never appears in a name.
    #
    # The account appears in neither, so it is checked wherever an ARN does.
    if account_id:
        violations.extend(
            _check_account(
                address, resource_type, after_values, before_values, account_id
            )
        )

    return violations


# ---------------------------------------------------------------------------
# Relationship targets, resolved against what THIS plan verified as owned
# ---------------------------------------------------------------------------
def _owned_identifiers(
    changes: list,
    environment: str,
    workspace_name: str,
    account_id: str | None,
    org_id: str,
    workspace_id: str,
    side_only: str | None = None,
) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """The verified names and ids of this plan's resources, INDEXED BY RESOURCE TYPE.

    Returns `(names_by_type, ids_by_type)`. Typed rather than pooled: review follow-up 1 found
    that one flat set let an owned EKS CLUSTER named `adp-dev-spw-alpha` vouch for an IAM ROLE
    of the same name, which this module never declares. A name identifies a resource only
    together with its kind.

    Only resources with NO violations contribute: a resource that failed its own check must not
    then serve as the justification for accepting an attachment that points at it, or one
    foreign resource would validate a whole subgraph.

    That filter is what makes harvesting from BOTH sides sound, and it is sound only because
    `validate_identity` now attributes each present side independently (review follow-up 3).
    Under the previous "at least one side" rule, a replacement could pass with a tagged `after`
    and an untagged `before`, and this function would then have published that unattributed
    `before.id` as a verified owned id — letting the foreign object it names vouch for the
    associations and rules that point at it. Both properties are needed: one resource's
    violations disqualify it, and one side's evidence does not cover the other's.
    """
    names_by_type: dict[str, set[str]] = {}
    ids_by_type: dict[str, set[str]] = {}

    for change in changes:
        if not isinstance(change, dict):
            continue
        address = change.get("address")
        detail = change.get("change")
        if not address or not isinstance(detail, dict):
            continue

        resource_type, _ = leaf_type_and_name(address)
        if resource_type in ADDRESS_IDENTIFIED_TYPES or resource_type.startswith(
            "data."
        ):
            continue

        if validate_identity(
            address,
            detail.get("before"),
            detail.get("after"),
            environment=environment,
            workspace_name=workspace_name,
            org_id=org_id,
            workspace_id=workspace_id,
            account_id=account_id,
        ):
            continue

        for _side, values in _present_sides(detail.get("before"), detail.get("after")):
            if side_only is not None and _side != side_only:
                continue
            for name_field in ("name", "node_group_name"):
                value = values.get(name_field)
                if isinstance(value, str) and value:
                    names_by_type.setdefault(resource_type, set()).add(value)
            for identifier_field in ("id", "arn"):
                identifier = values.get(identifier_field)
                if isinstance(identifier, str) and identifier:
                    ids_by_type.setdefault(resource_type, set()).add(identifier)
            if resource_type == "aws_eks_cluster":
                for group in relationship_values(
                    values, "vpc_config[].cluster_security_group_id"
                ):
                    if isinstance(group, str) and re.fullmatch(
                        r"sg-[0-9a-f]{8,17}", group
                    ):
                        ids_by_type.setdefault(
                            "workspace_eks_managed_group", set()
                        ).add(group)

    return names_by_type, ids_by_type


def relationship_values(values: object, path: str, *, unknown: bool = False) -> list:
    """Read a required typed path, retaining missing and malformed members as refusals.

    Terraform represents nested blocks and sets as lists. An unknown mask may mark
    a whole ancestor True; only mask traversal propagates that marker to a leaf.
    """
    parts = path.split(".")

    def visit(value, remaining):
        if unknown and value is True:
            return [True]
        if not remaining:
            return [value]
        part, *rest = remaining
        many = part.endswith("[]")
        key = part[:-2] if many else part
        child = value.get(key) if isinstance(value, dict) else None
        if many:
            if unknown and child is True:
                return [True]
            if not isinstance(child, list) or not child:
                return [None]
            return [leaf for item in child for leaf in visit(item, rest)]
        return visit(child, rest)

    return visit(values, parts)


def _configuration_expression(plan: dict, address: str, target_field: str) -> dict:
    """Root-module configuration is authenticated with the complete saved binary."""
    base = re.sub(r"\[[^]]*\]", "", address)
    for resource in (
        plan.get("configuration", {}).get("root_module", {}).get("resources", [])
    ):
        if resource.get("address") == base:
            # A terminal [] describes plan values; configuration stores one expression.
            config_path = target_field.removesuffix("[]")
            values = relationship_values(resource.get("expressions", {}), config_path)
            return values[0] if len(values) == 1 and isinstance(values[0], dict) else {}
    return {}


def _created_target_reference(
    plan, address, target_field, permitted_types, valid_addresses
):
    """Unknown create-side IDs require a configuration reference to a verified parent.

    Every resource reference must resolve to a permitted, independently attributed
    parent. count.index selects only among that verified collection. Arbitrary local
    expressions and unrelated references cannot authenticate an unknown target.
    """
    refs = _configuration_expression(plan, address, target_field).get("references", [])
    if not isinstance(refs, list) or not refs:
        return False
    if permitted_types == ("workspace_eks_managed_group",):
        base = "aws_eks_cluster.workspace"
        if not all(isinstance(ref, str) for ref in refs):
            return False
        normalized = {re.sub(r"\[[^]]*\]", "", ref) for ref in refs}
        exact = base + ".vpc_config.cluster_security_group_id"
        return (
            base in valid_addresses
            and exact in normalized
            and normalized.issubset({base, base + ".vpc_config", exact})
        )
    found = False
    for ref in refs:
        if ref == "count.index":
            continue
        if not isinstance(ref, str):
            return False
        normalized = re.sub(r"\[[^]]*\]", "", ref)
        base = ".".join(normalized.split(".")[:2])
        if base not in valid_addresses or base.split(".")[0] not in permitted_types:
            return False
        attribute = (
            "name"
            if target_field in NAME_VALUED_TARGET_FIELDS
            else "arn"
            if target_field.endswith("role_arn")
            else "id"
        )
        allowed_refs = {base, base + "." + attribute}
        # The AWS provider's IAM role id is its RoleName, not its ARN.
        if target_field == "role" and permitted_types == ("aws_iam_role",):
            allowed_refs.add(base + ".id")
        if normalized not in allowed_refs:
            return False
        found = True
    return found


def _check_relationship_targets(
    address: str,
    resource_type: str,
    change: dict,
    *,
    owned_by_side: dict,
    is_destructive: bool,
    plan: dict,
    valid_addresses: set[str],
) -> list[Violation]:
    """Attribute each present relationship side independently to its typed parent."""
    violations: list[Violation] = []
    for target_field, permitted_types in RELATIONSHIP_TARGET_FIELDS.get(
        resource_type, {}
    ).items():
        for side, values in _present_sides(change.get("before"), change.get("after")):
            owned_names, owned_ids = owned_by_side[side]
            index = (
                owned_names if target_field in NAME_VALUED_TARGET_FIELDS else owned_ids
            )
            reference = set().union(*(index.get(t, set()) for t in permitted_types))
            targets = relationship_values(values, target_field)
            known = all(isinstance(value, str) and value for value in targets)
            allowed = known and all(value in reference for value in targets)
            masks = relationship_values(
                change.get("after_unknown", {}), target_field, unknown=True
            )
            computed = (
                side == "after"
                and "create" in change.get("actions", [])
                and all(value is True for value in masks)
            )
            variables = plan.get("variables", {})
            mode = variables.get("networking_mode", {}).get("value")
            refs = _configuration_expression(plan, address, target_field).get(
                "references"
            )
            network_field = target_field in (
                "vpc_id",
                "subnet_ids[]",
                "vpc_config[].subnet_ids[]",
            )
            if network_field:
                subnet = target_field != "vpc_id"
                local_ref = "local.private_subnet_ids" if subnet else "local.vpc_id"
                if mode == "supplied":
                    supplied = variables.get(
                        "supplied_private_subnet_ids" if subnet else "supplied_vpc_id",
                        {},
                    ).get("value")
                    supplied = supplied if subnet else [supplied]
                    allowed = (
                        known
                        and isinstance(supplied, list)
                        and bool(supplied)
                        and set(targets) == set(supplied)
                        and refs == [local_ref]
                    )
                elif mode == "owned":
                    if not known and computed:
                        parent = "aws_subnet.private" if subnet else "aws_vpc.workspace"
                        allowed = refs == [local_ref] and parent in valid_addresses
                else:
                    allowed = False
            elif not known and computed:
                allowed = _created_target_reference(
                    plan,
                    address,
                    target_field,
                    permitted_types,
                    valid_addresses,
                )
            value = targets[0] if len(targets) == 1 else targets
            if not allowed:
                destruction = (
                    "DESTRUCTIVE " if is_destructive and side == "before" else ""
                )
                kind = "name" if target_field in NAME_VALUED_TARGET_FIELDS else "id"
                diagnosis = (
                    f"is not the {kind} of any {' or '.join(permitted_types)} this plan verified "
                    if known
                    else "cannot be resolved "
                )
                violations.append(
                    Violation(
                        address,
                        resource_type,
                        f"{destruction}{side}.{target_field}={value!r} {diagnosis}and is not "
                        f"bound to a verified {' / '.join(permitted_types)} parent. "
                        "Each present side needs its own ownership evidence; another side "
                        "or resource type cannot supply it: another kind does not confer ownership. "
                        "Computed create-side IDs require "
                        "authenticated configuration references.",
                    )
                )
    return violations


def _check_account(
    address: str,
    resource_type: str,
    after_values: dict,
    before_values: dict,
    account_id: str,
) -> list[Violation]:
    """Refuse a resource whose own ARN names a different account.

    Field-wise ARN parsing rather than a substring search: an ARN's account lives in field 4,
    and a substring test cannot tell an account id in the account field from the same digits
    appearing inside a resource name.

    Only the resource's OWN arn is checked. `role_arn`, `key_arn` and similar reference other
    resources, and a workspace legitimately uses an operator-supplied KMS key from another
    account — `var.kms_key_arn` exists for exactly that. Checking every ARN-shaped field
    would deny that supported case.
    """
    for values in (after_values, before_values):
        arn = values.get("arn")
        if not isinstance(arn, str) or not arn.startswith("arn:"):
            continue
        parts = arn.split(":", 5)
        if len(parts) < 6:
            return [
                Violation(
                    address,
                    resource_type,
                    f"has a malformed ARN {arn!r}; refusing to attribute a resource whose "
                    f"identity cannot be parsed.",
                )
            ]
        # IAM and other global services leave the account field populated but the region
        # empty; only the account field is consulted here.
        if parts[4] and parts[4] != account_id:
            return [
                Violation(
                    address,
                    resource_type,
                    f"has an ARN in account {parts[4]}, but this plan targets account "
                    f"{account_id}. A workspace's resources live in the workspace's own "
                    f"account.",
                )
            ]
    return []


# ---------------------------------------------------------------------------
# Ownership change: adoption of a network ADP does not own
# ---------------------------------------------------------------------------
def _check_ownership_change(
    address: str, resource_type: str, change: dict
) -> list[Violation]:
    """Refuse the two plan-visible routes into adopting supplied infrastructure.

    Neither is a delete, so a destructive-change guard alone passes both — which is why this
    is a separate check rather than another branch of the delete detection.
    """
    violations: list[Violation] = []

    # 1. An import. Terraform records it as `change.importing` (with the imported id inside)
    # from 1.5 onward. An imported resource is a MANAGED resource, so the count gate in
    # network.tf does not protect against it: the next `terraform destroy` deletes the
    # supplier's VPC.
    importing = change.get("importing")
    if importing is not None:
        imported_id = ""
        if isinstance(importing, dict):
            imported_id = str(importing.get("id", ""))
        violations.append(
            Violation(
                address,
                resource_type,
                f"is being IMPORTED{f' (id {imported_id})' if imported_id else ''}. That "
                f"moves an existing resource into this workspace's lifecycle. This provisioning "
                f"path does not authorize adoption of EKS, IAM, KMS, networking or any "
                f"other existing resource. Use separately reviewed migration tooling.",
            )
        )

    before_ownership = _tag_value(_values(change.get("before")), NETWORK_OWNERSHIP_TAG)
    after_ownership = _tag_value(_values(change.get("after")), NETWORK_OWNERSHIP_TAG)

    # 2. The ownership tag flipping on an existing resource. This is what switching
    # networking_mode on a live workspace looks like from the plan's side.
    if before_ownership and after_ownership and before_ownership != after_ownership:
        violations.append(
            Violation(
                address,
                resource_type,
                f"changes {NETWORK_OWNERSHIP_TAG} from {before_ownership!r} to "
                f"{after_ownership!r}. Networking ownership is not an in-place update: "
                f"moving between owned and supplied networking changes which party's "
                f"lifecycle this state governs. Create the workspace in the intended mode "
                f"rather than converting one.",
            )
        )

    for value in (before_ownership, after_ownership):
        if value and value not in NETWORK_OWNERSHIP_VALUES:
            violations.append(
                Violation(
                    address,
                    resource_type,
                    f"has {NETWORK_OWNERSHIP_TAG}={value!r}, which is not one of "
                    f"{sorted(NETWORK_OWNERSHIP_VALUES)}. The tag is what an operator reads "
                    f"off a VPC to know whether ADP created it; an unrecognised value "
                    f"answers that question wrongly rather than not at all.",
                )
            )

    return violations


# ---------------------------------------------------------------------------
# Plan validation
# ---------------------------------------------------------------------------
def validate_plan(
    plan: dict,
    *,
    environment: str,
    workspace_name: str,
    org_id: str,
    workspace_id: str,
    account_id: str | None = None,
) -> PlanReport:
    """Validate a `terraform show -json <planfile>` document for one workspace.

    Raises WorkspaceOwnershipError on anything malformed. A guard that cannot parse its input
    must not conclude "nothing to worry about".
    """
    if not isinstance(plan, dict):
        raise WorkspaceOwnershipError("plan JSON is not an object")

    # `format_version` is required UNCONDITIONALLY, which is stricter than the domain guard's
    # "either this or resource_changes". Caught by this module's own test suite: a document of
    # `{"resource_changes": []}` satisfied the weaker rule and was accepted as an empty plan,
    # while `terraform show -json` ALWAYS emits format_version. So a document without it is
    # not the output of that command — it is a truncated file, a hand-edited fragment, or an
    # error payload that happened to be valid JSON. Requiring it costs nothing real and
    # removes the one shape that could reach the accept path without being a plan.
    if "format_version" not in plan:
        raise WorkspaceOwnershipError(
            "plan JSON has no 'format_version'; refusing to treat an unrecognised document "
            "as a plan. `terraform show -json` always emits it, so its absence means this "
            "is not that command's output."
        )

    # Schema BEFORE emptiness. The `84e3f7ee` checkpoint review reproduced the version of
    # this that read `plan.get("resource_changes") or []`, which coerced every falsy wrong
    # type — false, 0, "", {} — into an accepted empty list, so the isinstance check below it
    # could never fire. The CLI printed "Validated 0 resource change(s)" and exited 0 on
    # {"format_version":"1.2","resource_changes":false}.
    #
    # A legitimately EMPTY plan is preserved: absent, or present as [], both mean "no
    # changes" and both pass. The distinction is absent-or-list versus present-but-not-a-list.
    if "resource_changes" in plan:
        changes = plan["resource_changes"]
        if not isinstance(changes, list):
            raise WorkspaceOwnershipError(
                f"'resource_changes' must be a list, got {type(changes).__name__} "
                f"({changes!r}); refusing to treat a malformed plan document as an empty plan"
            )
    else:
        changes = []

    if not environment or not workspace_name:
        raise WorkspaceOwnershipError(
            "environment and workspace_name are required to validate a workspace plan: "
            "they are the basis of the cross-workspace refusal, and without them every "
            "workspace's resources would look like this one's."
        )

    try:
        validate_identity_ids(org_id, workspace_id)
    except ValueError as exc:
        raise WorkspaceOwnershipError(str(exc)) from exc

    report = PlanReport()

    # The set of resources in THIS plan that pass their own identity check, computed before the
    # main loop because an attachment must be resolved against the whole plan rather than
    # against the entries that happen to precede it. Terraform's `resource_changes` order is
    # not a dependency order, so a per-entry accumulation would accept or refuse the same
    # attachment depending on where it appeared.
    #
    # Only clean resources contribute — see `_owned_identifiers`. One foreign resource must not
    # become the justification for accepting everything attached to it.
    owned_by_side = {
        side: _owned_identifiers(
            changes,
            environment,
            workspace_name,
            account_id,
            org_id,
            workspace_id,
            side_only=side,
        )
        for side in ("before", "after")
    }

    valid_addresses = {
        re.sub(r"\[[^]]*\]", "", entry["address"])
        for entry in changes
        if isinstance(entry, dict)
        and isinstance(entry.get("change"), dict)
        and isinstance(entry.get("address"), str)
        and not validate_identity(
            entry["address"],
            entry["change"].get("before"),
            entry["change"].get("after"),
            environment=environment,
            workspace_name=workspace_name,
            org_id=org_id,
            workspace_id=workspace_id,
            account_id=account_id,
        )
    }

    for change in changes:
        if not isinstance(change, dict):
            raise WorkspaceOwnershipError(
                f"resource_changes entry is not an object: {change!r}"
            )
        address = change.get("address")
        if not address:
            raise WorkspaceOwnershipError(
                f"resource_changes entry has no address: {change!r}"
            )

        detail = change.get("change")
        if not isinstance(detail, dict):
            raise WorkspaceOwnershipError(
                f"{address}: 'change' is missing or not an object"
            )
        actions = detail.get("actions")
        if not isinstance(actions, list) or not actions:
            raise WorkspaceOwnershipError(
                f"{address}: 'change.actions' is missing or not a list"
            )
        unknown = [a for a in actions if a not in VALID_ACTIONS]
        if unknown:
            raise WorkspaceOwnershipError(
                f"{address}: unrecognised action(s) {unknown!r} in 'change.actions'; valid "
                f"actions are {sorted(VALID_ACTIONS)}. Refusing to classify a plan whose "
                f"action vocabulary is not understood."
            )

        report.checked += 1
        resource_type, _ = leaf_type_and_name(address)

        is_destructive = bool(DESTRUCTIVE_ACTIONS.intersection(actions))
        if is_destructive:
            # Covers ["delete"], ["delete","create"] and ["create","delete"] — a plain
            # destroy and both replacement orderings.
            report.destructive.append(address)
            report.destructive_actions[address] = list(actions)
            line = f"{address} (actions: {', '.join(actions)})"
            consequence = REPLACEMENT_IS_DESTRUCTION.get(resource_type)
            if consequence:
                line += f" — {consequence}"
            report.destructive_detail.append(line)

        report.violations.extend(
            validate_identity(
                address,
                detail.get("before"),
                detail.get("after"),
                environment=environment,
                workspace_name=workspace_name,
                org_id=org_id,
                workspace_id=workspace_id,
                account_id=account_id,
            )
        )
        report.violations.extend(
            _check_relationship_targets(
                address,
                resource_type,
                detail,
                owned_by_side=owned_by_side,
                is_destructive=is_destructive,
                plan=plan,
                valid_addresses=valid_addresses,
            )
        )
        report.violations.extend(
            _check_ownership_change(address, resource_type, detail)
        )

    # Drift Terraform detected outside this configuration. Validated too: drift on a resource
    # this workspace does not own means the state contains something foreign, whether or not
    # this plan proposes to change it.
    drift = plan.get("resource_drift")
    if drift is not None and not isinstance(drift, list):
        raise WorkspaceOwnershipError(
            f"'resource_drift' must be a list, got {type(drift).__name__}"
        )
    for entry in drift or []:
        if not isinstance(entry, dict) or not entry.get("address"):
            raise WorkspaceOwnershipError(
                f"resource_drift entry is malformed: {entry!r}"
            )
        entry_change = entry.get("change")
        if not isinstance(entry_change, dict):
            entry_change = {}
        report.violations.extend(
            validate_identity(
                entry["address"],
                entry_change.get("before"),
                entry_change.get("after"),
                environment=environment,
                workspace_name=workspace_name,
                org_id=org_id,
                workspace_id=workspace_id,
                account_id=account_id,
            )
        )

        drift_type, _ = leaf_type_and_name(entry["address"])
        report.violations.extend(
            _check_relationship_targets(
                entry["address"],
                drift_type,
                entry_change,
                owned_by_side=owned_by_side,
                is_destructive=True,
                plan=plan,
                valid_addresses=valid_addresses,
            )
        )

    return report
