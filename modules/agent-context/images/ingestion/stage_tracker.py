"""Stage tracker — verify-after-write contract for indexing stages.

Issue #1423: Wraps each producer stage with the discipline:
    attempt -> write row -> produce -> read-back verify -> only-then record verified

This module is the integration layer between the ingest-repo pipeline and the
per-stage Postgres tracking in db.py. It handles:
- Creating the index run header
- Starting/verifying/failing each stage
- Read-back verification of artifacts (S3 head_object, vector count, etc.)
- Skip logic keyed off 'verified' status (not SHA alone)

Usage in ingest-repo.py:
    tracker = StageTracker(conn, repo, repo_id, commit_sha)
    with tracker.stage("clone") as stage:
        # ... do clone work ...
        stage.set_artifact(clone_path)
        stage.verify(lambda: os.path.exists(clone_path))
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable

import db as stage_db
from metrics import record_stage_complete, record_stage_failed
from telemetry import (
    asset_type_var,
    owner_sub_var,
    safe_emit,
    set_correlation_context,
    tenant_id_var,
)
from tracing import get_tracer

log = logging.getLogger("stage_tracker")

# Tracer for ingestion stage spans (no-op if tracing disabled)
_tracer = get_tracer("knowledge-layer.ingestion")


@dataclass
class StageResult:
    """Result of a tracked stage execution."""

    stage: str
    status: str  # verified | failed | skipped
    artifact_ref: str | None = None
    error: str | None = None


@dataclass
class StageContext:
    """Context object passed into a stage block. Collects artifact and verification."""

    _stage_name: str
    _artifact_ref: str | None = None
    _verified: bool = False
    _error: str | None = None

    def set_artifact(self, ref: str) -> None:
        """Record the artifact reference (S3 key, path, index name, etc.)."""
        self._artifact_ref = ref

    def verify(self, check_fn: Callable[[], bool]) -> bool:
        """Run the read-back verification. Returns True if artifact confirmed.

        The check_fn should perform the actual read-back: head_object, count query, etc.
        """
        try:
            self._verified = check_fn()
            if not self._verified:
                self._error = "read-back verification failed: artifact not found"
        except Exception as e:
            self._verified = False
            self._error = f"read-back verification error: {e}"
        return self._verified

    def fail(self, error: str) -> None:
        """Explicitly mark this stage as failed (e.g., producer threw)."""
        self._verified = False
        self._error = error


class StageTracker:
    """Manages per-stage tracking for one indexing run.

    Creates the index_runs header and provides a context manager for each stage
    that implements the verify-after-write contract.
    """

    def __init__(
        self,
        conn,
        repo: str,
        repo_id: str,
        commit_sha: str | None = None,
    ):
        self._conn = conn
        self._repo = repo
        self._repo_id = repo_id
        self._commit_sha = commit_sha
        self._run_id = stage_db.create_index_run(conn, repo_id, repo, commit_sha)
        self._results: list[StageResult] = []
        # Set run_id in correlation context for all subsequent logs
        safe_emit(set_correlation_context, run_id=self._run_id)

    @property
    def run_id(self) -> str:
        """The canonical run_id for this indexing run."""
        return self._run_id

    @property
    def results(self) -> list[StageResult]:
        """All stage results collected so far."""
        return list(self._results)

    def should_skip(self, stage: str) -> bool:
        """Check if this stage is already verified at the current SHA."""
        return stage_db.should_skip_stage(self._conn, self._repo, stage, self._commit_sha)

    @contextmanager
    def stage(self, stage_name: str):
        """Context manager implementing the stage contract.

        Usage:
            with tracker.stage("deepwiki") as ctx:
                wiki = generate_wiki(repo)
                s3_key = upload_to_s3(wiki)
                ctx.set_artifact(s3_key)
                ctx.verify(lambda: s3_head_object(s3_key))

        If an exception is raised inside the block, the stage is marked failed.
        If verify() is never called or returns False, the stage is marked failed.

        Emits an OTel span per stage (child of the root ingestion_run span)
        with correlation attributes. Fail-open: span errors never block ingestion.
        """
        # Update correlation context with current stage (fail-open)
        safe_emit(set_correlation_context, stage=stage_name)

        # Build span attributes from correlation context (fail-open)
        span_attrs = {}
        try:
            span_attrs = {
                "asset_id": self._repo_id,
                "run_id": self._run_id,
                "repo_name": self._repo,
                "stage": stage_name,
                "asset_type": asset_type_var.get() or "",
                "tenant_id": tenant_id_var.get() or "",
                "owner_sub": owner_sub_var.get() or "",
            }
        except Exception:
            pass  # fail-open: missing attrs are acceptable

        ctx = StageContext(_stage_name=stage_name)
        stage_id = stage_db.start_stage(self._conn, self._run_id, self._repo, stage_name)
        stage_start_time = time.monotonic()

        # Start OTel span (fail-open: wrap in try/except so tracing never blocks)
        span_cm = None
        active_span = None
        try:
            span_cm = _tracer.start_as_current_span(stage_name, attributes=span_attrs)
            active_span = span_cm.__enter__()
        except Exception:
            span_cm = None
            active_span = None

        try:
            yield ctx
        except Exception as e:
            ctx.fail(str(e))
            # Record exception on span (fail-open)
            if active_span is not None:
                try:
                    active_span.record_exception(e)
                except Exception:
                    pass

        # Set span status based on stage result (fail-open)
        if active_span is not None:
            try:
                from opentelemetry.trace import StatusCode

                if ctx._verified and ctx._artifact_ref:
                    active_span.set_attribute("artifact_ref", ctx._artifact_ref)
                    active_span.set_status(StatusCode.OK)
                else:
                    error_msg = ctx._error or "stage completed without verification"
                    active_span.set_status(StatusCode.ERROR, error_msg)
            except Exception:
                pass  # fail-open

        # End the span (fail-open)
        if span_cm is not None:
            try:
                span_cm.__exit__(None, None, None)
            except Exception:
                pass

        # Determine final state (unchanged from pre-tracing behavior)
        stage_duration_ms = (time.monotonic() - stage_start_time) * 1000
        if ctx._verified and ctx._artifact_ref:
            stage_db.verify_stage(self._conn, stage_id, ctx._artifact_ref)
            self._results.append(StageResult(
                stage=stage_name,
                status="verified",
                artifact_ref=ctx._artifact_ref,
            ))
            # Emit metrics (fail-open via safe_emit)
            safe_emit(
                record_stage_complete,
                tenant_id=tenant_id_var.get() or "",
                stage=stage_name,
                asset_type=asset_type_var.get() or "",
                latency_ms=stage_duration_ms,
            )
        else:
            error = ctx._error or "stage completed without verification"
            stage_db.fail_stage(self._conn, stage_id, error)
            self._results.append(StageResult(
                stage=stage_name,
                status="failed",
                error=error,
            ))
            # Emit metrics (fail-open via safe_emit)
            safe_emit(
                record_stage_failed,
                tenant_id=tenant_id_var.get() or "",
                stage=stage_name,
                asset_type=asset_type_var.get() or "",
            )

    def mark_skipped(self, stage_name: str, reason: str = "disabled") -> None:
        """Record a stage as skipped (feature disabled, not applicable, etc.)."""
        stage_db.skip_stage(self._conn, self._run_id, self._repo, stage_name, reason)
        self._results.append(StageResult(
            stage=stage_name,
            status="skipped",
        ))

    def finalize(self) -> None:
        """Complete the index run header (derives overall status from stages)."""
        stage_db.complete_index_run(self._conn, self._run_id)
