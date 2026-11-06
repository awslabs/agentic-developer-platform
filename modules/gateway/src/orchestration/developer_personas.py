"""Developer identities supported throughout engine delivery and recovery."""

import os

DEVELOPER_PERSONAS = frozenset({"developer", "agent-codex-developer"})
OPERATIONS_PERSONAS = frozenset({"operations", "agent-codex-operations"})


def operations_executor_enabled():
    return os.getenv("ADP_ENGINE_OPERATIONS_EXECUTOR", "").lower() in {"1", "true"}
