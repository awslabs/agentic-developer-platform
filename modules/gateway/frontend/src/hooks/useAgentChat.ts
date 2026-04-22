/**
 * useAgentChat — WebSocket client hook for the Agent Chat widget.
 *
 * Issue #97 Phase 1: connects to the agent-gateway WS endpoint using the
 * Cognito ID token, handles every inbound frame type, reassembles chunked
 * responses, and auto-reconnects with exponential backoff.
 *
 * The hook is intentionally framework-agnostic about the wire format so that
 * Phase 2 (AG-UI) can swap the frame parser without touching UI components.
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

/** Extract text content from a response frame (matches Lambda's priority). */
function extractContent(frame: WsResponseFrame): string {
  return frame.text || frame.result || frame.content || '';
}

function backoffMs(attempt: number): number {
  return Math.min(INITIAL_BACKOFF_MS * 2 ** attempt, MAX_BACKOFF_MS);
}

/** ES2023 findLastIndex polyfill — avoids bumping lib target. */
function findLastIndex<T>(arr: T[], predicate: (item: T) => boolean): number {
  for (let i = arr.length - 1; i >= 0; i--) {
    if (predicate(arr[i])) return i;
  }
  return -1;
}

// ---------------------------------------------------------------------------
// Hook
// ---------------------------------------------------------------------------

export interface UseAgentChatOptions {
  /** Active conversation (controls WS lifecycle). */
  conversation: Conversation | null;
  /** Called when messages change so the caller can persist to localStorage. */
  onMessagesChange: (sessionId: string, messages: ChatMessage[]) => void;
}

export interface UseAgentChatReturn extends AgentChatState {
  sendMessage: (text: string) => void;
}

