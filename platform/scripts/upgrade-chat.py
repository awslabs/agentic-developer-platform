#!/usr/bin/env python3
"""Ordered retirement of the legacy chat worker.

Without ``--delegated`` this helper never activates delegated chat: the ingress must
already publish unavailable chat and the ScaledJob is removed. With ``--delegated``
(deploy-all.sh ``--chat delegated``) the supervisor ScaledJob applied by
deploy-chat-scaledjob.sh reuses the legacy name with service account
``adp-chat-supervisor``; it is left in place and only credentialed ``adp-agent``
work has to finish before the legacy grants are retired.
"""

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time


ROOT = Path(__file__).resolve().parents[2]
NAMESPACE = "adp-gateway-agents"
NAME = "chat-agent-worker"
LEGACY_SERVICE_ACCOUNT = "adp-agent"
DELEGATED_SERVICE_ACCOUNT = "adp-chat-supervisor"
LEGACY = {
    key: "chat-" + key
    for key in (
        "sqs",
        "sessions",
        "secrets",
        "bootstrap_logs",
        "gateway_invoke",
        "chat_data",
        "chat_artifacts",
        "chat_queue",
    )
}
LEGACY["deny-direct-bedrock"] = "deny-direct-bedrock"
CURRENT = {"chat-queue-depth-only", "deny-direct-chat-authority"}
DENIED = {
    "bedrock:*",
    "bedrock-agentcore:*",
    "dynamodb:*",
    "s3:*",
    "secretsmanager:*",
    "kms:*",
    "execute-api:Invoke",
    "sts:AssumeRole",
    "iam:PassRole",
    "sqs:ReceiveMessage",
    "sqs:DeleteMessage",
    "sqs:SendMessage",
}


