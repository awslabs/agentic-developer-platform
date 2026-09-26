/** One bounded SigV4 Responses request using only the admitted destination. */
import { Hash } from '@smithy/hash-node';
import { SignatureV4 } from '@smithy/signature-v4';
import { ProbeStart } from './gateway-client';

export async function invokeTaskResponses(start: ProbeStart, body: string, timeout: number,
  request: typeof fetch = fetch) {
  // Match the platform's qualified Mantle route; no arbitrary URLs or redirects.
  if (!/^(us|eu|ap|ca|sa|me|af|il|mx)-[a-z]+-\d$/.test(start.region)) throw new Error('Unsupported Responses region');
  const hostname = `bedrock-mantle.${start.region}.api.aws`;
  const path = '/openai/v1/responses';
  const payload = JSON.stringify({ ...JSON.parse(body), model: start.model_id, stream: false,
    store: false, include: ['reasoning.encrypted_content'] });
  const signer = new SignatureV4({ credentials: { accessKeyId: start.access_key_id,
    secretAccessKey: start.secret_access_key, sessionToken: start.session_token },
    region: start.region, service: 'bedrock', sha256: Hash.bind(null, 'sha256') });
  const signed = await signer.sign({ method: 'POST', protocol: 'https:', hostname, path,
    headers: { host: hostname, 'content-type': 'application/json' }, body: payload });
  const response = await request(`https://${hostname}${path}`, { method: 'POST',
    headers: signed.headers, body: payload, redirect: 'error', signal: AbortSignal.timeout(timeout * 1000) });
  const reader = response.body?.getReader();
  if (!reader) throw new Error('Missing Responses receipt');
  const chunks: Uint8Array[] = [];
  let bytes = 0;
  try {
    while (true) {
      const part = await reader.read();
      if (part.done) break;
      bytes += part.value.byteLength;
      if (bytes > 65536) throw new Error('Responses receipt exceeds bound');
      chunks.push(part.value);
    }
  } finally {
    await reader.cancel().catch(() => undefined);
  }
  return { body: Buffer.concat(chunks), status: response.status,
    requestId: response.headers.get('x-amzn-requestid') || response.headers.get('x-request-id') || undefined };
}

export function validResponsesProbe(document: any, tools: boolean): boolean {
  if (!document || document.object !== 'response' || document.status !== 'completed' ||
      typeof document.id !== 'string' || !document.id || document.error != null ||
      document.incomplete_details != null || !Array.isArray(document.output)) return false;
  const output = document.output.filter((item: any) => item?.type !== 'reasoning');
  if (output.length !== 1) return false;
  const item = output[0];
  if (!item || typeof item !== 'object') return false;
  if (tools) {
    if (item.type !== 'function_call' || item.namespace !== 'mcp__adp' || item.name !== 'task_probe' ||
        typeof item.call_id !== 'string' || !item.call_id || typeof item.arguments !== 'string') return false;
    try {
      const args = JSON.parse(item.arguments);
      return args && typeof args === 'object' && Object.keys(args).length === 1 && args.value === 'OK';
    } catch { return false; }
  }
  return item.type === 'message' && item.role === 'assistant' && item.status === 'completed' &&
    Array.isArray(item.content) && item.content.length === 1 && item.content[0]?.type === 'output_text' &&
    typeof item.content[0].text === 'string' && /^OK\.?$/.test(item.content[0].text.trim());
}
