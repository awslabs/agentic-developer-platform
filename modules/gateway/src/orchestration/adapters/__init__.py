"""Input adapters — the ways a human decision reaches the engine (Issue #4209).

An *input adapter* is a named, first-class path by which a human answer to a gate
becomes an engine-side decision record. There is deliberately more than one, and
none of them is a legacy fallback:

- the **dashboard** approve action (the graph UI), and
- a **GitHub comment** on the issue that carries the gate.

Both produce the *same* decision shape through the *same* permission check and the
*same* guarded transition. The only thing that differs is the recorded input path,
which exists so an operator can answer "where did this approval come from?" — not
so the engine can treat one source as second-class.

Why this package exists at all: the GitHub-driven flow predates the engine, so
without a declared adapter it would remain "the old way that happens to still
work" — the exact framing that lets a later refactor quietly drop it. Naming it as
an adapter makes it a supported surface with tests attached (ruling D-R20: legacy
mode is a product feature, not a migration state).
"""

from .github_comments import (
    GateAnswer,
    GateAnswerOutcome,
    GateAnswerStatus,
    GateDecisionRecord,
    InputPath,
    apply_gate_answer,
    build_gate_decision,
)

__all__ = [
    "GateAnswer",
    "GateAnswerOutcome",
    "GateAnswerStatus",
    "GateDecisionRecord",
    "InputPath",
    "apply_gate_answer",
    "build_gate_decision",
]
