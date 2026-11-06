"""AWS contracts for exact-object, version-pinned storage access."""

import base64
import json
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

import boto3
import pytest
from botocore.config import Config
from fastapi import HTTPException

from src.knowledge import product_storage as storage
from src.knowledge import product_storage_routes as api
from src.knowledge.ingestion_authority import IngestionAuthority
from src.knowledge.ingestion_storage import object_command

KEY = "arn:aws:kms:us-east-1:123456789012:key/11111111-1111-1111-1111-111111111111"
OTHER_KEY = KEY.replace("11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222")
ROLE = "arn:aws:iam::123456789012:role/context-session"
VALUE = storage.Storage("tenant", "product", "fixture-context", ROLE, KEY, (KEY,))
OBJECT = VALUE.prefix + "uploads/receipt/doc.md"


def client():
    return boto3.client(
        "s3", region_name="us-east-1", aws_access_key_id="testing", aws_secret_access_key="testing", config=Config(signature_version="s3v4")
    )


@pytest.mark.parametrize("key", ["tenants/tenant/products/other/file", OBJECT + "*", OBJECT + "?", OBJECT + "/../x", OBJECT + "${x}"])
def test_policy_refuses_foreign_or_wildcard_objects(key):
    with pytest.raises(HTTPException):
        storage.session_policy(VALUE, key, write=True, encryption_key=KEY)


def test_read_policy_pins_version_and_exact_key():
    obj, kms = storage.session_policy(VALUE, OBJECT, version="v1", encryption_key=KEY)["Statement"]
    assert obj["Action"] == ["s3:GetObjectVersion"]
    assert obj["Resource"] == "arn:aws:s3:::fixture-context/" + OBJECT
    assert obj["Condition"] == {"StringEquals": {"s3:VersionId": "v1"}}
    assert kms["Resource"] == KEY and kms["Action"] == ["kms:Decrypt"]
    assert kms["Condition"]["StringEquals"]["kms:ViaService"] == "s3.us-east-1.amazonaws.com"
    write = storage.session_policy(VALUE, OBJECT, write=True, encryption_key=KEY)["Statement"][0]
    assert write["Condition"]["StringEquals"]["s3:x-amz-server-side-encryption-aws-kms-key-id"] == KEY


def test_sts_uses_product_tags_and_private_session_credentials(monkeypatch):
    sts, factory = Mock(), Mock(return_value="scoped-client")
    sts.assume_role.return_value = {
        "Credentials": {"AccessKeyId": "a", "SecretAccessKey": "b", "SessionToken": "c", "Expiration": datetime.now(UTC) + timedelta(minutes=15)}
    }
    monkeypatch.setattr(storage, "cloud", lambda _: sts)
    monkeypatch.setattr(storage.boto3, "client", factory)
    assert storage.s3_session(VALUE, OBJECT, encryption_key=KEY) == "scoped-client"
    args = sts.assume_role.call_args.kwargs
    assert args["RoleArn"] == ROLE and args["DurationSeconds"] == 900
    assert args["SourceIdentity"] == "knowledge-ingestion"
    assert args["Tags"] == [{"Key": "adp_tenant_id", "Value": "tenant"}, {"Key": "adp_product_id", "Value": "product"}]
    assert json.loads(args["Policy"])["Statement"][0]["Resource"].endswith(OBJECT)
    assert factory.call_args.kwargs["aws_session_token"] == "c"


def test_signed_put_binds_size_checksum_and_encryption(monkeypatch):
    s3 = client()
    monkeypatch.setattr(storage, "s3_session", lambda *a, **kw: s3)
    upload = {"object_key": OBJECT, "sha256": "ab" * 32, "content_length": 17, "content_type": "text/markdown", "kms_key_arn": KEY}
    link = api.upload_link(VALUE, upload)
    signed = parse_qs(urlsplit(link["url"]).query)
    assert signed["X-Amz-Expires"] == ["300"]
    headers = signed["X-Amz-SignedHeaders"][0].split(";")
    assert set(link["headers"]) - {"Content-Type"} <= set(headers)
    assert "content-length" in headers and "content-type" in headers
    assert link["headers"]["x-amz-checksum-sha256"] == base64.b64encode(bytes.fromhex("ab" * 32)).decode()
    assert link["headers"]["x-amz-server-side-encryption-bucket-key-enabled"] == "false"


@pytest.mark.parametrize("product,state,ok", [("other", "Enabled", False), ("product", "Disabled", False), ("product", "Enabled", True)])
def test_key_must_be_enabled_and_assigned_to_product(monkeypatch, product, state, ok):
    kms = Mock()
    kms.describe_key.return_value = {
        "KeyMetadata": {"Arn": OTHER_KEY, "KeyState": state, "KeyUsage": "ENCRYPT_DECRYPT", "KeySpec": "SYMMETRIC_DEFAULT", "KeyManager": "CUSTOMER"}
    }
    kms.list_resource_tags.return_value = {
        "Tags": [{"TagKey": "adp:tenant_id", "TagValue": "tenant"}, {"TagKey": "adp:product_id", "TagValue": product}]
    }
    monkeypatch.setattr(storage, "cloud", lambda _: kms)
    if ok:
        assert storage.validate_key(VALUE, None, managed=True) == OTHER_KEY
        kms.describe_key.assert_called_once_with(KeyId=storage.managed_alias("tenant", "product"))
    else:
        with pytest.raises(HTTPException):
            storage.validate_key(VALUE, OTHER_KEY)
    with pytest.raises(HTTPException):
        storage.validate_key(VALUE, OTHER_KEY.replace("123456789012", "999999999999"))


