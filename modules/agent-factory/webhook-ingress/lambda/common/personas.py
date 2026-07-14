"""Single source of truth for persona constants.

Issue #2151: Extracted from intent_parser.py so that spawn_persona() and all
trigger adapters validate against the same set — no drift possible.
"""

from __future__ import annotations

# Label names that trigger specific agent personas.
LABEL_TO_PERSONA: dict[str, str] = {
    "developer": "developer",
    "pm": "pm",
    "agent-operations": "operations",
    "agent-reviewer": "reviewer",
    "agent-architect": "architect",
    "malware-analysis-agent": "malware-analysis-agent",
    "superpower": "pt-superpower",
}

# @-mention patterns in issue/PR comments that trigger personas.
MENTION_TO_PERSONA: dict[str, str] = {
    "@agent-developer": "developer",
    "@agent-pm": "pm",
    "@agent-operations": "operations",
    "@agent-reviewer": "reviewer",
    "@agent-architect": "architect",
    "@agent-product": "product",
    "@agent-malware-analysis-agent": "malware-analysis-agent",
    "@agent-superpower": "pt-superpower",
    # Issue #3169: AIDLC inception persona. Mention-triggered only (no label
    # equivalent — the trigger path is issues.opened with aidlc-intent label
    # OR @agent-aidlc mention). Placed before codex to preserve the codex-last
    # dict-order invariant.
    "@agent-aidlc": "aidlc",
    # Issue #2706: codex supervisor persona. Mention-triggered only (the
    # platform standard); intentionally NOT in LABEL_TO_PERSONA. Placed last so
    # it cannot shadow an earlier persona under the first-match dict-order
    # routing in _extract_mention_persona().
    "@agent-codex": "codex",
}

# The canonical set of all valid personas — union of all mapping targets.
# Used by spawn_persona() to reject unknown persona values before any work.
VALID_PERSONAS: set[str] = set(MENTION_TO_PERSONA.values()) | set(
    LABEL_TO_PERSONA.values()
)
