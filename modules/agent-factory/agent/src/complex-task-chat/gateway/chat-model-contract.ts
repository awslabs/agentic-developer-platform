import { z } from 'zod';

const toolIdentifier = z.string().regex(/^[A-Za-z0-9_-]{1,200}$/);
const toolName = z.string().regex(/^[A-Za-z0-9_-]{1,128}$/);
export const modelText = z.object({ type: z.literal('text'), text: z.string().max(65_536) }).strict();
export const modelToolUse = z.object({
  type: z.literal('tool_use'), id: toolIdentifier, name: toolName, input: z.record(z.string(), z.json()),
}).strict();
const modelThinking = z.object({
  // Bedrock's thinking signatures run past 16 KB (dev 2026-10-22: 19,328 chars on an ordinary Dev Box turn),
  // and a cleared thinking block keeps its signature. A 16_000 cap made the loopback adapter refuse every
  // turn's first attempt, and Claude Code's retry sent a different, far larger body (#8223 cause 50).
  type: z.literal('thinking'), thinking: z.string().max(262_144), signature: z.string().min(1).max(65_536),
}).strict();
const modelToolResult = z.object({
  type: z.literal('tool_result'), tool_use_id: toolIdentifier,
  content: z.string().max(32_000), is_error: z.boolean().optional(),
}).strict();
export const modelOutputBlock = z.union([modelText, modelToolUse, modelThinking]);
/**
 * Size envelope of one model request. The default is sized for the chat sandbox's own prompts
 * and matches the gateway's chat-data validation. A Dev Box host drives Claude Code, whose
 * first request alone (system prompt + tool schemas) is far beyond 64 KiB and whose
 * conversations run to hundreds of messages, so the host adapter uses {@link HOST_MODEL_REQUEST_LIMITS}
 * (#8223 cause 36). The gateway's model route forwards the body to Bedrock, whose context window
 * (200k tokens) is the real ceiling; API Gateway accepts 10 MB.
 */
export type ModelRequestLimits = {
  bodyBytes: number; messages: number; blocks: number; contentChars: number; systemChars: number;
  tools: number; toolDescriptionChars: number; maxTokens: number;
  /**
   * Whether Anthropic prompt-cache markers (`cache_control`) ride through to the gateway. The chat
   * sandbox keeps them out (the gateway's chat-data validation knows nothing of them); a Dev Box host
   * forwards Claude Code's markers on system blocks, tool definitions and message content so Bedrock
   * serves the ~115k-token prefix from cache instead of re-reading it on every call (dev 2026-10-21:
   * every call carried cache_read_input_tokens 0 and took 4–9 s).
   */
  cacheMarkers: boolean;
};
export const DEFAULT_MODEL_REQUEST_LIMITS: ModelRequestLimits = {
  bodyBytes: 65_536, messages: 32, blocks: 64, contentChars: 32_000, systemChars: 16_000,
  tools: 32, toolDescriptionChars: 16_000, maxTokens: 10_000, cacheMarkers: false,
};
export const HOST_MODEL_REQUEST_LIMITS: ModelRequestLimits = {
  bodyBytes: 8 * 1024 * 1024, messages: 1_000, blocks: 256, contentChars: 1_000_000, systemChars: 1_000_000,
  // Claude Code's built-in tools plus every MCP tool the host exposes (adp-tools, Kiro Crew) exceeded 128
  // on dev 2026-10-21 (#8223 cause 44: `tools:too_big` on a 314 KB turn); the Messages API itself allows far more.
  tools: 1_024, toolDescriptionChars: 65_536, maxTokens: 32_000, cacheMarkers: true,
};

/** Anthropic prompt-cache breakpoint marker; the only shape a host may forward. */
export const cacheHint = z.object({ type: z.literal('ephemeral'), ttl: z.enum(['5m', '1h']).optional() }).strict();
export type CacheHint = z.infer<typeof cacheHint>;
export type SystemBlock = { type: 'text'; text: string; cache_control?: CacheHint };
/** One Server-Sent Event of a streamed model reply: the gateway's event name and raw JSON data. */
export type ModelStreamEvent = { event: string; data: string };

