/**
 * S3ArtifactStore — S3 + DynamoDB catalog for session artifacts.
 *
 * Keying: s3://<bucket>/<sessionId>/<taskId>/<filename>
 * Catalog: DynamoDB table with GSI on artifact id and org_id.
 *
 * Stage B (#185): identity fields (org_id, team_id, user_id) are written to
 * every catalog row. listBySession filters by team_id; fetch verifies team
 * match. Legacy rows (no identity) are visible only to their original session
 * and are lazy-migrated on read when identity is available.
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
import * as fs from 'fs';
import * as path from 'path';
import * as crypto from 'crypto';

const DEFAULT_TTL_DAYS = 30;
const PRESIGNED_URL_EXPIRY = 7 * 86400; // 7 days

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
    const filename = input.filename ?? path.basename(input.localPath);
    const taskId = input.taskId ?? 'default';
    const s3Key = `${input.sessionId}/${taskId}/${filename}`;
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
    identity?: CallerIdentity,
  ): Promise<void> {
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

    // Stage B (#185): team-level access check
    const rowTeamId = item.team_id as string | undefined;
    if (rowTeamId && identity?.teamId && rowTeamId !== identity.teamId) {
      throw new Error(`Access denied: artifact ${artifactId} belongs to a different team`);
    }

    // Lazy migration: backfill identity on legacy rows
    if (!item.org_id && identity?.orgId) {
      await this.backfillIdentity(item.PK as string, item.SK as string, identity);
    }

    const s3Key = item.s3Key as string;
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

    let items = (response.Items ?? []).map(item => {
      // Lazy migration: backfill identity on legacy rows (fire-and-forget)
      if (!item.org_id && identity?.orgId) {
        this.backfillIdentity(item.PK as string, item.SK as string, identity).catch(() => {});
      }

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
        // Carry team_id for filtering (not part of ArtifactRef, stripped below)
        _teamId: item.team_id as string | undefined,
      };
    });

    // Stage B (#185): team-level filtering
    // If caller has a team_id, hide rows from other teams.
    // Legacy rows (no team_id) are visible only within the same session (already scoped by PK).
    if (identity?.teamId) {
      items = items.filter(i => !i._teamId || i._teamId === identity.teamId);
    }

    if (filter?.contentType) {
      items = items.filter(i => i.contentType === filter.contentType);
    }
    if (filter?.filename) {
      items = items.filter(i => i.filename.includes(filter.filename!));
    }

    // Strip internal _teamId before returning
    return items.map(({ _teamId, ...rest }) => rest);
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
          await store.fetch(input.id as string, input.dest_path as string, identity);
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
