/**
 * Session identifier validation (#5660 / A07).
 *
 * A session id arrives from the client and is used BOTH as a DynamoDB key
 * component and as a path segment in the artifact S3 key layout:
 *
 *   o/<org_id>/t/<team_id>/u/<user_id>/s/<session_id>/<task_id>/{in|out}/<file>
 *
 * Because it lands in a storage path, an id that is not a single harmless
 * segment is a storage-scope bug, not a cosmetic one. Two concrete failures
 * this guards:
 *
 *  - A separator or `..` lets the id escape its own prefix, so a derived
 *    "delete everything under this session" scope reaches other sessions.
 *  - A bare `o` / `t` / `u` / `s` collides with the FIXED leading segments of
 *    the layout above. The sweeper's legacy prefix was `${sessionId}/`, so a
 *    session literally named `o` produced the prefix `o/` — every tenant's
 *    uploads. That delete fires on normal TTL expiry, long after the naming,
 *    which is why shape has to be refused at the edge rather than audited.
 *
 * Validation is shape-only and deliberately says nothing about ownership;
 * `assertOwnership` remains the authority on who may use a valid id.
 */

/** Max length — comfortably fits `sess-<epoch>-<rand>` and `sess-<uuid hex>`. */
export const MAX_SESSION_ID_LENGTH = 128;

/**
 * The fixed leading segments of the hierarchical artifact key layout. A session
 * id equal to one of these would make a derived prefix ambiguous with the root
 * of the shared layout.
 */
export const RESERVED_SESSION_IDS: readonly string[] = ['o', 't', 'u', 's'];

/**
 * Single path segment of unreserved characters. Anchored, so a separator,
 * whitespace or NUL cannot appear anywhere in the value.
 *
 * The charset is set by the id formats ALREADY IN PRODUCTION, all of which must
 * keep working — an over-strict rule here would strand live conversations, which
 * is a worse outcome than the bug being fixed:
 *
 *  - the SPA's `sess-<epoch>-<rand>`            (AgentChat.tsx)
 *  - the CLI's `sess-<uuid hex>`                (intake_dispatch.py)
 *  - Slack's thread timestamp `1758441600.123456` — hence `.`
 *    (channels/slack.py passes `thread_ts` straight through as `thread_id`)
 *  - the `session_key` fallback `webchat:C123:user-1` — hence `:`
 *    (channels/base.py, used whenever a channel supplies no thread id)
 *
 * `.` and `:` are safe in both a DynamoDB key and an S3 key: neither is a path
 * separator, so neither can widen a derived prefix. `/` is excluded, and `..`
 * is rejected separately below, so no admitted value can traverse.
 */
const SESSION_ID_PATTERN = /^[A-Za-z0-9_.:-]+$/;

/**
 * Reject `..` ANYWHERE, not just as the whole value. A segment like `a..b` is
 * harmless to S3 itself, but any consumer that normalises the path — a local
 * checkout, a sync tool, a signed-URL rewriter — can resolve it upward and out
 * of the caller's prefix. The traversal risk is in the consumers, so it is
 * refused at the edge.
 */
function containsTraversal(sessionId: string): boolean {
  return sessionId.includes('..');
}

export class InvalidSessionIdError extends Error {
  constructor(
    readonly sessionId: string,
    readonly reason: string,
  ) {
    super(`Invalid session id: ${reason}`);
    this.name = 'InvalidSessionIdError';
  }
}

/**
 * True when `sessionId` is safe to use as both a DynamoDB key component and a
 * single S3 path segment.
 */
export function isValidSessionId(sessionId: unknown): sessionId is string {
  if (typeof sessionId !== 'string') return false;
  if (sessionId.length === 0 || sessionId.length > MAX_SESSION_ID_LENGTH) return false;
  if (!SESSION_ID_PATTERN.test(sessionId)) return false;
  // The charset admits `.`, so traversal must be refused explicitly.
  if (containsTraversal(sessionId)) return false;
  if (sessionId === '.') return false;
  if (RESERVED_SESSION_IDS.includes(sessionId)) return false;
  return true;
}

/**
 * Throw {@link InvalidSessionIdError} unless `sessionId` is valid.
 *
 * Rejects rather than sanitising: silently rewriting a hostile id would hide
 * the attempt and write the caller's data to a path they did not ask for.
 */
export function assertValidSessionId(sessionId: unknown): string {
  if (typeof sessionId !== 'string' || sessionId.length === 0) {
    throw new InvalidSessionIdError(String(sessionId), 'must be a non-empty string');
  }
  if (sessionId.length > MAX_SESSION_ID_LENGTH) {
    throw new InvalidSessionIdError(sessionId, `exceeds ${MAX_SESSION_ID_LENGTH} characters`);
  }
  if (!SESSION_ID_PATTERN.test(sessionId)) {
    throw new InvalidSessionIdError(
      sessionId,
      'must be a single path segment of letters, digits, dots, colons, hyphens or underscores',
    );
  }
  if (containsTraversal(sessionId) || sessionId === '.') {
    throw new InvalidSessionIdError(sessionId, 'must not be a path traversal segment');
  }
  if (RESERVED_SESSION_IDS.includes(sessionId)) {
    throw new InvalidSessionIdError(
      sessionId,
      `must not collide with a reserved artifact key segment (${RESERVED_SESSION_IDS.join(', ')})`,
    );
  }
  return sessionId;
}
