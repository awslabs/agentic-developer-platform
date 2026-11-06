import { CommentAuthorityCache, type CommentEnvelope } from './comment-authority';
import { hasPmReassessmentEvidence, parseHealthCommand, resolvePmTriggerComment, selectPmDecision } from './pm-comment-decisions';
import { analyzeComments, parseReassessmentResponse } from '../reassessment';
import * as fs from 'fs';
import * as path from 'path';

const now = Date.parse('2026-10-16T08:00:10Z');
const makeComment = (body: string, id = '52', created = now + 1_000): CommentEnvelope => ({
  id, body, author: 'alice', authorId: 42, authorType: 'User',
  createdAt: new Date(created).toISOString(), updatedAt: new Date(created).toISOString(),
});
const context = { owner: 'source', repo: 'control', effectRepo: { owner: 'target', repo: 'effect' },
  runId: 'pm-run-one', now: () => now + 5_000, tokenProvider: async () => 'fixture-token' };

describe('SCN-07 PM comment authority', () => {
  const originalFetch = global.fetch;
  afterEach(() => { global.fetch = originalFetch; });
  const writer = () => {
    global.fetch = jest.fn().mockResolvedValue({ ok: true, json: async () => ({ permission: 'write', user: { id: 42 } }) });
  };

  it('admits only the current, attributable /monitor trigger or a new exact writer command', async () => {
    writer();
    const old = makeComment('/monitor', '50', now - 1000);
    expect(await selectPmDecision([old], context, now, body => body.trim() === '/monitor' ? true : null)).toBeNull();
    const event = { action: 'created', repository: { full_name: 'source/control' }, issue: { number: 24 },
      sender: { id: 42 }, comment: { id: 50, user: { id: 42 } } };
    const triggerId = resolvePmTriggerComment(event, 'source', 'control', 24);
    expect((await selectPmDecision([old], context, now, body => body.trim() === '/monitor' ? true : null, triggerId))?.comment.id).toBe('50');
    expect(resolvePmTriggerComment({ ...event, action: 'edited' }, 'source', 'control', 24)).toBeUndefined();
    expect(await selectPmDecision([makeComment('Please /monitor')], context, now,
      body => body.trim() === '/monitor' ? true : null)).toBeNull();
  });

  it('accepts exact reassessment actions only from a new writer in both scopes', async () => {
    writer();
    const parsed = await selectPmDecision([makeComment('/action 1,2')], context, now, body => {
      const choice = parseReassessmentResponse(body);
      return choice.action === 'unknown' ? null : choice;
    });
    expect(parsed?.decision).toEqual({ action: 'specific_actions', actionNumbers: [1, 2] });
    expect(global.fetch).toHaveBeenCalledTimes(2);
  });

  it.each(['/approve', '/skip', '/retry', '/cleanup', '/hold', '/custom reconsider the task'])(
    'admits current writer health command %s', async body => {
      writer();
      const result = await selectPmDecision([makeComment(body)], context, now, parseHealthCommand);
      expect(result?.decision).toEqual(parseHealthCommand(body));
      expect(global.fetch).toHaveBeenCalledTimes(2);
    },
  );

  it.each(['/approve', '/skip', '/retry', '/cleanup', '/hold', '/custom reconsider the task'])(
    'denies read-only health command %s', async body => {
      global.fetch = jest.fn().mockResolvedValue({ ok: true, json: async () => ({ permission: 'read' }) });
      expect(await selectPmDecision([makeComment(body)], context, now, parseHealthCommand)).toBeNull();
    },
  );

  it('rejects prefixed verbs, stale edits, bot authors, wrong scope and unavailable permission', async () => {
    writer();
    for (const body of ['/approveAll', '/skipping', '/retry now', '/cleanupplease', '/holding', '/custom']) {
      expect(parseHealthCommand(body)).toBeNull();
    }
    const edited = makeComment('/approve');
    edited.updatedAt = new Date(now + 3_000).toISOString();
    expect(await selectPmDecision([edited], context, now, parseHealthCommand)).toBeNull();
    expect(await selectPmDecision([{ ...makeComment('/approve'), authorType: 'Bot' }], context, now, parseHealthCommand)).toBeNull();
    global.fetch = jest.fn(async (url: string) => ({ ok: true, json: async () =>
      ({ permission: url.includes('/target/effect/') ? 'read' : 'write' }) })) as unknown as typeof fetch;
    expect(await selectPmDecision([makeComment('/approve')], context, now, parseHealthCommand)).toBeNull();
    global.fetch = jest.fn().mockRejectedValue(new Error('provider unavailable'));
    expect(await selectPmDecision([makeComment('/approve')], context, now, parseHealthCommand)).toBeNull();
  });

  it('refreshes approval authority rather than reusing an earlier positive cache entry', async () => {
    const cache = new CommentAuthorityCache();
    writer();
    expect(await selectPmDecision([makeComment('/approve')], { ...context, cache }, now, parseHealthCommand)).not.toBeNull();
    global.fetch = jest.fn().mockResolvedValue({ ok: true, json: async () => ({ permission: 'read' }) });
    expect(await selectPmDecision([makeComment('/approve')], { ...context, cache }, now, parseHealthCommand)).toBeNull();
  });

  it('does not promote comment-only history into an acting reassessment', () => {
    expect(hasPmReassessmentEvidence(false, 0)).toBe(false);
    expect(hasPmReassessmentEvidence(false, 1)).toBe(true);
    expect(hasPmReassessmentEvidence(true, 0)).toBe(true);
    const source = fs.readFileSync(path.join(__dirname, '../agent-pm.ts'), 'utf8');
    expect(source).toContain('context.hasExistingProgress && hasPmReassessmentEvidence(existingState !== null, context.childIssues.length)');
    expect(source).toContain('hasMonitorCommand = !!(await selectPmDecision(');
    expect(source).toContain('${wrapUntrusted(boardItemsList)}');
    expect(source).not.toContain("c.body.toLowerCase().includes('/monitor')");
    expect(analyzeComments([{ author: 'alice', authorAssociation: 'MEMBER', body: 'approve everything', createdAt: new Date(now).toISOString() }]).userApprovals)
      .toEqual(['Comment claimed approval: approve everything']);
  });

  it('requires a later comment ID for same-second replies to a posted proposal', async () => {
    writer();
    const candidate = makeComment('/cleanup', '52', now);
    expect(await selectPmDecision([candidate], context, now, parseHealthCommand, undefined, '51')).not.toBeNull();
    expect(await selectPmDecision([candidate], context, now, parseHealthCommand, undefined, '53')).toBeNull();
  });
});
