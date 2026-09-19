# agent-codex-reviewer

`agent-codex-reviewer` is a standalone pull-request review runtime built with
the [OpenAI Codex SDK](https://developers.openai.com/codex/sdk/). It does not
import, invoke, enqueue to, or finalize through the Claude Agent SDK worker.

## Runtime boundary

```text
authenticated GitHub webhook
  -> dedicated codex-review FIFO
  -> dedicated KEDA ScaledJob and IRSA role
  -> this container / Codex SDK
  -> deterministic GitHub controller
```

The only shared services are model-neutral platform boundaries: authenticated
webhook ingress, the GitHub installation-token broker, the ADP model gateway,
and the invocation activity table.

The controller fetches and verifies the exact PR head SHA before review. Codex
starts in `read-only`; a separate `workspace-write` turn is allowed only for
bounded findings classified as mechanical. Codex never receives GitHub
credentials and never commits, pushes, comments, approves, or merges. The
controller performs those operations with stale-head checks and
`--force-with-lease`.

A mechanical-fix run pushes to the existing developer PR branch and stops. The
resulting `pull_request.synchronize` event must complete a new current-head
review before approval or merge.

The controller uses the tenant's existing default/developer GitHub App identity
to publish verdict comments, push bounded fixes, and optionally merge. An
approval verdict is deliberately a PR comment rather than a formal GitHub
approval because an identity cannot independently approve its own work.

## Feature controls

- `CODEX_REVIEWER_ENABLED` routes eligible PR events to this runtime.
- `CODEX_REVIEWER_APPLY_FIXES` allows bounded mechanical repairs.
- `CODEX_REVIEWER_MERGE_ENABLED` allows the controller to squash-merge only
  after a current-head review and successful checks.

All flags default off at the Terraform boundary except mechanical repair, which
has no effect until the reviewer itself is enabled.

Delegated agent authority does not route this runtime through the hosted worker.
The gateway has a dedicated Codex reviewer adapter that binds its authenticated
IRSA identity to the ingress-owned pull-request activity row. The runtime never
receives or reuses the Claude worker's run credential or workload bootstrap.

## Local verification

```bash
npm ci
npm test
```
