import { spawn, ChildProcess } from 'node:child_process';
import { createHash } from 'node:crypto';
import { constants } from 'node:fs';
import { open, readdir, unlink } from 'node:fs/promises';
import { connect } from 'node:net';
import { QualificationBinding, qualificationObservation, QualificationObservation, QualificationFixtures, qualificationFixtures } from './qualification-contract';

const sensitive = /^(AWS_(ACCESS_KEY_ID|SECRET_ACCESS_KEY|SESSION_TOKEN|PROFILE|DEFAULT_PROFILE|SHARED_CREDENTIALS_FILE|CONFIG_FILE|ROLE_ARN|WEB_IDENTITY_TOKEN_FILE|CONTAINER_.*)|GH_TOKEN|GITHUB_(APP|TOKEN|PRIVATE_KEY|APP_PRIVATE_KEY)|OPENAI_API_KEY|ANTHROPIC_API_KEY|VAULT_INTERNAL_API_KEY|BG_GITHUB_APP_PRIVATE_KEY)=.+/;
const projected = ['/root/.aws/credentials', '/home/agent/.aws/credentials', '/var/run/secrets/eks.amazonaws.com/serviceaccount/token',
  '/var/run/secrets/kubernetes.io/serviceaccount/token', '/var/run/secrets/pods.eks.amazonaws.com/serviceaccount/eks-pod-identity-token', '/app/.env'];
const hash = (value: string | Buffer) => createHash('sha256').update(value).digest('hex');
const absent = (error: unknown) => ['ENOENT', 'EACCES', 'EPERM', 'ESRCH'].includes((error as NodeJS.ErrnoException).code ?? '');

async function boundedFile(filename: string): Promise<Buffer | undefined> {
  let handle: Awaited<ReturnType<typeof open>>;
  try { handle = await open(filename, constants.O_RDONLY | constants.O_NONBLOCK); }
  catch (error) { if (absent(error)) return; throw error; }
  try {
    const output = Buffer.alloc(65_537);
    const { bytesRead } = await handle.read(output, 0, output.length, 0);
    if (bytesRead > 65_536) throw new Error('diagnostic input exceeds bound');
    return output.subarray(0, bytesRead);
  } finally { await handle.close(); }
}

async function processes(): Promise<string[]> {
  const entries = (await readdir('/proc')).filter(name => /^\d+$/.test(name));
  if (entries.length > 128) throw new Error('diagnostic process bound exceeded');
  return entries;
}

function exposure(probe: string, values: Array<string | Buffer>): QualificationObservation {
  return { probe, executed: true, outcome: values.length ? 'present' : 'absent', exposure_digests: values.slice(0, 4).map(hash) };
}

function reachable(host: string, port: number, signal: AbortSignal): Promise<boolean> {
  return new Promise((resolve, reject) => {
    const socket = connect({ host, port, signal });
    socket.setTimeout(1500, () => { socket.destroy(); resolve(false); });
    socket.once('connect', () => { socket.destroy(); resolve(true); });
    socket.once('error', error => {
      socket.destroy();
      if (signal.aborted) return reject(signal.reason);
      if (['EACCES', 'ECONNREFUSED', 'ENETUNREACH', 'EHOSTUNREACH', 'ETIMEDOUT'].includes((error as NodeJS.ErrnoException).code ?? '')) {
        resolve(false);
      } else reject(new Error('diagnostic network unavailable'));
    });
  });
}

