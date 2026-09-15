"""Unit tests for lib/engine_registration.py.

Issue #4528, the worker half of the engine bridge. Added in response to review
PR #4558 (MEDIUM-2): the module shipped with 315 lines and zero tests, so its
central safety property — **fail-soft** — was asserted only in prose.

The tests below are organised around what the issue names as its own bug class 3,
"compile failure kills the AIDLC run". Every failure mode the module can reach
(no artifact, no tenant, malformed JSON, HTTP error, unreachable gateway,
unparseable response, and an unexpected exception from a dependency) is asserted
to produce a *string* and never a raise, because the caller in `entrypoint.py`
deliberately has no error handling of its own — the contract is that it needs none.

`register_loop_proposal` is tested for the tenant-overwrite property separately,
since that is the one thing the worker does that the gateway cannot do for it: the
document's declared `org_id` is agent-authored, and the worker replacing it is what
makes the request correct rather than merely caught downstream.
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
    DISABLED_ENV,
    EngineRegistrationError,
    draft_registration_note,
    proposal_artifact_path,
    register_loop_proposal,
    registration_disabled,
)

ISSUE = 4528
TENANT = "org-alpha"
ENDPOINT = "https://api-gw.example.com"
# The run's envelope `message_id` (Issue #4597) — the `event_id` PK of the run's
# `webhook-events` row, which is what the gateway resolves the owning tenant from.
RUN_ID = "evt-run-4597"

# What the gateway returns on a successful registration (`DraftRegisteredResponse`).
GATEWAY_OK = {
    "flow_id": "flow-abc123",
    "plan_version": 1,
    "plan_hash": "deadbeef",
    "decision_id": "dec-1",
    "nodes_created": 7,
    "edges_created": 9,
    "already_registered": False,
    "acceptance_gate_address": "loop/epic-1/wave-1/accept",
    "accept_command": "@agent-engine accept",
    # Composed server-side (#4885) — the worker knows only the API Gateway invoke
    # URL, so it cannot build a user-facing link itself.
    "flow_url": "https://gateway.example.com/flows/flow-abc123",
}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """A pod-like environment: endpoint, tenant and run id present, kill switch unset.

    `ADP_MESSAGE_ID` is set explicitly (Issue #4597) rather than inherited. It is
    genuinely present in a real pod and in an agent's own shell, so leaving it to the
    ambient environment makes this whole module pass locally and fail in CI — which
    is exactly what happened when the run-id header was added.
    """
    monkeypatch.setenv("ADP_GATEWAY_ENDPOINT", ENDPOINT)
    monkeypatch.setenv("ADP_TENANT_ID", TENANT)
    monkeypatch.setenv("ADP_MESSAGE_ID", RUN_ID)
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.delenv(DISABLED_ENV, raising=False)


@pytest.fixture(autouse=True)
def _no_real_signing():
    """Never reach botocore. Signing is not what these tests are about.

    Echoes the headers it was given plus an `Authorization`, which is what the real
    `_sigv4_sign_request` does (botocore's `add_auth` mutates the request's existing
    header set). The previous stub returned a fixed dict, silently dropping every
    header the caller passed — so an assertion about a header the worker sends could
    not distinguish "not sent" from "eaten by the stub". Issue #4597 added such a
    header, and this is what makes it testable.
    """

    def _echo(_method, _url, headers, _data):
        return {**headers, "Authorization": "AWS4-x"}

    with patch("lib.engine_registration._sigv4_sign_request", side_effect=_echo):
        yield


def write_proposal(work_dir: Path, document: dict | str) -> Path:
    """Emit an artifact where Step 7e of the AIDLC skill emits one."""
    path = proposal_artifact_path(work_dir, ISSUE)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(document if isinstance(document, str) else json.dumps(document), encoding="utf-8")
    return path


def valid_document(*, org_id: str = "") -> dict:
    """A minimal `LoopProposal`-shaped document, with org_id blank as Step 7e requires."""
    return {
        "flow_slug": "loop",
        "title": "Delivery loop",
        "org_id": org_id,
        "spec_revision": "issue-4528-r1",
        "nodes": [{"address": "loop/epic-1/wave-1/story", "kind": "story", "title": "A story"}],
        "edges": [],
    }


def http_response(body: str) -> MagicMock:
    response = MagicMock()
    response.read.return_value = body.encode("utf-8")
    response.__enter__ = lambda self: self
    response.__exit__ = MagicMock(return_value=False)
    return response


class TestKillSwitch:
    """`ADP_ENGINE_REGISTRATION_DISABLED` must reproduce today's behaviour exactly.

    The issue's regression clause: "runs with registration disabled behave exactly
    as today". Today means no HTTP call at all, not a call whose result is ignored.
    """

    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", " true "])
    def test_truthy_spellings_disable(self, monkeypatch, value):
        monkeypatch.setenv(DISABLED_ENV, value)
        assert registration_disabled() is True

    @pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "maybe"])
    def test_everything_else_leaves_registration_on(self, monkeypatch, value):
        monkeypatch.setenv(DISABLED_ENV, value)
        assert registration_disabled() is False

    def test_unset_leaves_registration_on(self):
        assert registration_disabled() is False

    def test_disabled_makes_no_http_call_and_returns_nothing(self, monkeypatch, tmp_path):
        monkeypatch.setenv(DISABLED_ENV, "true")
        write_proposal(tmp_path, valid_document())

        with patch("lib.engine_registration.urlopen") as urlopen:
            assert draft_registration_note(work_dir=tmp_path, issue=ISSUE) == ""

        urlopen.assert_not_called()


class TestMissingArtifactIsSilent:
    """No artifact means this run authored no proposal — not an error.

    Most AIDLC runs (inception stages, Run B materialisation) emit no proposal, so a
    warning here would fire on runs where nothing is wrong and train operators to
    ignore the warning that matters.
    """

    def test_absent_artifact_returns_empty_and_makes_no_call(self, tmp_path):
        with patch("lib.engine_registration.urlopen") as urlopen:
            assert draft_registration_note(work_dir=tmp_path, issue=ISSUE) == ""

        urlopen.assert_not_called()

    def test_artifact_path_is_the_path_step_7e_writes(self, tmp_path):
        assert proposal_artifact_path(tmp_path, ISSUE) == tmp_path / "aidlc/spaces/issue-4528/construction/loop-proposal/proposal.json"


class TestFailSoft:
    """Every failure returns a warning note. The run survives all of them.

    Each test asserts three things: no exception escapes, the note is a warning
    (not a success), and it says the committed artifacts are unaffected — an
    operator who reads "failed" where nothing was lost goes hunting for damage that
    does not exist.
    """

    def assert_is_warning(self, note: str) -> None:
        assert note.startswith("### ⚠️ Delivery loop not registered")
        assert "remain" in note and "source of truth" in note
        # No accept instruction in any form: a registration that did not happen has
        # nothing to accept. Matched on the command itself rather than the sentence
        # around it, so the #4599 reformat cannot make this assertion vacuous.
        assert "@agent-engine accept" not in note

    def test_malformed_json_warns(self, tmp_path):
        write_proposal(tmp_path, "{not json at all")
        self.assert_is_warning(draft_registration_note(work_dir=tmp_path, issue=ISSUE))

    def test_json_that_is_not_an_object_warns(self, tmp_path):
        write_proposal(tmp_path, "[1, 2, 3]")
        note = draft_registration_note(work_dir=tmp_path, issue=ISSUE)
        self.assert_is_warning(note)
        assert "must be a JSON object" in note

    def test_missing_tenant_warns_and_sends_nothing(self, monkeypatch, tmp_path):
        """A blank tenant is refused here rather than sent for the gateway to reject.

        A 422 from the engine would name the document, sending an operator to look
        at the proposal when the actual fault is a missing envelope field.
        """
        monkeypatch.delenv("ADP_TENANT_ID", raising=False)
        write_proposal(tmp_path, valid_document())

        with patch("lib.engine_registration.urlopen") as urlopen:
            note = draft_registration_note(work_dir=tmp_path, issue=ISSUE)

        urlopen.assert_not_called()
        self.assert_is_warning(note)
        assert "ADP_TENANT_ID" in note

    def test_missing_endpoint_warns(self, monkeypatch, tmp_path):
        monkeypatch.delenv("ADP_GATEWAY_ENDPOINT", raising=False)
        write_proposal(tmp_path, valid_document())
        note = draft_registration_note(work_dir=tmp_path, issue=ISSUE)
        self.assert_is_warning(note)
        assert "ADP_GATEWAY_ENDPOINT" in note

    @pytest.mark.parametrize("status", [400, 403, 409, 422, 500])
    def test_http_error_warns(self, tmp_path, status):
        """Includes 409 — the conflict a re-registration into a live flow now gets."""
        write_proposal(tmp_path, valid_document())
        error = HTTPError(url=ENDPOINT, code=status, msg="nope", hdrs=None, fp=None)

        with patch("lib.engine_registration.urlopen", side_effect=error):
            note = draft_registration_note(work_dir=tmp_path, issue=ISSUE)

        self.assert_is_warning(note)
        assert str(status) in note

    def test_unreachable_gateway_warns(self, tmp_path):
        write_proposal(tmp_path, valid_document())

        with patch("lib.engine_registration.urlopen", side_effect=URLError("connection refused")):
            note = draft_registration_note(work_dir=tmp_path, issue=ISSUE)

        self.assert_is_warning(note)
        assert "cannot reach gateway" in note

    def test_non_json_response_warns(self, tmp_path):
        write_proposal(tmp_path, valid_document())

        with patch("lib.engine_registration.urlopen", return_value=http_response("<html>502</html>")):
            note = draft_registration_note(work_dir=tmp_path, issue=ISSUE)

        self.assert_is_warning(note)

    def test_an_unexpected_exception_still_cannot_kill_the_run(self, tmp_path):
        """The bare-`Exception` clause, asserted rather than assumed.

        Signing reaches botocore, which can fail in ways this module does not
        enumerate. The blanket clause exists for exactly that, so it needs a test
        that proves a non-`EngineRegistrationError` is also contained.
        """
        write_proposal(tmp_path, valid_document())

        with patch("lib.engine_registration._sigv4_sign_request", side_effect=RuntimeError("botocore exploded")):
            note = draft_registration_note(work_dir=tmp_path, issue=ISSUE)

        self.assert_is_warning(note)
        assert "unexpected error" in note

    def test_draft_registration_note_never_raises_for_any_failure(self, tmp_path):
        """The contract in one assertion: entrypoint.py needs no error handling."""
        write_proposal(tmp_path, valid_document())

        for failure in (URLError("down"), RuntimeError("boom"), ValueError("weird"), OSError("io")):
            with patch("lib.engine_registration.urlopen", side_effect=failure):
                assert isinstance(draft_registration_note(work_dir=tmp_path, issue=ISSUE), str)


class TestTenantIsAlwaysServerResolved:
    """`org_id` comes from the pod's env, never from the agent-authored artifact."""

    def sent_document(self, urlopen: MagicMock) -> dict:
        return json.loads(urlopen.call_args[0][0].data.decode("utf-8"))

    def test_blank_org_id_is_filled_from_env(self, tmp_path):
        write_proposal(tmp_path, valid_document(org_id=""))

        with patch("lib.engine_registration.urlopen", return_value=http_response(json.dumps(GATEWAY_OK))) as urlopen:
            register_loop_proposal(work_dir=tmp_path, issue=ISSUE)

        assert self.sent_document(urlopen)["org_id"] == TENANT

    def test_a_forged_org_id_in_the_artifact_is_overwritten(self, tmp_path):
        """The security property: the agent authored this file and could name any tenant."""
        write_proposal(tmp_path, valid_document(org_id="org-victim"))

        with patch("lib.engine_registration.urlopen", return_value=http_response(json.dumps(GATEWAY_OK))) as urlopen:
            register_loop_proposal(work_dir=tmp_path, issue=ISSUE)

        assert self.sent_document(urlopen)["org_id"] == TENANT

    def test_absent_intent_ref_falls_back_to_the_run_issue(self, tmp_path):
        write_proposal(tmp_path, valid_document())

        with patch("lib.engine_registration.urlopen", return_value=http_response(json.dumps(GATEWAY_OK))) as urlopen:
            register_loop_proposal(work_dir=tmp_path, issue=ISSUE)

        assert self.sent_document(urlopen)["intent_ref"] == str(ISSUE)

    def test_a_declared_intent_ref_is_kept(self, tmp_path):
        """A proposal may be authored on one issue for an intent tracked on another."""
        document = valid_document()
        document["intent_ref"] = "4120"
        write_proposal(tmp_path, document)

        with patch("lib.engine_registration.urlopen", return_value=http_response(json.dumps(GATEWAY_OK))) as urlopen:
            register_loop_proposal(work_dir=tmp_path, issue=ISSUE)

        assert self.sent_document(urlopen)["intent_ref"] == "4120"

    def test_the_post_goes_to_the_agent_plane_route(self, tmp_path):
        """`/agent/*`, never `/internal/v1/*` — a different plane, not a spelling."""
        write_proposal(tmp_path, valid_document())

        with patch("lib.engine_registration.urlopen", return_value=http_response(json.dumps(GATEWAY_OK))) as urlopen:
            register_loop_proposal(work_dir=tmp_path, issue=ISSUE)

        request = urlopen.call_args[0][0]
        assert request.full_url == f"{ENDPOINT}/agent/orchestration/flows/drafts"
        assert request.method == "POST"
        assert "/internal/" not in request.full_url


class TestRunIdHeader:
    """Issue #4597: the run reference that lets the gateway establish the tenant.

    The pod is resolved as the shared `scaledjob-worker` registry entry, whose
    `org_id` is `__platform__` — no real tenant. So the gateway derives the owning
    tenant from the run's ingress row instead, and this header is how the request
    names that row. Without it every real-tenant registration is refused.
    """

    def sent_headers(self, urlopen: MagicMock) -> dict:
        """Header names lowercased: urllib title-cases what it stores, and the
        assertions here are about the wire name, not about urllib's casing."""
        return {name.lower(): value for name, value in urlopen.call_args[0][0].header_items()}

    def test_the_run_id_is_sent_under_the_platforms_header_name(self, tmp_path):
        """`X-Agent-RunId` — one word, matching `proxy/routes.py`'s `x-agent-runid`.

        The spelling is the contract. `X-Agent-Run-Id` is a different header and the
        gateway does not read it, so a hyphenation drift here refuses every
        registration while looking correct in a diff.
        """
        write_proposal(tmp_path, valid_document())

        with patch("lib.engine_registration.urlopen", return_value=http_response(json.dumps(GATEWAY_OK))) as urlopen:
            register_loop_proposal(work_dir=tmp_path, issue=ISSUE)

        assert self.sent_headers(urlopen)["x-agent-runid"] == RUN_ID

    def test_the_run_id_is_inside_the_signed_header_set(self, tmp_path):
        """Signed, not appended after signing, so it cannot be rewritten in flight.

        Asserted on what is handed to the signer: a header added to the request after
        `_sigv4_sign_request` returns would still arrive, and would still work, which
        is precisely why the weaker arrangement needs a test to stay out.
        """
        write_proposal(tmp_path, valid_document())

        with patch("lib.engine_registration._sigv4_sign_request", return_value={"Authorization": "AWS4-x"}) as signer:
            with patch("lib.engine_registration.urlopen", return_value=http_response(json.dumps(GATEWAY_OK))):
                register_loop_proposal(work_dir=tmp_path, issue=ISSUE)

        signed_headers = signer.call_args[0][2]
        assert signed_headers["X-Agent-RunId"] == RUN_ID

    def test_the_worker_sends_no_org_header(self, tmp_path):
        """The tenant is NOT asserted. `X-Agent-OrgId` is caller-influenced and the
        #4132 invariant forbids it gating access, so the worker must not start
        sending it here and invite the gateway to read it."""
        write_proposal(tmp_path, valid_document())

        with patch("lib.engine_registration.urlopen", return_value=http_response(json.dumps(GATEWAY_OK))) as urlopen:
            register_loop_proposal(work_dir=tmp_path, issue=ISSUE)

        assert "x-agent-orgid" not in self.sent_headers(urlopen)

    @pytest.mark.parametrize("value", ["", "   "])
    def test_a_missing_run_id_warns_and_sends_nothing(self, monkeypatch, tmp_path, value):
        """Reported here rather than sent blank, for the same reason as the tenant.

        The gateway's refusal would name the header; an operator reading the closing
        comment needs to know the *pod* had nothing to send. And it stays fail-soft: a
        warning note, never a raise.
        """
        monkeypatch.setenv("ADP_MESSAGE_ID", value)
        write_proposal(tmp_path, valid_document())

        with patch("lib.engine_registration.urlopen") as urlopen:
            note = draft_registration_note(work_dir=tmp_path, issue=ISSUE)

        urlopen.assert_not_called()
        assert "ADP_MESSAGE_ID" in note
        assert note.startswith("### ⚠️")

    def test_an_absent_run_id_env_var_warns_and_sends_nothing(self, monkeypatch, tmp_path):
        """Unset, not merely blank — the shape a pod that never exported it has."""
        monkeypatch.delenv("ADP_MESSAGE_ID", raising=False)
        write_proposal(tmp_path, valid_document())

        with patch("lib.engine_registration.urlopen") as urlopen:
            note = draft_registration_note(work_dir=tmp_path, issue=ISSUE)

        urlopen.assert_not_called()
        assert "ADP_MESSAGE_ID" in note


class TestSuccessNote:
    """What the human reads, and the one command they type."""

    def test_success_note_names_the_flow_and_the_accept_command(self, tmp_path):
        write_proposal(tmp_path, valid_document())

        with patch("lib.engine_registration.urlopen", return_value=http_response(json.dumps(GATEWAY_OK))):
            note = draft_registration_note(work_dir=tmp_path, issue=ISSUE)

        assert "flow-abc123" in note
        assert "loop/epic-1/wave-1/accept" in note
        assert "@agent-engine accept" in note
        # The plan must be described as executing nothing — this is the promise the
        # whole story rests on.
        assert "draft" in note and "executes nothing" in note

    def test_the_id_is_labelled_flow_not_plan(self, tmp_path):
        """#4885: the value is a `flow_id`, so calling it a "Plan" misdirects.

        The reader is being sent to find this thing in the graph UI, where it is
        addressed as a flow. A plan is the versioned document attached to it — a
        different noun that also appears in this note, as `v1`.
        """
        write_proposal(tmp_path, valid_document())

        with patch("lib.engine_registration.urlopen", return_value=http_response(json.dumps(GATEWAY_OK))):
            note = draft_registration_note(work_dir=tmp_path, issue=ISSUE)

        assert "**Flow**:" in note
        assert "**Plan**:" not in note

    def test_the_flow_id_links_to_the_gateway_url(self, tmp_path):
        """The note promises the plan is "visible in the graph UI"; this is the address.

        Without it the reader had to already know how to reach the UI in order to
        follow an instruction telling them it was there.
        """
        write_proposal(tmp_path, valid_document())

        with patch("lib.engine_registration.urlopen", return_value=http_response(json.dumps(GATEWAY_OK))):
            note = draft_registration_note(work_dir=tmp_path, issue=ISSUE)

        assert "[`flow-abc123`](https://gateway.example.com/flows/flow-abc123)" in note

    def test_the_url_is_taken_from_the_gateway_not_composed_here(self, tmp_path):
        """Only the gateway knows the user-facing origin.

        This worker holds `ADP_GATEWAY_ENDPOINT`, the API Gateway invoke URL — the
        machine plane. Composing a link from it would send an operator somewhere they
        cannot use. So the URL is whatever the response said, verbatim.
        """
        write_proposal(tmp_path, valid_document())
        response = {**GATEWAY_OK, "flow_url": "https://adp.internal.example/flows/flow-abc123"}

        with patch("lib.engine_registration.urlopen", return_value=http_response(json.dumps(response))):
            note = draft_registration_note(work_dir=tmp_path, issue=ISSUE)

        assert "(https://adp.internal.example/flows/flow-abc123)" in note
        assert ENDPOINT not in note

    @pytest.mark.parametrize("missing", [None, ""])
    def test_without_a_url_the_id_is_still_printed_bare(self, tmp_path, missing):
        """A missing link degrades the comment; it must never lose the plan.

        `flow_url` is `None` when `BG_GATEWAY_BASE_URL` is unset on the gateway, and
        absent entirely if this worker image is newer than the gateway it calls. Both
        are ordinary rollout states, not failures.
        """
        write_proposal(tmp_path, valid_document())
        response = {**GATEWAY_OK, "flow_url": missing}

        with patch("lib.engine_registration.urlopen", return_value=http_response(json.dumps(response))):
            note = draft_registration_note(work_dir=tmp_path, issue=ISSUE)

        assert "`flow-abc123`" in note
        assert "](" not in note, "a falsy flow_url produced a link anyway"

    def test_a_response_with_no_flow_url_key_at_all_still_succeeds(self, tmp_path):
        """An older gateway does not send the field. Registration still worked."""
        write_proposal(tmp_path, valid_document())
        response = {key: value for key, value in GATEWAY_OK.items() if key != "flow_url"}

        with patch("lib.engine_registration.urlopen", return_value=http_response(json.dumps(response))):
            note = draft_registration_note(work_dir=tmp_path, issue=ISSUE)

        assert "**Flow**: `flow-abc123`" in note
        assert "@agent-engine accept" in note

    def test_the_accept_command_is_quoted_from_the_gateway_not_hardcoded(self, tmp_path):
        """If the parser's wording changes, the comment follows it automatically."""
        write_proposal(tmp_path, valid_document())
        response = {**GATEWAY_OK, "accept_command": "@agent-engine approve-plan"}

        with patch("lib.engine_registration.urlopen", return_value=http_response(json.dumps(response))):
            note = draft_registration_note(work_dir=tmp_path, issue=ISSUE)

        assert "@agent-engine approve-plan" in note

    def test_the_command_is_fenced_not_inline(self, tmp_path):
        """Issue #4599: this note must not trigger the bridge it is announcing.

        It posts on every successful registration, and it used to quote the command
        in inline backticks — which the tick read as a live command: marked pending,
        parsed, refused (a bot has no `PLAN_APPROVE`) and answered on the thread with
        "this command cannot be applied by this account". Every registration produced
        that reply, so the feature's own success message was the noise source.

        A fenced block is ignored by the parser's code-awareness rule while staying
        copy-pasteable — the property the human actually needs from this line. The
        assertion is on the fence rather than on "no inline span anywhere" because
        other fields in this note (`flow_id`, the gate address) are legitimately
        inline-quoted; it is the COMMAND that must not be.
        """
        write_proposal(tmp_path, valid_document())

        with patch("lib.engine_registration.urlopen", return_value=http_response(json.dumps(GATEWAY_OK))):
            note = draft_registration_note(work_dir=tmp_path, issue=ISSUE)

        assert "```\n@agent-engine accept\n```" in note
        assert "`@agent-engine accept`" not in note.replace("```\n@agent-engine accept\n```", "")

    def test_an_idempotent_retry_says_so(self, tmp_path):
        """A fail-soft retry must not read as a second plan having been created."""
        write_proposal(tmp_path, valid_document())
        response = {**GATEWAY_OK, "already_registered": True}

        with patch("lib.engine_registration.urlopen", return_value=http_response(json.dumps(response))):
            note = draft_registration_note(work_dir=tmp_path, issue=ISSUE)

        assert "already registered" in note


class TestRegisterLoopProposalRaises:
    """The low-level function raises; only `draft_registration_note` swallows.

    Keeping the raise is what lets a future caller that *should* care about failure
    (a smoke test, an operator CLI) see it, without weakening the fail-soft path.
    """

    def test_missing_artifact_raises(self, tmp_path):
        with pytest.raises(EngineRegistrationError):
            register_loop_proposal(work_dir=tmp_path, issue=ISSUE)

    def test_http_error_raises(self, tmp_path):
        write_proposal(tmp_path, valid_document())
        error = HTTPError(url=ENDPOINT, code=409, msg="conflict", hdrs=None, fp=None)

        with patch("lib.engine_registration.urlopen", side_effect=error), pytest.raises(EngineRegistrationError):
            register_loop_proposal(work_dir=tmp_path, issue=ISSUE)
