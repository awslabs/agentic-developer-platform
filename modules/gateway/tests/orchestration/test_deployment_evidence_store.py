"""Real signatures protect S3 bytes even from an unrelated bucket writer."""

import base64
import hashlib
import importlib.util
import io
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import jwt
import pytest
from botocore.exceptions import ClientError
from cryptography.hazmat.primitives.asymmetric import rsa

from deployment_evidence.store import ISSUER, MAX_ENVELOPE, EvidenceStore, audience, bucket_name, envelope, object_key, verify

ROOT = Path(__file__).resolve().parents[4]
spec = importlib.util.spec_from_file_location("evidence_publisher", ROOT / "modules/gateway/scripts/publish-deployment-evidence.py")
publisher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publisher)


@pytest.fixture
def signed():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    # Retained evidence is still valid after the original short-lived token expired.
    issued = int((datetime.now(UTC) - timedelta(days=2)).timestamp())
    claims = dict(
        iss=ISSUER,
        aud=audience(b'{"verified":true}'),
        iat=issued,
        nbf=issued,
        exp=issued + 300,
        repository="org/repo",
        repository_id="17",
        run_id="42",
        run_attempt="2",
        workflow_ref="org/repo/.github/workflows/gateway-deploy.yml@refs/heads/main",
        workflow_sha="a" * 40,
    )
    expected = dict(
        repository="org/repo",
        repository_id=17,
        run_id=42,
        attempt=2,
        workflow_path=".github/workflows/gateway-deploy.yml",
        workflow_revision="a" * 40,
        published_at=datetime.fromtimestamp(issued + 30, UTC),
    )
    return SimpleNamespace(key=key, claims=claims, expected=expected, payload=b'{"verified":true}')


def package(ctx):
    return envelope(ctx.payload, jwt.encode(ctx.claims, ctx.key, algorithm="RS256", headers={"kid": "test"}))


def test_retained_signature_is_valid_after_bearer_expiry(signed):
    assert verify(package(signed), signing_key=signed.key.public_key(), **signed.expected) == signed.payload


@pytest.mark.parametrize(
    "claim,value",
    [
        ("iss", "https://attacker.invalid"),
        ("aud", "sts.amazonaws.com"),
        ("repository", "other/repo"),
        ("repository_id", "99"),
        ("run_id", "99"),
        ("run_attempt", "1"),
        ("workflow_sha", "b" * 40),
        ("workflow_ref", "org/repo/.github/workflows/unreviewed.yml@refs/heads/main"),
        ("job_workflow_ref", "org/repo/.github/workflows/unreviewed.yml@refs/heads/main"),
    ],
)
def test_other_workflow_run_or_audience_cannot_sign_this_evidence(signed, claim, value):
    signed.claims[claim] = value
    with pytest.raises((ValueError, jwt.PyJWTError)):
        verify(package(signed), signing_key=signed.key.public_key(), **signed.expected)


def test_reusable_workflow_requires_exact_callee_revision(signed):
    signed.expected["workflow_path"] = ".github/workflows/run-gateway-migrations.yml"
    signed.claims.update(job_workflow_ref="org/repo/.github/workflows/run-gateway-migrations.yml@refs/heads/main", job_workflow_sha="a" * 40)
    assert verify(package(signed), signing_key=signed.key.public_key(), **signed.expected) == signed.payload
    signed.claims["job_workflow_sha"] = "b" * 40
    with pytest.raises(ValueError, match="workflow"):
        verify(package(signed), signing_key=signed.key.public_key(), **signed.expected)


@pytest.mark.parametrize("delta", [-60, 301])
def test_object_written_outside_token_validity_is_refused(signed, delta):
    signed.expected["published_at"] = datetime.fromtimestamp(signed.claims["iat"] + delta, UTC)
    with pytest.raises(ValueError, match="published"):
        verify(package(signed), signing_key=signed.key.public_key(), **signed.expected)


def test_modified_bytes_and_forged_signature_are_refused(signed):
    raw = json.loads(package(signed))
    raw["payload"] = base64.b64encode(b'{"verified":false}').decode()
    with pytest.raises(jwt.InvalidAudienceError):
        verify(json.dumps(raw).encode(), signing_key=signed.key.public_key(), **signed.expected)
    wrong = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(jwt.InvalidSignatureError):
        verify(package(signed), signing_key=wrong.public_key(), **signed.expected)


def reader(signed, **changes):
    raw = package(signed)
    response = dict(
        ContentLength=len(raw),
        Body=io.BytesIO(raw),
        VersionId="immutable/version",
        ServerSideEncryption="AES256",
        LastModified=signed.expected["published_at"],
    )
    response.update(changes)
    calls = []

    def get(**kw):
        calls.append(kw)
        return response

    store = EvidenceStore(
        s3=SimpleNamespace(get_object=get), keys=SimpleNamespace(get_signing_key_from_jwt=lambda _: SimpleNamespace(key=signed.key.public_key()))
    )
    expected = {k: v for k, v in signed.expected.items() if k != "published_at"}
    expected.update(account="123456789012", environment="dev", region="us-east-1", kind="context", name="gateway-deploy.yml")
    return store, expected, calls, response


