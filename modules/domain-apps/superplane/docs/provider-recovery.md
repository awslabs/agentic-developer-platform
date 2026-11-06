# Recover planning rejected before credential delivery

This workflow resumes the same original workspace request and operation. It
handles only the initial dedicated AWS `prepare-infrastructure` call using the
installation-managed provider broker. An accepted session, existing artifact,
provider report/resource, active original grant, cancellation, cleanup, closed
lease, terminal operation, exhausted attempts or a previous credential retry
refuses a new recovery. Other interrupted phases retain their existing observation
and cleanup requirements.

Use the workspace details recovery control, or the authenticated API:

1. POST `/workspaces/{workspace_id}/provider-recovery/review` with the original
   operation's observed `expected_version` and a durable UUID `idempotency_key`.
2. Review the unchanged original request and spend limits. The originally selected
   human approver POSTs `/decision` with the returned `reauthorization_id` and
   explicit `result: allowed-once`. The recorded separation policy still applies.
3. The original requester POSTs `/resume` with that decision ID. The decision must
   leave more than 900 seconds for an AWS session. Review again with a fresh key
   if a pending decision expires; never change an old decision's expiry.
4. Observe the original operation through planning, then approve its ordinary
   lifecycle continuations. A recovery receipt alone does not establish readiness.

## Credential owner evidence

The producer invokes the protected Gateway `recover-uncredentialed` endpoint.
It rechecks the real current humans, original confirmed reservation and the fresh
`uncredentialed-planning` decision. The existing undispatched decision cannot
substitute for this scope.

While holding the operation row and provider-dispatch advisory lock, Gateway
consistently reads every possible prior execution mapping, including bounded
bootstrap retries. The original lease's producer must be present and agree with
its protected execution metadata; every prior execution grant must have expired.
The reserved provider broker commits `paid_domain_provider_session_issued` before
returning credentials and checks authority again afterward. The recovery owner
queries that persisted audit for the exact original operation. Any issued-session
entry refuses recovery, including one whose response may have been lost. A
still-running STS request cannot deliver credentials after its grant expires.
No raw credential, operator log assertion, empty resource list or worker report
is accepted as proof. Vault-role operations do not have this recovery path.

## Preserved history and one bounded retry

Harness checks the exact original single intent and expired execution lease.
Within one transaction it reconciles that intent as rejected before credentials,
records an immutable proof/decision receipt and new key, advances the lease fence
and releases its holder. The original intent row/key/producer and all admission
and approval identities remain. Attempt counts, the attempt ceiling, original
approval expiry and the delivered outbox are unchanged. The existing dispatcher
picks up the same running operation under the new fence and fresh decision.

Only the receipt-bound replacement is eligible for execution. Plan completion and
recovery exclude the predecessor only while its exact reconciled refusal and
receipt are intact. A duplicate resume returns the same receipt; another decision
cannot produce another retry cycle. SQL reset, deletion of an intent, replacement
workspace admission and expiry edits are not part of this workflow.

## Coordinated maintenance

Pause worker launch and API dispatch under the maintained installation lock.
Deploy Harness schema 12 and domain migration `047_provider_recovery` through the
schema owners, with compatible API, worker, Gateway and tick artifacts. The
app-owned API producer role adds only the exact protected recovery POST ARN;
review its saved Terraform plan and preserve the role identity/trust and existing
routes. Publish the three public routes, UI contract and qualified UI together.
Update the installed worker/schema binding and renew provider authority through
its owner when that binding changes. Verify installed artifacts before unpausing.
Use the original saved workspace/demo state for recovery and continuation.

Schema downgrade refuses to erase a provider retry or scoped decision. Tests
prove refusal, replay, original identity retention and completion; deployment and
live workspace/broker readiness are separate acceptance evidence.

Expired execution messages covered by the retained credential-recovery proof can
still be waiting in the operation queue. When a verified worker receives one,
Gateway rechecks its exact protected mapping, unchanged original admission and
expired grant before acknowledging it through the normal delivery journal. The
worker then exits without acquiring a lifecycle lease. This acknowledgement
retains the original execution, approval, intent and recovery receipt; it never
acknowledges a fresh retry or another operation. A later worker can receive the
newly authorized task without expired messages repeatedly blocking the queue.
