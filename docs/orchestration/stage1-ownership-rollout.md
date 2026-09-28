# Stage 1 ownership rollout and acceptance

Scope: #5127/#5161 (shared ownership) and #5128 (accepted execution policy),
coordinated by #5134. Target: existing embark1 dev, AWS account `879318057152`,
region `us-east-1`, registered connection `adp-embark1`. Stage 2 and Q1 are outside
this work. Code merge, deployed revision and feature acceptance are separate
milestones. This document records a rollout contract, not a live PASS.

## Launch paths and trusted identity

| Launch path | Ownership boundary | Trusted work identity |
| --- | --- | --- |
| Engine tick | `_dispatch_one` claims inside the SQL transaction before consuming the attempt; publication follows commit | Tenant installation and metadata-only GitHub lookup resolve the immutable repository ID; owner is the approved flow |
| Direct GitHub dispatch, including mentions/labels and CLI webhook entry | `publish_envelope` calls `/internal/v1/agent/work/admit` before sending SQS | Verified webhook writes protected execution/grant; gateway reads tenant, repository ID, issue and owner from those records |
| Scheduled service dispatch | Same protected producer admission | Scheduled root with issue zero owns no issue; repository ID is resolved for subsequent issue-bearing children |
| Delegated dispatch | Protected command reservation followed by shared admission before SQS | Child inherits immutable repository ID and flow from its protected parent |
| Delegated graph assignment | Shared admission is part of the SQL node transition; a conflict does not consume an attempt | Existing graph assignment and human decision checks remain authoritative |
| Worker bootstrap and refresh | Verify the pod and protected execution; require the exact held invocation before returning a credential | No tenant, issue, owner or generation is accepted from worker request fields |
| Worker terminal report | Release after the existing authenticated terminal transaction | Verified current attempt, pod and credential; stale callbacks cannot release a later generation |
| Crash cleanup | Bounded gateway maintenance, using positive exit evidence for the exact protected pod UID | An expired lease, missing pod or failed Kubernetes request is never exit evidence |

GitLab currently publishes an acknowledgement envelope, not GitHub issue work;
its existing path is excluded. Knowledge ingestion uses a separate queue and
asset contract. No alternate ownership or promotion endpoint is added.

The producer endpoint requires a signed STS `GetCallerIdentity` proof bound to
one invocation. The gateway sends a fixed operation to a fixed regional STS TLS
endpoint and checks the returned role against `ADP_WORK_CLAIM_PRODUCER_ROLES`.
A shared internal API key, claimed role header or valid worker AWS identity does
not authenticate the webhook producer. HTTP input cannot choose an owner or
request a release/handover.

## Sequential handoff and failure behavior

One issue has one active admitted invocation. A direct child of the current
owner may be queued with protected `work_claim_deferred_from` metadata. Its
bootstrap returns HTTP 425 while the parent holds the claim, without binding a
pod or returning an action credential. The worker waits before repository
execution. After release it acquires the next generation under the same lane.
Independent owners are refused rather than queued as children.

Startup waiting is bounded to 30 minutes. The gateway conditionally cancels an
unstarted execution before releasing its claim/reservation; a simultaneous
bootstrap wins the same protected status condition and prevents that cleanup.
Delayed messages cannot bootstrap a cancelled execution. Claims with missing
protected execution data remain unresolved for explicit reconciliation.

A definitive delegated admission refusal fences the protected command and
pending child, then releases its concurrency slot idempotently. It preserves the
historical dispatch-attempt count. Once a publisher crosses the `publishing`
fence, an unknown SQS outcome retains the reservation and requires the same
request ID. It is never treated as proof that no message arrived.

The cleanup pass reads external evidence before taking any claim row locks,
uses generation fencing, and paginates past live claims. A cancelled execution
with a previously bound pod still needs positive exit evidence. Normal terminal
reports record their actual outcome; failed/cancelled runs are not labelled as
successful completion.

A child that never obtains a claim and disappears before contacting bootstrap
can retain a pending protected dispatch reservation. It does not own or block
the issue. Existing dispatch reconciliation must resolve that intent; a missing
worker is not automatically credited as a completed run.

## Deployment configuration

All ownership enforcement defaults off. Producer, gateway and worker revisions
must be compatible before any cohort is opted in.

| Component | Required configuration |
| --- | --- |
| Gateway | `AGENT_AUTHORITY_ENABLED=true`, protected table/key material, approved worker image digests and service account; `ADP_WORK_CLAIMS_ENABLED=true` |
| Gateway producer authentication | `ADP_WORK_CLAIM_PRODUCER_ROLES=arn:aws:iam::879318057152:role/adp-dev-webhook-lambda-role` |
| Webhook | Protected authority enabled, `ADP_WORK_CLAIMS_ENABLED=true`, `ADP_AGENT_CONTROL_ENDPOINT=<verified API Gateway stage URL>/internal/v1/agent` |
| Tick | Protected authority enabled, protected table/events/KMS permissions, `ADP_WORK_CLAIMS_ENABLED=true`, explicitly selected dispatch repository and accepted fixture flow |
| Worker | Protected worker service account/permissions boundary, approved immutable image, projected bootstrap audience token, control endpoint and `ADP_AGENT_AUTHORITY_ENABLED=true` |

