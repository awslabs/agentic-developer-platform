"""Durable intake state and its ownership rule (#5331, EPIC #4191).

The reason this module is tested hard for its size: a session id is an opaque
bearer-ish string that lands in logs and shell history, so if possession of one were
enough to read a conversation, every leaked id would be a disclosure of somebody
else's planning discussion — including the draft and the issue it opened.

Four properties carry it:

* :class:`TestOwnershipIsReadFromTheRow` — authority comes off the stored row, never
  the request, and a foreign session is *not found* rather than *forbidden*.
* :class:`TestTheTenantIsPartOfOwnership` — the tenant is compared too, because
  `user_workspace` carries none and one human is not one principal.
* :class:`TestResumeFindsTheRightConversation` — by-user lookup returns the newest
  conversation, which is what `--resume` depends on when the caller never held the
  browser-minted id.
* :class:`TestStateIsReadFromTheShapesProductionWrites` — every projected field is
  traced to the writer that sets it, across BOTH tables. The failure this guards is
  not a crash: it is a readback that returns a plausible empty value, so a user is
  told their conversation captured nothing.

The stores are stubs throughout. Asserting authorization against a live table would
leave exactly the deny paths untested, since they are the ones that must not be
reachable. `StubDraftStore` models the chat-context table separately because the
draft genuinely lives in a different table under a different key schema.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from src.orchestration.intake_session import (
    INTAKE_CHANNEL,
    IntakeSessionReader,
    IntakeUnavailableError,
    SessionNotFoundError,
)

OWNER = "user-alpha"
INTRUDER = "user-beta"
SESSION = "sess-1700000000-abc1234"
TENANT = "org-alpha"
OTHER_TENANT = "org-beta"


def a_row(
    *,
    session_id: str = SESSION,
    user_id: str = OWNER,
    org_id: str | None = TENANT,
    updated_at: Any = 1_700_000_000,
    **overrides: Any,
) -> dict[str, Any]:
    """A sessions-table row shaped as the agent-factory ingest Lambda writes one.

    `user_workspace` is the real composite the table's GSI is keyed on, not a
    convenience field invented here: if this fixture and the Lambda disagreed, these
    tests would pass against a schema that does not exist.

    `org_id` is the tenant the ingest Lambda stamps on creation. Pass `None` to build
    a row that predates that stamp — the legacy shape whose refusal is asserted
    below.
    """
    row: dict[str, Any] = {
        "session_id": session_id,
        "user_workspace": f"{user_id}#{INTAKE_CHANNEL}",
        # DynamoDB hands numbers back as Decimal. Kept in the fixture so the
        # coercion is exercised by every test rather than by one that remembers to.
        # Wrapped only for values `Decimal` accepts, so a test may pass a deliberately
        # corrupt attribute through untouched instead of tripping over the fixture.
        "updated_at": Decimal(updated_at) if isinstance(updated_at, int) else updated_at,
    }
    if org_id is not None:
        row["org_id"] = org_id
    row.update(overrides)
    return row


def a_thread(
    *,
    processing_task_id: str = "",
    created_at: int = 1_700_000_000,
    issue_number: str = "",
    **overrides: Any,
) -> dict[str, Any]:
    """A thread as `create_thread` writes one into the row's `threads` MAP.

    `processing_task_id` defaults to `""` rather than being absent, because that is
    what `_clear_thread_processing` leaves behind — it writes an empty string rather
    than REMOVEing the key, so "idle" in production means present-and-empty.

    `github_issue_number` sits HERE, inside the thread, which is the only place any
    production writer puts it.
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
        thread["github_issue_url"] = f"https://github.com/aws-e/adp/issues/{issue_number}"
    thread.update(overrides)
    return thread


class StubDraftStore:
    """The chat-context table, holding drafts at the key the worker writes them to.

    A SEPARATE stub from `StubStore` because the draft is in a separate table, keyed
    `PK`/`SK` where the sessions table is keyed `session_id`. Records the keys it was
    asked for, so a test can assert the reader addressed the real key rather than
    merely that it got an empty result — which it would also get from a wrong key.
    """

    def __init__(self, drafts: dict[str, dict[str, Any]] | None = None, *, error: Exception | None = None) -> None:
        self._drafts = drafts or {}
        self._error = error
        self.keys: list[dict[str, Any]] = []

    def get_item(self, **kwargs: Any) -> dict[str, Any]:
        self.keys.append(kwargs["Key"])
        if self._error:
            raise self._error
        key = kwargs["Key"]
        if key.get("SK") != "draft":
            # A wrong sort key finds nothing, exactly as DynamoDB would. Modelled so a
            # reader that guessed the key fails a test instead of quietly reporting an
            # empty draft.
            return {}
        draft = self._drafts.get(str(key.get("PK", "")))
        return {"Item": {"PK": key["PK"], "SK": "draft", "draft": draft}} if draft is not None else {}


