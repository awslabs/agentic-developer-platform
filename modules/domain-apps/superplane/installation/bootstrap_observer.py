"""Plan one recovery-only Job without enabling the paid ScaledJob or API.

Use the ordinary authenticated bootstrap /observe response after compatible
source/CI/image qualification. The reviewed image must contain observation_only.
No command override, credentials, queue body or paid admission is an input.
The existing native task bootstrap and recovery engine perform all observation,
claim fencing, accounting and settlement. Unrelated messages are not acknowledged.
"""

import copy
from datetime import datetime, timezone
import json
import re

from .config import LABEL, digest, require
from .runner import atomic
from .runtime_maintenance import container, objects, paused, payload

KIND = "bounded-bootstrap-observer-job-v1"
VARIABLE = "SUPERPLANE_OBSERVATION_ONLY"
FIELDS = {"operation_id", "org_id", "workspace_id", "job_id", "attempt_id"}
OBSERVER_LABEL = "adp.aws-e.io/bootstrap-observer"


def selected(receipt):
    selector = receipt["selector"]
    require(
        receipt["observation_only"] is True
        and receipt["mode"] == "recovery"
        and receipt["status"] == "pending"
        and receipt["domain"] == "superplane"
        and set(selector) == FIELDS
        and all(isinstance(v, str) and 1 <= len(v) <= 255 for v in selector.values())
        and all(receipt[k] == v for k, v in selector.items())
        and type(receipt["source_fence"]) is int
        and receipt["source_fence"] > 0
        and type(receipt["source_version"]) is int
        and receipt["source_version"] > 0
        and datetime.fromisoformat(receipt["not_after"]) > datetime.now(timezone.utc),
        "Fresh exact native recovery dispatch is required",
    )
    return selector


def render(worker, receipt, owner, attempt):
    selector = selected(receipt)
    require(
        re.fullmatch(r"[a-f0-9]{24}", attempt), "Use a new recorded observer attempt"
    )
    require(
        worker["kind"] == "ScaledJob"
        and worker["metadata"]["name"] == "superplane-paid-worker"
        and worker["metadata"].get("labels", {}).get(LABEL) == owner
        and worker["metadata"].get("annotations", {}).get("autoscaling.keda.sh/paused")
        == "true",
        "Observer must derive from the original paused owned worker",
    )
    spec = copy.deepcopy(worker["spec"]["jobTargetRef"])
    pod = spec["template"]["spec"]
    primary = container(pod, "paid-worker")
    require(
        len(pod["containers"]) == 1
        and not pod.get("initContainers")
        and not primary.get("command")
        and not primary.get("args")
        and "@sha256:" in primary["image"]
        and VARIABLE not in {e["name"] for e in primary["env"]},
        "Observer requires the qualified native worker entrypoint",
    )
    primary["env"].append(
        {"name": VARIABLE, "value": json.dumps(selector, sort_keys=True)}
    )
    mounts = [v for v in primary["volumeMounts"] if v["name"] == "state"]
    require(len(mounts) == 1, "Original lifecycle state must remain mounted")
    mounts[0]["readOnly"] = True
    labels = dict(spec["template"]["metadata"]["labels"])
    labels.pop("scaledjob.keda.sh/name", None)
    labels.update({LABEL: owner, OBSERVER_LABEL: attempt})
    # Preserve native NetworkPolicy selectors, but do not enable KEDA ownership.
    spec["template"]["metadata"]["labels"] = labels
    spec.update(backoffLimit=0, parallelism=1, completions=1, activeDeadlineSeconds=600)
    spec.pop("ttlSecondsAfterFinished", None)  # Owner proves UID-bound teardown.
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": "superplane-bootstrap-observer-" + attempt,
            "namespace": worker["metadata"]["namespace"],
            "labels": labels,
        },
        "spec": spec,
    }


def plan(installer, dispatch, *, attempt, qualification_sha256):
    installer.verify_deployment_identity()
    values = objects(installer)
    paused(installer, values)
    require(
        re.fullmatch(r"[a-f0-9]{64}", qualification_sha256),
        "Bind image/source qualification",
    )
    job = render(values[1], dispatch, installer.owner, attempt)
    api_selector = values[0]["spec"]["selector"]["matchLabels"]
    require(
        not all(
            job["spec"]["template"]["metadata"]["labels"].get(k) == v
            for k, v in api_selector.items()
        ),
        "Observer must not join the API Service",
    )
    return {
        "kind": KIND,
        "installation_id": installer.owner,
        "release_id": installer.release,
        "qualification_sha256": qualification_sha256,
        "dispatch": dispatch,
        "attempt": attempt,
        "before": [payload(v) for v in values],
        "job": job,
    }


def reviewed(installer, value, approved):
    require(
        value["kind"] == KIND
        and digest(value) == approved
        and value["installation_id"] == installer.owner
        and value["release_id"] == installer.release
        and value["job"]
        == render(
            value["before"][1], value["dispatch"], installer.owner, value["attempt"]
        ),
        "Observer differs from the reviewed exact recovery Job",
    )


