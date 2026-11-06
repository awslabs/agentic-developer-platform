import copy
import hashlib
import json
import subprocess
from pathlib import Path

import pytest
import yaml

from modules.tools.agentcore.infra.shared_receipt import receipt
from modules.tools.scripts.release_scope import verify_shared_receipt


ROOT = Path(__file__).resolve().parents[4]
ACCOUNT = "123456789012"
REGION = "us-east-1"
SOURCE = "a" * 40
FUNCTION = "example-shared"
DEPLOYMENT = "example-browser"
NAMESPACE = "example-browser-ns"
DEPLOYMENT_UID = "deployment-uid"
REPLICA_SET = "example-browser-abc123"
REPLICA_UID = "replicaset-uid"


def inputs(tmp_path):
    backend = tmp_path / "backend.hcl"
    variables = tmp_path / "private.tfvars"
    backend.write_text('bucket = "fictional-state"\nkey = "approved/tools/agentcore/current.tfstate"\n')
    variables.write_text('aws_account_id = "123456789012"\n')
    repository = f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/"
    return {"BACKEND": str(backend), "TFVARS": str(variables), "STATE_BUCKET": "fictional-state",
            "STATE_KEY": "approved/tools/agentcore/current.tfstate", "SOURCE_SHA": SOURCE,
            "EXPECTED_AWS_ACCOUNT_ID": ACCOUNT, "AWS_REGION": REGION, "TARGET_ENVIRONMENT": "dev",
            "WORKFLOW_REVISION": "d" * 40, "RELEASE_MODE": "apply",
            "GITHUB_RUN_ID": "42", "GITHUB_RUN_ATTEMPT": "2",
            "LAMBDA_REPO": repository + "example_lambda", "BROWSER_REPO": repository + "example-browser",
            "TF_VAR_image_uri": repository + "example_lambda@sha256:" + "b" * 64,
            "TF_VAR_browser_service_image": repository + "example-browser@sha256:" + "c" * 64}


def observer(environment, *, changed=None):
    outputs = {name: {"value": value} for name, value in
               (("lambda_name", FUNCTION), ("browser_deployment_name", DEPLOYMENT),
                ("browser_deployment_namespace", NAMESPACE))}
    results = {
        "aws sts get-caller-identity": ACCOUNT + "\n",
        "terraform output": outputs,
        "aws lambda get-function": {"Configuration": {"FunctionName": FUNCTION, "State": "Active",
                                         "LastUpdateStatus": "Successful"},
                       "Code": {"ResolvedImageUri": environment["TF_VAR_image_uri"]}},
        "kubectl get deployment": {"metadata": {"name": DEPLOYMENT, "namespace": NAMESPACE,
                                                "uid": DEPLOYMENT_UID, "generation": 3},
                                   "spec": {"replicas": 1, "selector": {"matchLabels": {"app.kubernetes.io/name": DEPLOYMENT}},
                                            "template": {"spec": {"containers": [
                                       {"name": "browser", "image": environment["TF_VAR_browser_service_image"]}]}}},
                                   "status": {"observedGeneration": 3, "replicas": 1, "availableReplicas": 1,
                                              "readyReplicas": 1, "updatedReplicas": 1}},
        "kubectl get replicaset": {"metadata": {"name": REPLICA_SET, "namespace": NAMESPACE, "uid": REPLICA_UID,
                                               "ownerReferences": [{"name": DEPLOYMENT, "uid": DEPLOYMENT_UID,
                                                                    "apiVersion": "apps/v1", "kind": "Deployment", "controller": True}]},
                                   "spec": {"template": {"spec": {"containers": [
                                       {"name": "browser", "image": environment["TF_VAR_browser_service_image"]}]}}}},
        "kubectl get pods": {"items": [{"metadata": {"name": "example-browser-pod", "namespace": NAMESPACE,
                                                    "uid": "pod-uid", "labels": {"app.kubernetes.io/name": DEPLOYMENT},
                                                    "ownerReferences": [{"name": REPLICA_SET, "uid": REPLICA_UID,
                                                                         "apiVersion": "apps/v1", "kind": "ReplicaSet", "controller": True}]},
                                         "spec": {"containers": [{"name": "browser", "image": environment["TF_VAR_browser_service_image"]}]},
                                         "status": {"phase": "Running", "containerStatuses": [{
                                             "name": "browser", "ready": True,
                                             "image": environment["TF_VAR_browser_service_image"],
                                             "imageID": "docker-pullable://" + environment["TF_VAR_browser_service_image"]}]}}]},
        "kubectl rollout status": "ready\n",
        "terraform state": b'{"serial": 7}',
    }
    results.update(changed or {})
    calls = []

    def read(*arguments):
        calls.append(arguments)
        key = " ".join(word for word in arguments[:3] if not word.startswith("-chdir="))
        if arguments[0] == "terraform":
            key = "terraform " + arguments[2]
        if arguments[:3] == ("kubectl", "get", "deployment"):
            key = "kubectl get deployment"
        if arguments[:3] == ("kubectl", "get", "pods"):
            key = "kubectl get pods"
        result = results[key]
        return result if isinstance(result, bytes) else (result if isinstance(result, str) else json.dumps(result)).encode()

    return read, calls


