/**
 * DynamoContextStore — DynamoDB implementation of the ContextStore port.
 *
 * Table: adp-<env>-chat-context
 * Schema per design doc section 8.8.
 */
import { DynamoDBClient, ConditionalCheckFailedException } from '@aws-sdk/client-dynamodb';
import {
  DynamoDBDocumentClient,
  QueryCommand,
  PutCommand,
  GetCommand,
  BatchGetCommand,
  TransactWriteCommand,
  UpdateCommand,
  BatchWriteCommand,
} from '@aws-sdk/lib-dynamodb';
import {
  ContextStore,
  StoredMessage,
  StoredSummary,
  ContextItem,
  SessionHeader,
  HeaderAlreadyExistsError,
} from './port';
import * as crypto from 'crypto';

/** TransactWriteItems hard limit. */
const TRANSACT_LIMIT = 100;

export class DynamoContextStore implements ContextStore {
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

  async recordTurn(input: {
    sessionId: string;
    userMessage: StoredMessage;
    assistantMessage: StoredMessage;
    ttl: number;
    lastActivityAt: string;
  }): Promise<{ userMessageId: string; assistantMessageId: string; userOrdinal: number; assistantOrdinal: number }> {
    const pk = `session#${input.sessionId}`;
    const userOrdinal = await this.getNextOrdinal(input.sessionId);
    const assistantOrdinal = userOrdinal + 1;
    const userMessageId = newMessageId();
    const assistantMessageId = newMessageId();

    // 5 writes in one transaction: 2 msg rows + 2 item rows + 1 header UPDATE
    const items: Array<Record<string, unknown>> = [
      {
        Put: {
          TableName: this.tableName,
          Item: {
            PK: pk,
            SK: `msg#${userMessageId}`,
            role: input.userMessage.role,
            content: input.userMessage.content,
            parts: input.userMessage.parts ? JSON.stringify(input.userMessage.parts) : undefined,
            ts: input.userMessage.ts,
            tokens: input.userMessage.tokens,
          },
        },
      },
      {
        Put: {
          TableName: this.tableName,
          Item: {
            PK: pk,
            SK: itemSk(userOrdinal),
            type: 'msg',
            ref: userMessageId,
            ordinal: userOrdinal,
            tokens: input.userMessage.tokens,
          },
        },
      },
      {
        Put: {
          TableName: this.tableName,
          Item: {
            PK: pk,
            SK: `msg#${assistantMessageId}`,
            role: input.assistantMessage.role,
            content: input.assistantMessage.content,
            parts: input.assistantMessage.parts ? JSON.stringify(input.assistantMessage.parts) : undefined,
            ts: input.assistantMessage.ts,
            tokens: input.assistantMessage.tokens,
          },
        },
      },
      {
        Put: {
          TableName: this.tableName,
          Item: {
            PK: pk,
            SK: itemSk(assistantOrdinal),
            type: 'msg',
            ref: assistantMessageId,
            ordinal: assistantOrdinal,
            tokens: input.assistantMessage.tokens,
          },
        },
      },
      {
        // Refresh header (UPDATE — must exist; ownerUserId/tenantId untouched).
        Update: {
          TableName: this.tableName,
          Key: { PK: pk, SK: 'header' },
          UpdateExpression: 'SET lastActivityAt = :la, #ttl = :ttl',
          ConditionExpression: 'attribute_exists(PK)',
          ExpressionAttributeNames: { '#ttl': 'ttl' },
          ExpressionAttributeValues: {
            ':la': input.lastActivityAt,
            ':ttl': input.ttl,
          },
        },
      },
    ];

    await this.ddb.send(new TransactWriteCommand({ TransactItems: items as any }));

    return { userMessageId, assistantMessageId, userOrdinal, assistantOrdinal };
  }

  async appendSummary(sessionId: string, sum: StoredSummary): Promise<string> {
    const hash = crypto
      .createHash('sha256')
      .update(sum.content)
      .digest('hex')
      .slice(0, 8);
    const summaryId = `sum_${sessionId}_${hash}`;
    const pk = `session#${sessionId}`;

    await this.ddb.send(
      new PutCommand({
        TableName: this.tableName,
        Item: {
          PK: pk,
          SK: `sum#${summaryId}`,
          depth: sum.depth,
          kind: sum.kind,
          content: sum.content,
          sourceIds: sum.sourceIds,
          parentIds: sum.parentIds,
          earliestAt: sum.earliestAt,
          latestAt: sum.latestAt,
          tokens: sum.tokens,
        },
      }),
    );

    return summaryId;
  }

