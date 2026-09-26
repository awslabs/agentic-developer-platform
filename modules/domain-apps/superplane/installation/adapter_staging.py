"""Receipt-bound adapter staging within the existing installer route fence."""

import copy
import json
from datetime import UTC, datetime, timedelta

from .api_adapters import project, verify_role
from .config import digest, require


def snapshot(installer):
    env = installer.env
    adapters = env["api_adapters"]
    transport = adapters["vault"]["transport"]
    service = installer.json(
        installer.kube(
            "get",
            "service",
            transport["service"],
            "-n",
            transport["namespace"],
            "-o",
            "json",
        )
    )
    require(
        service["spec"].get("type", "ClusterIP") == "ClusterIP"
        and service["spec"].get("selector") == transport["selector"]
        and any(
            p.get("port") == transport["port"]
            and p.get("targetPort") == transport["target_port"]
            and p.get("protocol", "TCP") == "TCP"
            for p in service["spec"].get("ports", [])
        ),
        "Selected vault Service transport changed",
    )
    ref = adapters["vault"]["secret_key_ref"]
    # Never emit Secret data or annotations (last-applied can contain data).
    metadata = installer.kube(
        "get",
        "secret",
        ref["name"],
        "-n",
        env["namespace"],
        "-o",
        'jsonpath={.metadata.uid}{" "}{.metadata.resourceVersion}',
    ).stdout.split()
    require(
        len(metadata) == 2 and all(metadata),
        "Existing vault Secret metadata is unavailable",
    )
    producer = adapters["dispatcher"]
    role_name = producer["role_arn"].rsplit("/", 1)[1]
    role = installer.json(installer.aws("iam", "get-role", "--role-name", role_name))[
        "Role"
    ]
    cluster = installer.json(
        installer.aws("eks", "describe-cluster", "--name", env["cluster"])
    )["cluster"]
    policies = []
    for name in installer.json(
        installer.aws("iam", "list-role-policies", "--role-name", role_name)
    )["PolicyNames"]:
        policies.append(
            installer.json(
                installer.aws(
                    "iam",
                    "get-role-policy",
                    "--role-name",
                    role_name,
                    "--policy-name",
                    name,
                )
            )["PolicyDocument"]
        )
    for policy in installer.json(
        installer.aws("iam", "list-attached-role-policies", "--role-name", role_name)
    )["AttachedPolicies"]:
        arn = policy["PolicyArn"]
        version = installer.json(
            installer.aws("iam", "get-policy", "--policy-arn", arn)
        )["Policy"]["DefaultVersionId"]
        policies.append(
            installer.json(
                installer.aws(
                    "iam",
                    "get-policy-version",
                    "--policy-arn",
                    arn,
                    "--version-id",
                    version,
                )
            )["PolicyVersion"]["Document"]
        )
    boundary = None
    if role.get("PermissionsBoundary"):
        arn = role["PermissionsBoundary"]["PermissionsBoundaryArn"]
        version = installer.json(
            installer.aws("iam", "get-policy", "--policy-arn", arn)
        )["Policy"]["DefaultVersionId"]
        boundary = installer.json(
            installer.aws(
                "iam",
                "get-policy-version",
                "--policy-arn",
                arn,
                "--version-id",
                version,
            )
        )["PolicyVersion"]["Document"]
    verify_role(env, role, policies, cluster["identity"]["oidc"]["issuer"])
    return {
        "environment_sha256": digest(env),
        "release_id": installer.release,
        "secret": {
            "namespace": env["namespace"],
            **ref,
            "uid": metadata[0],
            "resource_version": metadata[1],
        },
        "service": {
            "uid": service["metadata"]["uid"],
            "resource_version": service["metadata"]["resourceVersion"],
        },
        "role_arn": role["Arn"],
        "role_id": role["RoleId"],
        "role_policy_sha256": digest(
            {
                "trust": role["AssumeRolePolicyDocument"],
                "policies": policies,
                "boundary": role.get("PermissionsBoundary"),
                "boundary_document": boundary,
            }
        ),
        "cluster_arn": cluster["arn"],
        "oidc": cluster["identity"]["oidc"]["issuer"],
    }


def api_document(installer):
    return next(
        d
        for d in installer.docs
        if d["kind"] == "Deployment" and d["metadata"]["name"] == "superplane-api"
    )


def wait_api(installer):
    installer.kube(
        "rollout",
        "status",
        "deployment/superplane-api",
        "-n",
        installer.env["namespace"],
        f"--timeout={installer.env['timeout_seconds']}s",
        timeout=installer.env["timeout_seconds"] + 30,
    )
    expected = api_document(installer)
    current = installer.existing(expected)
    desired = expected["spec"]["template"]["spec"]
    actual = current["spec"]["template"]["spec"]
    require(
        actual["serviceAccountName"] == desired["serviceAccountName"]
        and actual["containers"][0]["image"] == desired["containers"][0]["image"]
        and actual["containers"][0]["env"] == desired["containers"][0]["env"]
        and current.get("status", {}).get("availableReplicas", 0) >= 1,
        "API stage Deployment differs from the reviewed template",
    )
    return current


