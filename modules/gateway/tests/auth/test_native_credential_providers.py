"""Real SDK request shapes with Stubber; never uses a live AWS identity."""

import hashlib
import time
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

import boto3
import pytest
from botocore.stub import ANY, Stubber
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from fastapi import HTTPException

from src.auth import native_credential_authority as authority

CONFIG = authority.NativeConfig(
    "https://gateway.example.test",
    "test-key",
    "arn:aws:kms:us-east-1:123456789012:key/00000000-0000-0000-0000-000000000001",
    "https://browser.example.test",
    "native",
    "browser",
)


def client(service):
    return boto3.client(service, region_name="us-east-1", aws_access_key_id="testing", aws_secret_access_key="testing")


def test_real_kms_public_key_and_digest_signing_contract(monkeypatch):
    providers = authority.NativeProviders()
    kms = client("kms")
    monkeypatch.setattr(providers, "client", lambda name: kms)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    der = key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    message = b"offline-encrypted-envelope" * 1000  # KMS RAW's 4096 limit must not truncate it.
    signature = key.sign(message, padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32), hashes.SHA256())
    with Stubber(kms) as stub:
        stub.add_response(
            "get_public_key",
            dict(KeyId=CONFIG.key_arn, PublicKey=der, KeyUsage="SIGN_VERIFY", SigningAlgorithms=["RSASSA_PSS_SHA_256"]),
            {"KeyId": CONFIG.key_arn},
        )
        stub.add_response(
            "sign",
            dict(KeyId=CONFIG.key_arn, Signature=signature, SigningAlgorithm="RSASSA_PSS_SHA_256"),
            dict(KeyId=CONFIG.key_arn, Message=hashlib.sha256(message).digest(), MessageType="DIGEST", SigningAlgorithm="RSASSA_PSS_SHA_256"),
        )
        pem = providers.public_key(CONFIG)
        result = providers.sign(CONFIG, message)
        serialization.load_pem_public_key(pem.encode()).verify(
            result, message, padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32), hashes.SHA256()
        )
        stub.assert_no_pending_responses()


@pytest.mark.parametrize("change", ["key", "usage", "algorithm", "size"])
def test_wrong_kms_key_or_capability_refused(monkeypatch, change):
    providers = authority.NativeProviders()
    kms = client("kms")
    monkeypatch.setattr(providers, "client", lambda name: kms)
    key = rsa.generate_private_key(public_exponent=65537, key_size=1024 if change == "size" else 2048)
    result = dict(
        KeyId=CONFIG.key_arn,
        PublicKey=key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo),
        KeyUsage="SIGN_VERIFY",
        SigningAlgorithms=["RSASSA_PSS_SHA_256"],
    )
    if change == "key":
        result["KeyId"] = CONFIG.key_arn + "different"
    if change == "usage":
        result["KeyUsage"] = "ENCRYPT_DECRYPT"
    if change == "algorithm":
        result["SigningAlgorithms"] = ["RSASSA_PKCS1_V1_5_SHA_256"]
    with Stubber(kms) as stub:
        stub.add_response("get_public_key", result, {"KeyId": CONFIG.key_arn})
        with pytest.raises(HTTPException):
            providers.public_key(CONFIG)


def test_assume_role_uses_exact900_and_human_receipt_tags(monkeypatch):
    providers = authority.NativeProviders()
    sts = client("sts")
    monkeypatch.setattr(providers, "client", lambda name: sts)
    role = "arn:aws:iam::123456789012:role/OfflineRole"
    expected = dict(
        RoleArn=role,
        ExternalId="external-id",
        RoleSessionName=ANY,
        DurationSeconds=900,
        Tags=[
            {"Key": "adp:user_id", "Value": "canonical-human"},
            {"Key": "adp:origin_kind", "Value": "human"},
            {"Key": "adp:receipt_id", "Value": "receipt"},
        ],
    )
    response = dict(
        Credentials=dict(
            AccessKeyId="ASIAOFFLINEEXAMPLE", SecretAccessKey="fictional-secret", SessionToken="fictional-session", Expiration=datetime.now(UTC)
        ),
        AssumedRoleUser=dict(AssumedRoleId="offline:id", Arn="arn:aws:sts::123456789012:assumed-role/OfflineRole/session"),
    )
    with Stubber(sts) as stub:
        stub.add_response("assume_role", response, expected)
        assert providers.assume(role_arn=role, external_id="external-id", user_id="canonical-human", receipt_id="receipt") == response["Credentials"]
        stub.assert_no_pending_responses()