def test_reads_exact_owned_object_and_retains_version(signed):
    store, args, calls, response = reader(signed)
    payload, digest, reference = store.read(**args)
    assert payload == signed.payload and digest == hashlib.sha256(payload).hexdigest()
    assert calls == [
        dict(
            Bucket="adp-dev-deployment-evidence-123456789012",
            Key="deployment-evidence/v1/17/42/2/context/gateway-deploy.yml.json",
            ExpectedBucketOwner="123456789012",
        )
    ]
    assert reference.endswith("?versionId=immutable%2Fversion") and response["Body"].closed


@pytest.mark.parametrize(
    "changes", [{"VersionId": "null"}, {"VersionId": None}, {"ServerSideEncryption": None}, {"ContentLength": MAX_ENVELOPE + 1}, {"ContentLength": 1}]
)
def test_unversioned_unencrypted_or_unbounded_objects_refused(signed, changes):
    store, args, _, response = reader(signed, **changes)
    with pytest.raises(ValueError):
        store.read(**args)
    assert response["Body"].closed


def test_missing_object_is_pending_but_access_denial_is_not(signed):
    store, args, _, _ = reader(signed)
    for code in ("NoSuchKey", "AccessDenied", "NoSuchBucket"):

        def get(**_):
            raise ClientError({"Error": {"Code": code}}, "GetObject")

        store.s3 = SimpleNamespace(get_object=get, list_objects_v2=get)
        if code == "NoSuchKey":
            assert store.read(**args) is None
        else:
            with pytest.raises(ClientError):
                store.read(**args)


@pytest.mark.parametrize("exists", [False, True])
def test_prefix_listing_distinguishes_missing_object_from_denied_existing_object(signed, exists):
    store, args, _, _ = reader(signed)
    key = object_key(17, 42, 2, "context", "gateway-deploy.yml")

    def get(**_):
        raise ClientError({"Error": {"Code": "AccessDenied"}}, "GetObject")

    def listing(**kw):
        assert kw == dict(Bucket=bucket_name(args["account"], args["environment"]), Prefix=key, MaxKeys=1, ExpectedBucketOwner=args["account"])
        return {"Contents": [{"Key": key}]} if exists else {}

    store.s3 = SimpleNamespace(get_object=get, list_objects_v2=listing)
    if exists:
        with pytest.raises(ClientError):
            store.read(**args)
    else:
        assert store.read(**args) is None


def test_key_and_bucket_inputs_cannot_escape_selected_scope():
    for environment in ("../prod", "dev/other", "", "DEV"):
        with pytest.raises(ValueError):
            bucket_name("123456789012", environment)
    for name in ("../gateway-deploy.yml", "other.yml", "gateway-deploy.yml/extra"):
        with pytest.raises(ValueError):
            object_key(17, 42, 2, "context", name)


def test_publisher_creates_only_and_never_logs_or_passes_token_in_argv(tmp_path):
    doc = dict(
        repository_id=17,
        run_id=42,
        run_attempt=2,
        account_id="123456789012",
        workflow_revision="a" * 40,
        workflow_path=".github/workflows/gateway-deploy.yml",
    )
    path = tmp_path / "context.json"
    path.write_text(json.dumps(doc))
    env = dict(
        GITHUB_REPOSITORY_ID="17",
        GITHUB_RUN_ID="42",
        GITHUB_RUN_ATTEMPT="2",
        ACCOUNT_ID="123456789012",
        ENVIRONMENT="dev",
        ADP_WORKFLOW_REVISION="a" * 40,
    )
    calls = []

    def aws(parts):
        calls.append(parts)
        if parts[0] == "sts":
            return {"Account": env["ACCOUNT_ID"]}
        stored = json.loads(Path(parts[parts.index("--body") + 1]).read_text())
        assert stored["signature"] == "private-signed-token"
        assert parts[parts.index("--if-none-match") + 1] == "*"
        assert parts[parts.index("--expected-bucket-owner") + 1] == env["ACCOUNT_ID"]
        return {"VersionId": "one-version"}

    result = publisher.publish(env, path, "context", "gateway-deploy.yml", aws=aws, request_token=lambda aud: "private-signed-token")
    assert "private-signed-token" not in json.dumps([calls, result])
    doc["run_attempt"] = 1
    path.write_text(json.dumps(doc))
    calls.clear()
    with pytest.raises(ValueError, match="this deployment"):
        publisher.publish(env, path, "context", "gateway-deploy.yml", aws=aws, request_token=lambda _: pytest.fail("must not sign"))
    assert calls == []
