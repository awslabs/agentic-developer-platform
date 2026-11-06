"""Validate a source-bound component set before and after protected evaluation."""

import re

from modules.tools.scripts.release_scope import operator_sequence


DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
SOURCE = re.compile(r"[0-9a-f]{40}\Z")
BASELINE = {"gateway": "gateway-deploy.yml", "facade and reconciler": "tools-facade-deploy.yml"}
CONNECTORS = frozenset({"atlassian", "github", "slack", "jira"})


def selected_workflows(paths):
    selected = dict(BASELINE)
    for step in operator_sequence(paths):
        if step["workflow"]:
            selected[step["component"]] = step["workflow"]
    return selected


def route_group(manifest):
    group = manifest.get("route_group")
    if group is None:
        return {}
    if (not isinstance(group, dict) or set(group) != {"group", "generation", "status", "routes"}
            or group["group"] != "tool-connectors"
            or type(group["generation"]) is not int or group["generation"] < 1
            or not isinstance(group["status"], str) or group["status"] not in {"fenced", "legacy", "active"}
            or not isinstance(group["routes"], dict) or set(group["routes"]) != CONNECTORS):
        raise ValueError("Coordinator: pin exactly four connector routes and a release fence")
    identifiers = []
    for route in group["routes"].values():
        if (not isinstance(route, dict) or set(route) != {"target_id", "backend", "digest"}
                or not isinstance(route["target_id"], str)
                or not re.fullmatch(r"[A-Za-z0-9_-]+", route["target_id"])
                or not isinstance(route["backend"], str) or route["backend"] not in {"legacy", "adapter"}
                or not isinstance(route["digest"], str) or not DIGEST.fullmatch(route["digest"])):
            raise ValueError("Coordinator: connector route is missing an immutable target or backend")
        identifiers.append(route["target_id"])
    if len(set(identifiers)) != len(CONNECTORS):
        raise ValueError("Coordinator: connector routes must have distinct target identities")
    if group["status"] == "active" and "tool-adapters" not in manifest["components"]:
        raise ValueError("Coordinator: an active connector group requires the selected adapter release")
    if group["status"] in {"active", "legacy"} and any(
        route["backend"] != ("adapter" if group["status"] == "active" else "legacy") for route in group["routes"].values()
    ):
        raise ValueError("Coordinator: a partial connector group cannot reopen")
    return {"route_group": group}


def verify_manifest(manifest, *, source, account, gateway_image, facade_image):
    if not (isinstance(manifest, dict) and SOURCE.fullmatch(source)
            and re.fullmatch(r"[0-9]{12}", account)
            and manifest.get("source_revision") == source
            and manifest.get("account") == account and manifest.get("environment") == "dev"
            and isinstance(manifest.get("paths"), list) and manifest["paths"]
            and all(isinstance(path, str) for path in manifest["paths"])):
        raise ValueError("Coordinator: supply the approved private release source, target and changed paths")
    workflows = selected_workflows(manifest["paths"])
    components = manifest.get("components")
    if not isinstance(components, dict) or set(components) != set(workflows):
        raise ValueError("Coordinator: supply receipts for every selected release component")
    for name, workflow in workflows.items():
        component = components[name]
        if not isinstance(component, dict):
            raise ValueError("Coordinator: release component receipt is missing")
        revision = component.get("source_revision")
        expected = component.get("expected_runtime")
        receipt = component.get("receipt")
        if not (isinstance(revision, str) and SOURCE.fullmatch(revision)
                and isinstance(expected, dict) and expected
                and all(isinstance(key, str) and isinstance(value, str) and DIGEST.fullmatch(value)
                        for key, value in expected.items())
                and isinstance(receipt, dict) and receipt.get("component") == name
                and receipt.get("workflow") == workflow and receipt.get("mode") == "apply"
                and receipt.get("source_revision") == revision
                and receipt.get("environment") == "dev" and receipt.get("account") == account
                and receipt.get("evidence_type") == "independent-runtime"
                and type(receipt.get("run_id")) is int and receipt["run_id"] > 0
                and type(receipt.get("run_attempt")) is int and receipt["run_attempt"] > 0):
            raise ValueError(f"Coordinator: verify the applied {name} source/run/attempt and independent receipt")
        if name == "shared Lambda and Browser":
            images = receipt.get("images", {})
            if (set(expected) != {"lambda", "browser"}
                    or not all(isinstance(images.get(target), dict)
                               and isinstance(images[target].get("built"), str)
                               and images[target]["built"].endswith("@" + expected[target])
                               and images[target].get("runtime") == images[target]["built"]
                               for target in expected)
                    or not re.fullmatch(r"[0-9a-f]{64}", receipt.get("state_sha256", ""))
                    or not isinstance(receipt.get("config"), dict)
                    or not all(re.fullmatch(r"[0-9a-f]{64}", receipt["config"].get(field, ""))
                               for field in ("backend_sha256", "tfvars_sha256", "state_identity_sha256"))):
                raise ValueError("Coordinator: shared runtime lacks independently read source/config/state evidence")
        elif receipt.get("component_digests") != expected or receipt.get("runtime_digests") != expected:
            raise ValueError(f"Coordinator: {name} runtime differs from its released image or target digest")
    for name, image in (("gateway", gateway_image), ("facade and reconciler", facade_image)):
        if components[name]["expected_runtime"].get("image") != image.rsplit("@", 1)[-1]:
            raise ValueError(f"Coordinator: {name} pinned image differs from deployed evaluation input")
    return {**{name: components[name]["expected_runtime"] for name in workflows}, **route_group(manifest)}


def verify_window(manifest, readback, *, source, account, gateway_image, facade_image):
    expected = verify_manifest(manifest, source=source, account=account,
                               gateway_image=gateway_image, facade_image=facade_image)
    for phase in ("before", "after"):
        observed = readback(phase)
        if observed != expected:
            raise ValueError(f"Coordinator: {phase} independent runtime readback changed or omitted a selected component")
    return {"source_revision": source, "account": account, "components": sorted(manifest["components"]),
            "receipts": {name: {"source_revision": value["source_revision"],
                                "run_id": value["receipt"]["run_id"],
                                "run_attempt": value["receipt"]["run_attempt"]}
                         for name, value in manifest["components"].items()},
            "evidence_type": "independent-runtime"}
