"""Every dispatch mode enforces broker registration, object ownership and isolation."""
import base64
import hashlib
import importlib
import json
from pathlib import Path
import sys
import time
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from isolation import IsolationError

JOB = "cyber-" + "a" * 32 + "-" + "b" * 32
SAMPLE = "s3://samples/o/acme/t/team/u/user/s/session/task/in/sample"
SCRIPT = b'import json; print(json.dumps({"ok": True}))'

@pytest.fixture(params=["triage", "static-a", "static-b"])
def worker(request, monkeypatch, tmp_path):
    stage = request.param.split("-")[0]
    module = importlib.import_module(stage + ".handler")
    now = int(time.time())
    body = dict(artifact_id=JOB, org_id="acme", team_id="team", user_id="user", sample_s3_uri=SAMPLE,
                stage=stage, registration_version=1, issued_at=now, expires_at=now+900)
    if request.param == "static-b":
        body.update(script_base64=base64.b64encode(SCRIPT).decode(), script_sha256=hashlib.sha256(SCRIPT).hexdigest(),
                    script_validation="python-syntax-v1")
    sqs, table = Mock(), Mock()
    def client(service, **kw):
        assert service == "sqs", "Worker must never create an ambient S3 client"
        return sqs
    monkeypatch.setattr(module.boto3, "client", client)
    monkeypatch.setattr(module.boto3, "resource", Mock(return_value=Mock(Table=Mock(return_value=table))))
    download = Mock(side_effect=lambda body, ref, dest: dest.write_bytes(b"sample"))
    sandbox = Mock(return_value={"ok": True, "hashes": {"sha256": "a"*64}})
    monkeypatch.setattr(module, "download_sample", download)
    monkeypatch.setattr(module, "run_isolated", sandbox)
    for key, value in dict(INPUT_QUEUE_URL="input", RESPONSE_QUEUE_URL="output", RESULTS_TABLE="results", AWS_REGION="us-east-1", CYBER_ALLOWED_BUCKETS="samples").items():
        monkeypatch.setenv(key, value)
    manifest = tmp_path / "manifest.json"
    manifest.write_text('{"python_packages":{},"system_binaries":{}}')
    monkeypatch.setenv("WORKER_MANIFEST_PATH", str(manifest))
    monkeypatch.setenv("CYBER_VALIDATOR_PATH", str(Path(__file__).resolve().parents[2]/"agent/skills/stage-3-static/validate_script.py"))
    def run():
        sqs.receive_message.return_value={"Messages":[{"Body":json.dumps(body),"ReceiptHandle":"receipt"}]}
        module.run()
        return json.loads(sqs.send_message.call_args.kwargs["MessageBody"])
    return body, run, download, sandbox, table


def test_every_valid_mode_uses_isolation(worker):
    body, run, download, sandbox, table = worker
    assert run()["status"] == "ok"
    assert download.call_count == sandbox.call_count == 1
    assert table.put_item.called

@pytest.mark.parametrize("change", [
    {"registration_version":0}, {"expires_at":0}, {"issued_at":2**40},
    {"sample_s3_uri":SAMPLE.replace("/acme/", "/victim/")},
    {"sample_s3_uri":SAMPLE.replace("/team/", "/victim-team/")},
    {"sample_s3_uri":SAMPLE.replace("/user/", "/victim-user/")},
    {"sample_s3_uri":SAMPLE.replace("samples/", "other-bucket/")}, {"user_id":""},
])
def test_refusal_happens_before_any_download_or_execution(worker, change):
    body, run, download, sandbox, table = worker
    body.update(change)
    result = run()
    assert result["status"] == "failed"
    assert not download.called and not sandbox.called
    assert "victim" not in json.dumps(result)


def test_kernel_unavailable_is_failed_stage(worker):
    body, run, download, sandbox, table = worker
    sandbox.side_effect = IsolationError("unavailable")
    assert run()["status"] == "failed"


def test_tampered_script_never_executes(worker):
    body, run, download, sandbox, table = worker
    if body.get("script_base64") is None:
        return
    body["script_sha256"] = "0" * 64
    result = run()
    assert result["status"] == "failed"
    assert result["findings"]["reason"] == "script_digest_mismatch"
    assert not sandbox.called


def test_legacy_script_locations_are_never_downloaded(worker):
    body, run, download, sandbox, table = worker
    if body["stage"] != "static":
        return
    body["script_s3_uri"] = "s3://samples/o/victim/script.py"
    assert run()["status"] == "failed"
    assert not download.called and not sandbox.called
