"""Replay missing direct Codex usage into the existing S3 budget tracker.

Dry-run by default. Requires an explicit person, tenant, and historical window.
Run with the gateway's database/AWS environment; see docs/codex-budget-recovery.md.
"""

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import boto3
from botocore.exceptions import ClientError
from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.chat_logging.s3_writer import ChatLogS3Writer  # noqa: E402
from src.chat_logging.schemas import ChatLog, ChatLogRequest, ChatLogResponse, ScrubbingMetadata, UsageInfo  # noqa: E402
from src.shared.database import get_session_factory  # noqa: E402
from src.shared.models.usage import UsageLog  # noqa: E402


def instant(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("include a timezone, for example 2026-09-12T00:00:00Z")
    return parsed.astimezone(UTC)


def candidates(org_id: str, user_id: str, since: datetime, until: datetime):
    # Missing S3 linkage alone is insufficient: hosted attribution cannot be
    # reconstructed from usage_logs. This recovery is for direct human use only.
    return (
        select(UsageLog)
        .where(
            UsageLog.org_id == org_id,
            UsageLog.user_id == user_id,
            UsageLog.account_type == "human",
            UsageLog.agent_run_id.is_(None),
            UsageLog.model.like("openai.%"),
            UsageLog.chat_log_s3_key.is_(None),
            UsageLog.request_id.is_not(None),
            UsageLog.request_id != "",
            UsageLog.input_tokens + UsageLog.output_tokens > 0,
            UsageLog.timestamp >= since,
            UsageLog.timestamp < until,
        )
        .order_by(UsageLog.timestamp, UsageLog.id)
    )


def replay_row(s3, bucket: str, row: UsageLog, *, apply: bool) -> str:
    timestamp = row.timestamp.replace(tzinfo=UTC) if row.timestamp.tzinfo is None else row.timestamp.astimezone(UTC)
    key = ChatLogS3Writer(bucket).generate_s3_key(row.org_id, row.user_id, row.request_id, timestamp)
    try:
        s3.head_object(Bucket=bucket, Key=key)
        return "existing"
    except ClientError as exc:
        if exc.response["Error"]["Code"] not in ("404", "NoSuchKey", "NotFound"):
            raise
    if not apply:
        return "missing"

    event = ChatLog(
        request_id=row.request_id,
        timestamp=timestamp,
        org_id=row.org_id,
        user_id=row.user_id,
        team_id=row.team_id,
        account_type="human",
        model=row.model,
        api_format="openai",
        latency_ms=row.latency_ms,
        request=ChatLogRequest(),
        response=ChatLogResponse(model=row.model, usage=UsageInfo(input_tokens=row.input_tokens, output_tokens=row.output_tokens)),
        scrubbing=ScrubbingMetadata(level="basic"),
    )
    try:
        # A retry or concurrent replay must not overwrite the object and emit a
        # second settlement event. Also catches a live writer racing the HEAD.
        s3.put_object(Bucket=bucket, Key=key, Body=event.model_dump_json().encode(), ContentType="application/json", IfNoneMatch="*")
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "PreconditionFailed":
            return "existing"
        raise
    return "queued"


async def run(args) -> dict:
    if args.until <= args.since or args.until > datetime.now(UTC):
        raise ValueError("use a nonempty historical window ending before the fix was deployed")
    async with get_session_factory()() as db:
        rows = list((await db.scalars(candidates(args.org_id, args.user_id, args.since, args.until).limit(args.max_rows + 1))).all())
    if len(rows) > args.max_rows:
        raise ValueError("too many rows; narrow the time window or explicitly increase --max-rows")
    s3 = boto3.client("s3", region_name=args.region)
    counts = {"missing": 0, "existing": 0, "queued": 0}
    recorded_cost = Decimal(0)
    for row in rows:
        outcome = await asyncio.to_thread(replay_row, s3, args.bucket, row, apply=args.apply)
        counts[outcome] += 1
        if outcome != "existing":
            recorded_cost += row.cost_usd
    return {
        "apply": args.apply,
        "org_id": args.org_id,
        "user_id": args.user_id,
        "since": args.since.isoformat(),
        "until": args.until.isoformat(),
        **counts,
        "recorded_cost_usd": str(recorded_cost),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--org-id", required=True)
    parser.add_argument("--user-id", required=True, help="Cognito sub for direct use")
    parser.add_argument("--since", type=instant, required=True)
    parser.add_argument("--until", type=instant, required=True, help="exclusive cutoff before deployment of the fix")
    parser.add_argument("--bucket", required=True, help="existing chat logs bucket with budget tracker notifications")
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--max-rows", type=int, default=10000)
    parser.add_argument("--apply", action="store_true", help="create missing S3 events; the tracker then updates budgets")
    args = parser.parse_args()
    if args.max_rows < 1:
        parser.error("--max-rows must be positive")
    print(json.dumps(asyncio.run(run(args)), indent=2))


if __name__ == "__main__":
    main()
