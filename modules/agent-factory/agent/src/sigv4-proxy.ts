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
import { workerAwsCredentialProvider, workerIdentityHeaders, readIdentityToken, gatewaySigningRegion } from './lib/runIdentity';
import { handleKnowledgeBridge } from './lib/knowledgeBridge';
import { proxyPort } from './lib/proxyPort';
import { responsesOutputDefault, withResponsesOutputBound } from './lib/responsesOutputBound';

const args = process.argv.slice(2);
const get = (flag: string, def: string) => {
  const i = args.indexOf(flag);
  return i !== -1 && args[i + 1] ? args[i + 1] : def;
};

const TARGET    = get('--target', process.env.SIGV4_PROXY_TARGET || '');
const PORT      = parseInt(get('--port', proxyPort()), 10);
const TENANT_ID = process.env.TENANT_ID || '';
const AGENT_RUN_ID = process.env.ADP_MESSAGE_ID || '';
const AGENT_CORRELATION_ID = process.env.ADP_CORRELATION_ID || '';
const RESPONSES_OUTPUT_DEFAULT = responsesOutputDefault();

if (!TARGET) { console.error('ERROR: --target is required'); process.exit(1); }

const targetUrl = new URL(TARGET);
const REGION = get('--region', gatewaySigningRegion(TARGET));

const STRIP = new Set([
  'authorization', 'x-amz-security-token', 'x-amz-date',
  'x-amz-content-sha256', 'host',
  'x-adp-run-credential', 'x-adp-workload-token', 'x-adp-report-credential',
]);

let platformCredentials: Awaited<ReturnType<typeof workerAwsCredentialProvider>>;
const signer = new SignatureV4({
  credentials: async () => {
    platformCredentials ??= await workerAwsCredentialProvider();
    return platformCredentials();
  },
  region: REGION,
  service: 'execute-api',
  sha256: Hash.bind(null, 'sha256'),
});

const server = http.createServer(async (req, res) => {
  if (await handleKnowledgeBridge(req, res)) return;
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
  const originalBody = Buffer.concat(chunks);
  // Codex can omit this optional provider field. Send a real output bound so
  // the engine can quote/reserve cost before forwarding; sign these final bytes.
  const body = method === 'POST' && !req.headers['content-encoding']
    ? withResponsesOutputBound(req.url || '/', originalBody, RESPONSES_OUTPUT_DEFAULT)
    : originalBody;

  // Clean headers — strip old auth
  const headers: Record<string, string> = { host: targetUrl.host };
  for (const [k, v] of Object.entries(req.headers)) {
    if (!STRIP.has(k.toLowerCase()) && typeof v === 'string') {
      headers[k.toLowerCase()] = v;
    }
  }

  if (body !== originalBody) {
    delete headers['transfer-encoding'];
    headers['content-length'] = String(body.length);
  }

  // Inject tenant identity header (Phase 2, issue #747)
  if (TENANT_ID) {
    headers['x-agent-orgid'] = TENANT_ID;
  }

  // Inject agent run identity headers (issue #1616: per-run cost traceability)
  if (AGENT_RUN_ID) {
    headers['x-agent-runid'] = AGENT_RUN_ID;
  }
  if (AGENT_CORRELATION_ID) {
    headers['x-agent-correlationid'] = AGENT_CORRELATION_ID;
  }

  // Re-sign with execute-api
  let signed: { headers: Record<string, string> };
  try {
    if (process.env.ADP_AGENT_AUTHORITY_ENABLED === 'true') {
      // Read refreshed identity per call; local clients cannot select a run.
      for (const [name, value] of Object.entries(workerIdentityHeaders())) {
        headers[name.toLowerCase()] = value;
      }
    } else if (process.env.ADP_RUN_REPORT_CREDENTIAL_FILE) {
      // Shared-role dispatch uses the same server-owned run assignment for
      // model attribution. Never forward a local client's chosen capability.
      headers['x-adp-report-credential'] = readIdentityToken(process.env.ADP_RUN_REPORT_CREDENTIAL_FILE);
    }
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
  //
  // Must sit ABOVE every other idle ceiling in the model path so this watchdog
  // never fires first on a healthy-but-quiet stream. The gateway's own Bedrock
  // streaming client tolerates 300s between chunks (pool/simple_pool.py), and
  // the API Gateway integration + both ALBs allow 900s. A long implementation
  // turn on a large context can legitimately go quiet for minutes (long time-to-
  // first-token, or an extended-thinking stretch), so the old 180s value severed
  // healthy streams mid-response — the SDK surfaced it as "API Error: Connection
  // closed mid-response" and the run stalled with nothing pushed (repeatedly, on
  // #4450). 600s clears those false kills while still catching a genuinely dead
  // upstream well before the 3600s socket timeout below.
  const RESP_IDLE_MS = 600_000; // 10 minutes

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
