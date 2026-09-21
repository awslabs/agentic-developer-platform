"""The intake-conversation API a terminal client plans through (#5331, EPIC #4191).

These tests are organised around what a CLI journey actually breaks on, not around
the routes' CRUD shape.

* :class:`TestStartingAConversation` — a session id comes back immediately, before
  any reply exists, because that is the only thing a caller that dies mid-turn has
  to resume with.
* :class:`TestOwnershipIsNotPossessionOfAnId` — a session id lands in logs and shell
  history, so every verb resolves ownership from the stored row, and a foreign
  session is *absent* rather than *forbidden*.
* :class:`TestResumeReadsBackWhatAClientCannotReconstruct` — the pending question,
  the draft and the issue the conversation opened.
* :class:`TestUnavailableIsNotFailure` — a deployment without the intake backend
  says so, distinctly from a broken one, because the CLI maps the two to different
  exit codes and an operator takes different action on each.
* :class:`TestAnInFlightTurnIsNotInterrupted` — re-sending into a turn the agent
  still holds would interleave with the answer being written.

The store and the ingest Lambda are stubs. The reader's and dispatcher's own units
are tested against stubs in `test_intake_session.py` / `test_intake_dispatch.py`; what
is asserted here is the HTTP contract over them — status codes, the shape a client
parses, and that authorization happens before the turn is dispatched.

"Nothing was dispatched" is asserted as `ingest.calls == []` throughout. That is the
real side effect now: the ingest Lambda is what creates the session, the thread, the
transcript and the registered run, so an invocation that should not have happened is
a conversation that should not exist — strictly more than a queue message.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from src.admin.access_control import AccessControl
from src.admin.config import AdminRole, Permission
from src.orchestration.intake_dispatch import IntakeDispatcher
from src.orchestration.intake_session import INTAKE_CHANNEL, IntakeSessionReader
from src.shared.exceptions import BedrockGatewayError
from src.shared.schemas.auth import TokenContext

ORG = "org-alpha"
# A second tenant the same human also belongs to. Present because `user_workspace`
# carries no tenant, so one person authenticates with the SAME `user_id` in both and
# a user-only ownership comparison cannot tell these two apart.
OTHER_ORG = "org-beta"
OWNER = "user-alpha"
INTRUDER = "user-beta"
SESSION = "sess-0123456789abcdef0123456789abcdef"
INGEST_FUNCTION = "adp-dev-agent-gateway-ingest"

SESSIONS = "/orchestration/intake/sessions"


def a_row(
    *,
    session_id: str = SESSION,
    user_id: str = OWNER,
    org_id: str | None = ORG,
    updated_at: int = 1_700_000_000,
    **overrides: Any,
) -> dict[str, Any]:
    """A sessions-table row as the agent-factory ingest Lambda writes one.

    `org_id` is the tenant stamped at creation, and half of the ownership comparison
    every read makes. Pass `None` for a row that predates the stamp.
    """
    row: dict[str, Any] = {
        "session_id": session_id,
        "user_workspace": f"{user_id}#{INTAKE_CHANNEL}",
        "updated_at": Decimal(updated_at),
    }
    if org_id is not None:
        row["org_id"] = org_id
    row.update(overrides)
    return row


class StubStore:
    """A sessions table over fixed rows, filtering queries as the real GSI does."""

    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self._rows = rows or []

    def get_item(self, **kwargs: Any) -> dict[str, Any]:
        wanted = kwargs["Key"]["session_id"]
        for row in self._rows:
            if row.get("session_id") == wanted:
                return {"Item": row}
        return {}

    def query(self, **kwargs: Any) -> dict[str, Any]:
        workspace = kwargs["ExpressionAttributeValues"][":workspace"]
        return {"Items": [row for row in self._rows if row.get("user_workspace") == workspace]}

    def update_item(self, **kwargs: Any) -> dict[str, Any]:
        return {}


class StubDraftStore:
    """The chat-context table, holding drafts at `PK=session#<id>, SK=draft`."""

    def __init__(self, drafts: dict[str, dict[str, Any]] | None = None) -> None:
        self._drafts = drafts or {}

    def get_item(self, **kwargs: Any) -> dict[str, Any]:
        key = kwargs["Key"]
        if key.get("SK") != "draft":
            return {}
        draft = self._drafts.get(str(key.get("PK", "")))
        return {"Item": {"PK": key["PK"], "SK": "draft", "draft": draft}} if draft is not None else {}


def a_thread(*, processing_task_id: str = "", created_at: int = 1_700_000_000, issue_number: str = "") -> dict[str, Any]:
    """A thread as `create_thread` writes one into the row's `threads` MAP.

    `processing_task_id` defaults to `""` because that is what
    `_clear_thread_processing` leaves behind — idle is present-and-empty, not absent.
    """
    thread: dict[str, Any] = {
        "topic": "Ship the CLI",
        "path": "long_running",
        "persona": "intent-refinement",
        "processing_task_id": processing_task_id,
        "messages": [],
        "created_at": Decimal(created_at),
    }
    if issue_number:
        thread["github_issue_number"] = issue_number
    return thread


class StubIngest:
    """The ingest Lambda, recording invocations or failing on demand.

    `calls` is the assertion surface for "nothing happened": an invocation creates the
    session row, the thread, the transcript entry and the run registration, so a call
    that should not have been made is a whole conversation that should not exist.
    """

    def __init__(self, error: Exception | None = None, response: dict[str, Any] | None = None) -> None:
        self._error = error
        self._response = response
        self.calls: list[dict[str, Any]] = []

    def invoke(self, **kwargs: Any) -> dict[str, Any]:
        if self._error:
            raise self._error
        self.calls.append(kwargs)
        if self._response is not None:
            return self._response
        # Echoes the envelope's `session_id`, as the real handler does: it keys the
        # session row off that value and returns it. A stub that returned a fixed id
        # would hide a dispatcher that ignored the id it was given.
        sent = json.loads(kwargs["Payload"])
        return {
            "StatusCode": 200,
            "Payload": json.dumps(
                {
                    "statusCode": 200,
                    "body": json.dumps(
                        {
                            "task_id": f"task-{len(self.calls)}",
                            "session_id": sent.get("session_id", ""),
                            "thread_id": "th-ab12",
                            "status": "processing",
                        }
                    ),
                }
            ),
        }

    @property
    def payloads(self) -> list[dict[str, Any]]:
        return [json.loads(call["Payload"]) for call in self.calls]


def token_context(*, user_id: str = OWNER, org_id: str = ORG) -> TokenContext:
    return TokenContext(
        user_id=user_id,
        org_id=org_id,
        team_id="",
        department_id="",
        account_type="human",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


def build_app(
    *,
    rows: list[dict[str, Any]] | None = None,
    ingest: StubIngest | None = None,
    function_name: str = INGEST_FUNCTION,
    store: Any | None = None,
    # Drafts live in the chat-context table, keyed `session#<id>`. Separate argument
    # because they are a separate table, not a column on the row.
    drafts: dict[str, dict[str, Any]] | None = None,
    caller: str = OWNER,
    caller_org: str = ORG,
    permitted: bool = True,
) -> tuple[FastAPI, StubIngest]:
    """A minimal app carrying only the intake router.

    Deliberately not `create_app()`: that pulls the whole middleware stack, and an
    authorization assertion here would then depend on all of it. Same shape as
    `test_registration.py`'s `app_with_router`.
    """
    from unittest.mock import AsyncMock, MagicMock

    from src.admin.exceptions import AccessDeniedError
    from src.auth.dependencies import get_current_user
    from src.orchestration import intake_routes
    from src.shared.database import get_db

    ingest_client = ingest if ingest is not None else StubIngest()

    app = FastAPI()
    app.include_router(intake_routes.router)

    # `AccessDeniedError` carries status_code=403 and is translated by the app-level
    # handler in `create_app()`. Registered here because this minimal app skips it;
    # without it a denied caller surfaces as 500 and the authz assertions would be
    # testing the harness rather than the route.
    @app.exception_handler(BedrockGatewayError)
    async def _gateway_error_handler(_request: Request, exc: BedrockGatewayError):
        return JSONResponse(status_code=exc.status_code, content={"error": exc.error, "message": exc.message})

    async def override_db():
        # Never touched: the intake routes take `get_db` only through
        # `get_access_control`, which is itself overridden below.
        yield None

    access = MagicMock(spec=AccessControl)
    if permitted:
        access.check_permission = AsyncMock(return_value=True)
    else:
        access.check_permission = AsyncMock(
            side_effect=AccessDeniedError(
                message=f"Permission '{Permission.PLAN_DRAFT.value}' is required for this operation",
                required_permission=Permission.PLAN_DRAFT.value,
                user_role=AdminRole.MEMBER.value,
            )
        )

    resolved_store = store if store is not None else StubStore(rows)
    resolved_drafts = StubDraftStore(drafts if drafts is not None else {})
    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user] = lambda: token_context(user_id=caller, org_id=caller_org)
    app.dependency_overrides[intake_routes.get_access_control] = lambda: access
    app.dependency_overrides[intake_routes._session_reader] = lambda: IntakeSessionReader(resolved_store, resolved_drafts)
    app.dependency_overrides[intake_routes._dispatcher] = lambda: IntakeDispatcher(ingest_client if function_name else None, function_name)
    return app, ingest_client


def client(**kwargs: Any) -> tuple[TestClient, StubIngest]:
    app, ingest_client = build_app(**kwargs)
    return TestClient(app, raise_server_exceptions=False), ingest_client


class TestStartingAConversation:
    """The first thing a CLI needs is something it can resume with."""

    def test_starting_returns_202_with_a_server_minted_session_id(self):
        """202, not 201: the reply is asynchronous.

        A 200 with an empty body would read as "here is your answer" and a client
        would render nothing.
        """
        http, _ = client()
        response = http.post(SESSIONS, json={"message": "Add per-tenant rate limiting"})
        assert response.status_code == 202
        body = response.json()
        assert body["session_id"].startswith("sess-")
        assert body["task_id"]
        assert body["status"] == "accepted"

    def test_the_session_id_is_returned_before_any_reply_exists(self):
        """The whole point: a caller that dies mid-turn can still resume.

        Asserted by checking the response carries an id while the conversation has
        produced nothing — no `last_response`, no draft, no reply field at all.
        """
        http, _ = client()
        body = http.post(SESSIONS, json={"message": "Add rate limiting"}).json()
        assert body["session_id"]
        assert "last_response" not in body, "a start response must not imply a reply has arrived"

    def test_the_caller_does_not_choose_their_own_session_id(self):
        """A client-chosen id could collide with, or address, somebody else's row."""
        http, _ = client()
        response = http.post(SESSIONS, json={"message": "Hi", "session_id": "sess-theirs"})
        # `extra="forbid"` on the request model: the field is rejected outright
        # rather than silently ignored, so a client cannot believe it took effect.
        assert response.status_code == 422

    def test_two_starts_are_two_conversations(self):
        http, _ = client()
        first = http.post(SESSIONS, json={"message": "One"}).json()["session_id"]
        second = http.post(SESSIONS, json={"message": "Two"}).json()["session_id"]
        assert first != second

    def test_the_turn_actually_reaches_the_planning_agent(self):
        """Otherwise the route is a 202 that plans nothing.

        Asserted on the INGEST invocation, not a queue write: a queue write would skip
        the session row, the thread and the run registration, so the 202 would hand
        back an id that reads back as 404.
        """
        http, ingest = client()
        http.post(SESSIONS, json={"message": "Add rate limiting"})
        assert len(ingest.calls) == 1
        assert ingest.calls[0]["FunctionName"] == INGEST_FUNCTION

    def test_the_thread_the_turn_landed_on_is_returned(self):
        """The readback's in-flight state and issue reference both live on the thread,
        so a client correlating what it sent with what it reads back needs it."""
        http, _ = client()
        assert http.post(SESSIONS, json={"message": "Hi"}).json()["thread_id"] == "th-ab12"

    def test_attribution_is_the_authenticated_caller_not_the_body(self):
        """This envelope is later treated as authority for spend and credentials."""
        http, ingest = client(caller=OWNER)
        http.post(SESSIONS, json={"message": "Hi"})
        payload = ingest.payloads[0]
        assert payload["user_id"] == OWNER
        assert payload["org_id"] == ORG

    def test_an_empty_message_is_refused_rather_than_dispatched(self):
        http, ingest = client()
        response = http.post(SESSIONS, json={"message": "   "})
        assert response.status_code == 502
        assert ingest.calls == [], "an empty turn must not open a conversation"

    def test_a_caller_without_plan_draft_is_refused_before_anything_is_sent(self):
        """Authorization precedes the side effect, not the other way round."""
        http, ingest = client(permitted=False)
        response = http.post(SESSIONS, json={"message": "Add rate limiting"})
        assert response.status_code == 403
        assert ingest.calls == [], "a denied caller's message must never reach the planning agent"

    def test_starting_requires_only_plan_draft_not_plan_approve(self):
        """The EPIC's core control, asserted at the route.

        A planning conversation is authoring. If starting one required approval
        authority, either nobody could plan or planners would hold the power to
        accept what they planned — the self-approval inversion.
        """
        import inspect

        from src.orchestration.intake_routes import send_intake_turn, start_intake_session

        for handler in (start_intake_session, send_intake_turn):
            source = inspect.getsource(handler)
            assert "Permission.PLAN_DRAFT" in source
            assert "Permission.PLAN_APPROVE" not in source


