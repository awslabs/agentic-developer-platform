/** Exercise production proxy lifecycle with real loopback HTTP streams. */
import * as fs from 'node:fs';
import * as http from 'node:http';
import * as vm from 'node:vm';
import { AddressInfo } from 'node:net';
import * as ts from 'typescript';

const production = ts.transpileModule(fs.readFileSync(require.resolve('./sigv4-proxy.ts'), 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
}).outputText;

function deferred() {
  let resolve!: () => void;
  const promise = new Promise<void>(r => { resolve = r; });
  return { promise, resolve };
}

async function fixture(handler: http.RequestListener, env: Record<string, string> = {}) {
  const upstream = http.createServer(handler);
  await new Promise<void>(r => upstream.listen(0, '127.0.0.1', r));
  let proxy!: http.Server;
  const timers = new Set<ReturnType<typeof setTimeout>>();
  const logger = { log: jest.fn(), error: jest.fn() };
  const signed: string[] = [];
  const progressRequests = new Set<http.ClientRequest>();
  const dependencies: Record<string, unknown> = {
    http: { ...http, createServer: (handler: http.RequestListener) => (proxy = http.createServer(handler)) },
    // Only transport/signing are substituted; production request/stream logic runs.
    https: { request: (options: http.RequestOptions, callback: (res: http.IncomingMessage) => void) => {
      const request = http.request({ ...options, hostname: '127.0.0.1', port: (upstream.address() as AddressInfo).port }, callback);
      if (options.path === '/internal/v1/agent/self/status') {
        progressRequests.add(request);
        request.once('close', () => progressRequests.delete(request));
      }
      return request;
    } },
    '@smithy/signature-v4': { SignatureV4: class { async sign(value: { headers: Record<string, string> }) { signed.push(value.headers['x-adp-run-credential']); return value; } } },
    '@smithy/hash-node': { Hash: function () {} },
    './lib/runIdentity': { gatewaySigningRegion: () => 'us-east-1', workerIdentityHeaders: () => ({ 'X-Adp-Run-Credential': `credential-${signed.length + 1}` }) },
    './lib/knowledgeBridge': { handleKnowledgeBridge: async () => false },
    './lib/proxyPort': { proxyPort: () => '0' },
    './lib/responsesOutputBound': { responsesOutputDefault: () => 1, withResponsesOutputBound: (_path: string, body: Buffer) => body },
  };
  vm.runInNewContext(production, {
    exports: {}, require: (name: string) => dependencies[name] ?? require(name), Buffer,
    process: { argv: ['node', 'proxy', '--target', 'https://test.invalid/agent', '--port', '0'], env },
    console: logger,
    setTimeout: (callback: () => void, ms: number) => {
      const timer = setTimeout(callback, ms); timers.add(timer); return timer;
    },
    clearTimeout: (timer: ReturnType<typeof setTimeout>) => { timers.delete(timer); clearTimeout(timer); },
  });
  if (!proxy.listening) await new Promise<void>(r => proxy.once('listening', r));
  return {
    options: { host: '127.0.0.1', port: (proxy.address() as AddressInfo).port, path: '/openai/v1/responses' },
    timers, logger, signed, socketTimeout: proxy.timeout,
    progressDrained: () => Promise.all([...progressRequests].map(request => new Promise<void>(resolve => request.once('close', resolve)))),
    close: async () => {
      for (const timer of timers) clearTimeout(timer);
      proxy.closeAllConnections(); upstream.closeAllConnections();
      await Promise.all([proxy, upstream].map(server => new Promise<void>(r => server.close(() => r()))));
    },
  };
}

async function within(promise: Promise<unknown>, timeoutMs = 1000) {
  let timeout!: ReturnType<typeof setTimeout>;
  try {
    await Promise.race([promise, new Promise<never>((_, reject) => {
      timeout = setTimeout(() => reject(new Error('upstream did not close after client disconnected')), timeoutMs);
    })]);
  } finally { clearTimeout(timeout); }
  // Let both peers process the close event before inspecting watchdog cleanup.
  await new Promise(r => setImmediate(r));
}