export function buildModelRequestSchema(limits: ModelRequestLimits) {
  // With markers off (the sandbox) a present `cache_control` key is refused: `never` admits only an
  // absent key, so the strict objects below stay byte-for-byte as strict as before this option.
  const marker: z.ZodType<CacheHint> = limits.cacheMarkers ? cacheHint : (z.never() as unknown as z.ZodType<CacheHint>);
  const text = modelText.extend({ text: z.string().min(1).max(limits.contentChars), cache_control: marker.optional() }).strict();
  const toolUse = modelToolUse.extend({ cache_control: marker.optional() }).strict();
  const thinking = modelThinking.extend({ cache_control: marker.optional() }).strict();
  const toolResult = z.object({
    type: z.literal('tool_result'), tool_use_id: toolIdentifier,
    content: z.string().max(limits.contentChars), is_error: z.boolean().optional(), cache_control: marker.optional(),
  }).strict();
  const systemBlocks = z.array(z.object({ type: z.literal('text'), text: z.string().max(limits.systemChars),
    cache_control: marker.optional() }).strict()).min(1).max(limits.blocks);
  const system: z.ZodType<string | SystemBlock[]> = limits.cacheMarkers
    ? z.union([z.string().max(limits.systemChars), systemBlocks]) : z.string().max(limits.systemChars);
  const turn = z.object({
    role: z.enum(['user', 'assistant']),
    content: z.union([z.string().min(1).max(limits.contentChars),
      z.array(z.union([text, toolUse, thinking, toolResult])).min(1).max(limits.blocks)]),
  }).strict().refine(message => typeof message.content === 'string' || message.content.every(block =>
    block.type === 'text' || (block.type === 'tool_result' ? message.role === 'user' : message.role === 'assistant')));
  // A host forwards Claude Code's mid-conversation `system`-role messages in place (#8223 cause 51): the
  // gateway keeps them in conversation order so the trailing cache breakpoint still caches the history.
  // The sandbox contract has no such messages.
  const systemTurn = z.object({ role: z.literal('system'), content: z.union([z.string().max(limits.systemChars), systemBlocks]) }).strict();
  const message: z.ZodType<ChatModelMessage> = (limits.cacheMarkers ? z.union([turn, systemTurn]) : turn) as unknown as z.ZodType<ChatModelMessage>;
  return z.object({
    messages: z.array(message).min(1).max(limits.messages),
    system: system.optional(),
    max_tokens: z.number().int().min(1).max(limits.maxTokens),
    tools: z.array(z.object({
      name: toolName, description: z.string().max(limits.toolDescriptionChars),
      input_schema: z.record(z.string(), z.json()).refine(value => value.type === 'object'),
      cache_control: marker.optional(),
    }).strict()).max(limits.tools).optional(),
  }).strict().refine(request => new Set(request.tools?.map(tool => tool.name)).size === (request.tools?.length ?? 0));
}

export const modelRequestSchema = buildModelRequestSchema(DEFAULT_MODEL_REQUEST_LIMITS);

type ModelTurn = { role: 'user' | 'assistant'; content: string | Array<z.infer<typeof modelOutputBlock> | {
  type: 'tool_result'; tool_use_id: string; content: string; is_error?: boolean; cache_control?: CacheHint;
} | (z.infer<typeof modelText> & { cache_control?: CacheHint })> };
/** A conversation turn; `system`-role turns are admitted by the host contract only (`cacheMarkers`). */
export type ChatModelMessage = ModelTurn | { role: 'system'; content: string | SystemBlock[] };
export type ChatModelRequest = Omit<z.infer<typeof modelRequestSchema>, 'messages'> & { messages: ChatModelMessage[] };
