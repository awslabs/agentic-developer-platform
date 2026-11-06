"""Read published shared indexes, retaining legacy filesystem compatibility."""

import json
import logging
import re
from pathlib import Path

from botocore.exceptions import ClientError

log = logging.getLogger(__name__)
MAX_INDEX_BYTES = 512 * 1024 * 1024


def load_code_index(repo: str, *, bucket: str, directory: str, s3=None) -> dict | None:
    # These trusted maintenance tools operate on shared repository artifacts.
    # Scoped indexes retain their own prefixes and are read by the scoped Door.
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo) or any(
        p in {".", ".."} for p in repo.split("/")
    ):
        return None
    filename = repo.replace("/", "-") + ".json"
    if bucket:
        try:
            if s3 is None:
                import boto3

                s3 = boto3.client("s3")
            response = s3.get_object(Bucket=bucket, Key="code-indexes/" + filename)
            body = response["Body"]
            try:
                raw = body.read(MAX_INDEX_BYTES + 1)
            finally:
                body.close()
            if len(raw) > MAX_INDEX_BYTES:
                return None
            value = json.loads(raw)
            # Never reinterpret another repository's flattened filename as ours.
            return value if isinstance(value, dict) and value.get("repo") == repo else None
        except ClientError as error:
            if error.response["Error"]["Code"] not in {"NoSuchKey", "404", "NotFound"}:
                log.warning("Published code index unavailable for %s", repo)
                return None
        except (ValueError, OSError):
            return None
    # Older ingestion wrote only the PVC. Keep those already-produced artifacts
    # readable until the corresponding registration has been reindexed.
    try:
        value = json.loads((Path(directory) / filename).read_text())
        return value if isinstance(value, dict) and value.get("repo", repo) == repo else None
    except (ValueError, OSError):
        return None
