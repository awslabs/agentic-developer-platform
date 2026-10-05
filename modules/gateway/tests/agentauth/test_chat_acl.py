"""Explicit session sharing: owner-only ACL writes, current-membership reads, immediate revocation.

A1 (``human``) is the admitted owner of ``session-a``. A2 (``other-user``) and the
bystander are current members of the same tenant and team; B1 (``outsider``) belongs
to another tenant. Readers other than A1 present capabilities for their own launches
whose liveness is synthetic; membership, ACL and ownership checks are the real ones.
"""

from datetime import UTC, datetime

import pytest
from sqlalchemy import update
from starlette.concurrency import run_in_threadpool

from src.agentauth import chat_data_routes
from src.agentauth.chat_capability import ChatLaunch
from src.agentauth.workload import VerifiedPod
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import Organization, Team, TeamMembership, User
from tests.agentauth.test_chat_artifact import artifacts as artifacts_fixture
from tests.agentauth.test_chat_artifact import create
from tests.agentauth.test_chat_history_routes import capability as capability_fixture
from tests.agentauth.test_chat_history_routes import client as client_fixture
from tests.agentauth.test_chat_history_routes import read, seed_history
from tests.agentauth.test_chat_history_routes import runtime as runtime_fixture
from tests.agentauth.test_chat_history_routes import store as store_fixture
from tests.agentauth.test_chat_history_routes import sts as sts_fixture
from tests.agentauth.test_chat_history_write import HEADER

artifacts = artifacts_fixture
capability = capability_fixture
client = client_fixture
runtime = runtime_fixture
store = store_fixture
sts = sts_fixture
ACL = "/v1/chat/data/session/acl"
DIGEST = "sha256:" + "a" * 64
FORGED = {"X-User-Id": "human", "X-Tenant-Id": "tenant", "X-Session-Id": "session-a", "X-Owner-User-Id": "human"}
SYNTHETIC = {"run-b", "run-c", "run-d", "run-e"}


@pytest.fixture
async def members(runtime, db_session_factory, monkeypatch):
    async with db_session_factory() as db:
        db.add_all(
            [
                User(id="other-user", org_id="tenant", team_id="team", email="other@example.test"),
                TeamMembership(id="other-member", org_id="tenant", user_id="other-user", team_id="team"),
                TenantMembership(id="other-tenant-member", tenant_id="tenant", user_id="other-user", role="member", is_active=False),
                User(id="bystander", org_id="tenant", team_id="team", email="bystander@example.test"),
                TeamMembership(id="bystander-member", org_id="tenant", user_id="bystander", team_id="team"),
                Organization(id="other-tenant", name="Other tenant"),
                Team(id="other-team", org_id="other-tenant", department_id="other-department", name="Other team"),
                User(id="outsider", org_id="other-tenant", team_id="other-team", email="outsider@example.test"),
                TeamMembership(id="outsider-member", org_id="other-tenant", user_id="outsider", team_id="other-team"),
            ]
        )
        await db.commit()
    service = runtime[0]
    original = service.current
    monkeypatch.setattr(service, "current", lambda launch, now: True if launch.run_id in SYNTHETIC else original(launch, now))

    async def credential(run, user, *, tenant="tenant", team="team", session=None):
        pod = VerifiedPod(f"pod-{run}", f"sandbox-{run}", "adp-gateway-agents", "adp-agent", "127.0.0.1", image_digest=DIGEST)
        launch = ChatLaunch(
            run_id=run,
            tenant_id=tenant,
            user_id=user,
            team_id=team,
            session_id=session or f"session-{run}",
            sandbox_uid=pod.uid,
            image_digest=DIGEST,
            attempt=1,
            credential_epoch=1,
            lease_generation=1,
            grant_id=f"grant-{run}",
            grant_epoch=1,
            operations=frozenset({"history.read", "history.expand", "artifact.read", "session.share"}),
            expires_at=runtime[-1] + 600,
        )
        service.launches.register(launch)
        # Directory lookups run on the event loop from a worker thread, exactly as the routes call them.
        return await run_in_threadpool(service.issue, run, pod, now=runtime[-1])

    return credential


async def acl(client, capability, operation="write", *, headers=None, **fields):
    return await client.post(
        f"{ACL}/{operation}",
        json={"run_id": "run-a", "session_id": "session-a", **fields},
        headers={"Authorization": f"Bearer {capability}", **(headers or {})},
    )


async def share(client, capability, add=(), remove=(), *, key="share-1", version=0, **changes):
    body = {"idempotency_key": key, "expected_version": version, "add": list(add), "remove": list(remove), **changes}
    return await acl(client, capability, headers={"X-User-Id": "other-user", "X-Tenant-Id": "other-tenant"}, **body)


def unshared(runtime):
    header = runtime[2].get_item(Key=HEADER)["Item"]
    return header.get("aclUserIds", []) == [] and "aclVersion" not in header


