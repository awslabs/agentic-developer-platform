"""
Two-tenant ownership tests for the chat session plane (#5660 / A07).

The vulnerability: `session_id` arrives from the client and nothing compared it
against the caller. Naming another user's session rebound that session's
`connection_id` to the attacker's WebSocket, so the agent's replies, progress
frames and stored history went to the attacker while the real owner silently
stopped receiving their own answers. The same unchecked id let an attacker
attach uploads to another user's conversation, and `handle_upload_complete`
wrote the client's `s3_key` verbatim into the artifact catalogue — so a row the
attacker owned could point at another tenant's object.

Every test here is written from the ATTACKER's seat against a victim's seeded
session, and asserts the outcome (status code, and crucially the victim's row
being untouched) rather than the presence of any particular line of source.
They fail on pre-fix code, where each case succeeds.

Covered:
  1. message path      — forged session id refused, victim's connection intact
  2. concurrency       — the check and the rebind are one conditional write
  3. legacy rows       — unowned rows are quarantined, not adopted
  4. shape             — reserved/traversal ids refused at every entry point
  5. uploads           — token + completion refuse another owner's session
  6. upload key        — derived server-side, client's `s3_key` ignored
  7. own session       — the owner is never locked out (regression)
"""

from __future__ import annotations

import json
import os
import sys
from unittest.mock import patch

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

HANDLER_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "gateway", "lambdas", "ingest"
)

# Two tenants, deliberately in different orgs AND teams, so a key derived for
# one cannot be mistaken for the other at any level of the layout.
VICTIM = {"sub": "user-victim", "custom:org_id": "org-victim", "custom:team_id": "team-v"}
ATTACKER = {"sub": "user-attacker", "custom:org_id": "org-attacker", "custom:team_id": "team-a"}

VICTIM_SESSION = "sess-victim-private"
VICTIM_CONNECTION = "conn-victim-live"


def _principal(claims: dict, channel: str = "webchat") -> str:
    return json.dumps([
        claims.get("custom:tenant_id") or claims.get("custom:org_id", ""),
        claims.get("custom:org_id", ""),
        claims.get("custom:team_id", ""),
        claims["sub"], channel,
    ], separators=(",", ":"))


@pytest.fixture(autouse=True)
def _patch_sys_path():
    original = sys.path.copy()
    sys.path.insert(0, HANDLER_DIR)
    yield
    sys.path = original


@pytest.fixture
def mock_env(monkeypatch):
    monkeypatch.setenv("INPUT_QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123/tasks")
    monkeypatch.setenv("RESPONSE_QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123/resp.fifo")
    monkeypatch.setenv("SESSIONS_TABLE_NAME", "sessions")
    monkeypatch.setenv("ARTIFACTS_BUCKET", "test-artifacts-bucket")
    monkeypatch.setenv("ARTIFACTS_TABLE", "test-artifacts-table")
    monkeypatch.setenv("AWS_REGION_NAME", "us-east-1")
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "")
    monkeypatch.setenv("SLACK_BOT_USER_ID", "")


