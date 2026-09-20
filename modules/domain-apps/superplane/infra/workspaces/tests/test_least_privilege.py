"""Grants are scoped and placement is structural — Issue #5532 (w6-09), AC-01.

## Why these assertions are here and not in a .tftest.hcl file

Three separate reasons, and each is a genuine limit of `terraform test` rather than a
preference:

**1. Unknown-at-plan-time values.** `cluster_security.tftest.hcl` cannot assert that the node
group's subnets are disjoint from the public subnets in owned mode. Both id sets are
AWS-assigned, so Terraform marks them unknown and the condition ERRORS with "Unknown condition
value" — which aborts the run and skips every later run in the file, a worse outcome than a
missing check. Verified during development, twice. Here the same invariant is asserted one
level up: the node group's `subnet_ids` is `local.private_subnet_ids`, which is the expression
that makes public placement unreachable in BOTH modes regardless of what AWS assigns.

**2. Absence is unassertable from inside a plan.** Referencing a resource the configuration
does not declare is a configuration error, not a failed assertion. So "no ingress rule exists"
and "no policy uses Resource: *" cannot be stated in HCL — the same gap
`../../control-plane/tests/test_platform_isolation.py` documents.

**3. Policy documents are data, not resources.** `aws_iam_policy_document` is mocked in the
plan tests (it must be, or every role's `assume_role_policy` is unresolvable), so the plan
never sees the real statements. Asserting on the mocked value would assert on the mock. The
statements only exist as source text at that point, so source text is what gets parsed.

## The parsing approach, and its honest limit

These checks parse the HCL declarations structurally — brace-depth block extraction, then
attribute matching inside the block — rather than grepping the file. A grep for `"*"` would
flag `identifiers = ["eks.amazonaws.com"]` prose and the `eks:ListClusters` statement that
legitimately requires a star resource, and a check that fires wrongly is a check that gets
deleted.

The limit: this reasons about the source, so a grant constructed dynamically (a variable
interpolated into `resources`) is not inspectable here. That is acceptable because this module
constructs no policy dynamically, and `test_workspace_isolation.py`'s allowlist is what stops
a new policy-bearing resource type appearing unreviewed.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

WORKSPACES = Path(__file__).resolve().parents[1]


def _strip_comments(text: str) -> str:
    """Drop comment lines and heredoc bodies.

    iam.tf's prose discusses `Principal: "*"`, `Resource: "*"` and bare-account principals at
    length while explaining why none of them appears. Raw text would read the explanation as
    the violation.
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


def _blocks(kind: str, path: Path) -> list[tuple[str, str, str]]:
    """Return (type, name, body) for each top-level `kind "type" "name" {` block.

    Brace-depth delimited rather than regex-delimited: a policy document body contains nested
    `statement`, `principals` and `condition` blocks, and a non-greedy match to the first `}`
    would truncate at the first nested block — cutting off exactly the statements this suite
    inspects.
    """
    text = _strip_comments(path.read_text())
    found: list[tuple[str, str, str]] = []
    opener = re.compile(rf'^{kind}\s+"([^"]+)"\s+"([^"]+)"\s*\{{', re.MULTILINE)

    for match in opener.finditer(text):
        depth = 0
        i = match.end() - 1
        while i < len(text):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        found.append((match.group(1), match.group(2), text[match.end() : i]))

    return found


def _nested_blocks(body: str, name: str) -> list[str]:
    """Return the bodies of each `name { ... }` block nested inside `body`."""
    found: list[str] = []
    opener = re.compile(rf"^\s*{name}\s*\{{", re.MULTILINE)

    for match in opener.finditer(body):
        depth = 0
        i = match.end() - 1
        while i < len(body):
            if body[i] == "{":
                depth += 1
            elif body[i] == "}":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        found.append(body[match.end() : i])

    return found


def _tf_files() -> list[Path]:
    return sorted(WORKSPACES.glob("*.tf"))


POLICY_DOCUMENTS = [
    (path.name, name, body)
    for path in _tf_files()
    for t, name, body in _blocks("data", path)
    if t == "aws_iam_policy_document"
]

# Statements that legitimately require `resources = ["*"]`, each with the AWS API constraint
# that forces it. An exemption list rather than a blanket allowance: adding one requires
# naming the action and why it cannot be scoped, which is where the reasoning gets recorded.
#
# `eks:ListClusters` takes no resource — AWS defines it as account-level. Scoping it would
# produce a policy that silently grants nothing. It reveals cluster NAMES in the workspace
# account; it does not reveal endpoints or CA certificates, which is what the scoped
# DescribeCluster statement controls.
STAR_RESOURCE_EXEMPT_SIDS = frozenset({"ListClustersRequiresStarResource"})


