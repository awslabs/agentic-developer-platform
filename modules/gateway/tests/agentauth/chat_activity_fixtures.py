"""Strict identity double for delegated Activity route tests.

TokenReview and capability cryptography are covered by the real chat-data
runtime tests. These reader tests must still present a workload and exercise
both identity checks instead of overriding the route's authentication dependency.
"""

from types import SimpleNamespace

from src.agentauth.chat_capability import ChatAuthorizationRefusedError
from src.agentauth.workload import WORKLOAD_HEADER, WorkloadRefusedError

WORKLOAD_TOKEN = "synthetic.activity.workload"
HEADERS = {"Authorization": "Bearer delegated", WORKLOAD_HEADER: WORKLOAD_TOKEN}


def workload_runtime(capabilities):
    pod = object()

    def verify_workload(token):
        if token != WORKLOAD_TOKEN:
            raise WorkloadRefusedError("test workload refused")
        return pod

    def verify_pod(token, presented_pod, *, now):
        if token != "delegated" or presented_pod is not pod:
            raise ChatAuthorizationRefusedError("test capability workload mismatch")

    capabilities.verify_pod.side_effect = verify_pod
    return SimpleNamespace(workloads=SimpleNamespace(verify=verify_workload)), capabilities
