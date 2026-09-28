#!/usr/bin/env python3
"""Read-only credential-binding flip preflight. Missing evidence never passes."""

from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
from datetime import datetime, timedelta, timezone

UTC = timezone.utc

METRICS = (
    "CredentialAuthorizationChecked",
    "CredentialAuthorizationFromRegistry",
    "CredentialAuthorizationDrift",
    "CredentialAuthorizationFallback",
)


def timestamp(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("timestamp must include timezone")
    return result.astimezone(UTC)


def validate_observations(evidence: dict, *, now: datetime, window_days: int) -> list[str]:
    """Require matching positive daily denominators over full UTC soak days.

    A quiet day with no observations is incomplete evidence, not zero drift.
    Today's partial bucket is also checked for drift/fallback when present.
    """
    if window_days < 7:
        return ["The soak window must be at least seven days"]
    today = now.astimezone(UTC).date()
    start = today - timedelta(days=window_days)
    series = {}
    try:
        for name in METRICS:
            values = {}
            for point in evidence[name]["Datapoints"]:
                at = timestamp(point["Timestamp"])
                day = at.date()
                count = point["Sum"]
                if (
                    isinstance(count, bool)
                    or not isinstance(count, (int, float))
                    or not math.isfinite(count)
                    or count < 0
                    or not float(count).is_integer()
                    or not start <= day <= today
                    or at > now
                    or day in values
                ):
                    raise ValueError("invalid daily observation")
                values[day] = count
            series[name] = values
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
        return ["Metric evidence is missing, malformed, duplicated or outside the requested window"]

    required = {start + timedelta(days=i) for i in range(window_days)}
    all_days = set().union(*(set(v) for v in series.values()))
    errors = []
    for day in sorted(required | all_days):
        if not all(day in series[name] for name in METRICS):
            errors.append(f"{day}: incomplete credential-binding metric coverage")
            continue
        checked, registry, drift, fallback = (series[name][day] for name in METRICS)
        if checked <= 0:
            errors.append(f"{day}: no positive credential-call denominator")
        if registry != checked or fallback != 0:
            errors.append(f"{day}: registry coverage is not 100 percent")
        if drift != 0:
            errors.append(f"{day}: credential authorization drift is nonzero")
    return errors


def read_json(command: list[str]):
    # No shell, no silent error-to-zero conversion, and no credential output.
    result = subprocess.run(command, capture_output=True, text=True, timeout=60)
    if result.returncode:
        raise ValueError(f"{command[0]} {command[1]} read failed (exit {result.returncode})")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        raise ValueError(f"{command[0]} returned invalid JSON") from None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", default="dev", choices=("dev", "staging", "prod"))
    parser.add_argument("--window-days", type=int, default=7)
    parser.add_argument("--sandbox-tenant", default="adp-security-test")
    parser.add_argument("--aws-region", default="us-east-1")
    parser.add_argument("--repo", default="aws-e/adp")
    args = parser.parse_args(argv)
    if not 7 <= args.window_days <= 30:
        parser.error("--window-days must be between 7 and 30")
    now = datetime.now(UTC)
    start = datetime.combine(now.date() - timedelta(days=args.window_days), datetime.min.time(), tzinfo=UTC)
    aws = ["aws", "--region", args.aws_region]
    errors = []
    observations = {}
    for metric in METRICS:
        try:
            observations[metric] = read_json(
                aws
                + [
                    "cloudwatch",
                    "get-metric-statistics",
                    "--namespace",
                    "BedrockGateway",
                    "--metric-name",
                    metric,
                    "--start-time",
                    start.isoformat(),
                    "--end-time",
                    now.isoformat(),
                    "--period",
                    "86400",
                    "--statistics",
                    "Sum",
                    "--dimensions",
                    f"Name=Environment,Value={args.environment}",
                    "--output",
                    "json",
                ]
            )
        except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
            errors.append(f"{metric}: telemetry read unavailable ({type(exc).__name__})")
    errors.extend(validate_observations(observations, now=now, window_days=args.window_days))

    nightly = None
    try:
        runs = read_json(
            [
                "gh",
                "run",
                "list",
                "--workflow",
                "credential-binding-adversarial-e2e.yml",
                "--repo",
                args.repo,
                "--branch",
                "main",
                "--event",
                "schedule",
                "--limit",
                "1",
                "--json",
                "status,conclusion,createdAt,databaseId,headSha",
            ]
        )
        nightly = runs[0]
        age = now - timestamp(nightly["createdAt"])
        if nightly["status"] != "completed" or nightly["conclusion"] != "success" or not timedelta(0) <= age <= timedelta(hours=36):
            errors.append("Latest scheduled main nightly must be complete, successful and no older than 36 hours")
        if args.environment != "dev":
            errors.append("Existing nightly is dev-only; target-environment adversarial evidence is required")
    except (ValueError, KeyError, TypeError, IndexError, OSError, subprocess.TimeoutExpired):
        errors.append("Latest nightly evidence unavailable")

    try:
        parameter = read_json(
            aws + ["ssm", "get-parameter", "--name", f"/adp/{args.environment}/{args.sandbox_tenant}/enable-user-credentials", "--output", "json"]
        )
        if parameter["Parameter"]["Value"] not in ("true", "1"):
            errors.append("Sandbox enable-user-credentials must be true")
    except (ValueError, KeyError, TypeError, OSError, subprocess.TimeoutExpired):
        errors.append("Sandbox credential-enable evidence unavailable")

    print(
        json.dumps(
            {
                "ready": not errors,
                "environment": args.environment,
                "window_start": start.isoformat(),
                "observed_at": now.isoformat(),
                "nightly": nightly,
                "errors": errors,
            },
            indent=2,
        )
    )
    return int(bool(errors))


if __name__ == "__main__":
    sys.exit(main())
