/**
 * S3ArtifactStore — S3 + DynamoDB catalog for session artifacts.
 *
 * Stage C (#186): Hierarchical S3 key layout.
 *   New key: o/<org_id>/t/<team_id>/u/<user_id>/s/<session_id>/<task_id>/{in|out}/<filename>
 *   Legacy key: <sessionId>/<taskId>/<filename>
 *
 * New writes require a complete identity and always use the hierarchical key.
 * Legacy flat keys are quarantined because their owner cannot be verified.
 *
 * Stage B (#185): identity fields (org_id, team_id, user_id) are written to
 * every catalog row. Hierarchical rows missing catalog identity are lazily
 * backfilled only after their storage path proves ownership. Legacy flat rows
 * remain quarantined because their owner cannot be independently verified.
 *
 * Ownership binding (#5660 / A07)
 * -------------------------------
 * Two client-supplied values used to be trusted here, and both are now derived
 * or verified server-side:
 *
 *  - `recordUpload` accepted the storage key from the client, so a caller could
 *    register an object living in another tenant's area as their own artifact
 *    and read it back. The key is re-derived from the caller's identity, exactly
 *    as `presignUpload` builds it.
 *  - `fetch` and `listBySession` authorized reads by comparing the row's `team_id`
 *    to the caller's. That field is written by whoever created the row, so forged
 *    metadata validated its own author. Both paths are now checked against the
 *    storage path derived from the reader's identity (`keyIsReadableBy`).
 *
 * Session ids are shape-validated before being used as a key or path segment;
 * see `../session-id`.
 */
import {
  S3Client,
  PutObjectCommand,
  GetObjectCommand,
} from '@aws-sdk/client-s3';
import { getSignedUrl } from '@aws-sdk/s3-request-presigner';
import { DynamoDBClient } from '@aws-sdk/client-dynamodb';
import {
  DynamoDBDocumentClient,
  PutCommand,
  QueryCommand,
  UpdateCommand,
} from '@aws-sdk/lib-dynamodb';
import { z } from 'zod';
import { ArtifactStore, ArtifactRef, TurnScope, CallerIdentity } from './port';
import { AgentTool, AgentToolResult } from '../context/types';
import { assertValidSessionId, isValidSessionId } from '../session-id';
import * as fs from 'fs';
import * as path from 'path';
import * as crypto from 'crypto';

const DEFAULT_TTL_DAYS = 30;
const PRESIGNED_URL_EXPIRY = 7 * 86400; // 7 days

/**
 * Prefix an identity may read under, at the full depth of the hierarchical
 * layout. Returns null when the identity is incomplete — a blank segment would
 * widen the prefix to span other principals, so there is no usable prefix
 * rather than a permissive one.
 */
function derivedIdentityPrefix(identity?: CallerIdentity): string | null {
  if (!identity?.orgId || !identity?.teamId || !identity?.userId) return null;
  for (const segment of [identity.orgId, identity.teamId, identity.userId]) {
    if (!/^[A-Za-z0-9_.:-]{1,128}$/.test(segment) || segment.includes('..')) return null;
  }
  return `o/${identity.orgId}/t/${identity.teamId}/u/${identity.userId}/`;
}

function assertSafeStorageSegment(value: string, label: string): void {
  if (!value || value.length > 255 || value.includes('/') || value.includes('\\') || value.includes('..')) {
    throw new Error(`${label} is not a safe storage path segment`);
  }
}

/**
 * True when `s3Key` sits under the prefix derived from the reader's OWN
 * identity (#5660 / A07).
 *
 * This deliberately does not consult the row's `team_id`. That field is written
 * from whoever created the row, so a caller who registers an upload pointing at
 * another tenant's object can also supply metadata that matches themselves —
 * the label validates the forger, not the object. The storage path is the only
 * evidence the reader did not author.
 */
export function keyIsReadableBy(s3Key: string, sessionId: string, identity?: CallerIdentity): boolean {
  if (!isValidSessionId(sessionId) || typeof s3Key !== 'string' || s3Key.length === 0) return false;
  if (s3Key.includes('..')) return false;
  const prefix = derivedIdentityPrefix(identity);
  if (!prefix) return false;
  return s3Key.startsWith(`${prefix}s/${sessionId}/`);
}

export class S3ArtifactStore implements ArtifactStore {
  private readonly s3: S3Client;
  private readonly ddb: DynamoDBDocumentClient;

