"""
Server-issued chat session identifiers (#5615 / S16).

WHAT WAS WRONG. A browser opening a conversation invented its own identifier —
`sess-${Date.now()}-${Math.random().toString(36).slice(2,9)}` — and the server
adopted whatever it was told. Two consequences, and it matters that they are
stated at their real size:

  1. The identifier was mostly a clock reading, so it was predictable.
  2. Naming an identifier the server had never seen WAS the creation request.

Together those let an attacker create the identifier a victim's browser was
about to choose. The victim's own new conversation was then refused as somebody
else's — a lockout, i.e. availability, not disclosure.

WHAT WAS ALREADY FIXED AND MUST STAY FIXED. #5660/A07 (merged as PR #5742) added
the ownership record and the conditional writes. Guessing a victim's EXISTING
identifier already yields nothing: no history, no reply redirection, no upload.
`test_session_ownership.py` owns that proof and is unchanged. Randomness here
SUPPLEMENTS those checks; it never replaces them, and
`TestRandomnessDoesNotReplaceOwnership` below pins that it has not quietly
become the only thing standing between two tenants.

WHAT THESE TESTS COVER — the six scenarios the assignment requires:

  1. fresh creation                   TestFreshCreation
  2. collision / retry / lost reply   TestMintingIsRobust
  3. owner reconnect + history        TestOwnerReconnect
  4. unknown / foreign identifiers    TestAnUnknownIdIsNotACreationRequest,
                                      TestRandomnessDoesNotReplaceOwnership
  5. response-owner binding           TestResponseRoutingStaysWithTheOwner
  6. attachments on the issued id     TestAttachmentsUseTheAcknowledgedId

Every test drives `lambda_handler` through the real WebSocket event shape, so
what is exercised is the production boundary rather than a helper in isolation.
"""

from __future__ import annotations

import json
import os
import sys
import time
from unittest.mock import patch

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

HANDLER_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "gateway", "lambdas", "ingest"
)

# Two tenants in different orgs AND teams, so a value derived for one cannot be
# mistaken for the other at any level of the S3 key layout.
OWNER = {
    "sub": "user-owner", "custom:tenant_id": "tenant-owner",
    "custom:org_id": "org-owner", "custom:team_id": "team-o",
}
STRANGER = {
    "sub": "user-stranger", "custom:tenant_id": "tenant-stranger",
    "custom:org_id": "org-stranger", "custom:team_id": "team-s",
}

