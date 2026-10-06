# Agent Coding Guidelines

Behavioral guidelines to reduce common LLM coding mistakes. These apply to every agent writing, modifying, or reviewing code, including Claude and native Codex workers. The platform authority and the assigned role's completion contract still govern the run.

Adapted from https://github.com/forrestchang/andrej-karpathy-skills/blob/main/CLAUDE.md.

Use judgment proportional to the task. Routine implementation choices do not require user approval.

## 1. Think Before Coding

Don't assume. Don't hide confusion. Surface tradeoffs.

Before implementing:
- State material assumptions explicitly. Resolve uncertainty from the available code and task context first.
- If multiple interpretations materially change the required outcome, surface the tradeoff.
- If a simpler approach exists, say so. Push back when warranted.
- Ask for clarification only when missing information blocks a correct implementation; continue independent work while waiting.

## 2. Simplicity First

Minimum code that solves the problem. Nothing speculative.

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

## 3. Surgical Changes

Touch only what you must. Clean up only your own mess.

When editing existing code:
- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it — don't delete it.

When your changes create orphans:
- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

**The test**: every changed line should trace directly to the user's request.

## 4. Goal-Driven Execution

Define success criteria from the assigned acceptance criteria. Verify the required outcome, then follow the role's handoff/completion contract; do not keep expanding scope.

Choose focused checks proportional to the change. Add regression tests for meaningful behavior or bugs; do not add tests that only mirror implementation, or repeatedly run broad suites without a new reason.

Transform tasks into verifiable goals:
- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

For multi-step tasks, state a brief plan:
```
1. [Step] → verify: [check]
2. [Step] → verify: [check]
```

Strong success criteria let you loop independently. Weak criteria ("make it work") require constant clarification.

---

**These guidelines are working if**: fewer unnecessary changes in diffs, fewer rewrites due to overcomplication, clarifying questions come before implementation rather than after mistakes.
