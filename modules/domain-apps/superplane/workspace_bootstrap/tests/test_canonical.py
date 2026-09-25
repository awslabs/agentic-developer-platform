"""Offline coverage for `canonical.py`'s pure-Python pieces — issue #6048.

`canonical.publish` itself issues raw SQL against tables no scripted double
models faithfully (the point `test_registry_postgres.py`'s own docstring makes
about F11), so its behavior is proved against real PostgreSQL in
`test_canonical_shared_postgres.py`. What belongs here, offline, is everything
that does not need a database: the placement default/validation at the top of
`publish`, and the deterministic membership-generation fingerprint.
"""

from __future__ import annotations

from typing import ClassVar

import pytest
from superplane_bootstrap.canonical import _membership_generation
from superplane_bootstrap.errors import BootstrapRefused
from superplane_bootstrap.registration import WorkspaceTarget


class _RaisingStore:
    """A store whose every method call is itself the test failure.

    `publish` refuses `cluster_placement` before issuing any SQL, so a call that
    reaches this store proves the validation did NOT run early enough.
    """

    def execute(self, *_args, **_kwargs):
        raise AssertionError("publish issued SQL before validating cluster_placement")


def test_unknown_cluster_placement_is_refused_before_any_sql():
    from superplane_bootstrap.canonical import publish

    with pytest.raises(BootstrapRefused, match="unknown cluster_placement"):
        publish(
            _RaisingStore(),
            {
                "workspace_id": "22222222-2222-4222-8222-222222222222",
                "org_id": "org",
                "cluster_arn": "arn:aws:eks:us-east-1:000000000000:cluster/x",
                "namespace": "ns",
                "endpoint": "https://example.invalid",
                "cluster_placement": "borrowed",
            },
        )


def test_membership_generation_is_deterministic_and_64_hex():
    import re

    identity = {"workspace_id": "a", "namespace": "b", "cluster_arn": "c"}
    first = _membership_generation(identity)
    second = _membership_generation(identity)

    assert first == second
    assert re.fullmatch(r"[a-f0-9]{64}", first)


def test_membership_generation_changes_with_the_identity():
    a = _membership_generation({"workspace_id": "a", "namespace": "b"})
    b = _membership_generation({"workspace_id": "a", "namespace": "different"})

    assert a != b


def test_membership_generation_is_order_independent():
    """Key order in the input dict must not change the fingerprint — the
    generation is compared across a replay that may rebuild the dict differently."""
    a = _membership_generation({"workspace_id": "a", "namespace": "b"})
    b = _membership_generation({"namespace": "b", "workspace_id": "a"})

    assert a == b


class TestWorkspaceTargetClusterPlacement:
    """Issue #6048: `WorkspaceTarget` carries the placement, defaulting safely."""

    _base: ClassVar[dict] = {
        "workspace_id": "22222222-2222-4222-8222-222222222222",
        "org_id": "org",
        "account_id": "000000000000",
        "region": "us-east-1",
        "cluster_name": "cluster",
        "cluster_arn": "arn:aws:eks:us-east-1:000000000000:cluster/x",
        "endpoint": "https://example.invalid",
        "namespace": "ns",
        "namespace_uid": "uid",
        "cluster_ownership": "adp-created",
        "credential_reference_id": "cred",
        "contract_version": "v1",
    }

    def test_defaults_to_dedicated(self):
        """Every caller built before this field existed keeps constructing the
        exact same record — the property that makes this an additive change."""
        target = WorkspaceTarget(**self._base)
        assert target.cluster_placement == "dedicated"

    def test_shared_placement_can_be_named_explicitly(self):
        target = WorkspaceTarget(**self._base, cluster_placement="shared")
        assert target.cluster_placement == "shared"

    def test_cluster_placement_is_part_of_the_immutable_identity(self):
        dedicated = WorkspaceTarget(**self._base, cluster_placement="dedicated")
        shared = WorkspaceTarget(**self._base, cluster_placement="shared")
        assert dedicated.immutable_identity != shared.immutable_identity

    def test_dedicated_registration_preserves_historical_document_shape(self):
        from superplane_bootstrap.registry import _target_mapping

        assert _target_mapping(WorkspaceTarget(**self._base)) == self._base