class StubStore:
    """A sessions table returning fixed rows, recording what was asked of it."""

    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self._rows = rows or []
        self.updates: list[dict[str, Any]] = []
        self.queries: list[dict[str, Any]] = []

    def get_item(self, **kwargs: Any) -> dict[str, Any]:
        wanted = kwargs["Key"]["session_id"]
        for row in self._rows:
            if row.get("session_id") == wanted:
                return {"Item": row}
        return {}

    def query(self, **kwargs: Any) -> dict[str, Any]:
        self.queries.append(kwargs)
        workspace = kwargs["ExpressionAttributeValues"][":workspace"]
        return {"Items": [row for row in self._rows if row.get("user_workspace") == workspace]}

    def update_item(self, **kwargs: Any) -> dict[str, Any]:
        self.updates.append(kwargs)
        return {}


class TestOwnershipIsReadFromTheRow:
    """Authority comes off the stored row, never off the request."""

    def test_the_owner_can_read_their_own_session(self):
        drafts = StubDraftStore({f"session#{SESSION}": {"intent": "Ship the CLI"}})
        reader = IntakeSessionReader(StubStore([a_row()]), drafts)
        session = reader.get(session_id=SESSION, user_id=OWNER, org_id=TENANT)
        assert session.session_id == SESSION
        assert session.user_id == OWNER
        # From the context table, not the row — see
        # `TestStateIsReadFromTheShapesProductionWrites`.
        assert session.draft == {"intent": "Ship the CLI"}

    def test_another_users_session_is_reported_as_absent_not_forbidden(self):
        """The distinction matters: *forbidden* confirms the session exists.

        A caller who has guessed or scraped an id must not be able to use this
        surface to learn that it is real, so both cases raise the same error.
        """
        reader = IntakeSessionReader(StubStore([a_row()]))
        with pytest.raises(SessionNotFoundError):
            reader.get(session_id=SESSION, user_id=INTRUDER, org_id=TENANT)

    def test_a_missing_session_raises_the_same_error_as_a_foreign_one(self):
        reader = IntakeSessionReader(StubStore([a_row()]))
        with pytest.raises(SessionNotFoundError) as missing:
            reader.get(session_id="sess-does-not-exist", user_id=OWNER, org_id=TENANT)
        with pytest.raises(SessionNotFoundError) as foreign:
            reader.get(session_id=SESSION, user_id=INTRUDER, org_id=TENANT)
        # Same type AND same message: a caller must not be able to tell the two
        # apart by string-matching either.
        assert type(missing.value) is type(foreign.value)
        assert str(missing.value) == str(foreign.value)

    def test_an_empty_caller_identity_is_refused_rather_than_matched(self):
        """Fail closed: an empty `user_id` must not match a malformed row.

        A row whose `user_workspace` is absent projects to an empty owner, so an
        unauthenticated caller reaching here with no identity would otherwise read
        it.
        """
        reader = IntakeSessionReader(StubStore([{"session_id": SESSION}]))
        with pytest.raises(SessionNotFoundError):
            reader.get(session_id=SESSION, user_id="", org_id=TENANT)

    def test_an_unconfigured_deployment_is_unavailable_not_empty(self):
        """ "Cannot plan here" and "you have no sessions" are different facts.

        Reporting the first as the second would send an operator looking for their
        conversation instead of at their deployment.
        """
        reader = IntakeSessionReader(None)
        with pytest.raises(IntakeUnavailableError):
            reader.get(session_id=SESSION, user_id=OWNER, org_id=TENANT)
        with pytest.raises(IntakeUnavailableError):
            reader.latest_for_user(user_id=OWNER, org_id=TENANT)


