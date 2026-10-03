/** Reviewed host tools. No shell, credential material or provider URL enters the SDK. */
import { randomUUID } from 'node:crypto';
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

export function githubTools(input: { persona: string; repository: string; revision: string; capabilities: readonly Capability[] },
  execute: (name: string, args: Record<string, unknown>, work: () => Promise<string>, signal: AbortSignal) => Promise<string>,
  progress: (text: string, detail: RepositoryProgress) => void = () => {}) {
  if (!/^[a-f0-9]{40}$/.test(input.revision)) throw new Error('Invalid repository revision');
  const definitions: HostTool[] = input.capabilities.includes('repository.read') ? [
    { name: 'repository_file', description: 'Read up to 200 lines from a tracked UTF-8 file at the admitted commit. Files are limited to 1 MiB and each result to 24 KiB; narrow the range for long lines.', capability: 'repository.read', readOnly: true,
      input: z.object({ path, start_line: z.number().int().min(1).max(1048576).optional(), max_lines: z.number().int().min(1).max(200).optional() }) },
    { name: 'repository_files', description: 'List a directory at the admitted commit, at most 100 entries. Omit directory for the root; use offset for later entries. Does not recursively enumerate the repository.', capability: 'repository.read', readOnly: true,
      input: z.object({ directory: path.optional(), offset: z.number().int().min(0).max(100000).optional() }) },
  ] : [];
  return {
    definitions,
    async execute(name: string, raw: Record<string, unknown>, signal: AbortSignal) {
      const tool = definitions.find(tool => tool.name === name);
      if (!tool) throw new Error('Unknown GitHub host tool');
      const args = tool.input.strict().parse(raw) as Record<string, any>;
      const id = randomUUID();
      const subject = name === 'repository_file'
        ? `${args.path} (lines ${args.start_line ?? 1}–${(args.start_line ?? 1) + (args.max_lines ?? 100) - 1})`
        : `${args.directory ?? 'repository root'} (directory entries ${(args.offset ?? 0) + 1}–${(args.offset ?? 0) + 100})`;
      let isError = false;
      let content: string;
      try {
        content = await execute(name, args, async () => {
          progress(`${name === 'repository_file' ? 'Reading' : 'Listing'} ${subject}`, { id, category: 'tool', state: 'running' });
          try {
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
            // These fixed Git commands cannot mutate the repository. A confirmed
            // read failure is useful repair evidence; transport/journal failures
            // remain outside this catch and retain their replay fence.
            signal.throwIfAborted();
            isError = true;
            return 'Repository read unavailable or exceeds limits. Check the path and narrow the requested range.';
          }
        }, signal);
      } catch (error) {
        progress(`Repository operation did not complete: ${subject}`, { id, category: 'tool', state: 'failed' });
        throw error;
      }
      progress(`${isError ? 'Repository read unavailable:' : name === 'repository_file' ? 'Read' : 'Listed'} ${subject}`,
        { id, category: 'tool', state: isError ? 'failed' : 'completed' });
      return { status: 'confirmed' as const, content, ...(isError ? { isError: true } : {}) };
    },
  };
}
