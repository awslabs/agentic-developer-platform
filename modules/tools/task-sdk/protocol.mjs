/** Credential-free bounded Task IPC. Model/tool outcomes stay host-owned. */
import { randomUUID } from 'node:crypto';

export const MAX_FRAME_BYTES = 65536;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const TASK = /^tsk_[0-9a-f-]{36}$/;
export class ProtocolError extends Error {}
export class Cancelled extends Error {}

export function frame(type, task_id, fields = {}) {
  return { protocol_version: 1, type, request_id: randomUUID(), task_id, ...fields };
}

export function encode(value) {
  const bytes = Buffer.from(JSON.stringify(value) + '\n');
  if (bytes.length > MAX_FRAME_BYTES) throw new ProtocolError('process frame exceeds limit');
  return bytes;
}

export function decode(line) {
  if (Buffer.byteLength(line) > MAX_FRAME_BYTES) throw new ProtocolError('process frame exceeds limit');
  const value = JSON.parse(line);
  if (!value || value.protocol_version !== 1 || !UUID.test(value.request_id) || !TASK.test(value.task_id)) throw new ProtocolError('invalid host frame');
  if (!['start', 'turn', 'cancel', 'control.result', 'model.result', 'cyber.result', 'tool.result', 'report.ack', 'artifact.chunk'].includes(value.type)) throw new ProtocolError('unknown host frame');
  for (const forbidden of ['run_credential', 'gateway_token', 'aws_access_key_id', 'github_token', 'api_key', 'owner_token']) {
    if (forbidden in value) throw new ProtocolError('host frame contains authority');
  }
  return value;
}

