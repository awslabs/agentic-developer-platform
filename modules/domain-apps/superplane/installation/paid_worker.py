"""Native paid-worker source preparation; shared binding activation is unavailable.

The default installer plan renders an inert projection. No caller-authored
receipt can turn it into authority, and preflight refuses before external tools.
"""

import copy
import ipaddress
import re

import yaml

from .api_adapters import closed, name
from .config import MODULE, SCHEMA, digest, https_origin, image, require

COMPONENT = "superplane-paid-worker"
WORKER = "superplane-paid-worker"
UNAVAILABLE = "paid-worker-binding-attestation-unavailable"


def validate(env, lock):
    if "paid_worker" not in env:
        return
    config = env["paid_worker"]
    closed(
        config,
        {
            "mode",
            "namespace",
            "role_arn",
            "queue_observer_role_arn",
            "queue_url",
            "queue_arn",
            "database_secret",
            "workspace_credentials_secret",
            "provider_secret",
            "operation_schema",
            "skypilot_url",
            "management_api_server",
            "node_selector",
            "egress",
            "max_replica_count",
            "active_deadline_seconds",
        },
        "paid_worker",
    )
    require(
        config["mode"] == "native-controller",
        "paid_worker supports native-controller only",
    )
    require(
        config["namespace"] == env.get("namespace"),
        "paid_worker must use the owned management namespace",
    )
    require(bool(env.get("api_adapters")), "paid_worker requires staged API adapters")
    account, region = str(env.get("account_id")), str(env.get("region"))
    roles = [config[k] for k in ("role_arn", "queue_observer_role_arn")]
    require(
        all(
            isinstance(role, str)
            and re.fullmatch(
                r"arn:aws:iam::"
                + re.escape(account)
                + r":role/[A-Za-z0-9+=,.@_-]{1,64}",
                role,
            )
            for role in roles
        )
        and len(set(roles)) == 2,
        "paid_worker requires distinct existing same-account worker and queue-observer roles",
    )
    other_roles = {env["api_adapters"]["dispatcher"]["role_arn"]}
    other_roles.update(
        value
        for key, value in env.get("execution", {}).items()
        if key.endswith("role_arn")
    )
    require(
        not set(roles) & other_roles,
        "paid_worker identities must be separate from API and controller identities",
    )
    queue = config["queue_url"]
    match = (
        re.fullmatch(
            r"https://sqs\."
            + re.escape(region)
            + r"\.amazonaws\.com/"
            + re.escape(account)
            + r"/([A-Za-z0-9_-]{1,80})",
            queue,
        )
        if isinstance(queue, str)
        else None
    )
    require(
        match and config["queue_arn"] == f"arn:aws:sqs:{region}:{account}:{match[1]}",
        "paid_worker queue URL/ARN must identify one standard queue in the selected account and region",
    )
    for key in ("database_secret", "workspace_credentials_secret", "provider_secret"):
        require(name(config[key]), "paid_worker requires exact existing Secret names")
    require(
        len(
            {
                config[k]
                for k in (
                    "database_secret",
                    "workspace_credentials_secret",
                    "provider_secret",
                )
            }
        )
        == 3,
        "paid_worker Secret purposes must be separate",
    )
    require(
        config["workspace_credentials_secret"] != "superplane-workspace-access",
        "paid_worker cannot use read-only manager credentials",
    )
    schema = config["operation_schema"]
    require(
        isinstance(schema, str)
        and SCHEMA.fullmatch(schema)
        and schema != "public"
        and schema == env.get("database", {}).get("schema"),
        "paid_worker operation schema must match the dedicated domain schema",
    )
    require(
        config["skypilot_url"]
        == f"http://skypilot-api.{env.get('skypilot_namespace')}.svc.cluster.local:46580",
        "paid_worker SkyPilot URL must match the selected private service",
    )
    require(
        https_origin(config["management_api_server"]),
        "paid_worker management API must be an exact HTTPS origin",
    )
    selector = config["node_selector"]
    require(
        isinstance(selector, dict)
        and selector
        and all(
            isinstance(k, str)
            and re.fullmatch(r"(?:[a-z0-9.-]+/)?[A-Za-z0-9][A-Za-z0-9_.-]{0,62}", k)
            and isinstance(v, str)
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}", v)
            for k, v in selector.items()
        ),
        "paid_worker scheduling must select explicit reviewed nodes",
    )
    for key, maximum in (("max_replica_count", 4), ("active_deadline_seconds", 3600)):
        require(
            type(config[key]) is int and 1 <= config[key] <= maximum,
            "paid_worker concurrency and deadline must be bounded",
        )
    closed(
        config["egress"],
        {"gateway", "sts", "database", "skypilot", "workspace", "management"},
        "paid_worker.egress",
    )
    for endpoint in config["egress"].values():
        closed(endpoint, {"cidr", "port"}, "paid_worker egress endpoint")
        try:
            network = ipaddress.ip_network(endpoint["cidr"], strict=True)
        except (ValueError, TypeError):
            require(False, "paid_worker egress requires exact host CIDRs")
        require(
            network.prefixlen == network.max_prefixlen
            and not (
                network.network_address.is_unspecified
                or network.network_address.is_multicast
                or network.network_address.is_loopback
            ),
            "paid_worker egress requires exact routable host CIDRs",
        )
        require(
            type(endpoint["port"]) is int and 1 <= endpoint["port"] <= 65535,
            "paid_worker egress port is invalid",
        )
    if lock is None:
        return
    source = lock.get("image_sources", {}).get(COMPONENT, {})
    require(
        COMPONENT not in lock.get("pending_images", {})
        and re.fullmatch(
            r"sha256:[a-f0-9]{64}", str(lock.get("images", {}).get(COMPONENT, ""))
        )
        and lock["images"][COMPONENT] != "sha256:" + "0" * 64,
        "paid_worker requires a separately resolved immutable paid-worker image",
    )
    require(
        source.get("registry") == f"{account}.dkr.ecr.{region}.amazonaws.com"
        and source.get("repository") == "adp-" + COMPONENT
        and source.get("source_revision") == lock.get("source_revision")
        and re.fullmatch(r"[a-f0-9]{40}", str(source.get("source_revision", ""))),
        "paid_worker image must come from the exact reviewed release source and dedicated repository",
    )
    require(
        lock["images"][COMPONENT] != lock["images"].get("superplane-executor"),
        "paid_worker cannot reuse the controller-service digest",
    )


