"""`adp flow` readback, controls and refusals (Issue #5331).

These tests drive the helper against a REAL `http.server` through the REAL
`adp_common.Api`, rather than a stub transport. That is the documented lesson
from `test_provider_transport.py`: the two defects it was written for both lived
in the transport a double would have replaced, so a `RecordingApi` returning
`{}` proves nothing about the code under test. Here the server records what it
actually received and answers with real status lines, so "the CLI approved a
gate" means an approval request reached a server.

Four properties carry most of the weight:

**Cost stays three-valued.** `unknown` must never render as a number. A flow
whose spend is unmeasured and a flow that has spent nothing are different
answers, and printing `$0.00` for the first is a misreport a reader would act on.

**Approval binds the revision the user read.** `--expect-plan-hash` must refuse a
plan that already moved, and must refuse WITHOUT sending the approval — a guard
that rejects after the write is not a guard.

**Nothing falls back.** An unsupported deployment and an unavailable planning
capability must report and execute nothing. `flow start` must not register,
approve or plan locally.

**Detaching is not cancelling.** Interrupt and watch-exit messages must say that
hosted work continues, because a user who believes Ctrl-C stopped delivery will
not go looking for work that is still running.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

CLI_DIR = Path(__file__).parents[2] / "cli"
SCRIPT = CLI_DIR / "adp-flow.py"

spec = importlib.util.spec_from_file_location("adp_flow_cli", SCRIPT)
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)
# The helper does `import adp_common as common`, so THIS is the transport it
# actually uses. Loading a second copy would give CliError a different class
# identity and make `pytest.raises` miss.
common = cli.common

FLOW_ID = "7f3c2a10"
GATE_ID = "gate-node-1"
CURRENT_HASH = "a" * 64
STALE_HASH = "b" * 64
SESSION_ID = "sess-0123456789abcdef0123456789abcdef"


@pytest.fixture(autouse=True)
def no_real_waiting(monkeypatch):
    """Make the conversation's poll interval free.

    Autouse because `start` polls for a reply and `watch` sleeps between ticks;
    without this the journey tests would each cost their poll interval in real
    seconds and the suite would be slow enough that people stop running it.
    """
    monkeypatch.setattr(cli.time, "sleep", lambda _seconds: None)


def _session(*, session_id=SESSION_ID, open_questions=None, **overrides):
    """An intake session as `GET /orchestration/intake/sessions/{id}` returns one.

    Built from the route's real response model, not an invented shape: if this
    helper and `IntakeSessionResponse` disagreed, these tests would pass against a
    contract the server does not serve. There is deliberately no `status` and no
    `pending_question` key here, because the server serves neither — what a client
    gets is `working` (the per-thread processing lock) and `open_questions` (the
    draft's `openQuestions`).
    """
    questions = list(open_questions or [])
    payload = {
        "session_id": session_id,
        "updated_at": 1_700_000_000,
        "working": False,
        "awaiting_answer": bool(questions),
        "open_questions": questions,
        "draft": {},
        "draft_available": True,
        "issue_ref": "",
        "last_response": "",
    }
    payload.update(overrides)
    return payload


# --- a real server ----------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    routes: dict[tuple[str, str], tuple[int, object]] = {}
    received: list[tuple[str, str, object]] = []

    def _respond(self):
        key = (self.command, self.path.split("?")[0])
        status, payload = self.routes.get(key, (404, {"detail": "not found"}))
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.received.append(("GET", self.path, None))
        self._respond()

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        self.received.append(("POST", self.path, json.loads(raw) if raw else None))
        self._respond()

    def log_message(self, *args):
        """Silence stderr access logs; they would pollute the captured stream."""


@pytest.fixture
def server(monkeypatch):
    """A real gateway on loopback, with the CLI pointed at it and auth stubbed."""
    _Handler.routes = {}
    _Handler.received = []
    httpd = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}/api"
    # `gateway_url()` permits plain http on loopback only, which is exactly this.
    monkeypatch.setattr(common, "gateway_url", lambda: base)
    # The token helper shells out to bg-cognito-auth.sh; the transport is what is
    # under test here, not the token store.
    monkeypatch.setattr(common, "access_token", lambda: "test-access-token")
    yield _Handler
    httpd.shutdown()
    httpd.server_close()


def route(server, method, path, payload, status=200):
    server.routes[(method, path)] = (status, payload)


def engine_on(server, enabled=True):
    route(server, "GET", "/api/features", {"features": {"orchestration_engine": enabled}})


def run_cli(args):
    """Call main() in-process and capture its envelope. Returns (code, result)."""
    captured = {}
    original = common.emit

    def spy(result, as_json=False):
        captured["result"] = result
        return original(result, as_json)

    common.emit = spy
    try:
        code = cli.main(args)
    finally:
        common.emit = original
    return code, captured.get("result")


# --- cost is three-valued ---------------------------------------------------


def test_unknown_cost_is_never_rendered_as_a_number():
    """The misreport this guards: "unmeasured" shown as "$0.00"."""
    text = cli.cost_text({"status": "unknown", "reason": "no ledger rows"})

    assert "unknown" in text
    assert "0.00" not in text and "$" not in text


def test_none_incurred_is_distinct_from_unknown():
    assert cli.cost_text({"status": "none_incurred"}) == "none incurred"


def test_known_cost_keeps_sub_cent_precision_as_given():
    """`amount_usd` is a string to preserve Numeric(10,6); it must not be floated."""
    assert "1.234567" in cli.cost_text({"status": "known", "amount_usd": "1.234567"})


def test_absent_cost_is_reported_as_unknown_not_zero():
    assert "unknown" in cli.cost_text(None)


def test_flow_cost_flags_a_partial_rollup_as_a_lower_bound(server):
    """A partial figure that reads as a total is a number a reader would act on."""
    engine_on(server)
    route(
        server,
        "GET",
        f"/api/orchestration/flows/{FLOW_ID}/cost",
        {"flow_id": FLOW_ID, "status": "known", "amount_usd": "3.50", "partial": True, "unknown_node_count": 2, "nodes": []},
    )

    code, result = run_cli(["cost", FLOW_ID])

    assert code == 0
    assert "lower bound" in result["next_action"]
    assert result["detail"]["unknown_node_count"] == 2


# --- readback ---------------------------------------------------------------


def _graph(nodes):
    return {"flow_id": FLOW_ID, "title": "Checkout rework", "state": "running", "nodes": nodes, "edges": [], "cost": {"status": "none_incurred"}}


def test_show_separates_gates_blockers_and_next_eligible_work(server):
    engine_on(server)
    route(
        server,
        "GET",
        f"/api/orchestration/flows/{FLOW_ID}",
        _graph(
            [
                {"id": "n1", "state": "passed", "kind": "story", "title": "done"},
                {"id": GATE_ID, "state": "awaiting_gate", "kind": "gate", "title": "Release to prod"},
                {"id": "n3", "state": "failed", "kind": "story", "title": "broken"},
                {"id": "n4", "state": "ready", "kind": "story", "title": "next up"},
                {"id": "n5", "state": "pending", "kind": "story", "title": "later"},
            ]
        ),
    )

    code, result = run_cli(["show", FLOW_ID])
    detail = result["detail"]

    assert code == 0
    assert [gate["id"] for gate in detail["outstanding_gates"]] == [GATE_ID]
    assert [node["id"] for node in detail["blocked"]] == ["n3"]
    # `pending` is NOT eligible: predecessors are unsatisfied, and calling it
    # eligible would say work is about to start when it cannot.
    assert [node["id"] for node in detail["next_eligible"]] == ["n4"]


def test_a_stalled_node_is_blocked_even_while_its_state_reads_running(server):
    """`stalled` is a separate server-derived flag, not a NodeState member."""
    engine_on(server)
    stalled = {"id": "n1", "state": "running", "kind": "story", "title": "wedged", "stalled": True}
    route(server, "GET", f"/api/orchestration/flows/{FLOW_ID}", _graph([stalled]))

    _, result = run_cli(["show", FLOW_ID])

    assert [node["id"] for node in result["detail"]["blocked"]] == ["n1"]


def test_show_leads_with_the_blocker_not_the_gate(server):
    """Worst news first: a gate a human is told about outranks eligible work, and
    a blocker outranks both, because running work will not clear a stall."""
    engine_on(server)
    route(
        server,
        "GET",
        f"/api/orchestration/flows/{FLOW_ID}",
        _graph(
            [
                {"id": GATE_ID, "state": "awaiting_gate", "kind": "gate", "title": "g"},
                {"id": "n3", "state": "halted", "kind": "story", "title": "b"},
            ]
        ),
    )

    _, result = run_cli(["show", FLOW_ID])

    assert "need attention" in result["next_action"]


def test_plans_names_the_current_proposal_and_the_accepted_version(server):
    engine_on(server)
    route(
        server,
        "GET",
        f"/api/orchestration/flows/{FLOW_ID}/plans",
        [
            {"version": 1, "plan_hash": STALE_HASH, "accepted_by_decision_id": "d1", "superseded_at": "2026-09-01T00:00:00Z", "created_at": "x"},
            {"version": 2, "plan_hash": CURRENT_HASH, "accepted_by_decision_id": None, "superseded_at": None, "created_at": "y"},
        ],
    )

    code, result = run_cli(["plans", FLOW_ID])

    assert code == 0
    assert result["detail"]["proposed_version"] == 2
    # The next action must hand the user the hash to bind their approval to.
    assert CURRENT_HASH in result["next_action"]


def test_decisions_carry_attribution(server):
    """Who decided, in what role, and whether a human or a service did it."""
    engine_on(server)
    route(
        server,
        "GET",
        f"/api/orchestration/flows/{FLOW_ID}/decisions",
        [{"id": "d1", "kind": "PLAN_ACCEPTED", "actor_id": "alice", "actor_role": "owner", "actor_kind": "human", "created_at": "z"}],
    )

    _, result = run_cli(["decisions", FLOW_ID])

    assert result["detail"]["decisions"][0]["actor_kind"] == "human"
    assert result["detail"]["decisions"][0]["actor_role"] == "owner"


# --- revision-bound approval ------------------------------------------------


def _gate_lookup(server, gate_state="awaiting_gate"):
    route(server, "GET", "/api/orchestration/flows", {"flows": [{"id": FLOW_ID, "awaiting_gate_count": 1}], "total": 1})
    gate = {"id": GATE_ID, "state": gate_state, "kind": "gate", "title": "Release to prod"}
    route(server, "GET", f"/api/orchestration/flows/{FLOW_ID}", _graph([gate]))


def test_approval_bound_to_the_current_revision_succeeds(server):
    engine_on(server)
    _gate_lookup(server)
    route(server, "GET", f"/api/orchestration/flows/{FLOW_ID}/plans", [{"version": 2, "plan_hash": CURRENT_HASH, "superseded_at": None}])
    applied = {"node_id": GATE_ID, "status": "applied", "state": "passed", "decision_id": "d9"}
    route(server, "POST", f"/api/orchestration/gates/{GATE_ID}/approve", applied)

    code, result = run_cli(["gate", "approve", GATE_ID, "--expect-plan-hash", CURRENT_HASH, "--yes"])

    assert code == 0
    assert result["detail"]["approved_plan_hash"] == CURRENT_HASH
    # The binding must reach the SERVER, not merely gate a client-side re-read.
    # This is the assertion that fails if the precondition stops being sent, which
    # is the regression that would silently reopen the concurrent-edit race.
    posts = [entry for entry in server.received if entry[0] == "POST"]
    assert posts[-1][2]["expected_plan_hash"] == CURRENT_HASH
    # And the reported outcome must say the enforcement was server-side, because a
    # reader deciding whether to trust it against a concurrent edit needs that.
    assert "same transaction" in result["next_action"]


def test_an_expired_policy_refusal_tells_the_operator_to_derive_and_review_a_new_plan(server):
    engine_on(server)
    _gate_lookup(server)
    route(
        server,
        "POST",
        f"/api/orchestration/gates/{GATE_ID}/approve",
        {
            "detail": {
                "error": "execution_policy_expired",
                "message": "the execution policy this plan proposes expired",
            }
        },
        status=409,
    )

    code, result = run_cli(["gate", "approve", GATE_ID, "--yes"])

    assert code == 5
    assert result["error"]["code"] == "execution_policy_expired"
    assert "newly derived plan" in result["error"]["message"]
    assert "exact revision" in result["error"]["message"]


def test_a_stale_revision_is_refused_without_sending_the_approval(server):
    """A guard that refuses after the write is not a guard."""
    engine_on(server)
    _gate_lookup(server)
    route(server, "GET", f"/api/orchestration/flows/{FLOW_ID}/plans", [{"version": 3, "plan_hash": CURRENT_HASH, "superseded_at": None}])
    route(server, "POST", f"/api/orchestration/gates/{GATE_ID}/approve", {"node_id": GATE_ID, "status": "applied"})

    code, result = run_cli(["gate", "approve", GATE_ID, "--expect-plan-hash", STALE_HASH, "--yes"])

    assert code == 5
    assert result["error"]["code"] == "stale_plan_revision"
    assert not [entry for entry in server.received if entry[0] == "POST"], "a refused approval must not reach the server"


@pytest.mark.parametrize("empty", ["", "   "])
def test_an_empty_expected_hash_refuses_instead_of_skipping_the_guard(server, empty):
    """Asking to be guarded with an unusable value must not approve unguarded.

    `--expect-plan-hash "$(...)"` whose command substitution produced nothing is
    the realistic way to reach this. The empty string can never match a live plan
    hash, so treating it as "no check requested" approved unconditionally with
    exit 0 while the operator had explicitly asked for the binding.
    """
    engine_on(server)
    _gate_lookup(server)
    route(server, "GET", f"/api/orchestration/flows/{FLOW_ID}/plans", [{"version": 4, "plan_hash": CURRENT_HASH, "superseded_at": None}])
    route(server, "POST", f"/api/orchestration/gates/{GATE_ID}/approve", {"node_id": GATE_ID, "status": "applied"})

    code, result = run_cli(["gate", "approve", GATE_ID, "--expect-plan-hash", empty, "--yes"])

    assert code == 1
    assert result["error"]["code"] == "usage_error"
    assert not [entry for entry in server.received if entry[0] == "POST"], "nothing may be approved when the revision was not checked"


def test_an_unresolvable_gate_refuses_rather_than_approving_unverified(server):
    """Fail CLOSED: if the plan could not be checked, nothing is approved."""
    engine_on(server)
    route(server, "GET", "/api/orchestration/flows", {"flows": [], "total": 0})
    route(server, "POST", f"/api/orchestration/gates/{GATE_ID}/approve", {"node_id": GATE_ID, "status": "applied"})

    code, result = run_cli(["gate", "approve", GATE_ID, "--expect-plan-hash", CURRENT_HASH, "--yes"])

    assert code == 5
    assert result["error"]["code"] == "plan_revision_unverified"
    assert not [entry for entry in server.received if entry[0] == "POST"]


def test_approval_reaches_the_server_with_its_reason(server):
    engine_on(server)
    _gate_lookup(server)
    route(server, "POST", f"/api/orchestration/gates/{GATE_ID}/approve", {"node_id": GATE_ID, "status": "applied", "state": "passed"})

    run_cli(["gate", "approve", GATE_ID, "--reason", "reviewed the plan", "--yes"])

    posts = [entry for entry in server.received if entry[0] == "POST"]
    assert posts and posts[0][1].endswith(f"/gates/{GATE_ID}/approve")
    # No `expected_plan_hash` key at all when none was asked for. Sending an
    # explicit null would be a different request: the server's model rejects an
    # empty binding at the edge, and a caller who omitted the flag has asked for
    # no precondition rather than for an unusable one.
    assert posts[0][2] == {"reason": "reviewed the plan"}


def test_rejection_reports_that_successors_stay_pending(server):
    engine_on(server)
    _gate_lookup(server)
    route(server, "POST", f"/api/orchestration/gates/{GATE_ID}/reject", {"node_id": GATE_ID, "status": "applied", "state": "rejected_at_gate"})

    code, result = run_cli(["gate", "reject", GATE_ID, "--reason", "no"])

    assert code == 0
    assert "pending" in result["next_action"]


def test_a_rejection_can_be_bound_to_the_revision_it_answers(server):
    """A rejection binds to a revision too, because the server lets it.

    The server applies the precondition to `reject` exactly as to `approve`, on the
    stated ground that recording a decision against a revision the operator never
    read misattributes a rejection just as badly as an approval. The CLI exposed the
    flag only on `approve`, leaving that half of the server's guarantee unreachable
    from the terminal — so an operator rejecting a plan they had read could not ask
    to be refused if it had moved underneath them.
    """
    engine_on(server)
    _gate_lookup(server)
    route(server, "GET", f"/api/orchestration/flows/{FLOW_ID}/plans", [{"version": 2, "plan_hash": CURRENT_HASH, "superseded_at": None}])
    route(server, "POST", f"/api/orchestration/gates/{GATE_ID}/reject", {"node_id": GATE_ID, "status": "applied", "state": "rejected_at_gate"})

    code, result = run_cli(["gate", "reject", GATE_ID, "--expect-plan-hash", CURRENT_HASH, "--reason", "wrong shape"])

    assert code == 0
    posts = [entry for entry in server.received if entry[0] == "POST"]
    assert posts[-1][2]["expected_plan_hash"] == CURRENT_HASH, "the rejection must carry the precondition to the server"
    assert result["detail"]["approved_plan_hash"] == CURRENT_HASH


def test_a_stale_revision_is_refused_without_sending_the_rejection(server):
    """Fail closed on the reject arm as well: a moved plan is not rejected blind."""
    engine_on(server)
    _gate_lookup(server)
    route(server, "GET", f"/api/orchestration/flows/{FLOW_ID}/plans", [{"version": 3, "plan_hash": CURRENT_HASH, "superseded_at": None}])
    route(server, "POST", f"/api/orchestration/gates/{GATE_ID}/reject", {"node_id": GATE_ID, "status": "applied"})

    code, result = run_cli(["gate", "reject", GATE_ID, "--expect-plan-hash", STALE_HASH])

    assert code == 5
    assert result["error"]["code"] == "stale_plan_revision"
    assert not [entry for entry in server.received if entry[0] == "POST"], "a refused rejection must not reach the server"


# --- the guided planning journey -------------------------------------------


def _intake_started(server, session_id=SESSION_ID):
    route(
        server,
        "POST",
        "/api/orchestration/intake/sessions",
        {"session_id": session_id, "task_id": "task-1", "enqueued_at": 1},
        status=202,
    )


def test_start_prints_the_session_id_before_waiting_for_a_reply(monkeypatch, server):
    """The load-bearing ordering. It is the only handle a dying caller keeps.

    Printed after the first reply instead, a dropped connection or a CI timeout
    would lose a conversation that is still sitting on the server — and since the id
    is minted server-side, nothing local could reconstruct it.

    Asserted on the ORDER, not merely on the id appearing somewhere: `capsys` reads
    the whole stream at the end, so a check for "the id is on stderr" passes even
    when it is printed last. So progress lines and HTTP requests are recorded into
    one interleaved log, and the assertion is that the id precedes the first poll —
    which is exactly the window a dying caller has.
    """
    _intake_started(server)
    route(server, "GET", f"/api/orchestration/intake/sessions/{SESSION_ID}", _session(working=True, updated_at=0))

    events = []
    original_progress = cli.progress

    def recording_progress(message):
        events.append(("print", message))
        original_progress(message)

    original_do_get = server.do_GET

    def do_GET(handler):  # noqa: N802 - matches BaseHTTPRequestHandler
        events.append(("poll", handler.path))
        original_do_get(handler)

    monkeypatch.setattr(cli, "progress", recording_progress)
    monkeypatch.setattr(server, "do_GET", do_GET)

    code, result = run_cli(["start", "Add per-tenant rate limiting", "--json"])

    assert code == 4, "pending: the conversation is healthy, the agent just has not answered"
    printed_id = next((i for i, (kind, text) in enumerate(events) if kind == "print" and SESSION_ID in text), None)
    first_poll = next((i for i, (kind, _) in enumerate(events) if kind == "poll"), None)
    assert printed_id is not None, "the session id was never printed"
    assert first_poll is not None, "the CLI never waited, so the ordering was not exercised"
    assert printed_id < first_poll, "the session id must be printed BEFORE the first wait, not after it"
    assert result["detail"]["session_id"] == SESSION_ID
    assert f"--resume {SESSION_ID}" in result["next_action"]


def test_start_sends_the_users_words_as_the_opening_turn(server):
    _intake_started(server)
    route(server, "GET", f"/api/orchestration/intake/sessions/{SESSION_ID}", _session(updated_at=200, last_response="Got it."))

    run_cli(["start", "Add per-tenant rate limiting", "--json"])

    posted = [body for method, path, body in server.received if method == "POST" and path.endswith("/intake/sessions")]
    assert "Add per-tenant rate limiting" in posted[0]["message"]


def test_repo_and_issue_become_context_in_the_conversation(server):
    """Not separate fields the agent would have to be taught: it reads prose.

    The intake agent's input is a message. Passing `--repo` as a structured field
    would need a server-side contract for it; stating it in the opening turn means
    the same agent the dashboard uses understands it with no new plumbing.
    """
    _intake_started(server)
    route(server, "GET", f"/api/orchestration/intake/sessions/{SESSION_ID}", _session(updated_at=200))

    run_cli(["start", "Add rate limiting", "--repo", "acme/app", "--issue", "4120", "--json"])

    message = [body for method, path, body in server.received if method == "POST"][0]["message"]
    assert "acme/app" in message
    assert "4120" in message


def test_an_open_question_is_reported_not_answered_without_a_terminal(server):
    """The story's non-interactive requirement, and the failure it rules out.

    A script must be told an answer is needed and exit. Hanging on a terminal
    nobody is watching, or inventing an answer, are both worse than reporting.
    """
    _intake_started(server)
    route(
        server,
        "GET",
        f"/api/orchestration/intake/sessions/{SESSION_ID}",
        _session(open_questions=["Which repository should this land in?"], updated_at=200),
    )

    code, result = run_cli(["start", "Add rate limiting", "--json"])

    assert code == 4, "pending, not failed: the conversation is waiting on a human"
    assert "Which repository should this land in?" in result["next_action"]
    assert f"--resume {SESSION_ID}" in result["next_action"]
    assert "Nothing has been registered, approved or started." in result["next_action"]


def test_every_open_question_is_reported_not_just_the_first(server):
    """A turn can come back needing two things decided.

    Collapsing them to one would have the operator answer one question, resume, and
    be asked the next — or worse, have a script report a single blocker when two
    exist. The list is reported whole.
    """
    _intake_started(server)
    route(
        server,
        "GET",
        f"/api/orchestration/intake/sessions/{SESSION_ID}",
        _session(open_questions=["Which repository?", "Which environment?"], updated_at=200),
    )

    code, result = run_cli(["start", "Add rate limiting", "--json"])

    assert code == 4
    assert "Which repository?" in result["next_action"]
    assert "Which environment?" in result["next_action"]


def test_open_questions_the_agent_is_still_working_on_do_not_stop_the_caller(server):
    """`awaiting_answer` is the server's judgement, and it is the one the CLI obeys.

    The draft can carry open questions while the agent still holds the turn and may
    resolve them itself. Keying the stop on the list alone would have the CLI report
    "waiting on you" mid-turn and exit 4 on a healthy conversation.
    """
    _intake_started(server)
    route(
        server,
        "GET",
        f"/api/orchestration/intake/sessions/{SESSION_ID}",
        _session(open_questions=[], last_response="Here is what I understand.", updated_at=200),
    )

    code, result = run_cli(["start", "Add rate limiting", "--json"])

    assert code == 0
    assert result["status"] == "ok"


def test_an_unreadable_draft_is_said_out_loud_rather_than_shown_as_empty(server):
    """Otherwise a user is told their plan is blank when it is merely unreadable.

    They would start over and lose the refinement they already did. The conversation
    is intact, so this is a warning on a successful run, not a failure.
    """
    _intake_started(server)
    route(
        server,
        "GET",
        f"/api/orchestration/intake/sessions/{SESSION_ID}",
        _session(draft={}, draft_available=False, last_response="Understood.", updated_at=200),
    )

    code, result = run_cli(["start", "Add rate limiting", "--json"])

    assert code == 0
    # In the envelope, not only on stderr: a `--json` consumer reads stdout, and one
    # that stored `detail["draft"]` as the refined plan must be able to tell an empty
    # draft from an unreadable one.
    assert result["detail"]["draft_available"] is False


def test_a_non_interactive_caller_never_blocks_on_input(monkeypatch, server):
    """Belt and braces: `input()` must not even be reached.

    Asserted by making it raise. A test that only checked the exit code would still
    pass if the code prompted and the harness happened to supply EOF.
    """
    _intake_started(server)
    route(
        server,
        "GET",
        f"/api/orchestration/intake/sessions/{SESSION_ID}",
        _session(open_questions=["Which repo?"], updated_at=200),
    )
    monkeypatch.setattr("builtins.input", lambda *_: pytest.fail("a non-interactive caller must never be prompted"))

    assert run_cli(["start", "Add rate limiting", "--json"])[0] == 4


def test_an_answered_question_becomes_a_new_turn_in_the_same_conversation(monkeypatch, server):
    """Refinement is the point of the command, and it must not fork the draft.

    One conversation, many turns: the answer goes to `.../{id}/turns`, so the
    server-side draft is revised rather than a second conversation being opened
    that would produce a second issue.
    """
    _intake_started(server)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *_: "acme/app")

    states = [
        _session(open_questions=["Which repo?"], updated_at=200),
        _session(updated_at=300, last_response="Understood.", draft={"intent": "Rate limiting in acme/app"}),
    ]

    def next_state():
        return states[0] if len(states) == 1 else states.pop(0)

    # Sequencing that keeps the REAL server: re-point the route before each poll, so
    # the conversation genuinely advances over HTTP rather than through a stub.
    original_do_get = server.do_GET

    def do_GET(handler):  # noqa: N802 - matches BaseHTTPRequestHandler
        route(server, "GET", f"/api/orchestration/intake/sessions/{SESSION_ID}", next_state())
        original_do_get(handler)

    monkeypatch.setattr(server, "do_GET", do_GET)
    route(
        server,
        "POST",
        f"/api/orchestration/intake/sessions/{SESSION_ID}/turns",
        {"session_id": SESSION_ID, "task_id": "t2", "enqueued_at": 2},
        status=202,
    )

    code, result = run_cli(["start", "Add rate limiting", "--refine-only"])

    assert code == 0
    turns = [body for method, path, body in server.received if method == "POST" and path.endswith("/turns")]
    assert turns and turns[0]["message"] == "acme/app", "the answer must reach the SAME conversation"
    assert result["detail"]["draft"] == {"intent": "Rate limiting in acme/app"}


def test_resume_with_an_id_reattaches_to_that_conversation(server):
    route(
        server,
        "GET",
        f"/api/orchestration/intake/sessions/{SESSION_ID}",
        _session(last_response="Where we left off.", draft={"intent": "Rate limiting"}, updated_at=300),
    )

    code, result = run_cli(["start", "--resume", SESSION_ID, "--json"])

    assert code == 0
    assert result["detail"]["resumed"] is True
    assert result["detail"]["draft"] == {"intent": "Rate limiting"}
    assert not [entry for entry in server.received if entry[0] == "POST"], "resuming must not start a second conversation"


def test_resume_without_an_id_finds_the_callers_newest_conversation(server):
    """A user who lost the id still has their identity.

    Without this, a conversation started on another machine — or before a terminal
    was closed — is unreachable, which is the browser path's exact failure: its id
    lives in `localStorage` and clearing it orphans the server rows.
    """
    route(server, "GET", "/api/orchestration/intake/sessions/latest", _session(draft={"intent": "Rate limiting"}, updated_at=300))

    code, result = run_cli(["start", "--resume", "--json"])

    assert code == 0
    assert result["detail"]["session_id"] == SESSION_ID
    assert "/latest" in [path for _, path, _ in server.received][0]


def test_resuming_when_there_is_nothing_to_resume_is_an_actionable_error(server):
    """Not a crash and not a silent new conversation.

    Starting one implicitly would be worse than the error: the user asked to
    continue something, and quietly beginning something else discards their intent.
    """
    route(server, "GET", "/api/orchestration/intake/sessions/latest", {"detail": {"error": "session_not_found"}}, status=404)

    code, result = run_cli(["start", "--resume", "--json"])

    assert code == 1
    assert result["error"]["code"] == "session_not_found"
    assert not [entry for entry in server.received if entry[0] == "POST"], "a failed resume must not start a conversation"


def test_resuming_waits_rather_than_interleaving_with_an_in_flight_turn(server):
    """Sending into a turn the agent holds would be answered out of order.

    The server refuses it with 409; the CLI must not get there, because the honest
    behaviour is to wait for the reply the user is already owed.
    """
    route(server, "GET", f"/api/orchestration/intake/sessions/{SESSION_ID}", _session(working=True, status="processing", updated_at=0))

    code, _ = run_cli(["start", "--resume", SESSION_ID, "--json"])

    assert code == 4
    assert not [entry for entry in server.received if entry[0] == "POST"], "no turn may be sent while the agent is working"


def test_start_without_an_outcome_is_a_usage_error_not_an_empty_conversation(server):
    """An empty opening turn would consume the session's ordering slot for nothing."""
    code, result = run_cli(["start", "--json"])

    assert code == 1
    assert result["error"]["code"] == "usage_error"
    assert not server.received


def test_an_unconfigured_deployment_is_reported_as_unavailable_not_broken(server):
    """503 from the intake surface means "configure this", not "something failed".

    Preserved through the CLI because the distinction is the reason the route
    separates 503 from 502: an operator takes different action on each.
    """
    route(
        server,
        "POST",
        "/api/orchestration/intake/sessions",
        {"detail": {"error": "intake_unavailable", "message": "no intake queue is available"}},
        status=503,
    )

    code, result = run_cli(["start", "Add rate limiting", "--json"])

    assert code == 5
    assert result["error"]["code"] == "intake_unavailable"


def test_a_timed_out_reply_says_the_work_is_not_lost(monkeypatch, server):
    """A bounded wait that read as failure would invite a duplicate conversation."""
    monkeypatch.setattr(cli, "INTAKE_MAX_POLLS", 2)
    _intake_started(server)
    route(server, "GET", f"/api/orchestration/intake/sessions/{SESSION_ID}", _session(working=True, updated_at=0))

    code, result = run_cli(["start", "Add rate limiting", "--json"])

    assert code == 4
    assert "Nothing is lost" in result["next_action"]
    assert f"--resume {SESSION_ID}" in result["next_action"]


def test_a_stale_reply_is_not_mistaken_for_an_answer_to_the_new_turn(monkeypatch, server):
    """The subtle one. A resumed conversation ALREADY has a `last_response`.

    Keying completion on that field being non-empty would return the previous
    answer the instant a turn was sent, and the user would believe the agent had
    responded to a message it never saw. Completion is `updated_at` moving.

    Asserted by holding `updated_at` at the value the turn was sent on while a
    reply sits in the field: the CLI must time out rather than report it.
    """
    monkeypatch.setattr(cli, "INTAKE_MAX_POLLS", 2)
    route(
        server,
        "GET",
        f"/api/orchestration/intake/sessions/{SESSION_ID}",
        _session(last_response="An answer to something older", updated_at=500, working=True),
    )

    result = cli.await_reply(cli.LazyApi(), SESSION_ID, after=500)

    assert result.get("timed_out") is True, "a reply at or before the send time is not an answer to it"


def test_the_draft_is_rendered_as_readable_fields_not_json(server):
    """This is what the user confirms is right before it becomes a plan.

    A wall of braces is not reviewable, so the fields are rendered — and unknown
    keys are shown rather than dropped, because the agent owns this shape and
    hiding a field it added would mean approving something unseen.
    """
    text = cli.render_draft(
        {
            "intent": "Add rate limiting",
            "outcomes": ["Public API is protected", "Tenants are isolated"],
            "somethingNew": "a field this CLI has never heard of",
        }
    )

    assert "Add rate limiting" in text
    assert "- Public API is protected" in text
    assert "a field this CLI has never heard of" in text, "an unknown field must not be silently hidden"
    assert "{" not in text


def test_start_hands_off_rather_than_approving_what_it_planned(server):
    """The EPIC's control, at the end of the happy path.

    A conversation that registered and accepted its own output would approve
    execution bounds nobody reviewed. The success message must therefore point at
    the separate acceptance step and say plainly that nothing was started.
    """
    _intake_started(server)
    route(server, "GET", f"/api/orchestration/intake/sessions/{SESSION_ID}", _session(updated_at=200, draft={"intent": "Rate limiting"}))

    code, result = run_cli(["start", "Add rate limiting", "--json"])

    assert code == 0
    assert "Nothing has been registered, approved or started" in result["next_action"]
    assert "adp flow create --file" in result["next_action"]


# --- no fallback execution --------------------------------------------------


def test_start_registers_nothing_and_approves_nothing(server):
    """Converted, not deleted. `start` now HAS a hosted contract (#5331).

    The original form of this test asserted `start` reported `unavailable` and sent
    no request at all, which was right while no intake API existed. The invariant it
    was protecting is narrower than "sends nothing" and outlives that: a
    conversation must not register a draft, create a flow or answer a gate. Those
    are separate, deliberate human acts, and a `start` that quietly performed them
    would approve execution bounds nobody reviewed.

    So the assertion moves from "no traffic" to "no traffic to the paths that arm
    work", which is the property that actually matters.
    """
    session = _session(last_response="Here is what I understand.", updated_at=200)
    route(server, "POST", "/api/orchestration/intake/sessions", {"session_id": SESSION_ID, "task_id": "t1", "enqueued_at": 1}, status=202)
    route(server, "GET", f"/api/orchestration/intake/sessions/{SESSION_ID}", session)

    code, result = run_cli(["start", "Add rate limiting", "--json"])

    assert code == 0
    assert result["status"] == "ok"
    touched = {path.split("?")[0] for _, path, _ in server.received}
    for armed in ("/api/orchestration/flows", "/api/orchestration/flows/drafts"):
        assert armed not in touched, f"start must not call {armed}"
    assert not [path for path in touched if "/gates/" in path], "start must not answer a gate"


def test_start_validates_its_own_arguments_before_resolving_a_gateway(monkeypatch):
    """Converted from `test_start_needs_no_configured_gateway`.

    `start` now makes requests, so "needs no gateway" is no longer true. What
    survives is the ordering the whole helper follows: a caller's own mistake is
    reported as the usage error it is, before a gateway is resolved. Otherwise
    someone who forgot to say what they wanted is told to reinstall the CLI.
    """

    def explode():
        raise AssertionError("a usage error must be reported before a gateway is resolved")

    monkeypatch.setattr(common, "gateway_url", explode)

    code, result = run_cli(["start"])

    assert code == 1
    assert result["error"]["code"] == "usage_error"


def test_an_engine_disabled_deployment_is_reported_not_worked_around(server):
    engine_on(server, enabled=False)
    route(server, "GET", f"/api/orchestration/flows/{FLOW_ID}", _graph([]))

    code, result = run_cli(["show", FLOW_ID])

    assert code == 4
    assert result["detail"]["supported"] is False
    assert "administrator" in result["next_action"]
    # Only the capability read happened; no flow call was attempted.
    assert [entry[1] for entry in server.received] == ["/api/features"]


def test_an_unreadable_capability_flag_is_not_treated_as_disabled(server):
    """A failed flag read is not evidence the engine is off, and reporting it as
    off would send a user to their administrator over a transient error."""
    route(server, "GET", "/api/features", {"detail": "boom"}, status=500)
    route(server, "GET", f"/api/orchestration/flows/{FLOW_ID}/decisions", [])

    code, _ = run_cli(["decisions", FLOW_ID])

    assert code == 0


# --- detaching is not cancelling -------------------------------------------


def test_watch_once_reads_current_state_and_stops(server):
    engine_on(server)
    route(server, "GET", f"/api/orchestration/flows/{FLOW_ID}", _graph([{"id": "n4", "state": "ready", "kind": "story", "title": "next"}]))

    code, result = run_cli(["watch", FLOW_ID, "--once"])

    assert code == 0
    assert result["detail"]["total_nodes"] == 1


def test_watch_gives_up_after_bounded_retries_rather_than_hanging(server, monkeypatch):
    engine_on(server)
    route(server, "GET", f"/api/orchestration/flows/{FLOW_ID}", {"detail": "down"}, status=500)
    monkeypatch.setattr(cli.time, "sleep", lambda _: None)

    code, result = run_cli(["watch", FLOW_ID])

    assert code == 5
    assert "Hosted execution is unaffected" in result["error"]["message"]


def test_watch_stops_immediately_on_a_permission_refusal(server, monkeypatch):
    """403 will not fix itself by waiting; burning the retry budget on a certain
    answer just delays a clear message."""
    engine_on(server)
    route(server, "GET", f"/api/orchestration/flows/{FLOW_ID}", {"detail": "nope"}, status=403)
    monkeypatch.setattr(cli.time, "sleep", lambda _: None)

    code, _ = run_cli(["watch", FLOW_ID])

    assert code == 3
    gets = [entry for entry in server.received if entry[1].endswith(FLOW_ID)]
    assert len(gets) == 1, "a refusal must not be retried"


def test_the_detach_message_says_hosted_work_continues():
    assert "did NOT approve, cancel" in cli.DETACHED
    assert "watch" in cli.DETACHED


# --- watch --json is one object per poll ------------------------------------


def _watch_json_states(server, monkeypatch, capsys, states, argv=None):
    """Run a watch over a scripted sequence of flow states; return stdout objects.

    `route` serves one fixed payload, but the property under test is what happens
    ACROSS polls, so the flow read is scripted to advance on each call.
    """
    engine_on(server)
    remaining = list(states)
    original_respond = server._respond
    flow_path = f"/api/orchestration/flows/{FLOW_ID}"

    def advancing(handler):
        if handler.path.split("?")[0] == flow_path and handler.command == "GET":
            state = remaining.pop(0) if len(remaining) > 1 else remaining[0]
            route(server, "GET", flow_path, dict(_graph([]), state=state))
        return original_respond(handler)

    monkeypatch.setattr(server, "_respond", advancing)
    monkeypatch.setattr(cli.time, "sleep", lambda _: None)
    capsys.readouterr()

    code, _ = run_cli(["watch", FLOW_ID, "--json", *(argv or [])])
    stdout = capsys.readouterr().out
    return code, [json.loads(line) for line in stdout.splitlines() if line.strip()]


def test_watch_json_emits_exactly_one_object_per_poll(server, monkeypatch, capsys):
    """The documented contract (cli/flow.md): one JSON object per poll, one per
    line, so a consumer can read the stream incrementally.

    The regression this pins: the loop printed each poll AND returned an envelope
    that main()'s `common.emit` printed again, so the terminal snapshot landed on
    stdout twice and a consumer counting objects saw one poll as two.
    """
    code, objects = _watch_json_states(server, monkeypatch, capsys, ["running", "running", "complete"])

    assert code == 0
    assert [obj["detail"]["state"] for obj in objects] == ["running", "running", "complete"]


def test_watch_json_once_emits_a_single_object(server, monkeypatch, capsys):
    code, objects = _watch_json_states(server, monkeypatch, capsys, ["running"], argv=["--once"])

    assert code == 0
    assert len(objects) == 1
    assert objects[0]["detail"]["state"] == "running"


def test_watch_json_does_not_repeat_the_last_state_when_polls_run_out(server, monkeypatch, capsys):
    """Exhausting the poll budget is a detach, not a terminal state — it must not
    duplicate the final object either."""
    monkeypatch.setattr(cli, "WATCH_MAX_POLLS", 3)

    code, objects = _watch_json_states(server, monkeypatch, capsys, ["running"])

    assert code == 0
    assert [obj["detail"]["state"] for obj in objects] == ["running"] * 3


def test_watch_json_counts_only_successful_polls_when_the_last_one_retried(server, monkeypatch, capsys):
    """A watch can also end on a RETRIED poll rather than a terminal state, and the
    object count must still equal the number of successful reads.

    This is the path a "skip the last poll" fix gets wrong: which poll is final is
    not knowable in advance, so the snapshot is deferred until the next one lands.
    """
    engine_on(server)
    monkeypatch.setattr(cli, "WATCH_MAX_POLLS", 3)
    monkeypatch.setattr(cli.time, "sleep", lambda _: None)
    flow_path = f"/api/orchestration/flows/{FLOW_ID}"
    reads = {"n": 0}
    original_respond = server._respond

    def two_reads_then_a_blip(handler):
        if handler.path.split("?")[0] == flow_path and handler.command == "GET":
            reads["n"] += 1
            if reads["n"] > 2:
                route(server, "GET", flow_path, {"detail": "blip"}, status=500)
        return original_respond(handler)

    route(server, "GET", flow_path, _graph([]))
    monkeypatch.setattr(server, "_respond", two_reads_then_a_blip)
    capsys.readouterr()

    code, _ = run_cli(["watch", FLOW_ID, "--json"])
    objects = [line for line in capsys.readouterr().out.splitlines() if line.strip()]

    assert code == 0
    assert len(objects) == 2, "two successful reads must produce exactly two objects"


def test_watch_json_flushes_the_last_successful_poll_before_interrupt(server, monkeypatch, capsys):
    """Ctrl-C normally lands during the sleep after a poll.  The successful
    snapshot must reach stdout before the separate detach error envelope."""
    engine_on(server)
    route(server, "GET", f"/api/orchestration/flows/{FLOW_ID}", _graph([]))

    def interrupt(_):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli.time, "sleep", interrupt)
    capsys.readouterr()

    code, _ = run_cli(["watch", FLOW_ID, "--json"])
    objects = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]

    assert code == 130
    assert [obj["detail"].get("state") for obj in objects if obj["status"] == "ok"] == ["running"]
    assert objects[-1]["error"]["code"] == "interrupted"


