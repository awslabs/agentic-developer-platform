/**
 * Session Sweeper Lambda
 *
 * Subscribes to the DynamoDB TTL stream on adp-<env>-chat-context.
 * When a session header is expired (REMOVE event for SK="header"):
 *   1. Query all rows for that session PK
 *   2. BatchDelete all children (msg#*, sum#*, item#*)
 *   3. Query artifact catalog for the session
 *   4. BatchDelete catalog rows + S3 DeleteObjects for the session prefix
 *
 * Handles partial failures with retry and DLQ escalation.
 *
 * Ownership-scoped deletion (#5660 / A07)
 * ---------------------------------------
 * The S3 prefix used to be `${sessionId}/` — the session id taken directly as a
 * storage path. Uploads, however, live under the hierarchical layout
 *
 *   o/<org>/t/<team>/u/<user>/s/<session>/<task>/{in|out}/<file>
 *
 * whose leading segments are fixed. A session named `o` therefore produced the
 * prefix `o/`, and this job — on that session's ordinary TTL expiry, long after
 * the name was chosen — deleted every tenant's uploaded files, with no bucket
 * versioning to restore from.
 *
 * So the prefix is no longer taken from anywhere near the client. It is derived
 * from the expiring header's own recorded owner (`OldImage`, the authoritative
 * ownership record the TTL stream hands us), and only at the FULL depth of the
 * layout. If the owner record is missing, malformed, or the id fails shape
 * validation, the session is skipped, logged and counted — never guessed at,
 * and never widened to a shallower prefix.
 *
 * Legacy flat-key sessions (`<session>/<task>/<file>`, written before the
 * hierarchical layout) carry no owner identity. They are quarantined rather
 * than swept, because the only prefix that would reach them is the very
 * unqualified `${sessionId}/` that caused the incident. That trades some
 * unreclaimed storage for the guarantee, which is why the skip is metered:
 * the counter is the signal that the quarantine needs a deliberate,
 * separately-reviewed migration.
 *
 * Set SWEEPER_DRY_RUN=true to log intended deletions without performing them.
 */
import { DynamoDBClient } from '@aws-sdk/client-dynamodb';
import {
  DynamoDBDocumentClient,
  GetCommand,
  QueryCommand,
  TransactWriteCommand,
} from '@aws-sdk/lib-dynamodb';
import {
  S3Client,
  ListObjectsV2Command,
  DeleteObjectsCommand,
} from '@aws-sdk/client-s3';
import { isValidSessionId } from '../session-id';

const CONTEXT_TABLE = process.env.CONTEXT_TABLE ?? '';
const ARTIFACTS_TABLE = process.env.ARTIFACTS_TABLE ?? '';
const ARTIFACTS_BUCKET = process.env.ARTIFACTS_BUCKET ?? '';
const REGION = process.env.AWS_REGION ?? 'us-east-1';

/** Dry-run: log intended deletions, perform none. First-deployment safety. */
function isDryRun(): boolean {
  return (process.env.SWEEPER_DRY_RUN ?? '').toLowerCase() === 'true';
}

const ddbRaw = new DynamoDBClient({ region: REGION });
const ddb = DynamoDBDocumentClient.from(ddbRaw, {
  marshallOptions: { removeUndefinedValues: true },
});
const s3 = new S3Client({ region: REGION });

/** CloudWatch metric namespace for sweeper counters. */
const METRIC_NAMESPACE = 'ADP/ChatSweeper';

/**
 * Emit a CloudWatch metric via embedded metric format. Used for both halves of
 * the safety story: `SessionsSkipped` must be alarmed or a silent quarantine
 * means expired artifacts accumulate forever, and `SessionsSwept` must be
 * alarmed or a job that reclaims nothing looks identical to a healthy one.
 */
function emitMetric(name: string, value: number, reason?: string): void {
  console.log(
    JSON.stringify({
      _aws: {
        Timestamp: Date.now(),
        CloudWatchMetrics: [
          {
            Namespace: METRIC_NAMESPACE,
            Dimensions: [[]],
            Metrics: [{ Name: name, Unit: 'Count' }],
          },
        ],
      },
      [name]: value,
      ...(reason ? { reason } : {}),
    }),
  );
}

