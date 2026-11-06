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
import { modelAdmissionProgress, AdmissionStatus } from './lib/modelAdmissionProgress';

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
const BUDGET_WAIT_MAX_SECONDS = Number(process.env.ADP_BUDGET_WAIT_MAX_SECONDS ?? '4200');

if (!TARGET) { console.error('ERROR: --target is required'); process.exit(1); }
if (!Number.isFinite(BUDGET_WAIT_MAX_SECONDS) || BUDGET_WAIT_MAX_SECONDS <= 0) {
  console.error('ERROR: ADP_BUDGET_WAIT_MAX_SECONDS must be a positive number'); process.exit(1);
}

const targetUrl = new URL(TARGET);
const toolsTargetUrl = (() => {
  const configured = process.env.ADP_TOOLS_GATEWAY_URL;
  if (!configured) return null;
  try {
    const url = new URL(configured);
    if (url.protocol !== 'https:' || url.username || url.password || url.search || url.hash ||
        !url.pathname.endsWith('/agent/tools')) throw new Error('invalid tools gateway');
    return url;
  } catch { console.error('ERROR: ADP_TOOLS_GATEWAY_URL must be an HTTPS /agent/tools URL'); process.exit(1); }
})();
const REGION = get('--region', gatewaySigningRegion(TARGET));
const progressPath = (() => {
  if (process.env.ADP_AGENT_AUTHORITY_ENABLED !== 'true') return null;
  try {
    const control = new URL(process.env.ADP_AGENT_CONTROL_ENDPOINT || '');
    const prefix = targetUrl.pathname.slice(0, -'/agent'.length);
    if (!targetUrl.pathname.endsWith('/agent') || control.origin !== targetUrl.origin ||
        control.protocol !== 'https:' || control.pathname !== prefix + '/internal/v1/agent' ||
        control.search || control.hash || control.username || control.password) return null;
    return control.pathname + '/self/status';
  } catch { return null; }
})();

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

const toolsSigner = toolsTargetUrl ? new SignatureV4({
  credentials: async () => {
    platformCredentials ??= await workerAwsCredentialProvider();
    return platformCredentials();
  },
  region: gatewaySigningRegion(toolsTargetUrl.href), service: 'execute-api', sha256: Hash.bind(null, 'sha256'),
}) : null;

// 503 execution_policy_refused reasons the gateway itself classifies as unavailable
// rather than denied (gateway model_identity.py): the meter heals within minutes.
const RETRYABLE_POLICY_REASONS = new Set(['spend_unknown', 'budget_unavailable', 'budget_enforcement_unavailable']);

let admissionSequence = 0;
const reportModelAdmission = (status: AdmissionStatus, remaining: number): Promise<void> => {
  if (!progressPath) return Promise.resolve();
  admissionSequence = Math.max(Date.now(), admissionSequence + 1);
  const body = JSON.stringify({
    status: 'in_progress',
    model_admission_status: status,
    model_admission_sequence: String(admissionSequence),
    summary: status === 'not_waiting' ? 'Model admission is no longer waiting for budget.'
      : `Waiting for budget holds to settle; retrying model admission (up to ${Math.ceil(remaining / 1000)}s remaining)`,
  });
  return new Promise(resolve => {
    let finished = false;
    let request: http.ClientRequest | undefined;
    const finish = (failed: boolean) => {
      if (finished) return;
      finished = true;
      clearTimeout(timer);
      request?.destroy();
      if (failed) console.error('[proxy] budget wait progress unavailable');
      resolve();
    };
    const timer = setTimeout(() => finish(true), 5000);
    void (async () => {
      try {
        const signed = await signer.sign({
          method: 'POST', hostname: targetUrl.hostname, protocol: targetUrl.protocol,
          path: progressPath, headers: {
            host: targetUrl.host, 'content-type': 'application/json',
            'content-length': String(Buffer.byteLength(body)), ...workerIdentityHeaders(),
          }, body,
        });
        if (finished) return;
        request = https.request({
          hostname: targetUrl.hostname, port: targetUrl.port || 443,
          path: progressPath, method: 'POST', headers: signed.headers,
        }, response => { response.resume(); finish(response.statusCode !== 200); });
        request.once('error', () => finish(true));
        request.end(body);
      } catch { finish(true); }
    })();
  });
};
const admissionProgress = modelAdmissionProgress(reportModelAdmission);

