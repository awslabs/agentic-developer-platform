import * as http from 'node:http';
import { Hash } from '@smithy/hash-node';
import { SignatureV4 } from '@smithy/signature-v4';
import type { AwsCredentialIdentity } from '@smithy/types';
import { requestShapeSha256 } from './canonical-json';

const MAX_REQUEST_BYTES = 16 * 1024 * 1024;
const AUTH_HEADERS = new Set([
  'authorization',
  'host',
  'x-amz-content-sha256',
  'x-amz-date',
  'x-amz-security-token',
]);

export interface CapturedBedrockRequest {
  path: string;
  requestShapeSha256: string;
  providerRequestId: string | null;
  providerStatus: number | null;
  providerErrorCode: string | null;
  forwarded: boolean;
}

export interface CaptureProxyOptions {
  modelId: string;
  region: string;
  credentials: AwsCredentialIdentity;
  expectedRequestShapeSha256?: string;
  /** Test/manifest seam. Production always uses the regional Bedrock endpoint. */
  upstreamBaseUrl?: string;
  /** Test/manifest seam for inspecting the fixed, non-secret probe body. */
  onCapturedBody?: (body: unknown) => void;
  /** Stop the SDK promptly after a local shape refusal; no provider call exists. */
  onRequestRejected?: () => void;
  signal?: AbortSignal;
}

export interface CaptureProxy {
  baseUrl: string;
  captured(): Promise<CapturedBedrockRequest>;
  snapshot(): CapturedBedrockRequest | null;
  close(): Promise<void>;
}

function bedrockEndpoint(region: string): string {
  if (!region || /[^a-z0-9-]/.test(region)) throw new Error('Probe region is malformed');
  const suffix = region.startsWith('cn-') ? 'amazonaws.com.cn' : 'amazonaws.com';
  return `https://bedrock-runtime.${region}.${suffix}`;
}

function errorCode(body: Buffer): string | null {
  try {
    const value = JSON.parse(body.toString('utf8')) as Record<string, unknown>;
    const code = value.__type ?? value.code ?? value.error_code;
    return typeof code === 'string' && code.length > 0 ? code.split('#').pop() ?? code : null;
  } catch {
    return null;
  }
}

function modelFromPath(pathname: string): string | null {
  const match = pathname.match(/^\/model\/([^/]+)\/(?:invoke|invoke-with-response-stream)$/);
  if (!match) return null;
  try { return decodeURIComponent(match[1]); }
  catch { return null; }
}

async function listen(server: http.Server): Promise<number> {
  await new Promise<void>((resolve, reject) => {
    server.once('error', reject);
    server.listen(0, '127.0.0.1', () => {
      server.off('error', reject);
      resolve();
    });
  });
  const address = server.address();
  if (!address || typeof address === 'string') throw new Error('capture proxy did not bind a TCP port');
  return address.port;
}