def digest(document):
    return hashlib.sha256(
        json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def retirement_denial(document, *, allow_previous=False):
    statements = document.get("Statement", [])
    reviewed_actions = [DENIED]
    if allow_previous:
        # Preparation can recognize the previous release so the normal apply
        # strengthens its denial. Removal and final verification require current
        # authority coverage, including the separate AgentCore IAM namespace.
        reviewed_actions.append(DENIED - {"bedrock-agentcore:*"})
    return (
        document.get("Version") == "2012-10-17"
        and len(statements) == 1
        and statements[0].get("Effect") == "Deny"
        and statements[0].get("Resource") == "*"
        and set(statements[0].get("Action", [])) in reviewed_actions
        and set(statements[0]) == {"Effect", "Resource", "Action"}
    )


def write(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")
    path.chmod(0o600)


def kube(*args):
    output = subprocess.check_output(
        ["kubectl", *args, "--request-timeout=30s"], text=True
    )
    return json.loads(output) if output.strip() else None


def active_pods(pods):
    return [
        p["metadata"]["name"]
        for p in pods["items"]
        if p.get("spec", {}).get("serviceAccountName") == "adp-agent"
        and p.get("status", {}).get("phase") not in ("Succeeded", "Failed")
    ]


def active_jobs(jobs):
    return [
        j["metadata"]["name"]
        for j in jobs["items"]
        if j.get("spec", {})
        .get("template", {})
        .get("spec", {})
        .get("serviceAccountName")
        == "adp-agent"
        and not any(
            c.get("type") in ("Complete", "Failed") and c.get("status") == "True"
            for c in j.get("status", {}).get("conditions", [])
        )
    ]


def work_remaining():
    return active_jobs(
        kube("get", "jobs", "-n", NAMESPACE, "-o", "json")
    ) + active_pods(kube("get", "pods", "-n", NAMESPACE, "-o", "json"))


def identity(account, region, environment):
    if not re.fullmatch("[0-9]{12}", account) or environment not in (
        "dev",
        "staging",
        "prod",
    ):
        raise ValueError("Invalid chat retirement target")
    import boto3

    session = boto3.Session(region_name=region)
    if session.client("sts").get_caller_identity()["Account"] != account:
        raise ValueError("Chat retirement account differs from caller")
    cluster = kube("config", "view", "--minify", "-o", "json")["clusters"][0]["name"]
    if (
        cluster
        != f"arn:aws:eks:{region}:{account}:cluster/adp-{environment}-eks-cluster"
    ):
        raise ValueError("Chat retirement kubeconfig differs from target")
    return session


def prepare(session, directory, account, region, environment):
    iam = session.client("iam")
    role = f"adp-{environment}-chat-worker-role"
    try:
        names = iam.list_role_policies(RoleName=role)["PolicyNames"]
    except iam.exceptions.NoSuchEntityException:
        names = []  # Fresh factory; never synthesize legacy authority.
    if set(names) - set(LEGACY.values()) - CURRENT:
        raise ValueError("Unknown chat inline policies; refusing automatic retirement")
    documents = {
        key: iam.get_role_policy(RoleName=role, PolicyName=name)["PolicyDocument"]
        for key, name in LEGACY.items()
        if name in names
    }
    if names and iam.list_attached_role_policies(RoleName=role)["AttachedPolicies"]:
        raise ValueError("Unexpected managed policies on legacy chat role")
    for key, document in documents.items():
        statements = document.get("Statement")
        if (
            document.get("Version") != "2012-10-17"
            or not isinstance(statements, list)
            or not statements
        ):
            raise ValueError("Invalid legacy chat policy document")
        if key == "deny-direct-bedrock":
            if statements != [
                {
                    "Effect": "Deny",
                    "Action": [
                        "bedrock:InvokeModel",
                        "bedrock:InvokeModelWithResponseStream",
                    ],
                    "Resource": "*",
                }
            ]:
                raise ValueError("Legacy chat denial differs from reviewed policy")
        else:
            for statement in statements:
                if statement.get("Effect") == "Allow":
                    continue
                if key != "secrets" or statement != {
                    "Sid": "DenyTenantAwsAccess",
                    "Effect": "Deny",
                    "Action": ["secretsmanager:GetSecretValue"],
                    "Resource": f"arn:aws:secretsmanager:*:{account}:secret:adp/*/tenants/*/aws-access-*",
                }:
                    raise ValueError(
                        "Legacy chat runtime policy contains an unexpected denial"
                    )
    denial_installed = "deny-direct-chat-authority" in names
    if denial_installed and not retirement_denial(
        iam.get_role_policy(RoleName=role, PolicyName="deny-direct-chat-authority")[
            "PolicyDocument"
        ],
        allow_previous=True,
    ):
        raise ValueError(
            "Installed chat retirement denial differs from the reviewed policy"
        )
    write(
        directory / "chat-retirement.tfvars.json",
        {
            "chat_legacy_upgrade_policies": {
                k: json.dumps(v) for k, v in documents.items()
            },
            "chat_legacy_upgrade_deny_ready": denial_installed,
        },
    )
    write(
        directory / "chat-retirement.json",
        {
            "account": account,
            "region": region,
            "environment": environment,
            "phase": "prepared",
            "policies": {LEGACY[k]: digest(v) for k, v in documents.items()},
        },
    )


def scaledjob_service_account(scaled):
    return (
        (scaled or {})
        .get("spec", {})
        .get("jobTargetRef", {})
        .get("template", {})
        .get("spec", {})
        .get("serviceAccountName")
    )


def is_delegated_scaledjob(scaled):
    return scaledjob_service_account(scaled) == DELEGATED_SERVICE_ACCOUNT


def require_ingress(session, environment, package=None, delegated=False):
    """The live ingest must be the packaged build in the state this mode needs.

    ``ADP_CHAT_MODEL_POLICY_ENABLED`` is the delegated-chat plumbing switch (the
    ingest roots every turn as a model root; without it and with the legacy worker
    retired it answers "Chat is currently unavailable"), so ``--delegated`` always
    expects ``true``. Whether a decision requires invocability evidence is the
    gateway's separate chat posture (``chat_model_policy_posture``, #7997) and is
    deliberately not read here.
    """
    live = session.client("lambda").get_function_configuration(
        FunctionName=f"adp-{environment}-agent-gateway-ingest"
    )
    package = package or (
        Path(os.environ["ADP_RELEASE_DIR"]) / "lambda/ingest.zip"
        if os.environ.get("ADP_RELEASE_DIR")
        else ROOT
        / "modules/agent-factory/infra/modules/lambda-gateway/.build/ingest.zip"
    )
    expected = base64.b64encode(hashlib.sha256(package.read_bytes()).digest()).decode()
    env = live.get("Environment", {}).get("Variables", {})
    if (
        live["State"] != "Active"
        or live["LastUpdateStatus"] != "Successful"
        or live["CodeSha256"] != expected
        or env.get("ADP_CHAT_LEGACY_RETIRED") != "true"
        or env.get("ADP_CHAT_MODEL_POLICY_ENABLED") != ("true" if delegated else "false")
    ):
        raise ValueError(
            "Delegated-chat ingress (model policy enabled) must be deployed before legacy retirement"
            if delegated
            else "Unavailable-chat ingress must be deployed before legacy retirement; delegated activation is separate"
        )
    return expected


def queue_empty(session, account, region, environment):
    sqs = session.client("sqs")
    url = sqs.get_queue_url(
        QueueName=f"adp-{environment}-agent-gateway-tasks.fifo",
        QueueOwnerAWSAccountId=account,
    )["QueueUrl"]
    keys = [
        "ApproximateNumberOfMessages",
        "ApproximateNumberOfMessagesNotVisible",
        "ApproximateNumberOfMessagesDelayed",
    ]
    attrs = sqs.get_queue_attributes(QueueUrl=url, AttributeNames=keys)["Attributes"]
    if set(attrs) != set(keys) or any(attrs[k] != "0" for k in keys):
        raise ValueError(
            "Legacy chat queue contains work; preserve messages and reconcile before retirement"
        )


def drain(session, directory, account, region, environment, delegated=False):
    expected = require_ingress(session, environment, delegated=delegated)
    # The delegated supervisor shares the FIFO queue and must keep consuming it;
    # only the legacy identity's own jobs have to finish before its grants go.
    if not delegated:
        queue_empty(session, account, region, environment)
    scaled = kube(
        "get", "scaledjob", NAME, "-n", NAMESPACE, "--ignore-not-found", "-o", "json"
    )
    if scaled and delegated and is_delegated_scaledjob(scaled):
        print("Delegated chat ScaledJob is active; retiring only legacy work", flush=True)
        scaled = None
    elif scaled and delegated:
        raise ValueError(
            "Delegated chat ScaledJob must be applied and Ready before legacy retirement"
        )
    if scaled:
        # Pause only admission; running jobs finish without deletion.
        patch = {
            "metadata": {
                "resourceVersion": scaled["metadata"]["resourceVersion"],
                "annotations": {"autoscaling.keda.sh/paused": "true"},
            }
        }
        kube(
            "patch",
            "scaledjob",
            NAME,
            "-n",
            NAMESPACE,
            "--type=merge",
            "-p",
            json.dumps(patch),
            "-o",
            "json",
        )
        subprocess.run(
            [
                "kubectl",
                "wait",
                "--for=condition=Paused",
                "scaledjob/" + NAME,
                "-n",
                NAMESPACE,
                "--timeout=120s",
            ],
            check=True,
        )
    deadline = time.monotonic() + 960
    while work_remaining():
        if time.monotonic() >= deadline:
            raise ValueError(
                "Legacy chat jobs did not drain; no jobs or IAM grants were deleted"
            )
        print("Waiting for legacy chat jobs to finish", flush=True)
        time.sleep(15)
    # SQS counters are approximate. Require a second empty observation after
    # quiescing, and do not delete/purge/receive any queued message.
    if not delegated:
        time.sleep(60)
        queue_empty(session, account, region, environment)
    if work_remaining():
        raise ValueError("Legacy chat work appeared during retirement")
    if scaled:
        subprocess.run(
            [
                "kubectl",
                "delete",
                "scaledjob",
                NAME,
                "-n",
                NAMESPACE,
                "--cascade=orphan",
                "--wait=true",
                "--timeout=60s",
            ],
            check=True,
        )
    if work_remaining():
        raise ValueError(
            "Legacy chat work remains after stopping admission; grants were not retired"
        )
    receipt = json.loads((directory / "chat-retirement.json").read_text())
    if (receipt["account"], receipt["region"], receipt["environment"]) != (
        account,
        region,
        environment,
    ):
        raise ValueError("Chat retirement evidence belongs to another target")
    receipt.update(phase="drained", ingest_code_sha256=expected)
    write(directory / "chat-retirement.json", receipt)


def permitted_deletion(resource, plan, account, receipt):
    """Only the observed grants on the drained legacy role; no general override."""
    values = {k: v.get("value") for k, v in plan.get("variables", {}).items()}
    environment = values.get("environment")
    if not receipt or (
        receipt.get("account"),
        receipt.get("region"),
        receipt.get("environment"),
        receipt.get("phase"),
    ) != (account, values.get("aws_region"), environment, "drained"):
        return False
    if values.get("chat_legacy_upgrade_policies") != {}:
        return False
    role = f"adp-{environment}-chat-worker-role"
    changes = {r["address"]: r["change"] for r in plan["resource_changes"]}
    retained = changes.get("aws_iam_role.chat_worker", {})
    old, new = retained.get("before") or {}, retained.get("after") or {}
    if (
        retained.get("actions") not in (["no-op"], ["update"])
        or old.get("name") != new.get("name")
        or old.get("name") != role
        or old.get("arn") != new.get("arn")
        or old.get("arn") != f"arn:aws:iam::{account}:role/{role}"
        or old.get("permissions_boundary") != new.get("permissions_boundary")
        or old.get("assume_role_policy") != new.get("assume_role_policy")
    ):
        return False
    denial = changes.get("aws_iam_role_policy.chat_worker_deny_direct_authority[0]", {})
    after = denial.get("after") or {}
    try:
        deny = json.loads(after.get("policy", "null"))
        document = json.loads(
            (resource["change"].get("before") or {}).get("policy", "null")
        )
    except (TypeError, ValueError):
        return False
    if (
        denial.get("actions") != ["no-op"]
        or denial.get("before") != after
        or after.get("role") != role
        or after.get("name") != "deny-direct-chat-authority"
        or not isinstance(deny, dict)
        or not retirement_denial(deny)
    ):
        return False
    before = resource["change"].get("before") or {}
    names = {
        f'aws_iam_role_policy.chat_worker_runtime["{k}"]': v
        for k, v in LEGACY.items()
        if k != "deny-direct-bedrock"
    }
    names["aws_iam_role_policy.chat_worker_deny_direct_bedrock[0]"] = (
        "deny-direct-bedrock"
    )
    name = names.get(resource.get("address"))
    return (
        name is not None
        and resource.get("type") == "aws_iam_role_policy"
        and resource.get("deposed") is None
        and resource["change"]["actions"] == ["delete"]
        and resource["change"].get("after") is None
        and before.get("role") == role
        and before.get("name") == name
        and before.get("id") == role + ":" + name
        and isinstance(document, dict)
        and receipt.get("policies", {}).get(name) == digest(document)
    )


def verify(session, account, region, environment, package=None, delegated=False):
    require_ingress(session, environment, package, delegated=delegated)
    scaled = kube(
        "get", "scaledjob", NAME, "-n", NAMESPACE, "--ignore-not-found", "-o", "json"
    )
    if scaled and not (delegated and is_delegated_scaledjob(scaled)):
        raise ValueError("Retired chat ScaledJob still exists")
    if delegated and not scaled:
        raise ValueError("Delegated chat ScaledJob is missing")
    if work_remaining():
        raise ValueError("Retired chat identity still has running pods")
    if not delegated:
        queue_empty(session, account, region, environment)
    iam = session.client("iam")
    role = f"adp-{environment}-chat-worker-role"
    if set(iam.list_role_policies(RoleName=role)["PolicyNames"]) != CURRENT:
        raise ValueError("Legacy chat inline grants remain")
    if iam.list_attached_role_policies(RoleName=role)["AttachedPolicies"]:
        raise ValueError("Unexpected managed grants on retired chat role")
    document = iam.get_role_policy(
        RoleName=role, PolicyName="deny-direct-chat-authority"
    )["PolicyDocument"]
    if not retirement_denial(document):
        raise ValueError("Retired chat authority denial is incomplete")
    if delegated:
        return {"status": "delegated", "reason": "legacy_grants_retired_supervisor_active"}
    return {"status": "retired", "reason": "delegated_chat_pending_qualification_6937"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=["prepare", "drain", "verify"])
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--account", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--environment", required=True)
    parser.add_argument(
        "--delegated",
        action="store_true",
        help="the delegated supervisor ScaledJob is applied; retire only legacy work",
    )
    args = parser.parse_args()
    session = identity(args.account, args.region, args.environment)
    if args.phase == "verify":
        print(json.dumps(verify(session, args.account, args.region, args.environment,
                                delegated=args.delegated)))
    elif args.phase == "drain":
        drain(session, args.directory, args.account, args.region, args.environment,
              delegated=args.delegated)
    else:
        prepare(session, args.directory, args.account, args.region, args.environment)
