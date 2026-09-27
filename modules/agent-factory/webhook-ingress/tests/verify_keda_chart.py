"""Render the real Terraform Helm inputs and check KEDA's runtime contract.

Run with python-hcl2==8.1.4, PyYAML, jsonschema and Helm 3.22+ installed:
  python verify_keda_chart.py --helm helm --chart /path/keda-2.21.0.tgz
Optional --live-scaledjobs validates a previously collected Kubernetes List.
This does not contact or mutate a cluster.
"""

import argparse
import json
import subprocess
import tempfile
from pathlib import Path

import hcl2
import jsonschema
import yaml


def decode(value):
    if isinstance(value, dict):
        return {decode(k): decode(v) for k, v in value.items()}
    if isinstance(value, list):
        return [decode(v) for v in value]
    if isinstance(value, str) and value.startswith('"'):
        return json.loads(value)
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--helm", default="helm")
    parser.add_argument("--chart", required=True)
    parser.add_argument("--live-scaledjobs", type=Path)
    args = parser.parse_args()
    source = Path(__file__).resolve().parents[1] / "infra/keda.tf"
    config = decode(hcl2.loads(source.read_text()))
    release = next(r["helm_release"]["keda"] for r in config["resource"] if "helm_release" in r)
    arn = "arn:aws:iam::123456789012:role/test-keda-operator"
    command = [
        args.helm,
        "template",
        release["name"],
        args.chart,
        "--namespace",
        release["namespace"],
        "--kube-version",
        "1.35.0",
    ]
    for setting in release["set"]:
        value = setting["value"]
        if value == "${aws_iam_role.keda_operator.arn}":
            value = arn
        assert "${" not in value, f"Unresolved Helm setting: {setting['name']}"
        command += ["--set", setting["name"] + "=" + value]
    with tempfile.TemporaryDirectory() as directory:
        for index, expression in enumerate(release["values"]):
            assert expression.startswith("${yamlencode(") and expression.endswith(")}")
            values = decode(hcl2.loads("value = " + expression[len("${yamlencode(") : -2]))["value"]
            values.pop("__is_block__", None)
            path = Path(directory) / f"values-{index}.yaml"
            path.write_text(yaml.safe_dump(values))
            command += ["--values", str(path)]
        documents = [
            d for d in yaml.safe_load_all(subprocess.check_output(command, text=True)) if d
        ]
    deployments = {d["metadata"]["name"]: d for d in documents if d["kind"] == "Deployment"}
    assert len(deployments) == 3
    accounts = {d["metadata"]["name"]: d for d in documents if d["kind"] == "ServiceAccount"}
    assert accounts["keda-operator"]["metadata"]["annotations"]["eks.amazonaws.com/role-arn"] == arn
    for name, deployment in deployments.items():
        pod = deployment["spec"]["template"]
        assert pod["spec"]["serviceAccountName"] in accounts
        for container in pod["spec"]["containers"]:
            assert ":2.21.0@sha256:" in container["image"], container["image"]
        pinned = pod["metadata"].get("annotations", {}).get("karpenter.sh/do-not-disrupt")
        assert pinned == ("true" if name == "keda-operator" else None)
    operator = deployments["keda-operator"]["spec"]["template"]["spec"]["containers"][0]
    assert "--service-account-token-mode=enforce-audience" in operator["args"]
    assert operator["resources"] == {
        "requests": {"cpu": "100m", "memory": "128Mi"},
        "limits": {"cpu": "500m", "memory": "512Mi"},
    }
    crds = {
        d["spec"]["names"]["kind"]: d for d in documents if d["kind"] == "CustomResourceDefinition"
    }
    assert {
        "ScaledJob",
        "ScaledObject",
        "TriggerAuthentication",
        "ClusterTriggerAuthentication",
    } <= crds.keys()
    assert any(d["kind"] == "APIService" for d in documents)
    assert any(d["kind"] == "ValidatingWebhookConfiguration" for d in documents)
    checked = 0
    if args.live_scaledjobs:
        schema = next(
            v["schema"]["openAPIV3Schema"]
            for v in crds["ScaledJob"]["spec"]["versions"]
            if v["name"] == "v1alpha1"
        )
        for job in json.loads(args.live_scaledjobs.read_text())["items"]:
            jsonschema.Draft7Validator(schema).validate(job)
            assert all(t["type"] == "aws-sqs-queue" for t in job["spec"]["triggers"])
            checked += 1
    print(
        json.dumps(
            {
                "chart": release["version"],
                "deployments": sorted(deployments),
                "crds": sorted(crds),
                "live_scaledjob_schemas_validated": checked,
                "irsa_and_resources_preserved": True,
            }
        )
    )


if __name__ == "__main__":
    main()
