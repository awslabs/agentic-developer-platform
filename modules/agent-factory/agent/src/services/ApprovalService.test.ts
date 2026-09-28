/**
 * Behavioral tests for the bounded, fail-closed approval poll (issue #4181).
 *
 * The function under test previously looped `while (true)` with no deadline, no
 * iteration cap and no authorization check, and approved on a bare `/approve`
 * substring from any commenter. These tests pin the replacement's safety
 * properties — above all that an unanswered request DENIES rather than hangs or
 * approves. They assert returned outcomes, not source text.
 */

/* eslint-disable @typescript-eslint/no-explicit-any */

import { ApprovalService } from './ApprovalService';
import { Config } from '../types';

const ISSUE = 42;
const REQUEST_ID = 'plan-42-r0';
const APPROVER = 'maintainer-alice';
const OUTSIDER = 'random-drive-by';

/** Fixed instant so `since` filtering is deterministic (no Date.now() reliance). */
const SINCE = new Date('2026-08-26T00:00:00Z');
const AFTER = '2026-08-26T00:05:00Z';
const BEFORE = '2026-08-25T23:00:00Z';

function makeConfig(overrides: Partial<Config> = {}): Config {
  return {
    awsRegion: 'us-east-1',
    secretPrefix: 'test',
    // 0ms polling keeps the bounded loop fast under fake timers.
    pollingInterval: 0,
    maxRetries: 3,
    logLevel: 'INFO',
    bedrockModel: 'test-model',
    ...overrides,
  };
}

function makeLogger() {
  return { info: jest.fn(), warn: jest.fn(), error: jest.fn(), debug: jest.fn() };
}

function comment(body: string, author: string, created_at: string = AFTER) {
  return { id: 1, body, author, created_at };
}

/**
 * GitHub client double. `permission` maps login → repo permission level;
 * anyone absent resolves to 'none' (unauthorized).
 */
function makeClient(
  comments: ReturnType<typeof comment>[],
  permission: Record<string, string> = { [APPROVER]: 'write' }
) {
  return {
    getComments: jest.fn().mockResolvedValue(comments),
    getUserPermission: jest.fn(async (user: string) => permission[user] ?? 'none'),
  };
}

function makeService(client: any, config: Config = makeConfig()) {
  const logger = makeLogger();
  const service = new ApprovalService(client as any, logger as any, config);
  return { service, logger };
}

