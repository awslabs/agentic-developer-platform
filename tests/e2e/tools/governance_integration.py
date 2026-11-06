"""Local governance predicates; live results need independent readback."""

from decimal import Decimal, InvalidOperation
from uuid import UUID

FAMILIES = ("websearch", "browser", "code_interpreter", "github", "jira", "slack")
STATEFUL = {"browser", "interpreter"}


def identifier(value):
    try:
        if type(value) is not str or str(UUID(value)) != value:
            raise ValueError
    except (TypeError, ValueError):
        raise ValueError("original operation identity required") from None
    return value


def amount(value):
    if isinstance(value, bool) or not isinstance(value, (str, Decimal)):
        raise ValueError("explicit USD amount required")
    try:
        result = Decimal(value)
    except InvalidOperation:
        raise ValueError("explicit USD amount required") from None
    if not result.is_finite() or result < 0:
        raise ValueError("explicit USD amount required")
    return result


def governance_ac01(family, observation):
    if family not in FAMILIES or not isinstance(observation, dict) or set(observation) != {"cap", "rate"}:
        raise ValueError("isolated cap and rate observations required")
    for sample in observation.values():
        if (not isinstance(sample, dict) or set(sample) != {
                "status", "retry_after_seconds", "provider_calls_before", "provider_calls_after",
                "reservations_before", "reservations_after"} or sample["status"] != 429 or
                type(sample["retry_after_seconds"]) is not int or not 1 <= sample["retry_after_seconds"] <= 86400 or
                any(type(sample[key]) is not int or sample[key] < 0 for key in (
                    "provider_calls_before", "provider_calls_after", "reservations_before", "reservations_after")) or
                sample["provider_calls_before"] != sample["provider_calls_after"] or
                sample["reservations_before"] != sample["reservations_after"]):
            raise ValueError("denial did not precede dispatch and reservation")
    return {"denials": ["cap", "rate"], "provider_dispatch": "none"}


def governance_ac02(observation):
    fields = {"attempts", "cap_usd", "held_usd", "other_held_usd", "provider_calls_before",
              "provider_calls_after", "result_status"}
    if not isinstance(observation, dict) or set(observation) != fields:
        raise ValueError("concurrent replay and hold observations required")
    attempts = observation["attempts"]
    keys = {"request_id", "receipt_id", "reservation_id"}
    if (not isinstance(attempts, list) or len(attempts) < 2 or
            any(not isinstance(item, dict) or set(item) != keys for item in attempts)):
        raise ValueError("concurrent replay attempts required")
    for attempt in attempts:
        for key in keys:
            identifier(attempt[key])
    held = amount(observation["held_usd"])
    if (any(item != attempts[0] for item in attempts[1:]) or not held or
            held + amount(observation["other_held_usd"]) > amount(observation["cap_usd"]) or
            observation["result_status"] != "unknown" or
            type(observation["provider_calls_before"]) is not int or
            type(observation["provider_calls_after"]) is not int or
            observation["provider_calls_before"] < 0 or
            observation["provider_calls_after"] - observation["provider_calls_before"] != 1):
        raise ValueError("unknown provider usage lost its bounded original hold")
    return {**attempts[0], "accounting_status": "unknown", "held_usd": str(held)}


def governance_ac03(observation):
    if not isinstance(observation, dict) or set(observation) != {"owned", "export", "foreign_status"}:
        raise ValueError("owner-scoped activity and export observations required")
    owned, exported = observation["owned"], observation["export"]
    fields = {"tenant_id", "team_id", "user_id", "tool", "run_id", "request_id", "receipt_id",
              "estimated_cost_usd", "settled_cost_usd", "accounting_status"}
    if (not isinstance(owned, dict) or not isinstance(exported, dict) or
            not fields <= owned.keys() or not fields <= exported.keys() or
            any(owned[key] != exported[key] for key in fields) or
            observation["foreign_status"] not in (403, 404) or
            any(not isinstance(owned[key], str) or not owned[key] for key in
                ("tenant_id", "team_id", "user_id", "tool", "run_id"))):
        raise ValueError("foreign or misattributed activity")
    identifier(owned["request_id"])
    identifier(owned["receipt_id"])
    estimate = amount(owned["estimated_cost_usd"])
    if (not estimate or owned["settled_cost_usd"] is not None or
            owned["accounting_status"] != "estimate_only" or
            any(key in owned or key in exported for key in ("arguments", "result", "secret", "authorization"))):
        raise ValueError("estimate confused with settlement or private result retained")
    return {"receipt_id": owned["receipt_id"], "estimated_cost_usd": str(estimate),
            "settled_cost_usd": None, "accounting_status": "estimate_only"}


