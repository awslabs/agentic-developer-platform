"""Every network resource is gated on owned mode — Issue #5532 (w6-09), AC-01.

## What this suite adds that the plan test does not

`tests/supplied_networking.tftest.hcl` plans the supplied mode and asserts that the count of
each network resource is zero. That is the direct proof, and it is the stronger one for the
resources it names. Its weakness is enumeration: it can only assert about resources somebody
remembered to list. A ninth network resource added to `network.tf` next quarter — an
`aws_egress_only_internet_gateway`, a second NAT gateway, a VPC peering connection — would be
adopted into every supplied-mode workspace's state, and that plan test would still pass.

So this suite asserts the RULE rather than the instances: every resource declaration in
`network.tf` whose type can damage a network carries the `local.owns_network` gate. A new
resource without the gate fails here, by type category, without anyone having extended a list
of names.

## Verified during development, and the reason this file exists in this shape

The gate was removed from `aws_nat_gateway` deliberately, to check the plan test would catch
it. It did — but it failed with `Error: Invalid index` from the route table's reference to
`aws_nat_gateway.workspace[0]`, not with any of the assertion messages written for exactly
this case. The cause is cascade: an ungated NAT gateway in supplied mode has no public subnet
to sit in, so the configuration errors out before the assertions evaluate. A future author
seeing "Invalid index" has no reason to connect it to network ownership.

That is what an unnamed failure costs. This suite names it.

## What it deliberately does NOT check

Whether the gate's expression is *correct* — that `local.owns_network ? 1 : 0` and not
`local.owns_network ? 0 : 1`. Text cannot tell those apart usefully, and the plan test
already distinguishes them: it asserts non-zero counts in owned mode and zero in supplied.
The two halves are complementary, and neither alone is sufficient.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

WORKSPACES = Path(__file__).resolve().parents[1]
NETWORK_TF = WORKSPACES / "network.tf"

# Resource types whose adoption into a supplied network's state would damage a network ADP
# does not own. Each entry names the damage, because the error message is the whole product
# of this suite: a failure that does not explain itself gets suppressed rather than fixed.
#
# This is a category list, not an instance list — that distinction is the point (see the
# docstring). Anything matching one of these types must carry the gate, whether or not it
# exists today.
NETWORK_OWNING_TYPES = {
    "aws_vpc": "the supplier's VPC",
    "aws_subnet": "the supplier's subnets",
    "aws_internet_gateway": "the supplier's internet gateway",
    "aws_egress_only_internet_gateway": "the supplier's IPv6 egress gateway",
    "aws_nat_gateway": "the supplier's egress path, breaking every other workload in their VPC",
    "aws_eip": "an Elastic IP that may be referenced elsewhere in the supplier's account",
    "aws_route_table": "the supplier's routing, redirecting traffic that is not this workspace's",
    "aws_route": "a route in the supplier's route table",
    "aws_route_table_association": "the supplier's subnet routing",
    "aws_vpc_endpoint": "a VPC endpoint other workloads in the supplier's VPC may use",
    "aws_vpc_peering_connection": "a peering connection joining two networks ADP does not own",
    "aws_vpn_gateway": "the supplier's VPN attachment",
    "aws_transit_gateway_vpc_attachment": "the supplier's transit gateway attachment",
    "aws_default_route_table": "the supplier's default route table",
    "aws_default_security_group": "the supplier's default security group",
    "aws_network_acl": "the supplier's network ACLs",
    "aws_flow_log": "a flow log whose destination the supplier owns",
}

# The gate. Any of these forms counts: what matters is that the declaration's count or
# for_each is conditioned on the networking mode, not the exact spelling.
GATE_PATTERN = re.compile(r"^\s*(?:count|for_each)\s*=.*owns_network", re.MULTILINE)


def _strip_comments(text: str) -> str:
    """Drop comment lines and heredoc bodies.

    `network.tf`'s prose names every type in NETWORK_OWNING_TYPES while explaining why the
    gate exists and which alternatives were rejected. Raw text would read the explanation as
    the violation — the same reason ../../control-plane/tests/test_platform_isolation.py
    strips before matching.
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


