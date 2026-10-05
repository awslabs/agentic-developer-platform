"""History storage integration with moto; identity callbacks are synthetic."""

import json

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from src.agentauth.bootstrap import BootstrapStore
from src.agentauth.chat_capability import (
    ChatAuthorizationRefusedError,
    ChatAuthorizationUnavailableError,
    ChatCapabilityService,
    ChatLaunch,
    ChatLaunchStore,
)
from src.agentauth.chat_history_store import ChatHistoryExpiredError, ChatHistoryStore
from src.agentauth.run_credential import CREDENTIAL_KEY_ENV
from src.agentauth.workload import VerifiedPod

NOW = 1_800_000_000
POD = VerifiedPod("pod-a", "sandbox-a", "sandboxes", "sandbox", "127.0.0.1")
CONTEXT_OWNER = {"orgId": "tenant-a", "tenantId": "tenant-a", "teamId": "team-a", "ownerUserId": "alice"}


@pytest.fixture
def history():
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        client.create_table(
            TableName="authority",
            BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}, {"AttributeName": "sk", "AttributeType": "S"}],
        )
        table = boto3.resource("dynamodb", region_name="us-east-1").create_table(
            TableName="context",
            BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"}, {"AttributeName": "SK", "AttributeType": "S"}],
        )
        launches = ChatLaunchStore(BootstrapStore(table_name="authority", dynamodb_client=client))
        state = {"live": True, "members": {("tenant-a", "alice", "team-a"), ("tenant-a", "bob", "team-a"), ("tenant-b", "carol", "team-b")}}
        capabilities = ChatCapabilityService(
            launches,
            current=lambda launch, now: state["live"],
            member=lambda tenant, user, team: (tenant, user, team) in state["members"],
            env={CREDENTIAL_KEY_ENV: "synthetic-history-key"},
        )

        def credential(user="alice", tenant="tenant-a", team="team-a", run="run-a", session="session-a", **changes):
            launch = ChatLaunch.model_validate(
                {
                    "run_id": run,
                    "tenant_id": tenant,
                    "user_id": user,
                    "team_id": team,
                    "session_id": session,
                    "sandbox_uid": "pod-a",
                    "image_digest": "sha256:" + "a" * 64,
                    "attempt": 1,
                    "credential_epoch": 1,
                    "lease_generation": 1,
                    "grant_id": f"grant-{run}",
                    "grant_epoch": 1,
                    "operations": ["history.read", "history.expand"],
                    "expires_at": NOW + 1000,
                    **changes,
                }
            )
            launches.register(launch)
            return capabilities.issue(run, POD, now=NOW)

        header = {
            "PK": "session#session-a",
            "SK": "header",
            "ownerUserId": "alice",
            "tenantId": "tenant-a",
            "orgId": "tenant-a",
            "teamId": "team-a",
            "ttl": NOW + 1000,
        }
        table.put_item(Item=header)
        yield ChatHistoryStore(table, capabilities), table, credential, state, header


def read(store, token, **changes):
    return store.read_page(token, **{"run_id": "run-a", "session_id": "session-a", "now": NOW, **changes})


def messages(store, token, ids, **changes):
    return store.get_messages(token, ids=ids, **{"run_id": "run-a", "session_id": "session-a", "now": NOW, **changes})


def summary(store, token, **changes):
    return store.get_summary(token, **{"run_id": "run-a", "session_id": "session-a", "summary_id": "summary-a", "now": NOW, **changes})


