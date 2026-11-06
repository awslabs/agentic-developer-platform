"""Partial state proof and plan identity cannot be borrowed by ordinary retries."""

import copy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import boto3
import pytest
from fastapi import HTTPException
from moto import mock_aws

from src.internal import domain_partial_apply as owner
from src.shared.domain_partial_apply import IDENTITY, record_id, validate
from src.shared.domain_provider_contract import canonical, digest


def evidence():
    now = datetime.now(UTC)
    observation = {
        "kind": "partial-apply-state-v1",
        "apply_started": True,
        "source_operation_id": "original-plan",
        "module_sha256": "a" * 64,
        "source_files": {
            name: "b" * 64
            for name in (
                "workspace.tfplan",
                "workspace-plan.json",
                "workspace-inventory.json",
                "workspace-estimate.json",
                "workspace-authorization.proposed.json",
                "workspace-backend.json",
            )
        },
        "apply_files": {"command-failure-original/stderr": "c" * 64},
    }
    snapshot = {
        "resources": {
            "aws_eks_cluster.workspace": {"type": "aws_eks_cluster", "id": "cluster", "tainted": False},
            "aws_eip.nat[0]": {"type": "aws_eip", "id": "eipalloc-0123456789abcdef0", "tainted": True},
        },
        "sha256": "d" * 64,
        "serial": 2,
        "version_id": "original-version",
        "cluster_arn": "arn:aws:eks:us-west-2:123456789012:cluster/cluster",
    }
    value = {
        "version": 1,
        "org_id": "org",
        "workspace_id": "workspace",
        "operation_id": "apply",
        "plan_digest": "e" * 64,
        "source_key": "original-key",
        "source_fence": 1,
        "source_version": 2,
        "evidence_kind": "partial-apply-state-v1",
        "apply_started": True,
        "planning_started": True,
        "original_artifact_id": "f" * 64,
        "original_planning_operation_id": "original-plan",
        "account_id": "123456789012",
        "region": "us-west-2",
        "inspection_sha256": "1" * 64,
        "checked_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=30)).isoformat(),
        "snapshot": snapshot,
        "preparation_boundary": {
            "kind": "partial-apply-state-v1",
            "observation": observation,
            "backend_observation": snapshot,
            "cluster_status": "ACTIVE",
            "sha256": digest(observation),
            "lifecycle_policy_sha256": "2" * 64,
        },
        **{
            k: "retained-" + k
            for k in (
                "source_principal",
                "workload_binding",
                "source_pod_name",
                "source_acquired_at",
                "source_image",
                "storage_uid",
                "volume_uid",
                "probe_job_uid",
                "credential_id",
                "installation_id",
                "adp_org_id",
            )
        },
    }
    value["record_id"] = record_id(value)
    return value


