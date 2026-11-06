"""Reconcile interrupted text requests from provider-owned zero-usage logs.

This operator path cannot write off historical recovery holds. A gateway error,
elapsed time, or an absent provider log never proves that a request was free.
"""

import asyncio
import hashlib
import json
from datetime import timedelta
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select

from src.budget.settlement import settle_usage
from src.shared.models.budget import BudgetSettlementReceipt
from src.shared.models.usage import UsageLog

from .cost import escape_like
from .flow_budget import get_flow_reservations
from .flow_meter import meter_target
from .models import OrchestrationDecision, OrchestrationFlow
from .policy_admission import load_in_force_policy


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def verified_zero_event(event, *, request_id, model, account_id, region):
    """Only complete provider token counters can establish zero text usage."""
    raw = event["message"]
    value = json.loads(raw)
    if (
        value.get("schemaType") != "ModelInvocationLog"
        or value.get("schemaVersion") != "1.0"
        or value.get("operation") != "Responses"
        or value.get("accountId") != account_id
        or value.get("region") != region
        or value.get("requestId") != request_id
        or not model.startswith("openai.gpt-")
        or value.get("modelId", "").rsplit("/", 1)[-1] not in {model, *(f"{p}.{model}" for p in ("us", "eu", "apac", "global"))}
    ):
        raise ValueError("Provider log identity does not match the recorded request")
    counts = {}
    for side, fields in (
        ("input", ("inputTokenCount", "cacheReadInputTokenCount", "cacheWriteInputTokenCount")),
        ("output", ("outputTokenCount",)),
    ):
        section = value.get(side)
        if not isinstance(section, dict) or section.get(f"{side}ContentType") != "application/json":
            raise ValueError("Provider log is not a supported text request")
        for field in fields:
            amount = section.get(field)
            if type(amount) is not int or amount != 0:
                raise ValueError("Provider usage is missing or nonzero; reservation retained")
            counts[field] = amount
    return {
        "provider_request_id": request_id,
        "provider_timestamp": value["timestamp"],
        "event_id": event["eventId"],
        "log_stream": event["logStreamName"],
        "event_sha256": hashlib.sha256(raw.encode()).hexdigest(),
        "token_counts": counts,
    }


def read_zero_evidence(logs, *, log_group, row, account_id, region):
    UUID(row.provider_request_id)  # Never interpolate a malformed filter value.
    events = {}
    token = None
    # The usage timestamp records finalization, including a cancelled stream.
    # Bound the read and refuse ambiguity rather than search unrelated traffic.
    for _ in range(30):
        arguments = {
            "logGroupName": log_group,
            "startTime": int((row.timestamp - timedelta(minutes=20)).timestamp() * 1000),
            "endTime": int((row.timestamp + timedelta(minutes=20)).timestamp() * 1000),
            "filterPattern": '"' + row.provider_request_id + '"',
            "limit": 100,
        }
        if token:
            arguments["nextToken"] = token
        response = logs.filter_log_events(**arguments)
        for event in response.get("events", []):
            events[event["eventId"]] = event
        following = response.get("nextToken")
        if not following or following == token:
            break
        token = following
    else:
        raise ValueError("Provider evidence pagination incomplete")
    if len(events) != 1:
        raise ValueError("Exactly one matching provider usage record is required")
    return {
        "log_group": log_group,
        **verified_zero_event(next(iter(events.values())), request_id=row.provider_request_id, model=row.model, account_id=account_id, region=region),
    }