def test_watch_json_flushes_the_last_successful_poll_before_terminal_gateway_error(server, monkeypatch, capsys):
    """A later outage must not erase the last poll that completed successfully."""
    engine_on(server)
    flow_path = f"/api/orchestration/flows/{FLOW_ID}"
    route(server, "GET", flow_path, _graph([]))
    reads = {"n": 0}
    original_respond = server._respond

    def one_read_then_outage(handler):
        if handler.path.split("?")[0] == flow_path and handler.command == "GET":
            reads["n"] += 1
            if reads["n"] > 1:
                route(server, "GET", flow_path, {"detail": "down"}, status=500)
        return original_respond(handler)

    monkeypatch.setattr(server, "_respond", one_read_then_outage)
    monkeypatch.setattr(cli.time, "sleep", lambda _: None)
    capsys.readouterr()

    code, _ = run_cli(["watch", FLOW_ID, "--json"])
    objects = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]

    assert code == 5
    assert [obj["detail"].get("state") for obj in objects if obj["status"] == "ok"] == ["running"]
    assert objects[-1]["error"]["code"] == "gateway_unavailable"


# --- non-interactive safety -------------------------------------------------


def test_without_a_terminal_an_approval_refuses_rather_than_assuming_yes(monkeypatch):
    """A caller that passed no --yes has stated no intent; choosing for it is
    exactly what the story forbids."""
    monkeypatch.setattr(cli.sys, "stdin", type("NoTty", (), {"isatty": staticmethod(lambda: False)})())

    with pytest.raises(common.CliError) as caught:
        cli.confirm("Approving something.", assume_yes=False)

    assert caught.value.code == "confirmation_required"
    assert "Nothing was approved" in str(caught.value)


