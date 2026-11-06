#!/usr/bin/env python3
"""Render a validated Deployment/HPA/PDB bundle from the chosen HTTP profile."""

import argparse
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import re
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[2] / "modules/gateway/k8s"
PROFILES = ("default", "llm-proxy")


def memory_bytes(value):
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)(Ki|Mi|Gi|Ti|Pi|Ei|k|M|G|T|P|E)?", str(value))
    if not match:
        raise ValueError("Unsupported gateway memory quantity")
    units = {"": 1, **{unit + "i": 1024 ** power for power, unit in enumerate("KMGTPE", 1)},
             **{unit: 1000 ** power for power, unit in enumerate("kMGTPE", 1)}}
    return Decimal(match[1]) * units[match[2] or ""]


def default_memory_headroom(resources):
    # Maintained deployment control also applies to a pinned older source.
    # Live four-worker pods reached 1.9Gi before the pricing exec OOMed at 2Gi.
    # Preserve larger operator settings; never lower requests or limits.
    requested = memory_bytes(resources["requests"]["memory"])
    limited = memory_bytes(resources["limits"]["memory"])
    if requested <= 0 or limited < requested:
        raise ValueError("Gateway memory request must be positive and fit its limit")
    for kind, floor in (("requests", "2Gi"), ("limits", "3Gi")):
        if memory_bytes(resources[kind]["memory"]) < memory_bytes(floor):
            resources[kind]["memory"] = floor


def render(arguments):
    result = subprocess.run(["kubectl", *arguments], capture_output=True, text=True, check=False)
    if result.returncode:
        raise ValueError("Cannot render gateway manifests locally; no cluster mutation attempted")
    return json.loads(result.stdout)


