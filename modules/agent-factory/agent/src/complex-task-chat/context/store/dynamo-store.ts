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
  TranscriptEntry,
  SessionHeader,
  HeaderAlreadyExistsError,
} from './port';
import * as crypto from 'crypto';

/** TransactWriteItems hard limit. */
const TRANSACT_LIMIT = 100;

export class DynamoContextStore implements ContextStore {
  private readonly ddb: DynamoDBDocumentClient;
  private scrubber?: { scrub(text: string): string };

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

  /**
   * Attach a scrubber instance. All subsequent write operations (recordTurn,
   * appendSummary, replaceRangeWithSummary) will scrub text content before
   * persisting to DDB. Task-scoped: set once per task run.
   */
  setScrubber(scrubber: { scrub(text: string): string }): void {
    this.scrubber = scrubber;
  }

  /** Apply scrubber to text if one is configured. */
  private scrubText(text: string): string {
    return this.scrubber ? this.scrubber.scrub(text) : text;
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

    // Scrub message content before persistence (LCM scrubber, #137)
    const userContent = this.scrubText(input.userMessage.content);
    const assistantContent = this.scrubText(input.assistantMessage.content);

    // 5 writes in one transaction: 2 msg rows + 2 item rows + 1 header UPDATE
    const items: Array<Record<string, unknown>> = [
      {
        Put: {
          TableName: this.tableName,
          Item: {
            PK: pk,
            SK: `msg#${userMessageId}`,
            role: input.userMessage.role,
            content: userContent,
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
            content: assistantContent,
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
    // Scrub summary content before persistence and hash derivation (#137)
    const scrubbedContent = this.scrubText(sum.content);
    const hash = crypto
      .createHash('sha256')
      .update(scrubbedContent)
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
          content: scrubbedContent,
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

  /**
   * Every context item for a session, in ordinal order, with NO page cap.
   *
   * `readContextItems` above issues a single Query and drops
   * `LastEvaluatedKey`, so it silently stops at DynamoDB's 1 MB page boundary.
   * That is fine for context assembly (which is token-bounded anyway) but wrong
   * for a hand-off that must see the whole conversation, so this drains every
   * page (#4208).
   *
   * Ordering: the SK is `item#` + an 8-digit zero-padded ordinal, so DDB's
   * lexicographic sort IS numeric order and pages arrive already sorted.
   */
  async readAllContextItems(sessionId: string): Promise<ContextItem[]> {
    const items: ContextItem[] = [];
    let exclusiveStartKey: Record<string, unknown> | undefined;

    do {
      const result = await this.ddb.send(
        new QueryCommand({
          TableName: this.tableName,
          KeyConditionExpression: 'PK = :pk AND begins_with(SK, :prefix)',
          ExpressionAttributeValues: {
            ':pk': `session#${sessionId}`,
            ':prefix': 'item#',
          },
          ExclusiveStartKey: exclusiveStartKey,
        }),
      );

      for (const item of result.Items ?? []) {
        items.push({
          ordinal: item.ordinal as number,
          type: item.type as 'msg' | 'sum',
          ref: item.ref as string,
          tokens: typeof item.tokens === 'number' ? (item.tokens as number) : undefined,
        });
      }

      exclusiveStartKey = result.LastEvaluatedKey as Record<string, unknown> | undefined;
    } while (exclusiveStartKey);

    // Defensive: DDB returns SK order, but a hand-off consumer depends on
    // ordinal order specifically. Sorting costs nothing at these sizes.
    items.sort((a, b) => a.ordinal - b.ordinal);
    return items;
  }

  /**
   * The full ordered transcript for a session (#4208).
   *
   * Hydrates every context item into its underlying record: `msg` refs via
   * `getMessagesByIds` (batched) and `sum` refs via `getSummaryById`. Summaries
   * appear inline at the ordinal they occupy, because `replaceRangeWithSummary`
   * destructively evicts the turns it summarizes — dropping summaries would
   * leave a silent hole in the conversation rather than a compressed one.
   *
   * Deliberately NOT capped: this is the input to the inception hand-off, and a
   * truncated hand-off means inception re-asks everything the intake already
   * established.
   */
  async getFullTranscript(sessionId: string): Promise<TranscriptEntry[]> {
    const items = await this.readAllContextItems(sessionId);
    if (items.length === 0) return [];

    // Batch-fetch all messages in one pass rather than per-item round trips.
    // Keyed, NOT positional: `getMessagesByIds` omits rows it cannot find, so
    // zipping its array against the ID list would shift every message after an
    // evicted one onto the wrong ordinal and role.
    const messageIds = items.filter(i => i.type === 'msg').map(i => i.ref);
    const messageById = await this.getMessageMapByIds(sessionId, messageIds);

    const entries: TranscriptEntry[] = [];
    for (const item of items) {
      if (item.type === 'msg') {
        const message = messageById.get(item.ref);
        // A missing message means the row was evicted or TTL'd out from under
        // us. Skip it rather than emitting a hole the consumer must handle.
        if (message) {
          entries.push({ ordinal: item.ordinal, type: 'msg', ref: item.ref, message });
        }
        continue;
      }

      const summary = await this.getSummaryById(sessionId, item.ref);
      if (summary) {
        entries.push({ ordinal: item.ordinal, type: 'sum', ref: item.ref, summary });
      }
    }

    return entries;
  }

  async replaceRangeWithSummary(
    sessionId: string,
    fromOrd: number,
    toOrd: number,
    sum: StoredSummary,
  ): Promise<string> {
    const pk = `session#${sessionId}`;
    // Scrub summary content before persistence (#137)
    const scrubbedContent = this.scrubText(sum.content);
    const summaryId = deriveSummaryId(sessionId, scrubbedContent);

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
          content: scrubbedContent,
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
    const byId = await this.getMessageMapByIds(sessionId, ids);

    const results: StoredMessage[] = [];
    for (const id of ids) {
      const m = byId.get(id);
      if (m) results.push(m);
    }
    return results;
  }

  /**
   * Batch-fetch messages, keyed by ID.
   *
   * `getMessagesByIds` returns a positional array with missing rows OMITTED, so
   * callers cannot zip it back against their input IDs — one evicted message
   * shifts every later message onto the wrong ID. Anything that needs the
   * ID→message association (e.g. transcript hydration) must use this instead.
   */
  private async getMessageMapByIds(
    sessionId: string,
    ids: string[],
  ): Promise<Map<string, StoredMessage>> {
    const byId = new Map<string, StoredMessage>();
    if (ids.length === 0) return byId;

    const pk = `session#${sessionId}`;

    // BatchGetItem has a 100-item limit.
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

    return byId;
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
      // Stage A (#184): extended identity claims
      orgId: result.Item.orgId as string | undefined,
      teamId: result.Item.teamId as string | undefined,
      departmentId: result.Item.departmentId as string | undefined,
      accountType: result.Item.accountType as string | undefined,
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
            // Stage A (#184): extended identity claims
            orgId: header.orgId,
            teamId: header.teamId,
            departmentId: header.departmentId,
            accountType: header.accountType,
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
