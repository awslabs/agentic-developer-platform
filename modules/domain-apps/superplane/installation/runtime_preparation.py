"""Closed review contract for a separate, disabled domain runtime installation."""

from __future__ import annotations

import re
import hashlib
import os
import stat
import uuid

from .api_adapters import closed
from .config import (
    EKS_NAME,
    IDENTIFIER,
    SCHEMA,
    deployment_identity,
    digest,
    identity,
    require,
)


def names(env):
    prefix = f"adp-{env['environment']}-superplane-domain"
    return {
        "queue": prefix + "-operations",
        "worker_role": prefix + "-worker",
        "observer_role": prefix + "-observer",
    }


def validate_request(request):
    closed(
        request,
        {
            "account_id",
            "region",
            "environment",
            "cluster",
            "namespace",
            "operator_role_arn",
        },
        "runtime preparation request",
    )
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
        and re.fullmatch(
            rf"arn:aws:iam::{request['account_id']}:role/(?:[A-Za-z0-9+=,.@_-]+/)*[A-Za-z0-9+=,.@_-]{{1,64}}",
            request["operator_role_arn"],
        ),
        "Runtime preparation requires an explicit same-account operator role",
    )


def selected_operator(request, env):
    selected = deployment_identity(env, required=True)
    require(
        request["account_id"] == env.get("account_id")
        and request["operator_role_arn"] == selected["expected_role_arn"],
        "Runtime operator differs from the selected connection identity",
    )
    return dict(selected)


def verify_operator(request, selected, commands):
    """Check the credentials used for Terraform, not just the read-only inspector."""
    import json

    from .config import Refusal

    def observed(*args):
        result = commands.call(
            [
                "aws",
                "--region",
                request["region"],
                "--no-cli-pager",
                *args,
                "--output",
                "json",
            ],
            timeout=120,
        )
        try:
            value = json.loads(result.stdout)
        except (TypeError, ValueError):
            raise Refusal("Runtime operator identity response is invalid") from None
        require(
            isinstance(value, dict), "Runtime operator identity response is invalid"
        )
        return value

    target = observed("sts", "get-caller-identity")
    role_name = selected["expected_role_arn"].rsplit("/", 1)[1]
    prefix = f"arn:aws:sts::{request['account_id']}:assumed-role/{role_name}/"
    arn = target.get("Arn", "")
    require(
        target.get("Account") == request["account_id"]
        and isinstance(arn, str)
        and arn.startswith(prefix)
        and bool(arn[len(prefix) :])
        and "/" not in arn[len(prefix) :]
        and target.get("UserId")
        == selected["expected_role_id"] + ":" + arn[len(prefix) :],
        "Runtime operator session differs from the selected connection identity",
    )
    role = observed("iam", "get-role", "--role-name", role_name).get("Role")
    require(
        isinstance(role, dict)
        and role.get("Arn") == selected["expected_role_arn"]
        and role.get("RoleId") == selected["expected_role_id"],
        "Runtime operator role differs from the selected immutable identity",
    )