test.each(['before-headers', 'after-terminal-event'])('client disconnect %s closes upstream and clears its watchdog', async mode => {
  const arrived = deferred(), closed = deferred();
  const f = await fixture((_req, res) => {
    res.on('close', closed.resolve); arrived.resolve();
    if (mode === 'after-terminal-event') {
      res.writeHead(200, { 'content-type': 'text/event-stream' });
      res.write('data: {"type":"response.completed"}\n\n');
    }
  });
  try {
    const client = http.get(f.options, res => res.once('data', () => res.destroy()));
    client.on('error', () => {}); // Expected socket hangup when cancelled before headers.
    await arrived.promise;
    if (mode === 'before-headers') client.destroy();
    await within(closed.promise);
    expect(f.timers.size).toBe(0);
    expect(f.logger.error).not.toHaveBeenCalled();
  } finally { await f.close(); }
});

test('normal upstream EOF delivers all bytes and clears the watchdog', async () => {
  const f = await fixture((_req, res) => { res.write('first'); res.end('last'); });
  try {
    const body = await new Promise<string>((resolve, reject) => {
      http.get(f.options, res => {
        let body = ''; res.on('data', chunk => { body += chunk; });
        res.on('end', () => resolve(body)); res.on('error', reject);
      }).on('error', reject);
    });
    expect(body).toBe('firstlast');
    expect(f.timers.size).toBe(0);
    expect(f.logger.error).not.toHaveBeenCalled();
  } finally { await f.close(); }
});

test('aborted upstream closes downstream and clears the watchdog', async () => {
  let response!: http.ServerResponse;
  const f = await fixture((_req, res) => { response = res; res.write('first'); });
  try {
    await new Promise<void>((resolve, reject) => {
      http.get(f.options, res => {
        res.once('data', () => response.destroy());
        res.on('error', () => {}); res.on('close', resolve);
      }).on('error', reject);
    });
    expect(f.timers.size).toBe(0);
  } finally { await f.close(); }
});


test('a pre-provider budget hold waits for Retry-After, re-signs, and forwards only the admitted response', async () => {
  const arrivals: number[] = [];
  const bodies: string[] = [];
  const f = await fixture((req, res) => {
    arrivals.push(Date.now());
    const chunks: Buffer[] = [];
    req.on('data', chunk => chunks.push(chunk));
    req.on('end', () => {
      bodies.push(Buffer.concat(chunks).toString());
      if (arrivals.length === 1) {
        res.writeHead(429, { 'content-type': 'application/json', 'retry-after': '1.4', 'x-amzn-errortype': 'ThrottlingException' });
        res.end(JSON.stringify({ error: 'budget_reservations_pending', details: { scope: 'flow' } }));
      } else {
        res.writeHead(200, { 'content-type': 'text/event-stream' });
        res.end('data: completed\n\n');
      }
    });
  }, { ADP_AGENT_AUTHORITY_ENABLED: 'true', ADP_BUDGET_WAIT_MAX_SECONDS: '5' });
  try {
    const result = await new Promise<{ status: number; body: string }>((resolve, reject) => {
      const request = http.request({ ...f.options, method: 'POST', headers: { 'content-type': 'application/json' } }, res => {
        const chunks: Buffer[] = [];
        res.on('data', chunk => chunks.push(chunk));
        res.on('end', () => resolve({ status: res.statusCode!, body: Buffer.concat(chunks).toString() }));
        res.on('error', reject);
      });
      request.on('error', reject);
      request.end('{"input":"test"}');
    });
    expect(result).toEqual({ status: 200, body: 'data: completed\n\n' });
    expect(arrivals).toHaveLength(2);
    expect(arrivals[1] - arrivals[0]).toBeGreaterThanOrEqual(1300);
    expect(bodies).toEqual(['{"input":"test"}', '{"input":"test"}']);
    expect(f.signed).toEqual(['credential-1', 'credential-2']);
    expect(f.logger.log).toHaveBeenCalledWith(expect.stringContaining('waiting for budget holds'));
    expect(f.timers.size).toBe(0);
  } finally { await f.close(); }
});


