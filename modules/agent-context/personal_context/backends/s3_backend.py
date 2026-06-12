"""S3-native AGFS backend — drop-in replacement for OpenViking's filesystem.

Implements the AGFSBackend protocol (put/get/delete/list_prefix) against
Amazon S3 via boto3. Entries are stored as JSON objects under a configurable
key prefix.

Design reference: docs/design-1348-replace-openviking.md section 4.3.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)


class S3AGFSBackend:
    """AGFS-compatible backend using Amazon S3.

    Entries are stored as JSON objects at paths like:
      s3://{bucket}/{prefix}{path}

    Where ``path`` follows the AGFS convention:
      /personal/{owner_sub}/{type_dir}/{entry_id}.json
      /shared/{tenant_id}/{type_dir}/{entry_id}.json

    Parameters
    ----------
    bucket_name:
        S3 bucket name (e.g. ``agent-context-platform-data-{account_id}``).
    prefix:
        Key prefix prepended to all AGFS paths (default: ``personal-context``).
        Avoids collisions with other data in the same bucket.
    region_name:
        AWS region for the S3 client. If None, uses the default from
        environment/instance profile.
    """

    def __init__(
        self,
        bucket_name: str,
        prefix: str = "personal-context",
        region_name: str | None = None,
    ):
        self.bucket_name = bucket_name
        self.prefix = prefix.rstrip("/")
        kwargs: dict[str, Any] = {}
        if region_name:
            kwargs["region_name"] = region_name
        self._s3 = boto3.client("s3", **kwargs)

    def _key(self, path: str) -> str:
        """Convert an AGFS path to an S3 object key."""
        # AGFS paths start with / — strip leading slash for S3 key
        clean_path = path.lstrip("/")
        return f"{self.prefix}/{clean_path}"

    def put(self, path: str, data: dict[str, Any]) -> None:
        """Store a JSON entry at the given AGFS path."""
        key = self._key(path)
        body = json.dumps(data, default=str)
        self._s3.put_object(
            Bucket=self.bucket_name,
            Key=key,
            Body=body.encode("utf-8"),
            ContentType="application/json",
        )

    def get(self, path: str) -> dict[str, Any] | None:
        """Retrieve a JSON entry by AGFS path. Returns None if not found."""
        key = self._key(path)
        try:
            response = self._s3.get_object(Bucket=self.bucket_name, Key=key)
            body = response["Body"].read().decode("utf-8")
            return json.loads(body)
        except ClientError as e:
            if e.response["Error"]["Code"] == "NoSuchKey":
                return None
            raise

    def delete(self, path: str) -> None:
        """Delete an entry by AGFS path (idempotent)."""
        key = self._key(path)
        self._s3.delete_object(Bucket=self.bucket_name, Key=key)

    def list_prefix(self, prefix: str) -> list[dict[str, Any]]:
        """List all entries under a given AGFS prefix.

        Returns the parsed JSON content of each matching object.
        Skips objects that fail to parse (logs a warning).
        """
        s3_prefix = self._key(prefix)
        items: list[dict[str, Any]] = []

        paginator = self._s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket_name, Prefix=s3_prefix):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                try:
                    response = self._s3.get_object(Bucket=self.bucket_name, Key=key)
                    body = response["Body"].read().decode("utf-8")
                    data = json.loads(body)
                    items.append(data)
                except (ClientError, json.JSONDecodeError) as e:
                    logger.warning("Failed to read S3 object %s: %s", key, e)
                    continue

        return items
