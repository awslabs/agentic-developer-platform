"""Expired owner evidence is history; only a fresh, unchanged successor authorizes."""

import copy
from datetime import UTC, datetime, timedelta

import boto3
import pytest
from moto import mock_aws

from src.internal import domain_bootstrap_recovery as reader
from src.shared import domain_bootstrap_evidence as contract
from src.shared.domain_bootstrap_boundary import QUALIFIED_SOURCES
from src.shared.domain_bootstrap_recovery import validate


def proof(*, age=0):
    now = datetime.now(UTC) - timedelta(hours=age, minutes=1)
    image, source = next(iter(QUALIFIED_SOURCES.items()))
    value = dict(
        org_id="org",
        workspace_id="workspace",
        operation_id="operation",
        plan_digest="a" * 64,
        source_key="original",
        source_fence=3,
        version=1,
        evidence_kind="bootstrap-journal-barrier-v1",
        source_image="registry/worker@" + image,
        source_revision=source["revision"],
        source_hashes=source["hashes"],
        source_principal="invocation#1",
        source_acquired_at="2026-01-01T00:00:00+00:00",
        source_pod_name="original-pod",
        source_pod_uid="original-uid",
        workload_binding="original-uid",
        source_audit={"log_group": "original-log", "creation_event_id": "create", "terminal_event_ids": ["terminal"]},
        installation_id="installation",
        credential_id="credential",
        account_id="123456789012",
        region="us-east-1",
        adp_org_id="tenant",
        policy_sha256="b" * 64,
        checked_at=now.isoformat(),
        expires_at=(now + timedelta(hours=1)).isoformat(),
    )
    value["record_id"] = contract.record_id(value)
    return value


def item(value):
    return {"record_id": {"S": value["record_id"]}, "document": {"S": contract.canonical(value)}, "revision": {"S": contract.digest(value)}}


def append(predecessor, *, age=0):
    value = copy.deepcopy(predecessor)
    now = datetime.now(UTC) - timedelta(hours=age, minutes=1)
    value.update(checked_at=now.isoformat(), expires_at=(now + timedelta(hours=1)).isoformat())
    value["record_id"], value["renewal"] = contract.successor(predecessor)
    return value