class TestOwnershipIsNotPossessionOfAnId:
    """A session id appears in logs and shell history."""

    def test_the_owner_can_read_their_own_session(self):
        """The draft comes from the context table, not the row — see the class below."""
        http, _ = client(rows=[a_row()], drafts={f"session#{SESSION}": {"intent": "Ship the CLI"}})
        response = http.get(f"{SESSIONS}/{SESSION}")
        assert response.status_code == 200
        assert response.json()["draft"] == {"intent": "Ship the CLI"}

    def test_another_users_session_is_404_not_403(self):
        """403 would confirm the session exists.

        A caller who scraped or guessed an id must not be able to learn from this
        surface that it is real.
        """
        http, _ = client(rows=[a_row(user_id=OWNER)], caller=INTRUDER)
        assert http.get(f"{SESSIONS}/{SESSION}").status_code == 404

    def test_a_missing_session_is_indistinguishable_from_a_foreign_one(self):
        """Same status AND same body, so neither can be told apart by string match."""
        http, _ = client(rows=[a_row(user_id=OWNER)], caller=INTRUDER)
        foreign = http.get(f"{SESSIONS}/{SESSION}")
        missing = http.get(f"{SESSIONS}/sess-does-not-exist")
        assert foreign.status_code == missing.status_code == 404
        assert foreign.json() == missing.json()

    def test_a_turn_cannot_be_sent_into_another_users_conversation(self):
        """The ordering matters: ownership is checked BEFORE the dispatch.

        No later 403 could unsend a caller's words out of somebody else's
        conversation, so the assertion is on the ingest Lambda being untouched — not
        merely on the status code.
        """
        http, ingest = client(rows=[a_row(user_id=OWNER)], caller=INTRUDER)
        response = http.post(f"{SESSIONS}/{SESSION}/turns", json={"message": "Also do X"})
        assert response.status_code == 404
        assert ingest.calls == [], "the intruder's turn must never reach the owner's conversation"

    def test_latest_is_scoped_to_the_caller(self):
        http, _ = client(rows=[a_row(user_id=INTRUDER, session_id="sess-theirs")], caller=OWNER)
        assert http.get(f"{SESSIONS}/latest").status_code == 404

    def test_the_same_user_in_another_tenant_is_404_not_200(self):
        """Ownership is the user AND the tenant, because one human is not one principal.

        The intruder case above is the easy half. This is the hard half: the caller IS
        the owner by `user_id`, and `user_workspace` records nothing else, so a
        user-only comparison hands them a conversation — its transcript, its draft,
        the issue it opened — belonging to their other workspace.
        """
        http, _ = client(rows=[a_row(user_id=OWNER, org_id=ORG)], caller=OWNER, caller_org=OTHER_ORG)
        assert http.get(f"{SESSIONS}/{SESSION}").status_code == 404

    def test_a_turn_cannot_cross_into_the_same_users_other_tenant(self):
        """Asserted on the dispatch, not the status: no later refusal unsends a turn."""
        http, ingest = client(rows=[a_row(user_id=OWNER, org_id=ORG)], caller=OWNER, caller_org=OTHER_ORG)
        response = http.post(f"{SESSIONS}/{SESSION}/turns", json={"message": "Also do X"})
        assert response.status_code == 404
        assert ingest.calls == [], "a turn must not reach a conversation in another tenant"

    def test_resume_does_not_cross_tenants(self):
        """The GSI key is `user#channel`, so it cannot scope this — the code must.

        This is the cross-tenant read in its most likely form: it needs no leaked id
        at all, just a resume from the wrong workspace.
        """
        http, _ = client(
            rows=[a_row(session_id="sess-other-tenant", user_id=OWNER, org_id=OTHER_ORG, updated_at=1_700_000_500)],
            caller=OWNER,
            caller_org=ORG,
        )
        assert http.get(f"{SESSIONS}/latest").status_code == 404

    def test_resume_picks_this_tenants_session_over_a_newer_foreign_one(self):
        """`latest` sorts on `updated_at`, so the filter must precede the sort.

        Filtering after taking the newest row would return 404 here — the caller's own
        conversation exists and is reachable — and filtering neither way would return
        the other tenant's.
        """
        http, _ = client(
            rows=[
                a_row(session_id="sess-mine", user_id=OWNER, org_id=ORG, updated_at=1_700_000_100),
                a_row(session_id="sess-theirs", user_id=OWNER, org_id=OTHER_ORG, updated_at=1_700_000_900),
            ],
            caller=OWNER,
            caller_org=ORG,
        )
        response = http.get(f"{SESSIONS}/latest")
        assert response.status_code == 200
        assert response.json()["session_id"] == "sess-mine"

    def test_a_row_with_no_recorded_tenant_is_refused(self):
        """Rows written before the tenant stamp cannot prove whose they are.

        Refused to everyone rather than matched by whoever asks: treating an absent
        value as a wildcard would leave exactly the legacy rows cross-tenant readable.
        """
        http, _ = client(rows=[a_row(user_id=OWNER, org_id=None)], caller=OWNER, caller_org=ORG)
        assert http.get(f"{SESSIONS}/{SESSION}").status_code == 404
        assert http.get(f"{SESSIONS}/latest").status_code == 404

    def test_latest_takes_no_user_parameter_to_aim_at_someone_else(self):
        """A `user_id` query argument would make this a way to read other people's plans."""
        import inspect

        from src.orchestration.intake_routes import latest_intake_session

        parameters = inspect.signature(latest_intake_session).parameters
        assert "user_id" not in parameters
        assert "session_id" not in parameters


