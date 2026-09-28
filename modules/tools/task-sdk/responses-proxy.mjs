/** Bounded Responses compatibility transport for the real Codex process.
 * The canonical Task host still selects and meters its actual model/backend.
 * This adapter never claims that a Messages backend is an OpenAI model.
 * https://developers.openai.com/api/reference/resources/responses/methods/create
 */
import http from 'node:http';
import { createHash, randomBytes } from 'node:crypto';
import { MAX_FRAME_BYTES, ProtocolError } from './protocol.mjs';
import { normalizeRequest } from './model-proxy.mjs';

const object = value => value && typeof value === 'object' && !Array.isArray(value);
const namePattern = /^[A-Za-z0-9_-]{1,128}$/;
const text = value => { if (typeof value !== 'string') throw new ProtocolError('Responses text required'); return value; };

export function responsesRequest(body, { maxTokens, allowedTools }) {
  if (!object(body) || !Array.isArray(allowedTools) || body.previous_response_id != null || body.conversation != null || body.store === true || body.background === true) {
    throw new ProtocolError('Only explicit stateless Responses input is supported');
  }
  const permitted = new Set(allowedTools);
  const toolMap = {};
  const messages = [], system = [];
  const append = (role, block) => {
    if (!['user', 'assistant'].includes(role)) throw new ProtocolError('Unsupported Responses role');
    const prior = messages.at(-1);
    if (prior?.role === role) prior.content.push(block);
    else messages.push({ role, content: [block] });
  };
  if (body.instructions != null) system.push({ type: 'text', text: text(body.instructions) });
  const items = typeof body.input === 'string' ? [{ role: 'user', content: body.input }] : body.input;
  if (!Array.isArray(items) || !items.length || items.length > 256) throw new ProtocolError('Bounded Responses input required');
  const calls = new Map(), answered = new Set();
  for (const item of items) {
    if (!object(item)) throw new ProtocolError('Invalid Responses input item');
    if (item.type === 'function_call') {
      const functionName = item.namespace ? `${item.namespace}__${item.name}` : item.name;
      if (!permitted.has(functionName) || !namePattern.test(item.call_id) || calls.has(item.call_id)) throw new ProtocolError('Unapproved or duplicate Responses call');
      let input; try { input = JSON.parse(text(item.arguments)); } catch { throw new ProtocolError('Invalid function arguments'); }
      if (!object(input)) throw new ProtocolError('Function arguments must be an object');
      calls.set(item.call_id, functionName);
      append('assistant', { type: 'tool_use', id: item.call_id, name: functionName, input });
    } else if (item.type === 'function_call_output') {
      if (!calls.has(item.call_id) || answered.has(item.call_id)) throw new ProtocolError('Uncorrelated Responses function output');
      answered.add(item.call_id);
      const content = Array.isArray(item.output) ? item.output.map(block => {
        if (!object(block) || !['input_text', 'text'].includes(block.type)) throw new ProtocolError('Unsupported function output content');
        return text(block.text);
      }).join('\n') : text(item.output);
      append('user', { type: 'tool_result', tool_use_id: item.call_id, content });
    } else if (item.type === undefined || item.type === 'message') {
      const blocks = typeof item.content === 'string' ? [{ type: 'input_text', text: item.content }] : item.content;
      if (!Array.isArray(blocks)) throw new ProtocolError('Text-only Responses messages required');
      for (const block of blocks) {
        if (!object(block) || !['input_text', 'output_text'].includes(block.type)) throw new ProtocolError('Unsupported Responses content');
        const value = { type: 'text', text: text(block.text) };
        if (['system', 'developer'].includes(item.role)) system.push(value);
        else append(item.role, value);
      }
    } else throw new ProtocolError('Unsupported Responses item; reasoning/server-tool state is not silently discarded');
  }
  // Codex advertises built-in tools independently. They are deliberately absent
  // from the model request, and generated calls are checked again below.
  const tools = [];
  const offered = [];
  for (const tool of body.tools || []) {
    if (tool.type === 'namespace') {
      if (!namePattern.test(tool.name) || !Array.isArray(tool.tools)) throw new ProtocolError('Invalid tool namespace');
      for (const child of tool.tools) offered.push({ ...child, namespace: tool.name });
    } else offered.push(tool);
  }
  for (const tool of offered) {
    const hostName = tool.namespace ? `${tool.namespace}__${tool.name}` : tool.name;
    if (!permitted.has(hostName)) continue;
    if (tool.type !== 'function' || !namePattern.test(hostName) || !object(tool.parameters) || tools.some(value => value.name === hostName)) {
      throw new ProtocolError('Invalid approved Responses function definition');
    }
    toolMap[hostName] = { name: tool.name, ...(tool.namespace ? { namespace: tool.namespace } : {}) };
    tools.push({ name: hostName, description: typeof tool.description === 'string' ? tool.description : '', input_schema: tool.parameters });
  }

  let tool_choice;
  if (object(body.tool_choice)) {
    if (body.tool_choice.type !== 'function' || !tools.some(tool => tool.name === body.tool_choice.name)) throw new ProtocolError('Unapproved tool choice');
    tool_choice = { type: 'tool', name: body.tool_choice.name };
  } else if (body.tool_choice === 'required') tool_choice = { type: 'any' };
  else if (body.tool_choice && !['auto', 'none'].includes(body.tool_choice)) throw new ProtocolError('Unsupported tool choice');
  if (body.tool_choice === 'none') tools.length = 0;
  if (tool_choice && !tools.length) throw new ProtocolError('Required tool is unavailable');
  const normalized = normalizeRequest({ messages, ...(system.length ? { system } : {}), ...(tools.length ? { tools } : {}),
    ...(tool_choice ? { tool_choice } : {}), max_tokens: body.max_output_tokens }, maxTokens);
  return { ...normalized, toolNames: tools.map(tool => tool.name), toolMap };
}

