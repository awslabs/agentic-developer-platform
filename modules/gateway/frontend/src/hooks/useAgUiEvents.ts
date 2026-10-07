/**
 * useAgUiEvents — AG-UI protocol event consumer for the Agent Chat widget.
 *
 * Issue #97 Phase 2: Replaces the raw WS frame parser (`useAgentChat`) with
 * an AG-UI event dispatcher. During the backward-compat window the hook
 * handles BOTH legacy frames (type: notification/progress/response) and
 * AG-UI frames (type: ag_ui) so the UI works regardless of which format
 * the worker is emitting.
 *
 * The hook's public API is identical to `useAgentChat` — the page component
 * doesn't need to change except for the import.
 */

import { deploymentSetting } from '@/config/runtime';

import { useCallback, useEffect, useRef, useState } from 'react';
import { getIdToken, isTokenExpired, refreshToken as refreshTokenService } from '@/services/auth';
import { apiClient } from '@/services/api';
import { readChatReplay } from '@/services/chatReplay';
import type {
  AgentChatState,
  ChatMessage,
  ConnectionStatus,
  Conversation,
  WsFrame,
  WsProgressFrame,
  WsResponseFrame,
} from '@/types/chat';
import {
  AgUiEventType,
  type AgUiEvent,
  type AgUiWsFrame,
  type SessionMeta,
  type ToolCallInfo,
} from '@/types/ag-ui-events';
import { applyPatches } from '@/utils/jsonPatch';
import { terminalChatResponse } from './terminalChatResponse';

// ---------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------

const CHAT_UNCONFIGURED = 'Agent chat is not configured for this deployment.';

const MAX_RECONNECT_ATTEMPTS = 10;
const INITIAL_BACKOFF_MS = 1_000;
const MAX_BACKOFF_MS = 30_000;

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function generateId(): string {
  return `${Date.now()}-${Math.random().toString(36).slice(2, 9)}`;
}

/** Extract text content from a legacy response frame. */
function extractContent(frame: WsResponseFrame): string {
  return frame.text || frame.result || frame.content || '';
}

function backoffMs(attempt: number): number {
  return Math.min(INITIAL_BACKOFF_MS * 2 ** attempt, MAX_BACKOFF_MS);
}

/** ES2023 findLastIndex polyfill. */
function findLastIndex<T>(arr: T[], predicate: (item: T) => boolean): number {
  for (let i = arr.length - 1; i >= 0; i--) {
    if (predicate(arr[i])) return i;
  }
  return -1;
}

function cursorParts(cursor: string | null | undefined): [string, number] | null {
  const match = /^([a-f0-9]{32}):([0-9]{1,8})$/.exec(cursor ?? '');
  return match ? [match[1], Number(match[2])] : null;
}

// ---------------------------------------------------------------------------
// Hook types
// ---------------------------------------------------------------------------

export interface UseAgUiEventsOptions {
  /** Active conversation (controls WS lifecycle). */
  conversation: Conversation | null;
  /** Called when messages change so the caller can persist to localStorage. */
  onMessagesChange: (sessionId: string, messages: ChatMessage[], cursor?: string) => void;
}

export interface UseAgUiEventsReturn extends AgentChatState {
  /**
   * True when the server refused this conversation's identifier (#5615).
   *
   * The ordinary cause is benign and expected: the sessions table expires rows
   * after 24h, and this browser keeps the identifier in localStorage
   * indefinitely, so an owner returning the next day names a conversation the
   * server no longer has. The refusal is deliberately the same non-answer given
   * for somebody else's conversation, so this flag must not be read as "it was
   * mine and it expired" — only as "this identifier can no longer be used".
   *
   * Before this existed, the refusal was the Lambda's return value only, which
   * API Gateway discards for WebSocket routes: the browser was told nothing and
   * retried the same dead identifier forever, showing a permanent spinner.
   */
  sessionExpired: boolean;
  replayWarning: string | null;
  /**
   * Send a user message. `persona` (#4208) pins the agent persona for the turn,
   * bypassing the server-side classifier — used by the intent-intake flow. The
   * ingest Lambda validates it against an allowlist and rejects anything else,
   * so an unrecognised value fails the send rather than silently downgrading.
   */
  sendMessage: (text: string, attachments?: string[], persona?: string) => void;
  cancelTurn: () => Promise<void>;
  canCancel: boolean;
  cancelState: 'idle' | 'pending' | 'requested' | 'failed';
  /** Active tool calls for the current turn. */
  activeToolCalls: ToolCallInfo[];
  /** WebSocket ref exposed for upload-token/upload-complete actions. Stage C (#186). */
  wsRef: React.RefObject<WebSocket | null>;
}

// ---------------------------------------------------------------------------
// Hook implementation
// ---------------------------------------------------------------------------

