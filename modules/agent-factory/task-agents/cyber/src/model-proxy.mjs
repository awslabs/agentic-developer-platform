/** SDK loopback transport; it holds no AWS, provider or gateway credential. */
import http from 'node:http';
import { randomBytes } from 'node:crypto';
import { MAX_FRAME_BYTES, ProtocolError } from './protocol.mjs';

export function normalizeRequest(body, limit) {
  if (!body || !Array.isArray(body.messages) || !body.messages.length) throw new ProtocolError('SDK messages required');
  const select = (value, keys) => Object.fromEntries(keys.filter(key => value[key] !== undefined && value[key] !== null).map(key => [key, value[key]]));
  const block = value => {
    if (value.type === 'text') return select(value, ['type', 'text']);
    if (value.type === 'tool_use') return select(value, ['type', 'id', 'name', 'input']);
    if (value.type === 'tool_result') {
      const result = select(value, ['type', 'tool_use_id', 'content', 'is_error']);
      if (Array.isArray(result.content)) result.content = result.content.map(item => {
        if (item.type !== 'text') throw new ProtocolError('unsupported tool result content');
        return select(item, ['type', 'text']);
      });
      return result;
    }
    throw new ProtocolError('unsupported SDK content block');
  };
  const sdk_request = { messages: body.messages.map(message => ({ role: message.role,
    content: Array.isArray(message.content) ? message.content.map(block) : message.content })) };
  if (body.system != null) sdk_request.system = Array.isArray(body.system) ? body.system.map(value => {
    if (value.type !== 'text') throw new ProtocolError('unsupported SDK system block');
    return select(value, ['type', 'text']);
  }) : body.system;
  if (body.tools != null) sdk_request.tools = body.tools.map(value => {
    if (value.type && value.type !== 'custom') throw new ProtocolError('server tools forbidden');
    return select(value, ['name', 'description', 'input_schema']);
  });
  if (body.tool_choice != null) sdk_request.tool_choice = select(body.tool_choice, ['type', 'name', 'disable_parallel_tool_use']);
  if (body.stop_sequences != null) sdk_request.stop_sequences = body.stop_sequences;
  // The host chooses the model. SDK model/stream/metadata never become authority.
  const max_tokens = Math.min(Number.isInteger(body.max_tokens) && body.max_tokens > 0 ? body.max_tokens : limit, limit);
  if (Buffer.byteLength(JSON.stringify(sdk_request)) > MAX_FRAME_BYTES - 1024) throw new ProtocolError('SDK request exceeds Task frame bound');
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

export async function startProxy(bridge, { maxTokens }) {
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
        if (size > MAX_FRAME_BYTES) throw new ProtocolError('SDK HTTP request exceeds bound');
        chunks.push(chunk);
      }
      const body = JSON.parse(Buffer.concat(chunks).toString('utf8'));
      const normalized = normalizeRequest(body, maxTokens);
      const response = await bridge.model(normalized.sdk_request, normalized.max_tokens);
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
