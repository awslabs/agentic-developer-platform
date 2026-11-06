import copy
from datetime import UTC, datetime, timedelta

import pytest

from src.shared.domain_planning_recovery import record_id, validate


@pytest.fixture
def evidence():
    now = datetime.now(UTC)
    value = dict(
        org_id="org",
        workspace_id="workspace",
        operation_id="operation",
        plan_digest="a" * 64,
        source_key="original-key",
        source_fence=1,
        version=1,
        evidence_kind="interrupted-read-only-plan-v1",
        planning_started=True,
        apply_started=False,
        source_principal="worker#1",
        source_pod_name="original",
        workload_binding="pod-uid",
        storage_uid="claim",
        volume_uid="volume",
        inspection_sha256="b" * 64,
        source_image="registry/worker@sha256:" + "c" * 64,
        checked_at=now.isoformat(),
        expires_at=(now + timedelta(minutes=30)).isoformat(),
        preparation_boundary={
            "kind": "interrupted-read-only-plan-v1",
            "read_only_recipe": True,
            "qualified_source": {"runtime.py": "d" * 64},
            "sha256": "e" * 64,
            "entries": {"review": {"kind": "directory"}},
            "backend_observation": {
                "versioning": "Enabled",
                "versions_absent": True,
                "delete_markers_absent": True,
                "checksum_absent": True,
                "plan_lock": None,
            },
        },
    )
    value["record_id"] = record_id(value)
    return value


def test_interrupted_evidence_cannot_be_used_as_an_unstarted_or_saved_plan(evidence):
    assert validate(evidence, interrupted=True) == evidence
    for options in ({}, {"completion": True}, {"interrupted": True, "completion": True}):
        with pytest.raises(ValueError):
            validate(evidence, **options)


@pytest.mark.parametrize("change", ["apply", "unstarted", "saved-plan", "recipe", "state", "expired", "extra-attempt", "apply-lock"])
def test_incomplete_or_different_evidence_is_refused(evidence, change):
    value = copy.deepcopy(evidence)
    boundary = value["preparation_boundary"]
    if change == "apply":
        value["apply_started"] = True
    elif change == "unstarted":
        value["planning_started"] = False
    elif change == "saved-plan":
        boundary["entries"]["review/workspace.tfplan"] = {"kind": "file", "sha256": "f" * 64}
    elif change == "recipe":
        boundary["read_only_recipe"] = False
    elif change == "state":
        boundary["backend_observation"]["checksum_absent"] = False
    elif change == "expired":
        value["expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    elif change == "extra-attempt":
        value["attempt_extension"] = {"additional_attempts": 1}
    else:
        boundary["backend_observation"]["plan_lock"] = {"Operation": "OperationTypeApply"}
    with pytest.raises(ValueError):
        validate(value, interrupted=True)
