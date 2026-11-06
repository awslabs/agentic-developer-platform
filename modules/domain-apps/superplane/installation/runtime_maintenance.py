"""Saved-plan image/binding maintenance of an existing paused installation.

This does not install infrastructure, migrate schemas, activate dispatch, or
substitute a reconstructed document for an earlier installation receipt.
"""

import argparse
import copy
import hashlib
import json
import re
import time
from pathlib import Path

from . import paid_worker
from .config import LABEL, Refusal, digest, image, load, require, validate
from .lifecycle_worker import expected_binding
from .runner import Installer, atomic, local_lock

TARGETS = (
    ("Deployment", "superplane-api"),
    ("ScaledJob", "superplane-paid-worker"),
    ("ConfigMap", "superplane-paid-worker-binding"),
    ("ConfigMap", "superplane-paid-worker-config"),
)
KIND = "paused-runtime-maintenance-v1"


def payload(value):
    """All configurable fields and UID, excluding server bookkeeping/status."""
    result = copy.deepcopy(value)
    result.pop("status", None)
    for key in ("resourceVersion", "generation", "managedFields", "creationTimestamp"):
        result["metadata"].pop(key, None)
    if result.get("kind") == "Deployment":
        annotations = result["metadata"].get("annotations", {})
        annotations.pop("deployment.kubernetes.io/revision", None)
        if not annotations:
            result["metadata"].pop("annotations", None)
    return result


def container(pod, name):
    matches = [c for c in pod["containers"] if c["name"] == name]
    require(len(matches) == 1, "Maintenance container is missing or duplicated")
    return matches[0]


def variable(primary, name):
    matches = [e for e in primary["env"] if e["name"] == name]
    require(
        len(matches) == 1 and set(matches[0]) == {"name", "value"},
        "Maintenance requires the existing explicit runtime setting",
    )
    return matches[0]


def read(installer, kind, name):
    return installer.json(
        installer.kube(
            "get", kind, name, "-n", installer.env["namespace"], "-o", "json"
        )
    )


def objects(installer):
    return [read(installer, *target) for target in TARGETS]


def paused(installer, values):
    api, worker, *_ = values
    primary = container(api["spec"]["template"]["spec"], "superplane-api")
    require(
        variable(primary, "SUPERPLANE_OPERATION_DISPATCH_ENABLED")["value"] == "false"
        and worker["metadata"].get("annotations", {}).get("autoscaling.keda.sh/paused")
        == "true",
        "Maintenance requires disabled dispatch and paused worker launch",
    )
    for kind in ("jobs", "pods"):
        rows = installer.json(
            installer.kube(
                "get",
                kind,
                "-n",
                installer.env["namespace"],
                "-l",
                "scaledjob.keda.sh/name=superplane-paid-worker",
                "-o",
                "json",
            )
        )["items"]
        for row in rows:
            status = row.get("status", {})
            terminal = (
                status.get("phase") in {"Succeeded", "Failed"}
                if kind == "pods"
                else not status.get("active", 0)
                and any(
                    c.get("type") in {"Complete", "Failed"}
                    and c.get("status") == "True"
                    for c in status.get("conditions", [])
                )
            )
            require(terminal, "Maintenance refuses unfinished worker Jobs or pods")
    for row in api_pods(installer, api):
        if row.get("status", {}).get("phase") not in {"Succeeded", "Failed"}:
            require(
                variable(
                    container(row["spec"], "superplane-api"),
                    "SUPERPLANE_OPERATION_DISPATCH_ENABLED",
                )["value"]
                == "false",
                "An existing API pod still enables dispatch",
            )


def api_pods(installer, api):
    selector = api["spec"]["selector"]
    require(
        set(selector) == {"matchLabels"} and selector["matchLabels"],
        "Maintenance requires the exact Deployment label selector",
    )
    labels = ",".join(f"{k}={v}" for k, v in sorted(selector["matchLabels"].items()))

    def selected(kind):
        return installer.json(
            installer.kube(
                "get",
                kind,
                "-n",
                installer.env["namespace"],
                "-l",
                labels,
                "-o",
                "json",
            )
        )["items"]

    def owned(value, kind, uids):
        return any(
            r.get("kind") == kind
            and r.get("uid") in uids
            and r.get("controller") is True
            for r in value["metadata"].get("ownerReferences", [])
        )

    replicas = {
        r["metadata"]["uid"]
        for r in selected("replicasets")
        if owned(r, "Deployment", {api["metadata"]["uid"]})
    }
    result = []
    for pod in selected("pods"):
        if owned(pod, "ReplicaSet", replicas):
            result.append(pod)
        elif pod.get("status", {}).get("phase") not in {"Succeeded", "Failed"}:
            require(
                any(
                    c.get("type") == "Ready" and c.get("status") == "False"
                    for c in pod.get("status", {}).get("conditions", [])
                ),
                "A serving or ambiguous API pod has a foreign owner chain",
            )
    return result