class TestAMisconfiguredStoreIsUnavailableNotAbsent:
    """A table the reader cannot read is an OPERATOR's problem, and must say so.

    This is the failure mode the deploy wiring actually makes likely, which is why it
    is tested rather than left to the generic 500. `BG_INTAKE_SESSIONS_TABLE` is
    rendered from SSM with a convention-derived default, and the IAM grant is what
    truly gates access — so the two realistic misconfigurations are a table name that
    does not resolve (`ResourceNotFoundException`) and a grant that does not cover it
    (`AccessDeniedException`). Both surface here as a boto3 `ClientError` from the
    store call.

    Neither is `SessionNotFoundError`. Letting them become one would report "no such
    intake session" for a deployment whose intake is misconfigured, which is the
    single most misleading answer available: the operator is told the conversation
    does not exist, the user is told to start a new one, and the actual cause — a
    table name or a missing permission — is named nowhere. `IntakeUnavailableError`
    is the honest answer and the routes already map it to 503, the same code an
    unconfigured deployment gets, because the required action is the same.

    Distinct from the draft store, which deliberately degrades to
    `draft_available: false` instead of raising: there the rest of the conversation
    is still readable, whereas a failed sessions read leaves nothing to return.
    """

    @staticmethod
    def _client_error(code: str) -> Exception:
        """A boto3 `ClientError` as the real `Table` would raise it.

        Constructed from botocore rather than a bare `Exception` so this test pins
        the type production actually raises; a stand-in would let a handler that
        catches something narrower still pass.
        """
        from botocore.exceptions import ClientError

        return ClientError({"Error": {"Code": code, "Message": code}}, "GetItem")

    class _FailingStore:
        def __init__(self, error: Exception) -> None:
            self._error = error

        def get_item(self, **kwargs: Any) -> dict[str, Any]:
            raise self._error

        def query(self, **kwargs: Any) -> dict[str, Any]:
            raise self._error

    @pytest.mark.parametrize("code", ["AccessDeniedException", "ResourceNotFoundException"])
    def test_a_failed_session_read_is_unavailable_not_a_missing_session(self, code):
        reader = IntakeSessionReader(self._FailingStore(self._client_error(code)))
        with pytest.raises(IntakeUnavailableError):
            reader.get(session_id=SESSION, user_id=OWNER, org_id=TENANT)

    @pytest.mark.parametrize("code", ["AccessDeniedException", "ResourceNotFoundException"])
    def test_a_failed_resume_query_is_unavailable_not_an_empty_result(self, code):
        """`latest_for_user` returns `None` for "you have no sessions".

        So a swallowed failure here is worse than on `get`: `None` is a SUCCESSFUL
        answer that the route turns into a 404, telling a user with a live
        conversation that they have never started one — and inviting them to start a
        second while the first is unreachable.
        """
        reader = IntakeSessionReader(self._FailingStore(self._client_error(code)))
        with pytest.raises(IntakeUnavailableError):
            reader.latest_for_user(user_id=OWNER, org_id=TENANT)

    def test_the_unavailable_message_does_not_claim_the_deployment_is_unconfigured(self):
        """Two different 503s, and an operator fixes them differently.

        "Not configured" sends someone to the deploy wiring; a table that exists but
        cannot be read sends them to the IAM policy. Reusing the unconfigured wording
        for a broken read would send them to check an env var that is already set.
        """
        reader = IntakeSessionReader(self._FailingStore(self._client_error("AccessDeniedException")))
        with pytest.raises(IntakeUnavailableError) as failed:
            reader.get(session_id=SESSION, user_id=OWNER, org_id=TENANT)
        unconfigured = IntakeSessionReader(None)
        with pytest.raises(IntakeUnavailableError) as absent:
            unconfigured.get(session_id=SESSION, user_id=OWNER, org_id=TENANT)
        assert str(failed.value) != str(absent.value)
        assert "not configured" not in str(failed.value)

    def test_a_failed_read_does_not_reach_the_draft(self):
        """Ordering, not just the error type.

        The draft store swallows its own failures, so a reader that fetched the draft
        before settling the session read could turn a failed sessions read into a
        session-shaped answer with an empty draft.
        """
        drafts = StubDraftStore({f"session#{SESSION}": {"intent": "Ship the CLI"}})
        reader = IntakeSessionReader(self._FailingStore(self._client_error("AccessDeniedException")), drafts)
        with pytest.raises(IntakeUnavailableError):
            reader.get(session_id=SESSION, user_id=OWNER, org_id=TENANT)
        assert drafts.keys == []


