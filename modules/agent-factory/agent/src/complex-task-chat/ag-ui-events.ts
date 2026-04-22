/**
 * AG-UI Protocol Event Types
 *
 * Issue #97 Phase 2: Standard event types from the AG-UI protocol
 * (https://github.com/ag-ui-protocol/ag-ui). These replace the ad-hoc
 * `status: progress|completed|failed` frame shapes from Phase 1.
 *
 * We define our own types rather than importing `@ag-ui/core` on the
 * server side to keep the worker image lean. The shapes match the
 * protocol spec — contract tests validate conformance.
 */

// ---------------------------------------------------------------------------
// Event type enum
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
// Base event shape
// ---------------------------------------------------------------------------

export interface AgUiBaseEvent {
  /** AG-UI event type discriminator. */
  event_type: AgUiEventType;
  /** ISO-8601 timestamp. */
  timestamp?: string;
  /** Raw/original event data if transformed from another format. */
  rawEvent?: unknown;
}

// ---------------------------------------------------------------------------
// Lifecycle events
// ---------------------------------------------------------------------------

export interface RunStartedEvent extends AgUiBaseEvent {
  event_type: AgUiEventType.RUN_STARTED;
  threadId: string;
  runId: string;
}

export interface RunFinishedEvent extends AgUiBaseEvent {
  event_type: AgUiEventType.RUN_FINISHED;
  threadId: string;
  runId: string;
  result?: unknown;
}

export interface RunErrorEvent extends AgUiBaseEvent {
  event_type: AgUiEventType.RUN_ERROR;
  message: string;
  code?: string;
}

// ---------------------------------------------------------------------------
// Text message events
// ---------------------------------------------------------------------------

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

// ---------------------------------------------------------------------------
// Tool call events
// ---------------------------------------------------------------------------

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

// ---------------------------------------------------------------------------
// State management events
// ---------------------------------------------------------------------------

export interface StateDeltaEvent extends AgUiBaseEvent {
  event_type: AgUiEventType.STATE_DELTA;
  /** JSON Patch operations (RFC 6902). */
  delta: Array<{ op: string; path: string; value?: unknown }>;
}

export interface StateSnapshotEvent extends AgUiBaseEvent {
  event_type: AgUiEventType.STATE_SNAPSHOT;
  snapshot: Record<string, unknown>;
}

// ---------------------------------------------------------------------------
// Step events
// ---------------------------------------------------------------------------

export interface StepStartedEvent extends AgUiBaseEvent {
  event_type: AgUiEventType.STEP_STARTED;
  stepName: string;
}

export interface StepFinishedEvent extends AgUiBaseEvent {
  event_type: AgUiEventType.STEP_FINISHED;
  stepName: string;
}

// ---------------------------------------------------------------------------
// Custom event
// ---------------------------------------------------------------------------

export interface CustomEvent extends AgUiBaseEvent {
  event_type: AgUiEventType.CUSTOM;
  name: string;
  value: unknown;
}

// ---------------------------------------------------------------------------
// Union type
// ---------------------------------------------------------------------------

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
  | CustomEvent;

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Generate an ISO timestamp for events. */
export function agUiTimestamp(): string {
  return new Date().toISOString();
}

/** Generate a unique message/call ID. */
export function agUiId(prefix = 'msg'): string {
  return `${prefix}_${Date.now()}_${Math.random().toString(36).slice(2, 7)}`;
}