def review(request, inspector, *, existing=False):
    """Read target identities and inventory; never reserve a name or activate a worker."""
    validate_request(request)
    account, region = request["account_id"], request["region"]
    target = inspector.json(inspector.aws("sts", "get-caller-identity"))
    role = request["operator_role_arn"].rsplit("/", 1)[1]
    require(
        target.get("Account") == account
        and target.get("Arn", "").startswith(
            f"arn:aws:sts::{account}:assumed-role/{role}/"
        ),
        "Runtime preparation operator account or role differs from the reviewed target",
    )
    cluster = inspector.json(
        inspector.aws("eks", "describe-cluster", "--name", request["cluster"])
    )["cluster"]
    require(
        cluster.get("arn")
        == f"arn:aws:eks:{region}:{account}:cluster/{request['cluster']}"
        and cluster.get("status") == "ACTIVE"
        and re.fullmatch(
            rf"https://oidc\.eks\.{re.escape(region)}\.amazonaws\.com/id/[A-Za-z0-9]+",
            cluster.get("identity", {}).get("oidc", {}).get("issuer", ""),
        ),
        "Runtime preparation requires the selected active cluster and OIDC identity",
    )
    resource_names = names(request)
    queue_urls = inspector.json(
        inspector.aws(
            "sqs", "list-queues", "--queue-name-prefix", resource_names["queue"]
        )
    ).get("QueueUrls", [])
    require(
        isinstance(queue_urls, list)
        and (
            queue_urls
            == [
                f"https://sqs.{region}.amazonaws.com/{account}/{resource_names['queue']}"
            ]
            if existing
            else not queue_urls
        ),
        "Runtime preparation queue name already exists or inventory is ambiguous; do not adopt it",
    )
    roles = inspector.json(inspector.aws("iam", "list-roles"))["Roles"]
    require(
        isinstance(roles, list)
        and all(
            isinstance(role, dict) and isinstance(role.get("RoleName"), str)
            for role in roles
        ),
        "Runtime preparation role inventory is ambiguous",
    )
    selected_roles = [
        role["RoleName"]
        for role in roles
        if role["RoleName"]
        in (resource_names["worker_role"], resource_names["observer_role"])
    ]
    require(
        sorted(selected_roles)
        == sorted([resource_names["worker_role"], resource_names["observer_role"]])
        if existing
        else not selected_roles,
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
    selected_identity = selected_operator(request, env)
    closed(
        operator,
        {
            "review_id",
            "keda_operator_role_arn",
            "producer_registry_id",
            "worker_registry_id",
            "database_secret_id",
            "domain_database_secret_id",
            "domain_database_schema",
            "observation_credential_secret_id",
            "observation_url",
            "repo",
            "policy_configmap",
            "state_claim",
            "worker",
        },
        "runtime preparation operator references",
    )
    require(
        isinstance(operator["review_id"], str)
        and re.fullmatch(r"[A-Za-z0-9._-]{1,128}", operator["review_id"]),
        "An explicit operator review reference is required; it is not approval proof",
    )
    require(
        all(
            request[key] == env.get(key)
            for key in ("account_id", "region", "environment", "cluster", "namespace")
        )
        and reviewed.get("version") == 1
        and reviewed.get("status") == "review-only"
        and reviewed.get("installation_id") == identity(request)
        and reviewed.get("request_sha256") == digest(request)
        and reviewed.get("account_id") == request["account_id"]
        and reviewed.get("region") == request["region"]
        and reviewed.get("cluster_arn")
        == f"arn:aws:eks:{request['region']}:{request['account_id']}:cluster/{request['cluster']}"
        and reviewed.get("worker_ready") is False,
        "Runtime review and selected installation do not match",
    )
    resources = reviewed.get("resources")
    expected_names = names(request)
    require(
        isinstance(resources, dict)
        and resources
        == {
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
        dispatcher.get("role_arn")
        == f"arn:aws:iam::{request['account_id']}:role/adp-{request['environment']}-superplane-api-producer"
        and isinstance(dispatcher.get("api_id"), str)
        and re.fullmatch(r"[a-z0-9]{10}", dispatcher["api_id"])
        and isinstance(dispatcher.get("stage"), str)
        and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", dispatcher["stage"])
        and dispatcher.get("region") == request["region"]
        and dispatcher.get("endpoint")
        == f"https://{dispatcher['api_id']}.execute-api.{request['region']}.amazonaws.com/{dispatcher['stage']}",
        "Runtime preparation needs the selected managed producer API target",
    )
    require(
        isinstance(operator["keda_operator_role_arn"], str)
        and re.fullmatch(
            rf"arn:aws:iam::{request['account_id']}:role/[A-Za-z0-9+=,.@_-]{{1,64}}",
            operator["keda_operator_role_arn"],
        )
        and operator["keda_operator_role_arn"]
        not in (
            dispatcher["role_arn"],
            resources["worker_role_arn"],
            resources["observer_role_arn"],
        ),
        "Runtime preparation needs an existing separate same-account KEDA operator identity",
    )
    require(
        all(
            isinstance(operator[key], str)
            and re.fullmatch(
                r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
                operator[key],
            )
            for key in ("producer_registry_id", "worker_registry_id")
        )
        and operator["producer_registry_id"] != operator["worker_registry_id"]
        and operator["producer_registry_id"]
        == str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                "adp:domain-operation-registration:v1:" + dispatcher["role_arn"],
            )
        )
        and operator["worker_registry_id"]
        == str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                "adp:domain-operation-registration:v1:" + resources["worker_role_arn"],
            )
        ),
        "Runtime preparation requires the protected owner's deterministic registry identities",
    )
    secret_fields = (
        "database_secret_id",
        "domain_database_secret_id",
        "observation_credential_secret_id",
    )
    for key in secret_fields:
        require(
            isinstance(operator[key], str)
            and re.fullmatch(
                rf"arn:aws:secretsmanager:{request['region']}:{request['account_id']}:secret:[A-Za-z0-9/_+=.@-]{{1,512}}",
                operator[key],
            ),
            "Runtime preparation needs exact selected same-account secret identifiers",
        )
    require(
        len({operator[key] for key in secret_fields}) == 3,
        "Runtime preparation Harness, domain and observation secret purposes must be separate",
    )
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
            "mode",
            "lifecycle_policy_configmap",
            "lifecycle_state_claim",
            "lifecycle_policy_sha256",
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
        "runtime preparation worker input",
    )
    require(
        worker["mode"] == "native-lifecycle"
        and worker["lifecycle_policy_configmap"] == operator["policy_configmap"]
        and worker["lifecycle_state_claim"] == operator["state_claim"]
        and isinstance(worker["lifecycle_policy_sha256"], str)
        and re.fullmatch(r"[0-9a-f]{64}", worker["lifecycle_policy_sha256"]),
        "Runtime preparation requires the reviewed native lifecycle policy and state references",
    )
    require(
        isinstance(env.get("database"), dict)
        and isinstance(operator["domain_database_schema"], str)
        and SCHEMA.fullmatch(operator["domain_database_schema"])
        and operator["domain_database_schema"] != "public"
        and operator["domain_database_schema"] == env["database"].get("schema")
        and isinstance(worker["operation_schema"], str)
        and SCHEMA.fullmatch(worker["operation_schema"])
        and worker["operation_schema"] != "public"
        and worker["operation_schema"] != operator["domain_database_schema"],
        "Runtime preparation requires separate Harness operation and existing domain schemas",
    )
    require(
        https_origin(worker["management_api_server"]),
        "Runtime preparation needs the selected HTTPS management API",
    )
    selected = dict(
        worker,
        namespace=request["namespace"],
        role_arn=resources["worker_role_arn"],
        queue_observer_role_arn=resources["observer_role_arn"],
        queue_url=resources["queue_url"],
        queue_arn=resources["queue_arn"],
    )
    prepared_env = dict(env, paid_worker=selected)
    prepared_env["api_adapters"] = {
        **env["api_adapters"],
        "dispatcher": {
            **dispatcher,
            "operation_database_secret_ref": {
                "name": "superplane-operation-api-db",
                "key": "dsn",
            },
        },
    }
    paid_worker.validate(prepared_env, lock)
    require(
        isinstance(env.get("org_id"), str)
        and re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
            env["org_id"],
        )
        and isinstance(env.get("adp_org_id"), str)
        and 1 <= len(env["adp_org_id"]) <= 255,
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
        "domain_database_secret_id": operator["domain_database_secret_id"],
        "domain_database_schema": operator["domain_database_schema"],
        "queue_url": resources["queue_url"],
        "worker_namespace": request["namespace"],
        "worker_service_account": paid_worker.WORKER,
        "worker_container": "paid-worker",
        "worker_image_digests": [lock["images"][paid_worker.COMPONENT]],
        "repo": operator["repo"],
        "observation_url": operator["observation_url"],
        "observation_credential_secret_id": operator[
            "observation_credential_secret_id"
        ],
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
        "deployment_identity": selected_identity,
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
            "operator_role_arn": request["operator_role_arn"],
            "operator_role_id": selected_identity["expected_role_id"],
        },
        "paid_worker": selected,
        "gateway_binding_proposal": binding,
        "registry_proposals": [
            {
                "agent_id": operator["producer_registry_id"],
                "org_id": env["adp_org_id"],
                "scope": "internal",
                "role_arn": dispatcher["role_arn"],
                "credential_scopes": ["domain:operation-producer"],
            },
            {
                "agent_id": operator["worker_registry_id"],
                "org_id": env["adp_org_id"],
                "scope": "internal",
                "role_arn": resources["worker_role_arn"],
                "credential_scopes": [
                    "domain:operation-executor",
                    "domain:operation-recovery",
                ],
            },
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


MANAGED_ADDRESSES = frozenset(
    {
        "aws_sqs_queue.operations",
        "aws_iam_role.worker",
        "aws_iam_role.observer",
        "aws_iam_role_policy.worker",
        "aws_iam_role_policy.observer",
    }
)
READ_ADDRESSES = frozenset(
    {
        "data.aws_caller_identity.current",
        "data.aws_eks_cluster.selected",
        "data.aws_iam_openid_connect_provider.selected",
        "data.aws_iam_role.keda_operator",
        "data.aws_iam_role.operator",
    }
)
WORKER_ROUTES = (
    "task/acquire",
    "bootstrap",
    "task/heartbeat",
    "renew",
    "task/ack",
    "lease",
    "task/status",
    "authority",
    "recovery/scope",
    "recovery/authority",
    "recovery/observe",
    "recovery/inventory",
    "recovery/lifecycle",
    "recovery/account-creation",
    "recovery/bootstrap",
    "recovery/settlement",
)


def inspect_plan(plan, proposal, *, existing=False):
    """Refuse unrelated, destructive, ambiguous or privilege-expanding changes."""
    import json

    variables = proposal["terraform_variables"]
    account, region = variables["account_id"], variables["region"]
    prefix = f"adp-{variables['environment']}-superplane-domain"
    expected_names = {
        "aws_sqs_queue.operations": prefix + "-operations",
        "aws_iam_role.worker": prefix + "-worker",
        "aws_iam_role.observer": prefix + "-observer",
    }
    resources = plan.get("resource_changes")
    require(
        isinstance(resources, list), "Saved Terraform plan has no inspectable changes"
    )
    changes = {}
    for resource in resources:
        address = resource.get("address")
        require(
            address not in changes and address in MANAGED_ADDRESSES | READ_ADDRESSES,
            "Saved Terraform plan contains unknown or duplicate resources",
        )
        change = resource.get("change", {})
        actions = change.get("actions")
        require(
            actions in (["read"], ["no-op"])
            if address in READ_ADDRESSES
            else actions == (["no-op"] if existing else ["create"]),
            "Saved Terraform plan has unreviewed, destructive or missing changes",
        )
        changes[address] = change
    require(
        set(changes) >= MANAGED_ADDRESSES,
        "Saved Terraform plan omits an owned runtime resource",
    )
    if not existing:
        configurations = {
            entry.get("address"): entry
            for entry in plan.get("configuration", {})
            .get("root_module", {})
            .get("resources", [])
        }
        for role_name in ("worker", "observer"):
            address = "aws_iam_role_policy." + role_name
            references = (
                configurations.get(address, {})
                .get("expressions", {})
                .get("role", {})
                .get("references", [])
            )
            require(
                isinstance(references, list)
                and "aws_iam_role." + role_name + ".id" in references
                and set(references)
                <= {"aws_iam_role." + role_name, "aws_iam_role." + role_name + ".id"},
                "Saved Terraform plan policy must attach only to its exact reviewed role",
            )
    issuer = variables["oidc_issuer"].removeprefix("https://")
    trust_worker = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": "sts:AssumeRoleWithWebIdentity",
                "Principal": {
                    "Federated": f"arn:aws:iam::{account}:oidc-provider/{issuer}"
                },
                "Condition": {
                    "StringEquals": {
                        issuer + ":aud": "sts.amazonaws.com",
                        issuer
                        + ":sub": f"system:serviceaccount:{variables['namespace']}:superplane-paid-worker",
                    }
                },
            }
        ],
    }
    trust_observer = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": "sts:AssumeRole",
                "Principal": {"AWS": variables["keda_operator_role_arn"]},
            }
        ],
    }
    gateway = f"arn:aws:execute-api:{region}:{account}:{variables['api_id']}/{variables['api_stage']}/POST/internal/v1/controller-execution/"
    policies = {
        "aws_iam_role_policy.worker": {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": "execute-api:Invoke",
                    "Resource": [gateway + route for route in WORKER_ROUTES],
                }
            ],
        },
        "aws_iam_role_policy.observer": {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": "sqs:GetQueueAttributes",
                    "Resource": f"arn:aws:sqs:{region}:{account}:{prefix}-operations",
                }
            ],
        },
    }
    for address in MANAGED_ADDRESSES:
        change = changes[address]
        after, unknown = change.get("after"), change.get("after_unknown", {})
        require(
            isinstance(after, dict) and isinstance(unknown, dict),
            "Saved Terraform plan has unknown resource authority",
        )
        require(
            not any(
                unknown.get(key)
                for key in (
                    "name",
                    "path",
                    "policy",
                    "assume_role_policy",
                    "permissions_boundary",
                    "managed_policy_arns",
                    "tags",
                    "fifo_queue",
                    "sqs_managed_sse_enabled",
                )
            ),
            "Saved Terraform plan has unknown role or queue authority",
        )
        if not existing:
            require(
                change.get("before") is None,
                "Saved Terraform plan would adopt a foreign resource",
            )
        if address in expected_names:
            require(
                after.get("name") == expected_names[address],
                "Saved Terraform plan changed an owned resource name",
            )
            require(
                after.get("tags", {}).get("adp.aws-e.io/installation")
                == proposal["installation_id"],
                "Saved Terraform plan changed resource ownership",
            )
        if address == "aws_sqs_queue.operations":
            require(
                after.get("fifo_queue") is False
                and after.get("sqs_managed_sse_enabled") is True
                and after.get("visibility_timeout_seconds") == 3600
                and after.get("message_retention_seconds") == 345600
                and not after.get("policy")
                and not after.get("redrive_policy")
                and not unknown.get("policy")
                and not unknown.get("redrive_policy"),
                "Saved Terraform plan changed queue protection or bounds",
            )
        if address.startswith("aws_iam_role."):
            require(
                after.get("path", "/") == "/"
                and not after.get("permissions_boundary")
                and not after.get("managed_policy_arns")
                and not after.get("inline_policy"),
                "Saved Terraform plan adds an unexpected role authority",
            )
            expected_trust = (
                trust_worker if address.endswith("worker") else trust_observer
            )
            try:
                actual_trust = json.loads(after["assume_role_policy"])
            except (TypeError, ValueError, KeyError):
                actual_trust = None
            require(
                actual_trust == expected_trust,
                "Saved Terraform plan changes a protected role trust",
            )
        if address in policies:
            try:
                actual_policy = json.loads(after["policy"])
            except (TypeError, ValueError, KeyError):
                actual_policy = None
            require(
                actual_policy == policies[address],
                "Saved Terraform plan changes a protected policy",
            )
            require(
                after.get("name")
                == expected_names[
                    address.replace("aws_iam_role_policy", "aws_iam_role")
                ]
                + ("-invoke" if address.endswith("worker") else "-attributes"),
                "Saved Terraform plan changed a policy name",
            )
            if existing:
                require(
                    after.get("role")
                    == expected_names[
                        address.replace("aws_iam_role_policy", "aws_iam_role")
                    ],
                    "Saved Terraform plan changed a policy role",
                )
    return digest({address: changes[address] for address in sorted(MANAGED_ADDRESSES)})


