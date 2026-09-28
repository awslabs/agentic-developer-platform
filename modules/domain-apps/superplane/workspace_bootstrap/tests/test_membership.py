"""Immutable membership contract; identities do not depend on compute proximity."""

import json
from uuid import uuid4

import pytest
from superplane_bootstrap.errors import BootstrapRefused
from superplane_bootstrap.membership import SharedMembership


def binding():
    return SharedMembership.create(
        org_id=str(uuid4()),
        workspace_id=str(uuid4()),
        cluster_id=str(uuid4()),
        request_id=str(uuid4()),
        cluster_arn="arn:aws:eks:us-east-1:123456789012:cluster/shared",
        endpoint="https://shared.example",
    )


def test_membership_round_trip_preserves_namespace_and_generation():
    value = binding()
    assert SharedMembership.read(value.encode()) == value
    assert len(value.generation) == 64
    assert value.namespace == "sp-ws-" + value.workspace_id.replace("-", "")


@pytest.mark.parametrize(
    "field",
    [
        "generation",
        "workspace_id",
        "cluster_id",
        "org_id",
        "namespace",
        "request_id",
        "extra",
    ],
)
def test_changed_binding_without_matching_original_identity_is_refused(field):
    value = json.loads(binding().encode())
    value[field] = str(uuid4())
    with pytest.raises(BootstrapRefused):
        SharedMembership.read(json.dumps(value))
