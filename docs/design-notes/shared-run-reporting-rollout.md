# Shared-role engine reporting rollout

The engine can acknowledge developer and reviewer reports while protected IAM
execution authority remains disabled. It reuses the current worker IAM role and
GitHub App. Reporting authenticates a server-created SQL assignment; it does not
create separate developer/reviewer accounts or new worker roles.

Reporting activation is explicit. `shared_run_reporting_enabled` and
`shared_worker_continuation_enabled` in webhook-ingress Terraform both default to
`false`. The worker gateway ConfigMap and the existing non-secret worker-runtime
SSM wiring carry these values. Gateway Terraform consumes that wiring on its next
apply to configure the tick. Never use key presence as the activation switch.

Deploy migration 063 then 064 and compatible gateway and worker images first.
Verify the deployed worker image digest matches the reviewed release and contains
the reporting client and model proxy header support, and that `AGENT_RUN_LOGS_BUCKET` is configured. The existing
`agent-authority-signing/run-credential-key` is mirrored into an encrypted SSM
parameter owned by webhook-ingress; no secret value appears in outputs. The tick
receives only its parameter name and permission to read that exact parameter,
using the existing encryption key. Gateway pods retain their existing Secret
reference. Verify both readers can load the same key before enabling reporting.

After reviewing the infrastructure plan, enable `shared_run_reporting_enabled`
and apply webhook-ingress then gateway Terraform so both gateway and tick receive
`ADP_SHARED_RUN_REPORTING_ENABLED=true`. Restart gateway pods after ConfigMap or
key changes: updating an `envFrom` source does not update a running process. The
webhook rollout performs this restart; verify the resulting pod revision. Reapply
the gateway stack after webhook variable changes so the tick consumes the updated
SSM wiring. Verify a scoped story dispatch produces
one report assignment, a worker start receipt, an acknowledged PR binding and a
terminal receipt. Only then enable `shared_worker_continuation_enabled` for an
explicitly accepted flow continuation. `AGENT_WORKER_ROLE_ARN` always names the
existing shared worker role. These steps do not enable protected authority.

The dispatcher stores no raw capability in decisions or SQL metadata. It can
reconstruct the identical queue envelope after a publication failure, keeping the
same invocation, node attempt and SQS deduplication key. The worker uses the
existing logs bucket for an untrusted pending-report spool and records its start
before invoking the model. A redelivery with a candidate retries only reporting;
an interrupted start with no recoverable candidate requires an explicit recovery
decision. It never silently launches development again.

Legacy result reconciliation uses a valid current-attempt SQL terminal receipt
when the dispatch has a report assignment. Missing receipts remain unfinished,
even if advisory DynamoDB status says complete. Delivered code still requires
verified binding, merge, CI and accepted review evidence.

Disabling the reporting flag stops new report assignments and outbox publication;
it does not erase existing receipts or make them advisory. Keep the matching
signing key and artifact bucket available for in-flight acknowledgements. Key
rotation cannot reconstruct envelopes signed with the old key; drain or explicitly
recover outstanding assignments before rotating it.

When the webhook infrastructure hold is still active, targeting
`null_resource.keda_scaledjob` can pull unrelated resources and destructive
changes into its dependency graph. Do not apply that expanded plan. A scoped
prerequisite plan can instead target `aws_ssm_parameter.agent_run_reporting_key`,
`aws_ssm_parameter.worker_runtime_wiring`, and
`kubernetes_config_map.worker_gateway[0]`; inspect every resulting action.
Gateway restart and tick configuration still require their separate verified
steps above.

For the worker template, `scripts/plan-shared-worker-rollout.py` under
`modules/agent-factory/webhook-ingress` prepares an optimistic-concurrency JSON
patch from a live ScaledJob snapshot, the verified ECR digest, and the existing
trusted gateway and GitLab SSM URLs. It changes only the worker image and those
two environment entries. It preserves the service account, admission pause,
existing Jobs, and all other configuration. It does not apply the patch. Review
the generated patch, then use `kubectl patch --type=json --patch-file=...` on
that ScaledJob; a changed resource version requires a fresh snapshot and review.

The same command updates a supplied Terraform JSON overlay with `agent_image`
and `gitlab_webhook_enabled`. Keep that overlay in the matching
`environments/<environment>/modules/webhook-ingress.tfvars.json` so a later
managed rollout retains the image and existing GitLab integration. URLs remain
deployment-owned SSM inputs, and reporting/continuation flags remain explicit
separate rollout decisions. This scoped patch does not remove the deployment
hold or establish whole-module Terraform convergence.
