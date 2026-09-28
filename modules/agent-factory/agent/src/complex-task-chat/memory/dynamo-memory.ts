/**
 * DynamoMemoryProvider — DynamoDB-backed cross-agent memory store.
 *
 * Table: adp-<env>-agent-memory
 * Schema per design doc section 9.5.
 */
import { DynamoDBClient } from '@aws-sdk/client-dynamodb';
import {
  DynamoDBDocumentClient,
  QueryCommand,
  PutCommand,
  GetCommand,
  DeleteCommand,
  UpdateCommand,
} from '@aws-sdk/lib-dynamodb';
import { MemoryProvider, MemoryRecord, MemoryQuery, MemoryCapabilities, MemoryScope } from './types';
import { AgentTool } from '../context/types';
import { createMemoryTools } from './tools';
import * as crypto from 'crypto';

/** Per-kind TTL defaults in seconds */
const KIND_TTL: Record<string, { ttlSeconds: number | null; extendOnRead: boolean }> = {
  preference: { ttlSeconds: null, extendOnRead: false },
  fact: { ttlSeconds: 180 * 86400, extendOnRead: true },
  learning: { ttlSeconds: 90 * 86400, extendOnRead: true },
  'draft-learning': { ttlSeconds: 14 * 86400, extendOnRead: false },
};

export class DynamoMemoryProvider implements MemoryProvider {
  private readonly ddb: DynamoDBDocumentClient;

  constructor(
    private readonly tableName: string,
    region: string = 'us-east-1',
    client?: DynamoDBDocumentClient,
  ) {
    if (client) {
      this.ddb = client;
    } else {
      const rawClient = new DynamoDBClient({ region });
      this.ddb = DynamoDBDocumentClient.from(rawClient, {
        marshallOptions: { removeUndefinedValues: true },
      });
    }
  }

  async retrieve(input: MemoryQuery): Promise<MemoryRecord[]> {
    const scope = input.scope ?? {};
    const results: MemoryRecord[] = [];

    // Query by each scope dimension
    const scopeKeys = this.buildScopeKeys(scope);
    if (scopeKeys.length === 0) return [];

    for (const pk of scopeKeys) {
      const response = await this.ddb.send(
        new QueryCommand({
          TableName: this.tableName,
          KeyConditionExpression: 'PK = :pk AND begins_with(SK, :prefix)',
          ExpressionAttributeValues: {
            ':pk': pk,
            ':prefix': 'mem#',
          },
          ScanIndexForward: false, // newest first
          Limit: input.limit ?? 20,
        }),
      );

      for (const item of response.Items ?? []) {
        const record = this.itemToRecord(item);

        // Apply keyword filter if query is provided
        if (input.query && !record.content.toLowerCase().includes(input.query.toLowerCase())) {
          continue;
        }

        // Apply kind filter
        if (input.kinds && input.kinds.length > 0 && record.kind && !input.kinds.includes(record.kind)) {
          continue;
        }

        results.push(record);

        // Extend TTL on read if applicable
        const kindConfig = KIND_TTL[record.kind ?? 'fact'];
        if (kindConfig?.extendOnRead && kindConfig.ttlSeconds) {
          this.extendTtl(item.PK as string, item.SK as string, kindConfig.ttlSeconds).catch(() => {
            // Best-effort TTL extension
          });
        }
      }
    }

    // Deduplicate by id and cap at limit
    const seen = new Set<string>();
    const deduped = results.filter(r => {
      if (seen.has(r.id)) return false;
      seen.add(r.id);
      return true;
    });

    // Apply token budget cap (approximate: 4 chars/token)
    if (input.tokenBudget) {
      let tokens = 0;
      const budgeted: MemoryRecord[] = [];
      for (const r of deduped) {
        const rTokens = Math.ceil(r.content.length / 4);
        if (tokens + rTokens > input.tokenBudget) break;
        tokens += rTokens;
        budgeted.push(r);
      }
      return budgeted;
    }

    return deduped.slice(0, input.limit ?? 20);
  }

  async save(record: Omit<MemoryRecord, 'id' | 'createdAt'>): Promise<MemoryRecord> {
    const id = `mem_${crypto.randomUUID().replace(/-/g, '').slice(0, 16)}`;
    const now = new Date();
    const createdAt = now.toISOString();

    const scopeKeys = this.buildScopeKeys(record.scope);
    if (scopeKeys.length === 0) {
      throw new Error('At least one scope dimension is required');
    }

    // Compute TTL
    const kind = record.kind ?? 'fact';
    const kindConfig = KIND_TTL[kind] ?? KIND_TTL.fact;
    const ttl = kindConfig.ttlSeconds
      ? Math.floor(now.getTime() / 1000) + kindConfig.ttlSeconds
      : undefined;

    // Write to all scope dimensions
    for (const pk of scopeKeys) {
      await this.ddb.send(
        new PutCommand({
          TableName: this.tableName,
          Item: {
            PK: pk,
            SK: `mem#${createdAt}#${id}`,
            id,
            content: record.content,
            scope: record.scope,
            kind,
            tags: record.tags,
            source: record.source,
            createdAt,
            updatedAt: record.updatedAt,
            metadata: record.metadata,
            ttl,
          },
        }),
      );
    }

    return { ...record, id, createdAt };
  }

  async delete(id: string): Promise<void> {
    // Look up by GSI (by-id) to find PK/SK
    const response = await this.ddb.send(
      new QueryCommand({
        TableName: this.tableName,
        IndexName: 'by-id',
        KeyConditionExpression: 'id = :id',
        ExpressionAttributeValues: { ':id': id },
      }),
    );

    for (const item of response.Items ?? []) {
      await this.ddb.send(
        new DeleteCommand({
          TableName: this.tableName,
          Key: { PK: item.PK, SK: item.SK },
        }),
      );
    }
  }

  tools(): AgentTool[] {
    return createMemoryTools(this);
  }

  capabilities(): MemoryCapabilities {
    return {
      semanticSearch: false,
      keywordSearch: true,
      tagFiltering: false,
      scoping: ['user', 'component', 'persona', 'tenant'],
      delete: true,
      asyncExtraction: false,
      ttl: true,
    };
  }

  // ---- Private helpers ----

  private buildScopeKeys(scope: MemoryScope): string[] {
    const keys: string[] = [];
    if (scope.user) keys.push(`scope#user#${scope.user}`);
    if (scope.component) keys.push(`scope#component#${scope.component}`);
    if (scope.persona) keys.push(`scope#persona#${scope.persona}`);
    if (scope.tenant) keys.push(`scope#tenant#${scope.tenant}`);
    return keys;
  }

  private itemToRecord(item: Record<string, unknown>): MemoryRecord {
    return {
      id: item.id as string,
      content: item.content as string,
      scope: (item.scope as MemoryScope) ?? {},
      kind: item.kind as string | undefined,
      tags: item.tags as string[] | undefined,
      source: item.source as { agent?: string; sessionId?: string } | undefined,
      createdAt: item.createdAt as string,
      updatedAt: item.updatedAt as string | undefined,
      metadata: item.metadata as Record<string, unknown> | undefined,
    };
  }

  private async extendTtl(pk: string, sk: string, ttlSeconds: number): Promise<void> {
    const newTtl = Math.floor(Date.now() / 1000) + ttlSeconds;
    await this.ddb.send(
      new UpdateCommand({
        TableName: this.tableName,
        Key: { PK: pk, SK: sk },
        UpdateExpression: 'SET #ttl = :ttl',
        ExpressionAttributeNames: { '#ttl': 'ttl' },
        ExpressionAttributeValues: { ':ttl': newTtl },
      }),
    );
  }
}