def expected_runtime(installer):
    env = installer.env
    return {
        **env["api_adapters"],
        "source_revision": installer.lock["source_revision"],
        "release_id": installer.release,
        "schema": env["database"]["schema"],
        "schema_head": installer.lock["schema"]["observed"]["head"],
        "account_id": env["account_id"],
        "org_id": env["org_id"],
    }


def verify(installer, token):
    env = installer.env
    adapters = env["api_adapters"]
    deployed = wait_api(installer)
    before = snapshot(installer)
    expected = {
        **adapters,
        "source_revision": installer.lock["source_revision"],
        "release_id": installer.release,
        "schema": env["database"]["schema"],
        "schema_head": installer.lock["schema"]["observed"]["head"],
        "account_id": env["account_id"],
        "org_id": env["org_id"],
    }
    report = installer.json(
        installer.kube(
            "exec",
            "-i",
            "deployment/superplane-api",
            "-n",
            env["namespace"],
            "--",
            "python",
            "-m",
            "app.installation",
            "adapter-stage",
            data=json.dumps(expected),
        )
    )
    require(
        report.get("stage_version") == 1
        and report.get("management_only") is True
        and report.get("dispatch_enabled") is False
        and report.get("paid_admission_enabled") is False
        and report.get("source_revision") == expected["source_revision"]
        and report.get("release_id") == installer.release
        and report.get("producer_ready") is True
        and report.get("configuration_verified") is True
        and len(report.get("capabilities", {})) == 4
        and all(report["capabilities"].values()),
        "Actual disabled API adapter verification failed",
    )
    control = adapters["verification"]
    path = f"/internal/installation/workspaces/{control['workspace_id']}/credential-evidence/{control['connection_id']}"
    # Authenticated caller token only through stdin, never argv/env or receipts.
    program = """import json,sys,httpx
v=json.load(sys.stdin)
with httpx.Client(base_url="http://127.0.0.1:8000",timeout=15,trust_env=False,follow_redirects=False) as c:
 r=c.get(v["path"],headers={"Authorization":"Bearer "+v["token"]})
 if r.status_code!=200: raise SystemExit("authenticated credential control refused")
 value=r.json()
 management=c.get("/workspaces",headers={"Authorization":"Bearer "+v["token"]})
 if management.status_code!=200: raise SystemExit("authenticated management read refused")
 # Deliberately no plan revision or approval: this cannot authorize spending
 # even if the admission fence regresses. Require the staged 503, not any denial.
 refused=c.post("/workspaces",headers={"Authorization":"Bearer "+v["token"]},json={"name":"adapter-verification-no-approval"})
 if refused.status_code!=503 or refused.json().get("detail")!="operation admission is disabled for adapter verification": raise SystemExit("staged admission fence not observed")
 value["management_read_verified"]=True
 value["unapproved_admission_refused"]=True
 print(json.dumps(value))
"""
    metadata = installer.json(
        installer.kube(
            "exec",
            "-i",
            "deployment/superplane-api",
            "-n",
            env["namespace"],
            "--",
            "python",
            "-c",
            program,
            data=json.dumps({"path": path, "token": token}),
        )
    )
    require(
        metadata.get("control_version") == 1
        and metadata.get("org_id") == env["org_id"]
        and all(metadata.get(k) == v for k, v in control.items())
        and metadata.get("raw_material_returned") is False
        and datetime.fromisoformat(metadata["evidence_expires_at"]) > datetime.now(UTC),
        "Known credential metadata control differs from the reviewed target",
    )
    require(
        snapshot(installer) == before, "API adapter inputs changed during verification"
    )
    require_quiescent(installer)
    installer.receipt["adapter_stage"] = {
        "state": "verified-disabled",
        "deployment_uid": deployed["metadata"]["uid"],
        "verified_at": datetime.now(UTC).isoformat(),
        "binding": before,
        "report": report,
        "credential_metadata": metadata,
    }
    installer.save()


def activate(installer):
    stage = installer.receipt.get("adapter_stage", {})
    require(
        stage.get("state") == "verified-disabled"
        and datetime.fromisoformat(stage["verified_at"])
        > datetime.now(UTC) - timedelta(minutes=5)
        and stage["binding"] == snapshot(installer),
        "Adapter activation requires fresh unchanged verification",
    )
    require(
        wait_api(installer)["metadata"]["uid"] == stage["deployment_uid"],
        "Verified API Deployment was replaced",
    )
    stage["state"] = "activation-pending"
    installer.save()
    project(installer.env, installer.docs, active=True)
    installer.apply([api_document(installer)])
    wait_api(installer)
    stage["state"] = "activated-awaiting-full-verification"
    installer.save()


