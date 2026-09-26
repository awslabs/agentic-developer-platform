"""Invoke the existing shared CodeBuild CLI with reviewed native input transport."""

import argparse
import base64
import json
import os
from pathlib import Path
import re
import subprocess

import transport

build = transport.build
image = transport.image


def dispatch(args):
    checkout = Path(args.checkout).resolve()
    plan = transport.producer.read_plan(args.plan)
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