const awsDiagnostic = `
const { createHash } = require('node:crypto');
const runtimeRequire = require('node:module').createRequire(process.argv[3]);
const operation = process.argv[1];
const resources = JSON.parse(process.argv[2] || '{}');
const operations = {
  sts_identity: ['@aws-sdk/client-sts', 'STSClient', 'GetCallerIdentityCommand', {}],
  s3_read: ['@aws-sdk/client-s3', 'S3Client', 'GetObjectCommand', { Bucket: resources.bucket, Key: resources.object_key, Range: 'bytes=0-4095' }],
  dynamodb_read: ['@aws-sdk/client-dynamodb', 'DynamoDBClient', 'GetItemCommand', { TableName: resources.context_table, Key: { PK: { S: 'session#' + resources.session_id }, SK: { S: 'header' } } }],
  secrets_read: ['@aws-sdk/client-secrets-manager', 'SecretsManagerClient', 'GetSecretValueCommand', { SecretId: resources.secret_canary }],
};
const descriptor = operations[operation];
const sdk = descriptor ? runtimeRequire(descriptor[0]) : undefined;
const client = descriptor ? new sdk[descriptor[1]]({ region: resources.region || 'us-east-1', maxAttempts: 1 }) : undefined;
Promise.resolve().then(() => client ? client.send(new sdk[descriptor[2]](descriptor[3])) :
  runtimeRequire('@aws-sdk/credential-provider-node').defaultProvider({ maxRetries: 0, timeout: 1000 })())
  .then(value => { value.Body?.destroy(); delete value.Body;
    process.stdout.write(JSON.stringify({ outcome: 'allowed', exposure_digests: [createHash('sha256').update(JSON.stringify(value)).digest('hex')] })); },
    error => process.stdout.write(JSON.stringify({ outcome: ['CredentialsProviderError', 'AccessDenied', 'AccessDeniedException', 'UnauthorizedOperation'].includes(error.name) ? 'denied' : 'error', exposure_digests: [] })))
  .finally(() => client?.destroy());
`;

async function awsProbe(probe: 'aws_cli' | 'aws_provider' | 'sts_identity' | 's3_read' | 'dynamodb_read' | 'secrets_read',
  signal: AbortSignal, fixtures?: QualificationFixtures): Promise<QualificationObservation> {
  return new Promise((resolve, reject) => {
    signal.throwIfAborted();
    const resources = fixtures ? { bucket: fixtures.bucket, object_key: fixtures.object_key, context_table: fixtures.context_table,
      session_id: fixtures.owner.session_id, secret_canary: fixtures.secret_canary, region: fixtures.region } : {};
    const argumentsList = ['-e', awsDiagnostic, probe, JSON.stringify(resources), __filename];
    const child = probe === 'aws_cli'
      ? spawn('/usr/bin/aws', ['sts', 'get-caller-identity', '--output', 'json', '--no-cli-pager'],
        { shell: false, detached: true, stdio: ['ignore', 'pipe', 'pipe'] })
      : spawn(process.execPath, argumentsList,
        { shell: false, detached: true, stdio: ['ignore', 'pipe', 'pipe'] });
    let failed = false;
    const kill = () => {
      if (child.pid) {
        try { process.kill(-child.pid, 'SIGKILL'); }
        catch (error) { if ((error as NodeJS.ErrnoException).code !== 'ESRCH') failed = true; }
      }
    };
    signal.addEventListener('abort', kill, { once: true });
    if (signal.aborted) kill();
    const chunks: Buffer[] = [];
    let size = 0;
    const collect = (chunk: Buffer) => {
      size += chunk.length;
      if (size > 65_536) { failed = true; kill(); }
      else chunks.push(chunk);
    };
    child.stdout.on('data', collect);
    child.stderr.on('data', collect);
    child.once('error', () => {
      signal.removeEventListener('abort', kill);
      reject(new Error('diagnostic executable unavailable'));
    });
    child.once('close', code => {
      signal.removeEventListener('abort', kill);
      kill();
      if (signal.aborted || failed) return reject(new Error('diagnostic cancelled or output exceeded bound'));
      const output = Buffer.concat(chunks);
      if (probe !== 'aws_cli') {
        try {
          const value = qualificationObservation.parse({ probe, executed: true, ...JSON.parse(output.toString('utf8')) });
          if (code !== 0) throw new Error();
          resolve(value);
        } catch { reject(new Error('diagnostic output invalid')); }
        return;
      }
      const denied = /Unable to locate credentials|No credentials|AccessDenied|UnauthorizedOperation/.test(output.toString('utf8'));
      resolve({ probe, executed: true, outcome: code === 0 ? 'allowed' : denied ? 'denied' : 'error',
        exposure_digests: code === 0 ? [hash(output)] : [] });
    });
  });
}

