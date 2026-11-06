# Periodic runtime status

The installed minute repair worker produces opt-in Transform schedule occurrences
through the shared bounded schedule cursor. The producer initializes each current
configuration revision at the present time; enabling a schedule does not run old
occurrences. Numeric five-field UTC cron expressions are supported. A missed
occurrence older than five minutes is recorded as rejected, and the cursor moves
forward without historical catch-up.

For `source_policy: pinned`, the worker sends only the durable receipt key to
`POST /internal/v1/l5/transform/schedule-admissions`. Gateway resolves the current
configuration owner, scoped consent, Product sources, binding, selected provider,
immutable executor qualification and test policy before common budget and source
admission. The authenticated caller and retained run remain service-attributed.
Repair can call only its own schedule admission endpoint, and cannot obtain
provider credentials. The existing Transform broker policy retains its exact
selected package, region and time limit.

Duplicate occurrences retain the first source and run identity. Admission replay
rechecks current authority and does not capture sources or dispatch another run.
Schedule admission creates no Context/Insights contribution, PR, merge or deploy;
result publication remains separate durable work with its own authority.

## Branch consent and one retained commit

The original [architecture](architecture.md#events-publication-and-reuse) and
[TF06](stories/TF06.md) require resolving a configured branch to an immutable source
once per occurrence and retaining that source on retries. The installed producer
now retains an unresolved branch occurrence and submits its durable key to the
same Gateway admission endpoint. Gateway verifies explicit branch consent, reads
the selected GitHub ref, and conditionally retains the first commit before common
admission. Concurrent reads may observe different heads; only the first committed
freeze is retained. An admission failure or lost response never replaces it.

The owner's existing `delegation-scope` preview and digest include each enabled
branch schedule's exact repository, branch, trigger, binding, goal and cron
selection, in addition to the configuration revision and source scope. Existing
exact-commit grants acquire no branch rights. `branch_schedule_consent_required`
means the owner must review the current preview and grant that selection. Changing
the selection or configuration requires new consent; revoked consent cannot be
recovered by retrying. GitHub installation ownership, personal-installation owner
and linked Product repository rights are checked before and after resolution.

The resolver uses a token restricted to one repository ID and contents/metadata
read. It validates immutable repository/owner IDs and the exact returned branch
ref. The derived work grant contains only the frozen commit and credential ref,
with an occurrence proof bound to the real receipt, parent consent and Product
source binding generation. Source capture and broker checks retain current owner,
configuration, installation, source-link and parent-revocation checks. Other source
providers remain unsupported for branch resolution. A temporary
`branch_resolution_unavailable` is retryable; provider denial and invalid consent
are terminal for that occurrence.

Offline factory-to-Gateway tests cover pinned and branch schedules, revocation and
drift denials, frozen source capture, failed admission retry and duplicate
suppression. Actual concurrent PostgreSQL and SQLite writers verify that the first
freeze wins. These are not native periodic acceptance evidence. TF06 owns the
implemented runtime contract; TF07 still requires installed end-to-end evidence.

The Transform live `periodic-admission` collector remains a separate requirement.
The existing client/event scenario is an offline oracle and does not collect
installed evidence. Durable EventReceipt reasons and frozen sources are available;
the shared append-only EventObservation history currently belongs to Well
Architected and must not be reused for Transform without service-scoped identity.
