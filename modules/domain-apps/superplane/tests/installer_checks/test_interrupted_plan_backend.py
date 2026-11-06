import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from installation.interrupted_plan_backend import observe


@pytest.fixture
def case():
    start = datetime.now(UTC) - timedelta(hours=1)
    backend = {
        "bucket": "fixture-state",
        "region": "us-east-1",
        "lock_table": "fixture-lock",
    }
    target = {
        "environment": "dev",
        "account_id": "111122223333",
        "org_id": "org",
        "workspace_id": "workspace",
    }
    path = "fixture-state/dev/modules/superplane-workspaces/v2/org/workspace/terraform.tfstate"
    lock = {
        "ID": "old-plan",
        "Operation": "OperationTypePlan",
        "Info": "",
        "Who": "@stopped-worker",
        "Version": "1.9.8",
        "Created": (start + timedelta(minutes=1)).isoformat(),
        "Path": path,
    }
    state = SimpleNamespace(
        versioning="Enabled", versions={}, checksum=None, lock=lock, calls=[]
    )

    def get_item(**kw):
        state.calls.append(kw)
        return {
            "Item": state.checksum
            if kw["Key"]["LockID"]["S"].endswith("-md5")
            else (
                {"LockID": {"S": path}, "Info": {"S": json.dumps(state.lock)}}
                if state.lock
                else None
            )
        }

    client = SimpleNamespace(
        get_bucket_versioning=lambda **_: {"Status": state.versioning},
        list_object_versions=lambda **_: state.versions,
        describe_table=lambda **_: {
            "Table": {
                "TableArn": "arn:aws:dynamodb:us-east-1:111122223333:table/fixture-lock"
            }
        },
        get_item=get_item,
    )
    return state, lambda: observe(
        SimpleNamespace(client=lambda *_, **kw: client),
        backend,
        target,
        pod_name="stopped-worker",
        acquired_at=start,
        stopped_at=start + timedelta(minutes=5),
    )


@pytest.mark.parametrize("has_lock", [False, True])
def test_empty_backend_preserves_exact_plan_lock_bytes(case, has_lock):
    state, run = case
    if not has_lock:
        state.lock = None
    proof = run()
    assert proof["plan_lock"] == state.lock
    assert proof["plan_lock_info"] == (json.dumps(state.lock) if has_lock else None)
    assert len(state.calls) == 2 and all(c["ConsistentRead"] for c in state.calls)


@pytest.mark.parametrize(
    "change",
    [
        "apply",
        "foreign",
        "before",
        "after",
        "naive",
        "version",
        "unversioned",
        "state",
        "deleted",
        "truncated",
        "checksum",
    ],
)
def test_uncertain_or_changed_backend_is_refused(case, change):
    state, run = case
    if change == "apply":
        state.lock["Operation"] = "OperationTypeApply"
    elif change == "foreign":
        state.lock["Who"] = "@another-worker"
    elif change == "before":
        state.lock["Created"] = "2000-01-01T00:00:00Z"
    elif change == "after":
        state.lock["Created"] = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    elif change == "naive":
        state.lock["Created"] = "2026-01-01T00:00:00"
    elif change == "version":
        state.lock["Version"] = "9.0.0"
    elif change == "unversioned":
        state.versioning = "Suspended"
    elif change == "state":
        state.versions = {"Versions": [{"VersionId": "other"}]}
    elif change == "deleted":
        state.versions = {"DeleteMarkers": [{"VersionId": "old"}]}
    elif change == "truncated":
        state.versions = {"IsTruncated": True}
    else:
        state.checksum = {"Digest": {"S": "a" * 32}}
    with pytest.raises(ValueError):
        run()
