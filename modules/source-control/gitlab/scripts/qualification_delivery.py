"""Retain complete live qualification results as versioned delivery evidence."""
import json
import re
from urllib.parse import quote
from registration_delivery import private_receipt

LIVE_CHECKS = frozenset({"EC-01", "EC-02", "EC-03", "EC-04", "product-pipeline-passed",
    "cross-group-pending-control", "three-cold-starts-and-restart", "legal-failed-cancelled-cleanup",
    "manager-loss-cleanup", "fixture-stack-independence", "qualification-cleanup"})


def require(condition):
    if not condition:
        raise ValueError("complete live qualification evidence required")


def publish(result, args, env, s3):
    require(result.get("verdict") == "pass" and result.get("fresh_independent_api_readbacks") == "passed"
            and result.get("checks") == dict.fromkeys(LIVE_CHECKS, "passed")
            and result.get("network_cases") == 15 and type(result.get("executor_cases")) is int and result["executor_cases"] > 0
            and result.get("cleanup_cases") == 4)
    require(args.source_revision == env["ADP_SOURCE_REVISION"] and args.account_id == env["ACCOUNT_ID"]
            and args.region == env["AWS_REGION"] and args.workflow_run_id == int(env["GITHUB_RUN_ID"]))
    binding = {"source_revision": args.source_revision, "account_id": args.account_id, "region": args.region,
        "workflow_run_id": args.workflow_run_id, "run_attempt": int(env["GITHUB_RUN_ATTEMPT"]),
        "correlation": env["ADP_CORRELATION"], "run_id": args.run_id, "bundle_sha256": args.bundle_sha256}
    bucket = f"adp-{env['ENVIRONMENT']}-deployment-evidence-{args.account_id}";owner = {"ExpectedBucketOwner": args.account_id}
    require(s3.get_bucket_versioning(Bucket=bucket, **owner).get("Status") == "Enabled")
    refs = {}
    for name in sorted(LIVE_CHECKS):
        key = f"gitlab-qualification/repos/{int(env['GITHUB_REPOSITORY_ID'])}/runs/{args.workflow_run_id}/attempts/{binding['run_attempt']}/{name}.json"
        body = json.dumps({**binding, "check": name, "result": result}, sort_keys=True, separators=(",", ":")).encode()
        response = s3.put_object(Bucket=bucket, Key=key, Body=body, ContentType="application/json", ServerSideEncryption="AES256", IfNoneMatch="*", **owner)
        version = response.get("VersionId");require(version and version != "null")
        saved = s3.get_object(Bucket=bucket, Key=key, VersionId=version, **owner)
        try:require(saved.get("VersionId") == version and saved["Body"].read(len(body) + 1) == body)
        finally:saved["Body"].close()
        refs[name] = f"s3://{bucket}/{key}?versionId={quote(version, safe='')}"
    return {**result, **binding, "evidence_refs": refs}


def verify(receipt, env, account, checkout):
    expected = {"source_revision": checkout, "account_id": account, "region": env["AWS_REGION"],
        "workflow_run_id": int(env["GITHUB_RUN_ID"]), "run_attempt": int(env["GITHUB_RUN_ATTEMPT"]),
        "correlation": env["ADP_CORRELATION"], "run_id": env["ADP_QUALIFICATION_RUN_ID"],
        "bundle_sha256": env["ADP_QUALIFICATION_BUNDLE_SHA256"]}
    require(all(type(receipt.get(k)) is type(v) and receipt[k] == v for k,v in expected.items())
            and receipt.get("verdict") == "pass" and receipt.get("fresh_independent_api_readbacks") == "passed"
            and receipt.get("checks") == dict.fromkeys(LIVE_CHECKS, "passed"))
    refs = receipt.get("evidence_refs")
    require(isinstance(refs, dict) and set(refs) == LIVE_CHECKS)
    for name, ref in refs.items():
        prefix = (f"s3://adp-{env['ENVIRONMENT']}-deployment-evidence-{account}/gitlab-qualification/repos/"
                  f"{int(env['GITHUB_REPOSITORY_ID'])}/runs/{expected['workflow_run_id']}/attempts/{expected['run_attempt']}/{name}.json?versionId=")
        require(isinstance(ref,str) and ref.startswith(prefix) and ref[len(prefix):] != "null"
                and re.fullmatch(r"[A-Za-z0-9_.~%-]{1,256}", ref[len(prefix):]))
    return refs