class TestResumeReadsBackWhatAClientCannotReconstruct:
    """`--resume` needs the open questions, the draft and the issue.

    All three come from where production writes them: the draft and its
    `openQuestions` from the chat-context table, the issue from the thread that
    opened it. None of them is a column on the session row.
    """

    def test_latest_resolves_the_newest_conversation_without_an_id(self):
        """What makes resume usable at all: the id is not derivable from the user."""
        http, _ = client(
            rows=[
                a_row(session_id="sess-old", updated_at=1_700_000_000),
                a_row(session_id="sess-new", updated_at=1_700_009_999),
            ]
        )
        response = http.get(f"{SESSIONS}/latest")
        assert response.status_code == 200
        assert response.json()["session_id"] == "sess-new"

    def test_latest_is_not_captured_as_a_session_id_by_the_other_route(self):
        """Route-order regression. FastAPI matches in declaration order.

        If `/{session_id}` were declared first, `latest` would be read as an id, the
        lookup would miss, and resume would 404 forever while the code looked right.
        """
        http, _ = client(rows=[a_row()])
        assert http.get(f"{SESSIONS}/latest").status_code == 200

    def test_open_questions_are_exposed_as_structure(self):
        """So a non-interactive client reports "waiting on you" instead of hanging.

        Sourced from `draft.openQuestions`, which is where the persona is instructed
        to put what it still needs decided. There is no `pending_question` column for
        this on the session row, and inventing one made the field permanently empty.
        """
        http, _ = client(
            rows=[a_row()],
            drafts={f"session#{SESSION}": {"intent": "Ship the CLI", "openQuestions": ["Which repo?"]}},
        )
        body = http.get(f"{SESSIONS}/{SESSION}").json()
        assert body["awaiting_answer"] is True
        assert body["open_questions"] == ["Which repo?"]

    def test_no_open_questions_means_not_awaiting(self):
        """A client must be able to tell "not waiting" from "field absent"."""
        http, _ = client(rows=[a_row()], drafts={f"session#{SESSION}": {"intent": "Ship the CLI"}})
        body = http.get(f"{SESSIONS}/{SESSION}").json()
        assert body["awaiting_answer"] is False
        assert body["open_questions"] == []

    def test_open_questions_while_the_agent_is_working_are_not_awaiting_an_answer(self):
        """The agent may still resolve them itself within the turn it holds.

        Reporting "waiting on you" here would have a script answer a question already
        being answered, and the answer would arrive interleaved with the agent's own.
        """
        http, _ = client(
            rows=[a_row(threads={"t-1": a_thread(processing_task_id="task-9")})],
            drafts={f"session#{SESSION}": {"openQuestions": ["Which repo?"]}},
        )
        body = http.get(f"{SESSIONS}/{SESSION}").json()
        assert body["working"] is True
        assert body["open_questions"] == ["Which repo?"]
        assert body["awaiting_answer"] is False

    def test_an_unreadable_draft_is_reported_rather_than_shown_as_empty(self):
        """`{}` alone cannot distinguish "no draft yet" from "cannot read drafts".

        A client that showed the first for the second would tell a user their answers
        had been discarded. The conversation itself still reads back.
        """
        app, _ = build_app(rows=[a_row()])
        from src.orchestration import intake_routes

        class ExplodingDrafts:
            def get_item(self, **_: Any) -> dict[str, Any]:
                raise RuntimeError("chat-context table is not readable")

        app.dependency_overrides[intake_routes._session_reader] = lambda: IntakeSessionReader(StubStore([a_row()]), ExplodingDrafts())
        body = TestClient(app, raise_server_exceptions=False).get(f"{SESSIONS}/{SESSION}").json()
        assert body["draft_available"] is False
        assert body["draft"] == {}
        assert body["session_id"] == SESSION

    def test_the_issue_the_conversation_opened_is_carried(self):
        """So a resume does not open a second one — the duplicate-issue failure.

        The number lives on the thread `create_thread` wrote, inside the row's
        `threads` MAP — not as a top-level `github_issue_number` attribute.
        """
        http, _ = client(rows=[a_row(threads={"t-1": a_thread(issue_number="5331")})])
        assert http.get(f"{SESSIONS}/{SESSION}").json()["issue_ref"] == "5331"

    def test_reading_requires_only_usage_read(self):
        """A transcript and a draft are not an approval record.

        No actor_id, no decision row, no accepted plan. Gating this on PLAN_APPROVE
        would mean nobody could resume their own conversation without also being able
        to accept plans.
        """
        import inspect

        from src.orchestration.intake_routes import get_intake_session, latest_intake_session

        for handler in (get_intake_session, latest_intake_session):
            source = inspect.getsource(handler)
            assert "Permission.USAGE_READ" in source
            assert "Permission.PLAN_APPROVE" not in source

    def test_a_row_missing_newer_fields_still_reads(self):
        """The agent-factory Lambdas add keys over time.

        A strict projection would turn each of their deploys into a resume outage.

        The row still carries the two attributes that establish ownership — the
        workspace and the tenant. Tolerance is for cosmetic fields; a row that cannot
        say which tenant it belongs to is refused, not read (see
        `test_intake_session.py`).
        """
        http, _ = client(
            store=StubStore(
                [
                    {
                        "session_id": SESSION,
                        "user_workspace": f"{OWNER}#{INTAKE_CHANNEL}",
                        "org_id": ORG,
                    }
                ]
            )
        )
        response = http.get(f"{SESSIONS}/{SESSION}")
        assert response.status_code == 200
        assert response.json()["draft"] == {}


