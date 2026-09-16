#!/usr/bin/env python3
"""Check collector egress before activating EKS network-policy enforcement."""
import json
import subprocess
import sys


def kubectl(*args):
    return subprocess.check_output(["kubectl", *args, "-o", "json"], text=True)


def collector_allowed(policy):
    spec = policy.get("spec", {})
    if spec.get("podSelector") != {"matchLabels": {"app.kubernetes.io/name": "adot-collector"}}:
        return False
    ports = set()
    for rule in spec.get("egress", []):
        if rule.get("to"):
            continue
        for port in rule.get("ports", []):
            ports.add((port.get("protocol", "TCP"), port.get("port")))
    return "Egress" in spec.get("policyTypes", []) and {("TCP", 53), ("UDP", 53), ("TCP", 443)} <= ports


def main():
    if sys.argv[1] == "enabled":
        raw = kubectl("get", "configmap", "amazon-vpc-cni", "-n", "kube-system", "--ignore-not-found")
        print(str(bool(raw.strip()) and json.loads(raw).get("data", {}).get("enable-network-policy-controller") == "true").lower())
        return
    namespaces = json.loads(kubectl("get", "namespaces"))["items"]
    if not any(ns["metadata"]["name"] == "adp-agents" for ns in namespaces):
        return
    policies = json.loads(kubectl("get", "networkpolicies", "-n", "adp-agents"))["items"]
    if not any(p["metadata"]["name"] == "default-deny-egress" for p in policies):
        return
    if not any(collector_allowed(p) for p in policies):
        sys.exit("Collector DNS/HTTPS egress policy is missing or incompatible; upgrade webhook-ingress before enabling enforcement")
    print("Collector DNS and HTTPS egress audited before network-policy activation")


if __name__ == "__main__":
    main()
