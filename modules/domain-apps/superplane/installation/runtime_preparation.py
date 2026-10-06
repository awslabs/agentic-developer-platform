"""Closed review contract for a separate, disabled domain runtime installation."""

from __future__ import annotations

import re

from .api_adapters import closed
from .config import EKS_NAME, IDENTIFIER, digest, identity, require


def names(env):
    prefix = f"adp-{env['environment']}-superplane-domain"
    return {
        "queue": prefix + "-operations",
        "worker_role": prefix + "-worker",
        "observer_role": prefix + "-observer",
    }


def validate_request(request):
    closed(request, {"account_id", "region", "environment", "cluster", "namespace", "operator_role_arn"}, "runtime preparation request")
    require(
        isinstance(request["account_id"], str)
        and re.fullmatch(r"[0-9]{12}", request["account_id"])
        and isinstance(request["region"], str)
        and re.fullmatch(r"[a-z]{2}-[a-z]+-[1-9][0-9]*", request["region"])
        and isinstance(request["environment"], str)
        and IDENTIFIER.fullmatch(request["environment"])
        and len(request["environment"]) <= 31
        and isinstance(request["cluster"], str)
        and EKS_NAME.fullmatch(request["cluster"])
        and isinstance(request["namespace"], str)
        and IDENTIFIER.fullmatch(request["namespace"]),
        "Runtime preparation requires exact account, region, environment, cluster and namespace",
    )
    require(
        isinstance(request["operator_role_arn"], str)
        and re.fullmatch(rf"arn:aws:iam::{request['account_id']}:role/[A-Za-z0-9+=,.@_-]{{1,64}}", request["operator_role_arn"]),
        "Runtime preparation requires an explicit same-account operator role",
    )


def review(request, inspector):
    """Read target identities and inventory; never reserve a name or activate a worker."""
    validate_request(request)
    account, region = request["account_id"], request["region"]
    target = inspector.json(inspector.aws("sts", "get-caller-identity"))
    role = request["operator_role_arn"].split(":role/", 1)[1]
    require(
        target.get("Account") == account
        and target.get("Arn", "").startswith(f"arn:aws:sts::{account}:assumed-role/{role}/"),
        "Runtime preparation operator account or role differs from the reviewed target",
    )
    cluster = inspector.json(inspector.aws("eks", "describe-cluster", "--name", request["cluster"]))["cluster"]
    require(
        cluster.get("arn") == f"arn:aws:eks:{region}:{account}:cluster/{request['cluster']}"
        and cluster.get("status") == "ACTIVE"
        and re.fullmatch(rf"https://oidc\.eks\.{re.escape(region)}\.amazonaws\.com/id/[A-Za-z0-9]+", cluster.get("identity", {}).get("oidc", {}).get("issuer", "")),
        "Runtime preparation requires the selected active cluster and OIDC identity",
    )
    resource_names = names(request)
    queue_urls = inspector.json(inspector.aws("sqs", "list-queues", "--queue-name-prefix", resource_names["queue"])).get("QueueUrls", [])
    require(
        isinstance(queue_urls, list) and not queue_urls,
        "Runtime preparation queue name already exists or inventory is ambiguous; do not adopt it",
    )
    roles = inspector.json(inspector.aws("iam", "list-roles"))["Roles"]
    require(
        isinstance(roles, list)
        and all(isinstance(existing, dict) and existing.get("RoleName") not in (resource_names["worker_role"], resource_names["observer_role"]) for existing in roles),
        "Runtime preparation role name already exists or inventory is ambiguous; do not adopt it",
    )
    resources = {
        "queue_name": resource_names["queue"],
        "queue_arn": f"arn:aws:sqs:{region}:{account}:{resource_names['queue']}",
        "queue_url": f"https://sqs.{region}.amazonaws.com/{account}/{resource_names['queue']}",
        "worker_role_arn": f"arn:aws:iam::{account}:role/{resource_names['worker_role']}",
        "observer_role_arn": f"arn:aws:iam::{account}:role/{resource_names['observer_role']}",
    }
    return {
        "version": 1,
        "status": "review-only",
        "installation_id": identity(request),
        "request_sha256": digest(request),
        "account_id": account,
        "region": region,
        "cluster_arn": cluster["arn"],
        "oidc_issuer": cluster["identity"]["oidc"]["issuer"],
        "resources": resources,
        "worker_ready": False,
    }


