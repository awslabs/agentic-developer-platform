# CLI coverage and remote-control qualification

The operator authorized review and merge of existing CLI work, assignment of suitable Epic #5644 stories to ADP agents, and use of those runs to exercise remote control on 25 September 2026. Task submission must reuse the existing Task API. This record separates shipped commands, candidate implementations and proposed coverage.

## Baseline and existing work

Baseline main: `200e97aa7` (the full revision is recorded by Git). The existing CLI reference was last audited against a 19 September revision and understates deployment selection, flow/model commands and newer Superplane operations. Parser source and served-artifact checks take precedence over stale availability prose.

- PR #6074, Task API CLI: `adp task submit/status/monitor/abort`. Reuse this helper and its deployment-bound OAuth/service-principal transport. Review found unknown task states returned a successful status exit; repaired with regression tests. Merged as `a281c01bbe96787c863ad5a77041b5bf2276029f`; publication is recorded separately.
- PR #5716, capability discovery: `adp capabilities` and `adp doctor`, plus a checked command manifest. Review repairs address own-scope versus administrator Bedrock reads, conditional setup capabilities, current-main integration and colliding regression IDs.
- #5637 is already closed; do not dispatch it again. Other stories require current work-claim/PR checks before assignment.

## Important API distinction

The existing `/v1/tasks` API durably submits, reads, streams, accepts input and cancels Tasks. Its current public command contract contains input and cancellation, not pause/resume. The existing human Activity control API exposes capability-gated pause/resume/steer/abort for supported legacy hosted-agent runs. These are distinct authority and runtime contracts. Do not send a service-principal token to a human endpoint, invent pause by stopping polling, or label a queued input as a confirmed steering handoff.

The CLI will expose supported Task operations through the Task API. Activity controls will reuse ControlService through the existing authorized route. Human Task submission and repository developer persona support must be confirmed from actual admission policy/runtime support before #5516 can use them; no parallel dispatcher will be built.

## Assignment order

1. Review/repair/merge existing #6074 and #5716. Preserve their live acceptance gaps.
2. Reconcile #5516 with Task API submission/authentication and #5629 with all four now-implemented Activity controls. Check existing engine claims and authorize only one development action at a time.
3. Use a real bounded #5629 development run for pause → confirmed quiescence → steer → resume → observed changed implementation. Verify one invocation/session and no duplicate task.
4. Use a separately bounded owned run for abort, repeated/concurrent command IDs, final report and queue acknowledgement. Preserve its implementation branch for subsequent continuation/review.
5. Extend adjacent coverage stories in dependency order: tenant selection, activity/usage readback and budget configuration first; then remaining administrative/domain groups. Feed scenarios into the existing regression catalog, not a new scheduler.

Live readback on 25 September supersedes the old issue summary: the flow has 33 nodes, is explicitly paused, and its current stored policy expired at `2026-09-22T21:00:00Z`. It records $809.564317 usage against a $1,000 ceiling, 40 attempts per node and four concurrent actions. Six nodes retain historical running state with exited worker histories and `authority_unverifiable` review holds. #5516 and #5629 remain pending with zero attempts and no bound PR. None of these counters or settings were changed. Current admission/ownership must be inspected before new assignment. Retain the documented one-action concurrency, total inference budget and stricter live-test daily bounds; code merge is not live acceptance.

## Remote-control evidence

For each exercised operation record selected deployment/tenant/caller, Task and invocation IDs, CLI/server/worker revisions, saved command ID and exact redacted CLI command, HTTP receipt, observed runtime state, SDK/tool effects, stream cursor, terminal report, usage and cleanup.

Cover capability gating; ownership/tenant refusal; pause requested versus confirmed; no new tool starts while paused; resume without replay; steering text actually consumed; concurrent/repeated IDs and changed-payload conflict; abort during work and pause; stream disconnect/reconnect and terminal replay; Ctrl-C detaching without remote mutation; and honest unknown outcomes. Task cancellation and Activity abort are evaluated against their respective contracts.

## Productive hosted run and initial live observations

Assignment: [#5629 comment](https://github.com/aws-e/adp/issues/5629#issuecomment-5837828184). ADP admitted developer invocation `eda71c62-723c-5726-9424-13d03d4355a7`, generation 1, without changing the paused Epic flow or its expired policy. Ping and state report all four controls available.

These initial probes use the authenticated public Activity API and are **API qualification**, not the story's installed-CLI acceptance:

| Check | Observed result |
|---|---|
| Pause | Accepted, delivered; remained `pause_requested` with zero tracked active tools and reason `background work behind completed tools is not observable`. Full quiescence was **not confirmed**. |
| Replay | Same pause ID/payload returned the existing delivered command; changed payload returned HTTP 409. |
| Steer | Accepted while pause was pending; after resume, journal reports instruction handed to runtime. Application requested a specifically named operator-login isolation regression; code evidence is still pending. |
| Resume | Applied on the same invocation/generation; state returned to running. Pending pause was explicitly cancelled as unconfirmed. |
| Explanation stream | HTTP 200 SSE returned authored explanation sequence 1 and heartbeats. Reconnect with saved `Last-Event-ID` returned sequence 2, without repeating sequence 1. |
| Abort | Not yet exercised; preserve useful implementation before the bounded abort test. |

The public streaming route is `/activity/invocations/{invocation_id}/agent/events` under the selected gateway's `/api` mount. Control state is the sibling `/state`; signed human mutations use `/pause`, `/resume`, `/steer`, `/abort`. This does not alter the separate Task API `/v1/tasks/{task_id}/events` and `/cancel` contracts.

CLI testing isolation: shared fixtures remove inherited `BG_CONFIG_DIR`, `ADP_LEGACY_CONFIG_DIR`, deployment pins and other ADP/store overrides before using temporary homes. A separate process wrapper isolates home, XDG and AWS stores. Live login/configuration digests were checked unchanged after isolated runs. No test writes operator tokens.
