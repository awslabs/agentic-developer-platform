"""Model usage stays under its run and cannot be created after revocation."""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from src.agentauth.chat_crew_run import CrewRunStore
from src.crew.model_token import mint_model_token
from src.crew.model_usage import CrewModelUsage
from src.crew.targets import CrewLease, CrewOwner, ResolvedCrewTarget
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import Organization, User
from tests.agentauth.test_chat_crew_run import registry as registry_fixture

registry = registry_fixture
NOW = 1_800_000_000


@pytest.fixture
def active(registry, monkeypatch):
    store = CrewRunStore(registry, host_role_arn="arn:aws:iam::000000000000:role/guest")
    run_id = store.mint(
        tenant_id="tenant-a",
        user_id="owner-a",
        session_id="box-a",
        lease_generation=3,
        lease_expires_at=NOW + 300,
        target_kind="ec2-instance",
        target_ref="i-current",
        deployment_id="dep-a",
        now=NOW,
    )
    monkeypatch.setattr("src.crew.handoff.current_crew_store", lambda: store)
    monkeypatch.setattr("src.crew.model_token._secret", lambda: b"fictional-test-signing-key")
    owner = CrewOwner("tenant-a", "owner-a")
    endpoint = "https://10.4.0.3:8443"
    target = ResolvedCrewTarget(
        endpoint,
        CrewLease(
            tenant_id="tenant-a",
            user_id="owner-a",
            target_kind="ec2-instance",
            target_ref="box-a",
            generation=3,
            expires_at=NOW + 300,
            endpoint=endpoint,
        ),
        "ready",
        host_run_id=run_id,
        host_instance_ref="i-current",
        host_fingerprint="a" * 64,
    )
    return store, owner, target


async def test_authorized_model_call_records_receipt_under_run_and_survives_revocation(active, db_session):
    store, owner, target = active
    token = await mint_model_token(owner, target, now=NOW)
    calls = []

    async def model():
        calls.append("provider")
        return {"content": [{"type": "text", "text": "ok"}], "usage": {"input_tokens": 4, "output_tokens": 2}}

    usage = CrewModelUsage(store)
    assert (await usage.invoke(token, target, "test-model", model, now=NOW + 1))["usage"]["output_tokens"] == 2
    assert calls == ["provider"] and store.read_run(target.host_run_id)["status"] == "active"
    db_session.add(Organization(id="tenant-a", name="Tenant A"))
    for name, role in (("owner-a", "member"), ("admin-a", "admin"), ("other-a", "member")):
        db_session.add(User(id=name, org_id="tenant-a", team_id="team", email=f"{name}@example.test"))
        db_session.add(TenantMembership(id=f"membership-{name}", user_id=name, tenant_id="tenant-a", role=role))
    await db_session.commit()

    def identity(name):
        return SimpleNamespace(account_type="human", user_id=name, org_id="tenant-a", expires_at=datetime.now(UTC))

    for name in ("owner-a", "admin-a"):
        receipts = await usage.receipts(target.host_run_id, identity(name), db_session)
        assert len(receipts) == 1 and receipts[0]["usage"] == {"input_tokens": 4, "output_tokens": 2}
        assert receipts[0]["run_id"] == target.host_run_id and receipts[0]["generation"] == 3
    for name in ("other-a",):
        with pytest.raises(HTTPException) as denied:
            await usage.receipts(target.host_run_id, identity(name), db_session)
        assert denied.value.status_code == 403
    store.revoke_for_lease(tenant_id="tenant-a", session_id="box-a", lease_generation=3, expected_run_id=target.host_run_id)
    with pytest.raises(HTTPException) as denied:
        await usage.invoke(token, target, "test-model", model, now=NOW + 2)
    assert denied.value.status_code == 401 and calls == ["provider"]
    assert store.read_run(target.host_run_id) is None
    assert len(await usage.receipts(target.host_run_id, identity("owner-a"), db_session)) == 1


