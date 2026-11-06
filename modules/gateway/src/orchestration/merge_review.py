"""Reload an R1-verified, content-addressed review for the merge controller."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
from dataclasses import replace
from types import SimpleNamespace
from urllib.parse import urlsplit

import boto3
from botocore.config import Config
from sqlalchemy import select

from src.agentauth.artifact_keys import artifact_prefix

from .models import OrchestrationAction
from .review_cycle import CycleBlockedError
from .review_cycle_dispatch import current_author_run
from .review_evidence import _head_bound_refs, parse_review_result, require_verified_state, validate_review_result

MAX_REVIEW_BYTES = 8 * 1024 * 1024
logger = logging.getLogger(__name__)


def ci_observation_only(detail: dict) -> bool:
    """True when a review row's findings are only the harness's required-checks snapshot.

    The reviewer harness records the merge controller's canonical CI observation of
    the inspected head as one blocking finding (`finding_id` `delivery-blocked`,
    `source` `required-checks`). That is CI state at an instant, not a reviewer
    judgement: the merge controller re-observes the same head's checks live before
    any merge or delivery record. Such a row must therefore not veto an earlier
    complete review of the exact same head once the check has gone green (#7639).
    """
    try:
        cycle = json.loads(detail.get("cycle_input", "{}"))
    except (TypeError, ValueError):
        return False
    findings = cycle.get("findings") if isinstance(cycle, dict) else None
    if not isinstance(findings, list) or not findings:
        return False
    for finding in findings:
        if not isinstance(finding, dict) or finding.get("finding_id") != "delivery-blocked":
            return False
        if not _required_checks_summary(finding.get("summary")):
            return False
    return True


_REQUIRED_CHECKS_PREFIX = re.compile(r'^\s*\{\s*"source"\s*:\s*"required-checks"\s*,')


def _required_checks_summary(summary) -> bool:
    """Recognise the harness's required-checks observation even when the stored
    summary was truncated by the evidence row's length bound (the live rows cut
    at about 1 000 characters, which leaves unterminated JSON)."""
    if not isinstance(summary, str):
        return False
    try:
        observation = json.loads(summary)
    except ValueError:
        return bool(_REQUIRED_CHECKS_PREFIX.match(summary))
    return isinstance(observation, dict) and observation.get("source") == "required-checks"


def read_review_document(reference, *, org_id, reviewer_run_id, attempt, storage=None):
    """Read only the server-owned review namespace, with the original digest."""
    uri = urlsplit(reference)
    bucket = os.environ.get("AGENT_RUN_LOGS_BUCKET")
    prefix = artifact_prefix(SimpleNamespace(tenant_id=org_id, invocation_id=reviewer_run_id, current_attempt=attempt))
    digest = re.fullmatch(r"sha256=([a-f0-9]{64})", uri.fragment)
    if not bucket or uri.scheme != "s3" or uri.netloc != bucket or uri.query or digest is None:
        raise CycleBlockedError("review_artifact_unverifiable")
    key = uri.path.lstrip("/")
    if key != f"{prefix}review-result/{digest[1]}.json":
        raise CycleBlockedError("review_artifact_namespace_mismatch")
    storage = storage or boto3.client(
        "s3",
        region_name=os.environ.get("AWS_REGION", "us-east-1"),
        config=Config(connect_timeout=3, read_timeout=10, retries={"total_max_attempts": 1}),
    )
    obj = storage.get_object(Bucket=bucket, Key=key)
    with obj["Body"] as body:
        data = body.read(MAX_REVIEW_BYTES + 1)
    if not 0 < len(data) <= MAX_REVIEW_BYTES or obj.get("ContentType") != "application/json" or hashlib.sha256(data).hexdigest() != digest[1]:
        raise CycleBlockedError("review_artifact_integrity_failed")
    return json.loads(data)


async def load_merge_review(session, *, context, node, binding, reviewer_run_id, raw_execution, head_sha, storage=None, release_before_io=False):
    """Revalidate the exact bytes that authenticated R1 ingestion accepted.

    The succeeded ledger row and digest authenticate historical artifact-reference
    validation. M1 separately reobserves current provider rules, checks and reviews.
    An arbitrary upload or an own-prefix string alone cannot reach this path.

    Read-only session owners may release SQL before storage I/O. They must
    revalidate current assignment and authority after this function returns.
    """
    rows = list(
        (
            await session.scalars(
                select(OrchestrationAction)
                .where(
                    OrchestrationAction.org_id == node.org_id,
                    OrchestrationAction.execution_id == context.execution.id,
                    OrchestrationAction.kind == "review_evidence",
                    OrchestrationAction.status == "succeeded",
                )
                .order_by(OrchestrationAction.created_at.desc(), OrchestrationAction.id.desc())
                .limit(101)
            )
        ).all()
    )
    if len(rows) > 100:
        raise CycleBlockedError("review_history_limit")
    ci_snapshot_skipped = False
    for row in rows:
        detail = row.detail or {}
        cycle = json.loads(detail.get("cycle_input", "{}"))
        if cycle.get("reviewer_run_id") != reviewer_run_id or detail.get("reviewed_head_sha") != head_sha:
            continue
        if detail.get("complete_review") != "true" or detail.get("publication_outstanding") == "true" or not row.artifact_ref:
            if ci_observation_only(detail):
                # Look past a CI-only snapshot to the reviewer's own verdict for this
                # head; the controller still requires the live checks to be green.
                logger.info("merge review: skipping required-checks snapshot for head %s (row %s)", head_sha, row.id)
                ci_snapshot_skipped = True
                continue
            raise CycleBlockedError("review_incomplete")
        if release_before_io:
            session.expunge_all()
            await session.rollback()
        document = await asyncio.to_thread(
            read_review_document,
            row.artifact_ref,
            org_id=node.org_id,
            reviewer_run_id=reviewer_run_id,
            attempt=int(raw_execution["current_attempt"]["N"]),
            storage=storage,
        )
        result = parse_review_result(document)
        author = await current_author_run(session, node=node, default=binding.run_id)
        if cycle.get("author_run_id") != author:
            raise CycleBlockedError("review_author_changed")
        evidence = validate_review_result(
            document,
            identity=context.identity,
            binding=binding,
            flow_id=node.flow_id,
            author_run_id=author,
            reviewer_run_id=reviewer_run_id,
            execution_id=context.execution.id,
            actual_head_sha=head_sha,
            trusted_artifact_refs=frozenset(ref.ref for ref in _head_bound_refs(result)),
        )
        require_verified_state(evidence)
        if not evidence.is_complete_review or evidence.publication_blockers:
            raise CycleBlockedError("review_incomplete")
        return replace(evidence, artifact_ref=row.artifact_ref)
    if ci_snapshot_skipped:
        raise CycleBlockedError("review_incomplete")
    raise CycleBlockedError("fresh_verified_review_missing")
