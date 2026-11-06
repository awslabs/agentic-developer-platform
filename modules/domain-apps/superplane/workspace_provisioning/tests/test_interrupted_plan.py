import copy
import json
from types import SimpleNamespace

import pytest

from workspace_provisioning.interrupted_plan import release_plan_lock


@pytest.fixture
def case():
    backend = {
        "bucket": "workspace-state",
        "region": "us-east-1",
        "lock_table": "workspace-locks",
    }
    key = "dev/modules/superplane-workspaces/v2/org/workspace/terraform.tfstate"
    lock = {
        "ID": "original-lock",
        "Operation": "OperationTypePlan",
        "Version": "1.9.8",
        "Who": "@original-worker",
        "Path": backend["bucket"] + "/" + key,
    }
    raw = json.dumps(lock)
    observed = {
        **backend,
        "key": key,
        "versions_absent": True,
        "delete_markers_absent": True,
        "checksum_absent": True,
        "plan_lock": lock,
        "plan_lock_info": raw,
    }
    boundary = {
        "kind": "interrupted-read-only-plan-v1",
        "read_only_recipe": True,
        "backend_observation": observed,
    }
    owner = {
        "record_id": "owner-evidence",
        "evidence_kind": boundary["kind"],
        "planning_started": True,
        "apply_started": False,
        "preparation_boundary": boundary,
        "org_id": "org",
        "workspace_id": "workspace",
        "account_id": "123456789012",
        "source_pod_name": "original-worker",
    }
    proof = {
        "evidence_kind": boundary["kind"],
        "planning_started": True,
        "apply_started": False,
        "original_authority_expired": True,
        "issued_sessions_expired": True,
        "owner_evidence_id": owner["record_id"],
        "owner_evidence": owner,
        "preparation_boundary": boundary,
    }
    item = {"LockID": {"S": lock["Path"]}, "Info": {"S": raw}}

    class Cloud:
        def __init__(self):
            self.item = copy.deepcopy(item)
            self.versions = {}
            self.checksum = None
            self.deletions = []

        def client(self, service, **_):
            assert service in {"s3", "dynamodb"}
            return self

        def list_object_versions(self, **_):
            return self.versions

        def get_item(self, **kw):
            return {
                "Item": self.checksum
                if kw["Key"]["LockID"]["S"].endswith("-md5")
                else self.item
            }

        def delete_item(self, **kw):
            self.deletions.append(kw)
            self.item = None

    return SimpleNamespace(
        cloud=Cloud(),
        config={"environment": "dev", "backend": backend},
        proof=proof,
        item=item,
    )


def test_only_exact_original_plan_lock_is_conditionally_released(case):
    release_plan_lock(case.cloud, case.config, case.proof, verify=lambda: None)
    assert case.cloud.deletions == [
        {
            "TableName": "workspace-locks",
            "Key": {"LockID": case.item["LockID"]},
            "ConditionExpression": "#info = :expected",
            "ExpressionAttributeNames": {"#info": "Info"},
            "ExpressionAttributeValues": {":expected": case.item["Info"]},
        }
    ]


@pytest.mark.parametrize(
    "change",
    [
        "new-lock",
        "state",
        "delete-marker",
        "checksum",
        "backend",
        "apply",
        "unexpired",
        "attempt-extension",
    ],
)
def test_drift_or_wrong_authority_never_deletes_a_lock(case, change):
    if change == "new-lock":
        case.cloud.item["Info"] = {"S": "another owner"}
    elif change == "state":
        case.cloud.versions = {"Versions": [{"VersionId": "new-state"}]}
    elif change == "delete-marker":
        case.cloud.versions = {"DeleteMarkers": [{"VersionId": "history"}]}
    elif change == "checksum":
        case.cloud.checksum = {"Digest": {"S": "a" * 32}}
    elif change == "backend":
        case.config = {
            **case.config,
            "backend": {**case.config["backend"], "bucket": "foreign"},
        }
    elif change == "apply":
        case.proof["apply_started"] = True
    elif change == "unexpired":
        case.proof["original_authority_expired"] = False
    else:
        case.proof["attempt_extension"] = {"additional_attempts": 1}
    with pytest.raises(ValueError):
        release_plan_lock(case.cloud, case.config, case.proof, verify=lambda: None)
    assert case.cloud.deletions == []


def test_authority_loss_before_lock_release_prevents_deletion(case):
    reads = 0

    def verify():
        nonlocal reads
        reads += 1
        if reads == 2:
            raise ValueError("authority withdrawn")

    with pytest.raises(ValueError, match="authority withdrawn"):
        release_plan_lock(case.cloud, case.config, case.proof, verify=verify)
    assert case.cloud.deletions == []


def test_changed_module_is_refused_before_retained_state_is_accepted(tmp_path):
    from workspace_provisioning.interrupted_plan import verify_retained

    (tmp_path / "scripts").mkdir()
    (tmp_path / "main.tf").write_text('resource "null_resource" "unreviewed" {}')
    with pytest.raises(ValueError, match="reviewed read-only recipe"):
        verify_retained({}, tmp_path, {}, target={}, backend={})


def test_changed_runtime_is_not_qualified(tmp_path):
    from workspace_provisioning.interrupted_plan import QUALIFIED_SOURCE, verify_source

    for name in QUALIFIED_SOURCE:
        path = (
            (
                tmp_path.parent
                / "infra/workspaces"
                / name.removeprefix("_data/workspaces/")
            )
            if name.startswith("_data/")
            else tmp_path / name
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("unreviewed recipe")
    with pytest.raises(ValueError, match="source is not qualified"):
        verify_source(tmp_path)
