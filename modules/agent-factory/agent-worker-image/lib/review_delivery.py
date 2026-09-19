"""Deliver the dispatched review's structured evidence before its run terminates."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from lib import review_result, status_gateway_client

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReviewDelivery:
    expectation: dict
    report_path: Path
    result_path: Path

    def finish(self, *, reviewed_head_sha: str) -> str:
        """Keep evidence failure visible without erasing the run's delivered review."""
        try:
            note = review_result.reviewer_evidence_note(
                repo=self.expectation.get("repo", ""),
                pr_number=self.expectation.get("pr_number"),
                provider_repository_id=self.expectation.get("provider_repository_id"),
                provider_pr_node_id=self.expectation.get("provider_pr_node_id", ""),
                reviewed_head_sha=reviewed_head_sha,
                report_path=str(self.report_path),
                result_path=str(self.result_path),
            )
            if not self.result_path.is_file():
                return (
                    note or "> **Review evidence not produced.** No structured result was written."
                )
            with self.result_path.open("rb") as source:
                data = source.read(256 * 1024 + 1)
            key = status_gateway_client.upload_review_result(data)
            return f"{note}\n\n> Review evidence recorded at `{key}`. Recording grants no merge approval."
        except Exception as error:
            # Never expose transport exceptions or model-provided document values.
            logger.warning("Review evidence delivery failed (%s)", type(error).__name__)
            detail = (
                str(error)
                if isinstance(error, status_gateway_client.StatusGatewayError)
                else "result production failed"
            )
            return (
                f"> **Review evidence not recorded.** {detail}. "
                "The local result is retained for diagnosis; this run grants "
                "no autonomous approval."
            )


def prepare_review_delivery(envelope: dict) -> ReviewDelivery | None:
    """Export only this dispatch's inputs; never reuse another run's report files."""
    for name in (
        review_result.REVIEW_EXPECT_ENV,
        review_result.AGENT_REPORT_PATH_ENV,
        review_result.RESULT_PATH_ENV,
    ):
        os.environ.pop(name, None)
    expectation = envelope.get("review_expect")
    if (
        envelope.get("persona") != "reviewer"
        or not isinstance(expectation, dict)
        or not expectation
    ):
        return None
    directory = Path(tempfile.mkdtemp(prefix="adp-review-"))
    delivery = ReviewDelivery(
        dict(expectation), directory / "report.json", directory / "result.json"
    )
    os.environ[review_result.REVIEW_EXPECT_ENV] = json.dumps(expectation, sort_keys=True)
    os.environ[review_result.AGENT_REPORT_PATH_ENV] = str(delivery.report_path)
    os.environ[review_result.RESULT_PATH_ENV] = str(delivery.result_path)
    return delivery
