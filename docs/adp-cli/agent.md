# Agent Activity CLI

`adp agent` uses the selected deployment and your existing human login to inspect Activity and control owned runs. `--admin` selects tenant-admin reads, checked by the server. It never supplies owner/tenant headers or worker credentials. Service callers submit work using the existing `adp task` Task API surface. Enrolled humans use [`adp agent trigger`](hosted-coding.md), backed by the same Task API. Returned Task invocation IDs resolve owner-only Activity detail and retained reports; Use `adp agent list --tasks` for the separate owner-only Task list. The Activity screen exposes this same list under “View my Tasks”, with links to the canonical Task event stream.

```bash
adp agent list --max-pages 5 --page-size 20 --json
adp agent list --tasks --max-pages 5 --page-size 20 --json
adp agent chain CHAIN_ID --json
adp agent status --run RUN_ID --json
adp agent wait --run RUN_ID --timeout 60 --json
adp agent logs --run RUN_ID --json
adp agent logs --run RUN_ID --follow --timeout 60 --json
adp agent ping --run RUN_ID --json
adp agent state --run RUN_ID --json
adp agent pause --run RUN_ID --command-id UUID --reason 'Inspect progress' --dry-run --json
adp agent pause --run RUN_ID --command-id UUID --reason 'Inspect progress' --yes --json
adp agent resume --run RUN_ID --command-id NEW_UUID --reason 'Continue reviewed work' --yes --json
adp agent steer --run RUN_ID --command-id NEW_UUID --instruction 'Run the focused regression before committing.' --yes --json
adp agent abort --run RUN_ID --command-id NEW_UUID --reason 'End bounded fixture' --yes --json
```

Keep a UUID for each intent. An explicit retry must use the same ID and payload; changed payload conflicts. The CLI never automatically retries a mutation. A lost or truncated HTTP acknowledgement triggers one state read. Matching ID/action history from the observed worker generation is informational only: the journal omits payload binding, so it cannot prove that the submitted reason/instruction applied. The submitted payload outcome remains unknown even when the journal reports an earlier matching ID as applied. Missing history or a generation change also remains unknown. `--expected-generation` checks the state read but is advisory: the current server request schema cannot enforce an atomic generation precondition. Reason is required (1–1000 characters) except steer, whose actual server schema accepts only instruction (1–4000 characters) and UUID.

Controls require both available runtime state and the individual capability. Unavailable/old runtimes and terminal runs are refused without posting. `--yes` only confirms intent; the server still enforces ownership, tenant, live-session and terminal checks. A dry run reads state and reports the proposed body without writing.

Read commands return the common JSON envelope. Controls return exit 0 only for `applied`, exit 4 for pending/delivered/unknown/unavailable, and exit 5 for rejection/cancellation. HTTP errors retain `error.http_status`; usage errors return 1, authentication errors 2, and permission errors 3. `wait` succeeds only for Activity's authoritative `complete`; terminal failure returns 5 and deadline/unknown state returns 4. A successful `status` command means the read succeeded, not the run.

Pause acknowledgement does not prove quiescence: inspect state for `paused`. An in-flight provider call or background process may prevent confirmation. Steering delivery does not prove model comprehension. Abort acknowledgement does not prove exit: inspect Activity final status. Ctrl-C and watch deadlines detach the client and never cancel the hosted run.

`logs` reads retained Markdown. The current API returns the same 404 for unavailable, pending, expired, redacted, missing or inaccessible transcript; the CLI reports this limitation explicitly instead of guessing or returning empty success. `logs --follow` emits NDJSON explanation envelopes with `detail.event`, `detail.data` and `detail.last_event_id`. Reconnect using `--last-event-id` and the same run. Duplicate authored sequences are suppressed; generation changes fail, reset events disclose retention gaps and clear the sequence bound while retaining the generation guard, and terminal stream closure directs you to read the actual run status. Interrupted stream reads and gateway `finished` events detach with the last cursor and no inference about execution success. Streams and waits default to 60 seconds, capped at 3600 seconds. Exited workers may refuse stream reconnect; durable after-exit SSE replay is not promised.

Live acceptance remains open for #5629. Source tests and API probes do not establish served installed-CLI acceptance, full pause quiescence, or steering comprehension. Reuse the existing nightly regression scenarios and bounded owned fixtures; preserve ordinary/admin identity separation and record exact client/server/worker revisions.

## Recover a worker that exited during bootstrap

A standalone GitHub worker can fail before registering live controls. After its pod is removed, `agent state` correctly remains unavailable. `recover-exit` records a failed outcome only when the gateway can independently retrieve a successful Kubernetes audit response containing the exact original, terminated Pod.

```bash
adp agent recover-exit --run RUN_ID --operation-id UUID \
  --expected-attempt 1 --expected-credential-epoch 1 \
  --expected-workload-uid POD_UID --audit-id AUDIT_UUID \
  --audit-at 2026-10-18T11:44:12Z --dry-run --json
# Repeat the identical identity, evidence hint and operation ID with --yes to commit.
adp agent recovery-status --run RUN_ID --operation-id UUID --json
```

The dry run verifies protected ownership and provider evidence through the preview API without changing the run. The server selects its configured EKS audit log group; an audit ID and timestamp only locate evidence. It requires your current human session and membership, the original protected launch owner, unchanged attempt/credential epoch/pod binding, no control registration, and a terminal worker from an approved runtime image. Graph continuations, delegated children and partial lineage metadata are refused. Missing or expired audit evidence remains unavailable; a missing pod or old heartbeat is insufficient.

Commit atomically records the failed Activity outcome, protected execution outcome and recovery receipt. It preserves attempts, binding, grants, spend and budgets. It never sends or receives queue messages, schedules another review or changes a delivery node. A lost acknowledgement is reconciled by reading the same operation ID; the CLI does not repeat the mutation. Recovery success means the failure was durably recorded, not that the original assignment succeeded. The gateway and CLI source plus the Terraform-owned exact audit-log read permission/configuration must be deployed before this command is available.

Task discovery includes only Tasks admitted after the owner-list rollout; historical
Tasks remain readable by ID and are not backfilled automatically. The owner list
uses `/me/agent-invocations/tasks`, independently paginated from native runs/chains,
and reapplies current Task ownership and persona policy to every result. Empty
pages can still have a next cursor. `--tasks --admin` is unavailable. The screen
streams `/v1/tasks/{task_id}/events` with authenticated fetch and real-event replay
cursors; a heartbeat or opening snapshot is never progress. Reports and Task
events are retained evidence, not complete native client transcripts.
