# Story-card task progress and failure diagnostics

Story cards reuse the task board from Live Agent Activity. They show a collapsed
completed/total count, blocked count and progress bar. Expanding the card displays
the same checklist component, including parent steps and task filters. Claude
TodoWrite checklists and Codex boards retain their existing formats.

The shared `ExplanationEvents` publisher saves its latest sanitized assignment
plan to a run-local file. The Python worker reports changed snapshots every ten
seconds and flushes once before terminal reporting. Gateway stores the snapshot
on the authenticated run assignment. Visible story cards read it in the existing
activity batches on the graph refresh cadence (normally thirty seconds). No model call or persistent live
stream is opened by a story card.

The current attempt's newest assignment is selected before its board is read.
A newer run without a board does not inherit an older run's apparent activity.
Run identity, persona, attempt and observation time remain visible. A stopped
run's board remains available; tasks last reported in progress are not described
as currently running. Task counts are agent reports, never proof of review,
deployment or story acceptance. Removed tasks are not counted as completed.

Engine-dispatched Codex development receives the accepted node's criteria and
delivery requirements with its protected launch context. A linked issue may
cover several stages or an entire epic; it supplies background without expanding
the current assignment. The controller identifies each board by its flow, node
and a digest of the accepted work. Retrying unchanged work resumes that board.
A whole-issue board, another node's board, or changed requirements start a new
board while preserving the previous file byte-for-byte under
`.adp/tasks/archive/`. Archived tasks remain historical work, never implied passes.

Roll out the dispatcher and worker together for scoped development. Older
dispatches without node context retain their existing issue-based behavior.
A running worker keeps its loaded board until it exits; updating the image does
not rewrite an active assignment. Preserve its work and reconcile its exit before
using normal recovery with the scoped runtime.

Failure diagnosis is separate from the engine's current scheduling decision.
Cards show the recorded failure category, responsible role, recommended next
step and a link to that exact run. Known operations runtime diagnostics such as
turn limits, context limits and transport interruptions retain their meaning.
Unclassified failures explicitly say that the cause is unavailable. An operations
failure report can be expanded in the card after the existing evidence redactor
is applied. No report text authorizes a retry or overrides a hold.

For failed operations runs, cards also inspect the current attempt's latest
completed diagnosis from the authenticated task ledger. Known missing L5
configuration and DevOps human-evidence codes add fixed, actionable guidance;
unfinished later tasks do not erase a completed diagnosis. Arbitrary code text,
task titles and secret values are never published. A bounded bulk lookup reports
overflow explicitly. The terminal failure code, attempt history and acceptance
state remain unchanged, including when the attempt cap has been reached.

Task text and operations report text are visible only to the dispatched run's
owner or a caller with tenant-wide activity access. Other flow readers receive
bounded diagnostic descriptions and an explicit restricted-board message.
The snapshot write uses the existing run reporting capability and requires an
active, started assignment. Out-of-order updates cannot roll the board back;
terminal assignments reject further snapshots.

## Operations story restoration

Claude and Codex operations workers read `repair_tasks` before model work. Its
`story_board` field loads `.adp/tasks/<issue>.json` only from the currently
authorized `agent/issue-<issue>` workspace, through the protected source reader.
Versions 1 and 2 retain task IDs, status, parents and evidence notes. The source
records the exact file SHA-256, repository identity and confirmed provider base;
a newer remote branch revision is never attributed to older local bytes.

The full restored board is published automatically to the existing assignment
plan feed. Progress claims never authorize work or establish release acceptance.
Claude TodoWrite uses `ID [open|in_progress|done|blocked] title` and keeps blocked
tasks pending with their explicit marker. Codex uses the operations-only
`story_task_progress` host tool with `expected_sha256` and explicit ID/status
updates. It runs through the existing control and receipt journal under the
already admitted artifact capability. No native Codex tool admission is widened.

Updates merge by ID, retain omitted tasks and original evidence, and permit
explicit changes from blocked to another status. New tasks require a kind and
title and are marked as additions from the reporting run. Both operations stacks
also save a private cumulative delta through the existing run-report endpoint,
independently of public explanation/control availability or display size. They do
not write `.adp` files or change the story branch. The next authorized `repair_tasks`
read restores the latest acknowledged delta against the current branch board.
Unchanged task rows retain their claimed statuses. Branch edits win, deleted rows
are not resurrected, and additions incorporated into the branch become branch
rows. Explicit conflicts and bounded history retain the prior claims. These are
progress claims only: no native execution, approval or acceptance authority changes.

The gateway derives tenant, flow, story node, issue, repository/installation,
branch, run, attempt, claim generation and accepted-plan scope from the authenticated
assignment. An earlier plan can bridge only through verified node-preserving
lineage, including reviewed source maintenance. Writes use a revision compare and
swap, reject terminal assignments, and retry the identical request after a lost
acknowledgement. A legacy display update cannot erase a private checkpoint. Private
deltas and scope are excluded from public activity and story-card responses.

Private deltas are limited to 64 KiB, report envelopes to 128 KiB, and the effective
restored board to the existing 48 KiB/1,000-task context bound. Sensitive content,
oversized payloads and unavailable scope are rejected explicitly while preserving
the previous acknowledged checkpoint. Worker files use private permissions. The
combined `repair_tasks` transport preserves UTF-8 and refuses responses above
256 KiB with an actionable error; it never silently truncates tasks or repair
history. Unavailable restore can recover after a successful authorized read.

Missing, malformed or unavailable imports do not discard the independent
operations repair-cycle history. A later successful `repair_tasks` read can
restore the board without restarting. An absent board is distinguished from an
unreadable one. Automatic model context is limited to 48 KiB of compact JSON;
larger valid boards return task counts, source metadata and the digest with
instructions for complete `repair_read` pagination. Files above the protected
256 KiB read bound require owner-side metadata reduction. A display above the
shared feed's text bound is reported explicitly and is never partially counted.

## Rollout and limits

Apply migration `172_story_task_board` for installations without story snapshots;
the private checkpoint delta uses its existing JSON column and needs no additional
migration. Deploy Gateway before deploying the
shared `adp-agent-runtime` image for Claude and Codex story agents, then the
frontend. Deployments with separate worker images must rebuild each image. Follow the platform's canonical deployment guide
for account selection and authorization. This change does not deploy itself.

The publisher is shared by Claude and Codex controls and benefits every persona
that emits an assignment plan through that existing feed. It requires live
explanations and run reporting to be enabled. A persona that has not emitted a
board displays an explicit missing-snapshot message. Older runs are not
backfilled; their retained transcripts remain the source for old checklists.

Private progress is durable only after the reporter receives and verifies the
gateway acknowledgement. The normal reporting interval is ten seconds, with a
final flush before terminal reporting. An abrupt worker loss can lose changes
since the last acknowledgement; outages may make this longer than one interval.
A failed final flush emits `story_checkpoint_final_flush_failed` and directs the
operator to retain activity and read `repair_tasks` before resuming. This is
periodic checkpointing, not an immediate or zero-loss save. Older runs without a
structured checkpoint remain dependent on their branch board and retained activity.
The card displays the observation time rather than asserting current
liveness. If a live preview was truncated or a checklist format is unfamiliar,
the original text remains readable but no partial completion total is shown.
