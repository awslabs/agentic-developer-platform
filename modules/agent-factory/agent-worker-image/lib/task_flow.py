"""Task-only bootstrap called by the shared Python entrypoint."""

from __future__ import annotations

from lib.task_host import TaskHost


def run_task_assignment(assignment, envelope: dict, *, heartbeat, acknowledge) -> int:
    return TaskHost().run(
        assignment,
        envelope,
        heartbeat=heartbeat,
        acknowledge=acknowledge,
    )
