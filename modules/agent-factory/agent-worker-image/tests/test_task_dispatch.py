"""Task/legacy dispatch recognition and command routing — Task API T4 (#5797).

Covers T4-AC02 (unknown/malformed/stale task assignments cannot invoke a legacy
persona or use another run grant) at the recognition layer.

Every positive and negative case is driven by T0's published fixtures
(``docs/task-api/contracts/v1/fixtures/``) **unchanged**: the fixture corpus is
the frozen contract, and a test that edited a fixture to make itself pass would
be asserting against something the contract does not say. The corpus is loaded by
path and the ``$fixture`` metadata block is stripped, which is the only
transformation applied.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.task_commands import (
    TASK_AGENT_COMMANDS,
    UnknownTaskPersonaError,
    is_registered_task_persona,
    task_agent_command,
)
from lib.task_dispatch import (
    TASK_ENVELOPE_KIND,
    TaskDispatchError,
    is_task_envelope,
    is_task_persona,
    parse_task_envelope,
    reject_task_persona_on_legacy_path,
)

_REPO_ROOT = Path(__file__).resolve().parents[4]
_FIXTURES = _REPO_ROOT / "docs" / "task-api" / "contracts" / "v1" / "fixtures"


def _fixture(relative: str) -> dict:
    """Load a published contract fixture, stripping only its metadata block."""
    body = json.loads((_FIXTURES / relative).read_text())
    body.pop("$fixture", None)
    return body


@pytest.fixture
def task_envelope() -> dict:
    return _fixture("valid/envelope-dispatch.json")


@pytest.fixture
def legacy_envelope() -> dict:
    return _fixture("legacy/github-issue-comment-envelope.json")


# --- The published task envelope is recognised and parsed ---------------------


def test_published_task_envelope_is_recognised(task_envelope):
    assert is_task_envelope(task_envelope)


def test_published_task_envelope_yields_its_fixed_run_identity(task_envelope):
    assignment = parse_task_envelope(task_envelope)

    assert assignment.task_id == task_envelope["task_id"]
    assert assignment.invocation_id == task_envelope["invocation_id"]
    assert assignment.persona == "agent-task-investigator"
    assert assignment.dispatch_id == task_envelope["dispatch_id"]
    assert assignment.request_digest == task_envelope["request_digest"]
    assert assignment.generation == task_envelope["assignment_ref"]["generation"]
    assert assignment.grant_pk == task_envelope["assignment_ref"]["grant_pk"]
    assert assignment.grant_sk == task_envelope["assignment_ref"]["grant_sk"]
    assert assignment.input_digest == task_envelope["input_ref"]["input_digest"]
    assert len(assignment.artifact_refs) == 1


def test_message_id_is_the_invocation_not_a_transport_identifier(task_envelope):
    """One run handle. A redelivery has a new transport id but the same run."""
    assignment = parse_task_envelope(task_envelope)
    assert assignment.message_id == assignment.invocation_id


def test_assignment_is_immutable(task_envelope):
    """A mutable identity is how a superseded generation presents itself as live."""
    assignment = parse_task_envelope(task_envelope)
    with pytest.raises(Exception):
        assignment.generation = 99  # type: ignore[misc]


# --- Legacy messages are never matched by the task branch --------------------


@pytest.mark.parametrize(
    "fixture_name",
    [
        "legacy/github-issue-comment-envelope.json",
        "legacy/github-label-envelope-with-model-directive.json",
        "legacy/slack-channel-envelope.json",
    ],
)
def test_published_legacy_envelopes_are_not_task_envelopes(fixture_name):
    assert not is_task_envelope(_fixture(fixture_name))


def test_legacy_envelope_has_no_task_persona(legacy_envelope):
    """The legacy path's own guard must not fire on an ordinary legacy message."""
    reject_task_persona_on_legacy_path(legacy_envelope)


# --- Ambiguity is refused in both directions ---------------------------------


def test_legacy_persona_inside_task_kind_is_refused():
    """A GitHub persona claiming the task branch, with routing attached.

    Routing under source_ref would send the task branch looking for a repository;
    the persona would send it to a legacy runtime. Refusing is the only safe
    answer.
    """
    body = _fixture("invalid/envelope-legacy-persona-in-task-kind.json")
    with pytest.raises(TaskDispatchError):
        parse_task_envelope(body)


def test_legacy_envelope_claiming_task_kind_is_refused_on_the_legacy_path():
    """An envelope satisfying both descriptions would be routed nondeterministically."""
    body = _fixture("invalid/legacy-envelope-claiming-task-kind.json")
    # It claims the discriminator, so the task branch sees it first...
    assert is_task_envelope(body)
    # ...and refuses it, rather than proceeding on a body carrying GitHub routing.
    with pytest.raises(TaskDispatchError):
        parse_task_envelope(body)


def test_task_persona_on_the_legacy_path_is_refused():
    """A task persona has no repository and no repository grant."""
    body = _fixture("invalid/legacy-envelope-task-persona.json")
    assert not is_task_envelope(body)
    with pytest.raises(TaskDispatchError):
        reject_task_persona_on_legacy_path(body)


