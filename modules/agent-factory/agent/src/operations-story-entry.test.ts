jest.mock('./operations-story-tools', () => ({
  ...jest.requireActual('./operations-story-tools'),
  invokeOperationsStoryTool: jest.fn().mockResolvedValue(JSON.stringify({ story_board: { state: 'absent' } })),
}));
jest.mock('@anthropic-ai/claude-agent-sdk', () => ({
  query: jest.fn(), createSdkMcpServer: jest.fn(({ tools }) => ({ tools })),
  tool: jest.fn((name) => ({ name })),
}));

import { query } from '@anthropic-ai/claude-agent-sdk';
import { mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { main, storyPrompt, operationsMaxTurns, sdkFailure } from './operations-story-entry';
import { operationsStoryTools, invokeOperationsStoryTool } from './operations-story-tools';
import { sanitizedSdkEnv } from './github-runtime-auth';

it('exposes the shared task list while restricting repository and deployment work to brokered tools', async () => {
  const previousEnv = process.env;
  const directory = mkdtempSync(join(tmpdir(), 'operations-story-'));
  const reportPath = join(directory, 'report.json');
  const mockQuery = query as jest.Mock;
  mockQuery.mockImplementation(async function* () {
    yield { type: 'result', subtype: 'success', result: 'Runtime evidence remains unverified.' };
    throw new Error('The terminal result must end SDK iteration');
  });
  process.env = {
    ...previousEnv,
    ADP_ENGINE_OPERATIONS_STORY: 'true',
    AGENT_TYPE: 'operations',
    ADP_RULES_DIR: join(__dirname, '..', '..', 'rules'),
    ADP_OPERATIONS_STORY_CONTEXT: JSON.stringify({ title: 'Run the approved wave', workflows: ['deploy.yml'] }),
    ISSUE_NUMBER: '12',
    ADP_OPERATIONS_REPORT_FILE: reportPath,
    WORK_DIR: directory,
  };
  try {
    await main();
    expect(mockQuery).toHaveBeenCalledTimes(1);
    const { options } = mockQuery.mock.calls[0][0];
    expect(options.tools).toEqual(['TodoWrite']);
    expect(options.includePartialMessages).toBe(true);
    expect(options.allowedTools).toEqual([
      'TodoWrite',
      'mcp__pipeline-actions__actions_runs', 'mcp__pipeline-actions__actions_run', 'mcp__pipeline-actions__actions_wait',
      'mcp__pipeline-actions__actions_logs', 'mcp__pipeline-actions__actions_dispatch',
      'mcp__pipeline-actions__actions_rerun',
      ...['repair_tasks', 'repair_progress', 'repair_files', 'repair_read', 'repair_write', 'repair_replace', 'repair_publish', 'repair_status', 'repair_wait', 'repair_merge'].map(name => `mcp__pipeline-actions__${name}`),
    ]);
    expect(Object.keys(options.mcpServers)).toEqual(['pipeline-actions']);
    expect(options.mcpServers['pipeline-actions'].tools.map((tool: { name: string }) => tool.name)).toEqual([
      'repair_tasks', 'repair_progress', 'repair_files', 'repair_read', 'repair_write', 'repair_replace', 'repair_publish', 'repair_status', 'repair_wait', 'repair_merge',
      'actions_runs', 'actions_run', 'actions_wait', 'actions_logs', 'actions_dispatch', 'actions_rerun',
    ]);
    expect(options.settingSources).toEqual([]);
    expect(options.env.ADP_ENGINE_OPERATIONS_STORY).toBe('true');
    expect(options.env.PATH).toBe(process.env.PATH);
    // Mocking query alone cannot detect the SDK silently disabling TodoWrite.
    // Start the real pinned SDK against a loopback-only endpoint and inspect
    // its advertised tools before any model response or tool execution.
    const { stdout } = await promisify(execFile)(process.execPath, [
      join(__dirname, '..', 'tests', 'operations-checklist-sdk-probe.mjs'),
      require.resolve('@anthropic-ai/claude-agent-sdk'),
      JSON.stringify({ tools: options.tools, allowedTools: options.allowedTools, env: {
        CLAUDE_CODE_ENABLE_TASKS: options.env.CLAUDE_CODE_ENABLE_TASKS,
        CLAUDE_CODE_ENABLE_TODO_TOOLS: options.env.CLAUDE_CODE_ENABLE_TODO_TOOLS,
      } }),
    ], { timeout: 20000, env: { PATH: process.env.PATH } });
    expect(JSON.parse(stdout)).toEqual(['TodoWrite']);
    expect(JSON.parse(readFileSync(reportPath, 'utf8'))).toEqual({ report: 'Runtime evidence remains unverified.' });
  } finally {
    process.env = previousEnv;
    mockQuery.mockReset();
    rmSync(directory, { recursive: true, force: true });
  }
}, 30000);

it.each([
  ['Reached maximum number of turns (150)', 'turn_limit'],
  ['Claude Code process exited with code 1', 'sdk_process_exit_1'],
  ['Connection closed to https://secret.invalid/?token=private', 'sdk_connection_interrupted'],
  ['HTTP 503 with private provider payload', 'sdk_http_503'],
  ['MCP tool timeout with private input', 'sdk_timeout'],
  ['unknown private details', 'sdk_unclassified_error'],
])('records bounded SDK failure categories without provider content: %s', (message, expected) => {
  expect(sdkFailure(message)).toBe(expected);
});

it('projects the dedicated no-AWS operations story instructions and repair tools', () => {
  process.env.ADP_RULES_DIR = join(__dirname, '..', '..', 'rules');
  try {
    const prompt = storyPrompt({ title: 'Run the approved wave', issue: 12, workflows: ['deploy.yml'] });
    expect(prompt).toContain('never raw AWS or GitHub credentials');
    expect(prompt).toContain('Continue the automatically restored story board');
    expect(prompt).toContain('deploy.yml');
    expect(operationsStoryTools().allowedTools).toEqual([
      'mcp__pipeline-actions__actions_runs', 'mcp__pipeline-actions__actions_run', 'mcp__pipeline-actions__actions_wait',
      'mcp__pipeline-actions__actions_logs', 'mcp__pipeline-actions__actions_dispatch',
      'mcp__pipeline-actions__actions_rerun',
      ...['repair_tasks', 'repair_progress', 'repair_files', 'repair_read', 'repair_write', 'repair_replace', 'repair_publish', 'repair_status', 'repair_wait', 'repair_merge'].map(name => `mcp__pipeline-actions__${name}`),
    ]);
    expect(() => storyPrompt({ title: 'Unsafe', issue: 12, workflows: ['../unsafe.yml'] })).toThrow('Invalid operations story context');
  } finally { delete process.env.ADP_RULES_DIR; }
});

it('refuses oversized broker requests before invoking the subprocess', async () => {
  await expect(jest.requireActual('./operations-story-tools').invokeOperationsStoryTool('actions_dispatch', { workflow: 'deploy.yml', padding: 'x'.repeat(8192) }))
    .rejects.toThrow('Actions request exceeds limit');
});

it('removes all customer AWS credentials and role selectors from the SDK subprocess', () => {
  const sandbox = sanitizedSdkEnv({ ADP_ENGINE_OPERATIONS_STORY: 'true', AWS_ACCESS_KEY_ID: 'test',
    AWS_SECRET_ACCESS_KEY: 'test', AWS_SESSION_TOKEN: 'test', AWS_ROLE_ARN: 'role',
    AWS_WEB_IDENTITY_TOKEN_FILE: '/tmp/token', AWS_PROFILE: 'customer', GH_APP_PRIVATE_KEY: 'test',
    ADP_WORKER_IRSA_ROLE_ARN: 'transport-role', ADP_WORKER_IRSA_TOKEN_FILE: '/tmp/transport' });
  expect(Object.keys(sandbox).filter(key => key.startsWith('AWS_') || key === 'GH_APP_PRIVATE_KEY')).toEqual([]);
  expect(sandbox.ADP_WORKER_IRSA_ROLE_ARN).toBe('transport-role');
});


it('records a bounded report naming the result subtype when the story does not succeed', async () => {
  const previousEnv = process.env;
  const directory = mkdtempSync(join(tmpdir(), 'operations-story-'));
  const reportPath = join(directory, 'report.json');
  const mockQuery = query as jest.Mock;
  mockQuery.mockImplementation(async function* () {
    yield { type: 'assistant', message: { content: [{ type: 'text', text: 'Reading the deploy workflow before dispatching.' }] } };
    yield { type: 'result', subtype: 'error_max_turns' };
  });
  process.env = {
    ...previousEnv,
    ADP_ENGINE_OPERATIONS_STORY: 'true',
    AGENT_TYPE: 'operations',
    ADP_RULES_DIR: join(__dirname, '..', '..', 'rules'),
    ADP_OPERATIONS_STORY_CONTEXT: JSON.stringify({ title: 'Run the approved wave', workflows: ['deploy.yml'] }),
    ISSUE_NUMBER: '12',
    ADP_OPERATIONS_REPORT_FILE: reportPath,
    ADP_OPERATIONS_MAX_TURNS: '45',
    WORK_DIR: directory,
  };
  try {
    await expect(main()).rejects.toThrow('Operations story did not complete');
    expect(mockQuery.mock.calls[0][0].options.maxTurns).toBe(45);
    const { report } = JSON.parse(readFileSync(reportPath, 'utf8'));
    expect(report).toContain('error_max_turns');
    expect(report).toContain('Reading the deploy workflow before dispatching.');
    expect(Buffer.byteLength(report)).toBeLessThanOrEqual(12000);
  } finally {
    process.env = previousEnv;
    rmSync(directory, { recursive: true, force: true });
  }
});

it('sizes the turn budget for the persona loop and rejects nonsense overrides', () => {
  expect(operationsMaxTurns(undefined)).toBe(60);
  expect(operationsMaxTurns('')).toBe(60);
  expect(operationsMaxTurns('120')).toBe(120);
  expect(() => operationsMaxTurns('0')).toThrow('Invalid ADP_OPERATIONS_MAX_TURNS');
  expect(() => operationsMaxTurns('abc')).toThrow('Invalid ADP_OPERATIONS_MAX_TURNS');
});

it.each([
  ['Claude Code returned an error result: Reached maximum number of turns (150)', 'error_max_turns'],
  ['SDK transport interrupted', 'error_sdk'],
])('leaves a bounded failure receipt when the SDK throws: %s', async (message, expected) => {
  const previousEnv = process.env;
  const directory = mkdtempSync(join(tmpdir(), 'operations-story-'));
  const reportPath = join(directory, 'report.json');
  (query as jest.Mock).mockImplementation(async function* () {
    yield { type: 'assistant', message: { content: [{ type: 'text', text: 'Deployment 42 is still queued.' }] } };
    throw new Error(message);
  });
  process.env = {
    ...previousEnv,
    ADP_ENGINE_OPERATIONS_STORY: 'true', AGENT_TYPE: 'operations',
    ADP_RULES_DIR: join(__dirname, '..', '..', 'rules'),
    ADP_OPERATIONS_STORY_CONTEXT: JSON.stringify({ title: 'Wait for the rollout', workflows: ['deploy.yml'] }),
    ISSUE_NUMBER: '12', ADP_OPERATIONS_REPORT_FILE: reportPath, WORK_DIR: directory,
  };
  try {
    await expect(main()).rejects.toThrow('Operations story did not complete');
    const { report } = JSON.parse(readFileSync(reportPath, 'utf8'));
    expect(report).toContain(expected);
    expect(report).toContain('Deployment 42 is still queued.');
    expect(Buffer.byteLength(report)).toBeLessThanOrEqual(12000);
  } finally {
    process.env = previousEnv;
    (query as jest.Mock).mockReset();
    rmSync(directory, { recursive: true, force: true });
  }
});


it('keeps an oversized success report by truncating it instead of failing the run', async () => {
  const previousEnv = process.env;
  const directory = mkdtempSync(join(tmpdir(), 'operations-story-'));
  const reportPath = join(directory, 'report.json');
  const mockQuery = query as jest.Mock;
  const long = 'evidence '.repeat(3000); // ~27 KB, far beyond the 12 KB receipt bound
  mockQuery.mockImplementation(async function* () {
    yield { type: 'result', subtype: 'success', result: long };
  });
  process.env = {
    ...previousEnv,
    ADP_ENGINE_OPERATIONS_STORY: 'true',
    AGENT_TYPE: 'operations',
    ADP_RULES_DIR: join(__dirname, '..', '..', 'rules'),
    ADP_OPERATIONS_STORY_CONTEXT: JSON.stringify({ title: 'Run the approved wave', workflows: ['deploy.yml'] }),
    ISSUE_NUMBER: '12',
    ADP_OPERATIONS_REPORT_FILE: reportPath,
    WORK_DIR: directory,
  };
  try {
    await main();
    const { report } = JSON.parse(readFileSync(reportPath, 'utf8'));
    expect(Buffer.byteLength(report)).toBeLessThanOrEqual(12000);
    expect(report.endsWith('...')).toBe(true);
    expect(report.startsWith('evidence evidence')).toBe(true);
  } finally {
    process.env = previousEnv;
    rmSync(directory, { recursive: true, force: true });
  }
});
