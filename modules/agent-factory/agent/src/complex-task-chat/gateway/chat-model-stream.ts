import { z } from 'zod';

export const MODEL_STREAM_TYPE = 'application/x-ndjson';
const MAX_FRAME_BYTES = 65_536;
const MAX_STREAM_BYTES = 8 * 1024 * 1024;
const MAX_FRAMES = 16_384;
const identifier = z.string().regex(/^[A-Za-z0-9_.:-]{1,128}$/);
const bindingSchema = z.object({
  run_id: identifier, session_id: identifier, operation_id: identifier,
  lease_generation: z.number().int().positive(), request_digest: z.string().regex(/^[a-f0-9]{64}$/),
}).strict();
const base = bindingSchema.extend({ sequence: z.number().int().min(0).max(MAX_FRAMES - 1) });
const frameSchema = z.discriminatedUnion('type', [
  base.extend({ type: z.literal('text_delta'), index: z.number().int().min(0).max(63), text: z.string().min(1) }).strict(),
  base.extend({ type: z.literal('receipt'), receipt: z.unknown() }).strict(),
  base.extend({ type: z.literal('error'), code: z.enum(['denied', 'incomplete']) }).strict(),
]);

export type ModelStreamBinding = z.infer<typeof bindingSchema>;
export type ModelTextDelta = { type: 'text_delta'; index: number; text: string };
export type ModelTextListener = (event: ModelTextDelta) => void | Promise<void>;

export class ModelStreamError extends Error {
  constructor(readonly code: 'invalid_response' | 'scope_mismatch' | 'incomplete' | 'denied') {
    super(`Chat model stream ${code}`);
  }
}

export async function readModelStream(
  body: ReadableStream<Uint8Array>, binding: ModelStreamBinding, signal: AbortSignal, onText?: ModelTextListener,
): Promise<unknown> {
  const reader = body.getReader();
  const decoder = new TextDecoder('utf-8', { fatal: true });
  let rejectAbort: (error: Error) => void = () => undefined;
  const aborted = new Promise<never>((_, reject) => { rejectAbort = reject; });
  const abort = () => rejectAbort(new ModelStreamError('incomplete'));
  signal.addEventListener('abort', abort, { once: true });
  let pending = '';
  let sequence = 0;
  let bytes = 0;
  let textBytes = 0;
  let receipt: unknown;
  let terminal = false;
  const decode = (value?: Uint8Array, stream = false) => {
    try {
      return decoder.decode(value, { stream });
    } catch {
      throw new ModelStreamError('invalid_response');
    }
  };
  try {
    if (signal.aborted) throw new ModelStreamError('incomplete');
    while (true) {
      const { done, value } = await Promise.race([reader.read(), aborted]);
      if (done) {
        pending += decode();
        if (pending || !terminal) throw new ModelStreamError('incomplete');
        return receipt;
      }
      bytes += value.byteLength;
      if (bytes > MAX_STREAM_BYTES) throw new ModelStreamError('invalid_response');
      pending += decode(value, true);
      let boundary: number;
      while ((boundary = pending.indexOf('\n')) >= 0) {
        const line = pending.slice(0, boundary);
        pending = pending.slice(boundary + 1);
        if (terminal || Buffer.byteLength(line) + 1 > MAX_FRAME_BYTES) throw new ModelStreamError('invalid_response');
        let document: unknown;
        try { document = JSON.parse(line); } catch { throw new ModelStreamError('invalid_response'); }
        const parsed = frameSchema.safeParse(document);
        if (!parsed.success || parsed.data.sequence !== sequence++) throw new ModelStreamError('invalid_response');
        const frame = parsed.data;
        if (Object.entries(binding).some(([key, value]) => frame[key as keyof ModelStreamBinding] !== value)) {
          throw new ModelStreamError('scope_mismatch');
        }
        if (signal.aborted) throw new ModelStreamError('incomplete');
        if (frame.type === 'error') throw new ModelStreamError(frame.code);
        if (frame.type === 'receipt') {
          terminal = true;
          receipt = frame.receipt;
        } else {
          textBytes += Buffer.byteLength(frame.text);
          if (textBytes > MAX_FRAME_BYTES) throw new ModelStreamError('invalid_response');
          await Promise.race([Promise.resolve(onText?.({ type: 'text_delta', index: frame.index, text: frame.text })), aborted]);
        }
      }
      if (Buffer.byteLength(pending) >= MAX_FRAME_BYTES) throw new ModelStreamError('invalid_response');
    }
  } catch (error) {
    if (error instanceof ModelStreamError) throw error;
    throw new ModelStreamError('incomplete');
  } finally {
    signal.removeEventListener('abort', abort);
    await reader.cancel().catch(() => undefined);
    reader.releaseLock();
  }
}
