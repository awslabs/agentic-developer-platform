"""Strict identity double for delegated Activity route tests.

TokenReview and capability cryptography are covered by the real chat-data
runtime tests. These reader tests must still present a workload and exercise
both identity checks instead of overriding the route's authentication dependency.
"""

from datetime import UTC, datetime
from types import SimpleNamespace

from src.activity import chat_work
from src.agentauth.chat_capability import ChatAuthorizationRefusedError
from src.agentauth.workload import WORKLOAD_HEADER, WorkloadRefusedError

WORKLOAD_TOKEN = "synthetic.activity.workload"
HEADERS = {"Authorization": "Bearer delegated", WORKLOAD_HEADER: WORKLOAD_TOKEN}


def pin_activity_query_clock(monkeypatch):
    """Keep October provider fixtures inside the query retention window."""

    class QueryClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 5, tzinfo=UTC).astimezone(tz)

    monkeypatch.setattr(chat_work, "datetime", QueryClock)


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
    return SimpleNamespace(verify_workload=verify_workload), capabilities
