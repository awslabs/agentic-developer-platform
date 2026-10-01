/** Supply the optional Responses output cap before signing and budget quotation. */
export function responsesOutputDefault(env: NodeJS.ProcessEnv = process.env): number {
  const value = env.SIGV4_PROXY_RESPONSES_MAX_OUTPUT_TOKENS ?? '16384';
  const limit = Number(value);
  if (!/^\d+$/.test(value) || !Number.isSafeInteger(limit) || limit < 1) {
    throw new Error('SIGV4_PROXY_RESPONSES_MAX_OUTPUT_TOKENS must be a positive integer');
  }
  return limit;
}

export function withResponsesOutputBound(path: string, body: Buffer, limit: number): Buffer {
  if (path.split('?')[0] !== '/openai/v1/responses') return body;
  let document: unknown;
  try { document = JSON.parse(body.toString('utf8')); } catch { return body; }
  if (!document || typeof document !== 'object' || Array.isArray(document)) return body;
  // Explicit limits (including invalid ones) remain the caller's request. The
  // gateway validates and quotes the exact bytes; never repair an invalid cap.
  if (Object.prototype.hasOwnProperty.call(document, 'max_output_tokens')) return body;
  return Buffer.from(JSON.stringify({ ...document, max_output_tokens: limit }));
}