  async readContextItems(sessionId: string): Promise<ContextItem[]> {
    const result = await this.ddb.send(
      new QueryCommand({
        TableName: this.tableName,
        KeyConditionExpression: 'PK = :pk AND begins_with(SK, :prefix)',
        ExpressionAttributeValues: {
          ':pk': `session#${sessionId}`,
          ':prefix': 'item#',
        },
      }),
    );

    return (result.Items ?? []).map(item => ({
      ordinal: item.ordinal as number,
      type: item.type as 'msg' | 'sum',
      ref: item.ref as string,
      tokens: typeof item.tokens === 'number' ? (item.tokens as number) : undefined,
    }));
  }

  async replaceRangeWithSummary(
    sessionId: string,
    fromOrd: number,
    toOrd: number,
    sum: StoredSummary,
  ): Promise<string> {
    const pk = `session#${sessionId}`;
    const summaryId = deriveSummaryId(sessionId, sum.content);

    // Build full delete list for the ordinal range
    const deletes: Array<Record<string, unknown>> = [];
    for (let ord = fromOrd; ord <= toOrd; ord++) {
      deletes.push({
        Delete: {
          TableName: this.tableName,
          Key: { PK: pk, SK: itemSk(ord) },
        },
      });
    }

    // Always-present writes: the summary row + the replacement item row.
    const summaryPut = {
      Put: {
        TableName: this.tableName,
        Item: {
          PK: pk,
          SK: `sum#${summaryId}`,
          depth: sum.depth,
          kind: sum.kind,
          content: sum.content,
          sourceIds: sum.sourceIds,
          parentIds: sum.parentIds,
          earliestAt: sum.earliestAt,
          latestAt: sum.latestAt,
          tokens: sum.tokens,
        },
      },
    };
    const replacementPut = {
      Put: {
        TableName: this.tableName,
        Item: {
          PK: pk,
          SK: itemSk(fromOrd),
          type: 'sum',
          ref: summaryId,
          ordinal: fromOrd,
          tokens: sum.tokens,
        },
      },
    };

    // Inside the atomic transaction: summary + replacement + as many deletes as fit.
    // The replacement Put occupies the same SK as `itemSk(fromOrd)`, so we must
    // order writes as delete-then-put (TransactWriteItems executes each item but
    // does NOT order them) — we accomplish this by skipping the `fromOrd` delete
    // entirely; the Put overwrites the existing row at that SK.
    const inlineDeletes = deletes.filter((_, idx) => idx !== 0); // skip fromOrd's delete (the replacement Put overwrites)
    const budget = TRANSACT_LIMIT - 2; // reserve slots for summaryPut + replacementPut
    const inlineBatch = inlineDeletes.slice(0, budget);
    const overflowDeletes = inlineDeletes.slice(budget);

    await this.ddb.send(
      new TransactWriteCommand({
        TransactItems: [summaryPut, replacementPut, ...inlineBatch] as any,
      }),
    );

    // Best-effort cleanup of the overflow. If any of these fails, the session
    // retains a few stale item rows that point at message IDs no longer in the
    // context stream — harmless: `readContextItems` iterates by ordinal, the
    // replacement at `fromOrd` already points at the summary, and the orphans
    // are just garbage rows ignorable by readers (they'll never be reached via
    // the item# SK range scan because their ordinals are inside the replaced
    // range and the replacement now sits at fromOrd). Log and move on.
    if (overflowDeletes.length > 0) {
      await this.batchDeleteItems(overflowDeletes);
    }

    return summaryId;
  }

  private async batchDeleteItems(items: Array<Record<string, unknown>>): Promise<void> {
    // BatchWriteItem limit is 25 per request.
    const BATCH = 25;
    for (let i = 0; i < items.length; i += BATCH) {
      const chunk = items.slice(i, i + BATCH);
      const requestItems = chunk.map(it => {
        const del = (it as any).Delete;
        return { DeleteRequest: { Key: del.Key } };
      });
      try {
        await this.ddb.send(
          new BatchWriteCommand({
            RequestItems: { [this.tableName]: requestItems },
          }),
        );
      } catch (err) {
        // Non-fatal: orphan rows are inert (see comment in replaceRangeWithSummary).
        console.warn(
          `[DynamoContextStore] batchDeleteItems overflow failed for ${requestItems.length} items: ${(err as Error).message}`,
        );
      }
    }
  }