async def reads(client, token, run_id, *, session_id="session-a"):
    page = await read(client, token, run_id=run_id, session_id=session_id, headers=FORGED, limit=1)
    messages = await read(client, token, "messages", run_id=run_id, session_id=session_id, ids=["message-1"], headers=FORGED)
    listing = await client.post(
        "/v1/chat/data/artifact/list",
        json={"run_id": run_id, "session_id": session_id},
        headers={"Authorization": f"Bearer {token}", **FORGED},
    )
    return page, messages, listing


async def test_owner_shares_member_reads_and_revocation_applies_on_the_next_read(client, runtime, capability, artifacts, members):
    seed_history(runtime)
    uploaded = await create(client, capability)
    reader = await members("run-b", "other-user")
    for response in await reads(client, reader, "run-b"):
        assert response.status_code == 404, response.text

    shared = await share(client, capability, add=["other-user"])
    assert shared.status_code == 200, shared.text
    assert shared.json() == {"acl": ["other-user"], "version": 1}
    header = runtime[2].get_item(Key=HEADER)["Item"]
    assert (header["aclUserIds"], header["aclVersion"], header["ownerUserId"]) == (["other-user"], 1, "human")
    assert header["chatLease"]["run_id"] == "run-a"
    assert (await acl(client, capability, "read")).json() == {"acl": ["other-user"], "version": 1}

    page, messages, listing = await reads(client, reader, "run-b")
    assert page.status_code == 200 and page.json()["status"] == "partial", page.text
    assert messages.json()["entries"][0]["message"]["content"] == "human: message-1"
    assert listing.status_code == 200 and [entry["id"] for entry in listing.json()["entries"]] == [uploaded["id"]]
    cursor = page.json()["next_cursor"]
    assert (await read(client, reader, run_id="run-b", limit=1, cursor=cursor)).status_code == 200

    revoked = await share(client, capability, remove=["other-user"], key="revoke-1", version=1)
    assert revoked.status_code == 200, revoked.text
    assert revoked.json() == {"acl": [], "version": 2}
    for response in await reads(client, reader, "run-b"):
        assert response.status_code == 404, response.text
    assert (await read(client, reader, run_id="run-b", limit=1, cursor=cursor)).status_code == 404
    assert (await read(client, capability)).status_code == 200


async def test_other_tenant_and_unshared_same_tenant_members_never_read_or_get_added(client, runtime, capability, artifacts, members):
    seed_history(runtime)
    await create(client, capability)
    outsider = await members("run-c", "outsider", tenant="other-tenant", team="other-team")
    bystander = await members("run-d", "bystander")
    for user in ("outsider", "ghost"):
        response = await share(client, capability, add=[user], key=f"share-{user}")
        assert response.status_code == 404, response.text
        assert response.json()["detail"]["error"] == "chat_scope_refused"
    assert unshared(runtime)
    assert (await share(client, capability, add=["other-user", "outsider"], key="share-mixed")).status_code == 404
    assert unshared(runtime)
    assert (await share(client, capability, add=["other-user"])).status_code == 200
    for token, run in ((outsider, "run-c"), (bystander, "run-d")):
        for response in await reads(client, token, run):
            assert response.status_code == 404, response.text
    # Tenant equality never grants access, even when a stale ACL names another tenant's user.
    header = runtime[2].get_item(Key=HEADER)["Item"]
    runtime[2].put_item(Item={**header, "aclUserIds": ["other-user", "outsider"]})
    for response in await reads(client, outsider, "run-c"):
        assert response.status_code == 404, response.text


async def test_acl_members_cannot_share_revoke_or_read_the_acl(client, runtime, capability, artifacts, members):
    seed_history(runtime)
    assert (await share(client, capability, add=["other-user"])).status_code == 200
    reader = await members("run-b", "other-user")
    bound = await members("run-e", "other-user", session="session-a")
    assert (await read(client, reader, run_id="run-b")).status_code == 200
    assert (await read(client, bound, run_id="run-e")).status_code == 200
    for token, run in ((reader, "run-b"), (bound, "run-e")):
        for body in ({"add": ["bystander"]}, {"remove": ["other-user"]}):
            response = await acl(client, token, run_id=run, idempotency_key=f"{run}-{next(iter(body))}", expected_version=1, headers=FORGED, **body)
            assert response.status_code == 404, response.text
        assert (await acl(client, token, "read", run_id=run, headers=FORGED)).status_code == 404
    header = runtime[2].get_item(Key=HEADER)["Item"]
    assert (header["aclUserIds"], header["aclVersion"]) == (["other-user"], 1)
    assert (await read(client, await members("run-d", "bystander"), run_id="run-d")).status_code == 404


