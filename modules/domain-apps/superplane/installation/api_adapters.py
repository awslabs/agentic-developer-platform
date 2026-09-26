"""Explicit existing API transports. No keys, IAM, registry or binding creation."""

import re
from urllib.parse import urlsplit

from .config import require


def closed(value, keys, name):
    require(
        isinstance(value, dict) and set(value) == set(keys),
        f"{name} requires exactly its documented fields",
    )


def name(value):
    return isinstance(value, str) and re.fullmatch(
        r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", value
    )


def validate(env):
    if "api_adapters" not in env:
        return
    adapters = env["api_adapters"]
    closed(adapters, {"vault", "dispatcher", "verification"}, "api_adapters")
    vault, dispatcher, control = (
        adapters[k] for k in ("vault", "dispatcher", "verification")
    )
    closed(vault, {"url", "secret_key_ref", "transport"}, "api_adapters.vault")
    ref, transport = vault["secret_key_ref"], vault["transport"]
    closed(ref, {"name", "key"}, "vault.secret_key_ref")
    require(
        name(ref["name"])
        and isinstance(ref["key"], str)
        and re.fullmatch(r"[A-Za-z0-9._-]{1,253}", ref["key"]),
        "vault requires an existing Secret name and key",
    )
    closed(
        transport,
        {"namespace", "service", "port", "target_port", "selector", "security"},
        "vault.transport",
    )
    require(
        name(transport["namespace"]) and name(transport["service"]),
        "vault transport must name an exact Service and namespace",
    )
    for field in ("port", "target_port"):
        require(
            type(transport[field]) is int and 1 <= transport[field] <= 65535,
            "vault transport ports must be explicit TCP ports",
        )
    # First supported boundary is the reviewed cluster-only Gateway Service.
    # TLS termination at an ALB/custom name needs its own transport verifier.
    require(
        transport["security"] == "reviewed-cluster-http",
        "vault transport requires explicit reviewed-cluster-http boundary",
    )
    require(
        vault["url"]
        == f"http://{transport['service']}.{transport['namespace']}.svc.cluster.local:{transport['port']}",
        "vault URL must exactly match the selected internal Service",
    )
    selector = transport["selector"]
    require(
        isinstance(selector, dict) and 1 <= len(selector) <= 8,
        "vault Service selector is required",
    )
    require(
        all(
            isinstance(k, str)
            and re.fullmatch(r"(?:[a-z0-9.-]+/)?[A-Za-z0-9][A-Za-z0-9_.-]{0,62}", k)
            and isinstance(v, str)
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}", v)
            for k, v in selector.items()
        ),
        "vault Service selector is invalid",
    )
    closed(
        dispatcher,
        {"endpoint", "region", "role_arn", "api_id", "stage"},
        "api_adapters.dispatcher",
    )
    require(
        isinstance(dispatcher["api_id"], str)
        and re.fullmatch(r"[a-z0-9]{10}", dispatcher["api_id"]),
        "dispatcher api_id is invalid",
    )
    require(
        isinstance(dispatcher["stage"], str)
        and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", dispatcher["stage"]),
        "dispatcher stage is invalid",
    )
    require(
        dispatcher["region"] == env.get("region"),
        "dispatcher signing region must match the selected region",
    )
    expected = f"https://{dispatcher['api_id']}.execute-api.{dispatcher['region']}.amazonaws.com/{dispatcher['stage']}"
    require(
        dispatcher["endpoint"] == expected,
        "dispatcher endpoint must match the exact invoke API, region and stage",
    )
    # No query, credentials, fragment, alternate port, suffix or normalization.
    require(urlsplit(expected).scheme == "https", "dispatcher endpoint must use TLS")
    require(
        isinstance(dispatcher["role_arn"], str)
        and re.fullmatch(
            r"arn:aws:iam::"
            + re.escape(str(env.get("account_id")))
            + r":role/[A-Za-z0-9+=,.@_/-]{1,512}",
            dispatcher["role_arn"],
        ),
        "dispatcher requires an existing role in the target account",
    )
    closed(
        control,
        {"workspace_id", "connection_id", "credential_id", "service", "label"},
        "api_adapters.verification",
    )
    import uuid

    for field in ("workspace_id", "connection_id"):
        try:
            canonical = str(uuid.UUID(control[field]))
        except (ValueError, TypeError, AttributeError):
            canonical = None
        require(
            canonical is not None and canonical == control[field],
            "adapter verification must name existing canonical workspace and connection IDs",
        )
    for field in ("credential_id", "service", "label"):
        require(
            isinstance(control[field], str)
            and 0 < len(control[field]) <= 255
            and re.fullmatch(r"[A-Za-z0-9 _.:/@+-]+", control[field])
            and not control[field].startswith("arn:"),
            "adapter verification requires bounded existing credential metadata",
        )


