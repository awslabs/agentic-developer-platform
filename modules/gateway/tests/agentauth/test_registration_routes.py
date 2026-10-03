"""The worker's own status and control-registration routes (#5028 AC4).

These routes exist to make the worker's unconditioned ``DynamoDBWebhookEventsUpdate``
grant removable. Every agent worker assumes the same platform IAM role, so SigV4
proves only "some worker"; the run identity comes from the HMAC-verified run
credential and the presenting pod from a TokenReview-verified workload token.

What is asserted here is the *route layer*: that both proofs are demanded, that a
caller cannot name another run or reach into the request for its own identity, and
that refusals are indistinguishable. The service's own decisions (row key
derivation, conditional writes, generation idempotency) are asserted against a real
emulated DynamoDB in ``test_registration.py``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.agentauth import registration_routes, routes
from src.agentauth.bootstrap import BootstrapRefusedError
from src.agentauth.registration import ControlRegistration, RegistrationRefusedError
from src.agentauth.store import AuthorityStoreError
from src.agentauth.workload import VerifiedPod, WorkloadRefusedError

CREDENTIAL = "adpr1.eyJhIjoxfQ.deadbeef"
POD = VerifiedPod(
    uid="pod-uid-a",
    name="agent-a",
    namespace="adp-agents",
    service_account="agent-scaledjob-sa",
    ip="10.0.1.5",
)


class StubService:
    """Records what the route layer passed down, and refuses on demand."""

    def __init__(self, *, refuse: Exception | None = None) -> None:
        self.refuse = refuse
        self.calls: list[tuple[str, dict]] = []

    def _maybe_refuse(self):
        if self.refuse is not None:
            raise self.refuse

    def _resolve(self, **kwargs):
        self._maybe_refuse()
        return SimpleNamespace(status=SimpleNamespace(value="running"), tenant_id="org", invocation_id="run")

    def record_status(self, *, credential_token, pod, status, fields=None):
        self.calls.append(("status", {"credential": credential_token, "pod": pod, "status": status, "fields": fields}))
        self._maybe_refuse()

    def register_control(self, *, credential_token, pod, token, token_expires_at):
        self.calls.append(("register", {"credential": credential_token, "pod": pod, "token": token, "expires": token_expires_at}))
        self._maybe_refuse()
        return ControlRegistration(generation=4, address=pod.ip, port=8770)

    def clear_control(self, *, credential_token, pod, generation):
        self.calls.append(("clear", {"credential": credential_token, "pod": pod, "generation": generation}))
        self._maybe_refuse()


class StubWorkloads:
    def __init__(self, *, pod=POD, refuse=False) -> None:
        self.pod = pod
        self.refuse = refuse
        self.tokens: list[str] = []

    def verify(self, token: str) -> VerifiedPod:
        self.tokens.append(token)
        if self.refuse or not token:
            raise WorkloadRefusedError("workload token refused")
        return self.pod


class StubRuntime:
    def __init__(self, workloads, *, flow_refused=False):
        self.workloads = workloads
        self.flow_refused = flow_refused
        self.store = SimpleNamespace(_read=lambda *args: {})

    def authenticate(self, credential_token, workload_token):
        return self.workloads.verify(workload_token), None, object(), object()

    async def validate_flow(self, record, grant):
        if self.flow_refused:
            raise BootstrapRefusedError("engine assignment halted")


@pytest.fixture
def harness():
    """Both routers mounted, as ``app.py`` mounts them.

    The operator's ``routes.py`` router is included *first and always*, because one
    of the things under test is that its ``/control/{run_id}/{action}`` pattern does
    not swallow a registration path.
    """

    def build(*, service=None, workloads=None, flow_refused=False):
        app = FastAPI()
        app.include_router(routes.router)
        app.include_router(registration_routes.router)
        stub_service = service or StubService()
        stub_workloads = workloads or StubWorkloads()
        runtime = registration_routes.RegistrationRuntime(
            service=stub_service,
            runtime=StubRuntime(stub_workloads, flow_refused=flow_refused),
        )
        # The transport check is asserted separately; overriding it here isolates
        # the credential and workload proofs from the SigV4 layer in front of them.
        app.dependency_overrides[routes.require_agent_transport] = lambda: None
        app.dependency_overrides[registration_routes.get_registration_runtime] = lambda: runtime
        return TestClient(app, raise_server_exceptions=False), stub_service, stub_workloads

    return build


def headers(*, credential=CREDENTIAL, workload="a-projected-workload-token"):
    sent = {}
    if credential is not None:
        sent["X-Adp-Run-Credential"] = credential
    if workload is not None:
        sent["X-Adp-Workload-Token"] = workload
    return sent


class TestRoutesAreActuallyReachable:
    """A shadowed route is an unenforced one.

    Regression: these handlers first lived at ``/internal/v1/agent/...``, where
    ``POST /control/registration/clear`` matched the coordinator router's
    ``POST /control/{run_id}/{action}`` with ``run_id="registration"``. That router
    is included first, so it won and the clear handler was unreachable — the
    control token and address would have stayed in every terminal row. Asserted by
    request rather than by inspecting the patterns.
    """

    @pytest.mark.parametrize(
        ("path", "body"),
        [
            ("/internal/v1/agent/self/status", {"status": "in_progress"}),
            (
                "/internal/v1/agent/self/control/registration",
                {"control_token": "t" * 40, "control_token_expires_at": "2026-09-13T13:00:00Z"},
            ),
            ("/internal/v1/agent/self/control/registration/clear", {"control_generation": 4}),
        ],
    )
    def test_each_route_reaches_its_own_handler(self, harness, path, body):
        client, service, _ = harness()

        response = client.post(path, json=body, headers=headers())

        assert response.status_code == 200
        assert len(service.calls) == 1


class TestBothProofsAreRequired:
    def test_a_missing_run_credential_is_refused(self, harness):
        # SigV4 alone cannot identify a run: every worker shares the platform role.
        client, service, _ = harness()

        response = client.post("/internal/v1/agent/self/status", json={"status": "in_progress"}, headers=headers(credential=None))

        assert response.status_code == 404
        assert service.calls == []

    def test_a_refused_workload_token_is_refused(self, harness):
        client, service, _ = harness(workloads=StubWorkloads(refuse=True))

        response = client.post("/internal/v1/agent/self/status", json={"status": "in_progress"}, headers=headers())

        assert response.status_code == 404
        assert service.calls == []

    def test_the_workload_token_is_verified_on_every_request(self, harness):
        # Not trusted from bootstrap: a credential that leaked out of its pod is
        # otherwise indistinguishable from the pod for the rest of its TTL.
        client, _, workloads = harness()

        for _ in range(3):
            client.post("/internal/v1/agent/self/status", json={"status": "in_progress"}, headers=headers())

        assert len(workloads.tokens) == 3

    def test_the_verified_pod_not_the_request_supplies_the_control_address(self, harness):
        client, service, _ = harness()

        response = client.post(
            "/internal/v1/agent/self/control/registration",
            json={"control_token": "t" * 40, "control_token_expires_at": "2026-09-13T13:00:00Z"},
            headers=headers(),
        )

        # A caller-supplied address is the control-channel redirect this path
        # exists to remove, so it comes from the TokenReview-verified pod.
        assert response.json()["control_address"] == POD.ip
        assert service.calls[0][1]["pod"] is POD


class TestACallerCannotNameAnotherRun:
    @pytest.mark.parametrize(
        "field",
        ["event_id", "arrived_at", "tenant_id", "owner", "parent_invocation_id", "control_address", "control_generation"],
    )
    def test_identity_and_protected_fields_are_rejected_at_parse_time(self, harness, field):
        # ``extra="forbid"``: an unrecognized field is a 422, not a silently
        # ignored one. Silently ignoring would let a caller believe it had set
        # ``tenant_id`` and leave no signal that it tried.
        client, service, _ = harness()

        response = client.post(
            "/internal/v1/agent/self/status",
            json={"status": "in_progress", field: "attacker-supplied"},
            headers=headers(),
        )

        assert response.status_code == 422
        assert service.calls == []

    def test_a_status_write_carries_no_run_key_at_all(self, harness):
        client, service, _ = harness()

        client.post(
            "/internal/v1/agent/self/status",
            json={"status": "complete", "summary": "done", "run_id": "keda-job-1"},
            headers=headers(),
        )

        # ``run_id`` is a free-text observability field, not the row key. Nothing
        # reaching the service can point at another run's row.
        assert set(service.calls[0][1]["fields"]) <= {"run_id", "summary"}


class TestRefusalsAreUniform:
    @pytest.mark.parametrize(
        "reason",
        ["not found", "execution_attempt_superseded", "execution_not_active:cancelled", "workload_binding_mismatch"],
    )
    def test_every_authorization_refusal_is_the_same_404(self, harness, reason):
        # A caller able to tell these apart learns whether a run it named exists
        # and how many attempts it has had.
        client, _, _ = harness(service=StubService(refuse=RegistrationRefusedError(reason)))

        response = client.post("/internal/v1/agent/self/status", json={"status": "in_progress"}, headers=headers())

        assert (response.status_code, response.json()["detail"]) == (404, "not found")

    @pytest.mark.parametrize("reason", ["unsupported status", "unsupported field", "invalid control token"])
    def test_the_callers_own_malformed_input_is_a_400(self, harness, reason):
        # Safe to distinguish: only reachable after authenticating as itself.
        client, _, _ = harness(service=StubService(refuse=RegistrationRefusedError(reason)))

        response = client.post("/internal/v1/agent/self/status", json={"status": "bogus"}, headers=headers())

        assert response.status_code == 400

    def test_a_conflicting_registration_is_a_409(self, harness):
        # Distinct from 404 because retrying is wrong rather than unauthorized:
        # the attempt already registered a different listener identity.
        client, _, _ = harness(service=StubService(refuse=RegistrationRefusedError("control already registered")))

        response = client.post(
            "/internal/v1/agent/self/control/registration",
            json={"control_token": "t" * 40, "control_token_expires_at": "2026-09-13T13:00:00Z"},
            headers=headers(),
        )

        assert response.status_code == 409

    def test_a_store_outage_is_a_503_not_a_404(self, harness):
        # A 404 would tell the worker its run does not exist, and the worker would
        # stop trying. An outage must be retryable.
        client, _, _ = harness(service=StubService(refuse=AuthorityStoreError("events table unavailable")))

        response = client.post("/internal/v1/agent/self/status", json={"status": "in_progress"}, headers=headers())

        assert response.status_code == 503

    def test_no_refusal_echoes_the_credential_or_the_workload_token(self, harness):
        client, _, _ = harness(service=StubService(refuse=RegistrationRefusedError("execution_attempt_superseded")))

        response = client.post("/internal/v1/agent/self/status", json={"status": "in_progress"}, headers=headers())

        body = response.text
        assert CREDENTIAL not in body
        assert "a-projected-workload-token" not in body
        assert "superseded" not in body


class TestResponsesAreNotCached:
    def test_a_registration_response_is_no_store(self, harness):
        # The response carries the assigned generation, which the listener binds
        # to. A cached one would hand a later attempt an earlier generation.
        client, _, _ = harness()

        response = client.post(
            "/internal/v1/agent/self/control/registration",
            json={"control_token": "t" * 40, "control_token_expires_at": "2026-09-13T13:00:00Z"},
            headers=headers(),
        )

        assert response.headers["Cache-Control"] == "no-store"


class TestBodyValidation:
    @pytest.mark.parametrize("token", ["", "short", "t" * 257])
    def test_an_implausible_control_token_is_refused(self, harness, token):
        client, service, _ = harness()

        response = client.post(
            "/internal/v1/agent/self/control/registration",
            json={"control_token": token, "control_token_expires_at": "2026-09-13T13:00:00Z"},
            headers=headers(),
        )

        assert response.status_code == 422
        assert service.calls == []

    @pytest.mark.parametrize("expiry", ["not-a-date", "2026-09-13", "2026-09-13T13:00:00+00:00", ""])
    def test_a_malformed_expiry_is_refused(self, harness, expiry):
        # The expiry is compared against ``now`` to decide whether a run is
        # controllable, so a format the reader cannot parse would read as expired.
        client, service, _ = harness()

        response = client.post(
            "/internal/v1/agent/self/control/registration",
            json={"control_token": "t" * 40, "control_token_expires_at": expiry},
            headers=headers(),
        )

        assert response.status_code == 422
        assert service.calls == []

    @pytest.mark.parametrize("generation", [0, -1, "four", 10_000_000])
    def test_an_implausible_generation_is_refused_at_clear(self, harness, generation):
        client, service, _ = harness()

        response = client.post(
            "/internal/v1/agent/self/control/registration/clear",
            json={"control_generation": generation},
            headers=headers(),
        )

        assert response.status_code == 422
        assert service.calls == []

    def test_an_oversized_free_text_field_is_refused_not_trimmed(self, harness):
        client, service, _ = harness()

        response = client.post(
            "/internal/v1/agent/self/status",
            json={"status": "failed", "error_message": "x" * 9000},
            headers=headers(),
        )

        assert response.status_code == 422
        assert service.calls == []


class TestTransportIsStillEnforced:
    def test_the_routes_refuse_without_the_iam_transport_dependency(self):
        # The overrides above bypass it deliberately; this asserts it is actually
        # attached, so the two proofs are additions to SigV4 rather than a
        # replacement for it.
        app = FastAPI()
        app.include_router(registration_routes.router)
        client = TestClient(app, raise_server_exceptions=False)

        response = client.post("/internal/v1/agent/self/status", json={"status": "in_progress"}, headers=headers())

        assert response.status_code == 403


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("status", {"status": "in_progress"}),
        ("control/registration", {"control_token": "t" * 40, "control_token_expires_at": "2026-09-13T13:00:00Z"}),
        (
            "control/registration/renew",
            {
                "control_token": "t" * 40,
                "control_token_expires_at": "2026-09-13T13:00:00Z",
                "control_generation": 1,
                "expected_epoch": 1,
                "rotation_id": "11111111-2222-4333-8444-555555555555",
            },
        ),
        ("control/registration/state", {"control_generation": 1}),
    ],
)
def test_halted_flow_cannot_register_or_report_in_progress(harness, path, body):
    client, service, _ = harness(flow_refused=True)
    response = client.post(f"/internal/v1/agent/self/{path}", json=body, headers=headers())
    assert response.status_code == 404
    assert service.calls == []


@pytest.mark.parametrize(
    ("path", "body"),
    [("status", {"status": "failed"}), ("control/registration/clear", {"control_generation": 4})],
)
def test_halted_flow_can_still_report_termination_and_clear(harness, path, body):
    client, service, _ = harness(flow_refused=True)
    response = client.post(f"/internal/v1/agent/self/{path}", json=body, headers=headers())
    assert response.status_code == 200
    assert len(service.calls) == 1


@pytest.mark.parametrize("merged", [False, True])
def test_completion_checks_authenticated_assignment_before_recording(harness, monkeypatch, merged):
    from contextlib import asynccontextmanager
    from unittest.mock import AsyncMock

    from src.orchestration.run_reports import RunReportError

    @asynccontextmanager
    async def session():
        yield object()

    client, service, _ = harness()
    runtime = client.app.dependency_overrides[registration_routes.get_registration_runtime]()
    runtime.runtime.store._read = lambda *args: {"orchestration_node_id": {"S": "assigned-node"}}
    gate = AsyncMock(side_effect=None if merged else RunReportError("reviewer_merge_not_delivered"))
    monkeypatch.setattr("src.shared.database.get_session_factory", lambda: session)
    monkeypatch.setattr("src.orchestration.review_assignment.require_reviewer_merge", gate)
    response = client.post("/internal/v1/agent/self/status", json={"status": "complete"}, headers=headers())
    assert response.status_code == (200 if merged else 409)
    assert gate.call_args.kwargs == {"org_id": "org", "node_id": "assigned-node", "run_id": "run"}
    assert len(service.calls) == int(merged)


@pytest.mark.parametrize("error,code", [(RegistrationRefusedError("not found"), 404), (AuthorityStoreError("unavailable"), 503)])
def test_completion_authentication_refusals_do_not_become_server_errors(harness, error, code):
    client, service, _ = harness(service=StubService(refuse=error))
    response = client.post("/internal/v1/agent/self/status", json={"status": "complete"}, headers=headers())
    assert response.status_code == code
    assert service.calls == []
