import { createHash } from 'node:crypto';

export const NATIVE_PROBE_NORMALIZATION = 'codex-native-probe-v1';
export const NATIVE_PROBE_SDK = '0.155.1';
const syntheticId = '00000000-0000-4000-8000-000000000001';
const volatileTurnKeys = ['installation_id', 'session_id', 'thread_id', 'turn_id', 'window_id', 'context_window_id', 'root_turn_id'];

export function canonicalJson(value) {
  if (Array.isArray(value)) return '[' + value.map(canonicalJson).join(',') + ']';
  if (value && typeof value === 'object') return '{' + Object.keys(value).sort().map(key => JSON.stringify(key) + ':' + canonicalJson(value[key])).join(',') + '}';
  return JSON.stringify(value);
}

/** Normalize only probe-local identity, clock and temporary roots. Tool schemas,
 * instructions, permissions, reasoning and structured output remain in the hash.
 * This is a bounded non-streaming provider probe, not a native execution receipt.
 */
export function nativeProbeBody(capture) {
  const body = structuredClone(capture.body);
  if (!capture.root.startsWith('/') || !Array.isArray(body.input) || !Array.isArray(body.tools) || body.stream !== true) throw new Error('Invalid native capture');
  for (const [index, message] of body.input.entries()) {
    if (message.type !== 'message' || !Array.isArray(message.content)) throw new Error('Unexpected initial native input');
    if (message.id !== undefined) message.id = 'msg_00000000-0000-4000-8000-' + String(index + 1).padStart(12, '0');
    const metadata = message.internal_chat_message_metadata_passthrough;
    if (metadata) {
      if ('turn_id' in metadata) metadata.turn_id = syntheticId;
      if ('create_time' in metadata) metadata.create_time = 0;
    }
    for (const part of message.content) {
      if (part.type !== 'input_text' || typeof part.text !== 'string') throw new Error('Unexpected probe content');
      // Only known SDK system context can contain randomized probe directories.
      if (part.text.startsWith('<skills_instructions>') || part.text.startsWith('<environment_context>')) {
        part.text = part.text.split(capture.root).join('/adp-native-probe');
      }
      if (part.text.startsWith('<environment_context>')) {
        part.text = part.text.replace(/<timezone>(?:Etc\/UTC|\/UTC|UTC)<\/timezone>/, '<timezone>UTC</timezone>');
        part.text = part.text.replace(/<current_date>\d{4}-\d{2}-\d{2}<\/current_date>/, '<current_date>2026-01-01</current_date>');
      }
    }
  }
  if ('prompt_cache_key' in body) body.prompt_cache_key = syntheticId;
  if (body.client_metadata) {
    for (const key of ['x-codex-window-id', 'x-codex-installation-id', 'session_id', 'turn_id', 'thread_id', 'root_turn_id']) {
      if (key in body.client_metadata) body.client_metadata[key] = syntheticId;
    }
    if (typeof body.client_metadata['x-codex-turn-metadata'] === 'string') {
      const metadata = JSON.parse(body.client_metadata['x-codex-turn-metadata']);
      for (const key of volatileTurnKeys) if (key in metadata) metadata[key] = syntheticId;
      if ('turn_started_at_unix_ms' in metadata) metadata.turn_started_at_unix_ms = 0;
      body.client_metadata['x-codex-turn-metadata'] = canonicalJson(metadata);
    }
  }
  if (JSON.stringify(body.include) !== '["reasoning.encrypted_content"]') throw new Error('Unsupported native response include contract');
  body.stream = false;
  body.store = false;
  body.max_output_tokens = 512;
  const json = canonicalJson(body);
  if (Buffer.byteLength(json) > 65536) throw new Error('Native probe exceeds transport bound');
  return { body: json, digest: createHash('sha256').update(json).digest('hex') };
}
