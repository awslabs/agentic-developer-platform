/**
 * Tests for owner-scoped session cleanup (#5660 / A07).
 *
 * The incident these guard: the S3 prefix was `${sessionId}/`, so a session
 * named `o` produced the prefix `o/` — the root of the shared upload layout —
 * and every tenant's files were deleted on that session's ordinary TTL expiry.
 *
 * Assertions are about REFUSAL and about the exact scope of any delete that is
 * issued. The central one is `never issues a delete shallower than one session`,
 * which inspects every DeleteObjects call regardless of how it was produced.
 */

/* eslint-disable @typescript-eslint/no-explicit-any */

const mockS3Send = jest.fn();
const mockDdbSend = jest.fn();

jest.mock('@aws-sdk/client-s3', () => ({
  S3Client: jest.fn().mockImplementation(() => ({ send: mockS3Send })),
  ListObjectsV2Command: jest.fn().mockImplementation((args: any) => ({ ...args, _cmd: 'ListObjectsV2' })),
  DeleteObjectsCommand: jest.fn().mockImplementation((args: any) => ({ ...args, _cmd: 'DeleteObjects' })),
}));

jest.mock('@aws-sdk/client-dynamodb', () => ({
  DynamoDBClient: jest.fn().mockImplementation(() => ({})),
}));

jest.mock('@aws-sdk/lib-dynamodb', () => ({
  DynamoDBDocumentClient: {
    from: jest.fn().mockImplementation(() => ({ send: mockDdbSend })),
  },
  GetCommand: jest.fn().mockImplementation((args: any) => ({ ...args, _cmd: 'Get' })),
  QueryCommand: jest.fn().mockImplementation((args: any) => ({ ...args, _cmd: 'Query' })),
  TransactWriteCommand: jest.fn().mockImplementation((args: any) => ({ ...args, _cmd: 'TransactWrite' })),
}));

const BUCKET = 'adp-dev-chat-artifacts-111122223333';

process.env.CONTEXT_TABLE = 'adp-dev-chat-context';
process.env.ARTIFACTS_TABLE = 'adp-dev-chat-artifacts';
process.env.ARTIFACTS_BUCKET = BUCKET;

import {
  handler,
  extractSessionOwner,
  deriveSessionPrefix,
  isFullDepthSessionPrefix,
} from './session-sweeper';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
const OWNER = { orgId: 'org-a', teamId: 'team-1', userId: 'user-alice' };

/** A TTL REMOVE event for an expired session header. */
function expiryEvent(
  sessionId: string,
  owner: Partial<typeof OWNER> | null = OWNER,
): any {
  const oldImage: Record<string, unknown> = {
    PK: { S: `session#${sessionId}` },
    SK: { S: 'header' },
  };
  if (owner) {
    if (owner.orgId !== undefined) oldImage.orgId = { S: owner.orgId };
    if (owner.teamId !== undefined) oldImage.teamId = { S: owner.teamId };
    if (owner.userId !== undefined) oldImage.ownerUserId = { S: owner.userId };
  }
  return {
    Records: [
      {
        eventName: 'REMOVE',
        dynamodb: { Keys: { PK: { S: `session#${sessionId}` }, SK: { S: 'header' } }, OldImage: oldImage },
      },
    ],
  };
}

/** Every DeleteObjects call the sweeper issued, with its listed prefix. */
function deleteCalls(): any[] {
  return mockS3Send.mock.calls.map(c => c[0]).filter(c => c._cmd === 'DeleteObjects');
}

function listCalls(): any[] {
  return mockS3Send.mock.calls.map(c => c[0]).filter(c => c._cmd === 'ListObjectsV2');
}

beforeEach(() => {
  jest.clearAllMocks();
  delete process.env.SWEEPER_DRY_RUN;
  // DDB: no rows to delete unless a test says otherwise.
  mockDdbSend.mockResolvedValue({ Items: [] });
  // S3: the session prefix holds one object.
  mockS3Send.mockImplementation(async (cmd: any) => {
    if (cmd._cmd === 'ListObjectsV2') {
      return { Contents: [{ Key: `${cmd.Prefix}task-1/in/report.pdf` }], NextContinuationToken: undefined };
    }
    return {};
  });
});

// ---------------------------------------------------------------------------