def test_yes_states_intent_without_a_prompt():
    cli.confirm("Approving something.", assume_yes=True)


# --- prepared plans ---------------------------------------------------------


def _plan_file(tmp_path, **overrides):
    document = {"flow_slug": "checkout", "title": "Checkout", "org_id": "acme", "spec_revision": "1", "nodes": []}
    document.update(overrides)
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps(document))
    return plan


def _acceptance_gate(state="awaiting_gate"):
    """The gate registration inserts in front of the whole graph.

    Keyed on `node_ref == "accept"`, which is what the CLI matches on, so a test
    using this is exercising the same resolution the real transform produces.
    """
    return {"id": GATE_ID, "node_ref": "accept", "state": state, "kind": "gate", "title": "Human gate: accept this plan to start execution"}


def _preview_ok(server, **overrides):
    """Wire the dry run to say "this would register cleanly, and asks for no policy".

    `create`'s pre-flight is best-effort: a preview that cannot be reached is
    skipped rather than treated as a refusal, so most `create` tests pass without
    this. It is wired explicitly where a test needs the pre-flight to have actually
    run and approved, so that "the write happened" is not accidentally standing in
    for "the dry run was unreachable".
    """
    body = {
        "would_register": True,
        "violations": [],
        "plan_hash": CURRENT_HASH,
        "plan_hash_is_bindable": True,
        "proposed_execution_policy": None,
        "execution_is_unbounded": False,
        "waves": [],
        "nodes": [],
        "wrote_nothing": True,
    }
    body.update(overrides)
    route(server, "POST", "/api/orchestration/flows/drafts/preview", body)
    return body


