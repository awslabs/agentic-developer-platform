"""Finish a Codex engine review without generic commit/push finalization."""
from __future__ import annotations

import json
import re

from adp_review.client import ReviewError, mint_review_token, submit_review
from lib import pr_binding


def finish_engine_review(output: str, *, envelope: dict, delivery, run, cwd) -> str:
    # This is the deterministic adapter's result, not an inferred model verdict.
    result = json.loads(output.strip().splitlines()[-1])
    cycle = envelope["review_cycle_input"]
    head = result.get("sha", "")
    base = result.get("repair_base_sha")
    if (result.get("status") != "engine_reviewed"
            or not re.fullmatch(r"[0-9a-f]{40}", head)
            or run(["git", "rev-parse", "HEAD"], cwd=cwd).stdout.strip() != head):
        raise RuntimeError("Codex result does not match the inspected checkout")
    if head != cycle["head_sha"]:
        if (cycle.get("allow_story_repairs") is not True or base != cycle["head_sha"]
                or run(["git", "rev-parse", "HEAD^"], cwd=cwd).stdout.strip() != base):
            raise RuntimeError("Codex repaired head is not a child of its assigned revision")
    elif base is not None:
        raise RuntimeError("Codex repair result has inconsistent lineage")

    report = result.get("report")
    if not isinstance(report, dict) or not isinstance(result.get("body"), str):
        raise RuntimeError("Codex did not produce a structured review")
    if cycle["action"] == "repair":
        if head == cycle["head_sha"] and report.get("verdict") != "approve":
            raise RuntimeError("Codex story repair remains blocked; no commit was published")
        # A separately dispatched repair remains an authoring execution. Its next
        # reviewer must be a distinct run, even when both use the Codex persona.
        pr_binding.register_pull_request(repo=cycle["repo"], pr_number=cycle["pr_number"])
        return ("Codex story repair delivered to the existing PR" if base
                else "Codex verified the existing PR; no repair was needed")
    if delivery is None:
        raise RuntimeError("Codex engine review has no evidence assignment")
    event = {"approve": "APPROVE", "request-changes": "REQUEST_CHANGES"}.get(report.get("verdict"), "COMMENT")
    try:
        token, identity = mint_review_token(repo=cycle["repo"])
        submission = submit_review(repo=cycle["repo"], pr_number=cycle["pr_number"],
                                   event=event, body=result["body"], commit_id=head, token=token)
        submission["identity"] = identity
    except ReviewError:
        # Retain the real review and an honest publication failure. Never turn a
        # default-App comment or a failed submission into a formal approval.
        submission = {"outcome": "failed", "verdict_recorded": False}
    report = {**report, "submission": submission}
    delivery.report_path.write_text(json.dumps(report), encoding="utf-8")
    return delivery.finish(reviewed_head_sha=head, repaired_from_sha=base, required=True)