class TestTheTenantIsPartOfOwnership:
    """One human is not one principal: the same user in two tenants is two callers.

    This is the property that separates a user's workspaces on this surface. It needs
    its own class because every other test in this file uses a single tenant, and a
    user-only comparison passes all of them — which is exactly how the gap reached
    CI green.
    """

    def test_the_same_user_cannot_read_their_other_tenants_session(self):
        """The audit's case, and the reason this class exists.

        `user_id` matches; only the tenant differs. A reader comparing the user alone
        returns the conversation — its transcript, its draft and the issue it opened —
        to a caller authenticated into a different workspace.
        """
        reader = IntakeSessionReader(StubStore([a_row(user_id=OWNER, org_id=TENANT, draft={"intent": "Tenant A's plan"})]))
        with pytest.raises(SessionNotFoundError):
            reader.get(session_id=SESSION, user_id=OWNER, org_id=OTHER_TENANT)

    def test_a_cross_tenant_read_is_absent_not_forbidden(self):
        """Same indistinguishability the foreign-user case already promises.

        *Forbidden* would confirm that a conversation exists under another tenant,
        which is itself information a caller should not get from this surface.
        """
        reader = IntakeSessionReader(StubStore([a_row()]))
        with pytest.raises(SessionNotFoundError) as cross_tenant:
            reader.get(session_id=SESSION, user_id=OWNER, org_id=OTHER_TENANT)
        with pytest.raises(SessionNotFoundError) as missing:
            reader.get(session_id="sess-does-not-exist", user_id=OWNER, org_id=TENANT)
        assert type(cross_tenant.value) is type(missing.value)
        assert str(cross_tenant.value) == str(missing.value)

    def test_resume_does_not_cross_tenants_even_though_the_index_cannot_filter(self):
        """`user_workspace` is `user#channel`, so the GSI returns BOTH tenants' rows.

        This is the more likely exposure of the two: it needs no leaked session id at
        all, just the same person authenticated into a different workspace, and the
        other tenant's row is returned whenever it is the more recently updated one.
        """
        store = StubStore(
            [
                a_row(session_id="sess-other-tenant", user_id=OWNER, org_id=OTHER_TENANT, updated_at=1_700_009_999),
                a_row(session_id="sess-my-tenant", user_id=OWNER, org_id=TENANT, updated_at=1_700_000_500),
            ]
        )
        latest = IntakeSessionReader(store).latest_for_user(user_id=OWNER, org_id=TENANT)
        assert latest is not None
        assert latest.session_id == "sess-my-tenant"

    def test_resume_finds_nothing_when_only_another_tenants_session_exists(self):
        store = StubStore([a_row(session_id="sess-other-tenant", user_id=OWNER, org_id=OTHER_TENANT)])
        assert IntakeSessionReader(store).latest_for_user(user_id=OWNER, org_id=TENANT) is None

    def test_a_caller_with_no_tenant_is_refused_rather_than_matched(self):
        """Fail closed, symmetrically with the empty-`user_id` rule.

        An empty `org_id` must not become a wildcard that matches a row whose tenant
        is also unrecorded.
        """
        reader = IntakeSessionReader(StubStore([a_row(org_id=None)]))
        with pytest.raises(SessionNotFoundError):
            reader.get(session_id=SESSION, user_id=OWNER, org_id="")
        assert reader.latest_for_user(user_id=OWNER, org_id="") is None

    def test_a_row_with_no_recorded_tenant_belongs_to_nobody(self):
        """Legacy rows predate the stamp and cannot prove their tenant.

        The safe reading of "unknown tenant" is "not yours". Treating an absent value
        as a wildcard would leave exactly the legacy rows cross-tenant readable, which
        inverts the fix. Such a session is unreachable, not exposed, and expires on
        the table's TTL.
        """
        reader = IntakeSessionReader(StubStore([a_row(org_id=None)]))
        with pytest.raises(SessionNotFoundError):
            reader.get(session_id=SESSION, user_id=OWNER, org_id=TENANT)
        assert reader.latest_for_user(user_id=OWNER, org_id=TENANT) is None

    def test_the_tenant_is_required_rather_than_defaulted(self):
        """A default would let a future call site silently fall back to user-only.

        Asserted on the signature because that is the guard: omitting the argument
        must be a `TypeError` here and in review, not a quiet cross-tenant read in
        production.
        """
        reader = IntakeSessionReader(StubStore([a_row()]))
        with pytest.raises(TypeError):
            reader.get(session_id=SESSION, user_id=OWNER)  # type: ignore[call-arg]
        with pytest.raises(TypeError):
            reader.latest_for_user(user_id=OWNER)  # type: ignore[call-arg]


