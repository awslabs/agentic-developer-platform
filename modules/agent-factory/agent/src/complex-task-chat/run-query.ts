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
 */
import { tool, createSdkMcpServer } from '@anthropic-ai/claude-agent-sdk';
import { z } from 'zod';
import { resilientQuery } from '../utils/resilientQuery';
import { AgentTool, AgentToolResult, SDKMessage } from './context/types';

// When the `result` message lands, give the underlying stream this long to close
// gracefully before we force-exit the loop (mirrors agent-worker.ts:863).
const POST_COMPLETION_TIMEOUT_MS = 10 * 60 * 1000;
// Heartbeat cadence for log-only "still alive" notifications.
const HEARTBEAT_INTERVAL_MS = 30_000;

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
  log?: (msg: string) => void;
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
    log = console.log,
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

  const heartbeat = setInterval(() => {
    // Force-exit safety net (mirrors agent-worker.ts:868-880).
    if (queryCompletedAt !== null) {
      const elapsed = Date.now() - queryCompletedAt;
      if (elapsed >= POST_COMPLETION_TIMEOUT_MS) {
        log(`[run-query] Force exit — stream did not close ${Math.round(elapsed / 1000)}s after result`);
        clearInterval(heartbeat);
      }
    }
    log(`[run-query] Still processing... (turn ${turnCount})`);
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
    };
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
        case 'assistant': {
          turnCount++;
          const content = msg.message as Record<string, unknown> | undefined;
          if (content?.content && Array.isArray(content.content)) {
            const textParts = (content.content as Array<Record<string, unknown>>)
              .filter(p => p.type === 'text')
              .map(p => p.text as string);
            if (textParts.length > 0) {
              resultText = textParts.join('\n');
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
 * is the final yield.
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
  // Fold: walk history and group assistant replies under the preceding user turn.
  // For turns where history starts with an assistant message (shouldn't happen
  // normally), we emit a synthetic user wrapper.
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

  let currentUserText: string | null = null;
  for (const m of history) {
    if (m.role === 'user') {
      if (currentUserText !== null) {
        const combined = pending.length > 0
          ? `${currentUserText}\n\n<prior-assistant-response>\n${pending.join('\n\n')}\n</prior-assistant-response>`
          : currentUserText;
        yield* flushUser(combined);
        pending.length = 0;
      }
      currentUserText = m.content;
    } else {
      pending.push(m.content);
    }
  }

  if (currentUserText !== null) {
    const combined = pending.length > 0
      ? `${currentUserText}\n\n<prior-assistant-response>\n${pending.join('\n\n')}\n</prior-assistant-response>`
      : currentUserText;
    yield* flushUser(combined);
  } else if (pending.length > 0) {
    // Only assistant messages in history — wrap as context on the new user turn.
    yield* flushUser(`<prior-assistant-context>\n${pending.join('\n\n')}\n</prior-assistant-context>\n\n${userMessage}`);
    return;
  }

  // Final yield: the new user turn.
  yield* flushUser(userMessage);
}