def project(env, lock, docs):
    if not env.get("paid_worker"):
        return
    config = env["paid_worker"]
    labels = copy.deepcopy(docs[0]["metadata"]["labels"])
    labels["app.kubernetes.io/name"] = WORKER

    def obj(kind, suffix, **fields):
        return {
            "apiVersion": "v1",
            "kind": kind,
            "metadata": {
                "name": WORKER + suffix,
                "namespace": env["namespace"],
                "labels": dict(labels),
            },
            **fields,
        }

    scaled = yaml.safe_load((MODULE / "executor/deploy/paid-worker.yaml").read_text())
    scaled["metadata"] = obj("ScaledJob", "")["metadata"]
    scaled["metadata"]["annotations"] = {
        "autoscaling.keda.sh/paused": "true",
        "adp.aws-e.io/preparation-only": UNAVAILABLE,
    }
    spec = scaled["spec"]
    spec["maxReplicaCount"] = 0
    spec["jobTargetRef"]["activeDeadlineSeconds"] = config["active_deadline_seconds"]
    template = spec["jobTargetRef"]["template"]
    template["metadata"]["labels"] = dict(labels)
    pod = template["spec"]
    pod["nodeSelector"] = dict(config["node_selector"])
    pod["initContainers"][0]["image"] = image(lock, "superplane-controller")
    worker = pod["containers"][0]
    worker["image"] = image(lock, COMPONENT)
    worker["env"] = [
        value
        for value in worker["env"]
        if not value["name"].startswith("SUPERPLANE_LIFECYCLE_")
    ]
    worker["env"].extend(
        {"name": key, "value": value}
        for key, value in {
            "SUPERPLANE_PAID_WORKER_MODE": "native-controller",
            "AWS_EC2_METADATA_DISABLED": "true",
            "AWS_STS_REGIONAL_ENDPOINTS": "regional",
        }.items()
    )
    worker["volumeMounts"] = [
        value
        for value in worker["volumeMounts"]
        if value["name"] not in {"state", "policy"}
    ]
    pod["volumes"] = [
        value for value in pod["volumes"] if value["name"] not in {"state", "policy"}
    ]
    for volume in pod["volumes"]:
        key = {
            "database": "database_secret",
            "workspaces": "workspace_credentials_secret",
            "provider": "provider_secret",
        }.get(volume["name"])
        if key:
            volume["secret"]["secretName"] = config[key]
        if volume["name"] == "database":
            volume["secret"]["items"] = [
                {"key": key, "path": key}
                for key in ("domain-dsn", "execution-dsn", "ca.pem")
            ]
        if volume["name"] == "provider":
            volume["secret"]["items"] = [
                {"key": "skypilot-token", "path": "skypilot-token"}
            ]
        if "emptyDir" in volume:
            volume["emptyDir"] = {"medium": "Memory", "sizeLimit": "64Mi"}
    spec["triggers"][0]["metadata"].update(
        queueURL=config["queue_url"], awsRegion=env["region"]
    )
    spec["triggers"][0]["authenticationRef"]["name"] = WORKER + "-queue-observer"
    service_account = obj("ServiceAccount", "", automountServiceAccountToken=False)
    service_account["metadata"]["annotations"] = {
        "eks.amazonaws.com/role-arn": config["role_arn"]
    }
    authentication = obj(
        "TriggerAuthentication",
        "-queue-observer",
        spec={
            "podIdentity": {
                "provider": "aws",
                "roleArn": config["queue_observer_role_arn"],
            }
        },
    )
    authentication["apiVersion"] = "keda.sh/v1alpha1"
    configmap = obj(
        "ConfigMap",
        "-config",
        data={
            "AWS_REGION": env["region"],
            "ADP_EXECUTION_AUTHORITY_ENDPOINT": env["api_adapters"]["dispatcher"][
                "endpoint"
            ],
            "SUPERPLANE_OPERATION_SCHEMA": config["operation_schema"],
            "SKYPILOT_URL": config["skypilot_url"],
            "SUPERPLANE_MANAGEMENT_API_SERVER": config["management_api_server"],
        },
    )
    # Intent is reviewable but does not grant packets before live target checks.
    policy = obj(
        "NetworkPolicy",
        "-preparation-deny",
        spec={
            "podSelector": {"matchLabels": {"app.kubernetes.io/name": WORKER}},
            "policyTypes": ["Ingress", "Egress"],
            "ingress": [],
            "egress": [],
        },
    )
    policy["apiVersion"] = "networking.k8s.io/v1"
    docs.extend([service_account, configmap, authentication, policy, scaled])


def preparation_report(env, lock):
    return {
        "version": 1,
        "mode": "native-controller",
        "state": "source-preparation-only",
        "configuration_sha256": digest(env["paid_worker"]),
        "paid_worker_image": image(lock, COMPONENT),
        "activation_available": False,
        "gate": UNAVAILABLE,
        "live_identity_verified": False,
        "live_schema_verified": False,
        "live_network_verified": False,
        "shared_binding_verified": False,
    }


def require_activation_available(env):
    if env.get("paid_worker"):
        require(
            False,
            UNAVAILABLE
            + ": authenticated non-consuming shared binding read contract is not available; use the default offline plan for source preparation",
        )
