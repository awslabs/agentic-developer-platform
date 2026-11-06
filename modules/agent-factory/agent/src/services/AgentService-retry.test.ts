jest.mock('../components/GitHubClient', () => ({ GitHubClient: jest.fn() }));
jest.mock('../components/AgentOrchestrator', () => ({ AgentOrchestrator: jest.fn() }));

import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';
import { AgentService } from './AgentService';

const originalFetch = global.fetch;
const originalEnv = { ...process.env };
const eventPath = path.join(os.tmpdir(), `legacy-retry-fixture-${process.pid}.json`);
const createdAt = new Date(Date.now() - 1_000).toISOString();
const makeComment = (body: string) => ({ id: '51', author: 'alice', authorId: 42,
  authorType: 'User', body, createdAt, updatedAt: createdAt });
const client = (body: string) => ({
  getApprovalComments: jest.fn(async () => [makeComment(body)]), getApprovalToken: jest.fn(async () => 'fixture-token'),
});

beforeEach(() => {
  process.env.GITHUB_EVENT_PATH = eventPath;
  fs.writeFileSync(eventPath, JSON.stringify({ action: 'created', repository: { full_name: 'source/control' },
    issue: { number: 24 }, sender: { id: 42 }, comment: { id: 51, user: { id: 42 } } }));
  global.fetch = jest.fn().mockResolvedValue({ ok: true, json: async () => ({ permission: 'write', user: { id: 42 } }) });
});
afterEach(() => { process.env = { ...originalEnv }; global.fetch = originalFetch; });
afterAll(() => { fs.rmSync(eventPath, { force: true }); });

describe('SCN-07 legacy AgentService retry authority', () => {
  it('uses the same trigger-scoped writer decision for lock takeover and retry guidance', async () => {
    const service = new AgentService();
    const selected = await (service as any).findAuthorizedRetry(client('/retry use the revised plan'), 'source', 'control', 24);
    expect(selected).toEqual({ guidance: 'use the revised plan' });
    const source = fs.readFileSync(path.join(__dirname, 'AgentService.ts'), 'utf8');
    expect(source).toContain('const isRetry = retry !== null;');
    expect(source).toContain('acquireLock(String(issueContext.issueNumber), isRetry)');
    expect(source).toContain('issueContext.retryGuidance = retry.guidance;');
    expect(source).not.toContain('getRetryGuidance(githubClient');
  });

  it('does not bypass the lock or inject guidance from a reader or unrelated event', async () => {
    const service = new AgentService();
    global.fetch = jest.fn().mockResolvedValue({ ok: true, json: async () => ({ permission: 'read' }) });
    expect(await (service as any).findAuthorizedRetry(client('/retry take over'), 'source', 'control', 24)).toBeNull();
    fs.writeFileSync(eventPath, JSON.stringify({ action: 'edited', repository: { full_name: 'source/control' },
      issue: { number: 24 }, sender: { id: 42 }, comment: { id: 51, user: { id: 42 } } }));
    expect(await (service as any).findAuthorizedRetry(client('/retry take over'), 'source', 'control', 24)).toBeNull();
  });
});