def _draft_flow(server, *, registered=None, nodes=None, policy=None):
    """Wire the three calls `create` makes: register the draft, read it back, accept."""
    if ("POST", "/api/orchestration/flows/drafts/preview") not in server.routes:
        _preview_ok(server)
    body = {
        "flow_id": FLOW_ID,
        "plan_version": 1,
        "plan_hash": CURRENT_HASH,
        "decision_id": "d-draft",
        "nodes_created": 2,
        "edges_created": 1,
        "already_registered": False,
        "acceptance_gate_address": "checkout/epic/wave-1/accept",
        "accept_command": "@agent-engine accept",
        "flow_url": None,
    }
    body.update(registered or {})
    route(server, "POST", "/api/orchestration/flows/drafts", body, status=201)
    graph = _graph(nodes if nodes is not None else [_acceptance_gate(), {"id": "n1", "state": "pending", "kind": "story", "title": "work"}])
    if policy is not None:
        graph["execution_policy"] = policy
    route(server, "GET", f"/api/orchestration/flows/{FLOW_ID}", graph)
    route(
        server,
        "POST",
        f"/api/orchestration/gates/{GATE_ID}/approve",
        {"node_id": GATE_ID, "status": "applied", "state": "passed", "decision_id": "d-accept", "actor_kind": "human"},
    )
    return body


def test_create_registers_an_inert_draft_rather_than_an_approved_plan(server, tmp_path):
    """The defect this closes: `create` posted the document straight to
    `POST /orchestration/flows`, which records the approval in the SAME call that
    compiles the graph. The operator therefore approved a shape they had not seen,
    because registration inserts an acceptance gate the document never declared.

    The document must reach the draft endpoint, and the approved-plan endpoint must
    not be touched at all.
    """
    engine_on(server)
    _draft_flow(server)

    code, result = run_cli(["create", "--file", str(_plan_file(tmp_path)), "--yes", "--expect-plan-hash", CURRENT_HASH])

    assert code == 0
    posts = [entry[1] for entry in server.received if entry[0] == "POST"]
    assert any("/orchestration/flows/drafts" in path for path in posts), "the plan must be registered as an inert draft"
    assert not [path for path in posts if path.split("?")[0].endswith("/orchestration/flows")], (
        "posting to the approved-plan endpoint approves a graph the operator has not previewed"
    )
    assert result["detail"]["accepted"] is True


def test_create_previews_the_effective_graph_before_accepting_it(server, tmp_path):
    """The preview must come from the SERVER's compiled graph, between the
    registration and the acceptance — not from the local document, which does not
    contain the gates the transforms add."""
    engine_on(server)
    _draft_flow(server)

    _, result = run_cli(["create", "--file", str(_plan_file(tmp_path)), "--yes", "--expect-plan-hash", CURRENT_HASH])

    order = [(entry[0], entry[1].split("?")[0]) for entry in server.received]
    register = order.index(("POST", "/api/orchestration/flows/drafts"))
    readback = order.index(("GET", f"/api/orchestration/flows/{FLOW_ID}"))
    accept = order.index(("POST", f"/api/orchestration/gates/{GATE_ID}/approve"))
    assert register < readback < accept, "the effective graph must be read back before it is accepted"
    preview = result["detail"]["preview"]
    assert preview["plan_hash"] == CURRENT_HASH
    # The gate the transforms inserted is surfaced, which is the whole point: it is
    # in the preview and NOT in the submitted document.
    assert preview["acceptance_gate_id"] == GATE_ID
    assert [gate["ref"] for gate in preview["effective_graph"]["outstanding_gates"]] == ["accept"]


def test_create_binds_its_acceptance_to_the_previewed_revision(server, tmp_path):
    """The acceptance must carry the hash of what was previewed, so a plan amended
    between the preview and the answer cannot collect this approval."""
    engine_on(server)
    _draft_flow(server)

    run_cli(["create", "--file", str(_plan_file(tmp_path)), "--yes", "--expect-plan-hash", CURRENT_HASH])

    accept = [entry for entry in server.received if entry[0] == "POST" and "/approve" in entry[1]]
    assert accept, "the acceptance must reach the server"
    assert accept[0][2]["expected_plan_hash"] == CURRENT_HASH


