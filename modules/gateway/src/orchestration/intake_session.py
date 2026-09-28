"""Durable intake-conversation state, readable by a non-browser client (#5331).

EPIC #4191, intent #4120. This is the state layer that makes `adp flow start
--resume` possible; the routes over it live in `intake_routes.py`.

--------------------------------------------------------------------------------
Why this exists when #4208 already ships an intake conversation
--------------------------------------------------------------------------------

The #4208 planning conversation is reachable only from a browser. Its transport is
an API Gateway **WebSocket** whose route is selected by `$request.body.action`, and
`$connect` is its sole authorization gate. A terminal client cannot drive it without
hand-rolling RFC 6455 plus the response router's own 24 KB chunking — and doing so
still would not make `--resume` work, which is the requirement that actually forces
this module.

Resume needs the draft revision, whether the agent is mid-turn, and the issue the
conversation opened. Those are already *written* durably, but nothing reads them
back: `DraftStore.get()` and `getFullTranscript()` have no production caller,
`STATE_SNAPSHOT` is never emitted and is a no-op in the client, and the session id is
minted in the browser (`sess-<epoch>-<rand>`) and kept in `localStorage`. So the
identifier needed to recover a conversation is not derivable from the user at all:
clearing browser storage orphans server rows irrecoverably.

This module supplies the missing read-back, keyed so a *user* can find their own
sessions without having held the browser's id.

--------------------------------------------------------------------------------
It reads the shapes production writes, which are not all on one row
--------------------------------------------------------------------------------

Every field here is traced to a specific production writer, because the cost of
guessing is not a crash — it is a readback that returns a plausible empty value and a
client that renders "nothing captured yet" over a fully-formed conversation. Four
shapes matter, and three of them are not where a single-table reading would look:

- **The draft is in a different table.** The only writer is the chat worker's
  `DynamoDraftStore`, which puts it in `CONTEXT_TABLE` (`adp-<env>-chat-context`) at
  `PK=session#<id>, SK=draft` under a `draft` attribute. A `get_item` against the
  sessions table cannot see it at any key. Hence `DraftReader` below, and hence
  `draft_available` — a client must be able to tell "no draft yet" from "this
  deployment cannot read drafts", because only the second is an operator's problem.
- **In-flight state is per-thread, not per-row.** The lock the ingest and response
  Lambdas actually maintain is `threads.<thread_id>.processing_task_id`, cleared to
  `""` rather than removed. There is no row-level `status`; the nearest thing,
  `response_status`, is written only by the REST router and only ever as `"complete"`,
  so it says a reply landed, never that one is pending.
- **The issue is per-thread too.** `github_issue_number` / `github_issue_url` live
  inside a thread's map (`create_thread`), never at the top level.
- **There is no `pending_question` anywhere in production.** Nothing in
  agent-factory writes that attribute — the persona's open questions are draft
  content (`IntentDraft.openQuestions`), not a session-level flag. An earlier
  revision of this module read *and wrote* it, which made it look tested while being
  unreachable: the only writer was this file. Both are gone, and "waiting on you" is
  derived from the draft's open questions plus an idle conversation.

The rule this encodes: a readback field must name the writer that sets it. A field
only this module writes is a field production does not have.

**It deliberately does not reimplement the planning agent.** Sending a turn enqueues
onto the same SQS FIFO, with the same `MessageGroupId = session_id` serialization and
the same `intent-refinement` persona pin, so there is exactly one intake
implementation and a conversation started in the CLI is the same conversation the SPA
would show. Reuse is at the queue and state layer, because that is where the shared
behaviour lives; the WebSocket is a browser transport detail, not the contract.

--------------------------------------------------------------------------------
Ownership is enforced on the row, never taken from the request
--------------------------------------------------------------------------------

A session id is a bearer-ish opaque string that appears in logs and shell history, so
possession of one cannot be authority to read the conversation. Every read compares
the caller's authenticated `user_id` AND `org_id` against the values recorded on the
row, and a mismatch is reported as *not found* rather than *forbidden* — so a caller
cannot use this surface to discover that somebody else's session exists.

`user_workspace` is the existing `f"{user_id}#{channel}"` attribute the sessions
table already indexes (`user-workspace-index`), so resume-by-user is a GSI query
rather than a scan, and this module introduces no new key scheme for the ingest path
to keep in sync.

**Both halves of the identity are required, because one human is not one principal.**
`user_workspace` carries the user and the channel; it carries no tenant. A person who
belongs to two workspaces authenticates with the same `user_id` in both, so comparing
only that value makes their tenants indistinguishable here: authenticated into one,
they could read and send into a conversation — its transcript, its draft, the issue it
opened — created under the other. The tenant is therefore compared as well, against
the `org_id` the ingest Lambda stamps on the row at creation.

A row carrying **no** `org_id` is refused to every caller rather than matched by any.
Rows created before that stamp existed cannot prove which tenant they belong to, and
the safe reading of "unknown tenant" is "not yours" — the alternative, treating an
absent value as a wildcard, would make exactly the legacy rows readable across
tenants. Such a session is unreachable rather than exposed; it expires on the table's
TTL, and its owner starts a new conversation.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

logger = logging.getLogger(__name__)

# The channel these sessions are recorded under. Matches what the ingest Lambda
# writes for a socket turn, so the CLI and the SPA address the SAME session rather
# than two rows that merely share an id: `user_workspace` is built from this value,
# and a distinct channel here would silently partition a user's conversations by the
# client they happened to start from.
INTAKE_CHANNEL = "webchat"

# The persona that runs an intake conversation. Pinned explicitly because the server
# classifier would otherwise route a plain sentence to a general agent, and the
# refinement contract ("you produce one thing: an intent clear enough to hand to the
# inception flow") is the whole reason this conversation is worth resuming.
INTAKE_PERSONA = "intent-refinement"

# How long a session may sit idle before the table's TTL reaps it. Matches the
# ingest Lambda's 24h so a CLI-started conversation does not outlive a
# browser-started one; a longer value here would make `--resume` succeed for rows the
# SPA considers gone.
SESSION_TTL_SECONDS = 24 * 3600

# The sort key the draft is stored under in the chat-context table. Mirrors
# `DRAFT_SK` in `agent/src/complex-task-chat/draft/dynamo-draft-store.ts`, which is
# the only writer. A mismatch here fails silently — `get_item` returns no Item, and
# the readback reports an empty draft for a conversation that has a full one — so the
# constant is named and referenced once rather than inlined at the call site.
DRAFT_SORT_KEY = "draft"


class SessionStore(Protocol):
    """The subset of the sessions table this module uses.

    A Protocol rather than a boto3 `Table` so the ownership and resume logic is
    testable without AWS, matching `orchestration/dispatch_pass.py`'s `SQSClient`.
    The alternative — asserting authorization against a live table — would make the
    not-found-not-forbidden property effectively untested, and that property is the
    one protecting other users' conversations.
    """

    def get_item(self, **kwargs: Any) -> dict[str, Any]: ...

    def query(self, **kwargs: Any) -> dict[str, Any]: ...


class DraftStore(Protocol):
    """The subset of the chat-context table the draft readback uses.

    A second store because the draft is in a second TABLE. `adp-<env>-chat-context`
    is keyed `PK`/`SK`, where the sessions table is keyed `session_id` alone, so one
    client cannot address both — this is a table boundary, not an abstraction choice.
    """

    def get_item(self, **kwargs: Any) -> dict[str, Any]: ...


class IntakeUnavailableError(RuntimeError):
    """The intake backend cannot answer: unconfigured, or configured and unreadable.

    Distinct from "no such session" on purpose: a client must be able to tell a
    deployment that cannot plan at all from one where a specific conversation is
    missing, because the first is an operator's problem and the second is the
    caller's. The CLI maps this to its `unavailable` exit code, not its failure one.

    Covers both halves of "the operator must fix something", because the required
    action is the same and the caller's options are identical:

    - nothing is wired (no table name in the environment), and
    - a table is named but the read fails — a name that does not resolve, or a
      service role whose policy does not cover it.

    The second is the one deployment makes likely: the table names are rendered from
    SSM with convention-derived defaults, so a wrong guess reaches DynamoDB and comes
    back `ResourceNotFoundException`/`AccessDeniedException` rather than being caught
    as absent configuration. The two carry different messages, because one sends an
    operator to the deploy wiring and the other to the IAM policy.
    """


class SessionNotFoundError(LookupError):
    """No session with this id belongs to this caller.

    Raised identically for "does not exist" and "belongs to somebody else". The
    ambiguity is the point — see the module docstring.
    """


@dataclass(frozen=True)
class IntakeSession:
    """A planning conversation's durable state, as a client may read it.

    Frozen: this is a point-in-time readback, and a mutable copy would invite a
    caller to treat a local edit as having changed the conversation.
    """

    session_id: str
    user_id: str
    # The tenant recorded on the row. Empty for a row written before the stamp
    # existed, which is refused rather than matched — see the module docstring.
    org_id: str
    updated_at: int
    # True while any thread in this conversation holds a `processing_task_id`.
    # Computed from the thread map rather than stored, because the lock production
    # maintains is per-thread and there is no row-level status to read.
    working: bool = False
    # The refinement artifact — `intent`, `motivation`, `outcomes`, `constraints`,
    # `openQuestions`. Shape is the agent's `IntentDraft`, passed through rather than
    # re-modelled here: this module's job is to make it readable, not to become a
    # second definition of it that can drift. Read from the CHAT-CONTEXT table.
    draft: dict[str, Any] = field(default_factory=dict)
    # Whether the draft could be read at all. `{}` is ambiguous on its own — it is
    # both "the conversation has not produced one yet" and "this deployment has no
    # context table configured" — and a client that showed the first for the second
    # would tell a user their answers were discarded.
    draft_available: bool = True
    # The issue this conversation opened, if it has. Carried so a resume does not
    # open a second one — the duplicate-issue failure mode that makes a naive
    # client-side retry unsafe. Read from the thread map, where `create_thread`
    # writes it; there is no top-level attribute.
    issue_ref: str = ""
    last_response: str = ""
    last_response_task_id: str = ""
    repository: str = ""
    requested_issue: str = ""
    created_at: int = 0

    @property
    def open_questions(self) -> list[str]:
        """What the persona still needs decided, from the draft it maintains.

        This is the real "waiting on you" signal. It is draft content
        (`IntentDraft.openQuestions`) because that is where the persona is instructed
        to put it — there is no session-level question attribute in production, and
        the earlier revision of this module that read one was reading a field only
        itself ever wrote.
        """
        raw = self.draft.get("openQuestions")
        if not isinstance(raw, list):
            return []
        return [str(item) for item in raw if str(item).strip()]

    @property
    def is_awaiting_answer(self) -> bool:
        """The conversation needs the user before it can progress.

        Both halves are required. Open questions alone do not mean it is the user's
        turn — the agent may be mid-answer and about to resolve them itself — so a
        conversation that still holds its processing lock is reported as working, not
        as blocked. Reporting "waiting on you" for a turn in flight would make a
        script answer a question the agent was already answering.
        """
        return bool(self.open_questions) and not self.working

    @property
    def is_working(self) -> bool:
        """The agent holds the turn, so a new turn would interleave with it.

        Read from the thread-level processing lock the ingest and response Lambdas
        actually maintain, rather than inferred from the absence of a reply: a
        conversation whose reply was lost is not the same as one still thinking, and
        only the first is safe to re-send into.
        """
        return self.working


def _workspace_key(user_id: str) -> str:
    """The `user-workspace-index` hash key for a user's intake sessions.

    Built exactly as the ingest Lambda builds it, so both writers agree. Centralised
    in one function because a divergence here would not fail loudly — it would
    quietly return "you have no sessions" for a user who has several.
    """
    return f"{user_id}#{INTAKE_CHANNEL}"


def _coerce_int(value: Any, default: int = 0) -> int:
    """DynamoDB numbers arrive as `Decimal`; timestamps must survive to JSON.

    Defaulting rather than raising because a row with an unreadable timestamp is
    still a row whose *conversation* is intact, and refusing to resume a recoverable
    session over a cosmetic field would be the worse failure.

    `ArithmeticError` is caught alongside the obvious two because
    `int(Decimal("NaN"))` raises `InvalidOperation`, which is neither `TypeError` nor
    `ValueError` — and `Decimal` is exactly what this function receives, so the
    narrower pair would let a single corrupt attribute 500 a resume.
    """
    try:
        return int(value)
    except (TypeError, ValueError, ArithmeticError):
        return default


def _threads_of(item: dict[str, Any]) -> list[dict[str, Any]]:
    """The conversation's threads, newest first, as a list.

    `threads` is a DynamoDB MAP keyed by thread id. Sorted on `created_at` so
    "the current thread" is well defined — map iteration order is not a chronology,
    and relying on it would pick an arbitrary thread's issue and lock.
    """
    threads = item.get("threads")
    if not isinstance(threads, dict):
        return []
    values = [thread for thread in threads.values() if isinstance(thread, dict)]
    return sorted(values, key=lambda thread: _coerce_int(thread.get("created_at")), reverse=True)


def _is_working(threads: list[dict[str, Any]]) -> bool:
    """Whether any thread still holds its processing lock.

    ANY, not the newest: a stale lock on an older thread still means the worker holds
    that session's FIFO message group, so a new turn would queue behind it rather than
    being answered.

    `_clear_thread_processing` writes `""` rather than REMOVE, so an empty string is
    the cleared state and truthiness is the correct test — not key presence.
    """
    return any(str(thread.get("processing_task_id") or "") for thread in threads)


def _issue_ref_of(threads: list[dict[str, Any]]) -> str:
    """The issue this conversation opened, from the newest thread that has one.

    Searched across threads rather than read off the newest alone: a follow-up turn
    creates a new thread, and the issue belongs to the conversation.
    """
    for thread in threads:
        number = str(thread.get("github_issue_number") or "")
        if number:
            return number
    return ""


def _session_from_item(item: dict[str, Any], *, draft: dict[str, Any] | None = None, draft_available: bool = True) -> IntakeSession:
    """Project a stored row into the readback shape.

    Tolerant of absent fields throughout: rows are written by the agent-factory
    ingest and response Lambdas, which add keys over time, so a strict projection
    here would turn every one of their deploys into a resume outage. Tolerance stops
    at the two attributes that establish ownership — those are compared, not defaulted.

    `draft` arrives from the caller because it lives in a different table, so a
    projection cannot fetch it. `None` means "not read", which is why
    `draft_available` is a separate argument rather than inferred from emptiness.
    """
    threads = _threads_of(item)
    return IntakeSession(
        session_id=str(item.get("session_id", "")),
        # From the row, never from the request. These are the values every ownership
        # comparison is made against.
        user_id=str(item.get("user_workspace", "")).split("#", 1)[0],
        org_id=str(item.get("org_id") or ""),
        updated_at=_coerce_int(item.get("updated_at")),
        working=_is_working(threads),
        draft=draft if isinstance(draft, dict) else {},
        draft_available=draft_available,
        issue_ref=_issue_ref_of(threads),
        last_response=str(item.get("last_response") or ""),
        last_response_task_id=str(item.get("last_response_task_id") or ""),
        repository=str(item.get("intake_repository") or ""),
        requested_issue=str(item.get("intake_issue") or ""),
        created_at=_coerce_int(item.get("created_at")),
    )


class IntakeSessionReader:
    """Reads intake conversations back, scoped to their owner.

    Holds no AWS client of its own — the stores are injected — so the authorization
    behaviour can be tested exhaustively offline.

    Two stores, because the state is in two tables. The draft store is separately
    optional: a deployment may have the sessions table wired and the context table not,
    and that must degrade to "draft unavailable" on an otherwise readable conversation
    rather than 503 the whole readback. Ownership is checked against the SESSIONS row
    either way, so the draft fetch never widens what a caller can reach.
    """

    def __init__(self, store: SessionStore | None, draft_store: DraftStore | None = None) -> None:
        # `None` is a legitimate state, not a caller error: a deployment without the
        # intake backend configured must answer "unavailable" on every verb rather
        # than fail at import and take the whole app down.
        self._store = store
        self._draft_store = draft_store

    def _require_store(self) -> SessionStore:
        if self._store is None:
            raise IntakeUnavailableError("conversational planning is not configured on this deployment; no intake session store is available")
        return self._store

    @staticmethod
    def _unreadable(operation: str, exc: Exception) -> IntakeUnavailableError:
        """A configured table that could not be read, as an unavailability.

        NOT a `SessionNotFoundError`. The two realistic causes here — a table name
        that does not resolve and a service role whose policy does not cover it — are
        both operator-fixable misconfigurations, and reporting either as "no such
        intake session" is the most misleading answer available: the user is told to
        start a conversation over, the operator is told nothing, and the readback
        looks healthy because a 404 is a normal response.

        `exc_info` because the two causes are indistinguishable in a one-line
        message and the distinction is the whole fix. The caller-facing message names
        the two candidates without echoing the AWS error, which can carry the account
        id and the table ARN.
        """
        logger.error("intake session store unreadable operation=%s", operation, exc_info=exc)
        return IntakeUnavailableError(
            "this deployment could not read intake conversations; the intake sessions table "
            "may be misnamed or the gateway may lack permission to read it"
        )

    def _read_draft(self, session_id: str) -> tuple[dict[str, Any], bool]:
        """The live draft for a session, and whether it could be read.

        Keyed `PK=session#<id>, SK=draft` — the key `DynamoDraftStore` writes, in the
        chat-context table. Any other key silently returns nothing, which is why this
        is one function rather than an inline `get_item` per call site.

        A store failure degrades to `(empty, False)` rather than raising: the
        conversation, its transcript and its issue are still readable and useful, and
        the flag tells the client not to render an empty draft as "nothing captured".
        """
        if self._draft_store is None:
            return {}, False
        try:
            response = self._draft_store.get_item(Key={"PK": f"session#{session_id}", "SK": DRAFT_SORT_KEY})
        except Exception:
            # Logged, not raised. `exc_info` because a mis-scoped IAM grant and a
            # missing table look identical in a one-line message, and this is the
            # first place an operator looks when drafts come back empty.
            logger.warning("intake draft read failed session=%s", session_id, exc_info=True)
            return {}, False
        item = response.get("Item") or {}
        draft = item.get("draft")
        return (draft if isinstance(draft, dict) else {}), True

    def get(self, *, session_id: str, user_id: str, org_id: str) -> IntakeSession:
        """One session, if it belongs to this caller in this tenant.

        Raises `SessionNotFoundError` both when the row is absent and when it belongs
        to somebody else, so a caller holding a leaked id learns nothing from the
        difference.

        `org_id` is required, not optional. A default would let a future call site
        omit the tenant and silently fall back to user-only matching — the exact
        cross-tenant comparison this signature exists to prevent — whereas a missing
        argument is a `TypeError` at import-and-test time.
        """
        store = self._require_store()
        if not session_id or not user_id or not org_id:
            # Fail closed rather than querying with an empty key: an empty `user_id`
            # or `org_id` compared against a row's owner would match a malformed row.
            raise SessionNotFoundError("no such intake session")

        try:
            response = store.get_item(Key={"session_id": session_id})
        except Exception as exc:
            # Before `Item` is inspected, so a failed read can never be mistaken for
            # an absent row. Deliberately broad: the point is that NOTHING from the
            # store escapes as a 500 or degrades into a 404, and narrowing this to
            # `ClientError` would let a botocore connection error do both.
            raise self._unreadable("get_item", exc) from exc
        item = response.get("Item")
        if not item:
            raise SessionNotFoundError("no such intake session")

        # Ownership is settled on the sessions row BEFORE the draft is fetched. The
        # draft is the most sensitive thing here — it is the user's unshaped thinking
        # — so a foreign session must not cause a read of it at all, not merely fail
        # to return it.
        session = _session_from_item(item)
        if not self._belongs_to(session, user_id=user_id, org_id=org_id):
            raise SessionNotFoundError("no such intake session")

        draft, available = self._read_draft(session.session_id)
        return _session_from_item(item, draft=draft, draft_available=available)

    @staticmethod
    def _belongs_to(session: IntakeSession, *, user_id: str, org_id: str) -> bool:
        """Whether this caller owns this conversation. Both halves must match.

        One function for both read paths so they cannot drift into checking different
        things; the mismatch is logged here, once, because it is worth seeing in an
        audit trail even though the caller is told only that the session is absent.

        A row with no recorded tenant fails for everyone: `org_id` is empty on the
        session, the caller's is non-empty (the callers guard that), so the comparison
        is false. That is the intended refusal, not an accident of the expression.
        """
        if session.user_id == user_id and session.org_id == org_id:
            return True
        logger.warning(
            "intake session ownership mismatch session=%s caller=%s caller_org=%s row_org_present=%s",
            session.session_id,
            user_id,
            org_id,
            bool(session.org_id),
        )
        return False

    def latest_for_user(self, *, user_id: str, org_id: str) -> IntakeSession | None:
        """The caller's most recently updated intake session, or None.

        This is what makes `--resume` usable without the browser's id: the session
        identifier is minted client-side and not derivable from the user, so without
        a by-user lookup a conversation started elsewhere is unreachable from a
        terminal.

        Queries the existing `user-workspace-index` rather than scanning. `Limit` is
        not used to pick the newest — the index's range key is `session_id`, which is
        not chronological — so rows are compared on `updated_at`. Sorting a bounded
        page and taking the max is correct where trusting index order would silently
        resume the wrong conversation.

        **The index cannot scope this by tenant, so the code must.** `user_workspace`
        is `user#channel`, so a user who belongs to two tenants has all of their
        conversations under one GSI key. Without the tenant filter below, a resume
        would hand back whichever was most recently updated — potentially the other
        tenant's, which is the cross-tenant read in its most likely form, since it
        needs no leaked id at all.
        """
        store = self._require_store()
        if not user_id or not org_id:
            return None

        try:
            response = store.query(
                IndexName="user-workspace-index",
                KeyConditionExpression="user_workspace = :workspace",
                ExpressionAttributeValues={":workspace": _workspace_key(user_id)},
            )
        except Exception as exc:
            # Worse to swallow here than on `get`: this method returns `None` for "you
            # have no sessions", which is a SUCCESSFUL answer the route renders as 404.
            # A caught failure would tell a user with a live conversation that they
            # have never started one, and invite them to start a second while the
            # first stays unreachable — `--resume`'s worst outcome.
            raise self._unreadable("query", exc) from exc
        items = [item for item in (response.get("Items") or []) if item.get("session_id")]
        # The GSI key scopes to this user but NOT to this tenant, so this filter is
        # load-bearing here rather than merely defence in depth: it is the only thing
        # separating a user's two tenants on the resume path.
        owned = [(item, _session_from_item(item)) for item in items]
        owned = [pair for pair in owned if self._belongs_to(pair[1], user_id=user_id, org_id=org_id)]
        if not owned:
            return None
        # One draft read, for the one session being returned. Fetching a draft per
        # candidate row would multiply the cost of a resume by a user's session count
        # to answer a question about exactly one of them.
        item, session = max(owned, key=lambda pair: pair[1].updated_at)
        draft, available = self._read_draft(session.session_id)
        return _session_from_item(item, draft=draft, draft_available=available)