  constructor(
    private readonly bucket: string,
    private readonly tableName: string,
    region: string = 'us-east-1',
  ) {
    this.s3 = new S3Client({ region });
    const rawDdb = new DynamoDBClient({ region });
    this.ddb = DynamoDBDocumentClient.from(rawDdb, {
      marshallOptions: { removeUndefinedValues: true },
    });
  }

  async publish(input: {
    sessionId: string;
    taskId?: string;
    localPath: string;
    filename?: string;
    contentType?: string;
    ttl?: number;
    supersedes?: string;
    source?: 'agent' | 'user';
    identity?: CallerIdentity;
  }): Promise<ArtifactRef> {
    assertValidSessionId(input.sessionId);
    const filename = input.filename ?? path.basename(input.localPath);
    const taskId = input.taskId ?? 'default';
    const direction = (input.source === 'user') ? 'in' : 'out';
    const s3Key = S3ArtifactStore.buildS3Key({
      identity: input.identity,
      sessionId: input.sessionId,
      taskId,
      direction,
      filename,
    });
    const id = `art_${crypto.randomUUID().replace(/-/g, '').slice(0, 12)}`;
    const now = new Date();

    const fileBuffer = fs.readFileSync(input.localPath);
    const checksum = crypto.createHash('sha256').update(fileBuffer).digest('hex');
    const contentType = input.contentType ?? this.guessContentType(filename);

    await this.s3.send(
      new PutObjectCommand({
        Bucket: this.bucket,
        Key: s3Key,
        Body: fileBuffer,
        ContentType: contentType,
      }),
    );

    const url = await getSignedUrl(
      this.s3,
      new GetObjectCommand({ Bucket: this.bucket, Key: s3Key }),
      { expiresIn: PRESIGNED_URL_EXPIRY },
    );

    const ttlSeconds = input.ttl ?? DEFAULT_TTL_DAYS * 86400;
    const ttlEpoch = Math.floor(now.getTime() / 1000) + ttlSeconds;

    const ref: ArtifactRef = {
      id,
      url,
      urlExpiresAt: new Date(now.getTime() + PRESIGNED_URL_EXPIRY * 1000).toISOString(),
      filename,
      contentType,
      sizeBytes: fileBuffer.length,
      checksum,
      createdAt: now.toISOString(),
      supersedes: input.supersedes,
      source: input.source ?? 'agent',
    };

    await this.ddb.send(
      new PutCommand({
        TableName: this.tableName,
        Item: {
          PK: `session#${input.sessionId}`,
          SK: `art#${now.toISOString()}#${id}`,
          ...ref,
          s3Key,
          ttl: ttlEpoch,
          // Stage B (#185): identity fields for team-level access control
          org_id: input.identity?.orgId,
          team_id: input.identity?.teamId,
          user_id: input.identity?.userId,
        },
      }),
    );

    return ref;
  }

  async fetch(
    artifactId: string,
    destPath: string,
    sessionId: string,
    identity?: CallerIdentity,
  ): Promise<void> {
    assertValidSessionId(sessionId);
    const queryResult = await this.ddb.send(
      new QueryCommand({
        TableName: this.tableName,
        IndexName: 'by-id',
        KeyConditionExpression: 'id = :id',
        ExpressionAttributeValues: { ':id': artifactId },
        Limit: 1,
      }),
    );

    const item = queryResult.Items?.[0];
    if (!item) throw new Error(`Artifact not found: ${artifactId}`);

    const s3Key = item.s3Key as string;

    // #5660 (A07): authorize against the STORAGE PATH, derived from the reader's
    // own identity — not against the row's team_id.
    //
    // The old check compared `item.team_id` to the caller's team. That field is
    // written by whoever created the row, so an attacker who registers an upload
    // pointing at another tenant's object supplies matching metadata too: the
    // label authorizes its own forger. The key's location is the one piece of
    // evidence the reader did not author.
    //
    // A legacy flat key (`<session>/<task>/<file>`) has no identity in its path,
    // so it cannot be proven to belong to the reader. Those are refused rather
    // than allowed on the strength of the absent metadata that used to permit
    // them — the quarantine the issue asks for, not a guess.
    if (!keyIsReadableBy(s3Key, sessionId, identity)) {
      throw new Error(
        `Access denied: artifact ${artifactId} is not stored under the caller's own prefix`,
      );
    }

    // Lazy migration: backfill identity on legacy rows. Only reached once the
    // key check above has already proven the row is the caller's, so this can no
    // longer stamp the caller's identity onto another tenant's row.
    if (!item.org_id && identity?.orgId) {
      await this.backfillIdentity(item.PK as string, item.SK as string, identity);
    }

    const response = await this.s3.send(
      new GetObjectCommand({ Bucket: this.bucket, Key: s3Key }),
    );

    const body = await response.Body?.transformToByteArray();
    if (!body) throw new Error(`Empty body for artifact: ${artifactId}`);

    const dir = path.dirname(destPath);
    fs.mkdirSync(dir, { recursive: true });
    fs.writeFileSync(destPath, Buffer.from(body));
  }