def governance_ac04(tool, observation):
    if (tool not in {"search", *STATEFUL} or not isinstance(observation, dict) or
            set(observation) != {"expected_operations", "operations", "pricing_approved", "run_cap_usd"} or
            observation["pricing_approved"] is not True or amount(observation["run_cap_usd"]) > 20):
        raise ValueError("priced, bounded operation matrix required")
    expected, operations = observation["expected_operations"], observation["operations"]
    if (not isinstance(expected, list) or not expected or not isinstance(operations, list) or
            len(expected) != len(operations)):
        raise ValueError("missing or duplicate operation accounting")
    expected_ids = [identifier(value) for value in expected]
    if len(set(expected_ids)) != len(expected_ids):
        raise ValueError("duplicate expected operation")
    estimated_total = settled_total = Decimal("0")
    for operation in operations:
        if not isinstance(operation, dict) or set(operation) != {
                "request_id", "receipt_id", "operation_ref", "execution_id", "tool_name",
                "tenant_id", "team_id", "user_id", "run_id", "result_status", "cleanup_status",
                "estimated_cost_usd", "settled_cost_usd", "accounting_status"}:
            raise ValueError("incomplete operation accounting")
        if (identifier(operation["request_id"]) not in expected_ids or
                not identifier(operation["receipt_id"]) or
                not identifier(operation["operation_ref"]) or
                not identifier(operation["execution_id"]) or
                any(not isinstance(operation[key], str) or not operation[key]
                    for key in ("tenant_id", "team_id", "user_id", "run_id")) or
                not isinstance(operation["tool_name"], str) or
                not operation["tool_name"].startswith({"search": "websearch", "interpreter": "code_interpreter",
                                                      "browser": "browser"}[tool] + "___") or
                operation["result_status"] not in {"confirmed", "failed"} or
                operation["cleanup_status"] != ("confirmed" if tool in STATEFUL else "not_required") or
                operation["accounting_status"] != "settled" or
                not amount(operation["estimated_cost_usd"]) or operation["settled_cost_usd"] is None):
            raise ValueError("missing result, cleanup or settled cost")
        estimated_total += amount(operation["estimated_cost_usd"])
        settled_total += amount(operation["settled_cost_usd"])
    if estimated_total > amount(observation["run_cap_usd"]) or settled_total > amount(observation["run_cap_usd"]):
        raise ValueError("operation costs exceeded the local cap")
    if {operation["request_id"] for operation in operations} != set(expected_ids):
        raise ValueError("missing or duplicated operation result")
    return {"operation_count": len(operations), "result_status": "complete", "cost_status": "settled"}


def governance_ac04_link(client, tool, observation, observer):
    if observer is None or getattr(observer, "isolated", False) is not True or not callable(getattr(observer, "export", None)) or not callable(getattr(observer, "observe", None)):
        raise ValueError("separate offline provider and activity readbacks required")
    summary = governance_ac04(tool, observation)
    linked = []
    receipts = set()
    for operation in observation["operations"]:
        receipt_id = operation["receipt_id"]
        if receipt_id in receipts:
            raise ValueError("different operations reused a receipt")
        receipts.add(receipt_id)
        exported = observer.export(receipt_id=receipt_id, client=client, tool=tool)
        provider = observer.observe(operation_ref=operation["operation_ref"],
                                    execution_id=operation["execution_id"], operation=operation["tool_name"],
                                    client=client, timeout=30)
        identity = ("tenant_id", "team_id", "user_id", "run_id", "tool_name", "request_id",
                    "receipt_id", "execution_id", "result_status", "cleanup_status", "accounting_status")
        if (not isinstance(exported, dict) or
                exported.get("call_id") != receipt_id or
                any(exported.get(key if key != "tool_name" else "tool") != operation[key] for key in identity) or
                any(key in exported for key in ("arguments", "result", "secret", "authorization", "access_token", "content")) or
                amount(exported.get("estimated_cost_usd")) != amount(operation["estimated_cost_usd"]) or
                amount(exported.get("settled_cost_usd")) != amount(operation["settled_cost_usd"])):
            raise ValueError("owner-scoped export does not match original result and cost")
        if not isinstance(provider, dict):
            raise ValueError("provider observation unavailable")
        facts = provider.get("facts")
        if not isinstance(facts, dict):
            raise ValueError("provider observation unavailable")
        correlation = facts.get("correlation")
        outcome = facts.get("durable_outcome")
        termination = facts.get("termination")
        termination_verified = isinstance(termination, dict) and (
            termination.get("status") == "verified" and termination.get("source") == "aws-agentcore-session" and
            termination.get("provider_status") == "TERMINATED" and
            bool(termination.get("observed_at")) and
            type(termination.get("cleanup_elapsed_seconds")) in (int, float) and
            0 <= termination["cleanup_elapsed_seconds"] <= 600
        ) if tool in STATEFUL else isinstance(termination, dict) and termination.get("status") == "unsupported" and termination.get("source") == "capability"
        if (provider.get("adapter") != "adp-core-provider-observer-v1" or
                provider.get("offline_only") is not True or
                provider.get("client") != client or provider.get("operation") != operation["tool_name"] or
                provider.get("operation_ref") != operation["operation_ref"] or
                provider.get("execution_id") != operation["execution_id"] or
                not isinstance(correlation, dict) or correlation.get("status") != "verified" or
                correlation.get("source") != "provider-owned-ledger" or
                correlation.get("provider_request_id") != operation["request_id"] or
                correlation.get("provider_receipt_id") != receipt_id or
                not isinstance(outcome, dict) or outcome.get("status") != "verified" or
                outcome.get("source") != "provider-owned-ledger" or
                outcome.get("state") != operation["result_status"] or
                not termination_verified):
            raise ValueError("independent provider result or cleanup did not match the operation")
        linked.append({"request_id": operation["request_id"], "receipt_id": receipt_id,
                       "execution_id": operation["execution_id"], "result_status": operation["result_status"],
                       "cleanup_status": operation["cleanup_status"], "cost_status": operation["accounting_status"]})
    return {**summary, "linked_operations": linked}
