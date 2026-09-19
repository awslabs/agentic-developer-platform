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
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import entrypoint  # noqa: E402
from lib.engine_registration import AMENDMENT_REQUEST_ENV, FLOW_ID_ENV  # noqa: E402

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
        "base_plan_hash": "cafebabe",
    },
    "payload": {"replan_request": "replan: gate the deploy wave", "requested_by": "jane-dev"},
    "arrived_at": "2026-09-19T14:22:00Z",
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

    def _run(envelope: dict) -> int:
        for var in (FLOW_ID_ENV, AMENDMENT_REQUEST_ENV, "ADP_TENANT_ID"):
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
        which is the only authority for what this run was commissioned to do."""
        run_worker(AUTHORING_ENVELOPE)  # establishes the env, then:
        monkeypatch.setenv(FLOW_ID_ENV, "flow-victim")
        monkeypatch.setenv(AMENDMENT_REQUEST_ENV, "req-victim")
        run_worker(AUTHORING_ENVELOPE)
        assert_reached_the_export_block(AUTHORING_ENVELOPE)
        assert os.environ.get(FLOW_ID_ENV) == FLOW_ID
        assert os.environ.get(AMENDMENT_REQUEST_ENV) == REQUEST_ID
