import { ApprovalService } from './ApprovalService';
import { bindPostedApproval } from './posted-approval';
import type { CommentEnvelope } from '../utils/comment-authority';
import { Config } from '../types';

const REQUEST_ID = 'request-42';
const ISSUE = 42;
const OWNER = 'owner';
const REPO = 'repo';
const PLAN = 'Exact plan for this run';

function fixture() {
  const postedAt = new Date(Date.now() - 1_000).toISOString();
  const receipt = { id: '10', body: PLAN, createdAt: postedAt, updatedAt: postedAt };
  return { receipt, request: bindPostedApproval(REQUEST_ID, 'run-one', OWNER, REPO, ISSUE,
    PLAN, receipt, 30 * 60_000)! };
}

function reply(body: string, author = 'alice', authorType = 'User'): CommentEnvelope {
  const createdAt = new Date().toISOString();
  return { id: '11', body, author, authorId: 42, authorType, createdAt, updatedAt: createdAt };
}

function client(comments: CommentEnvelope[], receipt = fixture().receipt) {
  return {
    getRepository: jest.fn(() => ({ owner: OWNER, repo: REPO })),
    getApprovalToken: jest.fn(async () => 'fixture-token'),
    getApprovalComments: jest.fn(async () => comments),
    getApprovalPlan: jest.fn(async () => receipt),
  };
}

function service(githubClient: ReturnType<typeof client>, overrides: Partial<Config> = {}) {
  const config = { pollingInterval: 0, maxRetries: 3, ...overrides } as Config;
  const logger = { info: jest.fn(), warn: jest.fn(), error: jest.fn() };
  return new ApprovalService(githubClient as any, logger as any, config);
}

describe('ApprovalService bound polling', () => {
  const originalEnv = { ...process.env };
  const originalFetch = global.fetch;
  beforeEach(() => {
    delete process.env.TARGET_REPO;
    delete process.env.APPROVAL_TIMEOUT_MS;
    delete process.env.APPROVAL_MAX_POLLS;
    global.fetch = jest.fn().mockResolvedValue({ ok: true, json: async () => ({ permission: 'write', user: { id: 42 } }) });
  });
  afterEach(() => { process.env = { ...originalEnv }; global.fetch = originalFetch; });

  it.each(['admin', 'maintain', 'write'])('allows a current %s writer once', async permission => {
    global.fetch = jest.fn().mockResolvedValue({ ok: true, json: async () => ({ permission }) });
    const { request, receipt } = fixture();
    const githubClient = client([reply(`/approve ${REQUEST_ID}`)], receipt);
    const approvalService = service(githubClient);
    expect((await approvalService.pollForApproval(ISSUE, request)).outcome).toBe('allowed-once');
    expect((await approvalService.pollForApproval(ISSUE, request)).outcome).toBe('rejected');
  });

  it('returns authorized rejection feedback but never treats feedback as approval', async () => {
    const { request, receipt } = fixture();
    const result = await service(client([reply(`/reject ${REQUEST_ID} revise it`)], receipt))
      .pollForApproval(ISSUE, request);
    expect(result).toMatchObject({ outcome: 'rejected', approver: 'alice', feedback: 'revise it' });
  });

  it.each(['read', 'triage', 'none'])('denies %s despite a matching command', async permission => {
    global.fetch = jest.fn().mockResolvedValue({ ok: true, json: async () => ({ permission }) });
    process.env.APPROVAL_MAX_POLLS = '2';
    const { request, receipt } = fixture();
    expect((await service(client([reply(`/approve ${REQUEST_ID}`)], receipt)).pollForApproval(ISSUE, request)).outcome)
      .toBe('rejected');
  });

  it('does not mistake the workflow-triggering human for the agent', async () => {
    process.env.GITHUB_ACTOR = 'alice';
    const { request, receipt } = fixture();
    expect((await service(client([reply(`/approve ${REQUEST_ID}`)], receipt)).pollForApproval(ISSUE, request)).outcome)
      .toBe('allowed-once');
  });

  it('ignores bot and bare, quoted, stale and prefixed commands', async () => {
    process.env.APPROVAL_MAX_POLLS = '1';
    const { request, receipt } = fixture();
    const comments = [reply(`/approve ${REQUEST_ID}`, 'agent[bot]', 'Bot'),
      reply('/approve'), reply('> /approve request-42'), reply('/approve request-42-more'),
      reply('/approve previous')];
    expect((await service(client(comments, receipt)).pollForApproval(ISSUE, request)).outcome).toBe('rejected');
  });

  it('denies mismatched issue, repository or changed plan before effects', async () => {
    const { request, receipt } = fixture();
    const githubClient = client([reply(`/approve ${REQUEST_ID}`)], receipt);
    expect((await service(githubClient).pollForApproval(43, request)).outcome).toBe('unavailable');
    githubClient.getApprovalPlan.mockResolvedValue({ ...receipt, body: 'changed' });
    expect((await service(githubClient).pollForApproval(ISSUE, request)).outcome).toBe('rejected');
  });

  it('does not borrow authority from the control repository when the effect repository differs', async () => {
    process.env.TARGET_REPO = 'owner/effect';
    global.fetch = jest.fn(async (url: string) => ({ ok: true,
      json: async () => ({ permission: url.includes('/effect/') ? 'read' : 'write' }) })) as unknown as typeof fetch;
    const { request, receipt } = fixture();
    process.env.APPROVAL_MAX_POLLS = '1';
    expect((await service(client([reply(`/approve ${REQUEST_ID}`)], receipt)).pollForApproval(ISSUE, request)).outcome)
      .toBe('rejected');
  });

  it('bounds unanswered requests by max polls and distinguishes API outage', async () => {
    process.env.APPROVAL_MAX_POLLS = '3';
    const { request, receipt } = fixture();
    const githubClient = client([], receipt);
    expect((await service(githubClient).pollForApproval(ISSUE, request)).outcome).toBe('rejected');
    expect(githubClient.getApprovalComments).toHaveBeenCalledTimes(3);
    const second = fixture();
    const outageClient = client([], second.receipt);
    outageClient.getApprovalComments.mockRejectedValue(new Error('private'));
    expect((await service(outageClient).pollForApproval(ISSUE, second.request)).outcome).toBe('unavailable');
    expect(outageClient.getApprovalComments).toHaveBeenCalledTimes(3);
  });

  it('scans before sleeping and catches a reply between posting and waiting', async () => {
    const { request, receipt } = fixture();
    expect((await service(client([reply(`/approve ${REQUEST_ID}`)], receipt), { pollingInterval: 60_000 })
      .pollForApproval(ISSUE, request)).outcome).toBe('allowed-once');
  });
});
