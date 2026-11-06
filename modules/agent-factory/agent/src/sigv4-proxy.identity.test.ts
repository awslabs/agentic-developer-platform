/** Run the real proxy against a local TLS receiver with disposable credentials. */
import { spawn, execFileSync, ChildProcess } from 'node:child_process';
import * as fs from 'node:fs';
import * as http from 'node:http';
import * as https from 'node:https';
import * as net from 'node:net';
import * as os from 'node:os';
import * as path from 'node:path';
import { SignatureV4 } from '@smithy/signature-v4';
import { Hash } from '@smithy/hash-node';

test.each(['protected', 'shared', 'protected-responses', 'protected-tools'])('%s model requests use refreshed supervisor proof and preserve bytes', async (mode) => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'adp-proxy-identity-'));
  const key = path.join(dir, 'key.pem');
  const cert = path.join(dir, 'cert.pem');
  const credential = path.join(dir, 'credential');
  const workload = path.join(dir, 'workload');
  const irsa = path.join(dir, 'irsa');
  let proxy: ChildProcess | undefined;
  let proxyErrors = '';
  let receiver: https.Server | undefined;
  const captures: { path: string; headers: http.IncomingHttpHeaders; body: Buffer }[] = [];
  const assumptions: URLSearchParams[] = [];
  try {
    execFileSync('openssl', ['req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-keyout', key, '-out', cert, '-days', '1', '-subj', '/CN=localhost', '-addext', 'subjectAltName=IP:127.0.0.1'], { stdio: 'ignore' });
    fs.writeFileSync(credential, 'current-credential\n');
    fs.writeFileSync(workload, 'current-pod\n');
    fs.writeFileSync(irsa, 'platform-irsa-token');
    receiver = https.createServer({ key: fs.readFileSync(key), cert: fs.readFileSync(cert) }, async (req, res) => {
      const chunks: Buffer[] = [];
      for await (const chunk of req) chunks.push(chunk);
      if (req.url === '/sts' || req.url === '/sts/') {
        assumptions.push(new URLSearchParams(Buffer.concat(chunks).toString()));
        res.setHeader('Content-Type', 'text/xml');
        res.end(`<AssumeRoleWithWebIdentityResponse xmlns="https://sts.amazonaws.com/doc/2011-06-15/"><AssumeRoleWithWebIdentityResult><Credentials><AccessKeyId>LOCAL_PLATFORM_KEY</AccessKeyId><SecretAccessKey>LOCAL_PLATFORM_SECRET</SecretAccessKey><SessionToken>LOCAL_PLATFORM_SESSION</SessionToken><Expiration>${new Date(Date.now() + 3600000).toISOString()}</Expiration></Credentials><AssumedRoleUser><Arn>arn:aws:sts::123456789012:assumed-role/worker/test</Arn><AssumedRoleId>LOCAL:test</AssumedRoleId></AssumedRoleUser></AssumeRoleWithWebIdentityResult><ResponseMetadata><RequestId>local-request</RequestId></ResponseMetadata></AssumeRoleWithWebIdentityResponse>`);
        return;
      }
      captures.push({ path: req.url || '', headers: req.headers, body: Buffer.concat(chunks) });
      if (mode === 'protected-tools') {
        const received = new URL(req.url!, `https://${req.headers.host}`);
        const query: Record<string, string[]> = Object.create(null);
        received.searchParams.forEach((value, name) => { (query[name] ??= []).push(value); });
        const headers: Record<string, string> = {};
        for (const name of req.headers.authorization!.match(/SignedHeaders=([^,]+)/)![1].split(';')) {
          headers[name] = String(req.headers[name]);
        }
        const date = String(req.headers['x-amz-date']).replace(/^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})Z$/, '$1-$2-$3T$4:$5:$6Z');
        const verifier = new SignatureV4({ service: 'execute-api', region: 'us-east-1', sha256: Hash.bind(null, 'sha256'),
          credentials: { accessKeyId: 'LOCAL_PLATFORM_KEY', secretAccessKey: 'LOCAL_PLATFORM_SECRET', sessionToken: 'LOCAL_PLATFORM_SESSION' } });
        const expected = await verifier.sign({ method: req.method!, protocol: 'https:', hostname: received.hostname,
          path: received.pathname, query, headers, body: Buffer.concat(chunks) }, { signingDate: new Date(date) });
        if (expected.headers.authorization !== req.headers.authorization) res.statusCode = 403;
      }
      res.end('ok');
    });
    await new Promise<void>(resolve => receiver!.listen(0, '127.0.0.1', resolve));
    const receiverPort = (receiver.address() as net.AddressInfo).port;
    const reservePort = net.createServer();
    await new Promise<void>(resolve => reservePort.listen(0, '127.0.0.1', resolve));
    const port = (reservePort.address() as net.AddressInfo).port;
    await new Promise<void>(resolve => reservePort.close(() => resolve()));
    proxy = spawn(process.execPath, [require.resolve('ts-node/dist/bin.js'), '--transpile-only', path.join(__dirname, 'sigv4-proxy.ts'), '--port', String(port), '--target', `https://127.0.0.1:${receiverPort}`], {
      cwd: path.join(__dirname, '..'),
      env: {
        ...process.env,
        AWS_ACCESS_KEY_ID: 'LOCAL_CUSTOMER_KEY', AWS_SECRET_ACCESS_KEY: 'LOCAL_CUSTOMER_SECRET', AWS_SESSION_TOKEN: 'LOCAL_CUSTOMER_SESSION',
        AWS_REGION: 'us-west-2', AWS_DEFAULT_REGION: 'us-west-2', ADP_WORKER_AWS_REGION: 'us-east-1',
        AWS_ENDPOINT_URL_STS: `https://127.0.0.1:${receiverPort}/sts`,
        ADP_WORKER_IRSA_ROLE_ARN: 'arn:aws:iam::123456789012:role/worker', ADP_WORKER_IRSA_TOKEN_FILE: irsa,
        AWS_ROLE_ARN: '', AWS_PROFILE: '', AWS_EC2_METADATA_DISABLED: 'true',
        ADP_AGENT_AUTHORITY_ENABLED: mode.startsWith('protected') ? 'true' : 'false',
        ADP_TOOLS_GATEWAY_URL: mode === 'protected-tools' ? `https://127.0.0.1:${receiverPort}/api/agent/tools` : '',
        ADP_RUN_CREDENTIAL_FILE: credential, ADP_WORKLOAD_TOKEN_FILE: workload,
        ADP_RUN_REPORT_CREDENTIAL_FILE: mode === 'shared' ? credential : '',
        ADP_MESSAGE_ID: 'protected-run', TENANT_ID: 'protected-tenant', NODE_EXTRA_CA_CERTS: cert, NODE_TLS_REJECT_UNAUTHORIZED: '1',
      },
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    proxy.stderr!.on('data', data => { proxyErrors += String(data); });
    // Readiness comes from the actual listener, without fixed startup sleeps.
    await new Promise<void>((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error('proxy did not start')), 10000);
      proxy!.once('exit', code => { clearTimeout(timer); reject(new Error(`proxy exited: ${code}`)); });
      proxy!.stdout!.on('data', data => {
        if (String(data).includes('[sigv4-proxy]')) { clearTimeout(timer); resolve(); }
      });
    });
    const responses = mode === 'protected-responses';
    const tools = mode === 'protected-tools';
    const bytes = responses ? Buffer.from('{"model":"openai.gpt-6-sol","input":"hello"}') : Buffer.from('{  "literal": "a\\nb", "messages": [] }');
    const request = (requestedPath = tools ? '/tools/call' : responses ? '/openai/v1/responses' : '/v1/messages', payload = bytes) => new Promise<number>((resolve, reject) => {
      const req = http.request({ hostname: '127.0.0.1', port, path: requestedPath, method: 'POST', headers: {
        'content-type': 'application/json', 'content-length': payload.length,
        'x-adp-run-credential': 'forged', 'x-adp-workload-token': 'forged', 'x-agent-runid': 'forged',
        'x-adp-report-credential': 'forged-report',
      } }, res => { res.resume(); res.on('end', () => resolve(res.statusCode!)); });
      req.on('error', reject);
      req.end(payload);
    });
    const firstStatus = await request();
    if (firstStatus !== 200) throw new Error(`Proxy returned ${firstStatus}: ${proxyErrors}`);
    fs.writeFileSync(credential, 'refreshed-credential\n');
    expect(await request()).toBe(200);
    const ownHeader = mode.startsWith('protected') ? 'x-adp-run-credential' : 'x-adp-report-credential';
    expect(captures.map(c => c.headers[ownHeader])).toEqual(['current-credential', 'refreshed-credential']);
    for (const capture of captures) {
      if (tools) expect(capture.path).toBe('/api/agent/tools/call');
      expect(capture.headers['x-adp-workload-token']).toBe(mode.startsWith('protected') ? 'current-pod' : undefined);
      expect(capture.headers[mode.startsWith('protected') ? 'x-adp-report-credential' : 'x-adp-run-credential']).toBeUndefined();
      expect(capture.headers['x-agent-runid']).toBe('protected-run');
      expect(capture.headers.authorization).toContain('Credential=LOCAL_PLATFORM_KEY/');
      expect(capture.headers.authorization).toContain('/us-east-1/execute-api/');
      if (responses) {
        expect(JSON.parse(capture.body.toString())).toEqual({ model: 'openai.gpt-6-sol', input: 'hello', max_output_tokens: 16384 });
        expect(capture.headers['content-length']).toBe(String(capture.body.length));
        expect(capture.headers['x-amz-content-sha256']).toBe(require('node:crypto').createHash('sha256').update(capture.body).digest('hex'));
      } else {
        expect(capture.body.equals(bytes)).toBe(true);
      }
    }
    expect(assumptions.length).toBeGreaterThan(0);
    for (const assumption of assumptions) {
      expect(assumption.get('Action')).toBe('AssumeRoleWithWebIdentity');
      expect(assumption.get('RoleArn')).toBe('arn:aws:iam::123456789012:role/worker');
      expect(assumption.get('WebIdentityToken')).toBe('platform-irsa-token');
    }
    if (tools) {
      expect(await request('/tools/executions/00000000-0000-4000-8000-000000000001/close?tool=browser&tag=b&tag=a&value=a%20b')).toBe(200);
      const listStatus = await new Promise<number>((resolve, reject) => {
        const list = http.get({ hostname: '127.0.0.1', port, path: '/tools?live=true' }, response => {
          response.resume(); response.on('end', () => resolve(response.statusCode!));
        });
        list.on('error', reject);
      });
      expect(listStatus).toBe(200);
      expect(captures[3].path).toBe('/api/agent/tools?live=true');
      expect(captures[3].headers['x-adp-run-credential']).toBe('refreshed-credential');
      expect(captures[3].headers['x-adp-workload-token']).toBe('current-pod');
      const original = Buffer.from(JSON.stringify({ tool: 'browser___start', arguments: { url: 'https://example.org' },
        request_id: '00000000-0000-4000-8000-000000000099', ttl_seconds: 2 }));
      expect(await request('/tools/operations/recover', original)).toBe(200);
      expect(captures[4].path).toBe('/api/agent/tools/operations/recover');
      expect(captures[4].body.equals(original)).toBe(true);
      expect(captures[4].headers['x-adp-run-credential']).toBe('refreshed-credential');
      expect(captures[4].headers['x-adp-workload-token']).toBe('current-pod');
      expect(captures[4].headers['x-agent-runid']).toBe('protected-run');
    } else {
      expect(await request('/tools/call')).toBe(404);
    }
    expect(captures).toHaveLength(tools ? 5 : 2);
    expect(await request('/tools/../internal/v1/agent')).toBe(404);
    expect(await request('/tools/%2e%2e/internal')).toBe(404);
    expect(captures).toHaveLength(tools ? 5 : 2);
    fs.unlinkSync(credential);
    expect(await request()).toBe(502);
    expect(captures).toHaveLength(tools ? 5 : 2);
  } finally {
    if (proxy && proxy.exitCode === null) {
      const exited = new Promise<void>(resolve => proxy!.once('exit', () => resolve()));
      proxy.kill('SIGKILL');
      await exited;
    }
    if (receiver) await new Promise<void>(resolve => receiver!.close(() => resolve()));
    fs.rmSync(dir, { recursive: true, force: true });
  }
}, 20000);
