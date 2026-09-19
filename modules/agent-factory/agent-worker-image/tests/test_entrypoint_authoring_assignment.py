"""The authoring-assignment export in entrypoint.main (issue #4529).

`lib/engine_registration.amendment_registration_note` reads the assignment out of the
process environment rather than taking it as arguments, deliberately: the two ids are
the *authorization* for the amendment route, not parameters to it, and the only place
they may come from is the trusted dispatch envelope. This file is the test of that
boundary — that the envelope's `orchestration.flow_id` / `orchestration.request_id`
reach the environment, and that nothing else does.

Three things are asserted, one per class:

  * a commissioned authoring assignment IS exported,
  * every other kind of run — webhook trigger, node dispatch, new-flow authoring — is
    left byte-identical to before, and
  * the agent cannot name its own assignment: the ids are not read from the payload,
    the issue, or a pre-set environment variable that a prior step could have planted.

**On proving these tests are not vacuous.** `main()` has many early returns, and
`aidlc` is in `PERSONAS_EXTENDING_BRANCH`, so an unpatched run reaches
`is_delivery_completed`, raises `InvocationCompletionError` with no DynamoDB, and
returns `AGENT_EXIT_RETRYABLE` *before* the export block. Every "exports nothing"
assertion would then pass for entirely the wrong reason. So `is_delivery_completed` is
patched, and each absence test additionally asserts a POSITIVE control — that
`ADP_TENANT_ID`, set a few lines above the code under test in the same block, is
present. If the run bails early the control fails loudly instead of the test passing
on a technicality.

**On WORK_DIR.** `main()` step 5 calls `shutil.rmtree(WORK_DIR)`, and the module-level
default is `/work/repo` — which is a real checkout on a developer or agent machine.
`run_worker` monkeypatches `WORK_DIR` to `tmp_path` for that reason, and `shutil.rmtree`
is NOT patched out, so a regression that reintroduced the module-level path would
delete a temp dir rather than someone's work.
"""

from __future__ import annotations

import json
import hashlib
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import entrypoint  # noqa: E402
from lib.engine_registration import (  # noqa: E402
    AMENDMENT_BASE_HASH_ENV,
    AMENDMENT_BASE_PATH_ENV,
    AMENDMENT_BASE_VERSION_ENV,
    AMENDMENT_OUTPUT_PATH_ENV,
    AMENDMENT_REQUEST_ENV,
    AMENDMENT_REQUEST_TEXT_ENV,
    FLOW_ID_ENV,
)

#: Every name the export block owns. Used by the fixture's wipe and by the
#: "nothing leaks" assertions, so adding a sixth variable to the block without adding
#: it here cannot leave a stale-value test silently covering five of six names.
ALL_ASSIGNMENT_ENV = (
    FLOW_ID_ENV,
    AMENDMENT_REQUEST_ENV,
    AMENDMENT_REQUEST_TEXT_ENV,
    AMENDMENT_BASE_VERSION_ENV,
    AMENDMENT_BASE_HASH_ENV,
    AMENDMENT_BASE_PATH_ENV,
    AMENDMENT_OUTPUT_PATH_ENV,
)

FLOW_ID = "flow-abc123"
REQUEST_ID = "req-7c9a1e20"
AUTHOR_RUN_ID = "replan:8f14e45f-ceea-467a-9a3e-4dc1b0a1f4b7"

#: What `authoring_dispatch._build_envelope` publishes for a `replan:` ask. Copied
#: field-for-field from that function rather than minimised, because a test built on a
#: guessed envelope shape proves nothing about the real dispatch.
AUTHORING_ENVELOPE = {
    "version": "1.0",
    "message_id": AUTHOR_RUN_ID,
    "actor": {"user_id": "cognito-sub-jane-123", "org_id": "acme-corp"},
    "cognito_sub": "cognito-sub-jane-123",
    "channel": "orchestration",
    "tenant_id": "acme-corp",
    "persona": "aidlc",
    "source_ref": {"installation_id": 99887766, "repo": "acme-corp/flagship-app", "issue": 42},
    "intent": {"trigger": "engine_replan", "label": None, "persona": "aidlc"},
    "correlation": {
        "correlation_id": AUTHOR_RUN_ID,
        "root_human_id": "cognito-sub-jane-123",
        "is_human_rooted": True,
        "chain_depth": 0,
    },
    "orchestration": {
        "flow_id": FLOW_ID,
        "request_id": REQUEST_ID,
        "root_decision_id": "dec-99",
        "base_plan_version": 4,
        "base_plan_hash": ("a" * 64),
    },
    "payload": {"replan_request": "replan: gate the deploy wave", "requested_by": "jane-dev"},
    "arrived_at": "2026-09-19T14:22:00Z",
}


