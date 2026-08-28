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

/**
 * One hydrated entry in a session's full transcript (#4208).
 *
 * `msg` entries carry the raw message; `sum` entries carry a summary that
 * replaced a range of evicted turns. Consumers must handle both — a session
 * that has been compacted has genuine summaries in its timeline, and treating
 * them as absent loses the conversation they stand in for.
 */
export interface TranscriptEntry {
  ordinal: number;
  type: 'msg' | 'sum';
  /** The message or summary ID this entry was hydrated from. */
  ref: string;
  message?: StoredMessage;
  summary?: StoredSummary;
}

export interface SessionHeader {
  sessionId: string;
  ownerUserId: string;
  tenantId?: string;
  // Stage A (#184): extended identity claims for team-aware ownership.
  // All optional for backward compatibility with existing sessions.
  orgId?: string;
  teamId?: string;
  departmentId?: string;
  accountType?: string;
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

  /**
   * Read context items (ordered by ordinal). Includes `tokens` when the backing
   * record carries one.
   *
   * NOTE: implementations may stop at one page of results. Context assembly is
   * token-bounded so that is fine there; use `readAllContextItems` when you
   * need every item.
   */
  readContextItems(sessionId: string): Promise<ContextItem[]>;

  /**
   * Read EVERY context item for a session in ordinal order, draining all pages
   * (#4208). Optional so existing test doubles need not implement it.
   */
  readAllContextItems?(sessionId: string): Promise<ContextItem[]>;

  /**
   * The full ordered transcript, with messages and summaries hydrated inline
   * (#4208). Not capped — this feeds the inception hand-off, and a truncated
   * transcript makes inception re-ask what the intake already established.
   * Optional so existing test doubles need not implement it.
   */
  getFullTranscript?(sessionId: string): Promise<TranscriptEntry[]>;

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
