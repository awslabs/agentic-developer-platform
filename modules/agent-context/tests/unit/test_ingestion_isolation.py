"""#4720 deployment and admission contract: parsers inherit no ambient authority."""

import importlib.util
import json
import os
import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
IMAGE = ROOT / "images/ingestion"


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), IMAGE / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def manifests(name):
    raw = (ROOT / "manifests" / name).read_text()
    return list(yaml.safe_load_all(re.sub(r"\$\{(\w+)\}", r"fixture-\1", raw)))


def test_content_worker_has_no_shared_credentials_tokens_or_storage():
    doc = manifests("ingestion-scaledjob.yaml")[0]
    template = doc["spec"]["jobTargetRef"]["template"]
    pod = template["spec"]
    (worker,) = pod["containers"]
    (init,) = pod["initContainers"]
    assert pod["serviceAccountName"] == "ingestion-receiver"
    assert pod["automountServiceAccountToken"] is False
    assert template["metadata"]["annotations"]["eks.amazonaws.com/skip-containers"] == "worker"
    assert not pod.get("shareProcessNamespace") and not any(
        pod[k] for k in ["hostNetwork", "hostPID", "hostIPC"]
    )
    assert all("emptyDir" in v and v["emptyDir"].get("sizeLimit") for v in pod["volumes"])
    assert not worker.get("envFrom") and not init.get("envFrom")
    env = {e["name"]: e for e in worker["env"]}
    assert env["INGESTION_ISOLATION_REQUIRED"]["value"] == "true"
    assert env["AWS_EC2_METADATA_DISABLED"]["value"] == "true"
    assert env["AWS_SHARED_CREDENTIALS_FILE"]["value"] == "/dev/null"
    assert not any(
        re.search(r"DB_|SECRET|PASSWORD|ACCESS_KEY|WEB_IDENTITY|ROLE_ARN|SQS", k) for k in env
    )
    assert not any("secretKeyRef" in e.get("valueFrom", {}) for e in worker["env"])
    assert any(m["name"] == "admission" and m["readOnly"] for m in worker["volumeMounts"])
    assert init["command"] == ["python3", "/app/receive-job.py"]
    assert env["SCIP_ENABLED"]["value"] == "true" and env["SCIP_ISOLATED_BACKEND"]["value"] == ""


