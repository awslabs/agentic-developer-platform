"""Plan existing authority resources in the authorized dev account; never apply.

The full Terraform plan stays private on the runner. Only resource addresses and
actions are printed; credentials, secret values and Terraform state are omitted.
"""

import json
import os
import subprocess
import tempfile
from pathlib import Path


def output(*args, cwd=None):
    return subprocess.check_output(args, cwd=cwd, text=True)


root = Path(__file__).resolve().parents[3]
infra = root / "modules/agent-factory/webhook-ingress/infra"
identity = json.loads(output("aws", "sts", "get-caller-identity", "--output", "json"))
expected = os.environ["ACCOUNT_ID"]
if expected != "879318057152" or identity["Account"] != expected or os.environ.get("ENVIRONMENT") != "dev":
    raise SystemExit("Stage 1 prerequisite planning is restricted to the authorized embark1 dev account")
digest = output(
    "aws",
    "ecr",
    "describe-images",
    "--repository-name",
    "adp-agent-runtime",
    "--image-ids",
    "imageTag=latest",
    "--query",
    "imageDetails[0].imageDigest",
    "--output",
    "text",
).strip()
if not digest.startswith("sha256:") or len(digest) != 71:
    raise SystemExit("Current worker image digest could not be resolved")
print("Planning prerequisites using current worker digest", digest, flush=True)
subprocess.run(["aws", "eks", "update-kubeconfig", "--name", "adp-dev-eks-cluster", "--region", "us-east-1"], check=True, stdout=subprocess.DEVNULL)
targets = [
    "random_password.agent_run_credential",
    "tls_private_key.agent_control_envelope",
    "tls_private_key.agent_control_envelope_secondary",
    "kubernetes_secret.agent_authority",
    "kubernetes_config_map.agent_control_verification_keys",
    "kubernetes_cluster_role.gateway_agent_tokenreview",
    "kubernetes_cluster_role_binding.gateway_agent_tokenreview",
    "kubernetes_role.gateway_agent_pod_read",
    "kubernetes_role_binding.gateway_agent_pod_read",
    "aws_iam_policy.agent_authority_boundary",
    "aws_iam_role.agent_authority_worker",
    "aws_iam_role_policy.agent_authority_worker",
    "kubernetes_service_account.agent_authority_worker",
]
subprocess.run(
    [
        "terraform",
        "init",
        "-input=false",
        "-reconfigure",
        f"-backend-config={root / 'environments/dev/modules/webhook-ingress-backend.tfvars'}",
        f"-backend-config=bucket={os.environ['STATE_BUCKET']}",
    ],
    cwd=infra,
    check=True,
    stdout=subprocess.DEVNULL,
)
with tempfile.TemporaryDirectory(prefix="stage1-authority-plan-") as scratch:
    plan = Path(scratch) / "plan.bin"
    args = [
        "terraform",
        "plan",
        "-input=false",
        "-lock-timeout=30s",
        "-no-color",
        f"-out={plan}",
        "-var-file=terraform.tfvars",
        "-var=agent_authority_enabled=true",
        f"-var=agent_authority_worker_image_digests={json.dumps([digest])}",
    ]
    args += [f"-target={target}" for target in targets]
    result = subprocess.run(args, cwd=infra, text=True, capture_output=True)
    if result.returncode:
        # Terraform diagnostics name missing resources/inputs. Do not print the
        # plan's stdout, which can include values not marked sensitive by a provider.
        print(result.stderr)
        raise SystemExit(result.returncode)
    report = json.loads(output("terraform", "show", "-json", str(plan), cwd=infra))
    changes = [
        {"address": r["address"], "actions": r["change"]["actions"]}
        for r in report.get("resource_changes", [])
        if r["change"]["actions"] != ["no-op"]
    ]
    print(json.dumps({"account": expected, "plan_only": True, "worker_digest": digest, "changes": changes}, indent=2))
