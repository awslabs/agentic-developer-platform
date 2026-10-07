import { describe, expect, it } from 'vitest';
import { terminalChatResponse } from '@/hooks/terminalChatResponse';
import type { ChatMessage, WsResponseFrame } from '@/types/chat';

const frame: WsResponseFrame = {
  type: 'response', terminal_delivery: true, delivery_id: `chat-terminal-${'a'.repeat(64)}`,
  session_id: 'session-a', task_id: 'task-a', status: 'completed', content: 'Saved reply',
  retryable: false, accounting_status: 'settled',
};
const streaming: ChatMessage = { id: 'bubble', taskId: 'task-a', role: 'assistant', status: 'streaming', content: 'partial', timestamp: 1 };

describe('protected terminal responses', () => {
  it.each(['completed', 'failed', 'cancelled', 'interrupted'] as const)('finishes %s once, including after local history is restored', status => {
    const result = terminalChatResponse({ ...frame, status }, 'session-a', [streaming], new Map())!;
    expect(result).toHaveLength(1);
    expect(result[0]).toMatchObject({
      id: 'bubble', taskId: 'task-a', content: 'Saved reply', terminalOutcome: status,
      terminalDeliveryId: frame.delivery_id, status: ['failed', 'interrupted'].includes(status) ? 'error' : 'complete',
    });
    expect(terminalChatResponse({ ...frame, status }, 'session-a', JSON.parse(JSON.stringify(result)), new Map())).toBeNull();
  });

  it('assembles reordered and duplicate chunks without finalizing partial text', () => {
    const chunks = new Map<string, string[]>();
    const chunked = { ...frame, chunk_total: 3 };
    expect(terminalChatResponse({ ...chunked, chunk_index: 3, content: 'last' }, 'session-a', [streaming], chunks)).toBeNull();
    expect(terminalChatResponse({ ...chunked, chunk_index: 1, content: 'first ' }, 'session-a', [streaming], chunks)).toBeNull();
    expect(terminalChatResponse({ ...chunked, chunk_index: 1, content: 'first ' }, 'session-a', [streaming], chunks)).toBeNull();
    const result = terminalChatResponse({ ...chunked, chunk_index: 2, content: 'middle ' }, 'session-a', [streaming], chunks)!;
    expect(result[0].content).toBe('first middle last');
    expect(chunks.size).toBe(0);
  });

  it('never replaces another task’s streaming bubble', () => {
    const newer = { ...streaming, taskId: 'newer-task' };
    const result = terminalChatResponse(frame, 'session-a', [newer], new Map(), 'newer-task')!;
    expect(result).toHaveLength(2);
    expect(result[0]).toEqual(newer);
  });

  it.each([
    { session_id: 'other-session' }, { delivery_id: 'invalid' }, { status: 'notification' },
    { retryable: true }, { accounting_status: 'invented' }, { chunk_total: 129 },
    { chunk_total: 2, chunk_index: 3 }, { terminal_delivery: false },
  ])('refuses malformed terminal fields: %j', changes => {
    expect(terminalChatResponse({ ...frame, ...changes } as WsResponseFrame, 'session-a', [streaming], new Map())).toBeNull();
  });
});
