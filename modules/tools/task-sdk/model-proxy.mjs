/** SDK loopback transport; it holds no AWS, provider or gateway credential. */
import http from 'node:http';
import { randomBytes } from 'node:crypto';
import { MAX_FRAME_BYTES, ProtocolError } from './protocol.mjs';

const MAX_SDK_HTTP_BYTES = 1024 * 1024;
const MAX_MODEL_REQUEST_BYTES = MAX_FRAME_BYTES - 4096;

function boundToolPreviews(request) {
  const size = () => Buffer.byteLength(JSON.stringify(request));
  if (size() <= MAX_MODEL_REQUEST_BYTES) return;
  // Preserve instructions, schemas and tool-call correlation. Only compact tool
  // previews; original evidence remains in host artifacts. Oldest previews go first.
  for (const message of request.messages) {
    if (!Array.isArray(message.content)) continue;
    for (const block of message.content) {
      if (block.type !== 'tool_result') continue;
      const original = JSON.stringify(block.content);
      if (Buffer.byteLength(original) <= 4096) continue;
      const text = Array.isArray(block.content)
        ? block.content.filter(item => item.type === 'text').map(item => item.text).join('\n')
        : typeof block.content === 'string' ? block.content : '';
      const refs = [...new Set(text.match(/\bart_[A-Za-z0-9_-]+\b/g) || [])].slice(0, 100);
      block.content = [{ type: 'text', text: JSON.stringify({
        context_notice: 'Earlier tool preview shortened to fit Task transport. Image previews may be omitted. Do not infer omitted content; use retained evidence and disclose coverage limitations.',
        preview_start: text.slice(0, 1000), preview_end: text.slice(-1000), artifact_refs: refs,
      }) }];
      if (size() <= MAX_MODEL_REQUEST_BYTES) return;
    }
  }
}

export function normalizeRequest(body, limit) {
  if (!body || !Array.isArray(body.messages) || !body.messages.length) throw new ProtocolError('SDK messages required');
  const select = (value, keys) => Object.fromEntries(keys.filter(key => value[key] !== undefined && value[key] !== null).map(key => [key, value[key]]));
  const block = value => {
    if (value.type === 'text') return select(value, ['type', 'text']);
    if (value.type === 'tool_use') return select(value, ['type', 'id', 'name', 'input']);
    if (value.type === 'tool_result') {
      const result = select(value, ['type', 'tool_use_id', 'content', 'is_error']);
      if (Array.isArray(result.content)) result.content = result.content.map(item => {
        if (item.type === 'image') {
          const source = item.source;
          if (source?.type !== 'base64' || !['image/jpeg', 'image/png'].includes(source.media_type) || typeof source.data !== 'string' || source.data.length > 16384) throw new ProtocolError('invalid bounded image preview');
          return { type: 'image', source: select(source, ['type', 'media_type', 'data']) };
        }
        if (item.type !== 'text') throw new ProtocolError('unsupported tool result content');
        return select(item, ['type', 'text']);
      });
      return result;
    }
    throw new ProtocolError('unsupported SDK content block');
  };
  // New SDK versions emit environment reminders as system-role messages.
  // Anthropic transports carry those in the separate system field, not messages.
  const reminders = [];
  const messages = body.messages.filter(message => {
    if (message.role !== 'system') return true;
    const content = typeof message.content === 'string' ? [{type: 'text', text: message.content}] : message.content;
    if (!Array.isArray(content) || !content.length || content.some(value => value.type !== 'text' || typeof value.text !== 'string' || !value.text)) {
      throw new ProtocolError('unsupported SDK system message');
    }
    reminders.push(...content.map(value => select(value, ['type', 'text'])));
    return false;
  });
  if (!messages.length) throw new ProtocolError('SDK conversation messages required');
  const sdk_request = { messages: messages.map(message => ({ role: message.role,
    content: Array.isArray(message.content) ? message.content.map(block) : message.content })) };
  if (body.system != null) sdk_request.system = Array.isArray(body.system) ? body.system.map(value => {
    if (value.type !== 'text') throw new ProtocolError('unsupported SDK system block');
    return select(value, ['type', 'text']);
  }) : body.system;
  if (reminders.length) sdk_request.system = [...(Array.isArray(sdk_request.system) ? sdk_request.system : sdk_request.system ? [{type: 'text', text: sdk_request.system}] : []), ...reminders];
  if (body.tools != null) sdk_request.tools = body.tools.map(value => {
    if (value.type && value.type !== 'custom') throw new ProtocolError('server tools forbidden');
    return select(value, ['name', 'description', 'input_schema']);
  });
  if (body.tool_choice != null) sdk_request.tool_choice = select(body.tool_choice, ['type', 'name', 'disable_parallel_tool_use']);
  if (body.stop_sequences != null) sdk_request.stop_sequences = body.stop_sequences;
  // The host chooses the model. SDK model/stream/metadata never become authority.
  const max_tokens = Math.min(Number.isInteger(body.max_tokens) && body.max_tokens > 0 ? body.max_tokens : limit, limit);
  boundToolPreviews(sdk_request);
  if (Buffer.byteLength(JSON.stringify(sdk_request)) > MAX_MODEL_REQUEST_BYTES) throw new ProtocolError('SDK request exceeds Task frame bound');
  return { sdk_request, max_tokens };
}