export function responsesResult(result, allowedTools, toolMap = {}) {
  if (!object(result) || !Array.isArray(result.content) || !['end_turn', 'stop_sequence', 'tool_use', 'max_tokens'].includes(result.stop_reason)) {
    throw new ProtocolError('Unconfirmed or unsupported Task model result');
  }
  const hasCalls = result.content.some(block => block.type === "tool_use");
  if ((result.stop_reason === "tool_use") !== hasCalls) throw new ProtocolError("Model stop reason does not match tool completion");
  const output = [], ids = new Set();
  for (const block of result.content) {
    if (block.type === 'text') output.push({ type: 'message', id: `msg_${result.turn_id}_${output.length}`, status: 'completed', role: 'assistant',
      content: [{ type: 'output_text', text: text(block.text), annotations: [] }] });
    else if (block.type === 'tool_use') {
      if (!allowedTools.includes(block.name) || !namePattern.test(block.id) || ids.has(block.id) || !object(block.input)) throw new ProtocolError('Model requested an unauthorized function');
      ids.add(block.id);
      output.push({ type: 'function_call', id: `fc_${block.id}`, call_id: block.id, ...(toolMap[block.name] || { name: block.name }), arguments: JSON.stringify(block.input), status: 'completed' });
    } else throw new ProtocolError('Unsupported Task model output block');
  }
  const usage = result.usage;
  if (usage != null && (!Number.isInteger(usage.input_tokens) || usage.input_tokens < 0 || !Number.isInteger(usage.output_tokens) || usage.output_tokens < 0)) {
    throw new ProtocolError('Invalid confirmed model usage');
  }
  return { id: `resp_${result.turn_id}`, object: 'response', created_at: Math.floor(Date.now() / 1000),
    status: result.stop_reason === 'max_tokens' ? 'incomplete' : 'completed', model: 'task-authorized', output,
    error: null, incomplete_details: result.stop_reason === 'max_tokens' ? { reason: 'max_output_tokens' } : null,
    usage: usage == null ? null : { input_tokens: usage.input_tokens, output_tokens: usage.output_tokens, total_tokens: usage.input_tokens + usage.output_tokens },
    parallel_tool_calls: false, store: false };
}