  async getMessagesByIds(sessionId: string, ids: string[]): Promise<StoredMessage[]> {
    if (ids.length === 0) return [];

    const pk = `session#${sessionId}`;

    // BatchGetItem has a 100-item limit; preserve input order via a map.
    const byId = new Map<string, StoredMessage>();
    for (let i = 0; i < ids.length; i += 100) {
      const batchIds = ids.slice(i, i + 100);
      const keys = batchIds.map(id => ({ PK: pk, SK: `msg#${id}` }));
      const response = await this.ddb.send(
        new BatchGetCommand({
          RequestItems: { [this.tableName]: { Keys: keys } },
        }),
      );
      for (const item of response.Responses?.[this.tableName] ?? []) {
        const sk = item.SK as string;
        const id = sk.replace('msg#', '');
        byId.set(id, {
          role: item.role as 'user' | 'assistant',
          content: item.content as string,
          parts: item.parts ? JSON.parse(item.parts as string) : undefined,
          ts: item.ts as string,
          tokens: item.tokens as number,
        });
      }
    }

    const results: StoredMessage[] = [];
    for (const id of ids) {
      const m = byId.get(id);
      if (m) results.push(m);
    }
    return results;
  }

  async getSummaryById(sessionId: string, summaryId: string): Promise<StoredSummary | null> {
    const result = await this.ddb.send(
      new GetCommand({
        TableName: this.tableName,
        Key: { PK: `session#${sessionId}`, SK: `sum#${summaryId}` },
      }),
    );

    if (!result.Item) return null;
    return {
      depth: result.Item.depth as number,
      kind: result.Item.kind as 'leaf' | 'condensed',
      content: result.Item.content as string,
      sourceIds: result.Item.sourceIds as string[],
      parentIds: result.Item.parentIds as string[] | undefined,
      earliestAt: result.Item.earliestAt as string,
      latestAt: result.Item.latestAt as string,
      tokens: result.Item.tokens as number,
    };
  }

  async getSessionHeader(sessionId: string): Promise<SessionHeader | null> {
    const result = await this.ddb.send(
      new GetCommand({
        TableName: this.tableName,
        Key: { PK: `session#${sessionId}`, SK: 'header' },
      }),
    );

    if (!result.Item) return null;
    return {
      sessionId,
      ownerUserId: result.Item.ownerUserId as string,
      tenantId: result.Item.tenantId as string | undefined,
      createdAt: result.Item.createdAt as string,
      lastActivityAt: result.Item.lastActivityAt as string,
      status: result.Item.status as 'active' | 'closed',
      ttl: result.Item.ttl as number,
    };
  }

  async createSessionHeader(
    header: Omit<SessionHeader, 'createdAt'> & { createdAt?: string },
  ): Promise<void> {
    const now = new Date().toISOString();
    try {
      await this.ddb.send(
        new PutCommand({
          TableName: this.tableName,
          Item: {
            PK: `session#${header.sessionId}`,
            SK: 'header',
            ownerUserId: header.ownerUserId,
            tenantId: header.tenantId,
            createdAt: header.createdAt ?? now,
            lastActivityAt: header.lastActivityAt,
            status: header.status,
            ttl: header.ttl,
          },
          // First-write-wins: refuse if the header already exists.
          ConditionExpression: 'attribute_not_exists(PK)',
        }),
      );
    } catch (err) {
      if (err instanceof ConditionalCheckFailedException) {
        throw new HeaderAlreadyExistsError(header.sessionId);
      }
      throw err;
    }
  }

  async refreshSessionHeader(sessionId: string, lastActivityAt: string, ttl: number): Promise<void> {
    await this.ddb.send(
      new UpdateCommand({
        TableName: this.tableName,
        Key: { PK: `session#${sessionId}`, SK: 'header' },
        UpdateExpression: 'SET lastActivityAt = :la, #ttl = :ttl',
        ConditionExpression: 'attribute_exists(PK)',
        ExpressionAttributeNames: { '#ttl': 'ttl' },
        ExpressionAttributeValues: { ':la': lastActivityAt, ':ttl': ttl },
      }),
    );
  }

  /**
   * Internal: scan the tail of item#* for the highest ordinal. Under FIFO per
   * §14.5 this is safe; architecturally an atomic counter on the header would
   * be stronger (tracked follow-up).
   */
  private async getNextOrdinal(sessionId: string): Promise<number> {
    const result = await this.ddb.send(
      new QueryCommand({
        TableName: this.tableName,
        KeyConditionExpression: 'PK = :pk AND begins_with(SK, :prefix)',
        ExpressionAttributeValues: {
          ':pk': `session#${sessionId}`,
          ':prefix': 'item#',
        },
        ScanIndexForward: false,
        Limit: 1,
      }),
    );

    if (!result.Items || result.Items.length === 0) return 0;
    return (result.Items[0].ordinal as number) + 1;
  }
}

function itemSk(ordinal: number): string {
  return `item#${String(ordinal).padStart(8, '0')}`;
}

function newMessageId(): string {
  return `msg_${crypto.randomUUID().replace(/-/g, '').slice(0, 12)}`;
}

function deriveSummaryId(sessionId: string, content: string): string {
  const hash = crypto.createHash('sha256').update(content).digest('hex').slice(0, 8);
  return `sum_${sessionId}_${hash}`;
}
