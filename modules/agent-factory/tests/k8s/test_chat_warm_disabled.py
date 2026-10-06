"""The default setting must not modify the existing chat or Terraform paths."""

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_default_off_never_invokes_cluster_tools_even_with_stale_configuration(tmp_path):
    calls = tmp_path / "calls"
    for tool in ("kubectl", "node"):
        stub = tmp_path / tool
        stub.write_text(f"#!/bin/sh\necho {tool} >> '{calls}'\nexit 77\n")
        stub.chmod(0o755)
    environment = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}",
                   "SANDBOX_IMAGE_DIGEST": "invalid", "CHAT_WARM_CAPACITY": "500"}
    environment.pop("CHAT_WARM_ACTION", None)
    result = subprocess.run(["bash", str(ROOT / "chat-warm/configure-chat-warm.sh")],
                            env=environment, capture_output=True, text=True)
    assert result.returncode == 0
    assert result.stdout == result.stderr == ""
    assert not calls.exists()


def test_warm_resources_are_outside_existing_chat_deployment_path():
    existing = ROOT / "agent/k8s"
    warm = ROOT / "chat-warm"
    assert not warm.is_relative_to(existing)
    manifest = (warm / "chat-warm.yaml").read_text()
    assert "chat-agent-worker" not in manifest
    assert "adp.io/chat-sandbox" not in manifest
    assert "AGENT_ENTRYPOINT" not in manifest