def manifest_platform(raw, expected):
    """Resolve only a child authenticated by the requested immutable manifest."""
    require(
        "sha256:" + hashlib.sha256(raw.encode()).hexdigest() == expected,
        "Registry manifest differs from its requested digest",
    )
    value = json.loads(raw)
    require(value.get("schemaVersion") == 2, "Unsupported image manifest version")
    manifests = {
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    }
    if value.get("mediaType") in manifests:
        require("config" in value and "layers" in value, "Invalid platform manifest")
        return expected
    require(
        value.get("mediaType")
        in {
            "application/vnd.oci.image.index.v1+json",
            "application/vnd.docker.distribution.manifest.list.v2+json",
        },
        "Unsupported image index",
    )
    matches = [
        m
        for m in value.get("manifests", [])
        if m.get("platform", {}).get("os") == "linux"
        and m.get("platform", {}).get("architecture") == "amd64"
    ]
    require(
        len(matches) == 1
        and matches[0].get("mediaType") in manifests
        and not matches[0].get("urls")
        and not matches[0].get("data")
        and re.fullmatch(r"sha256:[a-f0-9]{64}", str(matches[0].get("digest", ""))),
        "Image index has no unique supported linux/amd64 platform",
    )
    return matches[0]["digest"]


def runtime_digests(installer, reference):
    prefix = f"{installer.env['account_id']}.dkr.ecr.{installer.env['region']}.amazonaws.com/"
    require(
        reference.startswith(prefix) and reference.count("@") == 1,
        "Maintenance runtime image must name the selected ECR registry",
    )
    repository, expected = reference[len(prefix) :].split("@")

    def fetch(selected):
        response = installer.json(
            installer.aws(
                "ecr",
                "batch-get-image",
                "--repository-name",
                repository,
                "--image-ids",
                "imageDigest=" + selected,
            )
        )
        require(
            not response.get("failures") and len(response.get("images", [])) == 1,
            "Maintenance runtime manifest is unavailable",
        )
        return manifest_platform(response["images"][0]["imageManifest"], selected)

    platform = fetch(expected)
    require(
        platform == expected or fetch(platform) == platform,
        "Index child is not the selected platform manifest",
    )
    return sorted({expected, platform})


def verify_rollout(installer, value):
    expected = {
        c["name"]: c["image"]
        for c in value["after"][0]["spec"]["template"]["spec"]["containers"]
    }
    pods = api_pods(installer, value["after"][0])
    deadline = time.monotonic() + installer.env["timeout_seconds"]
    while any(p["metadata"].get("deletionTimestamp") for p in pods):
        require(
            time.monotonic() < deadline, "Previous API pod remains during maintenance"
        )
        time.sleep(1)
        pods = api_pods(installer, value["after"][0])
    require(bool(pods), "No actual API pod was observed after rollout")
    for pod in pods:
        actual = {c["name"]: c["image"] for c in pod["spec"]["containers"]}
        states = {
            c["name"]: c for c in pod.get("status", {}).get("containerStatuses", [])
        }
        require(
            all(
                actual.get(name) == ref
                and states.get(name, {}).get("ready") is True
                and states[name].get("imageID", "").rsplit("@", 1)[-1]
                in value["runtime_image_digests"][ref]
                for name, ref in expected.items()
            ),
            "Actual API or transport image/readiness differs from the reviewed maintenance",
        )


def verify_schema(installer, api):
    pods = [
        p
        for p in api_pods(installer, api)
        if not p["metadata"].get("deletionTimestamp")
        and p.get("status", {}).get("phase") == "Running"
    ]
    require(bool(pods), "An actual owned API pod is required for the schema read")
    pod = sorted(pods, key=lambda p: p["metadata"]["name"])[0]
    report = installer.json(
        installer.kube(
            "exec",
            pod["metadata"]["name"],
            "-n",
            installer.env["namespace"],
            "-c",
            "superplane-api",
            "--",
            "python",
            "-m",
            "app.installation",
            "database",
        )
    )
    expected = {
        "database": installer.env["database"]["database"],
        "schema": installer.env["database"]["schema"],
        "revision": installer.lock["schema"]["observed"]["head"],
    }
    require(
        all(report.get(k) == v for k, v in expected.items()),
        "Complete the real scoped schema migration before runtime maintenance",
    )
    return expected


