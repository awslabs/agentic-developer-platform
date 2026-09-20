# Expedited PMM rollout — dev, 2026-09-20

The operator requested PMM live without the seven-day soak on 2026-09-20.
Target: account `879318057152`, region `us-east-1`, AWS profile `embark1`.
This supersedes the elapsed-time prerequisite in the earlier dated PMM readiness
report for this dev rollout. Profiles remain deferred.

Use immediate, bounded live verification of human ownership, per-persona model
selection, delegation, AI-DLC/replan and rollback before declaring the feature
live. Preserve accurate telemetry: absent observations remain absent, and the
credential-binding gate must not report a seven-day soak that did not happen.
The global credential-binding rollout remains separately tracked by #3186.

The shortened schedule does not remove runtime requirements. Install compatible
gateway/worker images and their authenticated authority/signing prerequisites;
verify actual provider model receipts and rollback. Do not certify success from
the UI flag alone or from report-only proposals. Keep background probes disabled
unless separately configured within the agreed test budget. Customer-linked
accounts are outside this rollout's verification scope.

The user authorized deployment and approved a $10 total model-test ceiling.
Count failed and indeterminate paid attempts and any automatically dispatched
fleet tests against that ceiling. Keep recurring paid schedules suspended.
A private ledger records reservations and receipts before each paid attempt.

## Deployment record

- Replan ownership fix #5558 merged as
  `1a5ff3e8368906270c1a7c87861a9ef164c504f3`.
- Readiness evidence repair #5570 merged as
  `7381d64db992133a3fb30883329253f4c4ebfb18`.
- Both PRs passed CI before merge. Existing gateway deployment workflows publish
  the merged code. Verify deployed images and health before activation.
- The UX change is included in this rollout branch; its existing offline
  frontend, CLI and worker checks passed in the prior verification candidate.
- Infrastructure is being planned against the existing state using retained
  upgrade inputs. No broad webhook apply or elapsed-time gate bypass is implied.

## Verified preparation

- Gateway deployment runs `35503009378` and `35503198166` succeeded. A later
  concurrent main deployment, `35504420271`, is also healthy and retains these fixes.
- Authority preparation installed 25 resources. The gateway role had exhausted
  its 10,240-byte inline-policy quota; identical scoped dispatch and task-source
  grants now use two managed policies. Twelve Terraform rollout tests passed.
- Five probe resources are installed, including a suspended CronJob and its
  dedicated service account, IAM role and immutable registry identity. Its local
  execution flag is false; gateway probe admission and budgets remain disabled.
- Worker image build `35505332757` succeeded from `ed897ee42f0e224268ee1e87ad871c7d15aa016e`.
  The probe CronJob pins
  `879318057152.dkr.ecr.us-east-1.amazonaws.com/adp-agent-runtime@sha256:6fb49790e5b5fe4d5b126e30fb2450ac6b8237f0a2beb9162c90a26da7a5897e`.
  The ordinary ScaledJob has not switched to it.
- The live Door ACL and server hashes match the current source, including
  tenant enforcement for gateway-mediated runs. Its ingress policy omitted the
  gateway. A reviewed change adding only the gateway namespace AND pod selector
  was applied; gateway probes returned health 200, unauthenticated tools 401 and
  authenticated tools 200. Eleven network-policy tests passed.
- Both EKS clusters' `aws-auth` maps contain only their node roles, with no legacy
  worker, user or account mapping. The cyber cluster was inspected through its
  existing authorized ARC runner identity. The main cluster's explicit legacy
  worker administrator access entry and IAM AdministratorAccess still need retirement
  after active legacy jobs drain.
- PR #5576 merged as `3eda9bea0680d5e8988b2c7f50257308b97b6208` after all CI
  checks passed. Gateway deployment `35506167046` succeeded. Its bundled smoke
  requests skipped for lack of a usable workflow refresh token; separate actual
  Cognito-authenticated probes verified auth/me 200, default administration 200,
  posture 200, unknown-model refusal 422 and an unchanged stored default afterward.
  All four gateway replicas were updated and available. Default remains NULL at
  revision 1; posture remains report_only at revision 1.
- The tick's authority IAM policy was applied through a separate reviewed plan
  containing exactly one create. Its deployed authority flag remains false.
