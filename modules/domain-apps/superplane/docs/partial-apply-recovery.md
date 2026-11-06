# Recover a retained partial infrastructure apply

This path preserves the original workspace and paid apply admission. It supports
an existing-account, dedicated managed EKS workspace with an ACTIVE control
plane, no nodegroups or NAT, and exactly one tainted, unused NAT EIP. It does not
support arbitrary Terraform failures or changing the cluster, account or region.
Source implementation and tests do not establish live workspace readiness.

## Prepare the installation

Use the existing Installer maintenance lock and retain the current state, policy,
image bindings and owner receipts. Pause dispatch and KEDA. Install compatible
Gateway, API, paid-worker and operation consumers with Harness schema 15 and
domain revision `053_partial_preparation_refresh`. The API migration creates an immutable
preparation registry and adds a separate recovery decision scope. Existing
migration-owner default grants supply the domain runtime access; no broader
schema or cross-tenant permissions are added. Keep matching consumers until all
retained operations finish; downgrade refuses retained repair history.

Install the API producer role’s exact `partial-apply-observation` and
`repair-partial-apply` invoke routes through the maintained control-plane owner.
The original producer identity and trust remain unchanged.

Fix the provider or module defect through its maintained owner before planning.
Do not replay the failed saved plan, edit or untaint backend state, reset attempts,
or change the workspace provisioning pointer. Preserve unrelated partial workspaces.

An expired automatic recovery observer can advance the lease fence while retaining
the original partial call. Repair verifies every intervening claim against its
authenticated binding and append-only audit, and refuses live claims, missing
provenance or any intervening execution grant. The owner evidence and repair
receipt retain the original call's fence and producer; the current lease advances
from its latest fence. Attempt counts and ceilings remain unchanged.

Provider preflight shares a bounded 25-second response window with provider session
issuance. Only read-only authority checks may retry a transport failure, within one
total 25-second deadline. Credential issuance and mutations are never retried by
this transport. A worker exit before recording any provider intent uses ordinary
expired-lease recovery; it is not permission to replay a partial apply.

## Observe and prepare

The installation owner's `planning_recovery.record(..., partial_apply=True)`
requires the original source descriptor and historical source tree, original pod
audit, original retained PVC/PV, a completed inert read-only probe, current
installation enrollment and maintenance receipt. `partial_apply_probe` reads only
known failed-apply diagnostics and the original proposal's hashes. It does not
read credentials or execute providers. Backend inspection records the version,
checksum, lineage, serial, all retained identities and exact unassociated EIP.
The protected `sppa1:` record lasts at most one hour. Generate it only when the
runtime is ready to complete the procedure; expiry never authorizes execution.

The authenticated original requester uses:

1. `POST /workspaces/{workspace_id}/partial-apply/preparation/preview`.
2. The ordinary operation approval issue/decision endpoints for the returned exact
   `approval_request`. This authorizes preparation only, with zero infrastructure
   resource/cost reservation and the original workspace cap retained.
3. `POST /workspaces/{workspace_id}/partial-apply/preparation` with the returned
   request ID, revision and decided approval ID.

Concurrent admissions and retries after a lost registration response find the
same committed request. A separately registered ancillary operation performs a
fresh native plan with its normal scoped provider session. It verifies the exact
backend before and after planning. The ordinary native guard checks ownership;
a further repair guard requires a complete/applyable plan, every retained ID
unchanged, and only the exact tainted EIP's delete/create replacement. The proposal
cannot use ordinary lifecycle continuation to authorize its own apply.

## When an unstarted preparation's proof expires

Never edit or extend the protected record. Once it expires, the installation owner
may perform a full new inspection with
`planning_recovery.record(..., partial_apply=True, fresh_inspection=True)` under a
new maintained run. Its immutable version-2 record includes that run's inspection
ID and retains the one-hour maximum lifetime. Pass its `record_id` as
`owner_evidence_id` in both the preparation preview and admission bodies. The
requester must obtain a new exact preparation approval; old approval decisions do
not authorize this request.

Succession is refused if any previous preparation has a still-fresh proof, a live
lease, a provider intent, an artifact, or missing registered provenance. At most
five inspection admissions are allowed for the original apply. Every prior
admission, attempt counter and immutable proof remains retained; the original
paid apply, budget, workspace and infrastructure state remain unchanged. A started
preparation requires recovery of its actual execution, not a new inspection.
Gateway independently checks predecessor admissions before issuing authority.
The repair review selects the exact proof bound to the successful artifact.

## Review and resume the original paid apply

Use the `partial-apply/repair/review`, `/decision`, and `/resume` endpoints with the
fresh preparation artifact and current original operation version. The original
selected human makes the decision; the recorded distinct-approver policy still
applies. Gateway independently checks immutable artifact identity, actual
successful preparation, current humans, original budget, expired source grants
and every issued session. Harness serializes recovery under operation/lease locks,
retains the failed call, and creates exactly one receipt and replacement key.

The worker verifies that receipt and applies the new exact plan in a separate
repair directory. It verifies the versioned state immediately before applying.
Terraform's saved-plan/state and native backend/ownership guards remain active.
It preserves the original source-artifact lineage and records the repair artifact
separately. After apply, all retained IDs except the EIP must still exist, and a
fresh AWS read must show the replaced EIP absent. Inventory sealing must account
for the completed repair; the earlier failed call is excluded only by the verified
receipt. Lost replies use the stored repaired result and fresh observations.

Continue through ordinary bootstrap approval and observation. Report recovery
only after the real UI independently shows all readiness checks Ready and a
fresh healthy heartbeat. EKS ACTIVE or a successful repair receipt alone is not
workspace readiness.
