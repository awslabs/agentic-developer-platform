import { createHash } from 'node:crypto';
import { canonicalJson } from '../../invocability-probe/canonical-json';
import { ChatDataClient } from './chat-data-client';
import { MODEL_STREAM_TYPE, readModelStream } from './chat-model-stream';

const request = { messages: [{ role: 'user' as const, content: 'Hello' }], max_tokens: 16 };
const binding = { run_id: 'run-a', session_id: 'session-a', lease_generation: 2, operation_id: 'call-a',
  request_digest: createHash('sha256').update(canonicalJson(request)).digest('hex') };
const receipt = { ...binding, model_id: 'approved-model', status: 'confirmed', handoff: 'confirmed',
  reservation_status: 'settled', automatic_replay_permitted: false,
  content: [{ type: 'text', text: 'Résumé 😀' }], stop_reason: 'end_turn',
  usage: { input_tokens: 4, output_tokens: 2, estimated_usd: '0.01' } };
const text = { type: 'text_delta', index: 0, text: 'Résumé 😀' };
const frame = (value: object, sequence = 0) => Buffer.from(JSON.stringify({ ...binding, sequence, ...value }) + '\n');
const terminal = (sequence = 1, value = receipt) => frame({ type: 'receipt', receipt: value }, sequence);

function response(chunks: Uint8Array[], close = true, cancel = jest.fn()): Response {
  return new Response(new ReadableStream({
    start(controller) { chunks.forEach(chunk => controller.enqueue(chunk)); if (close) controller.close(); }, cancel,
  }), { headers: { 'Content-Type': MODEL_STREAM_TYPE } });
}

function parse(reply: Response, onText = jest.fn()) {
  return readModelStream(reply.body!, binding, new AbortController().signal, onText);
}

describe('bound model stream decoder', () => {
  it('handles UTF-8 split at every byte and delivers text before the terminal receipt', async () => {
    const delivered = jest.fn();
    const bytes = Buffer.concat([frame(text), terminal()]);
    const reply = response(Array.from(bytes, value => Uint8Array.of(value)));
    await expect(parse(reply, delivered)).resolves.toEqual(receipt);
    expect(delivered).toHaveBeenCalledWith(text);
    expect(reply.body?.locked).toBe(false);
  });

  it.each([
    { run_id: 'other' }, { session_id: 'other' }, { lease_generation: 3 },
    { operation_id: 'other' }, { request_digest: 'a'.repeat(64) },
  ])('refuses mismatched frame authority before exposing text: %j', async changed => {
    const delivered = jest.fn();
    await expect(parse(response([frame({ ...text, ...changed })]), delivered)).rejects.toMatchObject({ code: 'scope_mismatch' });
    expect(delivered).not.toHaveBeenCalled();
  });

  it.each([
    [frame(text, 1)], [frame(text), frame(text)], [terminal(0), frame(text)],
    [terminal(0), terminal(1)], [frame({ ...text, secret: 'private' })],
    [frame({ ...text, index: 64 })], [frame({ ...text, type: 'thinking' })],
    [frame({ ...text, text: 'x'.repeat(65_536) })], [Buffer.from('x'.repeat(65_536))],
    [Buffer.from([0xff, 0x0a])], [Buffer.from('{}\n')], [Buffer.from('\n')],
  ])('refuses invalid, oversized or out-of-order frames: case %#', async (...chunks) => {
    await expect(parse(response(chunks))).rejects.toMatchObject({ code: 'invalid_response' });
  });

  it.each([[], [frame(text)], [terminal(0).subarray(0, -1)]])('never treats a truncated response as completion', async (...chunks) => {
    await expect(parse(response(chunks))).rejects.toMatchObject({ code: 'incomplete' });
  });

  it.each(['denied', 'incomplete'])('preserves a sanitized %s failure', async code => {
    await expect(parse(response([frame({ type: 'error', code })]))).rejects.toMatchObject({ code });
  });

  it('bounds the entire wire response before parsing an oversized chunk', async () => {
    await expect(parse(response([new Uint8Array(8 * 1024 * 1024 + 1)]))).rejects.toMatchObject({ code: 'invalid_response' });
  });

  it('bounds accumulated text even when every individual frame fits', async () => {
    const delivered = jest.fn();
    const chunk = { ...text, text: 'x'.repeat(32_768) };
    await expect(parse(response([frame(chunk), frame(chunk, 1), frame(text, 2)]), delivered))
      .rejects.toMatchObject({ code: 'invalid_response' });
    expect(delivered).toHaveBeenCalledTimes(2);
  });

  it('cancels a quiet reader and releases its lock on abort', async () => {
    const cancelled = jest.fn();
    const reply = response([], false, cancelled);
    const controller = new AbortController();
    const pending = readModelStream(reply.body!, binding, controller.signal);
    controller.abort();
    await expect(pending).rejects.toMatchObject({ code: 'incomplete' });
    expect(cancelled).toHaveBeenCalledTimes(1);
    expect(reply.body?.locked).toBe(false);
  });

  it('classifies a broken transport as incomplete rather than malformed JSON', async () => {
    const body = new ReadableStream<Uint8Array>({ start(controller) { controller.error(new TypeError('socket closed')); } });
    await expect(readModelStream(body, binding, new AbortController().signal)).rejects.toMatchObject({ code: 'incomplete' });
  });

  it('does not expose more data when the text consumer is cancelled', async () => {
    const controller = new AbortController();
    const delivered = jest.fn(() => { controller.abort(); return new Promise<void>(() => undefined); });
    await expect(readModelStream(response([frame(text), terminal()]).body!, binding, controller.signal, delivered))
      .rejects.toMatchObject({ code: 'incomplete' });
    expect(delivered).toHaveBeenCalledTimes(1);
  });
});

