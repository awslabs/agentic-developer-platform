import { writeFileSync, renameSync } from 'node:fs';

/** Capture the original cause before cleanup logs or process.exit obscure it. */
export function writeFailureReport(error: unknown): void {
  const path = process.env.ADP_FAILURE_REPORT_FILE;
  if (!path) return;
  let message = error instanceof Error ? error.message : String(error);
  for (const [name, value] of Object.entries(process.env)) {
    if (/TOKEN|SECRET|PASSWORD|PRIVATE_KEY|CREDENTIAL/i.test(name) && value && value.length >= 8) {
      message = message.split(value).join('[redacted]');
    }
  }
  message = message.replace(/Bearer\s+\S+/gi, 'Bearer [redacted]')
    .replace(/(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+)/g, '[redacted]');
  try {
    writeFileSync(`${path}.tmp`, JSON.stringify({ status: 'agent_failed', error: { message: message.slice(0, 8192) } }), { mode: 0o600 });
    renameSync(`${path}.tmp`, path);
  } catch {
    console.error('[worker] Failure diagnostic could not be persisted');
  }
}
