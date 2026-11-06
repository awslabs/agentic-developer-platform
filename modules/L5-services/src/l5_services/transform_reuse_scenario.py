"""Cross-check offline consumer receipts and later reuse against a separate audit snapshot."""

from l5_services.transform_reuse_probe import DESTINATIONS, probe_reuse


def _require(condition, reason):
    if not condition:
        raise ValueError(reason)


async def verify_ec07(driver, *, run_id, result_id, scope, now):
    report = await probe_reuse(driver, run_id=run_id, result_id=result_id,
                               scope=scope, now=now)
    audit = await driver.read_reuse_oracle(result_id)
    _require(isinstance(audit, dict) and audit.get("source") == "consumer-audit"
             and audit.get("scope") == scope and audit.get("run_id") == run_id
             and audit.get("result_id") == result_id
             and isinstance(audit.get("evidence_ref"), str) and bool(audit["evidence_ref"])
             and isinstance(audit.get("receipts"), dict)
             and set(audit["receipts"]) == DESTINATIONS
             and isinstance(audit.get("later_task"), dict)
             and isinstance(audit.get("denials"), dict)
             and set(audit["denials"]) == {"foreign", "revoked"},
             "independent consumer audit is missing or unscoped")
    rows = await driver.read_publications(result_id)
    _require(isinstance(rows, list) and len(rows) == len(DESTINATIONS)
             and all(isinstance(row, dict) and row.get("destination") in DESTINATIONS
                     and isinstance(row.get("receipt"), dict) for row in rows),
             "publication readback changed after the consumer probe")
    references = [audit["evidence_ref"], *report["consumer_refs"], report["later_task_ref"]]
    for row in rows:
        receipt = audit["receipts"].get(row["destination"])
        _require(isinstance(receipt, dict)
                 and receipt.get("receipt_id") == row["receipt"].get("receipt_id")
                 and receipt.get("contribution_id") == row["publication_id"]
                 and receipt.get("contribution_hash") == row["contribution_hash"]
                 and receipt.get("record_refs") == [report["result_ref"]]
                 and isinstance(receipt.get("evidence_ref"), str)
                 and bool(receipt["evidence_ref"]),
                 "audited consumer receipt differs from the persisted contribution")
        references.append(receipt["evidence_ref"])
    task = await driver.read_later_task(result_id)
    observed_task = audit["later_task"]
    _require(isinstance(task, dict)
             and observed_task.get("task_id") == task.get("task_id")
             and observed_task.get("run_id") == task.get("run_id") != run_id
             and observed_task.get("citation") == task.get("input", {}).get("citation") == report["citation"]
             and observed_task.get("candidate_tree") == task.get("input", {}).get("candidate_tree")
             and observed_task.get("entitlement_ref") == task.get("entitlement", {}).get("evidence_ref")
             and observed_task.get("evidence_ref") == report["later_task_ref"],
             "audited later task did not use the entitled original citation")
    for kind in ("foreign", "revoked"):
        denial = await driver.read_reuse_denial(result_id, kind)
        observed_denial = audit["denials"][kind]
        _require(isinstance(denial, dict) and isinstance(observed_denial, dict)
                 and observed_denial.get("actor_id") == denial.get("actor_id")
                 and observed_denial.get("status") == denial.get("status")
                 and observed_denial.get("effect_count") == denial.get("effect_count") == 0
                 and observed_denial.get("entitlement_ref") == denial.get("entitlement", {}).get("evidence_ref")
                 and observed_denial.get("evidence_ref") == denial.get("evidence_ref")
                 and isinstance(observed_denial.get("audit_ref"), str)
                 and bool(observed_denial["audit_ref"]),
                 "audited foreign or revoked reuse does not match the denial")
        references.extend((denial["evidence_ref"], denial["entitlement"]["evidence_ref"],
                           observed_denial["audit_ref"]))
    references.append(task["entitlement"]["evidence_ref"])
    _require(len(references) == len(set(references)),
             "consumer, entitlement or denial evidence references are reused")
    return {"phase": "offline-consumer-reuse", "live_qualified": False,
            "result_ref": report["result_ref"], "consumer_refs": report["consumer_refs"],
            "later_task_ref": report["later_task_ref"], "audit_ref": audit["evidence_ref"],
            "denials": report["denials"]}