def seed(table, *, owned=True):
    ownership = CONTEXT_OWNER if owned else {}
    for ordinal in (3, 1, 2):
        table.put_item(
            Item={**ownership, "PK": "session#session-a", "SK": f"item#{ordinal:08d}", "ordinal": ordinal, "type": "msg", "ref": f"msg-{ordinal}"}
        )
        table.put_item(
            Item={
                **ownership,
                "PK": "session#session-a",
                "SK": f"msg#msg-{ordinal}",
                "role": "user" if ordinal == 1 else "assistant",
                "content": f"message {ordinal}",
                "ts": "2026-10-03T10:30:00.123456Z",
                "tokens": 4,
                "parts": json.dumps([{"type": "file", "artifactId": "attachment-a"}]),
            }
        )
    table.put_item(
        Item={
            **ownership,
            "PK": "session#session-a",
            "SK": "sum#summary-a",
            "depth": 1,
            "kind": "leaf",
            "content": "compacted",
            "sourceIds": ["msg-2", "msg-1"],
            "earliestAt": "2026-10-03T10:30:00.123456Z",
            "latestAt": "2026-10-03T10:31:00Z",
            "tokens": 2,
        }
    )


def test_ordered_pagination_and_summary_sources_preserve_attachments(history):
    store, table, credential, _, _ = history
    seed(table)
    token = credential()
    first = read(store, token, limit=2)
    assert first["status"] == "partial"
    assert [entry["ordinal"] for entry in first["entries"]] == [1, 2]
    final = read(store, token, limit=2, cursor=first["next_cursor"])
    assert final["status"] == "ok"
    assert [entry["ordinal"] for entry in final["entries"]] == [3]
    assert final["next_cursor"] is None
    expanded = summary(store, token)["entries"][0]["summary"]
    hydrated = messages(store, token, expanded["sourceIds"])
    assert [entry["ref"] for entry in hydrated["entries"]] == ["msg-2", "msg-1"]
    assert hydrated["entries"][0]["message"]["parts"] == [{"type": "file", "artifactId": "attachment-a"}]
    assert hydrated["entries"][0]["message"]["ts"] == expanded["earliestAt"]
    assert "PK" not in hydrated["entries"][0]["message"]


def test_empty_and_missing_sources_are_not_conflated(history):
    store, _, credential, _, _ = history
    token = credential()
    assert read(store, token)["status"] == "empty"
    assert messages(store, token, ["missing"])["coverage"]["missing_source_ids"] == ["missing"]
    assert messages(store, token, ["missing"])["status"] == "partial"
    assert summary(store, token)["status"] == "partial"
    assert summary(store, token)["coverage"]["complete"] is False


@pytest.mark.parametrize("user,tenant,team", [("bob", "tenant-a", "team-a"), ("carol", "tenant-b", "team-b")])
def test_other_user_and_tenant_cannot_read_guessed_ids(history, user, tenant, team):
    store, table, credential, _, _ = history
    seed(table)
    token = credential(user=user, tenant=tenant, team=team)
    for call in (lambda: read(store, token), lambda: messages(store, token, ["msg-1"]), lambda: summary(store, token)):
        with pytest.raises(ChatAuthorizationRefusedError):
            call()


def test_explicit_sharing_requires_current_acl_and_membership_on_every_read(history):
    store, table, credential, state, header = history
    header["aclUserIds"] = ["bob"]
    table.put_item(Item=header)
    token = credential(user="bob")
    assert read(store, token)["status"] == "empty"
    state["members"].remove(("tenant-a", "bob", "team-a"))
    with pytest.raises(ChatAuthorizationRefusedError):
        read(store, token)
    state["members"].add(("tenant-a", "bob", "team-a"))
    header["aclUserIds"] = []
    table.put_item(Item=header)
    with pytest.raises(ChatAuthorizationRefusedError):
        read(store, token)


@pytest.mark.parametrize("change", [{"ownerUserId": None}, {"orgId": None}, {"tenant_id": "other"}, {"user_id": "bob"}, {"aclUserIds": "alice"}])
def test_ambiguous_legacy_header_is_denied(history, change):
    store, table, credential, _, header = history
    table.put_item(Item={**header, **change})
    with pytest.raises(ChatAuthorizationRefusedError):
        read(store, credential())


def test_personal_chat_with_explicit_empty_team_remains_private(history):
    store, table, credential, state, header = history
    table.put_item(Item={**header, "teamId": ""})
    state["members"].update({("tenant-a", "alice", ""), ("tenant-a", "bob", "")})
    assert read(store, credential(team=""))["status"] == "empty"
    with pytest.raises(ChatAuthorizationRefusedError):
        read(store, credential(user="bob", team="", run="run-b"), run_id="run-b")


