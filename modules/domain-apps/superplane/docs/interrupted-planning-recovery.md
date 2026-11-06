# Recovery of an interrupted initial plan

`interrupted-planning` is a separate recovery scope for an initial managed-account
Terraform plan that started but produced no saved proposal. It never classifies
that execution as unstarted, and it does not recover apply, bootstrap, adoption,
shared placement or account creation. Only an original operation without prior
retry receipts is eligible.

The installation owner uses `installation.planning_recovery.record` with
`interrupted=True`. Its inert probe mounts the original volume read-only. The
collector requires the qualified source recipe, original image and protected pod
audit, stopped worker, paused dispatch, original storage identities, exact module
and provider cache, and an empty review directory. The reviewed recipe cannot
apply infrastructure. Unknown files, a saved plan, changed source or uncertain
provenance refuse recovery.

The three maintained process-read diagnostics are the only supported exec
exception. Qualification reads the complete protected audit, verifies both
stream stages and the installation operator's immutable role identity, and
matches the complete command to a reviewed program. Stdin, attach, additional
containers, arbitrary commands and altered programs refuse. The original
environment ConfigMap must remain unchanged during qualification; collect the
final evidence before rolling that ConfigMap.

If a workspace policy extension selected the source policy, pass its completed
receipt and exact saved plan as `policy_extension={"receipt": ..., "plan": ...}`.
The collector verifies the plan digest, unchanged installation and storage,
original worker image, next policy bytes and live immutable ConfigMap UID. It
does not rewrite the receipt's original environment or call it runtime maintenance.

The owner independently reads the exact versioned backend and checksum key.
No state version, delete marker or checksum may exist. A retained lock must be
the stopped pod's `OperationTypePlan` lock, created during its original execution
with the qualified Terraform version. Its complete original bytes are retained
in the owner evidence. A foreign lock or an apply lock refuses recovery.

Gateway joins this evidence to the original admission, provider intent, pod and
credential owner. All original grants must expire, followed by the existing full
900-second session grace period. Issuance audits are retained. Current requester
and approver authority and the original budget remain mandatory. Missing files
or an empty AWS listing alone are never sufficient proof.

The original requester uses `/workspaces/{workspace_id}/interrupted-planning-recovery/review`,
the selected approver decides through `/decision`, and `/resume` records the
distinct immutable retry. Attempts and their ceiling remain unchanged; this scope
does not grant an extension. The new worker verifies and archives the retained
tree before preparing another plan. Under its fresh lease and provider authority,
it rechecks the backend and conditionally releases only the exact recorded plan
lock. A changed lock or withdrawn execution authority prevents that release.
Infrastructure apply still requires a separate approval of the resulting saved
plan.

Queued redeliveries of an expired recovery attempt can also be acknowledged once
the retained retry receipt supersedes that attempt's exact source fence. The
handler checks its protected mapping, complete envelope and expired grant; it
preserves the old execution and pod binding. Current and successor generations
remain ineligible for this acknowledgement.

Deploy compatible Harness 18, domain migration `058_interrupted_planning`,
Gateway/tick, API and worker consumers together under the maintenance lock while
dispatch remains paused. Prepare builds first; publish the final one-hour owner
evidence after the expiry boundary and before changing the original ConfigMap.
The producer policy adds only the new recovery route. The provider policy adds
`s3:ListBucketVersions` on the existing workspace state prefix so the worker can
detect backend changes before releasing the lock. Downgrade refuses after new
decisions or retry receipts exist.

Before changing the shared worker binding, advance the enrolled provider policy
fingerprint through `installation.provider_authority` with
`--repair-state-history-read-from`, the original private policy snapshot through
`--previous-policy-document`, and the applied shard's exact
`--repaired-policy-version`. This accepts only the added version-list read on the
same workspace state prefix. All other grants, trust, owner fields and expiry
remain unchanged. After the new worker binding is installed, use the separate
`--rotate-binding-from` transition on that successor document. Each transition
checks the actual live policy and binding and conditionally advances one generation;
neither writes IAM nor approves a workspace operation.

Provider-preflight diagnostics record exception type and code locations without
exception text, locals, credentials or request contents. They preserve refusal
behavior and provide evidence for diagnosing a subsequent interruption.
