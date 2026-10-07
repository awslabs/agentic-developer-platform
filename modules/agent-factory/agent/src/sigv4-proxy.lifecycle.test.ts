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

async function fixture(handler: http.RequestListener) {
  const upstream = http.createServer(handler);
  await new Promise<void>(r => upstream.listen(0, '127.0.0.1', r));
  let proxy!: http.Server;
  const timers = new Set<ReturnType<typeof setTimeout>>();
  const logger = { log: jest.fn(), error: jest.fn() };
  const dependencies: Record<string, unknown> = {
    http: { ...http, createServer: (handler: http.RequestListener) => (proxy = http.createServer(handler)) },
    // Only transport/signing are substituted; production request/stream logic runs.
    https: { request: (options: http.RequestOptions, callback: (res: http.IncomingMessage) => void) =>
      http.request({ ...options, hostname: '127.0.0.1', port: (upstream.address() as AddressInfo).port }, callback) },
    '@smithy/signature-v4': { SignatureV4: class { async sign(value: unknown) { return value; } } },
    '@smithy/hash-node': { Hash: function () {} },
    './lib/runIdentity': { gatewaySigningRegion: () => 'us-east-1' },
    './lib/knowledgeBridge': { handleKnowledgeBridge: async () => false },
    './lib/proxyPort': { proxyPort: () => '0' },
    './lib/responsesOutputBound': { responsesOutputDefault: () => 1, withResponsesOutputBound: (_path: string, body: Buffer) => body },
  };
  vm.runInNewContext(production, {
    exports: {}, require: (name: string) => dependencies[name] ?? require(name), Buffer,
    process: { argv: ['node', 'proxy', '--target', 'https://test.invalid/agent', '--port', '0'], env: {} },
    console: logger,
    setTimeout: (callback: () => void, ms: number) => {
      const timer = setTimeout(callback, ms); timers.add(timer); return timer;
    },
    clearTimeout: (timer: ReturnType<typeof setTimeout>) => { timers.delete(timer); clearTimeout(timer); },
  });
  if (!proxy.listening) await new Promise<void>(r => proxy.once('listening', r));
  return {
    options: { host: '127.0.0.1', port: (proxy.address() as AddressInfo).port, path: '/openai/v1/responses' },
    timers, logger,
    close: async () => {
      for (const timer of timers) clearTimeout(timer);
      proxy.closeAllConnections(); upstream.closeAllConnections();
      await Promise.all([proxy, upstream].map(server => new Promise<void>(r => server.close(() => r()))));
    },
  };
}

async function within(promise: Promise<void>) {
  let timeout!: ReturnType<typeof setTimeout>;
  try {
    await Promise.race([promise, new Promise<never>((_, reject) => {
      timeout = setTimeout(() => reject(new Error('upstream did not close after client disconnected')), 1000);
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
