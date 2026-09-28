"""Scoped incident repair. Run dry first; applying requires its exact plan hash.

Run inside the gateway's maintenance Job using the deployed image and its
database/S3 configuration. Never accepts client-supplied receipt contents.
"""

import argparse
import asyncio
import hashlib
import json
from datetime import datetime
from decimal import Decimal

import boto3
from sqlalchemy import select

from src.budget.pricing_correction import CORRECTION, correct_request
from src.shared.database import get_session_factory
from src.shared.models.budget import BudgetPricingCorrection
from src.shared.models.usage import UsageLog


def instant(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("timestamp must include timezone")
    return parsed


async def reconcile(args):
    identity = boto3.client("sts").get_caller_identity()
    if identity["Account"] != args.account_id:
        raise ValueError("maintenance account mismatch")
    if args.start >= args.end:
        raise ValueError("empty time interval")
    if args.apply and not args.plan_sha256:
        raise ValueError("apply requires reviewed plan hash")
    s3 = boto3.client("s3")
    report = {
        "correction_id": CORRECTION,
        "org_id": args.org_id,
        "account_id": args.account_id,
        "start": args.start.isoformat(),
        "end": args.end.isoformat(),
        "items": [],
        "skipped": [],
    }
    async with get_session_factory()() as db:
        rows = (
            await db.scalars(
                select(UsageLog)
                .where(
                    UsageLog.org_id == args.org_id,
                    UsageLog.timestamp >= args.start,
                    UsageLog.timestamp < args.end,
                    UsageLog.model.in_(["openai.gpt-6-sol", "openai.gpt-6-luna"]),
                )
                .order_by(UsageLog.timestamp, UsageLog.id)
            )
        ).all()
        for row in rows:
            audit = await db.get(BudgetPricingCorrection, (args.org_id, row.request_id, CORRECTION)) if row.request_id else None
            if audit:
                report["skipped"].append({"request_id": row.request_id, "reason": "already_corrected"})
                continue
            if not row.pricing_decision or "unknown_model" not in row.pricing_decision.get("estimate_reasons", []):
                report["skipped"].append({"request_id": row.request_id, "reason": "not_captured_fallback"})
                continue
            # Derive the object address from trusted SQL identity and UTC date;
            # do not follow an arbitrary supplied bucket or cross-tenant key.
            key = f"{row.org_id}/{row.user_id}/{row.timestamp:%Y/%m/%d}/{row.request_id}.json"
            try:
                response = await asyncio.to_thread(s3.get_object, Bucket=args.bucket, Key=key, ExpectedBucketOwner=args.account_id)
                payload = json.loads(response["Body"].read())
                fields = (
                    "request_id",
                    "org_id",
                    "user_id",
                    "account_type",
                    "root_human_id",
                    "team_id",
                    "department_id",
                    "timestamp",
                    "pricing_decision",
                )
                log = {field: payload.get(field) for field in fields}
                del payload
                # A failed record must not leave partial credits in the batch.
                async with db.begin_nested():
                    result = await correct_request(
                        db, org_id=args.org_id, request_id=row.request_id, log=log, source_key=key, actor=args.actor, apply=args.apply
                    )
                result.pop("status")
                report["items"].append(
                    {
                        "request_id": row.request_id,
                        "agent_run_id": row.agent_run_id,
                        "source_decision_sha256": log["pricing_decision"]["content_sha256"],
                        **result,
                    }
                )
            except Exception as exc:
                # No SQL parameters, tokens or conversation content in logs.
                skipped = {"request_id": row.request_id, "reason": type(exc).__name__}
                if type(exc) is ValueError:
                    skipped["detail"] = str(exc)
                report["skipped"].append(skipped)
        encoded = json.dumps(report, sort_keys=True, separators=(",", ":")).encode()
        digest = hashlib.sha256(encoded).hexdigest()
        report.update(
            plan_sha256=digest,
            credit_usd=str(sum((Decimal(i["credit_usd"]) for i in report["items"]), Decimal(0))),
            original_usd=str(sum((Decimal(i["original_usd"]) for i in report["items"]), Decimal(0))),
            corrected_usd=str(sum((Decimal(i["corrected_usd"]) for i in report["items"]), Decimal(0))),
            applied=args.apply,
        )
        if args.apply:
            if digest != args.plan_sha256:
                await db.rollback()
                raise ValueError("plan changed; no credits committed, run dry again")
            await db.commit()
        else:
            await db.rollback()
    print(json.dumps(report, sort_keys=True), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--org-id", required=True)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--actor", required=True)
    parser.add_argument("--start", type=instant, required=True)
    parser.add_argument("--end", type=instant, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--plan-sha256")
    asyncio.run(reconcile(parser.parse_args()))