function responseFor(options: http.RequestOptions): Promise<{ status: number; headers: http.IncomingHttpHeaders; body: string }> {
  return new Promise((resolve, reject) => {
    http.get(options, res => {
      const chunks: Buffer[] = [];
      res.on('data', chunk => chunks.push(chunk));
      res.on('end', () => resolve({ status: res.statusCode!, headers: res.headers, body: Buffer.concat(chunks).toString() }));
      res.on('error', reject);
    }).on('error', reject);
  });
}

const hold = JSON.stringify({ error: 'budget_reservations_pending', details: { scope: 'flow' } });

test('budget wait updates the authenticated run summary once across rapid denials without delaying admission', async () => {
  const persisted: string[] = [];
  const cleared = deferred();
  const credentials: string[] = [];
  let modelCalls = 0;
  const f = await fixture((req, res) => {
    if (req.url === '/internal/v1/agent/self/status') {
      credentials.push(String(req.headers['x-adp-run-credential']));
      const chunks: Buffer[] = [];
      req.on('data', chunk => chunks.push(chunk));
      req.on('end', () => {
        const update = JSON.parse(Buffer.concat(chunks).toString());
        if (update.status === 'in_progress') persisted.push(update.summary);
        if (update.model_admission_status === 'not_waiting') cleared.resolve();
        res.writeHead(200, { 'content-type': 'application/json' }); res.end('{}');
      });
      return;
    }
    modelCalls += 1;
    if (modelCalls < 3) {
      res.writeHead(429, { 'content-type': 'application/json', 'retry-after': '0' }); res.end(hold);
    } else {
      res.writeHead(200, { 'content-type': 'text/event-stream' }); res.end('data: accepted\n\n');
    }
  }, {
    ADP_AGENT_AUTHORITY_ENABLED: 'true',
    ADP_AGENT_CONTROL_ENDPOINT: 'https://test.invalid/internal/v1/agent',
    ADP_BUDGET_WAIT_MAX_SECONDS: '8',
  });
  try {
    const response = await responseFor(f.options);
    await within(cleared.promise);
    await within(f.progressDrained());
    expect(response).toMatchObject({ status: 200, body: 'data: accepted\n\n' });
    expect(modelCalls).toBe(3);
    expect(persisted).toHaveLength(2);
    expect(persisted[0]).toMatch(/^Waiting for budget holds to settle; retrying model admission \(up to \d+s remaining\)$/);
    expect(persisted[1]).toBe('Model admission is no longer waiting for budget.');
    expect(credentials).toHaveLength(2);
    expect(f.logger.error).not.toHaveBeenCalled();
    expect(f.timers.size).toBe(0);
  } finally { await f.close(); }
});

test('budget holds stop at the configured deadline without a second provider attempt', async () => {
  let calls = 0;
  const f = await fixture((_req, res) => {
    calls += 1;
    res.writeHead(429, { 'content-type': 'application/json', 'retry-after': '30' }); res.end(hold);
  }, { ADP_BUDGET_WAIT_MAX_SECONDS: '0.2' });
  try {
    const started = Date.now();
    const response = await responseFor(f.options);
    expect(response.status).toBe(409);
    expect(JSON.parse(response.body)).toEqual({ error: 'budget_wait_exhausted', retryable: false });
    expect(Date.now() - started).toBeGreaterThanOrEqual(150);
    expect(calls).toBe(1);
    expect(f.timers.size).toBe(0);
  } finally { await f.close(); }
});