@pytest.mark.parametrize("sort_key,operation", [("msg#msg-1", "message"), ("sum#summary-a", "summary"), ("item#00000001", "page")])
def test_conflicting_child_ownership_is_denied(history, sort_key, operation):
    store, table, credential, _, _ = history
    seed(table)
    table.update_item(
        Key={"PK": "session#session-a", "SK": sort_key},
        UpdateExpression="SET user_id = :other",
        ExpressionAttributeValues={":other": "bob"},
    )
    token = credential()
    with pytest.raises(ChatAuthorizationRefusedError):
        if operation == "message":
            messages(store, token, ["msg-1"])
        elif operation == "summary":
            summary(store, token)
        else:
            read(store, token)


def test_guessed_message_never_queries_another_partition(history):
    store, table, credential, _, _ = history
    table.put_item(Item={"PK": "session#other", "SK": "msg#secret", "content": "private"})
    assert messages(store, credential(), ["secret"])["entries"] == []
    with pytest.raises(ChatAuthorizationRefusedError):
        messages(store, credential(), ["session#other/msg#secret"])


def test_cursor_is_bound_to_run_limit_and_current_authority(history):
    store, table, credential, state, _ = history
    seed(table)
    token = credential()
    cursor = read(store, token, limit=1)["next_cursor"]
    for changes in ({"limit": 2}, {"cursor": cursor + "x"}, {"cursor": "☃.☃.☃"}, {"cursor": ""}):
        with pytest.raises(ChatAuthorizationRefusedError):
            read(store, token, **{"limit": 1, "cursor": cursor, **changes})
    with pytest.raises(ChatAuthorizationRefusedError):
        read(store, credential(run="run-b"), run_id="run-b", limit=1, cursor=cursor)
    state["live"] = False
    with pytest.raises(ChatAuthorizationRefusedError):
        read(store, token, limit=1, cursor=cursor)


def test_expired_capability_and_wrong_operation_are_denied(history):
    store, _, credential, _, _ = history
    with pytest.raises(ChatAuthorizationRefusedError):
        read(store, credential(), now=NOW + 300)
    with pytest.raises(ChatAuthorizationRefusedError):
        read(store, credential(run="limited", operations=["artifact.read"]), run_id="limited")
    with pytest.raises(ChatAuthorizationRefusedError):
        read(store, credential(), session_id="guessed")


def test_expired_session_is_not_an_empty_history(history):
    store, table, credential, _, header = history
    table.put_item(Item={**header, "ttl": NOW})
    with pytest.raises(ChatHistoryExpiredError):
        read(store, credential())


def test_empty_page_with_continuation_is_partial(history, monkeypatch):
    store, table, credential, _, _ = history
    seed(table)
    query = table.query

    def empty_page(**kwargs):
        result = query(**kwargs)
        result["Items"] = []
        return result

    monkeypatch.setattr(table, "query", empty_page)
    result = read(store, credential(), limit=1)
    assert result["entries"] == []
    assert result["status"] == "partial"
    assert result["next_cursor"]
    assert result["coverage"]["complete"] is False


@pytest.mark.parametrize("method", ["get_item", "query"])
def test_storage_outages_are_unavailable_not_empty(history, monkeypatch, method):
    store, table, credential, _, _ = history
    token = credential()

    def unavailable(**kwargs):
        raise ClientError({"Error": {"Code": "InternalServerError", "Message": "private diagnostic"}}, method)

    monkeypatch.setattr(table, method, unavailable)
    with pytest.raises(ChatAuthorizationUnavailableError, match="chat history unavailable") as error:
        read(store, token)
    assert "private diagnostic" not in str(error.value)


