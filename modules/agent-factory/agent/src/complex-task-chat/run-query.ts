/**
 * Shared runQuery — Claude Agent SDK stream loop used by the chat agent.
 *
 * Responsibilities:
 * - Register custom tools via `createSdkMcpServer` so port-provided tools
 *   (expand_summary, save_learning, publish_artifact, ...) are actually callable.
 *   Without this, only the built-in Bash/Read/Write/... tools reach the agent.
 * - Stream prior session history as an AsyncIterable<SDKUserMessage> so the model
 *   sees real turn alternation (not XML jammed into the system prompt).
 * - Restore the queryLoop label + break-on-result pattern from `agent-worker.ts`
 *   so the iterator terminates deterministically.
 * - Force-exit safety net: if the stream doesn't close within
 *   POST_COMPLETION_TIMEOUT_MS after the `result` message, log and carry on.
 * - Harvest input/output token counts from the SDK `result` message.
 * - Emit progress events (tool use, thinking preview) so the orchestrator can
 *   forward them to the WebSocket and keep the connection warm through long
 *   (multi-minute) research turns. Server-side progress is the only way to
 *   defeat API Gateway WebSocket's 10-min idle-timeout — it resets on any data
 *   frame, so a trickle of progress notifications keeps the socket alive.
 */
import { tool, createSdkMcpServer } from '@anthropic-ai/claude-agent-sdk';
import { z } from 'zod';
import { resilientQuery } from '../utils/resilientQuery';
import { AgentTool, AgentToolResult, SDKMessage } from './context/types';

// When the `result` message lands, give the underlying stream this long to close
// gracefully before we force-exit the loop (mirrors agent-worker.ts:863).
const POST_COMPLETION_TIMEOUT_MS = 10 * 60 * 1000;
// Heartbeat cadence — emits a synthetic progress event to the client so the
// WebSocket stays warm during pure-reasoning turns (no tool_use, no thinking
// blocks to trigger real progress events).  20s is well under API Gateway's
// 10-min idle timeout.
const HEARTBEAT_INTERVAL_MS = 20_000;
// Minimum gap between progress events we emit to the client. Prevents flooding
// when the agent rips through many tool calls in quick succession. Split by
// event type — tool_use and thinking events should flow promptly so the user
// can follow what the agent is doing, while heartbeats are throttled harder
// because they carry no new information.
const PROGRESS_MIN_INTERVAL_MS = {
  tool_use: 750,  // tool invocations should feel immediate
  thinking: 2_000, // intermediate reasoning text
  heartbeat: 8_000, // no new info, don't spam
  text_delta: 0,   // token streaming — never throttle, never dedup
};
// Hard cap on characters of a thinking preview. The client only needs a teaser.
const PROGRESS_PREVIEW_MAX_CHARS = 200;

/**
 * Progress event emitted mid-turn. The orchestrator is expected to forward
 * these to the user over the delivery channel (WebSocket). Each event type is
 * a structured signal: `tool_use` says "I'm calling tool X", `thinking` carries
 * a short preview of the model's current reasoning.
 */
export type ProgressEvent =
  | {
      type: 'tool_use';
      tool_name: string;
      input_summary: string;
      turn: number;
    }
  | {
      type: 'thinking';
      preview: string;
      turn: number;
    }
  | {
      type: 'heartbeat';
      turn: number;
    }
  | {
      /**
       * A text delta from the streaming model. The orchestrator should emit
       * this as an incremental AG-UI TEXT_MESSAGE_CONTENT with the delta
       * appended to the current assistant bubble, so users see the reply
       * being typed rather than dumped as one blob at turn end.
       *
       * `delta` is plain text — may be a single token or a batched group,
       * depending on what the SDK flushes.
       */
      type: 'text_delta';
      delta: string;
      turn: number;
    };

