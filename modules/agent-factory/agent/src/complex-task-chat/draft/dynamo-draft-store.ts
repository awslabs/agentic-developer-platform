/**
 * DynamoDraftStore — DDB implementation of the DraftStore port (#4208).
 *
 * Rides the EXISTING chat-context table (adp-<env>-chat-context) as one row per
 * session: PK=session#<sessionId>, SK=draft. No new table, no new column, no
 * migration — the row sits alongside the `item#` / `msg#` / `sum#` rows the LCM
 * context store already writes under the same partition key.
 */
import { DynamoDBClient } from '@aws-sdk/client-dynamodb';
import { DynamoDBDocumentClient, GetCommand, PutCommand } from '@aws-sdk/lib-dynamodb';
import { DraftStore, IntentDraft } from './port';

/** Sort key for the single draft row in a session's partition. */
const DRAFT_SK = 'draft';

export class DynamoDraftStore implements DraftStore {
  private readonly ddb: DynamoDBDocumentClient;

  constructor(
    private readonly tableName: string,
    region: string = 'us-east-1',
    client?: DynamoDBDocumentClient,
    /**
     * TTL to stamp on the draft row, matching the session's own expiry so
     * drafts do not outlive the conversation they describe. Omitted => no TTL.
     */
    private readonly ttl?: number,
  ) {
    if (client) {
      this.ddb = client;
    } else {
      this.ddb = DynamoDBDocumentClient.from(new DynamoDBClient({ region }), {
        marshallOptions: { removeUndefinedValues: true },
      });
    }
  }

  async get(sessionId: string): Promise<IntentDraft | null> {
    const result = await this.ddb.send(
      new GetCommand({
        TableName: this.tableName,
        Key: { PK: `session#${sessionId}`, SK: DRAFT_SK },
      }),
    );
    if (!result.Item) return null;
    return (result.Item.draft as IntentDraft) ?? null;
  }

  async put(sessionId: string, draft: IntentDraft): Promise<IntentDraft> {
    const stored: IntentDraft = { ...draft, updatedAt: new Date().toISOString() };

    await this.ddb.send(
      new PutCommand({
        TableName: this.tableName,
        Item: {
          PK: `session#${sessionId}`,
          SK: DRAFT_SK,
          draft: stored,
          ...(this.ttl ? { ttl: this.ttl } : {}),
        },
      }),
    );

    return stored;
  }
}

/** No-op store for when draft persistence is not configured. */
export class NoopDraftStore implements DraftStore {
  private drafts = new Map<string, IntentDraft>();

  async get(sessionId: string): Promise<IntentDraft | null> {
    return this.drafts.get(sessionId) ?? null;
  }

  async put(sessionId: string, draft: IntentDraft): Promise<IntentDraft> {
    const stored: IntentDraft = { ...draft, updatedAt: new Date().toISOString() };
    this.drafts.set(sessionId, stored);
    return stored;
  }
}

/**
 * Build a DraftStore from env. Uses the same CONTEXT_TABLE the LCM context
 * store uses, since the draft row lives in that table's session partition.
 * No table configured => in-memory no-op, so the tool still works (the panel
 * updates for the life of the turn) without failing the run.
 */
export function buildDraftStore(
  env: Record<string, string | undefined> = process.env,
  ttl?: number,
): DraftStore {
  const table = env.CONTEXT_TABLE;
  if (!table) return new NoopDraftStore();
  return new DynamoDraftStore(table, env.AWS_REGION ?? 'us-east-1', undefined, ttl);
}
