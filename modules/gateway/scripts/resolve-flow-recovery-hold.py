#!/usr/bin/env python3
"""Preview an audited owner adjustment for a synthetic historical recovery hold.

Apply is an explicit acceptance of uncertainty, not a claim of reconstructed
usage. It requires the preview snapshot and an owner's approval reference. It
never resets spending, changes limits, or settles ordinary model requests.
"""

import argparse
import asyncio
import json
import sys
from decimal import Decimal
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

import boto3
from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.orchestration.budget_read import read_flow_budget
from src.orchestration.flow_budget import get_flow_reservations
from src.orchestration.flow_meter import meter_target
from src.orchestration.meter_recovery import release_recovery_hold
from src.orchestration.models import OrchestrationDecision, OrchestrationFlow
from src.orchestration.policy_admission import load_in_force_policy
from src.orchestration.zero_usage_recovery import digest
from src.shared.database import get_session_factory


async def resolve(args):
    UUID(args.recovery)
    identity = await asyncio.to_thread(boto3.client("sts").get_caller_identity)
    if identity["Account"] != args.account:
        raise ValueError("AWS account differs from the selected target")
    store = get_flow_reservations()
    try:
        async with get_session_factory()() as db:
            flow = await db.scalar(select(OrchestrationFlow).where(OrchestrationFlow.org_id == args.org, OrchestrationFlow.id == args.flow))
            if flow is None:
                raise ValueError("Flow unavailable")
            inputs = await load_in_force_policy(db, org_id=args.org, flow_id=args.flow)
            if inputs.refusal or inputs.policy is None:
                raise ValueError("Accepted flow policy unavailable")
            target = meter_target(org_id=args.org, flow_id=args.flow, policy=inputs.policy)
            decisions = (
                await db.scalars(
                    select(OrchestrationDecision).where(
                        OrchestrationDecision.org_id == args.org,
                        OrchestrationDecision.flow_id == args.flow,
                        OrchestrationDecision.kind == "meter_ceiling_restored",
                    )
                )
            ).all()
            matches = []
            for decision in decisions:
                reason = json.loads(decision.reason or "{}")
                if reason.get("recovery_id") == args.recovery:
                    matches.append((decision, reason))
            if len(matches) != 1:
                raise ValueError("A unique matching recovery decision is required")
            restored, recovery = matches[0]
            prepared = await db.get(OrchestrationDecision, recovery["intent_id"])
            if prepared is None or prepared.org_id != args.org or prepared.flow_id != args.flow or prepared.kind != "meter_ceiling_prepared":
                raise ValueError("Original recovery audit unavailable")
            inventory = json.loads(prepared.reason)
            if inventory.get("recovery_id") != args.recovery or inventory.get("unreconciled_exposure_hold_usd") != recovery["unreconciled_hold_usd"]:
                raise ValueError("Original recovery records disagree")
            amount = Decimal(recovery["unreconciled_hold_usd"])
            client = await store._get_client()
            value = await client.hget(target.key(), "recovery-" + args.recovery)
            if value is None:
                raise ValueError("Recovery hold is missing")
            current_amount = Decimal(value.split(":")[0])
            if current_amount == 0:
                proof = await client.get(target.key() + ":recovery-resolution:" + args.recovery)
                if proof:
                    return {"already_resolved": True, "decision_id": proof, "recorded_spend_unchanged": True}
            if current_amount != amount:
                raise ValueError("Recovery hold changed; do not overwrite it")
            budget = await read_flow_budget(db, flow)
            preview = {
                "account": args.account,
                "org": args.org,
                "flow": args.flow,
                "recovery_id": args.recovery,
                "recovery_decision_id": restored.id,
                "release_usd": str(amount),
                "reason": args.reason,
                "flow_limit_usd": str(inputs.policy.limits.max_spend_usd),
                "plan_version": inputs.plan_version,
                "policy_digest": digest(inputs.policy.model_dump(mode="json")),
                "run_limit_usd": str(budget.run_limit_usd),
                "resolution": "owner_accepts_unverified_historical_exposure",
                "historical_usage_reconstructed": False,
                "recorded_spend_unchanged": True,
            }
            snapshot = digest(preview)
            if not args.apply:
                return {**preview, "snapshot": snapshot, "budget_before": budget.model_dump(mode="json"), "applied": False}
            if not args.acknowledge_unverified_history or not args.approval_reference or args.expected_snapshot != snapshot:
                raise ValueError("An explicit owner acknowledgement, approval reference and current preview are required")
            intent_id = str(uuid5(NAMESPACE_URL, "flow-recovery-resolution:" + snapshot))
            existing = await db.get(OrchestrationDecision, intent_id)
            audit = {**preview, "snapshot": snapshot, "approval_reference": args.approval_reference}
            if existing is None:
                db.add(
                    OrchestrationDecision(
                        id=intent_id,
                        org_id=args.org,
                        flow_id=args.flow,
                        kind="meter_hold_release_prepared",
                        actor_id=identity["Arn"],
                        actor_role="operator",
                        actor_kind="service",
                        reason=json.dumps(audit, sort_keys=True),
                    )
                )
                await db.commit()
            elif existing.org_id != args.org or existing.flow_id != args.flow or json.loads(existing.reason) != audit:
                raise ValueError("Resolution intent conflicts with an earlier operation")
            await db.scalar(select(OrchestrationFlow).where(OrchestrationFlow.id == args.flow).with_for_update())
            current = await load_in_force_policy(db, org_id=args.org, flow_id=args.flow)
            if current.refusal or current.policy != inputs.policy or current.plan_version != inputs.plan_version:
                raise ValueError("Policy changed during resolution")
            if not await release_recovery_hold(store, target, recovery_id=args.recovery, expected_usd=amount, decision_id=intent_id):
                raise ValueError("Recovery hold changed; no release applied")
            result = {**audit, "decision_id": intent_id, "applied": True}
            db.add(
                OrchestrationDecision(
                    org_id=args.org,
                    flow_id=args.flow,
                    kind="meter_hold_released",
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
    for name in ("account", "org", "flow", "recovery", "reason"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-snapshot")
    parser.add_argument("--approval-reference")
    parser.add_argument("--acknowledge-unverified-history", action="store_true")
    args = parser.parse_args()
    if not args.reason.strip():
        parser.error("A reason is required")
    print(json.dumps(asyncio.run(resolve(args)), sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        result = {"error": type(error).__name__}
        if type(error) is ValueError:
            result["detail"] = str(error)
        print(json.dumps(result), file=sys.stderr)
        raise SystemExit(1) from None