def test_authority_outage_is_not_empty(history):
    store, _, credential, _, _ = history
    token = credential()

    def unavailable(*args):
        raise RuntimeError("directory offline")

    store.capabilities.member = unavailable
    with pytest.raises(ChatAuthorizationUnavailableError):
        read(store, token)


def test_compacted_timeline_preserves_summary_entry(history):
    store, table, credential, _, _ = history
    seed(table)
    table.delete_item(Key={"PK": "session#session-a", "SK": "item#00000002"})
    table.put_item(
        Item={**CONTEXT_OWNER, "PK": "session#session-a", "SK": "item#00000001", "ordinal": 1, "type": "sum", "ref": "summary-a", "tokens": 2}
    )
    entries = read(store, credential())["entries"]
    assert entries == [{"ordinal": 1, "type": "sum", "ref": "summary-a", "tokens": 2}, {"ordinal": 3, "type": "msg", "ref": "msg-3"}]


def test_missing_message_does_not_shift_other_source_references(history):
    store, table, credential, _, _ = history
    seed(table)
    result = messages(store, credential(), ["msg-3", "missing", "msg-1", "msg-3"])
    assert result["status"] == "partial"
    assert result["coverage"]["missing_source_ids"] == ["missing"]
    assert [entry["ref"] for entry in result["entries"]] == ["msg-3", "msg-1", "msg-3"]


def test_renewed_capability_does_not_extend_cursor_lifetime(history):
    store, table, credential, _, _ = history
    seed(table)
    cursor = read(store, credential(), limit=1)["next_cursor"]
    refreshed = store.capabilities.issue("run-a", POD, now=NOW + 300)
    with pytest.raises(ChatAuthorizationRefusedError, match="cursor refused"):
        read(store, refreshed, now=NOW + 300, limit=1, cursor=cursor)


def test_corrupted_ordering_is_not_returned_as_valid_history(history):
    store, table, credential, _, _ = history
    table.put_item(Item={**CONTEXT_OWNER, "PK": "session#session-a", "SK": "item#00000001", "ordinal": 9, "type": "msg", "ref": "msg-1"})
    with pytest.raises(ChatAuthorizationUnavailableError, match="ordering unavailable"):
        read(store, credential())


@pytest.mark.parametrize("limit", [0, 101, True, "1"])
def test_page_size_is_bounded(history, limit):
    store, _, credential, _, _ = history
    with pytest.raises(ValueError):
        read(store, credential(), limit=limit)


@pytest.mark.parametrize("owned", [False, True])
@pytest.mark.parametrize("user,tenant,team", [("bob", "tenant-a", "team-a"), ("carol", "tenant-b", "team-b")])
@pytest.mark.parametrize("operation", ["message", "summary", "page"])
def test_recreated_header_cannot_adopt_another_owners_children(history, owned, user, tenant, team, operation):
    store, table, credential, _, header = history
    seed(table, owned=owned)
    table.delete_item(Key={"PK": header["PK"], "SK": "header"})
    table.put_item(
        Item={**header, "ownerUserId": user, "tenantId": tenant, "orgId": tenant, "teamId": team},
        ConditionExpression="attribute_not_exists(PK)",
    )
    token = credential(user=user, tenant=tenant, team=team)
    with pytest.raises(ChatAuthorizationRefusedError, match="record ownership unavailable"):
        if operation == "message":
            messages(store, token, ["msg-1"])
        elif operation == "summary":
            summary(store, token)
        else:
            read(store, token)


@pytest.mark.parametrize("field", list(CONTEXT_OWNER))
@pytest.mark.parametrize("missing", [True, False])
def test_incomplete_child_provenance_is_quarantined_even_for_current_owner(history, field, missing):
    store, table, credential, _, _ = history
    seed(table)
    key = {"PK": "session#session-a", "SK": "msg#msg-1"}
    row = table.get_item(Key=key)["Item"]
    if missing:
        del row[field]
    else:
        row[field] = None
    table.put_item(Item=row)
    with pytest.raises(ChatAuthorizationRefusedError, match="record ownership unavailable"):
        messages(store, credential(), ["msg-1"])
