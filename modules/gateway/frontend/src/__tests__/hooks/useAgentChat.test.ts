/**
 * Unit tests for the useAgentChat hook.
 *
 * Issue #97 Phase 1: verifies frame handling, chunk reassembly,
 * auto-reconnect backoff, and error handling.
 *
 * Uses a mock WebSocket implementation to avoid real network calls.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import { useAgentChat } from '@/hooks/useAgentChat';
import type { Conversation, ChatMessage } from '@/types/chat';

// ---------------------------------------------------------------------------
// Mock WebSocket
// ---------------------------------------------------------------------------

class MockWebSocket {
  static instances: MockWebSocket[] = [];
  static OPEN = 1;
  static CLOSED = 3;

  url: string;
  readyState = 0; // CONNECTING
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

  // Test helpers
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

function latestWs(): MockWebSocket {
  return MockWebSocket.instances[MockWebSocket.instances.length - 1];
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('useAgentChat', () => {
  let onMessagesChange: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    MockWebSocket.instances = [];
    onMessagesChange = vi.fn();
    vi.stubGlobal('WebSocket', MockWebSocket);
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  // -----------------------------------------------------------------------
  // Connection lifecycle
  // -----------------------------------------------------------------------

  it('connects when conversation is provided', async () => {
    const conv = makeConversation();
    renderHook(() => useAgentChat({ conversation: conv, onMessagesChange }));

    // Allow async connect
    await act(async () => { await Promise.resolve(); });

    expect(MockWebSocket.instances).toHaveLength(1);
    expect(latestWs().url).toContain('token=mock-id-token');
  });

  it('reports connected status after WS open', async () => {
    const conv = makeConversation();
    const { result } = renderHook(() => useAgentChat({ conversation: conv, onMessagesChange }));

    await act(async () => { await Promise.resolve(); });
    act(() => latestWs().simulateOpen());

    expect(result.current.connectionStatus).toBe('connected');
  });

  // -----------------------------------------------------------------------
  // Notification frames
  // -----------------------------------------------------------------------

  it('handles notification frames as system messages', async () => {
    const conv = makeConversation();
    renderHook(() => useAgentChat({ conversation: conv, onMessagesChange }));

    await act(async () => { await Promise.resolve(); });
    act(() => latestWs().simulateOpen());

    act(() => {
      latestWs().simulateMessage({
        type: 'notification',
        task_id: 't1',
        session_id: 'test-session',
        message: 'Task queued',
      });
    });

    expect(onMessagesChange).toHaveBeenCalled();
    const msgs = onMessagesChange.mock.calls[0][1] as ChatMessage[];
    expect(msgs).toHaveLength(1);
    expect(msgs[0].role).toBe('system');
    expect(msgs[0].content).toBe('Task queued');
  });

  // -----------------------------------------------------------------------
  // Progress frames — heartbeat
  // -----------------------------------------------------------------------

  it('creates a streaming bubble on heartbeat when no pending bubble exists', async () => {
    const conv = makeConversation();
    renderHook(() => useAgentChat({ conversation: conv, onMessagesChange }));

    await act(async () => { await Promise.resolve(); });
    act(() => latestWs().simulateOpen());

    act(() => {
      latestWs().simulateMessage({
        type: 'progress',
        kind: 'heartbeat',
        task_id: 't1',
        session_id: 'test-session',
      });
    });

    const msgs = onMessagesChange.mock.calls.at(-1)![1] as ChatMessage[];
    expect(msgs.at(-1)?.role).toBe('assistant');
    expect(msgs.at(-1)?.status).toBe('streaming');
    expect(msgs.at(-1)?.content).toBe('');
  });

  // -----------------------------------------------------------------------
  // Progress frames — tool_use
  // -----------------------------------------------------------------------

  it('keeps the typing indicator alive on tool_use progress', async () => {
    // Tool-name/input aren't on the wire today — the server only emits
    // kind=tool_use. The hook treats every progress kind as "keep indicator
    // alive". Once the response Lambda forwards tool metadata, update this
    // test to assert toolUse is populated.
    const conv = makeConversation();
    renderHook(() => useAgentChat({ conversation: conv, onMessagesChange }));

    await act(async () => { await Promise.resolve(); });
    act(() => latestWs().simulateOpen());

    act(() => {
      latestWs().simulateMessage({
        type: 'progress',
        kind: 'tool_use',
        task_id: 't1',
        session_id: 'test-session',
      });
    });

    const msgs = onMessagesChange.mock.calls.at(-1)![1] as ChatMessage[];
    expect(msgs.at(-1)?.role).toBe('assistant');
    expect(msgs.at(-1)?.status).toBe('streaming');
  });

  // -----------------------------------------------------------------------
  // Response frames — single completed
  // -----------------------------------------------------------------------

  it('finalizes assistant bubble on completed response', async () => {
    const conv = makeConversation();
    renderHook(() => useAgentChat({ conversation: conv, onMessagesChange }));

    await act(async () => { await Promise.resolve(); });
    act(() => latestWs().simulateOpen());

    act(() => {
      latestWs().simulateMessage({
        type: 'response',
        status: 'completed',
        task_id: 't1',
        session_id: 'test-session',
        text: 'Hello world!',
      });
    });

    const msgs = onMessagesChange.mock.calls.at(-1)![1] as ChatMessage[];
    const last = msgs.at(-1)!;
    expect(last.role).toBe('assistant');
    expect(last.status).toBe('complete');
    expect(last.content).toBe('Hello world!');
  });

  // -----------------------------------------------------------------------
  // Response frames — failed
  // -----------------------------------------------------------------------

  it('creates error bubble on failed response', async () => {
    const conv = makeConversation();
    const { result } = renderHook(() => useAgentChat({ conversation: conv, onMessagesChange }));

    await act(async () => { await Promise.resolve(); });
    act(() => latestWs().simulateOpen());

    act(() => {
      latestWs().simulateMessage({
        type: 'response',
        status: 'failed',
        task_id: 't1',
        session_id: 'test-session',
        reason: 'Model overloaded',
      });
    });

    const msgs = onMessagesChange.mock.calls.at(-1)![1] as ChatMessage[];
    const last = msgs.at(-1)!;
    expect(last.status).toBe('error');
    expect(last.errorReason).toBe('Model overloaded');
    expect(result.current.isAwaitingReply).toBe(false);
  });

  // -----------------------------------------------------------------------
  // Response frames — chunked reassembly
  // -----------------------------------------------------------------------

  it('reassembles chunked responses by task_id', async () => {
    // Server sends 1-based chunk_index with status=completed on every chunk
    // (see response/routers/websocket.py _split_content). This test also
    // arrives out of order (1, 3, 2) to prove the buffer waits for every
    // slot before finalizing.
    const conv = makeConversation();
    renderHook(() => useAgentChat({ conversation: conv, onMessagesChange }));

    await act(async () => { await Promise.resolve(); });
    act(() => latestWs().simulateOpen());

    // Chunk 1 of 3
    act(() => {
      latestWs().simulateMessage({
        type: 'response',
        status: 'completed',
        task_id: 't1',
        session_id: 'test-session',
        text: 'Part1',
        chunk_index: 1,
        chunk_total: 3,
      });
    });

    // Chunk 3 of 3 — arrives out of order
    act(() => {
      latestWs().simulateMessage({
        type: 'response',
        status: 'completed',
        task_id: 't1',
        session_id: 'test-session',
        text: 'Part3',
        chunk_index: 3,
        chunk_total: 3,
      });
    });

    // Chunk 2 of 3 — the last missing piece, should trigger finalization
    act(() => {
      latestWs().simulateMessage({
        type: 'response',
        status: 'completed',
        task_id: 't1',
        session_id: 'test-session',
        text: 'Part2',
        chunk_index: 2,
        chunk_total: 3,
      });
    });

    const msgs = onMessagesChange.mock.calls.at(-1)![1] as ChatMessage[];
    const last = msgs.at(-1)!;
    expect(last.content).toBe('Part1Part2Part3');
    expect(last.status).toBe('complete');
  });

  // -----------------------------------------------------------------------
  // Send message
  // -----------------------------------------------------------------------

  it('sends message over WS and adds user bubble', async () => {
    const conv = makeConversation();
    const { result } = renderHook(() => useAgentChat({ conversation: conv, onMessagesChange }));

    await act(async () => { await Promise.resolve(); });
    act(() => latestWs().simulateOpen());

    act(() => result.current.sendMessage('Hello'));

    expect(latestWs().sent).toHaveLength(1);
    const sent = JSON.parse(latestWs().sent[0]);
    expect(sent.action).toBe('sendMessage');
    // Ingest Lambda reads `text`, not `message` — regression guard for the
    // field-name bug that silently drops every message.
    expect(sent.text).toBe('Hello');
    expect(sent.message).toBeUndefined();
    expect(sent.session_id).toBe('test-session');

    const msgs = onMessagesChange.mock.calls.at(-1)![1] as ChatMessage[];
    expect(msgs.at(-1)?.role).toBe('user');
    expect(msgs.at(-1)?.content).toBe('Hello');
    expect(result.current.isAwaitingReply).toBe(true);
  });

  // -----------------------------------------------------------------------
  // Auto-reconnect
  // -----------------------------------------------------------------------

  it('auto-reconnects with exponential backoff on unexpected close', async () => {
    const conv = makeConversation();
    const { result } = renderHook(() => useAgentChat({ conversation: conv, onMessagesChange }));

    await act(async () => { await Promise.resolve(); });
    const ws1 = latestWs();
    act(() => ws1.simulateOpen());
    expect(result.current.connectionStatus).toBe('connected');

    // Unexpected close
    act(() => ws1.simulateClose(1006));

    expect(result.current.connectionStatus).toBe('reconnecting');
    expect(result.current.reconnectAttempt).toBe(1);

    // Advance past first backoff (1s)
    await act(async () => { vi.advanceTimersByTime(1100); await Promise.resolve(); });

    // A new WS should have been created
    expect(MockWebSocket.instances).toHaveLength(2);
  });

  it('gives up after MAX_RECONNECT_ATTEMPTS', async () => {
    const conv = makeConversation();
    const { result } = renderHook(() => useAgentChat({ conversation: conv, onMessagesChange }));

    await act(async () => { await Promise.resolve(); });

    // First connection + close to start reconnect cycle
    act(() => latestWs().simulateOpen());
    act(() => latestWs().simulateClose(1006));

    // Each reconnect: advance timer to trigger connect, then close without opening
    // (simulating immediate connection failure) — this keeps reconnectAttemptRef incrementing
    for (let i = 0; i < 10; i++) {
      await act(async () => { vi.advanceTimersByTime(60_000); await Promise.resolve(); });
      const ws = latestWs();
      // Close immediately without opening (connection failure)
      act(() => ws.simulateClose(1006));
    }

    // After 10 failed reconnects + 1 initial, should give up
    expect(result.current.connectionStatus).toBe('disconnected');
  });

  // -----------------------------------------------------------------------
  // Content extraction priority
  // -----------------------------------------------------------------------

  it('extracts content using text > result > content priority', async () => {
    const conv = makeConversation();
    renderHook(() => useAgentChat({ conversation: conv, onMessagesChange }));

    await act(async () => { await Promise.resolve(); });
    act(() => latestWs().simulateOpen());

    // Frame with 'result' but no 'text'
    act(() => {
      latestWs().simulateMessage({
        type: 'response',
        status: 'completed',
        task_id: 't1',
        session_id: 'test-session',
        result: 'from-result',
      });
    });

    const msgs = onMessagesChange.mock.calls.at(-1)![1] as ChatMessage[];
    expect(msgs.at(-1)?.content).toBe('from-result');
  });
});
