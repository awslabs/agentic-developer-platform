"""Independent source, native job and claim checks for live Security assessments."""

from hashlib import sha256


def verify_assessment(run, result, findings, native_job, sources, reviews):
    """Verify readbacks, not inferred native status or deterministic claim text.

    Sources must be fetched independently at the pinned revision. A review is
    an independently produced rubric judgment, not the provider's validation.
    The caller retains source/job/review artifacts in a private evidence store.
    """
    capability = run["input"]["capability"]
    if capability not in {"threat_model", "code_review"}:
        raise AssertionError("unsupported Security capability")
    expected_type = "document" if capability == "threat_model" else "repository_commit"
    if run["state"] != "succeeded" or run["provider_state"] != "completed":
        raise AssertionError("assessment has no terminal successful run")
    if (result["run_id"] != run["run_id"] or result["scope"] != run["scope"]
            or result["native_job_ref"] != run["native_job_ref"]
            or native_job["native_job_ref"] != run["native_job_ref"]
            or native_job["status"] != "COMPLETED"):
        raise AssertionError("native job or result does not match the scoped run")
    if result["coverage"] != "complete" or native_job["findings_coverage"] != "complete":
        raise AssertionError("incomplete finding coverage")
    pinned = run["input"]["sources"]
    snapshots = run["admission"]["source_snapshots"]
    if not pinned or len(pinned) != len(snapshots) or len(pinned) != len(sources):
        raise AssertionError("incomplete pinned source readback")
    for source, snapshot, readback in zip(pinned, snapshots, sources, strict=True):
        if (source["record_type"] != expected_type or not source["record_revision"]
                or source["record_revision"].lower() in {"head", "latest", "null"}
                or snapshot["source_ref"] != source or readback["source_ref"] != source):
            raise AssertionError("native assessment source revision differs from pinned receipt")
        content = readback["content"]
        if (len(content) != snapshot["content"]["bytes"]
                or sha256(content).hexdigest() != snapshot["content"]["sha256"]):
            raise AssertionError("source bytes differ from pinned snapshot")
    if result["finding_count"] != len(findings) or len(findings) != len(native_job["findings"]):
        raise AssertionError("native and Product finding counts differ")
    native = {item["native_id"]: item for item in native_job["findings"]}
    if len(native) != len(findings) or len({item["native_id"] for item in findings}) != len(findings):
        raise AssertionError("duplicate or unaccounted native finding")
    for finding in findings:
        if (finding["native_id"] not in native or finding["native_job_ref"] != run["native_job_ref"]
                or finding["claim"] != native[finding["native_id"]]["claim"]
                or not finding["source_refs"]
                or any(source not in pinned for source in finding["source_refs"])):
            raise AssertionError("finding claim, job or source does not match native readback")
    required = set(native) if len(native) < 3 else set(native_job["review_sample"])
    if (len(required) != min(len(native), 3) or not required <= set(native)
            or set(reviews) != required or not native_job["review_rubric_revision"]):
        raise AssertionError("independent review does not cover the selected claims")
    for native_id, review in reviews.items():
        if (review["judgment"] not in {"supported", "contradicted", "uncertain"}
                or not review["evidence_ref"]
                or review["source_revision"] not in {source["record_revision"] for source in pinned}):
            raise AssertionError(f"independent review lacks cited revision for {native_id}")
    if not findings and not native_job["known_controls"]:
        raise AssertionError("zero findings needs independently checked controls")
    return {"run_id": run["run_id"], "result_id": result["result_id"],
            "capability": capability, "finding_count": len(findings), "reviewed": len(reviews),
            "native_job_ref": run["native_job_ref"], "source_revisions": [source["record_revision"] for source in pinned],
            "progress_coverage": native_job.get("progress_coverage", "unavailable"),
            "phases": native_job.get("phases"), "tasks": native_job.get("tasks")}
