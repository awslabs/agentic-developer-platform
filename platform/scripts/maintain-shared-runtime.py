#!/usr/bin/env python3
"""Manual, account-specific shared-runtime maintenance; never a whole-module apply.

Stages do not accept flows or dispatch workers. The normal tick starts the CLI
canary after its policy is accepted separately. Plans and secrets stay in private temporary storage;
only addresses, pinned images, non-secret overlays and verification are retained.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

from shared_runtime_plan_guard import (
    ACCOUNT, FLAGS, KEY_NAME, REGION, ROLE, TARGETS, WIRING_FLAGS, Refused, check_iam, check_other_work, check_plan, preserved_tick_inputs, require,
)

ROOT = Path(__file__).resolve().parents[2]
WEBHOOK = ROOT / "modules/agent-factory/webhook-ingress/infra"
GATEWAY = ROOT / "modules/gateway/infra"
CLUSTER = "adp-dev-eks-cluster"
NAMESPACE = "adp-gateway"
DEPLOYMENT = "bedrockgateway"
TICK = "adp-dev-orchestration-tick"
WIRING = "/adp/dev/webhook-ingress/worker-runtime/wiring"
LOCK_ID = "387a6df1-7b5c-f833-9334-305429bfdac4"
LOCK_OWNER = "runner@arc-runner-org-wxsd2-runner-d7kth"
LOCK_PATH = f"adp-terraform-state-{ACCOUNT}/dev/modules/webhook-ingress/terraform.tfstate"
AWS_JSON_SOURCES = {
    ("sts", "get-caller-identity"): "aws.sts.identity",
    ("ecr", "describe-images"): "aws.ecr.images",
    ("ssm", "get-parameter"): "aws.ssm.parameter",
    ("lambda", "get-function"): "aws.lambda.function",
    ("lambda", "get-function-configuration"): "aws.lambda.configuration",
    ("sqs", "get-queue-attributes"): "aws.sqs.attributes",
    ("iam", "get-role-policy"): "aws.iam.role_policy",
    ("dynamodb", "get-item"): "aws.dynamodb.lock_item",
}
JSON_SOURCES = frozenset(AWS_JSON_SOURCES.values()) | {
    "kubernetes.snapshot", "gateway.sql_probe", "kubernetes.gateway_pods",
    "parameter.worker_runtime_wiring", "kubernetes.worker_pods", "dynamodb.terraform_lock_info",
    "github.orphan_job", "kubernetes.orphan_owner_pods", "terraform.saved_plan",
}


def decode_json(value, *, source):
    require(source in JSON_SOURCES, "unregistered JSON source")
    try:
        return json.loads(value)
    except json.JSONDecodeError as error:
        # Only a static call-site label travels out. The exception's document,
        # message, arguments and provider output remain private.
        error.maintenance_json_source = source
        raise


def failure_location(error):
    frames = []
    cursor = error.__traceback__
    while cursor is not None:
        code = cursor.tb_frame.f_code
        frames.append({"filename": Path(code.co_filename).name, "function": code.co_name, "lineno": cursor.tb_lineno})
        cursor = cursor.tb_next
    source = getattr(error, "maintenance_json_source", None)
    return {"failure_type": type(error).__name__, "json_source": source if isinstance(source, str) and source in JSON_SOURCES else None, "locations": frames}


def command(args, *, stdin=None, cwd=None):
    result = subprocess.run(args, input=stdin, text=True, capture_output=True, cwd=cwd, check=False)
    if result.returncode:
        # Terraform and provider failures can include sensitive values. Never
        # forward raw stdout/stderr from this privileged maintenance operation.
        raise Refused(f"{Path(args[0]).name} command failed; no raw output emitted (exit {result.returncode})")
    return result.stdout


def aws(*args):
    source = AWS_JSON_SOURCES.get(args[:2])
    require(source is not None, "unregistered AWS JSON source")
    return decode_json(command(["aws", *args, "--region", REGION, "--output", "json"]), source=source)


def kube(*args, stdin=None, request_timeout="30s"):
    return command(["kubectl", f"--request-timeout={request_timeout}", *args], stdin=stdin)


def snapshot(kind, name, namespace=NAMESPACE):
    return decode_json(kube("get", kind, name, "-n", namespace, "-o", "json"), source="kubernetes.snapshot")


def parameter(name, *, decrypt=False):
    flags = ["--with-decryption"] if decrypt else []
    return aws("ssm", "get-parameter", "--name", name, *flags)["Parameter"]["Value"]


def private_json(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")
    path.chmod(0o600)


def checked_image(repository, revision, expected_digest=None):
    require(re.fullmatch(r"[0-9a-f]{40}", revision), "image revision must be a full reviewed commit SHA")
    images = aws("ecr", "describe-images", "--repository-name", repository, "--image-ids", f"imageTag={revision}")["imageDetails"]
    require(len(images) == 1, "image revision is ambiguous")
    digest = images[0]["imageDigest"]
    require(re.fullmatch(r"sha256:[0-9a-f]{64}", digest), "image digest is invalid")
    require(expected_digest is None or digest == expected_digest, "worker digest does not match approved source revision")
    return f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/{repository}@{digest}"


def gateway_probe():
    secret = snapshot("secret", "agent-authority-signing")
    expected_key_hash = hashlib.sha256(base64.b64decode(secret["data"]["run-credential-key"])).hexdigest()
    code = """import asyncio,hashlib,hmac,json,os,sys