def checked_bundle(manifest, profile, budget, root=ROOT, admission=None):
    if profile not in PROFILES:
        raise ValueError("capacity profile must be default or llm-proxy")
    base = render(["patch", "--local", "--type", "merge", "-p", "{}", "-f", str(manifest), "-o", "json"])
    if profile == "llm-proxy":
        deployment = render(["patch", "--local", "-f", str(manifest), "--type", "strategic",
                             "--patch-file", str(root / "profiles/llm-proxy/deployment-patch.yaml"), "-o", "json"])
        manifests = root / "profiles/llm-proxy"
    else:
        deployment = base
        manifests = root
    hpa = render(["patch", "--local", "--type", "merge", "-p", "{}", "-f", str(manifests / "hpa.yaml"), "-o", "json"])
    pdb = render(["patch", "--local", "--type", "merge", "-p", "{}", "-f", str(manifests / "pdb.yaml"), "-o", "json"])
    if (deployment.get("kind") != "Deployment" or hpa.get("kind") != "HorizontalPodAutoscaler"
            or pdb.get("kind") != "PodDisruptionBudget"
            or any(item.get("metadata", {}).get("namespace") != "adp-gateway" for item in (deployment, hpa, pdb))
            or hpa["spec"].get("minReplicas") != 2 or hpa["spec"]["scaleTargetRef"].get("name") != "bedrockgateway"
            or pdb["spec"]["selector"]["matchLabels"].get("app") != "bedrockgateway"):
        raise ValueError("Capacity bundle does not target the two-pod gateway baseline")
    cooldown = hpa["spec"].get("behavior", {}).get("scaleDown", {})
    policies = cooldown.get("policies", [])
    window = cooldown.get("stabilizationWindowSeconds")
    if (not isinstance(window, int) or not 0 < window <= 3600
            or cooldown.get("selectPolicy", "Max") not in ("Max", "Min")
            or not isinstance(policies, list) or not policies
            or any(not isinstance(policy, dict)
                   or policy.get("type") not in ("Pods", "Percent")
                   or not isinstance(policy.get("value"), int) or policy["value"] < 1
                   or not isinstance(policy.get("periodSeconds"), int)
                   or not 1 <= policy["periodSeconds"] <= 1800 for policy in policies)
            or pdb["spec"].get("maxUnavailable") != 1
            or deployment["spec"].get("strategy", {}).get("rollingUpdate", {}).get("maxUnavailable") != 0):
        raise ValueError("Capacity bundle needs a finite HPA cooldown and replica-safe disruption policy")
    base_container = next(item for item in base["spec"]["template"]["spec"]["containers"] if item["name"] == "bedrockgateway")
    container = next(item for item in deployment["spec"]["template"]["spec"]["containers"] if item["name"] == "bedrockgateway")
    resources = container.get("resources", {})
    if (not isinstance(resources, dict)
            or any(not isinstance(resources.get(kind), dict) or any(not resources[kind].get(name) for name in ("cpu", "memory"))
                   for kind in ("requests", "limits"))):
        raise ValueError("Capacity bundle needs explicit CPU and memory requests and limits")
    if profile == "default":
        default_memory_headroom(resources)
    if (base_container["image"] != container["image"] or base_container.get("securityContext") != container.get("securityContext")
            or base["spec"]["template"]["spec"].get("serviceAccountName") != deployment["spec"]["template"]["spec"].get("serviceAccountName")
            or any(base_container.get(probe) != container.get(probe) for probe in ("readinessProbe", "startupProbe", "livenessProbe"))):
        raise ValueError("Capacity profile cannot change the image, identity, security or probes")
    env = {item["name"]: item.get("value") for item in container.get("env", [])}
    args = container.get("args", [])
    # New defaults declare one worker explicitly. An older pinned manifest
    # without args still inherits its released four-worker image command.
    workers = int(args[args.index("--workers") + 1]) if "--workers" in args else 4
    if env.get("BG_RDS_SPLIT_POOL_ENABLED") != "false":
        raise ValueError("Split DB pools require separate fleet qualification")
    per_pod = workers * (int(env["BG_RDS_POOL_SIZE"]) + int(env["BG_RDS_POOL_MAX_OVERFLOW"]))
    surge = deployment["spec"].get("strategy", {}).get("rollingUpdate", {}).get("maxSurge", 0)
    replicas = hpa["spec"]["maxReplicas"]
    surge = math.ceil(replicas * int(surge[:-1]) / 100) if isinstance(surge, str) and surge.endswith("%") else int(surge)
    if (workers < 1 or per_pod < 1 or replicas < 2 or surge < 0
            or (profile == "default" and per_pod != 12) or (profile == "llm-proxy" and per_pod != 20)):
        raise ValueError("Capacity bundle has an unreviewed worker, pool or replica ceiling")
    if profile == "llm-proxy" and any(value is None for value in budget):
        raise ValueError("Selecting llm-proxy requires DB maximum, reserved, other and terminating connection budget inputs")
    if any(value is not None for value in budget):
        if any(value is None or value < 0 for value in budget):
            raise ValueError("Capacity budget inputs must all be non-negative integers")
        maximum, reserved, other, terminating = budget
        required = per_pod * (replicas + surge + terminating) + reserved + other
        if maximum <= 0 or required > maximum:
            raise ValueError("HTTP fleet plus reserved and other DB connections exceed the configured DB ceiling")
    names = ("BG_MAX_ACTIVE_STREAMS_PER_WORKER", "BG_STREAM_ADMISSION_QUEUE_LIMIT",
             "BG_STREAM_ADMISSION_WAIT_MS")
    container["env"] = [item for item in container["env"] if item["name"] not in names]
    if admission is not None:
        container["env"].extend({"name": name, "value": str(value)} for name, value in zip(names, admission))
    annotations = deployment.setdefault("metadata", {}).setdefault("annotations", {})
    annotations["adp.aws-e.example/capacity-profile"] = profile
    annotations["adp.aws-e.example/capacity-selection"] = selection_digest(profile, budget, admission)
    return {"deployment": deployment, "hpa": hpa, "pdb": pdb}


def selection_digest(profile, budget, admission=None):
    return hashlib.sha256(json.dumps([profile, budget, admission], separators=(",", ":")).encode()).hexdigest()


def validate_admission(values):
    if all(value is None for value in values):
        return None
    if any(value is None or not re.fullmatch(r"[0-9]+", str(value)) for value in values):
        raise ValueError("Inference admission needs complete numeric worker, queue and wait limits")
    active, queue, wait_ms = map(int, values)
    if not (1 <= active <= 100000 and 0 <= queue <= 10000 and 1 <= wait_ms <= 5000):
        raise ValueError("Inference admission limits exceed finite safety bounds")
    return active, queue, wait_ms


def selected_admission(environment):
    keys = ("capacity-max-active-streams-per-worker", "capacity-stream-admission-queue-limit",
            "capacity-stream-admission-wait-ms")
    return validate_admission([selected_parameter(environment, key) for key in keys])


