import { createSdkMcpServer, tool } from '@anthropic-ai/claude-agent-sdk';
import { execFile } from 'node:child_process';
import { z } from 'zod';

const name = 'pipeline-actions';
const workflow = z.string().regex(/^[A-Za-z0-9][A-Za-z0-9_.-]*\.ya?ml$/);
const run_id = z.number().int().positive();
const waitSeconds = z.number().int().min(1).max(300).default(300);

export const operationsStoryToolNames = ['actions_runs', 'actions_run', 'actions_wait', 'actions_logs', 'actions_dispatch', 'actions_rerun', 'repair_tasks', 'repair_progress', 'repair_files', 'repair_read', 'repair_write', 'repair_replace', 'repair_publish', 'repair_status', 'repair_wait', 'repair_merge'] as const;

function deploymentPrerequisite(text: string): string | null {
  const board = JSON.parse(text).operations_tasks;
  if (!Array.isArray(board?.cycles)) throw new Error('Operations repair history unavailable');
  const published = new Map<number, string>();
  const merged = new Set<string>();
  for (const cycle of board.cycles) {
    if (!Array.isArray(cycle.evidence)) throw new Error('Operations repair history unavailable');
    for (const evidence of cycle.evidence) {
      if (evidence.kind === 'operations_repair_pr') published.set(evidence.pull_number, evidence.head);
      if (evidence.kind === 'operations_repair_merge') merged.add(`${evidence.pull_number}:${evidence.head}`);
    }
  }
  const pending = [...published].filter(([pull, head]) => !merged.has(`${pull}:${head}`))
    .map(([pull_number, head]) => ({ pull_number, head }));
  return pending.length ? JSON.stringify({
    error: 'repair_merge_required', pending_repairs: pending,
    next_action: 'Use repair_status to identify the unmet check or branch condition. Resolve it, use repair_wait for pending checks, then repair_merge the exact head. Deployment cannot include an unmerged repair.',
  }) : null;
}

function progressState(operation: string, text: string): { terminal: boolean; fingerprint: string } {
  const result = JSON.parse(text);
  if (result.error) return { terminal: true, fingerprint: text };
  if (operation === 'actions_wait') {
    const run = result.run;
    if (!run || !['queued', 'waiting', 'pending', 'requested', 'in_progress', 'completed'].includes(run.status)) {
      throw new Error('Workflow observation unavailable');
    }
    return { terminal: run.status === 'completed', fingerprint: JSON.stringify([run.id, run.run_attempt, run.status, run.conclusion]) };
  }
  const repair = result.repair;
  if (!repair || typeof repair.ready !== 'boolean' || typeof repair.merged !== 'boolean') throw new Error('Repair observation unavailable');
  return {
    terminal: repair.ready || repair.merged || repair.status === 'failure' || repair.status === 'error'
      || repair.checks?.some((check: { status?: string; conclusion?: string }) =>
        check.status === 'completed' && ['failure', 'cancelled', 'timed_out', 'action_required'].includes(check.conclusion || '')),
    fingerprint: JSON.stringify([repair.head, repair.base_head, repair.ready, repair.merged,
      repair.status, repair.status_contexts, repair.checks, repair.blockers]),
  };
}

export async function invokeOperationsStoryTool(operation: typeof operationsStoryToolNames[number], args: object): Promise<string> {
  if (operation === 'actions_wait' || operation === 'repair_wait') {
    const parsed = (operation === 'actions_wait'
      ? z.object({ run_id, seconds: waitSeconds }).strict()
      : z.object({ pull_number: run_id, seconds: waitSeconds }).strict()).parse(args);
    const read = operation === 'actions_wait' ? 'actions_run' : 'repair_status';
    const scope = 'run_id' in parsed ? { run_id: parsed.run_id } : { pull_number: parsed.pull_number };
    const deadline = Date.now() + parsed.seconds * 1000;
    let result = await invokeOperationsStoryTool(read, scope);
    const initial = progressState(operation, result);
    if (initial.terminal) return result;
    while (Date.now() < deadline) {
      await new Promise(resolve => setTimeout(resolve, Math.min(30000, deadline - Date.now())));
      // Each read rechecks live gateway authority. No mutation, cached
      // credential or additional model turn is needed while nothing changes.
      result = await invokeOperationsStoryTool(read, scope);
      const current = progressState(operation, result);
      if (current.terminal || current.fingerprint !== initial.fingerprint) return result;
    }
    return result;
  }
  const payload = JSON.stringify(args);
  if (Buffer.byteLength(payload) > (operation.startsWith('repair_') ? 65536 : 8192)) throw new Error('Actions request exceeds limit');
  if (operation === 'actions_dispatch' || operation === 'actions_rerun') {
    const blocked = deploymentPrerequisite(await invokeOperationsStoryTool('repair_tasks', {}));
    if (blocked) return blocked;
  }
  return new Promise((resolve, reject) => {
    const child = execFile('python3', ['-m', operation.startsWith('repair_') ? 'lib.operations_repair' : 'lib.actions_broker', operation], { timeout: 45000, maxBuffer: 256 * 1024 }, (error, stdout) => {
      if (error) reject(new Error('Gateway Actions operation refused or unavailable'));
      else resolve(stdout);
    });
    child.stdin?.end(payload);
  });
}