def plan(
    installer,
    before,
    transport_mode="preserve",
    transport_qualification=None,
    *,
    retain_worker=False,
    worker_active_deadline_seconds=None,
):
    require(type(retain_worker) is bool, "retain_worker must be a boolean")
    projected = {}
    if retain_worker or worker_active_deadline_seconds is not None:
        require(
            bool(installer.env.get("paid_worker")),
            "Maintenance requires a selected native worker",
        )
        paid_worker.validate(installer.env, installer.lock, retain_worker=retain_worker)
        docs = [{"metadata": {"labels": {LABEL: installer.owner}}}]
        paid_worker.project(installer.env, installer.lock, docs)
        projected = {(v["kind"], v["metadata"]["name"]): v for v in docs[1:]}
    if worker_active_deadline_seconds is not None:
        require(
            type(worker_active_deadline_seconds) is int
            and worker_active_deadline_seconds
            == projected[TARGETS[1]]["spec"]["jobTargetRef"]["activeDeadlineSeconds"],
            "Worker deadline must equal the reviewed native environment projection",
        )
    require(
        transport_mode in {"preserve", "matched-api"}, "Unknown transport selection"
    )
    require(
        (transport_mode == "preserve" and transport_qualification is None)
        or (
            transport_mode == "matched-api"
            and isinstance(transport_qualification, str)
            and re.fullmatch(r"[a-f0-9]{64}", transport_qualification)
        ),
        "Matched transport requires the reviewed TLS/route/image qualification digest",
    )
    require(
        len(before) == len(TARGETS),
        "Maintenance requires exactly four existing objects",
    )
    before = [payload(v) for v in before]
    for value, (kind, name) in zip(before, TARGETS, strict=True):
        meta = value["metadata"]
        require(
            value["kind"] == kind
            and meta["name"] == name
            and meta["namespace"] == installer.env["namespace"]
            and meta.get("uid")
            and not meta.get("deletionTimestamp")
            and meta.get("labels", {}).get(LABEL) == installer.owner,
            "Maintenance target identity or installation ownership differs",
        )
    after = copy.deepcopy(before)
    api, worker, binding, worker_config = after
    pod = api["spec"]["template"]["spec"]
    primary = container(pod, "superplane-api")
    primary["image"] = image(installer.lock, "superplane-api")
    variable(primary, "SUPERPLANE_SOURCE_REVISION")["value"] = installer.lock[
        "source_revision"
    ]
    variable(primary, "SUPERPLANE_RELEASE_ID")["value"] = installer.release
    if transport_mode == "matched-api":
        container(pod, "recovery-transport")["image"] = primary["image"]
    native = container(
        worker["spec"]["jobTargetRef"]["template"]["spec"], "paid-worker"
    )
    old_image = native["image"]
    native["image"] = image(installer.lock, "superplane-paid-worker")
    require("@sha256:" in old_image, "Current worker image must be immutable")
    require(
        not retain_worker or old_image == native["image"],
        "Retained worker image differs from the installed immutable image",
    )
    current = json.loads(binding["data"]["binding.json"])
    desired = expected_binding(installer.env, installer.lock)
    require(
        current == dict(desired, worker_image_digest=old_image.rsplit("@", 1)[1]),
        "Existing worker binding differs beyond its image digest",
    )
    if not retain_worker:
        binding["data"]["binding.json"] = json.dumps(desired, sort_keys=True)
    settings = worker_config["data"]
    require(
        settings.get("SUPERPLANE_DOMAIN_SCHEMA") == installer.env["database"]["schema"]
        and settings.get("SUPERPLANE_OPERATION_SCHEMA")
        == installer.env["paid_worker"]["operation_schema"]
        and isinstance(settings.get("SUPERPLANE_DOMAIN_SCHEMA_HEAD"), str),
        "Existing worker schema identities differ from the selected installation",
    )
    if retain_worker:
        require(
            all(settings.get(k) == v for k, v in projected[TARGETS[3]]["data"].items()),
            "Retained worker configuration differs from the reviewed environment and schema",
        )
    else:
        settings["SUPERPLANE_DOMAIN_SCHEMA_HEAD"] = installer.lock["schema"][
            "observed"
        ]["head"]
    if worker_active_deadline_seconds is not None:
        require(
            type(worker["spec"]["jobTargetRef"].get("activeDeadlineSeconds")) is int,
            "Maintenance requires the existing explicit worker deadline",
        )
        worker["spec"]["jobTargetRef"]["activeDeadlineSeconds"] = projected[TARGETS[1]][
            "spec"
        ]["jobTargetRef"]["activeDeadlineSeconds"]
    result = {
        "kind": KIND,
        "version": 1,
        "installation_id": installer.owner,
        "environment_sha256": digest(installer.env),
        "release_id": installer.release,
        "source_revision": installer.lock["source_revision"],
        "database": verify_schema(installer, before[0]),
        "transport_mode": transport_mode,
        "transport_qualification_sha256": transport_qualification,
        "runtime_image_digests": {
            ref: runtime_digests(installer, ref)
            for ref in sorted({c["image"] for c in pod["containers"]})
        },
        "before": before,
        "after": after,
    }
    # Preserve byte-for-byte replay of plans created before these opt-in changes.
    if retain_worker:
        result["retain_worker"] = True
    if worker_active_deadline_seconds is not None:
        result["worker_active_deadline_seconds"] = worker_active_deadline_seconds
    return result