class TestAnInFlightTurnIsNotInterrupted:
    """Re-sending into a turn the agent holds would interleave with its answer."""

    def test_sending_while_the_agent_is_working_is_refused_with_409(self):
        """In-flight is the thread's `processing_task_id`, the lock production holds."""
        http, ingest = client(rows=[a_row(threads={"t-1": a_thread(processing_task_id="task-9")})])
        response = http.post(f"{SESSIONS}/{SESSION}/turns", json={"message": "Actually, also X"})
        assert response.status_code == 409
        assert ingest.calls == [], "a turn sent into an in-flight one would be buffered onto the thread"

    def test_the_ingest_lambdas_own_busy_answer_is_also_a_409(self):
        """The authoritative check, reached when the agent takes the turn between our
        read and the dispatch.

        `handle_long_running` answers `status: queued` and appends the message to the
        thread — nothing is lost, so this must not surface as a failure. A caller told
        it failed would retry and stack duplicate turns.
        """
        queued = {
            "StatusCode": 200,
            "Payload": json.dumps(
                {"statusCode": 200, "body": json.dumps({"task_id": None, "session_id": SESSION, "thread_id": "th-1", "status": "queued"})}
            ),
        }
        http, _ = client(rows=[a_row(threads={"t-1": a_thread()})], ingest=StubIngest(response=queued))
        response = http.post(f"{SESSIONS}/{SESSION}/turns", json={"message": "Also X"})
        assert response.status_code == 409
        assert response.json()["detail"]["error"] == "turn_in_progress"

    def test_sending_into_an_idle_conversation_is_accepted(self):
        """Idle is a thread whose lock was cleared to `""`, which is what production writes."""
        http, ingest = client(rows=[a_row(threads={"t-1": a_thread()})])
        response = http.post(f"{SESSIONS}/{SESSION}/turns", json={"message": "Narrow it to one repo"})
        assert response.status_code == 202
        assert len(ingest.calls) == 1

    def test_a_retried_turn_carries_the_same_run_identity_rather_than_doubling(self):
        """A dropped response must not become two registered runs.

        `log_invocation` writes `event_id = message_id`, so the retry token has to
        reach that field — otherwise one turn is billed as two runs against the
        caller's budget, and the worker's status updates split across two rows.
        """
        http, ingest = client(rows=[a_row(threads={"t-1": a_thread()})])
        first = http.post(f"{SESSIONS}/{SESSION}/turns", json={"message": "Narrow it"}).json()
        http.post(f"{SESSIONS}/{SESSION}/turns", json={"message": "Narrow it", "retry_token": first["retry_token"]})
        assert ingest.payloads[0]["message_id"] == ingest.payloads[1]["message_id"]

    def test_the_retry_handle_is_the_registration_key_not_the_ingest_task_id(self):
        """The two are different namespaces, and handing back the wrong one double-bills.

        `task_id` is minted by the ingest Lambda and keys the thread lock; the
        registration row is written under `event_id = message_id`, which the gateway
        mints. A response that offered only `task_id` as the thing to retry with would
        have every retry register a SECOND run under a fresh key — one turn billed
        twice, with the worker's status updates split across two rows.
        """
        http, ingest = client(rows=[a_row(threads={"t-1": a_thread()})])
        body = http.post(f"{SESSIONS}/{SESSION}/turns", json={"message": "Narrow it"}).json()
        assert body["retry_token"] == ingest.payloads[0]["message_id"]
        assert body["task_id"] != body["retry_token"], "the ingest-minted task id is not a retry handle"

    def test_retrying_with_the_ingest_task_id_is_not_mistaken_for_the_retry_token(self):
        """Mirror of the above, stated as behaviour rather than as field identity.

        A client that sent the task id back would be asking to register a run under a
        key nothing was registered under. Nothing here silently accepts it as a retry.
        """
        http, ingest = client(rows=[a_row(threads={"t-1": a_thread()})])
        first = http.post(f"{SESSIONS}/{SESSION}/turns", json={"message": "Narrow it"}).json()
        http.post(f"{SESSIONS}/{SESSION}/turns", json={"message": "Narrow it", "retry_token": first["task_id"]})
        assert ingest.payloads[1]["message_id"] != ingest.payloads[0]["message_id"]

    def test_the_response_does_not_invite_a_retry_under_the_wrong_field(self):
        """`task_id` used to be the request's retry field. Left accepted, a client
        written against the old shape would keep double-registering in silence —
        `extra="forbid"` turns that into a 422 they can see."""
        http, ingest = client(rows=[a_row(threads={"t-1": a_thread()})])
        response = http.post(f"{SESSIONS}/{SESSION}/turns", json={"message": "Narrow it", "task_id": "task-1"})
        assert response.status_code == 422
        assert ingest.calls == []

    def test_the_turn_names_the_conversation_it_belongs_to(self):
        """The ingest Lambda keys the session row and the FIFO group off this, so it is
        what keeps a follow-up from overtaking the question it answers.

        Ordering itself is no longer this router's business — it belongs to the one
        writer that owns the queue. What the router must get right is naming the
        conversation, because a wrong id here opens a second one.
        """
        http, ingest = client(rows=[a_row(threads={"t-1": a_thread()})])
        http.post(f"{SESSIONS}/{SESSION}/turns", json={"message": "Narrow it"})
        assert ingest.payloads[0]["session_id"] == SESSION


