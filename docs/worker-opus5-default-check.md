# Worker Opus 5 Default Check

**Date**: 2026-07-28

**ANTHROPIC_MODEL** (raw value, verbatim from the pod environment):

```
global.anthropic.claude-opus-5
```

This file verifies that the hosted-agent (worker) default model is
`global.anthropic.claude-opus-5` after PR #3910.

No `/model` directive was passed on the dispatch for issue #3911 — the value above
is the pod default, read with `printenv ANTHROPIC_MODEL` inside the agent-runtime
container during the run that produced this file.

That this run completed normally — reasoning, tool calls, git, and the pull request —
is itself the end-to-end evidence that Opus 5 drives the full agent loop.