def replacements(before, after, path=""):
    """Generate only replacements; maintenance never adds or removes a field."""
    if before == after:
        return []
    if isinstance(before, dict) and isinstance(after, dict):
        require(
            before.keys() == after.keys(), "Maintenance cannot add or remove fields"
        )
        result = []
        for key in before:
            escaped = key.replace("~", "~0").replace("/", "~1")
            result.extend(replacements(before[key], after[key], path + "/" + escaped))
        return result
    if isinstance(before, list) and isinstance(after, list):
        require(len(before) == len(after), "Maintenance cannot change list membership")
        return [
            p
            for i, (a, b) in enumerate(zip(before, after, strict=True))
            for p in replacements(a, b, path + "/" + str(i))
        ]
    return [
        {"op": "test", "path": path, "value": before},
        {"op": "replace", "path": path, "value": after},
    ]


def reviewed(installer, value, approved):
    require(digest(value) == approved, "Exact saved maintenance plan approval required")
    require(
        value
        == plan(
            installer,
            value["before"],
            value["transport_mode"],
            value["transport_qualification_sha256"],
            retain_worker=value.get("retain_worker", False),
            worker_active_deadline_seconds=value.get("worker_active_deadline_seconds"),
        ),
        "Maintenance plan differs from the closed image/binding change",
    )
    return value


def observe(installer, value):
    current = objects(installer)
    paused(installer, current)
    states = []
    for live, before, after in zip(
        current, value["before"], value["after"], strict=True
    ):
        state = payload(live)
        require(
            state in (before, after),
            "Current object differs from both saved maintenance states",
        )
        states.append("desired" if state == after else "previous")
    return current, states