  async listBySession(
    sessionId: string,
    filter?: { contentType?: string; filename?: string; limit?: number },
    identity?: CallerIdentity,
  ): Promise<ArtifactRef[]> {
    assertValidSessionId(sessionId);
    const response = await this.ddb.send(
      new QueryCommand({
        TableName: this.tableName,
        KeyConditionExpression: 'PK = :pk AND begins_with(SK, :prefix)',
        ExpressionAttributeValues: {
          ':pk': `session#${sessionId}`,
          ':prefix': 'art#',
        },
        ScanIndexForward: false,
        Limit: filter?.limit ?? 100,
      }),
    );

    const authorizedItems = (response.Items ?? []).filter(item =>
      keyIsReadableBy((item.s3Key as string | undefined) ?? '', sessionId, identity),
    );

    // Lazy migration is only safe after the storage path has independently
    // proved ownership. Missing, legacy-flat, and foreign keys are quarantined
    // without mutating their catalog metadata.
    for (const item of authorizedItems) {
      if (!item.org_id && identity?.orgId) {
        this.backfillIdentity(item.PK as string, item.SK as string, identity).catch(() => {});
      }
    }

    let items = authorizedItems.map(item => {
      return {
        id: item.id as string,
        url: item.url as string,
        urlExpiresAt: item.urlExpiresAt as string,
        filename: item.filename as string,
        contentType: item.contentType as string,
        sizeBytes: item.sizeBytes as number,
        checksum: item.checksum as string,
        createdAt: item.createdAt as string,
        supersedes: item.supersedes as string | undefined,
        source: item.source as 'agent' | 'user',
      };
    });

    if (filter?.contentType) {
      items = items.filter(i => i.contentType === filter.contentType);
    }
    if (filter?.filename) {
      items = items.filter(i => i.filename.includes(filter.filename!));
    }

    return items;
  }

  toolsForTurn(scope: TurnScope): AgentTool[] {
    const store = this;
    const { sessionId, taskId, onPublish, identity } = scope;

    return [
      {
        name: 'publish_artifact',
        description:
          'Upload a file from the workspace as a durable artifact. Returns a reference with a pre-signed download URL.',
        inputSchema: {
          path: z.string().describe('Local file path to upload'),
          filename: z.string().optional().describe('Override filename (defaults to basename of path)'),
          contentType: z.string().optional().describe('MIME type (auto-detected if omitted)'),
          supersedes: z.string().optional().describe('Artifact ID this replaces (for lineage tracking)'),
        },
        handler: async (input: Record<string, unknown>): Promise<AgentToolResult> => {
          const ref = await store.publish({
            sessionId,
            taskId,
            localPath: input.path as string,
            filename: input.filename as string | undefined,
            contentType: input.contentType as string | undefined,
            supersedes: input.supersedes as string | undefined,
            identity,
          });
          onPublish?.(ref);
          return { content: [{ type: 'text', text: JSON.stringify(ref, null, 2) }] };
        },
      },
      {
        name: 'fetch_artifact',
        description: 'Download a previously published artifact to the workspace for editing.',
        inputSchema: {
          id: z.string().describe('Artifact ID to fetch (e.g. art_01HX...)'),
          dest_path: z.string().describe('Local path to save the file'),
        },
        handler: async (input: Record<string, unknown>): Promise<AgentToolResult> => {
          await store.fetch(input.id as string, input.dest_path as string, sessionId, identity);
          return { content: [{ type: 'text', text: `Downloaded ${input.id} to ${input.dest_path}` }] };
        },
      },
      {
        name: 'list_artifacts',
        description: 'List artifacts published in the current session.',
        inputSchema: {
          content_type: z.string().optional().describe('Filter by MIME type'),
          filename: z.string().optional().describe('Filter by filename substring'),
          limit: z.number().int().positive().optional().describe('Max results (default 20)'),
        },
        handler: async (input: Record<string, unknown>): Promise<AgentToolResult> => {
          const refs = await store.listBySession(sessionId, {
            contentType: input.content_type as string | undefined,
            filename: input.filename as string | undefined,
            limit: (input.limit as number) ?? 20,
          }, identity);
          if (refs.length === 0) {
            return { content: [{ type: 'text', text: 'No artifacts found.' }] };
          }
          return {
            content: [
              {
                type: 'text',
                text: refs
                  .map(r => `[${r.id}] ${r.filename} (${r.contentType}, ${r.sizeBytes} bytes) ${r.url}`)
                  .join('\n'),
              },
            ],
          };
        },
      },
    ];
  }

