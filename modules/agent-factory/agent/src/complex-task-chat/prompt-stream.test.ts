/**
 * Tests for buildPromptStream — the history-framing contract.
 *
 * History bleed regression history:
 *   - Quiz session → "fetch a 2024 CS paper" → worker kept asking quiz
 *     questions (physics P=IV answer for a CS ask).
 *   - UK-time Q&A → "video editing tools?" → worker restated UK time.
 *
 * Both cases had framed history (PR #121) and pivot rules in the persona
 * (PR #151). The framing wasn't strong enough against structured histories
 * because the directive was weak ("address only") and sat BEFORE the new
 * message, reducing recency-bias leverage.
 *
 * These tests pin:
 *   1. History is wrapped in <past-exchange>, not treated as live asks.
 *   2. The directive sits IMMEDIATELY BEFORE the new message (so the tail
 *      of the prompt is the ask, with the rule one paragraph up).
 *   3. The directive explicitly names the failure modes we have observed
 *      ("continue, repeat, or extend"; "don't carry over format/structure
 *      like a quiz").
 *   4. Only-assistant histories still get framed as reference material.
 *   5. Empty history still emits the directive.
 */
import { buildPromptStream } from './prompt-stream';
import { SDKMessage } from './context/types';

async function collect(history: SDKMessage[], userMessage: string): Promise<string[]> {
  const out: string[] = [];
  for await (const chunk of buildPromptStream(history, userMessage)) {
    out.push(chunk.message.content);
  }
  return out;
}

describe('buildPromptStream', () => {
  it('wraps a user/assistant pair in <past-exchange> blocks', async () => {
    const chunks = await collect(
      [
        { role: 'user', content: 'what time is it in the UK?' },
        { role: 'assistant', content: 'It is 9:47 AM BST.' },
      ],
      'anything',
    );

    const past = chunks.find(c => c.includes('<past-exchange>'));
    expect(past).toBeDefined();
    expect(past).toContain('<past-user>\nwhat time is it in the UK?\n</past-user>');
    expect(past).toContain('<past-assistant>\nIt is 9:47 AM BST.\n</past-assistant>');
  });

  it('never uses the old <prior-turn> / <current-user-message> labels (renamed)', async () => {
    const chunks = await collect(
      [
        { role: 'user', content: 'prior ask' },
        { role: 'assistant', content: 'prior reply' },
      ],
      'new ask',
    );

    const joined = chunks.join('\n---\n');
    expect(joined).not.toContain('<prior-turn>');
    expect(joined).not.toContain('<prior-user-message>');
    expect(joined).not.toContain('<prior-assistant-response>');
    expect(joined).not.toContain('<current-user-message>');
  });

  it('puts the directive IMMEDIATELY BEFORE the new user message (tail of prompt)', async () => {
    const chunks = await collect(
      [
        { role: 'user', content: 'prior ask' },
        { role: 'assistant', content: 'prior reply' },
      ],
      'what about video editing?',
    );

    const last = chunks[chunks.length - 1];
    // Directive must sit above the new message; the new message must be the
    // final visible text so the reply anchors on the ask (recency bias).
    const directiveIdx = last.indexOf('REFERENCE ONLY');
    const newMsgIdx = last.indexOf('<new-user-message>');
    expect(directiveIdx).toBeGreaterThan(-1);
    expect(newMsgIdx).toBeGreaterThan(directiveIdx);

    expect(last.trimEnd().endsWith('</new-user-message>')).toBe(true);
    expect(last).toContain('what about video editing?');
  });

  it('directive spells out the failure modes we have actually seen', async () => {
    const chunks = await collect([{ role: 'user', content: 'earlier' }], 'new');
    const last = chunks[chunks.length - 1];

    // Anti-continuation — the "quiz keeps going" bug.
    expect(last).toMatch(/do NOT continue, repeat, or extend/i);

    // Anti-structure-carryover — the "kept asking quiz questions" bug.
    expect(last).toMatch(/do not carry over.*format/i);
    expect(last.toLowerCase()).toContain('quiz');

    // Allows explicit backreferences — users must be able to say
    // "expand on that first answer" without the model refusing.
    expect(last).toMatch(/backreference|refers|refer/i);
  });

  it('frames a history with only assistant messages as reference', async () => {
    const chunks = await collect(
      [{ role: 'assistant', content: 'an earlier assistant utterance' }],
      'new ask',
    );
    const joined = chunks.join('\n---\n');
    expect(joined).toContain('<past-assistant-only>');
    expect(joined).toContain('an earlier assistant utterance');
  });

  it('empty history still emits the new message with the directive', async () => {
    const chunks = await collect([], 'just the new ask');
    expect(chunks.length).toBe(1);
    const only = chunks[0];
    expect(only).toContain('<new-user-message>\njust the new ask\n</new-user-message>');
    expect(only).toContain('REFERENCE ONLY');
  });

  it('preserves order across multiple user/assistant pairs', async () => {
    const chunks = await collect(
      [
        { role: 'user', content: 'Q1' },
        { role: 'assistant', content: 'A1' },
        { role: 'user', content: 'Q2' },
        { role: 'assistant', content: 'A2' },
      ],
      'Q3',
    );
    // First N chunks are past-exchange (one per user), last is the new message.
    expect(chunks.length).toBe(3);
    expect(chunks[0]).toContain('Q1');
    expect(chunks[0]).toContain('A1');
    expect(chunks[1]).toContain('Q2');
    expect(chunks[1]).toContain('A2');
    expect(chunks[2]).toContain('<new-user-message>\nQ3\n</new-user-message>');
  });

  it('handles a trailing user message without a reply (in-progress turn)', async () => {
    const chunks = await collect(
      [
        { role: 'user', content: 'Q1' },
        { role: 'assistant', content: 'A1' },
        { role: 'user', content: 'Q2 (no reply yet)' },
      ],
      'Q3',
    );
    expect(chunks.length).toBe(3);
    // Second exchange has no <past-assistant> block because there was no reply.
    expect(chunks[1]).toContain('Q2 (no reply yet)');
    expect(chunks[1]).not.toContain('<past-assistant>');
  });
});
