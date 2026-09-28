"""Only positive evidence about the original worker permits crash cleanup."""

import httpx
import pytest

from src.agentauth.workload import KubernetesWorkloadVerifier


@pytest.mark.parametrize(
    "status,uid,phase,container_state,expected",
    [
        (200, "original", "Failed", {"terminated": {"exitCode": 1}}, True),
        (200, "original", "Succeeded", {"terminated": {"exitCode": 0}}, True),
        (200, "replacement", "Failed", {"terminated": {}}, False),
        (200, "original", "Running", {"running": {}}, False),
        (200, "original", "Failed", {"running": {}}, False),
        (404, "original", "Failed", {"terminated": {}}, False),
        (403, "original", "Failed", {"terminated": {}}, False),
    ],
)
def test_exact_pod_and_worker_exit_are_required(tmp_path, status, uid, phase, container_state, expected):
    token = tmp_path / "token"
    token.write_text("gateway-identity")

    def respond(request):
        assert request.url.path == "/api/v1/namespaces/adp-agents/pods/worker"
        return httpx.Response(
            status,
            json={
                "metadata": {"uid": uid},
                "spec": {"serviceAccountName": "agent-scaledjob-sa"},
                "status": {"phase": phase, "containerStatuses": [{"name": "agent-worker", "state": container_state}]},
            },
        )

    with httpx.Client(base_url="https://kubernetes.test", transport=httpx.MockTransport(respond)) as client:
        verifier = KubernetesWorkloadVerifier(client=client, image_digests=frozenset({"sha256:" + "a" * 64}), gateway_token_path=token)
        assert verifier.has_exited(name="worker", uid="original") is expected
