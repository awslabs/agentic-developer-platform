"""Emit build hashes for D3. These hashes do not attest current runtime health."""

import argparse
import hashlib
import json
import os
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path


def produce(env, component, read, frontend=Path("modules/gateway/frontend/dist")):
    source = env.get("ADP_RELEASE_SOURCE", "")
    if not re.fullmatch(r"[0-9a-f]{40}", source):
        raise ValueError("immutable release source required")
    account = read(["sts", "get-caller-identity"])["Account"]
    if account != env["ACCOUNT_ID"]:
        raise ValueError("release account differs from resolved workflow account")
    digest, assets = None, {}
    if component == "gateway-frontend":
        total = 0
        for path in sorted(frontend.rglob("*")):
            if path.is_symlink():
                raise ValueError("build artifact symlinks are not allowed")
            if path.is_file():
                if path.stat().st_size > 8 * 1024 * 1024:
                    raise ValueError("build asset too large")
                payload = path.read_bytes()
                total += len(payload)
                if total > 32 * 1024 * 1024 or len(assets) >= 256:
                    raise ValueError("build evidence exceeds bounded inventory")
                assets[path.relative_to(frontend).as_posix()] = hashlib.sha256(payload).hexdigest()
        if "index.html" not in assets:
            raise ValueError("frontend build is missing")
    else:
        images = read(
            ["ecr", "describe-images", "--repository-name", "adp-gateway", "--image-ids", "imageTag=" + source, "--region", env["AWS_REGION"]]
        )["imageDetails"]
        if len(images) != 1 or source not in images[0].get("imageTags", []):
            raise ValueError("release image not identified by immutable source")
        digest = images[0]["imageDigest"]
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            raise ValueError("release image digest invalid")
    cluster = env.get("EKS_CLUSTER") or f"adp-{env['ENVIRONMENT']}-eks-cluster"
    namespace = env.get("NAMESPACE") or "adp-gateway"
    return {
        "schema_version": 1,
        "repository_id": int(env["GITHUB_REPOSITORY_ID"]),
        "run_id": int(env["GITHUB_RUN_ID"]),
        "run_attempt": int(env["GITHUB_RUN_ATTEMPT"]),
        "source_revision": source,
        "workflow_revision": env["ADP_RELEASE_DEFINITION"],
        "workflow_path": env["ADP_RELEASE_WORKFLOW"],
        "account_id": account,
        "region": env["AWS_REGION"],
        "resource_id": cluster + "/" + namespace,
        "component": component,
        "image_digest": digest,
        "assets": assets,
        "produced_at": datetime.now(UTC).isoformat(),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("component", choices=["gateway-backend", "gateway-frontend", "gateway-migrations"])
    args = parser.parse_args()

    def read(parts):
        return json.loads(subprocess.check_output(["aws", *parts, "--output", "json"], timeout=20))

    data = produce(os.environ, args.component, read)
    target = Path(os.environ["RUNNER_TEMP"]) / ("adp-release-" + args.component) / "release.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, sort_keys=True))


if __name__ == "__main__":
    main()
