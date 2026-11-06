"""Independently inspect Security Agent jobs under a read-only Product session."""

import json


def pages(fetch, key, **parameters):
    token, visited = None, set()
    for _ in range(100):
        page = fetch(**parameters, **({"nextToken": token} if token else {}))
        yield from page[key]
        token = page.get("nextToken")
        if not token:
            return
        if token in visited:
            break
        visited.add(token)
    raise ValueError("native provider pagination is incomplete")


def native_jobs(client, run):
    ref = run["native_job_ref"]
    space = run["admission"]["native_space_ref"]["record_id"]
    if run["input"]["capability"] == "code_review":
        reviews = list(pages(client.list_code_reviews, "codeReviewSummaries", agentSpaceId=space))
        # AWS list summaries omit agentSpaceId. Keep the scoped request and
        # validate each job's full identity before returning inventory evidence.
        if any(row.get("agentSpaceId", space) != space or not row.get("codeReviewId") for row in reviews):
            raise ValueError("foreign native review inventory")
        jobs = []
        for review in reviews:
            rows = list(pages(client.list_code_review_jobs_for_code_review, "codeReviewJobSummaries",
                              agentSpaceId=space, codeReviewId=review["codeReviewId"]))
            if any(row.get("codeReviewId") != review["codeReviewId"] or row.get("agentSpaceId", space) != space
                   for row in rows):
                raise ValueError("foreign native review job")
            for row in rows:
                detail = client.batch_get_code_review_jobs(agentSpaceId=space,
                                                           codeReviewJobIds=[row["codeReviewJobId"]])
                records = detail.get("codeReviewJobs") or []
                if (detail.get("notFound") or len(records) != 1
                        or records[0].get("agentSpaceId", space) != space
                        or records[0].get("codeReviewId") != review["codeReviewId"]
                        or records[0].get("codeReviewJobId") != row["codeReviewJobId"]):
                    raise ValueError("native review job inventory is incomplete")
            jobs.extend({**ref, "record_id": row["codeReviewJobId"], "record_revision": review["codeReviewId"]}
                        for row in rows)
    else:
        summaries = pages(client.list_threat_model_jobs, "threatModelJobSummaries",
                          agentSpaceId=space, threatModelId=ref["threat_model_id"], maxResults=100)
        rows = list(summaries)
        if any(row.get("agentSpaceId", space) != space or row.get("threatModelId") != ref["threat_model_id"]
               for row in rows):
            raise ValueError("foreign native threat job")
        jobs = []
        for row in rows:
            detail = client.batch_get_threat_model_jobs(agentSpaceId=space,
                                                        threatModelJobIds=[row["threatModelJobId"]])
            records = detail.get("threatModelJobs") or []
            if (detail.get("notFound") or len(records) != 1
                    or records[0].get("agentSpaceId") != space
                    or records[0].get("threatModelId") != ref["threat_model_id"]
                    or records[0].get("threatModelJobId") != row["threatModelJobId"]
                    or len(records[0].get("scopeDocs", [])) != 1
                    or not records[0]["scopeDocs"][0].get("artifactId")):
                raise ValueError("native threat job inventory is incomplete")
            jobs.append({**ref, "threat_model_job_id": row["threatModelJobId"],
                         "artifact_id": records[0]["scopeDocs"][0]["artifactId"]})
    if len(jobs) > 10000 or len({json.dumps(job, sort_keys=True) for job in jobs}) != len(jobs):
        raise ValueError("native job inventory is incomplete or duplicated")
    return jobs


def native_job(client, run):
    ref = run["native_job_ref"]
    space = run["admission"]["native_space_ref"]["record_id"]
    if run["input"]["capability"] == "threat_model":
        records = client.batch_get_threat_model_jobs(agentSpaceId=space,
                                                     threatModelJobIds=[ref["threat_model_job_id"]])["threatModelJobs"]
        if (len(records) != 1 or records[0].get("agentSpaceId") != space
                or records[0].get("threatModelId") != ref["threat_model_id"]
                or records[0].get("threatModelJobId") != ref["threat_model_job_id"]
                or len(records[0].get("scopeDocs", [])) != 1
                or records[0]["scopeDocs"][0].get("artifactId") != ref["artifact_id"]):
            raise ValueError("independent threat job differs from scoped run")
        job = records[0]
        rows = list(pages(client.list_threats, "threats", agentSpaceId=space,
                          threatJobId=ref["threat_model_job_id"], maxResults=100))
        if any(row.get("threatJobId") != ref["threat_model_job_id"] for row in rows):
            raise ValueError("foreign native threat summary")
        ids = [row["threatId"] for row in rows]
        if len(ids) > 10000 or len(set(ids)) != len(ids):
            raise ValueError("native threat IDs duplicate or exceed readback bound")
        details = []
        for offset in range(0, len(ids), 100):
            details.extend(client.batch_get_threats(agentSpaceId=space, threatIds=ids[offset:offset + 100])["threats"])
        if (len(details) != len(ids) or {row.get("threatId") for row in details} != set(ids)
                or any(row.get("threatJobId") != ref["threat_model_job_id"] for row in details)):
            raise ValueError("native threat details differ from selected job")
        findings = [{"native_id": row["threatId"], "claim": row.get("statement") or row["title"]}
                    for row in details]
        tasks = list(pages(client.list_threat_model_job_tasks, "threatModelJobTaskSummaries",
                           agentSpaceId=space, threatModelJobId=ref["threat_model_job_id"], maxResults=100))
        if any(row.get("threatModelJobId") != ref["threat_model_job_id"] for row in tasks):
            raise ValueError("foreign native task")
        phases = None
    else:
        records = client.batch_get_code_review_jobs(agentSpaceId=space,
                                                    codeReviewJobIds=[ref["record_id"]])["codeReviewJobs"]
        if (len(records) != 1 or records[0].get("agentSpaceId", space) != space
                or records[0].get("codeReviewId") != ref["record_revision"]
                or records[0].get("codeReviewJobId") != ref["record_id"]):
            raise ValueError("independent code review job differs from scoped run")
        job = records[0]
        rows = list(pages(client.list_findings, "findingsSummaries", agentSpaceId=space,
                          codeReviewJobId=ref["record_id"]))
        if any(row.get("codeReviewJobId") != ref["record_id"] for row in rows):
            raise ValueError("foreign native finding summary")
        ids = [row["findingId"] for row in rows]
        if len(ids) > 10000 or len(set(ids)) != len(ids):
            raise ValueError("native finding IDs duplicate or exceed readback bound")
        details = []
        for offset in range(0, len(ids), 50):
            details.extend(client.batch_get_findings(agentSpaceId=space, findingIds=ids[offset:offset + 50])["findings"])
        if (len(details) != len(ids) or {row.get("findingId") for row in details} != set(ids)
                or any(row.get("codeReviewJobId") != ref["record_id"] for row in details)):
            raise ValueError("native finding details differ from selected job")
        findings = [{"native_id": row["findingId"],
                     "claim": row.get("description") or row.get("name") or "Static finding"} for row in details]
        phases = job.get("steps")
        tasks = list(pages(client.list_code_review_job_tasks, "codeReviewJobTaskSummaries",
                           agentSpaceId=space, codeReviewJobId=ref["record_id"], maxResults=100))
        if any(row.get("codeReviewJobId") != ref["record_id"] for row in tasks):
            raise ValueError("foreign native task")
    return {"native_job_ref": ref, "status": job["status"], "findings_coverage": "complete",
            "findings": findings, "phases": phases, "tasks": tasks,
            "progress_coverage": "complete" if isinstance(phases, list) and tasks else "unavailable"}
