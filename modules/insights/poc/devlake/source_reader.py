"""Read bounded observation artifacts from separately approved source workflows."""

import io
import json
import os
import subprocess
import zipfile


REPOSITORY = "aws-e/adp"
MAX_ARCHIVE_BYTES = 4 * 1024 * 1024
MAX_RECEIPT_BYTES = 1024 * 1024


def _github(path):
    if not os.environ.get("GH_TOKEN"):
        raise ValueError("GitHub source readback requires protected job credentials")
    try:
        result = subprocess.run(["gh", "api", "-H", "Accept: application/vnd.github+json", path],
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=45, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ValueError("GitHub source readback is unavailable") from error
    if result.returncode or len(result.stdout) > MAX_ARCHIVE_BYTES:
        raise ValueError("GitHub source readback failed or exceeded size limit")
    return result.stdout


def _metadata(path, request):
    try:
        result = json.loads(request(path))
    except (UnicodeError, json.JSONDecodeError, TypeError) as error:
        raise ValueError("source workflow metadata is invalid") from error
    if not isinstance(result, dict):
        raise ValueError("source workflow metadata is invalid")
    return result


def approved_reader(manifest, plan, bindings, request=_github):
    """Build a source reader only when every criterion has a separately approved live producer.

    Bindings come from the protected workflow environment, never the campaign manifest.
    An authorized GitHub read proves provenance, not that a source job ran a real probe;
    approved source workflows must implement their own live assertions.
    """
    required = {row["criterion_id"] for row in plan["criteria"] if row["story"] != "S08"} | {"native", "providers"}
    if "delivery_cleanup_receipt" in manifest:
        required.add("delivery_cleanup")
    if not isinstance(bindings, dict) or set(bindings) != required:
        raise ValueError("approved independent source workflow inventory is incomplete")
    for criterion_id, binding in bindings.items():
        if (not isinstance(binding, dict) or not isinstance(binding.get("workflow"), str) or
                not binding["workflow"].startswith(".github/workflows/") or
                not isinstance(binding.get("job"), str) or not binding["job"] or
                not isinstance(binding.get("artifact"), str) or
                not binding["artifact"].endswith("-{run_id}-{run_attempt}") or
                binding["workflow"] == ".github/workflows/devlake-poc-qualify.yml"):
            raise ValueError(f"criterion {criterion_id} has no separately approved source workflow")
    if "delivery_cleanup" in required and bindings["delivery_cleanup"] != {
            "workflow": ".github/workflows/devlake-poc-infrastructure-source.yml",
            "job": "observe", "artifact": "devlake-infrastructure-finalized-{run_id}-{run_attempt}"}:
        raise ValueError("delivery cleanup requires the finalized infrastructure source")
    if bindings["providers"] != {
            "workflow": ".github/workflows/devlake-poc-provider-profile.yml",
            "job": "observe", "artifact": "devlake-provider-profile-{run_id}-{run_attempt}"}:
        raise ValueError("provider evidence requires the independent provider/Profile source")

    def read(artifact_id):
        if not isinstance(artifact_id, str) or not artifact_id.isdecimal() or artifact_id == "0":
            raise ValueError("independent source requires an actual GitHub artifact ID")
        url = f"repos/{REPOSITORY}/actions"
        artifact = _metadata(f"{url}/artifacts/{artifact_id}", request)
        source_run = artifact.get("workflow_run")
        run_id = source_run.get("id") if isinstance(source_run, dict) else None
        if (type(run_id) is not int or run_id <= 0 or artifact.get("expired") is not False or
                source_run.get("head_sha") != manifest["source_revision"]):
            raise ValueError("independent artifact is missing, expired or from a different release")
        run = _metadata(f"{url}/runs/{run_id}", request)
        jobs = _metadata(f"{url}/runs/{run_id}/jobs?per_page=100", request)
        if (run.get("id") != run_id or run.get("head_sha") != manifest["source_revision"] or
                run.get("conclusion") != "success" or run.get("event") != "workflow_dispatch" or
                type(run.get("run_attempt")) is not int or run.get("run_attempt") <= 0 or
                run_id == int(manifest["run_id"]) or
                not isinstance(jobs.get("jobs"), list) or
                jobs.get("total_count", 0) != len(jobs["jobs"])):
            raise ValueError("independent source workflow is missing or incomplete")
        references = {**manifest["criteria"], "native": manifest.get("native_receipt"),
                      "providers": manifest.get("provider_receipt"),
                      "delivery_cleanup": manifest.get("delivery_cleanup_receipt")}
        run_path = str(run.get("path") or "").split("@", 1)[0]
        matching = [criterion_id for criterion_id, reference in references.items()
                    if isinstance(reference, dict) and reference.get("ref") == artifact_id]
        if not matching or any(
                artifact.get("name") != bindings[criterion_id]["artifact"].format(
                    run_id=run_id, run_attempt=run["run_attempt"]) or
                run_path not in (bindings[criterion_id]["workflow"],
                                 f"{REPOSITORY}/" + bindings[criterion_id]["workflow"]) or
                not any(isinstance(job, dict) and job.get("name") == bindings[criterion_id]["job"] and
                        job.get("conclusion") == "success" and job.get("run_id") == run_id and
                        job.get("runner_name") for job in jobs["jobs"])
                for criterion_id in matching):
            raise ValueError("independent source job or artifact differs from approved bindings")
        archive = request(f"{url}/artifacts/{artifact_id}/zip")
        if not isinstance(archive, bytes) or len(archive) > MAX_ARCHIVE_BYTES:
            raise ValueError("independent source archive is invalid")
        try:
            with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
                if bundle.namelist() != ["observation.json"] or bundle.getinfo("observation.json").file_size > MAX_RECEIPT_BYTES:
                    raise ValueError("independent source archive must contain one bounded observation")
                raw = bundle.read("observation.json")
                try:
                    document = json.loads(raw)
                except (UnicodeError, json.JSONDecodeError) as error:
                    raise ValueError("independent source observation is invalid") from error
                if (not isinstance(document, dict) or not isinstance(document.get("observations"), dict) or
                        not document["observations"]):
                    raise ValueError("independent source has no observed measurements")
                return raw
        except (OSError, zipfile.BadZipFile) as error:
            raise ValueError("independent source archive is invalid") from error

    return read