def apply(installer, value, approved):
    reviewed(installer, value, approved)
    require(
        not installer.receipt.get("remote_lock")
        and not installer.receipt.get("lock_attempt"),
        "Recover the retained native installation lock before continuing",
    )
    installer.receipt["runtime_maintenance"] = {
        **installer.receipt.get("runtime_maintenance", {}),
        "plan_sha256": approved,
        "transport_mode": value["transport_mode"],
    }
    installer.save()
    with installer.exclusive():
        observe(installer, value)
        verify_schema(installer, value["before"][0])
        # Keep launch paused while changing its image/binding, then replace API.
        for index in (1, 3, 2, 0):
            live, states = observe(installer, value)
            current = live[index]
            kind, name = TARGETS[index]
            journal = installer.receipt["runtime_maintenance"].setdefault("objects", {})
            if states[index] == "desired":
                journal[name] = {
                    "state": "observed-desired",
                    "uid": current["metadata"]["uid"],
                }
                installer.save()
                continue
            meta = current["metadata"]
            require(
                meta.get("resourceVersion"), "Current object resource version is absent"
            )
            patch = [
                {"op": "test", "path": "/metadata/uid", "value": meta["uid"]},
                {
                    "op": "test",
                    "path": "/metadata/resourceVersion",
                    "value": meta["resourceVersion"],
                },
                {
                    "op": "test",
                    "path": "/metadata/labels/" + LABEL.replace("/", "~1"),
                    "value": installer.owner,
                },
                *replacements(value["before"][index], value["after"][index]),
            ]
            journal[name] = {
                "state": "write-pending",
                "uid": meta["uid"],
                "resource_version": meta["resourceVersion"],
                "patch_sha256": digest(patch),
            }
            installer.save()
            installer.kube(
                "patch",
                kind,
                name,
                "-n",
                installer.env["namespace"],
                "--type=json",
                "--field-manager=superplane-installer",
                "--patch-file=/dev/stdin",
                "-o",
                "json",
                data=json.dumps(patch),
            )
            actual = read(installer, kind, name)
            require(
                payload(actual) == value["after"][index],
                "Maintenance write readback differs",
            )
            journal[name] = {
                "state": "written-and-observed",
                "uid": actual["metadata"]["uid"],
                "resource_version": actual["metadata"]["resourceVersion"],
            }
            installer.save()
        installer.kube(
            "rollout",
            "status",
            "deployment/superplane-api",
            "-n",
            installer.env["namespace"],
            f"--timeout={installer.env['timeout_seconds']}s",
            timeout=installer.env["timeout_seconds"] + 30,
        )
        _, states = observe(installer, value)
        require(
            all(s == "desired" for s in states),
            "Maintenance did not reach every reviewed state",
        )
        verify_rollout(installer, value)
        verify_schema(installer, value["after"][0])
        installer.receipt["runtime_maintenance"]["state"] = "observed-paused"
        installer.receipt["status"] = "runtime-maintenance-complete"
        installer.save()
    return installer.receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("environment", "release-lock", "output"):
        parser.add_argument("--" + name, required=True, type=Path)
    mode = parser.add_mutually_exclusive_group(required=True)
    for name in ("plan", "apply", "observe", "recover-lock"):
        mode.add_argument("--" + name, action="store_true")
    parser.add_argument("--approved-plan-sha256")
    parser.add_argument("--confirm-stopped")
    parser.add_argument(
        "--transport-mode", choices=("preserve", "matched-api"), default="preserve"
    )
    parser.add_argument("--transport-qualification-sha256")
    parser.add_argument("--retain-worker", action="store_true")
    parser.add_argument("--worker-active-deadline-seconds", type=int)
    args = parser.parse_args(argv)
    try:
        env, lock = load(args.environment), load(args.release_lock)
        with local_lock(args.output):
            path = args.output / "maintenance-plan.json"
            value = None if args.plan else load(path)
            if value is not None:
                require(
                    digest(value) == args.approved_plan_sha256,
                    "Exact saved maintenance plan approval required",
                )
                require(
                    not args.retain_worker
                    and args.worker_active_deadline_seconds is None,
                    "Worker selections are plan-only; replay uses the exact saved plan",
                )
            validate(
                env,
                lock,
                control_plane_only=env.get("control_plane_only", False),
                retain_worker=args.retain_worker
                if args.plan
                else value.get("retain_worker", False),
            )
            installer = Installer(env, lock, args.output.resolve())
            if args.plan:
                require(
                    not path.exists() and not installer.receipt_path.exists(),
                    "Use a new maintenance output directory",
                )
                installer.target()
                before = objects(installer)
                paused(installer, before)
                value = plan(
                    installer,
                    before,
                    args.transport_mode,
                    args.transport_qualification_sha256,
                    retain_worker=args.retain_worker,
                    worker_active_deadline_seconds=args.worker_active_deadline_seconds,
                )
                atomic(path, value)
                installer.receipt["runtime_maintenance"] = {
                    "plan_sha256": digest(value)
                }
                installer.save()
            else:
                previous = load(installer.receipt_path)
                require(
                    previous.get("installation_id") == installer.owner
                    and previous.get("environment") == installer.env
                    and previous.get("release_lock") == installer.lock
                    and previous.get("runtime_maintenance", {}).get("plan_sha256")
                    == digest(value)
                    and re.fullmatch(r"[a-f0-9]{16}", str(previous.get("run_id", ""))),
                    "Maintenance receipt identity differs",
                )
                installer.receipt, installer.run_id = previous, previous["run_id"]
                # Live schema review uses kube exec, whose identity check saves
                # observations. Restore the genuine receipt before any such read.
                installer.target()
                value = reviewed(installer, value, args.approved_plan_sha256)
                if args.recover_lock:
                    installer.recover_lock(args.confirm_stopped)
                elif args.observe:
                    _, states = observe(installer, value)
                    print(
                        json.dumps(
                            {
                                "state": "observed-only",
                                "objects": states,
                                "lock_retained": bool(
                                    previous.get("remote_lock")
                                    or previous.get("lock_attempt")
                                ),
                            }
                        )
                    )
                    return 0
                else:
                    apply(installer, value, args.approved_plan_sha256)
            print(
                json.dumps(
                    {
                        "status": installer.receipt["status"],
                        "plan_sha256": digest(value),
                        "run_id": installer.run_id,
                        "receipt": str(installer.receipt_path),
                    }
                )
            )
            return 0
    except Refusal as exc:
        print(json.dumps({"status": "refused", "reason": str(exc)}))
        return 2
    except Exception:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "reason": "Preserve private maintenance plan and receipt",
                }
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