export interface RunQueryInput {
  systemPrompt: string;
  /** Prior turns (user + assistant) already in the conversation. */
  history: SDKMessage[];
  /** The new user message driving this turn. */
  userMessage: string;
  /** Port-provided tools to expose to the agent. */
  tools?: AgentTool[];
  model?: string;
  cwd?: string;
  maxTurns?: number;
  /** Claude effort level.  Controls verbosity + latency; used by channel
   *  profiles (e.g. webchat → 'low' for concise, fast replies). The SDK
   *  Options type supports 'low' | 'medium' | 'high' | 'max'. */
  effort?: 'low' | 'medium' | 'high' | 'max';
  log?: (msg: string) => void;
  /**
   * Optional progress sink. Called with mid-turn signals (tool invocations +
   * thinking previews). If the callback throws or returns a rejected Promise,
   * we swallow the error — progress is best-effort and must never break the
   * main turn. Throttled to at most one event per PROGRESS_MIN_INTERVAL_MS.
   */
  onProgress?: (event: ProgressEvent) => void | Promise<void>;
}

export interface RunQueryResult {
  text: string;
  tokens: { input: number; output: number };
  turnCount: number;
}

export async function runQuery(input: RunQueryInput): Promise<RunQueryResult> {
  const {
    systemPrompt,
    history,
    userMessage,
    tools: customTools = [],
    model = process.env.ANTHROPIC_MODEL ?? 'global.anthropic.claude-sonnet-4-6',
    cwd = '/tmp/workspace',
    maxTurns = 50,
    effort,
    log = console.log,
    onProgress,
  } = input;

  // 1) Build an MCP server that hosts port-provided tools.
  const mcpServer = customTools.length > 0
    ? createSdkMcpServer({
        name: 'chat-agent-tools',
        version: '1.0.0',
        tools: customTools.map(t =>
          tool(
            t.name,
            t.description,
            agentToolSchema(t),
            async (args: unknown): Promise<AgentToolResult> => {
              const typedArgs = (args ?? {}) as Record<string, unknown>;
              try {
                return await t.handler(typedArgs);
              } catch (err) {
                return {
                  content: [{ type: 'text', text: `Error: ${(err as Error).message}` }],
                  isError: true,
                };
              }
            },
          ),
        ),
      })
    : null;

  // 2) Base built-ins (parity with agent-worker.ts:899) + MCP tool names.
  const baseTools = ['Bash', 'Read', 'Write', 'Edit', 'Glob', 'Grep', 'WebSearch', 'WebFetch', 'Skill'];
  const mcpToolNames = customTools.map(t => `mcp__chat-agent-tools__${t.name}`);
  const allowedTools = [...baseTools, ...mcpToolNames];

  log(
    `[run-query] Starting query: model=${model}, history=${history.length}, customTools=${customTools.length}`,
  );

  let resultText = '';
  let inputTokens = 0;
  let outputTokens = 0;
  let turnCount = 0;
  let queryCompletedAt: number | null = null;
  let lastProgressAt = 0;
  let lastProgressKey = ''; // dedupe consecutive identical events

  /**
   * Fire a progress event if caller supplied onProgress AND we're past the
   * throttle window AND this isn't a repeat of the previous event. Errors
   * swallowed: progress is best-effort, a failed WS send must not abort the
   * main turn.
   */
  /**
   * Fire a progress event if caller supplied onProgress AND we're past the
   * throttle window AND this isn't a repeat of the previous event.
   *
   * @param force  When true, bypass PROGRESS_MIN_INTERVAL_MS (used by
   *               the heartbeat timer — it already runs on a longer cadence).
   */
  const emitProgress = (event: ProgressEvent, force = false): void => {
    if (!onProgress) return;
    // text_delta fires on every token — bypass the throttle + dedup entirely
    // so the reply actually streams into the UI.
    if (event.type === 'text_delta') {
      Promise.resolve(onProgress(event)).catch(err => {
        log(`[run-query] onProgress (text_delta) failed (non-fatal): ${(err as Error).message}`);
      });
      return;
    }

    // Dedup key includes the input summary for tool_use so two back-to-back
    // WebSearches with different queries are treated as distinct events.
    // Previously the key was just `tool:WebSearch`, so consecutive searches
    // with different queries were silently dropped and the user's UI went
    // dark between "Searching..." messages.
    const key =
      event.type === 'tool_use'
        ? `tool:${event.tool_name}:${event.input_summary}`
        : event.type === 'heartbeat'
          ? 'heartbeat'
          : 'thinking';
    const minInterval = PROGRESS_MIN_INTERVAL_MS[event.type];
    const now = Date.now();
    if (!force && now - lastProgressAt < minInterval) return;
    if (key === lastProgressKey && !force) return;
    lastProgressAt = now;
    lastProgressKey = key;
    Promise.resolve(onProgress(event)).catch(err => {
      log(`[run-query] onProgress failed (non-fatal): ${(err as Error).message}`);
    });
  };

  const heartbeat = setInterval(() => {
    // Force-exit safety net (mirrors agent-worker.ts:868-880).
    if (queryCompletedAt !== null) {
      const elapsed = Date.now() - queryCompletedAt;
      if (elapsed >= POST_COMPLETION_TIMEOUT_MS) {
        log(`[run-query] Force exit — stream did not close ${Math.round(elapsed / 1000)}s after result`);
        clearInterval(heartbeat);
      }
      return; // query done, don't emit heartbeats
    }

    // Coalesce: if a real progress event (tool_use, thinking) fired within
    // this heartbeat window, skip the synthetic heartbeat — the socket is
    // already warm.
    const msSinceLastProgress = Date.now() - lastProgressAt;
    if (msSinceLastProgress < HEARTBEAT_INTERVAL_MS) {
      log(`[run-query] Heartbeat skipped — real progress ${Math.round(msSinceLastProgress / 1000)}s ago (turn ${turnCount})`);
      return;
    }

    log(`[run-query] Emitting heartbeat (turn ${turnCount})`);
    emitProgress({ type: 'heartbeat', turn: turnCount }, /* force */ true);
  }, HEARTBEAT_INTERVAL_MS);

  try {
    // 3) Stream prior history as AsyncIterable<SDKUserMessage> so the model sees
    //    real turn alternation. The SDK only accepts `user` stream messages, so
    //    assistant turns are folded into preceding user messages as quoted context.
    //    This preserves information while keeping the stream schema-valid.
    const promptStream = buildPromptStream(history, userMessage);

    const streamOptions: Record<string, unknown> = {
      systemPrompt,
      model,
      cwd,
      allowedTools,
      settingSources: ['project'],
      permissionMode: 'bypassPermissions' as const,
      persistSession: false,
      maxTurns,
      // Emit SDKPartialAssistantMessage (`type: 'stream_event'`) with
      // content_block_delta events so we can stream the final reply to the
      // user token-by-token instead of dumping the whole paragraph as one
      // frame at turn end. Token streaming is the single biggest perceived-
      // quality improvement for chat UX.
      includePartialMessages: true,
    };
    if (effort !== undefined) {
      // The Claude Agent SDK's Options type has no `maxTokens` / `maxOutputTokens`
      // input field — those are only reported in `ModelUsage`.  The supported
      // output-length lever is `effort: 'low' | 'medium' | 'high' | 'max'`, which
      // is a soft nudge the model respects (latency + verbosity).  See
      // node_modules/@anthropic-ai/claude-agent-sdk/sdk.d.ts (Options.effort).
      streamOptions.effort = effort;
    }
    if (mcpServer) {
      streamOptions.mcpServers = { 'chat-agent-tools': mcpServer };
    }

    // Labeled loop so we can break out of the `for await` from inside the
    // switch (mirrors agent-worker.ts:892).
    queryLoop: // eslint-disable-line no-labels
    for await (const message of resilientQuery({
      queryParams: {
        prompt: promptStream,
        options: streamOptions as any,
      },
      maxRetries: 3,
      baseDelayMs: 10_000,
      maxDelayMs: 60_000,
      log,
    })) {
      const msg = message as Record<string, unknown>;

      switch (msg.type) {
        case 'stream_event': {
          // Incremental model output from the SDK — surfaces content_block_delta
          // events so we can forward text deltas to the user live. We match
          // defensively on shape (the SDK re-exports Anthropic Beta types that
          // we don't want to import directly here).
          const ev = (msg as { event?: Record<string, unknown> }).event;
          if (
            ev &&
            ev.type === 'content_block_delta' &&
            typeof (ev as { delta?: unknown }).delta === 'object'
          ) {
            const delta = ev.delta as Record<string, unknown>;
            if (delta.type === 'text_delta' && typeof delta.text === 'string' && delta.text.length > 0) {
              emitProgress({
                type: 'text_delta',
                delta: delta.text,
                turn: turnCount,
              });
            }
          }
          break;
        }

        case 'assistant': {
          turnCount++;
          const content = msg.message as Record<string, unknown> | undefined;
          const blocks = (content?.content as Array<Record<string, unknown>> | undefined) ?? [];

          // Walk blocks: grab text for resultText; emit progress for tool_use
          // and for "thinking" text (text blocks that aren't the final answer
          // — heuristic: any text block seen in an intermediate turn).
          const textParts: string[] = [];
          let sawToolUse = false;
          for (const block of blocks) {
            if (block.type === 'text' && typeof block.text === 'string') {
              textParts.push(block.text);
            } else if (block.type === 'tool_use' && typeof block.name === 'string') {
              sawToolUse = true;
              // Log per-tool-call so hung workers are diagnosable from pod
              // logs alone — heartbeat-only logs tell us "stuck on turn N"
              // but not which tool call is wedged.
              const inputSummary = summarizeToolInput(block.input as Record<string, unknown>);
              log(
                `[run-query] turn ${turnCount} tool_use: ${block.name}${inputSummary ? ' → ' + inputSummary : ''}`,
              );
              emitProgress({
                type: 'tool_use',
                tool_name: block.name,
                input_summary: inputSummary,
                turn: turnCount,
              });
            }
          }

          if (textParts.length > 0) {
            const joined = textParts.join('\n');
            // Any assistant turn that ALSO fires a tool_use is reasoning on
            // the way to a final answer — emit its text as a "thinking"
            // preview. The last assistant turn (no tool_use, lands before
            // `result`) is the final answer and stays silent here — the
            // orchestrator delivers it via the normal response path.
            if (sawToolUse) {
              emitProgress({
                type: 'thinking',
                preview: joined.slice(0, PROGRESS_PREVIEW_MAX_CHARS),
                turn: turnCount,
              });
            }
            resultText = joined;
          }
          break;
        }

        case 'user': {
          // Log tool_result blocks so we can see "called X at turn N" /
          // "X returned at turn N" pairs in the pod logs and spot hangs
          // that occur between call and return.
          const content = (msg.message as Record<string, unknown> | undefined)?.content;
          const blocks = Array.isArray(content) ? (content as Array<Record<string, unknown>>) : [];
          for (const block of blocks) {
            if (block.type === 'tool_result' && typeof block.tool_use_id === 'string') {
              const isError = block.is_error === true;
              log(
                `[run-query] turn ${turnCount} tool_result: ${block.tool_use_id.slice(0, 16)} ${isError ? 'ERROR' : 'ok'}`,
              );
            }
          }
          break;
        }

        case 'result': {
          // Harvest usage + cost from the result message.
          const usage = (msg as { usage?: { input_tokens?: number; output_tokens?: number } }).usage;
          if (usage) {
            inputTokens = usage.input_tokens ?? 0;
            outputTokens = usage.output_tokens ?? 0;
          }
          const cost = (msg as { total_cost_usd?: number }).total_cost_usd;
          const numTurns = (msg as { num_turns?: number }).num_turns;
          log(
            `[run-query] Result: ${numTurns ?? '?'} turns, $${cost?.toFixed(4) ?? '?'}, in=${inputTokens}, out=${outputTokens}`,
          );
          queryCompletedAt = Date.now();
          break queryLoop; // eslint-disable-line no-labels
        }
      }
    }
  } finally {
    clearInterval(heartbeat);
  }

  log(`[run-query] Complete: ${turnCount} turns`);

  return {
    text: resultText || '(no response)',
    tokens: { input: inputTokens, output: outputTokens },
    turnCount,
  };
}

