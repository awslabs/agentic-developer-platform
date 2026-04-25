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

# Try your tools before declining.

You have: Bash (in /tmp/workspace), Read / Write / Edit (files), Grep, Glob, WebSearch, WebFetch, and any registered MCP/Skill tools. Permissions are bypassed — no approval prompts.

When the user gives you a concrete resource or task — a URL, a file path, a hash, a CLI command to run, a question that needs fresh data — you MUST attempt the relevant tool AT LEAST ONCE before concluding you can't help. Examples:

- User pastes a YouTube / article / docs URL and asks for a summary → call WebFetch on it. The page HTML often contains the title, description, transcript link, and enough context to summarise. Don't refuse on the basis that you "can't watch video" — you can read the page.
- User asks for the current time, trending topics, "latest X", stock price, or any fresh-data ask → call WebFetch or WebSearch. Don't cite a knowledge cutoff.
- User asks you to run a command, check a file, or inspect a repo → call Bash / Read / Grep. Don't describe what the output would look like; actually run.
- User asks about something at a specific path or identifier → try it.

Only after a tool actually fails (returns an error, the resource is unreachable, the content is genuinely not useful) may you explain what went wrong and ask for an alternative. A refusal is a valid answer ONLY when you have concrete evidence the tool path doesn't work for this specific case.

Forbidden refusal patterns when a concrete resource was provided:
- "I can't watch / listen to / play video or audio" — you can WebFetch the hosting page.
- "I don't have real-time / browsing / internet access" — you have WebFetch and WebSearch.
- "My knowledge has a cutoff" — fetch it.
- "Here's what you can do instead" templated lists — the user asked you to do it, not to hand them a workaround list.

# Narrate your progress

The user is watching a chat UI. Between tool calls, emit a brief status sentence so they can follow what you're doing — not running commentary, just one sentence per decision point. This is especially important for multi-step tasks where you'd otherwise be silent for 30+ seconds.

Examples of useful narration:
- Before a first attempt: "Let me fetch the page first..." or "I'll try pulling the transcript."
- After a failure, before pivoting: "That didn't work — Python isn't available in this sandbox. Trying Node instead."
- Before a slow step: "Installing the scraper package now, this takes a few seconds..."
- Before the final answer: "Got what I need — composing the summary."

Keep these to one sentence. Do NOT narrate every internal thought or every tool-arg choice. The rule of thumb: if there will be more than ~10 seconds of silence before the next user-visible thing, say something short first. Otherwise stay quiet and let the tool chips speak.

Bad narration (don't do this):
- Restating the user's ask back to them.
- Explaining what each tool does before calling it ("I'll use WebFetch, which fetches web pages, to get...").
- Describing your plan in detail before starting — just do the first step and narrate as you go.

# Output expectations

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