class TestUnavailableIsNotFailure:
    """The CLI maps these to different exit codes; operators take different action."""

    def test_an_unconfigured_ingest_function_reports_503_on_start(self):
        """Not 500: nothing broke, a function name was never set."""
        http, _ = client(function_name="")
        response = http.post(SESSIONS, json={"message": "Add rate limiting"})
        assert response.status_code == 503
        assert response.json()["detail"]["error"] == "intake_unavailable"

    def test_an_unconfigured_store_reports_503_on_read(self):
        """Both read routes, because resume tries `latest` before it tries an id."""
        from src.orchestration import intake_routes

        app, _ = build_app()
        app.dependency_overrides[intake_routes._session_reader] = lambda: IntakeSessionReader(None)
        unconfigured = TestClient(app, raise_server_exceptions=False)
        for path in (f"{SESSIONS}/{SESSION}", f"{SESSIONS}/latest"):
            response = unconfigured.get(path)
            assert response.status_code == 503, path
            assert response.json()["detail"]["error"] == "intake_unavailable"

    def test_a_failed_dispatch_is_502_not_a_fake_success(self):
        """A caller told their message was sent waits forever for a reply."""
        http, _ = client(ingest=StubIngest(error=RuntimeError("throttled")))
        response = http.post(SESSIONS, json={"message": "Add rate limiting"})
        assert response.status_code == 502
        assert response.json()["detail"]["error"] == "intake_dispatch_failed"

    def test_unavailable_and_failed_are_different_statuses(self):
        """Conflating them sends an operator to the wrong place.

        503 means "configure this deployment"; 502 means "the dispatch failed, retry".
        A single code for both would make the CLI's `unavailable` exit code
        meaningless.
        """
        unconfigured, _ = client(function_name="")
        broken, _ = client(ingest=StubIngest(error=RuntimeError("throttled")))
        assert unconfigured.post(SESSIONS, json={"message": "Hi"}).status_code == 503
        assert broken.post(SESSIONS, json={"message": "Hi"}).status_code == 502

    def test_a_failure_response_does_not_echo_the_users_text(self):
        """A planning turn is user content and must not land in an error body."""
        secret = "migrate the acme-payments private repo"
        http, _ = client(ingest=StubIngest(error=RuntimeError("boom")))
        response = http.post(SESSIONS, json={"message": secret})
        assert secret not in response.text


