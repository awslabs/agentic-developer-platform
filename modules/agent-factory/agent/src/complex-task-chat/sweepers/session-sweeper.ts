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
 */
import { DynamoDBClient } from '@aws-sdk/client-dynamodb';
import {
  DynamoDBDocumentClient,
  QueryCommand,
  BatchWriteCommand,
} from '@aws-sdk/lib-dynamodb';
import {
  S3Client,
  ListObjectsV2Command,
  DeleteObjectsCommand,
} from '@aws-sdk/client-s3';

const CONTEXT_TABLE = process.env.CONTEXT_TABLE ?? '';
const ARTIFACTS_TABLE = process.env.ARTIFACTS_TABLE ?? '';
const ARTIFACTS_BUCKET = process.env.ARTIFACTS_BUCKET ?? '';
const REGION = process.env.AWS_REGION ?? 'us-east-1';

const ddbRaw = new DynamoDBClient({ region: REGION });
const ddb = DynamoDBDocumentClient.from(ddbRaw, {
  marshallOptions: { removeUndefinedValues: true },
});
const s3 = new S3Client({ region: REGION });

interface DynamoDBStreamEvent {
  Records: Array<{
    eventName: string;
    dynamodb?: {
      Keys?: Record<string, { S?: string }>;
      OldImage?: Record<string, unknown>;
    };
  }>;
}

export async function handler(event: DynamoDBStreamEvent): Promise<void> {
  for (const record of event.Records) {
    if (record.eventName !== 'REMOVE') continue;

    const pk = record.dynamodb?.Keys?.PK?.S;
    const sk = record.dynamodb?.Keys?.SK?.S;
    if (!pk || sk !== 'header') continue;

    const sessionId = pk.replace('session#', '');
    console.log(`[sweeper] Cleaning up session: ${sessionId}`);

    try {
      await cleanupSession(sessionId);
      console.log(`[sweeper] Successfully cleaned session: ${sessionId}`);
    } catch (err) {
      console.error(`[sweeper] Failed to clean session ${sessionId}:`, (err as Error).message);
      // Lambda will retry via the event source mapping
      throw err;
    }
  }
}

async function cleanupSession(sessionId: string): Promise<void> {
  const pk = `session#${sessionId}`;

  // 1. Delete all context table rows for this session
  await deleteAllByPK(CONTEXT_TABLE, pk);

  // 2. Delete artifact catalog rows
  if (ARTIFACTS_TABLE) {
    await deleteAllByPK(ARTIFACTS_TABLE, pk);
  }

  // 3. Delete S3 objects under the session prefix
  if (ARTIFACTS_BUCKET) {
    await deleteS3Prefix(sessionId);
  }
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

    // BatchWrite in groups of 25
    for (let i = 0; i < items.length; i += 25) {
      const batch = items.slice(i, i + 25);
      const deleteRequests = batch.map(item => ({
        DeleteRequest: { Key: { PK: item.PK, SK: item.SK } },
      }));

      await ddb.send(
        new BatchWriteCommand({
          RequestItems: { [tableName]: deleteRequests },
        }),
      );
    }
  } while (lastEvaluatedKey);
}

async function deleteS3Prefix(sessionId: string): Promise<void> {
  let continuationToken: string | undefined;

  do {
    const result = await s3.send(
      new ListObjectsV2Command({
        Bucket: ARTIFACTS_BUCKET,
        Prefix: `${sessionId}/`,
        ContinuationToken: continuationToken,
        MaxKeys: 1000,
      }),
    );

    const objects = result.Contents ?? [];
    continuationToken = result.NextContinuationToken;

    if (objects.length > 0) {
      await s3.send(
        new DeleteObjectsCommand({
          Bucket: ARTIFACTS_BUCKET,
          Delete: {
            Objects: objects.map(o => ({ Key: o.Key! })),
            Quiet: true,
          },
        }),
      );
    }
  } while (continuationToken);
}