def inspect_state(state, proposal, inspector):
    """Compare Terraform's isolated state to live AWS identities after apply loss."""
    import json

    resources = state.get("values", {}).get("root_module", {}).get("resources", [])
    require(isinstance(resources, list), "Domain runtime state is unavailable")
    managed = {
        entry.get("address"): entry
        for entry in resources
        if entry.get("mode") == "managed"
    }
    require(
        sum(entry.get("mode") == "managed" for entry in resources) == len(managed),
        "Domain runtime state has duplicate managed resources",
    )
    require(
        len(managed) == len(MANAGED_ADDRESSES) and set(managed) == MANAGED_ADDRESSES,
        "Domain runtime state contains foreign or missing resources",
    )
    changes = [
        {
            "address": address,
            "change": {
                "actions": ["no-op"],
                "after": entry.get("values", {}),
                "after_unknown": {},
            },
        }
        for address, entry in managed.items()
    ]
    inspect_plan({"resource_changes": changes}, proposal, existing=True)
    identity_resources = proposal["gateway_binding_proposal"]
    queue = managed["aws_sqs_queue.operations"]["values"]
    variables = proposal["terraform_variables"]
    expected_url = f"https://sqs.{variables['region']}.amazonaws.com/{variables['account_id']}/{queue['name']}"
    expected_arn = (
        f"arn:aws:sqs:{variables['region']}:{variables['account_id']}:{queue['name']}"
    )
    require(
        queue.get("url") == expected_url == identity_resources["queue_url"]
        and queue.get("arn") == expected_arn,
        "Domain runtime queue state changed identity",
    )
    attributes = inspector.json(
        inspector.aws(
            "sqs",
            "get-queue-attributes",
            "--queue-url",
            expected_url,
            "--attribute-names",
            "QueueArn",
            "FifoQueue",
            "SqsManagedSseEnabled",
            "Policy",
            "RedrivePolicy",
        )
    )["Attributes"]
    tags = inspector.json(
        inspector.aws("sqs", "list-queue-tags", "--queue-url", expected_url)
    )["Tags"]
    require(
        attributes.get("QueueArn") == expected_arn
        and attributes.get("FifoQueue", "false") == "false"
        and attributes.get("SqsManagedSseEnabled") == "true"
        and not attributes.get("Policy")
        and not attributes.get("RedrivePolicy")
        and tags.get("adp.aws-e.io/installation") == proposal["installation_id"],
        "Domain runtime live queue differs from owned state",
    )
    role_ids = {}
    for role_name in ("worker", "observer"):
        entry = managed["aws_iam_role." + role_name]["values"]
        role = inspector.json(
            inspector.aws("iam", "get-role", "--role-name", entry["name"])
        )["Role"]
        expected_role = f"arn:aws:iam::{variables['account_id']}:role/{entry['name']}"
        require(
            role.get("Arn") == expected_role
            and entry.get("arn") == expected_role
            and role.get("RoleId") == entry.get("unique_id")
            and any(
                tag.get("Key") == "adp.aws-e.io/installation"
                and tag.get("Value") == proposal["installation_id"]
                for tag in role.get("Tags", [])
            ),
            "Domain runtime live IAM role identity changed",
        )
        actual_trust = role.get("AssumeRolePolicyDocument")
        require(
            actual_trust == json.loads(entry["assume_role_policy"]),
            "Domain runtime live IAM trust changed",
        )
        policies = inspector.json(
            inspector.aws("iam", "list-role-policies", "--role-name", entry["name"])
        )["PolicyNames"]
        expected_policy = managed["aws_iam_role_policy." + role_name]["values"]
        require(
            policies == [expected_policy["name"]],
            "Domain runtime live inline role policies changed",
        )
        attached = inspector.json(
            inspector.aws(
                "iam", "list-attached-role-policies", "--role-name", entry["name"]
            )
        )["AttachedPolicies"]
        require(attached == [], "Domain runtime live role acquired managed policies")
        live_policy = inspector.json(
            inspector.aws(
                "iam",
                "get-role-policy",
                "--role-name",
                entry["name"],
                "--policy-name",
                expected_policy["name"],
            )
        )["PolicyDocument"]
        require(
            live_policy == json.loads(expected_policy["policy"]),
            "Domain runtime live inline policy changed",
        )
        role_ids[role_name] = role["RoleId"]
    output = (
        state.get("values", {})
        .get("outputs", {})
        .get("resource_identity", {})
        .get("value")
    )
    require(
        output
        == {
            "queue_name": queue["name"],
            "queue_arn": expected_arn,
            "queue_url": expected_url,
            "worker_role_arn": managed["aws_iam_role.worker"]["values"]["arn"],
            "worker_role_id": role_ids["worker"],
            "observer_role_arn": managed["aws_iam_role.observer"]["values"]["arn"],
            "observer_role_id": role_ids["observer"],
            "worker_ready": False,
            "installation_id": proposal["installation_id"],
        },
        "Domain runtime output or disabled readiness differs from live identities",
    )
    return {
        "queue_arn": expected_arn,
        "worker_role_id": role_ids["worker"],
        "observer_role_id": role_ids["observer"],
    }


