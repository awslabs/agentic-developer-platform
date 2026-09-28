#!/usr/bin/env python3
"""Resolve an explicit immutable release selector to an ECR digest before deployment."""
import re
import subprocess
import sys


def resolve(image):
    match = re.fullmatch(r"([0-9]{12})\.dkr\.ecr\.([a-z0-9-]+)\.amazonaws\.com(?:\.cn)?/([a-z0-9._/-]+)(@sha256:[0-9a-f]{64}|:[0-9a-f]{40})", image)
    if not match:
        raise ValueError("Expected an ECR image pinned by full source SHA or sha256 digest")
    account, region, repository, selector = match.groups()
    image_id = "imageDigest=" + selector[1:] if selector.startswith("@") else "imageTag=" + selector[1:]
    digest = subprocess.check_output([
        "aws", "ecr", "describe-images", "--registry-id", account, "--region", region,
        "--repository-name", repository, "--image-ids", image_id,
        "--query", "imageDetails[0].imageDigest", "--output", "text",
    ], text=True).strip()
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise ValueError("Registry did not return a valid OCI digest")
    if selector.startswith("@") and digest != selector[1:]:
        raise ValueError("Registry digest differs from requested release")
    return image[:-len(selector)] + "@" + digest


if __name__ == "__main__":
    try:
        if len(sys.argv) != 2:
            raise ValueError("Usage: resolve-ecr-image.py IMAGE")
        print(resolve(sys.argv[1]))
    except (ValueError, subprocess.CalledProcessError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
