"""Resolve a verified PR comment's review context with scoped GitHub access."""

import json
import re


def hydrate_pr_comment(envelope, run):
    payload = envelope.get("payload") or {}
    issue = payload.get("issue") or {}
    if not isinstance(issue.get("pull_request"), dict):
        return False
    source = envelope["source_ref"]
    repo, number = source["repo"], source["issue"]
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise ValueError("Invalid review repository")
    if type(number) is not int or number <= 0 or issue.get("number") != number:
        raise ValueError("Invalid review PR number")
    result = run(["gh", "api", f"repos/{repo}/pulls/{number}"])
    pr = json.loads(result.stdout)
    head, base = pr.get("head") or {}, pr.get("base") or {}
    if (
        pr.get("number") != number
        or pr.get("state") != "open"
        or (base.get("repo") or {}).get("full_name") != repo
        or (head.get("repo") or {}).get("full_name") != repo
        or not re.fullmatch(r"agent/issue-[0-9]+", head.get("ref", ""))
        or not re.fullmatch(r"[0-9a-f]{40}", head.get("sha", ""))
        or not base.get("ref")
    ):
        raise ValueError("PR review context does not match the assigned repository")
    payload["pull_request"] = pr
    source["pr"] = number
    source["sha"] = head["sha"]
    return True
