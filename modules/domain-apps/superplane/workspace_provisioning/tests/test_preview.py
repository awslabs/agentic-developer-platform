"""A preview binds real mode validation, tenant scope and the displayed decision."""

from dataclasses import replace

import pytest

from account_factory.modes import (
    AccountFactoryRequest,
    ModeError,
    OwnershipMode,
    ValidationAuthorization,
)
from workspace_provisioning.preview import preview_workspace


def request_for(mode):
    fields = {
        "mode": mode,
        "organization_id": "o-testorg1234",
        "management_account_id": "000000000001",
        "management_cluster": "fixture-management",
        "workspace_id": "ws-fixture",
        "region": "us-west-2",
    }
    if mode.creates_cluster:
        fields.update(
            vpc_cidr="10.64.0.0/16",
            availability_zones=("us-west-2a", "us-west-2b"),
            cluster_version="1.31",
        )
    else:
        fields["existing_cluster_name"] = "fixture-adopted"
    if mode.creates_account:
        fields.update(
            account_email="fixture@example.invalid",
            organizational_unit_id="ou-test-fixture01",
        )
    else:
        fields["target_account_id"] = "000000000002"
    return AccountFactoryRequest(**fields)


def authority():
    return ValidationAuthorization(
        organization_id="o-testorg1234",
        management_account_id="000000000001",
        management_cluster="fixture-management",
        workspace_id="ws-fixture",
        permitted_modes=frozenset(OwnershipMode),
        permitted_target_accounts=frozenset({"000000000002"}),
        permitted_organizational_units=frozenset({"ou-test-fixture01"}),
    )


def preview(request, **overrides):
    options = dict(
        authorization=authority(),
        requested_capacity={"min_nodes": 0, "max_nodes": 2},
        cost_estimate=None,
        approval_required=True,
    )
    options.update(overrides)
    return preview_workspace(request, **options)


@pytest.mark.parametrize("mode", OwnershipMode)
def test_mode_ownership_and_unknown_price_are_preserved(mode):
    request = request_for(mode)
    result = preview(request).as_dict()
    assert result["target"]["account"] == request.target_account_id
    assert result["ownership"]["cluster"] == request.cluster_ownership.value
    assert result["target"]["cluster"] == request.cluster_name
    assert result["cost_estimate"] is None
    assert "operation_id" not in result


def test_existing_cluster_mode_cannot_hide_managed_cluster_inputs():
    request = replace(
        request_for(OwnershipMode.BRING_EXISTING_CLUSTER), cluster_version="1.31"
    )
    with pytest.raises(ModeError, match="cluster_version must be absent"):
        preview(request)


@pytest.mark.parametrize(
    "changes",
    [
        {"workspace_id": "other-workspace"},
        {"permitted_target_accounts": frozenset()},
        {"management_account_id": None},
    ],
)
def test_unscoped_or_other_tenant_preview_is_refused(changes):
    with pytest.raises(ModeError):
        preview(
            request_for(OwnershipMode.BRING_EXISTING_CLUSTER),
            authorization=replace(authority(), **changes),
        )


def test_revision_changes_with_policy_capacity_target_or_pricing():
    request = request_for(OwnershipMode.BRING_EXISTING_CLUSTER)
    original = preview(request)
    assert preview(request).revision == original.revision
    assert (
        preview(request, requested_capacity={"max_nodes": 2, "min_nodes": 0}).revision
        == original.revision
    )
    variants = [
        preview(request, approval_required=False),
        preview(request, requested_capacity={"max_nodes": 3}),
        preview(replace(request, existing_cluster_name="other-cluster")),
        preview(
            request,
            cost_estimate={
                "amount_usd": 1.5,
                "currency": "USD",
                "as_of": "2026-09-24T00:00:00Z",
                "assumptions": ["hourly"],
            },
        ),
    ]
    assert all(item.revision != original.revision for item in variants)
    changed = original.as_dict()
    changed["target"]["cluster"] = "changed"
    assert original.as_dict()["target"]["cluster"] == "fixture-adopted"


@pytest.mark.parametrize("capacity", [{"nodes": float("nan")}, {1: "not-a-string-key"}])
def test_noncanonical_capacity_refused(capacity):
    with pytest.raises(ModeError):
        preview(
            request_for(OwnershipMode.BRING_EXISTING_CLUSTER),
            requested_capacity=capacity,
        )
