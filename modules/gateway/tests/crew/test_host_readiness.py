"""The ec2 Crew target says "starting" until the box's Crew answers — issue #9619."""

import httpx
import pytest
from fastapi import HTTPException

from src.crew import host_readiness, proxy
from src.crew.targets import CrewLease, CrewOwner, ResolvedCrewTarget


def _target(run_id="run-a", generation=1):
    endpoint = "https://10.4.0.3:8443"
    return ResolvedCrewTarget(
        endpoint,
        CrewLease(
            tenant_id="tenant-a",
            user_id="user-a",
            target_ref="box-a",
            target_kind="ec2-instance",
            generation=generation,
            expires_at=4_000_000_000,
            endpoint=endpoint,
        ),
        "ready",
        host_run_id=run_id,
        host_instance_ref="i-example",
        host_fingerprint="a" * 64,
    )


class FakeClient:
    def __init__(self, outcome):
        self.outcome = outcome
        self.calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, **kwargs):
        self.calls += 1
        assert url == "https://10.4.0.3:8443/" and kwargs["follow_redirects"] is False
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return httpx.Response(self.outcome)


@pytest.fixture(autouse=True)
def fresh_cache(monkeypatch):
    host_readiness._ready.clear()
    host_readiness._last_negative.clear()
    clock = {"now": 100.0}
    monkeypatch.setattr(host_readiness, "_monotonic", lambda: clock["now"])
    return clock


def _install(monkeypatch, client):
    from src.crew import handoff

    async def client_for(target):
        return client

    monkeypatch.setattr(handoff, "crew_host_client", client_for)
    return client


@pytest.mark.parametrize("status", [200, 302, 401, 404])
async def test_any_http_answer_through_the_ingress_means_crew_is_up_and_sticks(monkeypatch, status):
    client = _install(monkeypatch, FakeClient(status))
    assert await host_readiness.crew_host_answers(_target()) is True
    assert await host_readiness.crew_host_answers(_target()) is True
    assert client.calls == 1, "a positive answer is remembered for the host run"


async def test_a_502_means_nothing_listens_behind_the_ingress_yet(monkeypatch, fresh_cache):
    client = _install(monkeypatch, FakeClient(502))
    assert await host_readiness.crew_host_answers(_target()) is False
    assert await host_readiness.crew_host_answers(_target()) is False
    assert client.calls == 1, "a negative answer is not re-asked within the TTL"
    fresh_cache["now"] += host_readiness.NEGATIVE_TTL_SECONDS
    assert await host_readiness.crew_host_answers(_target()) is False
    assert client.calls == 2


async def test_transport_failures_are_not_yet_never_errors(monkeypatch):
    _install(monkeypatch, FakeClient(httpx.ConnectError("refused")))
    assert await host_readiness.crew_host_answers(_target()) is False

    async def no_trust(target):
        raise HTTPException(503, detail={"error": "crew_host_trust_unavailable"})

    from src.crew import handoff

    monkeypatch.setattr(handoff, "crew_host_client", no_trust)
    host_readiness._last_negative.clear()
    assert await host_readiness.crew_host_answers(_target()) is False


async def test_a_new_host_run_is_probed_afresh(monkeypatch):
    client = _install(monkeypatch, FakeClient(200))
    assert await host_readiness.crew_host_answers(_target("run-a")) is True
    assert await host_readiness.crew_host_answers(_target("run-b")) is True
    assert client.calls == 2


async def test_active_target_names_a_starting_crew_distinctly():
    class Resolver:
        async def resolve(self, owner, *, now):
            return ResolvedCrewTarget(
                _target().endpoint, _target().lease, "starting", host_run_id="run-a", host_instance_ref="i-example", host_fingerprint="a" * 64
            )

    with pytest.raises(HTTPException) as refused:
        await proxy.active_crew_target(CrewOwner("tenant-a", "user-a"), Resolver())
    assert refused.value.status_code == 409
    assert refused.value.detail == {"error": "crew_starting"}