export function useAgentChat({
  conversation,
  onMessagesChange,
}: UseAgentChatOptions): UseAgentChatReturn {
  const [connectionStatus, setConnectionStatus] = useState<ConnectionStatus>('disconnected');
  const [isAwaitingReply, setIsAwaitingReply] = useState(false);
  const [reconnectAttempt, setReconnectAttempt] = useState(0);

  // Refs to avoid stale closures inside WS callbacks.
  const wsRef = useRef<WebSocket | null>(null);
  const messagesRef = useRef<ChatMessage[]>([]);
  const sessionIdRef = useRef<string | null>(null);
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const chunkBufferRef = useRef<Map<string, string[]>>(new Map());
  const intentionalCloseRef = useRef(false);
  const reconnectAttemptRef = useRef(0);

  // Keep refs in sync with conversation prop.
  useEffect(() => {
    if (conversation) {
      messagesRef.current = conversation.messages;
      sessionIdRef.current = conversation.id;
    }
  }, [conversation]);

  // ------------------------------------------------------------------
  // Message mutation helper — updates ref + calls persistence callback
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
  // Frame handlers
  // ------------------------------------------------------------------

  const handleNotification = useCallback(
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

  const handleProgress = useCallback(
    (frame: WsProgressFrame) => {
      // All progress kinds (heartbeat, tool_use) keep the typing indicator
      // alive on the pending assistant bubble. Tool metadata (name, input)
      // isn't on the wire today — when the response Lambda starts forwarding
      // those fields, we can differentiate the UI per-kind.
      updateMessages((msgs) => {
        const idx = findLastIndex(msgs,
          (m: ChatMessage) => m.role === 'assistant' && m.status === 'streaming',
        );
        if (idx === -1) {
          // No pending bubble yet — create one so the indicator shows.
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
        // Touch timestamp to keep the indicator animating.
        const updated = [...msgs];
        updated[idx] = { ...updated[idx], timestamp: Date.now() };
        return updated;
      });
    },
    [updateMessages],
  );

  const handleResponse = useCallback(
    (frame: WsResponseFrame) => {
      const content = extractContent(frame);

      if (frame.status === 'failed') {
        // Error bubble.
        updateMessages((msgs) => {
          // Finalize any pending bubble.
          const idx = findLastIndex(msgs,
            (m: ChatMessage) => m.role === 'assistant' && m.status === 'streaming',
          );
          if (idx !== -1) {
            const updated = [...msgs];
            updated[idx] = {
              ...updated[idx],
              status: 'error',
              content: content || updated[idx].content,
              errorReason: frame.reason || 'Unknown error',
              toolUse: null,
            };
            return updated;
          }
          return [
            ...msgs,
            {
              id: generateId(),
              role: 'assistant' as const,
              content: content || '',
              status: 'error' as const,
              timestamp: Date.now(),
              taskId: frame.task_id,
              errorReason: frame.reason || 'Unknown error',
            },
          ];
        });
        setIsAwaitingReply(false);
        chunkBufferRef.current.delete(frame.task_id);
        return;
      }

      if (frame.status === 'completed') {
        // Terminal frame — may carry final content directly or be one of N
        // chunks. Chunked frames use 1-based `chunk_index` (see the server's
        // `_split_content` in response/routers/websocket.py). Normalize to
        // 0-based for array storage so `buf.join('')` preserves order.
        const isChunked = (frame.chunk_total ?? 1) > 1;

        if (isChunked) {
          const buf = chunkBufferRef.current.get(frame.task_id) || [];
          const idx = (frame.chunk_index ?? 1) - 1;
          buf[idx] = content;
          chunkBufferRef.current.set(frame.task_id, buf);

          const total = frame.chunk_total!;
          // Check each slot 0..total-1 explicitly. `Array.every` skips sparse
          // slots (standard behavior) which would falsely report "all filled"
          // if e.g. chunks 1 and 3 arrive before chunk 2. Out-of-order chunk
          // delivery is a real case with response Lambda concurrency.
          let allFilled = true;
          for (let i = 0; i < total; i++) {
            if (typeof buf[i] !== 'string') {
              allFilled = false;
              break;
            }
          }
          if (allFilled) {
            const fullContent = buf.join('');
            chunkBufferRef.current.delete(frame.task_id);
            finalizeAssistantBubble(frame.task_id, fullContent);
          } else {
            // Partial render while we wait for the rest.
            const partial = buf.filter((c) => typeof c === 'string').join('');
            updatePendingBubble(frame.task_id, partial);
          }
        } else {
          // Single-frame response.
          finalizeAssistantBubble(frame.task_id, content);
        }
        return;
      }

      // Any other status on a type=response frame is an intermediate ack
      // (classifier notification, escalation note). Update the pending
      // bubble with whatever content arrived but don't finalize.
      updatePendingBubble(frame.task_id, content);
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [updateMessages],
  );

  // ------------------------------------------------------------------
  // Bubble helpers
  // ------------------------------------------------------------------

  const updatePendingBubble = useCallback(
    (taskId: string, content: string) => {
      updateMessages((msgs) => {
        const idx = findLastIndex(msgs,
          (m: ChatMessage) => m.role === 'assistant' && (m.status === 'streaming' || m.taskId === taskId),
        );
        if (idx === -1) {
          return [
            ...msgs,
            {
              id: generateId(),
              role: 'assistant' as const,
              content,
              status: 'streaming' as const,
              timestamp: Date.now(),
              taskId,
            },
          ];
        }
        const updated = [...msgs];
        updated[idx] = { ...updated[idx], content, status: 'streaming', timestamp: Date.now() };
        return updated;
      });
    },
    [updateMessages],
  );

  const finalizeAssistantBubble = useCallback(
    (taskId: string, content: string) => {
      updateMessages((msgs) => {
        const idx = findLastIndex(msgs,
          (m: ChatMessage) => m.role === 'assistant' && (m.status === 'streaming' || m.taskId === taskId),
        );
        if (idx === -1) {
          return [
            ...msgs,
            {
              id: generateId(),
              role: 'assistant' as const,
              content,
              status: 'complete' as const,
              timestamp: Date.now(),
              taskId,
              toolUse: null,
            },
          ];
        }
        const updated = [...msgs];
        updated[idx] = {
          ...updated[idx],
          content,
          status: 'complete',
          toolUse: null,
          timestamp: Date.now(),
        };
        return updated;
      });
      setIsAwaitingReply(false);
    },
    [updateMessages],
  );

  // ------------------------------------------------------------------
  // Token helper
  // ------------------------------------------------------------------

  const getValidIdToken = useCallback(async (): Promise<string | null> => {
    if (isTokenExpired(1)) {
      try {
        await refreshTokenService();
      } catch {
        return null;
      }
    }
    return getIdToken();
  }, []);

  // ------------------------------------------------------------------
  // WS lifecycle
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
        const frame: WsFrame = JSON.parse(event.data);
        switch (frame.type) {
          case 'notification':
            handleNotification(frame);
            break;
          case 'progress':
            handleProgress(frame as WsProgressFrame);
            break;
          case 'response':
            handleResponse(frame as WsResponseFrame);
            break;
        }
      } catch (err) {
        console.warn('[useAgentChat] Failed to parse WS frame:', err);
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
        // Token invalid — try refreshing once, then reconnect.
        refreshTokenService()
          .then(() => scheduleReconnect())
          .catch(() => setConnectionStatus('disconnected'));
        return;
      }

      scheduleReconnect();
    };
  }, [getValidIdToken, handleNotification, handleProgress, handleResponse]);

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
      // (see gateway/lambdas/ingest/channels/webchat.py:125). Using the wrong
      // field name silently drops the message with a 200 OK.
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
    sendMessage,
  };
}