def apply(installer, value, approved):
    reviewed(installer, value, approved)
    installer.verify_deployment_identity()
    require(
        installer.receipt.get("remote_lock"),
        "Observer requires the installation owner lock",
    )
    values = objects(installer)
    paused(installer, values)
    require(
        [payload(v) for v in values] == value["before"], "Paused installation changed"
    )
    jobs = installer.json(
        installer.kube(
            "get",
            "jobs",
            "-n",
            installer.env["namespace"],
            "-l",
            OBSERVER_LABEL,
            "-o",
            "json",
        )
    )["items"]
    require(
        not jobs, "Observe and remove the previous observer before starting another"
    )
    intent = installer.directory / "bootstrap-observer-create-intent.json"
    require(
        not intent.exists(),
        "Observe any previous create; never replay an uncertain Job",
    )
    atomic(intent, {"approved_plan_sha256": approved, "job": value["job"]})
    result = installer.json(
        installer.kube("create", "-f", "-", "-o", "json", data=json.dumps(value["job"]))
    )
    require(
        result["metadata"]["name"] == value["job"]["metadata"]["name"],
        "Observer create identity changed",
    )
    atomic(installer.directory / "bootstrap-observer-created.json", result)
    installer.receipt["bootstrap_observer"] = {
        "name": result["metadata"]["name"],
        "uid": result["metadata"]["uid"],
        "plan_sha256": approved,
        "paid_dispatch_enabled": False,
        "paid_launcher_enabled": False,
    }
    installer.save()
    return installer.receipt["bootstrap_observer"]


def observe(installer, value):
    """Read native Job/Pod termination; success is not a workspace-ready claim."""
    installer.verify_deployment_identity()
    job = installer.existing(value["job"])
    saved = installer.receipt.get("bootstrap_observer")
    require(saved is not None, "Reconcile a lost create from its exact intent first")
    require(
        job is not None
        and job["metadata"]["uid"] == saved["uid"]
        and job["metadata"]["labels"].get(LABEL) == installer.owner,
        "Original observer Job absent or replaced",
    )
    pods = installer.json(
        installer.kube(
            "get",
            "pods",
            "-n",
            installer.env["namespace"],
            "-l",
            OBSERVER_LABEL + "=" + value["attempt"],
            "-o",
            "json",
        )
    )["items"]
    require(len(pods) <= 1, "Observer exceeded its single-pod bound")
    for pod in pods:
        require(
            any(
                o.get("uid") == saved["uid"]
                and o.get("kind") == "Job"
                and o.get("controller") is True
                for o in pod["metadata"].get("ownerReferences", [])
            ),
            "Observer pod owner changed",
        )
    terminal = (
        not job["status"].get("active", 0)
        and any(
            c.get("type") in {"Complete", "Failed"} and c.get("status") == "True"
            for c in job["status"].get("conditions", [])
        )
        and all(p["status"].get("phase") in {"Succeeded", "Failed"} for p in pods)
    )
    result = {
        "job_uid": saved["uid"],
        "terminal": terminal,
        "job_status": job["status"],
        "pods": [
            {
                "name": p["metadata"]["name"],
                "uid": p["metadata"]["uid"],
                "phase": p["status"].get("phase"),
            }
            for p in pods
        ],
    }
    atomic(installer.directory / "bootstrap-observer-observed.json", result)
    return result, job


def teardown(installer, value):
    require(
        installer.receipt.get("remote_lock"),
        "Observer teardown requires the owner lock",
    )
    result, job = observe(installer, value)
    require(
        result["terminal"], "Observe a running Job; do not interrupt its settlement"
    )
    values = objects(installer)
    paused(installer, values)
    require([payload(v) for v in values] == value["before"], "Paid runtime changed")
    intent = installer.directory / "bootstrap-observer-delete-intent.json"
    require(
        not intent.exists(),
        "Observe the previous deletion; never replay an uncertain teardown",
    )
    options = {
        "apiVersion": "v1",
        "kind": "DeleteOptions",
        "preconditions": {
            "uid": job["metadata"]["uid"],
            "resourceVersion": job["metadata"]["resourceVersion"],
        },
        "propagationPolicy": "Foreground",
    }
    atomic(
        intent,
        {"job": value["job"]["metadata"], "options": options, "observed": result},
    )
    installer.kube(
        "delete",
        "--raw",
        "/apis/batch/v1/namespaces/"
        + installer.env["namespace"]
        + "/jobs/"
        + job["metadata"]["name"],
        "-f",
        "-",
        data=json.dumps(options),
    )
    return verify_absence(installer, value)


def verify_absence(installer, value):
    """Independent absence read also handles a lost deletion response."""
    installer.verify_deployment_identity()
    require(
        (installer.directory / "bootstrap-observer-delete-intent.json").exists(),
        "No reviewed observer teardown intent exists",
    )
    require(
        installer.existing(value["job"]) is None, "Observer Job removal remains pending"
    )
    pods = installer.json(
        installer.kube(
            "get",
            "pods",
            "-n",
            installer.env["namespace"],
            "-l",
            OBSERVER_LABEL + "=" + value["attempt"],
            "-o",
            "json",
        )
    )["items"]
    require(not pods, "Observer pod removal remains pending")
    values = objects(installer)
    paused(installer, values)
    require(
        [payload(v) for v in values] == value["before"],
        "Paid runtime changed during teardown",
    )
    result = {
        "observer_job_absent": True,
        "observer_pods_absent": True,
        "paid_dispatch_enabled": False,
        "paid_launcher_enabled": False,
    }
    atomic(installer.directory / "bootstrap-observer-absence.json", result)
    return result
