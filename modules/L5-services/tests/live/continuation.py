"""Fail-closed reconciliation checks for uncertain provider and Context effects."""


def verify_recovery(before, after, recorded_attempts, native_jobs, publication_before, publication_after,
                    repair):
    """Inspect durable before/after readbacks; never submit or repair a native job here."""
    if (before["run_id"] != after["run_id"] or before["scope"] != after["scope"]
            or before["input"] != after["input"] or before["admission"] != after["admission"]):
        raise AssertionError("recovery changed the admitted scope or pinned source")
    attempts_before = before["attempts"]
    attempts_after = after["attempts"]
    if (not attempts_before or len(attempts_after) < len(attempts_before)
            or any(previous["attempt_id"] != current["attempt_id"]
                   or previous["operation"] != current["operation"]
                   or (previous.get("native_job_ref") is not None
                       and previous["native_job_ref"] != current.get("native_job_ref"))
                   for previous, current in zip(attempts_before, attempts_after))):
        raise AssertionError("recovery lost a retained attempt")
    if not 1 <= len(recorded_attempts) <= repair["max_attempts"]:
        raise AssertionError("recovery attempt bound exceeded or journal missing")
    recorded_ids = [attempt["attempt_id"] for attempt in recorded_attempts]
    after_ids = [attempt["attempt_id"] for attempt in attempts_after]
    if (len(recorded_ids) != len(set(recorded_ids)) or len(after_ids) != len(set(after_ids))
            or set(recorded_ids) != set(after_ids)):
        raise AssertionError("journal does not contain every durable attempt")
    if (repair["operator_command"] is not False or repair["work_progressed"] is not True
            or after["work"]["completed_at"] is None):
        raise AssertionError("durable recovery of owed work is unverified")
    if after["state"] != "succeeded" or after["provider_state"] != "completed":
        raise AssertionError("recovered assessment is not terminal")
    if (len(native_jobs) != 1 or native_jobs[0]["native_job_ref"] != after["native_job_ref"]
            or any(attempt.get("native_job_ref") not in (None, after["native_job_ref"])
                   for attempt in attempts_after)):
        raise AssertionError("uncertain submission produced another native job")
    if (publication_before["publication_id"] != publication_after["publication_id"]
            or publication_before["result_id"] != publication_after["result_id"]
            or publication_before["result_version"] != publication_after["result_version"]
            or publication_before["state"] != "retry_wait"
            or publication_after["state"] != "published"
            or not isinstance(publication_after["receipt"], dict)
            or publication_after["receipt"].get("contribution_id") != publication_after["publication_id"]
            or publication_after["receipt"].get("admitted_schema") != publication_after["schema_version"]
            or not publication_after["receipt"].get("receipt_id")
            or publication_after["attempts"] <= publication_before["attempts"]):
        raise AssertionError("Context publication remains unverified or was duplicated")
    return {"run_id": after["run_id"], "native_job_ref": after["native_job_ref"],
            "attempts": len(recorded_attempts), "publication_id": publication_after["publication_id"]}
