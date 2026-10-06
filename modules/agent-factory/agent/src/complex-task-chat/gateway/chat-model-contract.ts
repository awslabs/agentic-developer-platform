import { z } from 'zod';

const toolIdentifier = z.string().regex(/^[A-Za-z0-9_-]{1,200}$/);
const toolName = z.string().regex(/^[A-Za-z0-9_-]{1,128}$/);
export const modelText = z.object({ type: z.literal('text'), text: z.string().max(65_536) }).strict();
export const modelToolUse = z.object({
  type: z.literal('tool_use'), id: toolIdentifier, name: toolName, input: z.record(z.string(), z.json()),
}).strict();
const modelThinking = z.object({
  type: z.literal('thinking'), thinking: z.string().max(32_000), signature: z.string().min(1).max(16_000),
}).strict();
const modelToolResult = z.object({
  type: z.literal('tool_result'), tool_use_id: toolIdentifier,
  content: z.string().max(32_000), is_error: z.boolean().optional(),
}).strict();
export const modelOutputBlock = z.union([modelText, modelToolUse, modelThinking]);
export const modelRequestSchema = z.object({
  messages: z.array(z.object({
    role: z.enum(['user', 'assistant']),
    content: z.union([z.string().min(1).max(32_000),
      z.array(z.union([modelText.extend({ text: z.string().min(1).max(32_000) }), modelToolUse, modelThinking, modelToolResult])).min(1).max(64)]),
  }).strict().refine(message => typeof message.content === 'string' || message.content.every(block =>
    block.type === 'text' || (block.type === 'tool_result' ? message.role === 'user' : message.role === 'assistant'))))
    .min(1).max(32),
  system: z.string().max(16_000).optional(),
  max_tokens: z.number().int().min(1).max(10_000),
  tools: z.array(z.object({
    name: toolName, description: z.string().max(16_000),
    input_schema: z.record(z.string(), z.json()).refine(value => value.type === 'object'),
  }).strict()).max(32).optional(),
}).strict().refine(request => new Set(request.tools?.map(tool => tool.name)).size === (request.tools?.length ?? 0));

export type ChatModelRequest = z.infer<typeof modelRequestSchema>;