test.each(['ADP_POD_DEADLINE_AT', 'ADP_ACCEPTED_POLICY_EXPIRES_AT'])(
  'budget waits do not retry beyond expired %s', async field => {
    let calls = 0;
    const f = await fixture((_req, res) => {
      calls += 1;
      res.writeHead(429, { 'content-type': 'application/json', 'retry-after': '1' }); res.end(hold);
    }, { ADP_BUDGET_WAIT_MAX_SECONDS: '5', [field]: new Date(Date.now() - 1000).toISOString() });
    try {
      const response = await responseFor(f.options);
      expect(response.status).toBe(409);
      expect(JSON.parse(response.body).error).toBe('budget_wait_exhausted');
      expect(calls).toBe(1);
      expect(f.timers.size).toBe(0);
    } finally { await f.close(); }
  },
);

test('disconnect during a budget wait cancels its timer and does not retry', async () => {
  const denied = deferred();
  let calls = 0;
  const f = await fixture((_req, res) => {
    calls += 1;
    res.writeHead(429, { 'content-type': 'application/json', 'retry-after': '1' });
    res.end(hold, denied.resolve);
  }, { ADP_BUDGET_WAIT_MAX_SECONDS: '5' });
  try {
    const request = http.get(f.options);
    request.on('error', () => {});
    await denied.promise;
    await new Promise(r => setImmediate(r));
    request.destroy();
    await new Promise(r => setTimeout(r, 1250));
    expect(calls).toBe(1);
    expect(f.timers.size).toBe(0);
  } finally { await f.close(); }
});

test.each([
  ['spend_unknown'],
  ['budget_unavailable'],
  ['budget_enforcement_unavailable'],
])('a transient 503 execution_policy_refused/%s waits for the meter and forwards only the admitted response (#7711)', async (reason) => {
  const arrivals: number[] = [];
  const f = await fixture((req, res) => {
    arrivals.push(Date.now());
    req.resume();
    req.on('end', () => {
      if (arrivals.length === 1) {
        res.writeHead(503, { 'content-type': 'application/json' });
        res.end(JSON.stringify({ error: 'execution_policy_refused', reason, scope: 'flow' }));
      } else {
        res.writeHead(200, { 'content-type': 'application/json' });
        res.end('{"ok":true}');
      }
    });
  }, { ADP_AGENT_AUTHORITY_ENABLED: 'true', ADP_BUDGET_WAIT_MAX_SECONDS: '5' });
  try {
    const result = await new Promise<{ status: number; body: string }>((resolve, reject) => {
      const request = http.request({ ...f.options, method: 'POST', headers: { 'content-type': 'application/json' } }, res => {
        const chunks: Buffer[] = [];
        res.on('data', chunk => chunks.push(chunk));
        res.on('end', () => resolve({ status: res.statusCode!, body: Buffer.concat(chunks).toString() }));
        res.on('error', reject);
      });
      request.on('error', reject);
      request.end('{"input":"test"}');
    });
    expect(result).toEqual({ status: 200, body: '{"ok":true}' });
    expect(arrivals).toHaveLength(2);
    expect(arrivals[1] - arrivals[0]).toBeGreaterThanOrEqual(900);
    expect(f.signed).toEqual(['credential-1', 'credential-2']);
    expect(f.logger.log).toHaveBeenCalledWith(expect.stringContaining(`waiting for budget holds (${reason}`));
    expect(f.timers.size).toBe(0);
  } finally { await f.close(); }
});

