/** Reviewed host tools. No shell, credential material or provider URL enters the SDK. */
import { createHash, randomUUID } from 'node:crypto';
import { execFile } from 'node:child_process';
import { z } from 'zod';
import type { Capability } from './persona.js';
import type { HostTool } from './tool-server.js';

export function hostCommand(file: string, args: string[], signal: AbortSignal, input?: unknown, maxBuffer = 32768): Promise<string> {
  return new Promise((resolve, reject) => {
    const child = execFile(file, args, { signal, timeout: 45000, maxBuffer,
      cwd: process.env.WORK_DIR || process.cwd(), env: process.env }, (error, stdout) => {
      // execFile errors contain stderr and arguments. Never expose them as tool output.
      if (error) reject(new Error('Host tool refused or did not complete; inspect authorized diagnostics'));
      else resolve(stdout);
    });
    child.stdin?.end(input === undefined ? undefined : JSON.stringify(input));
  });
}

const path = z.string().min(1).max(240).refine(value => !value.startsWith('/') && !value.includes('\\')
  && !value.split('/').some(p => !p || p === '.' || p === '..' || p === '.git') && !/[\x00-\x1f\x7f]/.test(value));

export interface RepositoryProgress { id: string; category: 'tool'; state: 'running' | 'completed' | 'failed' }

