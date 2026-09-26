# Hosted repository coding through the Task API

`adp agent trigger` submits an enrolled human coding Task using the existing
`POST /v1/task-artifacts` and `POST /v1/tasks` endpoints. It uses the selected
ADP login and tenant. It does not create or impersonate a service account.

```bash
adp agent trigger --repo owner/repo --issue 123 \
  --persona agent-task-claude-developer \
  --snapshot-file snapshot.json --instructions-file issue.txt \
  --request-id saved-unique-request --dry-run --json
# Review the preview, then use the same arguments with --yes.
adp agent status --run tsk_UUID --json
adp agent logs --run tsk_UUID --follow --timeout 60 --json
adp agent steer --run tsk_UUID --command-id SAVED_UUID \
  --instruction 'Keep the patch scoped to the requested CLI behavior' --yes --json
adp agent wait --run tsk_UUID --timeout 120 --json
adp agent abort --run tsk_UUID --command-id ANOTHER_SAVED_UUID \
  --reason 'Cancel this owned task' --yes --json
```

Use real IDs returned by the commands. The `tsk_` handle identifies the canonical
Task; its `invocation_id` is a separate Activity identity. Task pause/resume are
unavailable. Abort and steer acceptance are pending until worker/terminal
readback; a timeout only detaches the client. Retry submissions with the same
request ID and unchanged files. A locally recorded unknown artifact upload is
not automatically repeated and never dispatches a paid Task.

The existing human Task policy must explicitly enroll the chosen persona,
Task/artifact scopes and repository paths. Each `repository_scopes` entry has
numeric `repository_id`, `repository` (`owner/repo`), and `path_prefixes`.
The gateway independently checks current tenant installation ownership, issue,
commit and each Git blob before admission. Repository access is read-only;
GitHub installation credentials remain in the gateway.

The snapshot contains 1–32 UTF-8 files and is at most 256 KiB:

```json
{"schema_version":"1.0","repository_id":42,"repository":"owner/repo",
 "commit_sha":"40_lowercase_hex_characters","issue":123,
 "files":[{"path":"cli/main.py","blob_sha":"git_blob_sha1","content":"original file content\n"}]}
```

Copy the real immutable commit and Git blob identities. Instructions are a
separate UTF-8 file of at most 16,000 characters. Supported repository paths use
letters, digits, dots, underscores, hyphens and slashes; traversal and `.git`
paths are refused. Only attached, enrolled files can be edited.

Both `agent-task-claude-developer` and `agent-task-codex-developer` expose four
repository tools: list files, read file, replace one exact text occurrence with
a current blob check, and submit patch. There is no shell or unrestricted
filesystem, network or Git tool. Codex runs the actual pinned Codex executable
through the host bridge; both engines currently use the gateway-authorized
Anthropic Messages backend. This is not OpenAI model compatibility evidence.
See [Task model enrollment](task-model-enrollment.md).

The report's recommendations contain ordered `ADP_PATCH_V1 i/N` fragments. Strip
each fragment's first line and concatenate its payload in order to recover the
actual unified diff. The worker derives this diff from its in-memory edits.
Tests are not executed and no branch, commit or PR is published by this runtime.
The report states those limits; productive code review and test/publication
remain separate steps.

Nightly case E42 runs only with an explicit `human_task_coding` fixture. It needs
`snapshot`, `instructions`, `persona`, `scenario` (`complete` or `cancel`),
`enrollment_verified: true`, `shared_budget_authorized: true`, `max_dispatches: 1`
and `max_task_usd` in `(0,1]`. These are operator attestations of existing policy
and budget headroom, not new enforcement counters. The existing standing Task
policy must enforce that per-task bound. Retain the shared qualification spend
ledger between runs; do not reset it or substitute the Task pilot's larger caps.
Each E42 execution dispatches one task, replays the same request, records stream
cursors and control receipts, and requires terminal readback. Missing fixture
blocks E42. Cleanup cancels only the newly created owned Task and requires
terminal proof. E42 does not claim test execution, publication, control delivery
from acceptance alone, or spend reconciliation without joined usage evidence.

E42 keeps recovery data in the case's durable report details, so terminating the
fixture instance does not discard an uncertain submission. The record contains
the original request ID, gateway, artifact/Task IDs and exact canonical Task
submit body. Reconcile with the same `Idempotency-Key` and body; never submit a
replacement request. Uploaded snapshot bytes are unnecessary for this replay.
Session credentials are excluded. E42 fixture instructions must fit within
4 KiB as JSON and survive the normal credential redactor unchanged; this keeps
recovery within SSM's output bound. This fixture bound does not change the CLI's
16,000-character instruction limit. Large patch results are retrieved using the
retained Task ID; the report carries their digest instead of duplicating them.
