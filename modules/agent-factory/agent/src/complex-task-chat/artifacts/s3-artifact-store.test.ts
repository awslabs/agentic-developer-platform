/**
 * Tests for S3ArtifactStore identity & access control (Stage B, #185)
 * and hierarchical S3 keys + user uploads (Stage C, #186).
 *
 * All AWS SDK calls are mocked — these tests exercise the DDB filtering,
 * team-level access check, lazy migration, hierarchical key layout,
 * presigned URL scoping, and upload-complete idempotency without network calls.
 */

/* eslint-disable @typescript-eslint/no-explicit-any */

// ---------------------------------------------------------------------------
// Mock AWS SDK before importing the store
// ---------------------------------------------------------------------------
const mockS3Send = jest.fn();
const mockDdbSend = jest.fn();

jest.mock('@aws-sdk/client-s3', () => ({
  S3Client: jest.fn().mockImplementation(() => ({ send: mockS3Send })),
  PutObjectCommand: jest.fn().mockImplementation((args: any) => ({ ...args, _cmd: 'PutObject' })),
  GetObjectCommand: jest.fn().mockImplementation((args: any) => ({ ...args, _cmd: 'GetObject' })),
}));

jest.mock('@aws-sdk/s3-request-presigner', () => ({
  getSignedUrl: jest.fn().mockResolvedValue('https://presigned.example.com/artifact'),
}));

jest.mock('@aws-sdk/client-dynamodb', () => ({
  DynamoDBClient: jest.fn().mockImplementation(() => ({})),
}));

jest.mock('@aws-sdk/lib-dynamodb', () => {
  const actual = jest.requireActual('@aws-sdk/lib-dynamodb');
  return {
    ...actual,
    DynamoDBDocumentClient: {
      from: jest.fn().mockImplementation(() => ({ send: mockDdbSend })),
    },
    PutCommand: jest.fn().mockImplementation((args: any) => ({ ...args, _cmd: 'Put' })),
    QueryCommand: jest.fn().mockImplementation((args: any) => ({ ...args, _cmd: 'Query' })),
    UpdateCommand: jest.fn().mockImplementation((args: any) => ({ ...args, _cmd: 'Update' })),
  };
});

jest.mock('fs', () => ({
  readFileSync: jest.fn().mockReturnValue(Buffer.from('test-content')),
  mkdirSync: jest.fn(),
  writeFileSync: jest.fn(),
}));

import { S3ArtifactStore, keyIsReadableBy } from './s3-artifact-store';
import { CallerIdentity } from './port';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
const BUCKET = 'test-bucket';
const TABLE = 'test-artifacts';

function makeStore(): S3ArtifactStore {
  return new S3ArtifactStore(BUCKET, TABLE, 'us-east-1');
}

function makeDdbItem(overrides: Record<string, any> = {}): Record<string, any> {
  return {
    PK: 'session#sess-1',
    SK: 'art#2026-01-01T00:00:00.000Z#art_abc123',
    id: 'art_abc123',
    url: 'https://presigned.example.com/artifact',
    urlExpiresAt: '2026-01-08T00:00:00.000Z',
    filename: 'report.pdf',
    contentType: 'application/pdf',
    sizeBytes: 1024,
    checksum: 'sha256-abc',
    createdAt: '2026-01-01T00:00:00.000Z',
    source: 'agent',
    s3Key: 'sess-1/default/report.pdf',
    ...overrides,
  };
}

const teamA: CallerIdentity = { orgId: 'org-1', teamId: 'team-A', userId: 'user-1' };
const teamB: CallerIdentity = { orgId: 'org-1', teamId: 'team-B', userId: 'user-2' };

/** The hierarchical key an identity's own upload actually lands on. */
function ownKey(identity: CallerIdentity, sessionId = 'sess-1', filename = 'report.pdf'): string {
  return `o/${identity.orgId}/t/${identity.teamId}/u/${identity.userId}/s/${sessionId}/default/in/${filename}`;
}