export function responsesEvents(response) {
  const events = [];
  const emit = (type, data) => events.push(`event: ${type}\ndata: ${JSON.stringify({ type, sequence_number: events.length, ...data })}\n\n`);
  emit('response.created', { response: { ...response, status: 'in_progress', output: [] } });
  emit('response.in_progress', { response: { ...response, status: 'in_progress', output: [] } });
  response.output.forEach((item, output_index) => {
    emit('response.output_item.added', { output_index, item: { ...item, status: 'in_progress', ...(item.type === 'message' ? { content: [] } : { arguments: '' }) } });
    if (item.type === 'message') item.content.forEach((part, content_index) => {
      const base = { item_id: item.id, output_index, content_index };
      emit('response.content_part.added', { ...base, part: { ...part, text: '' } });
      emit('response.output_text.delta', { ...base, delta: part.text });
      emit('response.output_text.done', { ...base, text: part.text });
      emit('response.content_part.done', { ...base, part });
    });
    else {
      emit('response.function_call_arguments.delta', { item_id: item.id, output_index, delta: item.arguments });
      emit('response.function_call_arguments.done', { item_id: item.id, output_index, arguments: item.arguments });
    }
    emit('response.output_item.done', { output_index, item });
  });
  emit(response.status === 'incomplete' ? 'response.incomplete' : 'response.completed', { response });
  return events.join('');
}

export async function startResponsesProxy(bridge, { maxTokens, maxRequests, allowedTools }) {
  if (!Number.isInteger(maxTokens) || maxTokens < 1 || !Number.isInteger(maxRequests) || maxRequests < 1 || !Array.isArray(allowedTools)) throw new ProtocolError('Missing bounded Responses authority');
  const token = randomBytes(32).toString('hex');
  let requests = 0;
  const receipts = new Map();
  const server = http.createServer(async (req, res) => {
    try {
      if (req.headers.authorization !== `Bearer ${token}`) { res.writeHead(401); res.end(); return; }
      if (req.method !== 'POST' || req.url !== '/v1/responses') { res.writeHead(404); res.end(); return; }
      const chunks = []; let size = 0;
      for await (const chunk of req) { size += chunk.length; if (size > MAX_FRAME_BYTES) throw new ProtocolError('Responses request exceeds Task bound'); chunks.push(chunk); }
      const body = JSON.parse(Buffer.concat(chunks).toString('utf8'));
      const request = responsesRequest(body, { maxTokens, allowedTools });
      const key = createHash('sha256').update(JSON.stringify(request)).digest('hex');
      if (!receipts.has(key)) {
        if (++requests > maxRequests) throw new ProtocolError('Codex model request ceiling reached');
        receipts.set(key, Promise.resolve().then(async () => responsesResult(
          await bridge.model(request.sdk_request, request.max_tokens), request.toolNames, request.toolMap)));
      }
      const result = await receipts.get(key);
      const output = body.stream ? responsesEvents(result) : JSON.stringify(result);
      if (Buffer.byteLength(output) > MAX_FRAME_BYTES * 4) throw new ProtocolError('Responses output exceeds bounded transport');
      res.writeHead(200, { 'content-type': body.stream ? 'text/event-stream' : 'application/json', 'cache-control': 'no-store' });
      res.end(output);
    } catch (error) {
      bridge.fail(error); // Stop the actual Codex process on uncertain delivery; never auto-replay a model request.
      if (!res.headersSent) res.writeHead(502, { 'content-type': 'application/json' });
      res.end(JSON.stringify({ error: { message: 'Task host did not confirm the Codex model operation.', type: 'server_error' } }));
    }
  });
  server.requestTimeout = 30000;
  await new Promise((resolve, reject) => { server.once('error', reject); server.listen(0, '127.0.0.1', resolve); });
  return { url: `http://127.0.0.1:${server.address().port}/v1`, token,
    close: async () => { server.closeAllConnections(); await new Promise(resolve => server.close(resolve)); } };
}