describe('ApprovalService.pollForApproval', () => {
  const originalEnv = { ...process.env };

  beforeEach(() => {
    delete process.env.APPROVAL_TIMEOUT_MS;
    delete process.env.APPROVAL_MAX_POLLS;
    delete process.env.GITHUB_ACTOR;
  });

  afterEach(() => {
    process.env = { ...originalEnv };
    jest.useRealTimers();
  });

  it('returns allowed-once and records the approver when an authorized user approves', async () => {
    const client = makeClient([comment(`/approve ${REQUEST_ID}`, APPROVER)]);
    const { service } = makeService(client);

    const result = await service.pollForApproval(ISSUE, SINCE, REQUEST_ID);

    expect(result.outcome).toBe('allowed-once');
    // Identity must be present so an audit can answer who approved what.
    expect(result.approver).toBe(APPROVER);
  });

  it('returns rejected with feedback when an authorized user denies', async () => {
    const client = makeClient([
      comment(`/reject ${REQUEST_ID} use a different approach`, APPROVER),
    ]);
    const { service } = makeService(client);

    const result = await service.pollForApproval(ISSUE, SINCE, REQUEST_ID);

    expect(result.outcome).toBe('rejected');
    expect(result.approver).toBe(APPROVER);
    expect(result.feedback).toBe('use a different approach');
  });

  // THE load-bearing assertion. If this fails, the fix is not a fix.
  it('DENIES on deadline expiry with no answer (never approves, never hangs)', async () => {
    process.env.APPROVAL_TIMEOUT_MS = '1';
    // A generous max-polls proves the DEADLINE is what stops the loop here, and
    // a 5ms poll interval guarantees the 1ms deadline is passed on the first
    // wake-up regardless of timer granularity.
    process.env.APPROVAL_MAX_POLLS = '1000';
    const client = makeClient([
      // Even a valid, authorized approval cannot be seen after the deadline —
      // an expired request is closed, not retroactively approvable.
      comment(`/approve ${REQUEST_ID}`, APPROVER),
    ]);
    const { service } = makeService(client, makeConfig({ pollingInterval: 5 }));

    const result = await service.pollForApproval(ISSUE, SINCE, REQUEST_ID);

    expect(result.outcome).toBe('rejected');
    // Deny-on-expiry carries no approver — that is how callers distinguish it
    // from an actionable human rejection.
    expect(result.approver).toBeUndefined();
    // It bailed at the deadline instead of burning through 1000 polls.
    expect(client.getComments).not.toHaveBeenCalled();
  });

  it('DENIES when the iteration cap is reached, proving both bounds are enforced', async () => {
    // A huge timeout guarantees the deadline cannot be what stops the loop, so
    // this test isolates the max-polls bound.
    process.env.APPROVAL_TIMEOUT_MS = String(60 * 60 * 1000);
    process.env.APPROVAL_MAX_POLLS = '3';
    const client = makeClient([]);
    const { service } = makeService(client);

    const result = await service.pollForApproval(ISSUE, SINCE, REQUEST_ID);

    expect(result.outcome).toBe('rejected');
    // Bounded: exactly the cap, then denial — not an open-ended wait.
    expect(client.getComments).toHaveBeenCalledTimes(3);
  });

  it('ignores an approve intent from an unauthorized commenter (the closed vulnerability)', async () => {
    process.env.APPROVAL_MAX_POLLS = '2';
    const client = makeClient(
      [comment(`/approve ${REQUEST_ID}`, OUTSIDER)],
      { [OUTSIDER]: 'read' } // can comment, cannot approve
    );
    const { service } = makeService(client);

    const result = await service.pollForApproval(ISSUE, SINCE, REQUEST_ID);

    expect(result.outcome).not.toBe('allowed-once');
    expect(result.outcome).toBe('rejected');
  });

  it('does not let the agent self-approve via its own comment', async () => {
    process.env.APPROVAL_MAX_POLLS = '2';
    process.env.GITHUB_ACTOR = 'aws-e-adp-agent-dev';
    // Give the bot admin rights to prove the bot check, not the permission
    // check, is what rejects it.
    const client = makeClient(
      [
        comment(`/approve ${REQUEST_ID}`, 'aws-e-adp-agent-dev'),
        comment(`/approve ${REQUEST_ID}`, 'github-actions[bot]'),
      ],
      { 'aws-e-adp-agent-dev': 'admin', 'github-actions[bot]': 'admin' }
    );
    const { service } = makeService(client);

    const result = await service.pollForApproval(ISSUE, SINCE, REQUEST_ID);

    expect(result.outcome).not.toBe('allowed-once');
  });

  it('ignores a stale approve intent naming a different request (named, not positional)', async () => {
    process.env.APPROVAL_MAX_POLLS = '2';
    const client = makeClient([
      // A real approval — of some OTHER request on a busy issue.
      comment('/approve plan-42-r0', APPROVER),
    ]);
    const { service } = makeService(client);

    // We are now asking about revision 1; revision 0's approval must not carry over.
    const result = await service.pollForApproval(ISSUE, SINCE, 'plan-42-r1');

    expect(result.outcome).not.toBe('allowed-once');
  });

  it('ignores a bare /approve with no request id', async () => {
    process.env.APPROVAL_MAX_POLLS = '2';
    const client = makeClient([comment('/approve', APPROVER)]);
    const { service } = makeService(client);

    const result = await service.pollForApproval(ISSUE, SINCE, REQUEST_ID);

    expect(result.outcome).not.toBe('allowed-once');
  });

  it('returns unavailable — distinct from rejected — on repeated polling errors', async () => {
    const client = makeClient([]);
    client.getComments = jest.fn().mockRejectedValue(new Error('502 bad gateway'));
    const { service } = makeService(client, makeConfig({ maxRetries: 3 }));

    const result = await service.pollForApproval(ISSUE, SINCE, REQUEST_ID);

    // A transport failure must never be recorded as a human denial.
    expect(result.outcome).toBe('unavailable');
    expect(result.outcome).not.toBe('rejected');
    // It gave up rather than looping forever swallowing the error.
    expect(client.getComments).toHaveBeenCalledTimes(3);
  });

  it('recovers when a transient error is followed by a successful poll', async () => {
    const client = makeClient([]);
    client.getComments = jest
      .fn()
      .mockRejectedValueOnce(new Error('transient 500'))
      .mockResolvedValue([comment(`/approve ${REQUEST_ID}`, APPROVER)]);
    const { service } = makeService(client, makeConfig({ maxRetries: 3 }));

    const result = await service.pollForApproval(ISSUE, SINCE, REQUEST_ID);

    expect(result.outcome).toBe('allowed-once');
  });

  it('falls back to safe bounded defaults when the timeout env is unparseable', async () => {
    process.env.APPROVAL_TIMEOUT_MS = 'not-a-number';
    process.env.APPROVAL_MAX_POLLS = 'garbage';
    const client = makeClient([comment(`/approve ${REQUEST_ID}`, APPROVER)]);
    const { service } = makeService(client);

    // Resolving to the default (not to unbounded) means this still terminates.
    const result = await service.pollForApproval(ISSUE, SINCE, REQUEST_ID);

    expect(result.outcome).toBe('allowed-once');
  });

  it('treats a non-positive max-polls env as the safe default rather than zero polls', async () => {
    // A literal 0 would mean "never poll", silently denying every request.
    process.env.APPROVAL_MAX_POLLS = '0';
    const client = makeClient([comment(`/approve ${REQUEST_ID}`, APPROVER)]);
    const { service } = makeService(client);

    const result = await service.pollForApproval(ISSUE, SINCE, REQUEST_ID);

    expect(result.outcome).toBe('allowed-once');
    expect(client.getComments).toHaveBeenCalled();
  });

  it('denies when the permission lookup itself fails (fails closed)', async () => {
    process.env.APPROVAL_MAX_POLLS = '2';
    const client = makeClient([comment(`/approve ${REQUEST_ID}`, APPROVER)]);
    client.getUserPermission = jest.fn().mockRejectedValue(new Error('403 forbidden'));
    const { service } = makeService(client);

    const result = await service.pollForApproval(ISSUE, SINCE, REQUEST_ID);

    expect(result.outcome).not.toBe('allowed-once');
  });

  it('ignores an approve intent posted before the wait began', async () => {
    process.env.APPROVAL_MAX_POLLS = '2';
    const client = makeClient([comment(`/approve ${REQUEST_ID}`, APPROVER, BEFORE)]);
    const { service } = makeService(client);

    const result = await service.pollForApproval(ISSUE, SINCE, REQUEST_ID);

    expect(result.outcome).not.toBe('allowed-once');
  });

  it('accepts maintain and admin permission levels, not just write', async () => {
    for (const level of ['admin', 'maintain']) {
      const client = makeClient([comment(`/approve ${REQUEST_ID}`, APPROVER)], {
        [APPROVER]: level,
      });
      const { service } = makeService(client);

      const result = await service.pollForApproval(ISSUE, SINCE, REQUEST_ID);
      expect(result.outcome).toBe('allowed-once');
    }
  });

  it('caches the permission lookup across polls on a chatty issue', async () => {
    process.env.APPROVAL_MAX_POLLS = '3';
    // Same unauthorized commenter seen on every poll.
    const client = makeClient([comment(`/approve ${REQUEST_ID}`, OUTSIDER)], {
      [OUTSIDER]: 'read',
    });
    const { service } = makeService(client);

    await service.pollForApproval(ISSUE, SINCE, REQUEST_ID);

    // Three polls, but only one permission API call.
    expect(client.getComments).toHaveBeenCalledTimes(3);
    expect(client.getUserPermission).toHaveBeenCalledTimes(1);
  });
});