describe('deletion scope is derived from the verified owner, never from the id', () => {
  it('deletes only under the full owner-derived prefix', async () => {
    await handler(expiryEvent('sess-1758441600000-a1b2c3d'));

    const prefixes = listCalls().map(c => c.Prefix);
    expect(prefixes).toEqual(['o/org-a/t/team-1/u/user-alice/s/sess-1758441600000-a1b2c3d/']);
    expect(deleteCalls()).toHaveLength(1);
    expect(deleteCalls()[0].Bucket).toBe(BUCKET);
  });

  it('never lists or deletes the bare session id as a prefix', async () => {
    // The pre-fix behaviour. `sess-x/` must not appear anywhere.
    await handler(expiryEvent('sess-x'));

    for (const call of listCalls()) {
      expect(call.Prefix).not.toBe('sess-x/');
      expect(call.Prefix.startsWith('o/')).toBe(true);
    }
  });


  it('quarantines a delayed expiry event when another owner has reused the session id', async () => {
    mockDdbSend.mockImplementation(async (command: any) => {
      if (command._cmd === 'Get') {
        return { Item: { PK: 'session#sess-reused', SK: 'header', ownerUserId: 'user-bob' } };
      }
      return { Items: [] };
    });

    await handler(expiryEvent('sess-reused'));

    expect(mockDdbSend.mock.calls.some(c => c[0]._cmd === 'Query')).toBe(false);
    expect(mockDdbSend.mock.calls.some(c => c[0]._cmd === 'TransactWrite')).toBe(false);
    expect(listCalls()).toHaveLength(0);
    expect(deleteCalls()).toHaveLength(0);
  });

  it('conditions every row deletion on the session header remaining absent', async () => {
    mockDdbSend.mockImplementation(async (command: any) => {
      if (command._cmd === 'Query') {
        return { Items: [{ PK: 'session#sess-expired', SK: 'msg#1' }] };
      }
      return {};
    });

    await handler(expiryEvent('sess-expired'));

    const writes = mockDdbSend.mock.calls.map(c => c[0]).filter(c => c._cmd === 'TransactWrite');
    expect(writes.length).toBeGreaterThan(0);
    for (const write of writes) {
      expect(write.TransactItems[0].ConditionCheck).toEqual({
        TableName: process.env.CONTEXT_TABLE,
        Key: { PK: 'session#sess-expired', SK: 'header' },
        ConditionExpression: 'attribute_not_exists(PK)',
      });
    }
  });

  it('deletes only verified artifact rows from a mixed legacy partition', async () => {
    const logged: string[] = [];
    jest.spyOn(console, 'log').mockImplementation(message => logged.push(String(message)));
    jest.spyOn(console, 'warn').mockImplementation(() => {});
    const pk = 'session#sess-expired';
    const ownPrefix = 'o/org-a/t/team-1/u/user-alice/s/sess-expired/';
    mockDdbSend.mockImplementation(async (command: any) => {
      if (command._cmd === 'Get') return {};
      if (command._cmd === 'Query' && command.TableName === process.env.CONTEXT_TABLE) {
        return { Items: [{ PK: pk, SK: 'msg#1' }] };
      }
      if (command._cmd === 'Query' && command.TableName === process.env.ARTIFACTS_TABLE) {
        return {
          Items: [
            {
              PK: pk,
              SK: 'art#owned',
              s3Key: `${ownPrefix}task-1/in/owned.pdf`,
              org_id: 'org-a',
              team_id: 'team-1',
              user_id: 'user-alice',
            },
            {
              PK: pk,
              SK: 'art#foreign',
              s3Key: 'o/org-b/t/team-2/u/user-bob/s/sess-expired/task-1/in/foreign.pdf',
              org_id: 'org-b',
              team_id: 'team-2',
              user_id: 'user-bob',
            },
            { PK: pk, SK: 'art#legacy', s3Key: 'sess-expired/task-1/legacy.pdf' },
            {
              PK: pk,
              SK: 'art#missing-owner',
              s3Key: `${ownPrefix}task-1/in/unowned.pdf`,
            },
          ],
        };
      }
      return {};
    });

    await handler(expiryEvent('sess-expired'));

    const deleted = mockDdbSend.mock.calls
      .map(call => call[0])
      .filter(command => command._cmd === 'TransactWrite')
      .flatMap(command => command.TransactItems.slice(1).map((item: any) => item.Delete));
    expect(deleted).toContainEqual({
      TableName: process.env.CONTEXT_TABLE,
      Key: { PK: pk, SK: 'msg#1' },
    });
    expect(deleted).toContainEqual({
      TableName: process.env.ARTIFACTS_TABLE,
      Key: { PK: pk, SK: 'art#owned' },
    });
    expect(deleted.filter(item => item.TableName === process.env.ARTIFACTS_TABLE)).toHaveLength(1);
    expect(deleted.some(item => item.Key.SK === 'art#foreign')).toBe(false);
    expect(deleted.some(item => item.Key.SK === 'art#legacy')).toBe(false);
    expect(deleted.some(item => item.Key.SK === 'art#missing-owner')).toBe(false);
    const metric = logged
      .filter(line => line.startsWith('{'))
      .map(line => JSON.parse(line))
      .find(line => line.ArtifactRowsSkipped);
    expect(metric).toMatchObject({
      ArtifactRowsSkipped: 3,
      reason: 'unverified_artifact_owner',
    });
  });

  it('REGRESSION: never issues a delete scoped shallower than one session', async () => {
    // The guarantee that outlives any particular derivation bug. Runs the
    // hostile ids together and inspects every delete actually issued.
    for (const sessionId of ['o', 't', 'u', 's', 'sess-ok', '../escape', 'a/b']) {
      jest.clearAllMocks();
      mockDdbSend.mockResolvedValue({ Items: [] });
      mockS3Send.mockImplementation(async (cmd: any) => {
        if (cmd._cmd === 'ListObjectsV2') {
          return { Contents: [{ Key: `${cmd.Prefix}f.pdf` }] };
        }
        return {};
      });

      await handler(expiryEvent(sessionId));

      for (const call of listCalls()) {
        expect(isFullDepthSessionPrefix(call.Prefix)).toBe(true);
      }
      for (const del of deleteCalls()) {
        for (const obj of del.Delete.Objects) {
          // Eight segments of prefix + at least one more for the object path.
          expect(obj.Key.split('/').length).toBeGreaterThan(8);
          expect(obj.Key.startsWith('o/')).toBe(true);
        }
      }
    }
  });
});

