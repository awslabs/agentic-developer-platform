/**
 * AG-UI Protocol Event Types — Frontend
 *
 * Issue #97 Phase 2: TypeScript types matching the AG-UI protocol events
 * as emitted by the worker and forwarded through the WS transport.
 *
 * These are consumed by the `useAgUiEvents` hook to update UI state.
 * We intentionally don't import `@ag-ui/core` — our own types keep the
 * bundle lean and give us full control over the wire format contract.
 */

// ---------------------------------------------------------------------------
// Event type enum (matches server-side ag-ui-events.ts)
// ---------------------------------------------------------------------------

export enum AgUiEventType {
  // Lifecycle
  RUN_STARTED = 'RUN_STARTED',
  RUN_FINISHED = 'RUN_FINISHED',
  RUN_ERROR = 'RUN_ERROR',

  // Text message streaming
  TEXT_MESSAGE_START = 'TEXT_MESSAGE_START',
  TEXT_MESSAGE_CONTENT = 'TEXT_MESSAGE_CONTENT',
  TEXT_MESSAGE_END = 'TEXT_MESSAGE_END',

  // Tool calls
  TOOL_CALL_START = 'TOOL_CALL_START',
  TOOL_CALL_ARGS = 'TOOL_CALL_ARGS',
  TOOL_CALL_END = 'TOOL_CALL_END',

  // State management
  STATE_DELTA = 'STATE_DELTA',
  STATE_SNAPSHOT = 'STATE_SNAPSHOT',

  // Steps
  STEP_STARTED = 'STEP_STARTED',
  STEP_FINISHED = 'STEP_FINISHED',

  // Custom
  CUSTOM = 'CUSTOM',
}

// ---------------------------------------------------------------------------
// Event interfaces
// ---------------------------------------------------------------------------

export interface AgUiBaseEvent {
  event_type: AgUiEventType;
  timestamp?: string;
}

export interface RunStartedEvent extends AgUiBaseEvent {
  event_type: AgUiEventType.RUN_STARTED;
  threadId: string;
  runId: string;
}

export interface RunFinishedEvent extends AgUiBaseEvent {
  event_type: AgUiEventType.RUN_FINISHED;
  threadId: string;
  runId: string;
  result?: { tokens?: { input: number; output: number }; turnCount?: number };
}

export interface RunErrorEvent extends AgUiBaseEvent {
  event_type: AgUiEventType.RUN_ERROR;
  message: string;
  code?: string;
}

export interface TextMessageStartEvent extends AgUiBaseEvent {
  event_type: AgUiEventType.TEXT_MESSAGE_START;
  messageId: string;
  role: 'assistant';
}

export interface TextMessageContentEvent extends AgUiBaseEvent {
  event_type: AgUiEventType.TEXT_MESSAGE_CONTENT;
  messageId: string;
  delta: string;
}

export interface TextMessageEndEvent extends AgUiBaseEvent {
  event_type: AgUiEventType.TEXT_MESSAGE_END;
  messageId: string;
}

export interface ToolCallStartEvent extends AgUiBaseEvent {
  event_type: AgUiEventType.TOOL_CALL_START;
  toolCallId: string;
  toolCallName: string;
  parentMessageId?: string;
}

export interface ToolCallArgsEvent extends AgUiBaseEvent {
  event_type: AgUiEventType.TOOL_CALL_ARGS;
  toolCallId: string;
  delta: string;
}

export interface ToolCallEndEvent extends AgUiBaseEvent {
  event_type: AgUiEventType.TOOL_CALL_END;
  toolCallId: string;
}

export interface StateDeltaEvent extends AgUiBaseEvent {
  event_type: AgUiEventType.STATE_DELTA;
  delta: Array<{ op: string; path: string; value?: unknown }>;
}

export interface StateSnapshotEvent extends AgUiBaseEvent {
  event_type: AgUiEventType.STATE_SNAPSHOT;
  snapshot: Record<string, unknown>;
}

export interface StepStartedEvent extends AgUiBaseEvent {
  event_type: AgUiEventType.STEP_STARTED;
  stepName: string;
}

export interface StepFinishedEvent extends AgUiBaseEvent {
  event_type: AgUiEventType.STEP_FINISHED;
  stepName: string;
}

export interface CustomAgUiEvent extends AgUiBaseEvent {
  event_type: AgUiEventType.CUSTOM;
  name: string;
  value: unknown;
}

export type AgUiEvent =
  | RunStartedEvent
  | RunFinishedEvent
  | RunErrorEvent
  | TextMessageStartEvent
  | TextMessageContentEvent
  | TextMessageEndEvent
  | ToolCallStartEvent
  | ToolCallArgsEvent
  | ToolCallEndEvent
  | StateDeltaEvent
  | StateSnapshotEvent
  | StepStartedEvent
  | StepFinishedEvent
  | CustomAgUiEvent;

// ---------------------------------------------------------------------------
// WS frame carrying an AG-UI event (type: "ag_ui")
// ---------------------------------------------------------------------------

export interface AgUiWsFrame {
  type: 'ag_ui';
  task_id: string;
  event: AgUiEvent;
  timestamp?: string;
}

// ---------------------------------------------------------------------------
// Session metadata (populated from STATE_DELTA)
// ---------------------------------------------------------------------------

export interface SessionMeta {
  tokens?: { input: number; output: number };
  turnCount?: number;
  heartbeat?: { turn: number; ts: number };
}

// ---------------------------------------------------------------------------
// Tool call tracking
// ---------------------------------------------------------------------------

export interface ToolCallInfo {
  toolCallId: string;
  toolCallName: string;
  args: string;
  status: 'running' | 'complete';
  parentMessageId?: string;
}