- Two admission-only Jobs using the pinned probe image and its actual IRSA service
  account completed successfully with `claimed=false, reason=disabled`. Neither
  invoked an SDK or requested destination credentials. Receipts were retained and
  both temporary Jobs removed. The CronJob remains suspended.
- The existing marker-signing secret is a placeholder. It must be securely
  initialized after legacy work is drained/reconciled; no real signing key was
  replaced or exposed during preparation.
- PR #5577 merged as `a026371515d53d329ea13cf2e2d079fa7b2d9645` after all CI checks
  passed. Gateway/frontend deployment `35506917122` succeeded. The downloaded CLI
  exactly matches reviewed source (SHA-256
  `277fcd3f4493c15a4aaba687a5ffa8fb3e50e5895b296ea78633c3a0199a661e`), and the published
  frontend entry `/assets/index-CBP6e30S.js` contains the new model-choice feedback.
  Thirty UI tests, 52 CLI tests, TypeScript and focused lint passed locally.
  Authenticated admin reads after this deployment still show NULL default/revision 1
  and report_only/posture revision 1. The bundled workflow smoke skip described
  above is not counted as live acceptance; separate actual-login checks are retained.

The default operation is `GET` / `PUT`
`/api/admin/persona-models/default/claude-agent-sdk`. The PUT accepts
`canonical_model_id`, `expected_revision` and `reason`; only a platform admin can
use it. It requires fresh exact platform destination, SDK revision and request-shape
evidence with a provider request ID, and commits the change and audit together.
This prepares an activation operation; it does not assert that any live model has
been qualified or promote an unproven default.

The proposed Terraform admission-pause plan is NOT applied: its dependency graph
also proposed changing deployed webhook Lambda packages/configuration and creating
engine-signing resources. Preserve active jobs and review those changes separately.
Authority, run tasks, source-isolation assertions and PMM enforcement remain off.

## Resumed cutover preparation

- Refreshed Ada credentials and reconfirmed the approved AWS account. Paused
  only the webhook ScaledJob using the KEDA pause annotation after a server dry
  run; KEDA reports Paused=True. This narrow live change avoids the unapplied
  broad Terraform quiesce plan. No active worker or live queue messages remained.
  The 103 historical dead-letter messages are retained and were not replayed.
- A reviewed saved gateway Terraform plan added exactly a dedicated
  Bedrock-only role and policy. Only the gateway role may assume it; it has no
  secret, queue, storage or platform-administration permission. Registered it
  with an authenticated platform administrator and transactional audit after
  the existing real STS/IAM routing verifier succeeded. No account mappings changed.
- Fresh-container manifest generation exposed random SDK device IDs. Version-2
  normalization covers only the known anonymous fixed-session device field and
  existing date reminder. Models, tools, token limits, account and session
  identities remain covered. All nine manifests matched across fresh containers.
  These are fake-upstream shape checks, not paid provider evidence.

- Worker build `35512479119` and gateway release `35512616564` succeeded
  from source `5e33b0df222d808421a9b4cfe26e847ff8d71d08`. The protected
  worker's live secret/SQS/S3/IAM/EKS reads were denied and its scoped
  CloudWatch log event was written and read back. This proves those
  permissions, not complete protected-run compatibility.
- Published real GitHub/GitLab Lambda packages with revision checks and pinned
  S3 object versions. Both updates succeeded; all existing Lambda environment
  values were verified unchanged. The broader producer Terraform plan remains
  unapplied because it includes unrelated engine-signing provisioning.
- Two unrelated AI-DLC tasks (issues 5526 and 5532) arrived during preparation.
  Admissions were resumed to let them run on the legacy worker. Marker seeding
  refused the nonempty queue before writing; its placeholder remains unchanged.
- A one-off three-slot/$3 SDK qualification run found a second request-shape
  issue: the SDK inserts the configured dollar budget into its initial reminder.
  Two slots completed with local shape refusals and no provider request ID; a
  third started slot was stopped by deleting the Job. Retain $1 conservatively
  for that unresolved slot. Admission was restored to disabled and the CronJob
  stayed suspended. No successful provider invocation is claimed.
- Version-3 normalization additionally covers only the exact zero-spend initial
  budget reminder with equal total/remaining values. The SDK still receives the
  admitted budget unchanged; provider max_tokens and thinking limits remain
  fingerprinted. Captured $0.01 and $1 requests now match. Local shape refusal
  also aborts the SDK immediately instead of waiting for its timeout.
