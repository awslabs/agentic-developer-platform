"""Saved-plan image-only maintenance for an installed dedicated reader.

Call with the authorized Installer and separately qualified image. The original
installation/release, Secrets, IAM, native admission and worker binding remain.
An attempted apply must be observed, never replayed.
"""

import copy
import json

from .config import LABEL, digest, require
from .runner import atomic
from .runtime_maintenance import (
    api_pods,
    container,
    objects,
    paused,
    payload,
    read,
    replacements,
    runtime_digests,
)

NAME = "superplane-dedicated-readers"
KIND = "dedicated-reader-image-maintenance-v1"


def target(before, image):
    require(
        before["kind"] == "Deployment" and before["metadata"]["name"] == NAME,
        "Reader maintenance target differs",
    )
    after = copy.deepcopy(before)
    selected = container(after["spec"]["template"]["spec"], NAME)
    require(
        selected["image"] != image and "@sha256:" in selected["image"],
        "Reader maintenance requires a distinct pinned image",
    )
    selected["image"] = image
    return after


def plan(installer, image, qualification_sha256):
    installer.verify_deployment_identity()
    paused(installer, objects(installer))
    before = payload(read(installer, "Deployment", NAME))
    require(
        before["metadata"].get("labels", {}).get(LABEL) == installer.owner,
        "Reader deployment belongs to another installation",
    )
    require(len(qualification_sha256) == 64, "Bind the reviewed image qualification")
    return {
        "kind": KIND,
        "installation_id": installer.owner,
        "release_id": installer.release,
        "qualification_sha256": qualification_sha256,
        "before": before,
        "after": target(before, image),
        "image": image,
        "runtime_digests": runtime_digests(installer, image),
    }


def reviewed(installer, value, approved):
    require(
        value["kind"] == KIND
        and digest(value) == approved
        and value["installation_id"] == installer.owner
        and value["release_id"] == installer.release
        and value["after"] == target(value["before"], value["image"]),
        "Reader maintenance exceeds its exact saved image plan",
    )


def observe(installer, value, approved):
    reviewed(installer, value, approved)
    installer.verify_deployment_identity()
    actual = read(installer, "Deployment", NAME)
    require(
        payload(actual) == value["after"], "Reader deployment differs from saved result"
    )
    status = actual.get("status", {})
    desired = actual["spec"].get("replicas", 1)
    require(
        desired > 0
        and status.get("observedGeneration") == actual["metadata"]["generation"]
        and status.get("updatedReplicas") == status.get("availableReplicas") == desired,
        "Reader rollout has not settled",
    )
    pods = [
        p
        for p in api_pods(installer, actual)
        if p.get("status", {}).get("phase") not in {"Succeeded", "Failed"}
    ]
    require(len(pods) == desired, "Reader pod inventory is not settled")
    for pod in pods:
        selected = container(pod["spec"], NAME)
        states = {s["name"]: s for s in pod["status"]["containerStatuses"]}
        require(
            not pod["metadata"].get("deletionTimestamp")
            and selected["image"] == value["image"]
            and states[NAME]["ready"]
            and states[NAME]["imageID"].rsplit("@", 1)[-1] in value["runtime_digests"],
            "Reader process image or readiness differs",
        )
    return {
        "state": "reader-image-observed",
        "deployment": payload(actual),
        "pods": [
            {"name": p["metadata"]["name"], "uid": p["metadata"]["uid"]} for p in pods
        ],
    }


def apply(installer, value, approved):
    reviewed(installer, value, approved)
    require(
        not (installer.directory / "reader-image-intent.json").exists(),
        "Observe an attempted reader maintenance; do not replay",
    )
    installer.verify_deployment_identity()
    with installer.exclusive():
        paused(installer, objects(installer))
        live = read(installer, "Deployment", NAME)
        require(payload(live) == value["before"], "Reader changed after planning")
        patch = [
            {"op": "test", "path": "/metadata/uid", "value": live["metadata"]["uid"]},
            {
                "op": "test",
                "path": "/metadata/resourceVersion",
                "value": live["metadata"]["resourceVersion"],
            },
            *replacements(value["before"], value["after"]),
        ]
        atomic(
            installer.directory / "reader-image-intent.json",
            {"patch": patch, "plan_sha256": approved},
        )
        installer.kube(
            "patch",
            "Deployment",
            NAME,
            "-n",
            installer.env["namespace"],
            "--type=json",
            "--patch-file=/dev/stdin",
            data=json.dumps(patch),
        )
        installer.kube(
            "rollout",
            "status",
            "deployment/" + NAME,
            "-n",
            installer.env["namespace"],
            "--timeout=180s",
            timeout=190,
        )
        result = observe(installer, value, approved)
        installer.receipt.update(
            status="dedicated-reader-runtime-maintenance-complete",
            reader_runtime_maintenance=result,
            reader_plan_sha256=approved,
        )
        installer.save()
    return result
