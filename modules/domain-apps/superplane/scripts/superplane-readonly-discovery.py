"""Read-only fixed-namespace discovery using the runner's own Kubernetes identity."""

import argparse
import json
import ssl
import time
import urllib.error
import urllib.request
from pathlib import Path

allowed = {
    "AWS_REGION",
    "SUPERPLANE_RELEASE_ID",
    "SUPERPLANE_SOURCE_REVISION",
    "SUPERPLANE_SECURITY_PROFILE",
    "DOMAIN_AUTH_ENFORCED",
    "COGNITO_ENABLED",
    "COGNITO_ISSUER",
    "COGNITO_JWKS_URL",
    "DOMAIN_AUTH_ALLOWED_CLIENT_IDS",
    "CORS_ORIGINS",
    "SUPERPLANE_DB_SCHEMA",
    "SUPERPLANE_MANAGEMENT_ONLY",
    "SUPERPLANE_INSTALLATION_REQUIRED",
    "SUPERPLANE_ORG_ID",
    "SUPERPLANE_ADP_ORG_ID",
    "ADP_ORG_ID",
    "ORG_ID",
    "SKYPILOT_DB_SCHEMA",
    "CONTROL_PLANE_API_URL",
    "CONTROLLER_STATUS_URL",
    "OBSERVATION_API_URL",
}


def metadata(x):
    m = x["metadata"]
    return {
        k: m[k]
        for k in ("name", "namespace", "uid", "resourceVersion", "labels")
        if k in m
    }


def containers(spec):
    out = []
    for c in spec.get("containers", []):
        env = []
        for e in c.get("env", []):
            if e["name"] in allowed and "value" in e:
                env.append(e)
            elif "valueFrom" in e:
                env.append({"name": e["name"], "valueFrom": e["valueFrom"]})
        out.append(
            {
                "name": c["name"],
                "image": c["image"],
                "env": env,
                "envFrom": c.get("envFrom", []),
            }
        )
    return out


def run(output):
    result = {
        "mode": "read-only",
        "identity": "runner-in-cluster-serviceaccount",
        "resources": [],
        "errors": [],
    }
    try:
        directory = Path("/var/run/secrets/kubernetes.io/serviceaccount")
        namespace = (directory / "namespace").read_text().strip()
        if namespace != "arc-runners":
            raise ValueError("Unexpected runner namespace")
        token = (directory / "token").read_text().strip()
        context = ssl.create_default_context(cafile=str(directory / "ca.crt"))
        deadline = time.monotonic() + 90

        # Redirects must not forward the service-account credential elsewhere.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None

        opener = urllib.request.build_opener(
            NoRedirect(), urllib.request.HTTPSHandler(context=context)
        )
        for ns in ("superplane", "skypilot"):
            for kind, prefix in (
                ("deployments", "/apis/apps/v1"),
                ("jobs", "/apis/batch/v1"),
                ("services", "/api/v1"),
                ("serviceaccounts", "/api/v1"),
                ("networkpolicies", "/apis/networking.k8s.io/v1"),
            ):
                path = prefix + "/namespaces/" + ns + "/" + kind
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Discovery deadline")
                request = urllib.request.Request(
                    "https://kubernetes.default.svc" + path,
                    headers={"Authorization": "Bearer " + token},
                    method="GET",
                )
                try:
                    with opener.open(request, timeout=min(10, remaining)) as response:
                        raw = response.read(2 * 1024 * 1024 + 1)
                    if len(raw) > 2 * 1024 * 1024:
                        raise ValueError("Resource listing too large")
                    for item in json.loads(raw)["items"]:
                        result["resources"].append(sanitize(kind, item))
                except urllib.error.HTTPError as error:
                    result["errors"].append(
                        {"namespace": ns, "kind": kind, "status": error.code}
                    )
    except Exception as error:
        # Report only a type, never credential-bearing request/response material.
        result["errors"].append({"type": type(error).__name__})
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2) + "\n")
    print(
        json.dumps(
            {
                "resource_count": len(result["resources"]),
                "error_count": len(result["errors"]),
            }
        )
    )
    return 1 if result["errors"] else 0


def sanitize(kind, item):
    result = {"kind": kind, "metadata": metadata(item)}
    if kind in {"deployments", "jobs"}:
        spec = item["spec"]["template"]["spec"]
        result["containers"] = containers(spec)
        result["service_account"] = spec.get("serviceAccountName")
        result["volumes"] = [
            {k: v for k, v in value.items() if k in {"name", "secret", "configMap"}}
            for value in spec.get("volumes", [])
        ]
        result["status"] = {
            k: v
            for k, v in item.get("status", {}).items()
            if k
            in {
                "replicas",
                "readyReplicas",
                "availableReplicas",
                "active",
                "succeeded",
                "failed",
            }
        }
    elif kind == "services":
        result["ports"] = item["spec"].get("ports", [])
    elif kind == "serviceaccounts":
        result["role_arn"] = (
            item["metadata"].get("annotations", {}).get("eks.amazonaws.com/role-arn")
        )
    elif kind == "networkpolicies":
        result["spec"] = item["spec"]
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    raise SystemExit(run(parser.parse_args().output))