export function useAgUiEvents({
  conversation,
  onMessagesChange,
}: UseAgUiEventsOptions): UseAgUiEventsReturn {
  const [connectionStatus, setConnectionStatus] = useState<ConnectionStatus>('disconnected');
  const [isAwaitingReply, setIsAwaitingReply] = useState(false);
  const [reconnectAttempt, setReconnectAttempt] = useState(0);
  const [sessionExpired, setSessionExpired] = useState(false);
  const [replayWarning, setReplayWarning] = useState<string | null>(null);
  const [sessionMeta, setSessionMeta] = useState<SessionMeta | undefined>();
  const [activeToolCalls, setActiveToolCalls] = useState<ToolCallInfo[]>([]);
  const [activeTaskId, setActiveTaskId] = useState<string | null>(null);
  const [cancelState, setCancelState] = useState<UseAgUiEventsReturn['cancelState']>('idle');
  const activeTaskRef = useRef<string | null>(null);
  const cancelRequestRef = useRef<string | null>(null);

  // Refs to avoid stale closures inside WS callbacks.
  const wsRef = useRef<WebSocket | null>(null);
  const messagesRef = useRef<ChatMessage[]>([]);
  const sessionIdRef = useRef<string | null>(null);
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const chunkBufferRef = useRef<Map<string, string[]>>(new Map());
  const intentionalCloseRef = useRef(false);
  const reconnectAttemptRef = useRef(0);
  const replayCursorRef = useRef<string | null>(null);
  const replayRunningRef = useRef(false);
  const replayEpochRef = useRef(0);
  const replayRetryRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const resetSessionRef = useRef<string | null>(null);
  const replayBufferRef = useRef<WsFrame[]>([]);
  const replayApplyingRef = useRef(false);
  const toolCallsRef = useRef<Map<string, ToolCallInfo>>(new Map());
  // Buffer for TOOL_CALL_END events that arrive BEFORE their TOOL_CALL_START.
  // Observed in production: response Lambda + API Gateway can deliver AG-UI
  // frames out of order because they travel as separate SQS messages +
  // separate Lambda invocations. If END arrives first it was being silently
  // discarded (no matching entry in toolCallsRef), leaving the tool-call
  // chip stuck in "running" forever. On START we now check this buffer and
  // immediately mark complete if the END already arrived.
  const endedBeforeStartRef = useRef<Set<string>>(new Set());
  // Task IDs whose assistant message already arrived via AG-UI RUN_FINISHED.
  // During the Phase-1↔Phase-2 backward-compat window, the worker emits BOTH
  // the AG-UI event sequence AND a legacy `type:"response"` terminal frame
  // for the same task (see modules/agent-factory/agent/src/complex-task-chat/
  // complex-task-chat-agent.ts — the legacy sendResponse is labelled
  // "Legacy terminal response"). Without dedup the UI renders two identical
  // assistant bubbles. Bounded-size set so it doesn't grow unbounded during
  // a long session.
  const aguiCompletedTaskIdsRef = useRef<Set<string>>(new Set());
  const streamSequencesRef = useRef<Map<string, number>>(new Map());

  // Keep refs in sync with conversation prop.
  useEffect(() => {
    if (conversation) {
      messagesRef.current = conversation.messages;
      sessionIdRef.current = conversation.id;
    }
  }, [conversation]);

  // A refusal belongs to the identifier that was refused, so switching or
  // starting a conversation clears it (#5615). Keyed on the id rather than the
  // object so an unrelated message update does not re-enable a dead send box.
  useEffect(() => {
    if (resetSessionRef.current === (conversation?.id ?? null)) return;
    resetSessionRef.current = conversation?.id ?? null;
    setSessionExpired(false);
    const lastMessage = conversation?.messages[conversation.messages.length - 1];
    setIsAwaitingReply(lastMessage?.role === 'user' || lastMessage?.status === 'streaming');
    replayCursorRef.current = cursorParts(conversation?.replayCursor) ? conversation!.replayCursor! : null;
    replayBufferRef.current = [];
    replayRunningRef.current = false;
    setReplayWarning(null);
    activeTaskRef.current = null;
    cancelRequestRef.current = null;
    setActiveTaskId(null);
    setCancelState('idle');
  }, [conversation]);

  const trackActiveTask = useCallback((taskId?: string) => {
    if (taskId && taskId !== activeTaskRef.current) {
      activeTaskRef.current = taskId;
      setActiveTaskId(taskId);
      setCancelState('idle');
      cancelRequestRef.current = null;
    }
  }, []);

  const cancelTurn = useCallback(async () => {
    const sessionId = sessionIdRef.current;
    const taskId = activeTaskRef.current;
    if (!sessionId || !taskId || !isAwaitingReply || sessionExpired || cancelRequestRef.current || cancelState === 'requested') return;
    const requestKey = `${sessionId}/${taskId}`;
    cancelRequestRef.current = requestKey;
    setCancelState('pending');
    try {
      const result = await apiClient.post<{ status: string; session_id: string; task_id: string }>('/v1/chat/turns/cancel', {
        session_id: sessionId, task_id: taskId,
      });
      if (result.status !== 'cancellation_requested' || result.session_id !== sessionId || result.task_id !== taskId) {
        throw new Error('Cancellation receipt mismatch');
      }
      if (sessionIdRef.current === sessionId && activeTaskRef.current === taskId) setCancelState('requested');
    } catch {
      if (sessionIdRef.current === sessionId && activeTaskRef.current === taskId) setCancelState('failed');
    } finally {
      if (cancelRequestRef.current === requestKey) cancelRequestRef.current = null;
    }
  }, [isAwaitingReply, sessionExpired, cancelState]);

  // ------------------------------------------------------------------
  // Message mutation helper
  // ------------------------------------------------------------------

  const updateMessages = useCallback(
    (updater: (prev: ChatMessage[]) => ChatMessage[]) => {
      const next = updater(messagesRef.current);
      messagesRef.current = next;
      if (sessionIdRef.current && !replayApplyingRef.current) {
        onMessagesChange(sessionIdRef.current, next);
      }
    },
    [onMessagesChange],
  );

  // ------------------------------------------------------------------
  // AG-UI event handlers
  // ------------------------------------------------------------------

  const handleRunStarted = useCallback((event: AgUiEvent & { event_type: typeof AgUiEventType.RUN_STARTED }) => {
    trackActiveTask(event.runId);
    setIsAwaitingReply(true);
    // Reset tool calls for new run
    toolCallsRef.current.clear();
    endedBeforeStartRef.current.clear();
    setActiveToolCalls([]);
  }, [trackActiveTask]);

  const handleRunFinished = useCallback(
    (event: AgUiEvent & { event_type: typeof AgUiEventType.RUN_FINISHED }) => {
      setIsAwaitingReply(false);
      toolCallsRef.current.clear();
      endedBeforeStartRef.current.clear();
      setActiveToolCalls([]);

      // Mark this task_id as AG-UI-completed so the legacy `type:"response"`
      // frame that the worker emits ~40ms later for the same task is ignored
      // in handleLegacyResponse (prevents duplicate assistant bubbles).
      // runId on RUN_FINISHED is the task_id.
      if (event.runId) {
        aguiCompletedTaskIdsRef.current.add(event.runId);
        // Soft cap — a single active chat produces one id per turn; 256 is
        // more than any realistic session will touch in a browser session.
        if (aguiCompletedTaskIdsRef.current.size > 256) {
          const first = aguiCompletedTaskIdsRef.current.values().next().value;
          if (first) aguiCompletedTaskIdsRef.current.delete(first);
        }
      }

      // Update session meta with final result
      if (event.result) {
        setSessionMeta(prev => ({
          ...prev,
          tokens: event.result?.tokens ?? prev?.tokens,
          turnCount: event.result?.turnCount ?? prev?.turnCount,
        }));
      }

      // Finalize any streaming assistant bubble
      updateMessages((msgs) => {
        const idx = findLastIndex(msgs, (m) => m.role === 'assistant' && m.status === 'streaming');
        if (idx === -1) return msgs;
        const updated = [...msgs];
        updated[idx] = { ...updated[idx], status: 'complete' };
        return updated;
      });
    },
    [updateMessages],
  );

  const handleRunError = useCallback(
    (event: AgUiEvent & { event_type: typeof AgUiEventType.RUN_ERROR }) => {
      setIsAwaitingReply(false);
      toolCallsRef.current.clear();
      endedBeforeStartRef.current.clear();
      setActiveToolCalls([]);

      updateMessages((msgs) => {
        // Check if there's a streaming bubble to convert to error
        const idx = findLastIndex(msgs, (m) => m.role === 'assistant' && m.status === 'streaming');
        if (idx !== -1) {
          const updated = [...msgs];
          updated[idx] = {
            ...updated[idx],
            status: 'error',
            errorReason: event.message,
            content: updated[idx].content || event.message,
          };
          return updated;
        }
        // No streaming bubble — create an error message
        return [
          ...msgs,
          {
            id: generateId(),
            role: 'assistant' as const,
            content: event.message,
            status: 'error' as const,
            timestamp: Date.now(),
            errorReason: event.message,
          },
        ];
      });
    },
    [updateMessages],
  );

  const handleTextMessageStart = useCallback(
    (event: AgUiEvent & { event_type: typeof AgUiEventType.TEXT_MESSAGE_START }) => {
      // Create or find the streaming assistant bubble
      updateMessages((msgs) => {
        const idx = findLastIndex(msgs, (m) => m.role === 'assistant' && m.status === 'streaming');
        if (idx !== -1) {
          // Already have a streaming bubble — update its AG-UI messageId
          const updated = [...msgs];
          updated[idx] = { ...updated[idx], agUiMessageId: event.messageId };
          return updated;
        }
        return [
          ...msgs,
          {
            id: generateId(),
            role: 'assistant' as const,
            content: '',
            status: 'streaming' as const,
            timestamp: Date.now(),
            agUiMessageId: event.messageId,
          },
        ];
      });
    },
    [updateMessages],
  );

  const handleTextMessageContent = useCallback(
    (event: AgUiEvent & { event_type: typeof AgUiEventType.TEXT_MESSAGE_CONTENT }) => {
      updateMessages((msgs) => {
        // Find the message by AG-UI messageId, or fall back to last streaming bubble
        let idx = findLastIndex(msgs, (m) => m.agUiMessageId === event.messageId);
        if (idx === -1) {
          idx = findLastIndex(msgs, (m) => m.role === 'assistant' && m.status === 'streaming');
        }
        if (idx === -1) {
          // No bubble yet — create one
          return [
            ...msgs,
            {
              id: generateId(),
              role: 'assistant' as const,
              content: event.delta,
              status: 'streaming' as const,
              timestamp: Date.now(),
              agUiMessageId: event.messageId,
            },
          ];
        }
        const updated = [...msgs];
        updated[idx] = {
          ...updated[idx],
          content: updated[idx].content + event.delta,
          timestamp: Date.now(),
        };
        return updated;
      });
    },
    [updateMessages],
  );

  const handleTextMessageEnd = useCallback(
    (event: AgUiEvent & { event_type: typeof AgUiEventType.TEXT_MESSAGE_END }) => {
      updateMessages((msgs) => {
        let idx = findLastIndex(msgs, (m) => m.agUiMessageId === event.messageId);
        if (idx === -1) {
          idx = findLastIndex(msgs, (m) => m.role === 'assistant' && m.status === 'streaming');
        }
        if (idx === -1) return msgs;
        const updated = [...msgs];
        // Don't mark complete yet — RUN_FINISHED does that. Just mark the text as done.
        updated[idx] = { ...updated[idx], timestamp: Date.now() };
        return updated;
      });
    },
    [updateMessages],
  );

  const handleToolCallStart = useCallback(
    (event: AgUiEvent & { event_type: typeof AgUiEventType.TOOL_CALL_START }) => {
      // If END already arrived for this id (out-of-order delivery), start
      // in 'complete' state so the UI never shows a stuck "running" chip.
      const alreadyEnded = endedBeforeStartRef.current.delete(event.toolCallId);
      const toolCall: ToolCallInfo = {
        toolCallId: event.toolCallId,
        toolCallName: event.toolCallName,
        args: '',
        status: alreadyEnded ? 'complete' : 'running',
        parentMessageId: event.parentMessageId,
      };
      toolCallsRef.current.set(event.toolCallId, toolCall);
      setActiveToolCalls([...toolCallsRef.current.values()]);

      // Also update the streaming message's toolCalls array
      updateMessages((msgs) => {
        const idx = findLastIndex(msgs, (m) => m.role === 'assistant' && m.status === 'streaming');
        if (idx === -1) return msgs;
        const updated = [...msgs];
        const currentCalls = updated[idx].toolCalls ?? [];
        updated[idx] = {
          ...updated[idx],
          toolCalls: [...currentCalls, toolCall],
          // Legacy compat: also set toolUse for existing renderer
          toolUse: { tool_name: event.toolCallName, tool_input: '' },
          timestamp: Date.now(),
        };
        return updated;
      });
    },
    [updateMessages],
  );

  const handleToolCallArgs = useCallback(
    (event: AgUiEvent & { event_type: typeof AgUiEventType.TOOL_CALL_ARGS }) => {
      const tc = toolCallsRef.current.get(event.toolCallId);
      if (tc) {
        tc.args += event.delta;
        setActiveToolCalls([...toolCallsRef.current.values()]);
      }
    },
    [],
  );

  const handleToolCallEnd = useCallback(
    (event: AgUiEvent & { event_type: typeof AgUiEventType.TOOL_CALL_END }) => {
      const tc = toolCallsRef.current.get(event.toolCallId);
      if (tc) {
        tc.status = 'complete';
        setActiveToolCalls([...toolCallsRef.current.values()]);
      } else {
        // END arrived before START (out-of-order WS delivery). Remember the
        // id so the matching START creates the entry already-complete.
        endedBeforeStartRef.current.add(event.toolCallId);
      }

      // Update the message's tool call status
      updateMessages((msgs) => {
        const idx = findLastIndex(msgs, (m) => m.role === 'assistant' && m.status === 'streaming');
        if (idx === -1) return msgs;
        const updated = [...msgs];
        const calls = (updated[idx].toolCalls ?? []).map(c =>
          c.toolCallId === event.toolCallId ? { ...c, status: 'complete' as const } : c,
        );
        updated[idx] = {
          ...updated[idx],
          toolCalls: calls,
          // Clear legacy toolUse when tool completes
          toolUse: null,
          timestamp: Date.now(),
        };
        return updated;
      });
    },
    [updateMessages],
  );

  const handleStateDelta = useCallback(
    (event: AgUiEvent & { event_type: typeof AgUiEventType.STATE_DELTA }) => {
      // Apply JSON Patch operations to session meta.
      //
      // Issue #4208: pointers are resolved properly (nested paths included)
      // instead of being flattened to a single top-level key. The old
      // `path.replace(/^\//,'')` turned `/draft/intent` into a literal key
      // named "draft/intent", so nested patches were silently dropped and the
      // panel just never updated.
      setSessionMeta(
        prev =>
          applyPatches(
            { ...(prev ?? {}) } as Record<string, unknown>,
            event.delta,
          ) as SessionMeta,
      );

      // If it's a heartbeat, keep the typing indicator alive
      const isHeartbeat = event.delta.some(op => op.path === '/heartbeat');
      if (isHeartbeat) {
        updateMessages((msgs) => {
          const idx = findLastIndex(msgs, (m) => m.role === 'assistant' && m.status === 'streaming');
          if (idx === -1) {
            // Create a streaming bubble so the indicator shows
            return [
              ...msgs,
              {
                id: generateId(),
                role: 'assistant' as const,
                content: '',
                status: 'streaming' as const,
                timestamp: Date.now(),
              },
            ];
          }
          const updated = [...msgs];
          updated[idx] = { ...updated[idx], timestamp: Date.now() };
          return updated;
        });
      }
    },
    [updateMessages],
  );

  // ------------------------------------------------------------------
  // AG-UI event dispatcher
  // ------------------------------------------------------------------

  const dispatchAgUiEvent = useCallback(
    (event: AgUiEvent) => {
      if (event.stream_id !== undefined || event.stream_sequence !== undefined) {
        const streamId = event.stream_id;
        const sequence = event.stream_sequence;
        if (
          typeof streamId !== 'string' || !/^[a-f0-9]{64}$/.test(streamId) ||
          typeof sequence !== 'number' || !Number.isSafeInteger(sequence) ||
          sequence < 0 || sequence >= 16_384
        ) return;
        const sequences = streamSequencesRef.current;
        if (sequence <= (sequences.get(streamId) ?? -1)) return;
        sequences.set(streamId, sequence);
        if (sequences.size > 128) sequences.delete(sequences.keys().next().value!);
      }
      switch (event.event_type) {
        case AgUiEventType.RUN_STARTED:
          handleRunStarted(event);
          break;
        case AgUiEventType.RUN_FINISHED:
          handleRunFinished(event);
          break;
        case AgUiEventType.RUN_ERROR:
          handleRunError(event);
          break;
        case AgUiEventType.TEXT_MESSAGE_START:
          handleTextMessageStart(event);
          break;
        case AgUiEventType.TEXT_MESSAGE_CONTENT:
          handleTextMessageContent(event);
          break;
        case AgUiEventType.TEXT_MESSAGE_END:
          handleTextMessageEnd(event);
          break;
        case AgUiEventType.TOOL_CALL_START:
          handleToolCallStart(event);
          break;
        case AgUiEventType.TOOL_CALL_ARGS:
          handleToolCallArgs(event);
          break;
        case AgUiEventType.TOOL_CALL_END:
          handleToolCallEnd(event);
          break;
        case AgUiEventType.STATE_DELTA:
          handleStateDelta(event);
          break;
        // STATE_SNAPSHOT, STEP_*, CUSTOM — handled but no-op for now
        default:
          break;
      }
    },
    [
      handleRunStarted,
      handleRunFinished,
      handleRunError,
      handleTextMessageStart,
      handleTextMessageContent,
      handleTextMessageEnd,
      handleToolCallStart,
      handleToolCallArgs,
      handleToolCallEnd,
      handleStateDelta,
    ],
  );

  // ------------------------------------------------------------------
  // Legacy frame handlers (backward compat during transition)
  // ------------------------------------------------------------------

  const handleLegacyNotification = useCallback(
    (frame: WsFrame) => {
      trackActiveTask(frame.task_id);
      updateMessages((msgs) => [
        ...msgs,
        {
          id: generateId(),
          role: 'system' as const,
          content: (frame as { message?: string }).message || 'Task received.',
          status: 'complete' as const,
          timestamp: Date.now(),
        },
      ]);
    },
    [updateMessages, trackActiveTask],
  );

  const handleLegacyProgress = useCallback(
    (frame: WsProgressFrame) => {
      // Legacy-format progress: both heartbeat and tool_use kinds just keep
      // the typing indicator alive. Tool name/input aren't on the legacy
      // wire (see response/routers/websocket.py). For rich tool rendering,
      // the worker emits AG-UI TOOL_CALL_* events — handled elsewhere.
      updateMessages((msgs) => {
        const idx = findLastIndex(msgs, (m) => m.role === 'assistant' && m.status === 'streaming');
        if (idx === -1) {
          return [
            ...msgs,
            {
              id: generateId(),
              role: 'assistant' as const,
              content: '',
              status: 'streaming' as const,
              timestamp: Date.now(),
              taskId: frame.task_id,
            },
          ];
        }
        const updated = [...msgs];
        updated[idx] = { ...updated[idx], timestamp: Date.now() };
        return updated;
      });
    },
    [updateMessages],
  );

  const handleLegacyResponse = useCallback(
    (frame: WsResponseFrame) => {
      if (frame.terminal_delivery !== undefined) {
        const messages = terminalChatResponse(frame, sessionIdRef.current, messagesRef.current, chunkBufferRef.current, activeTaskRef.current);
        if (messages) {
          updateMessages(() => messages);
          if (!messages.some(message => message.role === 'assistant' && message.status === 'streaming') &&
              (!activeTaskRef.current || activeTaskRef.current === frame.task_id)) {
            setIsAwaitingReply(false);
            toolCallsRef.current.clear();
            endedBeforeStartRef.current.clear();
            setActiveToolCalls([]);
            activeTaskRef.current = null;
            setActiveTaskId(null);
            setCancelState('idle');
          }
        }
        return;
      }
      // Dedup guard: if this task_id already finished via AG-UI RUN_FINISHED,
      // the worker is sending a legacy terminal frame for backward-compat —
      // ignoring it here prevents the duplicate assistant bubble. We still
      // process failure frames (those carry distinct error context the AG-UI
      // path may not have surfaced) — only drop successful completions.
      if (
        frame.task_id &&
        aguiCompletedTaskIdsRef.current.has(frame.task_id) &&
        frame.status !== 'failed'
      ) {
        return;
      }

      const content = extractContent(frame);

      // Ingest Lambda sends an immediate ACK for long_running / github_actions
      // paths as a `type:"response"` frame with `status:"notification"` and
      // the classifier's `escalation_note` as content (e.g. "On it — let me
      // check..."). The old code only matched status='completed'|'failed',
      // so these ACKs were silently dropped and the user stared at nothing
      // until the real reply arrived 15-30s later.
      //
      // Render notifications as a plain assistant bubble so the user sees
      // the acknowledgement. Do NOT clear isAwaitingReply — the typing
      // indicator should stay on until the final reply lands.
      if (frame.status === 'notification') {
        trackActiveTask(frame.task_id);
        if (content) {
          updateMessages((msgs) => [
            ...msgs,
            {
              id: generateId(),
              role: 'assistant' as const,
              content,
              status: 'complete' as const,
              timestamp: Date.now(),
              taskId: frame.task_id,
            },
          ]);
        }
        return;
      }

      if (frame.status === 'failed') {
        setIsAwaitingReply(false);
        updateMessages((msgs) => {
          const idx = findLastIndex(msgs, (m) => m.role === 'assistant' && m.status === 'streaming');
          if (idx !== -1) {
            const updated = [...msgs];
            updated[idx] = {
              ...updated[idx],
              status: 'error',
              content: content || updated[idx].content,
              errorReason: frame.reason || content,
            };
            return updated;
          }
          return [
            ...msgs,
            {
              id: generateId(),
              role: 'assistant' as const,
              content: content || 'An error occurred.',
              status: 'error' as const,
              timestamp: Date.now(),
              errorReason: frame.reason || content,
            },
          ];
        });
        return;
      }

      // Chunked response
      if (frame.chunk_total && frame.chunk_total > 1 && frame.chunk_index) {
        const buf = chunkBufferRef.current;
        const key = frame.task_id;
        if (!buf.has(key)) {
          buf.set(key, new Array(frame.chunk_total));
        }
        const chunks = buf.get(key)!;
        chunks[frame.chunk_index - 1] = content;

        // Check completeness
        const received = chunks.filter(Boolean).length;
        if (received < frame.chunk_total) {
          // Partial — show what we have so far
          const partial = chunks.filter(Boolean).join('');
          updateMessages((msgs) => {
            const idx = findLastIndex(msgs, (m) => m.role === 'assistant' && m.status === 'streaming');
            if (idx === -1) {
              return [
                ...msgs,
                {
                  id: generateId(),
                  role: 'assistant' as const,
                  content: partial,
                  status: 'streaming' as const,
                  timestamp: Date.now(),
                  taskId: frame.task_id,
                },
              ];
            }
            const updated = [...msgs];
            updated[idx] = { ...updated[idx], content: partial, timestamp: Date.now() };
            return updated;
          });
          return;
        }

        // All chunks received
        const fullContent = chunks.join('');
        buf.delete(key);

        if (frame.status === 'completed') {
          setIsAwaitingReply(false);
          updateMessages((msgs) => {
            const idx = findLastIndex(msgs, (m) => m.role === 'assistant' && m.status === 'streaming');
            if (idx !== -1) {
              const updated = [...msgs];
              updated[idx] = { ...updated[idx], content: fullContent, status: 'complete', toolUse: null };
              return updated;
            }
            return [
              ...msgs,
              {
                id: generateId(),
                role: 'assistant' as const,
                content: fullContent,
                status: 'complete' as const,
                timestamp: Date.now(),
                taskId: frame.task_id,
              },
            ];
          });
        }
        return;
      }

      // Non-chunked completed response
      if (frame.status === 'completed') {
        setIsAwaitingReply(false);
        updateMessages((msgs) => {
          const idx = findLastIndex(msgs, (m) => m.role === 'assistant' && m.status === 'streaming');
          if (idx !== -1) {
            const updated = [...msgs];
            updated[idx] = {
              ...updated[idx],
              content: content || updated[idx].content,
              status: 'complete',
              toolUse: null,
            };
            return updated;
          }
          return [
            ...msgs,
            {
              id: generateId(),
              role: 'assistant' as const,
              content,
              status: 'complete' as const,
              timestamp: Date.now(),
              taskId: frame.task_id,
            },
          ];
        });
      }
    },
    [updateMessages, trackActiveTask],
  );

  // ------------------------------------------------------------------
  const applyReplayEvent = useCallback((sessionId: string, kind: 'ag_ui' | 'terminal', payload: Record<string, unknown>, cursor: string) => {
    if (sessionIdRef.current !== sessionId || !cursorParts(cursor)) return;
    replayApplyingRef.current = true;
    try {
      if (kind === 'ag_ui' && payload.event && typeof payload.event === 'object') {
        dispatchAgUiEvent({ ...(payload.event as AgUiEvent), event_cursor: cursor });
      } else if (kind === 'terminal' && typeof payload.task_id === 'string') {
        handleLegacyResponse({
          type: 'response', task_id: payload.task_id, session_id: sessionId,
          terminal_delivery: true, delivery_id: payload.delivery_id as string,
          status: payload.status as WsResponseFrame['status'],
          retryable: payload.retryable as boolean,
          accounting_status: payload.accounting_status as WsResponseFrame['accounting_status'],
          content: payload.text as string,
        });
        if (payload.status === 'interrupted') {
          setReplayWarning('This turn was interrupted. Its external effects may be uncertain; it will not run again automatically.');
        }
      } else {
        throw new Error('Invalid replay event');
      }
      replayCursorRef.current = cursor;
      onMessagesChange(sessionId, messagesRef.current, cursor);
    } finally {
      replayApplyingRef.current = false;
    }
  }, [dispatchAgUiEvent, handleLegacyResponse, onMessagesChange]);

  const drainReplay = useCallback(async (sessionId: string) => {
    if (replayRunningRef.current || sessionIdRef.current !== sessionId) return;
    const epoch = replayEpochRef.current;
    replayRunningRef.current = true;
    let succeeded = false;
    try {
      for (let pageNumber = 0; pageNumber < 25; pageNumber++) {
        const cursor = replayCursorRef.current;
        const page = await readChatReplay(sessionId, cursor);
        if (sessionIdRef.current !== sessionId || epoch !== replayEpochRef.current) return;
        if (page.status === 'history_refresh_required') {
          if (page.reason === 'journal_unavailable' && replayBufferRef.current.length) throw new Error('Journal not ready');
          if (page.reason !== 'journal_unavailable' || cursor || messagesRef.current.some(message => message.role === 'assistant')) {
            setReplayWarning('Some output is unavailable. This transcript may be incomplete; check conversation history before continuing.');
          }
          if (page.reason !== 'journal_unavailable' && cursorParts(page.cursor)) {
            replayCursorRef.current = page.cursor;
            onMessagesChange(sessionId, messagesRef.current, page.cursor!);
          }
          succeeded = true;
          break;
        }
        if (page.status !== 'ok' || !cursorParts(page.cursor) || !Array.isArray(page.events)) throw new Error('Invalid replay page');
        if (!cursor && messagesRef.current.some(message => message.role === 'assistant')) {
          const parts = cursorParts(page.cursor);
          if (!parts || !Number.isSafeInteger(page.latest_sequence)) throw new Error('Invalid replay watermark');
          replayCursorRef.current = `${parts[0]}:${page.latest_sequence}`;
          onMessagesChange(sessionId, messagesRef.current, replayCursorRef.current);
          setReplayWarning('Older browser output has no replay cursor. Check conversation history for any missed output.');
          succeeded = true;
          break;
        }
        for (const event of page.events) {
          const parts = cursorParts(event.cursor);
          const previous = cursorParts(replayCursorRef.current);
          if (!parts || (previous && (parts[0] !== previous[0] || parts[1] !== previous[1] + 1)) || (!previous && parts[1] !== 1)) {
            throw new Error('Noncontiguous replay page');
          }
          applyReplayEvent(sessionId, event.kind, event.payload, event.cursor);
        }
        if (!page.has_more) {
          succeeded = true;
          break;
        }
        if (!page.events.length || pageNumber === 24) throw new Error('Replay page limit reached');
      }
    } catch {
      if (sessionIdRef.current === sessionId && epoch === replayEpochRef.current) {
        setReplayWarning('Replay is temporarily unavailable. Output may be missing; this turn will not run again automatically.');
      }
    } finally {
      if (epoch !== replayEpochRef.current) return;
      replayRunningRef.current = false;
      if (!succeeded && (replayCursorRef.current || replayBufferRef.current.length)) {
        if (!replayRetryRef.current) replayRetryRef.current = setTimeout(() => {
          replayRetryRef.current = null;
          if (epoch === replayEpochRef.current) void drainReplay(sessionId);
        }, 2_000);
        return;
      }
      if (succeeded && sessionIdRef.current === sessionId) {
        setReplayWarning(previous => previous?.startsWith('Replay is temporarily unavailable') ? null : previous);
        const buffered = replayBufferRef.current.splice(0);
        for (const frame of buffered) {
          if (frame.type === 'ag_ui' && cursorParts(frame.event.event_cursor)) {
            const parts = cursorParts(frame.event.event_cursor)!;
            const previous = cursorParts(replayCursorRef.current);
            if (previous && parts[0] === previous[0] && parts[1] <= previous[1]) continue;
            if (previous && parts[0] !== previous[0]) continue;
            if ((previous && parts[0] === previous[0] && parts[1] === previous[1] + 1) || (!previous && parts[1] === 1)) {
              applyReplayEvent(sessionId, 'ag_ui', { event: frame.event }, frame.event.event_cursor!);
              continue;
            }
            replayBufferRef.current.push(frame);
          } else if (frame.type === 'response' && frame.terminal_delivery) {
            handleLegacyResponse(frame);
          } else if (frame.type === 'ag_ui') {
            dispatchAgUiEvent(frame.event);
          }
        }
        if (replayBufferRef.current.length && !replayRetryRef.current) {
          replayRetryRef.current = setTimeout(() => {
            replayRetryRef.current = null;
            if (epoch === replayEpochRef.current) void drainReplay(sessionId);
          }, 250);
        }
      }
    }
  }, [applyReplayEvent, dispatchAgUiEvent, handleLegacyResponse, onMessagesChange]);

  const handleJournalFrame = useCallback((sessionId: string, frame: AgUiWsFrame) => {
    const parts = cursorParts(frame.event.event_cursor);
    if (!parts) {
      dispatchAgUiEvent(frame.event);
      return;
    }
    const previous = cursorParts(replayCursorRef.current);
    if (previous && parts[0] === previous[0] && parts[1] <= previous[1]) return;
    if (replayRunningRef.current || (previous && (parts[0] !== previous[0] || parts[1] !== previous[1] + 1)) ||
        (!previous && (parts[1] !== 1 || messagesRef.current.some(message => message.role === 'assistant')))) {
      if (replayBufferRef.current.length >= 256) {
        setReplayWarning('Replay is delayed. Output may be missing until the conversation is refreshed.');
        return;
      }
      replayBufferRef.current.push(frame);
      void drainReplay(sessionId);
      return;
    }
    applyReplayEvent(sessionId, 'ag_ui', { event: frame.event }, frame.event.event_cursor!);
  }, [applyReplayEvent, dispatchAgUiEvent, drainReplay]);

  // Token refresh
  // ------------------------------------------------------------------

  const getValidIdToken = useCallback(async (): Promise<string | null> => {
    try {
      const token = getIdToken();
      if (!token || isTokenExpired()) {
        const result = await refreshTokenService();
        return result?.token ?? null;
      }
      return token;
    } catch {
      return null;
    }
  }, []);

  // ------------------------------------------------------------------
  // WebSocket connection
  // ------------------------------------------------------------------

  const connect = useCallback(async () => {
    const sessionId = sessionIdRef.current;
    if (!sessionId) return;

    // Gateway-only deployments have no chat endpoint. Never send their login
    // token to another deployment's historical default endpoint.
    const wsBaseUrl = deploymentSetting('VITE_AGENT_WS_URL')?.trim();
    if (!wsBaseUrl) {
      setConnectionStatus('disconnected');
      updateMessages((msgs) => msgs.some((message) => message.content === CHAT_UNCONFIGURED)
        ? msgs
        : [...msgs, {
          id: generateId(), role: 'system', content: CHAT_UNCONFIGURED,
          status: 'error', timestamp: Date.now(),
        }]);
      return;
    }

    const token = await getValidIdToken();
    if (sessionIdRef.current !== sessionId) return;
    if (!token) {
      setConnectionStatus('disconnected');
      return;
    }

    setConnectionStatus('connecting');
    intentionalCloseRef.current = false;

    const url = `${wsBaseUrl}?token=${encodeURIComponent(token)}`;
    const ws = new WebSocket(url);
    wsRef.current = ws;

    ws.onopen = () => {
      if (wsRef.current !== ws || sessionIdRef.current !== sessionId) return;
      setConnectionStatus('connected');
      setReconnectAttempt(0);
      reconnectAttemptRef.current = 0;
      if (replayCursorRef.current || messagesRef.current.length) void drainReplay(sessionId);
    };

    ws.onmessage = (event) => {
      if (wsRef.current !== ws || sessionIdRef.current !== sessionId) return;
      try {
        const frame = JSON.parse(event.data) as WsFrame;

        // #5615: the ingress refused this conversation's identifier. Stop
        // waiting for a reply that is never coming and tell the user, rather
        // than spinning forever — this frame exists because a WebSocket
        // integration discards the Lambda's HTTP-shaped response.
        //
        // The refusal is terminal for this identifier, so there is nothing to
        // retry: the next message must go to a NEW server-issued one. The page
        // offers that; we do not silently create it here, because silently
        // moving a user's typing into a different conversation than the one on
        // screen is its own bug.
        if ((frame as { type?: string }).type === 'session_invalid') {
          setIsAwaitingReply(false);
          setSessionExpired(true);
          const notice = (frame as { content?: string }).content
            || 'That conversation is no longer available. Start a new one to continue.';
          updateMessages((msgs) =>
            msgs.some((m) => m.role === 'system' && m.content === notice)
              ? msgs
              : [...msgs, {
                id: generateId(), role: 'system' as const, content: notice,
                status: 'error' as const, timestamp: Date.now(),
              }],
          );
          return;
        }

        if (messagesRef.current.some(message => message.taskId === frame.task_id && message.terminalDeliveryId)) return;

        // AG-UI event path
        if (frame.type === 'ag_ui') {
          const agUiFrame = frame as AgUiWsFrame;
          handleJournalFrame(sessionId, agUiFrame);
          return;
        }

        if (frame.type === 'response' && frame.terminal_delivery && (replayCursorRef.current || replayRunningRef.current)) {
          replayBufferRef.current.push(frame);
          void drainReplay(sessionId);
          return;
        }

        // Legacy frame path (backward compat)
        switch (frame.type) {
          case 'notification':
            handleLegacyNotification(frame);
            break;
          case 'progress':
            handleLegacyProgress(frame as WsProgressFrame);
            break;
          case 'response':
            handleLegacyResponse(frame as WsResponseFrame);
            break;
        }
      } catch (err) {
        console.warn('[useAgUiEvents] Failed to parse WS frame:', err);
      }
    };

    ws.onerror = () => {
      // onerror is always followed by onclose — handle reconnect there.
    };

    ws.onclose = (event) => {
      if (wsRef.current !== ws) return;
      wsRef.current = null;

      if (intentionalCloseRef.current) {
        setConnectionStatus('disconnected');
        return;
      }

      // 4001 = auth failure from our authorizer
      if (event.code === 4001 || event.code === 4003) {
        refreshTokenService()
          .then(() => scheduleReconnect())
          .catch(() => setConnectionStatus('disconnected'));
        return;
      }

      scheduleReconnect();
    };
  }, [getValidIdToken, drainReplay, handleJournalFrame, handleLegacyNotification, handleLegacyProgress, handleLegacyResponse, updateMessages]);

  const scheduleReconnect = useCallback(() => {
    const attempt = reconnectAttemptRef.current;
    if (attempt >= MAX_RECONNECT_ATTEMPTS) {
      setConnectionStatus('disconnected');
      return;
    }

    setConnectionStatus('reconnecting');
    const nextAttempt = attempt + 1;
    reconnectAttemptRef.current = nextAttempt;
    setReconnectAttempt(nextAttempt);

    const delay = backoffMs(attempt);
    reconnectTimerRef.current = setTimeout(() => {
      connect();
    }, delay);
  }, [connect]);

  const disconnect = useCallback(() => {
    intentionalCloseRef.current = true;
    replayEpochRef.current += 1;
    replayRunningRef.current = false;
    replayBufferRef.current = [];
    if (replayRetryRef.current) {
      clearTimeout(replayRetryRef.current);
      replayRetryRef.current = null;
    }
    if (reconnectTimerRef.current) {
      clearTimeout(reconnectTimerRef.current);
      reconnectTimerRef.current = null;
    }
    if (wsRef.current) {
      wsRef.current.close();
      wsRef.current = null;
    }
    setConnectionStatus('disconnected');
    setReconnectAttempt(0);
    reconnectAttemptRef.current = 0;
  }, []);

  // Connect when conversation changes, disconnect on unmount.
  useEffect(() => {
    if (conversation) {
      connect();
    } else {
      disconnect();
    }
    return () => disconnect();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [conversation?.id]);

  const replaySessionId = conversation?.id;
  useEffect(() => {
    if (!replaySessionId || !isAwaitingReply) return;
    const interval = setInterval(() => {
      if (wsRef.current?.readyState === WebSocket.OPEN) void drainReplay(replaySessionId);
    }, 2_000);
    return () => clearInterval(interval);
  }, [replaySessionId, isAwaitingReply, drainReplay]);

  // ------------------------------------------------------------------
  // Send message
  // ------------------------------------------------------------------

  const sendMessage = useCallback(
    (text: string, attachments?: string[], persona?: string) => {
      if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) return;
      if (!sessionIdRef.current) return;
      // #5615: the server has already refused this identifier. Re-sending it
      // would be refused identically and would restart the spinner, so the send
      // is dropped until the page moves to a new conversation.
      if (sessionExpired) return;

      const userMsg: ChatMessage = {
        id: generateId(),
        role: 'user',
        content: text,
        status: 'complete',
        timestamp: Date.now(),
      };

      updateMessages((msgs) => [...msgs, userMsg]);
      setIsAwaitingReply(true);
      activeTaskRef.current = null;
      cancelRequestRef.current = null;
      setActiveTaskId(null);
      setCancelState('idle');

      // The ingest Lambda's webchat adapter reads `text`, not `message`
      // (gateway/lambdas/ingest/channels/webchat.py:125). Wrong field → silent drop.
      // Stage C (#186): include attachment IDs so they're forwarded to the worker.
      const payload: Record<string, unknown> = {
        action: 'sendMessage',
        text,
        session_id: sessionIdRef.current,
      };
      if (attachments && attachments.length > 0) {
        payload.attachments = attachments;
      }
      // #4208: pin the persona for this turn (e.g. 'intent-refinement'), skipping
      // the server-side classifier. Ingest validates it against an allowlist and
      // returns 400 for anything else — it never silently downgrades, so a typo
      // here surfaces as a failed send rather than a conversation on the wrong
      // persona.
      if (persona) {
        payload.persona = persona;
      }
      wsRef.current.send(JSON.stringify(payload));
    },
    [updateMessages, sessionExpired],
  );

  return {
    connectionStatus,
    isAwaitingReply,
    reconnectAttempt,
    sessionExpired,
    replayWarning,
    sessionMeta,
    sendMessage,
    cancelTurn,
    canCancel: isAwaitingReply && !sessionExpired && activeTaskId !== null && cancelState !== 'pending' && cancelState !== 'requested',
    cancelState,
    activeToolCalls,
    wsRef,
  };
}