async def test_wrong_target_and_revoked_membership_cannot_read(active, db_session):
    store, owner, target = active
    token = await mint_model_token(owner, target, now=NOW)

    async def model():
        return {"usage": {"input_tokens": 0, "output_tokens": 1}}

    usage = CrewModelUsage(store)
    await usage.invoke(token, target, "test-model", model, now=NOW + 1)
    db_session.add(Organization(id="tenant-a", name="Tenant A"))
    db_session.add(User(id="admin-a", org_id="tenant-a", team_id="team", email="admin@example.test"))
    db_session.add(TenantMembership(id="membership-admin", user_id="admin-a", tenant_id="tenant-a", role="admin", revoked_at=datetime.now(UTC)))
    await db_session.commit()
    with pytest.raises(HTTPException) as denied:
        await usage.receipts(target.host_run_id, SimpleNamespace(account_type="human", user_id="admin-a", org_id="tenant-a"), db_session)
    assert denied.value.status_code == 403


async def test_signed_host_refresh_delivers_only_through_pinned_client(active, monkeypatch):
    import httpx

    from src.crew import handoff, proxy

    store, owner, target = active
    captured = []

    async def host_client(selected):
        assert selected is target

        def deliver(request):
            captured.append(request)
            return httpx.Response(204)

        return httpx.AsyncClient(transport=httpx.MockTransport(deliver), trust_env=False)

    async def resolve(_owner, *, now):
        assert _owner == owner and now == NOW
        return target

    monkeypatch.setattr(handoff, "crew_host_client", host_client)
    monkeypatch.setattr(proxy, "get_target_resolver", lambda: SimpleNamespace(resolve=resolve))
    monkeypatch.setattr(proxy, "time", SimpleNamespace(time=lambda: NOW))
    monkeypatch.setattr("src.agentauth.chat_crew_run.current_crew_store", lambda: store)
    context = SimpleNamespace(account_type="service", auth_source="iam", _dev_box_instance_id="i-current")
    await proxy.refresh_crew_model_token(context, target.host_run_id, proxy.HostActivity(), None)
    assert len(captured) == 1
    assert str(captured[0].url) == "https://10.4.0.3:8443/model-token"
    import json

    delivered = json.loads(captured[0].content)
    assert delivered["run_id"] == target.host_run_id and delivered["lease_generation"] == 3
    assert delivered["token"] and "server.key" not in delivered
    with pytest.raises(HTTPException) as foreign:
        await proxy.refresh_crew_model_token(
            SimpleNamespace(account_type="service", auth_source="iam", _dev_box_instance_id="i-foreign"), target.host_run_id
        )
    assert foreign.value.status_code == 403 and len(captured) == 1
    store.revoke_for_lease(tenant_id="tenant-a", session_id="box-a", lease_generation=3, expected_run_id=target.host_run_id)
    with pytest.raises(HTTPException):
        await proxy.refresh_crew_model_token(context, target.host_run_id, proxy.HostActivity(), None)
    assert len(captured) == 1


async def test_model_receipt_route_uses_owner_or_admin_membership(active, db_session, monkeypatch):
    from src.crew import proxy

    store, owner, target = active
    token = await mint_model_token(owner, target, now=NOW)

    async def model():
        return {"usage": {"input_tokens": 1, "output_tokens": 2}}

    await CrewModelUsage(store).invoke(token, target, "test-model", model, now=NOW + 1)
    db_session.add(Organization(id="tenant-a", name="Tenant A"))
    for name, role in (("owner-a", "member"), ("admin-a", "admin"), ("foreign-a", "member")):
        db_session.add(User(id=name, org_id="tenant-a", team_id="team", email=f"{name}@example.test"))
        db_session.add(TenantMembership(id=f"membership-{name}", user_id=name, tenant_id="tenant-a", role=role))
    await db_session.commit()
    monkeypatch.setattr("src.agentauth.chat_crew_run.current_crew_store", lambda: store)
    for name in ("owner-a", "admin-a"):
        rows = await proxy.crew_model_receipts(target.host_run_id, SimpleNamespace(user_id=name, org_id="tenant-a", account_type="human"), db_session)
        assert len(rows) == 1 and rows[0]["run_id"] == target.host_run_id
    with pytest.raises(HTTPException) as denied:
        await proxy.crew_model_receipts(target.host_run_id, SimpleNamespace(user_id="foreign-a", org_id="tenant-a", account_type="human"), db_session)
    assert denied.value.status_code == 403