export async function startCaptureProxy(options: CaptureProxyOptions): Promise<CaptureProxy> {
  const endpoint = bedrockEndpoint(options.region);
  const upstream = new URL(options.upstreamBaseUrl ?? endpoint);
  if (upstream.pathname !== '/' || upstream.search || upstream.hash) {
    throw new Error('Bedrock upstream must be an origin without a path, query or fragment');
  }

  const signer = new SignatureV4({
    credentials: options.credentials,
    region: options.region,
    service: 'bedrock',
    sha256: Hash.bind(null, 'sha256'),
  });

  let resolveCapture!: (capture: CapturedBedrockRequest) => void;
  let rejectCapture!: (error: Error) => void;
  let settled = false;
  let capturedValue: CapturedBedrockRequest | null = null;
  let requestAdmitted = false;
  const capture = new Promise<CapturedBedrockRequest>((resolve, reject) => {
    resolveCapture = resolve;
    rejectCapture = reject;
  });
  const settle = (value: CapturedBedrockRequest) => {
    if (!settled) {
      settled = true;
      capturedValue = value;
      resolveCapture(value);
    }
  };

  const server = http.createServer(async (request, response) => {
    try {
      if (request.method !== 'POST' || !request.url) {
        response.writeHead(405).end('only Bedrock POST invocation is accepted');
        return;
      }
      const requestUrl = new URL(request.url, 'http://127.0.0.1');
      if (requestUrl.search || modelFromPath(requestUrl.pathname) !== options.modelId) {
        response.writeHead(403).end('request does not match the Gateway-selected model');
        return;
      }
      // maxRetries: 0 bounds ADP's wrapper, but the embedded provider client
      // may also have retry/fallback behavior. The proxy is the final physical
      // at-most-once boundary: only the first valid invocation can be sent.
      if (requestAdmitted) {
        response.writeHead(409).end('this probe slot already admitted one request');
        return;
      }
      requestAdmitted = true;

      const chunks: Buffer[] = [];
      let length = 0;
      for await (const chunk of request) {
        const bytes = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
        length += bytes.length;
        if (length > MAX_REQUEST_BYTES) throw new Error('Claude SDK request exceeds capture limit');
        chunks.push(bytes);
      }
      const body = Buffer.concat(chunks);
      const digest = requestShapeSha256(body);
      options.onCapturedBody?.(JSON.parse(body.toString('utf8')) as unknown);
      const path = requestUrl.pathname + requestUrl.search;

      if (options.expectedRequestShapeSha256 && digest !== options.expectedRequestShapeSha256) {
        settle({
          path,
          requestShapeSha256: digest,
          providerRequestId: null,
          providerStatus: null,
          providerErrorCode: 'request_shape_mismatch',
          forwarded: false,
        });
        response.writeHead(409, { 'content-type': 'application/json' });
        response.end(JSON.stringify({ error: 'request_shape_mismatch' }));
        options.onRequestRejected?.();
        return;
      }

      const headers: Record<string, string> = { host: upstream.host };
      for (const [name, value] of Object.entries(request.headers)) {
        if (!AUTH_HEADERS.has(name.toLowerCase()) && typeof value === 'string') {
          headers[name.toLowerCase()] = value;
        }
      }
      const signed = await signer.sign({
        method: 'POST',
        protocol: upstream.protocol,
        hostname: upstream.hostname,
        port: upstream.port ? Number(upstream.port) : undefined,
        path,
        headers,
        body,
      });
      // Record that a paid call may now exist before awaiting response headers.
      // If the timeout lands after fetch starts, this prevents the caller from
      // misreporting the attempt as "no_request_emitted".
      capturedValue = {
        path,
        requestShapeSha256: digest,
        providerRequestId: null,
        providerStatus: null,
        providerErrorCode: 'provider_response_indeterminate',
        forwarded: true,
      };
      const providerResponse = await fetch(new URL(path, upstream), {
        method: 'POST',
        headers: signed.headers as Record<string, string>,
        body,
        signal: options.signal,
      });
      const providerBody = Buffer.from(await providerResponse.arrayBuffer());
      const providerRequestId = providerResponse.headers.get('x-amzn-requestid');
      const providerErrorCode = providerResponse.ok ? null : errorCode(providerBody);
      settle({
        path,
        requestShapeSha256: digest,
        providerRequestId,
        providerStatus: providerResponse.status,
        providerErrorCode,
        forwarded: true,
      });

      const responseHeaders: Record<string, string> = {};
      providerResponse.headers.forEach((value, name) => { responseHeaders[name] = value; });
      response.writeHead(providerResponse.status, responseHeaders);
      response.end(providerBody);
    } catch (error) {
      if (!settled) {
        settled = true;
        rejectCapture(error as Error);
      }
      if (!response.headersSent) response.writeHead(502, { 'content-type': 'text/plain' });
      response.end('Bedrock capture proxy failure');
    }
  });

  const port = await listen(server);
  return {
    baseUrl: `http://127.0.0.1:${port}`,
    captured: () => capture,
    snapshot: () => capturedValue,
    close: async () => {
      await new Promise<void>((resolve, reject) => server.close((error) => {
        if (error && (error as NodeJS.ErrnoException).code !== 'ERR_SERVER_NOT_RUNNING') reject(error);
        else resolve();
      }));
    },
  };
}