@pytest.mark.parametrize("generations", [0, 1, 3, 8])
def test_consistent_reader_validates_history_and_selects_fresh_leaf_without_changing_old_bytes(monkeypatch, generations):
    records = [proof(age=generations)]
    for n in range(generations):
        records.append(append(records[-1], age=generations - n - 1))
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        client.create_table(
            TableName="authority",
            KeySchema=[{"AttributeName": "record_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "record_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        originals = [item(value) for value in records]
        for value in originals:
            client.put_item(TableName="authority", Item=value)
        reads = []
        original_get = client.get_item

        def get(**kwargs):
            assert kwargs["ConsistentRead"] is True
            reads.append(kwargs["Key"]["record_id"]["S"])
            return original_get(**kwargs)

        monkeypatch.setattr(client, "get_item", get)
        monkeypatch.setattr(reader, "aws_client", lambda _: client)
        monkeypatch.setattr(reader, "tables", lambda: ("authority", "unused"))
        assert reader.read_evidence(records[0]) == records[-1]
        assert reads == [r["record_id"] for r in records] + [contract.successor(records[-1])[0]]
        assert sorted(client.scan(TableName="authority")["Items"], key=str) == sorted(originals, key=str)


@pytest.mark.parametrize("fault", ["expired", "expired-successor", "source", "revision", "scalar", "child-malformed", "overflow"])
def test_reader_refuses_expired_or_invalid_history_and_never_hides_a_child(monkeypatch, fault):
    base = proof(age=2 if fault == "expired-successor" else 1 if fault in {"expired", "source"} else 0)
    if fault == "source":
        base["source_revision"] = "unqualified"
    records = [base]
    if fault in {"expired-successor", "source"}:
        records.append(append(base, age=1 if fault == "expired-successor" else 0))
    if fault == "overflow":
        records = [proof(age=9)]
        for n in range(9):
            records.append(append(records[-1], age=8 - n))
    items = {v["record_id"]: item(v) for v in records}
    if fault == "revision":
        items[base["record_id"]]["revision"]["S"] = "changed"
    elif fault == "scalar":
        items[base["record_id"]].update(document={"S": "[]"}, revision={"S": contract.digest([])})
    elif fault == "child-malformed":
        items[contract.successor(base)[0]] = {}

    class Client:
        def get_item(self, **kwargs):
            assert kwargs["ConsistentRead"] is True
            value = items.get(kwargs["Key"]["record_id"]["S"])
            return {} if value is None else {"Item": value}

    monkeypatch.setattr(reader, "aws_client", lambda _: Client())
    monkeypatch.setattr(reader, "tables", lambda: ("authority", "unused"))
    with pytest.raises(ValueError):
        reader.read_evidence(base)


@pytest.mark.parametrize(
    "fault",
    [
        "identity",
        "principal",
        "policy",
        "closure",
        "terms",
        "extra-field",
        "audit-log",
        "audit-creation",
        "audit-terminal",
        "audit-duplicate",
        "audit-unsorted",
        "failure-event",
        "overlap",
        "future",
        "lifetime",
        "generation",
        "boolean-generation",
        "predecessor",
        "predecessor-revision",
        "extra-renewal",
        "record-key",
        "fence-bool",
    ],
)
def test_successor_freezes_source_closure_terms_and_chronology(fault):
    base = proof(age=2)
    base["precomponent_failures"] = {"one": {"event_id": "original", "source_audit_sha256": "old"}}
    value = append(base)
    if fault == "identity":
        value["operation_id"] = "foreign"
    elif fault in {"principal", "policy", "closure", "terms", "extra-field"}:
        key = {
            "principal": "source_principal",
            "policy": "policy_sha256",
            "closure": "settled_bootstrap",
            "terms": "attempt_extension",
            "extra-field": "new",
        }[fault]
        value[key] = "changed"
    elif fault.startswith("audit-"):
        key, changed = {
            "audit-log": ("log_group", "other"),
            "audit-creation": ("creation_event_id", "other"),
            "audit-terminal": ("terminal_event_ids", ["other"]),
            "audit-duplicate": ("terminal_event_ids", ["terminal", "terminal"]),
            "audit-unsorted": ("terminal_event_ids", ["terminal", "a"]),
        }[fault]
        value["source_audit"][key] = changed
    elif fault == "failure-event":
        value["precomponent_failures"]["one"]["event_id"] = "changed"
    elif fault == "overlap":
        value["checked_at"] = base["checked_at"]
        value["expires_at"] = base["expires_at"]
    elif fault == "future":
        value["checked_at"] = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    elif fault == "lifetime":
        value["expires_at"] = (datetime.now(UTC) + timedelta(hours=2)).isoformat()
    elif fault == "generation":
        value["renewal"]["generation"] = 2
    elif fault == "boolean-generation":
        value["renewal"]["generation"] = True
    elif fault == "predecessor":
        value["renewal"]["predecessor_record_id"] = "other"
    elif fault == "predecessor-revision":
        value["renewal"]["predecessor_revision"] = "c" * 64
    elif fault == "extra-renewal":
        value["renewal"]["nonce"] = "arbitrary"
    elif fault == "record-key":
        value["record_id"] = base["record_id"]
    elif fault == "fence-bool":
        value["source_fence"] = True
    with pytest.raises(ValueError):
        contract.framing(value, require_fresh=False)
        contract.linked(base, value)


def test_fresh_collection_can_extend_audit_but_not_reuse_a_live_predecessor():
    base = proof(age=2)
    fresh = proof()
    fresh["source_audit"]["terminal_event_ids"] = ["additional", "terminal"]
    fresh["source_audit"]["historical_environment"] = {"snapshot_sha256": "d" * 64}
    original = copy.deepcopy(base)
    successor = contract.renew(fresh, base)
    assert validate(successor) == successor
    assert base == original and "renewal" not in fresh
    with pytest.raises(ValueError):
        contract.renew(fresh, proof())


def test_complete_large_owner_history_keeps_record_identity_and_revision():
    value = proof()
    value["retained_history_example"] = "unchanged original journal; original approval; " * 35000
    document = contract.encode_document(value)
    assert len(contract.canonical(value).encode()) > 1024 * 1024
    assert len(document.encode()) <= contract.MAX_STORED_BYTES
    stored = {**item(value), "document": {"S": document}}
    assert contract.decode_item(stored, value["record_id"]) == value
    assert stored["revision"]["S"] == contract.digest(value)
    legacy = proof()
    legacy["retained_history_example"] = "x" * 390000
    assert contract.MAX_STORED_BYTES < len(contract.canonical(legacy).encode()) < 400 * 1024
    assert contract.decode_item(item(legacy), legacy["record_id"]) == legacy
    small = proof()
    assert contract.encode_document(small) == contract.canonical(small)
    assert contract.decode_item(item(small), small["record_id"]) == small
    stored["revision"]["S"] = "f" * 64
    with pytest.raises(ValueError):
        contract.decode_item(stored, value["record_id"])


def test_owner_history_compression_refuses_expansion_and_uncompressible_oversize():
    import base64
    import gzip
    import random

    with pytest.raises(ValueError):
        contract.encode_document({"oversize": "x" * (contract.MAX_DOCUMENT_BYTES + 1)})
    document = contract.canonical(
        {"encoding": "gzip-base64-v1", "value": base64.b64encode(gzip.compress(b"x" * (contract.MAX_DOCUMENT_BYTES + 1))).decode()}
    )
    with pytest.raises(ValueError):
        contract.decode_document(document)
    with pytest.raises(ValueError):
        contract.encode_document({"large": random.Random(42).randbytes(400000).hex()})