# --- Malformed and stale assignments ------------------------------------------


def test_generation_disagreeing_with_its_authority_key_is_refused():
    """The conditional write is checked against the key, not the number.

    If the two could differ, a superseded worker could present a stale key beside
    a current generation number and be admitted as the live attempt.
    """
    body = _fixture("invalid/envelope-generation-mismatch.json")
    with pytest.raises(TaskDispatchError):
        parse_task_envelope(body)


def test_envelope_carrying_a_credential_is_refused():
    """The queue is durable; a credential here would outlive the run."""
    body = _fixture("invalid/envelope-carries-credential.json")
    with pytest.raises(TaskDispatchError):
        parse_task_envelope(body)


def test_message_id_that_is_not_the_invocation_is_refused():
    body = _fixture("invalid/envelope-message-id-not-invocation.json")
    with pytest.raises(TaskDispatchError):
        parse_task_envelope(body)


def test_transport_identifiers_in_the_envelope_are_refused():
    """A transport identifier must never become a run handle."""
    body = _fixture("invalid/envelope-transport-id-as-run-id.json")
    with pytest.raises(TaskDispatchError):
        parse_task_envelope(body)


@pytest.mark.parametrize("missing", ["task_id", "invocation_id", "dispatch_id", "assignment_ref"])
def test_missing_required_field_is_refused(task_envelope, missing):
    task_envelope.pop(missing)
    with pytest.raises(TaskDispatchError):
        parse_task_envelope(task_envelope)


def test_unsupported_schema_version_is_refused(task_envelope):
    task_envelope["schema_version"] = "2.0"
    with pytest.raises(TaskDispatchError):
        parse_task_envelope(task_envelope)


def test_unknown_field_is_refused(task_envelope):
    """Duplicate/unknown fields are rejected, so an extra field is not ignored."""
    task_envelope["extra_directive"] = "run-anyway"
    with pytest.raises(TaskDispatchError):
        parse_task_envelope(task_envelope)


def test_assignment_key_naming_another_invocation_is_refused(task_envelope):
    """The grant key names the run it binds; another run's grant is not usable."""
    task_envelope["assignment_ref"]["grant_sk"] = (
        "TASK_RUN#11111111-2222-4333-8444-555555555555#GEN#0000000001"
    )
    with pytest.raises(TaskDispatchError):
        parse_task_envelope(task_envelope)


def test_non_object_body_is_refused():
    for body in ("not json", [], 7, None):
        with pytest.raises(TaskDispatchError):
            parse_task_envelope(body)


def test_artifact_version_true_is_not_version_one(task_envelope):
    """bool is an int subclass; True must not pass as a pinned version."""
    task_envelope["input_ref"]["artifact_refs"][0]["version"] = True
    with pytest.raises(TaskDispatchError):
        parse_task_envelope(task_envelope)


def test_refusal_reason_does_not_echo_envelope_content(task_envelope):
    """A diagnostic must not become an echo of a caller-supplied body."""
    task_envelope["input_ref"]["input_digest"] = "sentinel-not-a-digest-value"
    with pytest.raises(TaskDispatchError) as raised:
        parse_task_envelope(task_envelope)
    assert "sentinel-not-a-digest-value" not in str(raised.value)


# --- Command routing ----------------------------------------------------------


def test_registered_persona_resolves_to_its_packaged_executable():
    """The mapping T5 (#5798) documents for its built package."""
    assert task_agent_command("agent-task-investigator") == [
        "node",
        "/app/task-agents/investigator/dist/index.js",
        "--embedded",
    ]


def test_unknown_task_persona_raises_and_never_returns_a_legacy_runtime():
    """T4-AC02: no fallback to Claude or Codex for an unregistered task persona."""
    with pytest.raises(UnknownTaskPersonaError):
        task_agent_command("agent-task-unregistered")


def test_no_registered_task_command_points_at_a_legacy_runtime():
    """A registration mistake that aimed a task persona at Claude is a security bug."""
    for command in TASK_AGENT_COMMANDS.values():
        joined = " ".join(command)
        assert "agent-worker.js" not in joined
        assert "codex-reviewer" not in joined
        assert "/app/task-agents/" in joined


def test_no_registered_task_command_carries_a_credential_argument():
    """The child receives no credential in its argument vector."""
    for command in TASK_AGENT_COMMANDS.values():
        joined = " ".join(command).lower()
        for forbidden in ("token", "secret", "key", "credential", "password"):
            assert forbidden not in joined


def test_registered_personas_are_task_personas():
    for persona in TASK_AGENT_COMMANDS:
        assert is_task_persona(persona)
        assert is_registered_task_persona(persona)


def test_task_persona_prefix_excludes_legacy_personas():
    for persona in ("agent-developer", "agent-reviewer", "aidlc", "agent-codex-reviewer"):
        assert not is_task_persona(persona)


def test_discriminator_constant_matches_the_published_envelope(task_envelope):
    assert task_envelope["kind"] == TASK_ENVELOPE_KIND
