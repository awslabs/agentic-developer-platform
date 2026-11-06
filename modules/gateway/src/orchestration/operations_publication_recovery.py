"""Read a pinned immutable registry tag using the reviewed deployment connection.

This observation alone cannot settle a pending lease: its controller must also
verify the previous workflow is terminal and retain the recorded owner/generation.
A missing tag leaves the outcome unknown, not safe to release.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime

from botocore.config import Config

from .review_cycle import CycleBlockedError

MANIFEST_TYPE = "application/vnd.docker.distribution.manifest.v2+json"


@dataclass(frozen=True)
class ImageReadback:
    account_id: str
    region: str
    repository: str
    source_revision: str
    image_digest: str
    observed_at: datetime


@dataclass(frozen=True)
class ImageAbsence:
    account_id: str
    region: str
    repository: str
    source_revision: str
    image_digest: str
    observed_at: datetime


def read_pending_image(scoped, *, account_id: str, region: str, environment: str, source_revision: str, image_digest: str, report_absence=False):
    if not (
        isinstance(account_id, str)
        and re.fullmatch(r"[0-9]{12}", account_id)
        and isinstance(region, str)
        and re.fullmatch(r"[a-z]{2}-[a-z]+-[0-9]+", region)
        and isinstance(environment, str)
        and environment in {"dev", "prod"}
        and isinstance(source_revision, str)
        and re.fullmatch(r"[0-9a-f]{40}", source_revision)
        and isinstance(image_digest, str)
        and re.fullmatch(r"sha256:[0-9a-f]{64}", image_digest)
    ):
        raise CycleBlockedError("operations_publication_recovery_inputs_invalid")
    repository = f"adp-tools-{environment}-adapters"
    bounded = Config(connect_timeout=3, read_timeout=10, retries={"total_max_attempts": 1})
    try:
        client = scoped.client("ecr", config=bounded)
        repositories = client.describe_repositories(registryId=account_id, repositoryNames=[repository])
        if not isinstance(repositories, dict) or repositories.get("nextToken") or len(repositories.get("repositories", [])) != 1:
            raise CycleBlockedError("operations_publication_repository_changed")
        target = repositories["repositories"][0]
        if (
            target.get("registryId") != account_id
            or target.get("repositoryName") != repository
            or target.get("repositoryArn") != f"arn:aws:ecr:{region}:{account_id}:repository/{repository}"
            or target.get("repositoryUri") != f"{account_id}.dkr.ecr.{region}.amazonaws.com/{repository}"
            or target.get("imageTagMutability") != "IMMUTABLE"
        ):
            raise CycleBlockedError("operations_publication_repository_changed")
        result = client.batch_get_image(
            registryId=account_id,
            repositoryName=repository,
            imageIds=[{"imageTag": source_revision}],
            acceptedMediaTypes=[MANIFEST_TYPE],
        )
    except CycleBlockedError:
        raise
    except Exception:
        raise CycleBlockedError("operations_publication_read_unavailable") from None
    if not isinstance(result, dict) or result.get("nextToken"):
        raise CycleBlockedError("operations_publication_read_unavailable")
    images, failures = result.get("images"), result.get("failures")
    if images == [] and isinstance(failures, list) and len(failures) == 1 and isinstance(failures[0], dict):
        missing = failures[0]
        if (
            missing.get("imageId") == {"imageTag": source_revision}
            and missing.get("failureCode") == "ImageNotFound"
            and {"imageId", "failureCode"} <= missing.keys() <= {"imageId", "failureCode", "failureReason"}
            and ("failureReason" not in missing or isinstance(missing["failureReason"], str))
        ):
            return ImageAbsence(account_id, region, repository, source_revision, image_digest, datetime.now(UTC)) if report_absence else None
    if not isinstance(images, list) or len(images) != 1 or failures != []:
        raise CycleBlockedError("operations_publication_read_unavailable")
    image = images[0]
    if not isinstance(image, dict):
        raise CycleBlockedError("operations_publication_image_changed")
    manifest = image.get("imageManifest")
    try:
        manifest_bytes = manifest.encode() if isinstance(manifest, str) else b""
    except UnicodeEncodeError:
        raise CycleBlockedError("operations_publication_image_changed") from None
    if (
        image.get("registryId") != account_id
        or image.get("repositoryName") != repository
        or image.get("imageId") != {"imageTag": source_revision, "imageDigest": image_digest}
        or image.get("imageManifestMediaType") != MANIFEST_TYPE
        or not 0 < len(manifest_bytes) <= 1_048_576
        or "sha256:" + hashlib.sha256(manifest_bytes).hexdigest() != image_digest
    ):
        raise CycleBlockedError("operations_publication_image_changed")
    return ImageReadback(account_id, region, repository, source_revision, image_digest, datetime.now(UTC))
