"""Emulator checks for persistent Crew lease run attribution."""

from types import SimpleNamespace

import boto3
import pytest
from moto import mock_aws

from src.agentauth.chat_crew_run import CrewRunConflictError, CrewRunStore
from src.budget.run_binding import RunBindingError, RunBindingResolver, verify_row_matches_caller

NOW = 1_800_000_000


@pytest.fixture
def registry():
    with mock_aws():
        table = boto3.resource("dynamodb", region_name="us-east-1").create_table(
            TableName="webhook-events",
            BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "event_id", "KeyType": "HASH"}, {"AttributeName": "arrived_at", "KeyType": "RANGE"}],
            AttributeDefinitions=[
                {"AttributeName": attribute, "AttributeType": "S"} for attribute in ("event_id", "arrived_at", "user_id", "root_human_id")
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": index,
                    "KeySchema": [{"AttributeName": attribute, "KeyType": "HASH"}, {"AttributeName": "arrived_at", "KeyType": "RANGE"}],
                    "Projection": {"ProjectionType": "ALL"},
                }
                for index, attribute in (("user-index", "user_id"), ("root-human-index", "root_human_id"))
            ],
        )
        yield table


def mint(table, **changes):
    arguments = dict(
        tenant_id="tenant",
        user_id="alice",
        session_id="session-a",
        lease_generation=2,
        lease_expires_at=NOW + 300,
        target_kind="eks-pod",
        target_ref="pod-123",
        now=NOW,
        executor_scope="user",
    )
    return CrewRunStore(table, host_role_arn="arn:aws:iam::000000000000:role/guest").mint(**{**arguments, **changes})


def test_failed_creation_receipt_write_cannot_leave_a_crew_binding(registry):
    with pytest.raises(CrewRunConflictError):
        mint(
            registry,
            creation_update={
                "TableName": registry.name,
                "Key": {"event_id": "missing-creation", "arrived_at": "creation"},
                "ConditionExpression": "attribute_exists(event_id)",
                "ExpressionAttributeValues": {},
            },
        )
    assert registry.scan()["Count"] == 0


@pytest.mark.asyncio
async def test_unverified_crew_pod_run_is_inert_until_activation(registry):
    run_id = mint(registry, provisioning=True)
    store = CrewRunStore(registry)
    row = await RunBindingResolver(registry.name, "us-east-1", table=registry).read_current(run_id)
    assert row["status"] == "provisioning"
    assert registry.get_item(Key={"event_id": "crew-lease#tenant#session-a#2", "arrived_at": "crew"})["Item"]["status"] == "provisioning"
    with pytest.raises(RunBindingError, match="Crew lease is no longer active"):
        verify_row_matches_caller(run_id=run_id, row=row, caller_user_id="worker", caller_org_id="tenant")
    with pytest.raises(CrewRunConflictError):
        store.attach_session(
            tenant_id="tenant",
            user_id="alice",
            session_id="session-b",
            lease_generation=2,
            target_kind="eks-pod",
            target_ref="pod-123",
            now=NOW,
        )
    with pytest.raises(ValueError, match="only a user-scoped pod"):
        mint(registry, executor_scope="session", provisioning=True)


def test_verified_crew_activation_is_one_atomic_owner_generation_transition(registry):
    run_id = mint(registry, provisioning=True)
    store = CrewRunStore(registry)
    checks = activation_checks(registry)
    options = dict(
        run_id=run_id,
        tenant_id="tenant",
        user_id="alice",
        session_id="session-a",
        lease_generation=2,
        target_ref="pod-123",
        pod_uid="verified-uid",
        now=NOW,
        owner_lease_expires_at=NOW + 60,
        **checks,
    )
    store.activate_for_pod(**options)
    assert store.read_run(run_id)["status"] == "active"
    assert store.read_run(run_id)["pod_uid"] == "verified-uid"
    owner = registry.get_item(Key={"event_id": "crew-owner#tenant#alice", "arrived_at": "crew"})["Item"]
    marker = registry.get_item(Key={"event_id": "crew-lease#tenant#session-a#2", "arrived_at": "crew"})["Item"]
    assert owner["pod_uid"] == "verified-uid" and marker["status"] == "active"
    assert store.read_run(run_id)["lease_expires_at"] == owner["lease_expires_at"] == NOW + 60
    assert marker["expires_at"] == NOW + 60 + 86400

    assert (
        store.attach_session(
            tenant_id="tenant",
            user_id="alice",
            session_id="session-b",
            lease_generation=2,
            target_kind="eks-pod",
            target_ref="pod-123",
            now=NOW,
        )
        == run_id
    )
    with pytest.raises(CrewRunConflictError):
        store.activate_for_pod(**options)