beforeEach(() => {
  mockS3Send.mockReset();
  mockDdbSend.mockReset();
});

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------
describe('S3ArtifactStore — identity & access control (#185)', () => {
  describe('publish()', () => {
    it('writes identity fields to the DDB catalog row', async () => {
      mockS3Send.mockResolvedValue({});
      mockDdbSend.mockResolvedValue({});

      const store = makeStore();
      await store.publish({
        sessionId: 'sess-1',
        localPath: '/tmp/report.pdf',
        identity: teamA,
      });

      // The PutCommand should include identity fields
      const putCall = mockDdbSend.mock.calls[0][0];
      expect(putCall.Item.org_id).toBe('org-1');
      expect(putCall.Item.team_id).toBe('team-A');
      expect(putCall.Item.user_id).toBe('user-1');
    });

    it('refuses a write when no owner identity is provided', async () => {
      mockS3Send.mockResolvedValue({});
      mockDdbSend.mockResolvedValue({});

      const store = makeStore();
      await expect(store.publish({
        sessionId: 'sess-1',
        localPath: '/tmp/report.pdf',
      })).rejects.toThrow(/complete, safe artifact owner identity/);
      expect(mockS3Send).not.toHaveBeenCalled();
      expect(mockDdbSend).not.toHaveBeenCalled();
    });
  });

  describe('listBySession()', () => {
    it('returns artifacts stored under the caller’s own derived prefix', async () => {
      mockDdbSend.mockResolvedValue({
        Items: [makeDdbItem({ team_id: 'team-A', org_id: 'org-1', s3Key: ownKey(teamA) })],
      });

      const store = makeStore();
      const refs = await store.listBySession('sess-1', undefined, teamA);

      expect(refs).toHaveLength(1);
      expect(refs[0].id).toBe('art_abc123');
    });

    it('filters out artifacts stored under another principal’s prefix', async () => {
      mockDdbSend.mockResolvedValue({
        Items: [makeDdbItem({ team_id: 'team-A', org_id: 'org-1', s3Key: ownKey(teamA) })],
      });

      const store = makeStore();
      const refs = await store.listBySession('sess-1', undefined, teamB);

      expect(refs).toHaveLength(0);
    });

    it('withholds a planted row whose team_id matches the reader but whose key does not (#5660)', async () => {
      // The row's team_id is written by whoever created it, so an attacker can
      // plant an entry into the victim's session carrying the victim's own team
      // label. Only the storage path is evidence the reader did not author.
      mockDdbSend.mockResolvedValue({
        Items: [makeDdbItem({ team_id: 'team-A', org_id: 'org-1', s3Key: ownKey(teamB) })],
      });

      const store = makeStore();
      const refs = await store.listBySession('sess-1', undefined, teamA);

      expect(refs).toHaveLength(0);
    });

    it('withholds legacy flat-key rows instead of guessing an owner (#5660)', async () => {
      // Pre-hierarchy rows carry no ownership evidence in their key. They are
      // quarantined rather than shown on the strength of session scoping alone.
      mockDdbSend.mockResolvedValue({
        Items: [makeDdbItem({ /* legacy flat s3Key, no team_id or org_id */ })],
      });

      const store = makeStore();
      const refs = await store.listBySession('sess-1', undefined, teamA);

      expect(refs).toHaveLength(0);
      expect(mockDdbSend.mock.calls.some((call: any[]) => call[0]._cmd === 'Update')).toBe(false);
    });

    it('does not backfill a foreign row withheld from listing', async () => {
      mockDdbSend.mockResolvedValue({
        Items: [makeDdbItem({ s3Key: ownKey(teamB) })],
      });

      const store = makeStore();
      expect(await store.listBySession('sess-1', undefined, teamA)).toHaveLength(0);
      expect(mockDdbSend.mock.calls.some((call: any[]) => call[0]._cmd === 'Update')).toBe(false);
    });

    it('withholds every row when the caller has no or partial identity (#5660)', async () => {
      mockDdbSend.mockResolvedValue({
        Items: [makeDdbItem({ team_id: 'team-A', org_id: 'org-1', s3Key: ownKey(teamA) })],
      });

      const store = makeStore();
      expect(await store.listBySession('sess-1', undefined, undefined)).toHaveLength(0);
      expect(await store.listBySession('sess-1', undefined, { orgId: 'org-1' })).toHaveLength(0);
    });

    it('triggers lazy migration for legacy rows when identity is available', async () => {
      mockDdbSend
        .mockResolvedValueOnce({
          // Query response
          Items: [makeDdbItem({ s3Key: ownKey(teamA) })],
        })
        .mockResolvedValueOnce({}); // UpdateCommand response

      const store = makeStore();
      await store.listBySession('sess-1', undefined, teamA);

      // Wait for fire-and-forget backfill
      await new Promise(r => setTimeout(r, 50));

      // Should have called UpdateCommand for backfill
      const updateCall = mockDdbSend.mock.calls.find(
        (call: any[]) => call[0]._cmd === 'Update',
      );
      expect(updateCall).toBeDefined();
      expect(updateCall![0].ExpressionAttributeValues[':org']).toBe('org-1');
      expect(updateCall![0].ExpressionAttributeValues[':team']).toBe('team-A');
      expect(updateCall![0].ExpressionAttributeValues[':user']).toBe('user-1');
    });
  });

  describe('fetch() — authorized by storage path, not by row metadata (#5660)', () => {
    it.each(['sess-2', 'sess-1-extra'])('refuses the same owner in another session: %s', async otherSession => {
      mockDdbSend.mockResolvedValue({ Items: [makeDdbItem({ s3Key: ownKey(teamA, otherSession) })] });
      const store = makeStore();
      await expect(store.fetch('art_abc123', '/tmp/out.pdf', 'sess-1', teamA)).rejects.toThrow(/Access denied/);
      expect(mockS3Send).not.toHaveBeenCalled();
      expect(mockDdbSend.mock.calls.some(call => call[0]._cmd === 'Update')).toBe(false);
      expect(await store.listBySession('sess-1', undefined, teamA)).toEqual([]);
    });

    it('binds the fetch tool to its trusted turn session', async () => {
      const store = makeStore();
      const fetch = jest.spyOn(store, 'fetch').mockResolvedValue();
      const tool = store.toolsForTurn({ sessionId: 'sess-1', identity: teamA }).find(t => t.name === 'fetch_artifact')!;
      await tool.handler({ id: 'art_abc123', dest_path: '/tmp/out.pdf', sessionId: 'sess-2' });
      expect(fetch).toHaveBeenCalledWith('art_abc123', '/tmp/out.pdf', 'sess-1', teamA);
    });

    it('allows a read of an object under the caller\'s own derived prefix', async () => {
      mockDdbSend.mockResolvedValue({
        Items: [makeDdbItem({ team_id: 'team-A', org_id: 'org-1', s3Key: ownKey(teamA) })],
      });
      mockS3Send.mockResolvedValue({
        Body: { transformToByteArray: () => Promise.resolve(Buffer.from('data')) },
      });

      const store = makeStore();
      await expect(store.fetch('art_abc123', '/tmp/out.pdf', 'sess-1', teamA)).resolves.toBeUndefined();
    });

    it('refuses a read of an object under another principal\'s prefix', async () => {
      mockDdbSend.mockResolvedValue({
        Items: [makeDdbItem({ team_id: 'team-A', org_id: 'org-1', s3Key: ownKey(teamA) })],
      });

      const store = makeStore();
      await expect(store.fetch('art_abc123', '/tmp/out.pdf', 'sess-1', teamB)).rejects.toThrow(
        /not stored under the caller's own prefix/,
      );
    });

    it('refuses even when the row\'s team metadata MATCHES the reader', async () => {
      // The case the old team_id check could not catch, and the reason this
      // check moved to the path. `recordUpload` used to accept a client-supplied
      // key, so an attacker could point a row at another tenant's object and
      // stamp their own team on it. The metadata then authorized its own forger.
      mockDdbSend.mockResolvedValue({
        Items: [
          makeDdbItem({
            org_id: teamB.orgId,
            team_id: teamB.teamId, // matches the reader below
            user_id: teamB.userId,
            s3Key: ownKey(teamA), // but the object lives in teamA's area
          }),
        ],
      });

      const store = makeStore();
      await expect(store.fetch('art_abc123', '/tmp/out.pdf', 'sess-1', teamB)).rejects.toThrow(
        /not stored under the caller's own prefix/,
      );
      // No S3 read may be attempted at all.
      expect(mockS3Send).not.toHaveBeenCalled();
    });

    it('refuses a legacy flat key, which cannot prove ownership', async () => {
      // `sess-1/default/report.pdf` carries no identity in its path, so there is
      // no evidence it belongs to the reader. Quarantined, not guessed.
      mockDdbSend.mockResolvedValue({
        Items: [makeDdbItem({ s3Key: 'sess-1/default/report.pdf' })],
      });

      const store = makeStore();
      await expect(store.fetch('art_abc123', '/tmp/out.pdf', 'sess-1', teamA)).rejects.toThrow(
        /not stored under the caller's own prefix/,
      );
    });

    it('refuses a read with no caller identity', async () => {
      // Previously allowed as "backward compat": an absent identity skipped the
      // check entirely, so the cheapest bypass was to send no identity at all.
      mockDdbSend.mockResolvedValue({
        Items: [makeDdbItem({ team_id: 'team-A', s3Key: ownKey(teamA) })],
      });

      const store = makeStore();
      await expect(store.fetch('art_abc123', '/tmp/out.pdf', 'sess-1')).rejects.toThrow(
        /not stored under the caller's own prefix/,
      );
    });

    it('refuses a partial identity rather than deriving a wider prefix', async () => {
      // Missing team/user would otherwise collapse the prefix to `o/org-1/`,
      // which spans every team and user in the org.
      mockDdbSend.mockResolvedValue({
        Items: [makeDdbItem({ s3Key: ownKey(teamA) })],
      });

      const store = makeStore();
      await expect(
        store.fetch('art_abc123', '/tmp/out.pdf', 'sess-1', { orgId: 'org-1' }),
      ).rejects.toThrow(/not stored under the caller's own prefix/);
    });

    it('refuses a traversal key even under a matching prefix', async () => {
      mockDdbSend.mockResolvedValue({
        Items: [
          makeDdbItem({
            s3Key: `o/${teamA.orgId}/t/${teamA.teamId}/u/${teamA.userId}/../../../../etc/secret`,
          }),
        ],
      });

      const store = makeStore();
      await expect(store.fetch('art_abc123', '/tmp/out.pdf', 'sess-1', teamA)).rejects.toThrow(
        /not stored under the caller's own prefix/,
      );
    });

    it('backfills identity only after the path check has passed', async () => {
      // Lazy migration is still useful, but it must not be reachable for a row
      // the caller does not own — otherwise the read that was refused still
      // stamps the caller's identity onto someone else's row.
      mockDdbSend
        .mockResolvedValueOnce({ Items: [makeDdbItem({ s3Key: ownKey(teamA) })] })
        .mockResolvedValueOnce({});
      mockS3Send.mockResolvedValue({
        Body: { transformToByteArray: () => Promise.resolve(Buffer.from('data')) },
      });

      const store = makeStore();
      await store.fetch('art_abc123', '/tmp/out.pdf', 'sess-1', teamA);

      const updateCall = mockDdbSend.mock.calls.find((call: any[]) => call[0]._cmd === 'Update');
      expect(updateCall).toBeDefined();
      expect(updateCall![0].ConditionExpression).toBe('attribute_not_exists(org_id)');
    });

    it('does not backfill a row whose read was refused', async () => {
      mockDdbSend.mockResolvedValue({
        Items: [makeDdbItem({ s3Key: ownKey(teamA) })],
      });

      const store = makeStore();
      await expect(store.fetch('art_abc123', '/tmp/out.pdf', 'sess-1', teamB)).rejects.toThrow();

      const updateCall = mockDdbSend.mock.calls.find((call: any[]) => call[0]._cmd === 'Update');
      expect(updateCall).toBeUndefined();
    });
  });

  describe('toolsForTurn()', () => {
    it('passes identity to publish and list calls', async () => {
      mockS3Send.mockResolvedValue({});
      mockDdbSend
        .mockResolvedValueOnce({}) // PutCommand for publish
        .mockResolvedValueOnce({ Items: [] }); // QueryCommand for list

      const store = makeStore();
      const tools = store.toolsForTurn({
        sessionId: 'sess-1',
        taskId: 'task-1',
        identity: teamA,
      });

      expect(tools).toHaveLength(3);

      // Call publish_artifact
      await tools[0].handler({ path: '/tmp/test.txt' });
      const putCall = mockDdbSend.mock.calls[0][0];
      expect(putCall.Item.org_id).toBe('org-1');
      expect(putCall.Item.team_id).toBe('team-A');

      // Call list_artifacts
      await tools[2].handler({});
      const queryCall = mockDdbSend.mock.calls[1][0];
      expect(queryCall.KeyConditionExpression).toContain('PK = :pk');
    });
  });

  describe('backfillIdentity() edge cases', () => {
    it('does not backfill when a partial identity cannot authorize the path', async () => {
      const partialIdentity: CallerIdentity = { orgId: 'org-1' };
      mockDdbSend.mockResolvedValueOnce({ Items: [makeDdbItem({ s3Key: ownKey(teamA) })] });

      const store = makeStore();
      await store.listBySession('sess-1', undefined, partialIdentity);
      await new Promise(resolve => setImmediate(resolve));

      const updateCall = mockDdbSend.mock.calls.find(
        (call: any[]) => call[0]._cmd === 'Update',
      );
      expect(updateCall).toBeUndefined();
    });
  });
});

// ---------------------------------------------------------------------------
// Stage C (#186): Hierarchical S3 keys, presigned uploads, upload-complete
// ---------------------------------------------------------------------------
describe('S3ArtifactStore — hierarchical keys & user uploads (#186)', () => {
  describe('buildS3Key()', () => {
    it('builds hierarchical key when full identity is provided', () => {
      const key = S3ArtifactStore.buildS3Key({
        identity: teamA,
        sessionId: 'sess-1',
        taskId: 'task-1',
        direction: 'out',
        filename: 'report.pdf',
      });
      expect(key).toBe('o/org-1/t/team-A/u/user-1/s/sess-1/task-1/out/report.pdf');
    });

    it('builds hierarchical key with "in" direction for user uploads', () => {
      const key = S3ArtifactStore.buildS3Key({
        identity: teamA,
        sessionId: 'sess-1',
        taskId: 'task-1',
        direction: 'in',
        filename: 'screenshot.png',
      });
      expect(key).toBe('o/org-1/t/team-A/u/user-1/s/sess-1/task-1/in/screenshot.png');
    });

    it('refuses a key when identity is incomplete', () => {
      expect(() => S3ArtifactStore.buildS3Key({
        identity: { orgId: 'org-1' }, // missing teamId/userId
        sessionId: 'sess-1',
        taskId: 'task-1',
        direction: 'out',
        filename: 'report.pdf',
      })).toThrow(/complete, safe artifact owner identity/);
    });

    it('refuses a key when no identity is provided', () => {
      expect(() => S3ArtifactStore.buildS3Key({
        sessionId: 'sess-1',
        taskId: 'task-1',
        direction: 'out',
        filename: 'report.pdf',
      })).toThrow(/complete, safe artifact owner identity/);
    });
  });

  describe('publish() with hierarchical keys', () => {
    it('writes to hierarchical S3 key when identity is provided', async () => {
      mockS3Send.mockResolvedValue({});
      mockDdbSend.mockResolvedValue({});

      const store = makeStore();
      await store.publish({
        sessionId: 'sess-1',
        taskId: 'task-1',
        localPath: '/tmp/report.pdf',
        identity: teamA,
      });

      // S3 PutObject should use hierarchical key
      const s3Call = mockS3Send.mock.calls[0][0];
      expect(s3Call.Key).toBe('o/org-1/t/team-A/u/user-1/s/sess-1/task-1/out/report.pdf');

      // DDB row should store the hierarchical key
      const ddbCall = mockDdbSend.mock.calls[0][0];
      expect(ddbCall.Item.s3Key).toBe('o/org-1/t/team-A/u/user-1/s/sess-1/task-1/out/report.pdf');
    });

    it('uses "in" direction for user source', async () => {
      mockS3Send.mockResolvedValue({});
      mockDdbSend.mockResolvedValue({});

      const store = makeStore();
      await store.publish({
        sessionId: 'sess-1',
        taskId: 'task-1',
        localPath: '/tmp/upload.png',
        source: 'user',
        identity: teamA,
      });

      const s3Call = mockS3Send.mock.calls[0][0];
      expect(s3Call.Key).toContain('/in/');
    });

    it('refuses publish when no identity is available', async () => {
      mockS3Send.mockResolvedValue({});
      mockDdbSend.mockResolvedValue({});

      const store = makeStore();
      await expect(store.publish({
        sessionId: 'sess-1',
        taskId: 'task-1',
        localPath: '/tmp/report.pdf',
      })).rejects.toThrow(/complete, safe artifact owner identity/);
      expect(mockS3Send).not.toHaveBeenCalled();
    });
  });

  describe('presignUpload()', () => {
    it('generates presigned URL scoped to correct hierarchical key', async () => {
      const { getSignedUrl: mockGetSignedUrl } = require('@aws-sdk/s3-request-presigner');
      mockGetSignedUrl.mockResolvedValue('https://presigned.example.com/upload');

      const store = makeStore();
      const result = await store.presignUpload({
        identity: teamA,
        sessionId: 'sess-1',
        taskId: 'task-1',
        filename: 'doc.pdf',
        contentType: 'application/pdf',
      });

      expect(result.uploadUrl).toBe('https://presigned.example.com/upload');
      expect(result.s3Key).toBe('o/org-1/t/team-A/u/user-1/s/sess-1/task-1/in/doc.pdf');
      expect(result.expiresIn).toBe(3600);

      // Verify PutObjectCommand was called with exact key
      const { PutObjectCommand: MockPutObj } = require('@aws-sdk/client-s3');
      const lastPutCall = MockPutObj.mock.calls[MockPutObj.mock.calls.length - 1][0];
      expect(lastPutCall.Key).toBe('o/org-1/t/team-A/u/user-1/s/sess-1/task-1/in/doc.pdf');
      expect(lastPutCall.Bucket).toBe(BUCKET);
      expect(lastPutCall.ContentType).toBe('application/pdf');
    });

    it('refuses a presign when identity is incomplete', async () => {
      const { getSignedUrl: mockGetSignedUrl } = require('@aws-sdk/s3-request-presigner');
      mockGetSignedUrl.mockResolvedValue('https://presigned.example.com/upload');

      const store = makeStore();
      await expect(store.presignUpload({
        identity: { orgId: 'org-1' },
        sessionId: 'sess-1',
        taskId: 'task-1',
        filename: 'doc.pdf',
        contentType: 'application/pdf',
      })).rejects.toThrow(/complete, safe artifact owner identity/);
    });
  });

  describe('recordUpload()', () => {
    it('creates DDB catalog row for new upload', async () => {
      mockDdbSend
        .mockResolvedValueOnce({ Items: [] }) // dedup query returns empty
        .mockResolvedValueOnce({}); // PutCommand

      const store = makeStore();
      const ref = await store.recordUpload({
        sessionId: 'sess-1',
        taskId: 'task-1',
        s3Key: 'o/org-1/t/team-A/u/user-1/s/sess-1/task-1/in/doc.pdf',
        filename: 'doc.pdf',
        contentType: 'application/pdf',
        sizeBytes: 2048,
        checksum: 'abc123',
        identity: teamA,
      });

      expect(ref.id).toMatch(/^art_/);
      expect(ref.filename).toBe('doc.pdf');
      expect(ref.source).toBe('user');
      expect(ref.checksum).toBe('abc123');

      // DDB put should have the identity fields
      const putCall = mockDdbSend.mock.calls[1][0];
      expect(putCall.Item.org_id).toBe('org-1');
      expect(putCall.Item.team_id).toBe('team-A');
      expect(putCall.Item.user_id).toBe('user-1');
      expect(putCall.Item.source).toBe('user');
    });

    it('returns existing ref when checksum matches (idempotent)', async () => {
      const { getSignedUrl: mockGetSignedUrl } = require('@aws-sdk/s3-request-presigner');
      mockGetSignedUrl.mockResolvedValueOnce('https://presigned.example.com/fresh-read');
      const existingItem = makeDdbItem({
        id: 'art_existing',
        checksum: 'abc123',
        source: 'user',
        url: 'https://stale-or-untrusted.example.com/artifact',
        s3Key: 'o/org-1/t/team-A/u/user-1/s/sess-1/task-1/in/doc.pdf',
        org_id: 'org-1',
        team_id: 'team-A',
        user_id: 'user-1',
      });
      mockDdbSend.mockResolvedValueOnce({ Items: [existingItem] });

      const store = makeStore();
      const ref = await store.recordUpload({
        sessionId: 'sess-1',
        taskId: 'task-1',
        s3Key: 'o/org-1/t/team-A/u/user-1/s/sess-1/task-1/in/doc.pdf',
        filename: 'doc.pdf',
        contentType: 'application/pdf',
        sizeBytes: 2048,
        checksum: 'abc123',
        identity: teamA,
      });

      // Should return existing ref, not create a new one
      expect(ref.id).toBe('art_existing');
      expect(ref.url).toBe('https://presigned.example.com/fresh-read');
      expect(ref.url).not.toBe(existingItem.url);
      // Only 1 DDB call (query), no PutCommand
      expect(mockDdbSend).toHaveBeenCalledTimes(1);
    });

    it.each([
      [
        'foreign key with caller-looking metadata',
        {
          s3Key: 'o/org-9/t/team-Z/u/user-victim/s/sess-victim/task-1/in/secret.pdf',
          org_id: 'org-1',
          team_id: 'team-A',
          user_id: 'user-1',
        },
      ],
      [
        'legacy row missing ownership',
        { s3Key: 'o/org-1/t/team-A/u/user-1/s/sess-1/task-1/in/doc.pdf' },
      ],
    ])('quarantines a checksum match from a %s', async (_label, poisonedFields) => {
      jest.spyOn(console, 'warn').mockImplementation(() => {});
      mockDdbSend
        .mockResolvedValueOnce({
          Items: [makeDdbItem({
            id: 'art_poisoned',
            checksum: 'abc123',
            url: 'https://victim.example.com/secret',
            ...poisonedFields,
          })],
        })
        .mockResolvedValueOnce({});

      const store = makeStore();
      const ref = await store.recordUpload({
        sessionId: 'sess-1',
        taskId: 'task-1',
        filename: 'doc.pdf',
        contentType: 'application/pdf',
        sizeBytes: 2048,
        checksum: 'abc123',
        identity: teamA,
      });

      expect(ref.id).not.toBe('art_poisoned');
      expect(ref.url).not.toBe('https://victim.example.com/secret');
      expect(mockDdbSend).toHaveBeenCalledTimes(2);
      const putCall = mockDdbSend.mock.calls[1][0];
      expect(putCall.Item.s3Key).toBe('o/org-1/t/team-A/u/user-1/s/sess-1/task-1/in/doc.pdf');
    });

    // #5660 (A07): the recorded location is always server-derived.
    it('records the server-derived key, ignoring the key in the request', async () => {
      mockDdbSend.mockResolvedValueOnce({ Items: [] }).mockResolvedValueOnce({});

      const store = makeStore();
      await store.recordUpload({
        sessionId: 'sess-1',
        taskId: 'task-1',
        // A key in another tenant's area. Accepting this is what let a caller
        // register someone else's object as their own artifact.
        s3Key: 'o/org-9/t/team-Z/u/user-victim/s/sess-victim/task-1/in/secret.pdf',
        filename: 'doc.pdf',
        contentType: 'application/pdf',
        sizeBytes: 2048,
        checksum: 'abc123',
        identity: teamA,
      });

      const putCall = mockDdbSend.mock.calls[1][0];
      expect(putCall.Item.s3Key).toBe('o/org-1/t/team-A/u/user-1/s/sess-1/task-1/in/doc.pdf');
      expect(putCall.Item.s3Key).not.toContain('user-victim');
    });

    it('derives the same key presignUpload issued, so a real upload still resolves', async () => {
      // The two must agree or legitimate uploads would record a key that holds
      // no object.
      mockS3Send.mockResolvedValue({});
      const store = makeStore();
      const presigned = await store.presignUpload({
        identity: teamA,
        sessionId: 'sess-1',
        taskId: 'task-1',
        filename: 'doc.pdf',
        contentType: 'application/pdf',
      });

      mockDdbSend.mockResolvedValueOnce({ Items: [] }).mockResolvedValueOnce({});
      await store.recordUpload({
        sessionId: 'sess-1',
        taskId: 'task-1',
        filename: 'doc.pdf',
        contentType: 'application/pdf',
        sizeBytes: 2048,
        checksum: 'abc123',
        identity: teamA,
      });

      const putCall = mockDdbSend.mock.calls[1][0];
      expect(putCall.Item.s3Key).toBe(presigned.s3Key);
    });

    it('records a key the uploader can then read back', async () => {
      // Ties the write path to the read check: what recordUpload stores must
      // satisfy keyIsReadableBy for its own uploader, or uploads would be
      // recorded and then be unreadable.
      mockDdbSend.mockResolvedValueOnce({ Items: [] }).mockResolvedValueOnce({});

      const store = makeStore();
      await store.recordUpload({
        sessionId: 'sess-1',
        taskId: 'task-1',
        filename: 'doc.pdf',
        contentType: 'application/pdf',
        sizeBytes: 2048,
        checksum: 'abc123',
        identity: teamA,
      });

      const recordedKey = mockDdbSend.mock.calls[1][0].Item.s3Key;
      expect(keyIsReadableBy(recordedKey, 'sess-1', teamA)).toBe(true);
      expect(keyIsReadableBy(recordedKey, 'sess-1', teamB)).toBe(false);
    });

    it('refuses a session id that is not a safe single segment', async () => {
      const store = makeStore();
      for (const sessionId of ['o', '../escape', 'a/b']) {
        await expect(
          store.recordUpload({
            sessionId,
            taskId: 'task-1',
            filename: 'doc.pdf',
            contentType: 'application/pdf',
            sizeBytes: 2048,
            checksum: 'abc123',
            identity: teamA,
          }),
        ).rejects.toThrow(/Invalid session id/);
      }
      expect(mockDdbSend).not.toHaveBeenCalled();
    });
  });

  describe('session id shape is enforced on every path-building entry point (#5660)', () => {
    it.each([['o'], ['t'], ['u'], ['s'], ['../escape'], ['a/b']])(
      'refuses %j in presignUpload, publish and listBySession',
      async sessionId => {
        const store = makeStore();
        await expect(
          store.presignUpload({
            identity: teamA,
            sessionId,
            taskId: 'task-1',
            filename: 'doc.pdf',
            contentType: 'application/pdf',
          }),
        ).rejects.toThrow(/Invalid session id/);

        await expect(
          store.publish({ sessionId, localPath: '/tmp/report.pdf', identity: teamA }),
        ).rejects.toThrow(/Invalid session id/);

        await expect(store.listBySession(sessionId, undefined, teamA)).rejects.toThrow(
          /Invalid session id/,
        );
      },
    );
  });
});
