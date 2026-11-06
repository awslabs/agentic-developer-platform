jest.mock('./utils/resilientQuery', () => ({ resilientQuery: jest.fn() }));

import { mkdtempSync, rmSync } from 'fs';
import { tmpdir } from 'os';
import { join } from 'path';
import { configureMonitoring, createInitialState, parseUserCommands, runMonitoringLoop } from './monitoring';
import type { CommentEnvelope } from './utils/comment-authority';

const verbs = ['/status', '/pause', '/resume', '/retry #42', '/stop', '/extend', '/help', '/instruct act', '/queryPM why'];
const comment = (body: string, author = 'alice', authorType = 'User'): CommentEnvelope => {
  const createdAt = new Date(Date.now() + 1000).toISOString();
  return { id: '900', author, authorId: 42, authorType, body, createdAt, updatedAt: createdAt };
};
const context = { owner: 'owner', repo: 'repo', runId: 'run-1', tokenProvider: async () => 'fixture-token' };

describe('SCN-01 monitoring command authority', () => {
  const originalFetch = global.fetch;
  afterEach(() => { global.fetch = originalFetch; });

  it.each(['admin', 'maintain', 'write'])('allows every exact monitoring verb for %s', async permission => {
    global.fetch = jest.fn().mockResolvedValue({ ok: true, json: async () => ({ permission }) });
    const commands = await parseUserCommands([comment(verbs.join('\n'))], context);
    expect(commands.map(command => command.command)).toEqual([
      '/status', '/pause', '/resume', '/retry', '/stop', '/extend', '/help', '/instruct', '/querypm',
    ]);
    expect(global.fetch).toHaveBeenCalledTimes(1);
  });

  it.each(['read', 'triage', 'none', 'unknown'])('denies every verb for %s before callback', async permission => {
    global.fetch = jest.fn().mockResolvedValue({ ok: true, json: async () => ({ permission }) });
    const onDenial = jest.fn();
    expect(await parseUserCommands([comment(verbs.join('\n'))], context, onDenial)).toEqual([]);
    expect(onDenial).toHaveBeenCalledTimes(1);
  });

  it('denies machine authors even with repository write permission', async () => {
    global.fetch = jest.fn();
    expect(await parseUserCommands([comment('/stop', 'agent[bot]', 'Bot')], context)).toEqual([]);
    expect(global.fetch).not.toHaveBeenCalled();
  });

  it('ignores prefixed verbs and processes case-insensitive queryPM exactly', async () => {
    global.fetch = jest.fn().mockResolvedValue({ ok: true, json: async () => ({ permission: 'write' }) });
    expect((await parseUserCommands([comment('/stopNow\n/queryPM why')], context)).map(command => command.command))
      .toEqual(['/querypm']);
  });

  it.each(verbs.flatMap(verb => [false, true].map(allowed => [verb, allowed] as const)))(
    'gates %s for writer=%s in the monitoring loop', async (verb, allowed) => {
    global.fetch = jest.fn().mockResolvedValue({ ok: true, json: async () => ({ permission: allowed ? 'write' : 'read' }) });
    const state = createInitialState(123, null);
    const workDir = mkdtempSync(join(tmpdir(), 'monitoring-authority-'));
    const postComment = jest.fn(async (_body: string) => {});
    const onCommand = jest.fn(async () => true);
    const onInstruct = jest.fn(async () => ({ success: true }));
    const onQueryPM = jest.fn(async () => ({ response: 'fixture response' }));
    let fetchedPages = 0;
    const execCommand = jest.fn(async (commandText: string) => {
      if (commandText.startsWith('gh api ')) {
        fetchedPages++;
        if (fetchedPages === 1) return '[[]]';
        state.isActive = false;
        return JSON.stringify([[{
          id: 900, user: { login: 'alice', id: 42, type: 'User' }, body: verb,
          created_at: new Date(Date.now() + 1000).toISOString(), updated_at: new Date().toISOString(),
        }]]);
      }
      if (commandText === 'gh auth token') return 'fixture-token';
      return '[]';
    });
    configureMonitoring({ pollIntervalMs: 1 });
    try {
      const generator = runMonitoringLoop(state, 'owner', 'repo', { log: jest.fn(), postComment, execCommand, onCommand, onInstruct, onQueryPM }, workDir);
      const events = [];
      let result = await generator.next();
      while (!result.done) {
        events.push(result.value.type);
        result = await generator.next();
      }
      expect(events.filter(event => event === 'user_command')).toHaveLength(allowed ? 1 : 0);
      expect(postComment.mock.calls.slice(1).some(([body]) => !body.startsWith('Comment ')) ||
        onCommand.mock.calls.length > 0 || onInstruct.mock.calls.length > 0 ||
        onQueryPM.mock.calls.length > 0).toBe(allowed);
      expect(onCommand).toHaveBeenCalledTimes(allowed && verb.startsWith('/retry') ? 1 : 0);
      expect(onInstruct).toHaveBeenCalledTimes(allowed && verb.startsWith('/instruct') ? 1 : 0);
      expect(onQueryPM).toHaveBeenCalledTimes(allowed && verb.startsWith('/queryPM') ? 1 : 0);
    } finally {
      configureMonitoring({ pollIntervalMs: 5 * 60 * 1000 });
      rmSync(workDir, { recursive: true, force: true });
    }
  });

  it('deduplicates equal-time pages, avoids initial history and bounds denied notices', async () => {
    const timestamp = new Date(Date.now() + 1_000).toISOString();
    const restComment = (id: number, body: string, login = 'alice') => ({
      id, user: { id: login === 'alice' ? 42 : 43, login, type: 'User' },
      body, created_at: timestamp, updated_at: timestamp,
    });
    const history = restComment(1, '/stop');
    const filler = Array.from({ length: 100 }, (_, index) => restComment(index + 10, 'regular conversation'));
    const page = [restComment(200, '/pause', 'bob'), restComment(201, '/instruct private guidance', 'bob'),
      restComment(202, '/status'), restComment(203, '/stop')];
    const editedHistory = { ...history, body: '/pause', updated_at: new Date(Date.parse(timestamp) + 1_000).toISOString() };
    const responses = [JSON.stringify([[history]]), JSON.stringify([[editedHistory, ...filler, ...page], [page[2], page[3]]])];
    global.fetch = jest.fn(async (url: string) => ({ ok: true, json: async () => ({
      permission: url.includes('/bob/') ? 'read' : 'write',
    }) })) as unknown as typeof fetch;
    const state = createInitialState(123, null);
    const workDir = mkdtempSync(join(tmpdir(), 'monitoring-pages-'));
    const postComment = jest.fn(async (_body: string) => {});
    const log = jest.fn();
    let fetchCount = 0;
    const execCommand = jest.fn(async (commandText: string) => {
      if (commandText === 'gh auth token') return 'fixture-token';
      if (commandText.startsWith('gh api ')) {
        const response = responses[fetchCount++];
        if (fetchCount === responses.length) state.isActive = false;
        return response;
      }
      return '[]';
    });
    try {
      const generator = runMonitoringLoop(state, 'owner', 'repo', { log, postComment, execCommand }, workDir);
      const events = [];
      let result = await generator.next();
      while (!result.done) { events.push(result.value.type); result = await generator.next(); }
      expect(events.filter(event => event === 'user_command')).toHaveLength(2);
      expect(postComment.mock.calls.filter(([body]) => body.startsWith('Comment '))).toHaveLength(1);
      expect(postComment.mock.calls.some(([body]) => body === 'Monitoring paused. Use `/resume` to continue.')).toBe(false);
      expect(postComment.mock.calls.some(([body]) => body === 'Monitoring stopped.')).toBe(true);
      expect(fetchCount).toBe(2);
    } finally {
      rmSync(workDir, { recursive: true, force: true });
    }
  });

  it('keeps observation and authorization separate across two active monitoring sessions', async () => {
    const timestamp = new Date(Date.now() + 1_000).toISOString();
    global.fetch = jest.fn().mockResolvedValue({ ok: true, json: async () => ({ permission: 'write' }) });
    for (const sessionNumber of [1, 2]) {
      const state = createInitialState(123, null);
      const workDir = mkdtempSync(join(tmpdir(), `monitoring-session-${sessionNumber}-`));
      let fetchCount = 0;
      const execCommand = jest.fn(async (commandText: string) => {
        if (commandText === 'gh auth token') return 'fixture-token';
        if (!commandText.startsWith('gh api ')) return '[]';
        fetchCount++;
        if (fetchCount === 1) return '[[]]';
        state.isActive = false;
        return JSON.stringify([[{ id: 100, user: { login: 'alice', id: 42, type: 'User' },
          body: '/stop', created_at: timestamp, updated_at: timestamp }]]);
      });
      try {
        const generator = runMonitoringLoop(state, 'owner', 'repo', {
          log: jest.fn(), postComment: async () => {}, execCommand,
        }, workDir);
        const events = [];
        let result = await generator.next();
        while (!result.done) { events.push(result.value.type); result = await generator.next(); }
        expect(events.filter(event => event === 'user_command')).toHaveLength(1);
        expect(fetchCount).toBe(2);
      } finally {
        rmSync(workDir, { recursive: true, force: true });
      }
    }
  });

  it('retries a partial page failure without leaking provider text or skipping a new command', async () => {
    const timestamp = new Date(Date.now() + 1_000).toISOString();
    global.fetch = jest.fn().mockResolvedValue({ ok: true, json: async () => ({ permission: 'write' }) });
    const state = createInitialState(123, null);
    const workDir = mkdtempSync(join(tmpdir(), 'monitoring-page-failure-'));
    const postComment = jest.fn(async (_body: string) => {});
    const log = jest.fn();
    let fetchCount = 0;
    const execCommand = jest.fn(async (commandText: string) => {
      if (commandText === 'gh auth token') return 'fixture-token';
      if (commandText.startsWith('gh api ')) {
        fetchCount++;
        if (fetchCount === 1) return '[[]]';
        if (fetchCount === 2) throw new Error('private provider detail');
        state.isActive = false;
        return JSON.stringify([[{ id: 3, user: { id: 42, login: 'alice', type: 'User' },
          body: '/stop', created_at: timestamp, updated_at: timestamp }]]);
      }
      return '[]';
    });
    configureMonitoring({ pollIntervalMs: 1 });
    try {
      const generator = runMonitoringLoop(state, 'owner', 'repo', { log, postComment, execCommand }, workDir);
      const events = [];
      let result = await generator.next();
      while (!result.done) { events.push(result.value.type); result = await generator.next(); }
      expect(events.filter(event => event === 'user_command')).toHaveLength(1);
      expect(postComment.mock.calls.some(([body]) => body === 'Monitoring stopped.')).toBe(true);
      expect(log.mock.calls.flat().join(' ')).not.toContain('private provider detail');
      expect(fetchCount).toBe(3);
    } finally {
      configureMonitoring({ pollIntervalMs: 5 * 60 * 1000 });
      rmSync(workDir, { recursive: true, force: true });
    }
  });

  it('still observes writer resume and stop after a pause', async () => {
    const timestamp = new Date(Date.now() + 1_000).toISOString();
    const restComment = (id: number, body: string) => ({ id, body, user: { id: 42, login: 'alice', type: 'User' },
      created_at: timestamp, updated_at: timestamp });
    global.fetch = jest.fn().mockResolvedValue({ ok: true, json: async () => ({ permission: 'write' }) });
    const state = createInitialState(123, null);
    const workDir = mkdtempSync(join(tmpdir(), 'monitoring-pause-'));
    const postComment = jest.fn(async (_body: string) => {});
    let fetchCount = 0;
    const execCommand = jest.fn(async (commandText: string) => {
      if (commandText === 'gh auth token') return 'fixture-token';
      if (!commandText.startsWith('gh api ')) return '[]';
      fetchCount++;
      if (fetchCount === 1) return '[[]]';
      if (fetchCount === 2) return JSON.stringify([[restComment(1, '/pause')]]);
      state.isActive = false;
      return JSON.stringify([[restComment(1, '/pause'), restComment(2, '/resume'), restComment(3, '/stop')]]);
    });
    configureMonitoring({ pollIntervalMs: 1 });
    try {
      const generator = runMonitoringLoop(state, 'owner', 'repo', { log: jest.fn(), postComment, execCommand }, workDir);
      const events = [];
      let result = await generator.next();
      while (!result.done) { events.push(result.value.type); result = await generator.next(); }
      expect(events.filter(event => event === 'user_command')).toHaveLength(3);
      expect(postComment.mock.calls.map(([body]) => body)).toEqual(expect.arrayContaining([
        'Monitoring paused. Use `/resume` to continue.', 'Monitoring resumed.', 'Monitoring stopped.',
      ]));
      expect(fetchCount).toBe(3);
    } finally {
      configureMonitoring({ pollIntervalMs: 5 * 60 * 1000 });
      rmSync(workDir, { recursive: true, force: true });
    }
  });

});