@pytest.mark.parametrize("changed", ["owner", "run", "interleaved"])
def test_attached_session_requires_same_verified_owner_pod(registry, monkeypatch, changed):
    run_id = mint(registry, provisioning=True)
    store = CrewRunStore(registry)
    checks = activation_checks(registry)
    store.activate_for_pod(
        run_id=run_id,
        tenant_id="tenant",
        user_id="alice",
        session_id="session-a",
        lease_generation=2,
        target_ref="pod-123",
        pod_uid="verified-uid",
        now=NOW,
        owner_lease_expires_at=NOW + 60,
        **checks,
    )
    owner_key = {"event_id": "crew-owner#tenant#alice", "arrived_at": "crew"}
    run_key = {"event_id": run_id, "arrived_at": store.read_run(run_id)["arrived_at"]}
    changed_key = run_key if changed == "run" else owner_key
    if changed == "interleaved":
        original = registry.meta.client.transact_write_items

        def change_between_read_and_write(**arguments):
            registry.update_item(Key=changed_key, UpdateExpression="SET pod_uid = :uid", ExpressionAttributeValues={":uid": "other-pod"})
            return original(**arguments)

        monkeypatch.setattr(registry.meta.client, "transact_write_items", change_between_read_and_write)
    else:
        registry.update_item(Key=changed_key, UpdateExpression="SET pod_uid = :uid", ExpressionAttributeValues={":uid": "other-pod"})
    with pytest.raises(CrewRunConflictError):
        store.attach_session(
            tenant_id="tenant",
            user_id="alice",
            session_id="session-b",
            lease_generation=2,
            target_kind="eks-pod",
            target_ref="pod-123",
            pod_uid="verified-uid",
            now=NOW,
            owner_lease_check=checks["lease_check"],
        )
    assert registry.get_item(Key=owner_key)["Item"]["session_count"] == 1
    assert registry.get_item(Key={"event_id": "crew-lease#tenant#session-b#2", "arrived_at": "crew"}).get("Item") is None


def activation_checks(registry):
    registry.put_item(Item={"event_id": "lease", "arrived_at": "check", "generation": 2})
    registry.put_item(Item={"event_id": "creation", "arrived_at": "check", "run_id": "run-write"})
    registry.put_item(Item={"event_id": "session", "arrived_at": "check", "status": "active"})
    return {
        "lease_check": {
            "TableName": registry.name,
            "Key": {"event_id": "lease", "arrived_at": "check"},
            "ConditionExpression": "generation = :generation",
            "ExpressionAttributeValues": {":generation": 2},
        },
        "creation_check": {
            "TableName": registry.name,
            "Key": {"event_id": "creation", "arrived_at": "check"},
            "ConditionExpression": "run_id = :run",
            "ExpressionAttributeValues": {":run": "run-write"},
        },
        "session_check": {
            "TableName": registry.name,
            "Key": {"event_id": "session", "arrived_at": "check"},
            "ConditionExpression": "#status = :active",
            "ExpressionAttributeNames": {"#status": "status"},
            "ExpressionAttributeValues": {":active": "active"},
        },
    }


@pytest.mark.parametrize("changed", ["owner", "marker", "generation"])
def test_crew_activation_denies_changed_owner_or_marker_without_partial_write(registry, changed):
    run_id = mint(registry, provisioning=True)
    store = CrewRunStore(registry)
    checks = activation_checks(registry)
    if changed == "owner":
        registry.update_item(
            Key={"event_id": "crew-owner#tenant#alice", "arrived_at": "crew"},
            UpdateExpression="SET target_ref = :target",
            ExpressionAttributeValues={":target": "wrong-pod"},
        )
    elif changed == "marker":
        registry.update_item(
            Key={"event_id": "crew-lease#tenant#session-a#2", "arrived_at": "crew"},
            UpdateExpression="SET #status = :active",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={":active": "active"},
        )
    with pytest.raises(CrewRunConflictError):
        store.activate_for_pod(
            run_id=run_id,
            tenant_id="tenant",
            user_id="alice",
            session_id="session-a",
            lease_generation=3 if changed == "generation" else 2,
            target_ref="pod-123",
            pod_uid="verified-uid",
            now=NOW,
            owner_lease_expires_at=NOW + 300,
            **checks,
        )
    assert store.read_run(run_id)["status"] == "provisioning"
    assert store.read_run(run_id).get("pod_uid") is None


@pytest.mark.parametrize("changed", ["lease", "creation", "session"])
def test_crew_activation_denies_stale_external_evidence_atomically(registry, changed):
    run_id = mint(registry, provisioning=True)
    store = CrewRunStore(registry)
    checks = activation_checks(registry)
    key = {"event_id": changed, "arrived_at": "check"}
    registry.update_item(
        Key=key,
        UpdateExpression="SET #field = :value",
        ExpressionAttributeNames={"#field": {"lease": "generation", "creation": "run_id", "session": "status"}[changed]},
        ExpressionAttributeValues={":value": {"lease": 3, "creation": "changed-run", "session": "ended"}[changed]},
    )
    with pytest.raises(CrewRunConflictError):
        store.activate_for_pod(
            run_id=run_id,
            tenant_id="tenant",
            user_id="alice",
            session_id="session-a",
            lease_generation=2,
            target_ref="pod-123",
            pod_uid="verified-uid",
            now=NOW,
            owner_lease_expires_at=NOW + 300,
            **checks,
        )
    assert store.read_run(run_id)["status"] == "provisioning"
    assert registry.get_item(Key={"event_id": "crew-owner#tenant#alice", "arrived_at": "crew"})["Item"].get("pod_uid") is None


