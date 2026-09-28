"""Operator exports stay scoped and never become a general conversation reader."""

import gzip
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

path = Path(__file__).resolve().parents[2] / "scripts/gpt6-receipt-export.py"
spec = importlib.util.spec_from_file_location("receipt_export", path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def exported(**overrides):
    log = dict.fromkeys(module.FIELDS)
    log.update(org_id="tenant", request_id="request", user_id="worker", pricing_decision={"ledger_cost_usd": "1.00"})
    manifest = {"account_id": "123456789012", "org_id": "tenant", "bucket": "logs", "receipts": {"tenant/receipt.json": {"log": log}}}
    manifest.update(overrides)
    data = gzip.compress(json.dumps(manifest).encode())
    return data, hashlib.sha256(data).hexdigest()


def reader(data, digest, **overrides):
    scope = dict(account_id="123456789012", org_id="tenant", bucket="logs")
    scope.update(overrides)
    return module.ExportReader(data, digest, **scope)


def test_export_returns_only_reviewed_receipt_and_pricing_fields():
    source = reader(*exported())
    result = source.get_object(Bucket="logs", Key="tenant/receipt.json", ExpectedBucketOwner="123456789012")
    assert set(json.load(result["Body"])) == set(module.FIELDS)
    with pytest.raises(ClientError):
        source.get_object(Bucket="logs", Key="unreviewed/receipt.json", ExpectedBucketOwner="123456789012")


def test_export_cannot_be_replaced_after_hash_review():
    data, digest = exported()
    with pytest.raises(ValueError, match="hash mismatch"):
        reader(data + b"changed", digest)


@pytest.mark.parametrize("scope", [{"account_id": "other"}, {"org_id": "other"}, {"bucket": "other"}])
def test_export_cannot_cross_scope(scope):
    with pytest.raises(ValueError, match="scope mismatch"):
        reader(*exported(), **scope)


def test_read_cannot_switch_bucket_or_owner():
    source = reader(*exported())
    with pytest.raises(ValueError, match="scope mismatch"):
        source.get_object(Bucket="elsewhere", Key="tenant/receipt.json", ExpectedBucketOwner="123456789012")
    with pytest.raises(ValueError, match="scope mismatch"):
        source.get_object(Bucket="logs", Key="tenant/receipt.json", ExpectedBucketOwner="other")


def test_conversation_fields_are_rejected():
    source = reader(*exported())
    source.manifest["receipts"]["tenant/receipt.json"]["log"]["messages"] = ["not pricing evidence"]
    with pytest.raises(ValueError, match="unexpected receipt fields"):
        source.get_object(Bucket="logs", Key="tenant/receipt.json", ExpectedBucketOwner="123456789012")
