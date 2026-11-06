#!/usr/bin/env python3
"""Collect private, read-only EC-04 observations; never publish scanned bytes."""
import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
INSTANCE = ROOT / "infra"
STAGES = ("absent", "present", "after_teardown")
WORKFLOWS = {"gitlab-infra-apply.yml", "gitlab-runners-apply.yml"}


class AuditError(ValueError):
    pass


def require(condition, reason):
    if not condition:
        raise AuditError(reason)


def private_bytes(path):
    path = Path(path)
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
            and stat.S_IMODE(info.st_mode) == 0o600, "private_audit_input_required")
    return path.read_bytes()


def plan(stage, fixtures, backend, variables, expected_role, approved_backend, approved_variables, *, run=subprocess.run,
         now=lambda: datetime.now(timezone.utc)):
    require(stage in STAGES, "instance_plan_stage_wrong")
    backend_digest = hashlib.sha256(private_bytes(backend)).hexdigest()
    variables_digest = hashlib.sha256(private_bytes(variables)).hexdigest()
    require(backend_digest == approved_backend and variables_digest == approved_variables,
            "instance_plan_inputs_not_approved")
    backend, variables = Path(backend).resolve(), Path(variables).resolve()
    require(not any(path == ROOT or ROOT in path.parents for path in (backend, variables))
            and backend != variables, "instance_plan_inputs_not_private")
    started = now().isoformat()
    identity = run(["aws", "sts", "get-caller-identity", "--output", "json"],
                   capture_output=True, check=False)
    require(identity.returncode == 0, "instance_plan_identity_missing")
    caller = json.loads(identity.stdout)
    role = re.fullmatch(r"arn:aws:iam::([0-9]{12}):role/([A-Za-z0-9+=,.@_/-]+)", expected_role)
    require(role and role[1] == fixtures["account_id"] and caller.get("Account") == role[1]
            and re.fullmatch(rf"arn:aws:sts::{role[1]}:assumed-role/{re.escape(role[2].split('/')[-1])}/[^/]+",
                             caller.get("Arn", "")), "instance_plan_identity_wrong")
    initialized = run(["terraform", "init", "-input=false", "-reconfigure", f"-backend-config={backend}"],
                      cwd=INSTANCE, capture_output=True, check=False)
    require(initialized.returncode == 0, "instance_backend_init_failed")
    completed = run(["terraform", "plan", "-detailed-exitcode", "-input=false", "-no-color",
                     f"-var-file={variables}"], cwd=INSTANCE, capture_output=True, check=False)
    finished = now().isoformat()
    require(completed.returncode == 0, "instance_plan_not_zero_drift")
    return {"stage": stage, "run_id": fixtures["run_id"], "source_revision": fixtures["source_revision"],
            "account_id": fixtures["account_id"], "working_directory": "modules/source-control/gitlab/infra",
            "started_at": started, "completed_at": finished, "exit_code": completed.returncode,
            "backend_sha256": backend_digest, "variables_sha256": variables_digest,
            "output_sha256": hashlib.sha256(completed.stdout).hexdigest()}


def workflow_run_matches(fixtures, row):
    """A native dispatch binds app source independently from the main workflow SHA."""
    bindings = fixtures.get("workflow_sources")
    if bindings is None:
        return row.get("head_sha") == fixtures["source_revision"]
    expected = bindings.get(row.get("workflow"))
    if not isinstance(expected, dict):
        return False
    return (row.get("head_sha") == expected["workflow_revision"]
            and all(row.get(key) == expected[key] for key in (
                "run_id", "run_attempt", "source_revision", "workflow_revision", "correlation",
                "contract_digest", "context_sha256", "context_reference")))


