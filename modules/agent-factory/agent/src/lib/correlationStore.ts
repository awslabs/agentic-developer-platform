import { workerAwsCredentials, workerAwsRegion } from './runIdentity';
/**
 * DynamoDB correlation pointer writer for the Node agent runtime.
 *
 * Writes pointers after successful outbound GitHub actions so that the next
 * inbound webhook on the same channel can look up the active correlation.
 *
 * Phase 2-d of EPIC #779. Intentional duplication of the worker-image Python
 * version — the Node agent runs in a separate process.
 */

import { DynamoDBClient, UpdateItemCommand } from '@aws-sdk/client-dynamodb';

let _client: DynamoDBClient | null = null;

function getClient(): DynamoDBClient {
  if (!_client) {
    _client = new DynamoDBClient({ region: workerAwsRegion(), credentials: workerAwsCredentials() });
  }
  return _client;
}

/**
 * Write a correlation pointer to DynamoDB. Fail-soft: logs and returns on error.
 */
export async function writePointer(
  channelKey: string,
  correlationId: string,
  rootHumanId: string,
  isHumanRooted: boolean,
  ttlDays: number = 7,
): Promise<void> {
  const tableName = process.env.CORRELATION_POINTERS_TABLE;
  if (!tableName) {
    return; // Env var not set — fail-safe
  }

  try {
    const now = Math.floor(Date.now() / 1000);
    await getClient().send(new UpdateItemCommand({
      TableName: tableName,
      Key: { channel_key: { S: channelKey } },
      UpdateExpression: 'SET latest_correlation_id = :correlation, latest_root_human_id = :root, latest_is_human_rooted = :human, updated_at = :now, expires_at = :expiry',
      ExpressionAttributeValues: {
        ':correlation': { S: correlationId },
        ':root': { S: rootHumanId },
        ':human': { BOOL: isHumanRooted },
        ':now': { N: String(now) },
        ':expiry': { N: String(now + ttlDays * 86400) },
      },
    }));
  } catch (err) {
    // Fail-soft — log but don't crash
    console.warn(`[correlation] Failed to write pointer (non-fatal): ${(err as Error).message}`);
  }
}
