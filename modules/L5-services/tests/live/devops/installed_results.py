"""Read admitted investigation evidence through installed scoped Product APIs."""

import hashlib
import json
from urllib.parse import quote


def read_investigation_result(driver, run, native_task):
    cursor, seen, matches = None, set(), []
    for _ in range(10):
        path = driver.path + "/results?limit=100" + ("&cursor=" + quote(cursor, safe="") if cursor else "")
        page = driver.request("GET", path, token_env="api_token_env")
        items = page.get("items")
        if not isinstance(items, list) or len(items) > 100 or any(not isinstance(item, dict) for item in items):
            raise ValueError("installed investigation results listing is incomplete")
        matches.extend(item for item in items if item.get("run_id") == run["run_id"])
        cursor = page.get("next_cursor")
        if not cursor:
            break
        if not isinstance(cursor, str) or cursor in seen:
            raise ValueError("installed investigation results repeated a cursor")
        seen.add(cursor)
    else:
        raise ValueError("installed investigation results exceeded page bound")
    if len(matches) != 1 or not isinstance(matches[0].get("result_id"), str) or not matches[0]["result_id"]:
        raise ValueError("installed investigation needs one retained result")
    result_id = matches[0]["result_id"]
    result = driver.request("GET", driver.path + "/results/" + quote(result_id, safe=""), token_env="api_token_env")
    if (result.get("result_id") != result_id or result.get("run_id") != run["run_id"]
            or result.get("scope") != run["scope"] or result.get("native_job_ref") != run["native_job_ref"]
            or result.get("native_task_id") != native_task["taskId"] or result.get("service_id") != "devops"
            or result.get("origin") != "adp_submitted" or result.get("provider_outcome") != "completed"
            or result.get("admission") != run["admission"] or result.get("revision") != 1):
        raise ValueError("installed investigation result is foreign or changed")
    reference = result.get("evidence_ref") or {}
    if (reference.get("artifact_id") != f"readback-{result_id}" or reference.get("version") != "1"
            or reference.get("media_type") != "application/json" or type(reference.get("bytes")) is not int
            or not 0 < reference["bytes"] <= 8 * 1024 * 1024):
        raise ValueError("installed investigation lacks a bounded evidence artifact")
    contents = driver.request("GET", driver.path + "/artifacts/" + quote(reference["artifact_id"], safe="") + "/content",
                              token_env="api_token_env", raw=True, max_bytes=reference["bytes"])
    if len(contents) != reference["bytes"] or hashlib.sha256(contents).hexdigest() != reference.get("sha256"):
        raise ValueError("installed investigation evidence digest differs")
    evidence = json.loads(contents)
    if (not isinstance(evidence, dict) or evidence.get("kind") != "devops_investigation"
            or evidence.get("native_job_ref") != run["native_job_ref"]
            or not isinstance(evidence.get("task"), dict)
            or any(evidence["task"].get(field) != native_task.get(field) for field in (
                "agentSpaceId", "taskId", "executionId", "taskType", "status"))
            or not isinstance(evidence.get("execution"), dict)
            or evidence["execution"].get("executionId") != native_task["executionId"]
            or evidence["execution"].get("agentSpaceId") != native_task["agentSpaceId"]
            or not isinstance(evidence.get("journal_records"), list)
            or not isinstance(evidence.get("operational_recommendations"), list)
            or evidence.get("costs") != [{"kind": "unknown", "currency": "USD"}]):
        raise ValueError("installed investigation evidence differs from independent native task")
    return {"result_id": result_id, "evidence_ref": reference}
