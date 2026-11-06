"""Compare scoped client readbacks and scheduled occurrence receipts."""

from urllib.parse import quote


def _require(condition, reason):
    if not condition:
        raise ValueError(reason)


async def probe_clients(driver, *, scope, run_id, result_id, trigger_id):
    _require(isinstance(scope, dict) and all(isinstance(scope.get(key), str) and scope[key]
             for key in ("tenant_id", "product_id", "project_id"))
             and all(isinstance(value, str) and value for value in (run_id, result_id, trigger_id)),
             "client selection is incomplete")
    root = (f"/v1/products/{quote(scope['product_id'], safe='')}/projects/"
            f"{quote(scope['project_id'], safe='')}/services/transform")
    expected_paths = (f"{root}/runs/{quote(run_id, safe='')}",
                      f"{root}/results/{quote(result_id, safe='')}")
    observations = []
    for surface in ("ui", "cli", "mcp"):
        trace = await driver.read_client(surface, run_id, result_id)
        _require(isinstance(trace, dict) and trace.get("surface") == surface
                 and isinstance(trace.get("evidence_ref"), str) and bool(trace["evidence_ref"])
                 and trace.get("requests") == [("GET", path) for path in expected_paths]
                 and trace.get("status") == 200, "client readback is not attributable to scoped routes")
        run, result = trace.get("run"), trace.get("result")
        _require(isinstance(run, dict) and isinstance(result, dict)
                 and run.get("run_id") == result.get("run_id") == run_id
                 and result.get("result_id") == result_id and result.get("service_id") == "transform"
                 and run.get("scope") == result.get("scope") == scope
                 and type(run.get("revision")) is int and run["revision"] > 0
                 and type(result.get("revision")) is int and result["revision"] > 0,
                 "client readback is not the selected scoped result")
        observations.append(trace)
    _require(all(trace["run"] == observations[0]["run"]
                 and trace["result"] == observations[0]["result"] for trace in observations[1:]),
             "client readbacks disagree")

    first = await driver.read_schedule_tick(trigger_id, replay=False)
    replay = await driver.read_schedule_tick(trigger_id, replay=True)
    _require(isinstance(first, dict) and isinstance(replay, dict)
             and first == replay and first.get("event_kind") == "schedule"
             and first.get("state") == "admitted" and first.get("reason") is None
             and first.get("trigger_id") == trigger_id and first.get("service_id") == "transform"
             and all(first.get(key) == scope[key] for key in ("tenant_id", "product_id", "project_id"))
             and isinstance(first.get("event_id"), str)
             and first["event_id"].startswith("schedule:")
             and isinstance(first.get("idempotency_key"), str) and bool(first["idempotency_key"])
             and isinstance(first.get("run_id"), str) and bool(first["run_id"])
             and first["run_id"] != run_id
             and isinstance(first.get("evidence_ref"), str) and bool(first["evidence_ref"])
             and isinstance(replay.get("evidence_ref"), str) and bool(replay["evidence_ref"]),
             "scheduled replay changed its scoped receipt")
    runs = await driver.read_event_runs(first)
    _require(isinstance(runs, list) and len(runs) == 1
             and isinstance(runs[0], dict) and runs[0].get("run_id") == first["run_id"]
             and runs[0].get("scope") == scope
             and runs[0].get("admission", {}).get("event_id") == first["event_id"],
             "scheduled occurrence is missing or duplicated")
    scm = await driver.read_scm()
    _require(isinstance(scm, dict) and scm.get("before") == scm.get("after")
             and isinstance(scm.get("before"), dict) and bool(scm["before"].get("revision"))
             and scm.get("pr_actions") == []
             and isinstance(scm.get("evidence_ref"), str) and bool(scm["evidence_ref"]),
             "Transform performed an implicit SCM action")
    return {"phase": "client-and-event-observations", "live_qualified": False,
            "run_id": run_id, "result_id": result_id, "event_id": first["event_id"],
            "scheduled_run_id": first["run_id"], "occurrence_count": len(runs),
            "client_refs": {trace["surface"]: trace["evidence_ref"] for trace in observations},
            "event_ref": first["evidence_ref"], "scm_ref": scm["evidence_ref"]}
