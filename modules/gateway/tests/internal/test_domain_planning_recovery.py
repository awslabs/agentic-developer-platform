"""Only exact, current protected owner records can supply the planning barrier."""

import copy
from datetime import UTC, datetime, timedelta

import boto3
import pytest
from moto import mock_aws

from src.internal import domain_planning_recovery as owner
from src.shared.domain_planning_recovery import IDENTITY, record_id
from src.shared.domain_provider_contract import canonical, digest


@pytest.mark.parametrize("change", [None, "missing", "revision", "expired", "foreign-fence", "started", "false-fence"])
def test_protected_evidence_read_refuses_changed_or_expired_records(monkeypatch, change):
    now = datetime.now(UTC)
    value = dict(
        org_id="org",
        workspace_id="workspace",
        operation_id="operation",
        plan_digest="a" * 64,
        source_key="operation-step:original",
        source_fence=3,
        version=1,
        evidence_kind="planning-start-barrier-v1",
        planning_started=False,
        source_principal="invocation#1",
        workload_binding="original-pod",
        storage_uid="original-claim",
        volume_uid="original-volume",
        inspection_sha256="b" * 64,
        source_image="registry/worker@sha256:" + "c" * 64,
        checked_at=now.isoformat(),
        expires_at=(now + timedelta(minutes=30)).isoformat(),
    )
    identity = {k: value[k] for k in IDENTITY}
    value["record_id"] = record_id(identity)
    stored = copy.deepcopy(value)
    if change == "expired":
        stored["expires_at"] = (now - timedelta(seconds=1)).isoformat()
    elif change == "foreign-fence":
        stored["source_fence"] += 1
    elif change == "false-fence":
        stored["source_fence"] = True
    elif change == "started":
        stored["planning_started"] = True
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
            with pytest.raises(ValueError):
                owner.read_evidence(identity)


@pytest.mark.parametrize("change", [None, "region", "extra-source", "retained-derived", "plan", "digest", "same-source"])
def test_pricing_evidence_distinguishes_retained_files_from_derived_reports(change):
    from src.shared.domain_planning_recovery import _retained_saved_files

    original = {"workspace.tfplan", "workspace-plan.json", "workspace-inventory.json"}
    derived = {"workspace-estimate.json", "workspace-authorization.proposed.json"}
    files = {name: "a" * 64 for name in original | derived}
    pricing = dict(
        version=1,
        original_files={name: files[name] for name in original},
        replacements={"scripts/check_workspace_plan.py": "b" * 64, "regional-pricing.json": "c" * 64},
        original_module_sha256="d" * 64,
        corrected_module_sha256="e" * 64,
        refusal_sha256="f" * 64,
    )
    observation = {"files": files, "pricing_revalidation": pricing}
    boundary = {"entries": {"review/" + name: {"kind": "file", "sha256": files[name]} for name in original}}
    value = {"region": "us-west-2"}
    if change == "region":
        value["region"] = "us-east-1"
    elif change == "extra-source":
        pricing["replacements"]["main.tf"] = "a" * 64
    elif change == "retained-derived":
        boundary["entries"]["review/workspace-estimate.json"] = {"kind": "file", "sha256": "a" * 64}
    elif change == "plan":
        pricing["original_files"]["workspace.tfplan"] = "b" * 64
    elif change == "digest":
        pricing["refusal_sha256"] = "invalid"
    elif change == "same-source":
        pricing["corrected_module_sha256"] = pricing["original_module_sha256"]
    assert _retained_saved_files(boundary, observation, value) is (change is None)
