"""Unit tests for the amendment half of lib/engine_registration.py.

Issue #4529, the worker side of gate-aware proposal authoring. The module's new-flow
half is covered by `test_engine_registration.py`; this file covers the amendment path,
which differs from it in the three things that are the *authorization* rather than the
payload, and each of those gets its own class below:

  * the flow is in the URL path,
  * the assignment (`request_id`) is in the query string, and
  * `X-Agent-RunId` must be inside the SIGNED header set, because the server refuses
    unless it equals the `author_run_id` it bound to that assignment.

Two properties are asserted harder than the rest, because getting either wrong is
silent rather than loud:

**Fail-soft.** `amendment_registration_note` is called from a finish path with no
error handling, after the authored artifacts are already committed and pushed. So
every failure mode must produce a *string*. Unlike the new-flow path, a commissioned
run that emitted nothing is a reportable outcome rather than silence — a human asked
for a replan and is waiting on an answer.

**No overstatement.** The route writes no node, edge or plan version, and the worker
holds no `PLAN_APPROVE`. A note that reads as though the plan changed would send an
operator to a graph still showing the old gates, so the note is asserted not to claim
the amendment is applied even when the server sends an unexpected status.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lib.engine_registration import (  # noqa: E402
    AMENDMENT_REQUEST_ENV,
    DISABLED_ENV,
    FLOW_ID_ENV,
    EngineRegistrationError,
    amendment_artifact_path,
    amendment_registration_note,
    authoring_assignment,
    register_amendment_proposal,
)

TENANT = "org-alpha"
ENDPOINT = "https://api-gw.example.com"

#: The run's envelope `message_id`. On this path it is not merely attribution: the
#: server wrote it onto the assignment as `author_run_id` and refuses a registration
#: presenting anything else.
RUN_ID = "replan:8f14e45f-ceea-467a-9a3e-4dc1b0a1f4b7"

FLOW_ID = "flow-abc123"
REQUEST_ID = "req-7c9a1e20"

#: What the route returns (`AmendmentDraftRegisteredResponse`). `status` is a literal
#: in the route with no branch that produces anything else.
GATEWAY_OK = {
    "draft_id": "amd-0001",
    "flow_id": FLOW_ID,
    "request_id": REQUEST_ID,
    "base_plan_version": 4,
    "proposal_hash": "cafebabe",
    "gate_diff": {
        "added": ["loop/epic-1/wave-3/gate-deploy"],
        "removed": [],
        "unchanged": ["loop/epic-1/wave-1/accept"],
        "changes_gating": True,
    },
    "already_registered": False,
    "status": "pending_human_accept",
    "accept_command": "@agent-engine accept amendment amd-0001",
    "flow_url": "https://gateway.example.com/flows/flow-abc123",
}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """A commissioned authoring pod: endpoint, tenant, run id, and an assignment.

    The assignment vars are set here rather than per-test because "this run was
    commissioned" is the premise of the whole file; the tests that need them absent
    delete them explicitly, which reads as the deviation it is.
    """
    monkeypatch.setenv("ADP_GATEWAY_ENDPOINT", ENDPOINT)
    monkeypatch.setenv("ADP_TENANT_ID", TENANT)
    monkeypatch.setenv("ADP_MESSAGE_ID", RUN_ID)
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv(FLOW_ID_ENV, FLOW_ID)
    monkeypatch.setenv(AMENDMENT_REQUEST_ENV, REQUEST_ID)
    monkeypatch.delenv(DISABLED_ENV, raising=False)


@pytest.fixture(autouse=True)
def _no_real_signing():
    """Echo the headers instead of signing, so a header assertion is meaningful.

    Mirrors `test_engine_registration._no_real_signing` deliberately: a stub that
    returned a fixed dict would swallow `X-Agent-RunId`, and this file's central
    claim is that the header is inside the set handed to the signer. A stub that
    dropped it would make that claim untestable while the tests still passed.
    """

    def _echo(_method, _url, headers, _data):
        return {**headers, "Authorization": "AWS4-x"}

    with patch("lib.engine_registration._sigv4_sign_request", side_effect=_echo):
        yield


def write_amendment(work_dir: Path, document: dict | str, *, request_id: str = REQUEST_ID) -> Path:
    path = amendment_artifact_path(work_dir, request_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(document if isinstance(document, str) else json.dumps(document), encoding="utf-8")
    return path


def valid_document(*, org_id: str = "", intent_ref: str | None = None) -> dict:
    """A minimal amended `LoopProposal`: the base plan plus a gate before the deploy wave."""
    document = {
        "flow_slug": "loop",
        "title": "Delivery loop",
        "org_id": org_id,
        "spec_revision": "issue-4529-r1",
        "nodes": [
            {"address": "loop/epic-1/wave-1/story", "kind": "story", "title": "A story"},
            {
                "address": "loop/epic-1/wave-3/gate-deploy",
                "kind": "gate",
                "title": "Approve deploy",
            },
        ],
        "edges": [],
    }
    if intent_ref is not None:
        document["intent_ref"] = intent_ref
    return document


def http_response(body: str) -> MagicMock:
    response = MagicMock()
    response.read.return_value = body.encode("utf-8")
    response.__enter__ = lambda self: self
    response.__exit__ = MagicMock(return_value=False)
    return response


def sent_request(urlopen: MagicMock):
    """The `Request` the module handed to urlopen."""
    return urlopen.call_args[0][0]


class TestAuthoringAssignment:
    """`authoring_assignment` is the only source of the two ids, and it is all-or-nothing."""

    def test_both_present_is_the_assignment(self):
        assert authoring_assignment() == (FLOW_ID, REQUEST_ID)

    def test_neither_present_is_not_an_amendment_run(self, monkeypatch):
        monkeypatch.delenv(FLOW_ID_ENV)
        monkeypatch.delenv(AMENDMENT_REQUEST_ENV)
        assert authoring_assignment() is None

    @pytest.mark.parametrize("missing", [FLOW_ID_ENV, AMENDMENT_REQUEST_ENV])
    def test_half_an_assignment_is_treated_as_none(self, monkeypatch, missing):
        """A half-present pair is not guessed at.

        With no `request_id` there is nothing to authorize against; with no `flow_id`
        there is no path to send it to. Either way the server would refuse, so
        reporting "not an amendment run" is the same outcome without the round trip
        and without a warning naming the wrong cause.
        """
        monkeypatch.delenv(missing)
        assert authoring_assignment() is None

    @pytest.mark.parametrize("blank", ["", "   "])
    def test_a_blank_value_is_not_an_assignment(self, monkeypatch, blank):
        """Set-but-empty is absent. A blank `request_id` in the query string would be
        a 422 from the route's `min_length=1`, reported as a validation error about a
        document that is in fact fine."""
        monkeypatch.setenv(AMENDMENT_REQUEST_ENV, blank)
        assert authoring_assignment() is None


class TestArtifactPath:
    """Keyed on the request, not the issue."""

    def test_path_is_keyed_on_the_request_id(self, tmp_path):
        assert amendment_artifact_path(tmp_path, REQUEST_ID) == tmp_path / f"aidlc/spaces/amendments/{REQUEST_ID}/proposal.json"

    def test_two_assignments_on_one_issue_do_not_share_a_file(self, tmp_path):
        """One issue can carry several `replan:` asks over a flow's life. A path keyed
        on the issue would make the second overwrite the first, then register whichever
        file was on disk against whichever assignment was live."""
        assert amendment_artifact_path(tmp_path, "req-one") != amendment_artifact_path(tmp_path, "req-two")


class TestTheRequestIsAddressedToTheAssignment:
    """The path, the query string and the header — the three parts of the authorization."""

    def test_flow_is_in_the_path_and_request_in_the_query(self, tmp_path):
        write_amendment(tmp_path, valid_document())
        with patch("lib.engine_registration.urlopen", return_value=http_response(json.dumps(GATEWAY_OK))) as urlopen:
            register_amendment_proposal(work_dir=tmp_path, flow_id=FLOW_ID, request_id=REQUEST_ID)

        url = sent_request(urlopen).full_url
        assert url == f"{ENDPOINT}/agent/orchestration/flows/{FLOW_ID}/amendments/drafts?request_id={REQUEST_ID}"

    def test_the_agent_plane_prefix_is_present(self, tmp_path):
        """`/agent` is what makes API Gateway authorise with AWS_IAM and inject
        `X-Caller-Identity`. Without it the request lands on the internal plane, where
        the route does not exist and no permission would have been required."""
        write_amendment(tmp_path, valid_document())
        with patch("lib.engine_registration.urlopen", return_value=http_response(json.dumps(GATEWAY_OK))) as urlopen:
            register_amendment_proposal(work_dir=tmp_path, flow_id=FLOW_ID, request_id=REQUEST_ID)

        assert sent_request(urlopen).full_url.startswith(f"{ENDPOINT}/agent/orchestration/")

    def test_the_run_id_header_is_inside_the_signed_set(self, tmp_path):
        """Signed, not appended afterwards, so it cannot be rewritten in flight.

        Asserted against what the signer was *handed* — the echo stub proves the
        header reached the signing call rather than being added to the outgoing
        request after the signature was computed over a set that lacked it.
        """
        write_amendment(tmp_path, valid_document())
        signed = {}

        def _capture(_method, _url, headers, _data):
            signed.update(headers)
            return {**headers, "Authorization": "AWS4-x"}

        with (
            patch("lib.engine_registration._sigv4_sign_request", side_effect=_capture),
            patch(
                "lib.engine_registration.urlopen",
                return_value=http_response(json.dumps(GATEWAY_OK)),
            ),
        ):
            register_amendment_proposal(work_dir=tmp_path, flow_id=FLOW_ID, request_id=REQUEST_ID)

        assert signed["X-Agent-RunId"] == RUN_ID

    def test_ids_needing_escaping_do_not_escape_the_path(self, tmp_path):
        """A flow or request id is server-generated, but it reaches this function as a
        string read from the environment, and a `../` or `?` in either would retarget
        the request rather than being rejected. Encoded, so a malformed id produces a
        404 for a flow that does not exist instead of a POST to a different route."""
        odd_flow, odd_request = "flow/../../admin", "req?x=1&y=2"
        write_amendment(tmp_path, valid_document(), request_id=odd_request)
        with patch("lib.engine_registration.urlopen", return_value=http_response(json.dumps(GATEWAY_OK))) as urlopen:
            register_amendment_proposal(work_dir=tmp_path, flow_id=odd_flow, request_id=odd_request)

        url = sent_request(urlopen).full_url
        assert url == f"{ENDPOINT}/agent/orchestration/flows/flow%2F..%2F..%2Fadmin/amendments/drafts?request_id=req%3Fx%3D1%26y%3D2"
        assert "/admin/" not in url
        # One query parameter, not three: an unescaped `&` would have added two.
        assert url.count("&") == 0

    def test_it_is_a_post(self, tmp_path):
        write_amendment(tmp_path, valid_document())
        with patch("lib.engine_registration.urlopen", return_value=http_response(json.dumps(GATEWAY_OK))) as urlopen:
            register_amendment_proposal(work_dir=tmp_path, flow_id=FLOW_ID, request_id=REQUEST_ID)

        assert sent_request(urlopen).method == "POST"


class TestTheDocumentIsTheAuthorsExceptForTheTenant:
    """What the worker overwrites, and what it must leave alone."""

    def test_tenant_is_overwritten_from_the_envelope(self, tmp_path):
        """The artifact is agent-authored, so its declared org is not trusted. The
        gateway compares and would reject a mismatch, but overwriting here means the
        request the worker sends is correct rather than merely caught."""
        write_amendment(tmp_path, valid_document(org_id="org-somebody-else"))
        with patch("lib.engine_registration.urlopen", return_value=http_response(json.dumps(GATEWAY_OK))) as urlopen:
            register_amendment_proposal(work_dir=tmp_path, flow_id=FLOW_ID, request_id=REQUEST_ID)

        assert json.loads(sent_request(urlopen).data)["org_id"] == TENANT

    def test_no_intent_ref_is_invented(self, tmp_path):
        """An amendment belongs to a flow that already has an intent, and the request
        row records the human who asked. Defaulting one from this run's issue would
        attribute the amendment to whatever issue the authoring run executed on.

        Absent, not blank: `intent_ref` is `str | None` on the wire, so an empty
        string would store a present-but-meaningless value where the schema's own way
        of saying "unknown" is omission.
        """
        write_amendment(tmp_path, valid_document())
        with patch("lib.engine_registration.urlopen", return_value=http_response(json.dumps(GATEWAY_OK))) as urlopen:
            register_amendment_proposal(work_dir=tmp_path, flow_id=FLOW_ID, request_id=REQUEST_ID)

        body = json.loads(sent_request(urlopen).data)
        # Absent entirely, which is distinct from present-and-blank. Spelled as two
        # assertions because `body.get("intent_ref") is None` alone would also pass for
        # an explicit `"intent_ref": null` the worker had inserted.
        assert "intent_ref" not in body, f"the worker invented an intent_ref: {body.get('intent_ref')!r}"
        assert REQUEST_ID not in json.dumps(body), "the assignment id must not leak into the document as an intent"

    def test_an_author_declared_intent_ref_survives(self, tmp_path):
        """The worker invents nothing, but it also erases nothing."""
        write_amendment(tmp_path, valid_document(intent_ref="4120"))
        with patch("lib.engine_registration.urlopen", return_value=http_response(json.dumps(GATEWAY_OK))) as urlopen:
            register_amendment_proposal(work_dir=tmp_path, flow_id=FLOW_ID, request_id=REQUEST_ID)

        assert json.loads(sent_request(urlopen).data)["intent_ref"] == "4120"

    def test_the_authored_gates_are_sent_unchanged(self, tmp_path):
        """The gate placement IS the deliverable of a `replan:` ask, so the worker must
        not normalise, reorder or drop it."""
        document = valid_document()
        write_amendment(tmp_path, document)
        with patch("lib.engine_registration.urlopen", return_value=http_response(json.dumps(GATEWAY_OK))) as urlopen:
            register_amendment_proposal(work_dir=tmp_path, flow_id=FLOW_ID, request_id=REQUEST_ID)

        assert json.loads(sent_request(urlopen).data)["nodes"] == document["nodes"]


class TestFailSoft:
    """Every failure mode returns a string. The finish path has no error handling."""

    def test_kill_switch_makes_no_call_and_says_nothing(self, monkeypatch, tmp_path):
        monkeypatch.setenv(DISABLED_ENV, "true")
        write_amendment(tmp_path, valid_document())
        with patch("lib.engine_registration.urlopen") as urlopen:
            assert amendment_registration_note(work_dir=tmp_path) == ""
        urlopen.assert_not_called()

    def test_a_run_with_no_assignment_says_nothing(self, monkeypatch, tmp_path):
        """The normal case for every other persona and every webhook trigger. Silence,
        not a warning: a warning on every non-amendment run trains operators to ignore
        the warning that matters."""
        monkeypatch.delenv(FLOW_ID_ENV)
        monkeypatch.delenv(AMENDMENT_REQUEST_ENV)
        with patch("lib.engine_registration.urlopen") as urlopen:
            assert amendment_registration_note(work_dir=tmp_path) == ""
        urlopen.assert_not_called()

    def test_a_commissioned_run_that_emitted_nothing_reports_it(self, tmp_path):
        """The one place this path is deliberately louder than the new-flow path.

        A human commented `replan:` and is waiting. An author that concluded without
        proposing anything is a real outcome that must reach them, not something to
        log and drop — which is what "" would do.
        """
        with patch("lib.engine_registration.urlopen") as urlopen:
            note = amendment_registration_note(work_dir=tmp_path)

        urlopen.assert_not_called()
        assert note != ""
        assert "not filed" in note
        assert "no amended plan was emitted" in note

    @pytest.mark.parametrize("unset", ["ADP_GATEWAY_ENDPOINT", "ADP_TENANT_ID", "ADP_MESSAGE_ID"])
    def test_a_missing_env_field_is_a_warning_not_a_raise(self, monkeypatch, tmp_path, unset):
        monkeypatch.delenv(unset)
        write_amendment(tmp_path, valid_document())
        note = amendment_registration_note(work_dir=tmp_path)
        assert "not filed" in note

    def test_malformed_artifact_is_a_warning(self, tmp_path):
        write_amendment(tmp_path, "{not json")
        note = amendment_registration_note(work_dir=tmp_path)
        assert "not filed" in note

    def test_a_non_object_artifact_is_a_warning(self, tmp_path):
        write_amendment(tmp_path, "[1, 2, 3]")
        assert "not filed" in amendment_registration_note(work_dir=tmp_path)

    @pytest.mark.parametrize("status", [403, 404, 422, 500])
    def test_an_http_refusal_is_a_warning(self, tmp_path, status):
        """404 is the interesting one: it is what the server returns when this run was
        NOT commissioned for this assignment, which is the fence working. The run must
        still succeed."""
        write_amendment(tmp_path, valid_document())
        error = HTTPError(url="u", code=status, msg="no", hdrs=None, fp=None)
        with patch("lib.engine_registration.urlopen", side_effect=error):
            note = amendment_registration_note(work_dir=tmp_path)
        assert "not filed" in note
        assert str(status) in note

    def test_an_unreachable_gateway_is_a_warning(self, tmp_path):
        write_amendment(tmp_path, valid_document())
        with patch("lib.engine_registration.urlopen", side_effect=URLError("connection refused")):
            note = amendment_registration_note(work_dir=tmp_path)
        assert "not filed" in note

    def test_an_unparseable_response_is_a_warning(self, tmp_path):
        write_amendment(tmp_path, valid_document())
        with patch(
            "lib.engine_registration.urlopen",
            return_value=http_response("<html>gateway timeout</html>"),
        ):
            note = amendment_registration_note(work_dir=tmp_path)
        assert "not filed" in note

    def test_a_non_object_response_is_a_warning(self, tmp_path):
        write_amendment(tmp_path, valid_document())
        with patch("lib.engine_registration.urlopen", return_value=http_response("[]")):
            assert "not filed" in amendment_registration_note(work_dir=tmp_path)

    def test_an_unexpected_exception_is_still_a_warning(self, tmp_path):
        """The blanket handler. A dependency raising something nobody anticipated must
        not take down a run whose real output is already pushed."""
        write_amendment(tmp_path, valid_document())
        with patch("lib.engine_registration.urlopen", side_effect=RuntimeError("boom")):
            note = amendment_registration_note(work_dir=tmp_path)
        assert "not filed" in note
        assert "unexpected error" in note

    def test_every_warning_says_the_in_force_plan_is_untouched(self, tmp_path):
        """The reassurance is the point. An operator who reads "failed" where nothing
        was applied will go looking for damage that does not exist — and on this path
        the specific fear is that a half-applied amendment changed the live plan."""
        write_amendment(tmp_path, valid_document())
        with patch("lib.engine_registration.urlopen", side_effect=URLError("nope")):
            note = amendment_registration_note(work_dir=tmp_path)
        assert "in-force plan is unchanged" in note
        assert "still open" in note

    def test_the_low_level_function_does_raise(self, tmp_path):
        """Fail-soft belongs to the note, not to the transport. `register_amendment_proposal`
        raising is what lets the note distinguish causes and report them."""
        with pytest.raises(EngineRegistrationError):
            register_amendment_proposal(work_dir=tmp_path, flow_id=FLOW_ID, request_id=REQUEST_ID)


class TestTheNoteNeverOverstates:
    """The note must not read as though the plan changed."""

    def _note(self, tmp_path, result: dict) -> str:
        write_amendment(tmp_path, valid_document())
        with patch("lib.engine_registration.urlopen", return_value=http_response(json.dumps(result))):
            return amendment_registration_note(work_dir=tmp_path)

    def test_a_successful_filing_reports_pending_human_accept(self, tmp_path):
        note = self._note(tmp_path, GATEWAY_OK)
        assert "pending_human_accept" in note
        assert "nothing has been applied" in note.lower()

    def test_it_reports_the_draft_id_and_base_revision(self, tmp_path):
        """Both are what the human needs to act: the id is what they type to accept,
        and the base version tells them whether this amendment was authored against
        the plan that is still in force."""
        note = self._note(tmp_path, GATEWAY_OK)
        assert "amd-0001" in note
        assert "v4" in note

    def test_it_reports_the_gate_diff(self, tmp_path):
        note = self._note(tmp_path, GATEWAY_OK)
        assert "+1 added" in note
        assert "-0 removed" in note
        assert "1 unchanged" in note
        assert "loop/epic-1/wave-3/gate-deploy" in note

    def test_a_gating_change_is_called_out(self, tmp_path):
        """`changes_gating` is the server's answer to "does accepting this change where
        humans get asked", which is the whole question a `replan:` ask is about."""
        note = self._note(tmp_path, GATEWAY_OK)
        assert "this changes where humans are asked to approve" in note

    def test_a_non_gating_change_says_so_rather_than_staying_silent(self, tmp_path):
        result = {**GATEWAY_OK, "gate_diff": {**GATEWAY_OK["gate_diff"], "changes_gating": False}}
        note = self._note(tmp_path, result)
        assert "no change to where humans are asked to approve" in note

    def test_an_absent_gate_diff_is_not_reported_as_zero_gate_changes(self, tmp_path):
        """A missing diff must not read as a measured "no gates changed" — that is the
        one reading that would make a human accept without looking."""
        result = {k: v for k, v in GATEWAY_OK.items() if k != "gate_diff"}
        note = self._note(tmp_path, result)
        assert "not reported by the engine" in note
        assert "0 added" not in note

    def test_an_unexpected_status_is_printed_verbatim_and_not_called_applied(self, tmp_path):
        """The route has no branch that returns anything but `pending_human_accept`, so
        this is a can't-happen. If it does happen the operator must see what the server
        actually said, and must still not be told the amendment is live."""
        note = self._note(tmp_path, {**GATEWAY_OK, "status": "applied"})
        assert "`applied`" in note
        assert "reported by the engine" in note
        assert "Nothing in this run applied it" in note

    def test_no_success_note_claims_the_amendment_is_in_force(self, tmp_path):
        for status in ("pending_human_accept", "applied", "", None):
            note = self._note(tmp_path, {**GATEWAY_OK, "status": status})
            lowered = note.lower()
            assert "amendment has been applied" not in lowered
            assert "plan updated" not in lowered
            assert "now in force" not in lowered

    def test_the_accept_command_is_the_servers_and_is_fenced(self, tmp_path):
        """Fenced for the #4599 reason: an inline `@agent-engine accept amendment <id>`
        in a bot comment is read by the tick as a live command, refused (the bot has no
        `PLAN_APPROVE`), and answered with an error — the success message triggering a
        failure reply on every registration."""
        note = self._note(tmp_path, GATEWAY_OK)
        assert "```\n@agent-engine accept amendment amd-0001\n```" in note

    @pytest.mark.parametrize("status", ["accepted", "superseded", "rejected"])
    def test_terminal_replay_does_not_ask_for_another_acceptance(self, tmp_path, status):
        note = self._note(tmp_path, {**GATEWAY_OK, "status": status, "accept_command": ""})
        assert f"`{status}`" in note
        assert "Reply with" not in note
        assert "@agent-engine accept" not in note
        assert "proposed for your approval" not in note

    def test_the_fallback_command_names_the_draft(self, tmp_path):
        """A bare `@agent-engine accept` would answer the flow's ACCEPTANCE GATE, which
        is a different and much larger action than accepting one amendment."""
        note = self._note(tmp_path, {k: v for k, v in GATEWAY_OK.items() if k != "accept_command"})
        assert "@agent-engine accept amendment amd-0001" in note

    def test_an_already_registered_filing_is_not_a_second_draft(self, tmp_path):
        """A fail-soft author retries, and a retry must not read as a new proposal."""
        note = self._note(tmp_path, {**GATEWAY_OK, "already_registered": True})
        assert "already on file" in note
        assert "existing draft is unchanged" in note

    def test_the_flow_id_links_when_the_server_supplies_a_url(self, tmp_path):
        note = self._note(tmp_path, GATEWAY_OK)
        assert f"[`{FLOW_ID}`]({GATEWAY_OK['flow_url']})" in note

    def test_a_missing_flow_url_still_prints_the_id(self, tmp_path):
        """A missing link is a degraded comment, never a missing plan. The worker cannot
        compose the URL itself — its endpoint is the API Gateway invoke URL, and pasting
        that would hand an operator a link to the machine plane."""
        note = self._note(tmp_path, {k: v for k, v in GATEWAY_OK.items() if k != "flow_url"})
        assert f"`{FLOW_ID}`" in note
        assert "](" not in note
