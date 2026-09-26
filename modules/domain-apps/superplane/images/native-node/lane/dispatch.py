"""Invoke the existing shared CodeBuild CLI with reviewed native input transport."""

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

import transport

build = transport.build
image = transport.image


def approved_deployment(path, digest):
    image.digest(digest)
    with Path(path).open("rb") as source:
        raw = source.read(65537)
    if len(raw) > 65536 or hashlib.sha256(raw).hexdigest() != digest:
        raise image.ImageRefused("approved deployment digest differs")
    value = image.runner.decode_json(raw)
    image.exact(
        value,
        {
            "version",
            "account_id",
            "region",
            "dispatcher_role_arn",
            "project_name",
            "project",
        },
    )
    if type(value["version"]) is not int or value["version"] != 1:
        raise image.ImageRefused("unknown deployment contract")
    return value


def project_view(project):
    environment = project["environment"]
    variables = environment["environmentVariables"]
    if any(value.get("type") != "PLAINTEXT" for value in variables) or len(
        {value["name"] for value in variables}
    ) != len(variables):
        raise image.ImageRefused("project environment variable identity differs")
    return {
        "service_role_arn": project["serviceRole"],
        "environment_image": environment["image"],
        "environment_type": environment["type"],
        "compute_type": environment["computeType"],
        "privileged_mode": environment["privilegedMode"],
        "image_pull_credentials_type": environment["imagePullCredentialsType"],
        "environment_variables": {v["name"]: v["value"] for v in variables},
        "vpc_id": project["vpcConfig"]["vpcId"],
        "subnet_ids": sorted(project["vpcConfig"]["subnets"]),
        "security_group_ids": sorted(project["vpcConfig"]["securityGroupIds"]),
        "timeout_minutes": project["timeoutInMinutes"],
        "queued_timeout_minutes": project["queuedTimeoutInMinutes"],
        "concurrent_build_limit": project["concurrentBuildLimit"],
        "source_type": project["source"]["type"],
        "source_location": project["source"]["location"],
        "buildspec": project["source"]["buildspec"],
        "artifact_type": project["artifacts"]["type"],
    }


def verify_project(project, deployment):
    if (
        project.get("name") != deployment["project_name"]
        or project_view(project) != deployment["project"]
    ):
        raise image.ImageRefused("project differs from approved lane deployment")


def claim_dispatch(args, plan):
    image.pattern(args.dispatch_id, r"[A-Za-z0-9_-]{1,150}")
    claim = {
        "version": 1,
        "dispatch_id": args.dispatch_id,
        "project": args.project,
        "account_id": args.account_id,
        "region": args.region,
        "source_revision": plan["source_revision"],
        "approved_plan_sha256": args.plan_sha256,
        "approved_deployment_sha256": args.deployment_sha256,
        "status": "CLAIMED_START_NOT_CONFIRMED",
    }
    with tempfile.TemporaryDirectory(prefix="superplane-native-claim-") as directory:
        path = Path(directory) / "claim.json"
        build.write(path, claim)
        checksum = base64.b64encode(bytes.fromhex(image.file_sha(path))).decode()
        # Never retry an ambiguous claim. Existing object or lost response means
        # reconcile/refuse; neither permits a second shared-CLI invocation.
        result = transport.aws(
            args.region,
            "s3api",
            "put-object",
            "--bucket",
            args.output_bucket,
            "--key",
            "dispatch/" + args.dispatch_id + "/claim.json",
            "--body",
            str(path),
            "--if-none-match",
            "*",
            "--checksum-sha256",
            checksum,
        )
        if (
            not result.get("VersionId")
            or result["VersionId"] == "null"
            or result.get("ChecksumSHA256") != checksum
        ):
            raise image.ImageRefused(
                "dispatch claim is unconfirmed; reconcile before retry"
            )
    return claim