class TestTheRouterCannotApproveAnything:
    """The structural justification for permissions below approval authority."""

    def test_the_router_declares_exactly_the_intake_routes(self):
        """A surface change is a review moment, not an accident."""
        from src.orchestration.intake_routes import router

        paths = {(route.path, tuple(sorted(route.methods))) for route in router.routes}
        assert paths == {
            (SESSIONS, ("POST",)),
            (f"{SESSIONS}/{{session_id}}/turns", ("POST",)),
            (f"{SESSIONS}/{{session_id}}", ("GET",)),
            (f"{SESSIONS}/latest", ("GET",)),
            # #5331 blocker 4: derives a plan DOCUMENT and writes nothing. It is on
            # this router, under `PLAN_DRAFT`, because deriving a document is
            # authoring — the tests below are what hold it to that.
            (f"{SESSIONS}/{{session_id}}/plan", ("POST",)),
        }

    def test_there_is_no_route_that_accepts_a_plan(self):
        """An intake conversation's output stays inert until a human acts on it.

        Asserted on what the module IMPORTS and CALLS, parsed, rather than on whether
        those names appear anywhere in the source. A substring scan cannot tell a call
        from a comment explaining why the call must not be made — and this module has
        to explain that, since "derives a document, accepts nothing" is precisely the
        boundary a reader needs stated.

        Parsing is also strictly more sensitive than scanning, which is the reason to
        prefer it rather than merely a convenience: it resolves `from x import
        compile_proposal as helper`, an alias a substring check for the call site
        would miss entirely.
        """
        import ast
        import inspect

        from src.orchestration import intake_routes

        forbidden = {"compile_proposal", "accept_execution_policy", "stamp_policy", "apply_gate_answer"}
        tree = ast.parse(inspect.getsource(intake_routes))

        imported: set[str] = set()
        called: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                # The imported NAME, not the local alias, so renaming on import does
                # not launder it.
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.Call):
                target = node.func
                if isinstance(target, ast.Name):
                    called.add(target.id)
                elif isinstance(target, ast.Attribute):
                    called.add(target.attr)

        assert not (imported & forbidden), f"intake_routes.py imports {sorted(imported & forbidden)}"
        assert not (called & forbidden), f"intake_routes.py calls {sorted(called & forbidden)}"

    def test_the_planning_route_writes_no_plan_and_grants_no_policy(self):
        """The new route's safety, asserted on the response contract itself.

        It returns a document. A `LoopProposal` carrying an `execution_policy` out of
        here would be the conversation proposing bounds nobody reviewed, and the
        `wrote_nothing` / `execution_is_unbounded` defaults are what a client repeats
        to its user — so they are pinned rather than left to a docstring.
        """
        from src.orchestration.intake_routes import PlanFromSessionResponse

        fields = PlanFromSessionResponse.model_fields
        assert fields["wrote_nothing"].default is True
        assert fields["execution_is_unbounded"].default is True
        # No field on this response can carry a policy at all.
        assert "execution_policy" not in fields
        assert "proposed_execution_policy" not in fields

    @pytest.mark.parametrize("method,path", [("DELETE", f"{SESSIONS}/{SESSION}"), ("PUT", f"{SESSIONS}/{SESSION}")])
    def test_there_is_no_delete_or_replace(self, method, path):
        """Ending a conversation is not ending the work it planned.

        A route that looked like either would invite exactly the confusion the CLI's
        detach semantics exist to avoid.
        """
        http, _ = client(rows=[a_row()])
        assert http.request(method, path).status_code == 405