# The immutable input is transported with the same trusted dispatch envelope.
_BASE_DOCUMENT = {"flow_slug": "demo", "nodes": [{"address": "demo/e/w/accept", "kind": "gate"}], "edges": []}
AUTHORING_ENVELOPE["payload"]["amendment_base"] = {
    "version": 1,
    "org_id": AUTHORING_ENVELOPE["tenant_id"],
    "flow_id": FLOW_ID,
    "request_id": REQUEST_ID,
    "author_run_id": AUTHOR_RUN_ID,
    "base_plan_version": 4,
    "base_plan_hash": "a" * 64,
    "document_sha256": hashlib.sha256(json.dumps(_BASE_DOCUMENT, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
    "document": _BASE_DOCUMENT,
}

#: A code-story dispatch, from `dispatch_pass._build_envelope`. This one is why the
#: export is gated on the PAIR: it carries `flow_id` in the same block, with no
#: `request_id`. A `flow_id`-only test would make every dispatched story look like a
#: commissioned amendment.
NODE_DISPATCH_ENVELOPE = {
    "version": "1.0",
    "message_id": "run-node-1",
    "actor": {"user_id": "cognito-sub-jane-123", "org_id": "acme-corp"},
    "cognito_sub": "cognito-sub-jane-123",
    "channel": "orchestration",
    "tenant_id": "acme-corp",
    "persona": "developer",
    "source_ref": {"installation_id": 99887766, "repo": "acme-corp/flagship-app", "issue": 42},
    "intent": {"trigger": "engine_dispatch", "label": None, "persona": "developer"},
    "correlation": {
        "correlation_id": "run-node-1",
        "root_human_id": "cognito-sub-jane-123",
        "is_human_rooted": True,
        "chain_depth": 0,
    },
    "orchestration": {
        "node_id": "node-7",
        "flow_id": FLOW_ID,
        "graph_address": "loop/epic-1/wave-1/story",
        "root_decision_id": "dec-1",
        "attempt": 1,
    },
    "payload": {},
    "arrived_at": "2026-09-19T14:22:00Z",
}

#: An ordinary GitHub webhook trigger: no `orchestration` block at all.
WEBHOOK_ENVELOPE = {
    "version": "1.0",
    "channel": "github",
    "tenant_id": "acme-corp",
    "persona": "developer",
    "message_id": "msg-abc-123",
    "actor": {"github_id": 12345678, "github_login": "jane-dev", "user_id": "cognito-sub-jane-123", "is_bot": False},
    "source_ref": {"installation_id": 99887766, "repo": "acme-corp/flagship-app", "issue": 42},
    "intent": {"trigger": "issue_labeled", "label": "developer"},
    "arrived_at": "2026-09-19T14:22:00Z",
}


def _subprocess_side_effect(*args, **kwargs):
    cmd = args[0] if args else kwargs.get("args", [])
    if cmd and cmd[0:2] == ["git", "ls-remote"]:
        return MagicMock(returncode=1, stdout="", stderr="")
    return MagicMock(returncode=0, stdout="", stderr="")


@pytest.fixture
def run_worker(monkeypatch, tmp_path):
    """Run `main()` against one envelope with the outside world stubbed out.

    Only the boundary is faked — SQS, GitHub, the vault, the agent subprocess. The
    envelope parsing and the export block under test run for real.
    """

    def _run(envelope: dict, *, clear_assignment: bool = True) -> int:
        # `clear_assignment=False` is for the tests that plant a value and require the
        # export block itself to deal with it. Clearing unconditionally here is what
        # made `test_a_dispatch_assignment_overwrites_a_preset_environment_value`
        # unable to fail: the planted values were gone before the code under test ran,
        # so it passed identically whether the export assigned or merely defaulted.
        to_clear = ["ADP_TENANT_ID"]
        if clear_assignment:
            to_clear += list(ALL_ASSIGNMENT_ENV)
        for var in to_clear:
            monkeypatch.delenv(var, raising=False)

        monkeypatch.setenv("QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123/q")
        monkeypatch.setenv("AWS_REGION", "us-east-1")
        monkeypatch.setenv("ADP_AGENT_AUTHORITY_ENABLED", "false")
        monkeypatch.setenv("ADP_GH_TOKEN_BROKER_ENABLED", "0")

        monkeypatch.setattr(entrypoint, "_receive_one_message", lambda *_: (json.dumps(envelope), "receipt"))
        monkeypatch.setattr(entrypoint, "_delete_message", MagicMock())
        monkeypatch.setattr(entrypoint, "create_check_run", MagicMock(return_value={"id": 111}))
        monkeypatch.setattr(entrypoint, "update_check_run", MagicMock())
        monkeypatch.setattr(entrypoint, "_start_sigv4_proxy", MagicMock())
        monkeypatch.setattr(entrypoint, "_stop_sigv4_proxy", MagicMock())
        monkeypatch.setattr(entrypoint, "BootstrapLogger", MagicMock())
        monkeypatch.setattr(entrypoint, "record_delivery_completed", MagicMock())
        # THE patch that makes the absence assertions mean anything: `aidlc` is in
        # PERSONAS_EXTENDING_BRANCH, so without this the run returns
        # AGENT_EXIT_RETRYABLE before it ever reaches the export block.
        monkeypatch.setattr(entrypoint, "is_delivery_completed", MagicMock(return_value=False))
        monkeypatch.setattr(entrypoint, "run_cmd", MagicMock(return_value=MagicMock(stdout="", stderr="", returncode=0)))
        monkeypatch.setattr(entrypoint, "mint_installation_token", MagicMock(return_value="ghs_test"))

        vault = MagicMock()
        vault.get_secret.return_value = {"app_id": "123", "private_key": "k"}
        monkeypatch.setattr(entrypoint, "VaultClient", MagicMock(return_value=vault))

        # WORK_DIR before anything can run: step 5 rmtree's it, and the module default
        # is /work/repo.
        work_dir = tmp_path / "repo"
        work_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(entrypoint, "WORK_DIR", work_dir)
        monkeypatch.setattr(entrypoint, "PERSONAS_DIR", tmp_path / "personas")
        monkeypatch.setattr(entrypoint, "SKILLS_DIR", tmp_path / "skills")

        with (
            patch.object(entrypoint.shutil, "copytree", MagicMock()),
            patch.object(entrypoint.subprocess, "run", side_effect=_subprocess_side_effect),
        ):
            return entrypoint.main()

    return _run


def assert_reached_the_export_block(envelope: dict) -> None:
    """Positive control.

    `ADP_TENANT_ID` is written in the same block as the assignment export, a few lines
    above it, and unconditionally. If it is absent the run bailed early and any
    assertion about what the export block did or did not do is vacuous.
    """
    assert os.environ.get("ADP_TENANT_ID") == envelope["tenant_id"], (
        "the run did not reach the envelope export block, so this test proves nothing "
        "about the assignment export"
    )


class TestAnAuthoringAssignmentIsExported:
    def test_both_ids_reach_the_environment(self, run_worker):
        run_worker(AUTHORING_ENVELOPE)
        assert_reached_the_export_block(AUTHORING_ENVELOPE)
        assert os.environ.get(FLOW_ID_ENV) == FLOW_ID
        assert os.environ.get(AMENDMENT_REQUEST_ENV) == REQUEST_ID

    def test_the_exported_ids_are_what_the_library_reads(self, run_worker):
        """End-to-end through the real accessor rather than the env var names, so a
        rename on either side cannot leave this passing while the wiring is broken."""
        from lib.engine_registration import authoring_assignment

        run_worker(AUTHORING_ENVELOPE)
        assert_reached_the_export_block(AUTHORING_ENVELOPE)
        assert authoring_assignment() == (FLOW_ID, REQUEST_ID)

    def test_the_run_id_binding_is_the_envelope_message_id(self, run_worker):
        """`ADP_MESSAGE_ID` does double duty: it is the `webhook-events` key elsewhere,
        and on this path it IS the `author_run_id` the server bound to the assignment.
        If the export drifted, the amendment route would refuse every registration with
        the same 404 it uses for "not commissioned"."""
        run_worker(AUTHORING_ENVELOPE)
        assert_reached_the_export_block(AUTHORING_ENVELOPE)
        assert os.environ.get("ADP_MESSAGE_ID") == AUTHOR_RUN_ID

    def test_whitespace_around_an_id_is_stripped(self, run_worker):
        """The ids go into a URL path and query string. A trailing newline would be
        percent-encoded into the request and refused as an unknown flow."""
        envelope = json.loads(json.dumps(AUTHORING_ENVELOPE))
        envelope["orchestration"]["flow_id"] = f" {FLOW_ID}\n"
        envelope["orchestration"]["request_id"] = f"\t{REQUEST_ID} "
        run_worker(envelope)
        assert_reached_the_export_block(envelope)
        assert os.environ.get(FLOW_ID_ENV) == FLOW_ID
        assert os.environ.get(AMENDMENT_REQUEST_ENV) == REQUEST_ID


class TestEverythingElseIsUnaffected:
    """The export must be invisible to every run that was not commissioned to amend."""

    def test_a_webhook_trigger_exports_no_assignment(self, run_worker):
        run_worker(WEBHOOK_ENVELOPE)
        assert_reached_the_export_block(WEBHOOK_ENVELOPE)
        assert FLOW_ID_ENV not in os.environ
        assert AMENDMENT_REQUEST_ENV not in os.environ

    def test_a_node_dispatch_exports_no_assignment(self, run_worker):
        """The case the pair test exists for: `orchestration.flow_id` is present, and
        `request_id` is not. Exporting the flow alone would make the finish path attempt
        an amendment registration on every dispatched story."""
        run_worker(NODE_DISPATCH_ENVELOPE)
        assert_reached_the_export_block(NODE_DISPATCH_ENVELOPE)
        assert FLOW_ID_ENV not in os.environ
        assert AMENDMENT_REQUEST_ENV not in os.environ

    def test_a_new_flow_authoring_run_exports_no_assignment(self, run_worker):
        """A first-time plan is `POST /flows/drafts`, authorized by holding
        `PLAN_DRAFT`. It has no request to amend, and must keep taking the new-flow
        path."""
        envelope = json.loads(json.dumps(AUTHORING_ENVELOPE))
        del envelope["orchestration"]
        run_worker(envelope)
        assert_reached_the_export_block(envelope)
        assert FLOW_ID_ENV not in os.environ
        assert AMENDMENT_REQUEST_ENV not in os.environ

    @pytest.mark.parametrize("dropped", ["flow_id", "request_id"])
    def test_half_an_assignment_exports_neither_id(self, run_worker, dropped):
        """Both or neither. Exporting one leaves the library holding a half-assignment
        it would have to guess about — and the guess would be a request to the wrong
        flow or with no authorization."""
        envelope = json.loads(json.dumps(AUTHORING_ENVELOPE))
        del envelope["orchestration"][dropped]
        run_worker(envelope)
        assert_reached_the_export_block(envelope)
        assert FLOW_ID_ENV not in os.environ
        assert AMENDMENT_REQUEST_ENV not in os.environ

    @pytest.mark.parametrize("blank", ["", "   "])
    def test_a_blank_id_exports_nothing(self, run_worker, blank):
        envelope = json.loads(json.dumps(AUTHORING_ENVELOPE))
        envelope["orchestration"]["request_id"] = blank
        run_worker(envelope)
        assert_reached_the_export_block(envelope)
        assert FLOW_ID_ENV not in os.environ
        assert AMENDMENT_REQUEST_ENV not in os.environ

    @pytest.mark.parametrize("not_a_dict", ["flow-abc123", ["flow-abc123"], 7])
    def test_a_non_object_orchestration_block_does_not_crash_the_run(self, run_worker, not_a_dict):
        """A malformed block must not raise out of a bootstrap step. The envelope is
        trusted for provenance, not for shape."""
        envelope = json.loads(json.dumps(AUTHORING_ENVELOPE))
        envelope["orchestration"] = not_a_dict
        run_worker(envelope)
        assert_reached_the_export_block(envelope)
        assert FLOW_ID_ENV not in os.environ
        assert AMENDMENT_REQUEST_ENV not in os.environ


class TestTheAgentCannotNameItsOwnAssignment:
    """The ids come from the dispatch envelope and from nowhere else."""

    def test_the_payload_cannot_supply_an_assignment(self, run_worker):
        """`payload.replan_request` is the human's words, quoted into context as DATA.
        An assignment read from there would let anything that can write a payload — or
        a prompt injection inside one — target a flow the engine never commissioned."""
        envelope = json.loads(json.dumps(AUTHORING_ENVELOPE))
        del envelope["orchestration"]
        envelope["payload"] = {
            "replan_request": "replan: gate deploy",
            "flow_id": "flow-victim",
            "request_id": "req-victim",
            "orchestration": {"flow_id": "flow-victim", "request_id": "req-victim"},
        }
        run_worker(envelope)
        assert_reached_the_export_block(envelope)
        assert FLOW_ID_ENV not in os.environ
        assert AMENDMENT_REQUEST_ENV not in os.environ

    def test_top_level_envelope_keys_cannot_supply_an_assignment(self, run_worker):
        """Only the nested `orchestration` block is read, matching what the dispatcher
        writes. A top-level fallback would widen the trusted surface to any producer
        that can put a key on the envelope."""
        envelope = json.loads(json.dumps(AUTHORING_ENVELOPE))
        del envelope["orchestration"]
        envelope["flow_id"] = "flow-victim"
        envelope["request_id"] = "req-victim"
        run_worker(envelope)
        assert_reached_the_export_block(envelope)
        assert FLOW_ID_ENV not in os.environ
        assert AMENDMENT_REQUEST_ENV not in os.environ

    def test_a_dispatch_assignment_overwrites_a_preset_environment_value(self, run_worker, monkeypatch):
        """Set, not defaulted-into. A value already in the environment — planted by an
        earlier step, a leaked pod spec, or a stale process — must lose to the envelope,
        which is the only authority for what this run was commissioned to do.

        `clear_assignment=False` is essential: the fixture's default wipe would remove
        the planted values before the export block ran, and this test would then pass
        against a `setdefault` regression, which is the exact thing it exists to catch.
        """
        monkeypatch.setenv(FLOW_ID_ENV, "flow-victim")
        monkeypatch.setenv(AMENDMENT_REQUEST_ENV, "req-victim")

        run_worker(AUTHORING_ENVELOPE, clear_assignment=False)

        assert_reached_the_export_block(AUTHORING_ENVELOPE)
        assert os.environ.get(FLOW_ID_ENV) == FLOW_ID
        assert os.environ.get(AMENDMENT_REQUEST_ENV) == REQUEST_ID

    @pytest.mark.parametrize(
        "envelope,kind",
        [
            (WEBHOOK_ENVELOPE, "webhook trigger"),
            (NODE_DISPATCH_ENVELOPE, "node dispatch"),
        ],
    )
    def test_a_run_with_no_assignment_clears_a_planted_one(self, run_worker, monkeypatch, envelope, kind):
        """A stale assignment must not be inherited by a run that was never given one.

        The registration client reads these two names and has no envelope of its own to
        cross-check against, so a value surviving in the environment IS an assignment as
        far as it is concerned. A webhook trigger or a node dispatch that inherited one
        would file an amendment against a flow nobody commissioned it to touch.

        This is the question the broken overwrite test hid: it cleared the environment
        itself, so no test observed what an unassigned run does with a planted pair.
        """
        monkeypatch.setenv(FLOW_ID_ENV, "flow-victim")
        monkeypatch.setenv(AMENDMENT_REQUEST_ENV, "req-victim")

        run_worker(envelope, clear_assignment=False)

        assert_reached_the_export_block(envelope)
        assert FLOW_ID_ENV not in os.environ, f"a {kind} inherited a planted flow id"
        assert AMENDMENT_REQUEST_ENV not in os.environ, f"a {kind} inherited a planted request id"

    @pytest.mark.parametrize("dropped", ["flow_id", "request_id"])
    def test_half_an_assignment_clears_a_planted_pair(self, run_worker, monkeypatch, dropped):
        """A malformed assignment falls back to no-assignment, not to the planted pair.

        Half a block is the case where "leave the environment alone" is most tempting
        and most wrong: the run has something that looks like an assignment, so a
        planted value would be silently completing it.
        """
        envelope = json.loads(json.dumps(AUTHORING_ENVELOPE))
        del envelope["orchestration"][dropped]
        monkeypatch.setenv(FLOW_ID_ENV, "flow-victim")
        monkeypatch.setenv(AMENDMENT_REQUEST_ENV, "req-victim")

        run_worker(envelope, clear_assignment=False)

        assert_reached_the_export_block(envelope)
        assert FLOW_ID_ENV not in os.environ
        assert AMENDMENT_REQUEST_ENV not in os.environ


class TestTheBriefReachesTheRun:
    """The run is told what it was summoned to DO, not merely which assignment it is.

    The two ids identify the assignment; they do not describe the job. Review of the
    first cut of this story found the consequence: an artifact path with a consumer and
    no producer. A correctly summoned, correctly authorized author received two opaque
    identifiers, no request text, no base revision and no output path — so it would
    have followed its ordinary planning instructions, opened a flow nobody asked for,
    stopped at a gate and filed nothing, while the request stayed recorded and owed.

    These assertions are the producer half of that contract. The consumer half (that
    the authoring instructions name these same variables) is pinned by
    `tests/test_amendment_authoring_contract.py`, which reads the real staged
    instruction text against the real library constants. Neither implies the other:
    exporting a variable no instruction mentions is the bug being fixed here, and an
    instruction naming a variable nothing exports is the same bug mirrored.
    """

    def test_the_request_text_reaches_the_run(self, run_worker):
        """The human's words. Without them the author knows a plan should change but not
        how, which is the difference between doing the job and guessing at it."""
        run_worker(AUTHORING_ENVELOPE)
        assert_reached_the_export_block(AUTHORING_ENVELOPE)
        assert os.environ.get(AMENDMENT_REQUEST_TEXT_ENV) == AUTHORING_ENVELOPE["payload"]["replan_request"]

    def test_the_base_revision_reaches_the_run(self, run_worker):
        """What was in force when the human asked. The author amends the version the
        human was looking at; a fresh read could see a different one, and the server
        refuses a draft whose base does not match the assignment."""
        run_worker(AUTHORING_ENVELOPE)
        assert_reached_the_export_block(AUTHORING_ENVELOPE)
        assert os.environ.get(AMENDMENT_BASE_VERSION_ENV) == "4"
        assert os.environ.get(AMENDMENT_BASE_HASH_ENV) == ("a" * 64)

    def test_the_output_path_is_the_one_the_client_reads(self, run_worker, tmp_path):
        """The heart of the finding. The exported path is composed with the same helper
        `register_amendment_proposal` loads from, so an author following the instruction
        writes the file the client opens.

        Asserted through the real helper rather than against a hardcoded string: a
        change to `AMENDMENT_ARTIFACT_TEMPLATE` must move both sides together or fail
        here, which is precisely the drift that produced a consumer with no producer.
        """
        from lib.engine_registration import amendment_artifact_path

        run_worker(AUTHORING_ENVELOPE)
        assert_reached_the_export_block(AUTHORING_ENVELOPE)
        exported = os.environ.get(AMENDMENT_OUTPUT_PATH_ENV)
        assert exported == str(amendment_artifact_path(entrypoint.WORK_DIR, REQUEST_ID))
        # Absolute and inside the checkout: the author is given a path it can write to
        # without resolving anything itself, and a relative path would land wherever the
        # agent's shell happened to be.
        assert Path(exported).is_absolute()
        assert str(tmp_path) in exported

    def test_the_output_path_is_keyed_on_the_request_not_the_issue(self, run_worker):
        """Two `replan:` asks on one issue must not share a file. An issue-keyed path
        would let the second overwrite the first, and then register whichever file was
        on disk against whichever assignment was live."""
        second = json.loads(json.dumps(AUTHORING_ENVELOPE))
        second["orchestration"]["request_id"] = "req-second-ask"
        run_worker(AUTHORING_ENVELOPE)
        first_path = os.environ.get(AMENDMENT_OUTPUT_PATH_ENV)
        run_worker(second)
        assert_reached_the_export_block(second)
        assert os.environ.get(AMENDMENT_OUTPUT_PATH_ENV) != first_path
        assert "req-second-ask" in os.environ.get(AMENDMENT_OUTPUT_PATH_ENV, "")

    def test_an_empty_replan_still_gets_an_assignment_and_a_path(self, run_worker):
        """`replan:` with no text is a legitimate request — the human asked for a
        re-plan without saying what to change — and the parser records it. The author
        must still be commissioned and still be told where to write; it simply works
        from the plan alone. The text is ABSENT rather than blank so the instructions do
        not quote an empty request back at the human as if it said something.
        """
        envelope = json.loads(json.dumps(AUTHORING_ENVELOPE))
        envelope["payload"]["replan_request"] = ""
        run_worker(envelope)
        assert_reached_the_export_block(envelope)
        assert os.environ.get(FLOW_ID_ENV) == FLOW_ID
        assert os.environ.get(AMENDMENT_OUTPUT_PATH_ENV)
        assert AMENDMENT_REQUEST_TEXT_ENV not in os.environ

    @pytest.mark.parametrize("field", ["base_plan_version", "base_plan_hash"])
    def test_a_missing_base_field_is_absent_not_blank(self, run_worker, field):
        """A blank base version would read to the author as "there is no base", which is
        a different and wrong instruction. Absent means "not supplied"; the server holds
        the authoritative base either way and checks it on accept."""
        envelope = json.loads(json.dumps(AUTHORING_ENVELOPE))
        del envelope["orchestration"][field]
        run_worker(envelope)
        assert_reached_the_export_block(envelope)
        assert os.environ.get(FLOW_ID_ENV) == FLOW_ID, "the assignment itself must survive"
        missing = AMENDMENT_BASE_VERSION_ENV if field == "base_plan_version" else AMENDMENT_BASE_HASH_ENV
        assert missing not in os.environ

    def test_an_absent_payload_leaves_the_run_commissioned_without_text(self, run_worker):
        """The envelope is trusted for provenance, not for shape. A missing payload must
        leave the run commissioned with no request text rather than raising out of a
        bootstrap step — the assignment is still valid, the author just works from the
        plan alone.

        Only the absent/`null` case is driven through the whole run here. A payload that
        is a non-empty *non-object* (a bare string, a list, a number) crashes earlier in
        bootstrap, at `entrypoint.py`'s GitLab provider detection — `(envelope.get(
        "payload") or {}).get("provider")` raises `AttributeError` on any truthy
        non-dict. That is pre-existing on `origin/main` (three sites, the first at line
        1313 there) and unrelated to this story, so it is filed separately rather than
        fixed in this diff. The export block's own `isinstance` guard is proved directly
        below instead of through a run that cannot get that far.
        """
        envelope = json.loads(json.dumps(AUTHORING_ENVELOPE))
        del envelope["payload"]
        run_worker(envelope)
        assert_reached_the_export_block(envelope)
        assert os.environ.get(FLOW_ID_ENV) == FLOW_ID
        assert os.environ.get(AMENDMENT_OUTPUT_PATH_ENV), "the assignment must still be exported"
        assert AMENDMENT_REQUEST_TEXT_ENV not in os.environ

    @pytest.mark.parametrize("not_a_dict", ["replan: do a thing", ["replan"], 7])
    def test_a_non_object_payload_leaves_the_assignment_intact_and_the_text_absent(
        self, not_a_dict, monkeypatch, tmp_path
    ):
        """The export's shape guard, driven directly because the whole run cannot reach it.

        The envelope is trusted for provenance, not for shape. A `payload` that is a
        truthy non-object must leave the run commissioned with no request text rather
        than raising — the assignment is still valid.

        This calls `_export_authoring_assignment` rather than `main()` for the reason
        given in the previous test: `main()` raises `AttributeError` on such a payload at
        a pre-existing line of its own, so a whole-run probe would fail on somebody
        else's defect and prove nothing about this guard. The function under test is the
        real one, with the inputs `main()` hands it.
        """
        monkeypatch.setattr(entrypoint, "WORK_DIR", tmp_path)
        for name in ALL_ASSIGNMENT_ENV:
            monkeypatch.delenv(name, raising=False)

        entrypoint._export_authoring_assignment(
            {
                "orchestration": {"flow_id": FLOW_ID, "request_id": REQUEST_ID, "base_plan_version": 4},
                "payload": not_a_dict,
            }
        )

        assert os.environ.get(FLOW_ID_ENV) == FLOW_ID
        assert os.environ.get(AMENDMENT_REQUEST_ENV) == REQUEST_ID
        assert os.environ.get(AMENDMENT_BASE_VERSION_ENV) == "4"
        assert os.environ.get(AMENDMENT_OUTPUT_PATH_ENV)
        assert AMENDMENT_REQUEST_TEXT_ENV not in os.environ

    @pytest.mark.parametrize(
        "envelope,kind",
        [
            (WEBHOOK_ENVELOPE, "webhook trigger"),
            (NODE_DISPATCH_ENVELOPE, "node dispatch"),
        ],
    )
    def test_an_unassigned_run_gets_no_brief_at_all(self, run_worker, envelope, kind):
        """Every other kind of run behaves exactly as before. A node dispatch carries a
        `flow_id` and a `payload`, so a brief exported on a looser condition would reach
        runs that were commissioned to amend nothing."""
        run_worker(envelope)
        assert_reached_the_export_block(envelope)
        for name in ALL_ASSIGNMENT_ENV:
            assert name not in os.environ, f"a {kind} received {name}"

    def test_a_stale_brief_is_cleared_for_an_unassigned_run(self, run_worker, monkeypatch):
        """A leftover brief is worse than a leftover id, which is why it is deleted on
        the same branch.

        A stale id is checked by the server and refused. A stale *instruction* — "amend
        this plan, here is what the human asked, write it here" — is followed, by a run
        that was never asked to amend anything. Nothing downstream re-validates an
        instruction against an envelope, so this deletion is the only thing standing
        between a planted brief and an author acting on it.
        """
        for name in ALL_ASSIGNMENT_ENV:
            monkeypatch.setenv(name, f"planted-{name}")

        run_worker(WEBHOOK_ENVELOPE, clear_assignment=False)

        assert_reached_the_export_block(WEBHOOK_ENVELOPE)
        for name in ALL_ASSIGNMENT_ENV:
            assert name not in os.environ, f"a webhook trigger inherited a planted {name}"

    def test_a_planted_brief_loses_to_the_envelope(self, run_worker, monkeypatch):
        """Set, not defaulted-into, for the brief as well as the ids. A planted request
        text surviving alongside a real assignment would have the author amend the right
        plan according to the wrong instruction — the hardest failure of this class to
        notice, because everything about the run looks correctly commissioned."""
        for name in ALL_ASSIGNMENT_ENV:
            monkeypatch.setenv(name, f"planted-{name}")

        run_worker(AUTHORING_ENVELOPE, clear_assignment=False)

        assert_reached_the_export_block(AUTHORING_ENVELOPE)
        assert os.environ.get(AMENDMENT_REQUEST_TEXT_ENV) == AUTHORING_ENVELOPE["payload"]["replan_request"]
        assert os.environ.get(AMENDMENT_BASE_VERSION_ENV) == "4"
        assert os.environ.get(AMENDMENT_BASE_HASH_ENV) == ("a" * 64)
        assert os.environ.get(AMENDMENT_OUTPUT_PATH_ENV) == str(
            entrypoint.WORK_DIR / f"aidlc/spaces/amendments/{REQUEST_ID}/proposal.json"
        )

    def test_a_planted_brief_does_not_survive_a_partly_supplied_one(self, run_worker, monkeypatch):
        """The mixed case: a real assignment whose server block omitted the base
        revision, with a planted base revision already in the environment. The planted
        value must not silently complete the brief — an author would then amend against
        a version the server never named."""
        envelope = json.loads(json.dumps(AUTHORING_ENVELOPE))
        del envelope["orchestration"]["base_plan_version"]
        monkeypatch.setenv(AMENDMENT_BASE_VERSION_ENV, "999")

        run_worker(envelope, clear_assignment=False)

        assert_reached_the_export_block(envelope)
        assert os.environ.get(FLOW_ID_ENV) == FLOW_ID
        assert AMENDMENT_BASE_VERSION_ENV not in os.environ


class TestTheBriefSurvivesIntoTheAgentProcess:
    """The last link: exporting the brief into `os.environ` only matters if that is the
    environment the agent process actually gets.

    The chain is `_export_authoring_assignment` (line ~1463) → `agent_env =
    os.environ.copy()` (line ~2320) → `subprocess.run(command, env=agent_env)`, and on
    the Node side `workerAwsEnvironment()` spreads `process.env` and deletes only named
    AWS keys. So today the brief arrives, and the instructions committed for this issue
    are correct to tell an author to read these variables.

    Nothing pinned that, though, and two plausible future changes break it silently:
    turning `agent_env` into an allow-list, or widening a scrub to a prefix. Either
    leaves every other test in this file green — the export still happens, the variables
    are still in `os.environ` — while the author that reads them gets nothing. The
    failure is the original defect restored: a brief with a producer, a consumer, and no
    delivery.
    """

    def test_the_mediated_scrub_does_not_take_the_brief_with_it(self):
        """Mediation strips merge-capable credentials from the agent env, and an AIDLC
        author can be in that cohort. The scrub is named-credential only by design, so
        the brief must pass through it untouched — asserted here so a future widening to
        something prefix-based (`ADP_*`) fails rather than quietly unbriefing the author.
        """
        agent_env = {name: f"value-{name}" for name in ALL_ASSIGNMENT_ENV}
        agent_env.update({"GITHUB_TOKEN": "t", "GH_TOKEN": "t", "GIT_ASKPASS": "helper"})

        with patch.object(entrypoint, "_remove_token_file"):
            entrypoint._withhold_write_token(agent_env)

        # Positive control: if the scrub became a no-op this test would pass for the
        # wrong reason, so assert it did the job it exists to do.
        assert "GITHUB_TOKEN" not in agent_env, "the scrub under test did nothing"
        assert "GIT_ASKPASS" not in agent_env

        for name in ALL_ASSIGNMENT_ENV:
            assert agent_env.get(name) == f"value-{name}", f"the mediated scrub removed {name} from the author's brief"


def test_the_real_bootstrap_delivers_the_snapshot_to_the_agent_process(run_worker, monkeypatch):
    seen = []
    original = _subprocess_side_effect

    def capture(*args, **kwargs):
        command = args[0] if args else kwargs.get("args", [])
        if command == entrypoint.worker_command("aidlc"):
            path = kwargs["env"][AMENDMENT_BASE_PATH_ENV]
            seen.append(json.loads(Path(path).read_text()))
        return original(*args, **kwargs)

    monkeypatch.setattr(sys.modules[__name__], "_subprocess_side_effect", capture)
    assert run_worker(AUTHORING_ENVELOPE) == 0
    assert seen == [_BASE_DOCUMENT], "the actual agent subprocess must receive the accepted document"


def test_bad_snapshot_stops_bootstrap_before_an_author_runs(run_worker, monkeypatch):
    seen = []
    original = _subprocess_side_effect

    def capture(*args, **kwargs):
        command = args[0] if args else kwargs.get("args", [])
        if command == entrypoint.worker_command("aidlc"):
            seen.append(command)
        return original(*args, **kwargs)

    monkeypatch.setattr(sys.modules[__name__], "_subprocess_side_effect", capture)
    failure = MagicMock()
    monkeypatch.setattr(entrypoint, "_fail_bootstrap_status", failure)
    envelope = json.loads(json.dumps(AUTHORING_ENVELOPE))
    envelope["payload"]["amendment_base"]["author_run_id"] = "another-run"
    assert run_worker(envelope) == 1
    assert seen == []
    assert failure.call_args.args[2] == "authoring_input_binding_mismatch"
    assert AMENDMENT_BASE_PATH_ENV not in os.environ