`gateway-deploy.yml` reads the ownership switch and producer role list from
`/adp/dev/gateway/work-claims-enabled` and
`/adp/dev/gateway/work-claim-producer-roles`. Absent values disable admission.
The worker refuses a claim-required envelope on a legacy bootstrap path.
Lambda/tick configuration and the ScaledJob must be recorded in the scoped
rollout manifest and read back after deployment; changing a gateway flag alone
cannot enable Stage 1 correctly.

Follow `docs/adp-platform-deployment/deploy-with-agent.md`. Verify actual account
identity before writes. Do not run a broad Terraform apply or widen the EKS
endpoint allowlist. The existing ARC runner can reach this cluster.

Before opt-in, inventory pending queue deliveries, running jobs and existing
claims. Reconcile their ownership and versions; do not automatically adopt
current New UI stories. Preserve the shared release coordination in #5105.
Apply migration 050 only if a readback shows it missing, using the existing
migration workflow against the release image. No new schema is introduced here.

## Observed prerequisite gap — 2026-09-15

[Read-only inventory and targeted plan](https://github.com/aws-e/adp/actions/runs/34938324082)
ran as the existing deployment runner in account `879318057152`. It observed:

- Gateway image `d2fbf63683138d8572d1e38f5a6679de4736181a`.
- Gateway authority disabled; worker image digest allowlist `disabled`.
- `agent-authority-signing` secret absent.
- Worker ScaledJob using `adp-agent-runtime:latest` with no authority flags.
- Gateway dispatch repository empty.
- Authority table already present.

The **plan-only** step found 13 creates from existing authority definitions:

- `aws_iam_policy.agent_authority_boundary[0]`
- `aws_iam_role.agent_authority_worker[0]`
- `aws_iam_role_policy.agent_authority_worker[0]`
- `kubernetes_cluster_role.gateway_agent_tokenreview[0]`
- `kubernetes_cluster_role_binding.gateway_agent_tokenreview[0]`
- `kubernetes_config_map.agent_control_verification_keys[0]`
- `kubernetes_role.gateway_agent_pod_read[0]`
- `kubernetes_role_binding.gateway_agent_pod_read[0]`
- `kubernetes_secret.agent_authority[0]`
- `kubernetes_service_account.agent_authority_worker[0]`
- `random_password.agent_run_credential[0]`
- `tls_private_key.agent_control_envelope[0]`
- `tls_private_key.agent_control_envelope_secondary[0]`

No resources were applied or enabled. Issue #5161's deployment scope says
**“No IAM change.”** Creating these prerequisites therefore needs a recorded
scope amendment. The user was asked to approve this concrete prerequisite plan.

This is a bootstrap plan, not a complete activation plan. Further read-only
inspection found the existing gateway-authorized-dispatch policy and tick
protected-authority policy absent, and IAM simulation returned `implicitDeny`
for the webhook role invoking `POST /internal/v1/agent/work/admit`. The latter
needs a grant restricted to that actual API/stage/method/path. Worker registry,
SSM configuration, token scope and the live execution-policy prerequisites
#4539/#4898 also need readback before activation. Approval of the 13 creates does
not establish those later conditions or authorize a broad apply.

## Live acceptance evidence

Retain the exact reviewed PR head, merge SHA, each running image/Lambda revision,
fixture tenant/repository ID/issue/flow, accepted policy version, admission and
invocation IDs, claim generations, terminal evidence and cleanup result.

| Gate | Required evidence |
| --- | --- |
| A0 collision | Real engine and webhook launches against one disposable issue; exactly one admitted invocation, auditable competing refusal, surviving run completes and releases |
| A0 positive handoff | Developer → reviewer → repair in one lane, successive generations, no overlapping work credentials |
| A0 isolation/recovery | Repository rename retains immutable binding; other tenant cannot affect claim; worker crash releases only with positive exit evidence |
| A1 policy boundaries | Current membership, org/team/repository/environment scope, expiry, revocation and accepted version control the next action |
| A1 shared allowance | Parallel children and restarts retain one budget; unknown usage blocks new spend until reconciled |
| A1 human decisions | Accepted summary matches policy; agent cannot accept policy or clear a human-only gate |
| A1 permitted/denied live action | A real permitted evaluation succeeds; unauthorized target/action is refused with no broad credential fallback |

Every live gate remains **NOT RUN** until its evidence exists. Ordinary
Cognito smoke, local STS verification, mock tests and successful image builds
cannot replace it. Stage 1 is complete only after review, CI, deployment,
compatible cohort acceptance and scoped cleanup.

Rollback stops new autonomous admissions, reconciles pending effects and turns
off ownership admission across the cohort. Keep claim rows, protected receipts,
accepted plan versions and policy evidence. Restore pre-rollout configuration
from the recorded manifest; do not drop migration 050 or revoke in-flight worker
identity before resolving its effects.
