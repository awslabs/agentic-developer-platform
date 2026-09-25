#!/usr/bin/env python3
"""Publish the distinct browser overlay without colliding with the worker recipe."""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
RECIPE = "direct-browser-v1"


def run(*args):
    return subprocess.check_output(args, text=True).strip()


def main():
    source = os.environ.get("ADP_SOURCE_SHA", "")
    registry = os.environ.get("REGISTRY", "")
    region = os.environ.get("AWS_REGION", "")
    base = os.environ.get("WORKER_BASE_IMAGE", "")
    if not re.fullmatch(r"[0-9a-f]{40}", source):
        raise ValueError("ADP_SOURCE_SHA must be the full archived source SHA")
    if (
        not re.fullmatch(
            r"[0-9]{12}\.dkr\.ecr\." + re.escape(region) + r"\.amazonaws\.com", registry
        )
        or not region
    ):
        raise ValueError("REGISTRY must be an ECR registry in AWS_REGION")
    if not re.search(r"@sha256:[0-9a-f]{64}$", base):
        raise ValueError("WORKER_BASE_IMAGE must select an explicit digest")
    tag = f"{RECIPE}-{source}-{base.rsplit(':', 1)[1]}"
    if os.environ.get("IMAGE_TAG", tag) != tag:
        raise ValueError(f"IMAGE_TAG must be the recipe/source/base identity: {tag}")
    if os.environ.get("PUBLISH_LATEST", "false") != "false":
        raise ValueError("Mutable latest publication is unsupported")
    base = run(
        sys.executable, str(ROOT / "platform/scripts/resolve-ecr-image.py"), base
    )
    image = f"{registry}/adp-agent-runtime:{tag}"
    digest_args = (
        "aws",
        "ecr",
        "describe-images",
        "--region",
        region,
        "--registry-id",
        registry.split(".")[0],
        "--repository-name",
        "adp-agent-runtime",
        "--image-ids",
        f"imageTag={tag}",
        "--query",
        "imageDetails[0].imageDigest",
        "--output",
        "text",
    )
    existing = subprocess.run(digest_args, capture_output=True, text=True)
    if existing.returncode and "ImageNotFoundException" not in existing.stderr:
        raise ValueError(existing.stderr.strip() or "Registry lookup failed")
    digest = existing.stdout.strip() if existing.returncode == 0 else None
    if digest is not None and not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise ValueError("Registry returned an invalid digest")
    password = run("aws", "ecr", "get-login-password", "--region", region)
    subprocess.run(
        ["docker", "login", "--username", "AWS", "--password-stdin", registry],
        input=password,
        text=True,
        check=True,
    )
    labels = {
        "org.opencontainers.image.revision": source,
        "io.adp.image.recipe": RECIPE,
        "io.adp.image.base.digest": base.rsplit("@", 1)[1],
    }
    if digest:
        selected = f"{registry}/adp-agent-runtime@{digest}"
        subprocess.run(["docker", "pull", selected], check=True)
    else:
        selected = image
        args = ["docker", "build", "--pull", "--build-arg", f"WORKER_BASE_IMAGE={base}"]
        for key, value in labels.items():
            args += ["--label", f"{key}={value}"]
        subprocess.run(
            args
            + [
                "-f",
                "modules/domain-apps/cyber/agent/Dockerfile.browser-overlay",
                "-t",
                image,
                ".",
            ],
            cwd=ROOT,
            check=True,
        )
    actual = json.loads(
        run(
            "docker",
            "image",
            "inspect",
            selected,
            "--format",
            "{{json .Config.Labels}}",
        )
    )
    if not isinstance(actual, dict) or any(
        actual.get(k) != v for k, v in labels.items()
    ):
        raise ValueError(
            "Image provenance does not match the recipe/source/base identity"
        )
    if not digest:
        subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--entrypoint",
                "python3",
                image,
                "-m",
                "lib.contract_selfcheck",
            ],
            check=True,
        )
        subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--entrypoint",
                "python3",
                image,
                "-c",
                "import sys; sys.path.insert(0, '/app/skills/url-analysis'); "
                "import native_browser, local_browser, direct_capture; "
                "from bedrock_agentcore.tools.browser_client import BrowserClient; "
                "from playwright.sync_api import sync_playwright",
            ],
            check=True,
        )
        subprocess.run(["docker", "push", image], check=True)
        digest = run(*digest_args)
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            raise ValueError("Published digest missing")
    print(f"Verified image: {registry}/adp-agent-runtime@{digest}")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, subprocess.CalledProcessError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
