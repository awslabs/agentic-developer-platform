"""Validate the seven live runner observations, bound to this protected run."""
import hashlib
import json
import re
from registration_delivery import private_receipt

LIVE_CHECKS = frozenset({"eks-manager-ready", "same-vpc-private-asg", "qualified-image-lock",
    "project-ci-enabled", "adp-project-runner-assignment", "legal-pipeline-ran", "idle-scale-to-zero"})


def require(condition):
    if not condition:
        raise ValueError("live runner evidence incomplete or mismatched")


def verify(receipt, env, account, checkout):
    config = json.loads(env["ADP_RUNNER_CONFIG_JSON"])
    targets_hash = hashlib.sha256(json.dumps(config, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    expected = {"schema_version": 1, "source_revision": checkout, "workflow_run_id": int(env["GITHUB_RUN_ID"]),
                "run_attempt": int(env["GITHUB_RUN_ATTEMPT"]), "account_id": account, "region": env["AWS_REGION"],
                "correlation": env["ADP_CORRELATION"], "config_sha256": targets_hash}
    require(isinstance(receipt, dict) and set(receipt) == set(expected) | {"checks"}
            and all(type(receipt.get(k)) is type(v) and receipt[k] == v for k, v in expected.items()))
    require(set(receipt["checks"]) == LIVE_CHECKS)
    product = config["product"]
    required = {
        "eks-manager-ready": {"group_id": product["group_id"], "ready_replicas": 1, "live_managers": 1},
        "same-vpc-private-asg": {"vpc_id": config["vpc_id"], "asg_arn": config["asg_arn"], "public_ip": False},
        "qualified-image-lock": {"release_lock_sha256": config["release_lock_sha256"], "ami_id": config["ami_id"]},
        "project-ci-enabled": {"project_id": product["project_id"], "enabled": True, "shared_runners": False,
                               "developer_user_id": config["developer_user_id"], "developer_access": 30},
        "adp-project-runner-assignment": {"project_id": product["project_id"], "group_id": product["group_id"], "assigned": True},
        "legal-pipeline-ran": {"project_id": product["project_id"], "commit": config["smoke"]["commit"],
                               "status": "success", "artifact_verified": True},
        "idle-scale-to-zero": {"asg_arn": config["asg_arn"], "desired_capacity": 0, "instances": 0, "active_jobs": 0},
    }
    refs = {}
    for name, observation in required.items():
        row = receipt["checks"][name]
        require(set(row) == {"verdict", "observed", "evidence_ref"} and row["verdict"] == "pass")
        actual = row["observed"]
        require(set(actual) == set(observation) and all(type(actual[k]) is type(v) and actual[k] == v for k, v in observation.items()))
        prefix = (f"s3://adp-{config['environment']}-deployment-evidence-{account}/gitlab-runners/repos/"
                  f"{int(env['GITHUB_REPOSITORY_ID'])}/runs/{expected['workflow_run_id']}/attempts/{expected['run_attempt']}/{name}.json?versionId=")
        require(isinstance(row["evidence_ref"], str) and row["evidence_ref"].startswith(prefix)
                and re.fullmatch(r"[A-Za-z0-9_.~%-]{1,256}", row["evidence_ref"][len(prefix):])
                and row["evidence_ref"][len(prefix):] != "null")
        refs[name] = row["evidence_ref"]
    return refs