/** The owner identity recorded on a session header. */
interface SessionOwner {
  orgId: string;
  teamId: string;
  userId: string;
}

interface DynamoDBStreamEvent {
  Records: Array<{
    eventName: string;
    dynamodb?: {
      Keys?: Record<string, { S?: string }>;
      OldImage?: Record<string, unknown>;
    };
  }>;
}

/** Read a DynamoDB stream `OldImage` attribute as a string. */
function streamString(image: Record<string, unknown> | undefined, key: string): string {
  const attr = image?.[key] as { S?: string } | undefined;
  return typeof attr?.S === 'string' ? attr.S : '';
}

/**
 * Extract the owner identity from the expiring header image.
 *
 * Returns null when any component is absent. A partial identity is treated as
 * no identity on purpose: a prefix built from a blank segment collapses to a
 * shallower scope that spans other principals, which is the bug being fixed.
 */
export function extractSessionOwner(
  oldImage: Record<string, unknown> | undefined,
): SessionOwner | null {
  const orgId = streamString(oldImage, 'orgId');
  const teamId = streamString(oldImage, 'teamId');
  const userId = streamString(oldImage, 'ownerUserId');
  if (!orgId || !teamId || !userId) return null;
  // A segment containing a separator would escape its own level of the layout.
  for (const segment of [orgId, teamId, userId]) {
    if (!/^[A-Za-z0-9_-]+$/.test(segment)) return null;
  }
  return { orgId, teamId, userId };
}

/**
 * Build the S3 prefix for one session's artifacts, at the full depth of the
 * hierarchical layout. Mirrors `S3ArtifactStore.buildS3Key`.
 */
export function deriveSessionPrefix(owner: SessionOwner, sessionId: string): string {
  return `o/${owner.orgId}/t/${owner.teamId}/u/${owner.userId}/s/${sessionId}/`;
}

/**
 * The number of path segments a correctly-derived per-session prefix has:
 * o/<org>/t/<team>/u/<user>/s/<session>/ → 8 segments before the trailing slash.
 * Anything shallower spans more than one session and must never be deleted.
 */
const EXPECTED_PREFIX_SEGMENTS = 8;

/**
 * Last line of defence before any DeleteObjects call. Independent of how the
 * prefix was produced, so a future refactor of the derivation cannot quietly
 * reintroduce a bucket-wide or tenant-wide scope.
 */
export function isFullDepthSessionPrefix(prefix: string): boolean {
  if (!prefix.endsWith('/')) return false;
  if (prefix.startsWith('/') || prefix.includes('//')) return false;
  if (prefix.includes('..')) return false;
  const segments = prefix.slice(0, -1).split('/');
  if (segments.length !== EXPECTED_PREFIX_SEGMENTS) return false;
  if (segments.some(s => s.length === 0)) return false;
  // The layout's fixed markers must be in their expected positions.
  return segments[0] === 'o' && segments[2] === 't' && segments[4] === 'u' && segments[6] === 's';
}