export function githubTools(input: { persona: string; repository: string; revision: string; capabilities: readonly Capability[];
  storyProgress?: (digest: string, updates: { id: string; status: 'open' | 'in_progress' | 'done' | 'blocked'; kind?: 'code' | 'test' | 'infra'; title?: string }[]) => { updated: boolean; [key: string]: unknown } },
  execute: (name: string, args: Record<string, unknown>, work: () => Promise<string>, signal: AbortSignal) => Promise<string>,
  progress: (text: string, detail: RepositoryProgress) => void = () => {},
  actionsRequest: (name: string, args: Record<string, unknown>, signal: AbortSignal) => Promise<string> =
    (name, args, signal) => hostCommand('python3', ['-m', 'lib.actions_broker', name], signal, args, 512 * 1024)) {
  if (!/^[a-f0-9]{40}$/.test(input.revision)) throw new Error('Invalid repository revision');
  const definitions: HostTool[] = input.capabilities.includes('repository.read') ? [
    { name: 'repository_file', description: 'Read up to 200 lines from a tracked UTF-8 file at the admitted commit. Files are limited to 1 MiB and each result to 24 KiB; narrow the range for long lines.', capability: 'repository.read', readOnly: true,
      input: z.object({ path, start_line: z.number().int().min(1).max(1048576).optional(), max_lines: z.number().int().min(1).max(200).optional() }) },
    { name: 'repository_files', description: 'List a directory at the admitted commit, at most 100 entries. Omit directory for the root; use offset for later entries. Does not recursively enumerate the repository.', capability: 'repository.read', readOnly: true,
      input: z.object({ directory: path.optional(), offset: z.number().int().min(0).max(100000).optional() }) },
  ] : [];
  if (input.storyProgress && ['operations', 'agent-codex-operations'].includes(input.persona)
      && process.env.ADP_ENGINE_OPERATIONS_STORY === 'true' && input.capabilities.includes('artifacts.publish')) {
    const progressTool: HostTool = {
      name: 'story_task_progress', capability: 'artifacts.publish', readOnly: true,
      description: 'Report story task progress using the sha256 returned by repair_tasks. Updates merge by stable ID and retain omitted tasks/evidence. New tasks require kind/title. This updates the live board only; it does not write files, grant authority or prove acceptance.',
      input: z.object({ expected_sha256: z.string().regex(/^[a-f0-9]{64}$/), updates: z.array(z.strictObject({
        id: z.string().regex(/^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/), status: z.enum(['open', 'in_progress', 'done', 'blocked']),
        kind: z.enum(['code', 'test', 'infra']).optional(), title: z.string().min(1).max(1000).regex(/^[^\r\n\x00]+$/).optional(),
      })).min(1).max(1000) }),
    };
    definitions.push({ ...progressTool, schemaDigest: createHash('sha256').update(JSON.stringify(z.toJSONSchema(progressTool.input.strict(), { target: 'draft-7' }))).digest('hex') });
  }
  if (['true', '1'].includes((process.env.ADP_ENGINE_OPERATIONS_EXECUTOR ?? '').toLowerCase())
      && input.capabilities.includes('actions.read')) {
    const readTools: HostTool[] = [
      { name: 'actions_runs', description: 'List at most 20 deployment workflow runs in the bound repository.',
        capability: 'actions.read', readOnly: true,
        input: z.object({ page: z.number().int().min(1).max(5).optional() }) },
      { name: 'actions_run', description: 'Read a deployment workflow run in the bound repository.',
        capability: 'actions.read', readOnly: true,
        input: z.object({ run_id: z.number().int().positive() }) },
      { name: 'actions_logs', description: 'Read a redacted, bounded log tail for a run in the bound repository.',
        capability: 'actions.read', readOnly: true,
        input: z.object({ run_id: z.number().int().positive() }) },
    ];
    definitions.push(...readTools.map(tool => ({ ...tool,
      schemaDigest: createHash('sha256').update(JSON.stringify(z.toJSONSchema(tool.input.strict(), { target: 'draft-7' }))).digest('hex') })));
  }

  if (['operations', 'agent-codex-operations'].includes(input.persona)
      && ['true', '1'].includes((process.env.ADP_ENGINE_OPERATIONS_EXECUTOR ?? '').toLowerCase())
      && input.capabilities.includes('actions.write')) {
    const actionTools: HostTool[] = [
      { name: 'actions_dispatch', description: 'Dispatch an accepted deployment workflow through the gateway. Human-approval workflows are refused.',
        capability: 'actions.write', readOnly: false,
        input: z.object({ workflow: z.string().regex(/^[A-Za-z0-9][A-Za-z0-9_.-]*\.ya?ml$/), inputs: z.record(z.string(), z.string()).optional() }) },
      { name: 'actions_rerun', description: 'Rerun a failed correlated workflow run through the gateway.',
        capability: 'actions.write', readOnly: false,
        input: z.object({ run_id: z.number().int().positive() }) },
    ];
    definitions.push(...actionTools.map(tool => ({ ...tool,
      schemaDigest: createHash('sha256').update(JSON.stringify(z.toJSONSchema(tool.input.strict(), { target: 'draft-7' }))).digest('hex') })));
  }
  if (['operations', 'agent-codex-operations'].includes(input.persona)
      && ['true', '1'].includes((process.env.ADP_ENGINE_OPERATIONS_EXECUTOR ?? '').toLowerCase())) {
    const repairTools: HostTool[] = [
      { name: 'repair_tasks', description: 'Read the durable operations node and repair-cycle tasks with observed PR and deployment evidence. Resume the unfinished task.', capability: 'repository.read', readOnly: true,
        input: z.object({}) },
      { name: 'repair_progress', description: 'Record a repair stage. diagnosis_code is accepted only for stage diagnose. cycle is the repair cycle within this attempt, default 1. This does not prove deployment success.', capability: 'actions.write', readOnly: false,
        input: z.object({ cycle: z.number().int().min(1).max(1000).optional(), stage: z.enum(['diagnose', 'repair', 'test', 'publish', 'deploy', 'verify']), state: z.enum(['in_progress', 'done', 'blocked']), diagnosis_code: z.string().regex(/^[A-Za-z0-9_.-]{1,64}$/).optional() }) },
      { name: 'repair_files', description: 'List the current repair workspace files.', capability: 'repository.read', readOnly: true,
        input: z.object({ prefix: z.string().optional(), offset: z.number().int().min(0).optional(), limit: z.number().int().min(1).max(100).optional() }) },
      { name: 'repair_read', description: 'Read up to 8 KiB of source and its full file digest. Continue at next_offset (characters), passing expected_sha256 to reject changed files. Files up to 256 KiB are supported.', capability: 'repository.read', readOnly: true,
        input: z.object({ path, offset: z.number().int().min(0).optional(), limit: z.number().int().min(1).max(8192).optional(), expected_sha256: z.string().regex(/^[a-f0-9]{64}$/).optional() }) },
      { name: 'repair_write', description: 'Write a focused repair. Supply the digest returned by repair_read, or null for a new file.', capability: 'repository.write', readOnly: false,
        input: z.object({ path, content: z.string().max(32768), expected_sha256: z.string().regex(/^[a-f0-9]{64}$/).nullable() }) },
      { name: 'repair_replace', description: 'Replace one unique exact text fragment using the full digest from repair_read. Preserves the rest of a large file; gateway validates the entire proposed result before writing.', capability: 'repository.write', readOnly: false,
        input: z.object({ path, old_text: z.string().min(1).max(8192), new_text: z.string().max(8192), expected_sha256: z.string().regex(/^[a-f0-9]{64}$/) }) },
      { name: 'repair_publish', description: 'Publish workspace changes and create/update the assigned repair PR. Existing repository CI runs tests.', capability: 'change.create', readOnly: false,
        input: z.object({ title: z.string().min(1).max(240), body: z.string().min(1).max(12000) }) },
      { name: 'repair_status', description: 'Inspect the repair PR head, CI checks and merge readiness. Use Actions logs to diagnose failing tests.', capability: 'repository.read', readOnly: true,
        input: z.object({ pull_number: z.number().int().positive() }) },
      { name: 'repair_merge', description: 'Merge the assigned repair after automated checks pass. Returns not-ready while checks/protections block; continue fixing and testing.', capability: 'change.merge', readOnly: false,
        input: z.object({ pull_number: z.number().int().positive(), expected_head: z.string().regex(/^[a-f0-9]{40}$/) }) },
    ];
    definitions.push(...repairTools.filter(tool => input.capabilities.includes(tool.capability)).map(tool => ({ ...tool,
      schemaDigest: createHash('sha256').update(JSON.stringify(z.toJSONSchema(tool.input.strict(), { target: 'draft-7' }))).digest('hex') })));
  }
  return {
    definitions,
    async execute(name: string, raw: Record<string, unknown>, signal: AbortSignal) {
      const tool = definitions.find(tool => tool.name === name);
      if (!tool) throw new Error('Unknown GitHub host tool');
      const args = tool.input.strict().parse(raw) as Record<string, any>;
      const id = randomUUID();
      const subject = name === 'story_task_progress' ? 'story task progress' : name === 'repository_file'
        ? `${args.path} (lines ${args.start_line ?? 1}–${(args.start_line ?? 1) + (args.max_lines ?? 100) - 1})`
        : name === 'actions_dispatch' ? args.workflow
        : name === 'actions_runs' ? `page ${args.page ?? 1}`
        : name.startsWith('actions_') ? `run ${args.run_id}`
        : `${args.directory ?? 'repository root'} (directory entries ${(args.offset ?? 0) + 1}–${(args.offset ?? 0) + 100})`;
      let isError = false;
      let content: string;
      try {
        content = await execute(name, args, async () => {
          progress(`${name === 'story_task_progress' ? 'Reporting' : name.startsWith('actions_') ? 'Operating' : name === 'repository_file' ? 'Reading' : 'Listing'} ${subject}`, { id, category: 'tool', state: 'running' });
          try {
          if (name === 'story_task_progress') return JSON.stringify(input.storyProgress!(args.expected_sha256, args.updates));
          if (name.startsWith('repair_')) return await hostCommand('python3', ['-m', 'lib.operations_repair', name], signal, args, 512 * 1024);
          if (name.startsWith('actions_')) {
            const response = await actionsRequest(name, args, signal);
            if (name !== 'actions_logs') return response;
            const parsed: unknown = JSON.parse(response);
            if (!parsed || typeof parsed !== 'object' || !('log_tail' in parsed)
                || typeof parsed.log_tail !== 'string') throw new Error('Invalid gateway log response');
            let tail = parsed.log_tail;
            let bounded = JSON.stringify({ ...parsed, log_tail: tail });
            while (Buffer.byteLength(bounded) > 24576 && tail.length) {
              tail = tail.slice(Math.ceil(tail.length / 2));
              bounded = JSON.stringify({ ...parsed, log_tail: tail });
            }
            if (Buffer.byteLength(bounded) > 24576) throw new Error('Gateway log response exceeds tool limit');
            return bounded;

          }
          if (name === 'repository_file') {
            const spec = `${input.revision}:${args.path}`;
            const size = Number((await hostCommand('git', ['cat-file', '-s', spec], signal)).trim());
            if (!Number.isSafeInteger(size) || size < 0 || size > 1048576) throw new Error('Repository file exceeds read limit');
            const result = await hostCommand('git', ['show', '--no-textconv', spec], signal, undefined, 1048576);
            if (result.includes('\0')) throw new Error('Binary repository content is unavailable');
            const start = (args.start_line ?? 1) - 1;
            const content = result.split('\n').slice(start, start + (args.max_lines ?? 100)).join('\n');
            if (Buffer.byteLength(content) > 24576) throw new Error('Line range exceeds read limit');
            return content;
          }
          if (name === 'repository_files') {
            const spec = args.directory ? `${input.revision}:${args.directory}` : input.revision;
            const paths = (await hostCommand('git', ['ls-tree', '--name-only', spec], signal, undefined, 1048576)).trimEnd().split('\n');
            const content = paths.slice(args.offset ?? 0, (args.offset ?? 0) + 100).join('\n');
            if (Buffer.byteLength(content) > 24576) throw new Error('Directory page exceeds read limit');
            return content;
          }
          throw new Error('Unknown repository tool');
          } catch (error) {
            const readOnlyActions = name.startsWith('actions_') && definitions.some(tool => tool.name === name && tool.readOnly);
            if ((name.startsWith('actions_') && !readOnlyActions) || name.startsWith('repair_')) throw error;
            // These fixed Git commands and read-only Actions observations cannot
            // mutate anything. A confirmed read failure is useful repair evidence
            // the model can act on (observe later, diagnose elsewhere); killing the
            // whole operations run on a gateway 5xx wasted an attempt per outage.
            // Mutating Actions calls and repair publication keep their replay fence.
            signal.throwIfAborted();
            isError = true;
            if (readOnlyActions) {
              const detail = String((error as Error)?.message ?? error).replace(/\s+/g, ' ').slice(0, 200);
              return `Actions observation unavailable right now (${detail}). Retry later or continue with other evidence.`;
            }
            return 'Repository read unavailable or exceeds limits. Check the path and narrow the requested range.';
          }
        }, signal);
      } catch (error) {
        progress(`Repository operation did not complete: ${subject}`, { id, category: 'tool', state: 'failed' });
        throw error;
      }
      progress(`${isError ? 'Repository read unavailable:' : name === 'story_task_progress' ? 'Reported' : name === 'repository_file' ? 'Read' : 'Listed'} ${subject}`,
        { id, category: 'tool', state: isError ? 'failed' : 'completed' });
      return { status: 'confirmed' as const, content, ...(isError ? { isError: true } : {}) };
    },
  };
}