async def reconcile_zero_usage(args, *, session_factory, aws):
    identity = await asyncio.to_thread(aws.client("sts").get_caller_identity)
    if identity["Account"] != args.account:
        raise ValueError("AWS account differs from the selected target")
    config = await asyncio.to_thread(aws.client("bedrock").get_model_invocation_logging_configuration)
    log_group = config.get("loggingConfig", {}).get("cloudWatchConfig", {}).get("logGroupName")
    if not log_group:
        raise ValueError("Provider invocation logging is unavailable")
    logs = aws.client("logs")
    store = get_flow_reservations()
    try:
        async with session_factory() as db:
            flow = await db.scalar(select(OrchestrationFlow).where(OrchestrationFlow.org_id == args.org, OrchestrationFlow.id == args.flow))
            if flow is None:
                raise ValueError("Flow unavailable")
            inputs = await load_in_force_policy(db, org_id=args.org, flow_id=args.flow)
            if inputs.refusal or inputs.policy is None:
                raise ValueError("Accepted flow policy unavailable")
            target = meter_target(org_id=args.org, flow_id=args.flow, policy=inputs.policy)
            entries = await (await store._get_client()).hgetall(target.key())
            if "__initialized__" not in entries:
                raise ValueError("Missing meter cannot be reconstructed by request reconciliation")
            rows = (
                await db.scalars(
                    select(UsageLog).where(
                        UsageLog.org_id == args.org,
                        UsageLog.graph_address.like(escape_like(flow.slug) + "/%", escape="\\"),
                        UsageLog.request_id.in_(args.request),
                    )
                )
            ).all()
            if len(rows) != len(set(args.request)):
                raise ValueError("Every request must belong to this tenant and flow")
            items = []
            for row in sorted(rows, key=lambda row: row.request_id):
                if not row.provider_request_id or row.cost_usd != 0 or row.status_code < 400:
                    raise ValueError("Only recorded interrupted zero-cost requests qualify")
                if "bounded:" + row.request_id not in entries or row.request_id not in entries:
                    raise ValueError("Request has no unresolved bounded reservation")
                if await db.get(BudgetSettlementReceipt, (args.org, row.request_id)) is not None:
                    raise ValueError("Request already has a settlement; use normal receipt reconciliation")
                evidence = await asyncio.to_thread(
                    read_zero_evidence, logs, log_group=log_group, row=row, account_id=args.account, region=aws.region_name
                )
                items.append({"request_id": row.request_id, "reserved_usd": entries[row.request_id].split(":")[0], "evidence": evidence})
            report = {"account": args.account, "org": args.org, "flow": args.flow, "items": items, "reason": args.reason}
            snapshot = digest(report)
            if args.apply:
                if snapshot != args.expected_snapshot:
                    raise ValueError("Preview changed; review a fresh snapshot")
                # The immutable audit and zero receipts commit together. Redis is
                # reconciled by the normal scheduler, including after a crash.
                db.add(
                    OrchestrationDecision(
                        org_id=args.org,
                        flow_id=args.flow,
                        kind="provider_zero_usage_reconciled",
                        actor_id=identity["Arn"],
                        actor_role="operator",
                        actor_kind="service",
                        reason=json.dumps({**report, "snapshot": snapshot}, sort_keys=True),
                    )
                )
                for row in rows:
                    entities = [("user", row.user_id), ("org", row.org_id), ("team", row.team_id), ("department", row.department_id)]
                    if row.account_type == "service":
                        entities.append(("agent", row.user_id))
                    if row.root_human_id and row.root_human_id != row.user_id:
                        entities.append(("root_user", row.root_human_id))
                    scopes = [target.key()]
                    # Do not revive expired run counters. Related live counters
                    # can be settled only when this exact request is present.
                    client = await store._get_client()
                    for kind, run_id in (("run", row.agent_run_id), ("chain", row.chain_id)):
                        if run_id:
                            key = f"budget:resv:{{{args.org}}}:{kind}:{run_id}:run:lifetime"
                            if await client.hexists(key, row.request_id):
                                scopes.append(key)
                    await settle_usage(
                        db,
                        org_id=row.org_id,
                        request_id=row.request_id,
                        user_id=row.user_id,
                        cost=Decimal(0),
                        total_tokens=0,
                        entities=entities,
                        timestamp=row.timestamp,
                        reservation_scope_keys=scopes,
                        verified_provider_usage=True,
                    )
                await db.commit()
            else:
                await db.rollback()
            return {
                **report,
                "snapshot": snapshot,
                "applied": args.apply,
                "released_usd": str(sum((Decimal(item["reserved_usd"]) for item in items), Decimal(0))),
            }
    finally:
        await store.close()
