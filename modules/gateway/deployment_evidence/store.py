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
MAX_SCAN_ARCHIVE = 16 * 1024 * 1024
PREFIX = "deployment-evidence/v1"
# Legacy release components remain explicitly registered. Workflow contexts and
# version-2 delivery artifacts are generic: the verified definition and GitHub
# signature, rather than a product-specific workflow list, authorize their origin.
CONTEXT_WORKFLOWS = frozenset(
    {
        "gateway-deploy.yml",
        "gateway-frontend-deploy.yml",
        "run-gateway-migrations.yml",
        "security-scan.yml",
        "tools-interceptor-deploy.yml",
        "tools-facade-deploy.yml",
        "tools-platform-deploy.yml",
        "chat-agent-deploy.yml",
        # Knowledge Door (#7829): agent-context-deploy.yml publishes its context and
        # release receipt here instead of GitHub Actions artifacts.
        "agent-context-deploy.yml",
    }
)
RELEASE_COMPONENTS = frozenset(
    {
        "gateway-backend",
        "gateway-frontend",
        "gateway-migrations",
        "tools-interceptor",
        "tools-facade",
        "tools-platform",
        "tool-adapters",
        "agent-worker",
        "chat-worker",
        "knowledge-door",
    }
)
REPOSITORY_ARTIFACT_WORKFLOWS = {
    "scan": ".github/workflows/security-scan.yml",
    "cleanup": ".github/workflows/security-scan.yml",
    "reconciliation": ".github/workflows/security-scan.yml",
    "identity-proof": ".github/workflows/gateway-deploy.yml",
    "atlassian-foundation": ".github/workflows/tools-platform-deploy.yml",
    "atlassian-identity": ".github/workflows/tools-platform-deploy.yml",
}


def bucket_name(account, environment):
    if not re.fullmatch(r"[0-9]{12}", account) or not re.fullmatch(r"[a-z][a-z0-9-]{0,23}", environment):
        raise ValueError("invalid evidence target")
    return f"adp-{environment}-deployment-evidence-{account}"


def object_key(repository_id, run_id, attempt, kind, name):
    if any(type(v) is not int or v <= 0 for v in (repository_id, run_id, attempt)):
        raise ValueError("invalid evidence run identity")
    if kind == "context":
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,119}\.ya?ml", name):
            raise ValueError("unsupported context workflow")
    elif kind == "release":
        if name not in RELEASE_COMPONENTS:
            raise ValueError("unsupported release component")
    elif kind == "repository-artifact":
        if name not in REPOSITORY_ARTIFACT_WORKFLOWS:
            raise ValueError("unsupported repository artifact")
    elif kind in {"delivery", "workflow-artifact"}:
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", name):
            raise ValueError("unsupported workflow artifact name")
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

    def read_archive(self, *, source_revision, **expected):
        """Authenticate the descriptor before reading its exact archive version."""
        import boto3

        if expected.get("kind") == "workflow-artifact":
            workflow = expected.get("workflow_path", "")
            if not re.fullmatch(r"\.github/workflows/[A-Za-z0-9_][A-Za-z0-9_.-]{0,119}\.ya?ml", workflow):
                raise ValueError("unsupported workflow archive")
        elif (
            expected.get("kind") != "repository-artifact"
            or expected.get("name") not in REPOSITORY_ARTIFACT_WORKFLOWS
            or expected.get("workflow_path") != REPOSITORY_ARTIFACT_WORKFLOWS[expected["name"]]
        ):
            raise ValueError("unsupported repository archive")
        result = self.read(**expected)
        if result is None:
            return None
        payload, _, reference = result
        descriptor = json.loads(payload)
        bucket = bucket_name(expected["account"], expected["environment"])
        base = object_key(expected["repository_id"], expected["run_id"], expected["attempt"], expected["kind"], expected["name"])
        digest = descriptor.get("sha256", "")
        key = base.removesuffix(".json") + "/" + digest + ".zip"
        identity = {
            "schema_version": 1,
            "repository_id": expected["repository_id"],
            "run_id": expected["run_id"],
            "run_attempt": expected["attempt"],
            "workflow_path": expected["workflow_path"],
            "workflow_revision": expected["workflow_revision"],
            "source_revision": source_revision,
            "account_id": expected["account"],
            "component": expected["name"],
            "bucket": bucket,
            "key": key,
        }
        if (
            any(descriptor.get(k) != v for k, v in identity.items())
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
            or type(descriptor.get("size")) is not int
            or not 0 < descriptor["size"] <= MAX_SCAN_ARCHIVE
            or not isinstance(descriptor.get("version_id"), str)
            or descriptor["version_id"] in {"", "null"}
        ):
            raise ValueError("repository archive descriptor mismatch")
        s3 = self.s3 or boto3.client("s3", region_name=expected["region"])
        response = s3.get_object(Bucket=bucket, Key=key, VersionId=descriptor["version_id"], ExpectedBucketOwner=expected["account"])
        try:
            if response.get("ContentLength") != descriptor["size"]:
                raise ValueError("repository archive size mismatch")
            content = response["Body"].read(MAX_SCAN_ARCHIVE + 1)
        finally:
            response["Body"].close()
        if (
            response.get("VersionId") != descriptor["version_id"]
            or response.get("ServerSideEncryption") != "AES256"
            or len(content) != descriptor["size"]
            or hashlib.sha256(content).hexdigest() != digest
        ):
            raise ValueError("repository archive receipt mismatch")
        return content, digest, reference

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
