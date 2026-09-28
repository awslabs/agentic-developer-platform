"""Phase verification runner.

Orchestrates check execution, streams console output, uploads JSON evidence
to S3, updates DynamoDB status, and writes GitHub Step Summary.

Usage:
    python -m platform_deploy_mgmt.checks.runner --phase=1
"""

from __future__ import annotations

import argparse
import importlib
import os
import sys
import time
from datetime import datetime, timezone

from .boto_helpers import Context, build_context_from_env
from .shape import (
    CheckResult,
    PhaseEvidence,
    Result,
    RunMetadata,
    RunTarget,
    Severity,
    now_iso,
)

# Phase number -> (module_name, phase_display_name)
PHASE_REGISTRY: dict[int, tuple[str, str]] = {
    1: ("platform_deploy_mgmt.checks.phase_1", "Bootstrap state backend"),
}


def run_phase(phase: int, ctx: Context) -> int:
    """Execute all checks for a phase and handle reporting.

    Returns:
        0 if all hard checks pass, 1 otherwise.
    """
    if phase not in PHASE_REGISTRY:
        print(f"ERROR: Phase {phase} not registered. Available: {list(PHASE_REGISTRY.keys())}")
        return 1

    module_name, phase_name = PHASE_REGISTRY[phase]
    module = importlib.import_module(module_name)
    checks = module.CHECKS

    print(f"\n{'=' * 60}")
    print(f"  Phase {phase}: {phase_name}")
    print(f"  Target: {ctx.customer_account_id} ({ctx.environment})")
    print(f"{'=' * 60}\n")

    started_at = now_iso()
    start_time = time.perf_counter()
    results: list[CheckResult] = []

    for check_id, check_name, check_fn, severity, cost_class in checks:
        # Print check start (no newline for inline result)
        print(f"  [{check_id}] {check_name} ", end="", flush=True)

        try:
            result = check_fn(ctx)
        except Exception as e:
            result = CheckResult(
                id=check_id,
                name=check_name,
                result=Result.FAIL,
                severity=severity,
                duration_ms=0,
                detail=f"Unhandled exception: {e}",
                evidence={"exception": str(e)},
            )

        results.append(result)

        # Print result inline
        icon = {"pass": "✓", "fail": "✗", "skip": "⊘"}[result.result.value]
        color_code = {"pass": "32", "fail": "31", "skip": "33"}[result.result.value]
        print(f"\033[{color_code}m{icon} {result.result.value.upper()}\033[0m ({result.duration_ms}ms)")
        if result.result == Result.FAIL:
            print(f"         └─ {result.detail}")

    finished_at = now_iso()
    duration_seconds = time.perf_counter() - start_time

    # Build evidence
    run_meta = RunMetadata(
        workflow_run_id=ctx.workflow_run_id,
        workflow_run_url=ctx.workflow_run_url,
        started_at=started_at,
        finished_at=finished_at,
        duration_seconds=round(duration_seconds, 2),
    )
    target = RunTarget(
        account_id=ctx.customer_account_id,
        region=ctx.region,
        environment=ctx.environment,
    )
    evidence = PhaseEvidence.from_results(phase, phase_name, target, run_meta, results)

    # Print summary
    print(f"\n{'─' * 60}")
    s = evidence.summary
    print(f"  Summary: {s.passed} passed, {s.failed} failed, {s.skipped} skipped")
    print(f"  Overall: {evidence.result.upper()}")
    print(f"  Duration: {duration_seconds:.1f}s")
    print(f"{'─' * 60}\n")

    # Upload evidence and update status
    _upload_evidence(ctx, phase, evidence)
    _update_deployment_status(ctx, phase, evidence)
    _write_step_summary(phase, phase_name, evidence)

    # Exit code: 1 if any hard check failed
    has_hard_failure = any(r.result == Result.FAIL and r.severity == Severity.HARD for r in results)
    return 1 if has_hard_failure else 0


