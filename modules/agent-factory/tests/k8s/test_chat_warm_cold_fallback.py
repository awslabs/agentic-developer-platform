"""Warm-capacity failures cannot select a different queue or sandbox executor."""

import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WARM = ROOT / "chat-warm"
IMAGE = f"registry.example.test/sandbox@sha256:{'b' * 64}"


@pytest.mark.parametrize("sandbox_image,rollout_fails", [
    ("sandbox:latest", False),
    (IMAGE, True),
])
def test_unready_warm_capacity_does_not_block_isolated_cold_turn(tmp_path, sandbox_image, rollout_fails):
    job = {"spec": {"template": {"spec": {"containers": [{
        "name": "supervisor", "env": [{"name": "ADP_CHAT_SANDBOX_IMAGE", "value": sandbox_image}],
    }]}}}}
    fixture = tmp_path / "supervisor.json"
    fixture.write_text(json.dumps(job))
    commands = tmp_path / "calls"
    applied = tmp_path / "applied.yaml"
    stub = tmp_path / "kubectl"
    stub.write_text(
        '#!/bin/bash\n'
        'echo "$1" >> "$WARM_TEST_CALLS"\n'
        'case "$1" in\n'
        'get) cat "$WARM_TEST_JOB" ;;\n'
        'apply) cp "$3" "$WARM_TEST_APPLIED" ;;\n'
        'rollout) exit 42 ;;\n'
        'delete) exit 0 ;;\n'
        '*) exit 77 ;;\n'
        'esac\n'
    )
    stub.chmod(0o755)
    environment = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}",
                   "CHAT_WARM_ACTION": "enable", "CHAT_WARM_CAPACITY": "3",
                   "WARM_TEST_JOB": str(fixture), "WARM_TEST_CALLS": str(commands),
                   "WARM_TEST_APPLIED": str(applied)}
    failed = subprocess.run(["bash", str(WARM / "configure-chat-warm.sh")],
                            env=environment, capture_output=True, text=True)
    assert failed.returncode != 0
    calls = commands.read_text().splitlines()
    if rollout_fails:
        assert calls == ["get", "apply", "rollout", "delete", "delete"]
        priority, reservation, cache = yaml.safe_load_all(applied.read_text())
        assert priority["value"] < 0
        assert reservation["spec"]["replicas"] == 3
        assert cache["kind"] == "DaemonSet"
    else:
        assert calls == ["get"]
        assert not applied.exists()

    cold = subprocess.run(
        ["node", "-r", str(ROOT / "agent/node_modules/ts-node/register"),
         str(ROOT / "tests/k8s/chat_warm_isolation.cjs")],
        cwd=ROOT / "agent", capture_output=True, text=True, env=environment,
    )
    assert cold.returncode == 0, cold.stderr
    assert "INPUT_QUEUE_URL" not in (WARM / "chat-warm.yaml").read_text()
    assert "AGENT_ENTRYPOINT" not in (WARM / "chat-warm.yaml").read_text()