  /**
   * Build an owner-qualified S3 key. Incomplete identity is refused rather
   * than written to the unauthorizable legacy flat layout.
   *
   * Hierarchical: o/<org>/t/<team>/u/<user>/s/<session>/<task>/{in|out}/<filename>
   * Legacy:       <session>/<task>/<filename>
   */
  static buildS3Key(opts: {
    identity?: CallerIdentity;
    sessionId: string;
    taskId: string;
    direction: 'in' | 'out';
    filename: string;
  }): string {
    const { identity, sessionId, taskId, direction, filename } = opts;
    // Shape-check at the single point where every artifact path is built, so no
    // caller can construct a key that escapes its own session segment.
    assertValidSessionId(sessionId);
    const ownerPrefix = derivedIdentityPrefix(identity);
    if (!ownerPrefix) {
      throw new Error('A complete, safe artifact owner identity is required');
    }
    assertSafeStorageSegment(taskId, 'taskId');
    assertSafeStorageSegment(filename, 'filename');
    return `${ownerPrefix}s/${sessionId}/${taskId}/${direction}/${filename}`;
  }

  /**
   * Generate a presigned PUT URL for user uploads. Scoped to the caller's
   * exact hierarchical key — no wildcard.
   * Stage C (#186).
   */
  async presignUpload(opts: {
    identity: CallerIdentity;
    sessionId: string;
    taskId: string;
    filename: string;
    contentType: string;
  }): Promise<{ uploadUrl: string; s3Key: string; expiresIn: number }> {
    assertValidSessionId(opts.sessionId);
    const s3Key = S3ArtifactStore.buildS3Key({
      identity: opts.identity,
      sessionId: opts.sessionId,
      taskId: opts.taskId,
      direction: 'in',
      filename: opts.filename,
    });

    const expiresIn = 3600; // 1 hour
    const uploadUrl = await getSignedUrl(
      this.s3,
      new PutObjectCommand({
        Bucket: this.bucket,
        Key: s3Key,
        ContentType: opts.contentType,
      }),
      { expiresIn },
    );

    return { uploadUrl, s3Key, expiresIn };
  }

