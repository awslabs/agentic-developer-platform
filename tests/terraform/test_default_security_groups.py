"""CKV2_AWS_12 — every owned VPC adopts its default security group with deny-all.

Covers the three original selectors from the S20/S21 security scan:
  - checkov|original/checkov/results_sarif.sarif|ri=336  (cyber vpc.tf:16)
  - checkov|original/checkov/results_sarif.sarif|ri=337  (superplane network.tf:50)
  - checkov|original/checkov/results_sarif.sarif|ri=338  (platform networking main.tf:7)

Each test reads the Terraform source as text and asserts that the
aws_default_security_group resource exists, references the correct VPC,
and (for superplane) carries the owns_network gate so supplied-mode
workspaces never adopt a customer's default security group.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# ---------------------------------------------------------------------------
# The three scoped files
# ---------------------------------------------------------------------------
CYBER_VPC = REPO_ROOT / "modules" / "domain-apps" / "cyber" / "infra" / "vpc.tf"
SUPERPLANE_NET = (
    REPO_ROOT
    / "modules"
    / "domain-apps"
    / "superplane"
    / "infra"
    / "workspaces"
    / "network.tf"
)
PLATFORM_NET = (
    REPO_ROOT / "platform" / "infra" / "modules" / "networking" / "main.tf"
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
_RESOURCE_RE = re.compile(
    r'^resource\s+"aws_default_security_group"\s+"(\w+)"\s*\{',
    re.MULTILINE,
)

_GATE_RE = re.compile(
    r"^\s*count\s*=.*owns_network",
    re.MULTILINE,
)


def _resource_body(text: str, resource_name: str) -> str | None:
    """Extract the body of an aws_default_security_group resource by name."""
    for match in _RESOURCE_RE.finditer(text):
        if match.group(1) != resource_name:
            continue
        depth = 0
        i = match.end() - 1
        while i < len(text):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    return text[match.end() : i]
            i += 1
    return None


def _has_ingress_or_egress(body: str) -> bool:
    """Return True if the resource body declares any ingress or egress block."""
    return bool(re.search(r"^\s*(ingress|egress)\s*\{", body, re.MULTILINE))


# ---------------------------------------------------------------------------
# Tests — selector ri=336: cyber/infra/vpc.tf
# ---------------------------------------------------------------------------
class TestCyberVpc:
    """CKV2_AWS_12 at modules/domain-apps/cyber/infra/vpc.tf."""

    def test_file_exists(self) -> None:
        assert CYBER_VPC.exists(), f"{CYBER_VPC} is missing"

    def test_default_sg_resource_exists(self) -> None:
        text = CYBER_VPC.read_text()
        body = _resource_body(text, "threat_research")
        assert body is not None, (
            "vpc.tf must declare `resource \"aws_default_security_group\" "
            "\"threat_research\"` to adopt the default group with deny-all."
        )

    def test_references_correct_vpc(self) -> None:
        text = CYBER_VPC.read_text()
        body = _resource_body(text, "threat_research")
        assert body is not None
        assert "aws_vpc.threat_research.id" in body, (
            "The default security group must reference aws_vpc.threat_research.id"
        )

    def test_deny_all(self) -> None:
        text = CYBER_VPC.read_text()
        body = _resource_body(text, "threat_research")
        assert body is not None
        assert not _has_ingress_or_egress(body), (
            "The default security group must have no ingress or egress blocks (deny-all)."
        )


# ---------------------------------------------------------------------------
# Tests — selector ri=337: superplane/infra/workspaces/network.tf
# ---------------------------------------------------------------------------
class TestSuperplaneWorkspace:
    """CKV2_AWS_12 at modules/domain-apps/superplane/infra/workspaces/network.tf."""

    def test_file_exists(self) -> None:
        assert SUPERPLANE_NET.exists(), f"{SUPERPLANE_NET} is missing"

    def test_default_sg_resource_exists(self) -> None:
        text = SUPERPLANE_NET.read_text()
        body = _resource_body(text, "workspace")
        assert body is not None, (
            "network.tf must declare `resource \"aws_default_security_group\" "
            "\"workspace\"` to adopt the default group with deny-all."
        )

    def test_references_correct_vpc(self) -> None:
        text = SUPERPLANE_NET.read_text()
        body = _resource_body(text, "workspace")
        assert body is not None
        assert "aws_vpc.workspace[0].id" in body, (
            "The default security group must reference aws_vpc.workspace[0].id"
        )

    def test_gated_on_owns_network(self) -> None:
        """Supplied mode must NOT adopt the customer's default security group."""
        text = SUPERPLANE_NET.read_text()
        body = _resource_body(text, "workspace")
        assert body is not None
        assert _GATE_RE.search(body), (
            "The aws_default_security_group must be gated on local.owns_network. "
            "In supplied mode, adopting the customer's default security group "
            "would revoke rules their other workloads depend on."
        )

    def test_deny_all(self) -> None:
        text = SUPERPLANE_NET.read_text()
        body = _resource_body(text, "workspace")
        assert body is not None
        assert not _has_ingress_or_egress(body), (
            "The default security group must have no ingress or egress blocks (deny-all)."
        )


# ---------------------------------------------------------------------------
# Tests — selector ri=338: platform/infra/modules/networking/main.tf
# ---------------------------------------------------------------------------
class TestPlatformNetworking:
    """CKV2_AWS_12 at platform/infra/modules/networking/main.tf."""

    def test_file_exists(self) -> None:
        assert PLATFORM_NET.exists(), f"{PLATFORM_NET} is missing"

    def test_default_sg_resource_exists(self) -> None:
        text = PLATFORM_NET.read_text()
        body = _resource_body(text, "main")
        assert body is not None, (
            "main.tf must declare `resource \"aws_default_security_group\" "
            "\"main\"` to adopt the default group with deny-all."
        )

    def test_references_correct_vpc(self) -> None:
        text = PLATFORM_NET.read_text()
        body = _resource_body(text, "main")
        assert body is not None
        assert "aws_vpc.main.id" in body, (
            "The default security group must reference aws_vpc.main.id"
        )

    def test_deny_all(self) -> None:
        text = PLATFORM_NET.read_text()
        body = _resource_body(text, "main")
        assert body is not None
        assert not _has_ingress_or_egress(body), (
            "The default security group must have no ingress or egress blocks (deny-all)."
        )
