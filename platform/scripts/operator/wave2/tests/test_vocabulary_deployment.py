import json
import sys

import pytest

import vocabulary_deployment as subject
from test_worker_observation import DIGEST, JOB_UID, NONCE, POD_UID, RUN_ID, SA, live_pod


@pytest.mark.parametrize("defect", [None, "gateway_digest", "worker_digest", "owner", "replaced", "terminating", "unready", "account"])
def test_cli_binds_actual_fixture_runtime_images(tmp_path, monkeypatch, defect):
    image = "repo/worker@" + DIGEST
    gateway_image = "repo/gateway@sha256:" + "ab" * 32
    gateway_meta = {"name": "fixture-gateway", "namespace": "adp-gateway", "uid": "deployment-uid"}
    ledger = {"run_id": RUN_ID, "run_nonce": NONCE, "account_id": "879318057152", "k8s": [
        {"kind": "Deployment", **gateway_meta},
        {"kind": "Job", "uid": JOB_UID}, {"kind": "Pod", "uid": POD_UID},
    ]}
    identity = {"run_id": RUN_ID, "nonce": NONCE, "account_id": ledger["account_id"],
                "namespace": "adp-agents", "pod_name": "worker", "pod_uid": POD_UID,
                "job_uid": JOB_UID, "container": "agent-worker", "service_account": SA}
    rs = {"metadata": {"uid": "rs-uid", "ownerReferences": [
        {"kind": "Deployment", "uid": "deployment-uid", "controller": True}]}}
    gateway_pod = {"metadata": {"uid": "gateway-pod", "labels": {
        "adp.io/w2-fixture": RUN_ID, "adp.io/w2-nonce": NONCE},
        "ownerReferences": [{"kind": "ReplicaSet", "uid": "rs-uid", "controller": True}]},
        "status": {"phase": "Running", "containerStatuses": [{"ready": True, "imageID": gateway_image}]}}
    worker = live_pod()
    if defect == "gateway_digest":
        gateway_pod["status"]["containerStatuses"][0]["imageID"] = image
    if defect == "worker_digest":
        worker["status"]["containerStatuses"][0]["imageID"] = gateway_image
    if defect == "owner":
        rs["metadata"]["ownerReferences"][0]["uid"] = "ordinary-deployment"
    if defect == "replaced":
        worker["metadata"]["uid"] = "replacement-pod"
    if defect == "terminating":
        gateway_pod["metadata"]["deletionTimestamp"] = "2026-09-24T13:00:00Z"
    if defect == "unready":
        gateway_pod["status"]["containerStatuses"][0]["ready"] = False
    calls = []
    def command(args, **kwargs):
        calls.append(args)
        if args[0] == "aws":
            return "605440105851" if defect == "account" else "879318057152"
        resources = {"deployment": {"metadata": gateway_meta}, "replicasets": {"items": [rs]},
                     "pods": {"items": [gateway_pod]}, "pod": worker}
        return json.dumps(resources[args[2]])
    monkeypatch.setattr(subject.subprocess, "check_output", command)
    monkeypatch.setattr(subject, "load_ledger", lambda *args, **kwargs: ledger)
    identity_path, output = tmp_path / "identity.json", tmp_path / "deployed.json"
    identity_path.write_text(json.dumps({"expected_identity": identity}))
    monkeypatch.setattr(sys, "argv", ["collector", "--ledger", str(tmp_path / "ledger.json"),
        "--identity", str(identity_path), "--gateway-image", gateway_image,
        "--worker-image", image, "--out", str(output)])
    if defect:
        with pytest.raises(subject.ObservationError):
            subject.main()
        assert not output.exists()
    else:
        subject.main()
        result = json.loads(output.read_text())
        assert result["writer_digest_deployed"] is True
        assert result["gateway_digest_deployed"] is True
        assert result["gateway_pods"][0]["pod_uid"] == "gateway-pod"
        assert result["worker"]["pod_uid"] == POD_UID
        assert result["observed_at"]
    assert all("scaledjob" not in args and "bedrockgateway" not in args for args in calls)
