jest.mock('./utils/resilientQuery', () => ({ resilientQuery: jest.fn() }));

import { execSync } from 'child_process';
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'fs';
import { tmpdir } from 'os';
import { join } from 'path';
import { closeTask, configureBeads, createTask, getBeadsConfig, reportFailure, setLogger } from './beads';
import { runCommand, shellCommand } from './lib/command';
import { fetchWorkflowRunForIssue, handleAgentCompletion, type TrackedAgent } from './monitoring';

describe('repository and model text stays data at command boundaries', () => {
  let directory: string;
  let marker: string;
  let originalEnv: NodeJS.ProcessEnv;
  const originalConfig = getBeadsConfig();

  beforeEach(() => {
    directory = mkdtempSync(join(tmpdir(), 'adp-command-test-'));
    marker = join(directory, 'injected');
    originalEnv = process.env;
    process.env = {
      ...originalEnv,
      PATH: `${directory}:${originalEnv.PATH}`,
      ADP_COMMAND_TEST_LOG: join(directory, 'calls.jsonl'),
      ADP_COMMAND_TEST_ITEMS: join(directory, 'items.json'),
      ADP_AGENT_AUTHORITY_ENABLED: 'false',
      GH_TOKEN: '', GITHUB_TOKEN: '',
    };
    // Execute a real child process (and, for monitoring, a real shell). Only the
    // external CLIs are stubs; they record precisely what arguments they receive.
    const stub = `#!${process.execPath}
const fs = require('fs');
const args = process.argv.slice(2);
const index = args.indexOf('--description-file');
const descriptionFile = index < 0 ? undefined : args[index + 1];
fs.appendFileSync(process.env.ADP_COMMAND_TEST_LOG, JSON.stringify({ args, descriptionFile,
  description: descriptionFile && fs.readFileSync(descriptionFile, 'utf8'),
  mode: descriptionFile && (fs.statSync(descriptionFile).mode & 0o777),
}) + '\\n');
if (process.env.ADP_COMMAND_TEST_FAIL === 'true') process.exit(1);
if (args[0] === 'project' && args[1] === 'item-list') {
  process.stdout.write(fs.readFileSync(process.env.ADP_COMMAND_TEST_ITEMS, 'utf8'));
} else if (args[0] === 'project' && args[1] === 'field-list') {
  process.stdout.write(JSON.stringify({fields: [
    {id: 'blocked-field', name: 'blocked_by'},
    {id: 'status-field', name: 'Status', options: [{id: 'progress-option', name: 'In Progress'}]},
  ]}));
} else process.stdout.write(JSON.stringify({id: 'bd-fixture', labels: [], state: 'OPEN'}));
`;
    for (const executable of ['gh', 'bd']) writeFileSync(join(directory, executable), stub, { mode: 0o700 });
    configureBeads({ enabled: true, syncOnComplete: false });
    setLogger(jest.fn());
  });

  afterEach(() => {
    process.env = originalEnv;
    configureBeads(originalConfig);
    rmSync(directory, { recursive: true, force: true });
  });

  function calls(): Array<{ args: string[]; descriptionFile?: string; description?: string; mode?: number }> {
    return readFileSync(process.env.ADP_COMMAND_TEST_LOG!, 'utf8').trim().split('\n').map(line => JSON.parse(line));
  }

  function hostileText(): string {
    return `--title='quoted'; $(touch ${marker}) \`touch ${marker}\` "double" \\backslash\nnext line`;
  }

  it.each(['argv', 'shell'] as const)('preserves hostile and empty arguments through %s execution', mode => {
    const args = ['issue', 'edit', '42', '--add-label', hostileText(), ''];
    runCommand(mode === 'argv' ? ['gh', ...args] : shellCommand('gh', args), {
      encoding: 'utf8', cwd: directory, env: process.env,
    });
    expect(calls()[0].args).toEqual(args);
    expect(existsSync(marker)).toBe(false);
  });

  it('passes Beads titles, IDs, failure messages and close reasons literally', async () => {
    const text = hostileText();
    await createTask(text, { description: text, assignee: text, labels: [text] }, directory);
    await closeTask(text, text, directory);
    await reportFailure(text, text, directory);
    const recorded = calls();
    expect(recorded[0].args.slice(-2)).toEqual(['--', text]);
    expect(recorded[0].args).toEqual([
      '--json', 'create', '--assignee', text, '--labels', text,
      '--description-file', recorded[0].descriptionFile, '--', text,
    ]);
    expect(recorded[0].description).toBe(text);
    expect(recorded[0].mode).toBe(0o600);
    expect(existsSync(recorded[0].descriptionFile!)).toBe(false);
    expect(recorded[1].args).toEqual(['--json', 'close', '--reason', text, '--', text]);
    expect(recorded[3].args).toEqual(['comment', '--', text, `Agent failed: ${text}`]);
    expect(existsSync(marker)).toBe(false);
  });

  it('removes temporary descriptions when Beads fails', async () => {
    process.env.ADP_COMMAND_TEST_FAIL = 'true';
    await expect(createTask('title', { description: 'private description' }, directory)).rejects.toThrow();
    expect(existsSync(calls()[0].descriptionFile!)).toBe(false);
  });

  it('checks out a legal Git branch containing command substitution without executing it', () => {
    const options = { cwd: directory, encoding: 'utf8' as const, env: process.env };
    const branch = 'aidlc/42-$(touch${IFS}injected)';
    runCommand(['git', 'init', '-q'], options);
    runCommand(['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.test',
      '-c', 'commit.gpgsign=false', 'commit', '--allow-empty', '-qm', 'fixture'], options);
    runCommand(['git', 'check-ref-format', '--branch', branch], options);
    runCommand(['git', 'checkout', '-qb', branch, '--'], options);
    runCommand(['git', 'checkout', branch, '--'], options);
    expect(runCommand(['git', 'branch', '--show-current'], options).trim()).toBe(branch);
    expect(existsSync(marker)).toBe(false);
  });

  it('passes model-derived workflow names literally', async () => {
    const agent = hostileText();
    await fetchWorkflowRunForIssue('example', 'repo', 42, agent, async command => execSync(command, {
      cwd: directory, encoding: 'utf8', env: process.env,
    }));
    expect(calls().map(call => call.args[call.args.indexOf('--workflow') + 1]))
      .toEqual([`call-agent-${agent}.yml`, `agent-${agent}.yml`]);
    expect(existsSync(marker)).toBe(false);
  });

  it('passes project blockers, item IDs and assigned labels literally through the monitoring callback', async () => {
    const text = hostileText();
    writeFileSync(process.env.ADP_COMMAND_TEST_ITEMS!, JSON.stringify({ items: [
      { id: text, content: { number: 43 }, status: 'Todo', blocked_by: `#42, ${text}` },
      { id: 'next-item', content: { number: 44 }, status: 'Todo', blocked_by: '#42', assigned_agent: `@agent-${text}` },
    ] }));
    const result = await handleAgentCompletion(
      { issueNumber: 42, agentType: 'developer' } as TrackedAgent,
      { repoOwner: 'example', repoName: 'repo', projectNumber: 1, projectId: text,
        parentIssue: { number: 1, title: 'Parent', body: '' } },
      { log: jest.fn(), postComment: jest.fn(), execCommand: async command => execSync(command, {
        cwd: directory, encoding: 'utf8', env: process.env,
      }) },
    );
    expect(result).toEqual({ unblocked: [43, 44], triggered: [44] });
    const mutations = calls().filter(call => ['item-edit', 'edit'].includes(call.args[1]));
    expect(mutations[0].args).toEqual([
      'project', 'item-edit', '--id', text, '--project-id', text, '--field-id', 'blocked-field', '--text', text,
    ]);
    expect(mutations[1].args.at(-1)).toBe(' ');
    expect(mutations[2].args).toEqual(['issue', 'edit', '44', '--repo', 'example/repo', '--add-label', `agent-${text}`]);
    expect(mutations[3].args).toContain(text);
    expect(existsSync(marker)).toBe(false);
  });
});
