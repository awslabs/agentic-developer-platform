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

# Personas also selected automatically by platform events. They use the same
# envelope and queue as every other persona; the worker entrypoint selects the
# packaged runtime from the persona name. Keeping this catalogue in the normal
# Lambda package also makes code-only rollout independent of worker
# infrastructure migration state.
AUTOMATIC_PERSONAS: set[str] = {"agent-codex-reviewer", "intent-refinement"}

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
    # Issue #5038 (EPIC #4910, U4): Superplane domain-pack personas. Their
    # prompt files live in modules/domain-apps/superplane/agent/personas/ and are
    # staged into the worker image by stage-personas.sh, same as the cyber pack's
    # malware-analysis-agent.
    #
    # Mention-triggered only, deliberately: neither has a LABEL_TO_PERSONA entry.
    # @agent-superplane-operator can allocate paid compute, and a label is a
    # weaker, more easily-applied-by-accident trigger than a mention (a stale
    # label on a reopened issue re-dispatches). Following the `codex` precedent
    # of restricting the trigger surface for a persona whose actions are costly.
    #
    # Placed before aidlc/codex to preserve the codex-last dict-order invariant.
    # Neither key is a substring of any other mention string in this dict, so
    # first-match routing cannot shadow them in either direction — asserted by
    # test_no_mention_string_shadows_another in
    # webhook-ingress/lambda/common/tests/test_persona_prompt_files.py.
    "@agent-superplane-operator": "superplane-operator",
    "@agent-superplane-researcher": "superplane-researcher",
    # Issue #3169: AIDLC inception persona. Mention-triggered only (no label
    # equivalent — the trigger path is issues.opened with aidlc-intent label
    # OR @agent-aidlc mention). Placed before codex to preserve the codex-last
    # dict-order invariant.
    "@agent-aidlc": "aidlc",
    # The Codex reviewer supports the standard human mention path in addition
    # to automatic eligible-PR events. Keep this before @agent-codex and use
    # token-aware parsing so the older supervisor name cannot shadow it.
    "@agent-codex-reviewer": "agent-codex-reviewer",
    # Issue #2706: codex supervisor persona. Mention-triggered only (the
    # platform standard); intentionally NOT in LABEL_TO_PERSONA. Placed last so
    # it cannot shadow an earlier persona under the first-match dict-order
    # routing in _extract_mention_persona().
    "@agent-codex": "codex",
}

# The canonical set of all valid personas — union of all mapping targets.
# Used by spawn_persona() to reject unknown persona values before any work.
VALID_PERSONAS: set[str] = (
    set(MENTION_TO_PERSONA.values())
    | set(LABEL_TO_PERSONA.values())
    | AUTOMATIC_PERSONAS
)

# Harness compatibility is persona metadata, not a gateway default.  Keep it
# beside the authoritative persona registry so an execution adapter cannot be
# silently classified as whichever harness the gateway happens to know best.
PERSONA_COMPATIBILITY_CLASS: dict[str, str] = {
    "agent-codex-reviewer": "codex-sdk",
    "aidlc": "claude-agent-sdk",
    "architect": "claude-agent-sdk",
    "codex": "claude-agent-sdk",
    "developer": "claude-agent-sdk",
    "intent-refinement": "claude-agent-sdk",
    "malware-analysis-agent": "claude-agent-sdk",
    "operations": "claude-agent-sdk",
    "pm": "claude-agent-sdk",
    "product": "claude-agent-sdk",
    "pt-superpower": "claude-agent-sdk",
    "reviewer": "claude-agent-sdk",
    "superplane-operator": "claude-agent-sdk",
    "superplane-researcher": "claude-agent-sdk",
}


# Task-only shared Codex candidates. These are intentionally excluded from
# VALID_PERSONAS and mention/automatic maps: the legacy spawn path must never
# dispatch a Task candidate. A gateway-owned catalogue, service/model policy and
# explicit worker enablement are required before execution.
TASK_PERSONA_COMPATIBILITY_CLASS: dict[str, str] = {
    "agent-task-gpt-developer": "codex-sdk",
    "agent-task-gpt-intent-refinement": "codex-sdk",
}