def verify_selection(environment, bundle_dir):
    document = json.loads((bundle_dir / "deployment.json").read_text())
    annotations = document["metadata"]["annotations"]
    profile, budget = selected_capacity(environment)
    admission = selected_admission(environment)
    if (annotations.get("adp.aws-e.example/capacity-profile") != profile
            or annotations.get("adp.aws-e.example/capacity-selection") != selection_digest(profile, budget, admission)):
        raise ValueError("Capacity selection changed during apply; refuse remaining mutations")


def selected_fields_equal(expected, observed):
    if isinstance(expected, dict):
        return isinstance(observed, dict) and all(
            key in observed and selected_fields_equal(value, observed[key]) for key, value in expected.items())
    if isinstance(expected, list):
        return isinstance(observed, list) and len(expected) == len(observed) and all(
            selected_fields_equal(before, after) for before, after in zip(expected, observed))
    if isinstance(expected, str) and isinstance(observed, str):
        units = {"": 1, "n": Decimal("0.000000001"), "u": Decimal("0.000001"),
                 "m": Decimal("0.001"), "Ki": 1024, "Mi": 1024 ** 2,
                 "Gi": 1024 ** 3, "Ti": 1024 ** 4}
        pattern = r"([0-9]+(?:\.[0-9]+)?)(n|u|m|Ki|Mi|Gi|Ti)?"
        before, after = re.fullmatch(pattern, expected), re.fullmatch(pattern, observed)
        if before and after:
            try:
                return (Decimal(before[1]) * units[before[2] or ""]
                        == Decimal(after[1]) * units[after[2] or ""])
            except InvalidOperation:
                pass
    return expected == observed


def verify_applied(environment, bundle_dir, deployment_only=False):
    verify_selection(environment, bundle_dir)
    expected = {name: json.loads((bundle_dir / (name + ".json")).read_text())
                for name in ("deployment", "hpa", "pdb")}
    observed = {}
    for name, resource in (("deployment", "deployment/bedrockgateway"),
                           ("hpa", "hpa/bedrockgateway"), ("pdb", "pdb/bedrockgateway-pdb")):
        if deployment_only and name != "deployment":
            continue
        result = subprocess.run(["kubectl", "--request-timeout=30s", "-n", "adp-gateway",
                                 "get", resource, "-o", "json"],
                                text=True, capture_output=True, check=False)
        if result.returncode:
            raise ValueError("Capacity bundle readback failed; rollout is incomplete")
        observed[name] = json.loads(result.stdout)
    expected_deployment = expected["deployment"]
    deployed = observed["deployment"]
    annotations = expected_deployment["metadata"]["annotations"]
    if any(deployed.get("metadata", {}).get("annotations", {}).get(name) != annotations[name]
           for name in ("adp.aws-e.example/capacity-profile", "adp.aws-e.example/capacity-selection")):
        raise ValueError("Gateway capacity selection differs from deployed bundle")
    desired_container = next(item for item in expected_deployment["spec"]["template"]["spec"]["containers"]
                             if item["name"] == "bedrockgateway")
    current_container = next(item for item in deployed["spec"]["template"]["spec"]["containers"]
                             if item["name"] == "bedrockgateway")
    selected_env = ("BG_RDS_POOL_ENABLED", "BG_RDS_POOL_SIZE", "BG_RDS_POOL_MAX_OVERFLOW",
                    "BG_RDS_SPLIT_POOL_ENABLED", "BG_MAX_ACTIVE_STREAMS_PER_WORKER",
                    "BG_STREAM_ADMISSION_QUEUE_LIMIT", "BG_STREAM_ADMISSION_WAIT_MS")
    env = lambda container: {item["name"]: item.get("value") for item in container["env"] if item["name"] in selected_env}
    if (any(not selected_fields_equal(desired_container.get(name), current_container.get(name))
            for name in ("image", "command", "args", "resources", "readinessProbe", "startupProbe", "livenessProbe", "securityContext"))
            or env(current_container) != env(desired_container)):
        raise ValueError("Gateway pod configuration differs from selected capacity bundle")
    if (not selected_fields_equal(expected_deployment["spec"].get("strategy"), deployed["spec"].get("strategy"))
            or expected_deployment["spec"]["template"]["spec"].get("terminationGracePeriodSeconds")
            != deployed["spec"]["template"]["spec"].get("terminationGracePeriodSeconds")):
        raise ValueError("Gateway rollout strategy or drain period differs from selected capacity bundle")
    if deployment_only:
        return
    target_hpa = expected["hpa"]["spec"]
    live_hpa = observed["hpa"]["spec"]
    if any(not selected_fields_equal(target_hpa.get(name), live_hpa.get(name))
           for name in ("minReplicas", "maxReplicas", "scaleTargetRef", "metrics", "behavior")):
        raise ValueError("Gateway HPA differs from selected capacity bundle")
    if (observed["pdb"]["spec"].get("maxUnavailable") != expected["pdb"]["spec"].get("maxUnavailable")
            or observed["pdb"]["spec"].get("selector") != expected["pdb"]["spec"].get("selector")):
        raise ValueError("Gateway PDB differs from selected capacity bundle")


