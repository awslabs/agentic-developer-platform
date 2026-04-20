/**
 * SQS client helpers for the chat agent worker.
 *
 * Handles FIFO-specific bits: MessageGroupId, MessageDeduplicationId.
 */
import {
  SQSClient,
  ReceiveMessageCommand,
  DeleteMessageCommand,
  SendMessageCommand,
  Message,
} from '@aws-sdk/client-sqs';

export interface TaskPayload {
  task_id: string;
  session_id: string;
  message: string;
  agent_type?: string;
  user_id: string;
  tenant_id?: string;
  user_email?: string;
  source?: string;
  component?: string;
  attachments?: string[];
  // Delivery-routing fields supplied by the ingest Lambda. We echo these
  // back in TaskResponse so the response Lambda can post to the right
  // channel. Missing any of these → response Lambda falls back to REST
  // polling, which silently drops WS deliveries.
  thread_id?: string;
  connection_id?: string;
  channel?: string;
  platform_data?: Record<string, unknown>;
}

export interface TaskResponse {
  task_id: string;
  session_id: string;
  text: string;
  tokens?: { input: number; output: number };
  status: 'completed' | 'failed';
  /** Artifact references produced this turn (empty on error or no publishes). */
  artifacts?: Array<{
    id: string;
    url: string;
    urlExpiresAt: string;
    filename: string;
    contentType: string;
    sizeBytes: number;
  }>;
  // Delivery routing — echoed from TaskPayload. The response Lambda reads
  // `channel` (webchat|websocket|slack|cli|rest|poll) to pick a router, and
  // `connection_id` to post to a specific WebSocket connection.
  thread_id?: string;
  connection_id?: string;
  channel?: string;
  channel_metadata?: Record<string, unknown>;
}

export class SqsClient {
  private readonly client: SQSClient;
  private readonly inputQueueUrl: string;
  private readonly responseQueueUrl: string;

  constructor(env: Record<string, string | undefined> = process.env) {
    const region = env.AWS_REGION ?? 'us-east-1';
    this.client = new SQSClient({ region });
    this.inputQueueUrl = env.INPUT_QUEUE_URL ?? '';
    this.responseQueueUrl = env.RESPONSE_QUEUE_URL ?? '';

    if (!this.inputQueueUrl) {
      throw new Error('INPUT_QUEUE_URL env var is required');
    }
    if (!this.responseQueueUrl) {
      throw new Error('RESPONSE_QUEUE_URL env var is required');
    }
  }

  /**
   * Receive messages from the input FIFO queue.
   * KEDA ScaledJob typically processes one message per pod.
   */
  async receive(maxMessages: number = 1): Promise<Message[]> {
    const result = await this.client.send(
      new ReceiveMessageCommand({
        QueueUrl: this.inputQueueUrl,
        MaxNumberOfMessages: maxMessages,
        WaitTimeSeconds: 20,
        VisibilityTimeout: 900, // 15 min — matches activeDeadlineSeconds
      }),
    );
    return result.Messages ?? [];
  }

  /**
   * Send a response to the response queue.
   * For FIFO queues, includes MessageGroupId and MessageDeduplicationId.
   */
  async sendResponse(response: TaskResponse): Promise<void> {
    const isFifo = this.responseQueueUrl.endsWith('.fifo');

    await this.client.send(
      new SendMessageCommand({
        QueueUrl: this.responseQueueUrl,
        MessageBody: JSON.stringify(response),
        ...(isFifo
          ? {
              MessageGroupId: response.session_id,
              MessageDeduplicationId: `resp_${response.task_id}`,
            }
          : {}),
      }),
    );
  }

  /**
   * Delete a message after successful processing.
   */
  async deleteMessage(receiptHandle: string): Promise<void> {
    await this.client.send(
      new DeleteMessageCommand({
        QueueUrl: this.inputQueueUrl,
        ReceiptHandle: receiptHandle,
      }),
    );
  }
}