@pytest.mark.asyncio
async def test_provisioning_crew_run_refuses_routing_with_live_session_lease(registry, monkeypatch):
    from src.proxy.bedrock_principal import read_routing_run

    monkeypatch.setattr("time.time", lambda: NOW)
    monkeypatch.setattr(
        "src.proxy.bedrock_principal.get_settings",
        lambda: SimpleNamespace(webhook_events_table=registry.name, aws_region="us-east-1"),
    )
    context = active_session(monkeypatch)
    run_id = mint(registry, provisioning=True)
    context.update_item(
        Key={"PK": "session#session-a", "SK": "header"},
        UpdateExpression="SET chatLease.run_id = :run",
        ExpressionAttributeValues={":run": run_id},
    )
    assert await read_routing_run(run_id) is None


def test_crew_owner_guard_rejects_second_session_but_separates_tenants(registry):
    first = mint(registry)
    with pytest.raises(CrewRunConflictError):
        mint(registry, session_id="session-b")
    other = mint(registry, tenant_id="other-tenant", session_id="session-b")
    assert other != first
    assert registry.get_item(Key={"event_id": "crew-owner#tenant#alice", "arrived_at": "crew"})["Item"]["run_id"] == first
    assert registry.get_item(Key={"event_id": "crew-owner#other-tenant#alice", "arrived_at": "crew"})["Item"]["run_id"] == other


def test_crew_owner_lease_refuses_tenantless_key_before_any_write(registry):
    with pytest.raises(ValueError):
        mint(registry, tenant_id="")
    with pytest.raises(ValueError):
        CrewRunStore(registry).attach_session(
            tenant_id="",
            user_id="alice",
            session_id="session-b",
            lease_generation=2,
            target_kind="eks-pod",
            target_ref="pod-123",
            now=NOW,
        )
    assert registry.scan()["Count"] == 0


def test_crew_second_session_attaches_to_existing_owner_run_atomically(registry):
    run_id = mint(registry)
    store = CrewRunStore(registry)
    options = dict(
        tenant_id="tenant",
        user_id="alice",
        session_id="session-b",
        lease_generation=2,
        target_kind="eks-pod",
        target_ref="pod-123",
        now=NOW,
    )
    assert store.attach_session(**options) == run_id
    owner_key = {"event_id": "crew-owner#tenant#alice", "arrived_at": "crew"}
    marker_key = {"event_id": "crew-lease#tenant#session-b#2", "arrived_at": "crew"}
    assert registry.get_item(Key=owner_key)["Item"]["session_count"] == 2
    assert registry.get_item(Key=marker_key)["Item"]["run_id"] == run_id
    assert [row for row in registry.scan()["Items"] if row.get("kind") == "crew"] == [store.read_run(run_id)]
    with pytest.raises(CrewRunConflictError):
        store.attach_session(**options)
    assert registry.get_item(Key=owner_key)["Item"]["session_count"] == 2


@pytest.mark.parametrize("first_end", ["session-a", "session-b"])
def test_ending_one_attached_session_preserves_owner_run_until_last_session(registry, first_end):
    run_id = mint(registry)
    store = CrewRunStore(registry)
    store.attach_session(
        tenant_id="tenant",
        user_id="alice",
        session_id="session-b",
        lease_generation=2,
        target_kind="eks-pod",
        target_ref="pod-123",
        now=NOW,
    )
    remaining = "session-b" if first_end == "session-a" else "session-a"
    assert store.revoke_for_lease(tenant_id="tenant", session_id=first_end, lease_generation=2, expected_run_id=run_id)
    owner = registry.get_item(Key={"event_id": "crew-owner#tenant#alice", "arrived_at": "crew"})["Item"]
    assert owner["session_count"] == 1 and owner["run_id"] == run_id
    assert store.read_run(run_id)["status"] == "active"
    assert registry.get_item(Key={"event_id": f"crew-lease#tenant#{remaining}#2", "arrived_at": "crew"}).get("Item")
    assert store.revoke_for_lease(tenant_id="tenant", session_id=remaining, lease_generation=2, expected_run_id=run_id)
    assert store.read_run(run_id) is None
    assert registry.get_item(Key={"event_id": "crew-owner#tenant#alice", "arrived_at": "crew"}).get("Item") is None


def test_detached_session_releases_crew_capacity_without_unbinding_others(registry):
    run_id = mint(registry)
    store = CrewRunStore(registry)
    options = dict(tenant_id="tenant", user_id="alice", lease_generation=2, target_kind="eks-pod", target_ref="pod-123", now=NOW)
    assert store.attach_session(**options, session_id="session-b", max_sessions=2) == run_id
    with pytest.raises(CrewRunConflictError):
        store.attach_session(**options, session_id="session-c", max_sessions=2)
    assert store.revoke_for_lease(tenant_id="tenant", session_id="session-b", lease_generation=2)
    assert store.attach_session(**options, session_id="session-c", max_sessions=2) == run_id


