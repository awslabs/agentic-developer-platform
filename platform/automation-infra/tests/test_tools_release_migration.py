"""Check all schedules allowed by the declared IAM migration dependency graph."""
import itertools
import re
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "tools-deployment.tf"
ATTACH = "aws_iam_role_policy_attachment.tools_deployment_resource_ceiling"
GRANTS = "aws_iam_role_policy.tools_deployment"
FLOOR = "aws_iam_role_policy.tools_deployment_ceiling"


def resource_body(source, address):
    kind, name = address.split(".")
    start = source.index(f'resource "{kind}" "{name}" {{')
    end = source.find('\nresource "', start + 1)
    return source[start:] if end < 0 else source[start:end]


def schedules(source):
    edges = {}
    for address in (ATTACH, GRANTS, FLOOR):
        match = re.search(r"depends_on\s*=\s*\[([^]]*)\]", resource_body(source, address))
        edges[address] = set(re.findall(r"aws_iam_[a-z_]+\.[a-z_]+", match[1])) if match else set()
    return [order for order in itertools.permutations(edges)
            if all(not (edges[step] & set(order[index + 1:])) for index, step in enumerate(order))]


def test_legacy_inline_quota_and_denials_are_safe_in_every_permitted_order():
    source = SOURCE.read_text()
    orders = schedules(source)
    assert orders
    # An existing combined document fits alone but cannot coexist with the new
    # standalone ceiling. This fixture tests the AWS aggregate quota, not a
    # particular deployment's inventory. Rendered-document quotas are checked
    # separately by the mocked Terraform tests, including the duplicate denial.
    for order in orders:
        managed_attached = False
        inline_grants, inline_floor = 10140, 0
        for operation in order:
            if operation == ATTACH:
                managed_attached = True
            elif operation == GRANTS:
                assert managed_attached, "Resource denials must attach before legacy grants are replaced"
                inline_grants = 7000
            else:
                inline_floor = 1250
            assert inline_grants + inline_floor <= 10240, "Standalone ceiling raced the legacy inline replacement"
    assert orders == [(ATTACH, GRANTS, FLOOR)]


def test_adapter_fences_precede_resource_and_action_ceiling_exceptions():
    source = SOURCE.read_text() + "\n" + SOURCE.with_name("tools-adapters-deployment.tf").read_text()
    adapter_policy = "aws_iam_policy.tools_adapters_publication"
    adapter_attach = "aws_iam_role_policy_attachment.tools_adapters_publication"
    check_policy = "aws_iam_policy.tools_adapters_foundation_check"
    check_attach = "aws_iam_role_policy_attachment.tools_adapters_foundation_check"
    resource_ceiling = "aws_iam_policy.tools_deployment_resource_ceiling"
    nodes = (check_policy, check_attach, adapter_policy, adapter_attach, resource_ceiling, ATTACH, GRANTS, FLOOR)
    # Include implicit policy_arn dependencies as well as depends_on. Check all
    # permitted schedules; Terraform may run any independent updates together.
    edges = {
        node: set(re.findall(r"aws_iam_[a-z_]+\.[a-z_]+", resource_body(source, node))) & set(nodes) - {node}
        for node in nodes
    }
    permitted = [order for order in itertools.permutations(nodes)
                 if all(not (edges[step] & set(order[index + 1:])) for index, step in enumerate(order))]
    assert permitted, "Publication dependencies must not form a cycle"
    for order in permitted:
        installed = set()
        for operation in order:
            if operation == adapter_policy:
                assert check_attach in installed, "Foundation check policy must attach before opening its resource exceptions"
            if operation in (resource_ceiling, GRANTS, FLOOR):
                assert adapter_attach in installed, "New access opened before its explicit resource fences"
            installed.add(operation)
        # On removal Terraform reverses dependencies: remove exceptions before
        # dropping the optional managed policy and its explicit denials.
        removed = set()
        for operation in reversed(order):
            if operation == adapter_attach:
                assert {resource_ceiling, GRANTS, FLOOR} <= removed
            removed.add(operation)
