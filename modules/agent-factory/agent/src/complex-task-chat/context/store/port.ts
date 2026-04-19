/**
 * ContextStore port — persistence layer for session context items.
 *
 * Implementations: DynamoContextStore
 */

export interface StoredMessage {
  role: 'user' | 'assistant';
  content: string;
  parts?: unknown[];
  ts: string;
  tokens: number;
}

export interface StoredSummary {
  depth: number;
  kind: 'leaf' | 'condensed';
  content: string;
  sourceIds: string[];
  parentIds?: string[];
  earliestAt: string;
  latestAt: string;
  tokens: number;
}

export interface ContextItem {
  /** Ordinal position in the timeline */
  ordinal: number;
  /** Discriminator: 'msg' for raw message, 'sum' for summary */
  type: 'msg' | 'sum';
  /** Reference to the message or summary record */
  ref: string;
  /** Token count of the underlying record (joined at read time when available). */
  tokens?: number;
}

export interface SessionHeader {
  sessionId: string;
  ownerUserId: string;
  tenantId?: string;
  createdAt: string;
  lastActivityAt: string;
  status: 'active' | 'closed';
  ttl: number;
}

/**
 * Raised by `createSessionHeader` when the header already exists.
 * Callers should re-fetch via `getSessionHeader` and verify ownership.
 */
export class HeaderAlreadyExistsError extends Error {
  constructor(sessionId: string) {
    super(`Session header already exists for ${sessionId}`);
    this.name = 'HeaderAlreadyExistsError';
  }
}

export interface ContextStore {
  /**
   * Atomically record one full turn: user message + assistant message + header refresh.
   * Single TransactWriteItems per design doc §8.5. Returns ordinals of the two new messages.
   */
  recordTurn(input: {
    sessionId: string;
    userMessage: StoredMessage;
    assistantMessage: StoredMessage;
    ttl: number;
    lastActivityAt: string;
  }): Promise<{ userMessageId: string; assistantMessageId: string; userOrdinal: number; assistantOrdinal: number }>;

  /** Append a summary record. Returns summaryId. */
  appendSummary(sessionId: string, sum: StoredSummary): Promise<string>;

  /** Read all context items (ordered by ordinal). Includes `tokens` when the backing record carries one. */
  readContextItems(sessionId: string): Promise<ContextItem[]>;

  /**
   * Atomically create the summary and replace the given ordinal range with a single
   * item pointing at it. This is one transactional unit — if it fails, no orphaned
   * summary is left. If the range has >98 items (TransactWriteItems limit 100 minus
   * summary + replacement), the excess deletes are performed best-effort AFTER the
   * atomic summary+replacement write, so the catalog is always self-consistent.
   */
  replaceRangeWithSummary(
    sessionId: string,
    fromOrd: number,
    toOrd: number,
    sum: StoredSummary,
  ): Promise<string>;

  /** Batch-fetch raw messages by their IDs. Preserves input order. */
  getMessagesByIds(sessionId: string, ids: string[]): Promise<StoredMessage[]>;

  /** Fetch a single summary by ID. */
  getSummaryById(sessionId: string, summaryId: string): Promise<StoredSummary | null>;

  /** Get the session header. Returns null if no session exists. */
  getSessionHeader(sessionId: string): Promise<SessionHeader | null>;

  /**
   * Create the session header exactly once (conditional on attribute_not_exists).
   * Throws `HeaderAlreadyExistsError` if a header for this session already exists.
   */
  createSessionHeader(header: Omit<SessionHeader, 'createdAt'> & { createdAt?: string }): Promise<void>;

  /**
   * Refresh an existing header's `lastActivityAt` + `ttl`. Does NOT touch
   * ownerUserId/tenantId/createdAt/status. Use `UpdateItem` with condition that
   * the header exists.
   */
  refreshSessionHeader(sessionId: string, lastActivityAt: string, ttl: number): Promise<void>;
}