from sqlalchemy import text
from src.shared.database import get_session_factory
from src.orchestration.dispatch_pass import routing_blocker_for_node, resolve_installation_id, _latest_approval_decision_id
from src.orchestration.genesis import GenesisRefusedError, resolve_engine_genesis
async def dispatch_preflight(s,row):
 routing=routing_blocker_for_node(kind='story',issue_ref=row['issue_ref'])
 installation=await resolve_installation_id(s,org_id=row['org_id'])
 approval=await _latest_approval_decision_id(s,org_id=row['org_id'],flow_id=row['flow_id'])
 verified=False
 if approval is not None:
  try:
   genesis=await resolve_engine_genesis(s,org_id=row['org_id'],decision_id=approval)
   verified=genesis.flow_id==row['flow_id']
  except GenesisRefusedError:
   pass
 return {'contract':'shared-runtime-dispatch-preflight/v1','routing_blocker':routing.value if routing else None,'installation_id':installation,'approval_decision_id':approval,'human_approval_verified':verified}
async def probe():
 async with get_session_factory()() as s:
  await s.execute(text('SET TRANSACTION READ ONLY'))
  version=await s.scalar(text('SELECT version_num FROM alembic_version'))
  shared=await s.scalar(text("SELECT count(*) FROM orchestration_accepted_plans WHERE superseded_at IS NULL AND plan_document -> 'execution_continuation' ->> 'mode' = 'shared_worker_role'"))
  pending=await s.scalar(text("SELECT count(*) FROM orchestration_run_reports WHERE terminal_receipt IS NULL OR terminal_receipt = 'null'::jsonb"))
  executions=await s.scalar(text("SELECT count(*) FROM orchestration_executions WHERE status NOT IN ('concluded','superseded')"))
  actions=await s.scalar(text("SELECT count(*) FROM orchestration_actions WHERE status IN ('prepared','dispatched','unknown')"))
  authoring=await s.scalar(text("SELECT count(*) FROM orchestration_amendment_requests WHERE state IN ('queued','dispatched')"))
  other_ready=await s.scalar(text("SELECT count(*) FROM orchestration_nodes WHERE kind='story' AND state IN ('ready','running') AND flow_id NOT IN ('0737183c-99c4-4e1f-bdb7-e4432b46ca20','a555da26-2724-4f38-b960-8738e10fa88c')"))
  other_nodes=await s.execute(text("SELECT n.org_id, n.flow_id, f.state AS flow_state, n.id AS node_id, n.state, n.attempts, n.issue_ref FROM orchestration_nodes n LEFT JOIN orchestration_flows f ON f.id=n.flow_id AND f.org_id=n.org_id WHERE n.kind='story' AND n.state IN ('ready','running') AND n.flow_id NOT IN ('0737183c-99c4-4e1f-bdb7-e4432b46ca20','a555da26-2724-4f38-b960-8738e10fa88c') ORDER BY n.org_id, n.flow_id, n.id LIMIT 20"))
  other_nodes=[dict(row._mapping) for row in other_nodes]
  for row in other_nodes:
   row['dispatch_preflight']=await dispatch_preflight(s,row)
  key=os.environ.get('AGENT_RUN_CREDENTIAL_KEY','')
  print(json.dumps({'schema':version,'key_present':bool(key),'key_matches':hmac.compare_digest(hashlib.sha256(key.encode()).hexdigest(),sys.argv[1]),'shared_continuations':shared,'unfinished_report_assignments':pending,'unfinished_executions':executions,'unresolved_actions':actions,'pending_authoring':authoring,'other_ready_or_running_stories':other_ready,'other_ready_or_running_nodes':other_nodes,'other_ready_or_running_nodes_truncated':other_ready>20,'flags':{k:os.environ.get(k) for k in ['AGENT_AUTHORITY_ENABLED','ADP_SHARED_RUN_REPORTING_ENABLED','ADP_SHARED_WORKER_CONTINUATION_ENABLED','AGENT_WORKER_ROLE_ARN']}}))
