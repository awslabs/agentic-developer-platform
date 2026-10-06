"""Resolve actual Gateway deployment source; never infer it from workflow HEAD.

GitHub Deployment records retain new successful releases independently of Actions
artifact expiry. Pre-receipt workflows may use their authenticated release artifact.
Every baseline requires a completed successful backend job for the same run attempt.
"""

import argparse
import hashlib
import io
import json
import os
import re
import subprocess
import zipfile
from pathlib import Path

WORKFLOW = ".github/workflows/gateway-deploy.yml"
JOB = "Build and Deploy Backend"
ROLLOUT = "Migrate schema then roll out new image"
RECEIPT_STEP = "Publish successful Gateway source receipt"
TASK = "adp-gateway-source-v1"
MAX_PAGES = 10
MAX_BYTES = 65536


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}", value) is not None


def target(env):
    role = re.fullmatch(r"arn:aws:iam::([0-9]{12}):role/adp-[a-z0-9-]+-trusted-deployment", env["ADP_RECEIPT_ROLE"])
    require(role is not None, "protected deployment role missing or invalid")
    environment = env["ADP_RECEIPT_ENVIRONMENT"]
    require(re.fullmatch(r"[a-z0-9-]+", environment), "invalid environment")
    require(env["ADP_RECEIPT_TARGET"] == "adp-gateway-deploy-" + environment, "target environment mismatch")
    return {
        "repository_id": int(env["ADP_RECEIPT_REPOSITORY_ID"]),
        "account_id": role[1],
        "region": env["ADP_RECEIPT_REGION"],
        "resource_id": f"adp-{environment}-eks-cluster/adp-gateway",
    }


def validate_release(data, run, expected):
    require(isinstance(data, dict), "release must be an object")
    bindings = {
        **expected,
        "schema_version": 1,
        "component": "gateway-backend",
        "workflow_path": WORKFLOW,
        "run_id": run["id"],
        "run_attempt": run["run_attempt"],
        "workflow_revision": run["head_sha"],
    }
    for key, value in bindings.items():
        require(type(data.get(key)) is type(value) and data[key] == value, f"release {key} mismatch")
    require(sha(data.get("source_revision")), "release source is not immutable")
    require(sha(data.get("workflow_revision")), "release definition is not immutable")
    require(re.fullmatch(r"sha256:[0-9a-f]{64}", data.get("image_digest", "")), "release image digest invalid")
    require(data.get("assets") == {}, "backend receipt contains unexpected assets")
    return data["source_revision"]


def validate_run(run, repo_id):
    require(run.get("repository", {}).get("id") == repo_id, "run repository mismatch")
    require(run.get("head_branch") == "main", "run is not main")
    require(run.get("path") == WORKFLOW, "run workflow mismatch")
    require(run.get("event") in ("push", "workflow_dispatch"), "run event invalid")
    require(sha(run.get("head_sha")), "run head is not immutable")
    require(type(run.get("run_attempt")) is int and run["run_attempt"] > 0, "run attempt invalid")


def pages(api, path, key=None):
    separator = "&" if "?" in path else "?"
    for page in range(1, MAX_PAGES + 1):
        result = api(f"{path}{separator}per_page=100&page={page}")
        values = result[key] if key else result
        require(isinstance(values, list) and len(values) <= 100, "invalid paginated response")
        yield from values
        if len(values) < 100:
            return
    raise ValueError("receipt lookup exceeded bounded history; cannot establish baseline")


def artifact_release(api, prefix, run, repo_id):
    artifacts = list(pages(api, f"{prefix}/actions/runs/{run['id']}/artifacts", "artifacts"))
    matches = [a for a in artifacts if a.get("name") == f"adp-release-gateway-backend-{run['run_attempt']}"]
    require(len(matches) == 1, "missing or ambiguous legacy release artifact")
    artifact = matches[0]
    require(artifact.get("expired") is False, "legacy release artifact expired")
    require(0 < artifact.get("size_in_bytes", 0) <= MAX_BYTES, "legacy artifact exceeds bound")
    for key, value in {
        "id": run["id"],
        "repository_id": repo_id,
        "head_repository_id": repo_id,
        "head_branch": "main",
        "head_sha": run["head_sha"],
    }.items():
        require(artifact.get("workflow_run", {}).get(key) == value, f"artifact {key} mismatch")
    archive = api(f"{prefix}/actions/artifacts/{artifact['id']}/zip", binary=True)
    require(len(archive) <= MAX_BYTES, "legacy archive exceeds bound")
    require(artifact.get("digest") == "sha256:" + hashlib.sha256(archive).hexdigest(), "artifact digest mismatch")
    with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
        entries = zipped.infolist()
        require(len(entries) == 1 and entries[0].filename == "release.json", "ambiguous release archive")
        require(entries[0].file_size <= MAX_BYTES, "release payload exceeds bound")
        return json.loads(zipped.read(entries[0]))