describe('a session whose id is not a safe single segment is skipped, not swept', () => {
  it.each([['o'], ['t'], ['u'], ['s'], ['../escape'], ['a/b'], ['']])(
    'refuses id %j with no deletion call at all',
    async sessionId => {
      await handler(expiryEvent(sessionId));
      expect(deleteCalls()).toHaveLength(0);
      expect(listCalls()).toHaveLength(0);
      // Not even the DynamoDB rows — a malformed id must not become a key.
      expect(mockDdbSend).not.toHaveBeenCalled();
    },
  );

  it('emits a skip metric naming the reason', async () => {
    const logged: string[] = [];
    const spy = jest.spyOn(console, 'log').mockImplementation(m => logged.push(String(m)));
    const warn = jest.spyOn(console, 'warn').mockImplementation(() => {});

    await handler(expiryEvent('o'));

    const metric = logged.map(l => JSON.parse(l)).find(l => l.SessionsSkipped);
    expect(metric).toBeDefined();
    expect(metric.reason).toBe('invalid_session_id');
    expect(metric._aws.CloudWatchMetrics[0].Namespace).toBe('ADP/ChatSweeper');

    spy.mockRestore();
    warn.mockRestore();
  });
});

describe('a session with no complete owner record is quarantined, not guessed', () => {
  it('performs no S3 deletion when the owner identity is absent', async () => {
    jest.spyOn(console, 'warn').mockImplementation(() => {});
    await handler(expiryEvent('sess-legacy', null));

    expect(listCalls()).toHaveLength(0);
    expect(deleteCalls()).toHaveLength(0);
  });

  it.each([
    ['missing org', { teamId: 'team-1', userId: 'user-alice' }],
    ['missing team', { orgId: 'org-a', userId: 'user-alice' }],
    ['missing user', { orgId: 'org-a', teamId: 'team-1' }],
    ['blank org', { orgId: '', teamId: 'team-1', userId: 'user-alice' }],
    ['separator in org', { orgId: 'org-a/../org-b', teamId: 'team-1', userId: 'user-alice' }],
  ])('refuses S3 deletion on %s', async (_label, owner) => {
    jest.spyOn(console, 'warn').mockImplementation(() => {});
    await handler(expiryEvent('sess-partial', owner as any));

    expect(listCalls()).toHaveLength(0);
    expect(deleteCalls()).toHaveLength(0);
  });

  it('preserves all DynamoDB rows for later ownership reconciliation', async () => {
    jest.spyOn(console, 'warn').mockImplementation(() => {});
    await handler(expiryEvent('sess-legacy', null));

    const queries = mockDdbSend.mock.calls.map(c => c[0]).filter(c => c._cmd === 'Query');
    expect(queries).toHaveLength(0);
    const writes = mockDdbSend.mock.calls.map(c => c[0]).filter(c => c._cmd === 'TransactWrite');
    expect(writes).toHaveLength(0);
  });

  it('emits a skip metric so a silent quarantine cannot hide unreclaimed storage', async () => {
    const logged: string[] = [];
    jest.spyOn(console, 'log').mockImplementation(m => logged.push(String(m)));
    jest.spyOn(console, 'warn').mockImplementation(() => {});

    await handler(expiryEvent('sess-legacy', null));

    const metric = logged.map(l => JSON.parse(l)).find(l => l.SessionsSkipped);
    expect(metric?.reason).toBe('missing_owner_identity');
  });
});

