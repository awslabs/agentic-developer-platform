"""Explicit paid image release through the existing exact-commit CodeBuild dispatcher.

No project creation, IAM enrollment, lock promotion or deployment. A receipt is
claimed before dispatch; every uncertain outcome prohibits automatic repetition.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import tempfile
import selectors
import signal
import time
from datetime import UTC, datetime
from pathlib import Path

if __package__:
    from .resolve_lock import resolve_build_inputs
    from .dispatch_claim import BuildRefused, DispatchClaim, require
else:
    from resolve_lock import resolve_build_inputs
    from dispatch_claim import BuildRefused, DispatchClaim, require

ROOT = Path(__file__).resolve().parents[4]
COMPONENT = "superplane-paid-worker"
REPOSITORY = "adp-" + COMPONENT
BUILD_TIMEOUT_MINUTES = 60
QUEUED_TIMEOUT_MINUTES = 480

BUILDSPEC = "modules/domain-apps/superplane/releases/buildspecs/paid-worker.yml"


def command(argv, *, env=None, timeout=120, on_line=None):
    if on_line is not None:
        process = subprocess.Popen(
            argv,
            cwd=ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        completed = False
        deadline = time.monotonic() + timeout
        pending = b""
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while True:
                    remaining = deadline - time.monotonic()
                    require(
                        remaining > 0 and selector.select(remaining),
                        "local polling deadline reached; cloud build outcome remains unknown",
                    )
                    chunk = os.read(process.stdout.fileno(), 65536)
                    if not chunk:
                        break
                    pending += chunk
                    require(
                        len(pending) <= 1048576,
                        "shared dispatcher output exceeds bounded line size",
                    )
                    while b"\n" in pending:
                        line, pending = pending.split(b"\n", 1)
                        on_line(line.decode("utf-8", errors="replace") + "\n")
                if pending:
                    on_line(pending.decode("utf-8", errors="replace"))
            require(
                process.wait(timeout=max(0.01, deadline - time.monotonic())) == 0,
                "shared dispatcher failed; reconcile the original build",
            )
            completed = True
            return ""
        finally:
            if not completed:
                # Terminate only this isolated local polling process group. This
                # is never CodeBuild cancellation or proof that start did not occur.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=10)
            process.stdout.close()
    try:
        result = subprocess.run(
            argv,
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise BuildRefused("build command outcome unavailable") from None
    require(
        result.returncode == 0,
        "build command failed; no retry is authorized by this result",
    )
    return result.stdout


def validate(config):
    require(
        set(config)
        == {"account", "region", "environment", "source_sha", "python_image"},
        "closed paid build inputs required",
    )
    for key, pattern in {
        "account": r"[0-9]{12}",
        "region": r"[a-z]{2}(?:-[a-z]+)+-[0-9]+",
        "environment": r"[a-z][a-z0-9-]{0,39}",
        "source_sha": r"[a-f0-9]{40}",
        "python_image": r"[a-zA-Z0-9./:_-]+@sha256:[a-f0-9]{64}",
    }.items():
        require(
            isinstance(config[key], str) and re.fullmatch(pattern, config[key]),
            "paid build input is malformed: " + key,
        )
    require(
        config["source_sha"] != "0" * 40
        and not config["python_image"].endswith("sha256:" + "0" * 64),
        "placeholder release identity refused",
    )
    project = f"adp-{config['environment']}-{COMPONENT}"
    return project, f"adp-terraform-state-{config['account']}"


def build(config, receipt, *, run=command):
    project, bucket = validate(config)
    receipt = Path(receipt)
    require(
        not receipt.exists(),
        "build receipt already exists; reconcile its original build, never automatically repeat dispatch",
    )
    sha, region, account = (config[k] for k in ("source_sha", "region", "account"))
    require(
        run(["git", "rev-parse", "HEAD"]).strip() == sha
        and not run(
            ["git", "status", "--porcelain", "--untracked-files=normal"]
        ).strip(),
        "build requires the exact clean maintained checkout",
    )
    merged = json.loads(run(["gh", "api", f"repos/aws-e/adp/compare/{sha}...main"]))
    require(
        merged.get("status") in {"ahead", "identical"}
        and merged.get("merge_base_commit", {}).get("sha") == sha,
        "release source must already be merged into maintained main",
    )
    inputs = resolve_build_inputs(COMPONENT)
    require(
        inputs.source_path == "executor" and inputs.ecr_repository == REPOSITORY,
        "paid release source/repository mismatch",
    )

    def aws(*args):
        return json.loads(run(["aws", *args, "--region", region, "--output", "json"]))

    require(
        aws("sts", "get-caller-identity").get("Account") == account,
        "build target account differs",
    )
    projects = aws("codebuild", "batch-get-projects", "--names", project)
    require(
        not projects.get("projectsNotFound") and len(projects.get("projects", [])) == 1,
        "source-owned paid build project is unavailable; provision reviewed infrastructure first",
    )
    selected = projects["projects"][0]
    role = (
        f"arn:aws:iam::{account}:role/adp-{config['environment']}-codebuild-{COMPONENT}"
    )
    registry = f"{account}.dkr.ecr.{region}.amazonaws.com"
    require(
        selected.get("name") == project
        and selected.get("arn")
        == f"arn:aws:codebuild:{region}:{account}:project/{project}"
        and selected.get("serviceRole") == role
        and selected.get("source", {}).get("type") == "S3"
        and selected["source"].get("buildspec") == BUILDSPEC
        and selected["source"].get("location")
        == f"{bucket}/codebuild/src/{project}/explicit-source-required.zip",
        "paid project identity/source/buildspec differs from the maintained project",
    )
    require(
        selected.get("artifacts", {}).get("type") == "NO_ARTIFACTS"
        and not selected.get("secondarySources")
        and not selected.get("secondaryArtifacts"),
        "paid project has unreviewed source/artifact channels",
    )
    require(
        selected.get("timeoutInMinutes") == BUILD_TIMEOUT_MINUTES
        and selected.get("queuedTimeoutInMinutes") == QUEUED_TIMEOUT_MINUTES,
        "paid project timeout/queue window differs from reviewed limits",
    )
    environment = selected.get("environment", {})
    require(
        environment.get("type") == "LINUX_CONTAINER"
        and environment.get("image") == "aws/codebuild/amazonlinux2-x86_64-standard:5.0"
        and environment.get("computeType") == "BUILD_GENERAL1_MEDIUM"
        and environment.get("privilegedMode") is True
        and environment.get("imagePullCredentialsType") == "CODEBUILD",
        "paid project build environment differs",
    )
    variables = environment.get("environmentVariables", [])
    require(
        len(variables) == 2
        and {entry.get("name") for entry in variables} == {"ACCOUNT_ID", "REGISTRY"}
        and all(entry.get("type", "PLAINTEXT") == "PLAINTEXT" for entry in variables),
        "paid project contains unreviewed environment overrides",
    )
    fixed = {entry["name"]: entry.get("value") for entry in variables}
    require(
        fixed["ACCOUNT_ID"] == account and fixed["REGISTRY"] == registry,
        "paid project registry/account differs",
    )
    repositories = aws(
        "ecr", "describe-repositories", "--repository-names", REPOSITORY
    ).get("repositories", [])
    require(
        len(repositories) == 1
        and repositories[0].get("registryId") == account
        and repositories[0].get("repositoryName") == REPOSITORY
        and repositories[0].get("repositoryArn")
        == f"arn:aws:ecr:{region}:{account}:repository/{REPOSITORY}"
        and repositories[0].get("imageTagMutability") == "IMMUTABLE",
        "paid ECR repository must be separately provisioned and immutable",
    )
    overrides = {
        "AWS_REGION": region,
        "ACCOUNT_ID": account,
        "REGISTRY": registry,
        "ECR_REPO": REPOSITORY,
        "ORIGIN_REPOSITORY": inputs.origin_repository,
        "ORIGIN_REVISION": inputs.origin_revision,
        "SOURCE_PATH": inputs.source_path,
        "SUPERPLANE_SOURCE_DIR": "modules/domain-apps/superplane/executor",
        "IMAGE_TAG": sha,
        "PYTHON_IMAGE": config["python_image"],
    }
    claim = DispatchClaim(aws, bucket, account, project, config)
    state = {
        "dispatch_id": claim.dispatch_id,
        "config_sha256": claim.config_sha256,
        "claim_bucket": bucket,
        "claim_key": claim.prefix + "claim.json",
        "version": 1,
        "build_timeout_minutes": BUILD_TIMEOUT_MINUTES,
        "queued_timeout_minutes": QUEUED_TIMEOUT_MINUTES,
        "state": "dispatch-outcome-unknown",
        "config": config,
        "project": project,
        "repository": REPOSITORY,
        "started_at": datetime.now(UTC).isoformat(),
        "build_id": None,
        "digest": None,
        "promoted": False,
    }
    receipt.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(receipt, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as output:
        json.dump(state, output)
        output.flush()
        os.fsync(output.fileno())

    def save():
        with tempfile.NamedTemporaryFile(
            mode="w", dir=receipt.parent, delete=False
        ) as output:
            temporary = Path(output.name)
            json.dump(state, output)
            output.flush()
            os.fsync(output.fileno())
        try:
            os.replace(temporary, receipt)
        finally:
            temporary.unlink(missing_ok=True)
        claim.record(state)

    def observed_line(line):
        match = re.fullmatch(
            r"  Build: ("
            + re.escape(project)
            + r":[a-f0-9-]{36}) \(source: (codebuild/src/"
            + re.escape(project)
            + "/"
            + sha
            + r"-[0-9]+-[0-9]+\.zip)\)\n?",
            line,
        )
        if match:
            require(
                state["build_id"] in (None, match[1]),
                "shared dispatcher changed original build identity",
            )
            state["build_id"] = match[1]
            state["source_key"] = match[2]
            save()
        elif line.lstrip().startswith("Build:"):
            raise BuildRefused(
                "shared dispatcher returned an unrecognized build identity"
            )

    claimed = claim.claim(state)
    state["claim_version_id"] = claimed["VersionId"]
    state["claim_etag"] = claimed["ETag"]
    save()
    try:
        run(
            [
                "bash",
                str(ROOT / "platform/scripts/codebuild-run.sh"),
                project,
                *[f"name={key},value={value}" for key, value in overrides.items()],
            ],
            env={
                **os.environ,
                "ADP_RELEASE_BUILD": "true",
                "AWS_MAX_ATTEMPTS": "1",
                "AWS_RETRY_MODE": "standard",
                "SOURCE_SHA": sha,
                "STATE_BUCKET": bucket,
                "AWS_REGION": region,
            },
            timeout=(BUILD_TIMEOUT_MINUTES + QUEUED_TIMEOUT_MINUTES + 5) * 60,
            on_line=observed_line,
        )
        require(
            state["build_id"],
            "original build identity unavailable; retain unknown outcome and reconcile externally",
        )
        builds = aws("codebuild", "batch-get-builds", "--ids", state["build_id"]).get(
            "builds", []
        )
        require(len(builds) == 1, "original build evidence unavailable")
        observed = builds[0]
        require(
            all(
                observed.get("environment", {}).get(key) == environment.get(key)
                for key in (
                    "type",
                    "image",
                    "computeType",
                    "privilegedMode",
                    "imagePullCredentialsType",
                )
            )
            and observed.get("source", {}).get("type") == "S3",
            "completed build environment/source differs",
        )
        observed_variables = observed.get("environment", {}).get(
            "environmentVariables", []
        )
        expected = {**fixed, **overrides, "ADP_SOURCE_SHA": sha}
        require(
            observed.get("timeoutInMinutes") == BUILD_TIMEOUT_MINUTES
            and observed.get("queuedTimeoutInMinutes") == QUEUED_TIMEOUT_MINUTES
            and observed.get("id") == state["build_id"]
            and observed.get("projectName") == project
            and observed.get("buildStatus") == "SUCCEEDED"
            and observed.get("serviceRole") == role
            and observed.get("source", {}).get("location")
            == f"{bucket}/{state['source_key']}"
            and observed["source"].get("buildspec") == BUILDSPEC
            and len(observed_variables) == len(expected)
            and {entry.get("name"): entry.get("value") for entry in observed_variables}
            == expected
            and all(
                entry.get("type", "PLAINTEXT") == "PLAINTEXT"
                for entry in observed_variables
            ),
            "completed build differs from exact source/project/base contract",
        )
        images = aws(
            "ecr",
            "describe-images",
            "--repository-name",
            REPOSITORY,
            "--image-ids",
            "imageTag=" + sha,
        ).get("imageDetails", [])
        require(
            len(images) == 1
            and images[0].get("registryId") == account
            and images[0].get("repositoryName") == REPOSITORY
            and sha in images[0].get("imageTags", [])
            and re.fullmatch(r"sha256:[a-f0-9]{64}", images[0].get("imageDigest", ""))
            and images[0]["imageDigest"] != "sha256:" + "0" * 64,
            "produced ECR digest evidence unavailable",
        )
        state.update(
            state="built-awaiting-image-review", digest=images[0]["imageDigest"]
        )
        save()
        return state
    except BaseException:
        # Never re-dispatch, even when start-build returned no usable build ID.
        save()
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ("account", "region", "environment", "source-sha", "python-image"):
        parser.add_argument("--" + key, required=True)
    parser.add_argument("--receipt", required=True, type=Path)
    args = vars(parser.parse_args(argv))
    receipt = args.pop("receipt")
    try:
        result = build(args, receipt)
        print(
            json.dumps(
                {
                    "state": result["state"],
                    "digest": result["digest"],
                    "receipt": str(receipt),
                }
            )
        )
        return 0
    except Exception:
        print(
            "Paid build not accepted; inspect the private receipt and reconcile any original build before retrying."
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
