"""Ownership inventory against DynamoDB emulation, including reruns."""

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from src.orchestration.chat_data_migration import inventory


def _table(resource, name):
    return resource.create_table(
        TableName=name,
        KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
        AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"}, {"AttributeName": "SK", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )


def _seed_legacy(context, artifacts, memory):
    """Valid-owner, missing-owner and conflicting-owner records across all three tables."""
    owner = {"orgId": "org-a", "tenantId": "org-a", "teamId": "team", "ownerUserId": "alice"}
    context.put_item(Item={"PK": "session#alice", "SK": "header", **owner})
    context.put_item(Item={"PK": "session#alice", "SK": "msg#1", "content": "private", **owner})
    context.put_item(Item={"PK": "session#alice", "SK": "msg#unverified", "content": "unknown origin"})
    context.put_item(Item={"PK": "session#legacy", "SK": "header", "ownerUserId": "alice"})
    context.put_item(Item={"PK": "session#legacy", "SK": "msg#2"})
    context.put_item(Item={"PK": "session#alice", "SK": "sum#conflict", "ownerUserId": "bob"})
    prefix = "o/org-a/t/team/u/alice/s/alice/"
    artifacts.put_item(Item={"PK": "session#alice", "SK": "art#valid", "s3Key": prefix + "task/in/a", **owner})
    artifacts.put_item(Item={"PK": "session#alice", "SK": "art#conflict", "s3Key": prefix + "task/in/b", "user_id": "bob"})
    artifacts.put_item(Item={"PK": "session#legacy", "SK": "art#legacy", "s3Key": "legacy/task/in/c"})
    memory.put_item(Item={"PK": "scope#tenant#org-a", "SK": "mem#1", "scope": {"tenant": "org-a"}})


EXPECTED_QUARANTINE = {
    ("context", "session#alice", "msg#unverified", "child_owner_missing"),
    ("context", "session#alice", "sum#conflict", "child_owner_conflict"),
    ("context", "session#legacy", "header", "header_owner_missing_or_invalid"),
    ("context", "session#legacy", "msg#2", "session_header_unresolved"),
    ("artifacts", "session#alice", "art#conflict", "catalog_owner_conflict"),
    ("artifacts", "session#legacy", "art#legacy", "session_header_unresolved"),
    ("memory", "scope#tenant#org-a", "mem#1", "memory_index_unreferenced"),
}


def _entries(report):
    return {(entry["table"], entry["key"]["PK"], entry["key"]["SK"], entry["reason"]) for entry in report}


def _reconciled(counts, *, apply):
    for table in counts.values():
        assert table.get("total", 0) == table.get("owned", 0) + table.get("quarantined", 0) + table.get("backfill_candidates", 0), table
        if apply:
            assert table.get("backfill_candidates", 0) == table.get("backfilled", 0) + table.get("conflicts", 0), table
    return True


def test_legacy_quarantine_backfill_and_idempotent_rerun():
    with mock_aws():
        dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
        context, artifacts, memory = (_table(dynamodb, name) for name in ("context", "artifacts", "memory"))
        _seed_legacy(context, artifacts, memory)

        dry = inventory(context, artifacts, memory)
        assert dry["context"] == {"total": 6, "owned": 2, "quarantined": 4}
        assert dry["artifacts"] == {"total": 3, "backfill_candidates": 1, "quarantined": 2}
        assert dry["memory"] == {"total": 1, "quarantined": 1}
        assert "org_id" not in artifacts.get_item(Key={"PK": "session#alice", "SK": "art#valid"})["Item"]

        applied = inventory(context, artifacts, memory, apply=True)
        assert applied["artifacts"]["backfilled"] == 1
        assert inventory(context, artifacts, memory, apply=True)["artifacts"] == {"total": 3, "owned": 1, "quarantined": 2}
        assert artifacts.get_item(Key={"PK": "session#alice", "SK": "art#valid"})["Item"]["user_id"] == "alice"
        assert artifacts.get_item(Key={"PK": "session#alice", "SK": "art#conflict"})["Item"]["user_id"] == "bob"


def test_quarantine_report_names_every_record_with_a_reason_and_counts_survive_dry_run_apply_and_rerun():
    with mock_aws():
        dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
        context, artifacts, memory = (_table(dynamodb, name) for name in ("context", "artifacts", "memory"))
        _seed_legacy(context, artifacts, memory)
        reports = {}
        for mode, apply in (("dry_run", False), ("apply", True), ("rerun", True)):
            report = []
            counts = inventory(context, artifacts, memory, apply=apply, report=report.append)
            assert _reconciled(counts, apply=apply)
            assert {name: (table.get("total"), table.get("quarantined")) for name, table in counts.items()} == {
                "context": (6, 4),
                "artifacts": (3, 2),
                "memory": (1, 1),
            }
            assert _entries(report) == EXPECTED_QUARANTINE, mode
            assert all(entry["category"] == "quarantined" for entry in report)
            assert all(set(entry) == {"table", "key", "reason", "category"} for entry in report)
            assert "private" not in str(report) and "unknown origin" not in str(report) and "bob" not in str(report)
            reports[mode] = sorted(report, key=lambda entry: (entry["table"], entry["key"]["PK"], entry["key"]["SK"]))
        assert reports["dry_run"] == reports["apply"] == reports["rerun"]
        assert sum(len(entries) for entries in reports.values()) == 3 * len(EXPECTED_QUARANTINE)


def test_paged_scans_process_incrementally_with_exact_totals():
    with mock_aws():
        dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
        context, artifacts, memory = (_table(dynamodb, name) for name in ("context", "artifacts", "memory"))
        _seed_legacy(context, artifacts, memory)
        for index in range(7):
            owner = {"orgId": "org-a", "tenantId": "org-a", "teamId": "team", "ownerUserId": "alice"}
            context.put_item(Item={"PK": f"session#page-{index}", "SK": "header", **owner})
            context.put_item(Item={"PK": f"session#page-{index}", "SK": "msg#1", **owner})
        calls = {"context": [], "artifacts": [], "memory": []}
        for name, table in (("context", context), ("artifacts", artifacts), ("memory", memory)):
            original = table.scan

            def paged(_original=original, _name=name, **kwargs):
                calls[_name].append(kwargs)
                return _original(**kwargs)

            table.scan = paged
        unpaged = inventory(context, artifacts, memory)
        assert all(len(scans) == 1 and "Limit" not in scans[0] for scans in calls.values())
        for scans in calls.values():
            scans.clear()
        report = []
        paged_counts = inventory(context, artifacts, memory, page_size=2, report=report.append)
        assert paged_counts == unpaged
        assert paged_counts["context"] == {"total": 20, "owned": 16, "quarantined": 4}
        assert len(calls["context"]) == 10 and all(scan["Limit"] == 2 for scan in calls["context"])
        assert len(calls["artifacts"]) == 2 and len(calls["memory"]) == 1
        assert all("ExclusiveStartKey" in scan for scan in calls["context"][1:])
        assert _entries(report) == EXPECTED_QUARANTINE
        assert _reconciled(paged_counts, apply=False)
        applied = inventory(context, artifacts, memory, apply=True, page_size=2)
        assert applied["artifacts"] == {"total": 3, "backfill_candidates": 1, "backfilled": 1, "quarantined": 2}
        assert _reconciled(applied, apply=True)
        assert inventory(context, artifacts, memory, apply=True, page_size=1) == {**applied, "artifacts": {"total": 3, "owned": 1, "quarantined": 2}}


def test_non_string_sort_key_is_quarantined_once_in_every_mode():
    with mock_aws():
        dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
        context, artifacts, memory = (_table(dynamodb, name) for name in ("context", "artifacts", "memory"))
        owner = {"orgId": "org", "tenantId": "org", "teamId": "team", "ownerUserId": "alice"}
        context.put_item(Item={"PK": "session#alice", "SK": "header", **owner})

        class Catalog:
            name = "artifacts"
            meta = artifacts.meta

            @staticmethod
            def scan(**kwargs):
                return {"Items": [{"PK": "session#alice", "SK": 7, "s3Key": "o/org/t/team/u/alice/s/alice/task/in/file", **owner}]}

        for apply in (False, True, True):
            report = []
            counts = inventory(context, Catalog(), memory, apply=apply, report=report.append)
            assert counts["artifacts"] == {"total": 1, "quarantined": 1}
            assert _reconciled(counts, apply=apply)
            assert [(entry["key"], entry["reason"]) for entry in report] == [({"PK": "session#alice", "SK": "7"}, "sort_key_invalid")]


def test_backfill_conflicts_are_reported_with_their_keys(migration_tables, monkeypatch):
    context, artifacts, memory = migration_tables
    key = {"PK": "session#alice", "SK": "art#one"}

    def change_owner():
        row = artifacts.get_item(Key=key, ConsistentRead=True)["Item"]
        artifacts.put_item(Item={**row, "user_id": "other-owner"})

    _before_backfill(monkeypatch, artifacts, change_owner)
    report = []
    counts = inventory(context, artifacts, memory, apply=True, report=report.append)
    assert counts["artifacts"] == {"total": 1, "backfill_candidates": 1, "conflicts": 1}
    assert _reconciled(counts, apply=True)
    assert report == [{"table": "artifacts", "key": key, "reason": "backfill_conflict", "category": "conflict"}]
    report.clear()
    assert inventory(context, artifacts, memory, apply=True, report=report.append)["artifacts"] == {"total": 1, "quarantined": 1}
    assert report == [{"table": "artifacts", "key": key, "reason": "catalog_owner_conflict", "category": "quarantined"}]


def test_teamless_context_is_owned_but_artifacts_and_ambiguous_children_stay_quarantined():
    with mock_aws():
        dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
        context, artifacts, memory = (_table(dynamodb, name) for name in ("context", "artifacts", "memory"))
        owner = {"orgId": "org-a", "tenantId": "org-a", "teamId": "", "ownerUserId": "alice"}
        rows = [
            {"PK": "session#personal", "SK": "header", **owner},
            {"PK": "session#personal", "SK": "msg#valid", **owner},
            {"PK": "session#personal", "SK": "sum#missing"},
            {"PK": "session#personal", "SK": "msg#conflict", **owner, "team_id": "other"},
            {"PK": "session#missing-team", "SK": "header", **{field: value for field, value in owner.items() if field != "teamId"}},
            {"PK": "session#null-team", "SK": "header", **owner, "teamId": None},
        ]
        for row in rows:
            context.put_item(Item=row)
        catalog = {"PK": "session#personal", "SK": "art#legacy", "s3Key": "o/org-a/t//u/alice/s/personal/task/in/file"}
        artifacts.put_item(Item=catalog)
        for apply in (False, True, True):
            assert inventory(context, artifacts, memory, apply=apply) == {
                "context": {"total": 6, "owned": 2, "quarantined": 4},
                "artifacts": {"total": 1, "quarantined": 1},
                "memory": {},
            }
            assert artifacts.get_item(Key={"PK": catalog["PK"], "SK": catalog["SK"]})["Item"] == catalog
            for row in rows:
                assert context.get_item(Key={"PK": row["PK"], "SK": row["SK"]})["Item"] == row


def test_partial_scan_failure_is_not_an_empty_result():
    class PartialTable:
        def scan(self, **kwargs):
            if kwargs:
                raise RuntimeError("page unavailable")
            return {"Items": [{"PK": "session#x", "SK": "header"}], "LastEvaluatedKey": {"PK": "session#x", "SK": "header"}}

    with pytest.raises(RuntimeError, match="page unavailable"):
        inventory(PartialTable(), None, None)


def test_conflicting_header_and_catalog_aliases_are_quarantined():
    with mock_aws():
        dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
        context, artifacts, memory = (_table(dynamodb, name) for name in ("context", "artifacts", "memory"))
        context.put_item(
            Item={"PK": "session#one", "SK": "header", "orgId": "org", "tenantId": "org", "teamId": "team", "ownerUserId": "alice", "user_id": "bob"}
        )
        context.put_item(Item={"PK": "session#one", "SK": "draft", "draft": {"intent": "private"}})
        artifacts.put_item(Item={"PK": "session#one", "SK": "art#one", "s3Key": "o/org/t/team/u/alice/s/one/task/in/file"})
        assert inventory(context, artifacts, memory, apply=True) == {
            "context": {"total": 2, "quarantined": 2},
            "artifacts": {"total": 1, "quarantined": 1},
            "memory": {},
        }
        assert "user_id" not in artifacts.get_item(Key={"PK": "session#one", "SK": "art#one"})["Item"]


def test_valid_legacy_session_ids_and_traversal_are_distinguished():
    with mock_aws():
        dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
        context, artifacts, memory = (_table(dynamodb, name) for name in ("context", "artifacts", "memory"))
        for session in ("webchat:C123:user-1", "1758441600.123456", "a..b"):
            owner = {"orgId": "org", "tenantId": "org", "teamId": "team", "ownerUserId": "alice"}
            context.put_item(Item={"PK": f"session#{session}", "SK": "header", **owner})
            artifacts.put_item(Item={"PK": f"session#{session}", "SK": "art#one", "s3Key": f"o/org/t/team/u/alice/s/{session}/task/in/file", **owner})
        counts = inventory(context, artifacts, memory)
        assert counts["context"] == {"total": 3, "owned": 2, "quarantined": 1}
        assert counts["artifacts"] == {"total": 3, "backfill_candidates": 2, "quarantined": 1}


def test_artifact_scan_failure_does_not_backfill_partial_data():
    with mock_aws():
        dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
        context, artifacts, memory = (_table(dynamodb, name) for name in ("context", "artifacts", "memory"))
        owner = {"orgId": "org", "tenantId": "org", "teamId": "team", "ownerUserId": "alice"}
        context.put_item(Item={"PK": "session#alice", "SK": "header", **owner})
        artifacts.put_item(Item={"PK": "session#alice", "SK": "art#one", "s3Key": "o/org/t/team/u/alice/s/alice/task/in/file", **owner})

        class InterruptedScan:
            def scan(self, **kwargs):
                if kwargs:
                    raise RuntimeError("second artifact page unavailable")
                return {"Items": artifacts.scan()["Items"], "LastEvaluatedKey": {"PK": "session#alice", "SK": "art#one"}}

        with pytest.raises(RuntimeError, match="second artifact page unavailable"):
            inventory(context, InterruptedScan(), memory, apply=True)
        assert "user_id" not in artifacts.get_item(Key={"PK": "session#alice", "SK": "art#one"})["Item"]


@pytest.fixture
def migration_tables():
    with mock_aws():
        dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
        context, artifacts, memory = (_table(dynamodb, name) for name in ("context", "artifacts", "memory"))
        owner = {"orgId": "org", "tenantId": "org", "teamId": "team", "ownerUserId": "alice"}
        context.put_item(Item={"PK": "session#alice", "SK": "header", **owner})
        artifacts.put_item(Item={"PK": "session#alice", "SK": "art#one", "s3Key": "o/org/t/team/u/alice/s/alice/task/in/file", **owner})
        yield context, artifacts, memory


@pytest.mark.parametrize("field", ["orgId", "tenantId", "teamId", "ownerUserId", "all"])
@pytest.mark.parametrize("mutation", ["remove", "null"])
def test_catalog_ownership_cannot_be_inferred_from_header_or_path(migration_tables, field, mutation):
    context, artifacts, memory = migration_tables
    key = {"PK": "session#alice", "SK": "art#one"}
    row = artifacts.get_item(Key=key)["Item"]
    fields = ("orgId", "tenantId", "teamId", "ownerUserId") if field == "all" else (field,)
    for owner_field in fields:
        if mutation == "remove":
            del row[owner_field]
        else:
            row[owner_field] = None
    artifacts.put_item(Item=row)

    for apply in (False, True, True):
        assert inventory(context, artifacts, memory, apply=apply)["artifacts"] == {"total": 1, "quarantined": 1}
        assert artifacts.get_item(Key=key, ConsistentRead=True)["Item"] == row


def test_already_owned_legacy_catalog_remains_owned_without_backfill(migration_tables):
    context, artifacts, memory = migration_tables
    row = {
        "PK": "session#alice",
        "SK": "art#one",
        "s3Key": "o/org/t/team/u/alice/s/alice/task/in/file",
        "org_id": "org",
        "team_id": "team",
        "user_id": "alice",
    }
    artifacts.put_item(Item=row)
    for apply in (False, True, True):
        assert inventory(context, artifacts, memory, apply=apply)["artifacts"] == {"total": 1, "owned": 1}
        assert artifacts.get_item(Key={"PK": row["PK"], "SK": row["SK"]}, ConsistentRead=True)["Item"] == row


def _before_backfill(monkeypatch, artifacts, action):
    original_update = artifacts.update_item
    original_transaction = artifacts.meta.client.transact_write_items

    def update(**kwargs):
        action()
        return original_update(**kwargs)

    def transaction(**kwargs):
        action()
        return original_transaction(**kwargs)

    monkeypatch.setattr(artifacts, "update_item", update)
    monkeypatch.setattr(artifacts.meta.client, "transact_write_items", transaction)


@pytest.mark.parametrize("table_name", ["context", "artifacts"])
@pytest.mark.parametrize("field", ["orgId", "tenantId", "teamId", "ownerUserId", "org_id", "tenant_id", "team_id", "user_id", "owner_user_id"])
def test_backfill_rejects_concurrent_ownership_change(migration_tables, monkeypatch, table_name, field):
    context, artifacts, memory = migration_tables
    table = context if table_name == "context" else artifacts
    key = {"PK": "session#alice", "SK": "header" if table_name == "context" else "art#one"}

    def change_owner():
        row = table.get_item(Key=key, ConsistentRead=True)["Item"]
        table.put_item(Item={**row, field: "other-owner"})

    _before_backfill(monkeypatch, artifacts, change_owner)
    counts = inventory(context, artifacts, memory, apply=True)

    assert counts["artifacts"] == {"total": 1, "backfill_candidates": 1, "conflicts": 1}
    catalog = artifacts.get_item(Key={"PK": "session#alice", "SK": "art#one"}, ConsistentRead=True)["Item"]
    assert all(catalog.get(owner_field) in (None, "other-owner") for owner_field in ("org_id", "team_id", "user_id"))
    assert table.get_item(Key=key, ConsistentRead=True)["Item"][field] == "other-owner"
    assert inventory(context, artifacts, memory, apply=True)["artifacts"] == {"total": 1, "quarantined": 1}


@pytest.mark.parametrize("field", ["orgId", "tenantId", "teamId", "ownerUserId"])
@pytest.mark.parametrize("mutation", ["remove", "null"])
@pytest.mark.parametrize("table_name", ["context", "artifacts"])
def test_backfill_requires_complete_current_ownership(migration_tables, monkeypatch, field, mutation, table_name):
    context, artifacts, memory = migration_tables
    table = context if table_name == "context" else artifacts
    key = {"PK": "session#alice", "SK": "header" if table_name == "context" else "art#one"}

    def remove_owner():
        row = table.get_item(Key=key, ConsistentRead=True)["Item"]
        if mutation == "remove":
            del row[field]
        else:
            row[field] = None
        table.put_item(Item=row)

    _before_backfill(monkeypatch, artifacts, remove_owner)
    assert inventory(context, artifacts, memory, apply=True)["artifacts"] == {"total": 1, "backfill_candidates": 1, "conflicts": 1}
    assert "user_id" not in artifacts.get_item(Key={"PK": "session#alice", "SK": "art#one"}, ConsistentRead=True)["Item"]


@pytest.mark.parametrize("table_name", ["context", "artifacts"])
def test_backfill_rejects_concurrent_deletion(migration_tables, monkeypatch, table_name):
    context, artifacts, memory = migration_tables
    table = context if table_name == "context" else artifacts
    key = {"PK": "session#alice", "SK": "header" if table_name == "context" else "art#one"}
    _before_backfill(monkeypatch, artifacts, lambda: table.delete_item(Key=key))

    assert inventory(context, artifacts, memory, apply=True)["artifacts"] == {"total": 1, "backfill_candidates": 1, "conflicts": 1}
    assert "Item" not in table.get_item(Key=key, ConsistentRead=True)
    catalog = artifacts.get_item(Key={"PK": "session#alice", "SK": "art#one"}, ConsistentRead=True).get("Item", {})
    assert "user_id" not in catalog


def test_backfill_rejects_concurrent_object_path_change(migration_tables, monkeypatch):
    context, artifacts, memory = migration_tables
    key = {"PK": "session#alice", "SK": "art#one"}
    replacement = {**key, "s3Key": "o/org/t/team/u/bob/s/alice/task/in/file"}
    _before_backfill(monkeypatch, artifacts, lambda: artifacts.put_item(Item=replacement))

    assert inventory(context, artifacts, memory, apply=True)["artifacts"] == {"total": 1, "backfill_candidates": 1, "conflicts": 1}
    assert artifacts.get_item(Key=key, ConsistentRead=True)["Item"] == replacement


def test_nullable_catalog_ownership_can_be_backfilled(migration_tables):
    context, artifacts, memory = migration_tables
    key = {"PK": "session#alice", "SK": "art#one"}
    row = artifacts.get_item(Key=key)["Item"]
    artifacts.put_item(Item={**row, "org_id": None, "team_id": None, "user_id": None})

    assert inventory(context, artifacts, memory, apply=True)["artifacts"] == {"total": 1, "backfill_candidates": 1, "backfilled": 1}
    catalog = artifacts.get_item(Key=key, ConsistentRead=True)["Item"]
    assert (catalog["org_id"], catalog["team_id"], catalog["user_id"]) == ("org", "team", "alice")
    assert inventory(context, artifacts, memory, apply=True)["artifacts"] == {"total": 1, "owned": 1}


@pytest.mark.parametrize(
    "error",
    [
        {"Error": {"Code": "AccessDeniedException"}},
        {"Error": {"Code": "InternalServerError"}},
        {"Error": {"Code": "TransactionCanceledException"}},
        {"Error": {"Code": "TransactionCanceledException"}, "CancellationReasons": [{"Code": "TransactionConflict"}]},
        {
            "Error": {"Code": "TransactionCanceledException"},
            "CancellationReasons": [{"Code": "ConditionalCheckFailed"}, {"Code": "ProvisionedThroughputExceeded"}],
        },
    ],
)
def test_backfill_does_not_hide_storage_failures(migration_tables, monkeypatch, error):
    context, artifacts, memory = migration_tables

    def fail():
        raise ClientError(error, "TransactWriteItems")

    _before_backfill(monkeypatch, artifacts, fail)
    with pytest.raises(ClientError) as failure:
        inventory(context, artifacts, memory, apply=True)
    assert failure.value.response == error
    assert "user_id" not in artifacts.get_item(Key={"PK": "session#alice", "SK": "art#one"})["Item"]
