"""Build, exercise and publish immutable runtime images with retained provenance."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import uuid

import build_artifacts
import check_bootstrap_image
import check_job_images

ROOT = Path(__file__).resolve().parents[1]
REPOSITORIES = {name: f"adp-gitlab-runner-{name}" for name in ("bootstrap", "helper", "job")}


def command(args, **kwargs):
    result = subprocess.run(args, capture_output=True, check=False, timeout=1200, **kwargs)
    if result.returncode:
        # In particular, do not echo registry credentials or provider output.
        raise build_artifacts.BuildError(f"runtime image operation failed: {args[0]}")
    return result.stdout


def inputs(lock, root):
    previous = build_artifacts.ROOT
    try:
        build_artifacts.ROOT = root
        python_base = build_artifacts.bootstrap_base(lock)
    finally:
        build_artifacts.ROOT = previous
    upstream = "gitlab/gitlab-runner-helper:x86_64-v19.5.2@sha256:072376a64c654169ef9de96b4b40740c10f1ef1a69f913644f56b25d1d894401"
    expected_helper = (f"FROM {upstream}\nRUN install -d -m 0700 -o 10001 -g 10001 "
                       "/builds /cache /gitlab-runner-cache-init\nUSER 10001:10001\n")
    if (lock.get("upstream_helper_image") != upstream
            or (root / "bootstrap/nonroot-helper.Dockerfile").read_text() != expected_helper
            or lock.get("fleeting_plugin", {}).get("sha256") !=
            "2090afe6b2a888d1e58990dbf7b731124956d27358ff8b8532180235d9d49ebb"):
        raise build_artifacts.BuildError("reviewed helper or connector inputs differ")
    build_artifacts.verify_digest(root / "bootstrap/load_credentials.py", lock["loader_source_sha256"])
    return python_base


def save_receipt(path, receipt):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(receipt, indent=2) + "\n")
    temporary.chmod(0o600)
    temporary.replace(path)


def qualify_and_publish(images, lock, publish, receipt, save):
    # Exercise all images before publishing any of them.
    check_bootstrap_image.check(images["bootstrap"], lock["fleeting_plugin"]["sha256"])
    receipt["runtime_checks"] = check_job_images.check(images["job"], images["helper"])
    receipt["runtime_checks"]["bootstrap_both_uids_and_connector"] = "pass"
    save()
    if publish is not None:
        for name, image in images.items():
            # Record each successful publication immediately; partial runs are not retried blindly.
            receipt["images"][name] = publish(name, image)
            save()
        receipt["probe_image"] = receipt["images"]["job"]
    receipt["status"] = "images_published" if publish else "local_images_qualified"
    receipt["ami_produced"] = False
    receipt["live_qualified"] = False
    save()


def build(revision, region, account, bucket, receipt_path, plugin_file=None, root=ROOT):
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise build_artifacts.BuildError("reviewed source revision required")
    if not re.fullmatch(r"[a-z]{2}(-[a-z]+)+-[0-9]+", region) or not re.fullmatch(r"[0-9]{12}", account):
        raise build_artifacts.BuildError("exact build target required")
    if receipt_path.exists():
        raise build_artifacts.BuildError("existing build receipt requires reconciliation")
    lock = json.loads((root / "versions.lock.json").read_text())
    python_base = inputs(lock, root)
    registry = f"{account}.dkr.ecr.{region}.amazonaws.com"
    receipt = {"source_revision": revision, "account_id": account, "region": region,
               "attempt_id": str(uuid.uuid4()),
               "started_at": datetime.now(timezone.utc).isoformat(), "status": "building",
               "images": {}, "inputs": {}, "ami_produced": False, "live_qualified": False}
    receipt["local_only"] = plugin_file is not None
    receipt["builder_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    for path in ("bootstrap/Dockerfile", "bootstrap/requirements.lock", "bootstrap/load_credentials.py",
                 "bootstrap/job.Dockerfile", "bootstrap/nonroot-helper.Dockerfile"):
        receipt["inputs"][path] = hashlib.sha256((root / path).read_bytes()).hexdigest()
    local_only = plugin_file is not None
    images = {name: (f"adp-gitlab-runner-local-{name}:{revision}" if local_only else
                     f"{registry}/{repo}:{revision}") for name, repo in REPOSITORIES.items()}
    receipt_prefix = f"gitlab-runners/artifacts/{revision}/{region}"
    target_verified = False

    def save():
        save_receipt(receipt_path, receipt)
        if target_verified:
            command(["aws", "s3", "cp", str(receipt_path),
                     f"s3://{bucket}/{receipt_prefix}/attempts/{receipt['attempt_id']}/runtime-images.json",
                     "--region", region, "--only-show-errors"])

    save()
    try:
        with tempfile.TemporaryDirectory(prefix="adp-runner-build-") as directory:
            context = Path(directory)
            bootstrap = context / "bootstrap"
            bootstrap.mkdir()
            for name in ("Dockerfile", "requirements.lock", "load_credentials.py"):
                shutil.copyfile(root / "bootstrap" / name, bootstrap / name)
            plugin = bootstrap / "fleeting-plugin-aws"
            if local_only:
                build_artifacts.stage_plugin(plugin_file.read_bytes(), lock["fleeting_plugin"]["sha256"], plugin)
            else:
                identity = json.loads(command(["aws", "sts", "get-caller-identity", "--output", "json"]))
                if identity.get("Account") != account:
                    raise build_artifacts.BuildError("build account differs from reviewed target")
                target_verified = True
                save()
                build_artifacts.prepare_plugin(lock, region, bucket, plugin)
                for repository in REPOSITORIES.values():
                    response = json.loads(command(["aws", "ecr", "describe-repositories", "--region", region,
                                                   "--repository-names", repository, "--output", "json"]))
                    repos = response.get("repositories", [])
                    if (len(repos) != 1 or repos[0].get("registryId") != account
                            or repos[0].get("repositoryUri") != f"{registry}/{repository}"
                            or repos[0].get("imageTagMutability") != "IMMUTABLE"):
                        raise build_artifacts.BuildError("reviewed immutable repository is missing")
                    result = json.loads(command(["aws", "ecr", "batch-get-image", "--region", region,
                                                 "--repository-name", repository, "--image-ids", f"imageTag={revision}",
                                                 "--output", "json"]))
                    if (result.get("images") or len(result.get("failures", [])) != 1
                            or result["failures"][0].get("failureCode") != "ImageNotFound"):
                        raise build_artifacts.BuildError("image tag exists or absence unproven; reconcile before retry")
                docker_config = context / "docker-auth"
                docker_config.mkdir(mode=0o700)
                token = command(["aws", "ecr", "get-login-password", "--region", region])
                command(["docker", "--config", str(docker_config), "login", "--username", "AWS",
                         "--password-stdin", registry], input=token)
            for name, dockerfile in (("bootstrap", bootstrap / "Dockerfile"),
                                     ("helper", root / "bootstrap/nonroot-helper.Dockerfile"),
                                     ("job", root / "bootstrap/job.Dockerfile")):
                image_context = bootstrap if name == "bootstrap" else context / name
                if name != "bootstrap":
                    image_context.mkdir()
                command(["docker", "build", "--pull", "--no-cache", "--platform=linux/amd64",
                         "--build-arg", f"PYTHON_BASE={python_base}", "--label", f"org.opencontainers.image.revision={revision}",
                         "--file", str(dockerfile), "--tag", images[name],
                         str(image_context)])

            def publish(name, image):
                command(["docker", "--config", str(docker_config), "push", image])
                response = json.loads(command(["aws", "ecr", "describe-images", "--region", region,
                                               "--repository-name", REPOSITORIES[name], "--image-ids", f"imageTag={revision}",
                                               "--output", "json"]))
                details = response.get("imageDetails", [])
                if (len(details) != 1 or revision not in details[0].get("imageTags", [])
                        or not re.fullmatch(r"sha256:[0-9a-f]{64}", details[0].get("imageDigest", ""))):
                    raise build_artifacts.BuildError("published image digest not independently verified")
                return image + "@" + details[0]["imageDigest"]

            qualify_and_publish(images, lock, None if local_only else publish, receipt, save)
            if not local_only:
                command(["aws", "s3api", "put-object", "--bucket", bucket,
                         "--key", f"{receipt_prefix}/runtime-images.json", "--body", str(receipt_path),
                         "--if-none-match", "*", "--region", region])
    except Exception:
        receipt["status"] = "failed_reconcile_before_retry"
        try:
            save()
        except Exception:
            print("Private build receipt retention failed; reconcile image tags before retry", file=sys.stderr)
        raise
    return receipt


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--local-plugin", type=Path, help="Qualify locally with this checked connector; no AWS or publication")
    args = parser.parse_args()
    try:
        build(os.environ["ADP_SOURCE_SHA"], os.environ["AWS_REGION"], os.environ["EXPECTED_ACCOUNT"],
              os.environ.get("RUNNER_SOURCE_BUCKET", ""), args.receipt, args.local_plugin)
    except Exception:
        raise SystemExit("Runtime image build failed; inspect the retained private receipt before retry") from None
    print("Runtime images verified; AMI production and live qualification remain separate")