def _resource_blocks(path: Path) -> list[tuple[str, str, str]]:
    """Return (type, name, body) for each top-level resource block in a .tf file.

    Bodies are delimited by brace depth rather than by a regex, because a resource body
    contains nested blocks (`tags`, `route`, `lifecycle`) and a non-greedy match to the first
    `}` would truncate at the first nested block — cutting off the very `count` line this
    suite looks for in any resource whose count appears after a nested block.
    """
    text = _strip_comments(path.read_text())
    blocks: list[tuple[str, str, str]] = []
    opener = re.compile(r'^resource\s+"([^"]+)"\s+"([^"]+)"\s*\{', re.MULTILINE)

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
        blocks.append((match.group(1), match.group(2), text[match.end() : i]))

    return blocks


NETWORK_RESOURCES = [
    (t, n, body)
    for path in sorted(WORKSPACES.glob("*.tf"))
    for t, n, body in _resource_blocks(path)
    if t in NETWORK_OWNING_TYPES
]


def test_network_tf_exists_and_declares_network_resources() -> None:
    """Premise check: the parametrization below must not be empty.

    Without this, a renamed file or a parser that stopped matching would make every
    assertion in this suite pass by collecting zero cases — "no tests ran" and "tests
    passed" are the same exit code, which the domain CI lane calls out explicitly.
    """
    assert NETWORK_TF.exists(), (
        f"{NETWORK_TF} is missing: this suite has nothing to check."
    )
    assert len(NETWORK_RESOURCES) >= 7, (
        f"found only {len(NETWORK_RESOURCES)} network resource declarations in "
        f"network.tf, which suggests this suite's parser has stopped matching rather than "
        f"that the module stopped creating networks. Owned mode needs at least a VPC, "
        f"subnets, an IGW, an EIP, a NAT gateway and route tables."
    )


@pytest.mark.parametrize(
    ("resource_type", "resource_name", "body"),
    NETWORK_RESOURCES,
    ids=[f"{t}.{n}" for t, n, _ in NETWORK_RESOURCES],
)
def test_every_network_resource_is_gated_on_owned_mode(
    resource_type: str, resource_name: str, body: str
) -> None:
    """A network resource without the mode gate is adopted into every supplied-mode state."""
    if GATE_PATTERN.search(body):
        return

    damage = NETWORK_OWNING_TYPES[resource_type]
    raise AssertionError(
        f'network.tf declares `resource "{resource_type}" "{resource_name}"` with no '
        f"`count`/`for_each` conditioned on `local.owns_network`. In supplied networking "
        f"mode this resource would be created in — and on destroy would delete — "
        f"{damage}.\n\n"
        f"Design item 3 requires supporting supplied networking WITHOUT silently adopting "
        f"its lifecycle, and the gate is the mechanism: a resource absent from state cannot "
        f"be destroyed, whatever an operator types. Add:\n\n"
        f"    count = local.owns_network ? 1 : 0\n\n"
        f"or, if this resource genuinely must exist in both modes, say why in a comment and "
        f"add its type to this suite's exemptions with that reasoning."
    )


def test_supplied_vpc_is_read_through_a_data_source_not_a_resource() -> None:
    """The supplied VPC must be READ. A data source cannot be destroyed by a destroy."""
    text = _strip_comments(NETWORK_TF.read_text())

    assert re.search(r'^data\s+"aws_vpc"\s+"supplied"\s*\{', text, re.MULTILINE), (
        'network.tf must read the supplied VPC through `data "aws_vpc" "supplied"`. '
        "Reading requires no write permission and creates no state entry a destroy can act "
        "on, which is what distinguishes reading a network from adopting it."
    )

    assert not re.search(r'^resource\s+"aws_vpc"\s+"supplied"', text, re.MULTILINE), (
        "The supplied VPC must never be declared as a `resource`: that is the adoption "
        "design item 3 forbids. A workspace destroy would then delete a network its owner "
        "merely lent to ADP."
    )


def test_no_import_block_adopts_a_supplied_network() -> None:
    """`import` blocks are the other route into adoption, and they bypass the count gate.

    `import { to = aws_vpc.workspace[0] ... }` moves an existing VPC into this module's
    state. The gate does not help: the resource is now managed, and `terraform destroy`
    deletes it. network.tf records this as a rejected alternative; this asserts nobody
    reinstates it.
    """
    for tf_file in sorted(WORKSPACES.glob("*.tf")):
        text = _strip_comments(tf_file.read_text())
        matches = re.findall(r"^import\s*\{", text, re.MULTILINE)
        assert not matches, (
            f"{tf_file.name} contains an `import` block. Importing a supplied network into "
            f"this module's state is exactly the silent lifecycle adoption design item 3 "
            f"forbids — and it defeats the owned-mode count gate, because an imported "
            f"resource is a managed resource. See the rejected alternatives in network.tf."
        )