class TestDerivingAPlanFromARefinedIntent:
    """#5331 blocker 4, over HTTP rather than over the derivation's units.

    `test_planning.py` already holds `planning.py` to what it may and may not invent.
    What is asserted here is the part only the route can get wrong: that authorization
    and ownership are settled before a draft is read, that the repository reaching the
    resolver is checked against the AUTHENTICATED tenant's installations rather than
    the request's word for it, that the tenant and issue on the document come from
    server-held state, and that each refusal arrives as the status a client can act on.

    `_available_repositories` is replaced per test rather than stubbed at the
    connections service, because what these tests are about is the route's use of the
    answer — not how the answer is fetched, which `list_connections` owns and tests.

    It is replaced with `monkeypatch.setattr`, NOT with `dependency_overrides`: it is a
    plain function the handler awaits, not a FastAPI dependency, so an override entry
    for it is silently inert. Writing it that way first made every REFUSAL test here
    pass for the wrong reason — the real function raised on the harness's `None`
    session, was caught, and returned `[]`, which is itself a refusal. Only the one
    test asserting a repository is ACCEPTED could tell the difference, which is the
    argument for having it.
    """

    def _client(self, monkeypatch, *, installations=None, **kwargs):
        """The intake client with the connections read replaced by a fixed answer.

        Returns `[]` by default: that is the shape of "connections could not be read",
        and defaulting to it means a test asserting a repository is ACCEPTED has to say
        so explicitly rather than inheriting permission from the harness.
        """
        from src.orchestration import intake_routes

        async def _fixed(_current_user, _db):
            return list(installations or [])

        monkeypatch.setattr(intake_routes, "_available_repositories", _fixed)
        app, ingest_client = build_app(**kwargs)
        return TestClient(app, raise_server_exceptions=False), ingest_client

    @staticmethod
    def _draft(**overrides):
        draft = {"intent": "Cut checkout latency", "outcomes": ["p95 under 400ms", "No conversion regression"]}
        draft.update(overrides)
        return {f"session#{SESSION}": draft}

    def test_a_refined_draft_becomes_a_document_the_draft_routes_accept_unchanged(self, monkeypatch):
        """The whole point of blocker 4: the server produces the document, not the CLI.

        Asserted by validating the returned `proposal` with the server's OWN validator,
        because "it came back as JSON" is not the claim — the claim is that a client can
        post it straight to `/flows/drafts` without reassembling it.
        """
        from src.orchestration.proposal import LoopProposal, validate_proposal

        http, _ = self._client(monkeypatch, rows=[a_row()], drafts=self._draft())

        response = http.post(f"{SESSIONS}/{SESSION}/plan", json={})

        assert response.status_code == 200
        body = response.json()
        assert validate_proposal(LoopProposal.model_validate(body["proposal"])) == []
        # The user's own words, echoed, so an operator can see the stories were not invented.
        assert body["derived_from_outcomes"] == ["p95 under 400ms", "No conversion regression"]

    def test_the_tenant_on_the_document_is_the_authenticated_one(self, monkeypatch):
        """`compile_proposal` checks `org_id` against server-resolved context.

        Sourcing it from the caller's token rather than from the request is what makes
        the two unable to disagree. A request field here would be a tenant a caller
        could name.
        """
        http, _ = self._client(monkeypatch, rows=[a_row(org_id=ORG)], drafts=self._draft(), caller_org=ORG)

        body = http.post(f"{SESSIONS}/{SESSION}/plan", json={}).json()

        assert body["proposal"]["org_id"] == ORG
        # And there is no request field that could have set it.
        assert http.post(f"{SESSIONS}/{SESSION}/plan", json={"org_id": OTHER_ORG}).status_code == 422

    def test_a_caller_without_plan_draft_is_refused_before_the_draft_is_read(self, monkeypatch):
        """Authorization first, so a denied caller learns nothing about the session.

        Asserted as 403 rather than by inspecting the store, because the permission
        check runs before the reader is even called — a 404 here would mean the route
        had already looked the row up.
        """
        http, _ = self._client(monkeypatch, rows=[a_row()], drafts=self._draft(), permitted=False)

        assert http.post(f"{SESSIONS}/{SESSION}/plan", json={}).status_code == 403

    def test_somebody_elses_session_is_absent_rather_than_forbidden(self, monkeypatch):
        """A session id lands in logs and shell history.

        404 rather than 403 for the same reason the other verbs use it: 403 would
        confirm the session exists, which is itself the leak.
        """
        http, _ = self._client(monkeypatch, rows=[a_row(user_id=OWNER)], drafts=self._draft(), caller=INTRUDER)

        assert http.post(f"{SESSIONS}/{SESSION}/plan", json={}).status_code == 404

    def test_a_session_in_another_tenant_is_absent_too(self, monkeypatch):
        """One human, two orgs, one `user_id`: the tenant is the other half of ownership."""
        http, _ = self._client(monkeypatch, rows=[a_row(org_id=ORG)], drafts=self._draft(), caller_org=OTHER_ORG)

        assert http.post(f"{SESSIONS}/{SESSION}/plan", json={}).status_code == 404

    def test_a_repository_the_tenant_has_no_installation_for_is_403_not_422(self, monkeypatch):
        """The input is well-formed; the caller simply has no access to it.

        422 would tell them to fix their argument, and the argument is correct — the
        action is an operator connecting the repository. The refusal also has to land
        here rather than at dispatch, on a plan a human already approved.
        """
        http, _ = self._client(monkeypatch, rows=[a_row()], drafts=self._draft(), installations=[(11, ["acme/web"], True)])

        response = http.post(f"{SESSIONS}/{SESSION}/plan", json={"repository": "acme/not-connected"})

        assert response.status_code == 403
        assert response.json()["detail"]["error"] == "repository_not_connected"

    def test_a_repository_is_reported_as_the_installation_spells_it(self, monkeypatch):
        """Dispatch matches `repository_ids` verbatim, so case is not cosmetic.

        Echoing what the caller TYPED would hand a human a string to authorize that
        differs from the one admission later compares.
        """
        http, _ = self._client(monkeypatch, rows=[a_row()], drafts=self._draft(), installations=[(11, ["acme/web"], True)])

        body = http.post(f"{SESSIONS}/{SESSION}/plan", json={"repository": "ACME/Web"}).json()

        assert body["repository"] == "acme/web"
        assert body["repository_verified_live"] is True

    def test_an_unreadable_connection_list_refuses_the_repository_rather_than_accepting_it(self, monkeypatch):
        """The degraded path: `_available_repositories` returns `[]` when it cannot read.

        Failing open here — treating "I could not check" as "it is fine" — would make
        the resolution decorative, and silently, on exactly the path nobody exercises.
        """
        http, _ = self._client(monkeypatch, rows=[a_row()], drafts=self._draft(), installations=[])

        response = http.post(f"{SESSIONS}/{SESSION}/plan", json={"repository": "acme/web"})

        assert response.status_code == 403

    def test_no_repository_requested_is_a_plan_not_a_refusal(self, monkeypatch):
        """Nor a guess. Defaulting to "the only one they have" would be a grant nobody
        asked for, and would pick differently the day a second is connected."""
        http, _ = self._client(monkeypatch, rows=[a_row()], drafts=self._draft(), installations=[(11, ["acme/web"], True)])

        body = http.post(f"{SESSIONS}/{SESSION}/plan", json={}).json()

        assert body["repository"] == ""
        assert body["repository_verified_live"] is False

    def test_the_conversations_own_issue_wins_over_a_client_supplied_one(self, monkeypatch):
        """The session's `issue_ref` is what the planning agent actually opened.

        Preferring the request would let a retry carrying a different number re-bind the
        plan to an issue the conversation never touched — a silent change of target
        between two calls a caller believes are the same.
        """
        http, _ = self._client(monkeypatch, rows=[a_row(threads={"th-1": a_thread(issue_number="5331")})], drafts=self._draft())

        body = http.post(f"{SESSIONS}/{SESSION}/plan", json={"issue": "9999"}).json()

        assert body["issue_ref"] == "5331"
        assert {node["issue_ref"] for node in body["proposal"]["nodes"]} == {"5331"}

    def test_an_issue_reference_the_dispatch_parse_rejects_is_422_here(self, monkeypatch):
        """Otherwise the story nodes are born `malformed_issue_ref`, which surfaces only
        as a dispatch block on a plan already written and approved."""
        http, _ = self._client(monkeypatch, rows=[a_row()], drafts=self._draft())

        response = http.post(f"{SESSIONS}/{SESSION}/plan", json={"issue": "not-a-number"})

        assert response.status_code == 422
        assert response.json()["detail"]["error"] == "malformed_issue_ref"

    def test_a_draft_with_no_outcomes_yet_is_409_keep_refining(self, monkeypatch):
        """409, distinctly from 422: nothing is wrong with the request.

        The conversation is simply not finished, and retrying this call changes nothing
        until the draft does — which is a different instruction to the user than "fix
        your argument".
        """
        http, _ = self._client(monkeypatch, rows=[a_row()], drafts=self._draft(outcomes=[]))

        response = http.post(f"{SESSIONS}/{SESSION}/plan", json={})

        assert response.status_code == 409
        assert response.json()["detail"]["error"] == "draft_not_ready"

    def test_a_deployment_that_cannot_read_drafts_says_so_rather_than_saying_not_ready(self, monkeypatch):
        """503 vs 409 is the difference between a configuration problem and a young
        conversation. Reported as 409 it would send the operator to refine forever."""
        # An EMPTY draft store reads as "no draft", which is the 409 above. So
        # unavailability is induced where it really originates: a draft table the
        # reader cannot read at all.
        from src.orchestration.intake_session import IntakeSessionReader

        class UnreadableDrafts:
            def get_item(self, **kwargs):
                raise BedrockGatewayError("draft table is not configured")

        from src.orchestration import intake_routes

        http, _ = self._client(monkeypatch, rows=[a_row()])
        http.app.dependency_overrides[intake_routes._session_reader] = lambda: IntakeSessionReader(StubStore([a_row()]), UnreadableDrafts())

        response = http.post(f"{SESSIONS}/{SESSION}/plan", json={})

        assert response.status_code == 503
        assert response.json()["detail"]["error"] == "draft_unavailable"

    def test_deriving_twice_produces_the_same_document(self, monkeypatch):
        """What makes a lost response safe to retry.

        The registration path's `already_registered` idempotency keys on `plan_hash`, so
        a derivation that moved between two calls would turn one caller's dropped
        connection into two flows for one intent, the first orphaned and invisible.
        """
        from src.orchestration.compile import plan_hash
        from src.orchestration.proposal import LoopProposal

        http, _ = self._client(monkeypatch, rows=[a_row()], drafts=self._draft())

        first = http.post(f"{SESSIONS}/{SESSION}/plan", json={}).json()["proposal"]
        second = http.post(f"{SESSIONS}/{SESSION}/plan", json={}).json()["proposal"]

        assert plan_hash(LoopProposal.model_validate(first)) == plan_hash(LoopProposal.model_validate(second))

    def test_deriving_a_plan_dispatches_no_conversation_turn(self, monkeypatch):
        """This route reads; it does not talk to the agent.

        `ingest.calls == []` is the strong form of "nothing happened" on this router: an
        invocation creates a session, a thread, a transcript entry and a run.
        """
        http, ingest = self._client(monkeypatch, rows=[a_row()], drafts=self._draft())

        http.post(f"{SESSIONS}/{SESSION}/plan", json={})

        assert ingest.calls == []

    def test_the_returned_document_carries_no_policy_in_either_field(self, monkeypatch):
        """Over the wire, not just on the model: bounds here would be the conversation
        proposing authority nobody reviewed."""
        http, _ = self._client(monkeypatch, rows=[a_row()], drafts=self._draft())

        body = http.post(f"{SESSIONS}/{SESSION}/plan", json={}).json()

        assert body["proposal"].get("execution_policy") is None
        assert body["proposal"].get("proposed_execution_policy") is None
        assert body["wrote_nothing"] is True
        assert body["execution_is_unbounded"] is True
