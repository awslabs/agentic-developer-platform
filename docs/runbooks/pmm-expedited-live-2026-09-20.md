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

The user has authorized deployment. A total model-spend ceiling has been
requested and is pending; no paid test invocation is authorized by an assumed
ceiling. Record the ceiling before running the paid canaries.

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
- PR #5576 contains the gateway probe guards, audited proven-default activation
  API and Door ingress fix. It excludes worker paths that trigger the paid fleet.
  Its combined gateway checks passed 110 tests locally; live/CI status must still
  be verified before merging and activation.

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