def test_concurrent_last_session_end_retries_with_current_owner_count(registry, monkeypatch):
    run_id = mint(registry)
    store = CrewRunStore(registry)
    store.attach_session(
        tenant_id="tenant",
        user_id="alice",
        session_id="session-b",
        lease_generation=2,
        target_kind="eks-pod",
        target_ref="pod-123",
        now=NOW,
    )
    original_transaction = registry.meta.client.transact_write_items
    intervened = []

    def interleave(**kwargs):
        if not intervened:
            intervened.append(True)
            assert store.revoke_for_lease(tenant_id="tenant", session_id="session-b", lease_generation=2)
        return original_transaction(**kwargs)

    monkeypatch.setattr(registry.meta.client, "transact_write_items", interleave)
    assert store.revoke_for_lease(tenant_id="tenant", session_id="session-a", lease_generation=2, expected_run_id=run_id)
    assert intervened
    assert store.read_run(run_id) is None
    assert registry.get_item(Key={"event_id": "crew-owner#tenant#alice", "arrived_at": "crew"}).get("Item") is None


def test_stale_attached_session_cannot_revoke_replacement_owner(registry):
    run_id = mint(registry)
    store = CrewRunStore(registry)
    store.attach_session(
        tenant_id="tenant",
        user_id="alice",
        session_id="session-b",
        lease_generation=2,
        target_kind="eks-pod",
        target_ref="pod-123",
        now=NOW,
    )
    replacement = mint(registry, session_id="session-c", lease_generation=3, now=NOW + 301, lease_expires_at=NOW + 600)
    with pytest.raises(CrewRunConflictError):
        store.revoke_for_lease(tenant_id="tenant", session_id="session-b", lease_generation=2, expected_run_id=run_id)
    assert store.read_run(replacement) is not None
    owner = registry.get_item(Key={"event_id": "crew-owner#tenant#alice", "arrived_at": "crew"})["Item"]
    assert owner["run_id"] == replacement and owner["session_count"] == 1


@pytest.mark.parametrize(
    "override",
    [
        {"tenant_id": "other-tenant"},
        {"user_id": "bob"},
        {"lease_generation": 3},
        {"target_ref": "other-pod"},
        {"now": NOW + 301},
        {"session_id": "session-a"},
    ],
)
def test_crew_attachment_refuses_foreign_or_expired_owner(registry, override):
    mint(registry)
    options = dict(
        tenant_id="tenant",
        user_id="alice",
        session_id="session-b",
        lease_generation=2,
        target_kind="eks-pod",
        target_ref="pod-123",
        now=NOW,
    )
    with pytest.raises(CrewRunConflictError):
        CrewRunStore(registry).attach_session(**(options | override))
    assert registry.get_item(Key={"event_id": "crew-owner#tenant#alice", "arrived_at": "crew"})["Item"]["session_count"] == 1


@pytest.mark.parametrize("invalid_limit", [0, 21, False, 1.5])
def test_crew_attachment_rejects_unbounded_capacity(registry, invalid_limit):
    mint(registry)
    store = CrewRunStore(registry)
    with pytest.raises(ValueError):
        store.attach_session(
            tenant_id="tenant",
            user_id="alice",
            session_id="session-b",
            lease_generation=2,
            target_kind="eks-pod",
            target_ref="pod-123",
            now=NOW,
            max_sessions=invalid_limit,
        )
    assert registry.get_item(Key={"event_id": "crew-lease#tenant#session-b#2", "arrived_at": "crew"}).get("Item") is None


def test_crew_attachment_capacity_and_failed_creation_do_not_leave_session_markers(registry):
    run_id = mint(registry)
    store = CrewRunStore(registry)
    options = dict(
        tenant_id="tenant",
        user_id="alice",
        session_id="session-b",
        lease_generation=2,
        target_kind="eks-pod",
        target_ref="pod-123",
        now=NOW,
    )
    with pytest.raises(CrewRunConflictError):
        store.attach_session(**options, max_sessions=1)
    with pytest.raises(CrewRunConflictError):
        store.attach_session(
            **options,
            creation_update={
                "TableName": registry.name,
                "Key": {"event_id": "missing-creation", "arrived_at": "creation"},
                "ConditionExpression": "attribute_exists(event_id)",
                "ExpressionAttributeValues": {},
            },
        )
    marker = registry.get_item(Key={"event_id": "crew-lease#tenant#session-b#2", "arrived_at": "crew"}).get("Item")
    owner = registry.get_item(Key={"event_id": "crew-owner#tenant#alice", "arrived_at": "crew"})["Item"]
    assert marker is None
    assert owner["session_count"] == 1
    assert store.read_run(run_id)["status"] == "active"


def test_session_scoped_runs_do_not_acquire_the_crew_owner_guard(registry):
    first = mint(registry, executor_scope="session")
    second = mint(registry, session_id="session-b", executor_scope="session")
    assert first != second
    assert registry.get_item(Key={"event_id": "crew-owner#tenant#alice", "arrived_at": "crew"}).get("Item") is None