def _upload_evidence(ctx: Context, phase: int, evidence: PhaseEvidence) -> None:
    """Upload JSON evidence to S3 (platform account)."""
    try:
        s3 = ctx.platform_session.client("s3")
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        key = f"{ctx.customer_account_id}/{ctx.mode}/phase-{phase}/{timestamp}.json"
        s3.put_object(
            Bucket=ctx.evidence_bucket_name,
            Key=key,
            Body=evidence.to_json().encode("utf-8"),
            ContentType="application/json",
            ServerSideEncryption="AES256",
        )
        print(f"  Evidence uploaded: s3://{ctx.evidence_bucket_name}/{key}")
    except Exception as e:
        # Evidence upload failure is not fatal — log and continue
        print(f"  WARNING: Failed to upload evidence to S3: {e}", file=sys.stderr)


def _update_deployment_status(ctx: Context, phase: int, evidence: PhaseEvidence) -> None:
    """Update DynamoDB deployment status (platform account)."""
    try:
        dynamodb = ctx.platform_session.resource("dynamodb")
        table = dynamodb.Table(ctx.deployments_table_name)
        deploy_id = ctx.deploy_id or f"{ctx.customer_account_id}-{ctx.environment}"
        table.update_item(
            Key={"deployment_id": deploy_id, "phase": phase},
            UpdateExpression=(
                "SET #result = :result, "
                "last_check_passed_at = :ts, "
                "customer_account_id = :acct, "
                "#status = :status, "
                "summary_passed = :passed, "
                "summary_failed = :failed, "
                "summary_total = :total, "
                "#mode = :mode"
            ),
            ExpressionAttributeNames={
                "#result": "result",
                "#status": "status",
                "#mode": "mode",
            },
            ExpressionAttributeValues={
                ":result": evidence.result,
                ":ts": evidence.run.finished_at,
                ":acct": ctx.customer_account_id,
                ":status": evidence.result,
                ":passed": evidence.summary.passed,
                ":failed": evidence.summary.failed,
                ":total": evidence.summary.total,
                ":mode": ctx.mode,
            },
        )
        print(f"  DynamoDB updated: {ctx.deployments_table_name} (deploy_id={deploy_id}, phase={phase})")
    except Exception as e:
        # DDB update failure is not fatal — log and continue
        print(f"  WARNING: Failed to update DynamoDB: {e}", file=sys.stderr)


def _write_step_summary(phase: int, phase_name: str, evidence: PhaseEvidence) -> None:
    """Write GitHub Step Summary markdown."""
    summary_file = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_file:
        return

    lines = [
        f"## Phase {phase}: {phase_name}",
        "",
        f"**Result**: {evidence.result.upper()}",
        f"**Target**: {evidence.target.account_id} ({evidence.target.environment})",
        f"**Duration**: {evidence.run.duration_seconds}s",
        "",
        "| Check | Result | Duration | Detail |",
        "|-------|--------|----------|--------|",
    ]
    for check in evidence.checks:
        icon = {"pass": "✅", "fail": "❌", "skip": "⏭️"}[check.result.value]
        lines.append(
            f"| [{check.id}] {check.name} | {icon} {check.result.value} | {check.duration_ms}ms | {check.detail} |"
        )
    s = evidence.summary
    lines.extend(
        [
            "",
            f"**Summary**: {s.passed} passed, {s.failed} failed, {s.skipped} skipped",
        ]
    )

    try:
        with open(summary_file, "a") as f:
            f.write("\n".join(lines) + "\n")
    except OSError:
        pass


def main() -> None:
    """CLI entrypoint."""
    parser = argparse.ArgumentParser(description="Run phase verification checks")
    parser.add_argument("--phase", type=int, required=True, help="Phase number to verify (1-9)")
    args = parser.parse_args()

    ctx = build_context_from_env()
    exit_code = run_phase(args.phase, ctx)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