class TestResumeFindsTheRightConversation:
    """By-user lookup, because the caller may never have held the browser's id."""

    def test_the_newest_session_is_returned(self):
        reader = IntakeSessionReader(
            StubStore(
                [
                    a_row(session_id="sess-old", updated_at=1_700_000_000),
                    a_row(session_id="sess-new", updated_at=1_700_009_999),
                    a_row(session_id="sess-mid", updated_at=1_700_005_000),
                ]
            )
        )
        latest = reader.latest_for_user(user_id=OWNER, org_id=TENANT)
        assert latest is not None
        assert latest.session_id == "sess-new"

    def test_newest_is_decided_by_timestamp_not_by_index_order(self):
        """The GSI's range key is `session_id`, which is not chronological.

        Rows are deliberately supplied with the newest FIRST so a "take the last
        item" implementation passes the test above and fails here.
        """
        reader = IntakeSessionReader(
            StubStore(
                [
                    a_row(session_id="sess-zzz-newest", updated_at=1_700_009_999),
                    a_row(session_id="sess-aaa-oldest", updated_at=1_700_000_001),
                ]
            )
        )
        latest = reader.latest_for_user(user_id=OWNER, org_id=TENANT)
        assert latest is not None
        assert latest.session_id == "sess-zzz-newest"

    def test_another_users_sessions_are_never_returned(self):
        reader = IntakeSessionReader(StubStore([a_row(user_id=INTRUDER, session_id="sess-theirs")]))
        assert reader.latest_for_user(user_id=OWNER, org_id=TENANT) is None

    def test_the_lookup_uses_the_existing_index_rather_than_a_scan(self):
        """A scan would read every tenant's rows and cost the whole table per resume."""
        store = StubStore([a_row()])
        IntakeSessionReader(store).latest_for_user(user_id=OWNER, org_id=TENANT)
        assert store.queries, "resume must query, not scan"
        assert store.queries[0]["IndexName"] == "user-workspace-index"
        assert store.queries[0]["ExpressionAttributeValues"][":workspace"] == f"{OWNER}#{INTAKE_CHANNEL}"

    def test_a_user_with_no_sessions_gets_none_rather_than_an_error(self):
        assert IntakeSessionReader(StubStore([])).latest_for_user(user_id=OWNER, org_id=TENANT) is None

    def test_a_foreign_row_is_dropped_even_if_the_store_returns_it(self):
        """The ownership filter must not rely on the query having scoped correctly.

        `StubStore.query` filters by workspace, exactly as the real GSI does — which
        means it cannot exercise the in-code owner check at all: removing that check
        leaves every other test in this file green. This store answers the query
        *unfiltered*, standing in for the ways that assumption can break in
        production (a wrong `IndexName`, a GSI whose projection or key was changed, a
        future caller reusing this reader over a different query).

        Without this test the filter is dead code by coverage and a refactor would
        delete it as redundant. With it, "ownership is checked on every path" is a
        tested property rather than a comment.
        """

        class UnfilteredStore(StubStore):
            def query(self, **kwargs: Any) -> dict[str, Any]:
                self.queries.append(kwargs)
                return {"Items": list(self._rows)}

        store = UnfilteredStore([a_row(user_id=INTRUDER, session_id="sess-theirs", updated_at=1_700_009_999)])
        assert IntakeSessionReader(store).latest_for_user(user_id=OWNER, org_id=TENANT) is None

    def test_only_the_callers_rows_survive_a_mixed_unfiltered_page(self):
        """The caller's own newest row is still found when foreign rows outrank it."""

        class UnfilteredStore(StubStore):
            def query(self, **kwargs: Any) -> dict[str, Any]:
                self.queries.append(kwargs)
                return {"Items": list(self._rows)}

        store = UnfilteredStore(
            [
                a_row(user_id=INTRUDER, session_id="sess-theirs-newest", updated_at=1_700_009_999),
                a_row(user_id=OWNER, session_id="sess-mine", updated_at=1_700_000_500),
            ]
        )
        latest = IntakeSessionReader(store).latest_for_user(user_id=OWNER, org_id=TENANT)
        assert latest is not None
        assert latest.session_id == "sess-mine"


