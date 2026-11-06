#!/usr/bin/env python3
"""Preview or extend one existing flow meter without changing spend or flow state.

Run with the gateway's existing database/Redis access. Preview is read-only.
Apply requires the preview digest, snapshots the full original meter in an
append-only decision, and compares the exact hash atomically before extension.
This command cannot reconstruct a missing meter or resume a paused flow.
"""

import argparse
import asyncio
import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import boto3
from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.orchestration.flow_budget import get_flow_reservations
from src.orchestration.flow_meter import meter_target
from src.orchestration.models import OrchestrationDecision, OrchestrationFlow
from src.orchestration.policy_admission import load_in_force_policy
from src.shared.database import get_session_factory


def digest(entries):
    return hashlib.sha256(json.dumps(entries, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


async def maintain(args):
    identity = await asyncio.to_thread(boto3.client("sts").get_caller_identity)
    if identity["Account"] != args.account:
        raise ValueError("AWS account differs from the selected target")
    store = get_flow_reservations()
    try:
        async with get_session_factory()() as db:
            flow = await db.scalar(select(OrchestrationFlow).where(OrchestrationFlow.org_id == args.org, OrchestrationFlow.id == args.flow))
            if flow is None or flow.archived_at is not None:
                raise ValueError("Flow unavailable")
            inputs = await load_in_force_policy(db, org_id=args.org, flow_id=args.flow)
            if inputs.refusal is not None or inputs.policy is None or inputs.policy.expires_at <= datetime.now(UTC):
                raise ValueError("A live accepted policy is required")
            target = meter_target(org_id=args.org, flow_id=args.flow, policy=inputs.policy)
            redis = await store._get_client()
            entries = await redis.hgetall(target.key())
            if "__initialized__" not in entries:
                raise ValueError("Missing meter: retention extension cannot reconstruct accounting")
            preview = {
                "flow_id": args.flow,
                "org_id": args.org,
                "plan_version": inputs.plan_version,
                "digest": digest(entries),
                "fields": len(entries),
                "ttl_seconds": await redis.ttl(target.key()),
                "retention_seconds": target.ttl_seconds,
                "execution_paused": flow.execution_paused,
            }
            if not args.apply:
                return preview
            if args.expected_digest != preview["digest"]:
                raise ValueError("Preview digest differs; inspect a fresh preview")
            intent = OrchestrationDecision(
                org_id=args.org,
                flow_id=args.flow,
                kind="meter_retention_prepared",
                actor_id=identity["Arn"],
                actor_role="operator",
                actor_kind="service",
                reason=json.dumps({**preview, "reason": args.reason, "original_entries": entries}, sort_keys=True),
            )
            db.add(intent)
            await db.commit()
            # A policy mutation cannot race this operation. No pause/state write
            # is necessary because every retained amount and request stays intact.
            await db.scalar(select(OrchestrationFlow).where(OrchestrationFlow.id == args.flow).with_for_update())
            current = await load_in_force_policy(db, org_id=args.org, flow_id=args.flow)
            if current.refusal is not None or current.plan_version != inputs.plan_version or current.policy != inputs.policy:
                raise ValueError("Policy changed during maintenance")
            if not await store.extend_flow_retention(target, expected_entries=entries):
                raise ValueError("Meter changed or is malformed; no extension applied")
            after = await redis.hgetall(target.key())
            result = {**preview, "intent_id": intent.id, "after_digest": digest(after), "after_ttl_seconds": await redis.ttl(target.key())}
            db.add(
                OrchestrationDecision(
                    org_id=args.org,
                    flow_id=args.flow,
                    kind="meter_retention_extended",
                    actor_id=identity["Arn"],
                    actor_role="operator",
                    actor_kind="service",
                    reason=json.dumps(result, sort_keys=True),
                )
            )
            await db.commit()
            return result
    finally:
        await store.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("account", "org", "flow", "reason"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-digest")
    args = parser.parse_args()
    if not args.reason.strip():
        parser.error("--reason must be nonempty")
    print(json.dumps(asyncio.run(maintain(args)), sort_keys=True))


if __name__ == "__main__":
    main()
