"""One owned Security fault journey with independent recovery and cleanup checks."""

from cleanup import verify_owned_cleanup, verify_scope_denials
from continuation import verify_recovery


def isolation_recovery_cleanup(case, foreign_case, driver):
    selection = case["recovery"]
    if (selection["scope"] != case["scope"]
            or selection["input"]["sources"] != selection["sources"]
            or selection["input"]["binding_id"] != selection["binding_id"]
            or not selection["owned"] or set(selection["owned"]) & set(selection["linked"])
            or selection["max_attempts"] > case["max_attempts"]):
        raise AssertionError("fault fixture does not match selected scope, source or owned cleanup")
    sources = selection["sources"]
    if (selection["sources"] != case["selected"]["sources"]
            or selection["binding_id"] != case["selected"]["binding_id"]
            or selection["input"]["capability"] not in {"threat_model", "code_review"}
            or not sources or any(not source.get("record_revision")
                                  or source["record_revision"].lower() in {"latest", "head", "null"}
                                  or source["tenant_id"] != case["scope"]["tenant_id"]
                                  or source["project_id"] != case["scope"]["project_id"] for source in sources)):
        raise AssertionError("fault assessment does not pin the selected capability, binding and source revisions")
    if foreign_case["scope"]["tenant_id"] == case["scope"]["tenant_id"]:
        raise AssertionError("isolation needs an independent foreign tenant")
    driver.check_selection(selection)
    driver.arm_faults(selection)
    driver.submit(selection)
    before, publication_before = driver.read_faulted(selection)
    if (before["scope"] != case["scope"] or publication_before["state"] != "retry_wait"
            or not before["attempts"]):
        raise AssertionError("fault injection did not reach both real recovery boundaries")
    driver.retain_attempts(before["attempts"])
    driver.restart_workers(selection)
    after, publication_after, repair = driver.read_recovered(selection)
    driver.retain_attempts(after["attempts"])
    probes = verify_scope_denials(driver.scope_probes(after["run_id"], selection, foreign_case))
    native_jobs = driver.native_jobs(after)
    recovered = verify_recovery(before, after, driver.recorded_attempts(), native_jobs,
                                publication_before, publication_after,
                                {**repair, "max_attempts": selection["max_attempts"]})
    driver.validate_cleanup_ownership(selection)
    driver.cleanup_owned(selection)
    cleaned = verify_owned_cleanup(selection["owned"], selection["linked"], driver.cleanup_readback(selection))
    return {"recovery": recovered, "denials": probes, "cleanup": cleaned}