const server = http.createServer(async (req, res) => {
  if (await handleKnowledgeBridge(req, res)) return;
  // Health-check endpoint for entrypoint readiness probe (issue #747)
  if (req.url === '/__health') {
    res.writeHead(200, { 'content-type': 'text/plain' });
    res.end('ok');
    return;
  }

  const method = req.method || 'GET';
  const toolsRoute = /^\/tools(?:\/|\?|$)/.test(req.url || '');
  if (toolsRoute && (!toolsTargetUrl || process.env.ADP_AGENT_AUTHORITY_ENABLED !== 'true' ||
      !/^\/tools(?:\/[A-Za-z0-9_-]+)*$/.test((req.url || '').split('?')[0]))) {
    res.writeHead(404); res.end(); return;
  }
  const upstream = toolsRoute ? toolsTargetUrl! : targetUrl;
  const upstreamPath = toolsRoute
    ? upstream.pathname + (req.url || '').slice('/tools'.length)
    : targetUrl.pathname.replace(/\/$/, '') + (req.url || '/');
  const upstreamUrl = `${upstream.protocol}//${upstream.host}${upstreamPath}`;

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
  const headers: Record<string, string> = { host: upstream.host };
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
  const query: Record<string, string[]> = Object.create(null);
  parsed.searchParams.forEach((value, name) => { (query[name] ??= []).push(value); });
  const expiry = (value: string | undefined) => {
    const parsedExpiry = Date.parse(value || '');
    return Number.isFinite(parsedExpiry) ? parsedExpiry : value ? 0 : Infinity;
  };
  const deadline = Math.min(Date.now() + BUDGET_WAIT_MAX_SECONDS * 1000,
    expiry(process.env.ADP_POD_DEADLINE_AT), expiry(process.env.ADP_ACCEPTED_POLICY_EXPIRES_AT));
  let proxyReq: http.ClientRequest | undefined;
  let retryTimer: ReturnType<typeof setTimeout> | undefined;
  let attempt = 0;
  const admissionKey = Symbol();
  res.once('close', () => {
    admissionProgress.clear(admissionKey);
    if (!res.writableFinished) {
      if (retryTimer) clearTimeout(retryTimer);
      proxyReq?.destroy();
    }
  });

  const exhausted = () => {
    admissionProgress.clear(admissionKey);
    if (res.destroyed) return;
    res.writeHead(409, { 'content-type': 'application/json' });
    res.end(JSON.stringify({ error: 'budget_wait_exhausted', retryable: false }));
  };
  const retryAfterMs = (value: string | string[] | undefined) => {
    if (typeof value !== 'string') return 0;
    const seconds = Number(value);
    if (value.trim() && Number.isFinite(seconds) && seconds >= 0) return seconds * 1000;
    const date = Date.parse(value);
    return Number.isFinite(date) ? Math.max(0, date - Date.now()) : 0;
  };

  const dispatch = async (): Promise<void> => {
    if (res.destroyed) return;
    if (attempt && Date.now() >= deadline) { exhausted(); return; }
    let signed: { headers: Record<string, string> };
    try {
      if (process.env.ADP_AGENT_AUTHORITY_ENABLED === 'true') {
        for (const [name, value] of Object.entries(workerIdentityHeaders())) {
          headers[name.toLowerCase()] = value;
        }
      } else if (process.env.ADP_RUN_REPORT_CREDENTIAL_FILE) {
        headers['x-adp-report-credential'] = readIdentityToken(process.env.ADP_RUN_REPORT_CREDENTIAL_FILE);
      }
      signed = await (toolsRoute ? toolsSigner! : signer).sign({
        method, hostname: upstream.hostname, path: parsed.pathname, query,
        protocol: upstream.protocol, headers, body: body.length ? body : undefined,
      });
    } catch (err) {
      if (res.destroyed) return;
      console.error('[proxy] signing error:', err);
      res.writeHead(502); res.end('Proxy signing error'); return;
    }
    if (res.destroyed) return;
    if (attempt && Date.now() >= deadline) { exhausted(); return; }
    console.log(`[proxy] → ${method} ${upstreamUrl} (${body.length}b)`);

    const upstreamRequest = https.request({
      hostname: parsed.hostname,
      port: parsed.port || 443,
      path: parsed.pathname + (parsed.search || ''),
      method,
      headers: signed.headers,
      timeout: 3600_000, // 1 hour — match Bedrock's maximum response time
    }, (proxyRes) => {
      if (res.destroyed) { proxyRes.destroy(); return; }
      console.log(`[proxy] ← ${proxyRes.statusCode}`);

      if (!toolsRoute && (proxyRes.statusCode === 429 || proxyRes.statusCode === 502 || proxyRes.statusCode === 503) &&
          /\bapplication\/json\b/i.test(String(proxyRes.headers['content-type'] || ''))) {
        const chunks: Buffer[] = [];
        let length = 0;
        let forwarded = false;
        const forward = () => {
          if (forwarded || res.destroyed) return;
          admissionProgress.clear(admissionKey);
          forwarded = true;
          res.writeHead(proxyRes.statusCode || 502, proxyRes.headers);
          for (const chunk of chunks) res.write(chunk);
        };
        const bodyTimeout = setTimeout(() => proxyRes.destroy(new Error('budget response timeout')), 30_000);
        proxyRes.once('close', () => clearTimeout(bodyTimeout));
        proxyRes.once('end', () => clearTimeout(bodyTimeout));
        proxyRes.on('data', (chunk: Buffer) => {
          if (forwarded) { res.write(chunk); return; }
          length += chunk.length;
          chunks.push(chunk);
          if (length > 8192) forward();
        });
        proxyRes.once('end', () => {
          if (res.destroyed) return;
          if (forwarded) { res.end(); return; }
          let payload: Record<string, unknown> | undefined;
          try { payload = JSON.parse(Buffer.concat(chunks).toString('utf8')); }
          catch { payload = undefined; }
          const error = payload?.error;
          const settlement = typeof payload?.detail === 'object' && payload.detail !== null
            ? payload.detail as Record<string, unknown> : payload;
          const settlementError = settlement?.error;
          const details = settlement?.details;
          const retryable = settlement?.retryable ?? (typeof details === 'object' && details !== null
            ? (details as Record<string, unknown>).retryable : undefined);
          if (proxyRes.statusCode === 502 &&
              ((settlementError === 'usage_settlement_unconfirmed' && retryable === false) ||
               (typeof settlementError === 'object' && settlementError !== null &&
                (settlementError as Record<string, unknown>).type === 'usage_settlement_unconfirmed' &&
                (settlementError as Record<string, unknown>).retryable === false))) {
            res.writeHead(409, { ...proxyRes.headers, 'x-should-retry': 'false' });
            res.end(Buffer.concat(chunks));
            return;
          }
          // The gateway answers 503 execution_policy_refused when the flow's spend is
          // momentarily unreadable (an in-flight pending marker during a gateway
          // rollout) or the budget meter is unavailable. It classifies those reasons as
          // retryable (#7711); the worker must wait for the meter to heal like it waits
          // for pending reservations, not end the run and burn the attempt.
          const policyReason = typeof payload?.reason === 'string' ? payload.reason : undefined;
          const unavailablePolicy = proxyRes.statusCode === 503 && error === 'execution_policy_refused' &&
            policyReason !== undefined && RETRYABLE_POLICY_REASONS.has(policyReason);
          const pendingHold = proxyRes.statusCode === 429 && error === 'budget_reservations_pending';
          if (!pendingHold && !unavailablePolicy) {
            forward(); res.end(); return;
          }
          attempt += 1;
          const remaining = deadline - Date.now();
          if (remaining <= 0) { exhausted(); return; }
          const backoff = Math.min(30_000, 1000 * 2 ** Math.min(attempt - 1, 5));
          const wait = Math.max(retryAfterMs(proxyRes.headers['retry-after']), backoff + Math.random() * backoff * 0.2);
          console.log(`[proxy] waiting for budget holds (${unavailablePolicy ? policyReason : 'budget_reservations_pending'}, attempt ${attempt}, up to ${Math.ceil(remaining / 1000)}s remaining)`);
          admissionProgress.wait(admissionKey, unavailablePolicy ? 'budget_unavailable' : 'budget_reservations_pending', deadline);
          retryTimer = setTimeout(() => {
            const elapsedTimer = retryTimer;
            retryTimer = undefined;
            if (elapsedTimer) clearTimeout(elapsedTimer);
            if (wait >= remaining || Date.now() >= deadline) exhausted();
            else void dispatch();
          }, Math.min(wait, remaining));
        });
        proxyRes.once('error', (err) => {
          if (res.destroyed) return;
          if (forwarded) res.destroy(err);
          else { res.writeHead(502); res.end('Upstream error'); }
        });
        proxyRes.once('close', () => {
          if (!proxyRes.complete && !res.destroyed && !res.writableEnded) {
            if (forwarded) res.destroy();
            else { res.writeHead(502); res.end('Upstream error'); }
          }
        });
        return;
      }

      admissionProgress.clear(admissionKey);
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
      const clearIdle = () => {
        clearTimeout(idleTimeout);
        proxyRes.removeListener('data', bumpIdle);
      };
      proxyRes.once('end', clearIdle);
      proxyRes.once('close', clearIdle);

      // Handle errors on the response stream (e.g. from idle-timeout destroy).
      // Without this handler, a destroyed proxyRes emits an unhandled 'error'
      // which would crash the proxy process.
      proxyRes.on('error', (err) => {
        if (res.destroyed) return;
        console.error('[proxy] response stream error:', err.message);
        if (!res.headersSent) { res.writeHead(502); res.end('Upstream error'); }
        else if (!res.destroyed) res.destroy();
      });

      proxyRes.pipe(res);
    });

    // Enforce the request-level timeout — the declared `timeout` option only
    // emits a 'timeout' event; without this handler the socket is never aborted.
    upstreamRequest.on('timeout', () => {
      console.error('[proxy] upstream request timeout — destroying connection');
      upstreamRequest.destroy(new Error('upstream request timeout'));
    });

    upstreamRequest.on('error', (err) => {
      if (res.destroyed) return;
      console.error('[proxy] upstream error:', err.message);
      if (!res.headersSent) { res.writeHead(502); res.end('Upstream error'); }
      else if (!res.destroyed) res.destroy();
    });

    proxyReq = upstreamRequest;
    if (res.destroyed) { upstreamRequest.destroy(); return; }
    if (body.length) upstreamRequest.write(body);
    upstreamRequest.end();
  };
  await dispatch();
});

server.listen(PORT, '127.0.0.1', () => {
  console.log(`[sigv4-proxy] listening on http://127.0.0.1:${PORT}`);
  console.log(`[sigv4-proxy] target: ${TARGET} | region: ${REGION}`);
});

// A budget denial can keep the downstream socket quiet for 4200s before the
// admitted provider request begins. Leave another response-idle interval for
// the first provider byte so this ceiling cannot cancel a healthy budget wait.
server.setTimeout(Math.max(3600_000, BUDGET_WAIT_MAX_SECONDS * 1000 + 660_000));
server.keepAliveTimeout = 3600_000;
