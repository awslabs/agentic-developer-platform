"""SQLite runtime counterparts of the PostgreSQL decision predicates."""

import json


def payload_flags(reason):
    try:
        value = json.loads(reason or "{}")
    except (ValueError, TypeError):
        return 0
    if not isinstance(value, dict):
        return 0
    return (
        1
        | (2 if all(key in value for key in ("reason", "criterion_ids", "plan_version")) else 0)
        | (4 if isinstance(value.get("attempt"), int) else 0)
        | (8 if value.get("contract") == "dispatch-admission-refusal/v1" else 0)
    )


def category_v1(kind, actor_id, actor_kind, reason, rejection_reason):
    if kind in {"node_stalled", "node_halted"}:
        return 1
    if kind in {"gate_approved", "gate_rejected"}:
        return 4
    if kind == "node_dispatched" and payload_flags(reason) & 1:
        return 2
    if kind == "result_observed" and payload_flags(reason) & 1:
        return 3
    if kind == "evaluation_waived" and actor_kind == "human" and payload_flags(reason) & 2:
        return 5
    if (
        kind == "transition_rejected"
        and actor_id == "system:orchestration-dispatch"
        and actor_kind == "service"
        and payload_flags(rejection_reason) & 8
    ):
        return 6
    if kind == "evaluation_evidence_wait" and actor_id == "system:orchestration-tick" and actor_kind == "service" and payload_flags(reason) & 1:
        return 7
    if kind == "developer_retry_checked" and actor_id == "system:developer-recovery" and actor_kind == "service" and payload_flags(reason) & 1:
        return 8
    return None


def category(kind, actor_id, actor_kind, reason, rejection_reason):
    if kind == "result_observed" and actor_id == "system:repository-evaluation" and actor_kind == "service" and payload_flags(reason) & 1:
        return 10
    previous = category_v1(kind, actor_id, actor_kind, reason, rejection_reason)
    if previous is not None:
        return previous
    if (
        kind == "transition_rejected"
        and actor_id == "system:repository-evaluation"
        and actor_kind == "service"
        and payload_flags(rejection_reason) & 1
    ):
        return 9
    return None


def clearer(kind, actor_id, actor_kind):
    if kind == "node_dispatched":
        return 1
    if kind == "evaluation_evidence_ready" and actor_id == "system:orchestration-tick" and actor_kind == "service":
        return 2
    return None


def register(connection):
    if connection.info.get("adp_decision_predicates_v2"):
        return
    dbapi_connection = connection.connection.dbapi_connection
    dbapi_connection.create_function("adp_decision_payload_flags_v1", 1, payload_flags, deterministic=True)
    dbapi_connection.create_function("adp_decision_category_v1", 5, category_v1, deterministic=True)
    dbapi_connection.create_function("adp_decision_category_v2", 5, category, deterministic=True)
    dbapi_connection.create_function("adp_decision_clearer_v1", 3, clearer, deterministic=True)
    connection.info["adp_decision_predicates_v2"] = True
