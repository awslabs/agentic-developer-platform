import type { ExplanationEvents } from './explanation-events';

/** Project public SDK blocks into the shared feed; never forward tool results or thinking. */
export class ClaudeProgress {
  private tools = new Map<string, string>();
  private messageId = '';
  private blocks = new Map<number, string>();
  constructor(private events: ExplanationEvents) {}
  observe(message: any): void {
    if (message.type === 'stream_event') {
      const event = message.event;
      if (event?.type === 'message_start') {
        this.messageId = event.message.id;
        this.blocks.clear();
      }
      if (!this.messageId) return;
      if (event?.type === 'content_block_start' && event.content_block.type === 'text') {
        this.blocks.set(event.index, event.content_block.text || '');
      }
      if (event?.type === 'content_block_delta' && event.delta.type === 'text_delta' && this.blocks.has(event.index)) {
        const text = (this.blocks.get(event.index)! + event.delta.text).slice(0, 16384);
        this.blocks.set(event.index, text);
        // Send complete lines/sentences, not arbitrary fragments of a credential.
        const boundary = Math.max(text.lastIndexOf('\n'), text.lastIndexOf('. '));
        if (boundary >= 0) this.events.publish(text.slice(0, boundary + 1), {
          id: `${this.messageId}:${event.index}`, category: 'message', state: 'running',
        });
      }
      return;
    }
    if (message.type === 'assistant' && Array.isArray(message.message?.content)) {
      message.message.content.forEach((block: any, index: number) => {
        if (block.type === 'text' && typeof block.text === 'string') this.events.publish(block.text, {
          id: `${message.message.id}:${index}`, category: 'message', state: 'completed',
        });
        if (block.type === 'tool_use') {
          this.tools.set(block.id, block.name);
          if (this.tools.size > 128) this.tools.delete(this.tools.keys().next().value!);
          this.events.publish(`Running: ${block.name}`, { id: block.id, category: 'tool', state: 'running' });
        }
      });
    }
    if (message.type === 'user' && Array.isArray(message.message?.content)) {
      for (const block of message.message.content) {
        if (block.type === 'tool_result') this.events.publish(`${this.tools.get(block.tool_use_id) ?? 'Tool'} ${block.is_error ? 'failed' : 'completed'}`, {
          id: block.tool_use_id, category: 'tool', state: block.is_error ? 'failed' : 'completed',
        });
      }
    }
  }
}