export class QualificationDiagnostics {
  private child?: ChildProcess;
  private created = false;
  private readonly marker: string;
  private readonly filename: string;

  constructor(private readonly binding: QualificationBinding) {
    this.marker = `adp-e45-residue=${hash(binding.evaluation_id)}`;
    this.filename = `/tmp/adp-e45-${hash(binding.evaluation_id)}`;
  }

  async execute(probe: string, signal: AbortSignal, fixtures?: QualificationFixtures): Promise<QualificationObservation | undefined> {
    signal.throwIfAborted();
    if (probe === 'environment') {
      return exposure(probe, Object.entries(process.env).map(([key, value]) => `${key}=${value ?? ''}`).filter(value => sensitive.test(value)));
    }
    if (probe === 'proc') {
      const values: string[] = [];
      for (const processId of await processes()) {
        signal.throwIfAborted();
        const content = await boundedFile(`/proc/${processId}/environ`);
        values.push(...(content?.toString('utf8').split('\0').filter(value => sensitive.test(value)) ?? []).slice(0, 4 - values.length));
        if (values.length === 4) break;
      }
      return exposure(probe, values);
    }
    if (probe === 'projected_files') {
      const values: Buffer[] = [];
      for (const filename of projected) {
        signal.throwIfAborted();
        const content = await boundedFile(filename);
        if (content?.length) values.push(content);
      }
      return exposure(probe, values);
    }
    if (probe === 'aws_cli' || probe === 'aws_provider' || probe === 'sts_identity') return awsProbe(probe, signal);
    if (probe === 's3_read' || probe === 'dynamodb_read' || probe === 'secrets_read') {
      if (!fixtures) return undefined;
      return awsProbe(probe, signal, qualificationFixtures.parse(fixtures));
    }
    if (['metadata', 'kubernetes_api', 'network_routes'].includes(probe)) {
      const [host, port] = probe === 'metadata' ? ['169.254.169.254', 80] as const :
        probe === 'kubernetes_api' ? ['kubernetes.default.svc', 443] as const : ['example.com', 443] as const;
      const allowed = await reachable(host, port, signal);
      return { probe, executed: true, outcome: allowed ? 'allowed' : 'denied', exposure_digests: [] };
    }
    if (probe === 'sandbox_identity') {
      if (this.binding.actor === 'a1') {
        const handle = await open(this.filename, constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL | constants.O_NOFOLLOW, 0o600);
        this.created = true;
        try { await handle.writeFile(this.marker); } finally { await handle.close(); }
        this.child = spawn(process.execPath, ['-e', 'setInterval(() => {}, 1000)', this.marker], { shell: false, stdio: 'ignore' });
        await new Promise<void>((resolve, reject) => { this.child!.once('spawn', resolve); this.child!.once('error', reject); });
        this.child.unref();
      }
      return { probe, executed: true, outcome: 'distinct', exposure_digests: [] };
    }
    if (probe === 'residue_file') return exposure(probe, (await boundedFile(this.filename))?.length ? [this.marker] : []);
    if (probe === 'residue_process') {
      const values: string[] = [];
      for (const processId of await processes()) {
        signal.throwIfAborted();
        if ((await boundedFile(`/proc/${processId}/cmdline`))?.toString('utf8').split('\0').includes(this.marker)) values.push(this.marker);
      }
      return exposure(probe, values);
    }
    return undefined;
  }

  async cleanup(leaveForTeardown = false): Promise<void> {
    if (leaveForTeardown) return;
    try {
      if (this.child && this.child.exitCode === null && this.child.signalCode === null) {
        const child = this.child;
        await new Promise<void>((resolve, reject) => {
          const timer = setTimeout(() => reject(new Error('diagnostic process cleanup unverified')), 1000);
          child.once('exit', () => { clearTimeout(timer); resolve(); });
          try { child.kill('SIGKILL'); }
          catch { clearTimeout(timer); reject(new Error('diagnostic process cleanup unverified')); }
        });
      }
    } finally {
      if (this.created) { await unlink(this.filename); this.created = false; }
    }
  }
}
