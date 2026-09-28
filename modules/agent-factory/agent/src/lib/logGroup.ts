/**
 * Resolves the agent worker's primary CloudWatch log group name (issue #4221).
 *
 * Previously every agent entrypoint hardcoded `/github-ccsdk-agent/logs`, a group
 * that exists in no account and that no Terraform resource creates. The workers
 * call CreateLogStream against it, get ResourceNotFoundException, and leave
 * `cwInitialized` false — so `log()` never buffers and PutLogEvents is never even
 * attempted. The only trace was a single swallowed console.warn on pod stdout,
 * which KEDA garbage-collects with the pod. Net effect: the hosted path's primary
 * structured log stream was silently discarded.
 *
 * The group is now Terraform-managed and env-scoped
 * (`aws_cloudwatch_log_group.agent_logs` in webhook-ingress/infra/cloudwatch.tf),
 * following the precedent set for bootstrap logs in #4028: env-less names cause
 * staging/prod to write into the dev group, and env-scoped IAM then silently
 * denies outside dev.
 *
 * The `ENVIRONMENT` → `ENV` → `dev` fallback chain deliberately mirrors
 * `agent-worker-image/entrypoint.py:488` and
 * `agent-worker-image/lib/bootstrap_logger.py:174` so a pod resolves the same
 * environment for both log groups. The KEDA ScaledJob pod template sets
 * ENVIRONMENT explicitly (scaledjob.tf); the ARC workflow path does not, and
 * relies on the `dev` default — `runner-iam/main.tf` already grants the runner
 * role `/adp/*`, so that path needs no IAM change.
 */

/** Resolve the deployment environment name, matching the Python workers' chain. */
export function resolveEnvironment(env: NodeJS.ProcessEnv = process.env): string {
  return env.ENVIRONMENT || env.ENV || 'dev';
}

/**
 * Resolve the env-scoped primary agent log group name.
 *
 * Must stay in sync with `aws_cloudwatch_log_group.agent_logs` in
 * `modules/agent-factory/webhook-ingress/infra/cloudwatch.tf` and with the
 * `CloudWatchLogGroups` IAM statement in `scaledjob-iam.tf`. A mismatch between
 * this name and the grant is the exact defect #4221 fixes, and it fails silently.
 */
export function resolveAgentLogGroup(env: NodeJS.ProcessEnv = process.env): string {
  return `/adp/${resolveEnvironment(env)}/agent-factory/agent`;
}
