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

from src.orchestration.dispatch_pass import DispatchPassReport, publish_pending, run_dispatch_pass
from src.orchestration.stall import StallConfig, StallReport, detect_stalls
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

# Attribute the detection pass's report is carried on (issue #4211).
#
# `_run` returns a single `TickReport` and `handler` reads one object — that shape
# is depended on by an existing test which stubs `_run` with a minimal object
# (`_TickReportStub` in `tests/orchestration/test_tick.py`) that has only the
# tick's own attributes. Widening `_run` to a tuple, or making `_emit_metrics`
# take a second positional argument, breaks that test. It is the test that pins
# the `tick_report` token surviving `awslambdaric`'s logging setup — the exact bug
# that shipped once already — so this story attaches its report to the existing
# return value instead, and the tick's contract is unchanged.
#
# `_attached_stall_report` therefore treats "absent" as a first-class case, which
# is also what makes detection optional at the boundary rather than a hard
# dependency of the tick.
_STALL_REPORT_ATTR = "stall_report"

# Attribute the dispatch pass's report is carried on (issue #4313).
#
# Same reasoning as `_STALL_REPORT_ATTR` above, and the same constraint: `_run`
# returns one object and `handler` reads one object, because `_TickReportStub` in
# `tests/orchestration/test_tick.py` stubs `_run` with a minimal object carrying
# only the tick's own attributes. That stub is what pins the `tick_report` token
# surviving `awslambdaric`'s logging setup — the exact bug that shipped once
# already — so this story attaches its report the same way rather than widening
# `_run`'s return type. "Absent" is therefore a first-class case here too.
_DISPATCH_REPORT_ATTR = "dispatch_report"


def _attached_stall_report(report: TickReport) -> StallReport | None:
    """The detection report carried on a tick report, if one is attached."""
    return getattr(report, _STALL_REPORT_ATTR, None)


