#!/usr/bin/env python3
"""Refuse a controller upgrade that would delete live ARC scale sets.

ARC's controller deletes AutoscalingRunnerSets from a different major/minor
release. A Terraform plan can show only an in-place Helm update while causing
that deletion. Coordinated migrations must pause the controller and upgrade
all scale-set charts/CRDs before resuming it; this gate never mutates resources.
"""
import argparse
import json
from pathlib import Path
import re
import subprocess
import tempfile


def release_line(version):
    match = re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.[0-9]+(?:[-+].*)?", version or "")
    if not match:
        raise ValueError("ARC release version is missing or malformed")
    return match.group(1), match.group(2)


def controller_versions(plan):
    versions = []
    for resource in plan.get("resource_changes", []):
        change = resource.get("change", {})
        after = change.get("after") or {}
        if resource.get("type") == "helm_release" and after.get("chart") == "gha-runner-scale-set-controller" and change.get("actions") not in (["no-op"], ["read"]):
            version = after.get("version")
            release_line(version)
            versions.append(version)
    return versions


def verify(plan, scale_sets):
    for version in controller_versions(plan):
        for resource in scale_sets:
            actual = resource.get("metadata", {}).get("labels", {}).get("app.kubernetes.io/version")
            if release_line(actual) != release_line(version):
                name = resource["metadata"]["name"]
                raise ValueError(f"ARC {version} would delete scale set {name} labelled {actual}; complete the coordinated chart/CRD migration first")


def resources(module):
    yield from module.get("resources", [])
    for child in module.get("child_modules", []):
        yield from resources(child)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True, type=Path)
    args = parser.parse_args()
    plan = json.loads(args.plan.read_text())
    if not controller_versions(plan):
        print("No ARC controller upgrade in this plan")
        return
    # Targeted plans omit unchanged data sources from planned_values. Terraform's
    # refreshed prior_state retains the discovery values used by those providers.
    previous = plan.get("prior_state", {}).get("values", {}).get("root_module", {})
    by_address = {r["address"]: r for r in resources(previous)}
    by_address.update({r["address"]: r for r in resources(plan["planned_values"]["root_module"])})
    values = list(by_address.values())
    platform = next(r["values"]["outputs"] for r in values if r["address"] == "data.terraform_remote_state.platform")
    account = next(r["values"]["account_id"] for r in values if r["address"] == "data.aws_caller_identity.current")
    observed = json.loads(subprocess.check_output(["aws", "sts", "get-caller-identity"], text=True))
    if observed["Account"] != account:
        raise ValueError("AWS identity differs from the saved Terraform plan")
    with tempfile.TemporaryDirectory(prefix="arc-upgrade-") as directory:
        config = str(Path(directory) / "kubeconfig")
        subprocess.run(["aws", "eks", "update-kubeconfig", "--name", platform["eks_cluster_name"], "--kubeconfig", config], check=True, stdout=subprocess.DEVNULL)
        base = ["kubectl", "--kubeconfig", config, "--request-timeout=30s"]
        available = subprocess.check_output(base + ["api-resources", "--api-group=actions.github.com", "-o", "name"], text=True).splitlines()
        live = []
        if "autoscalingrunnersets.actions.github.com" in available:
            live = json.loads(subprocess.check_output(base + ["get", "autoscalingrunnersets.actions.github.com", "-A", "-o", "json"], text=True))["items"]
        verify(plan, live)
        print(f"ARC release compatibility verified against {len(live)} live scale sets")


if __name__ == "__main__":
    main()
