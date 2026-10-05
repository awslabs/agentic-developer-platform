"""Finish a Codex engine review without generic commit/push finalization."""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from adp_review.client import ReviewError, mint_review_token, submit_review
from lib import pr_binding, review_delivery, review_result, run_report, status_gateway_client


class ReviewDeliveryBlocked(RuntimeError):
    """A completed model pass is not a completed delivery assignment."""


def require_delivery_success(output: str, envelope: dict) -> None:
    cycle = envelope.get("review_cycle_input")
    if cycle is None and not (envelope.get("source_ref") or {}).get("pr"):
        return  # Issue/design reviews have no PR to merge.
    try:
        result = json.loads(output.strip().splitlines()[-1])
    except (IndexError, ValueError, TypeError):
        raise ReviewDeliveryBlocked("Reviewer returned no verifiable delivery result") from None
    if not isinstance(result, dict):
        raise ReviewDeliveryBlocked("Reviewer returned no verifiable delivery result")
    if cycle is not None:
        required = cycle.get("reviewer_owned_delivery") is True
        merged = result.get("merged") is True
    else:
        required = os.environ.get("CODEX_REVIEWER_MERGE_ENABLED", "true") == "true"
        merged = result.get("status") == "merged"
    if required and not merged:
        raise ReviewDeliveryBlocked("Reviewer did not deliver a verified merge: " + str(
            result.get("delivery_blocked") or result.get("repair_blocked") or "unresolved review findings")[:800])


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
        if (cycle.get("allow_story_repairs") is not True or base != cycle["head_sha"]):
            raise RuntimeError("Codex repaired head is not a child of its assigned revision")
        # Both dispatch transports use the same checkpoint controller. Preserve
        # its assigned root across multiple fenced commits and base merges.
        run(["git", "merge-base", "--is-ancestor", base, head], cwd=cwd)
    elif base is not None:
        raise RuntimeError("Codex repair result has inconsistent lineage")

    report = result.get("report")
    blocked_note = f" Delivery blocked: {result['delivery_blocked'][:1000]}" if isinstance(result.get("delivery_blocked"), str) else ""
    if not isinstance(report, dict) or not isinstance(result.get("body"), str):
        raise RuntimeError("Codex did not produce a structured review")
    if cycle["action"] == "repair":
        owns_delivery = cycle.get("reviewer_owned_delivery") is True and delivery is not None
        if not owns_delivery and head == cycle["head_sha"] and report.get("verdict") != "approve":
            raise RuntimeError("Codex story repair remains blocked; no commit was published")
        # Engine repairs already target a bound PR. Keep that implementation
        # binding: the controller observes its live head and requests fresh review.
        if not owns_delivery:
            return ("Codex story repair delivered to the existing PR" if base
                    else "Codex verified the existing PR; no repair was needed")
    if delivery is None:
        raise RuntimeError("Codex engine review has no evidence assignment")
    # The retained controller publishes before merge. Repeated polls and parent
    # finalization replay the same evidence bytes instead of posting new reviews.
    fingerprint = hashlib.sha256(json.dumps({"head": head, "base": base, "report": report,
        "body": result["body"]}, sort_keys=True).encode()).hexdigest()
    cache = delivery.result_path.with_suffix(".input")
    if cache.is_file() and cache.read_text() == fingerprint and delivery.result_path.is_file():
        data = delivery.result_path.read_bytes()
        run_report.spool_review(data)
        key = status_gateway_client.upload_review_result(data)
        return f"Review evidence recorded at `{key}`." + blocked_note
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
    delivery.result_path.unlink(missing_ok=True)
    cache.write_text(fingerprint)
    return delivery.finish(reviewed_head_sha=head, repaired_from_sha=base, required=True) + blocked_note


def main():
    """Host-only evidence bridge; merge decisions and GitHub merge stay in TS."""
    value = json.load(sys.stdin)
    if os.environ.get("ADP_RUN_REPORT_CREDENTIAL_FILE"):
        run_report._assignment = {
            "credential": Path(os.environ["ADP_RUN_REPORT_CREDENTIAL_FILE"]).read_text(),
            "run_id": os.environ["ADP_MESSAGE_ID"],
            "attempt": int(os.environ["ADP_ORCHESTRATION_ATTEMPT"]),
            "tenant_id": os.environ["ADP_TENANT_ID"],
            "repo": value["cycle"]["repo"],
            "ownership_nonce": os.environ["ADP_RUN_REPORT_OWNERSHIP_NONCE"],
            "reviewer_owned_delivery": value["cycle"].get("reviewer_owned_delivery") is True,
        }
    delivery = review_delivery.ReviewDelivery(
        json.loads(os.environ[review_result.REVIEW_EXPECT_ENV]),
        Path(os.environ[review_result.AGENT_REPORT_PATH_ENV]),
        Path(os.environ[review_result.RESULT_PATH_ENV]),
    )
    try:
        finish_engine_review(json.dumps(value["result"]), envelope={"review_cycle_input": value["cycle"]},
            delivery=delivery, cwd=os.getcwd(),
            run=lambda args, **kw: subprocess.run(args, check=True, capture_output=True, text=True, **kw))
        print(json.dumps({"recorded": True, "head_sha": value["result"]["sha"]}))
    except run_report.RunReportError as error:
        print(json.dumps({"error": error.code, "retryable": error.retryable}))
    except status_gateway_client.StatusGatewayError:
        print(json.dumps({"error": "Review evidence was not accepted", "retryable": False}))


if __name__ == "__main__":
    main()