export async function handler(event: DynamoDBStreamEvent): Promise<void> {
  for (const record of event.Records) {
    if (record.eventName !== 'REMOVE') continue;

    const pk = record.dynamodb?.Keys?.PK?.S;
    const sk = record.dynamodb?.Keys?.SK?.S;
    if (!pk || sk !== 'header') continue;

    const sessionId = pk.replace('session#', '');

    // Shape first: an id that is not a single safe segment must not reach a
    // DynamoDB key or a storage path, whatever its ownership record says.
    if (!isValidSessionId(sessionId)) {
      console.warn(
        `[sweeper] Skipping session with invalid id shape: ${JSON.stringify(sessionId)} — no deletion attempted`,
      );
      emitMetric('SessionsSkipped', 1, 'invalid_session_id');
      continue;
    }

    // The expiring header is the authoritative ownership record; the TTL stream
    // hands it to us in OldImage. No owner → quarantine, do not guess.
    const owner = extractSessionOwner(record.dynamodb?.OldImage);
    if (!owner) {
      console.warn(
        `[sweeper] Skipping session ${sessionId}: no complete owner identity on the expiring header. ` +
          'Artifacts are quarantined rather than deleted under an unqualified prefix.',
      );
      emitMetric('SessionsSkipped', 1, 'missing_owner_identity');
      continue;
    }

    try {
      if (await currentHeaderExists(sessionId)) {
        console.warn(
          `[sweeper] Skipping stale expiry for ${sessionId}: a current session header exists`,
        );
        emitMetric('SessionsSkipped', 1, 'session_recreated');
        continue;
      }

      console.log(`[sweeper] Cleaning up session: ${sessionId}`);
      const outcome = await cleanupSession(sessionId, owner);
      if (outcome === 'dry-run') {
        console.log(`[sweeper] Dry-run complete for session: ${sessionId}; no deletion performed`);
        emitMetric('SessionsPlanned', 1);
        continue;
      }
      console.log(`[sweeper] Successfully cleaned session: ${sessionId}`);
      emitMetric('SessionsSwept', 1);
    } catch (err) {
      if (err instanceof SessionRecreatedError) {
        console.warn(`[sweeper] Skipping stale expiry for ${sessionId}: ${err.message}`);
        emitMetric('SessionsSkipped', 1, 'session_recreated');
        continue;
      }
      console.error(`[sweeper] Failed to clean session ${sessionId}:`, (err as Error).message);
      emitMetric('SessionsFailed', 1);
      // Lambda will retry via the event source mapping
      throw err;
    }
  }
}

class SessionRecreatedError extends Error {}

async function currentHeaderExists(sessionId: string): Promise<boolean> {
  const result = await ddb.send(
    new GetCommand({
      TableName: CONTEXT_TABLE,
      Key: { PK: `session#${sessionId}`, SK: 'header' },
      ConsistentRead: true,
      ProjectionExpression: 'PK',
    }),
  );
  return Boolean(result.Item);
}

async function assertSessionStillExpired(sessionId: string): Promise<void> {
  if (await currentHeaderExists(sessionId)) {
    throw new SessionRecreatedError('a current session incarnation now owns this id');
  }
}

/** Delete rows only while the expired session still has no current header. */
async function cleanupSessionRows(sessionId: string, owner: SessionOwner): Promise<void> {
  const pk = `session#${sessionId}`;

  // 1. Delete all context table rows for this session
  await deleteAllByPK(CONTEXT_TABLE, pk);

  // 2. Delete only artifact rows whose key and recorded identity both prove
  // they belong to the expired owner. The partition itself is client-derived
  // and can contain forged or legacy rows from before ownership enforcement.
  if (ARTIFACTS_TABLE) {
    await deleteOwnedArtifactRows(pk, deriveSessionPrefix(owner, sessionId), owner);
  }
}

async function cleanupSession(sessionId: string, owner: SessionOwner): Promise<'dry-run' | 'enforced'> {
  await cleanupSessionRows(sessionId, owner);

  // 3. Delete S3 objects under the owner-derived session prefix
  if (ARTIFACTS_BUCKET) {
    await deleteS3Prefix(sessionId, owner);
  }
  return isDryRun() ? 'dry-run' : 'enforced';
}

async function deleteOwnedArtifactRows(
  pk: string,
  sessionPrefix: string,
  owner: SessionOwner,
): Promise<void> {
  let lastEvaluatedKey: Record<string, unknown> | undefined;

  do {
    const result = await ddb.send(
      new QueryCommand({
        TableName: ARTIFACTS_TABLE,
        KeyConditionExpression: 'PK = :pk',
        ExpressionAttributeValues: { ':pk': pk },
        ProjectionExpression: 'PK, SK, s3Key, org_id, team_id, user_id',
        ExclusiveStartKey: lastEvaluatedKey,
        Limit: 250,
      }),
    );

    const items = result.Items ?? [];
    lastEvaluatedKey = result.LastEvaluatedKey as Record<string, unknown> | undefined;
    const ownedItems = items.filter(item => {
      const s3Key = item.s3Key;
      return typeof s3Key === 'string' &&
        s3Key.length > sessionPrefix.length &&
        s3Key.startsWith(sessionPrefix) &&
        !s3Key.includes('..') &&
        item.org_id === owner.orgId &&
        item.team_id === owner.teamId &&
        item.user_id === owner.userId;
    });
    const skippedCount = items.length - ownedItems.length;

    if (skippedCount > 0) {
      console.warn(
        `[sweeper] Quarantining ${skippedCount} unverified artifact row(s) for ${pk}; ` +
          'catalog ownership was not inferred from the session partition',
      );
      emitMetric('ArtifactRowsSkipped', skippedCount, 'unverified_artifact_owner');
    }

    if (isDryRun()) {
      if (ownedItems.length > 0) {
        console.log(
          `[sweeper][dry-run] Would delete ${ownedItems.length} verified artifact rows for ${pk}`,
        );
      }
      continue;
    }

    await deleteRowsConditionally(ARTIFACTS_TABLE, pk, ownedItems);
  } while (lastEvaluatedKey);
}

