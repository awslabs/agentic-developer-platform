# Agent Memory System

Agents persist and retrieve context across runs using the `adp` orphan branch. This branch exists in every target repo, never merges to main, and requires no setup — it is auto-created on first agent run.

## How It Works

The `adp` branch contains only `agent_context/`:

```
adp branch (orphan)
└── agent_context/
    ├── components/<name>/          # What happened with each system component
    │   └── issue-<N>_<timestamp>.md
    └── agents/<persona>/           # What each agent did recently
        └── run_issue-<N>_<timestamp>.md
```

- **Component records**: Detailed metadata + learnings for a specific system component
- **Agent run summaries**: Lightweight cross-references from each agent persona's perspective

## Before Starting Work

1. The memory module auto-loads context from the `adp` branch
2. Review component history for the system you're working on
3. Review your agent persona's recent runs for patterns
4. If previous runs mention specific errors or workarounds, apply them

## Before Finishing Work

1. The memory module auto-saves your records to the `adp` branch
2. Component record: structured metadata + learnings (detailed)
3. Agent run summary: lightweight cross-reference (brief)
4. If the run failed, still write what you learned — partial context is better than none

## Component Record Template

```markdown
# Component Record: <component-name>

## Metadata
- **Issue**: #<number> — <title>
- **Agent**: @agent-<type>
- **Date**: <ISO timestamp>
- **Status**: success | partial | failed
- **PR**: #<number> (if applicable)

## Summary
<What was done, in 2-3 sentences>

## Learnings
- <Specific insight 1 — include exact error messages, not vague descriptions>
- <Specific insight 2>
- <Workaround or pattern that worked>

## Errors Encountered
- <Exact error message and how it was resolved>
```

## Agent Run Summary Template

```markdown
# Agent Run: @agent-<type>

- **Issue**: #<number> — <title>
- **Date**: <ISO timestamp>
- **Component**: <component-name>
- **Status**: success | partial | failed

## Summary
<One sentence describing what this agent did>

See `agent_context/components/<component>/issue-<number>_*.md` for details.
```

## Rules

- Be specific in learnings — exact error messages, not vague descriptions
- Component folders are created automatically on first write
- Agent persona folders are created automatically on first write
- Files are named: `issue-<number>_<YYYY-MM-DD>T<HH-MM>.md` for components, `run_issue-<number>_<YYYY-MM-DD>T<HH-MM>.md` for agents
- Only the most recent 5 files per folder are loaded (to keep context manageable)
- Memory is advisory, not blocking — if read/write fails, the agent continues without it
- Concurrent writes are handled with `git pull --rebase` before push (retry once)

## Component Detection

Components are detected from issue labels or body:
- **Label-based** (preferred): `component:<name>` label on the issue
- **Body-based**: `Component: <name>` line in the issue body
- **Fallback**: `general` if no component is detected