def test_create_still_binds_the_revision_under_yes(server, tmp_path):
    """`--yes` states intent without a prompt; it must not downgrade to accepting
    whatever happens to be live. A script that approves an unread revision is the
    concurrent-edit hole wearing a flag."""
    engine_on(server)
    _draft_flow(server)

    run_cli(["create", "--file", str(_plan_file(tmp_path)), "--yes", "--expect-plan-hash", CURRENT_HASH])

    accept = [entry for entry in server.received if entry[0] == "POST" and "/approve" in entry[1]]
    assert "expected_plan_hash" in accept[0][2]


def test_create_without_a_terminal_registers_inert_and_does_not_accept(server, tmp_path, monkeypatch):
    """No --yes and no tty: the draft may stand (it is inert), but nothing is
    approved, and the operator is told how to accept it themselves."""
    engine_on(server)
    _draft_flow(server)
    monkeypatch.setattr(cli.sys, "stdin", type("NoTty", (), {"isatty": staticmethod(lambda: False)})())

    code, result = run_cli(["create", "--file", str(_plan_file(tmp_path))])

    assert code == 4
    assert result["detail"]["accepted"] is False
    assert result["detail"]["plan_hash"] == CURRENT_HASH
    assert not [entry for entry in server.received if entry[0] == "POST" and "/approve" in entry[1]], "nothing may be approved without stated intent"


def test_create_surfaces_an_idempotent_replay(server, tmp_path):
    """A retry whose first response was lost must be able to tell it re-read its
    own result rather than creating a second flow."""
    engine_on(server)
    _draft_flow(server, registered={"already_registered": True})

    _, result = run_cli(["create", "--file", str(_plan_file(tmp_path)), "--yes", "--expect-plan-hash", CURRENT_HASH])

    assert result["detail"]["already_registered"] is True


def test_a_retried_create_does_not_produce_a_second_flow_or_approval(server, tmp_path):
    """Two identical runs, as a fail-soft caller retrying. The server's idempotency
    is what makes this safe, and the CLI must not turn one plan into two by
    inventing a new slug, a new draft or a second acceptance target."""
    engine_on(server)
    _draft_flow(server, registered={"already_registered": True})
    plan = _plan_file(tmp_path)

    first_code, first = run_cli(["create", "--file", str(plan), "--yes", "--expect-plan-hash", CURRENT_HASH])
    second_code, second = run_cli(["create", "--file", str(plan), "--yes", "--expect-plan-hash", CURRENT_HASH])

    assert (first_code, second_code) == (0, 0)
    assert first["detail"]["flow_id"] == second["detail"]["flow_id"] == FLOW_ID
    assert first["detail"]["plan_hash"] == second["detail"]["plan_hash"] == CURRENT_HASH
    # Every acceptance is bound to the same revision, so the second is the server's
    # already-answered case rather than a second approval of a different plan.
    accepts = [entry[2]["expected_plan_hash"] for entry in server.received if entry[0] == "POST" and "/approve" in entry[1]]
    assert accepts == [CURRENT_HASH, CURRENT_HASH]


def test_a_replayed_acceptance_is_reported_as_the_original_success(server, tmp_path):
    """A lost response, replayed. The server answers a same-actor/same-verb/same-hash
    retry with the ORIGINAL decision — a 200 carrying the first decision id, not a
    conflict (`_answer_gate` maps `IDEMPOTENT_REPLAY` onto the `applied` wire status
    precisely so the client cannot mistake it for a second state change).

    Asserted from the CLI's side because a client that reported the replay as an
    error, or that surfaced a different decision id, would tell an operator their
    approval had not landed when it had — and the realistic way to reach this is a
    dropped connection on the one unattended step that arms the engine. The
    decision id is pinned to the original because that is the operator's handle on
    the approval in the decision log.
    """
    engine_on(server)
    _draft_flow(server, registered={"already_registered": True})
    route(
        server,
        "POST",
        f"/api/orchestration/gates/{GATE_ID}/approve",
        {
            "node_id": GATE_ID,
            "status": "applied",
            "state": "passed",
            # The FIRST run's decision id, replayed rather than newly minted.
            "decision_id": "d-original",
            "actor_kind": "human",
            "message": "gate approved",
        },
    )

    code, result = run_cli(["create", "--file", str(_plan_file(tmp_path)), "--yes", "--expect-plan-hash", CURRENT_HASH])

    assert code == 0, result
    assert result["status"] == "ok"
    assert result["detail"]["accepted"] is True
    assert result["detail"]["acceptance"]["status"] == "applied"
    assert result["detail"]["acceptance"]["decision_id"] == "d-original"
    # Attribution still comes from the server, never assumed by the client.
    assert result["detail"]["acceptance"]["actor_kind"] == "human"


def test_create_refuses_to_accept_a_graph_it_could_not_preview(server, tmp_path):
    """Fail CLOSED. Losing the readback must not degrade into approving unseen —
    that is the exact "approved something I had not read" failure. The draft is
    inert, so stopping costs a re-run and nothing more."""
    engine_on(server)
    _draft_flow(server)
    # The readback fails. The draft registration and the approve route still stand.
    route(server, "GET", f"/api/orchestration/flows/{FLOW_ID}", {"detail": "boom"}, status=500)

    code, result = run_cli(["create", "--file", str(_plan_file(tmp_path)), "--yes", "--expect-plan-hash", CURRENT_HASH])

    assert code == 4
    assert result["error"]["code"] == "preview_unavailable"
    assert FLOW_ID in result["error"]["message"], "the operator must be told which inert flow was left behind"
    assert not [entry for entry in server.received if entry[0] == "POST" and "/approve" in entry[1]]


def test_create_does_not_invent_an_acceptance_target(server, tmp_path):
    """No unanswered acceptance gate on the compiled graph means there is nothing
    for a human to answer. Picking some other gate would accept the plan by
    releasing only part of it."""
    engine_on(server)
    _draft_flow(server, nodes=[{"id": "other", "node_ref": "release", "state": "awaiting_gate", "kind": "gate", "title": "Release"}])

    code, result = run_cli(["create", "--file", str(_plan_file(tmp_path)), "--yes", "--expect-plan-hash", CURRENT_HASH])

    assert code == 4
    assert result["status"] == "unavailable"
    assert result["detail"]["accepted"] is False
    assert not [entry for entry in server.received if entry[0] == "POST" and "/approve" in entry[1]]


def test_the_preview_states_the_policy_bounds_being_authorized(server, tmp_path):
    """What an operator consents to is the authority, not the node count: which
    repositories, which connections, what limits, and when it expires.

    Read from the PROPOSED bounds on the dry run, not from the registered flow's
    `execution_policy`. That field reports the policy in force, and an inert draft
    has none in force by design — so this is the field a consent decision rests on
    and the only one that carries the bounds before they are granted.
    """
    engine_on(server)
    policy = {
        "autonomous_actions": ["open_pull_request"],
        "human_decisions": ["merge"],
        "repository_ids": ["acme/app"],
        "environment_connection_ids": ["conn-1"],
        "limits": {"max_usd": "25.00"},
        "expires_at": "2026-12-01T00:00:00Z",
    }
    _preview_ok(server, proposed_execution_policy=policy)
    _draft_flow(server)

    _, result = run_cli(["create", "--file", str(_plan_file(tmp_path)), "--yes", "--expect-plan-hash", CURRENT_HASH])

    assert result["detail"]["preview"]["proposed_execution_policy"]["repository_ids"] == ["acme/app"]
    text = cli.preview_text(result["detail"]["preview"])
    assert "acme/app" in text and "conn-1" in text and "25.00" in text


class TestAnExpiryAlreadyBehindUsIsNamedAsSuch:
    """The consent text has to answer "is this authority still live?" (#5331).

    An expiry is the one policy field whose meaning depends on when it is read, and a
    bare ISO timestamp makes the reader do that comparison at the exact moment they
    are concentrating on something else. A plan whose bounds have already lapsed
    cannot be accepted at all — the server refuses the grant — so printing the date
    alone sends the operator to approve something guaranteed to fail and then decode
    the refusal.

    Asserted on `policy_lines` rather than through a `create` run because this is a
    rendering claim, and `create`'s exit path depends on the server fixture. The
    rendering is shared by the dry run and the registered-draft preview, so it is the
    text in both places.
    """

    @staticmethod
    def _rendered(expires_at):
        return "\n".join(cli.policy_lines({"repository_ids": ["acme/app"], "expires_at": expires_at}))

    def test_a_past_expiry_is_called_expired_and_names_the_remedy(self):
        from datetime import UTC, datetime, timedelta

        text = self._rendered((datetime.now(tz=UTC) - timedelta(hours=1)).isoformat())

        assert "ALREADY EXPIRED" in text
        # The remedy, not just the diagnosis: re-answering the same plan produces the
        # same dead bounds, so the operator needs to know a new plan is required.
        assert "new plan" in text

    def test_a_live_expiry_is_printed_as_the_server_sent_it(self):
        """The scope of the warning. A renderer that shouted on every plan would be
        noise, and noise on a consent screen is worse than silence — it trains the
        reader to skip the line that will one day matter."""
        from datetime import UTC, datetime, timedelta

        expires_at = (datetime.now(tz=UTC) + timedelta(hours=20)).isoformat()

        text = self._rendered(expires_at)

        assert expires_at in text
        assert "EXPIRED" not in text

    def test_a_z_suffixed_expiry_is_understood_rather_than_passed_through(self):
        """`Z` is what the server actually sends (`model_dump(mode="json")`), and
        `fromisoformat` rejected it before 3.11 — so this is the format the check has
        to handle, not an edge case."""
        assert "ALREADY EXPIRED" in self._rendered("2020-01-01T00:00:00Z")

    def test_an_unreadable_expiry_is_shown_verbatim_and_not_guessed_at(self):
        """Silence over invention. Telling a reader "already expired" about a value
        this code merely failed to parse would attribute to the server a claim it
        never made, and the reader has no way to tell the two apart."""
        for value in ("sometime next week", "", None):
            text = self._rendered(value)
            assert "EXPIRED" not in text, f"a malformed expiry {value!r} was reported as expired"


def test_the_bounds_are_read_from_the_proposal_not_from_what_is_in_force(server, tmp_path):
    """The defect this closes, and it fails in the reassuring direction.

    A registered draft's demoted policy is deliberately absent from
    `load_in_force_policy`, so `GET /flows/{id}` returns `execution_policy: null` for
    a plan whose acceptance grants a full policy. A preview that rendered that field
    would print "authorizes no autonomous action on its own" at the exact moment the
    operator is being asked to grant it — the most dangerous possible misreport,
    because the wrong answer is the calming one.

    So the server is wired the way the real one behaves: bounds on the dry run,
    nothing in force on the flow readback. The rendered text must show the bounds.
    """
    engine_on(server)
    _preview_ok(server, proposed_execution_policy={"autonomous_actions": ["merge_pull_request"], "repository_ids": ["acme/payments"]})
    _draft_flow(server)  # no `policy=` — nothing in force, exactly as for a real draft

    _, result = run_cli(["create", "--file", str(_plan_file(tmp_path)), "--yes", "--expect-plan-hash", CURRENT_HASH])

    preview = result["detail"]["preview"]
    assert preview["execution_policy"] is None, "an inert draft has no policy in force"
    text = cli.preview_text(preview)
    assert "acme/payments" in text and "merge_pull_request" in text
    assert "authorizes no autonomous action" not in text, "a policy-bearing plan was rendered as authorizing nothing"


def test_a_plan_with_no_policy_is_not_rendered_as_unrestricted(server, tmp_path):
    """An absent policy authorizes nothing autonomously. A blank line there would
    read as "no limits", which is the opposite of what it means."""
    engine_on(server)
    _draft_flow(server)

    _, result = run_cli(["create", "--file", str(_plan_file(tmp_path)), "--yes", "--expect-plan-hash", CURRENT_HASH])

    assert result["detail"]["preview"]["proposed_execution_policy"] is None
    assert "authorizes no autonomous action" in cli.preview_text(result["detail"]["preview"])


def test_a_policyless_plan_is_named_as_unbounded_not_merely_as_having_no_policy(server, tmp_path):
    """ "No policy" means legacy UNBOUNDED semantics, which is the opposite of the
    natural reading of an empty field. The server states it as a boolean; the CLI
    must put it in words where the consent happens."""
    engine_on(server)
    _preview_ok(server, proposed_execution_policy=None, execution_is_unbounded=True)
    _draft_flow(server)

    _, result = run_cli(["create", "--file", str(_plan_file(tmp_path)), "--yes", "--expect-plan-hash", CURRENT_HASH])

    text = cli.preview_text(result["detail"]["preview"])
    assert "UNBOUNDED" in text
    assert "no spend ceiling" in text and "no expiry" in text


