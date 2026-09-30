# Security-agent filing and delivery recovery

Run `36359654455` completed scanning and filing, then failed to publish the
root EventBridge event: the workflow assumed the dedicated `trusted-scan` role,
which had no `events:PutEvents` grant. The older deployment identity's grant did
not authorize this new identity. Run `36392994870` subsequently reached triage
again and correctly refused to overwrite the already-filed traceability ledger.

The fix gives `trusted-scan` only `events:PutEvents` on the current account and
region's default bus, conditioned on source `adp.security-agent` and detail type
`ADP Agent Dispatch`. It grants no EventBridge rule/target administration. The
rule's literal persona, service identity and repository remain the payload
boundary. The scanner service role is unchanged.

Before authoring a grouping plan, triage now checks for completed filing. It
validates the filed traceability, matching date/source, the full finding-ID and
severity map, and the completion marker's finding coverage, issue IDs and counts.
A consistent completed filing is adopted without changing the traceability or
completion marker or making another model/issue-filing call. Missing or
conflicting evidence fails closed. Legacy ledgers lack a full content snapshot:
recovery compares finding identities and severities, not descriptions or raw
scan-file bytes. Changed finding identities or severities require a separate
scan-date prefix rather than overwriting an existing night's issue mappings.

Delivery can also run when triage was skipped because the exact scan content was
already marked processed. It still requires successful scanner and scan-gate
jobs and refuses failed/cancelled triage. The existing join barrier and durable
`root-dispatch.json` marker remain responsible for plan and dispatch reuse.
This does not remove the existing crash window between successful EventBridge
publication and durable marker persistence; do not delete a dispatch marker to
force a retry.

## Operator rollout

1. Merge the source fix and review/apply `platform/automation-infra` in the scan
   account. Confirm `ADP_SCAN_ROLE_ARN` names that stack's `scan_role_arn` output.
   The IAM correction is not live until applied.
2. Check the deployed EventBridge security-agent rule and target. The repository
   default remains disabled. A successful `PutEvents` response alone does not
   prove a worker started. Enabling delivery and its downstream agent work is a
   separate operator action; this patch does not enable the rule or a schedule.
3. Preserve the September 28 S3 ledger and dispatch markers. Manually dispatch
   `Security Agent Nightly` with `findings_run_date=2026-09-28` to reuse the saved
   findings without another metered code review. The recovery check must pass
   before proceeding. If it reports conflicting artifacts, reconcile them using
   the existing filed issues; never bypass the overwrite safeguard.
4. Confirm triage reports `resume=true`, delivery adopts the existing plan, and
   the root event is accepted (or an existing dispatch marker suppresses it).
   If the rule is enabled, separately verify the resulting delivery worker.

Validation: offline retry/traceability/dispatch/wiring tests and mocked Terraform
policy tests. No live IAM apply, event publication, issue filing, or paid scan
was performed while preparing this patch.