def test_apply_receipt_binds_source_config_state_and_two_independent_runtime_reads(tmp_path):
    environment = inputs(tmp_path)
    read, calls = observer(environment)
    evidence = receipt(environment, read)
    assert evidence["mode"] == "apply" and evidence["evidence_type"] == "independent-runtime"
    assert (evidence["source_revision"], evidence["run_id"], evidence["run_attempt"]) == (SOURCE, 42, 2)
    assert evidence["workflow_revision"] == "d" * 40
    assert evidence["config"]["tfvars_sha256"] == hashlib.sha256(Path(environment["TFVARS"]).read_bytes()).hexdigest()
    assert evidence["state_sha256"] == hashlib.sha256(b'{"serial": 7}').hexdigest()
    assert evidence["images"]["lambda"]["runtime"] == environment["TF_VAR_image_uri"]
    assert evidence["images"]["browser"]["runtime"] == environment["TF_VAR_browser_service_image"]
    assert any(call[:3] == ("aws", "lambda", "get-function") for call in calls)
    assert any(call[:3] == ("kubectl", "get", "pods") for call in calls)
    assert ("kubectl", "get", "replicaset", REPLICA_SET, "-n", NAMESPACE, "-o", "json") in calls
    assert {call[:3] for call in calls if call[0] == "kubectl"} == {
        ("kubectl", "rollout", "status"), ("kubectl", "get", "deployment"),
        ("kubectl", "get", "pods"), ("kubectl", "get", "replicaset"),
    }
    assert environment["STATE_KEY"] not in json.dumps(evidence)
    assert Path(environment["TFVARS"]).read_text() not in json.dumps(evidence)
    receipt_path = tmp_path / "shared-receipt.json"
    receipt_path.write_text(json.dumps(evidence))
    assert verify_shared_receipt(receipt_path, source=SOURCE, environment="dev", account=ACCOUNT) == evidence



@pytest.mark.parametrize("change,expected", [
    ({"aws lambda get-function": {"Configuration": {"FunctionName": FUNCTION, "State": "Active",
                                         "LastUpdateStatus": "Successful"}, "Code": {"ResolvedImageUri": "old-image"}}}, "Lambda"),
    ({"kubectl get pods": {"items": []}}, "Browser"),
    ({"kubectl get pods": {"items": [{"status": {"phase": "Running", "containerStatuses": []}}]}}, "Browser"),
    ({"terraform state": b""}, "state"),
])
def test_missing_or_mixed_runtime_cannot_claim_success(tmp_path, change, expected):
    environment = inputs(tmp_path)
    read, _ = observer(environment, changed=change)
    with pytest.raises(ValueError, match=expected):
        receipt(environment, read)

@pytest.mark.parametrize("release_mode", ["plan", ""])
def test_plan_mode_rejects_receipt_before_any_provider_read(tmp_path, release_mode):
    environment = inputs(tmp_path)
    environment["RELEASE_MODE"] = release_mode
    with pytest.raises(ValueError, match="protected shared apply"):
        receipt(environment, lambda *arguments: pytest.fail("plan contacted runtime provider"))


def test_plan_only_workflow_cannot_write_applied_receipt():
    workflow = yaml.load((ROOT / ".github/workflows/shared-agentcore-tools-deploy.yml").read_text(),
                         Loader=yaml.BaseLoader)
    steps = workflow["jobs"]["tools"]["steps"]
    apply = next(index for index, step in enumerate(steps) if step.get("name") == "Explicitly apply the reviewed saved plan")
    receipt_step = steps[apply + 1]
    assert receipt_step["if"] == "inputs.mode == 'apply'"
    assert "shared_receipt.py" in receipt_step["run"]
    assert "--if-none-match '*'" in receipt_step["run"]
    assert "codebuild/artifacts/shared-agentcore-tools/" in receipt_step["run"]
    ci = (ROOT / ".github/workflows/tools-ci.yml").read_text()
    assert "python -m pytest -q modules/tools/agentcore/tests/test_shared_receipt.py" in ci


