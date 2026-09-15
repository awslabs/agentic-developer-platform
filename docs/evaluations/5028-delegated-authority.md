# Delegated authority acceptance — #5028 / PR #5029

The isolated 2026-09-13 evaluation verified permitted AI-DLC dispatch and
cross-worker denial with real AWS and Kubernetes identities. Ordinary feature
flags remain off. This completes the authorization story; pause, resume, steer
and abort still return 501 and have no SDK implementation.

## Source and environment

- Gateway application source: `8f11df9a48bc0147213ab9e19d59433ceffbabc5`.
- Worker application source: `2285f81d47ad4ff4584e94be157ab1f4b0f7b184`.
  The subsequent service-grant fix changes gateway/infra/docs only.
- Initial gateway/worker checks used `e8abc376feab31ab788f27bdfdcbbd0c330d1d1f`.
  The final worker repeated the actual online revocation check; the final gateway
  repeated human service approval with the registered child ceiling.
- AWS account `879318057152`, region `us-east-1`; namespace
  `adp-e5028-20260913-212933`; five disposable DynamoDB tables, one FIFO queue,
  disposable PostgreSQL and a separate gateway deployment.
- Separate REST API, exact ALB rule and target group. IAM authentication supplied
  the real caller ARN. Product TokenReview verified live pod UID, service
  account, image and projected workload token. No authentication override.
- Six inert worker pods shared one role. AdministratorAccess was deliberately
  attached behind the candidate permissions boundary. The new service account
  could not assume the legacy worker role and had no EKS access entry/RBAC.
- Only dedicated image tags were built. Ordinary deployments and image tags
  were not changed. The legacy worker's administrator grants were not modified.

## Evidence by acceptance criterion

| AC | Observed result |
|---|---|
| 1 | A real Cognito user approved two isolated flows. `adp-trigger bind-wave` verified native GitHub parent relationships. Operations → coordinator → developer → reviewer dispatch succeeded; identical retries returned the same invocation. Evaluation dispatch was refused before story eligibility and succeeded afterward. Descendant monitoring returned 200. |
| 2 | A second worker sharing the IAM role could not bootstrap with another run's complete envelope, use its stolen run credential without its pod proof, or gain access by changing invocation/chain/human environment claims. |
| 3 | Actual ancestor, sibling and cross-flow requests returned 404, including flows approved by the same human. Claim/privilege-expansion bodies returned 422. Automated policy/resolver tests additionally cover existing cross-tenant targets. |
| 4 | Own status and idempotent registration succeeded. Eighteen real IAM/RBAC checks across two workers denied protected authority reads, other-run registration writes, queue injection, test-admin secret access, legacy-role assumption, signing Secret reads, legacy-SA TokenRequest and boundary removal. Twenty-two IAM simulations also passed. |
| 5 | Sixteen listener HTTP cases passed: bad signatures, issuer/audience/target/generation/action/command/body bindings and expiry were refused; identical retry preserved its recorded outcome and conflicting content returned 409. The ordinary listener returned 501. |
| 6 | Real 300-second invocation refresh preserved attempt 1. Listener token rotation advanced credential epoch 1 → 2 while retaining generation 1, process PID and the pending journal entry. Grant revocation prevented root/descendant renewal and dispatch. A fresh queued proof was refused by the final worker's real online request in 455.7 ms; the journal recorded `rejected`, identical retry remained `replayed`, and handoff count stayed zero. A separate SQL-halted flow renewed immediately before cancellation and refused renewal/dispatch afterward. |
| 7 | Service actors could not advance an awaiting human gate; the rejection was persisted. Human service approval refused child personas outside registration and accepted the permitted subset. Five real DynamoDB cases verified absent/narrow scope refusal, permitted scope and registration races. Structured audit records contain principal, authority reference, target, action and outcome; invalid authentication is recorded as unverified. |
| 8 | Worker-to-worker TCP connections timed out in three directions while the fixture gateway successfully reached the listener. Permitted dispatch and denied attacks used real workers, IAM and TokenReview. Cleanup and immutable image details are recorded alongside the sanitized results. |

## What the fixtures do and do not establish

The human approval used a normal temporary Cognito user. Its org-admin
membership existed only in isolated PostgreSQL. The existing installation
ownership row was read and copied into that database; no production user,
installation or membership was modified. PostgreSQL schema was created from
SQLAlchemy metadata, so this is not a production migration rehearsal.

The two Operations roots were provisioned by trusted fixture setup using the
actual human approval/genesis and protected bootstrap store. Subsequent bindings,
dispatch, bootstrap, monitoring, status, registration, renewal and credential
broker requests used product code and real authenticated HTTP. The queue had no
KEDA consumer, so no actual story developer/Operations agent was launched.
Fixture completion used the guarded SQL transition to make evaluation eligible;
it does not represent SDK task execution.

The listener's dormant assertion/journal path was tested by configuring its
existing component harness with a supported `pause` admission. No pause adapter
or SDK effect was installed. The product gateway and ordinary listener continued
to return 501. The queued revocation check exercised actual HTTPS/SigV4 and the
journal guard; successful SDK control delivery remains the responsibility of
each control-verb story.

Forwarding assertions expire after 30 seconds. Queued delivery requires an
uncached online check and a synchronous handoff within one second of starting
that check. An outage, stale proof, revocation or cancellation prevents handoff;
it does not undo an effect already delivered. Credentials renewing during a
live pod do not provide durable multi-day task recovery or a persistent journal.

The existing privileged legacy workers must be drained and their access handled
before claiming deployment-wide isolation. Authority workers use a distinct
service account/role with a mandatory boundary. Legacy shared-key integrations
excluded by that boundary need their documented broker migration before enablement.

## Validation

The final service-grant change passed 723 gateway authorization/dispatch tests
(4 PostgreSQL-only cases skipped in that run), including all 37 service-authority
tests. The earlier isolated PostgreSQL run passed 51 graph/wave/revalidation
cases, including the lock/concurrency cases. The final Node transport change
passed 212 targeted tests and TypeScript compilation; the broader worker suites
were also green before that focused change. Both Terraform stacks validated,
and flag-on/off isolation plans passed. No security baseline or suppression was
changed.

The sanitized JSON record contains the measured HTTP/IAM/renewal/journal and
cleanup results. CI/security results and resolution of the formal review are
linked in the PR; security findings are compared with current main rather than
reported as a clean repository scan.

After this live evaluation, main merged #5066's UI engine execution changes.
The integration preserves the committed engine run ID and timestamp across SQL,
protected authority, reporting and SQS. The reporting row is created atomically
with the grant, and an identical retry preserves its terminal status. Engine
evaluations receive monitoring-only authority. Automated integration tests cover
both story/evaluation dispatch through result observation and refusal of changed
run/node/attempt/flow/approval/persona claims. These are subsequent automated
checks, not additional live acceptance or a new deployment.

## Cleanup record

All owned workers/PostgreSQL, the fixture gateway, namespace, policies, keys,
five tables, FIFO queue, IAM role/boundary, temporary gateway IAM grant, API/ALB
resources, Cognito user, dedicated image tags and source archives were deleted
and verified absent. The owned SSM session and port-forward were stopped; ports
18564 and 18566 had no listener. Build/CI logs remain as audit history.

See [sanitized machine-readable evidence](5028-delegated-authority.json).