def authenticated_context(fixtures, workflow, details, store):
    expected = fixtures["workflow_sources"][workflow]
    require(details.get("id") == expected["run_id"]
            and details.get("run_attempt") == expected["run_attempt"]
            and details.get("repository", {}).get("id") == expected["repository_id"]
            and details.get("head_sha") == expected["workflow_revision"], "github_qualification_run_wrong")
    if store is None:
        import sys
        sys.path.insert(0, str(ROOT.parents[1] / "gateway"))
        from deployment_evidence.store import EvidenceStore
        store = EvidenceStore()
    result = store.read(account=fixtures["account_id"], environment="dev", region=expected["region"],
                        repository="aws-e/adp", repository_id=expected["repository_id"],
                        run_id=expected["run_id"], attempt=expected["run_attempt"], kind="context", name=workflow,
                        workflow_path=".github/workflows/" + workflow, workflow_revision=expected["workflow_revision"])
    require(result is not None, "authenticated_workflow_context_missing")
    payload, digest, reference = result
    context = json.loads(payload)
    require(digest == expected["context_sha256"] and reference == expected["context_reference"]
            and context.get("repository_id") == expected["repository_id"]
            and context.get("account_id") == fixtures["account_id"] and context.get("region") == expected["region"]
            and context.get("workflow_path") == ".github/workflows/" + workflow
            and all(context.get(key) == expected[key] for key in (
                "run_id", "run_attempt", "source_revision", "workflow_revision", "correlation"))
            and context.get("inputs", {}).get("adp_contract_digest") == expected["contract_digest"],
            "authenticated_workflow_context_wrong")
    return {key: expected[key] for key in ("run_attempt", "source_revision", "workflow_revision", "correlation",
                                          "contract_digest", "context_sha256", "context_reference")}


def scan(fixtures, runs, job_logs, state_files, artifacts, canaries):
    """Scan complete provider job logs and private state/artifacts, without returning matches."""
    require(isinstance(runs, list) and runs and isinstance(job_logs, dict)
            and isinstance(state_files, list) and state_files
            and isinstance(artifacts, list) and artifacts
            and isinstance(canaries, list) and canaries
            and all(isinstance(value, str) and value for value in canaries), "secret_scan_coverage_missing")
    jobs = []
    for row in runs:
        require(row.get("workflow") in WORKFLOWS and workflow_run_matches(fixtures, row)
                and type(row.get("run_id")) is int and row["run_id"] > 0
                and row.get("conclusion") in {"success", "failure", "cancelled"}
                and isinstance(row.get("job_ids"), list) and row["job_ids"]
                and all(type(job) is int and job > 0 for job in row["job_ids"]), "secret_scan_run_wrong")
        jobs.extend((row["run_id"], job) for job in row["job_ids"])
    require({row["workflow"] for row in runs} == WORKFLOWS
            and len(jobs) == len(set(jobs)) and len(runs) == len({row["run_id"] for row in runs})
            and set(job_logs) == set(jobs)
            and len(state_files) == len(set(state_files))
            and len(artifacts) == len(set(artifacts)), "secret_scan_coverage_missing")
    patterns = [value.encode() for value in canaries]
    canary_hits = ca_pem_hits = 0
    digests = []
    for path in list(job_logs.values()) + state_files + artifacts:
        data = private_bytes(path)
        digests.append(hashlib.sha256(data).hexdigest())
        canary_hits += sum(data.count(pattern) for pattern in patterns)
        canary_hits += len(re.findall(rb"glrt-[A-Za-z0-9_-]{12,}", data))
        ca_pem_hits += data.count(b"BEGIN CERTIFICATE")
    require(canary_hits == ca_pem_hits == 0, "secret_material_exposed")
    return {"run_id": fixtures["run_id"], "source_revision": fixtures["source_revision"],
            "workflow_runs": runs, "full_logs_scanned": True, "state_scanned": True,
            "artifacts_scanned": True, "canary_hits": canary_hits, "ca_pem_hits": ca_pem_hits,
            "log_job_ids": [job for _, job in jobs], "state_count": len(state_files),
            "artifact_count": len(artifacts), "sha256s": digests}


