/**
 * Chat types for the Agent Chat widget (Phase 1 — L1 raw WS frames).
 *
 * Issue #97: These types map to the ad-hoc frame shapes emitted by the
 * agent-gateway pipeline (ingest → SQS → worker → response Lambda → WS).
 * Phase 2 will replace these with AG-UI event types.
 */

// ---------------------------------------------------------------------------
// WebSocket frame types (server → client)
// ---------------------------------------------------------------------------

/** Base shape shared by every inbound WS frame. */
export interface WsFrameBase {
  type: 'notification' | 'progress' | 'response';
  task_id: string;
  session_id: string;
  timestamp?: string;
}

/** Acknowledgement that the task was received by the pipeline. */
export interface WsNotificationFrame extends WsFrameBase {
  type: 'notification';
  message: string;
}

/** Progress updates during long-running tasks.
 *  The response Lambda's WS router only forwards `kind`, `turn`, and `status`
 *  today. Tool-name/input aren't on the wire yet — a future change in the
 *  response path would add them. Until then any `tool_use` progress renders
 *  as a generic typing indicator. */
export interface WsProgressFrame extends WsFrameBase {
  type: 'progress';
  kind: 'heartbeat' | 'tool_use';
  turn?: number;
  status?: string;
}

/** Response frame — may be chunked for large payloads. */
export interface WsResponseFrame extends WsFrameBase {
  type: 'response';
  status: 'progress' | 'completed' | 'failed';
  text?: string;
  result?: string;
  content?: string;
  reason?: string;
  /** Chunking metadata (PR #85). */
  chunk_index?: number;
  chunk_total?: number;
}

export type WsFrame = WsNotificationFrame | WsProgressFrame | WsResponseFrame;

// ---------------------------------------------------------------------------
// Client → server action
// ---------------------------------------------------------------------------

export interface WsSendAction {
  action: 'sendMessage';
  /** Ingest Lambda reads `text`, not `message`. Don't rename. */
  text: string;
  session_id: string;
}

// ---------------------------------------------------------------------------
// UI-level message model (stored in localStorage)
// ---------------------------------------------------------------------------

export type MessageRole = 'user' | 'assistant' | 'system';

export type MessageStatus = 'sending' | 'streaming' | 'complete' | 'error';

export interface ToolUseInfo {
  tool_name: string;
  tool_input?: string;
}

export interface ChatMessage {
  id: string;
  role: MessageRole;
  content: string;
  status: MessageStatus;
  timestamp: number;
  /** Populated for assistant messages during long-running tasks. */
  taskId?: string;
  /** Reason string when status === 'error'. */
  errorReason?: string;
  /** Active tool use shown below the pending bubble. */
  toolUse?: ToolUseInfo | null;
}

// ---------------------------------------------------------------------------
// Conversation thread (persisted in localStorage)
// ---------------------------------------------------------------------------

export interface Conversation {
  id: string; // same as session_id
  title: string;
  createdAt: number;
  updatedAt: number;
  messages: ChatMessage[];
}

// ---------------------------------------------------------------------------
// Hook state
// ---------------------------------------------------------------------------

export type ConnectionStatus = 'disconnected' | 'connecting' | 'connected' | 'reconnecting';

export interface AgentChatState {
  connectionStatus: ConnectionStatus;
  isAwaitingReply: boolean;
  reconnectAttempt: number;
}
