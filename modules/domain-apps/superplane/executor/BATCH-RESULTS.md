# Retained batch text results

An approved batch image can publish one bounded text result by writing a JSON
document to Kubernetes's standard `/dev/termination-log` before exiting zero:

```json
{"superplane_result_version":1,"text":"accuracy=0.95\n"}
```

The entire UTF-8 document must be **less than 4096 bytes**. A message at Kubernetes's
truncation limit, malformed JSON, unsupported version or ambiguous source refuses
successful status settlement. There is no fallback to error logs, arbitrary URLs,
binary content or agent-run artifact authority. Images must not put credentials in
results. Common credential patterns and control characters are removed before
storage, with an explicit `redacted` flag; this is best effort, not secret detection.

The trusted executor captures the completed original Job's owned Pod under the
current paid operation lease before reporting success. It checks the original UID,
approved image, Pod owner, zero exit status and unchanged source documents. A
bounded list of at most 32 Pods must be complete. No Pod or no message means no
captured result; the UI states that explicitly. An external administrator removing
a Pod before capture can prevent output retention. Workloads with larger outputs
need a separately governed artifact transport, which is not provided here.

The record binds operation, workspace, Job, Pod, allocation and approved plan. An
identical publication replay is allowed; changed output cannot replace it. A lost
acknowledgement leaves the committed result intact even if the operation requires
recovery. Result capture never releases budget or proves cleanup.

`GET /workspaces/{workspace_id}/batch-jobs/{job_id}/result` rechecks current workspace
READ permission and original paid lineage. It returns `not_captured` or the retained
plain-text document and hash, with `Cache-Control: no-store`. The UI reauthorizes
downloads and uses a text attachment; supplied content is never rendered as HTML.
Reads continue after approved Job teardown and installed profile removal. The
original provision ID remains separate from the current stop-operation ID.

Migration `033_retained_batch_results` is additive and refuses downgrade while
results remain. Records have no automatic expiry or delete endpoint; database
backup/retention ownership also covers these records. Runtime CI includes real
PostgreSQL API-to-worker capture/cleanup, replacement and revoked-access cases,
schema rollback refusal, UI races/injection checks and fixture Chromium downloads.
These checks do not constitute live batch acceptance.