def test_expired_crew_owner_can_be_replaced_without_stale_revocation(registry):
    first = mint(registry)
    successor = mint(registry, session_id="session-b", lease_generation=3, now=NOW + 301, lease_expires_at=NOW + 600)
    with pytest.raises(CrewRunConflictError):
        CrewRunStore(registry).revoke_for_lease(tenant_id="tenant", session_id="session-a", lease_generation=2)
    assert registry.get_item(Key={"event_id": "crew-owner#tenant#alice", "arrived_at": "crew"})["Item"]["run_id"] == successor
    assert CrewRunStore(registry).read_run(first) is not None


@pytest.mark.asyncio
async def test_crew_run_is_server_recorded_and_resolves_to_human(registry, monkeypatch):
    monkeypatch.setattr("time.time", lambda: NOW)
    run_id = mint(registry)
    assert run_id != "session-a"
    row = await RunBindingResolver(registry.name, "us-east-1", table=registry).read_current(run_id)
    assert row["kind"] == "crew"
    assert row["lease_expires_at"] == NOW + 300
    assert CrewRunStore(registry).read_run(run_id)["target_ref"] == "pod-123"
    binding = verify_row_matches_caller(run_id=run_id, row=row, caller_user_id="worker", caller_org_id="tenant")
    assert binding.root_human_id == "alice"
    assert binding.is_human_rooted is True
    with pytest.raises(CrewRunConflictError):
        mint(registry)


def active_session(monkeypatch, *, expires_at=NOW + 300):
    table = boto3.resource("dynamodb", region_name="us-east-1").create_table(
        TableName="chat-context",
        BillingMode="PAY_PER_REQUEST",
        KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
        AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"}, {"AttributeName": "SK", "AttributeType": "S"}],
    )
    table.put_item(
        Item={
            "PK": "session#session-a",
            "SK": "header",
            "tenantId": "tenant",
            "ownerUserId": "alice",
            "sessionMode": "persistent",
            "sessionState": "active",
            "status": "active",
            "ttl": NOW + 3600,
            "chatLease": {"run_id": "original-run", "generation": 2, "sandbox_uid": "pod-123", "expires_at": expires_at},
        }
    )
    monkeypatch.setattr("src.proxy.bedrock_principal._get_context_table", lambda: table)
    return table


@pytest.mark.asyncio
async def test_crew_run_expires_even_before_ttl_deletes_row(registry, monkeypatch):
    from types import SimpleNamespace

    from src.proxy.bedrock_principal import read_routing_run

    monkeypatch.setattr("time.time", lambda: NOW)
    monkeypatch.setattr(
        "src.proxy.bedrock_principal.get_settings",
        lambda: SimpleNamespace(webhook_events_table=registry.name, aws_region="us-east-1"),
    )
    context = active_session(monkeypatch)
    run_id = mint(registry)
    assert await read_routing_run(run_id) is not None
    resolver = RunBindingResolver(registry.name, "us-east-1", table=registry)
    assert await resolver.resolve(run_id) is not None
    context.update_item(
        Key={"PK": "session#session-a", "SK": "header"},
        UpdateExpression="SET chatLease.expires_at = :expiry",
        ExpressionAttributeValues={":expiry": NOW + 600},
    )
    monkeypatch.setattr("time.time", lambda: NOW + 301)
    assert await read_routing_run(run_id) is not None
    context.update_item(
        Key={"PK": "session#session-a", "SK": "header"},
        UpdateExpression="SET chatLease.expires_at = :expiry",
        ExpressionAttributeValues={":expiry": NOW + 300},
    )
    assert await read_routing_run(run_id) is None
    assert await resolver.resolve(run_id) is not None
    assert CrewRunStore(registry).read_run(run_id) is not None
    context.update_item(
        Key={"PK": "session#session-a", "SK": "header"},
        UpdateExpression="SET chatLease.expires_at = :future, sessionState = :ending",
        ExpressionAttributeValues={":future": NOW + 600, ":ending": "ending"},
    )
    assert await read_routing_run(run_id) is None


@pytest.mark.asyncio
async def test_revocation_deletes_both_run_and_lease_marker_without_a_cached_verdict(registry, monkeypatch):
    from types import SimpleNamespace

    from src.proxy.bedrock_principal import read_routing_run

    monkeypatch.setattr("time.time", lambda: NOW)
    monkeypatch.setattr(
        "src.proxy.bedrock_principal.get_settings",
        lambda: SimpleNamespace(webhook_events_table=registry.name, aws_region="us-east-1"),
    )
    active_session(monkeypatch)
    run_id = mint(registry)
    import fakeredis.aioredis

    resolver = RunBindingResolver(registry.name, "us-east-1", client=fakeredis.aioredis.FakeRedis(), table=registry)
    assert await resolver.resolve(run_id) is not None
    assert await read_routing_run(run_id) is not None
    assert CrewRunStore(registry).revoke_for_lease(tenant_id="tenant", session_id="session-a", lease_generation=2)
    assert await read_routing_run(run_id) is None
    assert await resolver.resolve(run_id) is None
    from src.proxy.bedrock_principal import BedrockRoutingIdentityError, resolve_routing_principal
    from tests.proxy.test_bedrock_routing_resolver import _context

    caller = _context(
        user_id="worker",
        org_id="__platform__",
        team_id="__agents__",
        account_type="service",
        auth_source="iam",
        scope="internal",
        attributed_org_id="tenant",
    )
    with pytest.raises(BedrockRoutingIdentityError) as error:
        await resolve_routing_principal(None, caller, agent_run_id=run_id)
    assert error.value.details["reason"] == "unknown_run"
    assert not CrewRunStore(registry).revoke_for_lease(tenant_id="tenant", session_id="session-a", lease_generation=2)
    assert mint(registry) != run_id


