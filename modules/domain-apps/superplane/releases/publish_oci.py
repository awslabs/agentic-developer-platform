#!/usr/bin/env python3
"""Publish reviewed Superplane OCI bytes without rebuilding or changing manifests.

Review evidence is bound by hash, not interpreted as release/security approval.
The invoking operator owns approval. Preparation is local; --publish writes ECR.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

REPOSITORIES = {
    "python-base": {"adp-superplane-api", "adp-superplane-paid-worker"},
    "skypilot": {"adp-superplane-skypilot"},
}
DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
INDEX = "application/vnd.oci.image.index.v1+json"
MANIFEST = "application/vnd.oci.image.manifest.v1+json"
CONFIG = "application/vnd.oci.image.config.v1+json"
LAYER = "application/vnd.oci.image.layer.v1.tar+gzip"
TAR_LAYER = "application/vnd.oci.image.layer.v1.tar"


def digest(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


def blob(layout, descriptor):
    if descriptor.get("urls") or descriptor.get("data"):
        raise ValueError("External or embedded OCI descriptors are not supported")
    if type(descriptor.get("size")) is not int or descriptor["size"] < 0:
        raise ValueError("Invalid OCI descriptor size")
    expected = descriptor["digest"]
    if not DIGEST.fullmatch(expected):
        raise ValueError("Invalid OCI digest")
    path = layout / "blobs" / "sha256" / expected[7:]
    data = path.read_bytes()
    if digest(data) != expected or len(data) != descriptor["size"]:
        raise ValueError(f"OCI blob hash/size mismatch: {expected}")
    return data


def inspect(layout, platform_digest, config_digest, kind):
    if not DIGEST.fullmatch(platform_digest) or not DIGEST.fullmatch(config_digest):
        raise ValueError("Expected exact platform and config SHA256 digests")
    if json.loads((layout / "oci-layout").read_text()) != {
        "imageLayoutVersion": "1.0.0"
    }:
        raise ValueError("Unsupported OCI layout")
    visited = set()

    def find(index):
        for descriptor in index["manifests"]:
            if descriptor["digest"] in visited:
                continue
            visited.add(descriptor["digest"])
            if descriptor["digest"] == platform_digest:
                if descriptor["mediaType"] != MANIFEST:
                    raise ValueError("Expected a platform manifest, not an index")
                return descriptor
            if descriptor["mediaType"] == INDEX:
                found = find(json.loads(blob(layout, descriptor)))
                if found:
                    return found
        return None

    descriptor = find(json.loads((layout / "index.json").read_text()))
    if descriptor is None:
        raise ValueError("Reviewed platform is not reachable from OCI index")
    raw = blob(layout, descriptor)
    manifest = json.loads(raw)
    if manifest["schemaVersion"] != 2 or manifest["mediaType"] != MANIFEST:
        raise ValueError("Unsupported platform manifest")
    if manifest["config"]["digest"] != config_digest:
        raise ValueError("Config does not match reviewed digest")
    if manifest["config"]["mediaType"] != CONFIG:
        raise ValueError("Unsupported OCI config media type")
    config = json.loads(blob(layout, manifest["config"]))
    if (config.get("os"), config.get("architecture")) != ("linux", "amd64"):
        raise ValueError("Only reviewed linux/amd64 images are supported")
    runtime = config.get("config", {})
    if "com.adp.local-diagnostic" in (runtime.get("Labels") or {}):
        raise ValueError("Diagnostic images cannot be published")
    if kind == "python-base":
        if not any(
            e.startswith("PYTHON_VERSION=3.12.") for e in runtime.get("Env", [])
        ):
            raise ValueError("Expected reviewed Python 3.12 base")
        if runtime.get("User") != "0" or runtime.get("Cmd") != ["python3"]:
            raise ValueError(
                "Python base must preserve reviewed root user and python3 command"
            )
    rootfs = config.get("rootfs", {})
    diff_ids = rootfs.get("diff_ids", [])
    if rootfs.get("type") != "layers" or len(diff_ids) != len(manifest["layers"]):
        raise ValueError("Config rootfs must bind every OCI layer")
    for layer, diff_id in zip(manifest["layers"], diff_ids):
        if layer["mediaType"] not in {LAYER, TAR_LAYER} or not DIGEST.fullmatch(
            diff_id
        ):
            raise ValueError(
                "Only tar/gzip OCI layers with SHA256 diff IDs are supported"
            )
        blob(layout, layer)
        uncompressed = hashlib.sha256()
        opener = gzip.open if layer["mediaType"] == LAYER else open
        with opener(layout / "blobs" / "sha256" / layer["digest"][7:], "rb") as handle:
            while chunk := handle.read(1024 * 1024):
                uncompressed.update(chunk)
        if "sha256:" + uncompressed.hexdigest() != diff_id:
            raise ValueError("OCI layer does not match config diff ID")
    return descriptor, manifest, raw


def error_code(exc):
    return getattr(exc, "response", {}).get("Error", {}).get("Code")


def check_target(sts, ecr, account, region, repository):
    if sts.get_caller_identity()["Account"] != account:
        raise ValueError("Active AWS account differs from explicit target")
    registry = f"{account}.dkr.ecr.{region}.amazonaws.com"
    response = ecr.describe_repositories(
        registryId=account, repositoryNames=[repository]
    )
    repos = response["repositories"]
    if len(repos) != 1:
        raise ValueError("Expected exactly one existing repository")
    repo = repos[0]
    if (
        repo["registryId"] != account
        or repo["repositoryName"] != repository
        or repo["repositoryArn"]
        != f"arn:aws:ecr:{region}:{account}:repository/{repository}"
        or repo["repositoryUri"] != f"{registry}/{repository}"
        or repo["imageTagMutability"] != "IMMUTABLE"
    ):
        raise ValueError("Repository identity or immutable-tag policy mismatch")
    try:
        policy = json.loads(
            ecr.get_lifecycle_policy(registryId=account, repositoryName=repository)[
                "lifecyclePolicyText"
            ]
        )
    except Exception as exc:
        if error_code(exc) != "LifecyclePolicyNotFoundException":
            raise
    else:
        if any(
            rule["selection"]["tagStatus"] != "untagged" for rule in policy["rules"]
        ):
            raise ValueError(
                "Apply reviewed retention policy first: tagged inputs may expire"
            )
    return registry


def readback(ecr, account, repository, tag, expected, raw):
    try:
        response = ecr.describe_images(
            registryId=account, repositoryName=repository, imageIds=[{"imageTag": tag}]
        )
    except Exception as exc:
        if error_code(exc) == "ImageNotFoundException":
            return False
        raise
    images = response["imageDetails"]
    if len(images) != 1 or images[0]["imageDigest"] != expected:
        raise ValueError("Immutable tag collision: refusing to overwrite")
    response = ecr.batch_get_image(
        registryId=account,
        repositoryName=repository,
        imageIds=[{"imageDigest": expected}],
        acceptedMediaTypes=[MANIFEST],
    )
    images = response.get("images", [])
    if response.get("failures") or len(images) != 1:
        raise ValueError("Registry manifest readback failed")
    returned = images[0]["imageManifest"].encode()
    if returned != raw or digest(returned) != expected:
        raise ValueError("Registry manifest bytes differ from reviewed platform")
    return True


def receipt_write(path, receipt, *, create=False):
    receipt["updated_at"] = datetime.now(timezone.utc).isoformat()
    # Reserve the initial receipt exclusively; updates replace it atomically.
    # Both file and directory are flushed before a registry write can begin.
    if create:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        temporary = None
    else:
        fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temporary = Path(name)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(receipt, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        if temporary is not None:
            os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def publish(args, *, sts=None, ecr=None, run=subprocess.run):
    if args.repository not in REPOSITORIES[args.kind]:
        raise ValueError(
            "Repository is outside the selected artifact's closed target set"
        )
    if not re.fullmatch(r"[0-9]{12}", args.account) or not re.fullmatch(
        r"[a-z]{2}-[a-z]+-[0-9]+", args.region
    ):
        raise ValueError("Explicit standard AWS account and region required")
    if args.receipt.exists():
        raise ValueError("Use a new receipt path; preserve previous attempts")
    evidence_hash = digest(args.review_evidence.read_bytes())
    if evidence_hash != args.review_sha256:
        raise ValueError("Review evidence hash differs from explicit expected hash")
    descriptor, manifest, raw = inspect(
        args.layout, args.platform_digest, args.config_digest, args.kind
    )
    tag = f"{args.kind}-{args.platform_digest[7:]}"
    registry = f"{args.account}.dkr.ecr.{args.region}.amazonaws.com"
    receipt = {
        "schema": 1,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "publisher_sha256": digest(Path(__file__).read_bytes()),
        "status": "prepared",
        "kind": args.kind,
        "account": args.account,
        "region": args.region,
        "repository": args.repository,
        "tag": tag,
        "platform_digest": args.platform_digest,
        "config_digest": args.config_digest,
        "review_evidence_sha256": evidence_hash,
        "review_evidence_path": str(args.review_evidence.resolve()),
        "source_layout": str(args.layout.resolve()),
        "reference": f"{registry}/{args.repository}@{args.platform_digest}",
        "approval": "Operator supplied evidence binding; no security or release approval inferred",
    }
    receipt_write(args.receipt, receipt, create=True)
    if not args.publish:
        return receipt
    attempted = False
    try:
        if sts is None or ecr is None:
            import boto3

            session = boto3.Session(region_name=args.region)
            sts, ecr = session.client("sts"), session.client("ecr")
        check_target(sts, ecr, args.account, args.region, args.repository)
        if readback(ecr, args.account, args.repository, tag, args.platform_digest, raw):
            receipt["status"] = "reused"
        else:
            version = run(
                ["skopeo", "--version"], check=True, capture_output=True, text=True
            )
            receipt["skopeo_version"] = version.stdout.strip()
            with tempfile.TemporaryDirectory(prefix="superplane-publish-") as temporary:
                private = Path(temporary)
                selected = private / "selected"
                blobs = selected / "blobs" / "sha256"
                blobs.mkdir(parents=True)
                (selected / "oci-layout").write_text('{"imageLayoutVersion":"1.0.0"}')
                chosen = dict(
                    descriptor,
                    annotations={"org.opencontainers.image.ref.name": "reviewed"},
                )
                (selected / "index.json").write_text(
                    json.dumps(
                        {"schemaVersion": 2, "mediaType": INDEX, "manifests": [chosen]}
                    )
                )
                for item in [descriptor, manifest["config"], *manifest["layers"]]:
                    # Private snapshot: verify after copy to catch changed source bytes.
                    source = args.layout / "blobs" / "sha256" / item["digest"][7:]
                    shutil.copyfile(source, blobs / item["digest"][7:])
                    blob(selected, item)
                credentials = ecr.get_authorization_token(registryIds=[args.account])[
                    "authorizationData"
                ]
                if (
                    len(credentials) != 1
                    or credentials[0]["proxyEndpoint"] != f"https://{registry}"
                ):
                    raise ValueError("Registry authorization endpoint mismatch")
                authfile = private / "auth.json"
                fd = os.open(authfile, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w") as handle:
                    json.dump(
                        {
                            "auths": {
                                registry: {"auth": credentials[0]["authorizationToken"]}
                            }
                        },
                        handle,
                    )
                receipt["status"] = "publication_attempted"
                receipt_write(args.receipt, receipt)
                attempted = True
                # Capture transport output privately: do not leak registry credentials.
                result = run(
                    [
                        "skopeo",
                        "copy",
                        "--preserve-digests",
                        "--dest-authfile",
                        str(authfile),
                        f"oci:{selected}:reviewed",
                        f"docker://{registry}/{args.repository}:{tag}",
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                )
                if result.returncode:
                    raise RuntimeError(
                        "OCI copy failed; reconcile immutable tag using a fresh receipt"
                    )
            if not readback(
                ecr, args.account, args.repository, tag, args.platform_digest, raw
            ):
                raise RuntimeError("Published tag missing during registry readback")
            receipt["status"] = "published"
        receipt["registry_manifest_verified"] = True
        receipt_write(args.receipt, receipt)
        return receipt
    except Exception:
        # Publication may have succeeded despite a transport/readback error. Never
        # claim failure means absence, and never erase the durable attempt marker.
        receipt["status"] = "outcome_uncertain" if attempted else "blocked"
        receipt_write(args.receipt, receipt)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kind", choices=REPOSITORIES, required=True)
    for name in [
        "account",
        "region",
        "repository",
        "platform-digest",
        "config-digest",
        "review-sha256",
    ]:
        parser.add_argument(f"--{name}", required=True)
    for name in ["layout", "review-evidence", "receipt"]:
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--publish", action="store_true")
    args = parser.parse_args()
    print(json.dumps(publish(args), indent=2))


if __name__ == "__main__":
    main()