def test_historical_key_exception_is_only_aws_s3(monkeypatch):
    s3, kms = Mock(), Mock()
    s3.head_object.return_value = {"SSEKMSKeyId": OTHER_KEY}
    session = Mock(return_value=s3)
    monkeypatch.setattr(storage, "s3_session", session)
    monkeypatch.setattr(storage, "cloud", lambda _: kms)
    kms.describe_key.return_value = {"KeyMetadata": {"Arn": KEY, "KeyManager": "AWS"}}
    with pytest.raises(HTTPException):
        storage.read_session(VALUE, OBJECT, version="v1")
    kms.describe_key.assert_called_once_with(KeyId="alias/aws/s3")
    kms.describe_key.return_value = {"KeyMetadata": {"Arn": OTHER_KEY, "KeyManager": "AWS"}}
    storage.read_session(VALUE, OBJECT, version="v1")
    assert session.call_args.kwargs == {"version": "v1", "encryption_key": OTHER_KEY}


def test_worker_pins_original_version_and_encrypts_derived_content(monkeypatch):
    authority = Mock(spec=IngestionAuthority)
    authority.asset_type = "doc"
    authority.source_ref = "s3://fixture-context/" + OBJECT
    authority.source_object_version = "approved-version"
    authority.permits_object.return_value = True
    s3 = client()
    monkeypatch.setattr(storage, "read_session", Mock(return_value=(s3, {})))
    monkeypatch.setattr(storage, "s3_session", lambda *a, **kw: s3)
    monkeypatch.setenv("AGENT_CONTEXT_S3_BUCKET", VALUE.bucket)
    result = object_command(authority, "get", {"Bucket": VALUE.bucket, "Key": OBJECT}, storage=VALUE)
    assert parse_qs(urlsplit(result["url"]).query)["versionId"] == ["approved-version"]
    derived = VALUE.prefix + "content/assets/a/attempts/b/file.json"
    result = object_command(authority, "put", {"Bucket": VALUE.bucket, "Key": derived, "ContentLength": 25}, storage=VALUE)
    policy = json.loads(base64.b64decode(result["fields"]["policy"]))
    assert {"x-amz-server-side-encryption-aws-kms-key-id": KEY} in policy["conditions"]
    assert ["content-length-range", 25, 25] in policy["conditions"]


@pytest.mark.parametrize("operation,key", [("get", OBJECT), ("head", OBJECT), ("list", VALUE.prefix), ("list", VALUE.prefix + "uploads/")])
def test_workers_cannot_bypass_upload_verification(monkeypatch, operation, key):
    authority = Mock(spec=IngestionAuthority)
    authority.asset_type = "doc"
    authority.source_ref = "s3://" + VALUE.bucket + "/" + VALUE.prefix
    authority.source_object_version = None
    authority.permits_object.return_value = False
    aws = Mock(side_effect=AssertionError("An unverified upload must not reach AWS"))
    monkeypatch.setattr(storage, "read_session", aws)
    monkeypatch.setattr(storage, "s3_session", aws)
    monkeypatch.setenv("AGENT_CONTEXT_S3_BUCKET", VALUE.bucket)
    with pytest.raises(HTTPException) as error:
        object_command(authority, operation, {"Bucket": VALUE.bucket, "Prefix" if operation == "list" else "Key": key}, storage=VALUE)
    assert error.value.status_code == 403
    aws.assert_not_called()


@pytest.mark.parametrize("operation,key", [("get", OBJECT), ("list", "tenants/tenant/")])
def test_legacy_tenant_folders_cannot_use_ambient_credentials_for_products(monkeypatch, operation, key):
    from src.knowledge import ingestion_storage

    authority = Mock(spec=IngestionAuthority)
    authority.asset_type = "doc"
    authority.source_ref = "s3://" + VALUE.bucket + "/tenants/tenant/"
    authority.source_object_version = None
    authority.permits_object.return_value = False
    aws = Mock(side_effect=AssertionError("Product data requires a Product service session"))
    monkeypatch.setattr(ingestion_storage, "client", aws)
    monkeypatch.setenv("AGENT_CONTEXT_S3_BUCKET", VALUE.bucket)
    monkeypatch.setenv("PRODUCT_CONTEXT_STORAGE_ROLE_ARN", ROLE)
    with pytest.raises(HTTPException) as error:
        object_command(authority, operation, {"Bucket": VALUE.bucket, "Prefix" if operation == "list" else "Key": key})
    assert error.value.status_code == 403
    aws.assert_not_called()
