/**
 * SQS client helpers for the chat agent worker.
 *
 * Handles FIFO-specific bits: MessageGroupId, MessageDeduplicationId.
 *
 * Phase 2 (AG-UI): added `sendAgUiEvent()` for emitting AG-UI protocol events
 * alongside the existing `sendProgress()` / `sendResponse()` methods.
 * The response Lambda recognizes the `ag_ui_event` envelope and forwards the
 * event payload directly to the WebSocket client.
 */
import {
  SQSClient,
  ReceiveMessageCommand,
  DeleteMessageCommand,
  SendMessageCommand,
  Message,
} from '@aws-sdk/client-sqs';
import type { AgUiEvent } from './ag-ui-events';

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

/**
 * Progress frame emitted by the chat agent mid-turn. Goes to the SAME
 * response queue the final reply uses, with status="progress" so the
 * response Lambda treats it as a deliverable but doesn't close the thread.
 */
export interface ProgressMessage {
  task_id: string;
  session_id: string;
  status: 'progress';
  /** One of `tool_use` | `thinking` | `heartbeat` — see run-query ProgressEvent. */
  kind: string;
  /** Human-readable one-liner the UI can render as a status update. */
  text: string;
  turn: number;
  // Same routing echo as TaskResponse so the WebSocket router fires.
  thread_id?: string;
  connection_id?: string;
  channel?: string;
  channel_metadata?: Record<string, unknown>;
}

/**
 * AG-UI event envelope sent to the response queue. The response Lambda
 * recognizes `ag_ui_event: true` and forwards the `event` payload as-is
 * to the WebSocket, wrapped in `{ type: "ag_ui", ... }`. This lets the
 * frontend consume AG-UI events without changes to the transport layer.
 *
 * Status `"ag_ui"` tells the response Lambda to skip thread bookkeeping
 * (same as `"progress"` — AG-UI lifecycle events handle their own semantics).
 * Terminal AG-UI events (RUN_FINISHED, RUN_ERROR) are sent alongside the
 * existing `sendResponse()` call, so thread bookkeeping still happens via
 * the old path during the backward-compat window.
 */
export interface AgUiEventEnvelope {
  task_id: string;
  session_id: string;
  status: 'ag_ui';
  ag_ui_event: true;
  event: AgUiEvent;
  // Delivery routing
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
   * Send a mid-turn progress frame to the response queue. The response Lambda
   * forwards these like normal replies (same channel/connection_id routing),
   * but the frame's `status: "progress"` lets the Lambda skip end-of-turn
   * bookkeeping (thread re-enqueue, session lock clear).
   *
   * Each frame must have a UNIQUE dedup id on FIFO — same task can emit many.
   * We use `<task_id>-<turn>-<kind>` so the de-dup key is deterministic and
   * collision-proof within one turn.
   */
  async sendProgress(progress: ProgressMessage): Promise<void> {
    const isFifo = this.responseQueueUrl.endsWith('.fifo');
    await this.client.send(
      new SendMessageCommand({
        QueueUrl: this.responseQueueUrl,
        MessageBody: JSON.stringify(progress),
        ...(isFifo
          ? {
              MessageGroupId: progress.session_id,
              // Heartbeats can fire multiple times on the same turn, so
              // include a timestamp to avoid FIFO dedup collisions.
              MessageDeduplicationId: `prog_${progress.task_id}_${progress.turn}_${progress.kind}_${Date.now()}`,
            }
          : {}),
      }),
    );
  }

  /**
   * Send an AG-UI protocol event to the response queue. The response Lambda
   * detects `ag_ui_event: true` and forwards the event payload as a WS frame
   * of type `"ag_ui"`.
   *
   * These are mid-turn ephemera (like progress) — the Lambda skips thread
   * bookkeeping. Terminal state is still handled by the old `sendResponse()`.
   */
  async sendAgUiEvent(envelope: AgUiEventEnvelope): Promise<void> {
    const isFifo = this.responseQueueUrl.endsWith('.fifo');
    await this.client.send(
      new SendMessageCommand({
        QueueUrl: this.responseQueueUrl,
        MessageBody: JSON.stringify(envelope),
        ...(isFifo
          ? {
              MessageGroupId: envelope.session_id,
              // Each AG-UI event needs a unique dedup id. Use event_type + timestamp.
              MessageDeduplicationId: `agui_${envelope.task_id}_${envelope.event.event_type}_${Date.now()}`,
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
