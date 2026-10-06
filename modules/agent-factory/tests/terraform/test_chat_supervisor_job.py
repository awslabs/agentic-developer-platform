"""Offline checks for the one-shot supervisor's dedicated identity and startup."""

import os
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
JOB = yaml.safe_load((ROOT / "agent/k8s/chat-supervisor-job.yaml").read_text())
ROLE = (ROOT / "infra/chat-worker-iam.tf").read_text()


def test_supervisor_job_stays_suspended_until_delegated_transport_is_ready():
    assert JOB["apiVersion"] == "batch/v1"
    assert JOB["kind"] == "Job"
    assert JOB["metadata"]["namespace"] == "adp-gateway-agents"
    assert JOB["spec"]["suspend"] is True
    assert JOB["spec"]["backoffLimit"] == 0
    assert JOB["spec"]["activeDeadlineSeconds"] == 900


def test_supervisor_job_projects_only_its_own_aws_identity():
    pod = JOB["spec"]["template"]["spec"]
    assert pod["serviceAccountName"] == "adp-chat-supervisor"
    assert pod["automountServiceAccountToken"] is True
    assert 'name      = "adp-chat-supervisor"' in ROLE
    assert '"eks.amazonaws.com/role-arn" = aws_iam_role.chat_supervisor.arn' in ROLE
    assert pod["volumes"] == [{"name": "supervisor-identity", "projected": {"sources": [
        {"serviceAccountToken": {"audience": "sts.amazonaws.com", "expirationSeconds": 600, "path": "token"}},
    ]}}]
    assert len(pod["containers"]) == 1
    container = pod["containers"][0]
    env = {entry["name"]: entry["value"] for entry in container["env"]}
    assert len(env) == len(container["env"])
    assert container["image"] == "REPLACE_WITH_SUPERVISOR_IMAGE"
    assert env["AGENT_ENTRYPOINT"] == "chat-supervisor"
    assert env["ADP_CHAT_SUPERVISOR_ROLE_ARN"] == env["AWS_ROLE_ARN"] == "REPLACE_WITH_SUPERVISOR_ROLE_ARN"
    assert env["AWS_WEB_IDENTITY_TOKEN_FILE"] == "/var/run/secrets/adp-chat-supervisor/token"
    assert env["AWS_EC2_METADATA_DISABLED"] == "true"
    assert env["ADP_CHAT_SANDBOX_IMAGE"] == "REPLACE_WITH_SANDBOX_IMAGE_DIGEST"
    assert env["ADP_CHAT_SUPERVISOR_QUEUE_URL"] == "REPLACE_WITH_CHAT_FIFO_URL"
    assert env["ADP_CHAT_DATA_URL"] == "REPLACE_WITH_GATEWAY_HTTPS_ORIGIN"
    assert container["volumeMounts"] == [{"name": "supervisor-identity",
        "mountPath": "/var/run/secrets/adp-chat-supervisor", "readOnly": True}]
    assert "envFrom" not in container
    assert not {"AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"} & env.keys()
    security = container["securityContext"]
    assert security["runAsNonRoot"] and security["readOnlyRootFilesystem"]
    assert not security["allowPrivilegeEscalation"]
    assert security["capabilities"]["drop"] == ["ALL"]
    assert not any(key in pod for key in ("hostNetwork", "hostPID", "hostIPC", "hostPath"))


def test_job_entrypoint_invokes_supervisor_not_retired_worker(tmp_path):
    node = tmp_path / "node"
    node.write_text('#!/bin/sh\nprintf "%s\\n" "$*"\n')
    node.chmod(0o755)
    result = subprocess.run(
        ["/bin/sh", str(ROOT / "agent/startup.sh")],
        capture_output=True, text=True, check=False,
        env={"PATH": f"{tmp_path}:{os.environ['PATH']}", "AGENT_ENTRYPOINT": "chat-supervisor"},
    )
    assert result.returncode == 0
    assert result.stdout.splitlines()[-1] == "dist/complex-task-chat/chat-supervisor.js"
    assert "complex-task-chat-agent.js" not in result.stdout


def render_job(overrides=None):
    values = {
        "SUPERVISOR_IMAGE": f"registry.example.test/supervisor@sha256:{'a' * 64}",
        "SUPERVISOR_ROLE_ARN": "arn:aws:iam::000000000000:role/adp-test-chat-supervisor-role",
        "AWS_REGION": "us-east-1",
        "CHAT_FIFO_URL": "https://sqs.us-east-1.amazonaws.com/000000000000/chat-tasks.fifo",
        "SANDBOX_IMAGE_DIGEST": f"registry.example.test/sandbox@sha256:{'b' * 64}",
        "GATEWAY_HTTPS_ORIGIN": "https://gateway.example.test",
        "SANDBOX_CA_CONFIGMAP": "chat-sandbox-gateway-ca-" + "c" * 16,
    }
    values.update(overrides or {})
    return subprocess.run(
        ["node", str(ROOT / "agent/k8s/render-chat-supervisor-job.mjs")],
        capture_output=True, text=True, check=False, env={"PATH": os.environ["PATH"], **values},
    )


def test_renderer_binds_supervisor_identity_and_keeps_job_suspended():
    result = render_job()
    assert result.returncode == 0
    assert result.stderr == ""
    manifest = yaml.safe_load(result.stdout)
    assert manifest["spec"]["suspend"] is True
    pod = manifest["spec"]["template"]["spec"]
    env = {item["name"]: item["value"] for item in pod["containers"][0]["env"]}
    assert env["AWS_ROLE_ARN"] == "arn:aws:iam::000000000000:role/adp-test-chat-supervisor-role"
    assert env["ADP_CHAT_SUPERVISOR_ROLE_ARN"] == env["AWS_ROLE_ARN"]
    assert env["ADP_CHAT_SANDBOX_IMAGE"].endswith(f"@sha256:{'b' * 64}")
    assert "REPLACE_WITH_" not in result.stdout
    assert env["ADP_CHAT_SANDBOX_CA_CONFIGMAP"] == "chat-sandbox-gateway-ca-" + "c" * 16


def test_renderer_refuses_mismatched_role_queue_image_or_gateway():
    invalid_assignments = (
        {"SUPERVISOR_ROLE_ARN": "arn:aws:iam::000000000000:role/adp-test-chat-worker-role"},
        {"SUPERVISOR_ROLE_ARN": "arn:aws:iam::111111111111:role/adp-test-chat-supervisor-role"},
        {"CHAT_FIFO_URL": "https://sqs.us-east-1.amazonaws.com/111111111111/chat-tasks.fifo"},
        {"SANDBOX_IMAGE_DIGEST": "registry.example.test/sandbox:latest"},
        {"GATEWAY_HTTPS_ORIGIN": "https://gateway.example.test/internal"},
        {"GATEWAY_HTTPS_ORIGIN": "http://gateway.example.test"},
        {"SANDBOX_CA_CONFIGMAP": ""},
        {"SANDBOX_CA_CONFIGMAP": "platform-secrets"},
        {"SUPERVISOR_IMAGE": f"registry.example.test/sandbox@sha256:{'b' * 64}"},
    )
    for change in invalid_assignments:
        result = render_job(change)
        assert result.returncode == 1, change
        assert result.stdout == "", change
        assert "rendering refused" in result.stderr