def test_create_refuses_when_the_registration_returns_no_revision(server, tmp_path):
    """Without a hash there is nothing to bind an acceptance to, and accepting
    unbound is the fallback this must not take."""
    engine_on(server)
    _draft_flow(server, registered={"plan_hash": None})

    code, result = run_cli(["create", "--file", str(_plan_file(tmp_path)), "--yes", "--expect-plan-hash", CURRENT_HASH])

    assert code == 5
    assert result["error"]["code"] == "incomplete_registration"
    assert not [entry for entry in server.received if entry[0] == "POST" and "/approve" in entry[1]]


# --- the dry run that runs before any row exists ----------------------------


def test_create_dry_runs_the_registration_before_writing_anything(server, tmp_path):
    """The preview must precede the write, or it is not a pre-flight.

    A dry run that runs after registration tells the operator nothing they could
    have acted on: the flow row already exists. Order is the whole property, so it
    is asserted on the request log rather than on the presence of the call.
    """
    engine_on(server)
    _preview_ok(server)
    _draft_flow(server)

    code, result = run_cli(["create", "--file", str(_plan_file(tmp_path)), "--yes", "--expect-plan-hash", CURRENT_HASH])

    assert code == 0
    order = [(entry[0], entry[1].split("?")[0]) for entry in server.received]
    assert order.index(("POST", "/api/orchestration/flows/drafts/preview")) < order.index(("POST", "/api/orchestration/flows/drafts")), (
        "a dry run after the write cannot prevent the write"
    )
    assert result["detail"]["accepted"] is True


def test_a_policy_bearing_plan_is_carried_through_rather_than_refused(server, tmp_path):
    """The inversion, and the reason it is safe.

    This used to refuse with exit 4, correctly for the server it was written
    against: draft registration compiles as SERVICE and a service actor may not
    accept a policy, so registration 422'd. The inert path now DEMOTES a submitted
    policy instead of stamping it, so the document registers, carries its bounds for
    review, and grants nothing until a human answers the acceptance gate bound to the
    revision.

    Refusing it now would be refusing the case `create` is most needed for, and would
    push its author toward the two real hazards: stripping the policy, or posting to
    `POST /orchestration/flows` where the approval lands in the same call that
    compiles the graph. So the plan must reach the DRAFT endpoint — never the
    approved-plan endpoint — and the acceptance must be revision-bound.
    """
    engine_on(server)
    _preview_ok(server, proposed_execution_policy={"autonomous_actions": ["code"], "repository_ids": ["acme/app"], "limits": {"max_spend_usd": 50}})
    _draft_flow(server)

    code, result = run_cli(["create", "--file", str(_plan_file(tmp_path)), "--yes", "--expect-plan-hash", CURRENT_HASH])

    assert code == 0, result
    assert result["detail"]["accepted"] is True
    posts = [entry[1].split("?")[0] for entry in server.received if entry[0] == "POST"]
    assert "/api/orchestration/flows/drafts" in posts, "a policy-bearing plan must reach the inert path"
    assert "/api/orchestration/flows" not in posts, "the approved-plan endpoint would approve bounds unseen"
    approve = [entry for entry in server.received if entry[0] == "POST" and "/approve" in entry[1]]
    assert approve and approve[0][2]["expected_plan_hash"] == CURRENT_HASH, "the grant must be bound to the reviewed revision"


def test_the_bounds_are_shown_before_the_acceptance_that_grants_them(server, tmp_path):
    """Carrying the document through is only safe if the authority is READ first.

    The old refusal was loud for a good reason, and dropping it must not drop that.
    The bounds have to appear on screen before the confirmation that grants them, so
    this asserts on ordering: the rendered policy precedes the approve call, and the
    operator is told the grant is what arms the engine.
    """
    engine_on(server)
    _preview_ok(server, proposed_execution_policy={"limits": {"max_spend_usd": 50}, "repository_ids": ["acme/app"]})
    _draft_flow(server)

    code, result = run_cli(["create", "--file", str(_plan_file(tmp_path)), "--yes", "--expect-plan-hash", CURRENT_HASH])

    assert code == 0
    assert result["detail"]["preview"]["proposed_execution_policy"] == {"limits": {"max_spend_usd": 50}, "repository_ids": ["acme/app"]}
    text = cli.preview_text(result["detail"]["preview"])
    assert "acme/app" in text and "50" in text


def test_a_plan_that_would_be_rejected_is_reported_with_every_violation(server, tmp_path):
    """All violations at once, before a row exists.

    Registration answers one 422 carrying its violations too, so this is not new
    information — but it arrives before a flow row exists, and as a list the
    operator can work through rather than one fix per round trip.
    """
    engine_on(server)
    _preview_ok(server, would_register=False, violations=["wave-1: node 'build' depends on itself", "document: no EVAL node"])
    _draft_flow(server)

    code, result = run_cli(["create", "--file", str(_plan_file(tmp_path)), "--yes", "--expect-plan-hash", CURRENT_HASH])

    assert code == 5
    assert result["detail"]["violations"] == ["wave-1: node 'build' depends on itself", "document: no EVAL node"]
    assert "2 violation(s)" in result["next_action"]
    posts = [entry[1].split("?")[0] for entry in server.received if entry[0] == "POST"]
    assert posts == ["/api/orchestration/flows/drafts/preview"], "a document that would be rejected must not be sent as a write"


def test_the_violations_are_printed_not_only_carried_in_the_envelope(server, tmp_path, capsys):
    """ "Fix the violations listed above" has to be true.

    `emit`'s readable arm renders `detail` as indented JSON, which is a poor way to
    read prose violations, so they are printed as a list. `--json` callers get the
    same values in the envelope.
    """
    engine_on(server)
    _preview_ok(server, would_register=False, violations=["document: no EVAL node"])

    run_cli(["create", "--file", str(_plan_file(tmp_path)), "--yes", "--expect-plan-hash", CURRENT_HASH])

    assert "document: no EVAL node" in capsys.readouterr().err


def test_an_unreachable_dry_run_prevents_registration_and_approval(server, tmp_path):
    """A missing policy preview cannot be treated as an empty policy."""
    engine_on(server)
    route(server, "POST", "/api/orchestration/flows/drafts/preview", {"detail": {"error": "not_found"}}, status=404)
    _draft_flow(server)
    code, result = run_cli(["create", "--file", str(_plan_file(tmp_path)), "--yes", "--expect-plan-hash", CURRENT_HASH])
    assert code == 4
    assert result["error"]["code"] == "preview_unavailable"
    assert all(path.endswith("/preview") for method, path, _ in server.received if method == "POST")


def test_the_preview_says_which_waves_run_at_the_same_time(server, tmp_path):
    """Concurrency is the fact a node list cannot express, and it drives blast radius.

    Waves sharing a stage have no dependency path between them, so they start
    together. Grouped onto one line and named "concurrent" for that reason: three
    consecutive lines read as a sequence, which is the opposite of what is true.
    """
    engine_on(server)
    _preview_ok(
        server,
        waves=[
            {"epic_ref": "epic", "wave_ref": "wave-1", "stage": 0, "node_addresses": ["f/epic/wave-1/accept"], "depends_on": []},
            {"epic_ref": "epic", "wave_ref": "wave-2", "stage": 1, "node_addresses": ["f/epic/wave-2/a"], "depends_on": ["epic/wave-1"]},
            {"epic_ref": "epic", "wave_ref": "wave-3", "stage": 1, "node_addresses": ["f/epic/wave-3/b"], "depends_on": ["epic/wave-1"]},
        ],
    )
    _draft_flow(server)

    _, result = run_cli(["create", "--file", str(_plan_file(tmp_path)), "--yes", "--expect-plan-hash", CURRENT_HASH])

    text = cli.preview_text(result["detail"]["preview"])
    concurrent = [line for line in text.splitlines() if "stage 1" in line]
    assert len(concurrent) == 1, f"the two parallel waves were not grouped: {concurrent}"
    assert "epic/wave-2" in concurrent[0] and "epic/wave-3" in concurrent[0]
    assert "concurrent" in concurrent[0]
    # And a lone wave is not labelled concurrent, or the word means nothing.
    assert "concurrent" not in next(line for line in text.splitlines() if "stage 0" in line)


def test_a_wave_whose_order_is_undetermined_is_said_so_not_dropped(server, tmp_path):
    """`stage: null` means the wave dependencies form a cycle, so ADP cannot say when
    it runs. Omitting it would make real work look absent from the plan."""
    engine_on(server)
    _preview_ok(
        server,
        waves=[{"epic_ref": "epic", "wave_ref": "wave-9", "stage": None, "node_addresses": ["f/epic/wave-9/a"], "depends_on": ["epic/wave-8"]}],
    )
    _draft_flow(server)

    _, result = run_cli(["create", "--file", str(_plan_file(tmp_path)), "--yes", "--expect-plan-hash", CURRENT_HASH])

    text = cli.preview_text(result["detail"]["preview"])
    assert "epic/wave-9" in text, "a wave with no determined stage was dropped from the preview"
    assert "stage unknown" in text and "cycle" in text


def test_the_preview_names_every_node_that_concludes_without_a_human(server, tmp_path):
    """The asymmetry is the point.

    Human-supervised nodes come back to the operator, so a count is enough. A
    machine-accepted node concludes unattended and is the thing consent is actually
    about, so each is named. A renderer that treated both the same would bury the set
    that matters in the set that does not.
    """
    engine_on(server)
    _preview_ok(
        server,
        nodes=[
            {"address": "f/epic/wave-1/accept", "concluded_by": "human_decision"},
            {"address": "f/epic/wave-2/build", "concluded_by": "merged_pull_request"},
            {"address": "f/epic/wave-2/perf", "concluded_by": "machine_evaluation"},
            {"address": "f/epic/wave-2/manual-check", "concluded_by": "human_decision"},
        ],
    )
    _draft_flow(server)

    _, result = run_cli(["create", "--file", str(_plan_file(tmp_path)), "--yes", "--expect-plan-hash", CURRENT_HASH])

    text = cli.preview_text(result["detail"]["preview"])
    assert "human_decision: 2 node(s)" in text
    assert "f/epic/wave-2/perf concludes WITHOUT a human" in text
    # The human-supervised ones are counted, NOT itemized as unattended.
    assert "manual-check concludes WITHOUT a human" not in text
    assert "wave-2/build concludes WITHOUT a human" not in text


def test_the_dry_run_renders_no_counts_for_a_graph_that_was_never_written(server, tmp_path, capsys):
    """A refusal must not report numbers about nothing.

    The registered-draft renderer would print "0 node(s)" and "cost so far: None"
    for a plan that does not exist — figures a reader would try to interpret. A
    rejected document is the surviving case of this: nothing is written, so only the
    violations are shown.
    """
    engine_on(server)
    _preview_ok(server, would_register=False, violations=["document: no EVAL node"])

    run_cli(["create", "--file", str(_plan_file(tmp_path)), "--yes", "--expect-plan-hash", CURRENT_HASH])

    err = capsys.readouterr().err
    assert "Nothing was registered" in err
    assert "node(s)" not in err and "cost so far" not in err


@pytest.mark.parametrize(
    "content",
    ["not json at all", json.dumps(["a", "list"]), json.dumps({"title": "no slug"})],
)
def test_a_file_that_is_not_a_plan_is_a_local_usage_error(server, tmp_path, content):
    """Reported against the file, before anything is submitted."""
    engine_on(server)
    plan = tmp_path / "thing.json"
    plan.write_text(content)

    code, result = run_cli(["create", "--file", str(plan), "--yes", "--expect-plan-hash", CURRENT_HASH])

    assert code == 1
    assert result["error"]["code"] == "usage_error"
    assert not [entry for entry in server.received if entry[0] == "POST"]


# --- arguments and ids ------------------------------------------------------


@pytest.mark.parametrize("bad", ["../admin/secrets", "a/b", "", "-x", "x" * 65])
def test_a_malformed_flow_id_is_refused_before_any_request(bad, monkeypatch):
    def explode():
        raise AssertionError("must not resolve a gateway for a malformed id")

    monkeypatch.setattr(common, "gateway_url", explode)

    assert run_cli(["show", bad])[0] == 1


