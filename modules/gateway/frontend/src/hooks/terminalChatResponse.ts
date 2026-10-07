import type { ChatMessage, WsResponseFrame } from '@/types/chat';

export function terminalChatResponse(
  frame: WsResponseFrame,
  sessionId: string | null,
  messages: ChatMessage[],
  chunks: Map<string, string[]>,
  activeTaskId?: string | null,
): ChatMessage[] | null {
  if (frame.terminal_delivery !== true || frame.session_id !== sessionId || !frame.task_id ||
      !/^chat-terminal-[a-f0-9]{64}$/.test(frame.delivery_id ?? '') ||
      !['completed', 'failed', 'cancelled', 'interrupted'].includes(frame.status) ||
      typeof frame.retryable !== 'boolean' || (frame.retryable && frame.status !== 'interrupted') ||
      !['not_used', 'settled', 'unresolved'].includes(frame.accounting_status ?? '') ||
      typeof frame.content !== 'string' || frame.content.length > 131_072 ||
      messages.some(message => message.taskId === frame.task_id && message.terminalDeliveryId)) return null;
  const total = frame.chunk_total ?? 1;
  const index = frame.chunk_index ?? 1;
  if (!Number.isInteger(total) || total < 1 || total > 128 ||
      !Number.isInteger(index) || index < 1 || index > total) return null;
  const key = `terminal:${frame.delivery_id}`;
  const buffer = chunks.get(key) ?? new Array<string>(total);
  if (buffer.length !== total || (buffer[index - 1] !== undefined && buffer[index - 1] !== frame.content)) return null;
  buffer[index - 1] = frame.content;
  if (buffer.join('').length > 131_072) {
    chunks.delete(key);
    return null;
  }
  chunks.set(key, buffer);
  if (Array.from({ length: total }, (_, position) => buffer[position]).some(value => value === undefined)) return null;
  chunks.delete(key);
  const content = buffer.join('');
  const failed = frame.status === 'failed' || frame.status === 'interrupted';
  const result: ChatMessage = {
    id: frame.delivery_id!, role: 'assistant', taskId: frame.task_id,
    content, status: failed ? 'error' : 'complete', timestamp: Date.now(),
    terminalDeliveryId: frame.delivery_id, terminalOutcome: frame.status as ChatMessage['terminalOutcome'],
    retryable: frame.retryable, toolUse: null, toolCalls: [],
    ...(failed ? { errorReason: content } : {}),
  };
  let matching = -1;
  for (let position = messages.length - 1; position >= 0; position--) {
    const message = messages[position];
    if (message.role === 'assistant' && (message.taskId === frame.task_id ||
        (!message.taskId && message.status === 'streaming' && activeTaskId === frame.task_id))) {
      matching = position;
      break;
    }
  }
  if (matching < 0) return [...messages, result];
  return messages.map((message, position) => position === matching ? { ...result, id: message.id } : message);
}