def github_logs(fixtures, run_ids, directory, *, gh=subprocess.run, evidence_store=None):
    """Read each reviewed run's complete provider job inventory and private logs."""
    directory = Path(directory)
    info = directory.lstat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()
            and stat.S_IMODE(info.st_mode) == 0o700, "private_audit_directory_required")
    require(run_ids and len(set(run_ids)) == len(run_ids)
            and all(type(value) is int and value > 0 for value in run_ids), "github_run_ids_invalid")

    def request(endpoint):
        response = gh(["gh", "api", "repos/aws-e/adp/actions/" + endpoint],
                      capture_output=True, check=False)
        require(response.returncode == 0 and len(response.stdout) <= 16 * 1024 * 1024,
                "github_qualification_readback_missing")
        return response.stdout

    runs, logs = [], {}
    for run_id in run_ids:
        details = json.loads(request(f"runs/{run_id}"))
        match = re.fullmatch(r"\.github/workflows/([A-Za-z0-9-]+\.yml)",
                             details.get("path", ""))
        require(details.get("id") == run_id and match and match[1] in WORKFLOWS
                and details.get("head_branch") == "main"
                and (fixtures.get("workflow_sources") is not None or details.get("head_sha") == fixtures["source_revision"])
                and details.get("event") == "workflow_dispatch"
                and details.get("conclusion") in {"success", "failure", "cancelled"},
                "github_qualification_run_wrong")
        provenance = (authenticated_context(fixtures, match[1], details, evidence_store)
                      if fixtures.get("workflow_sources") is not None else {})
        job_ids = []
        for page in range(1, 21):
            payload = json.loads(request(f"runs/{run_id}/jobs?per_page=100&page={page}"))
            batch = payload.get("jobs")
            require(isinstance(batch, list) and len(batch) <= 100, "github_job_inventory_incomplete")
            job_ids.extend(row.get("id") for row in batch)
            if len(batch) < 100:
                break
        else:
            raise AuditError("github_job_inventory_incomplete")
        require(job_ids and len(job_ids) == len(set(job_ids))
                and all(type(job) is int and job > 0 for job in job_ids)
                and payload.get("total_count") == len(job_ids), "github_job_inventory_incomplete")
        for job_id in job_ids:
            output = request(f"jobs/{job_id}/logs")
            target = directory / f"run-{run_id}-job-{job_id}.log"
            descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(output)
            logs[(run_id, job_id)] = target
        runs.append({"run_id": run_id, "workflow": match[1], "head_sha": details["head_sha"],
                     "conclusion": details["conclusion"], "job_ids": job_ids, **provenance})
    return runs, logs


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixtures", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--plan-stage", choices=STAGES)
    action.add_argument("--scan-logs", action="store_true")
    parser.add_argument("--backend", type=Path)
    parser.add_argument("--variables", type=Path)
    parser.add_argument("--expected-role")
    parser.add_argument("--approved-backend-sha256")
    parser.add_argument("--approved-variables-sha256")
    parser.add_argument("--run-id", type=int, action="append")
    parser.add_argument("--audit-directory", type=Path)
    parser.add_argument("--state", type=Path, action="append")
    parser.add_argument("--artifact", type=Path, action="append")
    parser.add_argument("--canaries-file", type=Path)
    args = parser.parse_args()
    try:
        fixtures = json.loads(private_bytes(args.fixtures))
        if args.plan_stage:
            require(all((args.backend, args.variables, args.expected_role, args.approved_backend_sha256,
                         args.approved_variables_sha256)), "instance_plan_inputs_missing")
            result = plan(args.plan_stage, fixtures, args.backend, args.variables, args.expected_role,
                          args.approved_backend_sha256, args.approved_variables_sha256)
        else:
            require(args.audit_directory and args.state and args.artifact and args.canaries_file,
                    "secret_scan_inputs_missing")
            canaries = json.loads(private_bytes(args.canaries_file))
            runs, logs = github_logs(fixtures, args.run_id, args.audit_directory)
            result = scan(fixtures, runs, logs, args.state, args.artifact, canaries)
        output = args.output
        directory = output.parent.lstat()
        require(stat.S_ISDIR(directory.st_mode) and directory.st_uid == os.getuid()
                and stat.S_IMODE(directory.st_mode) == 0o700, "private_audit_directory_required")
        descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            json.dump(result, stream, sort_keys=True)
            stream.write("\n")
    except Exception:
        print('{"status":"incomplete","reason":"private_ec04_audit_failed"}')
        return 1
    print('{"status":"private_receipt_written"}')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
