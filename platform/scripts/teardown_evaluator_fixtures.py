"""Prepare/apply state-owned evaluator smoke fixture deletion before platform teardown."""

import argparse
import hashlib
import json
import os
import re
from pathlib import Path

import teardown as shared

OWNED = {"aws_security_group.evaluator", "aws_iam_role.browser", "aws_iam_role_policy.browser"}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def validate(state, plan, account, target):
    original = {a: (k, v) for a, k, v in shared.instances(state)}
    require(set(original) <= OWNED and original, "Fixture state is empty or contains unknown resources")
    rows = shared.deletions(plan)
    for row in rows:
        address, actual = row["address"], row["change"]["before"]
        require(address in original, "Unrecorded fixture deletion")
        kind, attrs = original[address]
        require(row["type"] == kind, "Fixture type changed")
        # tags_all binds effective ownership regardless of provider-default attribution.
        for field in ("id", "arn", "unique_id", "tags_all", "vpc_id"):
            require(actual.get(field) == attrs.get(field), "Fixture identity changed: " + field)
        if kind in ("aws_iam_role", "aws_security_group"):
            require(attrs.get("tags_all", {}).get("ManagedBy") == "evaluator-setup"
                    and attrs.get("tags_all", {}).get("Purpose") == "e2e-testing", "Fixture ownership tags differ")
            suffix = "evaluator" if kind == "aws_security_group" else "browser-trusted-checks"
            require(attrs["name"] == f"adp-{target}-{suffix}", "Fixture name differs")
            require(attrs["arn"].split(":")[4] == account, "Fixture account differs")
        elif kind == "aws_iam_role_policy":
            require(attrs["role"] == f"adp-{target}-browser-trusted-checks" and attrs["name"] == "browser-acceptance-fixtures", "Fixture policy owner differs")
    return rows


def verify_live(rows, account):
    for row in rows:
        before = row["change"]["before"]
        if row["type"] == "aws_security_group":
            groups = shared.aws("ec2", "describe-security-groups", "--group-ids", before["id"], absent=("InvalidGroup.NotFound",))
            if groups is None:
                continue
            group = groups["SecurityGroups"][0]
            tags = {t["Key"]: t["Value"] for t in group.get("Tags", [])}
            require(group["OwnerId"] == account and group["VpcId"] == before["vpc_id"] and group["GroupName"] == before["name"]
                    and tags == before["tags_all"], "Live fixture group ownership differs")
            interfaces = shared.aws("ec2", "describe-network-interfaces", "--filters", "Name=group-id,Values=" + before["id"])
            require(not interfaces["NetworkInterfaces"], "Evaluator fixture is still in use")
        elif row["type"] == "aws_iam_role":
            role = shared.aws("iam", "get-role", "--role-name", before["name"], absent=("NoSuchEntity",))
            if role:
                require(role["Role"]["RoleId"] == before["unique_id"] and role["Role"]["Arn"] == before["arn"], "Fixture role was replaced")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("account", "region", "target", "output-dir"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    require(re.fullmatch(r"[a-z][a-z0-9-]{0,39}", args.target), "Invalid evaluator target")
    require(not any(k.startswith("TF_CLI_ARGS") for k in os.environ), "Unset implicit Terraform CLI arguments")
    os.umask(0o077)
    os.environ.update(AWS_REGION=args.region, AWS_DEFAULT_REGION=args.region)
    require(shared.aws("sts", "get-caller-identity")["Account"] == args.account, "AWS account differs")
    directory = Path(args.output_dir).resolve()
    target = {"account": args.account, "region": args.region, "target": args.target}
    if not args.apply:
        require(not directory.exists(), "Use a new private output directory")
        directory.mkdir(parents=True, mode=0o700)
        shared.write_json(directory / "main.tf.json", {
            "terraform": {"required_providers": {"aws": {"source": "hashicorp/aws", "version": "~> 6.0"}}, "backend": {"s3": {}}},
            "provider": {"aws": {"region": args.region, "allowed_account_ids": [args.account],
                "default_tags": {"tags": {"ManagedBy": "evaluator-setup", "Purpose": "e2e-testing"}}}},
        })
        shared.write_json(directory / "backend.json", {
            "bucket": f"adp-terraform-state-{args.account}",
            "key": f"evaluator-setup/{args.target}-smoke-fixtures.tfstate",
            "region": args.region, "encrypt": True, "use_lockfile": True,
        })
    def tf(label, *command):
        result = shared.command(["terraform", *command], cwd=directory)
        (directory / f"{label}.log").write_text(result)
        return result
    if not args.apply:
        tf("init", "init", "-input=false", "-backend-config=backend.json")
        state = json.loads(tf("before", "state", "pull"))
        shared.write_json(directory / "before.tfstate", state)
        require({a for a, _, _ in shared.instances(state)} <= OWNED, "Fixture state contains other owners")
        tf("plan", "plan", "-destroy", "-input=false", "-lock-timeout=60s", "-out=delete.tfplan")
        plan = json.loads(tf("show", "show", "-json", "delete.tfplan"))
        rows = validate(state, plan, args.account, args.target)
        verify_live(rows, args.account)
        shared.write_json(directory / "delete.json", plan)
        shared.write_json(directory / "review.json", dict(target, lineage=state["lineage"], serial=state["serial"],
            sha256=hashlib.sha256((directory / "delete.tfplan").read_bytes()).hexdigest()))
        print(f"Prepared {len(rows)} evaluator fixture deletions; review private delete.json")
        return
    review = json.loads((directory / "review.json").read_text())
    require(all(review.get(k) == v for k, v in target.items()), "Reviewed fixture target differs")
    require(hashlib.sha256((directory / "delete.tfplan").read_bytes()).hexdigest() == review["sha256"], "Reviewed fixture plan changed")
    state = json.loads(tf("apply-state", "state", "pull"))
    require(state["lineage"] == review["lineage"] and state["serial"] == review["serial"], "Fixture state changed; prepare again")
    backend = json.loads((directory / ".terraform/terraform.tfstate").read_text())["backend"]
    require(backend["type"] == "s3"
            and backend["config"]["bucket"] == f"adp-terraform-state-{args.account}"
            and backend["config"]["key"] == f"evaluator-setup/{args.target}-smoke-fixtures.tfstate"
            and backend["config"]["region"] == args.region, "Fixture backend target changed")
    plan = json.loads(tf("apply-show", "show", "-json", "delete.tfplan"))
    require(plan == json.loads((directory / "delete.json").read_text()), "Reviewed fixture rendering changed")
    rows = validate(state, plan, args.account, args.target)
    verify_live(rows, args.account)
    tf("apply", "apply", "-input=false", "-lock-timeout=60s", "delete.tfplan")
    after = json.loads(tf("after", "state", "pull"))
    require(not list(shared.instances(after)), "Fixture resources remain in state")
    for row in rows:
        before = row["change"]["before"]
        if row["type"] == "aws_security_group":
            require(shared.aws("ec2", "describe-security-groups", "--group-ids", before["id"],
                    absent=("InvalidGroup.NotFound",)) is None, "Fixture security group remains")
        if row["type"] == "aws_iam_role":
            require(shared.aws("iam", "get-role", "--role-name", before["name"],
                    absent=("NoSuchEntity",)) is None, "Fixture role remains")
    verifier = shared.Run(Path(__file__).resolve().parents[2], args.account, args.region, args.target)
    verifier.directory = directory
    verifier.record_deletions("fixtures", rows)
    verifier.verify("fixtures")
    shared.write_json(directory / "complete.json", target)
    print("Evaluator smoke fixture resources removed and verified")


if __name__ == "__main__":
    main()
