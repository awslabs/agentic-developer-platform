/**
 * Tests for S3ArtifactStore identity & access control (Stage B, #185).
 *
 * All AWS SDK calls are mocked — these tests exercise the DDB filtering,
 * team-level access check, and lazy migration logic without network calls.
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

import { S3ArtifactStore } from './s3-artifact-store';
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

    it('omits identity fields when none provided (backward compat)', async () => {
      mockS3Send.mockResolvedValue({});
      mockDdbSend.mockResolvedValue({});

      const store = makeStore();
      await store.publish({
        sessionId: 'sess-1',
        localPath: '/tmp/report.pdf',
      });

      const putCall = mockDdbSend.mock.calls[0][0];
      expect(putCall.Item.org_id).toBeUndefined();
      expect(putCall.Item.team_id).toBeUndefined();
      expect(putCall.Item.user_id).toBeUndefined();
    });
  });

  describe('listBySession()', () => {
    it('returns artifacts from the same team', async () => {
      mockDdbSend.mockResolvedValue({
        Items: [makeDdbItem({ team_id: 'team-A', org_id: 'org-1' })],
      });

      const store = makeStore();
      const refs = await store.listBySession('sess-1', undefined, teamA);

      expect(refs).toHaveLength(1);
      expect(refs[0].id).toBe('art_abc123');
    });

    it('filters out artifacts from a different team', async () => {
      mockDdbSend.mockResolvedValue({
        Items: [makeDdbItem({ team_id: 'team-A', org_id: 'org-1' })],
      });

      const store = makeStore();
      const refs = await store.listBySession('sess-1', undefined, teamB);

      expect(refs).toHaveLength(0);
    });

    it('shows legacy rows (no team_id) to any caller within the session', async () => {
      mockDdbSend.mockResolvedValue({
        Items: [makeDdbItem({ /* no team_id or org_id */ })],
      });

      const store = makeStore();
      const refs = await store.listBySession('sess-1', undefined, teamA);

      expect(refs).toHaveLength(1);
      expect(refs[0].id).toBe('art_abc123');
    });

    it('triggers lazy migration for legacy rows when identity is available', async () => {
      mockDdbSend
        .mockResolvedValueOnce({
          // Query response
          Items: [makeDdbItem({ /* no org_id */ })],
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

  describe('fetch()', () => {
    it('allows same-team access', async () => {
      mockDdbSend.mockResolvedValue({
        Items: [makeDdbItem({ team_id: 'team-A', org_id: 'org-1' })],
      });
      mockS3Send.mockResolvedValue({
        Body: { transformToByteArray: () => Promise.resolve(Buffer.from('data')) },
      });

      const store = makeStore();
      await expect(store.fetch('art_abc123', '/tmp/out.pdf', teamA)).resolves.toBeUndefined();
    });

    it('rejects cross-team access', async () => {
      mockDdbSend.mockResolvedValue({
        Items: [makeDdbItem({ team_id: 'team-A', org_id: 'org-1' })],
      });

      const store = makeStore();
      await expect(store.fetch('art_abc123', '/tmp/out.pdf', teamB)).rejects.toThrow(
        'Access denied: artifact art_abc123 belongs to a different team',
      );
    });

    it('allows access to legacy rows (no team_id) and backfills identity', async () => {
      mockDdbSend
        .mockResolvedValueOnce({
          // Query
          Items: [makeDdbItem({ /* no team_id */ })],
        })
        .mockResolvedValueOnce({}) // UpdateCommand for backfill
        .mockResolvedValueOnce({}); // (unused, but safe)

      mockS3Send.mockResolvedValue({
        Body: { transformToByteArray: () => Promise.resolve(Buffer.from('data')) },
      });

      const store = makeStore();
      await store.fetch('art_abc123', '/tmp/out.pdf', teamA);

      // UpdateCommand should have been called for lazy migration
      const updateCall = mockDdbSend.mock.calls.find(
        (call: any[]) => call[0]._cmd === 'Update',
      );
      expect(updateCall).toBeDefined();
      expect(updateCall![0].ConditionExpression).toBe('attribute_not_exists(org_id)');
    });

    it('works without identity (backward compat)', async () => {
      mockDdbSend.mockResolvedValueOnce({
        Items: [makeDdbItem({ team_id: 'team-A' })],
      });
      mockS3Send.mockResolvedValueOnce({
        Body: { transformToByteArray: () => Promise.resolve(Buffer.from('data')) },
      });

      const store = makeStore();
      // No identity provided — should still work (no team check when caller has no team)
      await expect(store.fetch('art_abc123', '/tmp/out.pdf')).resolves.toBeUndefined();
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
    it('handles partial identity (orgId only, no teamId/userId) without DDB error', async () => {
      const partialIdentity: CallerIdentity = { orgId: 'org-1' };
      mockDdbSend
        .mockResolvedValueOnce({
          // Query
          Items: [makeDdbItem({ /* legacy row, no identity */ })],
        })
        .mockResolvedValueOnce({}); // UpdateCommand

      mockS3Send.mockResolvedValue({
        Body: { transformToByteArray: () => Promise.resolve(Buffer.from('data')) },
      });

      const store = makeStore();
      // Should NOT throw — backfill builds UpdateExpression dynamically
      await expect(store.fetch('art_abc123', '/tmp/out.pdf', partialIdentity)).resolves.toBeUndefined();

      // UpdateCommand should only SET org_id (no team_id or user_id)
      const updateCall = mockDdbSend.mock.calls.find(
        (call: any[]) => call[0]._cmd === 'Update',
      );
      expect(updateCall).toBeDefined();
      expect(updateCall![0].UpdateExpression).toBe('SET org_id = :org');
      expect(updateCall![0].ExpressionAttributeValues[':org']).toBe('org-1');
      expect(updateCall![0].ExpressionAttributeValues[':team']).toBeUndefined();
      expect(updateCall![0].ExpressionAttributeValues[':user']).toBeUndefined();
    });
  });
});
