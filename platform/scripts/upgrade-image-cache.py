#!/usr/bin/env python3
"""Find an existing immutable ECR image for a repeated source-SHA upgrade."""

import json
import re
import subprocess
import sys


IMAGE = re.compile(
    r"(?P<account>[0-9]{12})\.dkr\.ecr\.(?P<region>[a-z0-9-]+)\.amazonaws\.com/"
    r"(?P<repository>[a-z0-9._/-]+):(?P<tag>[0-9a-f]{40})"
)
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


def reusable_image(image, *, run=subprocess.run):
    """Return a pinned image only when its SHA tag cannot be overwritten."""
    match = IMAGE.fullmatch(image)
    if not match:
        raise ValueError("Expected an ECR image tagged with the full source SHA")
    account, region, repository, tag = match.group("account", "region", "repository", "tag")

    def describe(operation, missing_error, *args):
        result = run(
            ["aws", "ecr", operation, "--registry-id", account, "--region", region,
             *args, "--output", "json"],
            text=True, capture_output=True, check=False,
        )
        if result.returncode:
            if missing_error in result.stderr:
                return None
            raise RuntimeError(f"ECR {operation} failed: {result.stderr.strip()}")
        return json.loads(result.stdout)

    repositories = describe(
        "describe-repositories", "RepositoryNotFoundException",
        "--repository-names", repository,
    )
    if repositories is None:
        return ""
    listed = repositories.get("repositories", [])
    if len(listed) != 1 or listed[0].get("imageTagMutability") != "IMMUTABLE":
        return ""

    images = describe(
        "describe-images", "ImageNotFoundException",
        "--repository-name", repository, "--image-ids", f"imageTag={tag}",
    )
    if images is None:
        return ""
    details = images.get("imageDetails", [])
    digest = details[0].get("imageDigest", "") if len(details) == 1 else ""
    if not DIGEST.fullmatch(digest):
        raise ValueError("Existing SHA tag has no valid ECR image digest")
    return image.rsplit(":", 1)[0] + "@" + digest


if __name__ == "__main__":
    try:
        if len(sys.argv) != 2:
            raise ValueError("Usage: upgrade-image-cache.py ECR_IMAGE_WITH_FULL_SHA_TAG")
        print(reusable_image(sys.argv[1]))
    except (ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(exc, file=sys.stderr)
        sys.exit(1)
