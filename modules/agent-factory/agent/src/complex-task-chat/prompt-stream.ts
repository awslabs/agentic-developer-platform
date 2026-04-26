/**
 * Render history + new user ask as one plain transcript, yielded as a single
 * user message. Simplest possible shape the SDK will accept.
 *
 * The SDK's query() prompt is `string | AsyncIterable<SDKUserMessage>` — no
 * real multi-role messages array. So we build a transcript with `[user] / `
 * `[assistant]` headers and pass it as one user-role chunk. The model reads
 * that as a normal chat transcript (a shape it's seen in training a million
 * times) and answers the last `[user]` turn.
 *
 * No XML tags, no anti-bleed directives, no "pivot on new topic" rules. If
 * the transcript ends with a new user question, the model answers it. If it
 * refers back to an earlier turn, the model uses the earlier turns as
 * context. That is the natural behaviour we want.
 */
import { SDKMessage } from './context/types';

const STREAM_SESSION_ID = 'chat-agent-stream';

export interface PromptChunk {
  type: 'user';
  message: { role: 'user'; content: string };
  parent_tool_use_id: null;
  session_id: string;
}

export async function* buildPromptStream(
  history: SDKMessage[],
  userMessage: string,
): AsyncIterable<PromptChunk> {
  const lines: string[] = [];
  for (const m of history) {
    const role = m.role === 'assistant' ? '[assistant]' : '[user]';
    lines.push(`${role}\n${sanitize(m.content)}`);
  }
  lines.push(`[user]\n${sanitize(userMessage)}`);

  yield {
    type: 'user',
    message: { role: 'user', content: lines.join('\n\n') },
    parent_tool_use_id: null,
    session_id: STREAM_SESSION_ID,
  };
}

/**
 * Strip role markers from inside user/assistant content so a user cannot
 * inject a fake turn by writing `[assistant]\n...` into their own message.
 * Replaces the leading marker with a visible escape; preserves the character
 * count so nothing else shifts.
 */
function sanitize(text: string): string {
  return text.replace(/^\s*\[(user|assistant)\]/gim, '(\\$1)');
}
