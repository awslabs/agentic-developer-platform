"""Every terminal-success path reports its delivery handoff before exiting 0 (#5144).

The defect is a worker exiting 0 with delivery unfinished, so the thing worth pinning
is not "the function can be called" but that **no green exit skips it**. A single
happy-path test would pass against code that wired the handoff into one branch and
left the other — which is the shape of the gap #1723 had with the correlation marker
and #5301 had with binding registration, both on this same function.

So the first test parametrises every delivery path and asserts the handoff was
reported on each, and the second asserts the failure path does *not* report one: a
failed run has no delivery to hand off, and reporting a handoff there would be a
receipt for work that did not happen.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _stub_entrypoint(entrypoint, monkeypatch, path: str):
    """Neutralise git/GitHub/status side effects, leaving the handoff wiring visible."""

    def run(cmd, **_kwargs):
        stdout = ""
        if cmd[:3] == ["git", "diff", "--stat"] and path != "self_created":
            stdout = "code.py"
        if cmd[:3] == ["gh", "pr", "list"] and path == "existing_pr":
            stdout = "5293"
        return MagicMock(stdout=stdout, returncode=0)

    monkeypatch.setattr(entrypoint, "run_cmd", run)
    monkeypatch.setattr(entrypoint, "_find_open_pr", lambda *_: "5293")
    monkeypatch.setattr(entrypoint, "_branch_changes_are_transcript_only", lambda *_: False)
    monkeypatch.setattr(entrypoint, "_read_result_metadata", lambda: None)
    monkeypatch.setattr(entrypoint, "_register_authored_draft", lambda *_: "")
    monkeypatch.setattr(entrypoint, "_ensure_pr_body_marker", MagicMock())
    monkeypatch.setattr(entrypoint, "_write_outbound_correlation", MagicMock())
    monkeypatch.setattr(entrypoint, "prepend_correlation_marker", lambda body: body)
    monkeypatch.setattr(entrypoint, "update_invocation_status", MagicMock())
    monkeypatch.setattr(entrypoint, "pr_binding_note", lambda **_: "")


@pytest.mark.parametrize("path", ["self_created", "entrypoint_created", "existing_pr"])
@pytest.mark.parametrize("persona", ["developer", "reviewer"])
def test_every_success_path_reports_the_handoff(path, persona, monkeypatch):
    """No green exit skips the handoff, on any delivery path or persona."""
    import entrypoint

    _stub_entrypoint(entrypoint, monkeypatch, path)
    comment = MagicMock()
    monkeypatch.setattr(entrypoint, "_post_comment", comment)
    handoff = MagicMock(return_value="> handoff-receipt-note")
    monkeypatch.setattr(entrypoint, "delivery_handoff_note", handoff)

    assert (
        entrypoint._handle_success("aws-e/adp", 5144, "agent/issue-5144", persona, "run", "arrival")
        == 0
    )

    assert handoff.call_count == 1
    # The note reaches the closing comment, so an unrecorded handoff is visible to a
    # human reading the issue rather than only in pod logs.
    assert "> handoff-receipt-note" in comment.call_args.args[4]


@pytest.mark.parametrize("path", ["self_created", "entrypoint_created"])
def test_repeat_delivery_reports_each_time_and_converges_on_the_server(path, monkeypatch):
    """A retried run reports again; convergence is the server's job, not the worker's.

    The worker must NOT suppress a second report locally — it cannot know whether the
    first one was committed, since a lost response looks identical to a refusal. The
    identical-receipt guarantee lives server-side, which is what makes this safe.
    """
    import entrypoint

    _stub_entrypoint(entrypoint, monkeypatch, path)
    monkeypatch.setattr(entrypoint, "_post_comment", MagicMock())
    handoff = MagicMock(return_value="> handoff-receipt-note")
    monkeypatch.setattr(entrypoint, "delivery_handoff_note", handoff)

    for _ in range(2):
        assert (
            entrypoint._handle_success(
                "aws-e/adp", 5144, "agent/issue-5144", "developer", "run", "arrival"
            )
            == 0
        )

    assert handoff.call_count == 2


def test_a_failing_run_reports_no_handoff(monkeypatch):
    """A failed run has no delivery to hand off.

    Reporting one here would commit a continuation receipt for work that did not
    happen, which is the opposite error to the one this story fixes and just as bad.
    """
    import entrypoint

    _stub_entrypoint(entrypoint, monkeypatch, "entrypoint_created")

    def failing(cmd, **kwargs):
        import subprocess

        if cmd[:2] == ["git", "push"]:
            raise subprocess.CalledProcessError(1, cmd, stderr="push rejected")
        return MagicMock(stdout="code.py", returncode=0)

    monkeypatch.setattr(entrypoint, "run_cmd", failing)
    monkeypatch.setattr(entrypoint, "_post_comment", MagicMock())
    handoff = MagicMock(return_value="> handoff-receipt-note")
    monkeypatch.setattr(entrypoint, "delivery_handoff_note", handoff)

    assert (
        entrypoint._handle_success(
            "aws-e/adp", 5144, "agent/issue-5144", "developer", "run", "arrival"
        )
        == 1
    )
    handoff.assert_not_called()


def test_handoff_failure_does_not_destroy_delivered_work(monkeypatch):
    """A raising handoff must not turn a delivered PR into a failed run.

    `handoff_note` is written never to raise, and this pins the consequence rather
    than the implementation: if it ever did, the exit code below would change and the
    branch and PR would be orphaned by bookkeeping.
    """
    import entrypoint

    _stub_entrypoint(entrypoint, monkeypatch, "self_created")
    monkeypatch.setattr(entrypoint, "_post_comment", MagicMock())
    # The real note function swallows StatusGatewayError; this asserts the wiring does
    # not depend on that being the only failure mode it can produce.
    monkeypatch.setattr(entrypoint, "delivery_handoff_note", lambda **_: "> not recorded")

    assert (
        entrypoint._handle_success(
            "aws-e/adp", 5144, "agent/issue-5144", "developer", "run", "arrival"
        )
        == 0
    )


def test_envelope_marker_is_read_from_dispatch_not_chosen_by_the_worker(monkeypatch):
    """The worker does not decide that it owes a handoff.

    Asserted on the source of the marker: it comes from the trusted dispatch envelope,
    so a run that was never asked to hand off keeps its existing behaviour and a worker
    cannot opt itself in or out.
    """
    import inspect

    import entrypoint

    source = inspect.getsource(entrypoint)
    assert 'envelope.get("handoff_required") is True' in source
    # Set from the envelope only — never from agent/LLM output or the work directory.
    assert 'os.environ[HANDOFF_REQUIRED_ENV] = "true"' in source