class TestStateIsReadFromTheShapesProductionWrites:
    """Every field traced to its production writer.

    The cost of guessing here is not a crash — it is a readback that returns a
    plausible empty value, so a client renders "nothing captured yet" over a
    fully-formed conversation and the user concludes their answers were discarded.
    An earlier revision of this module read three attributes no production writer
    sets, and passed its own tests, because its only writer was itself.
    """

    def test_the_draft_comes_from_the_context_table_not_the_session_row(self):
        """The defect: the draft is in a DIFFERENT table.

        The only writer is the worker's `DynamoDraftStore`, which puts it in
        `adp-<env>-chat-context`. A `get_item` against the sessions table cannot
        reach it at any key, so projecting `draft` off the row returned `{}` for
        every conversation that had one.
        """
        drafts = StubDraftStore({f"session#{SESSION}": {"intent": "Ship the CLI"}})
        reader = IntakeSessionReader(StubStore([a_row()]), drafts)
        session = reader.get(session_id=SESSION, user_id=OWNER, org_id=TENANT)
        assert session.draft == {"intent": "Ship the CLI"}
        assert session.draft_available is True

    def test_the_draft_is_addressed_at_the_key_the_worker_writes(self):
        """A wrong key fails silently, so the key itself is asserted.

        `PK=session#<id>, SK=draft` is `DRAFT_SK` in `dynamo-draft-store.ts`. Any
        other key returns no Item, which is indistinguishable from "no draft yet" —
        which is exactly how a key mismatch would survive a coarser test.
        """
        drafts = StubDraftStore({f"session#{SESSION}": {"intent": "Ship the CLI"}})
        IntakeSessionReader(StubStore([a_row()]), drafts).get(session_id=SESSION, user_id=OWNER, org_id=TENANT)
        assert drafts.keys == [{"PK": f"session#{SESSION}", "SK": "draft"}]

    def test_a_draft_that_cannot_be_read_is_flagged_not_reported_as_empty(self):
        """`{}` is ambiguous; the flag disambiguates it.

        "No draft yet" and "this deployment cannot read drafts" are the same empty
        object. A client shown the first for the second tells a user their answers
        were discarded, which is the more alarming of the two by far.
        """
        session = IntakeSessionReader(StubStore([a_row()]), None).get(session_id=SESSION, user_id=OWNER, org_id=TENANT)
        assert session.draft == {}
        assert session.draft_available is False

    def test_a_failing_draft_store_still_yields_a_readable_conversation(self):
        """Fail soft, not closed. The transcript and the issue are still useful.

        Raising would make a mis-scoped IAM grant on ONE table take out the whole
        readback, including the in-flight check a client needs before sending.
        """
        drafts = StubDraftStore(error=RuntimeError("AccessDeniedException"))
        session = IntakeSessionReader(StubStore([a_row(threads={"t-1": a_thread(issue_number="5331")})]), drafts).get(
            session_id=SESSION, user_id=OWNER, org_id=TENANT
        )
        assert session.draft_available is False
        assert session.issue_ref == "5331", "the rest of the conversation must survive"

    def test_a_foreign_session_is_refused_before_its_draft_is_read(self):
        """Ordering, not just the outcome.

        The draft is the most sensitive thing here — a user's unshaped thinking — so
        a foreign session must not cause a read of it at all. Asserted on the store
        having been left untouched, because a later filter cannot un-read a row.
        """
        drafts = StubDraftStore({f"session#{SESSION}": {"intent": "secret"}})
        reader = IntakeSessionReader(StubStore([a_row(user_id=OWNER)]), drafts)
        with pytest.raises(SessionNotFoundError):
            reader.get(session_id=SESSION, user_id=INTRUDER, org_id=TENANT)
        assert drafts.keys == [], "a foreign session must not trigger a draft read"

    def test_working_is_read_from_the_per_thread_lock(self):
        """The defect: there is no row-level `status` in production.

        The lock the ingest and response Lambdas maintain is
        `threads.<id>.processing_task_id`. A reader looking for a top-level `status`
        found nothing and reported every conversation idle — so a client would send
        into a turn the agent still held.
        """
        working = IntakeSessionReader(StubStore([a_row(threads={"t-1": a_thread(processing_task_id="task-9")})]), None)
        assert working.get(session_id=SESSION, user_id=OWNER, org_id=TENANT).is_working is True

    def test_an_empty_task_id_is_the_cleared_state_not_a_held_lock(self):
        """`_clear_thread_processing` writes `""` rather than REMOVEing the key.

        So idle is present-and-empty, and a key-presence test would report every
        finished conversation as permanently working — a client could then never
        send a second turn.
        """
        idle = IntakeSessionReader(StubStore([a_row(threads={"t-1": a_thread(processing_task_id="")})]), None)
        assert idle.get(session_id=SESSION, user_id=OWNER, org_id=TENANT).is_working is False

    def test_a_conversation_with_no_threads_is_idle(self):
        """The shape between session creation and the first thread."""
        assert IntakeSessionReader(StubStore([a_row(threads={})]), None).get(session_id=SESSION, user_id=OWNER, org_id=TENANT).is_working is False

    def test_any_held_thread_makes_the_conversation_working(self):
        """ANY, not the newest.

        A stale lock on an older thread still holds that session's FIFO message
        group, so a new turn would queue behind it rather than being answered.
        """
        rows = [a_row(threads={"t-old": a_thread(processing_task_id="stuck", created_at=1_700_000_000), "t-new": a_thread(created_at=1_700_000_900)})]
        assert IntakeSessionReader(StubStore(rows), None).get(session_id=SESSION, user_id=OWNER, org_id=TENANT).is_working is True

    def test_the_issue_is_read_from_the_thread_that_opened_it(self):
        """The defect: `github_issue_number` is only ever written inside a thread.

        Carried so a resume does not open a second issue — the duplicate-issue
        failure mode that makes a naive client-side retry unsafe.
        """
        rows = [a_row(threads={"t-1": a_thread(issue_number="5331")})]
        assert IntakeSessionReader(StubStore(rows), None).get(session_id=SESSION, user_id=OWNER, org_id=TENANT).issue_ref == "5331"

    def test_the_issue_is_found_on_an_older_thread_too(self):
        """A follow-up turn creates a new thread; the issue belongs to the conversation."""
        rows = [a_row(threads={"t-1": a_thread(issue_number="5331", created_at=1_700_000_000), "t-2": a_thread(created_at=1_700_000_900)})]
        assert IntakeSessionReader(StubStore(rows), None).get(session_id=SESSION, user_id=OWNER, org_id=TENANT).issue_ref == "5331"

    def test_open_questions_come_from_the_draft_the_persona_maintains(self):
        """So a script learns an answer is required without parsing English.

        These are `IntentDraft.openQuestions` — draft content, because that is where
        the persona is instructed to put them. There is no session-level question
        attribute in production, and the one this module used to read was a field
        only this module ever wrote.
        """
        drafts = StubDraftStore({f"session#{SESSION}": {"intent": "Ship it", "openQuestions": ["Which repo?", "Which account scope?"]}})
        session = IntakeSessionReader(StubStore([a_row()]), drafts).get(session_id=SESSION, user_id=OWNER, org_id=TENANT)
        assert session.open_questions == ["Which repo?", "Which account scope?"]
        assert session.is_awaiting_answer is True

    def test_a_conversation_with_no_open_questions_is_not_awaiting_an_answer(self):
        drafts = StubDraftStore({f"session#{SESSION}": {"intent": "Ship it"}})
        session = IntakeSessionReader(StubStore([a_row()]), drafts).get(session_id=SESSION, user_id=OWNER, org_id=TENANT)
        assert session.open_questions == []
        assert session.is_awaiting_answer is False

    def test_blank_open_questions_do_not_count_as_waiting(self):
        """A model that emitted an empty string must not block the conversation forever."""
        drafts = StubDraftStore({f"session#{SESSION}": {"openQuestions": ["", "   "]}})
        session = IntakeSessionReader(StubStore([a_row()]), drafts).get(session_id=SESSION, user_id=OWNER, org_id=TENANT)
        assert session.open_questions == []
        assert session.is_awaiting_answer is False

    def test_a_malformed_open_questions_value_does_not_break_the_readback(self):
        """The draft is model-authored, so its fields are not schema-guaranteed."""
        drafts = StubDraftStore({f"session#{SESSION}": {"openQuestions": "Which repo?"}})
        session = IntakeSessionReader(StubStore([a_row()]), drafts).get(session_id=SESSION, user_id=OWNER, org_id=TENANT)
        assert session.open_questions == []

    def test_open_questions_during_a_turn_are_working_not_waiting(self):
        """Both halves of `awaiting_answer` matter.

        Open questions alone do not mean it is the user's turn — the agent may be
        mid-answer and about to resolve them itself. Reporting "waiting on you" here
        would make a script answer a question already being answered, and that
        answer would interleave with the reply being written.
        """
        drafts = StubDraftStore({f"session#{SESSION}": {"openQuestions": ["Which repo?"]}})
        rows = [a_row(threads={"t-1": a_thread(processing_task_id="task-9")})]
        session = IntakeSessionReader(StubStore(rows), drafts).get(session_id=SESSION, user_id=OWNER, org_id=TENANT)
        assert session.is_working is True
        assert session.is_awaiting_answer is False, "a turn in flight is not the user's turn"

    def test_resume_reads_the_draft_of_the_session_it_returns(self):
        """`latest_for_user` must resolve the draft too, or resume shows an empty one."""
        store = StubStore(
            [
                a_row(session_id="sess-old", updated_at=1_700_000_100),
                a_row(session_id="sess-new", updated_at=1_700_000_900),
            ]
        )
        drafts = StubDraftStore({"session#sess-new": {"intent": "The newest"}, "session#sess-old": {"intent": "The older"}})
        latest = IntakeSessionReader(store, drafts).latest_for_user(user_id=OWNER, org_id=TENANT)
        assert latest is not None
        assert latest.session_id == "sess-new"
        assert latest.draft == {"intent": "The newest"}

    def test_resume_reads_one_draft_not_one_per_candidate_row(self):
        """Cost, and it is the call a watching CLI repeats.

        Fetching a draft per candidate would multiply a resume by the user's session
        count to answer a question about exactly one of them.
        """
        store = StubStore([a_row(session_id=f"sess-{n}", updated_at=1_700_000_000 + n) for n in range(5)])
        drafts = StubDraftStore({})
        IntakeSessionReader(store, drafts).latest_for_user(user_id=OWNER, org_id=TENANT)
        assert len(drafts.keys) == 1

    def test_there_is_no_writer_for_state_production_owns(self):
        """This module reads. It does not invent state the agent is responsible for.

        The removed `record_pending_question` / `clear_pending_question` are the
        reason this test exists: a readback with its own writers can satisfy its own
        tests forever while production never sets the field. Asserted as absence so
        reintroducing one is a failing test, not a silent regression.
        """
        for forbidden in ("record_pending_question", "clear_pending_question", "update_item"):
            assert not hasattr(IntakeSessionReader, forbidden), f"{forbidden} writes state this module does not own"

    def test_rows_missing_newer_fields_still_project(self):
        """The agent-factory Lambdas add keys over time.

        A strict projection would turn each of their deploys into a resume outage,
        so a minimal row must still yield a usable session.

        "Minimal" means minimal among the *optional* fields. `org_id` is not one of
        them: it is half of the ownership comparison, so a row without it is refused
        rather than projected (see `TestTheTenantIsPartOfOwnership`). Tolerance applies
        to fields whose absence is cosmetic, never to the ones that establish who the
        conversation belongs to.
        """
        minimal = {"session_id": SESSION, "user_workspace": f"{OWNER}#{INTAKE_CHANNEL}", "org_id": TENANT}
        session = IntakeSessionReader(StubStore([minimal]), None).get(session_id=SESSION, user_id=OWNER, org_id=TENANT)
        assert session.draft == {}
        assert session.is_awaiting_answer is False
        assert session.is_working is False
        assert session.issue_ref == ""

    @pytest.mark.parametrize("bad_timestamp", ["not-a-number", None, "", Decimal("NaN")])
    def test_an_unreadable_timestamp_does_not_block_a_recoverable_session(self, bad_timestamp):
        """The conversation is intact; refusing it over a cosmetic field is worse.

        `Decimal("NaN")` is the case worth spelling out: DynamoDB numbers arrive as
        `Decimal`, and `int(Decimal("NaN"))` raises `InvalidOperation`, which is
        neither `TypeError` nor `ValueError`. A coercion catching only those two
        would turn one corrupt attribute into an unreadable conversation.
        """
        session = IntakeSessionReader(StubStore([a_row(updated_at=bad_timestamp)])).get(session_id=SESSION, user_id=OWNER, org_id=TENANT)
        assert session.updated_at == 0
