#!/usr/bin/env python3
"""Read-only prerequisite check. Never repairs a failed trust check in place."""

import argparse
import json
from pathlib import Path
import subprocess


def read(*args):
    return json.loads(subprocess.check_output(args, text=True))


def verify_environment(value):
    if not (value.get("can_admins_bypass") is False):
        raise AssertionError("Disable environment admin bypass")
    reviews = [
        r
        for r in value.get("protection_rules", [])
        if r["type"] == "required_reviewers"
    ]
    if not (
        len(reviews) == 1
        and reviews[0].get("reviewers")
        and reviews[0].get("prevent_self_review") is True
    ):
        raise AssertionError("Independent environment review is required")
    if not (
        value.get("deployment_branch_policy")
        == {"protected_branches": False, "custom_branch_policies": True}
    ):
        raise AssertionError("Use an exact main-only branch policy")


def verify_group(value, workflows, repository_id):
    if not (value.get("visibility") == "selected"):
        raise AssertionError("Restrict runner group repository visibility")
    if not (value.get("restricted_to_workflows") is True):
        raise AssertionError("Restrict group to reviewed workflow refs")
    if not (set(value.get("selected_workflows", [])) == set(workflows)):
        raise AssertionError(
            "Runner group workflow allowlist differs from reviewed inventory"
        )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--account", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--environment", required=True)
    parser.add_argument("--group-id")
    parser.add_argument(
        "--purpose",
        choices=["deployment", "build", "scan", "rules", "checks"],
        default="deployment",
    )
    args = parser.parse_args()
    identity = read("aws", "sts", "get-caller-identity")
    if not (identity["Account"] == args.account):
        raise AssertionError("Wrong AWS account")
    if not (
        ":assumed-role/" in identity["Arn"]
        and ("trusted-" + args.purpose + "/") in identity["Arn"]
    ):
        raise AssertionError("Run the canary under the trusted deployment identity")
    environment = {
        "deployment": "adp-deploy-",
        "build": "adp-build-",
        "scan": "adp-scan-",
        "rules": "adp-rules-",
        "checks": "adp-checks-",
    }[args.purpose] + args.environment
    verify_environment(
        read("gh", "api", f"repos/{args.repository}/environments/{environment}")
    )
    branches = read(
        "gh",
        "api",
        f"repos/{args.repository}/environments/{environment}/deployment-branch-policies",
    )["branch_policies"]
    if not (
        [(b["name"], b.get("type", "branch")) for b in branches] == [("main", "branch")]
    ):
        raise AssertionError("Only main may deploy")
    if args.purpose in {"scan", "rules", "checks"}:
        print(
            "Protected content-processing environment and account verified; no deployment permissions granted."
        )
        return
    if not (args.group_id):
        raise AssertionError("The deployment/build runner group ID is required")
    names = json.loads(
        (Path(__file__).parent / "deployment-workflows.json").read_text()
    )
    names += json.loads((Path(__file__).parent / "build-workflows.json").read_text())
    workflows = [
        f"{args.repository}/.github/workflows/{name}@refs/heads/main" for name in names
    ]
    repository_id = read("gh", "api", f"repos/{args.repository}")["id"]
    group = (
        f"orgs/{args.repository.split('/')[0]}/actions/runner-groups/{args.group_id}"
    )
    verify_group(read("gh", "api", group), workflows, repository_id)
    repos = read("gh", "api", group + "/repositories")["repositories"]
    if not ([r["id"] for r in repos] == [repository_id]):
        raise AssertionError("Only the deployment repository may use this group")
    print(
        "GitHub trust and assumed deployment account verified; review saved Terraform plans before cutover."
    )


if __name__ == "__main__":
    main()