export class HostBridge {
  constructor(start, write, { allowSteering = false } = {}) {
    this.allowSteering = allowSteering;
    this.steering = [];
    this.start = start;
    this.write = write;
    this.controller = new AbortController();
    this.pending = new Map();
    this.nextTurn = null;
    this.input = null;
    this.cancelCommand = null;
    this.seenTurns = new Map();
    this.report = null;
    this.serial = Promise.resolve();
    this.evidence = new Map([['instructions', { ref: 'instructions', source: 'instructions' }]]);
    for (const key of Object.keys(start.inputs || {})) this.evidence.set('inputs.' + key, { ref: 'inputs.' + key, source: 'inputs' });
    for (const artifact of start.artifacts || []) this.evidence.set(artifact.artifact_id, { ref: artifact.artifact_id, source: 'artifact', artifact_id: artifact.artifact_id });
    this.failure = null;
  }
  send(type, fields) { this.write(frame(type, this.start.task_id, fields)); }
  async request(kind, id, fields) {
    if (this.controller.signal.aborted) throw new Cancelled();
    if (this.pending.size >= 8 || this.pending.has(id)) throw new ProtocolError('too many outstanding operations');
    return new Promise((resolve, reject) => {
      this.pending.set(id, { kind, operation: fields.operation, tool: fields.tool, resolve, reject });
      try { this.send(kind, fields); } catch (error) { this.pending.delete(id); reject(error); }
    });
  }
  exclusive(callback) {
    const operation = this.serial.then(() => { if (this.controller.signal.aborted) throw new Cancelled(); return callback(); });
    this.serial = operation.catch(() => {});
    return operation;
  }
  fail(error) {
    this.failure ??= error;
    this.controller.abort();
    for (const pending of this.pending.values()) pending.reject(error);
    this.pending.clear();
    this.input?.reject(error);
    this.input = null;
  }
  current() {
    return this.exclusive(async () => {
      const request_id = randomUUID();
      const response = await this.request('control.request', request_id, { request_id });
      if (response.current !== true) throw new ProtocolError('task authority is no longer current');
    });
  }
  takeSteering() { return this.steering.splice(0); }
  model(sdk_request, max_tokens) { return this.exclusive(() => this._model(sdk_request, max_tokens)); }
  responses(responses_request) {
    return this.exclusive(async () => {
      const turn_id = this.nextTurn || randomUUID();
      this.nextTurn = null;
      const response = await this.request('model.request', turn_id, { turn_id, responses_request });
      if (response.operation_status !== 'confirmed') throw new ProtocolError(response.operation_status === 'unknown' ? 'model_outcome_unknown' : 'model request rejected');
      if (!response.responses_response || typeof response.responses_response !== 'object') throw new ProtocolError('missing confirmed Responses content');
      return { operationStatus: 'confirmed', turnId: turn_id, response: response.responses_response };
    });
  }
  async _model(sdk_request, max_tokens) {
    if ([...this.pending.values()].some(value => value.kind === 'model.request')) throw new ProtocolError('concurrent model operation');
    const turn_id = this.nextTurn || randomUUID();
    this.nextTurn = null;
    const response = await this.request('model.request', turn_id, { turn_id, max_tokens, sdk_request });
    if (response.operation_status !== 'confirmed') throw new ProtocolError(response.operation_status === 'unknown' ? 'model_outcome_unknown' : 'model request rejected');
    if (!Array.isArray(response.content) || typeof response.stop_reason !== 'string') throw new ProtocolError('missing confirmed model content');
    return { ...response, turn_id };
  }
  async tool(name, payload, modelCall) {
    if (!/^[a-z][a-z0-9_]{0,63}\.[a-z][a-z0-9_]{0,63}$/.test(name)) throw new ProtocolError('invalid tool name');
    let model_call;
    if (modelCall !== undefined) {
      if (!modelCall || typeof modelCall !== 'object' || Array.isArray(modelCall) ||
          Object.keys(modelCall).sort().join(',') !== 'call_id,turn_id' ||
          !UUID.test(modelCall.turn_id) || typeof modelCall.call_id !== 'string' ||
          modelCall.call_id.length < 1 || modelCall.call_id.length > 200) throw new ProtocolError('invalid model call');
      model_call = { ...modelCall };
    }
    const request_id = randomUUID();
    return this.exclusive(async () => {
      const response = await this.request('tool.request', request_id, { request_id, tool: name, payload, ...(model_call ? { model_call } : {}) });
      if (response.artifact?.artifact_id && response.operation_status === 'confirmed') {
        const id = response.artifact.artifact_id;
        this.evidence.set(id, { ref: id, source: 'artifact', artifact_id: id });
      }
      return response;
    });
  }
  progress(message, stage = 'analysis') {
    if (typeof message !== 'string' || !message.trim() || message.length > 2000) throw new ProtocolError('invalid progress');
    this.send('progress', { report_id: randomUUID(), message, stage, producer_timestamp: new Date().toISOString() });
  }
  async ask(prompt) {
    if (this.input) throw new ProtocolError('concurrent input request');
    if (typeof prompt !== 'string' || !prompt.trim() || prompt.length > 2000) throw new ProtocolError('invalid input prompt');
    const input_request_id = randomUUID();
    return new Promise((resolve, reject) => {
      this.input = { input_request_id, resolve, reject };
      this.send('input.required', { input_request_id, prompt });
    });
  }
  receive(value) {
    if (value.task_id !== this.start.task_id) throw new ProtocolError('host task mismatch');
    if (value.type === 'cancel') {
      if (!UUID.test(value.command_id) || value.intentional !== true) throw new ProtocolError('invalid cancellation');
      this.cancelCommand = value.command_id;
      this.controller.abort();
      for (const pending of this.pending.values()) pending.reject(new Cancelled());
      this.pending.clear();
      this.input?.reject(new Cancelled());
      this.input = null;
    } else if (value.type === 'turn') {
      if (!UUID.test(value.turn_id) || !Array.isArray(value.messages)) throw new ProtocolError('invalid input turn');
      const body = JSON.stringify(value.messages);
      if (this.seenTurns.has(value.turn_id)) {
        if (this.seenTurns.get(value.turn_id) !== body) throw new ProtocolError('changed replayed input');
        return;
      }
      if (!this.input && !this.allowSteering) throw new ProtocolError('unsolicited Task turn');
      if (this.nextTurn !== null) throw new ProtocolError('previous Task turn has not been consumed');
      const citations = [];
      const text = value.messages.map(message => {
        if (!UUID.test(message.command_id) || typeof message.text !== 'string') throw new ProtocolError('invalid input message');
        const ref = 'follow_up_input.' + message.command_id;
        citations.push({ ref, source: 'follow_up_input' });
        return message.text;
      }).join('\n');
      this.seenTurns.set(value.turn_id, body);
      this.nextTurn = value.turn_id;
      for (const citation of citations) this.evidence.set(citation.ref, citation);
      if (this.input) {
        const input = this.input; this.input = null; input.resolve(text);
      } else this.steering.push({ turn_id: value.turn_id, text });
    } else if (value.type === 'control.result') {
      const pending = this.pending.get(value.request_id);
      if (!pending || pending.kind !== 'control.request' || typeof value.current !== 'boolean') throw new ProtocolError('uncorrelated task control');
      this.pending.delete(value.request_id); pending.resolve(value);
    } else if (value.type === 'model.result' || value.type === 'cyber.result' || value.type === 'tool.result') {
      const id = value.type === 'model.result' ? value.turn_id : value.request_id;
      const pending = this.pending.get(id);
      if (!pending || pending.kind !== value.type.replace('.result', '.request') || (value.type === 'cyber.result' && pending.operation !== value.operation) || (value.type === 'tool.result' && pending?.tool !== value.tool)) throw new ProtocolError('uncorrelated host operation');
      if (!['pending', 'unknown', 'confirmed', 'rejected'].includes(value.operation_status)) throw new ProtocolError('invalid operation status');
      if (value.type === 'model.result' && value.operation_status === 'pending') return;
      this.pending.delete(id); pending.resolve(value);
    }
  }
}
