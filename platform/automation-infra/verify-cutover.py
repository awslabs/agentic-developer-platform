#!/usr/bin/env python3
"""Read-only prerequisite check. Never repairs a failed trust check in place."""
import argparse
import json
from pathlib import Path
import subprocess


def read(*args):
    return json.loads(subprocess.check_output(args, text=True))


def verify_environment(value):
    assert value.get("can_admins_bypass") is False, "Disable environment admin bypass"
    reviews = [r for r in value.get("protection_rules", []) if r["type"] == "required_reviewers"]
    assert len(reviews) == 1 and reviews[0].get("reviewers") and reviews[0].get("prevent_self_review") is True, "Independent environment review is required"
    assert value.get("deployment_branch_policy") == {"protected_branches": False, "custom_branch_policies": True}, "Use an exact main-only branch policy"


def verify_group(value, workflows, repository_id):
    assert value.get("visibility") == "selected", "Restrict runner group repository visibility"
    assert value.get("restricted_to_workflows") is True, "Restrict group to reviewed workflow refs"
    assert set(value.get("selected_workflows", [])) == set(workflows), "Runner group workflow allowlist differs from reviewed inventory"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--account", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--environment", required=True)
    parser.add_argument("--group-id")
    parser.add_argument("--purpose", choices=["deployment", "build", "scan"], default="deployment")
    args = parser.parse_args()
    identity = read("aws", "sts", "get-caller-identity")
    assert identity["Account"] == args.account, "Wrong AWS account"
    assert ":assumed-role/" in identity["Arn"] and ("trusted-" + args.purpose + "/") in identity["Arn"], "Run the canary under the trusted deployment identity"
    environment = {"deployment": "adp-deploy-", "build": "adp-build-", "scan": "adp-scan-"}[args.purpose] + args.environment
    verify_environment(read("gh", "api", f"repos/{args.repository}/environments/{environment}"))
    branches = read("gh", "api", f"repos/{args.repository}/environments/{environment}/deployment-branch-policies")["branch_policies"]
    assert [(b["name"], b.get("type", "branch")) for b in branches] == [("main", "branch")], "Only main may deploy"
    if args.purpose == "scan":
        print("Protected scan environment and account verified; no deployment permissions granted.")
        return
    assert args.group_id, "The deployment/build runner group ID is required"
    names = json.loads((Path(__file__).parent / "deployment-workflows.json").read_text())
    names += json.loads((Path(__file__).parent / "build-workflows.json").read_text())
    workflows = [f"{args.repository}/.github/workflows/{name}@refs/heads/main" for name in names]
    repository_id = read("gh", "api", f"repos/{args.repository}")["id"]
    group = f"orgs/{args.repository.split('/')[0]}/actions/runner-groups/{args.group_id}"
    verify_group(read("gh", "api", group), workflows, repository_id)
    repos = read("gh", "api", group + "/repositories")["repositories"]
    assert [r["id"] for r in repos] == [repository_id], "Only the deployment repository may use this group"
    print("GitHub trust and assumed deployment account verified; review saved Terraform plans before cutover.")


if __name__ == "__main__":
    main()