test.each([
  [429, 'rate_limit_exceeded', 'application/json'],
  [402, 'budget_exceeded', 'application/json'],
  [503, 'service_unavailable', 'application/json'],
  [429, 'budget_reservations_pending', 'text/plain'],
  [503, 'execution_policy_refused', 'application/json'], // no reason: not a classified unavailability
  [403, 'execution_policy_refused', 'application/json'],
])('unrecognized gateway response %i/%s/%s passes through without retry', async (status, error, contentType) => {
  let calls = 0;
  const payload = JSON.stringify({ error });
  const f = await fixture((_req, res) => {
    calls += 1;
    res.writeHead(status, { 'content-type': contentType, 'retry-after': '2', 'x-amzn-errortype': 'ThrottlingException' });
    res.end(payload);
  }, { ADP_BUDGET_WAIT_MAX_SECONDS: '1' });
  try {
    const response = await responseFor(f.options);
    expect(response.status).toBe(status);
    expect(response.body).toBe(payload);
    expect(response.headers['retry-after']).toBe('2');
    expect(response.headers['x-amzn-errortype']).toBe('ThrottlingException');
    expect(calls).toBe(1);
    expect(f.timers.size).toBe(0);
  } finally { await f.close(); }
});


test.each([['policy_expired'], ['budget_exceeded'], ['membership_revoked']])(
  'a 503 execution_policy_refused with a denied reason %s passes through without retry', async (reason) => {
  let calls = 0;
  const payload = JSON.stringify({ error: 'execution_policy_refused', reason, scope: 'flow' });
  const f = await fixture((_req, res) => {
    calls += 1;
    res.writeHead(503, { 'content-type': 'application/json' });
    res.end(payload);
  }, { ADP_BUDGET_WAIT_MAX_SECONDS: '1' });
  try {
    const response = await responseFor(f.options);
    expect(response.status).toBe(503);
    expect(response.body).toBe(payload);
    expect(calls).toBe(1);
    expect(f.timers.size).toBe(0);
  } finally { await f.close(); }
});

test('oversized JSON budget denial is passed through unchanged without replay', async () => {
  let calls = 0;
  const payload = JSON.stringify({ error: 'budget_reservations_pending', padding: 'x'.repeat(8192) });
  const f = await fixture((_req, res) => {
    calls += 1;
    res.writeHead(429, { 'content-type': 'application/json' }); res.end(payload);
  }, { ADP_BUDGET_WAIT_MAX_SECONDS: '1' });
  try {
    expect(await responseFor(f.options)).toMatchObject({ status: 429, body: payload });
    expect(calls).toBe(1);
    expect(f.timers.size).toBe(0);
  } finally { await f.close(); }
});

test('disconnect cancels model retry, bounds a stuck status write and then clears the wait', async () => {
  const progressOpened = deferred(), progressClosed = deferred(), cleared = deferred();
  let modelCalls = 0;
  const f = await fixture((req, res) => {
    if (req.url === '/internal/v1/agent/self/status') {
      const chunks: Buffer[] = [];
      req.on('data', chunk => chunks.push(chunk));
      req.on('end', () => {
        const update = JSON.parse(Buffer.concat(chunks).toString());
        if (update.model_admission_status === 'not_waiting') {
          res.writeHead(200); res.end('{}'); cleared.resolve();
        } else { res.once('close', progressClosed.resolve); progressOpened.resolve(); }
      });
      return;
    }
    modelCalls += 1;
    res.writeHead(429, { 'content-type': 'application/json', 'retry-after': '2' }); res.end(hold);
  }, {
    ADP_AGENT_AUTHORITY_ENABLED: 'true',
    ADP_AGENT_CONTROL_ENDPOINT: 'https://test.invalid/internal/v1/agent',
    ADP_BUDGET_WAIT_MAX_SECONDS: '5',
  });
  try {
    const request = http.get(f.options);
    request.on('error', () => {});
    await within(progressOpened.promise);
    request.destroy();
    await within(progressClosed.promise, 6000);
    await within(cleared.promise);
    await within(f.progressDrained());
    expect(modelCalls).toBe(1);
    expect(f.timers.size).toBe(0);
  } finally { await f.close(); }
}, 10000);