def test_get_user_live_revocation_boundary_and_no_identity_substitution(monkeypatch):
    providers = authority.NativeProviders()
    cognito = client("cognito-idp")
    monkeypatch.setattr(providers, "client", lambda name: cognito)
    actor = authority.NativeActor("subject", "tenant", "native", "session", int(time.time()) + 60, "offline-token")
    with Stubber(cognito) as stub:
        stub.add_response(
            "get_user", dict(Username="offline", UserAttributes=[{"Name": "sub", "Value": "subject"}]), {"AccessToken": "offline-token"}
        )
        providers.current_session(actor)
        stub.add_response("get_user", dict(Username="offline", UserAttributes=[{"Name": "sub", "Value": "other"}]), {"AccessToken": "offline-token"})
        with pytest.raises(HTTPException):
            providers.current_session(actor)
        stub.add_response(
            "get_user", dict(Username="offline", UserAttributes=[{"Name": "sub", "Value": "subject"}]), {"AccessToken": "offline-token"}
        )
        with pytest.raises(HTTPException):
            providers.current_session(replace(actor, expires_at=1))


def test_secret_read_exact_version_and_region(monkeypatch):
    providers = authority.NativeProviders()
    sm = client("secretsmanager")
    chosen = []

    def choose(service, region):
        chosen.append((service, region))
        return sm

    monkeypatch.setattr(providers, "client", choose)
    arn = "arn:aws:secretsmanager:eu-west-1:123456789012:secret:offline"
    version = "0" * 32
    with Stubber(sm) as stub:
        stub.add_response("describe_secret", {"VersionIdsToStages": {version: ["AWSCURRENT"]}}, {"SecretId": arn})
        assert providers.secret_version(arn) == version
        stub.add_response("get_secret_value", {"VersionId": version, "SecretString": "offline-file"}, {"SecretId": arn, "VersionId": version})
        assert providers.secret(arn, version) == b"offline-file"
    assert chosen == [("secretsmanager", "eu-west-1"), ("secretsmanager", "eu-west-1")]


def test_sdk_effects_have_no_automatic_retry(monkeypatch):
    observed = []
    monkeypatch.setattr(authority.boto3, "client", lambda *args, **kwargs: observed.append(kwargs))
    authority.NativeProviders().client("sts")
    assert observed[0]["config"].retries == {"total_max_attempts": 1}


@pytest.mark.parametrize("missing", ["enabled", "issuer", "origin", "key", "same_client"])
def test_configuration_is_disabled_until_every_prerequisite_exists(monkeypatch, missing):
    values = dict(
        ADP_NATIVE_CREDENTIALS_ENABLED="true",
        ADP_NATIVE_CREDENTIALS_ISSUER=CONFIG.issuer,
        ADP_NATIVE_CREDENTIALS_APPROVAL_ORIGIN=CONFIG.approval_origin,
        ADP_NATIVE_CREDENTIALS_KEY_ID=CONFIG.key_id,
        ADP_NATIVE_CREDENTIALS_KEY_ARN=CONFIG.key_arn,
    )
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(
        authority,
        "get_settings",
        lambda: SimpleNamespace(cognito_cli_client_id="native", cognito_client_id="native" if missing == "same_client" else "browser"),
    )
    if missing == "enabled":
        monkeypatch.delenv("ADP_NATIVE_CREDENTIALS_ENABLED")
    if missing == "issuer":
        monkeypatch.setenv("ADP_NATIVE_CREDENTIALS_ISSUER", "http://gateway.example.test")
    if missing == "origin":
        monkeypatch.setenv("ADP_NATIVE_CREDENTIALS_APPROVAL_ORIGIN", CONFIG.approval_origin + "/path")
    if missing == "key":
        monkeypatch.setenv("ADP_NATIVE_CREDENTIALS_KEY_ARN", "alias/offline")
    with pytest.raises(HTTPException) as error:
        authority.NativeConfig.configured()
    assert error.value.status_code == 503