def _attached_dispatch_report(report: TickReport) -> DispatchPassReport | None:
    """The dispatch pass's report carried on a tick report, if one is attached."""
    return getattr(report, _DISPATCH_REPORT_ATTR, None)


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

        # Stall/halt counters (issue #4211). Emitted in the same call rather than
        # from a second client so a CloudWatch failure cannot leave the tick's
        # numbers landing while detection's silently do not.
        #
        # `NotificationsFailed` is the alarm-worthy one: detection working while
        # delivery fails is indistinguishable from no detection at all (R-Q9d), so
        # it must be visible as its own metric and not folded into `Errors`.
        stall_report = _attached_stall_report(report)
        if stall_report is not None:
            metric_data.extend(
                [
                    {"MetricName": "StallsDetected", "Value": stall_report.stalls_detected, "Unit": "Count"},
                    {"MetricName": "HaltsDetected", "Value": stall_report.halts_detected, "Unit": "Count"},
                    {"MetricName": "NotificationsSent", "Value": stall_report.notifications_sent, "Unit": "Count"},
                    {"MetricName": "NotificationsFailed", "Value": stall_report.notifications_failed, "Unit": "Count"},
                    {"MetricName": "StallDetectionErrors", "Value": stall_report.errors, "Unit": "Count"},
                ]
            )

            for org_id, counts in stall_report.per_org.items():
                dimensions = [{"Name": "OrgId", "Value": org_id}]
                for metric_name, key in (
                    ("StallsDetected", "stalls_detected"),
                    ("HaltsDetected", "halts_detected"),
                    ("NotificationsSent", "notifications_sent"),
                    ("NotificationsFailed", "notifications_failed"),
                ):
                    metric_data.append(
                        {
                            "MetricName": metric_name,
                            "Value": counts[key],
                            "Unit": "Count",
                            "Dimensions": dimensions,
                        }
                    )

        # Dispatch counters (issue #4313). `Dispatched` is the metric that proves
        # the engine is actually handing work to agents rather than only recording
        # that it did. `PublishFailed` is the alarm-worthy one: a node committed to
        # `running` whose envelope never reached the queue is the invisible
        # dispatch this story exists to end, so it is its own metric and not
        # folded into `Errors`.
        dispatch_report = _attached_dispatch_report(report)
        if dispatch_report is not None:
            metric_data.extend(
                [
                    {"MetricName": "DispatchesAttempted", "Value": dispatch_report.dispatches_attempted, "Unit": "Count"},
                    {"MetricName": "Dispatched", "Value": dispatch_report.dispatched, "Unit": "Count"},
                    {"MetricName": "GenesisRefused", "Value": dispatch_report.genesis_refused, "Unit": "Count"},
                    {"MetricName": "DispatchUndispatchable", "Value": dispatch_report.undispatchable, "Unit": "Count"},
                    {"MetricName": "DispatchPublishFailed", "Value": dispatch_report.publish_failed, "Unit": "Count"},
                    {"MetricName": "DispatchErrors", "Value": dispatch_report.errors, "Unit": "Count"},
                    # A pass that hit its cap has work waiting, which is
                    # operationally different from one that had nothing to do.
                    {"MetricName": "DispatchCapped", "Value": 1 if dispatch_report.capped else 0, "Unit": "Count"},
                ]
            )

            for org_id, counts in dispatch_report.per_org.items():
                dimensions = [{"Name": "OrgId", "Value": org_id}]
                for metric_name, key in (
                    ("DispatchesAttempted", "dispatches_attempted"),
                    ("Dispatched", "dispatched"),
                    ("GenesisRefused", "genesis_refused"),
                    ("DispatchUndispatchable", "undispatchable"),
                    ("DispatchPublishFailed", "publish_failed"),
                    ("DispatchErrors", "errors"),
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
    """Open a session, tick, detect stalls, dispatch, commit — then publish.

    All three passes share one session and one transaction (issues #4211, #4313).
    The tick releases nodes whose predecessors are satisfied; detection then
    diagnoses the nodes that stopped moving; dispatch then hands the released work
    to an agent. Ordering matters twice:

    - Detection runs **after** the tick, so a node the tick just moved is measured
      from its new state, not its old one.
    - Dispatch runs **after** detection, so a node detection just failed or halted
      is not dispatched in the same invocation — detection's whole job is deciding
      that some `running` work is not viable, and dispatching against a state it
      just superseded would be racing our own pass.

    Both live here rather than inside `run_tick` deliberately: they are separate
    concerns with separate reports, and keeping `tick.py` untouched means the
    existing tick tests still pin the tick's behaviour exactly as they did before
    these stories.

    **The SQS publish is outside the transaction, and after the commit** (#4313
    hazard 3, the `knowledge/dispatch.py` row-before-publish invariant).
    `run_dispatch_pass` commits nothing and returns the envelopes it intends to
    send; they are sent only once the `running` rows are durable. A send that then
    fails leaves a node `running` with no run, which #4211's detector above
    recovers on a later tick — whereas publishing first and failing to commit would
    manufacture a run the graph has no record of.

    Both reports are attached to the returned `TickReport` rather than returned
    alongside it — see `_STALL_REPORT_ATTR` for why that shape matters.
    """
    # Under IAM auth the engine caches a token that outlives a warm Lambda
    # container's usefulness; resetting gives this invocation a fresh one.
    reset_engine()

    factory = get_session_factory()
    async with factory() as session:
        try:
            report = await run_tick(session)
            # `from_env` reads `ORCH_DEFECT_CYCLE_BOUND` and never raises — a bad
            # value degrades to the default bound rather than failing the tick
            # (#4403). Read per invocation, so retuning the knob takes effect on the
            # next tick without waiting for a cold start.
            stall_report = await detect_stalls(session, config=StallConfig.from_env())
            dispatch_report = await run_dispatch_pass(session)
        except Exception:
            await session.rollback()
            raise

        # Commit even on partial failure. Every transition is individually
        # guarded and idempotent, so rolling the batch back would discard correct
        # forward motion because an unrelated node failed — and the recorded
        # rejection rows are evidence that must survive. The failure is surfaced
        # by the non-success return, not by throwing the good work away.
        await session.commit()

        # Only now, with the `running` rows durable, does anything reach the queue.
        # `publish_pending` mutates the report in place and never raises: a failed
        # send is counted as `publish_failed`, which forces a non-success report.
        publish_pending(dispatch_report)

        setattr(report, _STALL_REPORT_ATTR, stall_report)
        setattr(report, _DISPATCH_REPORT_ATTR, dispatch_report)
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

    stall_report = _attached_stall_report(report)
    dispatch_report = _attached_dispatch_report(report)

    summary = {
        # A failed detection or dispatch pass makes the whole invocation an error.
        # An undelivered stall notification is a real failure of this Lambda's job
        # (R-Q9d), not a footnote on an otherwise-green tick — and so is a dispatch
        # that committed `running` and never reached the queue (#4313).
        "status": "ok"
        if report.success and (stall_report is None or stall_report.success) and (dispatch_report is None or dispatch_report.success)
        else "error",
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

    # Issue #4211 — detection counters, on the same greppable `tick_report` line so
    # "did anything stall?" is answerable from the one line the smoke check already
    # looks for, rather than needing a second query.
    if stall_report is not None:
        summary.update(
            {
                "stalls_detected": stall_report.stalls_detected,
                "halts_detected": stall_report.halts_detected,
                "notifications_sent": stall_report.notifications_sent,
                "notifications_failed": stall_report.notifications_failed,
                "stall_errors": stall_report.errors,
            }
        )

    # Issue #4313 — dispatch counters, on the same greppable `tick_report` line.
    # `dispatched` is the field an operator uses to confirm the new code is live:
    # only this code emits it, so its presence in
    # /aws/lambda/adp-<env>-orchestration-tick is the deploy verification the
    # issue's Deployment section calls for. Not optional polish.
    if dispatch_report is not None:
        summary.update(
            {
                "dispatches_attempted": dispatch_report.dispatches_attempted,
                "dispatched": dispatch_report.dispatched,
                "genesis_refused": dispatch_report.genesis_refused,
                "dispatch_undispatchable": dispatch_report.undispatchable,
                "dispatch_publish_failed": dispatch_report.publish_failed,
                "dispatch_errors": dispatch_report.errors,
                "dispatch_capped": dispatch_report.capped,
                "dispatch_enabled": dispatch_report.enabled,
            }
        )

    # Unconditional, single-line, machine-greppable. This is the line that proves
    # the schedule fired.
    logger.info("%s %s", TICK_REPORT_TOKEN, json.dumps(summary, sort_keys=True))

    _emit_metrics(report)

    if not report.success:
        logger.error(
            "orchestration tick completed with %d error(s) — see preceding tracebacks",
            report.errors,
        )

    if stall_report is not None and not stall_report.success:
        logger.error(
            "orchestration stall detection completed with %d error(s) and %d undelivered notification(s) — see preceding tracebacks",
            stall_report.errors,
            stall_report.notifications_failed,
        )

    if dispatch_report is not None and not dispatch_report.success:
        # A publish failure names its own recovery path so an operator reading this
        # line knows the node is not lost — #4211's detector will find it.
        logger.error(
            "orchestration dispatch completed with %d error(s) and %d unpublished dispatch(es) — "
            "affected nodes remain 'running' and are recoverable by the stall detector",
            dispatch_report.errors,
            dispatch_report.publish_failed,
        )

    return summary