describe('scoped client streaming integration', () => {
  let send: jest.SpiedFunction<typeof fetch>;
  let client: ChatDataClient;

  beforeEach(() => {
    send = jest.spyOn(globalThis, 'fetch').mockResolvedValueOnce(new Response(JSON.stringify({
      capability: 'synthetic.capability', run_id: binding.run_id, session_id: binding.session_id,
      lease_generation: binding.lease_generation, attempt: 1, expires_at: Math.floor(Date.now() / 1000) + 300,
    }), { headers: { 'Content-Type': 'application/json' } }));
    client = new ChatDataClient({ baseUrl: 'https://gateway.example.test', workloadToken: async () => 'sandbox.token' });
  });
  afterEach(() => jest.restoreAllMocks());

  it('delivers a provisional delta before returning the validated usage receipt', async () => {
    let controller!: ReadableStreamDefaultController<Uint8Array>;
    send.mockResolvedValueOnce(new Response(new ReadableStream({ start(value) { controller = value; } }), {
      headers: { 'Content-Type': MODEL_STREAM_TYPE },
    }));
    let sawText!: () => void;
    const delivered = new Promise<void>(resolve => { sawText = resolve; });
    let completed = false;
    const pending = client.invokeModel('call-a', request, event => { expect(event).toEqual(text); sawText(); })
      .then(value => { completed = true; return value; });
    controller.enqueue(frame(text));
    await delivered;
    expect(completed).toBe(false);
    controller.enqueue(terminal());
    controller.close();
    await expect(pending).resolves.toMatchObject({ content: receipt.content, usage: receipt.usage });
    expect(send.mock.calls[1][1]).toMatchObject({ redirect: 'error', headers: {
      Accept: MODEL_STREAM_TYPE, Authorization: 'Bearer synthetic.capability', 'X-Adp-Workload-Token': 'sandbox.token',
    } });
  });

  it('does not retry an interrupted stream or duplicate already displayed text', async () => {
    const delivered = jest.fn();
    send.mockResolvedValueOnce(response([frame(text)]));
    await expect(client.invokeModel('call-a', request, delivered)).rejects.toMatchObject({ code: 'incomplete' });
    expect(delivered).toHaveBeenCalledTimes(1);
    expect(send).toHaveBeenCalledTimes(2);
  });

  it.each([
    { usage: { ...receipt.usage, output_tokens: 17 } }, { reservation_status: 'unknown' },
    { run_id: 'other' }, { automatic_replay_permitted: true },
  ])('still validates the receipt after streaming: %j', async changed => {
    send.mockResolvedValueOnce(response([frame(text), terminal(1, { ...receipt, ...changed })]));
    await expect(client.invokeModel('call-a', request)).rejects.toHaveProperty('code');
    expect(send).toHaveBeenCalledTimes(2);
  });
});
