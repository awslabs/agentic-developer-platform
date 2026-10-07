"""Offline checks for opt-in chat sandbox image and node warming."""

import json
import os
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
WARM = ROOT / "chat-warm"
IMAGE = f"registry.example.test/sandbox@sha256:{'b' * 64}"


def render(image=IMAGE, capacity="2"):
    return subprocess.run(
        ["node", str(WARM / "render-chat-warm.mjs")],
        capture_output=True, text=True,
        env={**os.environ, "SANDBOX_IMAGE_DIGEST": image, "CHAT_WARM_CAPACITY": capacity},
    )


def test_bounded_capacity_uses_same_pinned_sandbox_image_without_running_it():
    for capacity in ("1", "2", "3"):
        result = render(capacity=capacity)
        assert result.returncode == 0, result.stderr
        priority, deployment, cache = yaml.safe_load_all(result.stdout)
        assert priority["value"] < 0
        assert priority["globalDefault"] is False
        assert deployment["spec"]["replicas"] == int(capacity)
        assert {deployment["kind"], cache["kind"]} == {"Deployment", "DaemonSet"}
        for resource in (deployment, cache):
            pod = resource["spec"]["template"]
            assert pod["spec"]["priorityClassName"] == priority["metadata"]["name"]
            assert pod["spec"]["automountServiceAccountToken"] is False
            assert "serviceAccountName" not in pod["spec"]
            assert "adp.io/chat-sandbox" not in pod["metadata"]["labels"]
            container = pod["spec"]["containers"][0]
            assert container["image"] == IMAGE
            assert container["imagePullPolicy"] == "IfNotPresent"
            assert container["command"][0] == "/bin/sh"
            assert "env" not in container and "volumeMounts" not in container
        reserve = deployment["spec"]["template"]["spec"]["containers"][0]
        assert reserve["resources"]["requests"] == {
            "cpu": "200m", "memory": "512Mi", "ephemeral-storage": "256Mi"
        }
        assert "REPLACE_WITH_" not in result.stdout


def test_invalid_capacity_or_unpinned_image_never_renders_resources():
    for image, capacity in ((IMAGE, "0"), (IMAGE, "4"), (IMAGE, "2\n---"),
                            ("registry.example.test/sandbox:latest", "2"), ("", "2")):
        result = render(image, capacity)
        assert result.returncode != 0
        assert result.stdout == ""


def test_disabled_invocation_never_calls_kubectl_or_node(tmp_path):
    stub = tmp_path / "kubectl"
    stub.write_text("#!/bin/sh\nexit 77\n")
    stub.chmod(0o755)
    result = subprocess.run(
        ["bash", str(WARM / "configure-chat-warm.sh")],
        capture_output=True, text=True,
        env={**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}",
             "CHAT_WARM_ACTION": "off"},
    )
    assert result.returncode == 0
    assert result.stdout == "" and result.stderr == ""


def test_enabled_invocation_reads_live_supervisor_digest_before_applying(tmp_path):
    job = {"spec": {"template": {"spec": {"containers": [{
        "name": "supervisor", "env": [{"name": "ADP_CHAT_SANDBOX_IMAGE", "value": IMAGE}],
    }]}}}}
    fixture = tmp_path / "job.json"
    fixture.write_text(json.dumps(job))
    applied = tmp_path / "applied.yaml"
    stub = tmp_path / "kubectl"
    stub.write_text(
        '#!/bin/bash\n'
        'case "$1" in\n'
        'get) cat "$WARM_TEST_JOB" ;;\n'
        'apply) cp "$3" "$WARM_TEST_APPLIED" ;;\n'
        'rollout) exit 0 ;;\n'
        '*) exit 77 ;;\n'
        'esac\n'
    )
    stub.chmod(0o755)
    environment = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}",
                   "CHAT_WARM_ACTION": "enable", "CHAT_WARM_CAPACITY": "3",
                   "WARM_TEST_JOB": str(fixture), "WARM_TEST_APPLIED": str(applied)}
    result = subprocess.run(["bash", str(WARM / "configure-chat-warm.sh")],
                            capture_output=True, text=True, env=environment)
    assert result.returncode == 0, result.stderr
    assert yaml.safe_load_all(applied.read_text())
    assert IMAGE in applied.read_text()
    job["spec"]["template"]["spec"]["containers"][0]["env"][0]["value"] = "sandbox:latest"
    fixture.write_text(json.dumps(job))
    applied.unlink()
    result = subprocess.run(["bash", str(WARM / "configure-chat-warm.sh")],
                            capture_output=True, text=True, env=environment)
    assert result.returncode != 0
    assert not applied.exists()
