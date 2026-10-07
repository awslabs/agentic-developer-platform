# Live workspace and retained run records

Invocation detail uses a responsive workspace: the assignment checklist sits
beside live activity, with summary, current status/liveness, controls and debugging
context in a separate panel. Activity can be filtered to authored updates or tool
activity without starting another stream. Opening the transcript retains identity
and the summary panel. Task-source invocations keep their existing Task stream.

Claude TodoWrite and Codex todo_list events feed the same record capture. Codex
engine-review inspection threads publish temporary plans as ordinary activity;
they cannot replace the repair thread's assignment checklist. A run without an
assignment plan explicitly has no captured assignment checklist. Completion of
these tasks is agent-reported, never a review approval or merge receipt.

Each worker incrementally writes `/tmp/adp-run-record.json`. On normal cleanup,
the Markdown transcript starts with a bounded, versioned base64 JSON comment and
a readable run summary. The gateway keeps using its existing authenticated
transcript endpoint and archive path. No new public storage or access path is
introduced. The UI validates the version, invocation ID and bounded field shapes,
hides the machine comment, and offers the original Markdown for download.

The record includes the full invocation ID, persona/model, repository/issue,
first and last observed checklist, task transitions, bounded recent authored
evidence, SDK session IDs, worker/region, and starting/last observed local commit.
The commit is not proof of a push. The parent appends the observed child exit code;
final delivery status remains owned by the platform's existing terminal handler.

The first checklist is the first observed plan, not a guaranteed dispatch or
previous-run snapshot. The UI separates tasks checked after this observation,
tasks already checked at this observation, tasks added and checked, remaining
tasks, and removed/renamed tasks. Exact task wording defines identity. A rename
never silently counts as completing the old task. Bounds are 100 tasks per plan,
1,000 recent transitions, 32 recent evidence entries and 32 SDK sessions.

If the child exits before final transcript flush, the surviving parent can recover
the last atomic JSON record, provided its invocation ID matches. It labels that
archive partial. Loss of the entire pod before upload is not covered. Legacy
transcripts and invalid/unsupported records stay readable and show that structured
history is unavailable; they do not fabricate zero progress. Capture closure and
successful process exit do not establish successful delivery.

Validation covers both runtime capture paths, reviewer plan isolation, persistence
and recovery, secret filtering, unsupported/mismatched records, renamed/reopened
tasks, the canonical Task view, transcript metadata retention and SSE filtering.
The real workspace components were also checked with browser fixtures at 390,
768, 1024 and 1440 pixel widths, in both activity and transcript views.
