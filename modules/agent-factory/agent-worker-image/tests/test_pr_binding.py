"""Unit tests for lib/pr_binding.py.

Issue #5301, the worker half of the story-to-PR binding. The module's whole reason
for existing is that a story used to wait forever on evidence that never arrives, so
these tests are organised around the two properties that keep it from reintroducing
that failure in a new place:

1. **Fail-soft, like `engine_registration`.** By the time this runs the branch is
   pushed and the PR is open, so no failure mode may raise into `entrypoint.py`,
   which deliberately has no error handling here. Every reachable failure (refusal,
   unreachable gateway, `gh` non-zero exit, malformed JSON, missing identity field,
   authority disabled) is asserted to return a *string*.

2. **Visible, not silent.** A failure means the story will hold rather than
   complete, so the returned note must say so. A silently-swallowed failure would
   reproduce the original bug's worst property — a story waiting with no stated
   reason — which is exactly what this issue exists to remove.

The third group covers what the module must *not* send: no node, flow, tenant or run
identifier. The gateway derives the story from the run credential, and a payload
carrying a story reference would be a way for a worker to bind its PR to a story it
was never dispatched for. That is asserted on the payload itself rather than trusted
to the server, because it is the worker's half of the contract.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lib.pr_binding import (  # noqa: E402
    BINDING_REQUIRED_ENV,
    binding_note,
    binding_required,
    register_pull_request,
)
from lib.status_gateway_client import StatusGatewayError  # noqa: E402

REPO = "aws-e/adp"
PR_NUMBER = 5293
HEAD_SHA = "6c7370387d5d57a6ff9ebb5a567f0744e7d99d0e"
PR_NODE_ID = "PR_kwDOABCD12345"
REPOSITORY_ID = 987654321


def _gh_view(**overrides) -> str:
    payload = {"id": PR_NODE_ID, "number": PR_NUMBER, "headRefOid": HEAD_SHA}
    payload.update(overrides)
    return json.dumps(payload)


def _fake_gh(view_stdout: str = "", repo_id_stdout: str = str(REPOSITORY_ID)):
    """Stub `subprocess.run` for the two `gh` calls the module makes, in order."""

    def runner(cmd, **kwargs):
        if cmd[:2] == ["gh", "pr"]:
            return MagicMock(stdout=view_stdout or _gh_view(), returncode=0)
        return MagicMock(stdout=repo_id_stdout, returncode=0)

    return runner


@pytest.fixture(autouse=True)
def _binding_env(monkeypatch):
    """Default every test to "the engine asked for a binding, authority is on"."""
    monkeypatch.setenv(BINDING_REQUIRED_ENV, "true")
    monkeypatch.setattr("lib.pr_binding.authority_enabled", lambda: True)


class TestBindingRequired:
    """The marker gates everything: an unmarked run must behave exactly as before."""

    def test_absent_env_is_false(self, monkeypatch):
        monkeypatch.delenv(BINDING_REQUIRED_ENV, raising=False)
        assert binding_required() is False

    @pytest.mark.parametrize("value", ["true", "TRUE", " true ", "1", "yes", "on"])
    def test_true_spellings(self, monkeypatch, value):
        monkeypatch.setenv(BINDING_REQUIRED_ENV, value)
        assert binding_required() is True

    @pytest.mark.parametrize("value", ["false", "0", "no", "", "maybe"])
    def test_other_values_are_false(self, monkeypatch, value):
        """Anything unrecognised must read as *off*, never as on."""
        monkeypatch.setenv(BINDING_REQUIRED_ENV, value)
        assert binding_required() is False

    def test_unmarked_run_registers_nothing(self, monkeypatch):
        """A webhook trigger or legacy dispatch sends no request and appends no note."""
        monkeypatch.delenv(BINDING_REQUIRED_ENV, raising=False)
        with patch("lib.pr_binding.post_self") as post:
            assert binding_note(repo=REPO, pr_number=PR_NUMBER) == ""
        post.assert_not_called()


class TestPayload:
    """What is sent — and what must never be."""

    def test_sends_immutable_provider_identity(self):
        with (
            patch("lib.pr_binding.subprocess.run", side_effect=_fake_gh()),
            patch("lib.pr_binding.post_self", return_value={"created": True}) as post,
        ):
            register_pull_request(repo=REPO, pr_number=PR_NUMBER)
        path, payload = post.call_args[0]
        assert path == "/pull-request"
        assert payload["provider_repository_id"] == REPOSITORY_ID
        assert payload["provider_pr_node_id"] == PR_NODE_ID
        assert payload["head_sha"] == HEAD_SHA
        assert payload["repo"] == REPO
        assert payload["pr_number"] == PR_NUMBER

    def test_sends_no_story_or_run_reference(self):
        """The gateway derives the story from the run credential.

        A story/flow/tenant/run field in this payload would be a lookup assertion the
        caller controls, which is precisely the weaker trust model this route rejects.
        """
        with (
            patch("lib.pr_binding.subprocess.run", side_effect=_fake_gh()),
            patch("lib.pr_binding.post_self", return_value={"created": True}) as post,
        ):
            register_pull_request(repo=REPO, pr_number=PR_NUMBER)
        payload = post.call_args[0][1]
        forbidden = {"node_id", "flow_id", "org_id", "tenant_id", "run_id", "attempt", "issue"}
        assert forbidden.isdisjoint(payload.keys())

    def test_reviewer_artifact_flag_only_when_asked(self):
        with (
            patch("lib.pr_binding.subprocess.run", side_effect=_fake_gh()),
            patch("lib.pr_binding.post_self", return_value={"created": True}) as post,
        ):
            register_pull_request(repo=REPO, pr_number=PR_NUMBER)
            assert "reviewer_artifact" not in post.call_args[0][1]
            register_pull_request(repo=REPO, pr_number=PR_NUMBER, reviewer_artifact=True)
            assert post.call_args[0][1]["reviewer_artifact"] is True

    def test_incomplete_identity_is_refused_locally(self):
        """A binding keyed on a mutable name is one a rename can re-point."""
        with patch("lib.pr_binding.subprocess.run", side_effect=_fake_gh(view_stdout=_gh_view(id=""))):
            with pytest.raises(StatusGatewayError):
                register_pull_request(repo=REPO, pr_number=PR_NUMBER)

    def test_missing_repository_id_is_refused_locally(self):
        with patch("lib.pr_binding.subprocess.run", side_effect=_fake_gh(repo_id_stdout="not-a-number")):
            with pytest.raises(StatusGatewayError):
                register_pull_request(repo=REPO, pr_number=PR_NUMBER)


class TestFailSoft:
    """No reachable failure may raise: the run's real output is already delivered."""

    def test_gateway_refusal_returns_visible_note(self):
        with (
            patch("lib.pr_binding.subprocess.run", side_effect=_fake_gh()),
            patch("lib.pr_binding.post_self", side_effect=StatusGatewayError("already bound elsewhere")),
        ):
            note = binding_note(repo=REPO, pr_number=PR_NUMBER)
        assert isinstance(note, str)
        assert "binding failed" in note.lower()
        assert "hold" in note.lower()

    def test_gh_nonzero_exit_returns_visible_note(self):
        error = subprocess.CalledProcessError(1, ["gh"], stderr="not found")
        with patch("lib.pr_binding.subprocess.run", side_effect=error):
            note = binding_note(repo=REPO, pr_number=PR_NUMBER)
        assert isinstance(note, str)
        assert "binding failed" in note.lower()

    def test_gh_timeout_returns_visible_note(self):
        with patch("lib.pr_binding.subprocess.run", side_effect=subprocess.TimeoutExpired(["gh"], 30)):
            note = binding_note(repo=REPO, pr_number=PR_NUMBER)
        assert isinstance(note, str)
        assert "binding failed" in note.lower()

    def test_malformed_gh_json_returns_visible_note(self):
        with patch("lib.pr_binding.subprocess.run", side_effect=_fake_gh(view_stdout="{not json")):
            note = binding_note(repo=REPO, pr_number=PR_NUMBER)
        assert isinstance(note, str)
        assert "binding failed" in note.lower()

    def test_authority_disabled_says_story_will_wait(self, monkeypatch):
        """Without the gateway path there is no way to authenticate a binding."""
        monkeypatch.setattr("lib.pr_binding.authority_enabled", lambda: False)
        with patch("lib.pr_binding.post_self") as post:
            note = binding_note(repo=REPO, pr_number=PR_NUMBER)
        post.assert_not_called()
        assert "wait" in note.lower()

    def test_no_pull_request_appends_nothing(self):
        """`transcript_only` and the no-PR path have nothing to bind."""
        for value in ("", None, 0):
            with patch("lib.pr_binding.post_self") as post:
                assert binding_note(repo=REPO, pr_number=value) == ""
            post.assert_not_called()

    def test_unparseable_pr_number_appends_nothing(self):
        with patch("lib.pr_binding.post_self") as post:
            assert binding_note(repo=REPO, pr_number="not-a-number") == ""
        post.assert_not_called()


class TestSuccessNote:
    """The note distinguishes a fresh binding from an idempotent retry."""

    def test_created_reports_registration(self):
        with (
            patch("lib.pr_binding.subprocess.run", side_effect=_fake_gh()),
            patch("lib.pr_binding.post_self", return_value={"created": True, "role": "implementation"}),
        ):
            note = binding_note(repo=REPO, pr_number=PR_NUMBER)
        assert f"#{PR_NUMBER}" in note
        assert "registered" in note.lower()
        assert "failed" not in note.lower()

    def test_already_bound_is_not_reported_as_failure(self):
        """A tick restart re-registers the same PR; that is convergence, not an error."""
        with (
            patch("lib.pr_binding.subprocess.run", side_effect=_fake_gh()),
            patch("lib.pr_binding.post_self", return_value={"created": False, "role": "implementation"}),
        ):
            note = binding_note(repo=REPO, pr_number=PR_NUMBER)
        assert "already registered" in note.lower()
        assert "failed" not in note.lower()
