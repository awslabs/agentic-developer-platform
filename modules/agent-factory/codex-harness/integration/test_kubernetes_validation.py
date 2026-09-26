"""Actual isolated Pods in an explicitly provisioned disposable local cluster."""

from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tarfile
import threading
import time
from urllib.parse import urlsplit
import uuid

import pytest

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "modules/agent-factory/agent-worker-image"))
from lib.codex_kubernetes_validation import from_host_configuration  # noqa: E402
from lib.codex_validation import ValidationCheck  # noqa: E402

IMAGE = os.environ.get("ADP_CODEX_KUBERNETES_IMAGE")
pytestmark = pytest.mark.skipif(not IMAGE, reason="requires explicit local Kubernetes qualification")


@pytest.fixture
def execution(tmp_path):
    path = Path(os.environ["ADP_CODEX_VALIDATION_KUBERNETES_CONFIG"])
    config = json.loads(path.read_text())
    assert urlsplit(config["endpoint"]).hostname in {"127.0.0.1", "localhost"}
    executor = from_host_configuration("tsk_" + str(uuid.uuid4()))
    archive = tmp_path / "source.tar"
    with tarfile.open(archive, "w") as stream:
        content = b"expected source\n"
        item = tarfile.TarInfo("source.txt")
        item.size = len(content)
        stream.addfile(item, io.BytesIO(content))
    yield executor, dict(archive=archive, archive_sha256=hashlib.sha256(archive.read_bytes()).hexdigest(), commit="a" * 40)
    assert executor.recover()
    # Source and receipt cleanup must be real API observations, not local flags.
    for resource in ["pods", "configmaps"]:
        response = executor.api.request("GET", executor.base + "/" + resource,
            params={"labelSelector": "adp.dev/validation-task=" + executor.scope})
        assert response["items"] == []


def test_actual_pod_has_source_without_credentials_root_or_capabilities(execution):
    executor, source = execution
    script = """set -eu
test "$(id -u)" = 65534
test "$(cat source.txt)" = 'expected source'
test ! -e /var/run/secrets/kubernetes.io/serviceaccount/token
test ! -e /var/run/docker.sock
test -z "${AWS_ACCESS_KEY_ID:-}"
test -z "${AWS_SESSION_TOKEN:-}"
grep -q 'CapEff:.*0000000000000000' /proc/self/status
! touch /root/host-write 2>/dev/null
echo isolated-pod-verified
"""
    result = executor.run(check=ValidationCheck("isolation", IMAGE, ("/bin/sh", "-c", script)), **source)
    assert result["status"] == "passed", result
    assert result["output"] == "isolated-pod-verified\n"


@pytest.mark.parametrize("kind,argv,reason", [
    ("failure", ("/bin/sh", "-c", "exit 7"), "process_failed"),
    ("output", ("/bin/sh", "-c", "yes output"), "output_limit"),
    ("timeout", ("/bin/sh", "-c", "sleep 30"), "timeout"),
])
def test_actual_failure_output_and_timeout_never_pass(execution, kind, argv, reason):
    executor, source = execution
    result = executor.run(check=ValidationCheck(kind, IMAGE, argv,
        timeout_seconds=3 if kind == "timeout" else 30, max_output_bytes=512), **source)
    assert result["status"] == "failed" and result["reason"] == reason, result
    assert len(result["output"].encode()) <= 512


def test_actual_cancellation_observes_exit_and_removes_source(execution):
    from concurrent.futures import ThreadPoolExecutor
    executor, source = execution
    stop = threading.Event()
    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(executor.run, check=ValidationCheck("cancel", IMAGE, ("sleep", "60")), cancelled=stop, **source)
        deadline = time.monotonic() + 20
        try:
            while time.monotonic() < deadline:
                pods = executor.api.request("GET", executor.base + "/pods",
                    params={"labelSelector": "adp.dev/validation-task=" + executor.scope})["items"]
                if any(pod.get("status", {}).get("phase") == "Running" for pod in pods):
                    break
                if running.done():
                    pytest.fail(str(running.result()))
                time.sleep(0.1)
            else:
                pytest.fail("Validation Pod never ran")
        finally:
            stop.set()
        result = running.result(timeout=20)
    assert result["status"] == "failed" and result["reason"] == "cancelled", result


@pytest.mark.skipif(not os.environ.get("ADP_CODEX_TEST_KUBERNETES_API_IP"), reason="requires reachable control endpoint")
def test_network_policy_blocks_a_control_verified_reachable_endpoint(execution):
    import ipaddress
    executor, source = execution
    address = str(ipaddress.ip_address(os.environ["ADP_CODEX_TEST_KUBERNETES_API_IP"]))
    script = f"""const s=require('net').connect(443,{json.dumps(address)});
s.setTimeout(1000);
s.on('connect',()=>process.exit(1));
s.on('timeout',()=>{{s.destroy();console.log('network-denied');}});
s.on('error',()=>{{s.destroy();console.log('network-denied');}});
"""
    result = executor.run(check=ValidationCheck("network", IMAGE, ("node", "-e", script)), **source)
    assert result["status"] == "passed", result
    assert result["output"] == "network-denied\n"


def test_qualified_node_enforces_the_bounded_process_limit(execution):
    executor, source = execution
    # A fixed set of 140 short-lived children, never recursive spawning. Keep
    # successful children alive until EAGAIN proves the pod's actual PID limit.
    script = """const {spawn}=require('child_process');
const children=[]; let started=0, refused=0;
const attempts=Array.from({length:140},()=>new Promise(resolve=>{
  const child=spawn('/bin/sleep',['5']);children.push(child);
  child.once('spawn',()=>{started++;resolve();});
  child.once('error',error=>{if(error.code==='EAGAIN')refused++;resolve();});
}));
Promise.all(attempts).then(()=>{
  for(const child of children)child.kill('SIGKILL');
  if(refused===0 || started<100 || started>128)process.exitCode=1;
  else console.log('process-limit-enforced');
});
"""
    result = executor.run(check=ValidationCheck("process_limit", IMAGE, ("node", "-e", script)), **source)
    assert result["status"] == "passed", result
    assert result["output"] == "process-limit-enforced\n"