def dispatch(args):
    checkout = Path(args.checkout).resolve()
    plan, _ = transport.approved_plan(args.plan, args.plan_sha256)
    deployment = approved_deployment(args.deployment, args.deployment_sha256)
    if (
        deployment["account_id"],
        deployment["region"],
        deployment["dispatcher_role_arn"],
        deployment["project_name"],
    ) != (args.account_id, args.region, args.dispatcher_role, args.project):
        raise image.ImageRefused("approved deployment scope differs")
    image.pattern(args.project, r"[A-Za-z0-9_-]{1,150}")
    image.pattern(
        args.dispatcher_role, r"arn:aws:iam::[0-9]{12}:role/[A-Za-z0-9+=,.@_/-]+"
    )
    identity = transport.aws(args.region, "sts", "get-caller-identity")
    expected = f"arn:aws:sts::{args.account_id}:assumed-role/{args.dispatcher_role.rsplit('/', 1)[-1]}/"
    if (
        identity["Account"] != args.account_id
        or not identity["Arn"].startswith(expected)
        or args.dispatcher_role.split(":")[4] != args.account_id
    ):
        raise image.ImageRefused("trusted dispatcher identity differs")
    # Require the reviewed revision on the locally fetched main history, not a PR.
    subprocess.run(
        [
            "git",
            "-C",
            str(checkout),
            "merge-base",
            "--is-ancestor",
            plan["source_revision"],
            "refs/remotes/origin/main",
        ],
        check=True,
    )
    projects = transport.aws(
        args.region, "codebuild", "batch-get-projects", "--names", args.project
    )["projects"]
    if (
        len(projects) != 1
        or projects[0]["source"]["buildspec"]
        != "modules/domain-apps/superplane/releases/buildspecs/native-node-lane.yml"
    ):
        raise image.ImageRefused("dedicated native project buildspec differs")
    verify_project(projects[0], deployment)
    if projects[0]["timeoutInMinutes"] < plan["build_timeout_minutes"] + 5:
        raise image.ImageRefused("project timeout cannot cover producer cleanup window")
    fixed = {
        v["name"]: v["value"]
        for v in projects[0]["environment"]["environmentVariables"]
    }
    if fixed.get("NATIVE_DISPATCHER_ROLE_ARN") != args.dispatcher_role:
        raise image.ImageRefused("selected project dispatcher identity differs")
    if (
        fixed.get("NATIVE_ACCOUNT_ID"),
        fixed.get("AWS_REGION"),
        fixed.get("NATIVE_INPUT_BUCKET"),
        fixed.get("NATIVE_OUTPUT_BUCKET"),
    ) != (args.account_id, args.region, args.bucket, args.output_bucket):
        raise image.ImageRefused(
            "dedicated project source/account/output scope differs"
        )
    claim_dispatch(args, plan)
    transport.prepare(args)
    output = Path(args.output)
    initial = json.loads((output / "dispatch.json").read_text())
    pointer = base64.b64encode(
        json.dumps(initial["envelope"], sort_keys=True).encode()
    ).decode()
    state = {
        **initial,
        "project": args.project,
        "dispatch_id": args.dispatch_id,
        "source_revision": plan["source_revision"],
    }

    def record():
        build.write(output / "child.json", state)
        transport.upload(
            args.region,
            args.output_bucket,
            "dispatch/" + args.dispatch_id + "/child.json",
            output / "child.json",
        )

    transport.approved_plan(args.plan, args.plan_sha256)
    current = transport.aws(
        args.region, "codebuild", "batch-get-projects", "--names", args.project
    )["projects"]
    if len(current) != 1:
        raise image.ImageRefused("approved project disappeared before start")
    verify_project(current[0], deployment)
    record()  # Before shared CLI starts: unknown start is an explicit obligation.
    process = None
    try:
        process = subprocess.Popen(
            [
                "bash",
                str(checkout / "platform/scripts/codebuild-run.sh"),
                args.project,
                "name=NATIVE_ENVELOPE_B64,value=" + pointer + ",type=PLAINTEXT",
            ],
            env={
                **os.environ,
                "STATE_BUCKET": args.bucket,
                "AWS_REGION": args.region,
                "SOURCE_SHA": plan["source_revision"],
                "ADP_RELEASE_BUILD": "true",
                "AWS_MAX_ATTEMPTS": "1",
                "AWS_RETRY_MODE": "standard",
            },
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        with (output / "dispatcher.log").open("w") as log:
            for line in process.stdout:
                print(line, end="", flush=True)
                log.write(line)
                log.flush()
                match = re.fullmatch(
                    r"  Build: ([A-Za-z0-9_-]+:[a-f0-9-]+) \(source: [^\n]+\)\n?", line
                )
                if match:
                    if match[1].split(":")[0] != args.project:
                        raise image.ImageRefused(
                            "shared dispatcher returned foreign project"
                        )
                    state.update(build_id=match[1], status="IN_PROGRESS")
                    record()
        returncode = process.wait()
        if returncode == 0 and not state["build_id"]:
            raise image.ImageRefused(
                "shared dispatcher completed without durable build identity"
            )
        state.update(status="SUCCEEDED" if returncode == 0 else "FAILED_OR_UNKNOWN")
        record()
        if returncode:
            raise image.ImageRefused("shared dispatcher failed; review child receipt")
    except BaseException:
        if process is not None and process.poll() is None:
            process.terminate()  # Stops local polling only, never claims cloud cleanup.
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        state.update(status="UNKNOWN_REVIEW_REQUIRED")
        record()
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in (
        "checkout",
        "account-id",
        "region",
        "project",
        "dispatcher-role",
        "plan",
        "plan-sha256",
        "deployment",
        "deployment-sha256",
        "inputs",
        "packer",
        "amazon-plugin",
        "output",
        "bucket",
        "output-bucket",
        "dispatch-id",
    ):
        parser.add_argument("--" + name, required=True)
    dispatch(parser.parse_args())


if __name__ == "__main__":
    main()
