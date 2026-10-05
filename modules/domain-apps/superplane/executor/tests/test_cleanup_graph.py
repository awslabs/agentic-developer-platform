"""The staged graph cannot hide mutations outside existing approval bounds."""

import json

import pytest
from harness_jobs.identity import OperationRefused

from superplane_executor.cleanup_graph import canonical, read, steps


def graph(**changes):
    return {
        "version": 1,
        "snapshot_id": "a" * 64,
        "snapshot_sha256": "b" * 64,
        "nodes": 2,
        "roots": 2,
        "network": 2,
        **changes,
    }


def test_each_mutation_has_a_distinct_original_approved_step():
    result = json.loads(steps(graph(), "original"))
    assert [r["step_id"] for r in result] == [
        "cordon:0",
        "cordon:1",
        "root:0",
        "root:1",
        "drain",
        "down",
        "node:0",
        "node:1",
        "network:0",
        "network:1",
        "inventory",
    ]
    assert all(
        (r["provider"], r["operation_kind"], r["target"])
        == ("aws", "delete_cluster", "original")
        for r in result
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"nodes": True},
        {"nodes": 17},
        {"roots": 3},
        {"network": -1},
        {"version": 2},
        {"snapshot_id": "foreign"},
        {"command": "delete everything"},
    ],
)
def test_unapproved_or_unbounded_header_is_refused(changes):
    with pytest.raises(OperationRefused):
        read(canonical(graph(**changes)))


def test_shared_step_limit_is_not_bypassed_by_a_compact_header():
    with pytest.raises(ValueError, match="step count"):
        steps(graph(nodes=16, network=32), "original")


def test_header_cannot_change_bytes_after_approval():
    with pytest.raises(OperationRefused):
        read(json.dumps(graph()))
