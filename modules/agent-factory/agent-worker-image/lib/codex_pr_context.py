"""Resolve a verified PR comment's review context with scoped GitHub access."""

import json
import re


def human_pr_comment(envelope):
    """Only the protected human mention may select the narrower review-only path."""
    payload = envelope.get("payload") or {}
    sender = payload.get("sender") or {}
    author = (payload.get("comment") or {}).get("user") or {}
    correlation = envelope.get("correlation") or {}
    return (
        envelope.get("channel") == "github"
        and (envelope.get("intent") or {}).get("trigger") == "mentioned"
        and sender.get("type") == author.get("type") == "User"
        and type(sender.get("id")) is int
        and sender["id"] > 0
        and sender["id"] == author.get("id")
        and correlation.get("is_human_rooted") is True
        and bool(correlation.get("root_human_id"))
        and bool((payload.get("comment") or {}).get("body"))
    )


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
        type(pr.get("number")) is not int
        or pr.get("number") != number
        or (base.get("repo") or {}).get("full_name") != repo
        or (head.get("repo") or {}).get("full_name") != repo
        or not isinstance(head.get("ref"), str)
        or not head["ref"]
        or not re.fullmatch(r"[0-9a-f]{40}", head.get("sha", ""))
        or not base.get("ref")
    ):
        raise ValueError("PR review context does not match the assigned repository")
    merged = pr.get("state") == "closed" and pr.get("merged") is True
    if pr.get("state") != "open" and not merged:
        raise ValueError("PR is closed without a merge")
    review_only = merged or not re.fullmatch(r"agent/issue-[0-9]+", head["ref"])
    if review_only:
        if (
            not human_pr_comment(envelope)
            or not re.fullmatch(r"[0-9a-f]{40}", base.get("sha", ""))
            or (merged and not re.fullmatch(r"[0-9a-f]{40}", pr.get("merge_commit_sha", "")))
        ):
            raise ValueError("Review-only PR context requires an authenticated human comment")
        payload["review_only"] = True
    payload["pull_request"] = pr
    source["pr"] = number
    source["sha"] = head["sha"]
    return True


def checkout_review_only_pr(envelope, run, cwd):
    """Fetch the provider PR ref, including a deleted merged branch, without writes."""
    source = envelope["source_ref"]
    number, expected = source["pr"], source["sha"]
    if type(number) is not int or number <= 0 or not re.fullmatch(r"[0-9a-f]{40}", expected):
        raise ValueError("Invalid review-only checkout identity")
    run(["git", "fetch", "--no-tags", "origin", f"refs/pull/{number}/head"], cwd=cwd)
    actual = run(["git", "rev-parse", "FETCH_HEAD"], cwd=cwd).stdout.strip()
    if actual != expected:
        raise ValueError("PR head changed before review-only checkout")
    run(["git", "checkout", "--detach", expected], cwd=cwd)
    return expected


def record_context_refusal(report, acknowledge):
    """Acknowledge only a durable run-bound failure; unknown writes retain delivery."""
    message = "PR review context verification failed before review execution."
    report("failed", {"summary": message, "error_message": message})
    acknowledge()