def restore_disabled(installer):
    stage = installer.receipt.setdefault("adapter_stage", {})
    stage["state"] = "disabled-restore-pending"
    installer.save()
    project(installer.env, installer.docs, active=False)
    installer.apply([copy.deepcopy(api_document(installer))])
    wait_api(installer)
    runtime = installer.json(
        installer.kube(
            "exec",
            "deployment/superplane-api",
            "-n",
            installer.env["namespace"],
            "--",
            "python",
            "-m",
            "app.installation",
            "readiness",
        )
    )
    require(
        runtime.get("mode") == "management"
        and runtime.get("operation_dispatch_enabled") is False
        and runtime.get("paid_admission_enabled") is False,
        "Disabled API stage availability was not observed",
    )
    stage["state"] = "disabled-restored"
    stage["management_process_observed"] = True
    stage["public_management_available"] = False
    installer.save()


def require_quiescent(installer):
    """Refuse to disable an API with admitted/active work; never cancel it."""
    from .config import LABEL

    current = installer.existing(api_document(installer))
    if current is None:
        installer.receipt["adapter_quiescence"] = {
            "api_absent": True,
            "active_work_verified": False,
        }
        return
    require(
        current["metadata"].get("labels", {}).get(LABEL) == installer.owner,
        "Cannot stage a foreign API Deployment",
    )
    program = """import asyncio,json
from sqlalchemy import text
from app.database import engine
async def check():
 counts={}
 async with engine.connect() as c:
  for table,predicate in (
   ("harness_operations", "state IN ('pending','running') OR cleanup_required"),
   ("harness_operation_leases", "closed_at IS NULL"),
   ("harness_dispatch_outbox", "delivered_at IS NULL AND abandoned_at IS NULL"),
   ("workspaces", "status IN ('Provisioning','Teardown')"),
   ("deployments", "status NOT IN ('Deleted','Failed','CancelledBeforeDispatch')")):
   exists=await c.scalar(text("SELECT to_regclass(:table)"),{"table":table})
   counts[table]=int(await c.scalar(text("SELECT count(*) FROM "+table+" WHERE "+predicate))) if exists else 0
 return counts
try: print(json.dumps(asyncio.run(check())))
except Exception: raise SystemExit("operation quiescence read refused") from None
"""
    counts = installer.json(
        installer.kube(
            "exec",
            "deployment/superplane-api",
            "-n",
            installer.env["namespace"],
            "--",
            "python",
            "-c",
            program,
        )
    )
    require(
        set(counts)
        == {
            "harness_operations",
            "harness_operation_leases",
            "harness_dispatch_outbox",
            "workspaces",
            "deployments",
        }
        and all(type(v) is int and v == 0 for v in counts.values()),
        "Adapter staging requires no outstanding admitted or active work; existing authority is not cancelled",
    )
    installer.receipt["adapter_quiescence"] = {
        "counts": counts,
        "active_work_verified": True,
        "observed_at": datetime.now(UTC).isoformat(),
    }
    installer.save()


def verify_active(installer):
    stage = installer.receipt.get("adapter_stage", {})
    require(
        stage.get("state") == "activated-awaiting-full-verification"
        and datetime.fromisoformat(stage["verified_at"])
        > datetime.now(UTC) - timedelta(minutes=5)
        and datetime.fromisoformat(stage["credential_metadata"]["evidence_expires_at"])
        > datetime.now(UTC)
        and stage["binding"] == snapshot(installer),
        "Activated adapter inputs drifted",
    )
    require(
        wait_api(installer)["metadata"]["uid"] == stage["deployment_uid"],
        "Activated API Deployment was replaced",
    )
    report = installer.json(
        installer.kube(
            "exec",
            "-i",
            "deployment/superplane-api",
            "-n",
            installer.env["namespace"],
            "--",
            "python",
            "-m",
            "app.installation",
            "adapter-active",
            data=json.dumps(expected_runtime(installer)),
        )
    )
    require(
        report.get("management_only") is False
        and report.get("dispatch_enabled") is True
        and report.get("paid_admission_enabled") is True
        and report.get("producer_ready") is True
        and report.get("release_id") == installer.release
        and len(report.get("capabilities", {})) == 4
        and all(report["capabilities"].values()),
        "Activated API identity, producer or capability verification failed",
    )
    stage["state"] = "activated-and-verified"
    stage["active_report"] = report
    installer.save()
