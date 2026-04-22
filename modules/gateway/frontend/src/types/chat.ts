/**
 * Chat types for the Agent Chat widget.
 *
 * Issue #97:
 * - Phase 1 (L1): ad-hoc frame shapes (notification/progress/response).
 * - Phase 2 (L2): AG-UI event protocol. The hook consumes both formats
 *   during the backward-compat window (type: "ag_ui" and legacy frames).
 */

import type { AgUiWsFrame, SessionMeta, ToolCallInfo } from './ag-ui-events';

// ---------------------------------------------------------------------------
// WebSocket frame types (server → client) — legacy + AG-UI
// ---------------------------------------------------------------------------

/** Base shape shared by every inbound WS frame. */
export interface WsFrameBase {
  type: 'notification' | 'progress' | 'response' | 'ag_ui';
  task_id: string;
  session_id?: string;
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

export type WsFrame = WsNotificationFrame | WsProgressFrame | WsResponseFrame | AgUiWsFrame;

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
  /** AG-UI messageId for correlating TEXT_MESSAGE events. */
  agUiMessageId?: string;
  /** Reason string when status === 'error'. */
  errorReason?: string;
  /** Active tool use shown below the pending bubble (legacy). */
  toolUse?: ToolUseInfo | null;
  /** AG-UI tool calls associated with this message. */
  toolCalls?: ToolCallInfo[];
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
  /** AG-UI session metadata (tokens, turn count, heartbeat). */
  sessionMeta?: SessionMeta;
}