@pytest.mark.parametrize(
    "change",
    [None, "missing", "revision", "expired", "foreign-fence", "false-fence", "unstarted", "cluster-tainted", "extra-attempts", "different-backend"],
)
def test_protected_partial_evidence_requires_exact_owner_record(monkeypatch, change):
    value = evidence()
    identity = {k: value[k] for k in IDENTITY}
    stored = copy.deepcopy(value)
    if change == "expired":
        stored["checked_at"] = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
        stored["expires_at"] = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    elif change == "foreign-fence":
        stored["source_fence"] += 1
    elif change == "false-fence":
        stored["source_fence"] = True
    elif change == "unstarted":
        stored["apply_started"] = False
    elif change == "cluster-tainted":
        stored["snapshot"]["resources"]["aws_eks_cluster.workspace"]["tainted"] = True
    elif change == "extra-attempts":
        stored["attempt_extension"] = 5
    elif change == "different-backend":
        stored["snapshot"] = {**stored["snapshot"], "version_id": "different"}
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        client.create_table(
            TableName="authority",
            KeySchema=[{"AttributeName": "record_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "record_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        if change != "missing":
            client.put_item(
                TableName="authority",
                Item={
                    "record_id": {"S": value["record_id"]},
                    "document": {"S": canonical(stored)},
                    "revision": {"S": "corrupt" if change == "revision" else digest(stored)},
                },
            )
        monkeypatch.setattr(owner, "aws_client", lambda _: client)
        monkeypatch.setattr(owner, "tables", lambda: ("authority", "unused"))
        if change is None:
            assert owner.read_evidence(identity) == value
        else:
            with pytest.raises((ValueError, HTTPException)):
                owner.read_evidence(identity)


def test_historical_validation_does_not_make_expired_proof_executable():
    value = evidence()
    now = datetime.now(UTC)
    value["checked_at"] = (now - timedelta(hours=2)).isoformat()
    value["expires_at"] = (now - timedelta(hours=1)).isoformat()
    assert validate(value, fresh=False) == value
    with pytest.raises(ValueError):
        validate(value)


def test_new_inspection_keeps_one_hour_expiry_and_distinct_immutable_key():
    original = evidence()
    fresh = {**copy.deepcopy(original), "version": 2, "inspection_id": "1" * 16}
    fresh["record_id"] = record_id(fresh)
    assert fresh["record_id"] != original["record_id"]
    assert validate(fresh) == fresh
    fresh["expires_at"] = (datetime.fromisoformat(fresh["checked_at"]) + timedelta(hours=2)).isoformat()
    with pytest.raises(ValueError):
        validate(fresh)


def test_selected_inspection_must_exist_and_bind_exact_source(monkeypatch):
    value = evidence()
    value.update(version=2, inspection_id="2" * 16)
    value["record_id"] = record_id(value)
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        client.create_table(
            TableName="authority",
            KeySchema=[{"AttributeName": "record_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "record_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        client.put_item(
            TableName="authority",
            Item={"record_id": {"S": value["record_id"]}, "document": {"S": canonical(value)}, "revision": {"S": digest(value)}},
        )
        monkeypatch.setattr(owner, "aws_client", lambda _: client)
        monkeypatch.setattr(owner, "tables", lambda: ("authority", "unused"))
        identity = {k: value[k] for k in IDENTITY}
        assert owner.read_evidence(identity, value["record_id"]) == value
        for changed, selected in [({**identity, "source_fence": 2}, value["record_id"]), (identity, "sppa1:" + "0" * 64)]:
            with pytest.raises(HTTPException):
                owner.read_evidence(changed, selected)


async def test_delivery_binds_actual_artifact_to_protected_receipt(monkeypatch):
    import json

    proof = {"owner_evidence": evidence()}
    repair = {"artifact_id": "b" * 64}
    monkeypatch.setattr(owner, "harness", lambda _: SimpleNamespace(partial_repair_identity=lambda _: repair))
    reader = AsyncMock(return_value=repair)
    monkeypatch.setattr(owner, "reviewed_artifact", reader)
    operation = {"recovery_scope": "partial-apply-repair", "provider_recovery_proof": json.dumps(proof)}
    binding = object()
    await owner.verify_repair_delivery(binding, operation)
    reader.assert_awaited_once_with(binding, operation, proof["owner_evidence"], repair["artifact_id"], fresh=False)
    reader.return_value = {"artifact_id": "c" * 64}
    with pytest.raises(HTTPException):
        await owner.verify_repair_delivery(binding, operation)


@pytest.mark.parametrize("obstruction", [None, "orphan", "unadmitted", "fresh", "intent", "artifact", "active", "limit"])
async def test_fresh_inspection_checks_all_prior_admissions(monkeypatch, obstruction):
    from contextlib import asynccontextmanager

    old = evidence()
    now = datetime.now(UTC)
    old.update(checked_at=(now - timedelta(hours=2)).isoformat(), expires_at=(now - timedelta(hours=1)).isoformat())
    fresh = evidence()
    fresh.update(version=2, inspection_id="3" * 16)
    fresh["record_id"] = record_id(fresh)
    if obstruction == "fresh":
        old = evidence()
    row = {"operation_id": "prior", "owner_evidence_json": canonical(old), "owner_evidence_sha256": digest(old)}
    registered = [] if obstruction == "orphan" else [row]
    admitted = [] if obstruction == "unadmitted" else [{"operation_id": "prior"}]
    if obstruction == "limit":
        registered = [{**row, "operation_id": str(i)} for i in range(5)]
        admitted = [{"operation_id": str(i)} for i in range(5)]
    domain = SimpleNamespace(fetch=AsyncMock(return_value=registered), fetchval=AsyncMock(return_value=obstruction == "artifact"))
    calls = []

    async def check(sql, operation):
        calls.append((sql, operation))
        assert operation == "prior"
        return ("harness_provider_call_intent" in sql and obstruction == "intent") or ("harness_operation_leases" in sql and obstruction == "active")

    ledger = SimpleNamespace(fetch=AsyncMock(return_value=admitted), fetchval=check)

    @asynccontextmanager
    async def domain_connect(binding):
        yield domain

    @asynccontextmanager
    async def operation_connect(binding):
        yield ledger

    monkeypatch.setattr(owner, "domain_connect", domain_connect)
    monkeypatch.setattr(owner, "operation_connect", operation_connect)
    operation = {"operation_id": "new", "org_id": "org", "workspace_id": "workspace"}
    if obstruction:
        with pytest.raises(HTTPException):
            await owner.verify_expired_preparations(object(), operation, fresh)
    else:
        await owner.verify_expired_preparations(object(), operation, fresh)
        assert len(calls) == 2
    assert domain.fetch.call_args.args[1:] == ledger.fetch.call_args.args[1:] == ("org", "workspace", "apply", "new")