def create_saved_plan(path, create):
    """Make only a newly created owned plan private; never repair a resumed file."""
    require(
        not path.exists() and not path.is_symlink(),
        "Unreceipted runtime plan already exists; use a new private directory",
    )
    create()
    require(
        not path.is_symlink() and path.is_file(),
        "New runtime plan is missing or linked",
    )
    info = path.stat()
    require(
        stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid(),
        "New runtime plan is not an owned regular file",
    )
    path.chmod(0o600)


def binary_plan_digest(path):
    """Hash exact private saved-plan bytes, refusing links and missing artifacts."""
    require(
        not path.is_symlink() and path.is_file(),
        "Saved binary runtime plan is missing or linked; obtain a fresh review",
    )
    info = path.stat()
    require(
        stat.S_ISREG(info.st_mode)
        and info.st_mode & 0o077 == 0
        and 0 < info.st_size <= 32 * 1024 * 1024,
        "Saved binary runtime plan is not private or exceeds the size bound",
    )
    return hashlib.sha256(path.read_bytes()).hexdigest()


def prepare(
    request,
    env,
    lock,
    operator,
    inspector,
    commands,
    directory,
    *,
    approved_plan_digest=None,
    approval_check=None,
):
    """Plan, explicitly authorize, apply and reconcile only this separate state.

    The operator must supply an exact reviewed digest for apply; no shared
    registry, Gateway, database, Kubernetes or worker readiness changes occur.
    """
    import fcntl
    import json
    import os
    import shutil
    from pathlib import Path

    from .config import Refusal
    from .runner import atomic

    validate_request(request)
    selected_identity = selected_operator(request, env)
    verify_operator(request, selected_identity, commands)
    work = Path(directory)
    require(
        work.is_absolute() and not work.is_symlink(),
        "Runtime preparation requires an absolute private working directory",
    )
    work.mkdir(mode=0o700, parents=True, exist_ok=True)
    require(
        work.stat().st_mode & 0o077 == 0,
        "Runtime preparation working directory must be private",
    )
    lock_file, receipt_file = (
        work / "runtime-preparation.lock",
        work / "runtime-preparation.json",
    )
    require(
        not lock_file.is_symlink() and not receipt_file.is_symlink(),
        "Runtime preparation refuses linked lock or receipt paths",
    )
    with lock_file.open("a+") as stream:
        os.chmod(lock_file, 0o600)
        fcntl.flock(stream, fcntl.LOCK_EX)
        if receipt_file.exists():
            try:
                receipt = json.loads(receipt_file.read_text())
            except (OSError, ValueError):
                raise Refusal(
                    "Saved runtime preparation receipt cannot be read"
                ) from None
            require(
                isinstance(receipt, dict)
                and receipt.get("status") in ("planned", "apply-attempted", "applied"),
                "Saved runtime preparation receipt has unknown status",
            )
            reviewed = review(
                request, inspector, existing=receipt["status"] != "planned"
            )
        else:
            receipt = None
            reviewed = review(request, inspector)
        proposal = compose(request, reviewed, env, lock, operator)
        proposal_hash = digest(proposal)
        if receipt:
            require(
                receipt.get("installation_id") == reviewed["installation_id"]
                and receipt.get("request_sha256") == reviewed["request_sha256"]
                and receipt.get("proposal_sha256") == proposal_hash
                and receipt.get("review_id") == operator["review_id"],
                "Saved runtime preparation identity or immutable review changed",
            )
        require(
            receipt is None
            or (
                set(receipt)
                in (
                    {
                        "version",
                        "status",
                        "installation_id",
                        "request_sha256",
                        "proposal_sha256",
                        "review_id",
                        "plan_sha256",
                        "binary_plan_sha256",
                        "resources",
                        "worker_ready",
                        "source_sha256",
                    },
                    {
                        "version",
                        "status",
                        "installation_id",
                        "request_sha256",
                        "proposal_sha256",
                        "review_id",
                        "plan_sha256",
                        "binary_plan_sha256",
                        "resources",
                        "worker_ready",
                        "source_sha256",
                        "applied_identity",
                    },
                )
                and receipt.get("version") == 1
                and receipt.get("worker_ready") is False
                and receipt.get("resources") == reviewed["resources"]
            ),
            "Saved runtime receipt contains unreviewed readiness or resources",
        )
        terraform_root = Path(__file__).resolve().parents[1] / "infra/domain-runtime"
        sources = (*terraform_root.glob("*.tf"), terraform_root / ".terraform.lock.hcl")
        require(
            {source.name for source in sources}
            == {
                "main.tf",
                "outputs.tf",
                "variables.tf",
                "versions.tf",
                ".terraform.lock.hcl",
            },
            "Reviewed runtime Terraform source set changed",
        )
        source_sha256 = digest({source.name: source.read_text() for source in sources})
        require(
            receipt is None or receipt["source_sha256"] == source_sha256,
            "Saved runtime Terraform source changed since approval",
        )
        terraform_dir = work / "terraform"
        require(
            not terraform_dir.is_symlink(),
            "Runtime preparation refuses a linked Terraform directory",
        )
        terraform_dir.mkdir(mode=0o700, exist_ok=True)
        require(
            terraform_dir.stat().st_mode & 0o077 == 0,
            "Runtime Terraform directory must be private",
        )
        for source in sources:
            destination = terraform_dir / source.name
            require(
                not destination.is_symlink(),
                "Runtime preparation refuses linked Terraform sources",
            )
            shutil.copyfile(source, destination)
        atomic(
            terraform_dir / "installation.auto.tfvars.json",
            proposal["terraform_variables"],
        )
        saved_plan = terraform_dir / "installation.tfplan"
        if receipt:
            require(
                binary_plan_digest(saved_plan) == receipt["binary_plan_sha256"],
                "Saved binary runtime plan changed; obtain a fresh independent review",
            )
        prefix = ["terraform", f"-chdir={terraform_dir}"]

        def call(*args):
            verify_operator(request, selected_identity, commands)
            if args[0] == "apply":
                require(
                    binary_plan_digest(saved_plan) == receipt["binary_plan_sha256"],
                    "Saved binary runtime plan changed before apply",
                )
            return commands.call([*prefix, *args], timeout=120)

        def structured(*args):
            try:
                return json.loads(call(*args).stdout)
            except (TypeError, ValueError):
                raise Refusal(
                    "Runtime Terraform returned an invalid structured response"
                ) from None

        call(
            "init",
            "-input=false",
            "-lockfile=readonly",
            f"-backend-config=bucket=adp-terraform-state-{request['account_id']}",
            f"-backend-config=key={request['environment']}/modules/superplane-domain-runtime/terraform.tfstate",
            f"-backend-config=region={request['region']}",
            "-backend-config=encrypt=true",
            "-backend-config=dynamodb_table=adp-terraform-locks",
        )
        if receipt and receipt["status"] != "planned":
            state = structured("show", "-json")
            observed = inspect_state(state, proposal, inspector)
            require(
                receipt.get("applied_identity") in (None, observed),
                "Replayed runtime resource identity changed",
            )
            receipt["applied_identity"] = observed
            receipt["status"] = "applied"
            atomic(receipt_file, receipt)
            return receipt
        if receipt is None:
            call(
                "plan", "-input=false", "-lock-timeout=60s", "-out=installation.tfplan"
            )
        binary_hash = binary_plan_digest(saved_plan)
        plan = structured("show", "-json", "installation.tfplan")
        plan_hash = inspect_plan(plan, proposal)
        if receipt:
            require(
                receipt.get("plan_sha256") == plan_hash,
                "Saved runtime plan changed; obtain a fresh independent review",
            )
        else:
            receipt = {
                "version": 1,
                "status": "planned",
                "installation_id": reviewed["installation_id"],
                "request_sha256": reviewed["request_sha256"],
                "proposal_sha256": proposal_hash,
                "source_sha256": source_sha256,
                "review_id": operator["review_id"],
                "plan_sha256": plan_hash,
                "binary_plan_sha256": binary_hash,
                "resources": reviewed["resources"],
                "worker_ready": False,
            }
            atomic(receipt_file, receipt)
        if approved_plan_digest is None:
            return receipt
        require(
            approved_plan_digest == plan_hash,
            "Exact saved runtime plan digest has not been independently approved",
        )
        require(
            approval_check is not None,
            "A separate authenticated plan approver is required before runtime apply",
        )
        try:
            approval = approval_check.verify_plan(
                plan_sha256=plan_hash,
                installation_id=reviewed["installation_id"],
                review_id=operator["review_id"],
            )
        except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
            raise Refusal("Separate authenticated plan approver unavailable") from None
        require(
            isinstance(approval, dict)
            and set(approval)
            == {"approved", "plan_sha256", "installation_id", "review_id", "approver"}
            and approval["approved"] is True
            and approval["plan_sha256"] == plan_hash
            and approval["installation_id"] == reviewed["installation_id"]
            and approval["review_id"] == operator["review_id"]
            and isinstance(approval["approver"], str)
            and approval["approver"]
            and approval["approver"] != request["operator_role_arn"],
            "Separate authenticated plan approver refused this exact runtime plan",
        )
        require(
            review(request, inspector) == reviewed,
            "Runtime target identity or name inventory changed before apply",
        )
        verify_operator(request, selected_identity, commands)
        require(
            binary_plan_digest(saved_plan) == receipt["binary_plan_sha256"],
            "Saved binary runtime plan changed after approval",
        )
        receipt["status"] = "apply-attempted"
        atomic(receipt_file, receipt)
        call("apply", "-input=false", "-lock-timeout=60s", "installation.tfplan")
        observed = inspect_state(structured("show", "-json"), proposal, inspector)
        receipt["applied_identity"] = observed
        receipt["status"] = "applied"
        atomic(receipt_file, receipt)
        return receipt
