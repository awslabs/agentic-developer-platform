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

import { useCallback, useEffect, useRef, useState } from 'react';
import { getIdToken, isTokenExpired, refreshToken as refreshTokenService } from '@/services/auth';
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

// ---------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------

const WS_BASE_URL =
  import.meta.env.VITE_AGENT_WS_URL ||
  'wss://8ea7pg40b7.execute-api.us-east-1.amazonaws.com/v1';

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

// ---------------------------------------------------------------------------
// Hook types
// ---------------------------------------------------------------------------

export interface UseAgUiEventsOptions {
  /** Active conversation (controls WS lifecycle). */
  conversation: Conversation | null;
  /** Called when messages change so the caller can persist to localStorage. */
  onMessagesChange: (sessionId: string, messages: ChatMessage[]) => void;
}

export interface UseAgUiEventsReturn extends AgentChatState {
  sendMessage: (text: string) => void;
  /** Active tool calls for the current turn. */
  activeToolCalls: ToolCallInfo[];
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
  const [sessionMeta, setSessionMeta] = useState<SessionMeta | undefined>();
  const [activeToolCalls, setActiveToolCalls] = useState<ToolCallInfo[]>([]);

  // Refs to avoid stale closures inside WS callbacks.
  const wsRef = useRef<WebSocket | null>(null);
  const messagesRef = useRef<ChatMessage[]>([]);
  const sessionIdRef = useRef<string | null>(null);
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const chunkBufferRef = useRef<Map<string, string[]>>(new Map());
  const intentionalCloseRef = useRef(false);
  const reconnectAttemptRef = useRef(0);
  const toolCallsRef = useRef<Map<string, ToolCallInfo>>(new Map());

  // Keep refs in sync with conversation prop.
  useEffect(() => {
    if (conversation) {
      messagesRef.current = conversation.messages;
      sessionIdRef.current = conversation.id;
    }
  }, [conversation]);

  // ------------------------------------------------------------------
  // Message mutation helper
  // ------------------------------------------------------------------

  const updateMessages = useCallback(
    (updater: (prev: ChatMessage[]) => ChatMessage[]) => {
      const next = updater(messagesRef.current);
      messagesRef.current = next;
      if (sessionIdRef.current) {
        onMessagesChange(sessionIdRef.current, next);
      }
    },
    [onMessagesChange],
  );

  // ------------------------------------------------------------------
  // AG-UI event handlers
  // ------------------------------------------------------------------

  const handleRunStarted = useCallback(() => {
    setIsAwaitingReply(true);
    // Reset tool calls for new run
    toolCallsRef.current.clear();
    setActiveToolCalls([]);
  }, []);

  const handleRunFinished = useCallback(
    (event: AgUiEvent & { event_type: typeof AgUiEventType.RUN_FINISHED }) => {
      setIsAwaitingReply(false);
      toolCallsRef.current.clear();
      setActiveToolCalls([]);

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
      const toolCall: ToolCallInfo = {
        toolCallId: event.toolCallId,
        toolCallName: event.toolCallName,
        args: '',
        status: 'running',
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
      // Apply JSON Patch operations to session meta
      setSessionMeta(prev => {
        const meta = { ...prev } as Record<string, unknown>;
        for (const op of event.delta) {
          // Simple path parsing: /tokens, /turnCount, /heartbeat
          const key = op.path.replace(/^\//, '');
          if (op.op === 'replace' || op.op === 'add') {
            meta[key] = op.value;
          } else if (op.op === 'remove') {
            delete meta[key];
          }
        }
        return meta as SessionMeta;
      });

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
      switch (event.event_type) {
        case AgUiEventType.RUN_STARTED:
          handleRunStarted();
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
    [updateMessages],
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
      const content = extractContent(frame);

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
    [updateMessages],
  );

  // ------------------------------------------------------------------
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
    if (!sessionIdRef.current) return;

    const token = await getValidIdToken();
    if (!token) {
      setConnectionStatus('disconnected');
      return;
    }

    setConnectionStatus('connecting');
    intentionalCloseRef.current = false;

    const url = `${WS_BASE_URL}?token=${encodeURIComponent(token)}`;
    const ws = new WebSocket(url);
    wsRef.current = ws;

    ws.onopen = () => {
      setConnectionStatus('connected');
      setReconnectAttempt(0);
      reconnectAttemptRef.current = 0;
    };

    ws.onmessage = (event) => {
      try {
        const frame = JSON.parse(event.data) as WsFrame;

        // AG-UI event path
        if (frame.type === 'ag_ui') {
          const agUiFrame = frame as AgUiWsFrame;
          dispatchAgUiEvent(agUiFrame.event);
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
  }, [getValidIdToken, dispatchAgUiEvent, handleLegacyNotification, handleLegacyProgress, handleLegacyResponse]);

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

  // ------------------------------------------------------------------
  // Send message
  // ------------------------------------------------------------------

  const sendMessage = useCallback(
    (text: string) => {
      if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) return;
      if (!sessionIdRef.current) return;

      const userMsg: ChatMessage = {
        id: generateId(),
        role: 'user',
        content: text,
        status: 'complete',
        timestamp: Date.now(),
      };

      updateMessages((msgs) => [...msgs, userMsg]);
      setIsAwaitingReply(true);

      // The ingest Lambda's webchat adapter reads `text`, not `message`
      // (gateway/lambdas/ingest/channels/webchat.py:125). Wrong field → silent drop.
      wsRef.current.send(
        JSON.stringify({
          action: 'sendMessage',
          text,
          session_id: sessionIdRef.current,
        }),
      );
    },
    [updateMessages],
  );

  return {
    connectionStatus,
    isAwaitingReply,
    reconnectAttempt,
    sessionMeta,
    sendMessage,
    activeToolCalls,
  };
}
