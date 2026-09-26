# Task model qualification

The dedicated `persona-model-probe` identity can opt into one Task Messages
profile by setting `ADP_TASK_PROBE_PERSONA` to one of:

- `agent-task-investigator` (16 output tokens)
- `agent-task-cyber` (64 output tokens)
- `agent-task-claude-developer` (64 output tokens)
- `agent-task-codex-developer` (64 output tokens)

The existing local `ADP_PERSONA_MODEL_PROBE_ENABLED=true` gate and server probe
admission must both be enabled. Each opted-in invocation claims at most one
slot; the server selects its model from the existing model allowlist and its
verified platform destination. Customer destinations remain excluded.

Without this opt-in the worker runs its existing Claude SDK probe cycle. Older
workers send no `task_persona` claim and cannot consume Task profile slots.
Task and legacy claims share exactly the existing daily cycle, slot count,
reserved spend and started spend. No budgets or daily manifest fingerprint are
reset by introducing Task profiles.

The checked-in `task-profiles.json` mirrors `src/tasks/personas.py`; gateway
parity tests require exact body, revision and SHA-256 matches. The worker checks
the entire body before durable start, performs one Bedrock InvokeModel call
with retries disabled, and requires the expected text/tool response and a
provider request ID before reporting proven evidence. A lost start receipt or
uncertain provider result is never retried automatically.

This qualifies the Messages provider contract used by each Task persona. It
does not claim to prove the complete coding/Codex runtime, repository tools or
remote-control acceptance; those require the separate Task integration and
live remote-control scenarios.
