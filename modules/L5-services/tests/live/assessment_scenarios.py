"""Bounded end-to-end assessment checks using independent evidence collectors."""

from clients import verify_trigger_result
from providers import verify_assessment


def real_security_assessments(case, driver):
    plans = case["assessments"]
    if ({plan["capability"] for plan in plans} != {"threat_model", "code_review"}
            or len(plans) != 2 or len({plan["receipt"] for plan in plans}) != 2):
        raise AssertionError("both explicitly selected capabilities require distinct bounded submissions")
    for plan in plans:
        expected_type = "document" if plan["capability"] == "threat_model" else "repository_commit"
        sources = plan["input"]["sources"]
        if (not sources or plan["input"]["capability"] != plan["capability"]
                or plan["input"]["binding_id"] != plan["binding_id"]
                or any(source["record_type"] != expected_type or not source["record_revision"]
                       or source["record_revision"].lower() in {"latest", "head", "null"}
                       or source["tenant_id"] != case["scope"]["tenant_id"]
                       or source["project_id"] != case["scope"]["project_id"] for source in sources)):
            raise AssertionError("assessment plan does not pin authorized capability, binding and sources")
    event = case["trigger_event"]
    matching = [plan for plan in plans if plan["capability"] == event["capability"]]
    if (len(matching) != 1 or event["kind"] != (
            "pull_request" if event["capability"] == "code_review" else "document_revision")
            or not event.get("event_id") or event["input"]["capability"] != event["capability"]
            or event["input"]["binding_id"] != matching[0]["binding_id"]
            or event["input"]["sources"] == matching[0]["input"]["sources"]
            or any(source["tenant_id"] != case["scope"]["tenant_id"]
                   or source["project_id"] != case["scope"]["project_id"]
                   or source["record_type"] != ("repository_commit" if event["capability"] == "code_review" else "document")
                   or not source.get("record_revision") or source["record_revision"].lower() in {"head", "latest", "null"}
                   for source in event["input"]["sources"])):
        raise AssertionError("trigger must pin a new authorized PR or document revision")
    if len(plans) + 2 > case["max_attempts"]:
        raise AssertionError("two assessments and an event exceed the authorized attempt bound")

    results = []
    for plan in plans:
        submitted = driver.submit(plan)
        run_id = submitted["run_id"]
        run, result, findings = driver.await_result(run_id)
        if (run["run_id"] != run_id or run["scope"] != case["scope"]
                or run["input"]["capability"] != plan["capability"]
                or run["input"]["binding_id"] != plan["binding_id"]
                or run["input"]["sources"] != plan["input"]["sources"]):
            raise AssertionError("persisted assessment differs from selected source or capability")
        native = driver.native_job(run)
        source_bytes = driver.read_sources(run, plan)
        reviews = driver.reviews(run, result, findings, native, plan)
        results.append(verify_assessment(run, result, findings, native, source_bytes, reviews))

    baseline = driver.capture_inventory(event)
    receipts = [driver.deliver_event(event), driver.deliver_event(event)]
    readback = driver.read_event_receipt(event)
    if readback != receipts[0] or receipts[0] != receipts[1]:
        raise AssertionError("duplicate deliveries disagree with independently read receipt")
    triggered = driver.await_result(receipts[0]["run_id"])
    trigger_run, trigger_result, trigger_findings = triggered
    if (trigger_run["scope"] != case["scope"] or trigger_run["input"]["capability"] != event["capability"]
            or trigger_run["input"]["binding_id"] != event["input"]["binding_id"]
            or trigger_run["input"]["sources"] != event["input"]["sources"]):
        raise AssertionError("event assessment has wrong scope or capability")
    driver.verify_triggered_evidence(trigger_run, trigger_result, trigger_findings, event)
    dedup = verify_trigger_result(receipts, trigger_run, trigger_result, driver.native_jobs_for_event(trigger_run, baseline))
    return {"assessments": results, "event": dedup}