def _sse(event: dict) -> bytes:
    import json

    return f"data: {json.dumps(event)}\n\n".encode()


def _receipts_in_table(store, run_id):
    from boto3.dynamodb.conditions import Key

    return store.table.query(KeyConditionExpression=Key("event_id").eq(f"crew-model#{run_id}"))["Items"]


async def test_streamed_model_call_confirms_the_receipt_with_the_streams_usage_at_its_end(active):
    """#8223 Dev Box latency: the stream route accounts like the buffered one. The receipt is
    reserved before Bedrock is opened, still pending while bytes flow, and confirmed with the
    usage the stream itself reported (input from message_start, output from message_delta)."""
    store, owner, target = active
    token = await mint_model_token(owner, target, now=NOW)
    usage = CrewModelUsage(store)
    events = [
        _sse({"type": "message_start", "message": {"usage": {"input_tokens": 115_000, "cache_read_input_tokens": 110_000, "output_tokens": 1}}}),
        _sse({"type": "content_block_delta", "delta": {"type": "text_delta", "text": "pong"}}),
        _sse({"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 7}}),
        _sse({"type": "message_stop"}),
    ]
    opened = []

    async def open_stream():
        opened.append("provider")
        assert [r["status"] for r in _receipts_in_table(store, target.host_run_id)] == ["pending"]

        async def chunks():
            for event in events:
                yield event

        return chunks()

    stream = await usage.invoke_stream(token, target, "test-model", open_stream, now=NOW + 1)
    seen = []
    async for chunk in stream:
        seen.append(chunk)
        assert [r["status"] for r in _receipts_in_table(store, target.host_run_id)] == ["pending"], "settled before the stream ended"
    assert seen == events and opened == ["provider"]
    (receipt,) = _receipts_in_table(store, target.host_run_id)
    assert receipt["status"] == "confirmed" and receipt["usage"] == {"input_tokens": 115_000, "output_tokens": 7}


@pytest.mark.parametrize("failure", ["provider_error", "no_usage", "client_gone", "open_refused"])
async def test_a_stream_that_does_not_complete_with_usage_is_recorded_failed(active, failure):
    store, owner, target = active
    token = await mint_model_token(owner, target, now=NOW)
    usage = CrewModelUsage(store)

    async def open_stream():
        if failure == "open_refused":
            raise RuntimeError("provider refused")

        async def chunks():
            yield _sse({"type": "message_start", "message": {"usage": {"input_tokens": 3, "output_tokens": 1}}})
            if failure == "provider_error":
                raise RuntimeError("upstream reset")
            if failure == "no_usage":
                yield _sse({"type": "message_stop"})
                return
            yield _sse({"type": "content_block_delta", "delta": {"type": "text_delta", "text": "po"}})
            yield _sse({"type": "message_delta", "usage": {"output_tokens": 9}})

        return chunks()

    if failure == "open_refused":
        with pytest.raises(RuntimeError):
            await usage.invoke_stream(token, target, "test-model", open_stream, now=NOW + 1)
    else:
        stream = await usage.invoke_stream(token, target, "test-model", open_stream, now=NOW + 1)
        if failure == "client_gone":
            await stream.__anext__()  # the client read one chunk and went away
            await stream.aclose()
        elif failure == "provider_error":
            with pytest.raises(RuntimeError):
                async for _ in stream:
                    pass
        else:
            async for _ in stream:
                pass
    (receipt,) = _receipts_in_table(store, target.host_run_id)
    assert receipt["status"] == "failed" and "usage" not in receipt


async def test_stream_reservation_is_refused_like_the_buffered_call(active):
    store, owner, target = active
    usage = CrewModelUsage(store)

    async def never():
        raise AssertionError("provider must not be opened")

    with pytest.raises(HTTPException) as denied:
        await usage.invoke_stream("not-a-token", target, "test-model", never, now=NOW + 1)
    assert denied.value.status_code == 401
    assert _receipts_in_table(store, target.host_run_id) == []