  /**
   * Record a user-uploaded artifact in the DDB catalog. Idempotent via
   * sha256 — if a row with the same checksum already exists for this session,
   * return the existing ref instead of creating a duplicate.
   * Stage C (#186).
   */
  async recordUpload(opts: {
    sessionId: string;
    taskId: string;
    /**
     * Ignored for storage purposes (#5660 / A07). Retained so existing callers
     * compile, but the recorded key is always re-derived below.
     */
    s3Key?: string;
    filename: string;
    contentType: string;
    sizeBytes: number;
    checksum: string;
    identity: CallerIdentity;
  }): Promise<ArtifactRef> {
    assertValidSessionId(opts.sessionId);

    // #5660 (A07): re-derive the key instead of recording the caller's.
    // Accepting a client key let a caller register an object living in another
    // tenant's area as their own artifact, then read it back through fetch().
    // presignUpload() already builds the key this same way, so the only key a
    // legitimate client could have uploaded to is the one derived here.
    const derivedKey = S3ArtifactStore.buildS3Key({
      identity: opts.identity,
      sessionId: opts.sessionId,
      taskId: opts.taskId,
      direction: 'in',
      filename: opts.filename,
    });

    // Idempotency is valid only for the exact server-derived upload. Rows in
    // this session partition may predate ownership enforcement, so checksum
    // alone is not evidence that the row belongs to this caller.
    const existingResult = await this.ddb.send(
      new QueryCommand({
        TableName: this.tableName,
        KeyConditionExpression: 'PK = :pk AND begins_with(SK, :prefix)',
        FilterExpression: 'checksum = :cs',
        ExpressionAttributeValues: {
          ':pk': `session#${opts.sessionId}`,
          ':prefix': 'art#',
          ':cs': opts.checksum,
        },
      }),
    );

    const matchingChecksumRows = existingResult.Items ?? [];
    const verifiedRows = matchingChecksumRows.filter(item =>
      item.s3Key === derivedKey &&
      item.org_id === opts.identity.orgId &&
      item.team_id === opts.identity.teamId &&
      item.user_id === opts.identity.userId,
    );
    const existing = verifiedRows[0];
    const unverifiedCount = matchingChecksumRows.length - verifiedRows.length;

    if (unverifiedCount > 0) {
      console.warn(
        `[artifact-store] Ignoring ${unverifiedCount} unverified checksum-matching row(s) ` +
          `for session ${opts.sessionId}; ownership was not inferred from the session partition`,
      );
    }

    if (existing) {
      const now = new Date();
      const url = await getSignedUrl(
        this.s3,
        new GetObjectCommand({ Bucket: this.bucket, Key: derivedKey }),
        { expiresIn: PRESIGNED_URL_EXPIRY },
      );
      return {
        id: existing.id as string,
        url,
        urlExpiresAt: new Date(now.getTime() + PRESIGNED_URL_EXPIRY * 1000).toISOString(),
        filename: existing.filename as string,
        contentType: existing.contentType as string,
        sizeBytes: existing.sizeBytes as number,
        checksum: existing.checksum as string,
        createdAt: existing.createdAt as string,
        source: existing.source as 'agent' | 'user',
      };
    }

    const id = `art_${crypto.randomUUID().replace(/-/g, '').slice(0, 12)}`;
    const now = new Date();
    const ttlEpoch = Math.floor(now.getTime() / 1000) + DEFAULT_TTL_DAYS * 86400;

    const url = await getSignedUrl(
      this.s3,
      new GetObjectCommand({ Bucket: this.bucket, Key: derivedKey }),
      { expiresIn: PRESIGNED_URL_EXPIRY },
    );

    const ref: ArtifactRef = {
      id,
      url,
      urlExpiresAt: new Date(now.getTime() + PRESIGNED_URL_EXPIRY * 1000).toISOString(),
      filename: opts.filename,
      contentType: opts.contentType,
      sizeBytes: opts.sizeBytes,
      checksum: opts.checksum,
      createdAt: now.toISOString(),
      source: 'user',
    };

    await this.ddb.send(
      new PutCommand({
        TableName: this.tableName,
        Item: {
          PK: `session#${opts.sessionId}`,
          SK: `art#${now.toISOString()}#${id}`,
          ...ref,
          s3Key: derivedKey,
          ttl: ttlEpoch,
          org_id: opts.identity.orgId,
          team_id: opts.identity.teamId,
          user_id: opts.identity.userId,
        },
      }),
    );

    return ref;
  }

  /** Lazy-migrate a legacy row by backfilling identity fields. */
  private async backfillIdentity(
    pk: string,
    sk: string,
    identity: CallerIdentity,
  ): Promise<void> {
    // Build SET clause dynamically — only include fields that are defined.
    // removeUndefinedValues strips undefined from ExpressionAttributeValues,
    // so referencing a stripped placeholder in UpdateExpression causes a
    // DynamoDB ValidationException.
    const parts: string[] = [];
    const values: Record<string, string> = {};
    if (identity.orgId) { parts.push('org_id = :org'); values[':org'] = identity.orgId; }
    if (identity.teamId) { parts.push('team_id = :team'); values[':team'] = identity.teamId; }
    if (identity.userId) { parts.push('user_id = :user'); values[':user'] = identity.userId; }
    if (parts.length === 0) return;

    await this.ddb.send(
      new UpdateCommand({
        TableName: this.tableName,
        Key: { PK: pk, SK: sk },
        UpdateExpression: `SET ${parts.join(', ')}`,
        ConditionExpression: 'attribute_not_exists(org_id)',
        ExpressionAttributeValues: values,
      }),
    ).catch(err => {
      // ConditionalCheckFailed means another request already backfilled — safe to ignore
      if (err.name !== 'ConditionalCheckFailedException') throw err;
    });
  }

  private guessContentType(filename: string): string {
    const ext = path.extname(filename).toLowerCase();
    const types: Record<string, string> = {
      '.pdf': 'application/pdf',
      '.csv': 'text/csv',
      '.json': 'application/json',
      '.html': 'text/html',
      '.md': 'text/markdown',
      '.txt': 'text/plain',
      '.png': 'image/png',
      '.jpg': 'image/jpeg',
      '.jpeg': 'image/jpeg',
      '.gif': 'image/gif',
      '.svg': 'image/svg+xml',
      '.zip': 'application/zip',
      '.tar': 'application/x-tar',
      '.gz': 'application/gzip',
      '.pptx': 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
      '.xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
      '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    };
    return types[ext] ?? 'application/octet-stream';
  }
}