test.each(['0', '-1', 'NaN', 'Infinity', ''])('invalid budget wait cap %j refuses proxy startup', value => {
  const logger = { error: jest.fn(), log: jest.fn() };
  expect(() => vm.runInNewContext(production, {
    exports: {}, require: (name: string) => name === './lib/proxyPort' ? { proxyPort: () => '0' }
      : name === './lib/responsesOutputBound' ? { responsesOutputDefault: () => 1 } : require(name), Buffer,
    process: {
      argv: ['node', 'proxy', '--target', 'https://test.invalid/agent', '--port', '0'],
      env: { ADP_BUDGET_WAIT_MAX_SECONDS: value },
      exit: () => { throw new Error('startup refused'); },
    },
    console: logger,
  })).toThrow('startup refused');
  expect(logger.error).toHaveBeenCalledWith(expect.stringContaining('ADP_BUDGET_WAIT_MAX_SECONDS'));
});

test('progress reporting refuses a control endpoint outside the model gateway origin', async () => {
  let modelCalls = 0;
  const f = await fixture((_req, res) => {
    modelCalls += 1;
    if (modelCalls === 1) {
      res.writeHead(429, { 'content-type': 'application/json' }); res.end(hold);
    } else {
      res.writeHead(200); res.end('accepted');
    }
  }, {
    ADP_AGENT_AUTHORITY_ENABLED: 'true',
    ADP_AGENT_CONTROL_ENDPOINT: 'https://elsewhere.invalid/internal/v1/agent',
    ADP_BUDGET_WAIT_MAX_SECONDS: '4',
  });
  try {
    expect(await responseFor(f.options)).toMatchObject({ status: 200, body: 'accepted' });
    expect(modelCalls).toBe(2);
    expect(f.signed).toEqual(['credential-1', 'credential-2']);
  } finally { await f.close(); }
});


test.each([
  [{ error: 'usage_settlement_unconfirmed', retryable: false }, 409],
  [{ error: { type: 'usage_settlement_unconfirmed', retryable: false } }, 409],
  [{ error: 'usage_settlement_unconfirmed', details: { retryable: false } }, 409],
  [{ detail: { error: 'usage_settlement_unconfirmed', details: { retryable: false } } }, 409],
  [{ detail: { error: 'usage_settlement_unconfirmed', details: { retryable: true } } }, 502],
  [{ detail: { error: 'usage_settlement_unconfirmed' } }, 502],
  [{ detail: { error: 'provider_unavailable', details: { retryable: false } } }, 502],
  [{ error: 'usage_settlement_unconfirmed', retryable: true }, 502],
  [{ error: 'provider_unavailable', retryable: false }, 502],
])('proxy makes only confirmed non-retryable accounting error safe from SDK replay', async (payload, expected) => {
  const body = JSON.stringify(payload);
  let calls = 0;
  const f = await fixture((_req, res) => {
    calls += 1;
    res.writeHead(502, { 'content-type': 'application/json', 'x-amzn-errortype': 'InternalFailure', 'x-should-retry': 'true' });
    res.end(body);
  });
  try {
    const response = await responseFor(f.options);
    expect(response.status).toBe(expected);
    expect(response.body).toBe(body);
    expect(response.headers['x-amzn-errortype']).toBe('InternalFailure');
    expect(response.headers['x-should-retry']).toBe(expected === 409 ? 'false' : 'true');
    expect(calls).toBe(1);
    expect(f.timers.size).toBe(0);
  } finally { await f.close(); }
});


test('proxy socket idle timeout covers the full pending-marker wait plus provider idle allowance', async () => {
  const f = await fixture((_req, res) => res.end('ok'));
  try {
    expect(f.socketTimeout).toBeGreaterThan(4_200_000 + 600_000);
  } finally { await f.close(); }
});