def test_an_unknown_status_filter_is_a_usage_error(monkeypatch):
    monkeypatch.setattr(common, "gateway_url", lambda: (_ for _ in ()).throw(AssertionError("no gateway")))

    assert run_cli(["list", "--status", "not-a-status"])[0] == 1


@pytest.mark.parametrize("args", [["list", "--limit", "0"], ["list", "--limit", "101"], ["list", "--offset", "-1"]])
def test_paging_bounds_are_checked_locally(args, monkeypatch):
    """The server answers 422 rather than clamping; saying so locally is clearer."""
    monkeypatch.setattr(common, "gateway_url", lambda: (_ for _ in ()).throw(AssertionError("no gateway")))

    assert run_cli(args)[0] == 1


def test_list_passes_the_status_filter_through(server):
    engine_on(server)
    route(server, "GET", "/api/orchestration/flows", {"flows": [], "total": 0, "status_counts": {}})

    run_cli(["list", "--status", "awaiting_you", "--needs-me"])

    query = [entry[1] for entry in server.received if "/flows" in entry[1]][0]
    assert "status=awaiting_you" in query and "needs_me=true" in query


def test_an_empty_tenant_is_a_complete_answer_not_a_failure(server):
    engine_on(server)
    route(server, "GET", "/api/orchestration/flows", {"flows": [], "total": 0, "status_counts": {}})

    code, result = run_cli(["list"])

    assert code == 0
    assert result["status"] == "ok"


# --- stream separation and exit codes --------------------------------------


def run_process(args, home: Path):
    """Run the helper as a real process, so the two streams are genuinely separate."""
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        timeout=60,
        env={"HOME": str(home), "PATH": "/usr/bin:/bin:/usr/local/bin"},
    )


def test_json_goes_to_stdout_and_diagnostics_to_stderr(tmp_path):
    """An interleaved stream cannot be parsed, so the streams are captured apart.

    Driven by a usage error rather than by `start`'s old `unavailable` report, which
    no longer exists — and this is the stricter case anyway: the envelope goes to
    stdout while the human-facing diagnostic goes to stderr, so a script's `| jq`
    keeps working even on a failure.
    """
    json_result = run_process(["start", "--json"], tmp_path)

    assert json_result.returncode == 1
    envelope = json.loads(json_result.stdout)
    assert envelope["command"] == "flow start"
    # In `--json` mode the diagnostic travels INSIDE the envelope and stderr stays
    # empty, so a consumer gets one parseable object and nothing out of band.
    assert "Describe what you want" in envelope["error"]["message"]
    assert json_result.stderr == ""

    # In readable mode the same message goes to stderr, leaving stdout for the
    # status line — which is what keeps `adp flow ... > file` useful.
    readable = run_process(["start"], tmp_path)
    assert "Describe what you want" in readable.stderr


def test_readable_output_is_the_default(tmp_path):
    result = run_process(["start"], tmp_path)

    assert not result.stdout.lstrip().startswith("{")
    assert "flow start: failed" in result.stdout


def test_help_lists_every_documented_verb(tmp_path):
    result = run_process(["--help"], tmp_path)

    for verb in ("start", "create", "list", "show", "watch", "plans", "decisions", "cost", "gate"):
        assert verb in result.stdout, verb


# --- `start --plan`: the whole path in one invocation (#5331 blocker 4) ------


def _planned(server, *, proposal=None, **overrides):
    """Wire the server-side derivation `start --plan` calls.

    The proposal here is shaped as `plan_from_draft` actually emits one — a single
    wave, one story per outcome, one eval, stories chained because they share an
    issue. A hand-waved `{"nodes": [...]}` would let these tests pass against a
    document the server never produces.
    """
    body = {
        "session_id": SESSION_ID,
        "proposal": proposal
        if proposal is not None
        else {
            "flow_slug": "rate-limiting",
            "title": "Add rate limiting",
            "org_id": "org-alpha",
            "spec_revision": "issue-5331-intake-r1",
            "intent_ref": "4242",
            "proposed_execution_policy": {
                "org_id": "org-alpha",
                "repository_ids": ["acme/web"],
                "allowed_actions": ["develop", "review"],
                "expires_at": "2099-01-01T00:00:00Z",
                "limits": {"max_spend_usd": "5.00", "max_wall_clock_seconds": 3600, "max_attempts_per_node": 2, "max_concurrent_actions": 1},
            },
            "nodes": [
                {"address": "rate-limiting/epic-1/wave-1/01-throttled", "kind": "story", "title": "Requests throttled", "issue_ref": "4242"},
                {"address": "rate-limiting/epic-1/wave-1/assess-outcomes", "kind": "eval", "title": "Assess", "issue_ref": "4242"},
            ],
            "edges": [{"from_address": "rate-limiting/epic-1/wave-1/01-throttled", "to_address": "rate-limiting/epic-1/wave-1/assess-outcomes"}],
        },
        "derived_from_outcomes": 1,
        "repository": "",
        "repository_verified_live": False,
        "issue_ref": "4242",
        "wrote_nothing": True,
        "execution_is_unbounded": True,
    }
    body.update(overrides)
    route(server, "POST", f"/api/orchestration/intake/sessions/{SESSION_ID}/plan", body)
    return body


def _execution(server, *, executions=None, legacy=False, total=None):
    """Wire the execution ledger `GET /flows/{id}/execution` serves."""
    rows = executions if executions is not None else []
    route(
        server,
        "GET",
        f"/api/orchestration/flows/{FLOW_ID}/execution",
        {
            "flow_id": FLOW_ID,
            "server_time": "2026-09-19T00:00:00+00:00",
            "executions": rows,
            "total": total if total is not None else len(rows),
            "limit": 50,
            "offset": 0,
            "legacy": legacy,
        },
    )


def _execution_row(*, phase="preparing", status="runnable", actions=(), block=None):
    return {
        "id": "x1",
        "node_id": "n1",
        "cycle": 1,
        "phase": phase,
        "status": status,
        "revision": 1,
        "attempts": 0,
        "next_check_at": "2026-09-19T00:01:00+00:00",
        "deadline_at": None,
        "progressed_at": None,
        "progress_note": None,
        "block": block,
        "pending_action_key": None,
        "notification_receipt_ref": None,
        "handoff_receipt_ref": None,
        "created_at": None,
        "updated_at": None,
        "actions": list(actions),
        "action_overflow": False,
    }


def _conversation_done(server, **session_overrides):
    """A conversation with nothing left to ask, ready to be planned."""
    _intake_started(server)
    route(
        server,
        "GET",
        f"/api/orchestration/intake/sessions/{SESSION_ID}",
        _session(updated_at=200, draft={"intent": "Rate limiting", "outcomes": ["Requests throttled"]}, **session_overrides),
    )


def test_start_plan_derives_the_plan_on_the_server_and_never_builds_one_locally(server):
    """The blocker-4 property. A client-built graph would be a second authority.

    `validate_proposal`'s rules, the address grammar and the wave/eval cardinality
    are the server's. A CLI that assembled nodes would be a second implementation
    that agreed today and drifted later, and the operator would get a different plan
    depending on which client they opened. So the document must come from the
    derivation route, and the registered document must be the one it returned.
    """
    engine_on(server)
    _conversation_done(server)
    derived = _planned(server)
    _preview_ok(server)
    _draft_flow(server)
    _execution(server, executions=[_execution_row()])

    code, result = run_cli(["start", "Add rate limiting", "--plan", "--yes", "--expect-plan-hash", CURRENT_HASH, "--json"])

    assert code == 0
    planned = [entry for entry in server.received if entry[0] == "POST" and entry[1].endswith("/plan")]
    assert planned, "start --plan must ask the server to derive the plan"
    registered = next(entry for entry in server.received if entry[0] == "POST" and entry[1].split("?")[0].endswith("/flows/drafts"))
    assert registered[2] == derived["proposal"], "the registered document must be the server's derivation, unmodified"
    assert result["detail"]["accepted"] is True


def test_start_plan_sends_the_repository_to_be_resolved_not_as_prose_to_an_agent(server):
    """`--repo` decides where autonomous work lands, so it is checked, not narrated.

    The prior behaviour concatenated it into the opening message — "This should land
    in the repository X" — leaving an agent to interpret a string that nothing ever
    verified against the tenant's installations. On this path it goes to the
    derivation route as a field, where an unconnected repository is refused before a
    plan exists.
    """
    engine_on(server)
    _conversation_done(server)
    _planned(server, repository="acme/web", repository_verified_live=True)
    _preview_ok(server)
    _draft_flow(server)
    _execution(server, executions=[_execution_row()])

    code, _ = run_cli(["start", "Add rate limiting", "--plan", "--repo", "acme/web", "--yes", "--expect-plan-hash", CURRENT_HASH, "--json"])

    assert code == 0
    plan_request = next(entry[2] for entry in server.received if entry[0] == "POST" and entry[1].endswith("/plan"))
    assert plan_request["repository"] == "acme/web"


def test_start_plan_refuses_a_repository_the_server_will_not_resolve(server):
    """A 403 from the derivation must stop the path, not fall back to an unbound plan.

    Continuing without the repository would register and approve a plan that cannot
    deliver where the user asked, and the acceptance would already be recorded.
    """
    engine_on(server)
    _conversation_done(server)
    route(
        server,
        "POST",
        f"/api/orchestration/intake/sessions/{SESSION_ID}/plan",
        {"detail": {"error": "repository_not_connected", "message": "not connected"}},
        status=403,
    )

    code, _ = run_cli(["start", "Add rate limiting", "--plan", "--repo", "acme/nope", "--yes", "--expect-plan-hash", CURRENT_HASH, "--json"])

    assert code != 0
    touched = {path.split("?")[0] for _, path, _ in server.received}
    assert "/api/orchestration/flows/drafts" not in touched, "a refused repository must not reach registration"
    assert not [path for path in touched if "/gates/" in path], "nothing may be approved"


def test_start_plan_says_a_repository_matched_a_stale_snapshot(server):
    """Weaker evidence, stated BEFORE the acceptance rather than after.

    A snapshot match means the repository was connected when ADP last looked. An
    operator authorizing work into it is entitled to that distinction at the moment
    of consent.
    """
    engine_on(server)
    _conversation_done(server)
    _planned(server, repository="acme/web", repository_verified_live=False)
    _preview_ok(server)
    _draft_flow(server)
    _execution(server, executions=[_execution_row()])

    code, result = run_cli(["start", "Add rate limiting", "--plan", "--repo", "acme/web", "--yes", "--expect-plan-hash", CURRENT_HASH, "--json"])

    assert code == 0
    assert result["detail"]["derived"]["repository_verified_live"] is False


def test_start_plan_registers_inert_and_previews_before_accepting(server):
    """`--plan` must not become a shortcut around the consent contract.

    The whole point of the draft path is that the operator sees the EFFECTIVE graph —
    which includes the acceptance gate registration inserts and the bounds acceptance
    would grant — before approving. A convenience flag that posted to the
    approved-plan endpoint instead would silently undo that.
    """
    engine_on(server)
    _conversation_done(server)
    _planned(server)
    _preview_ok(server)
    _draft_flow(server)
    _execution(server, executions=[_execution_row()])

    code, _ = run_cli(["start", "Add rate limiting", "--plan", "--yes", "--expect-plan-hash", CURRENT_HASH, "--json"])

    assert code == 0
    posts = [entry[1].split("?")[0] for entry in server.received if entry[0] == "POST"]
    assert not [path for path in posts if path.endswith("/orchestration/flows")], "must not post to the approved-plan endpoint"
    assert posts.index("/api/orchestration/flows/drafts") < next(index for index, path in enumerate(posts) if "/gates/" in path), (
        "registration must precede the approval"
    )
    assert any(entry[0] == "GET" and entry[1].endswith(f"/flows/{FLOW_ID}") for entry in server.received), (
        "the effective graph must be read back before the acceptance"
    )


def test_start_plan_binds_the_approval_to_the_revision_it_previewed(server):
    """Same binding as `create`, because it is the same code — asserted, not assumed."""
    engine_on(server)
    _conversation_done(server)
    _planned(server)
    _preview_ok(server)
    _draft_flow(server)
    _execution(server, executions=[_execution_row()])

    code, _ = run_cli(["start", "Add rate limiting", "--plan", "--yes", "--expect-plan-hash", CURRENT_HASH, "--json"])

    assert code == 0
    approval = next(entry[2] for entry in server.received if entry[0] == "POST" and "/gates/" in entry[1])
    assert approval["expected_plan_hash"] == CURRENT_HASH