def test_refresh_has_only_audience_bound_identity_and_does_not_parse_content():
    doc = manifests("repo-refresh-cronjob.yaml")[0]
    pod = doc["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    (container,) = pod["containers"]
    assert (
        pod["serviceAccountName"] == "ingestion-refresh"
        and pod["automountServiceAccountToken"] is False
    )
    assert container["command"] == ["python3", "/app/queue-refresh.py"]
    assert not container.get("envFrom")
    for volume in pod["volumes"]:
        assert "secret" not in volume and "persistentVolumeClaim" not in volume
        for source in volume.get("projected", {}).get("sources", []):
            assert source["serviceAccountToken"]["audience"] == "adp-ingestion-refresh"
    maintenance = manifests("knowledge-maintenance-cronjob.yaml")[0]
    command = str(
        maintenance["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]["args"]
    )
    assert not re.search(r"refresh-repos|publish-ingestion|ingest-repo", command)


def test_egress_has_no_database_metadata_or_other_private_targets():
    policy = manifests("ingestion-networkpolicy.yaml")[0]["spec"]
    assert policy["policyTypes"] == ["Ingress", "Egress"] and policy["ingress"] == []
    assert policy["podSelector"]["matchLabels"]["adp.io/ingestion-isolation"] == "v1"
    for rule in policy["egress"]:
        assert all(p["port"] not in [5432, 80, 6443, 8182] for p in rule["ports"])
        for peer in rule["to"]:
            if peer.get("ipBlock", {}).get("cidr") == "0.0.0.0/0":
                assert {"10.0.0.0/8", "169.254.0.0/16", "172.16.0.0/12", "192.168.0.0/16"} <= set(
                    peer["ipBlock"]["except"]
                )
                assert rule["ports"] == [{"protocol": "TCP", "port": 443}]


def test_receiver_iam_boundary_is_queue_only():
    tf = (ROOT / "terraform/ingestion-isolation.tf").read_text()
    actions = re.search(r"ingestion_receiver_actions = (\[[^\n]+\])", tf).group(1)
    assert set(json.loads(actions)) == {
        "sqs:ReceiveMessage",
        "sqs:ChangeMessageVisibility",
        "sqs:GetQueueAttributes",
        "sqs:GetQueueUrl",
    }
    assert "permissions_boundary = aws_iam_policy.ingestion_receiver_boundary.arn" in tf
    assert "policy = local.ingestion_receiver_policy" in tf
    assert "Resource = module.sqs_ingestion.queue_arn" in tf
    assert "system:serviceaccount:${var.namespace}:ingestion-receiver" in tf


def test_receiver_ignores_untrusted_queue_scope_and_uses_exclusive_admission_file(
    tmp_path, monkeypatch
):
    monkeypatch.syspath_prepend(str(IMAGE))
    receiver = load("receive-job")
    queue = MagicMock()
    queue.receive_message.return_value = {
        "Messages": [
            {
                "ReceiptHandle": "receipt",
                "Body": json.dumps(
                    {
                        "callback_grant": "signed-attempt",
                        "source": "victim/repo",
                        "scope": {"tenant_id": "victim"},
                        "installation_id": 99,
                    }
                ),
            }
        ]
    }
    monkeypatch.setattr(receiver.boto3, "client", lambda *a, **kw: queue)
    gateway = MagicMock(return_value={"callback_grant": "signed-attempt", "source": "owned/repo"})
    monkeypatch.setattr(receiver, "invoke", gateway)
    monkeypatch.setenv("SQS_QUEUE_URL", "https://queue.invalid/fixture")
    monkeypatch.setenv("INGESTION_CONTROL_URL", "http://gateway.invalid/ingestion-control")
    target = tmp_path / "job.json"
    monkeypatch.setenv("INGESTION_JOB_FILE", str(target))
    receiver.receive()
    gateway.assert_called_once_with(
        "http://gateway.invalid/ingestion-control",
        "signed-attempt",
        "bootstrap",
        {"receipt_handle": "receipt"},
    )
    assert json.loads(target.read_text())["source"] == "owned/repo"
    assert target.stat().st_mode & 0o777 == 0o400
    queue.delete_message.assert_not_called()
    with pytest.raises(FileExistsError):
        receiver.receive()


def test_worker_catalog_adapter_uses_named_operations_not_sql(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(IMAGE))
    import job_client
    import db

    gateway = MagicMock(return_value="canonical-run")
    monkeypatch.setattr(job_client, "call", gateway)
    monkeypatch.setenv("INGESTION_JOB_FILE", str(tmp_path / "job.json"))
    connection = db.get_connection()
    assert not hasattr(connection, "cursor")
    assert db.create_index_run(connection, None, "asset") == "canonical-run"
    gateway.assert_called_once_with(
        "catalog.create_index_run", {"repo_id": None, "repo": "asset", "commit_sha": None}
    )
    monkeypatch.delenv("INGESTION_JOB_FILE")
    monkeypatch.setenv("INGESTION_ISOLATION_REQUIRED", "true")
    with pytest.raises(RuntimeError):
        db.get_connection()


def test_gateway_and_worker_acl_derivation_stay_identical():
    import ast

    gateway = ROOT.parents[0] / "gateway/src/knowledge/source_policy/repo_acl.py"

    def function(path):
        return next(
            n
            for n in ast.parse(path.read_text()).body
            if isinstance(n, ast.FunctionDef) and n.name == "derive_acl_from_github"
        )

    assert ast.dump(function(gateway)) == ast.dump(function(IMAGE / "repo_acl.py"))


@pytest.mark.parametrize(
    "callback",
    [
        "http://gateway:8080",
        "http://gateway:8080/",
        "http://gateway:8080/internal/v1/knowledge-assets/status-callback",
    ],
)
def test_deploy_resolves_control_routes_from_gateway_origin(callback):
    import subprocess

    result = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; resolve_ingestion_authority_config; echo "$INGESTION_CONTROL_URL $INGESTION_REFRESH_URL"',
            "fixture",
            str(ROOT / "scripts/_common.sh"),
        ],
        env={
            **os.environ,
            "GATEWAY_CALLBACK_URL": callback,
            "INGESTION_RECEIVER_ROLE_ARN": "arn:aws:iam::111122223333:role/fixture",
        },
        check=True,
        capture_output=True,
        text=True,
    )
    assert (
        result.stdout.strip()
        == "http://gateway:8080/internal/v1/knowledge-assets/ingestion-control http://gateway:8080/internal/v1/knowledge-assets/refresh"
    )


def test_maintenance_prefers_published_index_and_retains_legacy_fallback(tmp_path, monkeypatch):
    import io
    from botocore.exceptions import ClientError

    monkeypatch.syspath_prepend(str(IMAGE))
    from stored_code_index import load_code_index

    (tmp_path / "fixture-service.json").write_text(
        json.dumps({"repo": "fixture/service", "version": "old"})
    )
    s3 = MagicMock()
    s3.get_object.return_value = {
        "Body": io.BytesIO(json.dumps({"repo": "fixture/service", "version": "new"}).encode())
    }
    assert (
        load_code_index("fixture/service", bucket="fixture", directory=str(tmp_path), s3=s3)[
            "version"
        ]
        == "new"
    )
    s3.get_object.return_value = {"Body": io.BytesIO(b'{"repo":"another/service"}')}
    assert (
        load_code_index("fixture/service", bucket="fixture", directory=str(tmp_path), s3=s3) is None
    )
    s3.get_object.side_effect = ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
    assert (
        load_code_index("fixture/service", bucket="fixture", directory=str(tmp_path), s3=s3)[
            "version"
        ]
        == "old"
    )