def selected_parameter(environment, key):
    name = f"/adp/{environment}/gateway/{key}"
    result = subprocess.run(["aws", "ssm", "get-parameter", "--name", name,
                             "--query", "Parameter.Value", "--output", "text"],
                            capture_output=True, text=True, check=False)
    if result.returncode:
        if "ParameterNotFound" in result.stderr:
            return None
        raise ValueError("Capacity selection cannot be read; refusing to reset the selected profile")
    return result.stdout.strip()


def selected_capacity(environment):
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", environment):
        raise ValueError("Invalid capacity environment")
    profile = selected_parameter(environment, "capacity-profile") or "default"
    if profile not in PROFILES:
        raise ValueError("Unknown capacity profile; refusing to change the gateway")
    budget = (None,) * 4
    if profile == "llm-proxy":
        keys = ("capacity-db-max", "capacity-db-reserved", "capacity-db-other", "capacity-terminating-pods")
        values = [selected_parameter(environment, key) for key in keys]
        if any(value is None or not re.fullmatch(r"[0-9]+", value) for value in values):
            raise ValueError("Selected profile needs a complete numeric DB and terminating-pod budget")
        budget = tuple(int(value) for value in values)
    return profile, budget


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--verify-bundle", type=Path)
    parser.add_argument("--verify-applied", type=Path)
    parser.add_argument("--verify-deployment", type=Path)
    parser.add_argument("--max-active", type=int)
    parser.add_argument("--queue-limit", type=int)
    parser.add_argument("--wait-ms", type=int)
    parser.add_argument("--root", type=Path, default=ROOT)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--profile")
    selection.add_argument("--environment")
    parser.add_argument("--output-dir", type=Path)
    for name in ("db-max", "db-reserved", "db-other", "terminating-pods"):
        parser.add_argument("--" + name, type=int)
    arguments = parser.parse_args()
    try:
        verification_paths = (arguments.verify_bundle, arguments.verify_applied, arguments.verify_deployment)
        if any(verification_paths):
            if (not arguments.environment or arguments.manifest or arguments.output_dir
                    or sum(path is not None for path in verification_paths) != 1):
                raise ValueError("Bundle verification needs only an environment and one bundle path")
            if arguments.verify_bundle:
                verify_selection(arguments.environment, arguments.verify_bundle)
            elif arguments.verify_deployment:
                verify_applied(arguments.environment, arguments.verify_deployment, deployment_only=True)
            else:
                verify_applied(arguments.environment, arguments.verify_applied)
            return
        if not arguments.manifest or not arguments.output_dir:
            raise ValueError("Capacity rendering needs a manifest and output directory")
        profile, budget = (selected_capacity(arguments.environment) if arguments.environment else
                           (arguments.profile, (arguments.db_max, arguments.db_reserved, arguments.db_other,
                                                arguments.terminating_pods)))
        admission = selected_admission(arguments.environment) if arguments.environment else validate_admission(
            (arguments.max_active, arguments.queue_limit, arguments.wait_ms))
        bundle = checked_bundle(arguments.manifest, profile, budget, arguments.root, admission)
        if not arguments.output_dir.is_dir() or any((arguments.output_dir / (name + ".json")).exists() for name in bundle):
            raise ValueError("Capacity output directory must exist and contain no previous bundle")
        for name, document in bundle.items():
            with (arguments.output_dir / (name + ".json")).open("x") as output:
                json.dump(document, output)
    except (ValueError, KeyError, IndexError, TypeError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
