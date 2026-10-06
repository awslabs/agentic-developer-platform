"""Explicit recovery of historical kubectl image updates during staged rollout."""

import copy
import re

import yaml

from .config import COMPONENTS, LABEL, digest, require

KEY = "deployment_image_recovery"


def validate(env):
    if KEY not in env:
        return
    entries = env[KEY]
    require(
        isinstance(entries, list) and 0 < len(entries) <= len(COMPONENTS),
        "Deployment image recovery requires a bounded nonempty list",
    )
    seen = set()
    for entry in entries:
        require(
            isinstance(entry, dict)
            and set(entry) == {"name", "namespace", "uid", "spec_sha256", "images"},
            "Deployment image recovery requires exact object/spec/image identities",
        )
        name = entry["name"]
        require(
            isinstance(name, str) and name in COMPONENTS and name not in seen,
            "Deployment image recovery target is unknown or duplicated",
        )
        seen.add(name)
        namespace = env["skypilot_namespace" if name == "skypilot-api" else "namespace"]
        require(
            entry["namespace"] == namespace, "Deployment recovery namespace mismatch"
        )
        require(
            isinstance(entry["uid"], str)
            and re.fullmatch(r"[a-f0-9-]{36}", entry["uid"]),
            "Deployment recovery requires the observed UID",
        )
        require(
            isinstance(entry["spec_sha256"], str)
            and re.fullmatch(r"[a-f0-9]{64}", entry["spec_sha256"]),
            "Deployment recovery requires the reviewed current spec digest",
        )
        images = entry["images"]
        allowed = (
            {name, "authenticated-transport"} if name == "skypilot-api" else {name}
        )
        require(
            isinstance(images, dict) and bool(images) and set(images) <= allowed,
            "Deployment recovery container is outside the selected service",
        )
        for value in images.values():
            require(
                isinstance(value, str)
                and re.fullmatch(r"[^\s@]+@sha256:[a-f0-9]{64}", value),
                "Deployment recovery requires immutable prior images",
            )


def force_args(installer, doc, current):
    """Permit one fenced apply only after proving conflicts are reviewed images."""
    if doc["kind"] != "Deployment":
        return []
    entry = next(
        (
            x
            for x in installer.env.get(KEY, [])
            if (x["name"], x["namespace"])
            == (doc["metadata"]["name"], doc["metadata"].get("namespace"))
        ),
        None,
    )
    if entry is None:
        return []
    require(current is not None, "Deployment recovery cannot create or adopt an object")
    meta = current["metadata"]
    require(
        meta.get("uid") == entry["uid"]
        and meta.get("labels", {}).get(LABEL) == installer.owner,
        "Deployment recovery object identity/owner changed",
    )
    desired = {
        c["name"]: c["image"] for c in doc["spec"]["template"]["spec"]["containers"]
    }
    actual = {
        c["name"]: c["image"] for c in current["spec"]["template"]["spec"]["containers"]
    }
    changed = {name for name in desired if desired[name] != actual.get(name)}
    if not changed:
        # A prior fenced apply may have committed before its response was lost.
        # Ordinary apply still checks any remaining non-image ownership conflicts.
        return []
    require(
        installer.receipt.get("stage") == "rollout"
        and {"migration", "bootstrap"} <= set(installer.receipt.get("completed", []))
        and installer.receipt.get("remote_lock"),
        "Deployment image recovery requires the locked post-migration rollout",
    )
    require(
        digest(current["spec"]) == entry["spec_sha256"]
        and changed == set(entry["images"])
        and all(actual.get(name) == image for name, image in entry["images"].items()),
        "Deployment recovery differs from the reviewed prior spec/images",
    )
    # A fresh managedFields read must be the same object version as apply's fence.
    observed = installer.json(
        installer.kube(
            "get",
            "Deployment",
            entry["name"],
            "-n",
            entry["namespace"],
            "-o",
            "json",
            "--show-managed-fields",
        )
    )
    require(
        all(
            observed["metadata"].get(k) == meta.get(k)
            for k in ("uid", "resourceVersion")
        ),
        "Deployment changed during recovery ownership read",
    )
    for name in changed:
        owners = []
        for fields in observed["metadata"].get("managedFields", []):
            containers = (
                fields.get("fieldsV1", {})
                .get("f:spec", {})
                .get("f:template", {})
                .get("f:spec", {})
                .get("f:containers", {})
            )
            if "f:image" in containers.get('k:{"name":"' + name + '"}', {}):
                owners.append(
                    (
                        fields.get("manager"),
                        fields.get("operation"),
                        fields.get("subresource", ""),
                    )
                )
        require(
            owners == [("kubectl-patch", "Update", "")],
            "Deployment image has an unreviewed field owner",
        )
    probe = copy.deepcopy(doc)
    for container in probe["spec"]["template"]["spec"]["containers"]:
        if container["name"] in changed:
            container["image"] = actual[container["name"]]
    # Holding only these images at their current values must remove ALL conflicts.
    # The exact same UID/RV fences this dry-run and the subsequent forced apply.
    installer.kube(
        "apply",
        "--server-side",
        "--dry-run=server",
        "--field-manager=superplane-installer",
        "-f",
        "-",
        "-o",
        "json",
        data=yaml.safe_dump(probe),
    )
    installer.receipt.setdefault(KEY, []).append(
        {
            "name": entry["name"],
            "namespace": entry["namespace"],
            "uid": meta["uid"],
            "resource_version": meta["resourceVersion"],
            "review": entry,
            "desired_sha256": digest(doc),
            "status": "image-only-conflicts-verified-before-apply",
        }
    )
    installer.save()
    return ["--force-conflicts"]
