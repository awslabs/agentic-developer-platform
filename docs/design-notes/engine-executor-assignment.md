# Accepted node executor assignments

A flow may select its story developer without changing the deployment-wide
`BG_ORCH_DISPATCH_PERSONA` default. The selection is an optional `executor` object
on a proposal node:

```json
{
  "address": "claude-desktop-6850/epic-6850/delivery/requests",
  "kind": "story",
  "title": "Preserve supported client-tool requests",
  "issue_ref": "6852",
  "executor": {
    "schema_version": 1,
    "kind": "agent",
    "role": "develop",
    "persona": "agent-codex-developer"
  }
}
```

The task remains a story: dependencies release it, a developer produces a PR,
and verified merge completes code delivery. The executor specifies who performs
its development work. It grants no additional permissions and does not replace
policy, model selection, review, deployment or evaluation contracts.

## Supported combinations

Version 1 supports `story` + `agent` + `develop`, with either `developer` or
`agent-codex-developer`. Other personas, roles, executor kinds and node kinds are
rejected during proposal parsing/preview. Review and repair assignments continue
through the existing Codex reviewer controller. Human gates stay human. Machine
evaluations continue using their evidence adapters; this contract does not turn
an arbitrary agent transcript into a passing evaluation.

This shape is intended to accommodate additional implemented executors later.
Adding a workflow/service executor or design/operations task requires its own
input/output, authority, completion and recovery behavior, plus a compatible
schema and validator entry. Merely registering a persona is not enough.

## Acceptance and compatibility

The assignment is included in the accepted plan and its hash. Dispatch reads the
exact node address from the current tenant/flow plan and refuses explicit
assignments on unaccepted drafts, including assignments equal to the global
default. It resolves model configuration for the selected persona using the
existing attributed-human path. A selectable persona still needs a valid model
and enabled deployed runtime.

Omitted assignments are not serialized into older node documents, preserving
existing plan hashes and legacy behavior. Unknown or invalid explicit assignments
never fall back to the global developer. An amendment changing an executor must
be accepted through the normal plan path; it does not rewrite existing run
assignments or their evidence.

The preview API returns the executor for each node, and CLI preview displays its
persona and role. The persisted plan remains the configuration readback. Worker
activity exposes Codex developer observations as development history. Credential
authorization, shared model accounting and failed-developer recovery recognize
both supported developer identities while retaining their existing scope and
policy checks.

## Qualification

Focused tests cover plan-hash changes, compatibility of omitted assignments,
invalid gate/persona/role/kind combinations, isolation between nodes, accepted
versus draft dispatch, develop/repair authority and visible Codex development.
A real engine Codex development run is still required to qualify the deployed
worker and PR/review/merge integration. Unit tests do not establish that live
qualification or the readiness of the Claude Desktop epic.