export function streamedMessage(message) {
  const events = [];
  const emit = (event, data) => events.push(`event: ${event}\ndata: ${JSON.stringify({ type: event, ...data })}\n\n`);
  emit('message_start', { message: { ...message, content: [], stop_reason: null, stop_sequence: null } });
  message.content.forEach((block, index) => {
    if (block.type === 'text') {
      emit('content_block_start', { index, content_block: { type: 'text', text: '' } });
      emit('content_block_delta', { index, delta: { type: 'text_delta', text: block.text } });
    } else if (block.type === 'tool_use') {
      emit('content_block_start', { index, content_block: { ...block, input: {} } });
      emit('content_block_delta', { index, delta: { type: 'input_json_delta', partial_json: JSON.stringify(block.input) } });
    } else throw new ProtocolError('unsupported SDK response block');
    emit('content_block_stop', { index });
  });
  emit('message_delta', { delta: { stop_reason: message.stop_reason, stop_sequence: null }, ...(message.usage ? { usage: message.usage } : {}) });
  emit('message_stop', {});
  return events.join('');
}

export async function startProxy(bridge, { maxTokens, maxTurns, finalReportTool }) {
  let modelTurns = 0;
  const token = randomBytes(32).toString('hex');
  const server = http.createServer(async (req, res) => {
    try {
      if (req.headers.authorization !== `Bearer ${token}` && req.headers['x-api-key'] !== token) {
        res.writeHead(401); res.end(); return;
      }
      if (req.method !== 'POST' || req.url?.split('?')[0] !== '/v1/messages') {
        res.writeHead(404); res.end(); return;
      }
      const chunks = []; let size = 0;
      for await (const chunk of req) {
        size += chunk.length;
        if (size > MAX_SDK_HTTP_BYTES) throw new ProtocolError('SDK HTTP request exceeds bound');
        chunks.push(chunk);
      }
      const body = JSON.parse(Buffer.concat(chunks).toString('utf8'));
      const normalized = normalizeRequest(body, maxTokens);
      // Report acceptance is a local terminal acknowledgment, never another paid turn.
      let response;
      if (bridge.report && !bridge.failure && !bridge.cancelCommand) {
        response = { turn_id: 'report_complete', content: [], stop_reason: 'end_turn' };
      } else {
        if (finalReportTool && Number.isInteger(maxTurns)) {
          const remaining = maxTurns - modelTurns;
          const guidance = `Task model turns remaining (including this one): ${remaining}. ` +
            (remaining <= 2 ? `Submit the final report now using ${finalReportTool}. Disclose missing coverage; do not invent evidence.` :
             remaining === 3 ? 'Finish essential evidence collection and close browser sessions now. The next two turns are reserved for report submission and any validation correction.' :
             'Batch independent tool calls; reserve the last two turns for report submission and validation correction.');
          const system = normalized.sdk_request.system;
          normalized.sdk_request.system = [...(Array.isArray(system) ? system : system ? [{type:'text', text:system}] : []), {type:'text', text:guidance}];
          if (remaining <= 2 && normalized.sdk_request.tools?.some(tool => tool.name === finalReportTool)) {
            normalized.sdk_request.tool_choice = { type: 'tool', name: finalReportTool, disable_parallel_tool_use: true };
          }
        }
        modelTurns++;
        response = await bridge.model(normalized.sdk_request, normalized.max_tokens);
      }
      const message = { id: `msg_${response.turn_id}`, type: 'message', role: 'assistant', model: 'task-authorized',
        content: response.content, stop_reason: response.stop_reason, stop_sequence: null,
        ...(response.usage ? { usage: response.usage } : {}) };
      const output = body.stream ? streamedMessage(message) : JSON.stringify(message);
      res.writeHead(200, { 'content-type': body.stream ? 'text/event-stream' : 'application/json' });
      res.end(output);
    } catch (error) {
      bridge.fail(error);
      if (!res.headersSent) res.writeHead(502, { 'content-type': 'application/json' });
      res.end(JSON.stringify({ type: 'error', error: { type: 'api_error', message: 'Task host did not confirm this model operation.' } }));
    }
  });
  server.requestTimeout = 30000;
  await new Promise((resolve, reject) => { server.once('error', reject); server.listen(0, '127.0.0.1', resolve); });
  return { url: `http://127.0.0.1:${server.address().port}`, token,
    close: async () => { server.closeAllConnections(); await new Promise(resolve => server.close(resolve)); } };
}
