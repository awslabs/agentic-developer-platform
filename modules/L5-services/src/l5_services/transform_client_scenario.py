"""Reconcile offline client parity, scheduled SQL receipts and a separate SCM audit."""

from datetime import UTC, datetime

from l5_services.transform_client_probe import probe_clients


def _require(condition, reason):
    if not condition:
        raise ValueError(reason)


async def verify_ec06(driver, *, scope, run_id, result_id, trigger_id,
                      scheduled_at, configuration_revision):
    _require(isinstance(scheduled_at, datetime) and scheduled_at.tzinfo is not None
             and scheduled_at.utcoffset() is not None
             and type(configuration_revision) is int and configuration_revision > 0,
             "recurring-event fixture requires a pinned UTC tick and configuration revision")
    instant = scheduled_at.astimezone(UTC).isoformat().replace("+00:00", "Z")
    expected_event_id = f"schedule:{instant}:revision:{configuration_revision}"
    report = await probe_clients(driver, scope=scope, run_id=run_id,
                                 result_id=result_id, trigger_id=trigger_id)
    oracle = await driver.read_event_oracle(trigger_id)
    _require(isinstance(oracle, dict) and oracle.get("source") == "sql-event-receipt"
             and oracle.get("scope") == scope and oracle.get("trigger_id") == trigger_id
             and isinstance(oracle.get("evidence_ref"), str) and bool(oracle["evidence_ref"])
             and oracle["evidence_ref"] not in set(report["client_refs"].values()) | {
                 report["event_ref"], report["scm_ref"]}
             and isinstance(oracle.get("events"), list) and len(oracle["events"]) == 1
             and oracle.get("run_ids") == [report["scheduled_run_id"]]
             and report["event_id"] == expected_event_id
             and report["occurrence_count"] == 1,
             "pinned scheduled tick is missing or has more than one SQL occurrence")
    receipt = oracle["events"][0]
    _require(isinstance(receipt, dict) and receipt.get("event_id") == expected_event_id
             and receipt.get("state") == "admitted"
             and receipt.get("configuration_revision") == configuration_revision
             and receipt.get("scope") == scope
             and receipt.get("run_id") == report["scheduled_run_id"]
             and receipt.get("evidence_ref") == report["event_ref"]
             and isinstance(receipt.get("idempotency_key"), str)
             and bool(receipt["idempotency_key"]),
             "SQL event receipt differs from the replayed scoped occurrence")
    audit = await driver.read_scm_audit()
    scm = await driver.read_scm()
    _require(isinstance(audit, dict) and audit.get("source") == "scm-audit"
             and isinstance(audit.get("evidence_ref"), str) and bool(audit["evidence_ref"])
             and audit["evidence_ref"] not in set(report["client_refs"].values()) | {
                 report["event_ref"], oracle["evidence_ref"], report["scm_ref"]}
             and isinstance(scm, dict) and scm.get("evidence_ref") == report["scm_ref"]
             and audit.get("before") == audit.get("after") == scm.get("before") == scm.get("after")
             and audit.get("actions") == [] and audit.get("authorization_ref") is None,
             "SCM audit records an unbound PR action or disagrees with independent readback")
    return {"phase": "offline-client-event-scm", "live_qualified": False,
            "client_refs": report["client_refs"], "occurrence_count": 1,
            "event_id": expected_event_id, "event_ref": report["event_ref"],
            "sql_ref": oracle["evidence_ref"], "scm_ref": report["scm_ref"],
            "scm_audit_ref": audit["evidence_ref"]}
