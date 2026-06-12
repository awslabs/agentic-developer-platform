"""S3-based content store for the ingestion pipeline.

Replaces all OpenViking read/write/list/search operations with S3 + S3 Vectors.
Single module that all ingestion scripts import instead of scattered OpenViking helpers.

Operations:
  - put_content(key, content, metadata) — write text content to S3
  - get_content(key) — read text content from S3
  - list_prefix(prefix) — list object keys under a prefix
  - query_vectors(query_embedding, org_id, top_k) — semantic search via S3 Vectors

Key layout (replaces viking:// URI scheme):
  content/wikis/{org-repo}-wiki.md
  content/code-indexes/{org-repo}-code-index.md
  content/infra/{account_id}/resources.json
  content/infra/{account_id}/relationships.json
  content/{org}/{repo}/.infra-map.json
  content/{org}/{repo}/.deploy-map.json
  content/meta/index-{slug}.md
  content/meta/lint-report.md
  content/web/{domain}/{path}.md
  content/docs/{slug}.md
  content/discoveries/...
"""

from __future__ import annotations

import json
import logging
from typing import Any

import boto3
from botocore.exceptions import ClientError

log = logging.getLogger("s3-store")


class S3ContentStore:
    """S3-backed content store replacing OpenViking's filesystem API.

    Provides put/get/list for text content stored as S3 objects.
    All keys are prefixed with a configurable root (default: "content/").
    """

    def __init__(
        self,
        bucket_name: str,
        prefix: str = "content",
        region_name: str | None = None,
    ):
        self.bucket_name = bucket_name
        self.prefix = prefix.rstrip("/")
        kwargs: dict[str, Any] = {}
        if region_name:
            kwargs["region_name"] = region_name
        self._s3 = boto3.client("s3", **kwargs)

    def _key(self, path: str) -> str:
        """Convert a logical path to an S3 key."""
        clean = path.lstrip("/")
        return f"{self.prefix}/{clean}"

    def put_content(self, path: str, content: str | bytes) -> bool:
        """Write text/binary content to S3. Returns True on success."""
        key = self._key(path)
        if isinstance(content, str):
            body = content.encode("utf-8")
            content_type = "text/plain; charset=utf-8"
        else:
            body = content
            content_type = "application/octet-stream"

        try:
            self._s3.put_object(
                Bucket=self.bucket_name,
                Key=key,
                Body=body,
                ContentType=content_type,
            )
            log.info("Wrote s3://%s/%s (%d bytes)", self.bucket_name, key, len(body))
            return True
        except ClientError as e:
            log.error("S3 put failed for %s: %s", key, e)
            return False

    def get_content(self, path: str) -> str | None:
        """Read text content from S3. Returns None if not found."""
        key = self._key(path)
        try:
            resp = self._s3.get_object(Bucket=self.bucket_name, Key=key)
            return resp["Body"].read().decode("utf-8")
        except ClientError as e:
            if e.response["Error"]["Code"] == "NoSuchKey":
                return None
            log.error("S3 get failed for %s: %s", key, e)
            return None

    def get_json(self, path: str) -> dict[str, Any] | None:
        """Read JSON content from S3. Returns None if not found or invalid."""
        content = self.get_content(path)
        if content is None:
            return None
        try:
            return json.loads(content)
        except (json.JSONDecodeError, ValueError):
            log.warning("Invalid JSON at %s", path)
            return None

    def list_prefix(self, path: str) -> list[dict[str, Any]]:
        """List objects under a prefix. Returns list of {name, key, is_dir} dicts.

        Simulates a directory listing by using S3 CommonPrefixes (delimiter='/').
        """
        prefix = self._key(path)
        if not prefix.endswith("/"):
            prefix += "/"

        items: list[dict[str, Any]] = []
        try:
            paginator = self._s3.get_paginator("list_objects_v2")
            for page in paginator.paginate(
                Bucket=self.bucket_name, Prefix=prefix, Delimiter="/"
            ):
                # Subdirectories
                for cp in page.get("CommonPrefixes", []):
                    dir_prefix = cp["Prefix"]
                    name = dir_prefix[len(prefix) :].rstrip("/")
                    if name:
                        items.append({"name": name, "is_dir": True})

                # Files
                for obj in page.get("Contents", []):
                    obj_key = obj["Key"]
                    name = obj_key[len(prefix) :]
                    if name and "/" not in name:
                        items.append({"name": name, "is_dir": False})
        except ClientError as e:
            log.error("S3 list failed for %s: %s", prefix, e)

        return items

    def exists(self, path: str) -> bool:
        """Check if an object exists at the given path."""
        key = self._key(path)
        try:
            self._s3.head_object(Bucket=self.bucket_name, Key=key)
            return True
        except ClientError:
            return False


def viking_uri_to_s3_path(uri: str) -> str:
    """Convert a legacy viking:// URI to an S3 content path.

    Examples:
      viking://resources/deepwiki/org-repo-wiki.md -> wikis/org-repo-wiki.md
      viking://resources/infra/123/resources.json  -> infra/123/resources.json
      viking://resources/org/repo/.code-index.md   -> repos/org/repo/.code-index.md
      viking://resources/meta/lint-report.md       -> meta/lint-report.md
      viking://resources/web/domain/path.md        -> web/domain/path.md
      viking://resources/docs/slug.md              -> docs/slug.md
      viking://resources/discoveries/...           -> discoveries/...
    """
    # Strip the viking:// prefix
    path = uri.replace("viking://resources/", "").replace("viking://resources", "")

    # Route to appropriate sub-prefix
    if path.startswith("deepwiki/"):
        return "wikis/" + path[len("deepwiki/"):]
    elif path.startswith("infra/"):
        return path  # Already correctly prefixed
    elif path.startswith("meta/"):
        return path
    elif path.startswith("web/"):
        return path
    elif path.startswith("docs/"):
        return path
    elif path.startswith("discoveries/"):
        return path
    else:
        # org/repo paths -> repos/org/repo/...
        return "repos/" + path
