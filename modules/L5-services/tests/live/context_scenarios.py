"""A later authorized Context read of an independently verified Security result."""

from evidence import verify_context_reuse


def context_reuse(case, foreign_case, driver):
    selection = case["context_reuse"]
    scope = case["scope"]
    if (selection["scope"] != scope or foreign_case["scope"]["tenant_id"] == scope["tenant_id"]
            or not selection["run_id"] or not selection["result_id"]
            or not selection["publication_id"] or not selection["sources"]
            or selection["max_attempts"] > case["max_attempts"]
            or selection["max_attempts"] < 3
            or selection["assessment_plan"]["input"]["sources"] != selection["sources"]
            or selection["assessment_plan"]["input"]["capability"] not in {"threat_model", "code_review"}
            or any(source.get("tenant_id") != scope["tenant_id"]
                   or source.get("project_id") != scope["project_id"]
                   or not source.get("record_revision")
                   or source["record_revision"].lower() in {"head", "latest", "null"}
                   for source in selection["sources"])):
        raise AssertionError("Context target requires a pinned, bounded, separately scoped Security result")
    run, result, findings = driver.read_product(selection)
    if (run["state"] != "succeeded" or run["scope"] != scope
            or result["scope"] != scope or result["finding_count"] != len(findings)
            or run["input"]["sources"] != selection["sources"]):
        raise AssertionError("selected Security result is not a verified terminal Product result")
    driver.verify_result(run, result, findings, selection["assessment_plan"])
    publication = driver.read_publication(selection)
    if (publication["state"] != "published" or publication["publication_id"] != selection["publication_id"]
            or publication["result_id"] != result["result_id"]
            or publication["payload"]["scope"] != scope
            or publication["payload"]["source_refs"] != selection["sources"]
            or publication["result_version"] != result["revision"]
            or publication["payload"]["native_job_ref"] != result["native_job_ref"]):
        raise AssertionError("independent publication differs from the pinned Security result")
    receipt = driver.read_receipt(selection)
    if (receipt != publication["receipt"] or receipt["contribution_id"] != publication["publication_id"]
            or not receipt["consumer_revision"]):
        raise AssertionError("Context receipt differs from durable Security publication")
    repeated = driver.replay_publication(publication, selection)
    agent_read = driver.later_agent_read(selection)
    visibility = driver.visibility(selection)
    denied = driver.denied_reads(selection, foreign_case)
    audit = driver.event_audit(selection)
    return verify_context_reuse(run, result, publication, receipt, agent_read, visibility,
                                denied, repeated, audit)
