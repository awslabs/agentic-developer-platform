"""Switch one paused runner's manager under the protected deployment identity."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

from deployment_guard import check_identity


class ActivationError(ValueError):
    pass


def require(condition):
    if not condition:
        raise ActivationError("manager activation not verified")


def command(args):
    result = subprocess.run(args, capture_output=True, text=True, check=False, timeout=270)
    require(result.returncode == 0)
    return result.stdout


def aws(service, operation, **values):
    return json.loads(command(["aws", service, operation, "--region", os.environ["AWS_REGION"],
                               "--cli-input-json", json.dumps(values), "--output", "json"]))


def selected_group(plan, group_id):
    require(re.fullmatch(r"[1-9][0-9]*", group_id) is not None)
    outputs = plan["planned_values"]["outputs"]["group_namespaces"]["value"]
    require(outputs[group_id]["manager"] == f"gitlab-runner-{group_id}")
    resources = plan["planned_values"]["root_module"]["resources"]
    discovery = [item for item in resources if item["address"] == "terraform_data.discovery"]
    require(len(discovery) == 1)
    cluster = discovery[0]["values"]["input"]["cluster_name"]
    require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}", cluster) is not None)
    return cluster


def promoted_operation(group_id, operation_id, call=aws):
    require(re.fullmatch(r"[1-9][0-9]*", group_id) is not None
            and re.fullmatch(r"[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}", operation_id) is not None)
    record = json.loads(call("ssm", "get-parameter",
                             Name=f"/adp/dev/gitlab-runner-operations/{group_id}",
                             WithDecryption=False)["Parameter"]["Value"])
    require(record.get("group_id") == int(group_id) and record.get("operation_id") == operation_id
            and record.get("secret_version_id") == operation_id and record.get("phase") == "promoted"
            and type(record.get("new_runner_id")) is int and record["new_runner_id"] > 0)
    versions = call("secretsmanager", "describe-secret",
                    SecretId=f"adp/dev/gitlab-runner/{group_id}")["VersionIdsToStages"]
    require([version for version, stages in versions.items() if "AWSCURRENT" in stages] == [operation_id])
    return record["new_runner_id"]


def stamp(value):
    require(isinstance(value, str))
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    require(result.tzinfo is not None)
    return result


def fresh_registration(parameter, runner_id, started, now):
    modified = parameter["LastModifiedDate"]
    if isinstance(modified, str):
        modified = stamp(modified)
    require(isinstance(modified, datetime) and modified.tzinfo is not None
            and started <= modified <= now)
    receipt = json.loads(parameter["Value"])
    require(set(receipt) == {"runner_id", "system_id"} and receipt["runner_id"] == runner_id
            and type(receipt["runner_id"]) is int
            and re.fullmatch(r"r_[0-9a-f]{12}", receipt["system_id"]) is not None)
    return receipt


def recover_terminating_manager(kube, group_id, account):
    """Finish an interrupted shutdown only after the caller proves idle/paused."""
    require(re.fullmatch(r"[1-9][0-9]*", group_id) is not None)
    namespace = f"gitlab-runner-{group_id}"
    pods = kube("get", "pods", "-n", namespace, "-l", "app=gitlab-runner", "-o", "json")["items"]
    terminating = [pod for pod in pods if pod["metadata"].get("deletionTimestamp")]
    if not terminating:
        return
    require(len(pods) == len(terminating) == 1)
    pod = terminating[0]
    metadata = pod["metadata"]
    require(metadata["namespace"] == namespace and not metadata.get("finalizers")
            and re.fullmatch(r"gitlab-runner-[a-z0-9]+-[a-z0-9]+", metadata["name"]) is not None
            and pod["spec"]["serviceAccountName"] == "gitlab-runner")
    sa = kube("get", "serviceaccount", "gitlab-runner", "-n", namespace, "-o", "json")
    require(sa["metadata"]["annotations"].get("eks.amazonaws.com/role-arn")
            == f"arn:aws:iam::{account}:role/adp-dev-gitlab-runner-{group_id}")
    owners = metadata["ownerReferences"]
    require(len(owners) == 1 and owners[0].get("controller") is True
            and owners[0]["kind"] == "ReplicaSet"
            and re.fullmatch(r"gitlab-runner-[a-z0-9]+", owners[0]["name"]) is not None)
    replica = kube("get", "replicaset", owners[0]["name"], "-n", namespace, "-o", "json")
    deployment = kube("get", "deployment", "gitlab-runner", "-n", namespace, "-o", "json")
    require(replica["metadata"]["uid"] == owners[0]["uid"]
            and replica["metadata"]["namespace"] == namespace
            and deployment["metadata"]["namespace"] == namespace
            and deployment["spec"]["strategy"]["type"] == "Recreate"
            and deployment["spec"]["replicas"] == 1)
    parents = replica["metadata"]["ownerReferences"]
    require(len(parents) == 1 and parents[0].get("controller") is True
            and parents[0]["kind"] == "Deployment" and parents[0]["name"] == "gitlab-runner"
            and parents[0]["uid"] == deployment["metadata"]["uid"])
    # Keep graceful deletion and let the API reject any concurrent replacement.
    # Never force-delete, remove finalizers, or delete a non-terminating pod.
    with tempfile.TemporaryDirectory(prefix="adp-manager-shutdown-") as directory:
        body = Path(directory) / "delete-options.json"
        body.write_text(json.dumps({"apiVersion": "v1", "kind": "DeleteOptions", "gracePeriodSeconds": 30,
                                    "preconditions": {"uid": metadata["uid"], "resourceVersion": metadata["resourceVersion"]}}))
        try:
            kube("delete", "--raw", f"/api/v1/namespaces/{namespace}/pods/{metadata['name']}", "-f", str(body))
            kube("wait", "--for=delete", "pod/" + metadata["name"], "-n", namespace, "--timeout=90s")
        except Exception:
            # Graceful shutdown can remove the pod between our ownership read
            # and DELETE/wait. Prove that exact UID is gone; never touch a
            # replacement or conceal a failure while the original remains.
            current = kube("get", "pods", "-n", namespace, "-l", "app=gitlab-runner", "-o", "json")["items"]
            if any(item["metadata"]["uid"] == metadata["uid"] for item in current):
                raise


def rollout(kube, group_id, operation_id, revision, account, runner_id, call=aws, now=None):
    now = now or datetime.now(timezone.utc)
    namespace = f"gitlab-runner-{group_id}"
    ns = kube("get", "namespace", namespace, "-o", "json")
    require(ns["metadata"]["labels"].get("adp-group-id") == group_id)
    sa = kube("get", "serviceaccount", "gitlab-runner", "-n", namespace, "-o", "json")
    require(sa["metadata"]["annotations"].get("eks.amazonaws.com/role-arn")
            == f"arn:aws:iam::{account}:role/adp-dev-gitlab-runner-{group_id}")
    deployment = kube("get", "deployment", "gitlab-runner", "-n", namespace, "-o", "json")
    require(deployment["metadata"]["namespace"] == namespace
            and deployment["spec"]["template"]["spec"]["serviceAccountName"] == "gitlab-runner"
            and deployment["spec"]["replicas"] == 1
            and deployment["spec"]["strategy"]["type"] == "Recreate")
    annotations = deployment["spec"]["template"]["metadata"].get("annotations", {})
    same = (annotations.get("adp.aws-e/runner-operation") == operation_id
            and annotations.get("adp.aws-e/source-revision") == revision)
    started = stamp(annotations["adp.aws-e/rollout-started-at"]) if same else now
    require(started <= now)
    if not same:
        patch = {"metadata": {"resourceVersion": deployment["metadata"]["resourceVersion"]},
                 "spec": {"template": {"metadata": {"annotations": {
                     "adp.aws-e/runner-operation": operation_id, "adp.aws-e/source-revision": revision,
                     "adp.aws-e/rollout-started-at": started.isoformat()}}}}}
        kube("patch", "deployment", "gitlab-runner", "-n", namespace, "--type=merge", "-p", json.dumps(patch))
    kube("rollout", "status", "deployment/gitlab-runner", "-n", namespace, "--timeout=240s")
    current = kube("get", "deployment", "gitlab-runner", "-n", namespace, "-o", "json")
    require(current["metadata"]["uid"] == deployment["metadata"]["uid"]
            and current["status"]["observedGeneration"] >= current["metadata"]["generation"]
            and all(current["status"].get(key) == 1 for key in
                    ("replicas", "updatedReplicas", "readyReplicas", "availableReplicas")))
    active = current["spec"]["template"]["metadata"]["annotations"]
    require(active.get("adp.aws-e/runner-operation") == operation_id
            and active.get("adp.aws-e/source-revision") == revision
            and stamp(active["adp.aws-e/rollout-started-at"]) == started)
    parameter = call("ssm", "get-parameter", Name=f"/adp/dev/gitlab-runners/{group_id}/registration-receipt",
                     WithDecryption=False)["Parameter"]
    return fresh_registration(parameter, runner_id, started, datetime.now(timezone.utc))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("check-plan", "rollout"))
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--group-id", required=True)
    parser.add_argument("--operation-id", required=True)
    args = parser.parse_args()
    revision, account = os.environ["ADP_SOURCE_SHA"], os.environ["EXPECTED_ACCOUNT"]
    require(re.fullmatch(r"[0-9a-f]{40}", revision) is not None
            and re.fullmatch(r"[0-9]{12}", account) is not None)
    check_identity(aws("sts", "get-caller-identity"), os.environ["EXPECTED_ROLE"])
    require(os.environ["EXPECTED_ROLE"].split(":")[4] == account)
    cluster = selected_group(json.loads(args.plan.read_text()), args.group_id)
    runner_id = promoted_operation(args.group_id, args.operation_id)
    if args.mode == "rollout":
        # A fresh kubeconfig prevents accidental use of a different active context.
        with tempfile.TemporaryDirectory(prefix="adp-runner-activation-") as directory:
            config = str(Path(directory) / "kubeconfig")
            command(["aws", "eks", "update-kubeconfig", "--name", cluster,
                     "--region", os.environ["AWS_REGION"], "--kubeconfig", config])

            def kube(*args):
                output = command(["kubectl", "--kubeconfig", config, *args])
                return json.loads(output) if args[-2:] == ("-o", "json") else None

            rollout(kube, args.group_id, args.operation_id, revision, account, runner_id)
    print("Paused manager operation verified" if args.mode == "rollout" else "Promoted operation and group plan verified")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        raise SystemExit("Manager activation refused; retain the paused registration and reconcile this operation") from None
