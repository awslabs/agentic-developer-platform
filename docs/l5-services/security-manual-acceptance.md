# Security manual release acceptance

Security setup uses the original two distinct fixture tenants, original source,
workflow run/attempt, selected resources, request keys, journals and completion
oracle. Browser automation is not used. These observations are partial facts:
`release_check_completed` and `native_qualified` remain `false` until the original
release evaluator verifies every required check.

## Prerequisites

Both original Products must have current source/profile configuration, the
selected ready Security binding, and the original provider connection. Product
membership alone does not authorize provider or native execution operations.
The fixture transport principal must satisfy the ordinary owner requirements for
those operations. Transferring a Product from a synthetic fixture account does
not transfer its tokens or native consent to the new owner.

The Product owner and selected observer use current interactive ADP sessions
separate from every fixture bearer. The owner may also be the observer. A
credential registered in an API, CLI, MCP or legacy UI fixture slot cannot
approve or record a human check. A designated automation subject cannot serve as
a human even with a different token. The observer must already have ordinary
configuration, source, binding and run access; selecting the observer grants no
new Product access.

The current owner must authorize native execution for the exact configuration
revision through the existing execution-consent controls. CLI and UI edits change
that revision and therefore require fresh native consent before the first MCP
submission. The evidence panel does not grant native authority.
Saving a Security configuration checks its actual tenant-local binding and
retains `execution_delegation` as a readiness gap until current execution consent
is verified. A save does not carry consent forward to a changed revision.

## Human steps

The original setup selector first registers all four fixture credentials in each
tenant and prints a tenant-local request ID. In ADP, open the selected Product's
Security configuration and expand **Release acceptance check**.

1. Enter the exact request ID and preview the owner consent. Review the recipient
   release, source revision, observer, displayed configuration/binding/run fields
   and expiry. Approve that exact snapshot.
2. The selected observer chooses **Prepare my check**. Both tenants must complete
   readiness before the setup selector performs its CLI edit.
3. For the subsequent edit request, review the requested region and reason, choose
   **Use requested Security choices**, and use the ordinary **Save Security
   configuration** button. The conditional save retains its canonical receipt in
   the same transaction as the configuration version.
4. Review and authorize the current native execution scope after the edits. The
   selector then submits the ordinary initial MCP request with its original key.
5. Prepare the final display request and review the full configuration, binding
   and run details. Record either a match or a mismatch. The selector compares
   these retained facts with independent API, CLI and MCP reads.

The later independent `read_setup` pass obtains a fresh protected export of the
same original human/effect chain. It does not repeat a save or submission.
Security run readbacks expose their stored configuration ID and revision so the
original evaluator can compare the exact admission pins across all surfaces.

## Recovery

An authenticated original flow-policy owner or platform operator can read
`GET /api/orchestration/flows/{flow_id}/security-evidence-rosters`, optionally
filtered by `node_id`. Current workspace membership remains required. Follow
`next_offset` with the same filters to inspect later pages. The response contains
only the original frozen case identifiers, fixture hash/expiry and attributed
release dispatch metadata. It retains paused, expired and superseded history and
does not read tenant Product content or local human evidence.

`no_roster_recorded` means this retained flow history has no roster record; it
does not prove that no historical grant existed. An inaccessible flow returns a
generic not-found response. Unverifiable records return
`security_evidence_roster_unavailable`, so missing current configurations cannot
be mistaken for an unfrozen fixture. A roster proves what the release requested,
not local owner consent, native authority or acceptance. This lookup changes no
fixture, grant, workflow, source or execution state.

Use **Read request status** to locate the exact phase. An uncertain save must use
**Reconcile original save**; do not click Save again. Reconciliation reads the
canonical atomic edit receipt and can recover a committed save after its human
window expires. It cannot manufacture an absent or mismatched receipt.
While the original save is still in flight, the selector waits and retries only
reconciliation within its original deadline; it never sends another save.

Within the original release lease, an expired request can be renewed for the
same release, fixture and selection. A completed observation/save gets an
export-only renewal and fresh explicit owner consent; it cannot perform the
effect again. Lease extensions do not extend an earlier request. A pause/resume,
execution phase/status transition, claim replacement, policy/source change or
revocation invalidates the old release authority. Ordinary scheduling polls do
not.
Each original tenant and phase is limited to 32 retained invitations per release
dispatch. Exact request replays do not consume another slot; changed snapshots
and renewals do. Exhaustion preserves history for review and cannot be bypassed
by replacing the original two-case roster.

A missing MCP receipt permits only bounded retransmission of the original
Product idempotency key and body under current native authority. A found receipt
is reconciled read-only. A missing CLI receipt permits only the original key,
body and If-Match condition; it never reads a new baseline for the old operation.
Original journals and failed selector logs are retained. A new workflow attempt
cannot borrow the old attempt's evidence.

Local ledger history remains append-only. Current Product/source authorization
is still required to inspect full retained views. Revocation prevents later
export/import while preserving attribution for already disclosed facts.

## Maintainer verification

The Gateway tests cover tenant-local owner/observer/credential boundaries,
canonical save receipts, expired-window reconciliation, export-only renewal,
release transfer and durable execution/control anchors. The composed offline
test executes the original two-tenant setup selector and a separate independent
`read_setup` process through actual JWT validation, Product/source authorization,
SQL, CLI commands, MCP and release authority. It poisons browser entrypoints and
replaces only external identity, provider and queue transports. It verifies that
fresh readback adds no saves or admissions, and that the real qualification
emitter refuses setup-only evidence. Source tests cover exact-key recovery.
A release still needs a complete original selector plus independent readback run
against two legitimate, ready fixture tenants; unit tests are not live acceptance.