async function deleteAllByPK(tableName: string, pk: string): Promise<void> {
  let lastEvaluatedKey: Record<string, unknown> | undefined;

  do {
    const result = await ddb.send(
      new QueryCommand({
        TableName: tableName,
        KeyConditionExpression: 'PK = :pk',
        ExpressionAttributeValues: { ':pk': pk },
        ProjectionExpression: 'PK, SK',
        ExclusiveStartKey: lastEvaluatedKey,
        Limit: 250,
      }),
    );

    const items = result.Items ?? [];
    lastEvaluatedKey = result.LastEvaluatedKey as Record<string, unknown> | undefined;

    if (isDryRun()) {
      if (items.length > 0) {
        console.log(`[sweeper][dry-run] Would delete ${items.length} rows from ${tableName} for ${pk}`);
      }
      continue;
    }

    await deleteRowsConditionally(tableName, pk, items);
  } while (lastEvaluatedKey);
}

async function deleteRowsConditionally(
  tableName: string,
  pk: string,
  items: Record<string, unknown>[],
): Promise<void> {
  // Each delete batch is conditional on the context header still being
  // absent. A delayed TTL event therefore cannot delete rows belonging to a
  // session incarnation created after this event's OldImage was emitted.
  for (let i = 0; i < items.length; i += 99) {
    const batch = items.slice(i, i + 99);
    await ddb.send(
      new TransactWriteCommand({
        TransactItems: [
          {
            ConditionCheck: {
              TableName: CONTEXT_TABLE,
              Key: { PK: pk, SK: 'header' },
              ConditionExpression: 'attribute_not_exists(PK)',
            },
          },
          ...batch.map(item => ({
            Delete: { TableName: tableName, Key: { PK: item.PK, SK: item.SK } },
          })),
        ],
      }),
    );
  }
}

async function deleteS3Prefix(sessionId: string, owner: SessionOwner): Promise<void> {
  const prefix = deriveSessionPrefix(owner, sessionId);

  // Independent re-check of the derived value. A prefix shallower than one
  // session would delete another principal's artifacts, so refuse rather than
  // trust the derivation above.
  if (!isFullDepthSessionPrefix(prefix)) {
    console.error(
      `[sweeper] Refusing to delete under non-session-scoped prefix ${JSON.stringify(prefix)} for session ${sessionId}`,
    );
    emitMetric('SessionsSkipped', 1, 'prefix_not_session_scoped');
    return;
  }

  let continuationToken: string | undefined;

  do {
    await assertSessionStillExpired(sessionId);
    const result = await s3.send(
      new ListObjectsV2Command({
        Bucket: ARTIFACTS_BUCKET,
        Prefix: prefix,
        ContinuationToken: continuationToken,
        MaxKeys: 1000,
      }),
    );

    const objects = result.Contents ?? [];
    continuationToken = result.NextContinuationToken;

    if (objects.length === 0) continue;

    if (isDryRun()) {
      console.log(
        `[sweeper][dry-run] Would delete ${objects.length} objects under ${prefix}: ` +
          JSON.stringify(objects.slice(0, 10).map(o => o.Key)),
      );
      continue;
    }

    await assertSessionStillExpired(sessionId);
    await s3.send(
      new DeleteObjectsCommand({
        Bucket: ARTIFACTS_BUCKET,
        Delete: {
          Objects: objects.map(o => ({ Key: o.Key! })),
          Quiet: true,
        },
      }),
    );
  } while (continuationToken);
}
