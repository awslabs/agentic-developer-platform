"""Warm reservations cannot acquire an accepted chat turn's identity or lifecycle."""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def test_warming_never_owns_assigned_pods_or_queue_receipts():
    priority, reservation, cache = yaml.safe_load_all((ROOT / "chat-warm/chat-warm.yaml").read_text())
    assert [priority["kind"], reservation["kind"], cache["kind"]] == ["PriorityClass", "Deployment", "DaemonSet"]
    assert priority["value"] < 0 and priority["globalDefault"] is False
    for resource in (reservation, cache):
        pod = resource["spec"]["template"]
        assert pod["spec"]["priorityClassName"] == priority["metadata"]["name"]
        assert pod["spec"]["automountServiceAccountToken"] is False
        assert "adp.io/chat-sandbox" not in pod["metadata"]["labels"]
        assert "adp.io/run-hash" not in pod["metadata"]["labels"]
        assert "serviceAccountName" not in pod["spec"]
        container = pod["spec"]["containers"][0]
        assert "env" not in container and "volumeMounts" not in container
    assert "ADP_CHAT_SUPERVISOR_QUEUE_URL" not in (ROOT / "chat-warm/chat-warm.yaml").read_text()
