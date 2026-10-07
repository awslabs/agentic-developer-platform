"""Opt-in warm infrastructure is the only thing an operator action may remove."""

import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "chat-warm/configure-chat-warm.sh"
IMAGE = f"registry.example.test/sandbox@sha256:{'a' * 64}"


def invoke(tmp_path, action, failure="none"):
    job = {"spec": {"template": {"spec": {"containers": [{
        "name": "supervisor", "env": [{"name": "ADP_CHAT_SANDBOX_IMAGE", "value": IMAGE}],
    }]}}}}
    fixture = tmp_path / "supervisor.json"
    fixture.write_text(json.dumps(job))
    calls = tmp_path / "calls"
    stub = tmp_path / "kubectl"
    stub.write_text(
        '#!/bin/bash\n'
        'echo "$*" >> "$WARM_TEST_CALLS"\n'
        'case "$1" in\n'
        'get) cat "$WARM_TEST_JOB" ;;\n'
        'apply) [[ "$WARM_TEST_FAILURE" != apply ]] ;;\n'
        'rollout) [[ "$WARM_TEST_FAILURE" != "$3" ]] ;;\n'
        'delete) [[ "$WARM_TEST_FAILURE" != delete || "$2" != deployment/chat-sandbox-node-reserve ]] ;;\n'
        '*) exit 77 ;;\n'
        'esac\n'
    )
    stub.chmod(0o755)
    result = subprocess.run(
        ["bash", str(SCRIPT)], capture_output=True, text=True,
        env={**os.environ, "CHAT_WARM_ACTION": action, "CHAT_WARM_CAPACITY": "2",
             "PATH": f"{tmp_path}:{os.environ['PATH']}", "WARM_TEST_JOB": str(fixture),
             "WARM_TEST_CALLS": str(calls), "WARM_TEST_FAILURE": failure},
    )
    return result, calls.read_text().splitlines() if calls.exists() else []


def assert_warm_only_deletion(calls):
    deletions = [call for call in calls if call.startswith("delete ")]
    assert len(deletions) == 2
    assert deletions[0].startswith("delete deployment/chat-sandbox-node-reserve daemonset/chat-sandbox-image-prepull -n adp-gateway-agents")
    assert deletions[1] == "delete priorityclass/adp-chat-warm-reserve --ignore-not-found"
    assert all("chat-agent-worker" not in call and "adp-chat-supervisor" not in call for call in deletions)


@pytest.mark.parametrize("failure", ["apply", "daemonset/chat-sandbox-image-prepull", "deployment/chat-sandbox-node-reserve"])
def test_enable_failure_cleans_partial_reservations_and_reports_failure(tmp_path, failure):
    result, calls = invoke(tmp_path, "enable", failure)
    assert result.returncode != 0
    assert calls[0].startswith("get job adp-chat-supervisor-once")
    assert calls[1].startswith("apply -f ")
    assert_warm_only_deletion(calls)


def test_success_waits_for_cache_and_bounded_capacity_without_deleting(tmp_path):
    result, calls = invoke(tmp_path, "enable")
    assert result.returncode == 0, result.stderr
    assert [call.split()[0] for call in calls] == ["get", "apply", "rollout", "rollout"]
    assert "daemonset/chat-sandbox-image-prepull" in calls[2]
    assert "deployment/chat-sandbox-node-reserve" in calls[3]


def test_disable_releases_only_warm_reservations_even_when_first_delete_fails(tmp_path):
    result, calls = invoke(tmp_path, "disable", "delete")
    assert result.returncode != 0
    assert_warm_only_deletion(calls)
    assert len(calls) == 2


def test_disable_succeeds_without_reading_supervisor_or_receiving_turns(tmp_path):
    result, calls = invoke(tmp_path, "disable")
    assert result.returncode == 0, result.stderr
    assert_warm_only_deletion(calls)
    assert len(calls) == 2