@pytest.mark.asyncio
async def test_minted_run_routes_to_owner_via_uncached_principal_lookup(registry, monkeypatch):
    from types import SimpleNamespace

    from src.proxy.bedrock_principal import resolve_routing_principal
    from tests.proxy.test_bedrock_routing_resolver import _context

    monkeypatch.setattr("time.time", lambda: NOW)
    monkeypatch.setattr(
        "src.proxy.bedrock_principal.get_settings",
        lambda: SimpleNamespace(webhook_events_table=registry.name, aws_region="us-east-1"),
    )
    active_session(monkeypatch)
    run_id = mint(registry)

    class Database:
        async def scalar(self, statement):
            return SimpleNamespace(id="alice", user_kind="human", org_id="tenant", team_id="team")

    caller = _context(
        user_id="scaledjob-worker",
        org_id="__platform__",
        team_id="__agents__",
        account_type="service",
        auth_source="iam",
        scope="internal",
        attributed_org_id="tenant",
    )
    principal, root_human = await resolve_routing_principal(Database(), caller, agent_run_id=run_id)
    assert root_human == "alice"
    assert (principal.user_id, principal.org_id, principal.team_id) == ("alice", "tenant", "team")
    assert caller._crew_root_human_id == "alice"
    assert caller._crew_root_run_id == run_id


def test_expired_row_marker_can_be_cleaned_after_independent_ttl_deletion(registry):
    run_id = mint(registry)
    registry.delete_item(Key={"event_id": run_id, "arrived_at": CrewRunStore(registry).read_run(run_id)["arrived_at"]})
    assert not CrewRunStore(registry).revoke_for_lease(tenant_id="tenant", session_id="session-a", lease_generation=2)
    assert registry.get_item(Key={"event_id": "crew-lease#tenant#session-a#2", "arrived_at": "crew"}).get("Item") is None


@pytest.mark.asyncio
async def test_crew_binding_rejects_changed_session_owner_and_tenant(registry, monkeypatch):
    from types import SimpleNamespace

    from src.budget.run_binding import RunBindingError
    from src.proxy.bedrock_principal import read_routing_run

    monkeypatch.setattr("time.time", lambda: NOW)
    monkeypatch.setattr(
        "src.proxy.bedrock_principal.get_settings",
        lambda: SimpleNamespace(webhook_events_table=registry.name, aws_region="us-east-1"),
    )
    context = active_session(monkeypatch)
    run_id = mint(registry)
    row = await read_routing_run(run_id)
    assert verify_row_matches_caller(run_id=run_id, row=row, caller_user_id="worker", caller_org_id="tenant").root_human_id == "alice"
    with pytest.raises(RunBindingError) as error:
        verify_row_matches_caller(run_id=run_id, row=row, caller_user_id="worker", caller_org_id="tenant-b")
    assert error.value.reason == "tenant_mismatch"

    header_key = {"PK": "session#session-a", "SK": "header"}
    context.update_item(Key=header_key, UpdateExpression="SET ownerUserId = :owner", ExpressionAttributeValues={":owner": "bob"})
    assert await read_routing_run(run_id) is None
    context.update_item(Key=header_key, UpdateExpression="SET ownerUserId = :owner", ExpressionAttributeValues={":owner": "alice"})
    context.update_item(Key=header_key, UpdateExpression="SET tenantId = :tenant", ExpressionAttributeValues={":tenant": "tenant-b"})
    assert await read_routing_run(run_id) is None
    context.update_item(Key=header_key, UpdateExpression="SET tenantId = :tenant", ExpressionAttributeValues={":tenant": "tenant"})
    key = {"event_id": run_id, "arrived_at": CrewRunStore(registry).read_run(run_id)["arrived_at"]}
    registry.update_item(Key=key, UpdateExpression="SET user_id = :user", ExpressionAttributeValues={":user": "bob"})
    assert await read_routing_run(run_id) is None