test('a fast admitted response cannot cancel or overtake a delayed wait status write', async () => {
  const releaseWait = deferred(), cleared = deferred();
  const persisted: { model_admission_status: string; model_admission_sequence: string }[] = [];
  let calls = 0;
  const f = await fixture((req, res) => {
    if (req.url === '/internal/v1/agent/self/status') {
      const chunks: Buffer[] = [];
      req.on('data', chunk => chunks.push(chunk));
      req.on('end', () => {
        const update = JSON.parse(Buffer.concat(chunks).toString());
        const write = () => {
          persisted.push(update);
          res.writeHead(200, { 'content-type': 'application/json' }); res.end('{}');
          if (update.model_admission_status === 'not_waiting') cleared.resolve();
        };
        if (update.model_admission_status === 'not_waiting') write();
        else void releaseWait.promise.then(write);
      });
      return;
    }
    if (++calls === 1) {
      res.writeHead(429, { 'content-type': 'application/json', 'retry-after': '0' }); res.end(hold);
    } else { res.writeHead(200); res.end('accepted'); }
  }, { ADP_AGENT_AUTHORITY_ENABLED: 'true', ADP_AGENT_CONTROL_ENDPOINT: 'https://test.invalid/internal/v1/agent', ADP_BUDGET_WAIT_MAX_SECONDS: '8' });
  try {
    expect(await responseFor(f.options)).toMatchObject({ status: 200, body: 'accepted' });
    expect(persisted).toEqual([]);
    releaseWait.resolve();
    await within(cleared.promise);
    await within(f.progressDrained());
    expect(persisted.map(update => update.model_admission_status)).toEqual(['budget_reservations_pending', 'not_waiting']);
    expect(Number(persisted[1].model_admission_sequence)).toBeGreaterThan(Number(persisted[0].model_admission_sequence));
    expect(f.timers.size).toBe(0);
  } finally { releaseWait.resolve(); await f.close(); }
});

test.each(['admitted', 'deadline'])('older Gateway refusing optional diagnostics preserves model %s behavior', async outcome => {
  const cleared = deferred();
  const reports: string[] = [];
  let modelCalls = 0;
  const f = await fixture((req, res) => {
    if (req.url === '/internal/v1/agent/self/status') {
      const chunks: Buffer[] = [];
      req.on('data', chunk => chunks.push(chunk));
      req.on('end', () => {
        const update = JSON.parse(Buffer.concat(chunks).toString());
        reports.push(update.model_admission_status);
        // Older Gateway StatusRequest forbids fields outside its allowlist.
        res.writeHead(422, { 'content-type': 'application/json' });
        res.end(JSON.stringify({ detail: [{ type: 'extra_forbidden', loc: ['body', 'model_admission_status'] }] }));
        if (update.model_admission_status === 'not_waiting') cleared.resolve();
      });
      return;
    }
    if (++modelCalls === 1) {
      res.writeHead(429, { 'content-type': 'application/json', 'retry-after': outcome === 'deadline' ? '30' : '0' });
      res.end(hold);
    } else { res.writeHead(200, { 'content-type': 'text/event-stream' }); res.end('data: accepted\n\n'); }
  }, {
    ADP_AGENT_AUTHORITY_ENABLED: 'true', ADP_AGENT_CONTROL_ENDPOINT: 'https://test.invalid/internal/v1/agent',
    ADP_BUDGET_WAIT_MAX_SECONDS: outcome === 'deadline' ? '0.2' : '5',
  });
  try {
    const result = await responseFor(f.options);
    await within(cleared.promise);
    await within(f.progressDrained());
    expect(result).toMatchObject(outcome === 'admitted'
      ? { status: 200, body: 'data: accepted\n\n' }
      : { status: 409, body: JSON.stringify({ error: 'budget_wait_exhausted', retryable: false }) });
    expect(modelCalls).toBe(outcome === 'admitted' ? 2 : 1);
    expect(reports).toEqual(['budget_reservations_pending', 'not_waiting']);
    expect(f.timers.size).toBe(0);
    expect(f.logger.error).toHaveBeenCalledTimes(2);
  } finally { await f.close(); }
});