async def test_revoked_tenant_membership_removes_access_while_still_listed(client, runtime, capability, artifacts, members, db_session_factory):
    seed_history(runtime)
    assert (await share(client, capability, add=["other-user"])).status_code == 200
    reader = await members("run-b", "other-user")
    page = await read(client, reader, run_id="run-b", limit=1)
    assert page.status_code == 200
    async with db_session_factory() as db:
        await db.execute(update(TenantMembership).where(TenantMembership.user_id == "other-user").values(revoked_at=datetime.now(UTC)))
        await db.commit()
    assert runtime[2].get_item(Key=HEADER)["Item"]["aclUserIds"] == ["other-user"]
    for response in await reads(client, reader, "run-b"):
        assert response.status_code == 404, response.text
    assert (await read(client, reader, run_id="run-b", limit=1, cursor=page.json()["next_cursor"])).status_code == 404
    # A revoked member cannot be re-added until the directory says otherwise.
    assert (await share(client, capability, add=["bystander", "other-user"], key="share-2", version=1)).status_code == 404
    assert (await share(client, capability, add=["bystander"], key="share-3", version=1)).status_code == 200


async def test_acl_writes_are_idempotent_version_fenced_and_validated(client, runtime, capability, members):
    first = await share(client, capability, add=["other-user"])
    assert first.status_code == 200, first.text
    before = sorted(runtime[2].scan()["Items"], key=lambda row: row["SK"])
    receipts = [row for row in before if row["SK"].startswith("acl#")]
    assert len(receipts) == 1 and receipts[0]["ownerUserId"] == "human" and receipts[0]["result"]["version"] == 1
    retry = await share(client, capability, add=["other-user"])
    assert retry.status_code == 200 and retry.json() == first.json()
    assert sorted(runtime[2].scan()["Items"], key=lambda row: row["SK"]) == before
    assert (await share(client, capability, add=["bystander"])).status_code == 409
    assert (await share(client, capability, add=["bystander"], key="share-2", version=0)).status_code == 409
    assert (await share(client, capability, add=["bystander"], key="share-3", version=2)).status_code == 409
    assert (await share(client, capability, add=["human"], key="share-4", version=1)).status_code == 404
    for body in (
        {"add": [], "remove": []},
        {"add": ["bystander"], "remove": ["bystander"]},
        {"add": ["bystander", "bystander"]},
        {"add": ["session#other/user"]},
        {"add": ["bystander"], "owner_user_id": "other-user"},
        {"add": ["bystander"], "expected_version": -1},
    ):
        assert (await share(client, capability, key="share-invalid", version=1, **body)).status_code == 422
    assert runtime[2].get_item(Key=HEADER)["Item"]["aclUserIds"] == ["other-user"]
    second = await share(client, capability, add=["bystander"], remove=["other-user"], key="share-5", version=1)
    assert second.status_code == 200 and second.json() == {"acl": ["bystander"], "version": 2}
    assert (await acl(client, capability, "read", headers=FORGED)).json() == {"acl": ["bystander"], "version": 2}


async def test_acl_write_is_fenced_against_concurrent_change_and_dead_lease(client, runtime, capability, members, monkeypatch):
    original = runtime[1].store.client.transact_write_items

    def race(**kwargs):
        header = runtime[2].get_item(Key=HEADER)["Item"]
        runtime[2].put_item(Item={**header, "aclUserIds": ["bystander"], "aclVersion": 1})
        return original(**kwargs)

    monkeypatch.setattr(runtime[1].store.client, "transact_write_items", race)
    response = await share(client, capability, add=["other-user"])
    assert response.status_code == 409 and response.json()["detail"]["error"] == "chat_acl_conflict"
    monkeypatch.setattr(runtime[1].store.client, "transact_write_items", original)
    assert runtime[2].get_item(Key=HEADER)["Item"]["aclUserIds"] == ["bystander"]
    runtime[2].update_item(
        Key=HEADER,
        UpdateExpression="SET chatLease.generation = :generation",
        ExpressionAttributeValues={":generation": 2},
    )
    assert (await share(client, capability, add=["other-user"], key="share-2", version=1)).status_code == 404
    assert runtime[2].get_item(Key=HEADER)["Item"]["aclUserIds"] == ["bystander"]


async def test_missing_forged_expired_capabilities_and_disabled_rollout_fail_closed(client, runtime, capability, members, monkeypatch):
    body = {"run_id": "run-a", "session_id": "session-a", "idempotency_key": "share-1", "expected_version": 0, "add": ["other-user"]}
    assert (await client.post(f"{ACL}/write", json=body, headers=FORGED)).status_code == 401
    assert (await share(client, "forged", add=["other-user"])).status_code == 401
    monkeypatch.setattr(chat_data_routes, "clock", lambda: runtime[-1] + 300)
    expired = await share(client, capability, add=["other-user"])
    assert (expired.status_code, expired.json()["detail"]["error"]) == (401, "capability_expired")
    monkeypatch.setattr(chat_data_routes, "clock", lambda: runtime[-1])
    assert (await share(client, capability, add=["other-user"], run_id="other-run")).status_code == 404
    monkeypatch.delenv("ADP_CHAT_DATA_ENABLED")
    assert (await share(client, capability, add=["other-user"])).status_code == 503
    assert unshared(runtime)