@pytest.mark.asyncio
async def test_crew_routing_refuses_owner_moved_to_another_tenant(registry, monkeypatch):
    from types import SimpleNamespace

    from src.proxy.bedrock_principal import BedrockRoutingIdentityError, resolve_routing_principal
    from tests.proxy.test_bedrock_routing_resolver import _context

    monkeypatch.setattr("time.time", lambda: NOW)
    monkeypatch.setattr(
        "src.proxy.bedrock_principal.get_settings",
        lambda: SimpleNamespace(webhook_events_table=registry.name, aws_region="us-east-1"),
    )
    active_session(monkeypatch)
    run_id = mint(registry)

    class Database:
        async def scalar(self, statement):
            return SimpleNamespace(id="alice", user_kind="human", org_id="tenant-b", team_id="team")

    caller = _context(
        user_id="worker",
        org_id="__platform__",
        team_id="__agents__",
        account_type="service",
        auth_source="iam",
        scope="internal",
        attributed_org_id="tenant",
    )
    with pytest.raises(BedrockRoutingIdentityError) as error:
        await resolve_routing_principal(Database(), caller, agent_run_id=run_id)
    assert error.value.details["reason"] == "run_owner_mismatch"


def test_crew_run_is_visible_in_owner_activity_with_date_filters(registry):
    from datetime import UTC, datetime, timedelta

    from src.activity.service import ActivityService

    run_id = mint(registry)
    timestamp = datetime.fromtimestamp(NOW, UTC)
    arrived_at = timestamp.strftime("%Y-%m-%dT%H:%M:%SZ")
    service = ActivityService(table_name=registry.name)
    result = service.query_by_user(
        "alice",
        tenant_id="tenant",
        since=(timestamp - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        until=(timestamp + timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    assert [item.invocation_id for item in result.items] == [run_id]
    assert result.items[0].invoked_at == arrived_at
    assert result.items[0].root_human_id == "alice"
    assert result.items[0].is_human_rooted is True
    assert result.items[0].trigger_kind == "human"
    assert service.get_invocation(run_id, user_id="alice", tenant_id="tenant") is not None
    assert service.get_invocation(run_id, user_id="bob", tenant_id="tenant") is None
    assert service.get_invocation(run_id, user_id="alice", tenant_id="tenant-other") is None


@pytest.mark.asyncio
async def test_ec2_run_records_deployment_without_using_chat_session(registry, monkeypatch):
    from types import SimpleNamespace

    from src.proxy.bedrock_principal import read_routing_run

    monkeypatch.setattr(
        "src.proxy.bedrock_principal.get_settings", lambda: SimpleNamespace(webhook_events_table=registry.name, aws_region="us-east-1")
    )
    monkeypatch.setattr("src.proxy.bedrock_principal._get_context_table", lambda: None)
    with pytest.raises(ValueError, match="recorded deployment"):
        mint(registry, target_kind="ec2-instance", target_ref="i-example")
    with pytest.raises(ValueError, match="approved guest role"):
        CrewRunStore(registry).mint(
            tenant_id="tenant",
            user_id="alice",
            session_id="session-a",
            lease_generation=2,
            lease_expires_at=NOW + 300,
            target_kind="ec2-instance",
            target_ref="i-example",
            deployment_id="example-deployment",
            now=NOW,
        )
    run_id = mint(registry, target_kind="ec2-instance", target_ref="i-example", deployment_id="example-deployment", executor_scope="session")
    row = await read_routing_run(run_id)
    assert row["deployment_id"] == "example-deployment"
    assert row["target_ref"] == "i-example"
    assert row["host_role_arn"] == "arn:aws:iam::000000000000:role/guest"
    assert row["executor_scope"] == "session"
    with pytest.raises(ValueError, match="unsupported Crew executor scope"):
        mint(registry, target_kind="ec2-instance", target_ref="i-example", deployment_id="example-deployment", executor_scope="user")


def test_pinned_ec2_host_run_is_provisioning_until_verified_activation(registry):
    store = CrewRunStore(registry, host_role_arn="arn:aws:iam::000000000000:role/guest")
    run_id = mint(
        registry, target_kind="ec2-instance", target_ref="i-example", deployment_id="example-deployment", executor_scope="session", provisioning=True
    )
    marker = registry.get_item(Key={"event_id": "crew-lease#tenant#session-a#2", "arrived_at": "crew"})["Item"]
    assert store.read_run(run_id)["status"] == marker["status"] == "provisioning"
    with pytest.raises(CrewRunConflictError):
        store.renew_for_lease(
            tenant_id="tenant",
            user_id="alice",
            session_id="session-a",
            lease_generation=2,
            target_ref="i-example",
            run_id=run_id,
            now=NOW + 1,
            lease_expires_at=NOW + 600,
        )
    assert store.revoke_for_lease(tenant_id="tenant", session_id="session-a", lease_generation=2, expected_run_id=run_id)
    assert store.read_run(run_id) is None


def prepared_host(registry):
    store = CrewRunStore(registry, host_role_arn="arn:aws:iam::000000000000:role/guest")
    run_id = mint(
        registry, target_kind="ec2-instance", target_ref="i-example", deployment_id="example-deployment", executor_scope="session", provisioning=True
    )
    arguments = dict(
        run_id=run_id,
        tenant_id="tenant",
        user_id="alice",
        session_id="session-a",
        lease_generation=2,
        target_ref="i-example",
        deployment_id="example-deployment",
        now=NOW + 1,
    )
    return store, arguments


def test_host_activation_updates_marker_and_run_together(registry):
    store, arguments = prepared_host(registry)
    store.activate_for_host(**arguments)
    marker = registry.get_item(Key={"event_id": "crew-lease#tenant#session-a#2", "arrived_at": "crew"})["Item"]
    assert marker["status"] == store.read_run(arguments["run_id"])["status"] == "active"
    with pytest.raises(CrewRunConflictError):
        store.activate_for_host(**arguments)


@pytest.mark.parametrize("change", ["owner", "instance", "deployment", "generation", "expired", "marker", "run", "role"])
def test_host_activation_denies_changed_or_retired_lease(registry, change):
    store, arguments = prepared_host(registry)
    row = store.read_run(arguments["run_id"])
    if change in {"owner", "instance", "deployment", "generation", "expired"}:
        name, replacement = {
            "owner": ("user_id", "bob"),
            "instance": ("target_ref", "i-foreign"),
            "deployment": ("deployment_id", "other"),
            "generation": ("lease_generation", 3),
            "expired": ("now", NOW + 301),
        }[change]
        arguments[name] = replacement
    elif change == "role":
        store.host_role_arn = "arn:aws:iam::000000000000:role/foreign"
    else:
        key = (
            {"event_id": "crew-lease#tenant#session-a#2", "arrived_at": "crew"}
            if change == "marker"
            else {"event_id": arguments["run_id"], "arrived_at": row["arrived_at"]}
        )
        registry.update_item(
            Key=key,
            UpdateExpression="SET #status = :active",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={":active": "active"},
        )
    before = registry.scan()["Items"]
    with pytest.raises(CrewRunConflictError):
        store.activate_for_host(**arguments)
    assert registry.scan()["Items"] == before


def test_host_activation_fences_interleaved_marker_replacement(registry, monkeypatch):
    store, arguments = prepared_host(registry)
    original = registry.meta.client.transact_write_items

    def replace_marker(**options):
        registry.update_item(
            Key={"event_id": "crew-lease#tenant#session-a#2", "arrived_at": "crew"},
            UpdateExpression="SET run_id = :run",
            ExpressionAttributeValues={":run": "successor"},
        )
        return original(**options)

    monkeypatch.setattr(registry.meta.client, "transact_write_items", replace_marker)
    with pytest.raises(CrewRunConflictError):
        store.activate_for_host(**arguments)
    assert store.read_run(arguments["run_id"])["status"] == "provisioning"


def test_ec2_run_renewal_requires_current_owner_generation_and_live_marker(registry):
    store = CrewRunStore(registry, host_role_arn="arn:aws:iam::000000000000:role/guest")
    run_id = mint(registry, target_kind="ec2-instance", target_ref="i-example", deployment_id="example-deployment", executor_scope="session")
    arguments = dict(
        tenant_id="tenant",
        user_id="alice",
        session_id="session-a",
        lease_generation=2,
        target_ref="i-example",
        run_id=run_id,
        now=NOW + 200,
        lease_expires_at=NOW + 600,
    )
    with pytest.raises(CrewRunConflictError):
        store.renew_for_lease(**{**arguments, "target_ref": "i-foreign"})
    assert store.read_run(run_id)["lease_expires_at"] == NOW + 300
    store.renew_for_lease(**arguments)
    assert store.read_run(run_id)["lease_expires_at"] == NOW + 600
    with pytest.raises(CrewRunConflictError):
        store.renew_for_lease(**{**arguments, "now": NOW + 601, "lease_expires_at": NOW + 900})
    assert store.revoke_for_lease(tenant_id="tenant", session_id="session-a", lease_generation=2, expected_run_id=run_id)
    with pytest.raises(CrewRunConflictError):
        store.renew_for_lease(**arguments)


def test_host_activation_declares_only_the_expression_values_each_item_uses(registry):
    """DynamoDB (unlike moto) refuses a TransactWriteItems item whose ExpressionAttributeValues
    carries a placeholder its expressions never reference. The run item passed the whole value
    map including :run and :arrived, so every real activation failed with ValidationException and
    GET /crew/target answered 500 for a ready Dev Box host (dev 2026-10-19, #8223)."""
    import re

    store, arguments = prepared_host(registry)
    client = store.table.meta.client
    real = client.transact_write_items
    seen = []

    def validating(**kwargs):
        for item in kwargs["TransactItems"]:
            update = item["Update"]
            expressions = " ".join(update.get(field, "") for field in ("UpdateExpression", "ConditionExpression"))
            referenced = set(re.findall(r":[A-Za-z_][A-Za-z0-9_]*", expressions))
            declared = set(update.get("ExpressionAttributeValues", {}))
            assert declared == referenced, (update["Key"], sorted(declared - referenced), sorted(referenced - declared))
            seen.append(update["Key"])
        return real(**kwargs)

    client.transact_write_items = validating
    try:
        store.activate_for_host(**arguments)
    finally:
        client.transact_write_items = real
    assert len(seen) == 2
    assert store.read_run(arguments["run_id"])["status"] == "active"