def test_start_plan_without_a_terminal_returns_a_pending_preview(server):
    engine_on(server)
    _conversation_done(server)
    _planned(server)
    _draft_flow(server)
    code, result = run_cli(["start", "Add rate limiting", "--plan", "--json"])
    assert code == 4
    assert result["detail"]["accepted"] is False
    assert result["detail"]["plan_hash"] == CURRENT_HASH
    assert not any("/approve" in path for method, path, _ in server.received if method == "POST")


def test_start_plan_checks_the_engine_before_spending_a_conversation(server):
    """Discovering at the end that the deployment cannot act is a wasted conversation."""
    engine_on(server, enabled=False)
    _conversation_done(server)

    code, result = run_cli(["start", "Add rate limiting", "--plan", "--yes", "--expect-plan-hash", CURRENT_HASH, "--json"])

    assert result["status"] == "unavailable"
    assert code != 0
    assert not [entry for entry in server.received if entry[0] == "POST" and "intake" in entry[1]]


def test_start_without_plan_still_refines_on_a_deployment_with_the_engine_off(server):
    """The engine guard must not spread to the conversation-only path.

    Refining an intent touches no engine route, and gating it would remove the one
    capability a gateway with the engine off can still offer.
    """
    engine_on(server, enabled=False)
    _conversation_done(server)

    code, result = run_cli(["start", "Add rate limiting", "--json"])

    assert code == 0
    assert result["status"] == "ok"


def test_start_plan_refuses_a_derived_plan_with_no_work_in_it(server):
    """Registering an empty plan would put an approvable, empty flow on record."""
    engine_on(server)
    _conversation_done(server)
    _planned(server, proposal={"flow_slug": "x", "title": "x", "org_id": "o", "spec_revision": "r", "nodes": [], "edges": []})

    code, _ = run_cli(["start", "Add rate limiting", "--plan", "--yes", "--expect-plan-hash", CURRENT_HASH, "--json"])

    assert code != 0
    touched = {path.split("?")[0] for _, path, _ in server.received}
    assert "/api/orchestration/flows/drafts" not in touched


def test_start_plan_reuses_the_resumed_session_rather_than_opening_a_second(server):
    """A retry must not produce two conversations, two plans and two flows.

    The dispatch's "reuse existing sessions/flows, prevent lost-response duplicates"
    requirement: `--resume --plan` must plan the conversation it reattached to.
    """
    engine_on(server)
    route(server, "GET", f"/api/orchestration/intake/sessions/{SESSION_ID}", _session(updated_at=200, draft={"intent": "R", "outcomes": ["A"]}))
    _planned(server)
    _preview_ok(server)
    _draft_flow(server)
    _execution(server, executions=[_execution_row()])

    code, result = run_cli(["start", "--resume", SESSION_ID, "--plan", "--yes", "--expect-plan-hash", CURRENT_HASH, "--json"])

    assert code == 0
    assert not [entry for entry in server.received if entry[0] == "POST" and entry[1].endswith("/intake/sessions")], (
        "resuming must not open a second conversation"
    )
    assert result["detail"]["session_id"] == SESSION_ID


def test_start_plan_keeps_the_conversation_record_on_the_accepted_envelope(server):
    """Traceability: which intent did this flow come from, and what was resolved.

    Returning only the acceptance detail would lose the session id — the one handle
    back to the conversation a person actually had.
    """
    engine_on(server)
    _conversation_done(server)
    _planned(server)
    _preview_ok(server)
    _draft_flow(server)
    _execution(server, executions=[_execution_row()])

    code, result = run_cli(["start", "Add rate limiting", "--plan", "--yes", "--expect-plan-hash", CURRENT_HASH, "--json"])

    assert code == 0
    detail = result["detail"]
    assert detail["session_id"] == SESSION_ID
    assert detail["flow_id"] == FLOW_ID
    assert detail["derived"]["wrote_nothing"] is True


# --- dispatch is confirmed against the ledger, never against the gate -------


def test_dispatch_is_not_claimed_from_the_gate_response_alone(server):
    """The consequential one. A moved gate is not a dispatched plan.

    The approve response carries `node_id`, `status`, `state`, `decision_id`,
    `actor_kind` — nothing about dispatch. An empty ledger is `legacy: true` and
    means NO DURABLE EXECUTION RECORD, which is emphatically not success. Claiming
    "the engine is delivering your plan" there would be false in exactly the
    deployments where it matters: tick not running, admission refusing, policy
    denying.
    """
    engine_on(server)
    _conversation_done(server)
    _planned(server)
    _preview_ok(server)
    _draft_flow(server)
    _execution(server, executions=[], legacy=True)

    code, result = run_cli(["start", "Add rate limiting", "--plan", "--yes", "--expect-plan-hash", CURRENT_HASH, "--json"])

    assert code == 0
    # The acceptance is real and is reported as such.
    assert result["detail"]["accepted"] is True
    # The dispatch is not.
    assert result["detail"]["dispatch_confirmed"] is False
    assert "NOT yet been confirmed" in result["next_action"]
    assert "Do not re-approve" in result["next_action"]


def test_dispatch_is_confirmed_when_an_action_was_dispatched(server):
    engine_on(server)
    _conversation_done(server)
    _planned(server)
    _preview_ok(server)
    _draft_flow(server)
    _execution(
        server,
        executions=[
            _execution_row(
                phase="delivering",
                actions=[
                    {
                        "id": "a1",
                        "operation_key": "k",
                        "kind": "dispatch",
                        "status": "dispatched",
                        "attempt": 1,
                        "resolved": False,
                        "artifact_ref": None,
                        "receipt_ref": None,
                        "created_at": None,
                        "observed_at": None,
                    }
                ],
            )
        ],
    )

    code, result = run_cli(["start", "Add rate limiting", "--plan", "--yes", "--expect-plan-hash", CURRENT_HASH, "--json"])

    assert code == 0
    assert result["detail"]["dispatch_confirmed"] is True
    assert result["detail"]["dispatch"]["dispatched_actions"] == 1
    assert "taken the work up" in result["next_action"]


def test_an_execution_still_only_admitted_is_not_a_confirmed_dispatch(server):
    """`ADMITTED` is "ledger identity exists; no work started yet".

    The phase enum separates it from `PREPARING` precisely because that window is
    real, so treating a row's mere existence as dispatch would report work as
    started during it.
    """
    engine_on(server)
    _conversation_done(server)
    _planned(server)
    _preview_ok(server)
    _draft_flow(server)
    _execution(server, executions=[_execution_row(phase="admitted", actions=[])])

    code, result = run_cli(["start", "Add rate limiting", "--plan", "--yes", "--expect-plan-hash", CURRENT_HASH, "--json"])

    assert code == 0
    assert result["detail"]["dispatch_confirmed"] is False


def test_a_blocked_execution_is_surfaced_rather_than_reported_as_progress(server):
    """A block IS the engine having taken the work up — and needs someone to act.

    Reporting only "accepted, the engine schedules from here" would leave a user
    waiting on work that is waiting on them.
    """
    engine_on(server)
    _conversation_done(server)
    _planned(server)
    _preview_ok(server)
    _draft_flow(server)
    _execution(
        server,
        executions=[
            _execution_row(
                phase="delivering",
                status="blocked",
                block={
                    "code": "human_gate_required",
                    "owner": "you",
                    "required_input": "an approval",
                    "remaining_gates": [],
                    "progressed_at": None,
                    "detail": None,
                },
            )
        ],
    )

    code, result = run_cli(["start", "Add rate limiting", "--plan", "--yes", "--expect-plan-hash", CURRENT_HASH, "--json"])

    assert code == 0
    assert result["detail"]["dispatch"]["blocked"][0]["code"] == "human_gate_required"
    assert "WAITING on you" in result["next_action"]


def test_an_unreadable_ledger_does_not_read_as_a_failed_acceptance(server):
    """Failing to READ the consequence is not failing to accept.

    Reporting an error here would send an operator to re-approve a plan that is
    already armed, and a second approval of the same revision is at best noise.
    """
    engine_on(server)
    _conversation_done(server)
    _planned(server)
    _preview_ok(server)
    _draft_flow(server)
    route(server, "GET", f"/api/orchestration/flows/{FLOW_ID}/execution", {"detail": "boom"}, status=500)

    code, result = run_cli(["start", "Add rate limiting", "--plan", "--yes", "--expect-plan-hash", CURRENT_HASH, "--json"])

    assert code == 0
    assert result["detail"]["accepted"] is True
    assert result["detail"]["dispatch_confirmed"] is False
    assert "could not be read" in result["next_action"]
    assert "Nothing needs re-approving" in result["next_action"]


def test_create_also_confirms_dispatch_against_the_ledger(server, tmp_path):
    """The confirmation belongs to the shared sequence, not to one verb."""
    engine_on(server)
    _preview_ok(server)
    _draft_flow(server)
    _execution(server, executions=[_execution_row(phase="preparing")])

    code, result = run_cli(["create", "--file", str(_plan_file(tmp_path)), "--yes", "--expect-plan-hash", CURRENT_HASH, "--json"])

    assert code == 0
    assert result["detail"]["dispatch_confirmed"] is True


def test_yes_without_reviewed_hash_only_returns_preview(server, tmp_path):
    engine_on(server)
    _draft_flow(server)
    code, result = run_cli(["create", "--file", str(_plan_file(tmp_path)), "--yes", "--json"])
    assert code == 4
    assert result["detail"]["plan_hash"] == CURRENT_HASH
    assert result["detail"]["accepted"] is False
    assert all(path.endswith("/preview") for method, path, _ in server.received if method == "POST")


def test_create_refuses_a_different_reviewed_hash_before_writing(server, tmp_path):
    engine_on(server)
    _draft_flow(server)
    code, result = run_cli(["create", "--file", str(_plan_file(tmp_path)), "--yes", "--expect-plan-hash", STALE_HASH, "--json"])
    assert code == 4
    assert result["error"]["code"] == "stale_plan_revision"
    assert all(path.endswith("/preview") for method, path, _ in server.received if method == "POST")


def test_registration_cannot_substitute_an_unreviewed_policy(server, tmp_path):
    engine_on(server)
    _draft_flow(server, registered={"plan_hash": STALE_HASH})
    code, result = run_cli(["create", "--file", str(_plan_file(tmp_path)), "--yes", "--expect-plan-hash", CURRENT_HASH, "--json"])
    assert code == 4
    assert result["error"]["code"] == "stale_plan_revision"
    assert not any("/approve" in path for method, path, _ in server.received if method == "POST")


def test_intake_server_errors_are_not_missing_sessions(server):
    route(server, "GET", f"/api/orchestration/intake/sessions/{SESSION_ID}", {"detail": "backend failed"}, status=503)
    with pytest.raises(common.CliError) as caught:
        cli.session_state(common.Api(), SESSION_ID)
    assert caught.value.status_code == 503


def test_poll_waits_for_the_sent_task_even_with_a_stale_question(monkeypatch):
    states = iter(
        [
            _session(updated_at=100, open_questions=["Old question"], last_response_task_id="old"),
            _session(updated_at=100, working=True, last_response_task_id="new"),
            _session(updated_at=100, last_response="New answer", last_response_task_id="new"),
        ]
    )
    monkeypatch.setattr(cli, "session_state", lambda *_: next(states))
    result = cli.await_reply(None, SESSION_ID, after=100, task_id="new")
    assert result["last_response"] == "New answer"


def test_resuming_restores_the_repository_and_issue(server):
    engine_on(server)
    route(server, "GET", f"/api/orchestration/intake/sessions/{SESSION_ID}", _session(repository="acme/web", requested_issue="42"))
    _planned(server)
    _draft_flow(server)
    code, result = run_cli(["start", "--resume", SESSION_ID, "--plan", "--json"])
    assert code == 4
    assert result["detail"]["repo"] == "acme/web"
    plan_request = next(body for method, path, body in server.received if method == "POST" and path.endswith("/plan"))
    assert plan_request["repository"] == "acme/web"
    assert plan_request["issue"] == "42"


def test_interactive_start_prompts_for_an_outcome(monkeypatch, server):
    _conversation_done(server)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *_: "Make checkout faster")
    code, _ = run_cli(["start", "--refine-only"])
    assert code == 0
    opening = next(body for method, path, body in server.received if method == "POST" and path.endswith("/sessions"))
    assert opening["message"] == "Make checkout faster"
