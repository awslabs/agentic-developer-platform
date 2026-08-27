"""Lambda entrypoint for the engine tick.

Issue #4203. Deliberately thin: open a session, call `run_tick`, commit, emit
metrics, log the report, return. Every decision worth testing lives in `tick.py`,
which is why that module takes a session and knows nothing about AWS.

**Why a container-image Lambda rather than a zip.** The Design section points at
`infra/modules/budget-lambda/` as the precedent, and the Terraform shape is
copied from it — scheduled EventBridge rule, Lambda in the VPC, `rds-db:connect`
scoped to a single dbuser. The *packaging* necessarily differs. The budget
Lambdas are flat zips over `lambda/<name>/handler.py` that talk to Postgres with
raw psycopg2 via the psycopg2 layer, but this story's logic is pinned to
`src/orchestration/tick.py`, and everything under `src/` is async SQLAlchemy over
asyncpg. Three consequences made the zip route unworkable:

- No existing Lambda layer ships sqlalchemy, asyncpg or greenlet, and adding one
  needs a new CodeBuild project in `platform/infra/modules/codebuild/` — a
  *different* Terraform state that `gateway-infra-apply.yml` does not apply. The
  gateway apply would then fail at plan time on the missing layer object.
- Every `archive_file` in the repo flattens filenames, so `from src...` imports
  cannot resolve inside those zips.
- `src/shared/database.py` verifies RDS TLS against
  `/etc/ssl/certs/rds-global-bundle.pem`, a path the Dockerfile provides and the
  bare Lambda runtime does not.

The `adp-gateway` image already contains `src/`, the async stack and that CA
bundle, so running the tick from it removes all three problems at the cost of one
`awslambdaric` line in the Dockerfile. `ecr_gateway_url` was already wired into
gateway infra.

The handler emits a log line containing the literal token `tick_report` on
**every** invocation — including a no-op tick and a failing one. That token is
what proves the schedule actually fired, as opposed to merely having been
deployed, so it must never be conditional on there being work to do.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

from src.orchestration.tick import TickReport, run_tick
from src.shared.database import get_session_factory, reset_engine

logger = logging.getLogger("bedrockgateway.orchestration.tick_handler")

# Set the level on THIS logger explicitly, and never gate it on the root logger
# having no handlers. `awslambdaric` installs a root handler before importing
# this module and leaves the root level at WARNING, so a
# `if not logging.getLogger().handlers: basicConfig(level=INFO)` guard is skipped
# in exactly the environment it was meant to cover — which silently suppressed
# every `tick_report` line in dev even though the tick itself ran fine and its
# CloudWatch metrics landed. The token has to survive on a warm container with a
# pre-configured root logger, since that is the steady state. Matches the
# explicit-setLevel pattern every other Lambda in this repo already uses
# (`lambda/budget-usage-tracker/handler.py:37-38`, `lambda/pre-signup/handler.py:23-25`).
logger.setLevel(getattr(logging, os.environ.get("BG_LOG_LEVEL", "INFO").upper(), logging.INFO))
if not logging.getLogger().handlers:  # pragma: no cover - local/pytest only
    logging.basicConfig(level=logging.INFO)

# CloudWatch namespace for the engine's own metrics. Kept distinct from the
# gateway's request metrics so an alarm on "the loop stopped moving" is not
# competing with request-rate noise.
METRIC_NAMESPACE = "ADP/Orchestration"

# The literal token the deployment smoke check greps for. Named as a constant so
# it cannot drift out of sync with the check that depends on it.
TICK_REPORT_TOKEN = "tick_report"


def _emit_metrics(report: TickReport) -> None:
    """Publish counters to CloudWatch, dimensioned by org where per-org.

    Metric emission must never change the tick's outcome: the transitions are
    already committed by the time this runs, so a CloudWatch failure is logged
    and swallowed rather than being allowed to make a successful tick look
    failed. This is the one place a caught exception is *not* a silent
    degradation — the durable work is already done and the log line still lands.
    """
    try:
        import boto3

        client = boto3.client("cloudwatch", region_name=os.environ.get("AWS_REGION", "us-east-1"))

        metric_data: list[dict[str, Any]] = [
            {"MetricName": "NodesExamined", "Value": report.nodes_examined, "Unit": "Count"},
            {"MetricName": "TransitionsEffected", "Value": report.transitions_effected, "Unit": "Count"},
            {"MetricName": "TransitionsRejected", "Value": report.transitions_rejected, "Unit": "Count"},
            {"MetricName": "Errors", "Value": report.errors, "Unit": "Count"},
            {"MetricName": "LostRaces", "Value": report.lost_races, "Unit": "Count"},
            # A tick that ran but could not finish its scan is operationally
            # different from one that had nothing to do.
            {"MetricName": "Truncated", "Value": 1 if report.truncated else 0, "Unit": "Count"},
        ]

        # Per-org dimensions. Emitted alongside the totals rather than instead of
        # them, so no org's counts are ever read out of another org's view.
        for org_id, counts in report.per_org.items():
            dimensions = [{"Name": "OrgId", "Value": org_id}]
            for metric_name, key in (
                ("NodesExamined", "nodes_examined"),
                ("TransitionsEffected", "transitions_effected"),
                ("TransitionsRejected", "transitions_rejected"),
                ("Errors", "errors"),
            ):
                metric_data.append(
                    {
                        "MetricName": metric_name,
                        "Value": counts[key],
                        "Unit": "Count",
                        "Dimensions": dimensions,
                    }
                )

        # PutMetricData caps at 1000 datums per call.
        for start in range(0, len(metric_data), 1000):
            client.put_metric_data(Namespace=METRIC_NAMESPACE, MetricData=metric_data[start : start + 1000])
    except Exception:
        logger.exception("orchestration tick: failed to emit CloudWatch metrics")


async def _run() -> TickReport:
    """Open a session, tick, commit or roll back."""
    # Under IAM auth the engine caches a token that outlives a warm Lambda
    # container's usefulness; resetting gives this invocation a fresh one.
    reset_engine()

    factory = get_session_factory()
    async with factory() as session:
        try:
            report = await run_tick(session)
        except Exception:
            await session.rollback()
            raise

        # Commit even on partial failure. Every transition is individually
        # guarded and idempotent, so rolling the batch back would discard correct
        # forward motion because an unrelated node failed — and the recorded
        # rejection rows are evidence that must survive. The failure is surfaced
        # by the non-success return, not by throwing the good work away.
        await session.commit()
        return report


def handler(event: dict | None = None, context: object | None = None) -> dict:
    """EventBridge entrypoint. Returns a summary; raises only on total failure.

    A per-node failure is reported as `"status": "error"` with the counts, not
    raised — the tick did real work and the numbers should reach CloudWatch. A
    failure to run at all (no DB, bad credentials) raises, so Lambda records an
    invocation error and the EventBridge failure metric fires.
    """
    try:
        report = asyncio.run(_run())
    except Exception:
        # Log the token even on total failure: a tick that could not run is
        # exactly what the smoke check needs to be able to see (R-NF3).
        logger.exception("%s status=fatal", TICK_REPORT_TOKEN)
        raise

    summary = {
        "status": "ok" if report.success else "error",
        "nodes_examined": report.nodes_examined,
        "transitions_effected": report.transitions_effected,
        "transitions_rejected": report.transitions_rejected,
        "errors": report.errors,
        "lost_races": report.lost_races,
        "pages_read": report.pages_read,
        "truncated": report.truncated,
        "orgs": len(report.per_org),
        "blocked_nodes": len(report.blocked),
    }

    # Unconditional, single-line, machine-greppable. This is the line that proves
    # the schedule fired.
    logger.info("%s %s", TICK_REPORT_TOKEN, json.dumps(summary, sort_keys=True))

    _emit_metrics(report)

    if not report.success:
        logger.error(
            "orchestration tick completed with %d error(s) — see preceding tracebacks",
            report.errors,
        )

    return summary
