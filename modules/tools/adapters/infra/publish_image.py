"""Qualify a reviewed adapter image without access to the shared registry."""

import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]

SELF_CHECK = """
import importlib.util, os, sys
import tool_adapters.server
import adp_tools.adapter_contracts
assert os.environ['ADP_ADAPTER_SOURCE_REVISION'] == sys.argv[1]
assert importlib.util.find_spec('src') is None
assert importlib.util.find_spec('sqlalchemy') is None
assert importlib.util.find_spec('asyncpg') is None
"""


class PublicationError(RuntimeError):
    pass


def run(args, *, env, capture=True, check=True, input=None, timeout=60):
    result = subprocess.run(
        args,
        env=env,
        cwd=ROOT,
        text=True,
        capture_output=capture,
        check=False,
        input=input,
        timeout=timeout,
    )
    if check and result.returncode:
        # Provider output can contain private deployment configuration. Build
        # logs are streamed separately; credentials and AWS errors are not.
        raise PublicationError("adapter_publication_command_failed")
    return result


def publish(env):
    source = env.get("ADP_SOURCE_SHA", "")
    account = env.get("ACCOUNT_ID", "")
    region = env.get("AWS_REGION", "")
    environment = env.get("ADP_TOOLS_ENVIRONMENT", "")
    bucket = env.get("ADP_ADAPTER_ARTIFACT_BUCKET", "")
    build_id = env.get("CODEBUILD_BUILD_ID", "")
    repository = f"adp-tools-{environment}-adapters"
    project = repository + "-build"
    if not (
        re.fullmatch(r"[0-9a-f]{40}", source)
        and re.fullmatch(r"[0-9]{12}", account)
        and re.fullmatch(r"[a-z]{2}-[a-z]+-[0-9]+", region)
        and environment in {"dev", "prod"}
        and env.get("IMAGE_TAG", source) == source
        and env.get("PUBLISH_LATEST", "false") == "false"
        and env.get("ADP_ADAPTER_QUALIFY_ONLY") == "true"
        and re.fullmatch(re.escape(project) + r":[0-9a-f-]{36}", build_id)
        and re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket)
    ):
        raise PublicationError("adapter_publication_invalid_inputs")
    image = f"{account}.dkr.ecr.{region}.amazonaws.com/{repository}:{source}"

    def aws(*args, timeout=60):
        return run(["aws", *args, "--region", region], env=env, timeout=timeout)

    identity = aws("sts", "get-caller-identity", "--query", "Account", "--output", "text").stdout.strip()
    if identity != account:
        raise PublicationError("adapter_publication_account_mismatch")

    with tempfile.TemporaryDirectory(prefix="adp-adapter-docker-") as config:
        docker_env = {**env, "DOCKER_CONFIG": config}

        def docker(*args, capture=True, input=None, timeout=60, check=True):
            return run(["docker", *args], env=docker_env, capture=capture, input=input, timeout=timeout, check=check)

        docker(
            "build",
            "--pull",
            "--no-cache",
            "--file",
            "modules/tools/adapters/Dockerfile",
            "--build-arg",
            f"ADP_SOURCE_REVISION={source}",
            "--tag",
            image,
            ".",
            timeout=1500,
        )
        info = json.loads(docker("image", "inspect", image).stdout)
        if not (
            isinstance(info, list)
            and len(info) == 1
            and info[0].get("Config", {}).get("Labels", {}).get("org.opencontainers.image.revision") == source
            and info[0].get("Config", {}).get("User") == "65532:65532"
            and isinstance(info[0].get("Id"), str)
            and re.fullmatch(r"sha256:[0-9a-f]{64}", info[0]["Id"])
        ):
            raise PublicationError("adapter_publication_image_identity_mismatch")
        container = "adp-adapter-publication-" + uuid.uuid4().hex
        try:
            docker(
                "run",
                "--rm",
                "--name",
                container,
                "--network",
                "none",
                "--read-only",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--pids-limit",
                "64",
                "--memory",
                "512m",
                "--cpus",
                "1",
                "--entrypoint",
                "python",
                image,
                "-c",
                SELF_CHECK,
                source,
            )
        finally:
            # A Docker CLI timeout can leave its container running despite --rm.
            docker("container", "rm", "--force", container, check=False)
            remaining = docker("ps", "--all", "--filter", f"name=^/{container}$", "--format", "{{.ID}}")
            if remaining.stdout.strip():
                raise PublicationError("adapter_publication_selfcheck_cleanup_failed")
        archive = Path(config) / "qualified-image.tar"
        docker("save", "--output", str(archive), image, timeout=600)
        if not 0 < archive.stat().st_size <= 1_073_741_824:
            raise PublicationError("adapter_publication_archive_size_invalid")
        with archive.open("rb") as stream:
            archive_hash = hashlib.file_digest(stream, "sha256").digest()
        key = f"codebuild/artifacts/{project}/{source}-{build_id.split(':', 1)[1]}.tar"
        result = json.loads(aws(
            "s3api", "put-object", "--bucket", bucket, "--key", key, "--body", str(archive),
            "--if-none-match", "*", "--checksum-sha256", base64.b64encode(archive_hash).decode(),
            "--output", "json", timeout=600,
        ).stdout)
        if (
            not isinstance(result, dict)
            or not isinstance(result.get("VersionId"), str)
            or not 0 < len(result["VersionId"]) <= 1024
            or result["VersionId"] == "null"
            or result.get("ChecksumSHA256") != base64.b64encode(archive_hash).decode()
        ):
            raise PublicationError("adapter_publication_archive_unverified")
    return {
        "schema_version": "1.0",
        "source_revision": source,
        "repository": repository,
        "image_digest": None,
        "reused": False,
        "selfcheck_passed": True,
        "qualified_only": True,
        "artifact_key": key,
        "artifact_version": result["VersionId"],
        "artifact_sha256": archive_hash.hex(),
        "config_digest": info[0]["Id"],
    }


def main():
    try:
        result = publish(dict(os.environ))
    except (PublicationError, OSError, ValueError, TypeError, subprocess.TimeoutExpired):
        print("adapter_publication_failed", file=sys.stderr)
        raise SystemExit(1) from None
    print("ADP_ADAPTER_PUBLICATION_V1 " + json.dumps(result, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
