/** A loopback transport holding only this worker's run identity, never a Door key. */
import { IncomingMessage, ServerResponse } from 'node:http';
import { SignatureV4 } from '@smithy/signature-v4';
import { Hash } from '@smithy/hash-node';
import { gatewaySigningRegion, workerAwsCredentials, workerIdentityHeaders } from './runIdentity';
import { proxyPort } from './proxyPort';

export function knowledgeBridgeUrl(): string {
  return `http://127.0.0.1:${proxyPort()}/__run/knowledge`;
}
const MAX_BODY = 1024 * 1024;
const MAX_RESPONSE = 4 * 1024 * 1024;

export function isProtectedKnowledgeRun(): boolean {
  return process.env.ADP_AGENT_AUTHORITY_ENABLED?.toLowerCase() === 'true';
}

/** Called before the model proxy's general path. No redirects or arbitrary paths. */
export async function handleKnowledgeBridge(req: IncomingMessage, res: ServerResponse): Promise<boolean> {
  if (!req.url?.startsWith('/__run/')) return false;
  const paths: Record<string, string> = {
    'POST /__run/knowledge/call': '/call',
    'POST /__run/knowledge/mcp/': '/mcp/',
    'GET /__run/knowledge/tools': '/tools',
  };
  const path = paths[`${req.method} ${req.url}`];
  if (!isProtectedKnowledgeRun() || !path) {
    res.writeHead(404); res.end('Not found'); return true;
  }
  try {
    const base = process.env.ADP_GATEWAY_ENDPOINT || '';
    const endpoint = new URL(base);
    if (endpoint.protocol !== 'https:' || endpoint.username || endpoint.password || endpoint.search || endpoint.hash) {
      throw new Error('Invalid gateway');
    }
    endpoint.pathname = endpoint.pathname.replace(/\/+$/, '') + '/internal/v1/agent/self/knowledge' + path;
    const chunks: Buffer[] = [];
    let size = 0;
    for await (const chunk of req) {
      size += Buffer.byteLength(chunk);
      if (size > MAX_BODY) {
        res.writeHead(413); res.end('Request too large'); return true;
      }
      chunks.push(Buffer.from(chunk));
    }
    const body = Buffer.concat(chunks);
    const signer = new SignatureV4({
      credentials: workerAwsCredentials(), region: gatewaySigningRegion(base),
      service: 'execute-api', sha256: Hash.bind(null, 'sha256'),
    });
    // Reread after upload. Never forward client ACL/auth.
    const headers = {
      host: endpoint.host, 'content-type': 'application/json',
      accept: 'application/json, text/event-stream', ...workerIdentityHeaders(),
    };
    const signed = await signer.sign({
      method: req.method!, hostname: endpoint.hostname, path: endpoint.pathname,
      protocol: endpoint.protocol, headers, body: body.length ? body : undefined,
    });
    const upstream = await fetch(endpoint, {
      method: req.method, headers: signed.headers, body: body.length ? body : undefined,
      redirect: 'error', signal: AbortSignal.timeout(35_000),
    });
    if (!upstream.ok) {
      await upstream.body?.cancel();
      res.writeHead(upstream.status); res.end('Knowledge request refused'); return true;
    }
    const responseChunks: Uint8Array[] = [];
    let responseSize = 0;
    const reader = upstream.body?.getReader();
    if (reader) {
      try {
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          responseSize += value.length;
          if (responseSize > MAX_RESPONSE) throw new Error('Response too large');
          responseChunks.push(value);
        }
      } finally { await reader.cancel(); reader.releaseLock(); }
    }
    res.writeHead(upstream.status, {
      'content-type': upstream.headers.get('content-type') || 'application/json',
      'cache-control': 'no-store',
    });
    res.end(Buffer.concat(responseChunks));
  } catch {
    // Provider and HTTP errors can contain credentials or upstream headers.
    res.writeHead(502); res.end('Knowledge service unavailable');
  }
  return true;
}