def test_the_module_declares_policy_documents_and_roles() -> None:
    """Premise check: the parametrizations below must not collect zero cases.

    An empty parametrization is green, so a parser that stopped matching would turn this whole
    suite into a no-op that reads as enforcement. "No tests ran" and "tests passed" share an
    exit code.
    """
    assert len(POLICY_DOCUMENTS) >= 3, (
        f"found only {len(POLICY_DOCUMENTS)} policy documents, which suggests this suite's "
        f"parser has stopped matching. The module needs at least the cluster and node trust "
        f"policies and the workspace admin policy."
    )
    assert len(IAM_ROLES) >= 2, (
        f"found only {len(IAM_ROLES)} IAM roles; expected at least the cluster and node roles."
    )
    # The statement parser is the part most likely to silently return nothing, since it
    # depends on nested-block extraction rather than a top-level regex.
    total_statements = sum(
        len(_nested_blocks(body, "statement")) for _, _, body in POLICY_DOCUMENTS
    )
    assert total_statements >= 4, (
        f"found only {total_statements} policy statements across {len(POLICY_DOCUMENTS)} "
        f"documents, which means the nested-block parser has stopped matching."
    )


IAM_ROLES = [
    (path.name, name, body)
    for path in _tf_files()
    for t, name, body in _blocks("resource", path)
    if t == "aws_iam_role"
]


@pytest.mark.parametrize(
    ("file_name", "doc_name", "body"),
    POLICY_DOCUMENTS,
    ids=[f"{n}" for _, n, _ in POLICY_DOCUMENTS],
)
def test_no_policy_statement_grants_star_resource_unless_aws_requires_it(
    file_name: str, doc_name: str, body: str
) -> None:
    """A `Resource: "*"` grant in a workspace is a grant over every resource in the account."""
    for statement in _nested_blocks(body, "statement"):
        sid_match = re.search(r'^\s*sid\s*=\s*"([^"]+)"', statement, re.MULTILINE)
        sid = sid_match.group(1) if sid_match else "<no sid>"

        resources_match = re.search(
            r"^\s*resources\s*=\s*\[(.*?)\]", statement, re.MULTILINE | re.DOTALL
        )
        if not resources_match:
            continue

        resources = resources_match.group(1)
        if '"*"' not in resources:
            continue

        assert sid in STAR_RESOURCE_EXEMPT_SIDS, (
            f'{file_name}: statement "{sid}" in data.aws_iam_policy_document.{doc_name} '
            f'grants `resources = ["*"]`.\n\n'
            f"In a workspace account that is every resource in the account, including the "
            f"tenant's own. Scope it to this workspace's ARNs — the name prefix and "
            f"local.cluster_name are available for exactly this.\n\n"
            f"If the AWS API genuinely does not accept a resource for these actions, add the "
            f"sid to STAR_RESOURCE_EXEMPT_SIDS in this file with the API constraint that "
            f"forces it, and split it into its own statement so the scope of the exemption is "
            f"visible."
        )


@pytest.mark.parametrize(
    ("file_name", "doc_name", "body"),
    POLICY_DOCUMENTS,
    ids=[f"{n}" for _, n, _ in POLICY_DOCUMENTS],
)
def test_no_trust_policy_admits_a_wildcard_or_bare_account_principal(
    file_name: str, doc_name: str, body: str
) -> None:
    """A bare-account principal is assumable by every principal in that account.

    The wildcard case is obvious. The bare-account case is the one that looks ordinary and is
    nearly as bad: `arn:aws:iam::<account>:root` in a trust policy means any principal in that
    account may assume the role — which in a workspace account includes the tenant's own roles
    and every role created there in future.
    """
    for statement in _nested_blocks(body, "statement"):
        for principals in _nested_blocks(statement, "principals"):
            identifiers_match = re.search(
                r"^\s*identifiers\s*=\s*\[(.*?)\]", principals, re.MULTILINE | re.DOTALL
            )
            if not identifiers_match:
                continue

            identifiers = identifiers_match.group(1)

            assert '"*"' not in identifiers, (
                f"{file_name}: data.aws_iam_policy_document.{doc_name} has a trust policy "
                f'principal of `"*"`, which lets anyone assume the role.'
            )

            assert not re.search(
                r'"arn:aws[a-z-]*:iam::[0-9]{12}:root"', identifiers
            ), (
                f"{file_name}: data.aws_iam_policy_document.{doc_name} names an account root "
                f"principal. That makes the role assumable by EVERY principal in the account, "
                f"including the tenant's own roles. Name the specific operator or "
                f"control-plane role instead. (var.workspace_admin_principal_arns refuses "
                f"`:root` for the same reason — this asserts the policy itself never "
                f"hardcodes one.)"
            )


