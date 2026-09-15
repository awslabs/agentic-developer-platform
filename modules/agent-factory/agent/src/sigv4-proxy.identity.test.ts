/** Run the real proxy against a local TLS receiver with disposable credentials. */
import { spawn, execFileSync, ChildProcess } from 'node:child_process';
import * as fs from 'node:fs';
import * as http from 'node:http';
import * as https from 'node:https';
import * as net from 'node:net';
import * as os from 'node:os';
import * as path from 'node:path';

test('protected model requests use refreshed supervisor proof and preserve bytes', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'adp-proxy-identity-'));
  const key = path.join(dir, 'key.pem');
  const cert = path.join(dir, 'cert.pem');
  const credential = path.join(dir, 'credential');
  const workload = path.join(dir, 'workload');
  let proxy: ChildProcess | undefined;
  let receiver: https.Server | undefined;
  const captures: { headers: http.IncomingHttpHeaders; body: Buffer }[] = [];
  try {
    execFileSync('openssl', ['req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-keyout', key, '-out', cert, '-days', '1', '-subj', '/CN=localhost', '-addext', 'subjectAltName=IP:127.0.0.1'], { stdio: 'ignore' });
    fs.writeFileSync(credential, 'current-credential\n');
    fs.writeFileSync(workload, 'current-pod\n');
    receiver = https.createServer({ key: fs.readFileSync(key), cert: fs.readFileSync(cert) }, async (req, res) => {
      const chunks: Buffer[] = [];
      for await (const chunk of req) chunks.push(chunk);
      captures.push({ headers: req.headers, body: Buffer.concat(chunks) });
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
        AWS_ACCESS_KEY_ID: 'LOCAL_TEST_ONLY', AWS_SECRET_ACCESS_KEY: 'LOCAL_TEST_ONLY', AWS_SESSION_TOKEN: '',
        AWS_ROLE_ARN: '', AWS_PROFILE: '', AWS_EC2_METADATA_DISABLED: 'true',
        ADP_AGENT_AUTHORITY_ENABLED: 'true', ADP_RUN_CREDENTIAL_FILE: credential, ADP_WORKLOAD_TOKEN_FILE: workload,
        ADP_MESSAGE_ID: 'protected-run', TENANT_ID: 'protected-tenant', NODE_EXTRA_CA_CERTS: cert, NODE_TLS_REJECT_UNAUTHORIZED: '1',
      },
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    // Readiness comes from the actual listener, without fixed startup sleeps.
    await new Promise<void>((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error('proxy did not start')), 10000);
      proxy!.once('exit', code => { clearTimeout(timer); reject(new Error(`proxy exited: ${code}`)); });
      proxy!.stdout!.on('data', data => {
        if (String(data).includes('[sigv4-proxy]')) { clearTimeout(timer); resolve(); }
      });
    });
    const bytes = Buffer.from('{  "literal": "a\\nb", "messages": [] }');
    const request = () => new Promise<number>((resolve, reject) => {
      const req = http.request({ hostname: '127.0.0.1', port, path: '/v1/messages', method: 'POST', headers: {
        'content-type': 'application/json', 'content-length': bytes.length,
        'x-adp-run-credential': 'forged', 'x-adp-workload-token': 'forged', 'x-agent-runid': 'forged',
      } }, res => { res.resume(); res.on('end', () => resolve(res.statusCode!)); });
      req.on('error', reject);
      req.end(bytes);
    });
    expect(await request()).toBe(200);
    fs.writeFileSync(credential, 'refreshed-credential\n');
    expect(await request()).toBe(200);
    expect(captures.map(c => c.headers['x-adp-run-credential'])).toEqual(['current-credential', 'refreshed-credential']);
    for (const capture of captures) {
      expect(capture.headers['x-adp-workload-token']).toBe('current-pod');
      expect(capture.headers['x-agent-runid']).toBe('protected-run');
      expect(capture.headers.authorization).toContain('Credential=LOCAL_TEST_ONLY/');
      expect(capture.body.equals(bytes)).toBe(true);
    }
    fs.unlinkSync(credential);
    expect(await request()).toBe(502);
    expect(captures).toHaveLength(2);
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
