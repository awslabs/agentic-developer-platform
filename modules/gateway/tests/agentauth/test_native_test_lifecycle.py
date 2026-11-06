"""Finite original cleanup and separate behavior/accounting proof, offline."""

from datetime import timedelta
from uuid import uuid4

import httpx
import pytest
from botocore.exceptions import EndpointConnectionError

from src.agentauth.test_run_admission_store import AdmissionRefusedError
from tests.agentauth import test_native_test_facade_admission as wire
from tests.migrations.conftest_postgres import pg_server, pg_url  # noqa: F401

admission, sessions, native, all_in = wire.admission, wire.sessions, wire.native, wire.all_in
facade_tool, facade, http_wire = wire.facade_tool, wire.facade, wire.http_wire


async def call(w):
    reply = await w.client.post(
        "/agent/tools/call", json={key: value for key, value in w.f.call.items() if key not in {"schema_version", "action"}}, headers=w.headers
    )
    assert reply.status_code == 200, reply.text
    return reply.json()


async def status(w):
    async with w.client_factory(transport=httpx.ASGITransport(app=w.app), base_url="https://gateway.internal") as client:
        response = await client.get("/v1/native-tests/runs/" + w.f.approval.run_id + "/status", headers=w.headers)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    return response.json()


async def close(w, execution_id, operation_id=None):
    return await w.client.post(
        "/agent/tools/executions/" + execution_id + "/close?tool=websearch___search",
        headers={**w.headers, "x-adp-native-test-operation": operation_id or str(uuid4())},
    )


async def test_owned_close_uses_original_hold_and_status_never_conflates_cleanup_with_billing(http_wire):
    w = http_wire
    receipt = await call(w)
    before = await status(w)
    assert before["principal"] == {"user_id": w.f.approval.principal.user_id, "tenant_id": w.f.approval.principal.tenant_id}
    assert before["funding_status"] == "held" and before["accounting_status"] == "pending"
    assert before["held_usd"] == "8.000000" and before["governance_complete"] is False
    assert before["executions"][0]["cleanup_status"] == "pending"
    reply = await close(w, receipt["execution_id"])
    assert reply.status_code == 200, reply.text
    assert reply.json() == {"execution_id": receipt["execution_id"], "state": "closed", "cleanup_status": "confirmed"}
    final = await status(w)
    execution = final["executions"][0]
    assert final["held_usd"] == before["held_usd"]
    assert final["funding_status"] == "held" and final["accounting_status"] == "pending"
    assert final["governance_complete"] is False
    assert execution["original_operation_id"] == w.f.operation
    assert execution["reservation_id"] == receipt["receipt_id"]
    assert execution["cleanup_status"] == "confirmed" and len(execution["cleanup_evidence_sha256"]) == 64
    assert execution["cleanup_confirmed_at"] <= execution["cleanup_deadline_at"] <= final["run_started_at"] + 600
    assert execution["session_deadline_at"] <= final["run_started_at"] + 300
    assert execution["cleanup_attempts_used"] == 1
    assert len(w.state["provider"]) == len(w.state["kms"]) == 1


async def test_original_owner_can_cleanup_after_dispatch_revocation_without_restoring_new_work(http_wire):
    w = http_wire
    receipt = await call(w)
    await w.f.f.issuer.revoke(w.f.f.owner, run_id=w.f.approval.run_id)
    w.f.f.fixture.clock[0] += timedelta(seconds=301)
    closed = await close(w, receipt["execution_id"])
    assert closed.status_code == 200, closed.text
    assert (await status(w))["executions"][0]["cleanup_status"] == "confirmed"
    with pytest.raises(AdmissionRefusedError):
        await w.f.f.issuer.verify(w.f.f.login, w.f.token)


@pytest.mark.parametrize("field", ["tool", "execution_id"])
async def test_terminal_evidence_cannot_change_original_execution_or_tool_family(http_wire, field):
    w = http_wire
    receipt = await call(w)
    assert (await close(w, receipt["execution_id"])).status_code == 200
    client = w.f.f.fixture.client
    row = client.get_item(TableName="offline-facade-executions", Key={"execution_id": {"S": receipt["execution_id"]}})["Item"]
    if field == "tool":
        row["tool"] = {"S": "browser___start"}
        row["cleanup_evidence"] = {"S": "provider_terminated"}
        client.put_item(TableName="offline-facade-executions", Item=row)
    else:
        # Simulate a corrupt/misrouted read; execution_id is the table key.
        original = w.app.state.native_test_runtime.tool_lifecycle.execution_client

        class WrongExecutionRead:
            def get_item(self, **kwargs):
                reply = original.get_item(**kwargs)
                reply["Item"]["execution_id"] = {"S": str(uuid4())}
                return reply

        w.app.state.native_test_runtime.tool_lifecycle.execution_client = WrongExecutionRead()
    current = await status(w)
    assert current["executions"][0]["cleanup_status"] == "unknown"
    assert current["held_usd"] == "8.000000" and current["governance_complete"] is False