asyncio.run(probe())
"""
    result = decode_json(kube("exec", "-i", f"deployment/{DEPLOYMENT}", "-n", NAMESPACE, "-c", DEPLOYMENT, "--", "python", "-", expected_key_hash, stdin=code), source="gateway.sql_probe")
    require(result.get("key_matches") is True, "running gateway key differs from current signing Secret")
    return result


def preflight(args, scratch):
    identity = aws("sts", "get-caller-identity")
    require(args.account_id == ACCOUNT == identity["Account"], "incorrect target account")
    print(json.dumps({"account_id": identity["Account"], "principal": identity["Arn"], "stage": args.stage}))
    os.environ["KUBECONFIG"] = str(scratch / "kubeconfig")
    command(["aws", "eks", "update-kubeconfig", "--name", CLUSTER, "--region", REGION, "--kubeconfig", os.environ["KUBECONFIG"]])
    gateway_image = checked_image("adp-gateway", args.gateway_revision)
    worker_image = checked_image("adp-agent-runtime", args.worker_revision, args.worker_digest)
    deployment = snapshot("deployment", DEPLOYMENT)
    container = next(c for c in deployment["spec"]["template"]["spec"]["containers"] if c["name"] == DEPLOYMENT)
    accepted_images = {gateway_image, gateway_image.split("@")[0] + ":" + args.gateway_revision}
    require(container["image"] in accepted_images, "gateway deployment is not the reviewed release")
    require(any(ref.get("configMapRef", {}).get("name") == "adp-worker-authority-config" for ref in container.get("envFrom", [])), "gateway does not consume worker ConfigMap")
    require(any(e.get("name") == "AGENT_RUN_CREDENTIAL_KEY" and e.get("valueFrom", {}).get("secretKeyRef", {}).get("name") == "agent-authority-signing" and e.get("valueFrom", {}).get("secretKeyRef", {}).get("key") == "run-credential-key" for e in container.get("env", [])), "gateway signing-key reference changed")
    selectors = ",".join(f"{k}={v}" for k, v in sorted(deployment["spec"]["selector"]["matchLabels"].items()))
    pods = decode_json(kube("get", "pods", "-n", NAMESPACE, "-l", selectors, "-o", "json"), source="kubernetes.gateway_pods")["items"]
    ready = [p for p in pods if not p["metadata"].get("deletionTimestamp")]
    require(len(ready) == deployment["spec"]["replicas"] > 0, "gateway rollout incomplete")
    for pod in ready:
        statuses = [s for s in pod.get("status", {}).get("containerStatuses", []) if s["name"] == DEPLOYMENT]
        require(len(statuses) == 1 and statuses[0]["ready"] and statuses[0]["imageID"].endswith(gateway_image), "gateway pod image/readiness differs")
    tick = aws("lambda", "get-function", "--function-name", TICK)
    require(tick["Code"]["ResolvedImageUri"] == gateway_image, "tick is not running the reviewed gateway image")
    require(tick["Configuration"].get("State") == "Active" and tick["Configuration"].get("LastUpdateStatus") == "Successful", "tick update incomplete")
    tick_env = tick["Configuration"]["Environment"]["Variables"]
    require(tick_env.get("AGENT_AUTHORITY_ENABLED", "false") == "false", "tick protected authority is enabled")
    config = snapshot("configmap", "adp-worker-authority-config")["data"]
    require(config.get("AGENT_AUTHORITY_ENABLED") == "false", "gateway protected authority is enabled")
    wiring = decode_json(parameter(WIRING), source="parameter.worker_runtime_wiring")
    worker = snapshot("scaledjob", "agent-scaledjob", "adp-agents")
    podspec = worker["spec"]["jobTargetRef"]["template"]["spec"]
    require(podspec["serviceAccountName"] == "agent-scaledjob-sa", "shared worker service account changed")
    service_account = snapshot("serviceaccount", "agent-scaledjob-sa", "adp-agents")
    require(service_account["metadata"].get("annotations", {}).get("eks.amazonaws.com/role-arn") == ROLE, "shared worker service account IAM role changed")
    live_worker = next(c for c in podspec["containers"] if c["name"] == "agent-worker")
    require(not any(e["name"] in {"ADP_AGENT_AUTHORITY_ENABLED", "AGENT_AUTHORITY_ENABLED"} and e.get("value") == "true" for e in live_worker.get("env", [])), "worker protected authority enabled")
    require(any(e["name"] == "AGENT_RUN_LOGS_BUCKET" and e.get("value") == f"adp-dev-agent-run-logs-{ACCOUNT}" for e in live_worker.get("env", [])), "existing worker reporting bucket is missing")
    probe = gateway_probe()
    require(probe["schema"] == "064_orchestration_run_reports" and probe["key_present"], "gateway report migration/key is unavailable")
    require(probe["flags"]["AGENT_AUTHORITY_ENABLED"] == "false", "running gateway protected authority enabled")
    for values in (tick_env, config, probe["flags"]):
        require(values.get("AGENT_WORKER_ROLE_ARN") in {None, "", ROLE}, "runtime shared worker role differs")
    if args.stage in {"prerequisites", "worker", "gateway-enable"}:
        require(all(tick_env.get(flag, "false") == "false" for flag in FLAGS), "tick is already enabled; this preparation stage would regress runtime")
        require(all(wiring.get(flag, False) is False for flag in WIRING_FLAGS), "tick wiring is already enabled")
    if args.stage in {"prerequisites", "worker"}:
        require(all(config.get(flag, "false") == "false" for flag in FLAGS), "gateway is already enabled; preparation would regress runtime")
    if args.stage in {"gateway-enable", "tick-enable", "verify"}:
        require(live_worker["image"] == worker_image, "approved worker image is not live")
        env = {e["name"]: e.get("value") for e in live_worker.get("env", [])}
        require(env.get("GITLAB_URL") == parameter("/adp/dev/gitlab/url").rstrip("/"), "worker GitLab wiring differs")
        require(env.get("ADP_AGENT_CONTROL_ENDPOINT") == parameter("/adp/dev/gateway/apigw-invoke-url").rstrip("/") + "/internal/v1/agent", "worker control endpoint differs")
    if args.stage == "tick-enable":
        require(all(config.get(flag) == "true" and probe["flags"].get(flag) == "true" for flag in FLAGS), "gateway canary stage is incomplete")
    queue = aws("sqs", "get-queue-attributes", "--queue-url", wiring["dispatch_queue_url"], "--attribute-names", "ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible", "ApproximateNumberOfMessagesDelayed")["Attributes"]
    worker_pods = decode_json(kube("get", "pods", "-n", "adp-agents", "-o", "json"), source="kubernetes.worker_pods")["items"]
    active_images = [c["image"] for pod in worker_pods if pod.get("status", {}).get("phase") not in {"Succeeded", "Failed"} for c in pod["spec"].get("containers", []) if c["name"] == "agent-worker"]
    print(json.dumps({"runtime_preflight": probe, "queue": queue, "active_worker_images": active_images}))
    if args.stage in {"gateway-enable", "tick-enable"}:
        require(all(probe[k] == 0 for k in ("shared_continuations", "unfinished_report_assignments", "unfinished_executions", "unresolved_actions", "pending_authoring")), "existing flows or outbox require separate review before global shared activation")
        check_other_work(probe)
        require(all(int(v) == 0 for v in queue.values()), "shared dispatch queue is not empty; let existing work drain")
        require(all(image == worker_image for image in active_images), "incompatible worker is still active; let it finish before activation")
    if args.stage == "verify":
        require(all(values.get(flag) == "true" for values in (tick_env, config, probe["flags"]) for flag in FLAGS), "full runtime verification requires enabled gateway and tick")
        require(all(values.get("AGENT_WORKER_ROLE_ARN") == ROLE for values in (tick_env, config, probe["flags"])), "full runtime verification requires the exact shared role")
        require(all(wiring.get(flag) is True for flag in WIRING_FLAGS), "shared runtime wiring is not enabled")
        require(all(image == worker_image for image in active_images), "incompatible active worker image remains")
    secret = snapshot("secret", "agent-authority-signing")
    signing_key = base64.b64decode(secret["data"]["run-credential-key"]).decode()
    require(bool(signing_key), "gateway signing key is empty")
    if args.stage in {"gateway-enable", "tick-enable", "verify"}:
        require(parameter(KEY_NAME, decrypt=True) == signing_key, "reporting key differs from gateway key")
    policy = aws("iam", "get-role-policy", "--role-name", TICK + "-role", "--policy-name", TICK + "-policy")["PolicyDocument"]
    overlay = preserved_tick_inputs(tick["Configuration"], policy, wiring, revision=args.gateway_revision)
    if args.stage in {"gateway-enable", "tick-enable", "verify"}:
        require(tick_env.get("AGENT_RUN_CREDENTIAL_KEY_PARAMETER") == KEY_NAME and tick_env.get("AGENT_WORKER_ROLE_ARN") == ROLE, "tick reporting prerequisites are incomplete")
        check_iam({"policy": json.dumps(policy)}, {"policy": json.dumps(policy)})
    return {"gateway_image": gateway_image, "worker_image": worker_image, "worker": worker, "wiring": wiring, "config": config, "signing_key": signing_key, "kms_key": wiring["webhook_events_kms_key_arn"], "tick_overlay": overlay, "probe": probe, "queue": queue, "active_worker_images": active_images}


def init(module):
    name = "webhook-ingress" if module == WEBHOOK else "gateway"
    command(["terraform", "init", "-input=false", "-reconfigure", f"-backend-config={ROOT}/environments/dev/modules/{name}-backend.tfvars", f"-backend-config=bucket=adp-terraform-state-{ACCOUNT}"], cwd=module)


def lock_info():
    # The CLI emits no JSON for a successful missing-item response. Project a
    # stable object instead of treating an arbitrary empty body as an unlocked
    # backend. Malformed output still stops maintenance before any unlock/plan.
    response = aws("dynamodb", "get-item", "--table-name", "adp-terraform-locks", "--key", json.dumps({"LockID": {"S": LOCK_PATH}}), "--consistent-read", "--query", "{Item: Item}")
    require(isinstance(response, dict) and "Item" in response, "lock lookup response is incomplete")
    item = response["Item"]
    if item is None:
        return None
    require(isinstance(item, dict) and isinstance(item.get("Info"), dict) and isinstance(item["Info"].get("S"), str), "lock lookup item is malformed")
    info = decode_json(item["Info"]["S"], source="dynamodb.terraform_lock_info")
    require(isinstance(info, dict), "lock ownership information is malformed")
    return info


def unlock_known_orphan():
    info = lock_info()
    if info is None:
        print("Known backend is unlocked; no unlock performed")
        return
    require(info.get("ID") == LOCK_ID and info.get("Who") == LOCK_OWNER and info.get("Operation") == "OperationTypePlan" and info.get("Path") == LOCK_PATH and info.get("Created") == "2026-09-20T23:34:38.654494254Z", "lock differs from reviewed orphan")
    job = decode_json(command(["gh", "api", "repos/aws-e/adp/actions/jobs/106168875014"]), source="github.orphan_job")
    require(job.get("run_id") == 35544870165 and job.get("status") == "completed" and job.get("conclusion") == "cancelled" and job.get("runner_name") == LOCK_OWNER.split("@", 1)[1] and job.get("runner_id") == 76719 and job.get("completed_at"), "lock owner job is not the reviewed cancelled job")
    log = command(["gh", "api", "repos/aws-e/adp/actions/jobs/106168875014/logs"])
    require("Terminate orphan process: pid (258) (terraform)" in log, "orphan process cleanup evidence unavailable")
    pods = decode_json(kube("get", "pods", "--all-namespaces", "-o", "json"), source="kubernetes.orphan_owner_pods")["items"]
    require(not any(p["metadata"]["name"] == job["runner_name"] for p in pods), "lock owner runner pod still exists")
    require(lock_info() == info, "lock changed during ownership verification")
    command(["terraform", "force-unlock", "-force", LOCK_ID], cwd=WEBHOOK)
    require(lock_info() is None, "known orphan lock was not removed")
    print("Removed only the verified cancelled-job orphan lock")


def plan_apply(module, stage, context, overlay, scratch, *, enabled, execute):
    name = "webhook-ingress" if module == WEBHOOK else "gateway"
    path = scratch / f"{stage}.tfvars.json"
    private_json(path, overlay)
    saved = scratch / f"{stage}.tfplan"
    targets = sorted(TARGETS[stage])
    if stage == "tick":
        targets += ["data.terraform_remote_state.platform", "data.aws_ssm_parameters_by_path.worker_runtime"]
    command(["terraform", "plan", "-input=false", "-lock-timeout=30s", f"-var-file={ROOT}/environments/dev/modules/{name}.tfvars", f"-var-file={path}", f"-out={saved}", *[f"-target={target}" for target in targets]], cwd=module)
    saved.chmod(0o600)
    plan = decode_json(command(["terraform", "show", "-json", str(saved)], cwd=module), source="terraform.saved_plan")
    changes = check_plan(plan, stage=stage, enabled=enabled, gateway_image=context["gateway_image"], signing_key=context["signing_key"], kms_key=context["kms_key"])
    digest = hashlib.sha256(saved.read_bytes()).hexdigest()
    print(json.dumps({"stage": stage, "plan_sha256": digest, "changes": changes, "apply": execute}))
    if execute:
        require(hashlib.sha256(saved.read_bytes()).hexdigest() == digest, "saved plan changed after validation")
        command(["terraform", "apply", "-input=false", "-lock-timeout=30s", str(saved)], cwd=module)


def patch_worker(context, scratch, *, execute):
    spec = importlib.util.spec_from_file_location("worker_rollout", WEBHOOK.parent / "scripts/plan-shared-worker-rollout.py")
    generator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(generator)
    patch, overlay = generator.prepare(context["worker"], account_id=ACCOUNT, image=context["worker_image"], control_endpoint=parameter("/adp/dev/gateway/apigw-invoke-url").rstrip("/") + "/internal/v1/agent", gitlab_url=parameter("/adp/dev/gitlab/url"))
    path = scratch / "worker.patch.json"
    private_json(path, patch)
    print(json.dumps({"worker_patch_operations": len(patch), "worker_image": context["worker_image"], "apply": execute}))
    if execute:
        kube("patch", "scaledjob", "agent-scaledjob", "-n", "adp-agents", "--type=json", "--patch-file", str(path))
        after = snapshot("scaledjob", "agent-scaledjob", "adp-agents")
        remaining, _ = generator.prepare(after, account_id=ACCOUNT, image=context["worker_image"], control_endpoint=parameter("/adp/dev/gateway/apigw-invoke-url").rstrip("/") + "/internal/v1/agent", gitlab_url=parameter("/adp/dev/gitlab/url"))
        require(all(op["op"] == "test" for op in remaining), "worker patch did not converge")
    return overlay


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--gateway-revision", required=True)
    parser.add_argument("--worker-revision", required=True)
    parser.add_argument("--worker-digest", required=True)
    parser.add_argument("--stage", choices=("verify", "prerequisites", "worker", "gateway-enable", "tick-enable"), required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--unlock-known-orphan", action="store_true")
    parser.add_argument("--evidence-directory", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    os.environ["AWS_PAGER"] = ""
    os.environ["TF_IN_AUTOMATION"] = "true"
    require(not args.unlock_known_orphan or args.stage == "prerequisites" and args.execute, "orphan unlock is restricted to explicit prerequisite execution")
    with tempfile.TemporaryDirectory(prefix="adp-shared-runtime-") as directory:
        scratch = Path(directory)
        context = preflight(args, scratch)
        enabled = args.stage in {"gateway-enable", "tick-enable"}
        overlay = {"agent_image": context["worker_image"], "gitlab_webhook_enabled": True, "agent_authority_enabled": False, "shared_run_reporting_enabled": enabled, "shared_worker_continuation_enabled": enabled}
        if args.stage == "prerequisites":
            init(WEBHOOK)
            if args.unlock_known_orphan:
                unlock_known_orphan()
            plan_apply(WEBHOOK, "prerequisites", context, overlay, scratch, enabled=False, execute=args.execute)
            if args.execute:
                require(parameter(KEY_NAME, decrypt=True) == context["signing_key"], "created reporting key differs from gateway key")
                init(GATEWAY)
                plan_apply(GATEWAY, "tick", context, context["tick_overlay"], scratch, enabled=False, execute=True)
        elif args.stage == "worker":
            patch_worker(context, scratch, execute=args.execute)
        elif args.stage == "gateway-enable":
            require(parameter(KEY_NAME, decrypt=True) == context["signing_key"], "reporting key differs from gateway key")
            init(WEBHOOK)
            plan_apply(WEBHOOK, "gateway-enable", context, overlay, scratch, enabled=True, execute=args.execute)
            if args.execute:
                # A retry after successful activation should verify the existing
                # rollout, not trigger another restart of already-enabled pods.
                if not all(context["probe"]["flags"].get(flag) == "true" for flag in FLAGS):
                    kube("rollout", "restart", f"deployment/{DEPLOYMENT}", "-n", NAMESPACE)
                # The per-request timeout is independent of the rollout watch's
                # deadline; give this one long watch its full bounded window.
                kube("rollout", "status", f"deployment/{DEPLOYMENT}", "-n", NAMESPACE, "--timeout=300s", request_timeout="330s")
                activated = gateway_probe()
                require(all(activated["flags"].get(flag) == "true" for flag in FLAGS), "gateway flags did not activate")
                require(all(decode_json(parameter(WIRING), source="parameter.worker_runtime_wiring").get(flag) is False for flag in WIRING_FLAGS), "gateway stage changed tick wiring")
        elif args.stage == "tick-enable":
            require(parameter(KEY_NAME, decrypt=True) == context["signing_key"], "reporting key differs from gateway key")
            init(WEBHOOK)
            plan_apply(WEBHOOK, "tick-wiring", context, overlay, scratch, enabled=True, execute=args.execute)
            if args.execute:
                init(GATEWAY)
                plan_apply(GATEWAY, "tick", context, context["tick_overlay"], scratch, enabled=True, execute=True)
                env = aws("lambda", "get-function-configuration", "--function-name", TICK)["Environment"]["Variables"]
                require(all(env.get(flag) == "true" for flag in FLAGS), "tick flags did not activate")
        evidence = args.evidence_directory
        evidence.mkdir(parents=True, exist_ok=True)
        if args.stage != "verify":
            # Gateway-only enable intentionally leaves SSM/tick false. Retain
            # only worker inputs until the final stage establishes one value.
            retained = {"agent_image": context["worker_image"], "gitlab_webhook_enabled": True}
            if args.stage == "tick-enable" and args.execute:
                retained.update({"agent_authority_enabled": False, "shared_run_reporting_enabled": True, "shared_worker_continuation_enabled": True})
            private_json(evidence / f"account-{ACCOUNT}-worker-runtime.tfvars.json", retained)
            private_json(evidence / f"account-{ACCOUNT}-tick-runtime.tfvars.json", context["tick_overlay"])
        private_json(evidence / "verification.json", {"stage": args.stage, "executed": args.execute, "account_id": ACCOUNT, "gateway_image": context["gateway_image"], "worker_image": context["worker_image"], "protected_authority_enabled": False, "preflight": context["probe"], "queue": context["queue"], "active_worker_images": context["active_worker_images"]})
        print(f"Stage {args.stage} complete; sanitized evidence and account-specific inputs retained")


def entrypoint():
    try:
        main()
    except Exception as error:
        # Do not use traceback formatting: it can include source lines, chained
        # exception messages and privileged values. Walk only code locations.
        print(json.dumps({"maintenance_stopped": failure_location(error)}), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(entrypoint())
