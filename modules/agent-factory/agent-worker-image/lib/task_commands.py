"""Registered task-agent command allowlist — Task API T4 (#5797).

Maps each explicitly registered task persona to the built executable that
implements it. Design section 3 fixes the first entry:

    agent-task-investigator -> node /app/task-agents/investigator/dist/index.js --embedded

## Why an unknown task persona must fail rather than fall back

``worker_command`` in the entrypoint returns the Claude agent for any persona it
does not recognise as Codex. That default is correct on the legacy path — a new
GitHub persona is a prompt variation of the same agent — and wrong here, because
a task persona's content is caller-supplied and the Claude agent is provisioned
with a repository credential, a checkout and tool access. Falling back would hand
caller-controlled instructions to a far more capable runtime than the caller was
authorised for, and it would do so silently.

So this is an allowlist with no default: an unregistered task persona raises
:class:`UnknownTaskPersonaError` and the run fails visibly. "Fail clearly and
never fall back to Claude" is T4-AC02.

The child receives no credential in its argument vector or environment, so the
command is arguments only; the environment is built separately in
``lib.task_host`` and deliberately does not inherit this process's.
"""

from __future__ import annotations

import os

#: Registered persona -> argv. Adding a task agent is one entry here plus the
#: package and its Dockerfile stage; it never changes the queue contract, the
#: KEDA resources or the legacy command selection.
TASK_AGENT_COMMANDS: dict[str, tuple[str, ...]] = {
    "agent-task-codex-developer": ("node", "/app/task-agents/cyber/dist/index.js", "--embedded", "--codex"),
    "agent-task-claude-developer": ("node", "/app/task-agents/cyber/dist/index.js", "--embedded", "--developer"),
    "agent-task-cyber": ("node", "/app/task-agents/cyber/dist/index.js", "--embedded"),
    "agent-task-investigator": (
        "node",
        "/app/task-agents/investigator/dist/index.js",
        "--embedded",
    ),
}


# Packaged candidates require explicit host enablement after qualification.
# This setting is never inherited by the SDK or accepted from a Task envelope.
CODEX_TASK_COMMANDS: dict[str, tuple[str, ...]] = {
    key: ("node", "/app/codex-harness/dist/task-entry.mjs", "--embedded")
    for key in (
        "agent-task-gpt-developer", "agent-task-gpt-intent-refinement",
        "agent-task-gpt-architect", "agent-task-gpt-product", "agent-task-gpt-pm",
    )
}


def _enabled_codex_command(persona: str):
    enabled = {value.strip() for value in os.environ.get("ADP_CODEX_TASK_PERSONAS", "").split(",") if value.strip()}
    return CODEX_TASK_COMMANDS.get(persona) if persona in enabled else None


class UnknownTaskPersonaError(Exception):
    """This task persona has no registered executable.

    A terminal, non-retryable condition: redelivering the same assignment to
    another pod running the same image cannot resolve it. The caller fails the
    run with a clear reason rather than substituting a different agent.
    """


def task_agent_command(persona: str) -> list[str]:
    """Return the registered executable for ``persona``.

    :raises UnknownTaskPersonaError: the persona is not registered. There is no
        default and no fallback to a legacy runtime.
    """
    command = TASK_AGENT_COMMANDS.get(persona) or _enabled_codex_command(persona)
    if command is None:
        raise UnknownTaskPersonaError(f"task persona is not packaged in this image: {persona}")
    return list(command)


def is_registered_task_persona(persona: str) -> bool:
    """Whether ``persona`` has a registered task executable in this image."""
    return persona in TASK_AGENT_COMMANDS or _enabled_codex_command(persona) is not None
