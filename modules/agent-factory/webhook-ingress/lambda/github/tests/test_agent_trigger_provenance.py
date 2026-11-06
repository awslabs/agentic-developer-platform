"""Shared tenant lookups used by GitHub ingress stay closed after retirement."""

from unittest.mock import patch

import pytest

from agent_trigger import _repo_in_tenant


@pytest.mark.parametrize(
    "repo,tenant,chain_repo,expected",
    [
        ("org/repo", "tenant-id", "org/repo", True),
        ("org/sibling", "org", "org/repo", True),
        ("foreign/repo", "org", "org/repo", False),
        ("", "org", "org/repo", False),
        ("unqualified", "org", "org/repo", False),
    ],
)
def test_shared_repo_tenant_lookup(repo, tenant, chain_repo, expected):
    with patch("common.installation_resolver.resolve_installation_for_tenant", return_value=None):
        assert _repo_in_tenant(repo, tenant, {"repo": chain_repo}) is expected


@pytest.mark.parametrize(
    "installations,expected", [([11, 11], True), ([11, 12], False), ([None], False)]
)
def test_owner_must_resolve_to_the_same_installation(installations, expected):
    with patch(
        "common.installation_resolver.resolve_installation_for_tenant", side_effect=installations
    ):
        assert _repo_in_tenant("owner/repo", "tenant", {}) is expected


def test_shared_lookup_outage_denies_the_repository():
    with patch(
        "common.installation_resolver.resolve_installation_for_tenant",
        side_effect=OSError("unavailable"),
    ):
        assert not _repo_in_tenant("owner/repo", "tenant", {})