export function operationsStoryTools(observe?: (operation: string, text: string) => void) {
  const call = async (operation: typeof operationsStoryToolNames[number], args: object) => {
    try {
      let text = await invokeOperationsStoryTool(operation, args);
      const result = JSON.parse(text);
      if (operation === 'repair_tasks') observe?.(operation, text);
      const rejectedMerge = operation === 'repair_merge' && !result.error && result.merge?.merged !== true;
      if (rejectedMerge) text = JSON.stringify({ ...result,
        error: 'repair_merge_not_completed',
        next_action: 'The repair is still unmerged. Read its blockers with repair_status; fix failed checks or use repair_wait for pending checks, then retry repair_merge. Do not dispatch a deployment yet.',
      });
      return { content: [{ type: 'text' as const, text }], isError: Boolean(result.error || rejectedMerge) };
    } catch {
      return { content: [{ type: 'text' as const, text: JSON.stringify({
        error: 'operations_tool_unavailable', operation,
        next_action: 'Read repair_tasks and the current PR/workflow before retrying. A failed or lost tool response does not establish that a merge or deployment was completed or never sent.',
      }) }], isError: true };
    }
  };
  const server = createSdkMcpServer({ name, version: '1.0.0', tools: [
    tool('repair_tasks', 'Read durable task progress, PRs and deployment receipts before resuming work.', {}, args => call('repair_tasks', args)),
    tool('repair_progress', 'Record a repair stage. diagnosis_code is accepted only with stage diagnose. cycle is the repair cycle within this attempt, default 1. This is not runtime proof.', { cycle: z.number().int().min(1).max(1000).optional(), stage: z.enum(['diagnose', 'repair', 'test', 'publish', 'deploy', 'verify']), state: z.enum(['in_progress', 'done', 'blocked']), diagnosis_code: z.string().regex(/^[A-Za-z0-9_.-]{1,64}$/).optional() }, args => call('repair_progress', args)),
    tool('repair_files', 'List current repair workspace files.', { prefix: z.string().optional(), offset: z.number().int().min(0).optional(), limit: z.number().int().min(1).max(100).optional() }, args => call('repair_files', args)),
    tool('repair_read', 'Read up to 8 KiB of source and its full file digest. Continue at next_offset (characters), passing expected_sha256 to reject changed files. Files up to 256 KiB are supported.', { path: z.string().min(1), offset: z.number().int().min(0).optional(), limit: z.number().int().min(1).max(8192).optional(), expected_sha256: z.string().regex(/^[a-f0-9]{64}$/).optional() }, args => call('repair_read', args)),
    tool('repair_write', 'Write a focused repair with its expected file digest.', { path: z.string().min(1), content: z.string().max(32768), expected_sha256: z.string().regex(/^[a-f0-9]{64}$/).nullable() }, args => call('repair_write', args)),
    tool('repair_replace', 'Replace one unique exact text fragment in a file using its full digest from repair_read. Preserves the rest of a large file; gateway validates the entire proposed result before writing.', { path: z.string().min(1), old_text: z.string().min(1).max(8192), new_text: z.string().max(8192), expected_sha256: z.string().regex(/^[a-f0-9]{64}$/) }, args => call('repair_replace', args)),
    tool('repair_publish', 'Publish a repair PR; existing CI runs tests.', { title: z.string().min(1).max(240), body: z.string().min(1).max(12000) }, args => call('repair_publish', args)),
    tool('repair_status', 'Read repair PR checks and merge readiness.', { pull_number: run_id }, args => call('repair_status', args)),
    tool('repair_wait', 'Observe repair checks until their state changes, the PR becomes mergeable, or five minutes elapse. No model calls while status is unchanged.', { pull_number: run_id, seconds: waitSeconds.optional() }, args => call('repair_wait', args)),
    tool('repair_merge', 'Merge an exact repair head after checks pass, then continue deployment verification.', { pull_number: run_id, expected_head: z.string().regex(/^[a-f0-9]{40}$/) }, args => call('repair_merge', args)),
    tool('actions_runs', 'List bounded workflow runs in the assigned repository.', { page: z.number().int().min(1).max(5).optional() }, args => call('actions_runs', args)),
    tool('actions_run', 'Read one workflow run.', { run_id }, args => call('actions_run', args)),
    tool('actions_wait', 'Observe a pending deployment until its status changes, it completes, or five minutes elapse. No model calls while status is unchanged. Each read checks current gateway authority.', { run_id, seconds: waitSeconds.optional() }, args => call('actions_wait', args)),
    tool('actions_logs', 'Read the masked, bounded log tail for a run.', { run_id }, args => call('actions_logs', args)),
    tool('actions_dispatch', 'Dispatch an allow-listed workflow. The gateway binds the engine correlation, source and definition revisions itself; no inputs are accepted and the workflow defaults apply.', { workflow }, args => call('actions_dispatch', args)),
    tool('actions_rerun', 'Rerun an eligible failed correlated workflow run.', { run_id }, args => call('actions_rerun', args)),
  ] });
  return { allowedTools: operationsStoryToolNames.map(operation => `mcp__${name}__${operation}`), mcpServers: { [name]: server } };
}