async def test_explicit_cleanup_attempts_are_finite_and_replay_has_no_authority(http_wire):
    w = http_wire
    receipt = await call(w)
    operation_id = str(uuid4())
    assert (await close(w, receipt["execution_id"], operation_id)).status_code == 200
    replay = await close(w, receipt["execution_id"], operation_id)
    assert replay.status_code == 202 and replay.json()["native_status"]["replay"] is True
    assert (await close(w, receipt["execution_id"])).status_code == 200
    refused = await close(w, receipt["execution_id"])
    assert refused.status_code == 403
    assert (await status(w))["executions"][0]["cleanup_attempts_used"] == 2
    assert len(w.state["provider"]) == 1


async def test_reconciler_and_explicit_owner_share_original_cleanup_attempts(http_wire, monkeypatch):
    from src.internal import tools_authorize as routes

    w = http_wire
    receipt = await call(w)
    assert (await close(w, receipt["execution_id"])).status_code == 200
    monkeypatch.setenv("ADP_TOOLS_RECONCILER_ROLE_ARN", "arn:aws:iam::123456789012:role/offline-reconciler")
    monkeypatch.setattr(
        routes,
        "verified_caller_identity",
        lambda request, settings: "arn:aws:sts::123456789012:assumed-role/offline-reconciler/pod",
    )
    body = {
        "tenant_id": w.f.approval.designation.tenant_id,
        "run_id": w.f.approval.run_id,
        "operation_id": str(uuid4()),
        "execution_id": receipt["execution_id"],
        "tool": "websearch___search",
    }
    async with w.client_factory(transport=httpx.ASGITransport(app=w.app), base_url="https://gateway.internal") as client:
        allowed = await client.post("/internal/v1/tools/native-cleanup", json=body)
        assert allowed.status_code == 200, allowed.text
        assert allowed.json()["native_cleanup"]["attempt"] == 2
        replay = await client.post("/internal/v1/tools/native-cleanup", json=body)
        assert replay.status_code == 200 and replay.json()["native_status"]["replay"] is True
        exhausted = await client.post("/internal/v1/tools/native-cleanup", json={**body, "operation_id": str(uuid4())})
        assert exhausted.status_code == 403
    current = await status(w)
    assert current["executions"][0]["cleanup_attempts_used"] == 2
    assert current["held_usd"] == "8.000000"


@pytest.mark.parametrize("caller", [None, "arn:aws:sts::123456789012:assumed-role/offline-facade/pod"])
async def test_reconciler_requires_exact_verified_iam_role_before_original_attempt(http_wire, monkeypatch, caller):
    from src.internal import tools_authorize as routes

    w = http_wire
    receipt = await call(w)
    monkeypatch.setenv("ADP_TOOLS_RECONCILER_ROLE_ARN", "arn:aws:iam::123456789012:role/offline-reconciler")
    monkeypatch.setattr(routes, "verified_caller_identity", lambda request, settings: caller)
    async with w.client_factory(transport=httpx.ASGITransport(app=w.app), base_url="https://gateway.internal") as client:
        reply = await client.post(
            "/internal/v1/tools/native-cleanup",
            json={
                "tenant_id": w.f.approval.designation.tenant_id,
                "run_id": w.f.approval.run_id,
                "operation_id": str(uuid4()),
                "execution_id": receipt["execution_id"],
                "tool": "websearch___search",
            },
        )
    assert reply.status_code == 403
    assert (await status(w))["executions"][0]["cleanup_attempts_used"] == 0