def compose(request, reviewed, env, lock, operator):
    """Render an inert owner proposal; only owner-operated paths can install it."""
    from urllib.parse import urlsplit

    from . import paid_worker
    from .config import https_origin

    validate_request(request)
    closed(
        operator,
        {
            "review_id", "keda_operator_role_arn", "producer_registry_id", "worker_registry_id",
            "database_secret_id", "observation_credential_secret_id", "observation_url",
            "repo", "policy_configmap", "state_claim", "worker",
        },
        "runtime preparation operator references",
    )
    require(
        isinstance(operator["review_id"], str)
        and re.fullmatch(r"[A-Za-z0-9._-]{1,128}", operator["review_id"]),
        "An explicit operator review reference is required; it is not approval proof",
    )
    require(
        all(request[key] == env.get(key) for key in ("account_id", "region", "environment", "cluster", "namespace"))
        and reviewed.get("version") == 1
        and reviewed.get("status") == "review-only"
        and reviewed.get("installation_id") == identity(request)
        and reviewed.get("request_sha256") == digest(request)
        and reviewed.get("account_id") == request["account_id"]
        and reviewed.get("region") == request["region"]
        and reviewed.get("cluster_arn") == f"arn:aws:eks:{request['region']}:{request['account_id']}:cluster/{request['cluster']}"
        and reviewed.get("worker_ready") is False,
        "Runtime review and selected installation do not match",
    )
    resources = reviewed.get("resources")
    expected_names = names(request)
    require(
        isinstance(resources, dict)
        and resources == {
            "queue_name": expected_names["queue"],
            "queue_arn": f"arn:aws:sqs:{request['region']}:{request['account_id']}:{expected_names['queue']}",
            "queue_url": f"https://sqs.{request['region']}.amazonaws.com/{request['account_id']}/{expected_names['queue']}",
            "worker_role_arn": f"arn:aws:iam::{request['account_id']}:role/{expected_names['worker_role']}",
            "observer_role_arn": f"arn:aws:iam::{request['account_id']}:role/{expected_names['observer_role']}",
        },
        "Runtime review resource identity changed",
    )
    require(
        isinstance(env.get("api_adapters"), dict)
        and isinstance(env["api_adapters"].get("vault"), dict)
        and isinstance(env["api_adapters"]["vault"].get("secret_key_ref"), dict)
        and isinstance(env["api_adapters"].get("verification"), dict)
        and isinstance(env["api_adapters"].get("dispatcher"), dict),
        "Runtime preparation needs the selected existing vault, verification and dispatcher services",
    )
    dispatcher = env["api_adapters"]["dispatcher"]
    require(
        dispatcher.get("role_arn") == f"arn:aws:iam::{request['account_id']}:role/adp-{request['environment']}-superplane-api-producer"
        and isinstance(dispatcher.get("api_id"), str)
        and re.fullmatch(r"[a-z0-9]{10}", dispatcher["api_id"])
        and isinstance(dispatcher.get("stage"), str)
        and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", dispatcher["stage"])
        and dispatcher.get("region") == request["region"]
        and dispatcher.get("endpoint") == f"https://{dispatcher['api_id']}.execute-api.{request['region']}.amazonaws.com/{dispatcher['stage']}",
        "Runtime preparation needs the selected managed producer API target",
    )
    require(
        isinstance(operator["keda_operator_role_arn"], str)
        and re.fullmatch(rf"arn:aws:iam::{request['account_id']}:role/[A-Za-z0-9+=,.@_-]{{1,64}}", operator["keda_operator_role_arn"])
        and operator["keda_operator_role_arn"] not in (dispatcher["role_arn"], resources["worker_role_arn"], resources["observer_role_arn"]),
        "Runtime preparation needs an existing separate same-account KEDA operator identity",
    )
    require(
        all(isinstance(operator[key], str) and re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", operator[key]) for key in ("producer_registry_id", "worker_registry_id"))
        and operator["producer_registry_id"] != operator["worker_registry_id"],
        "Runtime preparation needs distinct owner-provided registry identifiers",
    )
    for key in ("database_secret_id", "observation_credential_secret_id"):
        require(
            isinstance(operator[key], str)
            and re.fullmatch(rf"arn:aws:secretsmanager:{request['region']}:{request['account_id']}:secret:[A-Za-z0-9/_+=.@-]{{1,512}}", operator[key]),
            "Runtime preparation needs exact selected same-account secret identifiers",
        )
    require(operator["database_secret_id"] != operator["observation_credential_secret_id"], "Runtime preparation secret purposes must be separate")
    require(
        isinstance(operator["observation_url"], str)
        and urlsplit(operator["observation_url"]).scheme == "https"
        and urlsplit(operator["observation_url"]).hostname
        and not urlsplit(operator["observation_url"]).username
        and not urlsplit(operator["observation_url"]).password
        and not urlsplit(operator["observation_url"]).query
        and not urlsplit(operator["observation_url"]).fragment
        and isinstance(operator["repo"], str)
        and re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", operator["repo"]),
        "Runtime preparation needs an exact protected repository and observation HTTPS endpoint",
    )
    require(
        operator["policy_configmap"] == "superplane-lifecycle-policy"
        and operator["state_claim"] == "superplane-lifecycle-state",
        "Runtime preparation needs the existing immutable policy and durable state references",
    )
    worker = operator["worker"]
    closed(
        worker,
        {
            "database_secret", "workspace_credentials_secret", "provider_secret",
            "operation_schema", "skypilot_url", "management_api_server", "node_selector",
            "egress", "max_replica_count", "active_deadline_seconds",
        },
        "runtime preparation worker input",
    )
    require(isinstance(env.get("database"), dict) and worker["operation_schema"] == env["database"].get("schema"), "Runtime preparation needs the existing isolated domain schema")
    require(https_origin(worker["management_api_server"]), "Runtime preparation needs the selected HTTPS management API")
    selected = dict(worker, mode="native-controller", namespace=request["namespace"], role_arn=resources["worker_role_arn"], queue_observer_role_arn=resources["observer_role_arn"], queue_url=resources["queue_url"], queue_arn=resources["queue_arn"])
    prepared_env = dict(env, paid_worker=selected)
    paid_worker.validate(prepared_env, lock)
    require(
        isinstance(env.get("org_id"), str) and re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", env["org_id"])
        and isinstance(env.get("adp_org_id"), str) and 1 <= len(env["adp_org_id"]) <= 255,
        "Runtime preparation needs the selected protected domain and ADP organization mapping",
    )
    binding = {
        "domain": "superplane",
        "org_id": env["org_id"],
        "adp_org_id": env["adp_org_id"],
        "producer_registry_id": operator["producer_registry_id"],
        "worker_registry_id": operator["worker_registry_id"],
        "database_secret_id": operator["database_secret_id"],
        "database_schema": selected["operation_schema"],
        "queue_url": resources["queue_url"],
        "worker_namespace": request["namespace"],
        "worker_service_account": paid_worker.WORKER,
        "worker_container": "paid-worker",
        "worker_image_digests": [lock["images"][paid_worker.COMPONENT]],
        "repo": operator["repo"],
        "observation_url": operator["observation_url"],
        "observation_credential_secret_id": operator["observation_credential_secret_id"],
    }
    return {
        "status": "requires-shared-owner-review",
        "worker_ready": False,
        "binding_attested": False,
        "authority_verified": False,
        "shared_services_verified": False,
        "domain_schema_installed": False,
        "secret_projections_verified": False,
        "policy_immutable_verified": False,
        "state_durable_verified": False,
        "review_id": operator["review_id"],
        "installation_id": reviewed["installation_id"],
        "terraform_variables": {
            "account_id": request["account_id"],
            "region": request["region"],
            "environment": request["environment"],
            "cluster_name": request["cluster"],
            "namespace": request["namespace"],
            "oidc_issuer": reviewed["oidc_issuer"],
            "installation_id": reviewed["installation_id"],
            "api_id": dispatcher["api_id"],
            "api_stage": dispatcher["stage"],
            "keda_operator_role_arn": operator["keda_operator_role_arn"],
        },
        "paid_worker": selected,
        "gateway_binding_proposal": binding,
        "registry_proposals": [
            {"agent_id": operator["producer_registry_id"], "org_id": env["adp_org_id"], "scope": "internal", "role_arn": dispatcher["role_arn"], "credential_scopes": ["domain:operation-producer"]},
            {"agent_id": operator["worker_registry_id"], "org_id": env["adp_org_id"], "scope": "internal", "role_arn": resources["worker_role_arn"], "credential_scopes": ["domain:operation-executor", "domain:operation-recovery"]},
        ],
        "schema_owner": "modules/harness/jobs/harness_jobs/schema.py",
        "runtime_database_role": f"superplane_{request['environment']}_runtime",
        "secret_projection_names": {
            "database": selected["database_secret"],
            "workspace": selected["workspace_credentials_secret"],
            "provider": selected["provider_secret"],
        },
        "policy_configmap": operator["policy_configmap"],
        "state_claim": operator["state_claim"],
    }
