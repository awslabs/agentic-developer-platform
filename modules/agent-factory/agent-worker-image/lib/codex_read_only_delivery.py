"""Publish a source-only review through the existing distinct reviewer identity."""

import json
import re
import sys

from adp_review.client import mint_review_token, submit_review


def publish(value, *, mint=mint_review_token, submit=submit_review):
    if (
        not isinstance(value, dict)
        or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value.get("repo", ""))
        or any(part in {".", ".."} for part in value["repo"].split("/"))
        or type(value.get("pr_number")) is not int
        or value["pr_number"] <= 0
        or not re.fullmatch(r"[0-9a-f]{40}", value.get("head", ""))
        or value.get("event") not in {"APPROVE", "REQUEST_CHANGES", "COMMENT"}
        or not isinstance(value.get("body"), str)
        or not 1 <= len(value["body"].encode()) <= 60000
    ):
        raise ValueError("Invalid read-only review publication")
    token, _ = mint(repo=value["repo"])
    result = submit(
        repo=value["repo"],
        pr_number=value["pr_number"],
        event=value["event"],
        body=value["body"],
        commit_id=value["head"],
        token=token,
    )
    # These are provider response fields, never defaults copied from the request.
    return {
        key: result.get(key)
        for key in (
            "verdict_recorded",
            "state",
            "commit_id",
            "review_id",
            "outcome",
            "refusal_reason",
        )
    }


def main():
    raw = sys.stdin.read(65537)
    if len(raw.encode()) > 65536:
        raise ValueError("Read-only review publication exceeds limit")
    print(json.dumps(publish(json.loads(raw))))


if __name__ == "__main__":
    main()
