"""Reload an R1-verified, content-addressed review for the merge controller."""

from __future__ import annotations

import asyncio
import hashlib
import json
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


async def load_merge_review(session, *, context, node, binding, reviewer_run_id, raw_execution, head_sha, storage=None):
    """Revalidate the exact bytes that authenticated R1 ingestion accepted.

    The succeeded ledger row and digest authenticate historical artifact-reference
    validation. M1 separately reobserves current provider rules, checks and reviews.
    An arbitrary upload or an own-prefix string alone cannot reach this path.
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
    for row in rows:
        detail = row.detail or {}
        cycle = json.loads(detail.get("cycle_input", "{}"))
        if cycle.get("reviewer_run_id") != reviewer_run_id or detail.get("reviewed_head_sha") != head_sha:
            continue
        if detail.get("complete_review") != "true" or detail.get("publication_outstanding") == "true" or not row.artifact_ref:
            raise CycleBlockedError("review_incomplete")
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
    raise CycleBlockedError("fresh_verified_review_missing")