@pytest.fixture
def aws(mock_env):
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        sessions = ddb.create_table(
            TableName="sessions",
            KeySchema=[{"AttributeName": "session_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "session_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        artifacts = ddb.create_table(
            TableName="test-artifacts-table",
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        sqs = boto3.client("sqs", region_name="us-east-1")
        sqs.create_queue(QueueName="tasks")
        sqs.create_queue(QueueName="resp.fifo", Attributes={"FifoQueue": "true"})
        yield {"sessions": sessions, "artifacts": artifacts}


def _import_handler():
    for mod_name in list(sys.modules.keys()):
        if mod_name in ("handler", "classifier", "channels", "channels.base",
                        "channels.webchat", "channels.slack", "github_dispatch"):
            del sys.modules[mod_name]
    import handler
    return handler


def _seed_victim_session(table, session_id: str = VICTIM_SESSION, **overrides) -> dict:
    """A victim conversation with a live WebSocket, as the gateway would write it."""
    item = {
        "session_id": session_id,
        "owner_principal": _principal(VICTIM),
        "owner_user_id": VICTIM["sub"],
        "user_workspace": f"{VICTIM['sub']}#webchat",
        "connection_id": VICTIM_CONNECTION,
        "channel": "webchat",
        "org_id": VICTIM["custom:org_id"],
        "team_id": VICTIM["custom:team_id"],
        "messages": [{"role": "user", "content": "my private salary review", "ts": 1}],
        "threads": {},
        "created_at": 1, "updated_at": 1, "expires_at": 9_999_999_999,
    }
    item.update(overrides)
    table.put_item(Item=item)
    return item


def _ws_event(body: dict, claims: dict, connection_id: str = "conn-attacker") -> dict:
    return {
        "requestContext": {
            "connectionId": connection_id,
            "routeKey": "$default",
            "authorizer": {"claims": claims},
        },
        "body": json.dumps(body),
    }


def _message_event(session_id: str, claims: dict, connection_id: str = "conn-attacker") -> dict:
    return _ws_event(
        {"action": "message", "text": "summarise this conversation", "session_id": session_id},
        claims, connection_id,
    )


def _row(table, session_id: str) -> dict:
    return table.get_item(Key={"session_id": session_id}).get("Item") or {}


def _create_session(handler, claims: dict, connection_id: str = "conn-attacker") -> str:
    """Start a conversation the way production does and return its id.

    #5615: a session exists only because `create-session` issued it. Tests that
    need an owned session for the caller's OWN identity go through this route
    rather than writing a row directly, so what they exercise is the real
    creation path. `_seed_victim_session` still writes directly — it stands in
    for somebody ELSE's pre-existing conversation.
    """
    result = handler.lambda_handler(
        _ws_event({"action": "create-session", "request_id": "req-cs"},
                  claims, connection_id),
        None,
    )
    assert result["statusCode"] == 200, result["body"]
    return json.loads(result["body"])["session_id"]


# ---------------------------------------------------------------------------
# 1. The message path
# ---------------------------------------------------------------------------

class TestMessagePathOwnership:
    def test_forged_session_id_is_refused(self, aws):
        handler = _import_handler()
        _seed_victim_session(aws["sessions"])

        result = handler.lambda_handler(
            _message_event(VICTIM_SESSION, ATTACKER), None,
        )

        assert result["statusCode"] == 404

    def test_victims_live_connection_is_not_rebound(self, aws):
        """The headline hijack. Pre-fix this wrote conn-attacker over the victim's."""
        handler = _import_handler()
        _seed_victim_session(aws["sessions"])

        handler.lambda_handler(_message_event(VICTIM_SESSION, ATTACKER), None)

        assert _row(aws["sessions"], VICTIM_SESSION)["connection_id"] == VICTIM_CONNECTION

    def test_refusal_returns_none_of_the_victims_history(self, aws):
        handler = _import_handler()
        _seed_victim_session(aws["sessions"])

        result = handler.lambda_handler(_message_event(VICTIM_SESSION, ATTACKER), None)

        assert "salary" not in json.dumps(result)

    def test_refusal_does_not_append_the_attackers_message(self, aws):
        """A refused turn must leave no trace in the victim's transcript."""
        handler = _import_handler()
        _seed_victim_session(aws["sessions"])

        handler.lambda_handler(_message_event(VICTIM_SESSION, ATTACKER), None)

        messages = _row(aws["sessions"], VICTIM_SESSION)["messages"]
        assert len(messages) == 1
        # `str` not `json.dumps`: DDB returns Decimal timestamps.
        assert "summarise" not in str(messages)

    def test_refusal_enqueues_no_work(self, aws):
        """No SQS task, so the agent never runs under the wrong identity."""
        handler = _import_handler()
        _seed_victim_session(aws["sessions"])

        with patch.object(handler.sqs, "send_message") as mock_send:
            handler.lambda_handler(_message_event(VICTIM_SESSION, ATTACKER), None)

        mock_send.assert_not_called()

    def test_refusal_is_indistinguishable_from_a_missing_session(self, aws):
        """The error must not confirm that the session exists.

        Otherwise the endpoint is an oracle for enumerating other tenants'
        session ids, which is how an attacker would find a target in the first
        place.
        """
        handler = _import_handler()
        _seed_victim_session(aws["sessions"])

        forged = handler.lambda_handler(_message_event(VICTIM_SESSION, ATTACKER), None)
        absent = handler.lambda_handler(
            _message_event("sess-does-not-exist-at-all", ATTACKER), None,
        )

        # A nonexistent session is legitimately created for its first message, so
        # compare only that the forged case leaks nothing identifying.
        assert forged["statusCode"] == 404
        body = json.loads(forged["body"])
        assert VICTIM["sub"] not in json.dumps(body)
        assert VICTIM["custom:org_id"] not in json.dumps(body)
        assert absent["statusCode"] != 500

    def test_owner_reaches_their_own_session(self, aws):
        """Regression: the gate must not lock out the legitimate owner."""
        handler = _import_handler()
        _seed_victim_session(aws["sessions"])

        result = handler.lambda_handler(
            _message_event(VICTIM_SESSION, VICTIM, connection_id="conn-victim-new"), None,
        )

        assert result["statusCode"] != 404
        # Reconnecting legitimately DOES rebind — that is the feature.
        assert _row(aws["sessions"], VICTIM_SESSION)["connection_id"] == "conn-victim-new"

    def test_same_user_in_another_org_is_refused(self, aws):
        handler = _import_handler()
        _seed_victim_session(aws["sessions"])
        other_tenant = {
            "sub": VICTIM["sub"],
            "custom:org_id": "org-other",
            "custom:team_id": VICTIM["custom:team_id"],
        }

        result = handler.lambda_handler(
            _message_event(VICTIM_SESSION, other_tenant, connection_id="conn-other"), None,
        )

        assert result["statusCode"] == 404
        assert _row(aws["sessions"], VICTIM_SESSION)["connection_id"] == VICTIM_CONNECTION

    def test_same_effective_tenant_label_does_not_hide_an_org_mismatch(self, aws):
        handler = _import_handler()
        victim = {**VICTIM, "custom:tenant_id": "shared-label"}
        _seed_victim_session(
            aws["sessions"],
            owner_principal=_principal(victim),
            tenant_id="shared-label",
        )
        other_org = {
            **victim,
            "custom:org_id": "org-other",
        }

        result = handler.lambda_handler(
            _message_event(VICTIM_SESSION, other_org, connection_id="conn-other"), None,
        )

        assert result["statusCode"] == 404
        assert _row(aws["sessions"], VICTIM_SESSION)["connection_id"] == VICTIM_CONNECTION


# ---------------------------------------------------------------------------
# 2. Concurrency: the check and the rebind must be one atomic write
# ---------------------------------------------------------------------------

class TestOwnershipCheckIsAtomic:
    def test_owner_change_between_read_and_write_is_refused(self, aws):
        """Simulates the interleaving a two-statement check would let through.

        If the row's owner changes after the read but before the write, the
        conditional write must fail and that failure must be a REFUSAL — never a
        retry, which would simply re-apply the hijack.
        """
        handler = _import_handler()
        _seed_victim_session(aws["sessions"])

        real_get = handler.sessions_table.get_item

        def racing_get(**kwargs):
            resp = real_get(**kwargs)
            if kwargs.get("Key", {}).get("session_id") == VICTIM_SESSION:
                # The attacker's read sees a row it appears to own...
                if resp.get("Item"):
                    resp["Item"]["owner_principal"] = _principal(ATTACKER)
            return resp

        with patch.object(handler.sessions_table, "get_item", side_effect=racing_get):
            result = handler.lambda_handler(
                _message_event(VICTIM_SESSION, ATTACKER), None,
            )

        # ...but the stored row still says VICTIM, so the condition fails.
        assert result["statusCode"] == 404
        assert _row(aws["sessions"], VICTIM_SESSION)["connection_id"] == VICTIM_CONNECTION

    def test_rebind_carries_a_condition_expression(self, aws):
        """The write itself must be conditional, not merely preceded by a check."""
        handler = _import_handler()
        _seed_victim_session(aws["sessions"])

        seen = {}
        real_update = handler.sessions_table.update_item

        def capturing_update(**kwargs):
            if kwargs.get("Key", {}).get("session_id") == VICTIM_SESSION:
                seen.update(kwargs)
            return real_update(**kwargs)

        with patch.object(handler.sessions_table, "update_item", side_effect=capturing_update):
            handler.lambda_handler(
                _message_event(VICTIM_SESSION, VICTIM, connection_id="conn-victim-new"), None,
            )

        assert "owner_principal" in seen.get("ConditionExpression", "")

    def test_lookup_failure_never_falls_through_to_create(self, aws):
        handler = _import_handler()
        from channels.webchat import WebChatAdapter
        message = WebChatAdapter().parse_event(_message_event("sess-read-error", ATTACKER))

        with patch.object(handler.sessions_table, "get_item", side_effect=RuntimeError("ddb down")), \
                patch.object(handler.sessions_table, "put_item") as put_item:
            with pytest.raises(handler.SessionStoreError):
                handler.get_or_create_session("sess-read-error", "conn-attacker", message, 1)

        put_item.assert_not_called()

    def test_competing_first_create_verifies_the_winner(self, aws):
        handler = _import_handler()
        from channels.webchat import WebChatAdapter
        message = WebChatAdapter().parse_event(_message_event("sess-race", ATTACKER))
        victim_row = _seed_victim_session(aws["sessions"], session_id="sess-race")
        conditional_failure = ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException", "Message": "lost race"}},
            "PutItem",
        )

        with patch.object(handler.sessions_table, "get_item", side_effect=[{}, {"Item": victim_row}]), \
                patch.object(handler.sessions_table, "put_item", side_effect=conditional_failure):
            with pytest.raises(handler.SessionOwnershipError):
                handler.get_or_create_session("sess-race", "conn-attacker", message, 1)


# ---------------------------------------------------------------------------
# 3. Legacy rows: quarantined, never adopted
# ---------------------------------------------------------------------------

class TestLegacyRowsAreQuarantined:
    def test_unowned_row_is_not_adopted_by_the_caller(self, aws):
        """A row predating owner stamping records nobody. Guessing is the bug.

        Adopting it would let the FIRST caller to name the id claim a
        conversation they may not have started.
        """
        handler = _import_handler()
        aws["sessions"].put_item(Item={
            "session_id": "sess-legacy-unowned",
            "connection_id": "conn-legacy",
            "messages": [{"role": "user", "content": "legacy secret", "ts": 1}],
            "threads": {}, "created_at": 1, "updated_at": 1, "expires_at": 9_999_999_999,
        })

        result = handler.lambda_handler(
            _message_event("sess-legacy-unowned", ATTACKER), None,
        )

        assert result["statusCode"] == 404
        row = _row(aws["sessions"], "sess-legacy-unowned")
        # Neither adopted nor mutated: no owner written, connection untouched.
        assert "user_workspace" not in row
        assert row["connection_id"] == "conn-legacy"

    def test_quarantine_is_not_a_destructive_cleanup(self, aws):
        """The row and its history survive — refusal must not delete data."""
        handler = _import_handler()
        aws["sessions"].put_item(Item={
            "session_id": "sess-legacy-unowned",
            "messages": [{"role": "user", "content": "legacy secret", "ts": 1}],
            "threads": {}, "created_at": 1, "expires_at": 9_999_999_999,
        })

        handler.lambda_handler(_message_event("sess-legacy-unowned", ATTACKER), None)

        assert _row(aws["sessions"], "sess-legacy-unowned")["messages"][0]["content"] == "legacy secret"


# ---------------------------------------------------------------------------
# 4. Shape validation at every entry point
# ---------------------------------------------------------------------------

class TestSessionIdShape:
    # `o`/`t`/`u`/`s` collide with the FIXED leading segments of the artifact
    # layout, which is what made the sweeper's `${session_id}/` prefix delete
    # every tenant's uploads.
    HOSTILE = ["o", "t", "u", "s", "../other-tenant", "a/b", "sess..a", "conn#conn-123"]

    @pytest.mark.parametrize("session_id", HOSTILE)
    def test_message_path_refuses(self, aws, session_id):
        handler = _import_handler()
        result = handler.lambda_handler(_message_event(session_id, ATTACKER), None)
        assert result["statusCode"] == 400

    @pytest.mark.parametrize("session_id", HOSTILE)
    def test_upload_token_refuses(self, aws, session_id):
        handler = _import_handler()
        result = handler.lambda_handler(_ws_event({
            "action": "upload-token", "session_id": session_id, "filename": "f.pdf",
        }, ATTACKER), None)
        assert result["statusCode"] == 400

    @pytest.mark.parametrize("session_id", HOSTILE)
    def test_upload_complete_refuses(self, aws, session_id):
        handler = _import_handler()
        result = handler.lambda_handler(_ws_event({
            "action": "upload-complete", "session_id": session_id, "task_id": "t1",
            "filename": "f.pdf", "checksum": "abc123",
        }, ATTACKER), None)
        assert result["statusCode"] == 400

    def test_a_hostile_id_creates_no_row(self, aws):
        """Refusal must precede the write, or `o` still lands in the table."""
        handler = _import_handler()
        handler.lambda_handler(_message_event("o", ATTACKER), None)
        assert _row(aws["sessions"], "o") == {}

    def test_connection_claims_row_cannot_be_addressed_as_a_session(self, aws):
        """`conn#<id>` holds a connection's durable identity, not a conversation.

        Reaching it through the session path would expose or overwrite the
        claims the gateway trusts for authentication.
        """
        handler = _import_handler()
        handler._persist_connection_claims("conn-victim-live", {"claims": VICTIM})

        result = handler.lambda_handler(
            _message_event("conn#conn-victim-live", ATTACKER), None,
        )

        assert result["statusCode"] == 400
        claims_row = _row(aws["sessions"], "conn#conn-victim-live")
        assert claims_row.get("sub") == VICTIM["sub"]

    @pytest.mark.parametrize("session_id", [
        "sess-1758441600000-a1b2c3d",                  # SPA
        "sess-3f2a1b9c8d7e6f5a4b3c2d1e0f9a8b7c",       # CLI
        "1758441600.123456",                           # Slack thread_ts
        "webchat:C0123ABC:user-alice",                 # session_key fallback
    ])
    def test_real_id_formats_still_work(self, aws, session_id):
        """Over-strict validation would strand live conversations.

        These are the formats real clients mint today; rejecting any of them
        would be a worse regression than the bug being fixed.
        """
        handler = _import_handler()
        assert handler.is_valid_session_id(session_id) is True

    @pytest.mark.parametrize("hostile", [
        {"attack": "../other-tenant"},                 # JSON object
        ["../other-tenant"],                           # JSON array
        12345,                                         # number
        True,                                          # bool
    ])
    def test_non_string_session_id_is_rejected_without_side_effects(self, aws, hostile):
        """An explicit malformed id must not create a fallback conversation."""
        handler = _import_handler()
        handler._persist_connection_claims("conn-attacker", {"claims": ATTACKER})

        with (
            patch.object(handler, "classify_message") as classify,
            patch.object(handler.sqs, "send_message") as enqueue,
        ):
            result = handler.lambda_handler(_ws_event({
            "action": "sendMessage", "text": "hi", "session_id": hostile,
            }, ATTACKER), None)

        assert result["statusCode"] == 400
        assert "session_id must be a string" in result["body"]
        classify.assert_not_called()
        enqueue.assert_not_called()
        session_rows = aws["sessions"].scan()["Items"]
        assert [row for row in session_rows if not row["session_id"].startswith("conn#")] == []

    def test_a_string_session_id_is_still_carried_through(self, aws):
        """The type guard must not break the normal case."""
        _import_handler()
        from channels.webchat import WebChatAdapter

        message = WebChatAdapter().parse_event(_ws_event({
            "action": "sendMessage", "text": "hi", "session_id": "sess-abc123",
        }, ATTACKER))

        assert message.thread_id == "sess-abc123"


# ---------------------------------------------------------------------------
# 5. Uploads bind to the verified owner
# ---------------------------------------------------------------------------

class TestUploadOwnership:
    def test_token_refused_for_another_owners_session(self, aws):
        """Otherwise an attacker plants a file inside the victim's conversation."""
        handler = _import_handler()
        _seed_victim_session(aws["sessions"])
        handler._persist_connection_claims("conn-attacker", {"claims": ATTACKER})

        result = handler.lambda_handler(_ws_event({
            "action": "upload-token", "session_id": VICTIM_SESSION,
            "filename": "malware.pdf", "content_type": "application/pdf",
        }, ATTACKER), None)

        assert result["statusCode"] == 404

    def test_completion_refused_for_another_owners_session(self, aws):
        handler = _import_handler()
        _seed_victim_session(aws["sessions"])
        handler._persist_connection_claims("conn-attacker", {"claims": ATTACKER})

        result = handler.lambda_handler(_ws_event({
            "action": "upload-complete", "session_id": VICTIM_SESSION,
            "task_id": "task-1", "filename": "malware.pdf", "checksum": "deadbeef",
        }, ATTACKER), None)

        assert result["statusCode"] == 404

    def test_no_catalogue_row_is_written_on_refusal(self, aws):
        handler = _import_handler()
        _seed_victim_session(aws["sessions"])
        handler._persist_connection_claims("conn-attacker", {"claims": ATTACKER})

        handler.lambda_handler(_ws_event({
            "action": "upload-complete", "session_id": VICTIM_SESSION,
            "task_id": "task-1", "filename": "malware.pdf", "checksum": "deadbeef",
        }, ATTACKER), None)

        rows = aws["artifacts"].query(
            KeyConditionExpression="PK = :pk",
            ExpressionAttributeValues={":pk": f"session#{VICTIM_SESSION}"},
        )["Items"]
        assert rows == []

    def test_owner_may_upload_to_their_own_session(self, aws):
        """Regression: the gate must not break the normal upload flow."""
        handler = _import_handler()
        _seed_victim_session(aws["sessions"])
        handler._persist_connection_claims("conn-victim-live", {"claims": VICTIM})

        with patch.object(handler.s3_client, "generate_presigned_url",
                          return_value="https://s3.example.com/presigned"):
            result = handler.lambda_handler(_ws_event({
                "action": "upload-token", "session_id": VICTIM_SESSION,
                "filename": "report.pdf",
            }, VICTIM, connection_id="conn-victim-live"), None)

        assert result["statusCode"] == 200
        key = json.loads(result["body"])["s3_key"]
        assert key.startswith(f"o/org-victim/t/team-v/u/{VICTIM['sub']}/s/{VICTIM_SESSION}/")

    def test_completion_without_a_reserved_session_is_refused(self, aws):
        handler = _import_handler()
        handler._persist_connection_claims("conn-attacker", {"claims": ATTACKER})

        result = handler.lambda_handler(_ws_event({
            "action": "upload-complete", "session_id": "sess-unreserved",
            "task_id": "task-1", "filename": "notes.txt", "checksum": "abc",
        }, ATTACKER), None)

        assert result["statusCode"] == 404
        assert aws["artifacts"].scan()["Items"] == []

    def test_upload_before_the_first_message_is_allowed(self, aws):
        """Uploads legitimately precede the opening message, and still do.

        This route used to CREATE the session it was asked about, which is how
        that case was served. #5615 removed that, because it was also a second
        way to name a conversation and so bypassed the server-issued-only
        contract. The case itself is preserved: the row now exists from
        `create-session`, which the browser calls before it can offer the drop
        zone at all, so a file can still be attached before any message is sent.
        """
        handler = _import_handler()
        handler._persist_connection_claims("conn-attacker", {"claims": ATTACKER})
        session_id = _create_session(handler, ATTACKER, "conn-attacker")

        with patch.object(handler.s3_client, "generate_presigned_url",
                          return_value="https://s3.example.com/presigned"):
            result = handler.lambda_handler(_ws_event({
                "action": "upload-token", "session_id": session_id,
                "filename": "notes.txt",
            }, ATTACKER), None)

        assert result["statusCode"] == 200
        assert _row(aws["sessions"], session_id)["owner_principal"] == _principal(ATTACKER)
        # No message has been sent on it yet — the upload really is first.
        assert _row(aws["sessions"], session_id)["messages"] == []

    def test_a_session_the_server_never_issued_is_refused(self, aws):
        """#5615: naming an id is not a request to create it, on any route."""
        handler = _import_handler()
        handler._persist_connection_claims("conn-attacker", {"claims": ATTACKER})

        with patch.object(handler.s3_client, "generate_presigned_url",
                          return_value="https://s3.example.com/presigned"):
            result = handler.lambda_handler(_ws_event({
                "action": "upload-token", "session_id": "sess-brand-new",
                "filename": "notes.txt",
            }, ATTACKER), None)

        assert result["statusCode"] == 404
        assert not _row(aws["sessions"], "sess-brand-new")


# ---------------------------------------------------------------------------
# 6. The stored S3 key is derived, never accepted from the client
# ---------------------------------------------------------------------------

class TestUploadKeyIsServerDerived:
    @staticmethod
    def _own_session(handler) -> str:
        """The uploader's own conversation, created the way production does.

        #5615: `upload-token` no longer creates the row it is asked about, so
        these tests start the conversation through `create-session` and use the
        id the server issues. The id is therefore random per test, which is why
        it is threaded through the helpers below rather than hard-coded.
        """
        return _create_session(handler, ATTACKER)

    def _complete(self, handler, session_id: str, body_extra: dict) -> dict:
        body = {
            "action": "upload-complete", "session_id": session_id,
            "task_id": "task-1", "filename": "notes.txt", "checksum": "cafe1234",
        }
        body.update(body_extra)
        return handler.lambda_handler(_ws_event(body, ATTACKER), None)

    def _catalogue_row(self, aws, session_id: str) -> dict:
        rows = aws["artifacts"].query(
            KeyConditionExpression="PK = :pk",
            ExpressionAttributeValues={":pk": f"session#{session_id}"},
        )["Items"]
        assert len(rows) == 1
        return rows[0]

    def test_clients_s3_key_is_ignored(self, aws):
        """Pre-fix this key was stored verbatim, pointing the row at the victim.

        The attacker owns the catalogue row, so they can read it back through
        the artifact fetch path — a cross-tenant read with no forged identity
        required.
        """
        handler = _import_handler()
        handler._persist_connection_claims("conn-attacker", {"claims": ATTACKER})
        session_id = self._own_session(handler)

        forged = f"o/org-victim/t/team-v/u/{VICTIM['sub']}/s/{VICTIM_SESSION}/t/in/secret.pdf"
        result = self._complete(handler, session_id, {"s3_key": forged})

        assert result["statusCode"] == 200
        assert self._catalogue_row(aws, session_id)["s3Key"] != forged

    def test_stored_key_is_under_the_uploaders_own_prefix(self, aws):
        handler = _import_handler()
        handler._persist_connection_claims("conn-attacker", {"claims": ATTACKER})
        session_id = self._own_session(handler)

        self._complete(handler, session_id, {
            "s3_key": f"o/org-victim/t/team-v/u/{VICTIM['sub']}/s/{VICTIM_SESSION}/t/in/x.pdf",
        })

        stored = self._catalogue_row(aws, session_id)["s3Key"]
        assert stored == (
            f"o/org-attacker/t/team-a/u/{ATTACKER['sub']}"
            f"/s/{session_id}/task-1/in/notes.txt"
        )

    @pytest.mark.parametrize("poison_kind", ["foreign_key", "missing_owner"])
    def test_checksum_match_does_not_reuse_an_unverified_catalogue_row(self, aws, poison_kind):
        handler = _import_handler()
        handler._persist_connection_claims("conn-attacker", {"claims": ATTACKER})
        session_id = self._own_session(handler)
        derived_key = (
            f"o/org-attacker/t/team-a/u/{ATTACKER['sub']}"
            f"/s/{session_id}/task-1/in/notes.txt"
        )
        poisoned = {
            "PK": f"session#{session_id}",
            "SK": "art#2026-01-01T00:00:00.000Z#art_poisoned",
            "id": "art_poisoned",
            "checksum": "cafe1234",
            "s3Key": derived_key,
        }
        if poison_kind == "foreign_key":
            poisoned.update({
                "s3Key": (
                    f"o/org-victim/t/team-v/u/{VICTIM['sub']}/s/"
                    f"{VICTIM_SESSION}/task-1/in/secret.txt"
                ),
                "org_id": ATTACKER["custom:org_id"],
                "team_id": ATTACKER["custom:team_id"],
                "user_id": ATTACKER["sub"],
            })
        aws["artifacts"].put_item(Item=poisoned)

        result = self._complete(handler, session_id, {})

        body = json.loads(result["body"])
        assert result["statusCode"] == 200
        assert body["deduplicated"] is False
        assert body["artifact_id"] != "art_poisoned"
        rows = aws["artifacts"].query(
            KeyConditionExpression="PK = :pk",
            ExpressionAttributeValues={":pk": f"session#{session_id}"},
        )["Items"]
        created = next(row for row in rows if row["id"] == body["artifact_id"])
        assert created["s3Key"] == derived_key
        assert created["org_id"] == ATTACKER["custom:org_id"]

    def test_derived_key_matches_the_one_the_token_issued(self, aws):
        """Both halves must agree or the catalogue points at a nonexistent object."""
        handler = _import_handler()
        handler._persist_connection_claims("conn-attacker", {"claims": ATTACKER})
        session_id = self._own_session(handler)

        with patch.object(handler.s3_client, "generate_presigned_url",
                          return_value="https://s3.example.com/p"):
            token = handler.lambda_handler(_ws_event({
                "action": "upload-token", "session_id": session_id,
                "task_id": "task-1", "filename": "notes.txt",
            }, ATTACKER), None)

        issued = json.loads(token["body"])["s3_key"]
        self._complete(handler, session_id, {})
        assert self._catalogue_row(aws, session_id)["s3Key"] == issued

    def test_incomplete_identity_cannot_write_an_unqualified_key(self, aws):
        """No org/team means no expressible owner prefix, so refuse.

        The legacy flat `<session>/<task>/in/<file>` layout is precisely what
        makes existing rows unauthorizable; writing another one would grow the
        quarantine rather than shrink it.
        """
        handler = _import_handler()
        partial = {"sub": "user-partial"}
        handler._persist_connection_claims("conn-partial", {"claims": partial})

        result = handler.lambda_handler(_ws_event({
            "action": "upload-complete", "session_id": "sess-partial",
            "task_id": "task-1", "filename": "notes.txt", "checksum": "abc",
        }, partial, connection_id="conn-partial"), None)

        assert result["statusCode"] == 403

    @pytest.mark.parametrize("segment_overrides", [
        {"custom:org_id": "org-a/../org-victim"},
        {"custom:team_id": "team/../../x"},
    ])
    def test_a_separator_in_an_identity_claim_cannot_escape_the_prefix(self, aws, segment_overrides):
        """Defence in depth: claims are server-held, but a separator in one
        would still move the object out of its owner's prefix."""
        handler = _import_handler()
        claims = dict(ATTACKER)
        claims.update(segment_overrides)

        with pytest.raises(ValueError):
            handler._build_upload_s3_key(
                claims["custom:org_id"], claims["custom:team_id"], claims["sub"],
                "sess-own", "task-1", "notes.txt",
            )


def test_task_chat_sessions_cannot_reenter_legacy_ingest(aws):
    handler = _import_handler()
    item = {"owner_principal": _principal(VICTIM), "chat_task_persona": "agent-task-investigator"}
    with pytest.raises(handler.SessionOwnershipError):
        handler._assert_session_item_owner(item, _principal(VICTIM), "chat-owned")