@pytest.mark.parametrize("failure", ["horizon", "hold"])
async def test_missing_original_funds_or_exhausted_cleanup_horizon_cannot_buy_a_new_allowance(http_wire, failure):
    w = http_wire
    receipt = await call(w)
    if failure == "horizon":
        w.f.f.fixture.clock[0] += timedelta(seconds=511)
    else:
        target = w.f.f.service._target(w.f.approval.authority())
        await (await w.f.f.fixture.reservations._get_client()).hdel(target.key(), receipt["receipt_id"])
    reply = await close(w, receipt["execution_id"])
    assert reply.status_code == 403
    current = await status(w)
    assert current["governance_complete"] is False
    if failure == "hold":
        assert current["accounting_status"] == current["funding_status"] == "unknown"
        assert current["held_usd"] is None


async def test_lost_cleanup_ack_keeps_original_attempt_consumed_and_never_replays_close(http_wire, monkeypatch):
    w = http_wire
    receipt = await call(w)
    original = w.f.f.fixture.client.transact_write_items

    def lost(**kwargs):
        reply = original(**kwargs)
        if any(item.get("Put", {}).get("Item", {}).get("kind", {}).get("S") == "native_test_cleanup_attempt_v1" for item in kwargs["TransactItems"]):
            raise EndpointConnectionError(endpoint_url="https://offline.invalid")
        return reply

    monkeypatch.setattr(w.f.f.fixture.client, "transact_write_items", lost)
    operation_id = str(uuid4())
    reply = await close(w, receipt["execution_id"], operation_id)
    assert reply.status_code == 403
    replay = await close(w, receipt["execution_id"], operation_id)
    assert replay.status_code == 202 and replay.json()["native_status"]["replay"] is True
    current = await status(w)
    assert current["executions"][0]["cleanup_status"] == "pending"
    assert current["executions"][0]["cleanup_attempts_used"] == 1
    assert current["held_usd"] == "8.000000"


@pytest.mark.parametrize("lost_ack", [False, True])
async def test_original_setup_has_only_three_attempted_status_reads(http_wire, monkeypatch, lost_ack):
    w = http_wire
    await call(w)
    if lost_ack:
        original = w.f.f.fixture.client.transact_write_items

        def lost(**kwargs):
            reply = original(**kwargs)
            if any(item.get("Put", {}).get("Item", {}).get("kind", {}).get("S") == "native_test_status_reads_v1" for item in kwargs["TransactItems"]):
                raise EndpointConnectionError(endpoint_url="https://offline.invalid")
            return reply

        monkeypatch.setattr(w.f.f.fixture.client, "transact_write_items", lost)
    async with w.client_factory(transport=httpx.ASGITransport(app=w.app), base_url="https://gateway.internal") as client:
        for _ in range(3):
            response = await client.get("/v1/native-tests/runs/" + w.f.approval.run_id + "/status", headers=w.headers)
            assert response.status_code == (403 if lost_ack else 200)
        exhausted = await client.get("/v1/native-tests/runs/" + w.f.approval.run_id + "/status", headers=w.headers)
    assert exhausted.status_code == 403 and "status_reads_exhausted" in exhausted.text
    key = w.f.f.store.key("tenant-one", "STATUS", w.f.approval.run_id)
    assert w.f.f.store.document(key, "native_test_status_reads_v1")["attempts"] == 3


@pytest.mark.parametrize("facade", ["smoke"], indirect=True)
async def test_real_native_discovery_search_owned_close_and_discovery_keep_one_all_in_meter(http_wire):
    w = http_wire

    async def discover():
        return await w.client.get("/agent/tools?live=true", headers={**w.headers, "x-adp-native-test-operation": str(uuid4())})

    before = await discover()
    assert before.status_code == 200, before.text
    receipt = await call(w)
    assert (await close(w, receipt["execution_id"])).status_code == 200
    after = await discover()
    assert after.status_code == 200 and after.json()["discovery"] == before.json()["discovery"]
    current = await status(w)
    assert current["held_usd"] == "10.000000"
    assert current["funding_status"] == "held" and current["accounting_status"] == "pending"
    assert current["governance_complete"] is False
    assert len(current["executions"]) == 1 and current["executions"][0]["cleanup_status"] == "confirmed"
    assert [packet["method"] for packet in w.state["provider"]] == ["tools/list", "tools/call", "tools/list"]


@pytest.mark.parametrize("facade", ["smoke"], indirect=True)
async def test_native_discovery_never_follows_an_unquoted_second_page(http_wire):
    w = http_wire
    w.state["fault"] = "next_cursor"
    response = await w.client.get("/agent/tools?live=true", headers={**w.headers, "x-adp-native-test-operation": str(uuid4())})
    assert response.status_code == 503
    assert len(w.state["provider"]) == len(w.state["kms"]) == 1
