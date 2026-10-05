/**
 * Unit tests for the useAgUiEvents hook.
 *
 * Issue #97 Phase 2: verifies AG-UI event dispatching, backward-compat with
 * legacy frames, tool call tracking, state delta handling, and reconnect.
 *
 * Uses a mock WebSocket implementation (same pattern as useAgentChat tests).
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import { useAgUiEvents } from '@/hooks/useAgUiEvents';
import type { Conversation, ChatMessage } from '@/types/chat';
import { AgUiEventType } from '@/types/ag-ui-events';

// ---------------------------------------------------------------------------
// Mock WebSocket
// ---------------------------------------------------------------------------

class MockWebSocket {
  static instances: MockWebSocket[] = [];
  static OPEN = 1;
  static CLOSED = 3;

  url: string;
  readyState = 0;
  onopen: ((ev: Event) => void) | null = null;
  onclose: ((ev: CloseEvent) => void) | null = null;
  onmessage: ((ev: MessageEvent) => void) | null = null;
  onerror: ((ev: Event) => void) | null = null;
  sent: string[] = [];

  constructor(url: string) {
    this.url = url;
    MockWebSocket.instances.push(this);
  }

  send(data: string) {
    this.sent.push(data);
  }

  close() {
    this.readyState = 3;
    this.onclose?.(new CloseEvent('close', { code: 1000 }));
  }

  simulateOpen() {
    this.readyState = 1;
    this.onopen?.(new Event('open'));
  }

  simulateMessage(data: unknown) {
    this.onmessage?.(new MessageEvent('message', { data: JSON.stringify(data) }));
  }

  simulateClose(code = 1000) {
    this.readyState = 3;
    this.onclose?.(new CloseEvent('close', { code }));
  }
}

// ---------------------------------------------------------------------------
// Mock auth
// ---------------------------------------------------------------------------

vi.mock('@/services/auth', () => ({
  getIdToken: vi.fn(() => 'mock-id-token'),
  isTokenExpired: vi.fn(() => false),
  refreshToken: vi.fn(() => Promise.resolve({ token: 'new-token', expiresAt: '' })),
}));

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function makeConversation(id = 'test-session', messages: ChatMessage[] = []): Conversation {
  return {
    id,
    title: 'Test',
    createdAt: Date.now(),
    updatedAt: Date.now(),
    messages,
  };
}

function getLastWs(): MockWebSocket {
  return MockWebSocket.instances[MockWebSocket.instances.length - 1];
}

/** Build an AG-UI WS frame. */
function agUiFrame(event: Record<string, unknown>) {
  return {
    type: 'ag_ui',
    task_id: 'task-1',
    event,
    timestamp: new Date().toISOString(),
  };
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('useAgUiEvents', () => {
  beforeEach(() => {
    vi.stubEnv('VITE_AGENT_WS_URL', 'wss://chat.example.test/v1');
    MockWebSocket.instances = [];
    vi.stubGlobal('WebSocket', MockWebSocket);
    vi.useFakeTimers({ shouldAdvanceTime: true });
  });

  afterEach(() => {
    vi.unstubAllEnvs();
    vi.restoreAllMocks();
    vi.useRealTimers();
  });

  // ----- Connection lifecycle -----

  it('does not send credentials to a fallback endpoint when chat is unconfigured', async () => {
    vi.stubEnv('VITE_AGENT_WS_URL', '');
    const onMsg = vi.fn();
    const conv = makeConversation();
    const { result } = renderHook(() =>
      useAgUiEvents({ conversation: conv, onMessagesChange: onMsg }),
    );
    await act(async () => { await Promise.resolve(); });
    expect(MockWebSocket.instances).toHaveLength(0);
    expect(result.current.connectionStatus).toBe('disconnected');
    expect(onMsg).toHaveBeenCalledWith(conv.id, expect.arrayContaining([
      expect.objectContaining({ content: 'Agent chat is not configured for this deployment.', status: 'error' }),
    ]));
  });

  it('connects to WS when conversation is provided', async () => {
    const onMsg = vi.fn();
    const conv = makeConversation();

    const { result } = renderHook(() =>
      useAgUiEvents({ conversation: conv, onMessagesChange: onMsg }),
    );

    // connect() is async (awaits getValidIdToken) — advance timers + flush microtasks
    await act(async () => {
      await vi.advanceTimersByTimeAsync(50);
    });
    expect(MockWebSocket.instances).toHaveLength(1);
    expect(result.current.connectionStatus).toBe('connecting');

    act(() => getLastWs().simulateOpen());
    expect(result.current.connectionStatus).toBe('connected');
  });

  it('disconnects when conversation is null', async () => {
    const onMsg = vi.fn();
    const conv = makeConversation();

    const { result, rerender } = renderHook(
      ({ conv }) => useAgUiEvents({ conversation: conv, onMessagesChange: onMsg }),
      { initialProps: { conv: conv as Conversation | null } },
    );

    await vi.advanceTimersByTimeAsync(10);
    act(() => getLastWs().simulateOpen());
    expect(result.current.connectionStatus).toBe('connected');

    rerender({ conv: null });
    expect(result.current.connectionStatus).toBe('disconnected');
  });

  // ----- AG-UI: RUN_STARTED -----

  it('handles RUN_STARTED — sets isAwaitingReply', async () => {
    const onMsg = vi.fn();
    const conv = makeConversation();
    const { result } = renderHook(() =>
      useAgUiEvents({ conversation: conv, onMessagesChange: onMsg }),
    );

    await vi.advanceTimersByTimeAsync(10);
    act(() => getLastWs().simulateOpen());
    expect(result.current.isAwaitingReply).toBe(false);

    act(() => {
      getLastWs().simulateMessage(agUiFrame({
        event_type: AgUiEventType.RUN_STARTED,
        threadId: 'test-session',
        runId: 'task-1',
      }));
    });

    expect(result.current.isAwaitingReply).toBe(true);
  });

  // ----- AG-UI: TEXT_MESSAGE flow -----

  it('handles TEXT_MESSAGE_START → CONTENT → END — creates and populates assistant bubble', async () => {
    const onMsg = vi.fn();
    const conv = makeConversation();
    renderHook(() => useAgUiEvents({ conversation: conv, onMessagesChange: onMsg }));

    await vi.advanceTimersByTimeAsync(10);
    act(() => getLastWs().simulateOpen());

    // TEXT_MESSAGE_START
    act(() => {
      getLastWs().simulateMessage(agUiFrame({
        event_type: AgUiEventType.TEXT_MESSAGE_START,
        messageId: 'msg-1',
        role: 'assistant',
      }));
    });

    expect(onMsg).toHaveBeenCalled();
    let msgs = onMsg.mock.calls[onMsg.mock.calls.length - 1][1] as ChatMessage[];
    const bubble = msgs[msgs.length - 1];
    expect(bubble.role).toBe('assistant');
    expect(bubble.status).toBe('streaming');
    expect(bubble.agUiMessageId).toBe('msg-1');

    // TEXT_MESSAGE_CONTENT (two deltas)
    act(() => {
      getLastWs().simulateMessage(agUiFrame({
        event_type: AgUiEventType.TEXT_MESSAGE_CONTENT,
        messageId: 'msg-1',
        delta: 'Hello ',
      }));
    });

    act(() => {
      getLastWs().simulateMessage(agUiFrame({
        event_type: AgUiEventType.TEXT_MESSAGE_CONTENT,
        messageId: 'msg-1',
        delta: 'world!',
      }));
    });

    msgs = onMsg.mock.calls[onMsg.mock.calls.length - 1][1] as ChatMessage[];
    const updated = msgs[msgs.length - 1];
    expect(updated.content).toBe('Hello world!');
    expect(updated.status).toBe('streaming');

    // TEXT_MESSAGE_END
    act(() => {
      getLastWs().simulateMessage(agUiFrame({
        event_type: AgUiEventType.TEXT_MESSAGE_END,
        messageId: 'msg-1',
      }));
    });

    // Still streaming until RUN_FINISHED
    msgs = onMsg.mock.calls[onMsg.mock.calls.length - 1][1] as ChatMessage[];
    expect(msgs[msgs.length - 1].status).toBe('streaming');
  });

  // ----- AG-UI: RUN_FINISHED -----

  it('handles RUN_FINISHED — finalizes streaming bubble', async () => {
    const onMsg = vi.fn();
    const conv = makeConversation();
    const { result } = renderHook(() =>
      useAgUiEvents({ conversation: conv, onMessagesChange: onMsg }),
    );

    await vi.advanceTimersByTimeAsync(10);
    act(() => getLastWs().simulateOpen());

    // Start → Content → End
    act(() => {
      getLastWs().simulateMessage(agUiFrame({
        event_type: AgUiEventType.RUN_STARTED,
        threadId: 'test-session',
        runId: 'task-1',
      }));
    });
    act(() => {
      getLastWs().simulateMessage(agUiFrame({
        event_type: AgUiEventType.TEXT_MESSAGE_START,
        messageId: 'msg-1',
        role: 'assistant',
      }));
    });
    act(() => {
      getLastWs().simulateMessage(agUiFrame({
        event_type: AgUiEventType.TEXT_MESSAGE_CONTENT,
        messageId: 'msg-1',
        delta: 'Reply text',
      }));
    });

    // RUN_FINISHED
    act(() => {
      getLastWs().simulateMessage(agUiFrame({
        event_type: AgUiEventType.RUN_FINISHED,
        threadId: 'test-session',
        runId: 'task-1',
        result: { tokens: { input: 100, output: 50 }, turnCount: 3 },
      }));
    });

    expect(result.current.isAwaitingReply).toBe(false);
    const msgs = onMsg.mock.calls[onMsg.mock.calls.length - 1][1] as ChatMessage[];
    expect(msgs[msgs.length - 1].status).toBe('complete');
    expect(result.current.sessionMeta?.tokens).toEqual({ input: 100, output: 50 });
    expect(result.current.sessionMeta?.turnCount).toBe(3);
  });

  // ----- AG-UI: RUN_ERROR -----

  it('handles RUN_ERROR — creates error bubble', async () => {
    const onMsg = vi.fn();
    const conv = makeConversation();
    const { result } = renderHook(() =>
      useAgUiEvents({ conversation: conv, onMessagesChange: onMsg }),
    );

    await vi.advanceTimersByTimeAsync(10);
    act(() => getLastWs().simulateOpen());

    act(() => {
      getLastWs().simulateMessage(agUiFrame({
        event_type: AgUiEventType.RUN_ERROR,
        message: 'Model overloaded',
        code: 'OVERLOADED',
      }));
    });

    expect(result.current.isAwaitingReply).toBe(false);
    const msgs = onMsg.mock.calls[onMsg.mock.calls.length - 1][1] as ChatMessage[];
    const errMsg = msgs[msgs.length - 1];
    expect(errMsg.role).toBe('assistant');
    expect(errMsg.status).toBe('error');
    expect(errMsg.errorReason).toBe('Model overloaded');
  });

  // ----- AG-UI: TOOL_CALL flow -----

  it('handles TOOL_CALL_START → ARGS → END', async () => {
    const onMsg = vi.fn();
    const conv = makeConversation();
    const { result } = renderHook(() =>
      useAgUiEvents({ conversation: conv, onMessagesChange: onMsg }),
    );

    await vi.advanceTimersByTimeAsync(10);
    act(() => getLastWs().simulateOpen());

    // Create a streaming bubble first
    act(() => {
      getLastWs().simulateMessage(agUiFrame({
        event_type: AgUiEventType.TEXT_MESSAGE_START,
        messageId: 'msg-1',
        role: 'assistant',
      }));
    });

    // TOOL_CALL_START
    act(() => {
      getLastWs().simulateMessage(agUiFrame({
        event_type: AgUiEventType.TOOL_CALL_START,
        toolCallId: 'tc-1',
        toolCallName: 'WebSearch',
        parentMessageId: 'msg-1',
      }));
    });

    expect(result.current.activeToolCalls).toHaveLength(1);
    expect(result.current.activeToolCalls[0].toolCallName).toBe('WebSearch');
    expect(result.current.activeToolCalls[0].status).toBe('running');

    // TOOL_CALL_ARGS
    act(() => {
      getLastWs().simulateMessage(agUiFrame({
        event_type: AgUiEventType.TOOL_CALL_ARGS,
        toolCallId: 'tc-1',
        delta: '{"query": "React hooks"}',
      }));
    });

    expect(result.current.activeToolCalls[0].args).toBe('{"query": "React hooks"}');

    // TOOL_CALL_END
    act(() => {
      getLastWs().simulateMessage(agUiFrame({
        event_type: AgUiEventType.TOOL_CALL_END,
        toolCallId: 'tc-1',
      }));
    });

    expect(result.current.activeToolCalls[0].status).toBe('complete');

    // Check message toolCalls array
    const msgs = onMsg.mock.calls[onMsg.mock.calls.length - 1][1] as ChatMessage[];
    const assistantMsg = msgs[msgs.length - 1];
    expect(assistantMsg.toolCalls).toHaveLength(1);
    expect(assistantMsg.toolCalls![0].toolCallName).toBe('WebSearch');
    expect(assistantMsg.toolCalls![0].status).toBe('complete');
  });

  // ----- AG-UI: STATE_DELTA -----

  it('handles STATE_DELTA — updates session meta', async () => {
    const onMsg = vi.fn();
    const conv = makeConversation();
    const { result } = renderHook(() =>
      useAgUiEvents({ conversation: conv, onMessagesChange: onMsg }),
    );

    await vi.advanceTimersByTimeAsync(10);
    act(() => getLastWs().simulateOpen());

    act(() => {
      getLastWs().simulateMessage(agUiFrame({
        event_type: AgUiEventType.STATE_DELTA,
        delta: [
          { op: 'replace', path: '/tokens', value: { input: 200, output: 100 } },
          { op: 'replace', path: '/turnCount', value: 5 },
        ],
      }));
    });

    expect(result.current.sessionMeta?.tokens).toEqual({ input: 200, output: 100 });
    expect(result.current.sessionMeta?.turnCount).toBe(5);
  });

  it('handles STATE_DELTA heartbeat — creates streaming bubble', async () => {
    const onMsg = vi.fn();
    const conv = makeConversation();
    renderHook(() => useAgUiEvents({ conversation: conv, onMessagesChange: onMsg }));

    await vi.advanceTimersByTimeAsync(10);
    act(() => getLastWs().simulateOpen());

    act(() => {
      getLastWs().simulateMessage(agUiFrame({
        event_type: AgUiEventType.STATE_DELTA,
        delta: [
          { op: 'replace', path: '/heartbeat', value: { turn: 2, ts: Date.now() } },
        ],
      }));
    });

    const msgs = onMsg.mock.calls[onMsg.mock.calls.length - 1][1] as ChatMessage[];
    const streaming = msgs.find(m => m.role === 'assistant' && m.status === 'streaming');
    expect(streaming).toBeDefined();
  });

  // ----- Legacy backward compat -----

  it('handles legacy notification frame', async () => {
    const onMsg = vi.fn();
    const conv = makeConversation();
    renderHook(() => useAgUiEvents({ conversation: conv, onMessagesChange: onMsg }));

    await vi.advanceTimersByTimeAsync(10);
    act(() => getLastWs().simulateOpen());

    act(() => {
      getLastWs().simulateMessage({
        type: 'notification',
        task_id: 'task-1',
        message: 'Task received',
      });
    });

    const msgs = onMsg.mock.calls[onMsg.mock.calls.length - 1][1] as ChatMessage[];
    expect(msgs[msgs.length - 1].role).toBe('system');
    expect(msgs[msgs.length - 1].content).toBe('Task received');
  });

  it('handles legacy completed response frame', async () => {
    const onMsg = vi.fn();
    const conv = makeConversation();
    const { result } = renderHook(() =>
      useAgUiEvents({ conversation: conv, onMessagesChange: onMsg }),
    );

    await vi.advanceTimersByTimeAsync(10);
    act(() => getLastWs().simulateOpen());

    act(() => {
      getLastWs().simulateMessage({
        type: 'response',
        task_id: 'task-1',
        status: 'completed',
        text: 'Here is your answer.',
      });
    });

    expect(result.current.isAwaitingReply).toBe(false);
    const msgs = onMsg.mock.calls[onMsg.mock.calls.length - 1][1] as ChatMessage[];
    const reply = msgs[msgs.length - 1];
    expect(reply.role).toBe('assistant');
    expect(reply.status).toBe('complete');
    expect(reply.content).toBe('Here is your answer.');
  });

  it('handles legacy failed response frame', async () => {
    const onMsg = vi.fn();
    const conv = makeConversation();
    renderHook(() => useAgUiEvents({ conversation: conv, onMessagesChange: onMsg }));

    await vi.advanceTimersByTimeAsync(10);
    act(() => getLastWs().simulateOpen());

    act(() => {
      getLastWs().simulateMessage({
        type: 'response',
        task_id: 'task-1',
        status: 'failed',
        text: 'error: timeout',
      });
    });

    const msgs = onMsg.mock.calls[onMsg.mock.calls.length - 1][1] as ChatMessage[];
    expect(msgs[msgs.length - 1].status).toBe('error');
  });

  it('clears waiting state when ingest cannot restore connection identity', async () => {
    const onMsg = vi.fn();
    const conv = makeConversation();
    const { result } = renderHook(() =>
      useAgUiEvents({ conversation: conv, onMessagesChange: onMsg }),
    );
    await vi.advanceTimersByTimeAsync(10);
    act(() => getLastWs().simulateOpen());
    act(() => result.current.sendMessage('Hello'));
    expect(result.current.isAwaitingReply).toBe(true);

    const message = 'Chat could not restore your sign-in session. Please reconnect and retry.';
    act(() => getLastWs().simulateMessage({
      type: 'response',
      status: 'failed',
      code: 'connection_identity_unavailable',
      content: message,
      error: message,
      // No task_id: identity failure happens before a task can be dispatched.
    }));

    expect(result.current.isAwaitingReply).toBe(false);
    const msgs = onMsg.mock.calls[onMsg.mock.calls.length - 1][1] as ChatMessage[];
    expect(msgs[msgs.length - 1].status).toBe('error');
    expect(msgs[msgs.length - 1].errorReason).toBe(message);
    expect(msgs[msgs.length - 1].content).toBe(message);
  });

  it('handles legacy progress heartbeat frame', async () => {
    const onMsg = vi.fn();
    const conv = makeConversation();
    renderHook(() => useAgUiEvents({ conversation: conv, onMessagesChange: onMsg }));

    await vi.advanceTimersByTimeAsync(10);
    act(() => getLastWs().simulateOpen());

    act(() => {
      getLastWs().simulateMessage({
        type: 'progress',
        task_id: 'task-1',
        kind: 'heartbeat',
      });
    });

    const msgs = onMsg.mock.calls[onMsg.mock.calls.length - 1][1] as ChatMessage[];
    expect(msgs[msgs.length - 1].role).toBe('assistant');
    expect(msgs[msgs.length - 1].status).toBe('streaming');
  });

  it('handles legacy progress tool_use frame — keeps indicator alive', async () => {
    // Tool name/input aren't on the legacy wire (see response router).
    // For rich tool rendering the worker emits AG-UI TOOL_CALL_* events.
    // Legacy tool_use just keeps the typing indicator alive.
    const onMsg = vi.fn();
    const conv = makeConversation();
    renderHook(() => useAgUiEvents({ conversation: conv, onMessagesChange: onMsg }));

    await vi.advanceTimersByTimeAsync(10);
    act(() => getLastWs().simulateOpen());

    act(() => {
      getLastWs().simulateMessage({
        type: 'progress',
        task_id: 'task-1',
        kind: 'tool_use',
      });
    });

    const msgs = onMsg.mock.calls[onMsg.mock.calls.length - 1][1] as ChatMessage[];
    const msg = msgs[msgs.length - 1];
    expect(msg.role).toBe('assistant');
    expect(msg.status).toBe('streaming');
  });

  // ----- Chunked legacy response -----

  it('handles legacy chunked response', async () => {
    const onMsg = vi.fn();
    const conv = makeConversation();
    const { result } = renderHook(() =>
      useAgUiEvents({ conversation: conv, onMessagesChange: onMsg }),
    );

    await vi.advanceTimersByTimeAsync(10);
    act(() => getLastWs().simulateOpen());

    // Chunk 1 of 2
    act(() => {
      getLastWs().simulateMessage({
        type: 'response',
        task_id: 'task-chunk',
        status: 'completed',
        text: 'first half ',
        chunk_index: 1,
        chunk_total: 2,
      });
    });

    // Chunk 2 of 2
    act(() => {
      getLastWs().simulateMessage({
        type: 'response',
        task_id: 'task-chunk',
        status: 'completed',
        text: 'second half',
        chunk_index: 2,
        chunk_total: 2,
      });
    });

    expect(result.current.isAwaitingReply).toBe(false);
    const msgs = onMsg.mock.calls[onMsg.mock.calls.length - 1][1] as ChatMessage[];
    const reply = msgs[msgs.length - 1];
    expect(reply.content).toBe('first half second half');
    expect(reply.status).toBe('complete');
  });

  // ----- Send message -----

  it('sends message over WebSocket', async () => {
    const onMsg = vi.fn();
    const conv = makeConversation();
    const { result } = renderHook(() =>
      useAgUiEvents({ conversation: conv, onMessagesChange: onMsg }),
    );

    await vi.advanceTimersByTimeAsync(10);
    act(() => getLastWs().simulateOpen());

    act(() => result.current.sendMessage('Hello agent'));

    const ws = getLastWs();
    expect(ws.sent).toHaveLength(1);
    const sent = JSON.parse(ws.sent[0]);
    expect(sent.action).toBe('sendMessage');
    // Ingest Lambda reads `text`, not `message` — regression guard.
    expect(sent.text).toBe('Hello agent');
    expect(sent.message).toBeUndefined();
    expect(sent.session_id).toBe('test-session');

    // User message added to conversation
    const msgs = onMsg.mock.calls[onMsg.mock.calls.length - 1][1] as ChatMessage[];
    expect(msgs[msgs.length - 1].role).toBe('user');
    expect(msgs[msgs.length - 1].content).toBe('Hello agent');
  });

  // ----- Reconnect -----

  it('auto-reconnects with backoff on unexpected close', async () => {
    const onMsg = vi.fn();
    const conv = makeConversation();
    const { result } = renderHook(() =>
      useAgUiEvents({ conversation: conv, onMessagesChange: onMsg }),
    );

    await vi.advanceTimersByTimeAsync(10);
    act(() => getLastWs().simulateOpen());
    expect(result.current.connectionStatus).toBe('connected');

    // Unexpected close
    act(() => getLastWs().simulateClose(1006));
    expect(result.current.connectionStatus).toBe('reconnecting');
    expect(result.current.reconnectAttempt).toBe(1);

    // Wait for backoff (1s)
    await vi.advanceTimersByTimeAsync(1100);
    expect(MockWebSocket.instances.length).toBeGreaterThan(1);
  });

  // ----- Mixed AG-UI and legacy -----

  it('handles interleaved AG-UI and legacy frames correctly', async () => {
    const onMsg = vi.fn();
    const conv = makeConversation();
    const { result } = renderHook(() =>
      useAgUiEvents({ conversation: conv, onMessagesChange: onMsg }),
    );

    await vi.advanceTimersByTimeAsync(10);
    act(() => getLastWs().simulateOpen());

    // Legacy notification
    act(() => {
      getLastWs().simulateMessage({
        type: 'notification',
        task_id: 'task-1',
        message: 'Queued',
      });
    });

    // AG-UI RUN_STARTED
    act(() => {
      getLastWs().simulateMessage(agUiFrame({
        event_type: AgUiEventType.RUN_STARTED,
        threadId: 'test-session',
        runId: 'task-1',
      }));
    });

    expect(result.current.isAwaitingReply).toBe(true);

    // AG-UI text content
    act(() => {
      getLastWs().simulateMessage(agUiFrame({
        event_type: AgUiEventType.TEXT_MESSAGE_START,
        messageId: 'msg-1',
        role: 'assistant',
      }));
    });

    act(() => {
      getLastWs().simulateMessage(agUiFrame({
        event_type: AgUiEventType.TEXT_MESSAGE_CONTENT,
        messageId: 'msg-1',
        delta: 'AG-UI reply',
      }));
    });

    act(() => {
      getLastWs().simulateMessage(agUiFrame({
        event_type: AgUiEventType.RUN_FINISHED,
        threadId: 'test-session',
        runId: 'task-1',
      }));
    });

    expect(result.current.isAwaitingReply).toBe(false);
    const msgs = onMsg.mock.calls[onMsg.mock.calls.length - 1][1] as ChatMessage[];
    // Should have: system (notification) + assistant (AG-UI reply)
    const systemMsg = msgs.find(m => m.role === 'system');
    const assistantMsg = msgs.find(m => m.role === 'assistant');
    expect(systemMsg?.content).toBe('Queued');
    expect(assistantMsg?.content).toBe('AG-UI reply');
    expect(assistantMsg?.status).toBe('complete');
  });

  // ----- Unknown AG-UI event types -----

  it('ignores unknown AG-UI event types without error', async () => {
    const onMsg = vi.fn();
    const conv = makeConversation();
    renderHook(() => useAgUiEvents({ conversation: conv, onMessagesChange: onMsg }));

    await vi.advanceTimersByTimeAsync(10);
    act(() => getLastWs().simulateOpen());

    // Unknown event type
    act(() => {
      getLastWs().simulateMessage(agUiFrame({
        event_type: 'FUTURE_EVENT_TYPE',
        someField: 'value',
      }));
    });

    // Should not throw, should not add messages
    expect(onMsg).not.toHaveBeenCalled();
  });

  // ----- Out-of-order events -----

  it('handles TEXT_MESSAGE_CONTENT before TEXT_MESSAGE_START', async () => {
    const onMsg = vi.fn();
    const conv = makeConversation();
    renderHook(() => useAgUiEvents({ conversation: conv, onMessagesChange: onMsg }));

    await vi.advanceTimersByTimeAsync(10);
    act(() => getLastWs().simulateOpen());

    // Content arrives before start (edge case with out-of-order delivery)
    act(() => {
      getLastWs().simulateMessage(agUiFrame({
        event_type: AgUiEventType.TEXT_MESSAGE_CONTENT,
        messageId: 'msg-ooo',
        delta: 'Early content',
      }));
    });

    const msgs = onMsg.mock.calls[onMsg.mock.calls.length - 1][1] as ChatMessage[];
    const bubble = msgs[msgs.length - 1];
    expect(bubble.role).toBe('assistant');
    expect(bubble.content).toBe('Early content');
    expect(bubble.agUiMessageId).toBe('msg-ooo');
  });

  // ----- RUN_ERROR with existing streaming bubble -----

  it('RUN_ERROR converts streaming bubble to error', async () => {
    const onMsg = vi.fn();
    const conv = makeConversation();
    renderHook(() => useAgUiEvents({ conversation: conv, onMessagesChange: onMsg }));

    await vi.advanceTimersByTimeAsync(10);
    act(() => getLastWs().simulateOpen());

    // Create streaming bubble
    act(() => {
      getLastWs().simulateMessage(agUiFrame({
        event_type: AgUiEventType.TEXT_MESSAGE_START,
        messageId: 'msg-err',
        role: 'assistant',
      }));
    });

    act(() => {
      getLastWs().simulateMessage(agUiFrame({
        event_type: AgUiEventType.TEXT_MESSAGE_CONTENT,
        messageId: 'msg-err',
        delta: 'Partial reply...',
      }));
    });

    // Error while streaming
    act(() => {
      getLastWs().simulateMessage(agUiFrame({
        event_type: AgUiEventType.RUN_ERROR,
        message: 'Connection timeout',
        code: 'TIMEOUT',
      }));
    });

    const msgs = onMsg.mock.calls[onMsg.mock.calls.length - 1][1] as ChatMessage[];
    const errMsg = msgs[msgs.length - 1];
    expect(errMsg.status).toBe('error');
    expect(errMsg.content).toBe('Partial reply...');
    expect(errMsg.errorReason).toBe('Connection timeout');
  });

  // ----- #5615: a session identifier the server will not accept -----
  //
  // Since the ingress stopped creating a conversation for any identifier it is
  // handed, an identifier can now be refused. The common cause is benign and
  // has nothing to do with attack: the sessions row expires after 24h while
  // this browser keeps the id in localStorage indefinitely, so a user returning
  // the next day names a row that is genuinely gone.
  //
  // The refusal arrives as a pushed frame because API Gateway discards a
  // WebSocket integration's return body — without the frame the page would spin
  // forever on a turn that will never run.

  describe('session_invalid', () => {
    const invalidFrame = {
      type: 'session_invalid',
      session_id: 'test-session',
      error: 'session not found',
      content: 'That conversation is no longer available. Starting a new one.',
    };

    it('stops the spinner instead of waiting for a reply that cannot come', async () => {
      const onMsg = vi.fn();
      const conv = makeConversation();
      const { result } = renderHook(() =>
        useAgUiEvents({ conversation: conv, onMessagesChange: onMsg }),
      );
      await vi.advanceTimersByTimeAsync(10);
      act(() => getLastWs().simulateOpen());
      act(() => result.current.sendMessage('Are you there?'));
      expect(result.current.isAwaitingReply).toBe(true);

      act(() => getLastWs().simulateMessage(invalidFrame));

      expect(result.current.isAwaitingReply).toBe(false);
      expect(result.current.sessionExpired).toBe(true);
    });

    it('tells the user in the transcript why nothing happened', async () => {
      const onMsg = vi.fn();
      const conv = makeConversation();
      renderHook(() => useAgUiEvents({ conversation: conv, onMessagesChange: onMsg }));
      await vi.advanceTimersByTimeAsync(10);
      act(() => getLastWs().simulateOpen());

      act(() => getLastWs().simulateMessage(invalidFrame));

      const msgs = onMsg.mock.calls[onMsg.mock.calls.length - 1][1] as ChatMessage[];
      const last = msgs[msgs.length - 1];
      expect(last.role).toBe('system');
      expect(last.status).toBe('error');
      expect(last.content).toBe(invalidFrame.content);
    });

    it('refuses to send further messages on a refused identifier', async () => {
      /*
       * The refusal is terminal for this id. Retrying on it would be a
       * guaranteed-refused round-trip, and — more importantly — the page must
       * not drift into treating an unknown id as retryable, which is the
       * behaviour that made squatting possible in the first place.
       */
      const onMsg = vi.fn();
      const conv = makeConversation();
      const { result } = renderHook(() =>
        useAgUiEvents({ conversation: conv, onMessagesChange: onMsg }),
      );
      await vi.advanceTimersByTimeAsync(10);
      act(() => getLastWs().simulateOpen());
      act(() => getLastWs().simulateMessage(invalidFrame));

      const sentBefore = getLastWs().sent.length;
      act(() => result.current.sendMessage('Please try again'));

      expect(getLastWs().sent).toHaveLength(sentBefore);
      expect(result.current.isAwaitingReply).toBe(false);
    });

    it('does not repeat the notice if the server refuses more than once', async () => {
      const onMsg = vi.fn();
      const conv = makeConversation();
      renderHook(() => useAgUiEvents({ conversation: conv, onMessagesChange: onMsg }));
      await vi.advanceTimersByTimeAsync(10);
      act(() => getLastWs().simulateOpen());

      act(() => getLastWs().simulateMessage(invalidFrame));
      act(() => getLastWs().simulateMessage(invalidFrame));

      const msgs = onMsg.mock.calls[onMsg.mock.calls.length - 1][1] as ChatMessage[];
      expect(msgs.filter((m) => m.content === invalidFrame.content)).toHaveLength(1);
    });

    it('clears the refusal when the user moves to another conversation', async () => {
      /*
       * `sessionExpired` describes one identifier, not the tab. If it survived a
       * conversation switch, a user whose old conversation expired would find
       * the composer dead in a perfectly valid new one.
       */
      const onMsg = vi.fn();
      const expired = makeConversation('sess-expired-one');
      const { result, rerender } = renderHook(
        ({ conv }) => useAgUiEvents({ conversation: conv, onMessagesChange: onMsg }),
        { initialProps: { conv: expired as Conversation | null } },
      );
      await vi.advanceTimersByTimeAsync(10);
      act(() => getLastWs().simulateOpen());
      act(() => getLastWs().simulateMessage({ ...invalidFrame, session_id: expired.id }));
      expect(result.current.sessionExpired).toBe(true);

      rerender({ conv: makeConversation('sess-freshly-issued') });
      await vi.advanceTimersByTimeAsync(10);

      expect(result.current.sessionExpired).toBe(false);
    });

    it('carries no hint about whether the conversation ever existed', async () => {
      /*
       * The server answers identically for an expired id and for someone else's
       * (#5742's anti-enumeration non-answer). The hook must not enrich it —
       * deriving "expired" vs "not yours" here would rebuild the oracle the
       * server refuses to be, in the client.
       */
      const onMsg = vi.fn();
      const conv = makeConversation();
      renderHook(() => useAgUiEvents({ conversation: conv, onMessagesChange: onMsg }));
      await vi.advanceTimersByTimeAsync(10);
      act(() => getLastWs().simulateOpen());

      act(() => getLastWs().simulateMessage(invalidFrame));

      const msgs = onMsg.mock.calls[onMsg.mock.calls.length - 1][1] as ChatMessage[];
      const shown = msgs[msgs.length - 1].content.toLowerCase();
      expect(shown).not.toMatch(/expired|another user|other user|belongs to|forbidden/);
    });
  });
});
