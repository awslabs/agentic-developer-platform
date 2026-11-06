"""Read the applied shared provider from AWS and Kubernetes before recording a receipt."""

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path


IMAGE = re.compile(r"[a-z0-9./_-]+@sha256:[a-f0-9]{64}\Z")
SOURCE = re.compile(r"[a-f0-9]{40}\Z")
NAME = re.compile(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?\Z")


def command(*arguments):
    return subprocess.run(arguments, check=True, capture_output=True).stdout


def output_value(outputs, name):
    entry = outputs.get(name, {})
    value = entry.get("value") if isinstance(entry, dict) else None
    if not isinstance(value, str) or not re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", value):
        raise ValueError(f"Coordinator: confirm the applied {name} output before publishing a receipt")
    return value


def metadata(value, namespace, name=None):
    result = value.get("metadata", {})
    if (not isinstance(result.get("name"), str) or not NAME.fullmatch(result["name"])
            or (name is not None and result["name"] != name)
            or result.get("namespace") != namespace or not isinstance(result.get("uid"), str)
            or not result["uid"] or result.get("deletionTimestamp")):
        raise ValueError("Browser runtime object identity is missing or differs from the selected workload")
    return result


def controller_owner(value, kind):
    owners = [owner for owner in value.get("ownerReferences", []) if owner.get("controller") is True]
    if (len(owners) != 1 or owners[0].get("kind") != kind or owners[0].get("apiVersion") != "apps/v1"
            or not isinstance(owners[0].get("name"), str) or not NAME.fullmatch(owners[0]["name"])
            or not isinstance(owners[0].get("uid"), str) or not owners[0]["uid"]):
        raise ValueError("Browser runtime controller ownership is missing or ambiguous")
    return owners[0]


def browser_image_matches(spec, image):
    containers = spec.get("containers", [])
    return (len(containers) == 1 and containers[0].get("name") == "browser"
            and containers[0].get("image") == image)


def receipt(environment, read=command):
    source = environment["SOURCE_SHA"]
    workflow_revision = environment["WORKFLOW_REVISION"]
    account = environment["EXPECTED_AWS_ACCOUNT_ID"]
    region = environment["AWS_REGION"]
    deployment_environment = environment["TARGET_ENVIRONMENT"]
    run_id = environment["GITHUB_RUN_ID"]
    attempt = environment["GITHUB_RUN_ATTEMPT"]
    if not (SOURCE.fullmatch(source) and SOURCE.fullmatch(workflow_revision)
            and environment["RELEASE_MODE"] == "apply" and re.fullmatch(r"[0-9]{12}", account)
            and re.fullmatch(r"[a-z]{2}-[a-z]+-[0-9]+", region)
            and deployment_environment in ("dev", "prod")
            and run_id.isdigit() and int(run_id) > 0 and attempt.isdigit() and int(attempt) > 0):
        raise ValueError("Coordinator: confirm the protected shared apply source, target and run identity")
    built = {name: environment[variable] for name, variable in
             (("lambda", "TF_VAR_image_uri"), ("browser", "TF_VAR_browser_service_image"))}
    for name, variable in (("lambda", "LAMBDA_REPO"), ("browser", "BROWSER_REPO")):
        repository = environment[variable]
        if not (repository.startswith(f"{account}.dkr.ecr.{region}.amazonaws.com/")
                and IMAGE.fullmatch(built[name]) and built[name].startswith(repository + "@")):
            raise ValueError("Coordinator: supply the two reviewed immutable build images")

    if read("aws", "sts", "get-caller-identity", "--query", "Account", "--output", "text").decode().strip() != account:
        raise ValueError("Protected shared receipt identity differs from confirmed account")
    infra = "modules/tools/agentcore/infra"
    outputs = json.loads(read("terraform", f"-chdir={infra}", "output", "-json"))
    function_name = output_value(outputs, "lambda_name")
    deployment = output_value(outputs, "browser_deployment_name")
    namespace = output_value(outputs, "browser_deployment_namespace")
    function = json.loads(read("aws", "lambda", "get-function", "--function-name", function_name,
                               "--region", region, "--output", "json"))
    configuration = function.get("Configuration", {})
    lambda_runtime = function.get("Code", {}).get("ResolvedImageUri")
    if (configuration.get("FunctionName") != function_name or configuration.get("State") != "Active"
            or configuration.get("LastUpdateStatus") != "Successful" or lambda_runtime != built["lambda"]):
        raise ValueError("Applied Lambda resolved image is not the reviewed build")

    read("kubectl", "rollout", "status", f"deployment/{deployment}", "-n", namespace, "--timeout=180s")
    deployed = json.loads(read("kubectl", "get", "deployment", deployment, "-n", namespace, "-o", "json"))
    deployment_metadata = metadata(deployed, namespace, deployment)
    desired = deployed.get("spec", {}).get("replicas")
    status = deployed.get("status", {})
    labels = {"app.kubernetes.io/name": deployment}
    if (type(desired) is not int or desired != 1
            or type(deployment_metadata.get("generation")) is not int or deployment_metadata["generation"] < 1
            or type(status.get("observedGeneration")) is not int
            or status["observedGeneration"] < deployment_metadata["generation"]
            or any(type(status.get(key)) is not int or status[key] != desired for key in
                   ("replicas", "readyReplicas", "updatedReplicas", "availableReplicas"))
            or deployed.get("spec", {}).get("selector") != {"matchLabels": labels}
            or not browser_image_matches(deployed.get("spec", {}).get("template", {}).get("spec", {}), built["browser"])):
        raise ValueError("Applied Browser deployment is not fully ready on the reviewed image")
    pods = json.loads(read("kubectl", "get", "pods", "-n", namespace, "-l",
                           f"app.kubernetes.io/name={deployment}", "-o", "json")).get("items", [])
    active = [pod for pod in pods if not pod.get("metadata", {}).get("deletionTimestamp")]
    if len(active) != desired:
        raise ValueError("Browser pods are missing or mixed during release")
    pod = active[0]
    pod_metadata = metadata(pod, namespace)
    owner = controller_owner(pod_metadata, "ReplicaSet")
    replica_set = json.loads(read("kubectl", "get", "replicaset", owner["name"], "-n", namespace, "-o", "json"))
    replica_metadata = metadata(replica_set, namespace, owner["name"])
    deployment_owner = controller_owner(replica_metadata, "Deployment")
    if (replica_metadata["uid"] != owner["uid"]
            or deployment_owner["name"] != deployment or deployment_owner["uid"] != deployment_metadata["uid"]
            or not all(pod_metadata.get("labels", {}).get(key) == value for key, value in labels.items())
            or not browser_image_matches(replica_set.get("spec", {}).get("template", {}).get("spec", {}), built["browser"])):
        raise ValueError("Browser pod ownership differs from the selected Deployment and ReplicaSet")
    pod_images = pod.get("status", {}).get("containerStatuses", [])
    image_id = pod_images[0].get("imageID", "") if len(pod_images) == 1 else ""
    # containerd may report the image config digest in status.image. Bind the
    # requested pod spec and the complete resolved manifest identity instead.
    if (pod.get("status", {}).get("phase") != "Running" or len(pod_images) != 1
            or pod_images[0].get("ready") is not True or pod_images[0].get("name") != "browser"
            or not browser_image_matches(pod.get("spec", {}), built["browser"])
            or image_id.removeprefix("docker-pullable://") != built["browser"]):
        raise ValueError("Browser pod runtime image differs from the reviewed build")

    backend = Path(environment["BACKEND"])
    variables = Path(environment["TFVARS"])
    state = read("terraform", f"-chdir={infra}", "state", "pull")
    if not state:
        raise ValueError("Applied shared Terraform state is unavailable")
    def digest(content):
        return hashlib.sha256(content).hexdigest()

    return {
        "component": "shared Lambda and Browser", "workflow": "shared-agentcore-tools-deploy.yml",
        "mode": "apply", "evidence_type": "independent-runtime",
        "source_revision": source, "workflow_revision": workflow_revision, "account": account, "region": region,
        "environment": deployment_environment, "run_id": int(run_id), "run_attempt": int(attempt),
        "config": {"backend_sha256": digest(backend.read_bytes()), "tfvars_sha256": digest(variables.read_bytes()),
                   "state_identity_sha256": digest((environment["STATE_BUCKET"] + "/" + environment["STATE_KEY"]).encode())},
        "state_sha256": digest(state),
        "images": {name: {"built": image, "runtime": image} for name, image in built.items()},
        "runtime_digests": {"shared Lambda and Browser": {name: image.split("@", 1)[1]
                                                    for name, image in built.items()}},
    }


if __name__ == "__main__":
    target = Path(os.environ["SHARED_RECEIPT_FILE"])
    target.write_text(json.dumps(receipt(os.environ), sort_keys=True) + "\n")
    target.chmod(0o600)
