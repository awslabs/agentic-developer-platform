/** An unknown workload deadline cannot authorize a pause. */
export function controlDeadlineAt(env: NodeJS.ProcessEnv = process.env): number {
  const deadline = Date.parse(env.ADP_POD_DEADLINE_AT || '');
  if (!Number.isFinite(deadline)) return 0;
  const credential = Date.parse(env.ADP_CONTROL_TOKEN_EXPIRES_AT || '');
  return Number.isFinite(credential) ? Math.min(deadline, credential) : deadline;
}
