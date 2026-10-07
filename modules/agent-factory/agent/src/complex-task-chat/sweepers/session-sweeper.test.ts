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
const OWNER_ALIASES = {
  orgId: OWNER.orgId,
  tenantId: OWNER.orgId,
  teamId: OWNER.teamId,
  ownerUserId: OWNER.userId,
  org_id: OWNER.orgId,
  tenant_id: OWNER.orgId,
  team_id: OWNER.teamId,
  user_id: OWNER.userId,
  owner_user_id: OWNER.userId,
};

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
    if (owner.orgId !== undefined) {
      oldImage.orgId = { S: owner.orgId };
      oldImage.tenantId = { S: owner.orgId };
    }
    if (owner.teamId !== undefined) oldImage.teamId = { S: owner.teamId };
    if (owner.userId !== undefined) oldImage.ownerUserId = { S: owner.userId };
  }
  return {
    Records: [
      {
        eventName: 'REMOVE',
        userIdentity: { type: 'Service', principalId: 'dynamodb.amazonaws.com' },
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
        return { Items: [{ PK: 'session#sess-expired', SK: 'msg#1', ...OWNER_ALIASES }] };
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

  it('leaves a context child that names another owner quarantined', async () => {
    mockDdbSend.mockImplementation(async (command: any) => {
      if (command._cmd === 'Query' && command.TableName === process.env.CONTEXT_TABLE) {
        return { Items: [
          { PK: 'session#sess-expired', SK: 'msg#own', ...OWNER_ALIASES },
          { PK: 'session#sess-expired', SK: 'sum#foreign', ownerUserId: 'user-bob' },
          { PK: 'session#sess-expired', SK: 'msg#unverified' },
        ] };
      }
      return { Items: [] };
    });
    await handler(expiryEvent('sess-expired'));
    const deletions = mockDdbSend.mock.calls.flatMap(call =>
      call[0]._cmd === 'TransactWrite' ? call[0].TransactItems.slice(1).map((entry: any) => entry.Delete?.Key.SK) : [],
    );
    expect(deletions).toContain('msg#own');
    expect(deletions).not.toContain('sum#foreign');
    expect(deletions).not.toContain('msg#unverified');
  });

  it('quarantines artifact rows from a mixed legacy partition', async () => {
    const logged: string[] = [];
    jest.spyOn(console, 'log').mockImplementation(message => logged.push(String(message)));
    jest.spyOn(console, 'warn').mockImplementation(() => {});
    const pk = 'session#sess-expired';
    const ownPrefix = 'o/org-a/t/team-1/u/user-alice/s/sess-expired/';
    mockDdbSend.mockImplementation(async (command: any) => {
      if (command._cmd === 'Get') return {};
      if (command._cmd === 'Query' && command.TableName === process.env.CONTEXT_TABLE) {
        return { Items: [{ PK: pk, SK: 'msg#1', ...OWNER_ALIASES }] };
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
      ConditionExpression: expect.stringContaining('ownerUserId = :user'),
      ExpressionAttributeValues: { ':org': OWNER.orgId, ':team': OWNER.teamId, ':user': OWNER.userId, ':null': null },
    });
    expect(deleted.filter(item => item.TableName === process.env.ARTIFACTS_TABLE)).toHaveLength(0);
    expect(deleted.some(item => item.Key.SK === 'art#foreign')).toBe(false);
    expect(deleted.some(item => item.Key.SK === 'art#legacy')).toBe(false);
    expect(deleted.some(item => item.Key.SK === 'art#missing-owner')).toBe(false);
    expect(listCalls()).toHaveLength(0);
    expect(deleteCalls()).toHaveLength(0);
    const metric = logged
      .filter(line => line.startsWith('{'))
      .map(line => JSON.parse(line))
      .find(line => line.ArtifactRowsSkipped);
    expect(metric).toMatchObject({
      ArtifactRowsSkipped: 3,
      reason: 'unverified_artifact_owner',
    });
  });

  it('quarantines the entire object prefix when a later catalog page has ambiguous ownership', async () => {
    const prefix = 'o/org-a/t/team-1/u/user-alice/s/sess-expired/';
    mockDdbSend.mockImplementation(async (command: any) => {
      if (command._cmd === 'Query' && command.TableName === process.env.ARTIFACTS_TABLE) {
        return command.ExclusiveStartKey
          ? { Items: [{ PK: 'session#sess-expired', SK: 'art#unowned', s3Key: `${prefix}private.pdf` }] }
          : { Items: [{ PK: 'session#sess-expired', SK: 'art#owned', s3Key: `${prefix}public.pdf`,
              org_id: OWNER.orgId, team_id: OWNER.teamId, user_id: OWNER.userId }],
              LastEvaluatedKey: { PK: 'session#sess-expired', SK: 'art#owned' } };
      }
      return { Items: [] };
    });
    await handler(expiryEvent('sess-expired'));
    expect(listCalls()).toHaveLength(0);
    expect(deleteCalls()).toHaveLength(0);
    expect(mockDdbSend.mock.calls.some(call => call[0]._cmd === 'TransactWrite' &&
      call[0].TransactItems.some((item: any) => item.Delete?.TableName === process.env.ARTIFACTS_TABLE))).toBe(false);
  });

  it('does not report success when S3 acknowledges only a partial deletion', async () => {
    mockS3Send.mockImplementation(async (command: any) => {
      if (command._cmd === 'ListObjectsV2') return { Contents: [{ Key: `${command.Prefix}file.pdf` }] };
      if (command._cmd === 'DeleteObjects') return { Errors: [{ Key: 'file.pdf', Code: 'AccessDenied' }] };
      return {};
    });
    await expect(handler(expiryEvent('sess-expired'))).rejects.toThrow('artifact deletion partially failed');
    expect(deleteCalls()).toHaveLength(1);
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

describe('personal-session ownership and cleanup', () => {
  const personalOwner = { ...OWNER, teamId: '' };
  const personalFields = { orgId: OWNER.orgId, tenantId: OWNER.orgId, teamId: '', ownerUserId: OWNER.userId };

  it('recognizes an explicitly empty team and derives its reserved full-depth prefix', () => {
    const event = expiryEvent('personal-session', personalOwner);
    expect(extractSessionOwner(event.Records[0].dynamodb.OldImage)).toEqual(personalOwner);
    expect(deriveSessionPrefix(personalOwner, 'personal-session')).toBe('o/org-a/t/~personal/u/user-alice/s/personal-session/');
    expect(isFullDepthSessionPrefix(deriveSessionPrefix(personalOwner, 'personal-session'))).toBe(true);
    const namedTeam = expiryEvent('personal-session', { ...personalOwner, teamId: '~personal' });
    expect(extractSessionOwner(namedTeam.Records[0].dynamodb.OldImage)).toBeNull();
  });

  it.each([undefined, { NULL: true }, { BOOL: false }, { N: '0' }])('rejects a missing or non-string team: %p', attribute => {
    const image = expiryEvent('personal-session', personalOwner).Records[0].dynamodb.OldImage;
    if (attribute === undefined) delete image.teamId;
    else image.teamId = attribute;
    expect(extractSessionOwner(image)).toBeNull();
  });

  it.each([{ S: 'other-team' }, { BOOL: true }])('rejects conflicting or malformed optional team aliases: %p', attribute => {
    const image = expiryEvent('personal-session', personalOwner).Records[0].dynamodb.OldImage;
    image.team_id = attribute;
    expect(extractSessionOwner(image)).toBeNull();
  });

  it('deletes only independently owned context and lists only the reserved artifact prefix', async () => {
    const pk = 'session#personal-session';
    mockDdbSend.mockImplementation(async (command: any) => {
      if (command._cmd === 'Query' && command.TableName === process.env.CONTEXT_TABLE) {
        return { Items: [
          { PK: pk, SK: 'msg#owned', ...personalFields },
          { PK: pk, SK: 'sum#unverified' },
          { PK: pk, SK: 'msg#foreign', ...personalFields, ownerUserId: 'other-user' },
          { PK: pk, SK: 'msg#wrong-team', ...personalFields, team_id: 'other-team' },
        ] };
      }
      return {};
    });

    await handler(expiryEvent('personal-session', personalOwner));

    const writes = mockDdbSend.mock.calls.map(call => call[0]).filter(command => command._cmd === 'TransactWrite');
    expect(writes).toHaveLength(1);
    const deletion = writes[0].TransactItems[1].Delete;
    expect(writes[0].TransactItems).toHaveLength(2);
    expect(deletion.Key).toEqual({ PK: pk, SK: 'msg#owned' });
    expect(deletion.ExpressionAttributeValues[':team']).toBe('');
    for (const [field, parameter] of [['orgId', ':org'], ['tenantId', ':org'], ['teamId', ':team'], ['ownerUserId', ':user']]) {
      expect(deletion.ConditionExpression.split(' AND ')).toContain(`${field} = ${parameter}`);
    }
    expect(mockDdbSend.mock.calls.some(call => call[0].TableName === process.env.ARTIFACTS_TABLE)).toBe(true);
    expect(listCalls().map(call => call.Prefix)).toEqual([deriveSessionPrefix(personalOwner, 'personal-session')]);
  });

  it('honors dry-run and recreated-header guards for personal cleanup', async () => {
    process.env.SWEEPER_DRY_RUN = 'true';
    mockDdbSend.mockResolvedValue({ Items: [{ PK: 'session#personal-session', SK: 'msg#owned', ...personalFields }] });
    await handler(expiryEvent('personal-session', personalOwner));
    expect(mockDdbSend.mock.calls.some(call => call[0]._cmd === 'Query')).toBe(true);
    expect(mockDdbSend.mock.calls.some(call => call[0]._cmd === 'TransactWrite')).toBe(false);
    expect(deleteCalls()).toHaveLength(0);

    mockDdbSend.mockClear();
    mockS3Send.mockClear();
    delete process.env.SWEEPER_DRY_RUN;
    mockDdbSend.mockResolvedValue({ Item: { PK: 'session#personal-session', SK: 'header' } });
    await handler(expiryEvent('personal-session', personalOwner));
    expect(mockDdbSend).toHaveBeenCalledTimes(1);
    expect(mockS3Send).not.toHaveBeenCalled();
  });

  it.each([false, true])('requires explicit personal catalog ownership before deleting its prefix: %s', ambiguous => {
    const prefix = deriveSessionPrefix(personalOwner, 'personal-session');
    const row = {
      PK: 'session#personal-session', SK: 'art#owned', s3Key: `${prefix}gateway/out/art_a`,
      ...personalFields, org_id: OWNER.orgId, team_id: '', user_id: OWNER.userId,
    };
    mockDdbSend.mockImplementation(async (command: any) => {
      if (command._cmd === 'Query' && command.TableName === process.env.ARTIFACTS_TABLE) {
        const unverified = { PK: row.PK, SK: 'art#unknown', s3Key: `${prefix}gateway/out/art_b` };
        return { Items: ambiguous ? [row, unverified] : [row] };
      }
      return {};
    });
    return handler(expiryEvent('personal-session', personalOwner)).then(() => {
      if (ambiguous) expect(mockS3Send).not.toHaveBeenCalled();
      else expect(listCalls().map(call => call.Prefix)).toEqual([prefix]);
    });
  });

  it.each(
    Object.entries(personalFields).flatMap(([field, expected]) =>
      ['removed', 'null'].map(change => ({ field, expected, change })),
    ),
  )('stops before S3 when personal artifact $field becomes $change after selection', async ({ field, expected, change }) => {
    const prefix = deriveSessionPrefix(personalOwner, 'personal-session');
    const row: Record<string, unknown> = {
      PK: 'session#personal-session', SK: 'art#owned', s3Key: `${prefix}gateway/out/art_a`,
      ...personalFields, org_id: OWNER.orgId, team_id: '', user_id: OWNER.userId,
    };
    const currentRow = { ...row };
    let transactionAttempted = false;
    mockDdbSend.mockImplementation(async (command: any) => {
      if (command._cmd === 'Query' && command.TableName === process.env.ARTIFACTS_TABLE) {
        return { Items: [{ ...row }] };
      }
      if (command._cmd === 'TransactWrite') {
        transactionAttempted = true;
        if (change === 'removed') delete currentRow[field];
        else currentRow[field] = null;
        const deletion = command.TransactItems[1].Delete;
        const parameter = field === 'ownerUserId' ? ':user' : field === 'teamId' ? ':team' : ':org';
        expect(deletion.Key).toEqual({ PK: row.PK, SK: row.SK });
        expect(deletion.ConditionExpression.split(' AND ')).toContain(`${field} = ${parameter}`);
        expect(deletion.ExpressionAttributeValues[parameter]).toBe(expected);
        expect(currentRow[field]).not.toBe(expected);
        throw Object.assign(new Error('personal artifact ownership changed'), { name: 'TransactionCanceledException' });
      }
      return {};
    });

    await expect(handler(expiryEvent('personal-session', personalOwner))).rejects.toThrow('personal artifact ownership changed');
    expect(transactionAttempted).toBe(true);
    expect(mockS3Send).not.toHaveBeenCalled();
  });
});

describe('ownership aliases and catalog deletion races', () => {
  const sessionId = 'sess-expired';
  const catalogRow = {
    PK: `session#${sessionId}`,
    SK: 'art#owned',
    s3Key: `${deriveSessionPrefix(OWNER, sessionId)}task-1/in/file.pdf`,
    org_id: OWNER.orgId,
    team_id: OWNER.teamId,
    user_id: OWNER.userId,
  };

  it.each(Object.keys(OWNER_ALIASES))('quarantines a header with conflicting %s', async field => {
    const event = expiryEvent(sessionId);
    for (const [alias, value] of Object.entries(OWNER_ALIASES)) event.Records[0].dynamodb.OldImage[alias] = { S: value };
    event.Records[0].dynamodb.OldImage[field] = { S: 'another-owner' };

    await handler(event);

    expect(mockDdbSend).not.toHaveBeenCalled();
    expect(mockS3Send).not.toHaveBeenCalled();
  });

  it.each([{ N: '123' }, { BOOL: false }, { M: {} }])('rejects malformed ownership aliases %j', async attribute => {
    const event = expiryEvent(sessionId);
    event.Records[0].dynamodb.OldImage.owner_user_id = attribute;
    await handler(event);
    expect(mockDdbSend).not.toHaveBeenCalled();
    expect(mockS3Send).not.toHaveBeenCalled();
  });

  it('accepts matching and null optional header aliases consistently with migration', async () => {
    const event = expiryEvent(sessionId);
    const image = event.Records[0].dynamodb.OldImage;
    for (const [field, value] of Object.entries(OWNER_ALIASES)) image[field] = { S: value };
    image.owner_user_id = { NULL: true };
    await handler(event);
    expect(deleteCalls()).toHaveLength(1);
  });

  it.each(Object.keys(OWNER_ALIASES))('quarantines a catalog prefix with conflicting %s', async field => {
    mockDdbSend.mockImplementation(async (command: any) => {
      if (command._cmd === 'Query' && command.TableName === process.env.ARTIFACTS_TABLE) {
        const stored = { ...catalogRow, [field]: 'another-owner' };
        const projected = Object.fromEntries(command.ProjectionExpression.split(', ').map((name: string) => [name, (stored as any)[name]]));
        return { Items: [projected] };
      }
      return { Items: [] };
    });

    await handler(expiryEvent(sessionId));

    expect(mockDdbSend.mock.calls.some(call => call[0]._cmd === 'TransactWrite')).toBe(false);
    expect(mockS3Send).not.toHaveBeenCalled();
  });

  it('conditions catalog deletion on the verified object key and every ownership alias', async () => {
    mockDdbSend.mockImplementation(async (command: any) => {
      if (command._cmd === 'Query' && command.TableName === process.env.ARTIFACTS_TABLE) {
        return { Items: [catalogRow] };
      }
      return { Items: [] };
    });

    await handler(expiryEvent(sessionId));

    const transaction = mockDdbSend.mock.calls.find(call => call[0]._cmd === 'TransactWrite')![0];
    const deletion = transaction.TransactItems[1].Delete;
    expect(deletion.Key).toEqual({ PK: catalogRow.PK, SK: catalogRow.SK });
    expect(deletion.ConditionExpression).toContain('s3Key = :s3Key');
    expect(deletion.ExpressionAttributeValues[':s3Key']).toBe(catalogRow.s3Key);
    for (const field of Object.keys(OWNER_ALIASES)) expect(deletion.ConditionExpression).toContain(`${field} = `);
    for (const [field, parameter] of [['org_id', ':org'], ['team_id', ':team'], ['user_id', ':user']]) {
      expect(deletion.ConditionExpression.split(' AND ')).toContain(`${field} = ${parameter}`);
      expect(deletion.ExpressionAttributeValues[parameter]).toBe(OWNER_ALIASES[field as keyof typeof OWNER_ALIASES]);
    }
    for (const [field, parameter] of [['orgId', ':org'], ['tenantId', ':org'], ['teamId', ':team'], ['ownerUserId', ':user']]) {
      expect(deletion.ConditionExpression.split(' AND ')).toContain(
        `(attribute_not_exists(${field}) OR ${field} = :null OR ${field} = ${parameter})`,
      );
    }
    expect(deleteCalls()).toHaveLength(1);
  });

  it.each(['user_id', 'ownerUserId', 's3Key'])('stops S3 cleanup when %s changes after the query', async field => {
    const currentRow = { ...catalogRow, [field]: 'changed-after-query' };
    mockDdbSend.mockImplementation(async (command: any) => {
      if (command._cmd === 'Query' && command.TableName === process.env.ARTIFACTS_TABLE) return { Items: [{ ...catalogRow }] };
      if (command._cmd === 'TransactWrite') {
        const deletion = command.TransactItems[1].Delete;
        expect((currentRow as any)[field]).not.toBe((catalogRow as any)[field]);
        expect(deletion.ConditionExpression).toContain(`${field} = `);
        throw Object.assign(new Error('catalog ownership changed'), { name: 'TransactionCanceledException' });
      }
      return { Items: [] };
    });

    await expect(handler(expiryEvent(sessionId))).rejects.toThrow('catalog ownership changed');
    expect(mockS3Send).not.toHaveBeenCalled();
  });
});

describe('non-expiry events are ignored', () => {

  it('does not delete data for a manually deleted header', async () => {
    const event = expiryEvent('sess-manual');
    delete event.Records[0].userIdentity;
    await handler(event);
    expect(mockDdbSend).not.toHaveBeenCalled();
    expect(mockS3Send).not.toHaveBeenCalled();
  });

  it('quarantines a conflicting tenant on the expired header', async () => {
    const event = expiryEvent('sess-conflict');
    event.Records[0].dynamodb.OldImage.tenantId = { S: 'another-tenant' };
    await handler(event);
    expect(mockDdbSend).not.toHaveBeenCalled();
    expect(mockS3Send).not.toHaveBeenCalled();
  });
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
