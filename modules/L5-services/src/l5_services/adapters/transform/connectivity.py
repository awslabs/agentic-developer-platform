"""Read-only installed-worker connectivity checks, never executor qualification."""

import httpx


ACTIONS = {
    "cluster_api_timeout": "Check the worker's private subnet routes, DNS and security-group path to the EKS API, then repeat this probe.",
    "cluster_api_unreachable": "Check the installed EKS endpoint and CA, private DNS and worker-to-cluster TCP 443 connectivity.",
    "cluster_identity_rejected": "Verify the deployed worker IAM role and its EKS access entry; do not use operator credentials for this probe.",
    "cluster_read_forbidden": "Restore the worker's scoped RBAC for the configured node and each namespace's named deny-all policy.",
    "cluster_api_rejected": "Inspect EKS API health and the worker's scoped access, then repeat this read-only probe.",
    "cluster_response_invalid": "Inspect the installed cluster target and its bounded API response before retrying.",
    "cluster_probe_failed": "Inspect the deployed worker's AWS credential configuration and EKS transport, then repeat this read-only probe.",
    "executor_node_missing": "Observe current candidate capacity and requalify a replacement node before updating the protected node pin.",
    "executor_node_changed": "The configured node identity changed; observe and independently qualify the replacement before updating the protected pin.",
    "executor_node_not_ready": "Wait for the selected node to be Ready without a deletion timestamp; requalify any replacement.",
    "executor_namespaces_overlap": "Configure distinct provider and check namespaces through the reviewed executor bootstrap and reinstall the worker.",
    "deny_policy_missing": "Restore the named deny-all policy through the reviewed executor bootstrap before continuing.",
    "deny_policy_changed": "Restore deny-all ingress and egress for every Pod in this namespace through the reviewed executor bootstrap.",
}


def probe_connectivity(api, *, node_name, node_uid, provider_namespace, check_namespace):
    """At most three bounded GETs using the runtime's existing EKS transport."""
    report = {"schema_version": 1, "outcome": "blocked", "checks": {},
              "qualified": False, "native_calls": False, "live_acceptance": "not_evaluated"}

    def blocked(stage, code):
        report["diagnostic"] = {"stage": stage, "code": code, "next_action": ACTIONS[code]}
        return report

    if provider_namespace == check_namespace:
        return blocked("configuration", "executor_namespaces_overlap")
    targets = [
        ("node", "/api/v1/nodes/" + node_name),
        ("provider_policy", f"/apis/networking.k8s.io/v1/namespaces/{provider_namespace}/networkpolicies/deny-all"),
        ("check_policy", f"/apis/networking.k8s.io/v1/namespaces/{check_namespace}/networkpolicies/deny-all"),
    ]
    for stage, path in targets:
        try:
            value = api.request("GET", path, maximum=65536, timeout_seconds=2)
        except httpx.TimeoutException:
            return blocked(stage, "cluster_api_timeout")
        except httpx.HTTPStatusError as exc:
            code = {401: "cluster_identity_rejected", 403: "cluster_read_forbidden"}.get(
                exc.response.status_code, "cluster_api_rejected")
            return blocked(stage, code)
        except httpx.RequestError:
            return blocked(stage, "cluster_api_unreachable")
        except ValueError:
            return blocked(stage, "cluster_response_invalid")
        except Exception:
            # Provider exceptions and URLs can contain private target/session data.
            return blocked(stage, "cluster_probe_failed")
        if value is None:
            return blocked(stage, "executor_node_missing" if stage == "node" else "deny_policy_missing")
        if not isinstance(value, dict) or not isinstance(value.get("metadata"), dict):
            return blocked(stage, "cluster_response_invalid")
        metadata = value["metadata"]
        if stage == "node":
            if metadata.get("name") != node_name or metadata.get("uid") != node_uid:
                return blocked(stage, "executor_node_changed")
            status = value.get("status")
            conditions = status.get("conditions") if isinstance(status, dict) else None
            if (metadata.get("deletionTimestamp") or not isinstance(conditions, list)
                    or not any(isinstance(c, dict) and c.get("type") == "Ready" and c.get("status") == "True"
                               for c in conditions)):
                return blocked(stage, "executor_node_not_ready")
        else:
            namespace = provider_namespace if stage == "provider_policy" else check_namespace
            spec = value.get("spec")
            if (metadata.get("name") != "deny-all" or metadata.get("namespace") != namespace
                    or metadata.get("deletionTimestamp")
                    or not isinstance(spec, dict) or spec.get("podSelector") != {}
                    or not isinstance(spec.get("policyTypes"), list) or len(spec["policyTypes"]) != 2
                    or sorted(spec["policyTypes"], key=str) != ["Egress", "Ingress"]
                    or spec.get("ingress") or spec.get("egress")):
                return blocked(stage, "deny_policy_changed")
        report["checks"][stage] = "verified"
    report["outcome"] = "passed"
    return report
