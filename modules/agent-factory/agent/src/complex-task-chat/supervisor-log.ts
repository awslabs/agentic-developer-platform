/**
 * One-line JSON evidence the chat supervisor writes for every step (#8052). Lives in its
 * own module so the sandbox launcher can log without importing the admission client
 * (which imports the launcher).
 */
export function supervisorLog(runId: string, step: string, status: string, fields: Record<string, string | number | boolean> = {}): void {
  console.log(`[supervisor] ${JSON.stringify({ run_id: runId, step, status, ...fields })}`);
}