/**
 * Render a one-line summary of a tool's input for the progress frame. The
 * client only needs enough to say "agent is searching for X" or "agent is
 * reading file Y". We pick the most informative field by name.
 */
function summarizeToolInput(input: Record<string, unknown> | undefined): string {
  if (!input) return '';
  const prefer = ['query', 'url', 'path', 'file_path', 'pattern', 'command', 'summary_id'];
  for (const k of prefer) {
    const v = input[k];
    if (typeof v === 'string' && v.length > 0) return truncate(v, 120);
  }
  // Fallback: first string-valued field.
  for (const [, v] of Object.entries(input)) {
    if (typeof v === 'string' && v.length > 0) return truncate(v, 120);
  }
  return '';
}

function truncate(s: string, max: number): string {
  if (s.length <= max) return s;
  return `${s.slice(0, max - 1)}…`;
}

/**
 * Build the Zod raw shape the SDK's `tool()` helper expects from our AgentTool's
 * `inputSchema`. The AgentTool already carries a ZodRawShape, so this is a
 * pass-through — isolated as a function so we can swap the shape later without
 * touching callers.
 */
function agentToolSchema(t: AgentTool): Record<string, z.ZodTypeAny> {
  return t.inputSchema as unknown as Record<string, z.ZodTypeAny>;
}

