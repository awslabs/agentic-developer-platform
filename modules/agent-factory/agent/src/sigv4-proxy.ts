#!/usr/bin/env node
/**
 * SigV4 Re-signing Proxy for Bedrock Gateway
 *
 * The Claude Code SDK signs with service="bedrock" but API Gateway needs
 * service="execute-api". This proxy strips the incoming auth headers and
 * re-signs with the correct service using the runner's ambient AWS credentials.
 *
 * Uses only packages already present in node_modules (no extra installs):
 *   @smithy/signature-v4, @smithy/hash-node, @aws-sdk/credential-provider-node
 *
 * Usage:
 *   npx ts-node src/sigv4-proxy.ts \
 *     --target https://APIGW_ID.execute-api.REGION.amazonaws.com/STAGE/agent \
 *     --port 8080 --region us-east-1
 */

import * as http from 'http';
import * as https from 'https';
import { URL } from 'url';
import { SignatureV4 } from '@smithy/signature-v4';
import { Hash } from '@smithy/hash-node';
import { defaultProvider } from '@aws-sdk/credential-provider-node';

const args = process.argv.slice(2);
const get = (flag: string, def: string) => {
  const i = args.indexOf(flag);
  return i !== -1 && args[i + 1] ? args[i + 1] : def;
};

const TARGET    = get('--target', process.env.SIGV4_PROXY_TARGET || '');
const PORT      = parseInt(get('--port', process.env.SIGV4_PROXY_PORT || '8080'), 10);
const REGION    = get('--region', process.env.AWS_REGION || 'us-east-1');
const TENANT_ID = process.env.TENANT_ID || '';

if (!TARGET) { console.error('ERROR: --target is required'); process.exit(1); }

const targetUrl = new URL(TARGET);

const STRIP = new Set([
  'authorization', 'x-amz-security-token', 'x-amz-date',
  'x-amz-content-sha256', 'host',
]);

const signer = new SignatureV4({
  credentials: defaultProvider(),
  region: REGION,
  service: 'execute-api',
  sha256: Hash.bind(null, 'sha256'),
});

const server = http.createServer(async (req, res) => {
  // Health-check endpoint for entrypoint readiness probe (issue #747)
  if (req.url === '/__health') {
    res.writeHead(200, { 'content-type': 'text/plain' });
    res.end('ok');
    return;
  }

  const method = req.method || 'GET';
  const upstreamPath = targetUrl.pathname.replace(/\/$/, '') + (req.url || '/');
  const upstreamUrl  = `${targetUrl.protocol}//${targetUrl.host}${upstreamPath}`;

  // Collect body
  const chunks: Buffer[] = [];
  for await (const chunk of req) chunks.push(chunk as Buffer);
  const body = Buffer.concat(chunks);

  // Clean headers — strip old auth
  const headers: Record<string, string> = { host: targetUrl.host };
  for (const [k, v] of Object.entries(req.headers)) {
    if (!STRIP.has(k.toLowerCase()) && typeof v === 'string') {
      headers[k.toLowerCase()] = v;
    }
  }

  // Inject tenant identity header (Phase 2, issue #747)
  if (TENANT_ID) {
    headers['x-agent-orgid'] = TENANT_ID;
  }

  // Re-sign with execute-api
  let signed: { headers: Record<string, string> };
  try {
    signed = await signer.sign({
      method,
      hostname: targetUrl.hostname,
      path: upstreamPath,
      protocol: targetUrl.protocol,
      headers,
      body: body.length ? body : undefined,
    });
  } catch (err) {
    console.error('[proxy] signing error:', err);
    res.writeHead(502); res.end('Proxy signing error'); return;
  }

  console.log(`[proxy] → ${method} ${upstreamUrl} (${body.length}b)`);

  // Response-body idle timeout: if upstream sends headers then stalls mid-stream,
  // destroy the connection so the SDK sees a broken stream and retries.
  const RESP_IDLE_MS = 180_000; // 3 minutes

  const parsed = new URL(upstreamUrl);
  const proxyReq = https.request({
    hostname: parsed.hostname,
    port: parsed.port || 443,
    path: parsed.pathname + (parsed.search || ''),
    method,
    headers: signed.headers,
    timeout: 3600_000, // 1 hour — match Bedrock's maximum response time
  }, (proxyRes) => {
    console.log(`[proxy] ← ${proxyRes.statusCode}`);
    res.writeHead(proxyRes.statusCode || 502, proxyRes.headers);

    // Idle watchdog on the response body — upstream can send headers then stall.
    let idleTimeout = setTimeout(() => {
      console.error(`[proxy] response idle timeout: no data for ${RESP_IDLE_MS / 1000}s — destroying stream`);
      proxyRes.destroy(new Error('response idle timeout'));
    }, RESP_IDLE_MS);
    const bumpIdle = () => {
      clearTimeout(idleTimeout);
      idleTimeout = setTimeout(() => {
        console.error(`[proxy] response idle timeout: no data for ${RESP_IDLE_MS / 1000}s — destroying stream`);
        proxyRes.destroy(new Error('response idle timeout'));
      }, RESP_IDLE_MS);
    };
    proxyRes.on('data', bumpIdle);
    proxyRes.on('end', () => clearTimeout(idleTimeout));

    // Handle errors on the response stream (e.g. from idle-timeout destroy).
    // Without this handler, a destroyed proxyRes emits an unhandled 'error'
    // which would crash the proxy process.
    proxyRes.on('error', (err) => {
      console.error('[proxy] response stream error:', err.message);
      if (!res.headersSent) { res.writeHead(502); res.end('Upstream error'); }
      else if (!res.destroyed) res.destroy();
    });

    proxyRes.pipe(res);
  });

  // Enforce the request-level timeout — the declared `timeout` option only
  // emits a 'timeout' event; without this handler the socket is never aborted.
  proxyReq.on('timeout', () => {
    console.error('[proxy] upstream request timeout — destroying connection');
    proxyReq.destroy(new Error('upstream request timeout'));
  });

  proxyReq.on('error', (err) => {
    console.error('[proxy] upstream error:', err.message);
    if (!res.headersSent) { res.writeHead(502); res.end('Upstream error'); }
    else if (!res.destroyed) res.destroy();
  });

  if (body.length) proxyReq.write(body);
  proxyReq.end();
});

server.listen(PORT, '127.0.0.1', () => {
  console.log(`[sigv4-proxy] listening on http://127.0.0.1:${PORT}`);
  console.log(`[sigv4-proxy] target: ${TARGET} | region: ${REGION}`);
});

// Set server-level timeouts to match Bedrock's maximum response time.
// Default Node.js socket timeout would disconnect before large model responses complete.
// API Gateway integration timeout is 900s, Bedrock can take up to 3600s for large contexts.
server.setTimeout(3600_000);        // 1 hour socket timeout
server.keepAliveTimeout = 3600_000; // 1 hour keep-alive
