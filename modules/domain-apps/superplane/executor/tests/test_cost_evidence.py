"""Deletion evidence must never become a zero bill or measured usage."""

from types import SimpleNamespace

import pytest
from harness_jobs.inventory import AllocationResource
from superplane_executor.cost_evidence import build_cost_evidence


@pytest.mark.parametrize("released", [False, True])
def test_retained_regional_handles_survive_cleanup_without_invented_charges(released):
    operation = SimpleNamespace(
        grant=SimpleNamespace(
            lease=SimpleNamespace(
                org_id="org", workspace_id="workspace", operation_id="cleanup"
            )
        ),
        request=SimpleNamespace(
            action="teardown",
            parameters={
                "allocation_id": "allocation",
                "controller_source_operation_id": "original",
            },
        ),
    )
    plan = SimpleNamespace(data={"provider_account_id": "123456789012"})
    kinds = {
        "instance": "arn:aws:ec2:us-west-2:123456789012:instance/i-0123456789abcdef0",
        "volume": "arn:aws:ec2:us-west-2:123456789012:volume/vol-0123456789abcdef0",
        "network_interface": "arn:aws:ec2:us-west-2:123456789012:network-interface/eni-0123456789abcdef0",
        "address": "arn:aws:ec2:us-west-2:123456789012:elastic-ip/eipalloc-0123456789abcdef0",
        "network_dependency": "superplane-network:original-dependency",
        "node_command": "superplane-node-command:original-command",
        "workspace_object": "kubernetes:Job:namespace:original-job:original-uid",
        "kubernetes_node": "original-node-identity",
    }
    resources = {
        reference: AllocationResource(
            reference, "aws", reference, kind, frozenset({"original-create"})
        )
        for kind, reference in kinds.items()
    }
    disposition = "terminal" if released else "live"
    accounting = {
        "checked_at": "2026-09-25T00:00:00+00:00",
        "inventory_complete": released,
        "may_mark_released": released,
        "release_permitted": released,
        "resource_dispositions": {reference: disposition for reference in resources},
    }
    report = build_cost_evidence(operation, plan, resources, accounting)
    assert report["source_operation_id"] == "original"
    assert report["observation_operation_id"] == "cleanup"
    assert report["allocation_id"] == "allocation"
    assert report["approved_provider_account_id"] == "123456789012"
    expected = {
        "compute": {"instance"},
        "storage": {"volume"},
        "network": {"network_interface", "address", "network_dependency"},
        "transfer": {"instance", "network_interface", "network_dependency"},
    }
    assert set(report["categories"]) == set(expected)
    for category, selected in expected.items():
        evidence = report["categories"][category]
        assert {
            item["provider_reference"] for item in evidence["resource_observations"]
        } == {kinds[kind] for kind in selected}
        assert all(
            item["disposition"] == disposition
            for item in evidence["resource_observations"]
        )
        assert evidence["resource_inventory_complete"] is released
        assert evidence["resource_observed_at"] == accounting["checked_at"]
        for name in ("usage", "billed_cost"):
            assert evidence[name]["status"] == "unknown"
            assert evidence[name]["interval_start"] is None
            assert evidence[name]["interval_end"] is None
            assert evidence[name]["reason"]
        assert evidence["usage"]["quantity"] is None
        assert evidence["usage"]["unit"] is None
        assert evidence["billed_cost"]["amount"] is None
        assert evidence["billed_cost"]["currency"] is None


def test_missing_observation_remains_unobserved_in_every_cost_category():
    operation = SimpleNamespace(
        grant=SimpleNamespace(
            lease=SimpleNamespace(
                org_id="org", workspace_id="workspace", operation_id="source"
            )
        ),
        request=SimpleNamespace(
            action="provision", parameters={"allocation_id": "allocation"}
        ),
    )
    resource = AllocationResource(
        "instance", "aws", "i-0123456789abcdef0", "instance", frozenset()
    )
    report = build_cost_evidence(
        operation,
        SimpleNamespace(data={"provider_account_id": "123456789012"}),
        {resource.provider_reference: resource},
        {
            "resource_dispositions": {},
            "checked_at": "2026-09-25T00:00:00Z",
            "inventory_complete": False,
        },
    )
    assert report["source_operation_id"] == "source"
    assert (
        report["categories"]["compute"]["resource_observations"][0]["disposition"]
        == "unobserved"
    )
    for evidence in report["categories"].values():
        assert evidence["resource_inventory_complete"] is False
        assert evidence["billed_cost"]["status"] == "unknown"
        assert evidence["billed_cost"]["amount"] is None
