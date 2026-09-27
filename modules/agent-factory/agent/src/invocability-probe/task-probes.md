# Task model qualification

The dedicated `persona-model-probe` identity can opt into one Task Messages or Responses
profile by setting `ADP_TASK_PROBE_PERSONA` to one of:

- `agent-task-investigator` (16 output tokens)
- `agent-task-cyber` (64 output tokens)
- `agent-task-claude-developer` (64 output tokens)
- `agent-task-codex-developer` (64 output tokens; legacy Messages compatibility)
- `agent-task-gpt-intent-refinement` (64 output tokens; native Responses)
- `agent-task-gpt-developer` (128 output tokens; native namespaced Responses tools)

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
the entire body before durable start, performs one Bedrock InvokeModel call or one SigV4-signed Mantle Responses call
with retries and redirects disabled, and requires the expected text/tool response and a
provider request ID before reporting proven evidence. A lost start receipt or
uncertain provider result is never retried automatically.

Responses calls bind the claimed model and region, request nonstreaming output,
disable storage and request encrypted reasoning. Responses bodies are streamed
into a 64 KiB bound. Completed text must be exactly the OK sentinel; completed
tool evidence must contain exactly the admitted `mcp__adp.task_probe` call with
`{"value":"OK"}`. A probe never executes the returned tool. Codex Task claims
select only Codex models; Messages claims continue selecting Claude models.

This qualifies the bounded provider contract used by each Task persona. It
does not claim to prove the complete coding/Codex runtime, repository tools or
remote-control acceptance; those require the separate Task integration and
live remote-control scenarios.

The native Responses path is covered by local transport/admission tests. It has
not been deployed or qualified against a live admitted probe slot. Existing
probe enablement, account/spend approval and rollout gates remain unchanged.