OWNER_CONN = "conn-owner"
STRANGER_CONN = "conn-stranger"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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
    monkeypatch.setenv("WEBHOOK_EVENTS_TABLE", "webhook-events")


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
        ddb.create_table(
            TableName="webhook-events",
            KeySchema=[
                {"AttributeName": "event_id", "KeyType": "HASH"},
                {"AttributeName": "arrived_at", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "event_id", "AttributeType": "S"},
                {"AttributeName": "arrived_at", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        sqs = boto3.client("sqs", region_name="us-east-1")
        sqs.create_queue(QueueName="tasks")
        sqs.create_queue(QueueName="resp.fifo", Attributes={"FifoQueue": "true"})
        yield {"sessions": sessions, "artifacts": artifacts, "sqs": sqs}


def _import_handler():
    for mod_name in list(sys.modules.keys()):
        if mod_name in ("handler", "classifier", "channels", "channels.base",
                        "channels.webchat", "channels.slack",
                        "channels.gateway_api", "github_dispatch",
                        "invocation_logger", "user_resolver"):
            del sys.modules[mod_name]
    import handler
    return handler


# ---------------------------------------------------------------------------
# Event helpers — the real API Gateway WebSocket shapes
# ---------------------------------------------------------------------------


def _ws_event(body: dict, claims: dict | None, connection_id: str) -> dict:
    event: dict = {
        "requestContext": {
            "connectionId": connection_id,
            "routeKey": "$default",
        },
        "body": json.dumps(body),
    }
    if claims is not None:
        event["requestContext"]["authorizer"] = {"claims": claims}
    return event


def _create_session(handler, claims: dict, connection_id: str,
                    request_id: str = "req-1") -> dict:
    """Drive the `create-session` action and return the parsed reply."""
    result = handler.lambda_handler(
        _ws_event(
            {"action": "create-session", "request_id": request_id},
            claims, connection_id,
        ),
        None,
    )
    return {"statusCode": result["statusCode"], **json.loads(result["body"])}


def _send_message(handler, session_id: str, claims: dict | None,
                  connection_id: str, text: str = "hello") -> dict:
    result = handler.lambda_handler(
        _ws_event(
            {"action": "message", "text": text, "session_id": session_id},
            claims, connection_id,
        ),
        None,
    )
    return {"statusCode": result["statusCode"], **json.loads(result["body"])}


def _row(table, session_id: str) -> dict:
    return table.get_item(Key={"session_id": session_id}).get("Item") or {}


def _direct_response_classifier(handler):
    """Force the cheap classifier path so these tests assert session behaviour.

    A real Bedrock call is neither available nor relevant here: every assertion
    below is about which row was touched and who owns it.
    """
    from classifier import ClassificationResult

    def classify(*args, **kwargs):
        return ClassificationResult(
            path="direct_response", persona="developer",
            response="ack", thread_action="none", reasoning="test",
        )

    handler.classify_message = classify


# ---------------------------------------------------------------------------
# 1. Fresh creation
# ---------------------------------------------------------------------------


class TestFreshCreation:
    """The new contract: the server names the conversation, not the client."""

    def test_the_server_issues_an_id_the_client_did_not_ask_for(self, aws):
        handler = _import_handler()

        reply = _create_session(handler, OWNER, OWNER_CONN)

        assert reply["statusCode"] == 200
        assert reply["session_id"].startswith("sess-")
        assert _row(aws["sessions"], reply["session_id"])

    def test_the_issued_id_carries_full_entropy(self, aws):
        """16 bytes from the OS CSPRNG, as hex.

        This is the property the old `Date.now()` id lacked. Asserted on the
        SHAPE rather than by sampling a distribution, because a regression here
        would be a change of source (clock, counter, uuid1) and would show up as
        a change of length or alphabet.
        """
        handler = _import_handler()

        session_id = _create_session(handler, OWNER, OWNER_CONN)["session_id"]

        suffix = session_id.removeprefix("sess-")
        assert len(suffix) == 32
        assert all(c in "0123456789abcdef" for c in suffix)

    def test_ids_do_not_repeat_and_do_not_encode_the_clock(self, aws):
        handler = _import_handler()

        ids = [
            _create_session(handler, OWNER, OWNER_CONN, request_id=f"r{i}")["session_id"]
            for i in range(25)
        ]

        assert len(set(ids)) == 25, "a repeated id means the source is not random"
        # The defect it replaces: `sess-<ms-since-epoch>-...`. Ids minted inside
        # the same second must share no leading run, or the clock is still in there.
        assert len({i[:12] for i in ids}) > 1

    def test_the_client_cannot_influence_the_id_it_is_given(self, aws):
        """The body is ignored. A client that asks for a name does not get it."""
        handler = _import_handler()

        result = handler.lambda_handler(
            _ws_event(
                {"action": "create-session", "request_id": "req-1",
                 "session_id": "sess-i-want-this-one", "user_id": "somebody-else"},
                OWNER, OWNER_CONN,
            ),
            None,
        )

        issued = json.loads(result["body"])["session_id"]
        assert issued != "sess-i-want-this-one"
        assert not _row(aws["sessions"], "sess-i-want-this-one")

    def test_the_new_row_is_owned_from_the_instant_it_exists(self, aws):
        """No window in which the row exists unowned.

        An unowned row is exactly what `_assert_session_item_owner` quarantines,
        so a row created without its owner would be dead on arrival for its
        creator — and adoptable by whoever asked next if that check ever
        loosened.
        """
        handler = _import_handler()

        session_id = _create_session(handler, OWNER, OWNER_CONN)["session_id"]

        row = _row(aws["sessions"], session_id)
        assert row["owner_principal"] == json.dumps(
            [OWNER["custom:tenant_id"], OWNER["custom:org_id"],
             OWNER["custom:team_id"], OWNER["sub"], "webchat"],
            separators=(",", ":"),
        )
        assert row["owner_user_id"] == OWNER["sub"]
        assert row["tenant_id"] == OWNER["custom:tenant_id"]

    def test_the_owner_comes_from_verified_claims_not_from_the_body(self, aws):
        """The whole point: the id is bound to the identity $connect verified."""
        handler = _import_handler()

        result = handler.lambda_handler(
            _ws_event(
                {"action": "create-session", "request_id": "req-1",
                 "user_id": STRANGER["sub"], "tenant_id": STRANGER["custom:tenant_id"],
                 "org_id": STRANGER["custom:org_id"]},
                OWNER, OWNER_CONN,
            ),
            None,
        )

        row = _row(aws["sessions"], json.loads(result["body"])["session_id"])
        assert row["owner_user_id"] == OWNER["sub"]
        assert STRANGER["sub"] not in row["owner_principal"]
        assert STRANGER["custom:org_id"] not in row["owner_principal"]

    def test_creation_works_off_the_claims_persisted_at_connect(self, aws):
        """Production shape: a $default frame carries NO authorizer context.

        API Gateway runs the Cognito authorizer on $connect only. If creation
        depended on claims arriving with the message it would fail for every
        real browser while passing any test that injects them.
        """
        handler = _import_handler()
        handler.lambda_handler(
            {
                "requestContext": {
                    "routeKey": "$connect", "connectionId": OWNER_CONN,
                    "authorizer": {"claims": OWNER},
                },
            },
            None,
        )

        reply = _create_session(handler, None, OWNER_CONN)

        assert reply["statusCode"] == 200
        row = _row(aws["sessions"], reply["session_id"])
        assert row["owner_user_id"] == OWNER["sub"]

    def test_an_unauthenticated_caller_gets_no_id(self, aws):
        """A socket with no verified identity behind it cannot start anything.

        The refusal comes from the claims gate, before `create-session` runs:
        there are no persisted $connect claims for this connection, so there is
        no identity to own a conversation. What matters for this issue is the
        outcome — no identifier is issued and no row is written.
        """
        handler = _import_handler()

        reply = _create_session(handler, {}, "conn-anon")

        assert reply["statusCode"] == 401
        assert not reply.get("session_id")
        assert aws["sessions"].scan()["Count"] == 0

    def test_creation_refuses_a_claimless_call_on_its_own(self, aws):
        """Defense in depth, asserted directly because the route gate hides it.

        `handle_create_session` re-checks for claims instead of trusting that
        the caller reached it legitimately. The WebSocket route can no longer
        deliver a claimless event here, so this is called directly — a future
        caller (another route, an HTTP front door) must not be able to mint an
        unowned session by arriving without claims.
        """
        handler = _import_handler()

        result = handler.handle_create_session(
            {"requestContext": {"connectionId": "conn-direct"}},
            "conn-direct",
            {"action": "create-session", "request_id": "req-1"},
        )

        assert result["statusCode"] == 401
        assert "session_id" not in json.loads(result["body"])
        assert aws["sessions"].scan()["Count"] == 0

    def test_an_incomplete_owner_is_refused_rather_than_recorded(self, aws):
        """No tenant means no expressible owner, so there is nothing to own it.

        Recording the row anyway with a blank tenant would create precisely the
        unowned row the ownership check quarantines.
        """
        handler = _import_handler()

        reply = _create_session(handler, {"sub": "user-no-tenant"}, "conn-partial")

        assert reply["statusCode"] == 503
        assert "session_id" not in reply
        assert aws["sessions"].scan()["Count"] == 0

    def test_the_issued_id_is_immediately_usable_for_a_message(self, aws):
        """End to end: create, then send. This is the browser's actual sequence."""
        handler = _import_handler()
        _direct_response_classifier(handler)

        session_id = _create_session(handler, OWNER, OWNER_CONN)["session_id"]
        result = _send_message(handler, session_id, OWNER, OWNER_CONN, "hello there")

        assert result["statusCode"] == 200
        stored = _row(aws["sessions"], session_id)["messages"]
        assert any("hello there" in str(m) for m in stored)


# ---------------------------------------------------------------------------
# 2. Collision, retry and a lost reply
# ---------------------------------------------------------------------------


class TestMintingIsRobust:
    """Creation must fail closed, never by joining an existing conversation."""

    def test_a_real_collision_never_overwrites_the_existing_row(self, aws):
        """The conditional write itself, exercised against a row that is there.

        This is the test that distinguishes `ConditionExpression=
        attribute_not_exists(session_id)` from a plain `put_item`. A plain put
        would silently REPLACE whatever was at that id — on a collision with
        another tenant's conversation that is a destroyed conversation plus a
        cross-tenant handover, and no exception would ever be raised to notice
        it by.

        So the mint is steered onto an id that already exists, rather than
        injecting the exception a conditional write would have produced.
        """
        handler = _import_handler()
        # A conversation that already exists, owned by somebody else, with
        # history worth destroying.
        victim_id = "sess-aaaabbbbccccddddeeeeffff00001111"
        aws["sessions"].put_item(Item={
            "session_id": victim_id,
            "owner_principal": json.dumps(
                [STRANGER["custom:tenant_id"], STRANGER["custom:org_id"],
                 STRANGER["custom:team_id"], STRANGER["sub"], "webchat"],
                separators=(",", ":"),
            ),
            "owner_user_id": STRANGER["sub"],
            "user_workspace": f'{STRANGER["sub"]}#webchat',
            "tenant_id": STRANGER["custom:tenant_id"],
            "channel": "webchat",
            "messages": [{"role": "user", "content": "the stranger's history", "ts": 1}],
            "threads": {}, "created_at": 1, "updated_at": 1,
            "expires_at": 9_999_999_999,
        })

        # The first mint collides with that row; the second is genuinely new.
        minted = iter([victim_id, "sess-99998888777766665555444433332222"])
        with patch.object(handler, "_mint_session_id", side_effect=lambda: next(minted)):
            reply = _create_session(handler, OWNER, OWNER_CONN)

        assert reply["statusCode"] == 200
        assert reply["session_id"] != victim_id, "the colliding row must not be handed over"

        # The stranger's conversation is intact: same owner, same history.
        victim_row = _row(aws["sessions"], victim_id)
        assert victim_row["owner_user_id"] == STRANGER["sub"]
        assert "the stranger's history" in str(victim_row["messages"])

    def test_a_collision_retries_with_a_fresh_id_instead_of_adopting_the_row(self, aws):
        """The retry itself: a failed condition must produce a NEW id.

        Complements the test above — that one proves the condition is set, this
        one proves the handler responds to it by minting again rather than
        surfacing the collision as an error or returning the colliding id.
        """
        handler = _import_handler()
        real_put = handler.sessions_table.put_item
        squatted: dict = {}

        def put_once_conflicting(**kwargs):
            if not squatted:
                squatted["session_id"] = kwargs["Item"]["session_id"]
                raise ClientError(
                    {"Error": {"Code": "ConditionalCheckFailedException",
                               "Message": "exists"}},
                    "PutItem",
                )
            return real_put(**kwargs)

        with patch.object(handler.sessions_table, "put_item",
                          side_effect=put_once_conflicting):
            reply = _create_session(handler, OWNER, OWNER_CONN)

        assert reply["statusCode"] == 200
        assert reply["session_id"] != squatted["session_id"], (
            "a collision must mint a NEW id, never return the colliding one"
        )

    def test_exhausted_retries_refuse_rather_than_return_an_id(self, aws):
        """If every attempt collides, no id is issued and nothing is written."""
        handler = _import_handler()

        with patch.object(
            handler.sessions_table, "put_item",
            side_effect=ClientError(
                {"Error": {"Code": "ConditionalCheckFailedException", "Message": "x"}},
                "PutItem",
            ),
        ):
            reply = _create_session(handler, OWNER, OWNER_CONN)

        assert reply["statusCode"] == 503
        assert "session_id" not in reply
        assert aws["sessions"].scan()["Count"] == 0

    def test_a_store_failure_is_not_reported_as_a_created_session(self, aws):
        """A non-conditional error must not be mistaken for success.

        Returning an id for a row that was never written would give the client a
        conversation every later message is refused on.
        """
        handler = _import_handler()

        with patch.object(handler.sessions_table, "put_item",
                          side_effect=RuntimeError("dynamodb unavailable")):
            reply = _create_session(handler, OWNER, OWNER_CONN)

        assert reply["statusCode"] == 503
        assert "session_id" not in reply

    def test_the_reply_is_correlated_so_a_client_can_tell_replies_apart(self, aws):
        """API Gateway discards a WS integration's return body.

        The id only reaches the browser via post_to_connection, tagged with the
        client's `request_id`. Without that tag a client with two requests in
        flight could attribute the wrong id to the wrong conversation.
        """
        handler = _import_handler()
        posted: list[dict] = []

        class FakeApiGw:
            def post_to_connection(self, ConnectionId, Data):
                posted.append({"connection_id": ConnectionId, "data": json.loads(Data)})

        with patch.object(handler, "_get_apigw_client", return_value=FakeApiGw()):
            reply = _create_session(handler, OWNER, OWNER_CONN, request_id="req-abc")

        assert len(posted) == 1
        assert posted[0]["connection_id"] == OWNER_CONN
        assert posted[0]["data"]["request_id"] == "req-abc"
        assert posted[0]["data"]["session_id"] == reply["session_id"]

    def test_a_lost_reply_retried_gets_a_new_id_and_never_rebinds_the_first(self, aws):
        """The lost-response case, which is what a client retry actually is.

        The server cannot know its reply was dropped, so the retry is simply
        another create. What must hold is that the second id is new, the first
        row is untouched, and BOTH stay owned by the same person — the orphan is
        then reaped by the sessions table's TTL rather than being adoptable.
        """
        handler = _import_handler()

        first = _create_session(handler, OWNER, OWNER_CONN, request_id="req-1")
        # ... reply lost in flight; the browser times out and retries ...
        second = _create_session(handler, OWNER, OWNER_CONN, request_id="req-2")

        assert first["session_id"] != second["session_id"]
        orphan = _row(aws["sessions"], first["session_id"])
        assert orphan["messages"] == [], "the orphan must stay empty, not be reused"
        assert orphan["owner_user_id"] == OWNER["sub"]
        assert orphan["expires_at"] > int(time.time()), "the orphan must carry a TTL"

    def test_a_retry_by_a_different_person_cannot_reach_the_orphan(self, aws):
        """The orphan is owned, so a lost reply is not a free-floating id."""
        handler = _import_handler()
        _direct_response_classifier(handler)
        orphan_id = _create_session(handler, OWNER, OWNER_CONN)["session_id"]

        result = _send_message(handler, orphan_id, STRANGER, STRANGER_CONN)

        assert result["statusCode"] == 404
        assert _row(aws["sessions"], orphan_id)["messages"] == []


# ---------------------------------------------------------------------------
# 3. Owner reconnect and history
# ---------------------------------------------------------------------------


class TestOwnerReconnect:
    """The compatibility risk. An over-strict rule would strand real users."""

    def test_the_owner_returns_on_a_new_connection_and_keeps_their_history(self, aws):
        """A WebSocket id changes on every reconnect; the conversation must not.

        This is the regression that matters most: the browser keeps the session
        id in localStorage and reuses it across reloads, so if a reconnect were
        treated as an unknown id the user would lose every conversation they had.
        """
        handler = _import_handler()
        _direct_response_classifier(handler)
        session_id = _create_session(handler, OWNER, OWNER_CONN)["session_id"]
        _send_message(handler, session_id, OWNER, OWNER_CONN, "first question")

        # Reconnect: same person, brand-new connection id.
        result = _send_message(handler, session_id, OWNER, "conn-owner-reconnected",
                               text="second question")

        assert result["statusCode"] == 200
        row = _row(aws["sessions"], session_id)
        assert row["connection_id"] == "conn-owner-reconnected"
        transcript = str(row["messages"])
        assert "first question" in transcript, "history survived the reconnect"
        assert "second question" in transcript

    def test_a_session_created_before_this_change_still_works_for_its_owner(self, aws):
        """Existing owned conversations must keep working.

        Rows created by `get_or_create_session` under the OLD client-chosen id —
        including ids shaped like the old `sess-<clock>-<rand>` — are already
        stamped with an owner by #5742. Nothing here may invalidate them, or
        deploying this would log every current user out of their conversations.
        """
        handler = _import_handler()
        _direct_response_classifier(handler)
        legacy_id = "sess-1758441600000-a1b2c3d"
        aws["sessions"].put_item(Item={
            "session_id": legacy_id,
            "owner_principal": json.dumps(
                [OWNER["custom:tenant_id"], OWNER["custom:org_id"],
                 OWNER["custom:team_id"], OWNER["sub"], "webchat"],
                separators=(",", ":"),
            ),
            "owner_user_id": OWNER["sub"],
            "user_workspace": f'{OWNER["sub"]}#webchat',
            "tenant_id": OWNER["custom:tenant_id"],
            "org_id": OWNER["custom:org_id"], "team_id": OWNER["custom:team_id"],
            "connection_id": "conn-from-yesterday",
            "channel": "webchat",
            "messages": [{"role": "user", "content": "yesterday's question", "ts": 1}],
            "threads": {}, "created_at": 1, "updated_at": 1,
            "expires_at": 9_999_999_999,
        })

        result = _send_message(handler, legacy_id, OWNER, OWNER_CONN, "still here?")

        assert result["statusCode"] == 200
        assert "yesterday's question" in str(_row(aws["sessions"], legacy_id)["messages"])

    def test_an_expired_conversation_is_refused_and_not_silently_recreated(self, aws):
        """The honest consequence of refusing unknown ids, pinned deliberately.

        The sessions table expires rows after 24h (`expires_at`), while the
        browser keeps the id in localStorage indefinitely. An owner returning
        later names an id that genuinely no longer exists — and it must NOT be
        recreated, because "recreate on request" is the squatting behaviour being
        removed. The recovery is a new conversation, which is why the refusal is
        pushed to the client (see the frontend `session_invalid` handling) rather
        than left as a return value API Gateway discards.
        """
        handler = _import_handler()
        _direct_response_classifier(handler)
        reaped_id = _create_session(handler, OWNER, OWNER_CONN)["session_id"]
        # TTL deletion, as DynamoDB would perform it.
        aws["sessions"].delete_item(Key={"session_id": reaped_id})

        result = _send_message(handler, reaped_id, OWNER, OWNER_CONN)

        assert result["statusCode"] == 404
        assert not _row(aws["sessions"], reaped_id), "must not be recreated"
        # The owner can always start again — the recovery path is available.
        assert _create_session(handler, OWNER, OWNER_CONN,
                               request_id="req-2")["statusCode"] == 200

    def test_the_refusal_reaches_the_browser(self, aws):
        """Otherwise the client waits forever on a turn that will never run.

        A WebSocket integration's return value is discarded by API Gateway, so
        the 404 above is invisible to the page. The frame is what lets the SPA
        stop the spinner and offer a new conversation.
        """
        handler = _import_handler()
        _direct_response_classifier(handler)
        posted: list[dict] = []

        class FakeApiGw:
            def post_to_connection(self, ConnectionId, Data):
                posted.append(json.loads(Data))

        with patch.object(handler, "_get_apigw_client", return_value=FakeApiGw()):
            _send_message(handler, "sess-never-issued-0123456789abcdef",
                          OWNER, OWNER_CONN)

        assert any(frame.get("type") == "session_invalid" for frame in posted)

    def test_that_frame_does_not_say_whether_the_session_existed(self, aws):
        """It must stay the same non-answer #5742 established.

        If the frame distinguished "expired" from "somebody else's", it would be
        an oracle for enumerating other tenants' session ids — reintroducing by
        a side channel exactly what the 404 was shaped to avoid.
        """
        handler = _import_handler()
        _direct_response_classifier(handler)
        victim_id = _create_session(handler, OWNER, OWNER_CONN)["session_id"]
        frames: dict[str, dict] = {}

        class FakeApiGw:
            def __init__(self, label):
                self.label = label

            def post_to_connection(self, ConnectionId, Data):
                payload = json.loads(Data)
                if payload.get("type") == "session_invalid":
                    frames[self.label] = payload

        for label, session_id in (("existing", victim_id),
                                  ("absent", "sess-absent-0123456789abcdef01")):
            with patch.object(handler, "_get_apigw_client",
                              return_value=FakeApiGw(label)):
                _send_message(handler, session_id, STRANGER, STRANGER_CONN)

        assert set(frames) == {"existing", "absent"}
        for frame in frames.values():
            frame.pop("session_id", None)  # echoed back; the client sent it
        assert frames["existing"] == frames["absent"]


# ---------------------------------------------------------------------------
# 4. Unknown and foreign identifiers
# ---------------------------------------------------------------------------


class TestAnUnknownIdIsNotACreationRequest:
    """The behaviour change that actually closes the squatting path."""

    def test_a_client_named_unknown_id_creates_nothing(self, aws):
        """Pre-fix this wrote a row under the attacker's chosen name."""
        handler = _import_handler()
        _direct_response_classifier(handler)

        result = _send_message(handler, "sess-1758441600000-chosen",
                               OWNER, OWNER_CONN)

        assert result["statusCode"] == 404
        assert not _row(aws["sessions"], "sess-1758441600000-chosen")

    def test_squatting_an_id_no_longer_locks_anyone_out(self, aws):
        """The lockout, end to end — the actual remaining harm from #5615.

        Pre-fix: the stranger's message created `sess-<predicted>`, owned by the
        stranger; the victim's browser then chose that same id and was refused
        its own new conversation. Now the stranger's attempt creates nothing, so
        there is nothing to collide with — and the owner's id is unguessable
        anyway, which is the belt to that braces.
        """
        handler = _import_handler()
        _direct_response_classifier(handler)
        predicted = f"sess-{int(time.time() * 1000)}-abc1234"

        squat = _send_message(handler, predicted, STRANGER, STRANGER_CONN)
        assert squat["statusCode"] == 404
        assert not _row(aws["sessions"], predicted)

        # The victim's own new conversation is unaffected.
        reply = _create_session(handler, OWNER, OWNER_CONN)
        assert reply["statusCode"] == 200
        assert _row(aws["sessions"], reply["session_id"])["owner_user_id"] == OWNER["sub"]

    def test_the_refusal_costs_nothing_and_enqueues_nothing(self, aws):
        """No Bedrock spend, no SQS task, no invocation row for a refused id."""
        handler = _import_handler()
        _direct_response_classifier(handler)

        with patch.object(handler.sqs, "send_message") as send:
            _send_message(handler, "sess-never-issued-cafebabe", OWNER, OWNER_CONN)

        send.assert_not_called()

    def test_the_operator_plane_still_creates_its_own_sessions(self, aws):
        """`adp flow start` mints ids SERVER-side and arrives IAM-gated.

        Scoping the refusal by channel alone would have broken this path, because
        `channels/gateway_api.py` deliberately emits ChannelType.WEBCHAT so its
        rows stay indistinguishable from browser-started ones. It is
        distinguished by its `ingress` provenance instead.
        """
        handler = _import_handler()
        _direct_response_classifier(handler)
        cli_session = "sess-cli-9f8e7d6c5b4a3210"

        result = handler.lambda_handler({
            "source": "gateway-api",
            "session_id": cli_session,
            "message": "Add per-tenant rate limiting",
            "user_id": OWNER["sub"],
            "org_id": OWNER["custom:org_id"],
            "tenant_id": OWNER["custom:tenant_id"],
            "team_id": OWNER["custom:team_id"],
            "department_id": "", "account_type": "human",
            "requested_persona": "intent-refinement",
            "channel": "webchat", "message_id": "cli-0001",
        }, None)

        assert result["statusCode"] == 200
        assert _row(aws["sessions"], cli_session)["owner_user_id"] == OWNER["sub"]

    def test_a_message_with_no_id_still_starts_a_conversation(self, aws):
        """The fallback id is server-derived, so it is not a client's choice.

        With no `session_id` the handler uses `message.session_key`, built from
        the verified sub and the connection. Nothing client-chosen is in it and
        it cannot name another user's row, so refusing it would break the direct
        `handle_unified_message` callers for no security gain.
        """
        handler = _import_handler()
        _direct_response_classifier(handler)

        result = handler.lambda_handler(
            _ws_event({"action": "message", "text": "no id at all"},
                      OWNER, OWNER_CONN),
            None,
        )

        assert result["statusCode"] == 200
        rows = aws["sessions"].scan()["Items"]
        conversations = [r for r in rows if not r["session_id"].startswith("conn#")]
        assert len(conversations) == 1
        assert conversations[0]["owner_user_id"] == OWNER["sub"]


class TestRandomnessDoesNotReplaceOwnership:
    """The reviewer's finding, kept pinned: guessing a real id still gets nothing.

    These duplicate the intent of `test_session_ownership.py` on purpose, against
    a SERVER-ISSUED id. If a future change ever reasoned "the id is unguessable,
    so the owner check is redundant", these are the tests that fail.
    """

    def test_knowing_an_issued_id_does_not_grant_access_to_it(self, aws):
        handler = _import_handler()
        _direct_response_classifier(handler)
        owner_session = _create_session(handler, OWNER, OWNER_CONN)["session_id"]
        _send_message(handler, owner_session, OWNER, OWNER_CONN, "my private notes")

        # The stranger knows the id exactly — no guessing involved.
        result = _send_message(handler, owner_session, STRANGER, STRANGER_CONN,
                               "summarise this conversation")

        assert result["statusCode"] == 404
        assert "private notes" not in json.dumps(result)

    def test_the_owners_live_connection_is_not_rebound_to_the_stranger(self, aws):
        """The hijack: replies must keep going to the owner's screen."""
        handler = _import_handler()
        _direct_response_classifier(handler)
        owner_session = _create_session(handler, OWNER, OWNER_CONN)["session_id"]
        _send_message(handler, owner_session, OWNER, OWNER_CONN)

        _send_message(handler, owner_session, STRANGER, STRANGER_CONN)

        assert _row(aws["sessions"], owner_session)["connection_id"] == OWNER_CONN

    def test_a_refused_turn_leaves_no_trace_in_the_owners_transcript(self, aws):
        handler = _import_handler()
        _direct_response_classifier(handler)
        owner_session = _create_session(handler, OWNER, OWNER_CONN)["session_id"]
        _send_message(handler, owner_session, OWNER, OWNER_CONN, "mine")

        _send_message(handler, owner_session, STRANGER, STRANGER_CONN, "theirs")

        assert "theirs" not in str(_row(aws["sessions"], owner_session)["messages"])

    def test_an_unowned_row_is_not_adopted_by_whoever_names_it(self, aws):
        """Randomness cannot help here — the row exists and has no owner.

        Only the recorded-owner check can refuse this, which is why the two
        layers are not interchangeable.
        """
        handler = _import_handler()
        _direct_response_classifier(handler)
        aws["sessions"].put_item(Item={
            "session_id": "sess-legacy-unowned-00000000",
            "user_workspace": "someone#webchat",
            "messages": [], "threads": {}, "created_at": 1, "updated_at": 1,
        })

        result = _send_message(handler, "sess-legacy-unowned-00000000",
                               STRANGER, STRANGER_CONN)

        assert result["statusCode"] == 404
        assert "owner_principal" not in _row(
            aws["sessions"], "sess-legacy-unowned-00000000"
        )


# ---------------------------------------------------------------------------
# 5. Response routing
# ---------------------------------------------------------------------------


class TestResponseRoutingStaysWithTheOwner:
    """The id decides where an agent's answer is delivered."""

    def test_the_connection_recorded_for_delivery_is_the_authenticated_one(self, aws):
        """The row's `connection_id` is where replies go.

        It must come from the event's own connection, not from anything in the
        body, or a client could name a conversation and redirect its answers to a
        socket it controls.
        """
        handler = _import_handler()
        _direct_response_classifier(handler)
        session_id = _create_session(handler, OWNER, OWNER_CONN)["session_id"]

        handler.lambda_handler(
            _ws_event(
                {"action": "message", "text": "hi", "session_id": session_id,
                 "connection_id": STRANGER_CONN},
                OWNER, OWNER_CONN,
            ),
            None,
        )

        assert _row(aws["sessions"], session_id)["connection_id"] == OWNER_CONN

    def test_the_owners_reply_is_addressed_to_the_owners_socket(self, aws):
        """The answer's delivery address is set from the authenticated event.

        A direct response is not posted from here — it goes onto the response
        FIFO, and a separate delivery Lambda pushes it to whatever
        `connection_id` the envelope names. That field is therefore the routing
        decision, and it must be the connection the request was authenticated
        on rather than anything the body asked for.
        """
        handler = _import_handler()
        _direct_response_classifier(handler)
        session_id = _create_session(handler, OWNER, OWNER_CONN)["session_id"]

        handler.lambda_handler(
            _ws_event(
                {"action": "message", "text": "answer me", "session_id": session_id,
                 "connection_id": STRANGER_CONN},
                OWNER, OWNER_CONN,
            ),
            None,
        )

        queue_url = aws["sqs"].get_queue_url(QueueName="resp.fifo")["QueueUrl"]
        messages = aws["sqs"].receive_message(
            QueueUrl=queue_url, MaxNumberOfMessages=1, WaitTimeSeconds=0,
        ).get("Messages", [])
        assert messages, "the owner must have an answer queued for delivery"
        envelope = json.loads(messages[0]["Body"])
        assert envelope["connection_id"] == OWNER_CONN
        assert envelope["session_id"] == session_id

    def test_the_queued_task_carries_the_owner_not_the_sender_of_the_body(self, aws):
        """The worker inherits its owner's Bedrock destination from this envelope.

        A wrong identity here is cross-tenant spend and a cross-tenant answer,
        not a mislabel — so the envelope must be built from claims.
        """
        handler = _import_handler()
        from classifier import ClassificationResult

        handler.classify_message = lambda *a, **k: ClassificationResult(
            path="long_running", persona="developer", response=None,
            thread_action="new", reasoning="test",
        )
        session_id = _create_session(handler, OWNER, OWNER_CONN)["session_id"]

        handler.lambda_handler(
            _ws_event(
                {"action": "message", "text": "deep work please",
                 "session_id": session_id,
                 "user_id": STRANGER["sub"], "org_id": STRANGER["custom:org_id"]},
                OWNER, OWNER_CONN,
            ),
            None,
        )

        queue_url = aws["sqs"].get_queue_url(QueueName="tasks")["QueueUrl"]
        messages = aws["sqs"].receive_message(
            QueueUrl=queue_url, MaxNumberOfMessages=1, WaitTimeSeconds=0,
        ).get("Messages", [])
        assert messages, "the owner's long-running turn must be enqueued"
        task = json.loads(messages[0]["Body"])
        assert task["session_id"] == session_id
        assert task["user_id"] == OWNER["sub"]
        assert task["org_id"] == OWNER["custom:org_id"]


# ---------------------------------------------------------------------------
# 6. Attachments
# ---------------------------------------------------------------------------


class TestAttachmentsUseTheAcknowledgedId:
    """The id is also an S3 path segment, so uploads must follow it."""

    @staticmethod
    def _connect(handler, claims: dict, connection_id: str) -> None:
        handler.lambda_handler(
            {"requestContext": {"routeKey": "$connect", "connectionId": connection_id,
                                "authorizer": {"claims": claims}}},
            None,
        )

    def test_an_upload_token_for_the_issued_id_is_scoped_to_its_owner(self, aws):
        handler = _import_handler()
        self._connect(handler, OWNER, OWNER_CONN)
        session_id = _create_session(handler, OWNER, OWNER_CONN)["session_id"]

        with patch.object(handler.s3_client, "generate_presigned_url",
                          return_value="https://s3.example.test/presigned"):
            result = handler.lambda_handler(_ws_event({
                "action": "upload-token", "session_id": session_id,
                "task_id": "task-1", "filename": "notes.txt",
            }, OWNER, OWNER_CONN), None)

        assert result["statusCode"] == 200
        s3_key = json.loads(result["body"])["s3_key"]
        assert s3_key == (
            f'o/{OWNER["custom:org_id"]}/t/{OWNER["custom:team_id"]}'
            f'/u/{OWNER["sub"]}/s/{session_id}/task-1/in/notes.txt'
        )

    def test_upload_and_later_read_agree_on_the_same_key(self, aws):
        """Both halves must derive the same key, or the catalogue row points at
        an object that does not exist — an upload that silently reads back empty."""
        handler = _import_handler()
        self._connect(handler, OWNER, OWNER_CONN)
        session_id = _create_session(handler, OWNER, OWNER_CONN)["session_id"]

        with patch.object(handler.s3_client, "generate_presigned_url",
                          return_value="https://s3.example.test/p"):
            token = handler.lambda_handler(_ws_event({
                "action": "upload-token", "session_id": session_id,
                "task_id": "task-1", "filename": "notes.txt",
            }, OWNER, OWNER_CONN), None)
        issued_key = json.loads(token["body"])["s3_key"]

        complete = handler.lambda_handler(_ws_event({
            "action": "upload-complete", "session_id": session_id,
            "task_id": "task-1", "filename": "notes.txt", "checksum": "cafe1234",
        }, OWNER, OWNER_CONN), None)

        assert complete["statusCode"] == 200
        artifact_id = json.loads(complete["body"])["artifact_id"]
        rows = aws["artifacts"].query(
            KeyConditionExpression="PK = :pk",
            ExpressionAttributeValues={":pk": f"session#{session_id}"},
        )["Items"]
        stored = next(r for r in rows if r["id"] == artifact_id)
        assert stored["s3Key"] == issued_key

    def test_a_stranger_cannot_attach_to_an_issued_session(self, aws):
        """Knowing the id is not permission to add files to the conversation."""
        handler = _import_handler()
        self._connect(handler, OWNER, OWNER_CONN)
        self._connect(handler, STRANGER, STRANGER_CONN)
        session_id = _create_session(handler, OWNER, OWNER_CONN)["session_id"]

        with patch.object(handler.s3_client, "generate_presigned_url",
                          return_value="https://s3.example.test/p"):
            result = handler.lambda_handler(_ws_event({
                "action": "upload-token", "session_id": session_id,
                "task_id": "task-1", "filename": "exfil.txt",
            }, STRANGER, STRANGER_CONN), None)

        assert result["statusCode"] == 404
        assert _row(aws["sessions"], session_id)["owner_user_id"] == OWNER["sub"]

    def test_an_attachment_reaches_the_worker_under_the_issued_id(self, aws):
        """End to end: the id the server acknowledged is the one on the wire."""
        handler = _import_handler()
        from classifier import ClassificationResult

        handler.classify_message = lambda *a, **k: ClassificationResult(
            path="long_running", persona="developer", response=None,
            thread_action="new", reasoning="test",
        )
        self._connect(handler, OWNER, OWNER_CONN)
        session_id = _create_session(handler, OWNER, OWNER_CONN)["session_id"]

        handler.lambda_handler(_ws_event({
            "action": "sendMessage", "text": "review the attached file",
            "session_id": session_id, "attachments": ["art_abc123"],
        }, OWNER, OWNER_CONN), None)

        queue_url = aws["sqs"].get_queue_url(QueueName="tasks")["QueueUrl"]
        messages = aws["sqs"].receive_message(
            QueueUrl=queue_url, MaxNumberOfMessages=1, WaitTimeSeconds=0,
        ).get("Messages", [])
        assert messages
        task = json.loads(messages[0]["Body"])
        assert task["session_id"] == session_id
        assert "art_abc123" in json.dumps(task)


# ---------------------------------------------------------------------------
# 7. The upload route is not a second way to name a conversation
# ---------------------------------------------------------------------------


class TestUploadTokenIsNotACreationRoute:
    """The gap the review on PR #5857 found, at its real size.

    The message path refuses an id the store never issued. But `upload-token`
    still CREATED the row it was asked about, stamping the caller as its owner —
    so the refusal could be walked around in three steps:

        sendMessage(invented id)  -> 404, no row
        upload-token(same id)     -> 200, ROW CREATED, caller recorded as owner
        sendMessage(same id)      -> 200, the invented id is now "owned"

    That is a bypass of the server-issued-only contract, not cross-owner
    disclosure: another user's id was already refused here before this change
    and still is (`test_a_stranger_cannot_attach_to_an_issued_session` above).
    What it restores is SQUATTING — pre-creating the id a victim's browser is
    about to be issued, locking the victim out of their own new conversation.

    An upload token now requires a conversation that already exists and is owned
    by the verified caller. Nothing is lost by that: the row now comes into
    existence when the conversation is STARTED (`create-session`), which the
    browser does before it can offer the drop zone at all — so attaching a file
    before the first message still works, which is what the create-on-upload
    behaviour originally existed for.
    """

    @staticmethod
    def _connect(handler, claims: dict, connection_id: str) -> None:
        handler.lambda_handler(
            {"requestContext": {"routeKey": "$connect", "connectionId": connection_id,
                                "authorizer": {"claims": claims}}},
            None,
        )

    @staticmethod
    def _upload_token(handler, session_id: str, claims: dict,
                      connection_id: str, filename: str = "inert.txt") -> dict:
        """Ask for an upload token. No object is ever PUT: the presigner is
        stubbed, so this exercises the authorization decision and nothing else."""
        with patch.object(handler.s3_client, "generate_presigned_url",
                          return_value="https://s3.example.test/presigned"):
            result = handler.lambda_handler(_ws_event({
                "action": "upload-token", "session_id": session_id,
                "task_id": "task-1", "filename": filename,
            }, claims, connection_id), None)
        return {"statusCode": result["statusCode"], **json.loads(result["body"])}

    def test_an_upload_token_does_not_create_the_session_it_names(self, aws):
        """The bypass, end to end. Fails before the repair at every step after 1."""
        handler = _import_handler()
        _direct_response_classifier(handler)
        self._connect(handler, OWNER, OWNER_CONN)
        invented = "sess-invented-by-the-client"

        # 1. The message path already refuses it.
        assert _send_message(handler, invented, OWNER, OWNER_CONN)["statusCode"] == 404
        assert not _row(aws["sessions"], invented)

        # 2. The upload route must refuse it too, and must not create the row.
        assert self._upload_token(handler, invented, OWNER, OWNER_CONN)["statusCode"] == 404
        assert not _row(aws["sessions"], invented), (
            "upload-token created a client-named session, bypassing create-session"
        )

        # 3. So the id is still not usable — the squat never takes hold.
        assert _send_message(handler, invented, OWNER, OWNER_CONN)["statusCode"] == 404

    def test_the_refusal_does_not_distinguish_unknown_from_somebody_elses(self, aws):
        """Otherwise the route becomes an oracle for which ids exist."""
        handler = _import_handler()
        self._connect(handler, OWNER, OWNER_CONN)
        self._connect(handler, STRANGER, STRANGER_CONN)
        owned = _create_session(handler, OWNER, OWNER_CONN)["session_id"]

        unknown = self._upload_token(
            handler, "sess-never-issued-at-all", STRANGER, STRANGER_CONN)
        foreign = self._upload_token(handler, owned, STRANGER, STRANGER_CONN)

        assert unknown["statusCode"] == foreign["statusCode"] == 404
        assert unknown["error"] == foreign["error"]

    def test_an_upload_token_for_an_issued_session_still_works(self, aws):
        """Regression: the repair must not break attaching a file.

        Including BEFORE the first message is sent, which is the case the old
        create-on-upload behaviour existed to serve.
        """
        handler = _import_handler()
        self._connect(handler, OWNER, OWNER_CONN)
        session_id = _create_session(handler, OWNER, OWNER_CONN)["session_id"]

        reply = self._upload_token(handler, session_id, OWNER, OWNER_CONN)

        assert reply["statusCode"] == 200
        assert reply["s3_key"] == (
            f'o/{OWNER["custom:org_id"]}/t/{OWNER["custom:team_id"]}'
            f'/u/{OWNER["sub"]}/s/{session_id}/task-1/in/inert.txt'
        )

    def test_repeated_uploads_to_the_same_session_all_succeed(self, aws):
        """The ownership read must not be a one-shot reservation."""
        handler = _import_handler()
        self._connect(handler, OWNER, OWNER_CONN)
        session_id = _create_session(handler, OWNER, OWNER_CONN)["session_id"]

        replies = [
            self._upload_token(handler, session_id, OWNER, OWNER_CONN,
                               filename=f"file{i}.txt")
            for i in range(3)
        ]

        assert [r["statusCode"] for r in replies] == [200, 200, 200]
        assert len({r["s3_key"] for r in replies}) == 3

    def test_an_upload_and_its_completion_still_agree(self, aws):
        """The full attachment flow on an acknowledged id: token -> record -> read."""
        handler = _import_handler()
        self._connect(handler, OWNER, OWNER_CONN)
        session_id = _create_session(handler, OWNER, OWNER_CONN)["session_id"]

        issued_key = self._upload_token(handler, session_id, OWNER, OWNER_CONN)["s3_key"]
        complete = handler.lambda_handler(_ws_event({
            "action": "upload-complete", "session_id": session_id,
            "task_id": "task-1", "filename": "inert.txt", "checksum": "cafe1234",
        }, OWNER, OWNER_CONN), None)

        assert complete["statusCode"] == 200
        rows = aws["artifacts"].query(
            KeyConditionExpression="PK = :pk",
            ExpressionAttributeValues={":pk": f"session#{session_id}"},
        )["Items"]
        assert [r["s3Key"] for r in rows] == [issued_key]

    def test_a_session_from_before_this_change_can_still_take_uploads(self, aws):
        """Live conversations predate `create-session`; they must not be stranded."""
        handler = _import_handler()
        self._connect(handler, OWNER, OWNER_CONN)
        legacy_id = "sess-1700000000000-abc1234"  # the old clock-derived shape
        aws["sessions"].put_item(Item={
            "session_id": legacy_id,
            "owner_principal": json.dumps(
                [OWNER["custom:tenant_id"], OWNER["custom:org_id"],
                 OWNER["custom:team_id"], OWNER["sub"], "webchat"],
                separators=(",", ":"),
            ),
            "owner_user_id": OWNER["sub"],
            "org_id": OWNER["custom:org_id"], "team_id": OWNER["custom:team_id"],
            "tenant_id": OWNER["custom:tenant_id"],
            "channel": "webchat", "messages": [], "threads": {},
            "created_at": 1, "updated_at": 1, "expires_at": 9_999_999_999,
        })

        assert self._upload_token(
            handler, legacy_id, OWNER, OWNER_CONN)["statusCode"] == 200

    def test_an_unowned_legacy_row_is_not_adopted_by_an_upload(self, aws):
        """A row with no recorded owner proves nothing about who owns it.

        Quarantined rather than claimed — the same disposition the message path
        already gives it.
        """
        handler = _import_handler()
        self._connect(handler, OWNER, OWNER_CONN)
        aws["sessions"].put_item(Item={
            "session_id": "sess-unowned-legacy", "channel": "webchat",
            "messages": [], "threads": {},
            "created_at": 1, "updated_at": 1, "expires_at": 9_999_999_999,
        })

        reply = self._upload_token(handler, "sess-unowned-legacy", OWNER, OWNER_CONN)

        assert reply["statusCode"] == 404
        assert "owner_principal" not in _row(aws["sessions"], "sess-unowned-legacy")


# ---------------------------------------------------------------------------
# 8. What a pre-change client actually gets
# ---------------------------------------------------------------------------


class TestAPreChangeClientIsRefusedNotAccommodated:
    """The mixed-version behaviour, stated accurately.

    PR #5857 originally claimed an older cached copy of the chat page keeps
    working against this backend. That is true only for conversations it ALREADY
    has, and the review was right to reject the general claim. A pre-change page
    invents its own id for a NEW chat, this backend refuses it, and — because the
    pre-change hook's frame switch has no `session_invalid` case and no default
    branch — the recovery frame it is sent is silently dropped. The user sees
    their message vanish with no error.

    That is the intended trade: accommodating an invented id is exactly the
    behaviour being removed. The supported rollout is therefore to ship the page
    and this backend together and have open tabs reload; these tests pin the
    behaviour so the claim rests on evidence rather than optimism. Nothing here
    deploys anything or changes any cache.
    """

    @staticmethod
    def _connect(handler, claims: dict, connection_id: str) -> None:
        handler.lambda_handler(
            {"requestContext": {"routeKey": "$connect", "connectionId": connection_id,
                                "authorizer": {"claims": claims}}},
            None,
        )

    # The literal shape a pre-change page produced:
    # `sess-${Date.now()}-${Math.random().toString(36).slice(2,9)}`.
    OLD_STYLE_ID = "sess-1758700000000-k3f9x2q"

    def test_a_new_chat_from_a_pre_change_page_is_refused(self, aws):
        handler = _import_handler()
        _direct_response_classifier(handler)
        self._connect(handler, OWNER, OWNER_CONN)

        reply = _send_message(handler, self.OLD_STYLE_ID, OWNER, OWNER_CONN)

        assert reply["statusCode"] == 404
        assert not _row(aws["sessions"], self.OLD_STYLE_ID)

    def test_the_recovery_frame_is_sent_even_though_an_old_page_drops_it(self, aws):
        """The backend does its part; the old page cannot act on it.

        Worth pinning separately: the frame IS emitted, so the silent-loss
        symptom belongs to the old client's frame handling, not to a backend that
        fails to say anything.
        """
        handler = _import_handler()
        _direct_response_classifier(handler)
        self._connect(handler, OWNER, OWNER_CONN)
        posted: list[dict] = []

        class FakeApiGw:
            def post_to_connection(self, ConnectionId, Data):
                posted.append(json.loads(Data))

        with patch.object(handler, "_get_apigw_client", return_value=FakeApiGw()):
            _send_message(handler, self.OLD_STYLE_ID, OWNER, OWNER_CONN)

        assert [f for f in posted if f.get("type") == "session_invalid"]

    def test_a_conversation_the_old_page_already_had_keeps_working(self, aws):
        """The part of the compatibility claim that IS true.

        An id minted before this change is a row the store has seen, so it is
        judged on its recorded owner exactly as before — which is why existing
        conversations survive the rollout while new ones from a stale page do not.
        """
        handler = _import_handler()
        _direct_response_classifier(handler)
        self._connect(handler, OWNER, OWNER_CONN)
        aws["sessions"].put_item(Item={
            "session_id": self.OLD_STYLE_ID,
            "owner_principal": json.dumps(
                [OWNER["custom:tenant_id"], OWNER["custom:org_id"],
                 OWNER["custom:team_id"], OWNER["sub"], "webchat"],
                separators=(",", ":"),
            ),
            "owner_user_id": OWNER["sub"],
            "org_id": OWNER["custom:org_id"], "team_id": OWNER["custom:team_id"],
            "tenant_id": OWNER["custom:tenant_id"],
            "channel": "webchat",
            "messages": [{"role": "user", "content": "earlier turn", "ts": 1}],
            "threads": {}, "created_at": 1, "updated_at": 1,
            "expires_at": 9_999_999_999,
        })

        reply = _send_message(handler, self.OLD_STYLE_ID, OWNER, OWNER_CONN)

        assert reply["statusCode"] == 200
        history = _row(aws["sessions"], self.OLD_STYLE_ID)["messages"]
        assert any(m["content"] == "earlier turn" for m in history)