/**
 * Yield history as synthetic user-role SDK stream messages, with assistant turns
 * folded into the preceding user message as quoted context. The real user message
 * is the final yield, wrapped in <current-user-message> so the model can
 * unambiguously tell it apart from history and only act on the new ask.
 *
 * Prior framing (just yielding each history entry as a raw user turn) caused
 * the model to treat N historical user messages as N stacked live requests and
 * re-act on all of them — e.g. a simple "what time is it?" following earlier
 * research questions would trigger fresh searches + the time answer.
 */
async function* buildPromptStream(
  history: SDKMessage[],
  userMessage: string,
): AsyncIterable<{
  type: 'user';
  message: { role: 'user'; content: string };
  parent_tool_use_id: null;
  session_id: string;
}> {
  const sessionId = 'chat-agent-stream';
  const pending: string[] = [];

  const flushUser = function* (text: string) {
    yield {
      type: 'user' as const,
      message: { role: 'user' as const, content: text },
      parent_tool_use_id: null,
      session_id: sessionId,
    };
  };

  // Walk history: group each user turn with the assistant turns that followed
  // it, and wrap the whole pair in <prior-turn> tags. This makes it obvious
  // to the model that these are past exchanges to read for context, not
  // unaddressed asks stacked up for the current turn.
  let currentUserText: string | null = null;
  for (const m of history) {
    if (m.role === 'user') {
      if (currentUserText !== null) {
        const body = pending.length > 0
          ? `<prior-user-message>\n${currentUserText}\n</prior-user-message>\n<prior-assistant-response>\n${pending.join('\n\n')}\n</prior-assistant-response>`
          : `<prior-user-message>\n${currentUserText}\n</prior-user-message>`;
        yield* flushUser(`<prior-turn>\n${body}\n</prior-turn>`);
        pending.length = 0;
      }
      currentUserText = m.content;
    } else {
      pending.push(m.content);
    }
  }

  if (currentUserText !== null) {
    const body = pending.length > 0
      ? `<prior-user-message>\n${currentUserText}\n</prior-user-message>\n<prior-assistant-response>\n${pending.join('\n\n')}\n</prior-assistant-response>`
      : `<prior-user-message>\n${currentUserText}\n</prior-user-message>`;
    yield* flushUser(`<prior-turn>\n${body}\n</prior-turn>`);
  } else if (pending.length > 0) {
    // Only assistant messages in history (no paired user turn) — still wrap
    // them as context so the current message is the only live ask.
    yield* flushUser(`<prior-assistant-context>\n${pending.join('\n\n')}\n</prior-assistant-context>`);
  }

  // Final yield: the NEW user turn, explicitly marked. Prior turns above are
  // context only — this is the message the agent should act on.
  yield* flushUser(
    `<current-user-message>\n${userMessage}\n</current-user-message>\n\n` +
      `The blocks above tagged <prior-turn> are earlier exchanges in this conversation, ` +
      `kept for context only. The <current-user-message> above is the new request — ` +
      `address only that, unless it explicitly refers back to something from a prior turn.`,
  );
}
