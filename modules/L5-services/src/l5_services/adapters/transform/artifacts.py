"""Versioned private Transform artifacts bound to the current Product work."""

import asyncio
import base64
import json
import re
from datetime import UTC, datetime, timedelta
from hashlib import sha256

from botocore.exceptions import ClientError

from l5_services.workers.execution import AuthorityRevoked


class TransformArtifacts:
    def __init__(self, client, bucket, *, clock=None):
        if not isinstance(bucket, str) or not bucket:
            raise ValueError("Transform artifact bucket is required")
        self.client, self.bucket = client, bucket
        self.clock = clock or (lambda: datetime.now(UTC))

    def _prefix(self, authority):
        authority.assert_current()
        scope, request = authority.scope, authority.source_request
        identity = ":".join(scope[key] for key in ("tenant_id", "product_id", "project_id"))
        run = request["operation_id"]
        return f"l5/transform/artifacts/{sha256(identity.encode()).hexdigest()}/{sha256(run.encode()).hexdigest()}/"

    def _checkpoint_key(self, name, authority):
        if not re.fullmatch(r"[a-zA-Z0-9_.-]{1,100}", name):
            raise ValueError("Transform checkpoint name is invalid")
        return self._prefix(authority) + "checkpoints/" + name

    async def read_checkpoint(self, *, name, authority):
        key = self._checkpoint_key(name, authority)
        listed = await asyncio.to_thread(self.client.list_objects_v2, Bucket=self.bucket, Prefix=key, MaxKeys=1)
        if not any(entry.get("Key") == key for entry in listed.get("Contents", [])):
            authority.assert_current()
            return None
        response = await asyncio.to_thread(self.client.get_object, Bucket=self.bucket, Key=key)
        try:
            size = response.get("ContentLength")
            expiry = datetime.fromisoformat(response.get("Metadata", {})["expires-at"])
            if (type(size) is not int or not 0 <= size <= 32 * 1024 * 1024
                    or not response.get("VersionId") or response["VersionId"] == "null"
                    or expiry.tzinfo is None or expiry <= self.clock()):
                raise ValueError("Transform checkpoint retention or version is invalid")
            content = await asyncio.to_thread(response["Body"].read, size + 1)
        finally:
            response["Body"].close()
        authority.assert_current()
        if len(content) != size or sha256(content).hexdigest() != response["Metadata"].get("sha256"):
            raise ValueError("Transform checkpoint contents changed")
        return json.loads(content)

    async def save_checkpoint(self, *, name, value, authority):
        key = self._checkpoint_key(name, authority)
        content = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        if len(content) > 32 * 1024 * 1024:
            raise ValueError("Transform checkpoint exceeds its bound")
        try:
            result = await asyncio.to_thread(self.client.put_object, Bucket=self.bucket, Key=key, Body=content,
                ContentType="application/json", IfNoneMatch="*", Metadata={"sha256": sha256(content).hexdigest(),
                    "expires-at": (self.clock() + timedelta(days=7)).isoformat()})
            if not result.get("VersionId") or result["VersionId"] == "null":
                raise ValueError("Transform checkpoint storage must be versioned")
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") != "PreconditionFailed":
                raise
        stored = await self.read_checkpoint(name=name, authority=authority)
        if stored != value:
            raise AuthorityRevoked("Transform completion checkpoint already has different contents")
        return stored

    async def put(self, *, tenant_id, project_id, run_id, name, contents, media_type, authority):
        prefix = self._prefix(authority)
        if (tenant_id != authority.scope["tenant_id"] or project_id != authority.scope["project_id"]
                or run_id != authority.source_request["operation_id"]
                or not re.fullmatch(r"[a-zA-Z0-9_.-]{1,100}", name)
                or not isinstance(contents, bytes) or len(contents) > 32 * 1024 * 1024):
            raise AuthorityRevoked("Transform artifact differs from current work")
        digest = sha256(contents).hexdigest()
        key = prefix + name + "/" + digest
        expiry = (self.clock() + timedelta(days=7)).isoformat()
        response = await asyncio.to_thread(self.client.put_object, Bucket=self.bucket, Key=key, Body=contents,
                                          ContentType=media_type, Metadata={"sha256": digest, "expires-at": expiry})
        authority.assert_current()
        version = response.get("VersionId")
        if not isinstance(version, str) or not version or version == "null":
            raise ValueError("Transform artifact storage must be versioned")
        artifact_id = "tf-artifact-" + base64.urlsafe_b64encode(key.encode()).decode().rstrip("=")
        return {"tenant_id": tenant_id, "project_id": project_id, "artifact_id": artifact_id, "version": version,
                "sha256": digest, "bytes": len(contents), "media_type": media_type, "expires_at": expiry}

    async def read(self, *, reference, authority):
        prefix = self._prefix(authority)
        key = reference.get("artifact_id") if isinstance(reference, dict) else None
        if isinstance(key, str) and key.startswith("tf-artifact-"):
            encoded = key.removeprefix("tf-artifact-")
            try:
                key = base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True).decode()
            except (ValueError, UnicodeDecodeError):
                raise AuthorityRevoked("Transform artifact identifier is invalid") from None
        if (not isinstance(reference, dict) or reference.get("tenant_id") != authority.scope["tenant_id"]
                or reference.get("project_id") != authority.scope["project_id"]
                or not isinstance(key, str) or not key.startswith(prefix)
                or not isinstance(reference.get("version"), str) or not reference["version"]
                or reference["version"] == "null"
                or not isinstance(reference.get("sha256"), str)
                or not re.fullmatch(r"[a-f0-9]{64}", reference["sha256"])
                or not key.endswith("/" + reference["sha256"])
                or type(reference.get("bytes")) is not int or not 0 <= reference["bytes"] <= 32 * 1024 * 1024):
            raise AuthorityRevoked("Transform artifact is outside current work")
        response = await asyncio.to_thread(self.client.get_object, Bucket=self.bucket,
                                          Key=key, VersionId=reference["version"])
        try:
            expiry = datetime.fromisoformat(response.get("Metadata", {})["expires-at"])
            if (expiry.tzinfo is None or expiry <= self.clock()
                    or response.get("VersionId") != reference["version"]
                    or response.get("ContentLength") != reference["bytes"]):
                raise ValueError("Transform artifact version or retention changed")
            content = await asyncio.to_thread(response["Body"].read, reference["bytes"] + 1)
        finally:
            response["Body"].close()
        authority.assert_current()
        if len(content) != reference["bytes"] or sha256(content).hexdigest() != reference["sha256"]:
            raise ValueError("Transform artifact bytes changed")
        return content
