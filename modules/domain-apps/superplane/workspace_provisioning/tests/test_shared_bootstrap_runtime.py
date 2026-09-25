"""Shared dispatch cannot enter the dedicated actor/cluster bootstrap recipe."""

import json
from types import SimpleNamespace

import pytest
from superplane_bootstrap.membership import SharedMembership

from workspace_provisioning.bootstrap_runtime import bootstrap
from workspace_provisioning.runtime_config import LifecycleRefused


def test_approved_shared_request_requires_protected_composition_before_actor_assumption(
    monkeypatch,
):
    member = SharedMembership.create(
        org_id="11111111-1111-4111-8111-111111111111",
        workspace_id="22222222-2222-4222-8222-222222222222",
        cluster_id="33333333-3333-4333-8333-333333333333",
        request_id="44444444-4444-4444-8444-444444444444",
        cluster_arn="arn:aws:eks:us-east-1:123456789012:cluster/shared",
        endpoint="https://shared.example.test",
    )
    operation = SimpleNamespace(
        grant=SimpleNamespace(
            lease=SimpleNamespace(
                org_id=member.org_id, workspace_id=member.workspace_id
            )
        ),
        request=SimpleNamespace(
            parameters={
                "shared_membership": member.encode(),
                "lifecycle_inputs": json.dumps(
                    {
                        "cluster_placement": "shared",
                        "shared_cluster_id": member.cluster_id,
                    }
                ),
                "lifecycle_request": json.dumps(
                    {
                        "workspace_id": member.workspace_id,
                        "mode": "bring-existing-cluster",
                        "target_account_id": "123456789012",
                        "region": "us-east-1",
                        "existing_cluster_name": "shared",
                    }
                ),
            }
        ),
    )

    def forbidden(*args, **kwargs):
        pytest.fail("shared request reached dedicated actor assumption")

    monkeypatch.setattr(
        "workspace_provisioning.bootstrap_runtime.assume_session", forbidden
    )
    with pytest.raises(LifecycleRefused, match="installed runtime composition"):
        bootstrap(operation, None, {}, None, None, None, None, None, forbidden, None)
