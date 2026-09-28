/**
 * DynamoDB correlation pointer writer for the Node agent runtime.
 *
 * Writes pointers after successful outbound GitHub actions so that the next
 * inbound webhook on the same channel can look up the active correlation.
 *
 * Phase 2-d of EPIC #779. Intentional duplication of the worker-image Python
 * version — the Node agent runs in a separate process.
 */

import { DynamoDBClient, PutItemCommand } from '@aws-sdk/client-dynamodb';

let _client: DynamoDBClient | null = null;

function getClient(): DynamoDBClient {
  if (!_client) {
    _client = new DynamoDBClient({ region: process.env.AWS_REGION || 'us-east-1' });
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
    await getClient().send(new PutItemCommand({
      TableName: tableName,
      Item: {
        channel_key: { S: channelKey },
        latest_correlation_id: { S: correlationId },
        latest_root_human_id: { S: rootHumanId },
        latest_is_human_rooted: { BOOL: isHumanRooted },
        updated_at: { N: String(now) },
        expires_at: { N: String(now + ttlDays * 86400) },
      },
    }));
  } catch (err) {
    // Fail-soft — log but don't crash
    console.warn(`[correlation] Failed to write pointer (non-fatal): ${(err as Error).message}`);
  }
}
