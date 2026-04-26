/**
 * Prompt stream — frames conversation history as synthetic user-role SDK stream
 * messages and appends the new user ask with an anti-bleed directive.
 *
 * Each past user/assistant pair is wrapped in <past-exchange>. The new ask is
 * the final yield, preceded by a directive that:
 *   (a) tells the model past exchanges are reference-only, available for
 *       explicit backreferences but not to continue/repeat/extend;
 *   (b) tells the model to pivot completely when the new message opens a new
 *       topic (don't carry over format/structure like an active quiz).
 *
 * Prior framings didn't hold:
 *   - Raw yields made the model treat N historical user messages as N stacked
 *     live asks (e.g. "what time?" after earlier research triggered both).
 *   - <prior-turn> tags + "address only the current message" (PR #121, #151)
 *     still let the model carry over structure — a quiz history continued as
 *     a quiz, a video-editing ask got answered with stale time content.
 *
 * The new framing puts the directive IMMEDIATELY BEFORE the user message
 * (tail of prompt — recency bias favours tail instructions) and spells out
 * the rule as positive+negative+exception, not a single "address only" hint.
 */
import { SDKMessage } from './context/types';

/** Sentinel session id used for the synthetic stream; the SDK ignores it. */
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
  const pending: string[] = [];

  const flushUser = function* (text: string) {
    yield {
      type: 'user' as const,
      message: { role: 'user' as const, content: text },
      parent_tool_use_id: null,
      session_id: STREAM_SESSION_ID,
    };
  };

  // Walk history: pair each user turn with the assistant turns that followed
  // and wrap them in <past-exchange>. Reference material — not active asks.
  let currentUserText: string | null = null;
  for (const m of history) {
    if (m.role === 'user') {
      if (currentUserText !== null) {
        const body =
          pending.length > 0
            ? `<past-user>\n${currentUserText}\n</past-user>\n<past-assistant>\n${pending.join('\n\n')}\n</past-assistant>`
            : `<past-user>\n${currentUserText}\n</past-user>`;
        yield* flushUser(`<past-exchange>\n${body}\n</past-exchange>`);
        pending.length = 0;
      }
      currentUserText = m.content;
    } else {
      pending.push(m.content);
    }
  }

  if (currentUserText !== null) {
    const body =
      pending.length > 0
        ? `<past-user>\n${currentUserText}\n</past-user>\n<past-assistant>\n${pending.join('\n\n')}\n</past-assistant>`
        : `<past-user>\n${currentUserText}\n</past-user>`;
    yield* flushUser(`<past-exchange>\n${body}\n</past-exchange>`);
  } else if (pending.length > 0) {
    yield* flushUser(`<past-assistant-only>\n${pending.join('\n\n')}\n</past-assistant-only>`);
  }

  // Final user turn with the anti-bleed directive. The directive sits
  // IMMEDIATELY BEFORE the ask so the model's recency bias favours the rule,
  // and the ask itself is the final visible text so the reply anchors on it.
  yield* flushUser(
    `The <past-exchange> blocks above are previous turns in this conversation. ` +
      `They are REFERENCE ONLY — read them to resolve explicit backreferences (e.g. ` +
      `"that first answer", "the 2nd option you listed"), but do NOT continue, repeat, ` +
      `or extend them on your own. Each new user message starts a fresh response ` +
      `unless it explicitly refers to a past turn.\n\n` +
      `The user's new message is below. Respond to THIS message. If it opens a new ` +
      `topic, pivot completely — do not carry over the prior topic's framing, format, ` +
      `or structure (e.g. if the prior turns were a quiz, do not keep asking quiz ` +
      `questions unless the new message asks for that).\n\n` +
      `<new-user-message>\n${userMessage}\n</new-user-message>`,
  );
}