def project(env, docs, *, active=False):
    """Mutate only API resources; no Secret document or other workload role."""
    if "api_adapters" not in env:
        return
    validate(env)
    adapters = env["api_adapters"]
    vault, producer = adapters["vault"], adapters["dispatcher"]
    values = {
        "ADP_GATEWAY_INTERNAL_URL": vault["url"],
        "SUPERPLANE_OPERATION_GATEWAY_URL": producer["endpoint"],
        "SUPERPLANE_OPERATION_GATEWAY_REGION": producer["region"],
        "SUPERPLANE_OPERATION_DISPATCH_ENABLED": "true" if active else "false",
        "SUPERPLANE_MANAGEMENT_ONLY": "false" if active else "true",
        "AWS_EC2_METADATA_DISABLED": "true",
        "AWS_STS_REGIONAL_ENDPOINTS": "regional",
    }
    for doc in docs:
        if (
            doc["metadata"]["name"] != "superplane-api"
            or doc["metadata"].get("namespace") != env["namespace"]
        ):
            continue
        if doc["kind"] == "ServiceAccount":
            doc["metadata"].setdefault("annotations", {})[
                "eks.amazonaws.com/role-arn"
            ] = producer["role_arn"]
        if doc["kind"] == "Deployment":
            container = doc["spec"]["template"]["spec"]["containers"][0]
            container["env"] = [
                v
                for v in container["env"]
                if v["name"] not in {*values, "ADP_GATEWAY_INTERNAL_API_KEY"}
            ]
            container["env"].extend({"name": k, "value": v} for k, v in values.items())
            container["env"].append(
                {
                    "name": "ADP_GATEWAY_INTERNAL_API_KEY",
                    "valueFrom": {
                        "secretKeyRef": {**vault["secret_key_ref"], "optional": False}
                    },
                }
            )
        if doc["kind"] == "NetworkPolicy":
            transport = vault["transport"]
            rule = {
                "to": [
                    {
                        "namespaceSelector": {
                            "matchLabels": {
                                "kubernetes.io/metadata.name": transport["namespace"]
                            }
                        },
                        "podSelector": {"matchLabels": transport["selector"]},
                    }
                ],
                "ports": [
                    {"protocol": "TCP", "port": p}
                    for p in sorted({transport["port"], transport["target_port"]})
                ],
            }
            if rule not in doc["spec"]["egress"]:
                doc["spec"]["egress"].append(rule)


def verify_role(env, role, documents, oidc):
    """Closed dedicated API producer role; effective access still needs live proof."""
    producer = env["api_adapters"]["dispatcher"]
    require(
        role.get("Arn") == producer["role_arn"], "API producer role identity changed"
    )
    issuer = oidc.removeprefix("https://")
    statements = role.get("AssumeRolePolicyDocument", {}).get("Statement", [])
    require(
        isinstance(statements, list) and len(statements) == 1,
        "API role must have exactly one dedicated workload trust",
    )
    statement = statements[0]
    require(
        statement.get("Effect") == "Allow"
        and statement.get("Action") == "sts:AssumeRoleWithWebIdentity"
        and statement.get("Principal")
        == {"Federated": f"arn:aws:iam::{env['account_id']}:oidc-provider/{issuer}"}
        and statement.get("Condition")
        == {
            "StringEquals": {
                issuer + ":aud": "sts.amazonaws.com",
                issuer
                + ":sub": f"system:serviceaccount:{env['namespace']}:superplane-api",
            }
        },
        "API role trust must name the exact management OIDC, API ServiceAccount and audience",
    )
    prefix = f"arn:aws:execute-api:{producer['region']}:{env['account_id']}:{producer['api_id']}/{producer['stage']}/POST/internal/v1/controller-execution/"
    allowed = {
        prefix + route for route in ("producer-readiness", "verify-run", "dispatch")
    }
    observed = set()
    for document in documents:
        statements = document.get("Statement", [])
        if isinstance(statements, dict):
            statements = [statements]
        require(isinstance(statements, list), "API role policy statement is invalid")
        for statement in statements:
            if statement.get("Effect") == "Deny":
                continue
            action = statement.get("Action")
            resources = statement.get("Resource")
            if isinstance(resources, str):
                resources = [resources]
            require(
                statement.get("Effect") == "Allow"
                and action in ("execute-api:Invoke", ["execute-api:Invoke"])
                and isinstance(resources, list)
                and bool(resources)
                and all(isinstance(r, str) for r in resources)
                and set(resources) <= allowed
                and not {"NotAction", "NotResource", "Principal", "NotPrincipal"}
                & set(statement),
                "API producer role grants authority beyond the selected producer routes",
            )
            observed.update(resources)
    require(observed == allowed, "API producer role lacks required producer routes")


def image_contract_valid(report):
    return (
        isinstance(report, dict)
        and report.get("image_contract_version") == 1
        and report.get("configuration_verified") is False
        and report.get("authority_verified") is False
        and report.get("production_ready") is False
        and set(report.get("required_ports", []))
        == {
            "credential_evidence",
            "provider_authority",
            "allocation_inventory",
            "operation_facade",
        }
        and "capabilities" not in report
    )
