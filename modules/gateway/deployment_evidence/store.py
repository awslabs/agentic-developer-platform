"""S3 deployment evidence bound to a GitHub-signed, content-specific OIDC token.

The token is retained as a historical signature, never used as an AWS credential.
The audience binds exact bytes; repository/run/attempt/workflow claims bind origin.
S3 supplies the publication time, version, encryption and immutable object identity.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from datetime import UTC, datetime
from urllib.parse import quote

ISSUER = "https://token.actions.githubusercontent.com"
TRANSPORT = "s3-oidc-v1"
MAX_PAYLOAD = 65536
MAX_ENVELOPE = 128 * 1024
PREFIX = "deployment-evidence/v1"


def bucket_name(account, environment):
    if not re.fullmatch(r"[0-9]{12}", account) or not re.fullmatch(r"[a-z][a-z0-9-]{0,23}", environment):
        raise ValueError("invalid evidence target")
    return f"adp-{environment}-deployment-evidence-{account}"


def object_key(repository_id, run_id, attempt, kind, name):
    if any(type(v) is not int or v <= 0 for v in (repository_id, run_id, attempt)):
        raise ValueError("invalid evidence run identity")
    if kind == "context":
        if name not in {"gateway-deploy.yml", "run-gateway-migrations.yml"}:
            raise ValueError("unsupported context workflow")
    elif kind == "release":
        if name not in {"gateway-backend", "gateway-frontend", "gateway-migrations"}:
            raise ValueError("unsupported release component")
    else:
        raise ValueError("unsupported evidence kind")
    return f"{PREFIX}/{repository_id}/{run_id}/{attempt}/{kind}/{name}.json"


def audience(payload):
    return "adp-deployment-evidence:sha256:" + hashlib.sha256(payload).hexdigest()


def envelope(payload, token):
    if not 0 < len(payload) <= MAX_PAYLOAD:
        raise ValueError("evidence payload size invalid")
    raw = json.dumps({"transport": TRANSPORT, "payload": base64.b64encode(payload).decode(), "signature": token}, sort_keys=True).encode()
    if len(raw) > MAX_ENVELOPE:
        raise ValueError("evidence envelope too large")
    return raw


def verify(raw, *, signing_key, repository, repository_id, run_id, attempt, workflow_path, workflow_revision, published_at):
    import jwt

    if not 0 < len(raw) <= MAX_ENVELOPE:
        raise ValueError("evidence envelope size invalid")
    doc = json.loads(raw)
    if set(doc) != {"transport", "payload", "signature"} or doc["transport"] != TRANSPORT:
        raise ValueError("evidence envelope invalid")
    payload = base64.b64decode(doc["payload"], validate=True)
    if not 0 < len(payload) <= MAX_PAYLOAD:
        raise ValueError("evidence payload size invalid")
    # Expiry describes token use during publication, not the lifetime of retained
    # evidence. Its signature and all origin claims remain mandatory on every read.
    claims = jwt.decode(
        doc["signature"],
        signing_key,
        algorithms=["RS256"],
        issuer=ISSUER,
        audience=audience(payload),
        options={
            "verify_exp": False,
            "strict_aud": True,
            "require": ["exp", "iat", "nbf", "iss", "aud", "repository", "repository_id", "run_id", "run_attempt", "workflow_ref", "workflow_sha"],
        },
    )
    expected = {"repository": repository, "repository_id": str(repository_id), "run_id": str(run_id), "run_attempt": str(attempt)}
    if any(claims.get(k) != v for k, v in expected.items()):
        raise ValueError("evidence signer run identity mismatch")
    path = repository + "/" + workflow_path + "@"
    # Reusable migrations are signed by their callee identity; never let a caller
    # claim another workflow merely by changing the JSON workflow_path.
    ref_field, sha_field = ("job_workflow_ref", "job_workflow_sha") if "job_workflow_ref" in claims else ("workflow_ref", "workflow_sha")
    if not claims.get(ref_field, "").startswith(path) or claims.get(sha_field) != workflow_revision:
        raise ValueError("evidence signer workflow mismatch")
    issued, expires, not_before = (claims[k] for k in ("iat", "exp", "nbf"))
    if any(type(v) is not int for v in (issued, expires, not_before)) or not 0 < expires - issued <= 3600:
        raise ValueError("evidence signature lifetime invalid")
    timestamp = published_at.timestamp()
    if not max(issued, not_before) - 30 <= timestamp <= expires or timestamp > datetime.now(UTC).timestamp() + 30:
        raise ValueError("evidence was not published during signature validity")
    return payload


class EvidenceStore:
    def __init__(self, *, s3=None, keys=None):
        self.s3 = s3
        self.keys = keys

    def read(self, *, account, environment, region, repository, repository_id, run_id, attempt, kind, name, workflow_path, workflow_revision):
        import boto3
        import jwt
        from botocore.exceptions import ClientError

        bucket = bucket_name(account, environment)
        key = object_key(repository_id, run_id, attempt, kind, name)
        s3 = self.s3 or boto3.client("s3", region_name=region)
        try:
            response = s3.get_object(Bucket=bucket, Key=key, ExpectedBucketOwner=account)
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "NoSuchKey":
                return None
            if exc.response["Error"]["Code"] == "AccessDenied":
                # Prefix-scoped ListBucket does not necessarily permit S3's
                # implicit existence check for GetObject. Prove absence with an
                # explicit, authorized listing; never treat denial alone as pending.
                listing = s3.list_objects_v2(Bucket=bucket, Prefix=key, MaxKeys=1, ExpectedBucketOwner=account)
                if not any(item["Key"] == key for item in listing.get("Contents", [])):
                    return None
            raise
        try:
            if not 0 < response["ContentLength"] <= MAX_ENVELOPE:
                raise ValueError("evidence object size invalid")
            raw = response["Body"].read(MAX_ENVELOPE + 1)
        finally:
            response["Body"].close()
        version = response.get("VersionId")
        if not version or version == "null" or response.get("ServerSideEncryption") != "AES256":
            raise ValueError("evidence object must be versioned and encrypted")
        if len(raw) != response["ContentLength"]:
            raise ValueError("evidence object length mismatch")
        doc = json.loads(raw)
        token = doc["signature"]
        header = jwt.get_unverified_header(token)
        if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str):
            raise ValueError("evidence signature header invalid")
        # The issuer endpoint is fixed, never obtained from the envelope/header.
        keys = self.keys or jwt.PyJWKClient(ISSUER + "/.well-known/jwks", timeout=10)
        signing_key = keys.get_signing_key_from_jwt(token).key
        payload = verify(
            raw,
            signing_key=signing_key,
            repository=repository,
            repository_id=repository_id,
            run_id=run_id,
            attempt=attempt,
            workflow_path=workflow_path,
            workflow_revision=workflow_revision,
            published_at=response["LastModified"],
        )
        return payload, hashlib.sha256(payload).hexdigest(), f"s3://{bucket}/{key}?versionId={quote(version, safe='')}"
