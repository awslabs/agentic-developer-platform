/**
 * Channel-aware prompt directives (Issue #85, Problem B).
 *
 * Each delivery channel has different constraints (screen size, message limits,
 * user expectations). These directives compose ON TOP of the persona system
 * prompt — they don't replace it.
 *
 * Usage:
 *   const directive = getChannelDirective(channel ?? '');
 *   // Prepend to the base system prompt before composing.
 */

const WEBCHAT_DIRECTIVE = `\
You are replying in a real-time chat UI. Aim for concise, scannable output.
- Target total response length: under 4000 characters. Rarely exceed 8000.
- Lead with a 1-2 sentence TL;DR — users read the top first.
- Use short paragraphs and at most one small table. Avoid deep markdown nesting.
- For long plans, itineraries, reference data, or code >50 lines: call publish_artifact and link it in the reply instead of inlining.
- Target generation time under 60 seconds — users see a "thinking" indicator but get anxious after a minute.`;

const SLACK_DIRECTIVE = `\
You are replying in a Slack thread.
- Use Slack-flavored mrkdwn (no HTML, no tables past 2 columns).
- Keep under 3000 characters before Slack truncates. Publish longer output as a file snippet.
- Short emoji usage is encouraged for scannability (1-2 per response).`;

// TODO: Add WhatsApp profile when the channel exists.

const CHANNEL_DIRECTIVES: Record<string, string> = {
  webchat: WEBCHAT_DIRECTIVE,
  slack: SLACK_DIRECTIVE,
};

/**
 * Return a channel-specific directive to prepend to the system prompt.
 *
 * @param channel - The delivery channel identifier (e.g. 'webchat', 'slack').
 *                  Empty string or unknown channels return '' (current behavior).
 */
export function getChannelDirective(channel: string): string {
  if (!channel) return '';
  return CHANNEL_DIRECTIVES[channel] ?? '';
}

/**
 * Effort level per channel.  Returns undefined for channels with no
 * preference (uses SDK default, typically 'high').
 *
 * The Claude Agent SDK's `Options.effort` nudges the model toward
 * shorter/faster (`'low'`) or deeper (`'high'`, `'max'`) responses.
 * There is no hard output-token cap in the Options type (see sdk.d.ts
 * around Options — only `maxTurns`, `maxBudgetUsd`, and thinking budget
 * are available).  `effort` is the SDK-supported lever for verbosity.
 */
export function getChannelEffort(channel: string): 'low' | 'medium' | 'high' | 'max' | undefined {
  switch (channel) {
    case 'webchat':
      // Chat UI wants concise-but-useful replies. 'low' trims too hard for
      // multi-step questions; 'medium' still nudges shorter than the SDK
      // default ('high') but preserves enough structure to answer real asks.
      // Pair with the webchat directive (TL;DR + artifact links).
      return 'medium';
    default:
      return undefined;
  }
}