def test_browser_pod_different_runtime_digest_blocks_receipt(tmp_path):
    environment = inputs(tmp_path)
    read, _ = observer(environment)

    def changed_image(*arguments):
        observation = read(*arguments)
        if arguments[:3] == ("kubectl", "get", "pods"):
            pods = json.loads(observation)
            pods["items"][0]["status"]["containerStatuses"][0]["imageID"] = "containerd://sha256:" + "d" * 64
            return json.dumps(pods).encode()
        return observation


    with pytest.raises(ValueError, match="Browser pod runtime image"):
        receipt(environment, changed_image)


def alter_observation(read, resource, mutate):
    def changed(*arguments):
        result = read(*arguments)
        if arguments[:3] == ("kubectl", "get", resource):
            value = json.loads(result)
            mutate(value)
            return json.dumps(value).encode()
        return result
    return changed


@pytest.mark.parametrize("image_id_prefix", ["", "docker-pullable://"])
def test_containerd_config_digest_is_accepted_only_with_exact_spec_and_resolved_image(tmp_path, image_id_prefix):
    environment = inputs(tmp_path)
    read, _ = observer(environment)

    def normalized(pods):
        container = pods["items"][0]["status"]["containerStatuses"][0]
        container["image"] = "sha256:" + "f" * 64
        container["imageID"] = image_id_prefix + environment["TF_VAR_browser_service_image"]

    evidence = receipt(environment, alter_observation(read, "pods", normalized))
    assert evidence["images"]["browser"]["runtime"] == environment["TF_VAR_browser_service_image"]


@pytest.mark.parametrize("resource,mutate", [
    ("pods", lambda v: v["items"][0]["status"]["containerStatuses"][0].update(
        imageID="123456789012.dkr.ecr.us-east-1.amazonaws.com/foreign@sha256:" + "c" * 64)),
    ("pods", lambda v: v["items"][0]["status"]["containerStatuses"][0].update(imageID="sha256:" + "c" * 64)),
    ("pods", lambda v: v["items"][0]["status"]["containerStatuses"][0].update(imageID="")),
    ("pods", lambda v: v["items"][0]["spec"]["containers"][0].update(image="foreign@sha256:" + "c" * 64)),
    ("pods", lambda v: v["items"][0]["spec"]["containers"][0].update(name="foreign")),
    ("pods", lambda v: v["items"][0]["metadata"].update(namespace="foreign")),
    ("pods", lambda v: v["items"][0]["metadata"]["labels"].clear()),
    ("pods", lambda v: v["items"][0]["metadata"]["ownerReferences"][0].update(uid="foreign")),
    ("pods", lambda v: v["items"][0]["metadata"]["ownerReferences"][0].update(kind="Job")),
    ("pods", lambda v: v["items"][0]["metadata"]["ownerReferences"][0].update(apiVersion="foreign/v1")),
    ("pods", lambda v: v["items"][0]["metadata"]["ownerReferences"][0].update(controller=False)),
    ("pods", lambda v: v["items"][0]["metadata"]["ownerReferences"].append(
        copy.deepcopy(v["items"][0]["metadata"]["ownerReferences"][0]))),
    ("pods", lambda v: v["items"].append(copy.deepcopy(v["items"][0]))),
    ("pods", lambda v: v["items"][0]["status"]["containerStatuses"][0].update(ready=False)),
    ("replicaset", lambda v: v["metadata"].update(uid="foreign")),
    ("replicaset", lambda v: v["metadata"].update(namespace="foreign")),
    ("replicaset", lambda v: v["metadata"]["ownerReferences"][0].update(name="foreign")),
    ("replicaset", lambda v: v["metadata"]["ownerReferences"][0].update(uid="foreign")),
    ("replicaset", lambda v: v["metadata"]["ownerReferences"][0].update(apiVersion="foreign/v1")),
    ("replicaset", lambda v: v["spec"]["template"]["spec"]["containers"][0].update(image="old-image")),
    ("deployment", lambda v: v["metadata"].update(uid="")),
    ("deployment", lambda v: v["spec"].update(selector={"matchLabels": {}})),
    ("deployment", lambda v: v["status"].update(availableReplicas=0)),
    ("deployment", lambda v: v["status"].update(observedGeneration=2)),
])
def test_browser_identity_image_and_owner_negative_controls(tmp_path, resource, mutate):
    environment = inputs(tmp_path)
    read, _ = observer(environment)
    with pytest.raises(ValueError, match="Browser"):
        receipt(environment, alter_observation(read, resource, mutate))


def test_missing_replicaset_read_permission_cannot_publish_receipt(tmp_path):
    environment = inputs(tmp_path)
    read, _ = observer(environment)

    def denied(*arguments):
        if arguments[:3] == ("kubectl", "get", "replicaset"):
            raise subprocess.CalledProcessError(1, arguments, stderr=b"Forbidden")
        return read(*arguments)

    with pytest.raises(subprocess.CalledProcessError):
        receipt(environment, denied)