def resolve(env, api):
    expected = target(env)
    prefix = "repos/" + env["GITHUB_REPOSITORY"]
    runs = pages(api, f"{prefix}/actions/workflows/gateway-deploy.yml/runs?branch=main", "workflow_runs")
    for listed in runs:
        if listed["id"] == int(env["GITHUB_RUN_ID"]):
            continue
        run = api(f"{prefix}/actions/runs/{listed['id']}")
        validate_run(run, expected["repository_id"])
        jobs = list(pages(api, f"{prefix}/actions/runs/{run['id']}/attempts/{run['run_attempt']}/jobs", "jobs"))
        matches = [j for j in jobs if j.get("name") == JOB]
        require(len(matches) <= 1, "ambiguous backend job")
        if not matches:
            continue
        job = matches[0]
        steps = job.get("steps", [])
        rollout = [s for s in steps if s.get("name") == ROLLOUT]
        # Guard/build refusals never touched the serving release. A rollout
        # attempt without a successful job may have changed it: refuse stale fallback.
        if job.get("conclusion") != "success":
            require(
                not any(s.get("conclusion") not in (None, "skipped") or s.get("status") == "in_progress" for s in rollout),
                "later backend rollout did not finish successfully; baseline uncertain",
            )
            continue
        require(job.get("status") == "completed", "backend job not completed")
        require(len(rollout) == 1 and rollout[0].get("conclusion") == "success", "successful backend lacks successful rollout")
        receipts = list(pages(api, f"{prefix}/deployments?task={TASK}&environment={env['ADP_RECEIPT_TARGET']}"))
        matches = [
            d
            for d in receipts
            if isinstance(d.get("payload"), dict)
            and d["payload"].get("run_id") == run["id"]
            and d["payload"].get("run_attempt") == run["run_attempt"]
        ]
        require(len(matches) <= 1, "ambiguous durable deployment receipt")
        if matches:
            receipt = matches[0]
            require(receipt.get("task") == TASK and receipt.get("environment") == env["ADP_RECEIPT_TARGET"], "receipt target mismatch")
            source = validate_release(receipt["payload"], run, expected)
            require(receipt.get("sha") == source, "receipt source mismatch")
            statuses = list(pages(api, f"{prefix}/deployments/{receipt['id']}/statuses"))
            url = f"https://github.com/{env['GITHUB_REPOSITORY']}/actions/runs/{run['id']}/attempts/{run['run_attempt']}"
            require(statuses and statuses[0].get("state") in ("success", "inactive"), "receipt not successful")
            require(any(s.get("state") == "success" and s.get("log_url") == url for s in statuses), "receipt successful-run binding missing")
            return source
        require(not any(s.get("name") == RECEIPT_STEP for s in steps), "durable receipt missing for maintained workflow")
        # Only legacy workflows lacking the receipt step use authenticated artifacts.
        data = artifact_release(api, prefix, run, expected["repository_id"])
        return validate_release(data, run, expected)
    raise ValueError("no verified successful Gateway deployment baseline")


def publish(env, api):
    require(env.get("GITHUB_REF") == "refs/heads/main", "receipt publisher must run on main")
    expected = target(env)
    source, definition = env["ADP_RECEIPT_SOURCE"], env["ADP_RECEIPT_DEFINITION"]
    require(sha(source) and sha(definition), "receipt requires exact source and definition")
    require(definition == env["GITHUB_SHA"], "workflow definition differs from main run")
    run = {"id": int(env["GITHUB_RUN_ID"]), "run_attempt": int(env["GITHUB_RUN_ATTEMPT"]), "head_sha": definition}
    data = json.loads((Path(env["RUNNER_TEMP"]) / "adp-release-gateway-backend/release.json").read_text())
    require(validate_release(data, run, expected) == source, "built source differs from selected source")
    require(data["image_digest"] == env["ADP_RECEIPT_IMAGE"], "built image differs from deployed release")
    prefix = "repos/" + env["GITHUB_REPOSITORY"]
    result = api(
        prefix + "/deployments",
        body={
            "ref": source,
            "task": TASK,
            "auto_merge": False,
            "required_contexts": [],
            "environment": env["ADP_RECEIPT_TARGET"],
            "description": "Verified Gateway backend source and image",
            "payload": data,
            "production_environment": env["ADP_RECEIPT_ENVIRONMENT"] == "prod",
        },
    )
    require(result.get("sha") == source and type(result.get("id")) is int, "deployment receipt creation mismatch")
    url = f"https://github.com/{env['GITHUB_REPOSITORY']}/actions/runs/{run['id']}/attempts/{run['run_attempt']}"
    status = api(
        f"{prefix}/deployments/{result['id']}/statuses",
        body={
            "state": "success",
            "auto_inactive": False,
            "log_url": url,
            "description": "Backend rollout, internal-plane checks and engine parity passed",
        },
    )
    require(status.get("state") == "success", "deployment receipt status was not recorded")
    return result["id"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("resolve", "publish"))
    args = parser.parse_args()

    def api(path, binary=False, body=None):
        command = ["gh", "api", path]
        if body is not None:
            command += ["--method", "POST", "--input", "-"]
        result = subprocess.run(
            command, input=json.dumps(body).encode() if body is not None else None, capture_output=True, check=True, timeout=30
        ).stdout
        return result if binary else json.loads(result)

    if args.command == "resolve":
        source = resolve(os.environ, api)
        with open(os.environ["GITHUB_OUTPUT"], "a") as output:
            output.write("baseline=" + source + "\n")
        print("Verified deployed Gateway source: " + source)
    else:
        print("Recorded Gateway source receipt: " + str(publish(os.environ, api)))


if __name__ == "__main__":
    main()