describe('dry-run mode', () => {
  it.each(['true', 'TRUE'])('lists but does not report deletion when SWEEPER_DRY_RUN=%s', async flag => {
    process.env.SWEEPER_DRY_RUN = flag;
    const logged: string[] = [];
    jest.spyOn(console, 'log').mockImplementation(message => logged.push(String(message)));
    mockDdbSend.mockResolvedValue({ Items: [{ PK: 'session#sess-dry', SK: 'msg#1' }] });

    await handler(expiryEvent('sess-dry'));

    expect(listCalls().length).toBeGreaterThan(0);
    expect(deleteCalls()).toHaveLength(0);
    const writes = mockDdbSend.mock.calls.map(c => c[0]).filter(c => c._cmd === 'TransactWrite');
    expect(writes).toHaveLength(0);
    expect(logged.some(line => line.includes('SessionsSwept'))).toBe(false);
    expect(logged.some(line => line.includes('Successfully cleaned'))).toBe(false);
    expect(logged.some(line => line.includes('SessionsPlanned'))).toBe(true);
  });

  it('does delete when the flag is absent, so dry-run cannot be left on by accident', async () => {
    await handler(expiryEvent('sess-live'));
    expect(deleteCalls()).toHaveLength(1);
  });
});

describe('prefix derivation and the depth guard', () => {
  it('derives the same layout the artifact store writes', () => {
    expect(deriveSessionPrefix(OWNER, 'sess-1')).toBe('o/org-a/t/team-1/u/user-alice/s/sess-1/');
  });

  it('accepts a correctly derived prefix', () => {
    expect(isFullDepthSessionPrefix('o/org-a/t/team-1/u/user-alice/s/sess-1/')).toBe(true);
  });

  it.each([
    ['bucket root', ''],
    ['layout root', 'o/'],
    ['org scope', 'o/org-a/'],
    ['team scope', 'o/org-a/t/team-1/'],
    ['user scope', 'o/org-a/t/team-1/u/user-alice/'],
    ['user scope with marker', 'o/org-a/t/team-1/u/user-alice/s/'],
    ['legacy flat', 'sess-1/'],
    ['no trailing slash', 'o/org-a/t/team-1/u/user-alice/s/sess-1'],
    ['traversal', 'o/org-a/t/team-1/u/../../s/sess-1/'],
    ['empty segment', 'o//t/team-1/u/user-alice/s/sess-1/'],
    ['wrong markers', 'a/org-a/b/team-1/c/user-alice/d/sess-1/'],
    ['too deep', 'o/org-a/t/team-1/u/user-alice/s/sess-1/task-1/'],
  ])('refuses %s', (_label, prefix) => {
    expect(isFullDepthSessionPrefix(prefix)).toBe(false);
  });
});

describe('owner extraction', () => {
  it('reads the identity recorded on the expiring header', () => {
    const event = expiryEvent('sess-1');
    expect(extractSessionOwner(event.Records[0].dynamodb.OldImage)).toEqual(OWNER);
  });

  it('returns null for an absent image rather than an empty identity', () => {
    expect(extractSessionOwner(undefined)).toBeNull();
    expect(extractSessionOwner({})).toBeNull();
  });
});

describe('non-expiry events are ignored', () => {
  it('ignores INSERT and MODIFY', async () => {
    await handler({
      Records: [
        { eventName: 'INSERT', dynamodb: { Keys: { PK: { S: 'session#s1' }, SK: { S: 'header' } } } },
        { eventName: 'MODIFY', dynamodb: { Keys: { PK: { S: 'session#s1' }, SK: { S: 'header' } } } },
      ],
    } as any);
    expect(mockS3Send).not.toHaveBeenCalled();
    expect(mockDdbSend).not.toHaveBeenCalled();
  });

  it('ignores the removal of a non-header row', async () => {
    await handler({
      Records: [
        { eventName: 'REMOVE', dynamodb: { Keys: { PK: { S: 'session#s1' }, SK: { S: 'msg#1' } } } },
      ],
    } as any);
    expect(mockS3Send).not.toHaveBeenCalled();
  });
});