@pytest.mark.parametrize(
    ("file_name", "role_name", "body"),
    IAM_ROLES,
    ids=[n for _, n, _ in IAM_ROLES],
)
def test_every_role_name_is_scoped_to_this_workspace(
    file_name: str, role_name: str, body: str
) -> None:
    """A role name without the workspace prefix collides across workspaces.

    Two workspaces in one account whose roles share a name do not produce an error the second
    time: the second apply ADOPTS or overwrites the first's role, so a change to one
    workspace's permissions silently changes another's.
    """
    name_match = re.search(r"^\s*name\s*=\s*(.+)$", body, re.MULTILINE)
    assert name_match, f"{file_name}: aws_iam_role.{role_name} declares no name."

    assert "local.name_prefix" in name_match.group(1), (
        f"{file_name}: aws_iam_role.{role_name}'s name does not derive from "
        f"`local.name_prefix`, so it does not carry the environment and workspace. Two "
        f"workspaces would then collide on it — and the second apply adopts the first's role "
        f"rather than failing, so a permission change to one workspace silently changes "
        f"another's. The cross-variable length budget in variables.tf assumes this prefix."
    )


def test_node_group_subnets_are_the_private_subnet_expression() -> None:
    """Node placement must be structurally private, in both networking modes.

    This is the assertion `cluster_security.tftest.hcl` cannot make in owned mode, because the
    subnet ids are AWS-assigned and therefore unknown at plan time (verified: Terraform errors
    with "Unknown condition value" and skips the rest of the file). Asserted here one level up,
    on the expression rather than on the values: `local.private_subnet_ids` resolves to this
    module's private subnets in owned mode and to the supplied private subnets in supplied
    mode, so there is no input that places a node in a public subnet.
    """
    eks_tf = WORKSPACES / "eks.tf"
    node_groups = [
        (name, body)
        for t, name, body in _blocks("resource", eks_tf)
        if t == "aws_eks_node_group"
    ]

    assert node_groups, (
        "eks.tf declares no node group: this suite has nothing to check."
    )

    for name, body in node_groups:
        subnet_match = re.search(r"^\s*subnet_ids\s*=\s*(.+)$", body, re.MULTILINE)
        assert subnet_match, f"aws_eks_node_group.{name} declares no subnet_ids."

        assert subnet_match.group(1).strip() == "local.private_subnet_ids", (
            f"aws_eks_node_group.{name}'s subnet_ids is "
            f"`{subnet_match.group(1).strip()}`, not `local.private_subnet_ids`.\n\n"
            f"Workspace capacity is not internet-reachable, and that must hold structurally "
            f"rather than by the caller passing the right list: local.private_subnet_ids is "
            f"the single expression that resolves to private subnets in BOTH networking "
            f"modes. Referencing aws_subnet.private directly breaks supplied mode; "
            f"referencing a variable lets a caller pass a public subnet."
        )


def test_no_ingress_rule_opens_the_cluster_security_group() -> None:
    """Absence, which a plan cannot assert.

    The cluster security group is egress-only: nothing reaches the cluster's network
    interfaces from outside the VPC except through the API endpoint, whose exposure
    var.cluster_endpoint_public_access controls and whose allowlist refuses 0.0.0.0/0. An
    ingress rule here would quietly undo that bound — the endpoint control would still read as
    enforcement while traffic arrived by another path.
    """
    for path in _tf_files():
        for t, name, _ in _blocks("resource", path):
            assert t not in (
                "aws_vpc_security_group_ingress_rule",
                "aws_security_group_rule",
            ), (
                f"{path.name} declares `{t}.{name}`. The workspace cluster security group is "
                f"deliberately egress-only: an ingress rule would admit traffic from outside "
                f"the VPC while var.cluster_endpoint_public_access still read as the control "
                f"bounding exposure. If ingress is genuinely required, say which source and "
                f"why, and extend cluster_security.tftest.hcl to pin the bound."
            )


def test_cni_permissions_and_addon_are_bound_to_dedicated_workload_role():
    iam_resources = {
        (t, name): body for t, name, body in _blocks("resource", WORKSPACES / "iam.tf")
    }
    attachment = iam_resources[("aws_iam_role_policy_attachment", "vpc_cni")]
    assert re.search(r"role\s*=\s*aws_iam_role\.vpc_cni\.name", attachment)
    assert "AmazonEKS_CNI_Policy" in attachment
    for (kind, name), body in iam_resources.items():
        if kind == "aws_iam_role_policy_attachment" and name != "vpc_cni":
            assert "AmazonEKS_CNI_Policy" not in body
        assert "AmazonEC2ContainerRegistryReadOnly" not in body
    addon = next(
        body
        for kind, name, body in _blocks("resource", WORKSPACES / "node_network.tf")
        if kind == "aws_eks_addon"
    )
    assert re.search(
        r"service_account_role_arn\s*=\s*aws_iam_role\.vpc_cni\.arn", addon
    )
    node = next(
        body
        for kind, name, body in _blocks("resource", WORKSPACES / "eks.tf")
        if kind == "aws_eks_node_group"
    )
    assert "aws_eks_addon.vpc_cni" in node
